"""
Tests de la CLI.

La CLI es la interfaz "en crudo" mientras no hay frontend, así que tiene que
funcionar; pero lo que estos tests cuidan sobre todo es que **no tenga lógica
propia**. Todo lo que hace es leer del catálogo y llamar a `Instance`. El día
que aparezca un servidor HTTP, los dos tienen que ver exactamente lo mismo — el
modo de falla histórico de este repo fue tener dos motores divergiendo.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from backend.core import cli


@pytest.fixture
def correr(monkeypatch, tmp_path, adapters, capsys):
    """
    Corre la CLI contra una instalación aislada.

    Se pisa el armado por defecto para que la CLI no toque el disco real ni la
    red: es la misma inyección que usa cualquier otro test, aplicada al punto
    de entrada.
    """
    from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter
    from backend.core.instance import Instance

    # Una sola base para todo el test: los comandos de una misma prueba tienen
    # que verse entre sí. Con una base por invocación, un `users add` seguido de
    # un `users list` no encontraría nada — y el test estaría midiendo la
    # fixture en vez del código.
    real = SqliteStorageAdapter(IN_MEMORY)

    class Persistente:
        """
        El mismo almacén, pero que sobrevive al `close()` de cada comando.

        `cli.main` cierra su instancia al terminar, y una base `:memory:` vive
        mientras viva su conexión: cerrarla entre dos comandos del mismo test
        borraría todo. En producción cada invocación es un proceso nuevo con su
        archivo, así que el problema no existe; acá hay que sostenerla.
        """

        def __init__(self, delegado):
            self._delegado = delegado

        def __getattr__(self, nombre):
            return getattr(self._delegado, nombre)

        def close(self):
            pass

    almacen = Persistente(real)
    creadas = []

    def _instance(root, **kwargs):
        inst = Instance(
            root,
            storage=almacen,
            adapters=adapters,
            # Se reenvía: es justo lo que los tests de --plugin ejercitan.
            local_plugins=kwargs.get("local_plugins"),
        )
        creadas.append(inst)
        return inst

    monkeypatch.setattr(cli, "Instance", _instance)

    def _correr(*argv):
        # `--root` real, para que los comandos que escriben en disco —`init`—
        # no toquen la instalación de quien está desarrollando.
        codigo = cli.main(["--root", str(tmp_path), *argv])
        return codigo, capsys.readouterr().out

    _correr.instancias = creadas
    yield _correr
    real.close()


def test_tools_lista_el_catalogo(correr):
    codigo, salida = correr("tools")
    assert codigo == 0
    assert "core.log" in salida
    assert "core.wait" in salida


def test_tools_json_es_el_catalogo_crudo(correr):
    codigo, salida = correr("tools", "--json")
    catalogo = json.loads(salida)
    assert catalogo["contract"] == 1
    assert {t["id"] for t in catalogo["tools"]} == {
        "core.log",
        "core.set_status",
        "core.wait",
    }


def test_plugins_muestra_los_ports_de_cada_uno(correr):
    """La superficie de riesgo, visible sin abrir el código de nadie."""
    codigo, salida = correr("plugins")
    assert codigo == 0
    assert "core v" in salida
    assert "usa clock" in salida


def test_plugins_json_devuelve_el_catalogo(correr):
    """
    El flag estaba declarado y `cmd_plugins` no lo miraba: pedías JSON y te
    devolvía texto, sin error. Un consumidor automático —el MCP, un CI— no
    tiene forma de notar eso salvo fallando al parsear.
    """
    codigo, salida = correr("plugins", "--json")
    assert codigo == 0
    catalogo = json.loads(salida)
    assert catalogo["plugins"][0]["name"] == "core"
    assert catalogo["plugins"][0]["ports"] == ["clock"]


def test_doctor_corre_sin_instalacion_previa(correr):
    codigo, salida = correr("doctor")
    assert codigo == 0
    assert "Adapters" in salida


def test_add_y_workflows(correr, tmp_path):
    archivo = tmp_path / "mi-flujo.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.log | message=hola"]\n    B --> N\n'
    )

    codigo, salida = correr("add", str(archivo))
    assert codigo == 0
    assert "mi-flujo" in salida


def test_add_json_devuelve_el_workflow_guardado(correr, tmp_path):
    archivo = tmp_path / "mi-flujo.mmd"
    archivo.write_text(
        "%% folder: pruebas\n"
        'flowchart TD\n    B(inicio)\n    N["core.log | message=hola"]\n    B --> N\n'
    )

    codigo, salida = correr("add", str(archivo), "--json")

    assert codigo == 0
    datos = json.loads(salida)
    assert datos == {
        "ok": True,
        "name": "mi-flujo",
        "folder": "pruebas",
        "state": "enabled",
        "description": "",
    }


def test_add_json_de_un_archivo_inexistente(correr, tmp_path):
    codigo, salida = correr("add", str(tmp_path / "no-existe.mmd"), "--json")

    assert codigo == 2
    datos = json.loads(salida)
    assert datos["ok"] is False


def test_check_valida_un_archivo_sin_guardarlo(correr, tmp_path):
    archivo = tmp_path / "bueno.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.log | message=hola"]\n    B --> N\n'
    )
    codigo, salida = correr("check", str(archivo))
    assert codigo == 0
    assert "Sin problemas" in salida


def test_check_reporta_un_tool_inexistente_con_la_linea(correr, tmp_path):
    archivo = tmp_path / "malo.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.lgo | message=hola"]\n    B --> N\n'
    )
    codigo, salida = correr("check", str(archivo))
    assert codigo == 1
    assert "core.lgo" in salida
    assert "core.log" in salida  # la sugerencia


def test_check_de_algo_que_no_existe_avisa(correr):
    codigo, _ = correr("check", "no-existe-ni-como-archivo")
    assert codigo == 2


def test_run_ejecuta_y_muestra_el_log(correr, tmp_path):
    archivo = tmp_path / "corre.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.log | message=caso {id}"]\n    B --> N\n'
    )
    codigo, salida = correr("run", str(archivo), "--row", '{"id": "0044"}')
    assert codigo == 0
    assert "caso 0044" in salida
    assert "· ok" in salida


def test_run_devuelve_codigo_de_error_si_el_flujo_falla(correr, tmp_path):
    """
    Un flujo que falla tiene que salir con código distinto de cero: sin eso, un
    CI que corra la CLI daría verde sobre una corrida rota.
    """
    archivo = tmp_path / "falla.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.set_status | status=err"]\n    B --> N\n'
    )
    codigo, salida = correr("run", str(archivo))
    assert codigo == 1
    assert "falló en N" in salida


def test_run_con_row_invalido_no_ejecuta_nada(correr, tmp_path):
    archivo = tmp_path / "x.mmd"
    archivo.write_text('flowchart TD\n    B(inicio)\n    N["core.log | message=x"]\n    B --> N\n')
    codigo, _ = correr("run", str(archivo), "--row", "{no es json")
    assert codigo == 2


def test_run_json_trae_la_traza_completa(correr, tmp_path):
    archivo = tmp_path / "traza.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.log | message=hola {id}"]\n    B --> N\n'
    )
    codigo, salida = correr("run", str(archivo), "--row", '{"id": "7"}', "--json")
    datos = json.loads(salida)
    assert datos["status"] == "ok"
    assert datos["trace"][0]["params"]["message"] == "hola 7"


def test_dry_run_no_ejecuta_los_tools(correr, tmp_path, adapters):
    archivo = tmp_path / "seco.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.wait | seconds=300"]\n    B --> N\n'
    )
    codigo, salida = correr("run", str(archivo), "--dry-run")
    assert codigo == 0
    assert "dry run" in salida
    assert adapters["clock"].slept == []


# ── Carga de un plugin sin instalarlo ───────────────────────────────────
#
# Es lo que destraba el bucle de autoría: escribir un plugin, validarlo contra
# un flujo, corregirlo. Sin esto, quien escribe un plugin no tiene cómo
# mostrárselo al validador salvo instalándolo.

DEMO = str(pathlib.Path(__file__).resolve().parent / "demo_plugin.py")

# Plantilla de un plugin mínimo pero válido. Se formatea con `nombre` y `ports`.
PLUGIN_MINIMO = "\n".join([
    "from backend.core.contract import (",
    "    FunctionTool, Plugin, PluginManifest, ToolManifest, ToolResult,",
    ")",
    "",
    'MANIFEST = PluginManifest(name="{nombre}", label="{nombre}", ports={ports})',
    "PLUGIN = Plugin(",
    "    manifest=MANIFEST,",
    "    tools=[FunctionTool(",
    '        manifest=ToolManifest(id="{nombre}.hacer", label="hacer", category="X"),',
    "        fn=lambda ctx: ToolResult.ok(),",
    "    )],",
    ")",
    "",
])


def test_plugin_desde_un_archivo_aparece_en_el_catalogo(correr):
    codigo, salida = correr("--plugin", f"demo={DEMO}", "plugins", "--json")
    assert codigo == 0
    catalogo = json.loads(salida)
    assert {p["name"] for p in catalogo["plugins"]} == {"core", "demo"}
    assert "demo.mover" in {t["id"] for t in catalogo["tools"]}


def test_plugin_desde_una_carpeta(correr, tmp_path):
    """Un plugin de varios módulos se importa como paquete."""
    paquete = tmp_path / "mi_paquete"
    paquete.mkdir()
    (paquete / "__init__.py").write_text(
        PLUGIN_MINIMO.format(nombre="mio", ports="()"), encoding="utf-8"
    )
    codigo, salida = correr("--plugin", f"mio={paquete}", "plugins", "--json")
    assert codigo == 0
    assert "mio.hacer" in {t["id"] for t in json.loads(salida)["tools"]}


def test_un_plugin_roto_se_reporta_y_no_tumba_el_comando(correr, tmp_path):
    """
    Quien está iterando necesita el mensaje de error, no un stacktrace del
    intérprete. El registry lo captura y el catálogo lo publica.
    """
    roto = tmp_path / "roto.py"
    roto.write_text("import esto_no_existe\nPLUGIN = None\n", encoding="utf-8")

    codigo, salida = correr("--plugin", f"roto={roto}", "plugins", "--json")
    assert codigo == 1
    errores = json.loads(salida)["errors"]
    assert errores and "esto_no_existe" in errores[0]["error"]


def test_un_plugin_que_pide_un_port_sin_adapter_no_carga(correr, tmp_path):
    """La validación de ports también aplica a un plugin cargado desde disco."""
    sin_adapter = tmp_path / "sinadapter.py"
    sin_adapter.write_text(
        PLUGIN_MINIMO.format(nombre="raro", ports='("telepatia",)'), encoding="utf-8"
    )
    codigo, salida = correr("--plugin", f"raro={sin_adapter}", "plugins", "--json")
    assert codigo == 1
    catalogo = json.loads(salida)
    assert "no existen" in catalogo["errors"][0]["error"]
    # Y no entró ni uno de sus tools: no se registra a medias.
    assert "raro.hacer" not in {t["id"] for t in catalogo["tools"]}


def test_plugin_con_especificacion_invalida(correr):
    codigo, _ = correr("--plugin", "sin-igual", "plugins")
    assert codigo == 2


def test_plugin_que_no_existe_en_disco(correr):
    codigo, _ = correr("--plugin", "x=/ruta/que/no/existe.py", "plugins")
    assert codigo == 2


def test_el_flag_global_no_lo_pisa_el_positional_de_action(correr):
    """
    `action` tiene un positional llamado `plugin`. Sin `dest` explícito en el
    flag global, argparse los une en el mismo atributo sin avisar: gana el
    último, y el error aparece lejos de la causa.
    """
    codigo, salida = correr(
        "--plugin", f"demo={DEMO}", "action", "demo", "inventada", "--json"
    )
    # La acción no existe, pero el plugin **se cargó**: si el positional hubiera
    # pisado al flag, el comando habría muerto parseando --plugin.
    assert codigo == 1
    assert "ping" in json.loads(salida)["message"]


# ── check --json ────────────────────────────────────────────────────────


def test_check_json_estructura_los_diagnosticos(correr, tmp_path):
    archivo = tmp_path / "malo.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.lgo | message=hola"]\n    B --> N\n'
    )
    codigo, salida = correr("check", str(archivo), "--json")
    assert codigo == 1

    datos = json.loads(salida)
    assert datos["ok"] is False
    assert datos["nodes"] == 2
    d = datos["diagnostics"][0]
    # Lo que hace falta para corregir sin parsear castellano.
    assert d["severity"] == "error"
    assert d["line"] == 3
    assert d["node_id"] == "N"
    assert "core.lgo" in d["message"]


def test_check_json_de_un_flujo_sano(correr, tmp_path):
    archivo = tmp_path / "bueno.mmd"
    archivo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.log | message=hola"]\n    B --> N\n'
    )
    codigo, salida = correr("check", str(archivo), "--json")
    assert codigo == 0
    datos = json.loads(salida)
    assert datos["ok"] is True
    assert datos["diagnostics"] == []


def test_el_bucle_de_autoria_completo(correr, tmp_path):
    """
    El caso de uso real, de punta a punta: un flujo usa un tool que no existe,
    se escribe el plugin que lo provee, y el mismo flujo pasa a validar.
    """
    flujo = tmp_path / "flujo.mmd"
    flujo.write_text('flowchart TD\n    B(inicio)\n    N["nuevo.hacer"]\n    B --> N\n')

    # 1. Sin el plugin: no valida, y dice exactamente qué falta.
    codigo, salida = correr("check", str(flujo), "--json")
    assert codigo == 1
    assert "nuevo.hacer" in json.loads(salida)["diagnostics"][0]["message"]

    # 2. Se escribe el plugin que lo provee.
    plugin = tmp_path / "nuevo.py"
    plugin.write_text(PLUGIN_MINIMO.format(nombre="nuevo", ports="()"), encoding="utf-8")

    # 3. Con el plugin: valida.
    codigo, salida = correr("--plugin", f"nuevo={plugin}", "check", str(flujo), "--json")
    assert codigo == 0, salida
    assert json.loads(salida)["ok"] is True

    # 4. Y el dry-run lo recorre entero sin ejecutar un solo tool.
    codigo, salida = correr(
        "--plugin", f"nuevo={plugin}", "run", str(flujo), "--dry-run", "--json"
    )
    assert codigo == 0
    assert json.loads(salida)["dry_run"] is True


# ── action ──────────────────────────────────────────────────────────────


def test_action_ejecuta_y_devuelve_json(correr, adapters):
    adapters["http"].stub("https://api.test/ping", status=200, text="{}")
    codigo, salida = correr(
        "--plugin", f"demo={DEMO}",
        "action", "demo", "ping",
        "--params", '{"url": "https://api.test/ping"}',
        "--json",
    )
    assert codigo == 0
    assert "200" in json.loads(salida)["message"]


def test_action_inexistente_nombra_las_disponibles(correr):
    codigo, salida = correr("--plugin", f"demo={DEMO}", "action", "demo", "nada", "--json")
    assert codigo == 1
    assert "ping" in json.loads(salida)["message"]


def test_action_con_params_invalidos(correr):
    codigo, _ = correr("--plugin", f"demo={DEMO}", "action", "demo", "ping", "--params", "{roto")
    assert codigo == 2


def test_action_con_item_resuelve_params_desde_el_resource(correr):
    """Issue #7: --item ahorra reconstruir a mano lo que ya está guardado."""
    from backend.tests.demo_plugin import DESTINOS

    correr("--plugin", f"demo={DEMO}", "plugins", "--json")
    correr.instancias[-1].resource_store("demo", DESTINOS).write(
        "frio", {"ruta": "/deposito", "token": "sss"}
    )

    codigo, salida = correr(
        "--plugin", f"demo={DEMO}", "action", "demo", "probar_destino", "--item", "frio", "--json"
    )

    assert codigo == 0
    outputs = json.loads(salida)["outputs"]
    assert outputs["ruta"] == "/deposito"
    assert outputs["token"] == "sss"


