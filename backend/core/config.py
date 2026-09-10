"""
Configuración de la instancia, del lado del servidor.

La configuración es del servidor, no del cliente. Guardarla en el navegador de
cada operador significaba que no se sincronizaba entre máquinas, que nadie
avisaba que era así, y que se perdía al limpiar el navegador: con varios bots en
la red, cada uno tenía una configuración distinta e invisible.

Vive en la tabla `settings`, no en un archivo suelto. Un archivo al lado de la
base significa dos backups, dos formatos y dos lugares donde mirar cuando algo
no coincide; y el día que la base viva en otra parte, la mitad de la instalación
se habría quedado en el disco local.

Las claves son las que declaran los plugins como `Setting`. El núcleo no conoce
ninguna — ni siquiera cuáles son `secret`: eso se lo preguntan desde afuera en
cada escritura (`secret_keys`, ver `ToolRegistry.secret_setting_keys`), y acá
sólo se cifra lo que esa función marque, sin construir una noción propia de
qué plugin declara qué (issue #8).
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Iterable

from .jsonio import dumps, loads
from .ports import CryptoPort, PortError, StoragePort
from .schema import LOCAL_ORG

# Prefijo de las variables de entorno que pisan la configuración guardada.
ENV_PREFIX = "BOT_"

# Marca de un valor cifrado, para distinguirlo de un dict cualquiera que un
# plugin guardó como valor de un Setting. Ver `_envolver`/`_desenvolver`.
_CLAVE_CIFRADO = "__secret__"


class ConfigError(Exception):
    """Operación inválida sobre la configuración."""


class ConfigStore:
    """
    Configuración en la tabla `settings`, una fila por clave.

    Una fila por clave y no un blob con todo: dos pestañas guardando settings
    distintos al mismo tiempo se pisaban entera la una a la otra, y con filas
    cada `UPDATE` toca sólo lo suyo.
    """

    def __init__(
        self,
        db: StoragePort,
        crypto: CryptoPort | None = None,
        *,
        secret_keys: Callable[[], Iterable[str]] | None = None,
        org: str = LOCAL_ORG,
    ) -> None:
        self.db = db
        self.org = org
        # El cifrado entra por un port, igual que en EnvStore. `secret_keys`
        # es una función y no un set fijo (issue #8): qué Setting es
        # `secret` lo declaran los plugins, y `ConfigStore` no los conoce —
        # sólo le preguntan, en cada escritura, cuáles hay que cifrar.
        self.crypto = crypto
        self._secret_keys = secret_keys or (lambda: ())
        self._cache: dict | None = None

    def _cifrar(self, valor: Any) -> str:
        if self.crypto is None:
            raise ConfigError(
                "no hay cifrado configurado para guardar un setting secreto"
            )
        try:
            return self.crypto.encrypt(dumps(valor))
        except PortError as exc:
            raise ConfigError(str(exc)) from exc

    def _descifrar(self, cifrado: str) -> Any:
        """El valor original, o None si no se puede descifrar (otra llave)."""
        try:
            return loads(self.crypto.decrypt(cifrado), None)
        except Exception:
            return None

    def _envolver(self, valor: Any) -> dict:
        return {_CLAVE_CIFRADO: self._cifrar(valor)}

    def _desenvolver(self, valor: Any) -> Any:
        """El valor en claro si venía cifrado; `valor` tal cual si no."""
        if isinstance(valor, dict) and set(valor) == {_CLAVE_CIFRADO}:
            return self._descifrar(valor[_CLAVE_CIFRADO])
        return valor

    # ── Lectura ─────────────────────────────────────────────────────────

    def read(self) -> dict:
        """Configuración efectiva: lo guardado, con el entorno por encima."""
        if self._cache is not None:
            return dict(self._cache)

        datos = self._stored()
        datos.update(_from_env())
        self._cache = datos
        return dict(datos)

    def _stored(self) -> dict:
        """
        Lo que hay en la base, sin el entorno encima y ya descifrado.

        Descifrar acá y no en un método aparte es lo que hace que `read()` —
        y por lo tanto `effective_config()`, que es lo que arma `ctx.config`
        para un tool— vea siempre el valor en claro, sin que quien llama
        tenga que saber qué claves son secretas.
        """
        filas = self.db.query(
            "SELECT key, value FROM settings WHERE org = ? ORDER BY key", (self.org,)
        )
        # Un valor corrupto no tumba la lectura: el doctor reporta lo que falte.
        return {f["key"]: self._desenvolver(loads(f["value"], None)) for f in filas}

    def get(self, key: str, default: Any = None) -> Any:
        return self.read().get(key, default)

    # ── Escritura ───────────────────────────────────────────────────────

    def write(self, values: dict) -> dict:
        """Reemplaza la configuración completa."""
        self._save(dict(values), reemplazar=True)
        return self.read()

    def update(self, values: dict) -> dict:
        """Mergea las claves dadas, dejando el resto como está."""
        self._save(dict(values), reemplazar=False)
        return self.read()

    def delete(self, key: str) -> dict:
        self.db.execute(
            "DELETE FROM settings WHERE org = ? AND key = ?", (self.org, key)
        )
        self._cache = None
        return self.read()

    def _save(self, datos: dict, *, reemplazar: bool) -> None:
        # Una clave pisada por el entorno conserva su valor guardado. Sin esto,
        # guardar cualquier cosa persistía el override del entorno como si
        # alguien lo hubiera escrito: el `BOT_*` de una máquina se filtraba a la
        # configuración que comparten todas.
        del_entorno = _from_env()
        guardado = self._stored()
        persistible = {
            k: v
            for k, v in datos.items()
            if k not in del_entorno
        }

        secretas = set(self._secret_keys())
        ahora = time.time()
        filas = [
            (
                self.org,
                clave,
                dumps(self._envolver(valor) if clave in secretas else valor),
                ahora,
            )
            for clave, valor in persistible.items()
        ]
        if filas:
            self.db.executemany(
                "INSERT INTO settings (org, key, value, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (org, key) DO UPDATE SET"
                "   value = excluded.value, updated_at = excluded.updated_at",
                filas,
            )

        if reemplazar:
            # Lo que ya no está en `datos` se va, salvo lo que sólo existe
            # porque el entorno lo pisa: eso nunca estuvo guardado.
            sobran = [
                k for k in guardado if k not in datos and k not in del_entorno
            ]
            if sobran:
                marcas = ", ".join("?" * len(sobran))
                self.db.execute(
                    f"DELETE FROM settings WHERE org = ? AND key IN ({marcas})",
                    (self.org, *sobran),
                )

        self._cache = None

    def invalidate(self) -> None:
        """Olvida la caché; la próxima lectura vuelve a la base."""
        self._cache = None


def _from_env() -> dict:
    """
    Variables `BOT_<CLAVE>` que pisan la configuración guardada.

    Sirve para lo que es propio de cada máquina y no debería viajar en una base
    compartida. No se persisten al guardar.
    """
    return {
        key[len(ENV_PREFIX) :]: value
        for key, value in os.environ.items()
        if key.startswith(ENV_PREFIX) and len(key) > len(ENV_PREFIX)
    }
