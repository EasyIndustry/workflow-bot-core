"""
`SqliteFilePort` sobre `sqlite3`.

Mismo driver que `storage_sqlite.py`, pero sin schema propio ni migraciones:
abre cualquier archivo `.sqlite`/`.db` que el flujo indique por `path`, siempre
de sólo lectura -- vía el modo `ro` de la URI de `sqlite3`, no una convención
que un SQL de escritura pudiera esquivar.

Sin estado entre llamadas a propósito: cada `query`/`one`/`columns` abre y
cierra su propia conexión. Más simple que cachear conexiones por `path`, y el
costo de abrir un SQLite de sólo lectura es bajo -- si en el futuro hiciera
falta cachear, es un cambio interno a este adapter, no al port.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable

from backend.core.ports import PortError

# Nombre de tabla válido para interpolar en un PRAGMA (ver `columns`), mismo
# criterio que `storage_sqlite.py`.
_IDENTIFICADOR_SQL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExternalSqliteAdapter:
    """Lectura de sólo lectura contra un archivo SQLite externo, por path."""

    def _conectar(self, path: str) -> sqlite3.Connection:
        path = (path or "").strip()
        if not path:
            raise PortError("no se indicó un path")
        try:
            # `uri=True` + `mode=ro` abre de sólo lectura a nivel del propio
            # SQLite: un `INSERT`/`UPDATE` falla con "attempt to write a
            # readonly database", no depende de que el SQL del plugin se
            # porte bien.
            conexion = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conexion.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise PortError(f"no se pudo abrir {path!r}: {exc}") from exc
        return conexion

    def query(self, path: str, sql: str, params: Iterable = ()) -> list[dict]:
        conexion = self._conectar(path)
        try:
            filas = conexion.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise PortError(f"consulta fallida: {exc}") from exc
        finally:
            conexion.close()
        return [dict(f) for f in filas]

    def one(self, path: str, sql: str, params: Iterable = ()) -> dict | None:
        conexion = self._conectar(path)
        try:
            fila = conexion.execute(sql, tuple(params)).fetchone()
        except sqlite3.Error as exc:
            raise PortError(f"consulta fallida: {exc}") from exc
        finally:
            conexion.close()
        return dict(fila) if fila is not None else None

    def columns(self, path: str, table: str) -> list[str]:
        if not _IDENTIFICADOR_SQL.match(table):
            raise PortError(f"nombre de tabla inválido: {table!r}")
        conexion = self._conectar(path)
        try:
            filas = conexion.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error as exc:
            raise PortError(f"no se pudieron leer las columnas de {table!r}: {exc}") from exc
        finally:
            conexion.close()
        return [f["name"] for f in filas]


__all__ = ["ExternalSqliteAdapter"]