# ── resources ───────────────────────────────────────────────────────────


def test_resources_lista_items_con_secrets_tapados(correr):
    from backend.tests.demo_plugin import DESTINOS

    correr("--plugin", f"demo={DEMO}", "plugins", "--json")
    correr.instancias[-1].resource_store("demo", DESTINOS).write(
        "frio", {"ruta": "/deposito", "token": "sss"}
    )

    codigo, salida = correr("--plugin", f"demo={DEMO}", "resources", "demo", "destinos", "--json")

    assert codigo == 0
    datos = json.loads(salida)
    assert datos["key_field"] == "name"
    item = next(i for i in datos["items"] if i["name"] == "frio")
    assert item["ruta"] == "/deposito"
    assert item["token"] is None


def test_resources_de_coleccion_inexistente_es_error(correr):
    codigo, _ = correr("--plugin", f"demo={DEMO}", "resources", "demo", "nada", "--json")
    assert codigo == 2


# ── Actores desde la CLI ────────────────────────────────────────────────


def test_users_lista_los_sembrados(correr):
    codigo, salida = correr("users", "--json")
    assert codigo == 0
    nombres = {u["name"] for u in json.loads(salida)}
    assert nombres == {"local", "system"}


def test_users_add_crea_un_agente_restringido(correr):
    codigo, salida = correr("users", "add", "agente", "--kind", "agent")
    assert codigo == 0
    assert "peligrosos: no" in salida

    _, listado = correr("users", "--json")
    agente = next(u for u in json.loads(listado) if u["name"] == "agente")
    assert agente["can_run_dangerous"] is False
    assert "process" not in agente["allowed_ports"]


