"""
`StoragePort` sobre SQLite.

SQLite y no Postgres ni MySQL: los dos son **servidores**, y una máquina de
producción no debería necesitar instalar, mantener y respaldar uno. SQLite viene
en la stdlib —cero instalación— y tiene JSON1, así que guarda y consulta JSON
libre sin perder nada frente a JSONB.

Este archivo es el único del repo que importa `sqlite3`. El esquema no vive acá
sino en `core/schema.py`: el modelo de datos es del núcleo, el driver es del
adapter. `migrate()` recibe el esquema y lo aplica.

Devuelve `dict`, no `sqlite3.Row`. Parece un detalle y es el que sostiene todo:
si los stores recibieran filas del driver, el núcleo quedaría atado a él por la
puerta de atrás aunque el import estuviera acá.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from backend.core.ports import PortError

# Ruta especial de SQLite para una base que vive en RAM. Es lo que hace que la
# suite corra sin tocar el disco ni dejar archivos atrás.
IN_MEMORY = ":memory:"

# Nombre de tabla válido para interpolar en un PRAGMA (ver `columns`).
_IDENTIFICADOR_SQL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqliteStorageAdapter:
    """
    Base local, con migraciones por dominio.

    Una conexión por hilo: SQLite no permite compartir una entre hilos. WAL para
    que leer no bloquee escribir.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._memoria = self.path == IN_MEMORY
        if not self._memoria:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Una base en memoria vive mientras viva su conexión, así que no puede
        # ser por hilo: sería una base distinta por hilo. Se comparte una sola.
        self._compartida: sqlite3.Connection | None = None
        self._migrate_lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._memoria:
            if self._compartida is None:
                self._compartida = self._abrir()
            return self._compartida

        conexion = getattr(self._local, "conn", None)
        if conexion is None:
            conexion = self._abrir()
            self._local.conn = conexion
        return conexion

    def _abrir(self) -> sqlite3.Connection:
        conexion = sqlite3.connect(
            self.path,
            timeout=30.0,
            check_same_thread=not self._memoria,
        )
        conexion.row_factory = sqlite3.Row
        if not self._memoria:
            conexion.execute("PRAGMA journal_mode=WAL")
        conexion.execute("PRAGMA foreign_keys=ON")
        # Espera en vez de fallar cuando otro hilo está escribiendo.
        conexion.execute("PRAGMA busy_timeout=30000")
        return conexion

    # ── Migraciones ─────────────────────────────────────────────────────

    def migrate(
        self, schema: dict[str, int], migrations: dict[str, dict[int, str]]
    ) -> dict[str, int]:
        """Lleva cada dominio a su versión objetivo. Idempotente."""
        with self._migrate_lock:
            conexion = self.conn
            conexion.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " domain TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            aplicadas: dict[str, int] = {}
            for dominio, objetivo in schema.items():
                fila = conexion.execute(
                    "SELECT version FROM schema_version WHERE domain = ?", (dominio,)
                ).fetchone()
                actual = fila["version"] if fila else 0

                for version in range(actual + 1, objetivo + 1):
                    sql = migrations.get(dominio, {}).get(version)
                    if sql is None:
                        raise PortError(
                            f"Falta la migración {dominio} v{version}: el esquema declara "
                            f"v{objetivo} pero no hay cómo llegar"
                        )
                    conexion.executescript(sql)

                if actual != objetivo:
                    conexion.execute(
                        "INSERT INTO schema_version (domain, version) VALUES (?, ?) "
                        "ON CONFLICT (domain) DO UPDATE SET version = excluded.version",
                        (dominio, objetivo),
                    )
                    aplicadas[dominio] = objetivo
            conexion.commit()
            return aplicadas

    def versions(self) -> dict[str, int]:
        try:
            filas = self.conn.execute("SELECT domain, version FROM schema_version").fetchall()
        except sqlite3.Error:
            return {}
        return {f["domain"]: f["version"] for f in filas}

    # ── Acceso ──────────────────────────────────────────────────────────

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        try:
            filas = self.conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise PortError(f"consulta fallida: {exc}") from exc
        return [dict(f) for f in filas]

    def one(self, sql: str, params: Iterable = ()) -> dict | None:
        try:
            fila = self.conn.execute(sql, tuple(params)).fetchone()
        except sqlite3.Error as exc:
            raise PortError(f"consulta fallida: {exc}") from exc
        return dict(fila) if fila is not None else None

    def execute(self, sql: str, params: Iterable = ()) -> int:
        try:
            cursor = self.conn.execute(sql, tuple(params))
            self.conn.commit()
        except sqlite3.Error as exc:
            raise PortError(f"escritura fallida: {exc}") from exc
        return cursor.rowcount

    def executemany(self, sql: str, rows: Iterable[Iterable]) -> int:
        """
        Varias filas en una transacción.

        Existe para las ráfagas: un run de treinta nodos escribe decenas de
        líneas de log de una, y un commit por línea las convierte en decenas de
        fsync.
        """
        try:
            cursor = self.conn.executemany(sql, [tuple(f) for f in rows])
            self.conn.commit()
        except sqlite3.Error as exc:
            raise PortError(f"escritura fallida: {exc}") from exc
        return cursor.rowcount

    def columns(self, table: str) -> list[str]:
        # PRAGMA no acepta parámetros posicionales como el resto de las
        # sentencias -- `table` va interpolado sí o sí. Validar que sea un
        # identificador simple antes de armar el string es lo que impide que
        # esto sea una puerta de inyección si algún día `table` deja de venir
        # sólo de `schema.SCHEMA` y empieza a viajar más cerca de un input.
        if not _IDENTIFICADOR_SQL.match(table):
            raise PortError(f"nombre de tabla inválido: {table!r}")
        try:
            filas = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error as exc:
            raise PortError(f"no se pudieron leer las columnas de {table!r}: {exc}") from exc
        return [f["name"] for f in filas]

    def close(self) -> None:
        if self._memoria:
            if self._compartida is not None:
                self._compartida.close()
                self._compartida = None
            return
        conexion = getattr(self._local, "conn", None)
        if conexion is not None:
            conexion.close()
            self._local.conn = None


__all__ = ["IN_MEMORY", "SqliteStorageAdapter"]
