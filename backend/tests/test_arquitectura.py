"""
La arquitectura, como test.

Las reglas de capas se erosionan solas: alguien agrega un `import requests` en
un módulo del núcleo porque era lo más rápido, nadie lo ve en la revisión, y seis
meses después la capa de ports es decorativa. Estos tests convierten el checklist
de `docs/ARQUITECTURA.md` en algo que falla en CI.

No prueban comportamiento. Prueban que las dependencias apunten en la dirección
correcta: **Core → Plugin → Adapter**, nunca al revés.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# La raíz del backend: todo el paquete vive acá adentro.
RAIZ = pathlib.Path(__file__).resolve().parent.parent
CORE = RAIZ / "core"
ADAPTERS = RAIZ / "adapters"
MCP = RAIZ / "mcp"

# Lo que un módulo del núcleo puede importar además de la stdlib y de sí mismo.
# Está vacío a propósito: el núcleo no tiene dependencias, y que eso siga siendo
# cierto es una propiedad que se defiende, no un accidente.
DEPENDENCIAS_PERMITIDAS_EN_CORE: set[str] = set()

# Módulos de la stdlib que el núcleo usa. Si aparece uno nuevo acá, que sea una
# decisión y no un descuido.
STDLIB_ESPERADA = {
    "__future__", "argparse", "ast", "collections", "contextlib", "dataclasses", "datetime",
    "difflib", "enum", "functools", "importlib", "itertools", "json", "logging",
    "os", "pathlib", "re", "shutil", "socket", "string", "sys", "textwrap",
    "threading", "time", "traceback", "typing", "uuid", "warnings",
}


def _imports(path: pathlib.Path) -> list[tuple[str, int]]:
    """(módulo raíz, línea) de cada import absoluto del archivo."""
    arbol = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    encontrados = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                encontrados.append((alias.name.split(".")[0], nodo.lineno))
        elif isinstance(nodo, ast.ImportFrom):
            # level > 0 es un import relativo: dentro del propio paquete.
            if nodo.level == 0 and nodo.module:
                encontrados.append((nodo.module.split(".")[0], nodo.lineno))
    return encontrados


def _modulos(carpeta: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in carpeta.rglob("*.py") if "__pycache__" not in p.parts)


# ── La regla principal ──────────────────────────────────────────────────


@pytest.mark.parametrize("modulo", _modulos(CORE), ids=lambda p: p.name)
def test_el_nucleo_no_importa_ninguna_libreria_externa(modulo):
    """
    Primer ítem del checklist de arquitectura: si un módulo importa una librería
    fuera de `adapters/`, hay que moverla.

    `backend` cuenta como propio: son los imports internos del paquete, que el
    test siguiente controla por separado.
    """
    permitidos = STDLIB_ESPERADA | {"backend"} | DEPENDENCIAS_PERMITIDAS_EN_CORE

    intrusos = [
        f"{nombre} (línea {linea})"
        for nombre, linea in _imports(modulo)
        if nombre not in permitidos
    ]
    assert not intrusos, (
        f"{modulo.relative_to(RAIZ)} importa algo que no le corresponde: "
        f"{', '.join(intrusos)}. Si es una librería externa, va en adapters/."
    )


def _importa_adapters(modulo: pathlib.Path) -> bool:
    """Si el módulo importa algo de `backend.adapters`, sea como sea."""
    arbol = ast.parse(modulo.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").startswith(
            "backend.adapters"
        ):
            return True
        if isinstance(nodo, ast.Import):
            if any(a.name.startswith("backend.adapters") for a in nodo.names):
                return True
    return False


def test_solo_instance_conoce_el_paquete_de_adapters():
    """
    Elegir la implementación concreta es responsabilidad de quien arma la
    instalación, y de nadie más. Si otro módulo del núcleo importara `adapters`,
    la inyección dejaría de ser el único camino.

    `core/instance.py` es la única excepción, y está acotada: los imports viven
    dentro de las funciones que arman la instalación por defecto, así que un
    test que inyecta sus propios adapters nunca las ejecuta.
    """
    culpables = [
        str(modulo.relative_to(RAIZ))
        for modulo in _modulos(CORE)
        if modulo.name != "instance.py" and _importa_adapters(modulo)
    ]
    assert culpables == []


def test_ningun_adapter_conoce_el_motor():
    """
    Un adapter sólo puede importar `core.ports` —la forma que tiene que
    cumplir—. Si conociera el executor, el contrato de plugins o el registry, la
    dependencia iría en la dirección equivocada y la capa dejaría de ser
    reemplazable.
    """
    for modulo in _modulos(ADAPTERS):
        arbol = ast.parse(modulo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").startswith("core"):
                # `from backend.core.ports import X` y `from backend.core import ports` son la
                # misma cosa escrita distinto; cualquier otra es una fuga.
                trae_solo_ports = nodo.module == "core" and all(
                    a.name == "ports" for a in nodo.names
                )
                assert nodo.module == "core.ports" or trae_solo_ports, (
                    f"{modulo.relative_to(RAIZ)} importa de {nodo.module}; "
                    "un adapter sólo puede conocer core.ports"
                )
            if isinstance(nodo, ast.Import):
                for alias in nodo.names:
                    assert alias.name in ("core.ports",) or not alias.name.startswith("core"), (
                        f"{modulo.relative_to(RAIZ)} importa {alias.name}"
                    )


def test_los_ports_no_importan_nada_mas_que_la_stdlib():
    """Un port es sólo la forma: ni implementación, ni librería, ni núcleo."""
    for nombre, _ in _imports(CORE / "ports.py"):
        assert nombre in STDLIB_ESPERADA, f"core/ports.py importa {nombre}"


# ── Coherencia del catálogo de ports ────────────────────────────────────


def test_cada_port_declarado_tiene_su_adapter_por_defecto():
    """
    Un port sin implementación es una promesa vacía: un plugin que lo declare
    no podría cargar en ninguna instalación.
    """
    from backend.adapters import build_default_adapters
    from backend.core.ports import PLUGIN_PORTS

    disponibles = set(build_default_adapters())
    assert PLUGIN_PORTS <= disponibles, f"sin adapter: {PLUGIN_PORTS - disponibles}"


def test_los_ports_del_nucleo_no_se_le_ofrecen_a_los_plugins():
    """
    `storage` y `crypto` son del núcleo. Un plugin con acceso al almacenamiento
    elegiría dónde persisten sus datos —lo que `Resource` existe para impedir—
    y uno con acceso al cifrado podría leer secretos que no le corresponden.
    """
    from backend.core import ports

    assert ports.STORAGE not in ports.PLUGIN_PORTS
    assert ports.CRYPTO not in ports.PLUGIN_PORTS
    assert ports.PLUGIN_PORTS < set(ports.PORTS)


def test_los_adapters_por_defecto_cumplen_su_port():
    """
    Los `Protocol` son `runtime_checkable`, así que esto se puede afirmar de
    verdad y no confiar en que el nombre del archivo diga la verdad.
    """
    from backend.adapters import build_default_adapters
    from backend.core.ports import PORTS

    for nombre, adapter in build_default_adapters().items():
        assert isinstance(adapter, PORTS[nombre]), f"{nombre}: {type(adapter).__name__}"


def test_los_fakes_cumplen_el_mismo_port_que_los_reales():
    """
    Si un fake no cumpliera el port, la suite estaría probando contra una forma
    que no existe y los tests pasarían mintiendo.
    """
    from backend.core.ports import PORTS
    from backend.tests.fakes import fake_adapters

    for nombre, fake in fake_adapters().items():
        assert isinstance(fake, PORTS[nombre]), f"{nombre}: {type(fake).__name__}"


# ── El núcleo no conoce ninguna integración ─────────────────────────────


@pytest.mark.parametrize("modulo", _modulos(CORE), ids=lambda p: p.name)
def test_el_nucleo_no_nombra_ninguna_integracion(modulo):
    """
    Segundo ítem del checklist: si el core hace `if plugin == X` o menciona un
    proveedor, hay una fuga de lógica de negocio al núcleo.
    """
    texto = modulo.read_text(encoding="utf-8").lower()
    prohibidos = ("xano", "toothform", "oqcam", "noloco", "pywinauto")
    encontrados = [p for p in prohibidos if p in texto]
    assert not encontrados, f"{modulo.relative_to(RAIZ)} menciona: {', '.join(encontrados)}"


# ── Adapters de entrada ─────────────────────────────────────────────────
#
# El doc nombra Core / Port / Adapter / Plugin, pero todos los adapters de
# `adapters/` son de SALIDA: el motor llamando al mundo. La otra dirección —el
# mundo llamando al motor— son la CLI y el servidor MCP. Tienen su propia regla:
# hablan con la fachada, nunca con las tripas.


def test_el_mcp_solo_habla_con_la_fachada():
    """
    El MCP puede conocer `Instance`, `ports` y `contract`. Si empezara a
    importar el executor, el registry o los stores directo, dejaría de haber
    una fachada: habría dos caminos al motor esperando divergir, que es el modo
    de falla contra el que está diseñado todo el repo.
    """
    permitidos = {
        "backend.core",
        "backend.core.instance",
        "backend.core.ports",
        "backend.core.contract",
    }
    intrusos = []
    for modulo in _modulos(MCP):
        for nodo in ast.walk(ast.parse(modulo.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").startswith("backend"):
                if nodo.module not in permitidos:
                    intrusos.append(f"{modulo.name}: {nodo.module}")
    assert not intrusos, (
        f"el MCP importa por debajo de la fachada: {intrusos}. "
        f"Permitido: {', '.join(sorted(permitidos))}"
    )


def test_el_nucleo_no_conoce_al_mcp():
    """
    La dirección de la dependencia. El motor no sabe que existe un servidor
    MCP, igual que no sabe que existe la CLI de nadie más: si lo supiera, no
    se podría sacar.
    """
    culpables = [
        str(modulo.relative_to(RAIZ))
        for modulo in _modulos(CORE) + _modulos(ADAPTERS)
        if any(nombre == "mcp" for nombre, _ in _imports(modulo))
        or any(
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend.mcp")
            for n in ast.walk(ast.parse(modulo.read_text(encoding="utf-8")))
        )
    ]
    assert culpables == []


def test_las_operaciones_del_mcp_no_conocen_el_sdk():
    """
    `operations.py` es la lógica y `server.py` el protocolo. Mezclarlos haría
    que la lógica sólo se pueda probar levantando un servidor — y con el tiempo,
    que deje de probarse.
    """
    for nombre, _ in _imports(MCP / "operations.py"):
        assert nombre != "mcp", "operations.py no puede importar el SDK de MCP"


def test_cada_tool_del_mcp_tiene_handler_y_viceversa():
    """Deriva silenciosa entre lo que se declara y lo que se puede llamar."""
    from backend.mcp.server import HANDLERS, TOOLS

    assert {t.name for t in TOOLS} == set(HANDLERS)


# ── La política de ejecución ────────────────────────────────────────────


def test_el_actor_no_llega_al_tool():
    """
    Si un plugin pudiera leer quién ejecuta, alguien escribiría
    `if actor == "agent"` — lógica de negocio ramificando por el llamador. Y
    peor: un actor legible desde adentro es un actor que alguien va a intentar
    escribir, y entonces deja de servir para autorizar.
    """
    from backend.core.contract import ToolContext

    ctx = ToolContext(
        run_id="r", case_id="c", params={}, config={}, context={}, log=lambda *_: None
    )
    for prohibido in ("actor", "policy", "user", "kind"):
        assert not hasattr(ctx, prohibido), f"ToolContext expone {prohibido}"


def test_la_politica_es_inmutable():
    """Nada de lo que pase durante un run puede ampliar sus permisos."""
    import dataclasses

    from backend.core.users import RunPolicy

    assert dataclasses.fields(RunPolicy)
    assert RunPolicy.__dataclass_params__.frozen


def test_el_nucleo_no_guarda_credenciales():
    """
    `users` guarda quién dice ser, nunca cómo se prueba. En cuanto el núcleo
    crezca autenticación deja de ser "motor y trazas", y estaríamos escribiendo
    criptografía en core/ — justo lo que mandamos a adapters/.
    """
    from backend.core import schema

    ddl = schema.MIGRATIONS["users"][1].lower()
    for prohibido in ("password", "passwd", "hash", "token", "secret", "session"):
        assert prohibido not in ddl, f"la tabla users tiene una columna {prohibido}"


def test_el_arranque_no_se_lee_de_la_base():
    """
    No es una preferencia: hace falta para ABRIR la base. Si `boot` importara
    un store, sería un ciclo.
    """
    prohibidos = {"stores", "config", "users", "env_store", "resources", "log_store"}
    for nodo in ast.walk(ast.parse((CORE / "boot.py").read_text(encoding="utf-8"))):
        if isinstance(nodo, ast.ImportFrom):
            modulo = (nodo.module or "").split(".")[-1]
            assert modulo not in prohibidos, f"boot.py importa {modulo}"


def test_el_nombre_de_actor_significa_lo_mismo_en_las_dos_puntas():
    """
    El precio de la regla de arriba: `boot` no puede importar `users`, así que
    la regla del nombre de actor está escrita dos veces.

    Duplicada y sin vigilar, se separan. Y el síntoma sería el peor de los dos
    posibles: un `default_actor` que el diagnóstico aprueba y que el alta
    rechaza después, en una instalación que ya se dio por buena.
    """
    from backend.core import boot, users

    assert boot._NOMBRE_ACTOR.pattern == users._NOMBRE_VALIDO.pattern