def test_users_allow_amplia_los_permisos(correr):
    correr("users", "add", "agente", "--kind", "agent")
    codigo, salida = correr("users", "allow", "agente", "--dangerous", "--all-ports", "--json")
    assert codigo == 0
    afectado = json.loads(salida)[0]
    assert afectado["can_run_dangerous"] is True
    assert afectado["allowed_ports"] is None   # todos


def test_toda_rama_de_users_respeta_json(correr):
    """
    El mismo bug que tuvo `plugins --json`: un flag declarado que sólo algunas
    ramas miran. No da error, devuelve otra cosa, y quien lo consume se entera
    lejos de la causa.
    """
    for argv in (
        ("users", "add", "x", "--kind", "agent", "--json"),
        ("users", "allow", "x", "--dangerous", "--json"),
        ("users", "disable", "x", "--json"),
        ("users", "enable", "x", "--json"),
        ("users", "--json"),
    ):
        codigo, salida = correr(*argv)
        assert codigo == 0, argv
        json.loads(salida)   # revienta si devolvió texto


def test_users_disable_no_borra(correr):
    correr("users", "add", "temporal", "--kind", "agent")
    correr("users", "disable", "temporal")
    _, salida = correr("users", "--json")
    temporal = next(u for u in json.loads(salida) if u["name"] == "temporal")
    assert temporal["enabled"] is False


