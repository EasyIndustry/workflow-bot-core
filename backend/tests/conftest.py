"""
Fixtures compartidas.

Todo corre contra una base en memoria y adapters falsos. Ningún test toca la
red, el disco de verdad, un proceso ni el reloj del sistema — salvo los de
`test_adapters.py`, que existen justamente para probar los adapters reales y lo
hacen en un temporal y contra localhost.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# La raíz del repo, para que `backend` sea importable como paquete.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.adapters.crypto_fernet import FernetCryptoAdapter  # noqa: E402
from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter  # noqa: E402
from backend.core.builtins import build_builtin_plugin  # noqa: E402
from backend.core.instance import Instance  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.core.schema import MIGRATIONS, SCHEMA  # noqa: E402
from backend.tests.demo_plugin import build_plugin as build_demo_plugin  # noqa: E402
from backend.tests.fakes import fake_adapters  # noqa: E402

# Corpus de flujos escritos contra la arquitectura vigente. Ver su README.
FLOWS = pathlib.Path(__file__).resolve().parent / "flows"


@pytest.fixture
def db():
    """Base en memoria, ya migrada. Muere con el test."""
    almacen = SqliteStorageAdapter(IN_MEMORY)
    almacen.migrate(SCHEMA, MIGRATIONS)
    yield almacen
    almacen.close()


@pytest.fixture
def crypto(tmp_path):
    """Cifrado real, con la llave en un temporal que muere con el test."""
    return FernetCryptoAdapter(tmp_path / "secret.key")


@pytest.fixture
def adapters():
    """Los ports de plugin, falsos. El test los muta para guionar el mundo."""
    return fake_adapters()


@pytest.fixture
def instance(tmp_path, adapters):
    """
    Una instalación completa y aislada: base en memoria, adapters falsos.

    Es el mismo `Instance` que construiría un servidor o la CLI. Que un test
    pueda armarlo sin ninguna rama especial es la prueba de que la inyección
    funciona.
    """
    inst = Instance(
        tmp_path,
        storage=SqliteStorageAdapter(IN_MEMORY),
        adapters=adapters,
    )
    yield inst
    inst.close()


@pytest.fixture
def registry(adapters):
    """Registro con sólo los builtins del núcleo."""
    reg = ToolRegistry(adapters=adapters)
    reg._add_plugin("core", "builtin", build_builtin_plugin())
    return reg


@pytest.fixture
def demo_registry(adapters):
    """Los builtins más el plugin de prueba, que ejercita los cuatro ports."""
    reg = ToolRegistry(adapters=adapters)
    reg._add_plugin("core", "builtin", build_builtin_plugin())
    reg._add_plugin("demo", "tests.demo_plugin:PLUGIN", build_demo_plugin())
    return reg


@pytest.fixture
def demo_instance(tmp_path, adapters):
    """Una instalación con el plugin de prueba instalado."""
    inst = Instance(tmp_path, storage=SqliteStorageAdapter(IN_MEMORY), adapters=adapters)
    inst.registry._add_plugin("demo", "tests.demo_plugin:PLUGIN", build_demo_plugin())
    for path in sorted(FLOWS.glob("*.mmd")):
        inst.workflows.save_mmd(path.stem, path.read_text(encoding="utf-8"))
    yield inst
    inst.close()


@pytest.fixture
def flows():
    """El corpus de flujos de prueba, como {nombre: texto}."""
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(FLOWS.glob("*.mmd"))}
