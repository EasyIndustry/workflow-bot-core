"""
Adapters — las implementaciones concretas de los ports.

Es la única capa del repo con derecho a importar una librería externa o de
sistema. La regla del checklist de arquitectura ("¿este código importa una
librería fuera de `adapters/`? → mover") se verifica sobre este directorio.

Un adapter no conoce el motor de flujos, ni el contrato de plugins, ni el
registry. Sólo importa `core.ports`, que es la forma que tiene que cumplir. La
dirección de las llamadas es Core → Plugin → Adapter, siempre.

Elegir qué adapter se usa es responsabilidad de quien arma la instalación
(`core/instance.py`), no del plugin. Por eso `build_default_adapters()` está
acá, junta y explícita: cambiar de `urllib` a `requests` es escribir
`http_requests.py` al lado y cambiar una línea de este archivo.
"""

from __future__ import annotations

from backend.core import ports

from .clock_system import SystemClockAdapter
from .crypto_fernet import FernetCryptoAdapter
from .fs_local import LocalFsAdapter
from .http_urllib import UrllibHttpAdapter
from .process_subprocess import SubprocessAdapter
from .storage_sqlite import IN_MEMORY, SqliteStorageAdapter


def build_default_adapters(
    *,
    fs_root: str | None = None,
    http_timeout: float | None = None,
    process_timeout: float | None = None,
    process_allowlist: list[str] | None = None,
) -> dict[str, object]:
    """
    Los adapters que se le dan al registry, por nombre de port.

    Los parámetros acotan la superficie de riesgo de una instalación concreta:
    `fs_root` encierra el filesystem en un subárbol y `process_allowlist` limita
    qué ejecutables se pueden correr. Los dos son opcionales porque una
    instalación legítima puede necesitar el disco entero, pero un entorno de
    test o acotado debería usarlos.
    """
    return {
        ports.HTTP: UrllibHttpAdapter(
            **({"default_timeout": http_timeout} if http_timeout else {})
        ),
        ports.FS: LocalFsAdapter(root=fs_root),
        ports.PROCESS: SubprocessAdapter(
            **({"default_timeout": process_timeout} if process_timeout else {}),
            allowlist=process_allowlist,
        ),
        ports.CLOCK: SystemClockAdapter(),
    }


__all__ = [
    "IN_MEMORY",
    "FernetCryptoAdapter",
    "LocalFsAdapter",
    "SqliteStorageAdapter",
    "SubprocessAdapter",
    "SystemClockAdapter",
    "UrllibHttpAdapter",
    "build_default_adapters",
]