def test_run_con_actor_inexistente_sale_con_codigo_propio(correr, tmp_path):
    """
    Código 3, distinto del 1 (el flujo falló) y del 2 (mal invocado): un
    consumidor automático tiene que poder distinguir "no tenés permiso" de
    "el flujo se rompió".
    """
    archivo = tmp_path / "x.mmd"
    archivo.write_text('flowchart TD\n    B(inicio)\n    N["core.log | message=x"]\n    B --> N\n')
    codigo, _ = correr("--actor", "fantasma", "run", str(archivo))
    assert codigo == 3


def test_run_con_tool_inexistente_falla_antes_de_ejecutar(correr, tmp_path):
    """
    Issue #9: antes esto terminaba con código 0 y "status: ok", el nodo roto
    escondido detrás de una arista |err|. Mismo código 3 que un actor sin
    permiso: es la misma clase de "no se puede ejecutar", no un fallo de run.
    """
    archivo = tmp_path / "roto.mmd"
    archivo.write_text(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    N["archivos.copiar"]\n'
        '    E["core.log | message=manejado"]\n'
        '    B --> N\n'
        '    N -->|err| E\n'
    )
    codigo, _ = correr("run", str(archivo))
    assert codigo == 3


def test_run_allow_broken_corre_igual(correr, tmp_path):
    archivo = tmp_path / "roto.mmd"
    archivo.write_text(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    N["archivos.copiar"]\n'
        '    E["core.log | message=manejado"]\n'
        '    B --> N\n'
        '    N -->|err| E\n'
    )
    codigo, salida = correr("run", str(archivo), "--allow-broken", "--json")
    assert codigo == 0
    assert json.loads(salida)["status"] == "ok"


