"""
Almacenamiento genérico de los Resources de un plugin.

Un plugin declara qué colecciones administra y con qué esquema; el núcleo se
encarga de guardarlas y de servir el ABM. Ningún plugin escribe una vista ni
una ruta HTTP propia.

Backend: la tabla `plugin_items`, una fila por item. Que sea una tabla y no un
archivo por item en una carpeta que el plugin elige es lo que hace posible tres
cosas:

- **Que exista un lugar donde poner una política.** Cifrar un campo `secret`,
  registrar quién escribió o filtrar por organización se hace una vez, acá, y
  vale para todas las colecciones de todos los plugins.
- **Saber cuándo cambió un item** (`updated_at`), que una carpeta de archivos no
  dice.
- **Un solo backup.** La configuración de la instalación no queda partida entre
  la base y carpetas sueltas, con el riesgo de restaurar una sin la otra.

El plugin declara `Resource.fields` y no sabe dónde viven sus datos. Por eso
cambiar el backend no obliga a tocar un solo plugin.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .contract import Resource
from .jsonio import dumps, loads
from .ports import CryptoPort, PortError, StoragePort
from .schema import LOCAL_ORG

# La clave sigue restringida aunque ya no sea un nombre de archivo: viaja en la
# URL del ABM (`/resources/{plugin}/{resource}/{key}`) y es lo que un flujo
# escribe a mano en un param (`connection=CASOS BOT`).
_KEY_RE = re.compile(r"^[\w\- ]+$")

# Marca de un valor de campo cifrado, mismo criterio que `config.py`.
_CLAVE_CIFRADO = "__secret__"


class ResourceError(Exception):
    """Operación inválida sobre un resource."""


def validate_key(key: str) -> str:
    key = (key or "").strip()
    if not key or not _KEY_RE.match(key):
        raise ResourceError(
            f'Nombre inválido: "{key}" (solo letras, números, guiones y espacios)'
        )
    return key


class TableStore:
    """Una colección persistida en `plugin_items`, una fila por item."""

    def __init__(
        self,
        db: StoragePort,
        plugin: str,
        resource: Resource,
        crypto: CryptoPort | None = None,
        org: str = LOCAL_ORG,
    ) -> None:
        self.db = db
        self.plugin = plugin
        self.resource = resource
        # El cifrado entra por un port, igual que en EnvStore/ConfigStore: qué
        # campo es `secret` ya lo sabe este store (viene en `resource.fields`,
        # que ya recibe), así que acá no hace falta preguntarle a nadie más
        # (issue #8).
        self.crypto = crypto
        self.org = org

    @property
    def _scope(self) -> tuple[str, str, str]:
        return (self.org, self.plugin, self.resource.name)

    def _cifrar(self, valor: Any) -> str:
        if self.crypto is None:
            raise ResourceError(
                f'no hay cifrado configurado para guardar un campo secreto de "{self.resource.label}"'
            )
        try:
            return self.crypto.encrypt(dumps(valor))
        except PortError as exc:
            raise ResourceError(str(exc)) from exc

    def _descifrar(self, cifrado: str) -> Any:
        """El valor original, o None si no se puede descifrar (otra llave)."""
        try:
            return loads(self.crypto.decrypt(cifrado), None)
        except Exception:
            return None

    def _envolver_item(self, item: dict) -> dict:
        """Cifra los campos que el `Resource` declaró `secret`, antes de guardar."""
        resultado = dict(item)
        for campo, valor in item.items():
            declarado = self.resource.field(campo)
            if declarado is not None and declarado.secret and valor not in (None, ""):
                resultado[campo] = {_CLAVE_CIFRADO: self._cifrar(valor)}
        return resultado

    def _desenvolver_item(self, item: dict) -> dict:
        """El item con sus campos secretos ya descifrados, para el plugin."""
        resultado = dict(item)
        for campo, valor in item.items():
            if isinstance(valor, dict) and set(valor) == {_CLAVE_CIFRADO}:
                resultado[campo] = self._descifrar(valor[_CLAVE_CIFRADO])
        return resultado

    def list_keys(self) -> list[str]:
        filas = self.db.query(
            "SELECT key FROM plugin_items"
            " WHERE org = ? AND plugin = ? AND resource = ? ORDER BY key",
            self._scope,
        )
        return [f["key"] for f in filas]

    def list_items(self) -> list[dict]:
        """Todos los items, con su clave incluida. Los ilegibles se marcan."""
        filas = self.db.query(
            "SELECT key, data, updated_at FROM plugin_items"
            " WHERE org = ? AND plugin = ? AND resource = ? ORDER BY key",
            self._scope,
        )
        items = []
        for fila in filas:
            cuerpo = loads(fila["data"], None)
            if not isinstance(cuerpo, dict):
                items.append(
                    {
                        self.resource.key_field: fila["key"],
                        "_error": f'"{fila["key"]}" no es un objeto JSON válido',
                    }
                )
                continue
            items.append(
                {
                    self.resource.key_field: fila["key"],
                    **self._desenvolver_item(cuerpo),
                    "_updated_at": fila["updated_at"],
                }
            )
        return items

    def read(self, key: str) -> dict:
        fila = self.db.one(
            "SELECT data, updated_at FROM plugin_items"
            " WHERE org = ? AND plugin = ? AND resource = ? AND key = ?",
            (*self._scope, validate_key(key)),
        )
        if fila is None:
            raise ResourceError(f'No existe "{key}" en {self.resource.label}')
        cuerpo = loads(fila["data"], None)
        if not isinstance(cuerpo, dict):
            raise ResourceError(f'"{key}" no es un objeto JSON válido')
        return {**self._desenvolver_item(cuerpo), "_updated_at": fila["updated_at"]}

    def write(self, key: str, item: dict) -> dict:
        """Valida contra el esquema del resource y guarda."""
        key = validate_key(key)
        if errores := self.resource.validate_item(item):
            raise ResourceError("; ".join(errores))

        # La clave vive en su columna, no adentro del item. Y `_updated_at` es
        # del núcleo: si volviera adentro del JSON, cada edición desde la UI lo
        # guardaría como si fuera un campo declarado por el plugin.
        cuerpo = {
            k: v
            for k, v in item.items()
            if k != self.resource.key_field and not k.startswith("_")
        }
        ahora = time.time()
        self.db.execute(
            "INSERT INTO plugin_items (org, plugin, resource, key, data, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (org, plugin, resource, key) DO UPDATE SET"
            "   data = excluded.data, updated_at = excluded.updated_at",
            (*self._scope, key, dumps(self._envolver_item(cuerpo)), ahora),
        )
        # Lo que se devuelve es lo que se guardó, en claro: quien acaba de
        # escribir un secreto tiene derecho a ver lo que puso, distinto de
        # `list_items`/`read` de acá en más, que ya vienen de la base.
        return {self.resource.key_field: key, **cuerpo, "_updated_at": ahora}

    def delete(self, key: str) -> None:
        afectadas = self.db.execute(
            "DELETE FROM plugin_items"
            " WHERE org = ? AND plugin = ? AND resource = ? AND key = ?",
            (*self._scope, validate_key(key)),
        )
        if afectadas == 0:
            raise ResourceError(f'No existe "{key}" en {self.resource.label}')

    def exists(self, key: str) -> bool:
        try:
            clave = validate_key(key)
        except ResourceError:
            return False
        return (
            self.db.one(
                "SELECT 1 FROM plugin_items"
                " WHERE org = ? AND plugin = ? AND resource = ? AND key = ?",
                (*self._scope, clave),
            )
            is not None
        )


def store_for(
    db: StoragePort,
    plugin: str,
    resource: Resource,
    crypto: CryptoPort | None = None,
    org: str = LOCAL_ORG,
) -> TableStore:
    """
    Store de un resource. Ya no hay nada que configurar para poder guardar.

    Que un resource tuviera que declarar una carpeta antes de poder guardar
    significaba que no se podía crear un item hasta configurar dónde. Con la
    tabla, una instalación nueva guarda desde el primer minuto.
    """
    return TableStore(db, plugin, resource, crypto, org)
