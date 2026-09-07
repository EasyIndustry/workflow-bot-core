"""
Tests del diagnóstico.

Lo que responde "¿levanta todo?" es Python puro y sin dependencias, así que se
prueba entero. Los dos chequeos que importan son los que evitan un fallo en
producción: que no falte un plugin para un tool que un flujo usa, y que cada
plugin tenga adapter para los ports que pidió.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from backend.core.builtins import build_builtin_plugin
from backend.core.contract import (
    FunctionTool,
    Plugin,
    PluginManifest,
    Setting,
    ToolManifest,
    ToolResult,
)
from backend.core import boot
from backend.core.doctor import (
    Level,
    check_adapters,
    check_boot,
    check_config,
    check_crypto,
    check_env,
    check_plugins,
    check_python,
    check_storage,
    check_tools_used,
    check_workflows,
    format_text,
    run_checks,
)
from backend.core.registry import ToolRegistry
from backend.core.stores import WorkflowStore
from backend.tests.demo_plugin import build_plugin as build_demo_plugin
from backend.tests.fakes import fake_adapters

FLOWS = pathlib.Path(__file__).resolve().parent / "flows"


def _registry(*, con_demo: bool = False, adapters=None) -> ToolRegistry:
    reg = ToolRegistry(adapters=adapters if adapters is not None else fake_adapters())
    reg._add_plugin("core", "builtin", build_builtin_plugin())
    if con_demo:
        reg._add_plugin("demo", "tests.demo_plugin:PLUGIN", build_demo_plugin())
    return reg


def _flujos(db, **archivos: str) -> list:
    store = WorkflowStore(db)
    for nombre, contenido in archivos.items():
        store.save_mmd(nombre, contenido)
    return store.list()


def _corpus(db) -> list:
    store = WorkflowStore(db)
    store.import_dir(FLOWS)
    return store.list()


# ── Entorno ─────────────────────────────────────────────────────────────


def test_python_soportado():
    check = check_python()
    assert check.level is Level.OK


def test_env_faltante_no_es_un_problema(tmp_path):
    """
    `.env` es opcional: la configuración vive en la base. Reportarlo como error
    mandaría a crear un archivo que no hace falta.
    """
    assert check_env(tmp_path).level is Level.OK
    (tmp_path / ".env").write_text("X=1")
    assert check_env(tmp_path).level is Level.OK


# ── Adapters ────────────────────────────────────────────────────────────


def test_sin_adapters_es_error():
    """Sin adapters, ningún plugin que necesite un port puede cargar."""
    check = check_adapters(ToolRegistry())
    assert check.level is Level.ERROR
    assert check.fix


def test_los_adapters_atados_se_listan_con_su_implementacion():
    check = check_adapters(_registry())
    assert check.level is Level.OK
    assert any("clock: FakeClock" in d for d in check.detail)


def test_el_diagnostico_muestra_que_ports_usa_cada_plugin():
    """
    La superficie de riesgo de la instalación, sin leer el código de nadie:
    quién sale a la red, quién toca el disco, quién corre comandos.
    """
    check = check_adapters(_registry(con_demo=True))
    assert any(d.startswith("demo usa:") and "process" in d for d in check.detail)


# ── Plugins ─────────────────────────────────────────────────────────────


def test_plugins_cargados():
    check = check_plugins(_registry(con_demo=True))
    assert check.level is Level.OK
    assert any("demo v1.0.0" in d for d in check.detail)


def test_plugin_roto_es_error_con_su_motivo():
    reg = _registry()
    reg.register_local("fantasma", "modulo.que.no.existe:PLUGIN")
    check = check_plugins(reg)
    assert check.level is Level.ERROR
    assert any("fantasma" in d for d in check.detail)


def test_plugin_sin_adapter_para_su_port_se_reporta_como_error_de_carga():
    """
    El caso que esta arquitectura tiene que atrapar temprano: el plugin pide un
    port que nadie ató. Antes se manifestaba a mitad de un run, en producción.
    """
    reg = _registry(adapters={"clock": fake_adapters()["clock"]})
    reg._add_plugin("demo", "test", build_demo_plugin())
    check = check_plugins(reg)
    assert check.level is Level.ERROR
    assert any("no hay adapter atado" in d for d in check.detail)


# ── Configuración ───────────────────────────────────────────────────────


def test_config_faltante_nombra_la_clave():
    manifest = PluginManifest(
        name="p", label="P", settings=(Setting("rutaBase", required=True, label="Ruta base"),)
    )
    reg = _registry()
    reg._add_plugin("p", "test", Plugin(manifest=manifest))

    check = check_config(reg, {})
    assert check.level is Level.WARN
    assert any("rutaBase" in d for d in check.detail)


def test_config_completa():
    assert check_config(_registry(con_demo=True), {}).level is Level.OK


# ── Flujos ──────────────────────────────────────────────────────────────


def test_el_corpus_esta_sano(db):
    check = check_workflows(_corpus(db))
    assert check.level is Level.OK
    # El deshabilitado cuenta en el total pero no entre los habilitados.
    assert "habilitados" in check.message


def test_flujo_roto_se_reporta_con_linea(db):
    flujos = _flujos(db, roto="flowchart TD\n    A --> B\n")
    check = check_workflows(flujos)
    assert check.level is Level.ERROR
    assert any("roto" in d for d in check.detail)


def test_sin_flujos_es_warning():
    assert check_workflows([]).level is Level.WARN


# ── Tools usados vs. instalados ─────────────────────────────────────────


def test_detecta_tools_sin_plugin_y_quien_los_usa(db):
    flujos = _flujos(
        db,
        uno='flowchart TD\n    B(inicio)\n    N["falta.tool"]\n    B --> N\n',
        dos='flowchart TD\n    B(inicio)\n    N["falta.tool"]\n    B --> N\n',
    )
    check = check_tools_used(_registry(), flujos)
    assert check.level is Level.ERROR
    assert any("falta.tool" in d and "uno" in d and "dos" in d for d in check.detail)


def test_todos_los_tools_disponibles(db):
    check = check_tools_used(_registry(con_demo=True), _corpus(db))
    assert check.level is Level.OK


def test_flow_ejecutar_no_cuenta_como_faltante(db):
    """
    `flow.*` lo resuelve el executor, no el registry: no está en el catálogo y
    reportarlo como faltante mandaría a instalar un plugin que no existe.
    """
    flujos = _flujos(
        db,
        anidado='flowchart TD\n    B(inicio)\n    N["flow.ejecutar | flowName=x"]\n    B --> N\n',
    )
    assert check_tools_used(_registry(), flujos).level is Level.OK


def test_flujos_deshabilitados_no_cuentan(db):
    """Un flujo apagado que usa un tool que falta no es un problema hoy."""
    flujos = _flujos(
        db,
        apagado=(
            "%% state: disabled\n"
            'flowchart TD\n    B(inicio)\n    N["falta.tool"]\n    B --> N\n'
        ),
    )
    assert check_tools_used(_registry(), flujos).level is not Level.ERROR


def test_el_corpus_sin_el_plugin_demo_reporta_lo_que_falta(db):
    """
    El corpus usa `demo.*`. Sin ese plugin instalado, el chequeo tiene que
    nombrar exactamente los tools que faltan y qué flujos los usan.
    """
    check = check_tools_used(_registry(), _corpus(db))
    assert check.level is Level.ERROR
    assert any("demo." in d for d in check.detail)


# ── Almacenamiento ──────────────────────────────────────────────────────


class _CryptoFalso:
    """Lo mínimo que el chequeo consulta de un `CryptoPort`."""

    def __init__(self, *, available=True, key_exists=False, key_path="/x/secret.key"):
        self.available = available
        self.key_exists = key_exists
        self.key_path = key_path


def test_una_carpeta_escribible_alcanza_aunque_la_base_no_exista(tmp_path):
    """
    Una instalación nueva todavía no tiene su base: lo que hay que responder es
    si la va a poder crear, no si ya está.
    """
    check = check_storage(boot.load(tmp_path, entorno={}))
    assert check.level is Level.OK
    assert str(tmp_path / "data" / "bot.db") in check.detail


def test_una_ruta_que_no_se_puede_crear_es_error(tmp_path):
    """
    El modo de falla que este chequeo existe para evitar: sin él, la
    instalación no falla al arrancar sino la primera vez que alguien guarda
    algo — cuando ya nadie está mirando el diagnóstico.

    Acá el ancestro es un archivo, así que la carpeta no se va a poder crear
    pase lo que pase con los permisos ni con qué usuario corra.
    """
    (tmp_path / "no-soy-carpeta").write_text("x", encoding="utf-8")
    config = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}storage": str(tmp_path / "no-soy-carpeta" / "bot.db"),
    })
    check = check_storage(config)
    assert check.level is Level.ERROR
    assert check.fix


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root escribe igual: el permiso no dice nada sobre lo que va a pasar",
)
def test_una_carpeta_sin_permiso_de_escritura_es_error(tmp_path):
    encierro = tmp_path / "sin-permiso"
    encierro.mkdir()
    encierro.chmod(0o500)
    try:
        config = boot.load(tmp_path, entorno={
            f"{boot.PREFIJO}storage": str(encierro / "sub" / "bot.db"),
        })
        assert check_storage(config).level is Level.ERROR
    finally:
        encierro.chmod(0o700)


def test_la_base_en_memoria_se_avisa_como_advertencia(tmp_path):
    """Corre de verdad, pero no queda nada: hay que decirlo, no frenar."""
    check = check_storage(boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}storage": boot.EN_MEMORIA,
    }))
    assert check.level is Level.WARN
    assert "memoria" in check.message


def test_una_base_existente_informa_cuanto_ocupa(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bot.db").write_bytes(b"x" * 2048)
    check = check_storage(boot.load(tmp_path, entorno={}))
    assert check.level is Level.OK
    assert any("KB" in linea for linea in check.detail)


# ── Cifrado ─────────────────────────────────────────────────────────────


def test_sin_la_libreria_de_cifrado_se_avisa_antes():
    """
    Sin este chequeo el cliente se entera cuando intenta guardar su primer
    secreto — es decir, tarde, y en una pantalla que no es la de instalación.
    """
    check = check_crypto(_CryptoFalso(available=False))
    assert check.level is Level.WARN
    assert "cryptography" in check.fix


def test_falta_la_libreria_no_impide_arrancar():
    """
    Los flujos que no tocan un secreto corren igual, así que es advertencia y
    no error: `ok` significa "levanta", no "está todo instalado".
    """
    report = run_checks(
        root=pathlib.Path("/tmp"),
        registry=_registry(),
        crypto=_CryptoFalso(available=False),
    )
    assert report.ok


def test_con_la_llave_ya_generada_se_dice_donde_esta():
    check = check_crypto(_CryptoFalso(key_exists=True, key_path="/datos/secret.key"))
    assert check.level is Level.OK
    assert "/datos/secret.key" in check.detail


def test_sin_llave_todavia_se_avisa_que_va_a_existir():
    """
    Es el activo más importante de la instalación: sin ese archivo, un backup
    de la base no recupera ningún secreto. Que exista hay que decirlo antes.
    """
    check = check_crypto(_CryptoFalso(key_exists=False))
    assert check.level is Level.OK
    assert any("descifrar" in linea for linea in check.detail)


# ── Valores de arranque ─────────────────────────────────────────────────


def test_un_valor_de_arranque_dudoso_se_muestra_en_el_diagnostico(tmp_path):
    check = check_boot(boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}http_timeout": "treinta",
    }))
    assert check.level is Level.WARN
    assert any("http_timeout" in linea for linea in check.detail)


def test_un_arranque_sano_no_dice_nada(tmp_path):
    assert check_boot(boot.load(tmp_path, entorno={})).level is Level.OK


# ── Informe ─────────────────────────────────────────────────────────────


def test_el_informe_junta_todo_y_es_serializable(db, tmp_path):
    report = run_checks(
        root=tmp_path,
        registry=_registry(con_demo=True),
        config={},
        workflows=_corpus(db),
    )
    datos = report.to_dict()
    assert {c["name"] for c in datos["checks"]} >= {"Python", "Adapters", "Plugins", "Flujos"}
    assert isinstance(datos["ok"], bool)


def test_el_almacenamiento_y_el_cifrado_entran_al_informe(db, tmp_path):
    """
    Los dos que más importan en una instalación nueva: sin ellos, `doctor`
    puede decir que todo está bien en una máquina donde no se va a poder
    escribir la base ni guardar un secreto.
    """
    report = run_checks(
        root=tmp_path,
        registry=_registry(con_demo=True),
        config={},
        workflows=_corpus(db),
        boot=boot.load(tmp_path, entorno={}),
        crypto=_CryptoFalso(),
    )
    nombres = {c.name for c in report.checks}
    assert {"Almacenamiento", "Cifrado", "Arranque"} <= nombres


def test_sin_boot_ni_crypto_el_informe_sigue_corriendo(db, tmp_path):
    """Son opcionales: un test no tiene por qué construir una instalación entera."""
    report = run_checks(root=tmp_path, registry=_registry(), config={}, workflows=_corpus(db))
    assert {"Almacenamiento", "Cifrado"}.isdisjoint({c.name for c in report.checks})


def test_los_warnings_no_impiden_arrancar(db, tmp_path):
    """
    Un warning informa; sólo un error frena. Si un warning bloqueara, nadie
    podría arrancar con un flujo apagado o sin `.env`.
    """
    report = run_checks(root=tmp_path, registry=_registry(con_demo=True), config={}, workflows=[])
    assert report.warnings
    assert not report.errors
    assert report.ok


def test_formato_de_texto_incluye_el_arreglo(db, tmp_path):
    report = run_checks(root=tmp_path, registry=_registry(), config={}, workflows=_corpus(db))
    texto = format_text(report, color=False)
    assert "ERROR" in texto
    assert "→" in texto  # la línea del arreglo sugerido


def test_un_tool_que_no_esta_en_ningun_flujo_no_molesta(db):
    """El chequeo mira lo que los flujos usan, no lo que está instalado de más."""
    reg = _registry(con_demo=True)
    tool = FunctionTool(
        manifest=ToolManifest(id="extra.sinuso", label="x", category="X"),
        fn=lambda ctx: ToolResult.ok(),
    )
    reg._add_plugin("extra", "test", [tool])
    assert check_tools_used(reg, _corpus(db)).level is Level.OK