# ── Correr un archivo ya no lo guarda ───────────────────────────────────


def test_correr_un_archivo_no_lo_persiste(correr, tmp_path):
    """
    Antes sí, en silencio, para que flow.ejecutar pudiera resolver subflujos:
    validar un archivo te escribía en la base sin decirlo. Archivos para
    autoría, store para runtime, y un puente explícito en el medio.
    """
    archivo = tmp_path / "suelto.mmd"
    archivo.write_text('flowchart TD\n    B(inicio)\n    N["core.log | message=x"]\n    B --> N\n')

    codigo, _ = correr("run", str(archivo))
    assert codigo == 0

    _, listado = correr("workflows")
    assert "suelto" not in listado


def test_con_save_si_lo_persiste(correr, tmp_path):
    archivo = tmp_path / "guardado.mmd"
    archivo.write_text('flowchart TD\n    B(inicio)\n    N["core.log | message=x"]\n    B --> N\n')

    correr("run", str(archivo), "--save")
    _, listado = correr("workflows")
    assert "guardado" in listado


def test_correr_un_archivo_no_pisa_un_flujo_guardado_con_ese_nombre(correr, tmp_path):
    """
    Limpiar al terminar no puede llevarse por delante algo que el usuario
    guardó antes con el mismo nombre.
    """
    archivo = tmp_path / "propio.mmd"
    archivo.write_text('flowchart TD\n    B(inicio)\n    N["core.log | message=guardado"]\n    B --> N\n')
    correr("add", str(archivo))

    otro = tmp_path / "otro" / "propio.mmd"
    otro.parent.mkdir()
    otro.write_text('flowchart TD\n    B(inicio)\n    N["core.log | message=efimero"]\n    B --> N\n')
    correr("run", str(otro))

    _, listado = correr("workflows")
    assert "propio" in listado


