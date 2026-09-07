"""
Los dos dominios que el núcleo persiste: workflows y runs.

Son stores con una interfaz angosta —`list`, `get`, `save`, `delete`— por encima
del `StoragePort`. Nadie fuera de acá escribe SQL contra esas tablas, así que
cambiar cómo se guardan es cambiar este archivo y ninguno más.

Los dos son de la máquina. Compartirlos entre varios bots de una organización es
otra etapa; la columna `org` ya está para cuando toque.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .flow.executor import RunResult
from .flow.parser import parse_meta
from .jsonio import dumps, loads
from .ports import StoragePort
from .schema import LOCAL_ORG

_NAME_RE = re.compile(r"^[\w\- ]{1,120}$")


class StoreError(Exception):
    """Operación inválida sobre un store."""


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise StoreError(
            f"Nombre inválido: {name!r} (letras, números, guiones y espacios; hasta 120)"
        )
    return name


# ── Workflows ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Workflow:
    name: str
    content: str
    folder: str = ""
    state: str = "enabled"
    description: str = ""
    updated_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.state != "disabled"

    def to_dict(self, with_content: bool = True) -> dict:
        datos = {
            "name": self.name,
            "folder": self.folder,
            "state": self.state,
            "description": self.description,
            "updated_at": self.updated_at,
        }
        if with_content:
            datos["content"] = self.content
        return datos


class WorkflowStore:
    """
    Workflows en la base.

    Los `.mmd` siguen siendo el formato de intercambio: se importan al arrancar
    y se pueden exportar. Esa ida y vuelta es lo que permite versionarlos en git
    y meterlos en una release, que es la propiedad que hizo elegir Mermaid.
    """

    def __init__(self, db: StoragePort, org: str = LOCAL_ORG) -> None:
        self.db = db
        self.org = org

    def list(self, only_enabled: bool = False) -> list[Workflow]:
        sql = "SELECT * FROM workflows WHERE org = ?"
        params: list[Any] = [self.org]
        if only_enabled:
            sql += " AND state != 'disabled'"
        sql += " COLLATE NOCASE ORDER BY LOWER(name)"
        return [_workflow(f) for f in self.db.query(sql, params)]

    def get(self, name: str) -> Workflow | None:
        fila = self.db.one(
            "SELECT * FROM workflows WHERE org = ? AND name = ?", (self.org, _validate_name(name))
        )
        return _workflow(fila) if fila else None

    def save(
        self,
        name: str,
        content: str,
        folder: str = "",
        state: str = "enabled",
        description: str = "",
    ) -> Workflow:
        name = _validate_name(name)
        self.db.execute(
            "INSERT INTO workflows (org, name, folder, state, description, content, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (org, name) DO UPDATE SET"
            "   folder = excluded.folder, state = excluded.state,"
            "   description = excluded.description, content = excluded.content,"
            "   updated_at = excluded.updated_at",
            (self.org, name, folder, state or "enabled", description, content, time.time()),
        )
        return self.get(name)  # type: ignore[return-value]

    def save_mmd(self, name: str, raw: str) -> Workflow:
        """Guarda un `.mmd` crudo, tomando folder/state/description de su cabecera."""
        meta, contenido = parse_meta(raw)
        return self.save(name, contenido, meta.folder, meta.state, meta.description)

    def delete(self, name: str) -> bool:
        afectadas = self.db.execute(
            "DELETE FROM workflows WHERE org = ? AND name = ?", (self.org, _validate_name(name))
        )
        return afectadas > 0

    def to_mmd(self, name: str) -> str:
        """El workflow con su cabecera `%%`, tal como se guardaría en un archivo."""
        wf = self.get(name)
        if wf is None:
            raise StoreError(f'No existe el flujo "{name}"')
        cabecera = []
        if wf.folder:
            cabecera.append(f"%% folder: {wf.folder}")
        if wf.state and wf.state != "enabled":
            cabecera.append(f"%% state: {wf.state}")
        if wf.description:
            cabecera.append(f"%% description: {wf.description}")
        return ("\n".join(cabecera) + "\n" + wf.content) if cabecera else wf.content

    # ── Importación / exportación ───────────────────────────────────────

    def import_dir(self, directory: Path | str, overwrite: bool = False) -> list[str]:
        """Levanta los `.mmd` de una carpeta. Devuelve los nombres importados."""
        carpeta = Path(directory)
        if not carpeta.is_dir():
            return []
        importados = []
        for path in sorted(carpeta.glob("*.mmd")):
            if not overwrite and self.get(path.stem) is not None:
                continue
            self.save_mmd(path.stem, path.read_text(encoding="utf-8"))
            importados.append(path.stem)
        return importados

    def export_dir(self, directory: Path | str) -> list[str]:
        """Escribe cada workflow como `.mmd`. Para empaquetar una release."""
        carpeta = Path(directory)
        carpeta.mkdir(parents=True, exist_ok=True)
        escritos = []
        for wf in self.list():
            (carpeta / f"{wf.name}.mmd").write_text(self.to_mmd(wf.name), encoding="utf-8")
            escritos.append(wf.name)
        return escritos


def _workflow(fila) -> Workflow:
    return Workflow(
        name=fila["name"],
        content=fila["content"],
        folder=fila["folder"],
        state=fila["state"],
        description=fila["description"],
        updated_at=fila["updated_at"],
    )


# ── Runs ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    case_id: str
    status: str
    flow: str
    source: str
    actor: str
    started_at: float
    finished_at: float
    node_count: int
    failed_node: str | None = None
    message: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "status": self.status,
            "flow": self.flow,
            "source": self.source,
            "actor": self.actor,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "node_count": self.node_count,
            "failed_node": self.failed_node,
            "message": self.message,
            "dry_run": self.dry_run,
        }


class RunStore:
    """
    Historial de ejecuciones.

    Append-only: un run nunca se pisa. El modelo viejo guardaba un JSON por caso
    y lo reescribía entero en cada log, así que reprocesar un caso destruía la
    evidencia del run que había fallado — justo la que hace falta.

    En base y no en archivos porque listar filtrando por caso, estado o fecha
    con un archivo por run obliga a abrirlos todos.
    """

    def __init__(self, db: StoragePort, org: str = LOCAL_ORG) -> None:
        self.db = db
        self.org = org

    def save(
        self,
        result: RunResult,
        flow: str = "",
        source: str = "",
        started_at: float | None = None,
    ) -> dict:
        datos = result.to_dict()
        ahora = time.time()
        datos["flow"] = flow
        datos["source"] = source
        datos["started_at"] = started_at if started_at is not None else ahora
        datos["finished_at"] = ahora

        # El log no va en el blob: vive en `run_logs`, una fila por línea, para
        # poder mostrarlo acumulado por caso y limpiarlo con un DELETE. Ver
        # log_store.py. `Instance.run_detail()` lo vuelve a pegar al leer, así
        # que la forma que ve la API no cambia.
        guardado = {clave: valor for clave, valor in datos.items() if clave != "logs"}

        if self.db.one("SELECT 1 FROM runs WHERE run_id = ?", (result.run_id,)):
            raise StoreError(
                f"El run '{result.run_id}' ya está guardado; los runs no se sobrescriben"
            )

        self.db.execute(
            "INSERT INTO runs (run_id, org, case_id, source, flow, actor, status,"
            " failed_node, message, dry_run, node_count, started_at, finished_at, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.run_id,
                self.org,
                str(result.case_id),
                source,
                flow,
                result.actor,
                result.status,
                result.failed_node,
                result.message,
                1 if result.dry_run else 0,
                len(result.trace),
                datos["started_at"],
                datos["finished_at"],
                dumps(guardado),
            ),
        )
        return datos

    def get(self, run_id: str) -> dict | None:
        fila = self.db.one("SELECT data FROM runs WHERE run_id = ?", (run_id,))
        return loads(fila["data"], None) if fila else None

    def list(
        self,
        case_id: str | None = None,
        limit: int = 50,
        only_failed: bool = False,
        source: str | None = None,
        actor: str | None = None,
        include_dry: bool = True,
    ) -> list[RunSummary]:
        sql = "SELECT * FROM runs WHERE org = ?"
        params: list[Any] = [self.org]
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(str(case_id))
        if actor is not None:
            sql += " AND actor = ?"
            params.append(actor)
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        if only_failed:
            sql += " AND status != 'ok'"
        if not include_dry:
            sql += " AND dry_run = 0"
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        return [_summary(f) for f in self.db.query(sql, params)]

    def last_for_case(self, case_id: str, include_dry: bool = False) -> dict | None:
        filas = self.list(case_id=case_id, limit=1, include_dry=include_dry)
        return self.get(filas[0].run_id) if filas else None

    def latest_by_case(self, case_ids: list[str], include_dry: bool = False) -> dict[str, RunSummary]:
        """
        Último run de cada caso, en una sola consulta.

        Es lo que necesita la tabla para pintar el estado de cada fila sin hacer
        una consulta por fila.
        """
        if not case_ids:
            return {}
        marcas = ",".join("?" for _ in case_ids)
        sql = (
            "SELECT r.* FROM runs r JOIN ("
            f"  SELECT case_id, MAX(started_at) AS m FROM runs"
            f"  WHERE org = ? AND case_id IN ({marcas})"
            + ("" if include_dry else " AND dry_run = 0")
            + "  GROUP BY case_id"
            ") u ON r.case_id = u.case_id AND r.started_at = u.m WHERE r.org = ?"
        )
        params = [self.org, *[str(c) for c in case_ids], self.org]
        return {f["case_id"]: _summary(f) for f in self.db.query(sql, params)}

    def prune(self, keep: int = 5000) -> int:
        """Borra los más viejos pasado un tope. La evidencia vale más que el disco."""
        afectadas = self.db.execute(
            "DELETE FROM runs WHERE org = ? AND run_id NOT IN ("
            "  SELECT run_id FROM runs WHERE org = ? ORDER BY started_at DESC LIMIT ?)",
            (self.org, self.org, int(keep)),
        )
        return afectadas


def _summary(fila) -> RunSummary:
    return RunSummary(
        run_id=fila["run_id"],
        case_id=fila["case_id"],
        status=fila["status"],
        flow=fila["flow"],
        source=fila["source"],
        actor=fila["actor"],
        started_at=fila["started_at"],
        finished_at=fila["finished_at"],
        node_count=fila["node_count"],
        failed_node=fila["failed_node"],
        message=fila["message"],
        dry_run=bool(fila["dry_run"]),
    )
