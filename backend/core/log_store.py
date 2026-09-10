"""
El registro de eventos de cada fila de una fuente.

Es la trazabilidad que se usa todos los días: en la grilla, el botón **Log** de
una fila abre todo lo que le pasó a esa fila, run tras run. La app vieja lo
tenía y funcionaba; se mantiene igual de comportamiento y se cambia sólo dónde
vive.

**Qué es distinto del `trace` de un run.** El trace es el registro estructurado
de un run: un nodo, sus params resueltos, su estado. Sirve para depurar *una*
ejecución. El log es la narración en líneas —"Decisión stage = Produccion",
"RetryGate:export intento 2/5"— y **se acumula por fila**, cruzando runs. Son
dos cosas y las dos hacen falta; el trace vive en `runs.data`, el log acá.

**Por qué una fila por línea y no un JSON por run.** La pantalla pide "todo el
log de esta fila": con un blob por run habría que abrir y parsear todos los runs
del caso para armarlo. Con filas es una consulta por índice. Y limpiar es un
DELETE con WHERE en vez de reescribir blobs — que es justo lo que hacía el
modelo viejo, reescribiendo el archivo entero del caso en **cada** línea.

**Ráfagas.** Un run de 30 nodos escribe decenas de líneas de una. Van todas en
un `executemany` dentro de una transacción, después de que el run terminó: una
escritura por run y no una por línea.

**Limpieza.** Tres modos, y los tres existen porque sirven para cosas
distintas: a mano cuando alguien quiere empezar de cero en una fila; por
antigüedad para que la base no crezca sin techo; y "al desaparecer de la fuente"
porque un caso que ya no está en la bandeja no se va a volver a mirar, y es el
que más ocupa sin aportar nada.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from .ports import StoragePort
from .schema import LOCAL_ORG

# Modos de limpieza. `manual` no borra nada solo: es el default, porque perder
# la evidencia de un fallo sin haberlo pedido es peor que ocupar disco.
MODO_MANUAL = "manual"
MODO_EXPIRACION = "expiration"
MODO_HUERFANOS = "orphan"
MODOS = (MODO_MANUAL, MODO_EXPIRACION, MODO_HUERFANOS)


@dataclass(frozen=True)
class LogLine:
    id: int
    run_id: str
    case_id: str
    source: str
    flow: str
    ts: float
    level: str
    node_id: str | None
    message: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            # `t` derivado de `ts`, para que una línea leída de la tabla tenga
            # la misma forma que una recién salida del motor. `ts` es la verdad;
            # `t` es la hora ya formateada, que es lo que se copia y se lee.
            "t": time.strftime("%H:%M:%S", time.localtime(self.ts)) if self.ts else "",
            "run_id": self.run_id,
            "case_id": self.case_id,
            "source": self.source,
            "flow": self.flow,
            "ts": self.ts,
            "level": self.level,
            "node_id": self.node_id,
            "message": self.message,
        }


class LogStore:
    def __init__(self, db: StoragePort, org: str = LOCAL_ORG) -> None:
        self.db = db
        self.org = org

    # ── Escritura ───────────────────────────────────────────────────────

    def append_run(self, result, *, flow: str = "", source: str = "") -> int:
        """
        Vuelca las líneas de un `RunResult` en una sola escritura.

        Devuelve cuántas guardó. Un run sin líneas no toca la base.
        """
        entradas = list(getattr(result, "logs", None) or [])
        if not entradas:
            return 0

        ahora = time.time()
        filas = [
            (
                self.org,
                result.run_id,
                str(result.case_id or ""),
                source,
                flow,
                getattr(entrada, "ts", None) or ahora,
                getattr(entrada, "level", "info") or "info",
                getattr(entrada, "node_id", None),
                getattr(entrada, "message", ""),
            )
            for entrada in entradas
        ]
        self.db.executemany(
            "INSERT INTO run_logs (org, run_id, case_id, source, flow, ts, level, node_id, message)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            filas,
        )
        return len(filas)

    # ── Lectura ─────────────────────────────────────────────────────────

    def for_case(self, case_id: str, limit: int = 500) -> list[LogLine]:
        """
        Todo el log de una fila, del más viejo al más nuevo.

        `limit` corta por las **últimas** líneas y no por las primeras: cuando
        algo falla, lo que hay que leer es el final.
        """
        filas = self.db.query(
            "SELECT * FROM run_logs WHERE org = ? AND case_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (self.org, str(case_id), max(1, limit)),
        )
        return [_linea(f) for f in reversed(filas)]

    def for_run(self, run_id: str) -> list[LogLine]:
        filas = self.db.query(
            "SELECT * FROM run_logs WHERE org = ? AND run_id = ? ORDER BY id",
            (self.org, run_id),
        )
        return [_linea(f) for f in filas]

    def count_for_case(self, case_id: str) -> int:
        fila = self.db.one(
            "SELECT COUNT(*) AS n FROM run_logs WHERE org = ? AND case_id = ?",
            (self.org, str(case_id)),
        )
        return fila["n"] if fila else 0

    def counts_for_cases(self, case_ids: Iterable[str]) -> dict[str, int]:
        """
        Cuántas líneas tiene cada fila, en **una** consulta.

        La grilla necesita saber qué filas tienen log para no ofrecer un botón
        que abre un modal vacío. Una consulta por fila serían 128 consultas para
        dibujar una pantalla.
        """
        ids = [str(c) for c in case_ids if str(c)]
        if not ids:
            return {}
        marcas = ",".join("?" * len(ids))
        filas = self.db.query(
            f"SELECT case_id, COUNT(*) AS n FROM run_logs"  # noqa: S608 - marcas son placeholders
            f" WHERE org = ? AND case_id IN ({marcas}) GROUP BY case_id",
            [self.org, *ids],
        )
        return {f["case_id"]: f["n"] for f in filas}

    # ── Limpieza ────────────────────────────────────────────────────────

    def clear_case(self, case_id: str) -> int:
        """Lo que hace el botón "Limpiar" del modal. Devuelve cuántas borró."""
        afectadas = self.db.execute(
            "DELETE FROM run_logs WHERE org = ? AND case_id = ?", (self.org, str(case_id))
        )
        return afectadas

    def prune_older_than(self, days: float) -> int:
        if days <= 0:
            return 0
        limite = time.time() - days * 86400
        afectadas = self.db.execute(
            "DELETE FROM run_logs WHERE org = ? AND ts < ?", (self.org, limite)
        )
        return afectadas

    def prune_orphans(self, source: str, case_ids_vivos: Iterable[str]) -> int:
        """
        Borra el log de las filas de una fuente que ya no están en la fuente.

        Se llama al leer la fuente, que es el único momento en que se sabe qué
        filas siguen vivas. Con la lista vacía **no borra nada**: una fuente que
        falló al leer devuelve cero filas, y tomar eso por "no queda ninguna"
        borraría todo el historial por un error de red.
        """
        vivos = [str(c) for c in case_ids_vivos if str(c)]
        if not source or not vivos:
            return 0
        marcas = ",".join("?" * len(vivos))
        afectadas = self.db.execute(
            f"DELETE FROM run_logs WHERE org = ? AND source = ?"  # noqa: S608
            f" AND case_id NOT IN ({marcas})",
            [self.org, source, *vivos],
        )
        return afectadas

    def prune_keeping(self, max_per_case: int) -> int:
        """
        Deja las últimas N líneas de cada fila.

        Es el techo duro: un flujo con un reintento largo puede escribir cientos
        de líneas de una, y sin esto una sola fila se come la base. El modelo
        viejo tenía el mismo tope, en 200, aplicado en memoria.
        """
        if max_per_case <= 0:
            return 0
        afectadas = self.db.execute(
            "DELETE FROM run_logs WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY case_id ORDER BY id DESC"
            "    ) AS fila FROM run_logs WHERE org = ?"
            "  ) WHERE fila > ?"
            ")",
            (self.org, max_per_case),
        )
        return afectadas

    def total(self) -> int:
        fila = self.db.one("SELECT COUNT(*) AS n FROM run_logs WHERE org = ?", (self.org,))
        return fila["n"] if fila else 0


def _linea(fila) -> LogLine:
    return LogLine(
        id=fila["id"],
        run_id=fila["run_id"],
        case_id=fila["case_id"],
        source=fila["source"],
        flow=fila["flow"],
        ts=fila["ts"],
        level=fila["level"],
        node_id=fila["node_id"],
        message=fila["message"],
    )