# ── Arranque ────────────────────────────────────────────────────────────


def test_boot_muestra_la_config_en_efecto(correr):
    codigo, salida = correr("boot", "--json")
    assert codigo == 0
    datos = json.loads(salida)
    assert datos["default_actor"] == "local"
    assert "storage" in datos


def test_init_escribe_el_archivo_y_no_lo_pisa(correr, tmp_path):
    codigo, salida = correr("init")
    assert codigo == 0
    assert (tmp_path / "boot.env").is_file()

    # Sin --force no reescribe: la config de arranque no se pierde por accidente.
    codigo, _ = correr("init")
    assert codigo == 2
    codigo, _ = correr("init", "--force")
    assert codigo == 0


def test_lo_que_escribe_init_lo_puede_leer_el_arranque(correr, tmp_path):
    """
    El comando y el lector tienen que coincidir en la codificación. `init`
    escribe con BOM —Windows es el destino del instalador— y si `load` no lo
    tolerara, la primera clave del archivo que el propio comando genera se
    perdería en silencio.
    """
    from backend.core import boot

    assert correr("init")[0] == 0
    assert (tmp_path / boot.ARCHIVO).read_bytes().startswith(b"\xef\xbb\xbf")
    assert boot.desconocidas(tmp_path, entorno={}) == []
