"""
Tests del servidor MCP de autoría.

Dos capas, y se prueban distinto:

- `operations.py` es la lógica y no sabe que MCP existe. Se prueba llamándola
  derecho. Cada llamada levanta un subproceso de verdad —es el punto, ver más
  abajo— así que estos tests son los más lentos de la suite.
- `server.py` es cableado. Se prueba que los esquemas estén bien formados y que
  no haya deriva entre la lista de tools y sus handlers.

Lo que NO se prueba acá es el transporte stdio: eso es el SDK, y testearlo sería
testear código de otro.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from backend.mcp import operations as ops

DEMO = str(pathlib.Path(__file__).resolve().parent / "demo_plugin.py")
FLOWS = pathlib.Path(__file__).resolve().parent / "flows"


@pytest.fixture
def raiz(tmp_path):
    """
    Una instalación descartable.

    Las operaciones corren la CLI de verdad, así que sin esto escribirían en
    `backend/data/` — la instalación real de quien está desarrollando.
    """
    return str(tmp_path)


# ── Descubrimiento ──────────────────────────────────────────────────────


def test_list_tools_trae_el_catalogo_y_los_nativos(raiz):
    """
    Los tools nativos (`flow.*`) los resuelve el executor y no están en el
    registro. Sin nombrarlos acá, quien escribe un flujo no se entera de que
    existen — y son justamente los de control de flujo.
    """
    resultado = ops.list_tools(root=raiz)
    ids = {t["id"] for t in resultado["tools"]}
    assert ids == {"core.log", "core.set_status", "core.wait"}
    assert {n["id"] for n in resultado["nativos"]} == {"flow.ejecutar", "flow.retry_gate"}
    assert set(resultado["ports_disponibles"]) == {"http", "fs", "process", "clock", "browser", "window"}


def test_list_tools_incluye_un_plugin_local(raiz):
    resultado = ops.list_tools(plugins={"demo": DEMO}, root=raiz)
    ids = {t["id"] for t in resultado["tools"]}
    assert "demo.mover" in ids
    # Y con su tipado completo, que es para lo que sirve el catálogo.
    mover = next(t for t in resultado["tools"] if t["id"] == "demo.mover")
    assert mover["dangerous"] is True
    assert [p["name"] for p in mover["params"]] == ["origen", "destino"]


def test_list_plugins_publica_los_ports_de_cada_uno(raiz):
    resultado = ops.list_plugins(plugins={"demo": DEMO}, root=raiz)
    demo = next(p for p in resultado["plugins"] if p["name"] == "demo")
    assert set(demo["ports"]) == {"http", "fs", "process", "clock"}
    assert demo["actions"][0]["name"] == "ping"


def test_list_ports_sale_del_contrato_y_no_de_una_lista_a_mano():
    """
    Si mañana aparece un port nuevo, esto lo refleja solo. Una lista escrita a
    mano se desactualiza en silencio y manda a declarar algo que no existe.
    """
    from backend.core import ports as p

    resultado = ops.list_ports()
    assert {x["name"] for x in resultado["pedibles_por_un_plugin"]} == p.PLUGIN_PORTS
    assert set(resultado["reservados_al_nucleo"]) == {"storage", "crypto"}


# ── Validación ──────────────────────────────────────────────────────────


def test_check_flow_reporta_el_tool_que_falta_con_su_linea(raiz, tmp_path):
    flujo = tmp_path / "x.mmd"
    flujo.write_text('flowchart TD\n    B(inicio)\n    N["falta.tool"]\n    B --> N\n')

    resultado = ops.check_flow(str(flujo), root=raiz)
    assert resultado["ok"] is False
    d = resultado["diagnostics"][0]
    assert d["severity"] == "error"
    assert d["line"] == 3
    assert d["node_id"] == "N"


def test_check_flow_pasa_cuando_el_plugin_esta(raiz):
    resultado = ops.check_flow(str(FLOWS / "mundo.mmd"), plugins={"demo": DEMO}, root=raiz)
    assert resultado["ok"] is True
    assert resultado["diagnostics"] == []


def test_check_flow_de_algo_inexistente_levanta(raiz):
    with pytest.raises(ops.OperationError):
        ops.check_flow("no-existe-ni-como-archivo", root=raiz)


# ── Orientación (issue #17) ─────────────────────────────────────────────


def test_describe_installation_es_la_foto_entera_en_una_llamada(raiz):
    """
    Un agente que no conoce la instalación necesita saber qué hay ANTES de
    elegir qué tool usar. Hasta el #17, armar esta foto era pedir cinco cosas
    sueltas y cruzarlas a mano — que es justamente lo que no puede hacer
    quien no sabe que hay cinco cosas que pedir.
    """
    ops.save_flow(str(FLOWS / "mundo.mmd"), root=raiz)

    resultado = ops.describe_installation(plugins={"demo": DEMO}, root=raiz)

    flujo = next(f for f in resultado["flows"] if f["name"] == "mundo")
    assert flujo["enabled"] is True
    # Qué tools usa cada flujo, que es lo que liga un flujo con los plugins
    # que necesita tener instalados.
    assert "demo.mover" in flujo["tools"]

    demo = next(p for p in resultado["plugins"] if p["name"] == "demo")
    assert [c["name"] for c in demo["collections"]] == ["destinos"]
    assert demo["collections"][0]["items"] == 0
    assert "demo.mover" in [t["id"] for t in demo["tools"]]
    # Issue #18: sin esto, run_action("probar_destino") era un tanteo a
    # ciegas — el agente no tenía forma de saber que la acción existe.
    assert {a["name"] for a in demo["actions"]} == {"ping", "probar_destino"}

    assert {a["name"] for a in resultado["actors"]} == {"local", "system"}
    # La configuración de arranque: qué puede tocar esta instalación. No sale
    # de `effective_config` (que son los settings de los plugins).
    assert resultado["boot"]["default_actor"] == "local"
    assert "fs_root" in resultado["boot"]
    assert "window" in resultado["ports_disponibles"]
    assert resultado["runs"] == {"recientes": 0, "fallidos": 0, "ultimo_por_flujo": {}}


def test_describe_installation_trae_un_resumen_en_texto_plano(raiz):
    """Para un cliente que sólo pueda mostrar texto, el JSON no sirve de nada."""
    resumen = ops.describe_installation(root=raiz)["resumen"]
    assert "flujo(s) guardado(s)" in resumen
    assert "Actores:" in resumen
    assert "actor por defecto: local" in resumen


def test_list_flows_y_get_flow_evitan_adivinar_la_ruta_del_mmd(raiz):
    ops.save_flow(str(FLOWS / "mundo.mmd"), root=raiz)

    assert [f["name"] for f in ops.list_flows(root=raiz)["flows"]] == ["mundo"]

    detalle = ops.get_flow("mundo", plugins={"demo": DEMO}, root=raiz)
    assert detalle["runnable"] is True
    assert detalle["diagnostics"] == []
    assert "demo.mover" in detalle["content"]


def test_get_flow_cruza_los_diagnosticos_contra_lo_instalado(raiz):
    """
    El mismo flujo es ejecutable o no según qué plugins haya: sin `demo`, sus
    nodos apuntan a tools que no existen. Por eso el diagnóstico va con el
    contenido y no aparte.
    """
    ops.save_flow(str(FLOWS / "mundo.mmd"), root=raiz)

    detalle = ops.get_flow("mundo", root=raiz)
    assert detalle["runnable"] is False
    assert any("demo.mover" in d["message"] for d in detalle["diagnostics"])


def test_get_flow_de_uno_que_no_existe_lo_dice(raiz):
    assert "no-existe" in ops.get_flow("no-existe", root=raiz)["error"]


def test_list_runs_y_get_run_responden_que_corrio_y_por_que_fallo(raiz):
    _alta(raiz)
    ejecutado = ops.run_flow(str(FLOWS / "lineal.mmd"), root=raiz, row={"id": "0044"})

    runs = ops.list_runs(root=raiz)["runs"]
    assert [r["run_id"] for r in runs] == [ejecutado["run_id"]]
    assert runs[0]["case_id"] == "0044"

    detalle = ops.get_run(ejecutado["run_id"], root=raiz)
    assert detalle["run_id"] == ejecutado["run_id"]
    # La traza nodo por nodo, que es lo que `list_runs` deliberadamente no trae.
    assert detalle["trace"]


def test_list_runs_filtra_por_caso_y_por_fallidos(raiz):
    _alta(raiz)
    ops.run_flow(str(FLOWS / "lineal.mmd"), root=raiz, row={"id": "0044"})

    assert ops.list_runs(case_id="0044", root=raiz)["runs"]
    assert ops.list_runs(case_id="otro", root=raiz)["runs"] == []
    assert ops.list_runs(only_failed=True, root=raiz)["runs"] == []


def test_get_case_log_cruza_todos_los_runs_de_una_fila(raiz):
    _alta(raiz)
    ops.run_flow(str(FLOWS / "lineal.mmd"), root=raiz, row={"id": "0044"})
    ops.run_flow(str(FLOWS / "lineal.mmd"), root=raiz, row={"id": "0044"})

    registro = ops.get_case_log("0044", root=raiz)
    assert registro["case_id"] == "0044"
    # Dos corridas de la misma fila: el registro de la fila las acumula, que es
    # justamente lo que un run suelto no muestra.
    assert sum("procesando 0044" in e["message"] for e in registro["entries"]) == 2


def test_write_y_delete_resource_item_completan_una_coleccion(raiz):
    """
    `list_resource_items` sólo leía: un agente podía ver qué conexiones hay
    pero no dar de alta una.
    """
    escrito = ops.write_resource_item(
        "demo", "destinos", "ARCHIVO", {"ruta": "/archivo"}, plugins={"demo": DEMO}, root=raiz
    )
    assert escrito["ok"] is True
    assert escrito["item"]["ruta"] == "/archivo"

    items = ops.list_resource_items("demo", "destinos", plugins={"demo": DEMO}, root=raiz)["items"]
    assert [i["name"] for i in items] == ["ARCHIVO"]

    ops.delete_resource_item("demo", "destinos", "ARCHIVO", plugins={"demo": DEMO}, root=raiz)
    vacio = ops.list_resource_items("demo", "destinos", plugins={"demo": DEMO}, root=raiz)
    assert vacio["items"] == []


def test_write_resource_item_no_devuelve_el_secreto_ni_al_guardarlo(raiz):
    """
    `TableStore.write` sí se lo devuelve a quien lo escribió —pensado para una
    UI donde la misma persona lo tipeó—. Por este camino quien escribe puede
    ser un agente, así que se aplica el criterio de `resource_items_masked`.
    """
    escrito = ops.write_resource_item(
        "demo",
        "destinos",
        "CON-TOKEN",
        {"ruta": "/archivo", "token": "no-debe-volver"},
        plugins={"demo": DEMO},
        root=raiz,
    )
    assert escrito["item"]["token"] is None
    assert "no-debe-volver" not in json.dumps(escrito)


def test_write_resource_item_rechaza_una_coleccion_que_no_existe(raiz):
    resultado = ops.write_resource_item(
        "demo", "no-existe", "K", {}, plugins={"demo": DEMO}, root=raiz
    )
    assert resultado["ok"] is False
    assert "no-existe" in resultado["error"]


# ── Dry run ─────────────────────────────────────────────────────────────


def test_dry_run_resuelve_variables_sin_ejecutar_nada(raiz):
    resultado = ops.dry_run_flow(
        str(FLOWS / "mundo.mmd"),
        row={"id": "0044"},
        case_id="0044",
        plugins={"demo": DEMO},
        root=raiz,
    )
    assert resultado["status"] == "ok"
    # La traza guarda el valor al que se habría resuelto, no la plantilla.
    traer = next(t for t in resultado["trace"] if t["node_id"] == "TRAER")
    assert traer["params"]["url"] == "https://api.test/casos/0044"


def test_dry_run_destaca_las_variables_sin_resolver(raiz):
    """
    En un dry-run los nodos que producen variables no corren, así que lo que
    queda sin resolver es esperable. Pero cuando el flujo está mal escrito
    —un typo en el nombre de un output— es ahí donde se ve, así que se sube
    a un campo propio en vez de quedar enterrado en el log.
    """
    resultado = ops.dry_run_flow(
        str(FLOWS / "mundo.mmd"), row={"id": "0044"}, plugins={"demo": DEMO}, root=raiz
    )
    assert "carpeta" in resultado["sin_resolver"]


def test_dry_run_de_un_flujo_con_reintentos_no_espera_tiempo_real(raiz):
    """
    `reintentos.mmd` declara esperas de 60s. En seco no se ejecuta ni una, así
    que esto tiene que volver enseguida y no en cuatro minutos.
    """
    resultado = ops.dry_run_flow(
        str(FLOWS / "reintentos.mmd"), row={"id": "0044"}, plugins={"demo": DEMO}, root=raiz
    )
    assert resultado["status"] == "ok"


# ── Verificación de un plugin ───────────────────────────────────────────


def test_load_plugin_acepta_uno_valido(raiz):
    resultado = ops.load_plugin("demo", DEMO, root=raiz)
    assert resultado["ok"] is True
    assert resultado["plugin"]["ports"] == ["http", "fs", "process", "clock"]
    assert "demo.mover" in resultado["plugin"]["tools"]
    assert resultado["plugin"]["actions"] == ["ping", "probar_destino"]


def test_load_plugin_reporta_un_port_sin_adapter(raiz, tmp_path):
    plugin = tmp_path / "raro.py"
    plugin.write_text(
        "from backend.core.contract import Plugin, PluginManifest\n"
        'PLUGIN = Plugin(manifest=PluginManifest(name="raro", label="R", '
        'ports=("telepatia",)))\n',
        encoding="utf-8",
    )
    resultado = ops.load_plugin("raro", str(plugin), root=raiz)
    assert resultado["ok"] is False
    assert "no existen" in resultado["errors"][0]


def test_load_plugin_reporta_un_error_de_import_sin_stacktrace(raiz, tmp_path):
    """
    Quien está iterando necesita el mensaje, no un traceback del intérprete
    subiendo por el servidor MCP.
    """
    plugin = tmp_path / "roto.py"
    plugin.write_text("import modulo_inexistente_xyz\n", encoding="utf-8")

    resultado = ops.load_plugin("roto", str(plugin), root=raiz)
    assert resultado["ok"] is False
    assert "modulo_inexistente_xyz" in resultado["errors"][0]


def test_load_plugin_de_una_ruta_que_no_existe(raiz):
    with pytest.raises(ops.OperationError):
        ops.load_plugin("x", "/ruta/que/no/existe.py", root=raiz)


# ── Instalación real de un plugin ────────────────────────────────────────


def _plugin_minimo(nombre: str) -> str:
    return (
        "from backend.core.contract import Plugin, PluginManifest\n"
        f'PLUGIN = Plugin(manifest=PluginManifest(name="{nombre}", label="{nombre}"))\n'
    )


def test_install_plugin_copia_un_archivo_suelto_como_paquete(raiz, tmp_path):
    """
    Un plugin de un solo `.py` no tiene forma de paquete: se normaliza a
    `plugins_dir/<name>/__init__.py`, que es lo único que
    `Instance._plugins_de_la_carpeta` reconoce ahí.
    """
    plugins_dir = tmp_path / "plugins"

    resultado = ops.install_plugin("demo", DEMO, str(plugins_dir), source="agent", root=raiz)

    assert resultado["ok"] is True
    assert resultado["name"] == "demo"
    assert resultado["source"] == "agent"
    destino = plugins_dir / "demo"
    assert (destino / "__init__.py").is_file()

    procedencia = json.loads((destino / ops.ARCHIVO_PROCEDENCIA).read_text())
    assert procedencia["name"] == "demo"
    assert procedencia["source"] == "agent"
    assert procedencia["version"] == resultado["version"]


def test_install_plugin_copia_una_carpeta_tal_cual(raiz, tmp_path):
    origen = tmp_path / "mi_paquete"
    origen.mkdir()
    (origen / "__init__.py").write_text(_plugin_minimo("mio"), encoding="utf-8")

    plugins_dir = tmp_path / "plugins"
    resultado = ops.install_plugin("mio", str(origen), str(plugins_dir), source="bucket", root=raiz)

    assert resultado["ok"] is True
    assert (plugins_dir / "mio" / "__init__.py").is_file()


def test_install_plugin_invalido_no_toca_disco(raiz, tmp_path):
    plugin = tmp_path / "raro.py"
    plugin.write_text(
        "from backend.core.contract import Plugin, PluginManifest\n"
        'PLUGIN = Plugin(manifest=PluginManifest(name="raro", label="R", '
        'ports=("telepatia",)))\n',
        encoding="utf-8",
    )
    plugins_dir = tmp_path / "plugins"

    resultado = ops.install_plugin("raro", str(plugin), str(plugins_dir), source="agent", root=raiz)

    assert resultado["ok"] is False
    assert resultado["errors"]
    assert not plugins_dir.exists()


def test_install_plugin_reinstala_reemplazando_entero(raiz, tmp_path):
    """
    Reinstalar no mezcla contenidos de las dos versiones: la vieja desaparece
    entera al renombrar la carpeta temporal sobre el destino.
    """
    plugins_dir = tmp_path / "plugins"
    primero = ops.install_plugin("demo", DEMO, str(plugins_dir), source="agent", root=raiz)
    assert primero["ok"] is True

    origen_v2 = tmp_path / "demo_v2"
    origen_v2.mkdir()
    (origen_v2 / "__init__.py").write_text(_plugin_minimo("demo"), encoding="utf-8")
    (origen_v2 / "extra.py").write_text("x = 1\n", encoding="utf-8")

    segundo = ops.install_plugin("demo", str(origen_v2), str(plugins_dir), source="agent", root=raiz)

    assert segundo["ok"] is True
    destino = plugins_dir / "demo"
    assert (destino / "extra.py").is_file()
    assert "PluginManifest" in (destino / "__init__.py").read_text()


def test_install_plugin_rechaza_cuando_origen_esta_dentro_del_destino(raiz, tmp_path):
    """
    Issue #2: instalar un archivo que vive dentro de su propia carpeta destino
    —el bug real: `plugins_dir` apuntando al checkout de desarrollo en vez de
    a una instalación separada— no puede terminar en un rmtree que borra todo
    lo que no sea ese archivo (un __init__.py de re-export, una carpeta de
    fixtures, lo que sea que compartiera carpeta con él).
    """
    plugins_dir = tmp_path / "plugins"
    paquete = plugins_dir / "archivos"
    paquete.mkdir(parents=True)
    (paquete / "plugin.py").write_text(_plugin_minimo("archivos"), encoding="utf-8")
    (paquete / "__init__.py").write_text("from .plugin import PLUGIN\n", encoding="utf-8")
    prueba = paquete / "prueba"
    prueba.mkdir()
    (prueba / "fixture.txt").write_text("dato de prueba\n", encoding="utf-8")

    with pytest.raises(ops.OperationError) as exc:
        ops.install_plugin(
            "archivos", str(paquete / "plugin.py"), str(plugins_dir), source="agent", root=raiz
        )

    assert "se pisan" in str(exc.value)
    assert (paquete / "plugin.py").is_file()
    assert (paquete / "__init__.py").is_file()
    assert (prueba / "fixture.txt").is_file()


def test_install_plugin_no_borra_el_destino_anterior_lo_deja_de_backup(raiz, tmp_path):
    """
    Reemplazar un destino existente no es un rmtree: lo que hubiera ahí queda
    renombrado al lado, recuperable a mano.
    """
    plugins_dir = tmp_path / "plugins"
    primero = ops.install_plugin("demo", DEMO, str(plugins_dir), source="agent", root=raiz)
    assert primero["ok"] is True
    destino = plugins_dir / "demo"
    (destino / "marca.txt").write_text("algo que no viene de la instalación\n", encoding="utf-8")

    origen_v2 = tmp_path / "demo_v2"
    origen_v2.mkdir()
    (origen_v2 / "__init__.py").write_text(_plugin_minimo("demo"), encoding="utf-8")

    segundo = ops.install_plugin("demo", str(origen_v2), str(plugins_dir), source="agent", root=raiz)

    assert segundo["ok"] is True
    assert segundo["backup"] is not None
    backup = pathlib.Path(segundo["backup"])
    assert backup.is_dir()
    assert (backup / "marca.txt").read_text(encoding="utf-8") == (
        "algo que no viene de la instalación\n"
    )
    assert not (destino / "marca.txt").exists()


# ── Guardar un flujo sin ejecutarlo ──────────────────────────────────────


def test_save_flow_persiste_sin_ejecutar_nada(raiz, tmp_path):
    flujo = tmp_path / "mi-flujo.mmd"
    flujo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.log | message=hola"]\n    B --> N\n'
    )

    resultado = ops.save_flow(str(flujo), root=raiz)

    assert resultado["ok"] is True
    assert resultado["name"] == "mi-flujo"
    # Queda disponible por nombre, no sólo en el archivo de origen.
    assert ops.check_flow("mi-flujo", root=raiz)["ok"] is True


def test_save_flow_admite_un_nombre_propio(raiz, tmp_path):
    flujo = tmp_path / "x.mmd"
    flujo.write_text(
        'flowchart TD\n    B(inicio)\n    N["core.log | message=hola"]\n    B --> N\n'
    )

    resultado = ops.save_flow(str(flujo), name="otro-nombre", root=raiz)

    assert resultado["name"] == "otro-nombre"
    assert ops.check_flow("otro-nombre", root=raiz)["ok"] is True


def test_save_flow_de_un_archivo_inexistente_no_persiste(raiz):
    resultado = ops.save_flow("no-existe.mmd", root=raiz)
    assert resultado["ok"] is False


# ── El bucle completo ───────────────────────────────────────────────────


def test_el_bucle_de_autoria_de_punta_a_punta(raiz, tmp_path):
    """
    El caso de uso real, entero: se pide algo, falta un tool, se genera el
    plugin desde la plantilla, se verifica, y el flujo pasa a validar.

    Que la plantilla produzca un plugin que carga a la primera es la propiedad
    que importa: si el andamio generara código inválido, todo el bucle empieza
    con una corrección en lugar de con un avance.
    """
    plantilla = ops.plugin_template(name="clima", ports=["http"])
    plugin = tmp_path / plantilla["filename"]
    plugin.write_text(plantilla["code"], encoding="utf-8")

    assert plantilla["ports_declarados"] == ["http"]
    assert ops.load_plugin("clima", str(plugin), root=raiz)["ok"] is True

    flujo = tmp_path / "consulta.mmd"
    flujo.write_text(
        "flowchart TD\n"
        "    SN(inicio)\n"
        '    T["traer § clima.hacer | url=https://api.test/{ciudad}"]\n'
        '    L["registrar § core.log | message=listo {ciudad}"]\n'
        "    SN --> T\n"
        "    T -->|ok| L\n",
        encoding="utf-8",
    )

    assert ops.check_flow(str(flujo), root=raiz)["ok"] is False  # falta el plugin

    locales = {"clima": str(plugin)}
    assert ops.check_flow(str(flujo), plugins=locales, root=raiz)["ok"] is True

    seco = ops.dry_run_flow(
        str(flujo), row={"id": "1", "ciudad": "Rosario"}, plugins=locales, root=raiz
    )
    assert seco["status"] == "ok"
    assert seco["trace"][0]["params"]["url"] == "https://api.test/Rosario"


def test_la_plantilla_ignora_ports_que_no_existen():
    resultado = ops.plugin_template(name="x", ports=["http", "telepatia"])
    assert resultado["ports_declarados"] == ["http"]
    assert resultado["ports_ignorados"] == ["telepatia"]


def test_la_plantilla_sin_ports_igual_es_valida(raiz, tmp_path):
    plantilla = ops.plugin_template(name="simple")
    plugin = tmp_path / plantilla["filename"]
    plugin.write_text(plantilla["code"], encoding="utf-8")
    assert ops.load_plugin("simple", str(plugin), root=raiz)["ok"] is True


# ── Acciones ────────────────────────────────────────────────────────────


def test_run_action_inexistente_nombra_las_disponibles(raiz):
    resultado = ops.run_action("demo", "inventada", plugins={"demo": DEMO}, root=raiz)
    assert resultado["status"] == "err"
    assert "ping" in resultado["message"]


def _sembrar_destino(raiz, key, **campos):
    """Escribe un item de `destinos` directo a la base, sin pasar por el MCP."""
    from backend.core.instance import Instance
    from backend.tests.demo_plugin import DESTINOS

    inst = Instance(raiz)
    try:
        inst.resource_store("demo", DESTINOS).write(key, campos)
    finally:
        inst.close()


def test_list_resource_items_tapa_secrets(raiz):
    """
    Issue #7: un agente tiene que poder ver qué "sources" existen sin que el
    campo `secret` (token, acá) viaje nunca en claro.
    """
    _sembrar_destino(raiz, "frio", ruta="/deposito", token="Bearer abc123")

    resultado = ops.list_resource_items("demo", "destinos", plugins={"demo": DEMO}, root=raiz)

    assert resultado["key_field"] == "name"
    item = next(i for i in resultado["items"] if i["name"] == "frio")
    assert item["ruta"] == "/deposito"
    assert item["token"] is None


def test_describe_extra_params_sin_describer_es_vacio(raiz):
    """
    Issue #27: la operación llega hasta `Instance.describe_extra_params` por
    el mismo camino que el resto (CLI en subproceso, --json). La mayoría de
    los tools no describe una forma dinámica: vacío, no error.
    """
    resultado = ops.describe_extra_params("demo.mover", plugins={"demo": DEMO}, root=raiz)
    assert resultado == {"tool": "demo.mover", "params": []}


def test_run_action_con_item_resuelve_params_desde_el_resource(raiz):
    """
    La otra mitad del issue #7: "ingresar un source" es una sola llamada, sin
    reconstruir la config del item a mano.
    """
    _sembrar_destino(raiz, "frio", ruta="/deposito", token="secreto-de-verdad")

    resultado = ops.run_action(
        "demo", "probar_destino", item="frio", plugins={"demo": DEMO}, root=raiz
    )

    assert resultado["status"] == "ok"
    assert resultado["outputs"]["ruta"] == "/deposito"
    assert resultado["outputs"]["token"] == "secreto-de-verdad"


def test_run_action_con_item_y_params_explicitos_estos_pisan(raiz):
    _sembrar_destino(raiz, "frio", ruta="/deposito", token="secreto-de-verdad")

    resultado = ops.run_action(
        "demo", "probar_destino", params={"ruta": "/otro"}, item="frio",
        plugins={"demo": DEMO}, root=raiz,
    )

    assert resultado["outputs"]["ruta"] == "/otro"
    assert resultado["outputs"]["token"] == "secreto-de-verdad"


def test_run_action_con_item_inexistente_es_err(raiz):
    resultado = ops.run_action(
        "demo", "probar_destino", item="no-existe", plugins={"demo": DEMO}, root=raiz
    )
    assert resultado["status"] == "err"
    assert "no-existe" in resultado["message"]


def test_run_action_con_item_en_accion_sin_resource_es_err(raiz):
    resultado = ops.run_action(
        "demo", "ping", item="frio", plugins={"demo": DEMO}, root=raiz
    )
    assert resultado["status"] == "err"
    assert "no está atada a ninguna colección" in resultado["message"]


# ── El cableado del protocolo ───────────────────────────────────────────


def test_cada_tool_declarada_tiene_su_handler():
    """Deriva silenciosa: agregar una tool y olvidar el handler, o al revés."""
    from backend.mcp.server import HANDLERS, TOOLS

    assert {t.name for t in TOOLS} == set(HANDLERS)


def test_los_esquemas_estan_bien_formados():
    from backend.mcp.server import TOOLS

    for tool in TOOLS:
        assert tool.description, f"{tool.name} sin descripción"
        esquema = tool.input_schema
        assert esquema["type"] == "object"
        # Todo lo obligatorio tiene que estar declarado como propiedad.
        for obligatorio in esquema.get("required", []):
            assert obligatorio in esquema["properties"], f"{tool.name}: {obligatorio}"


def test_la_lista_de_tools_no_crece_con_los_plugins():
    """
    La superficie del servidor es fija. Proyectar cada tool de plugin como tool
    de MCP rompería tres cosas: un tool sin run no tiene contexto ni traza, la
    lista se negocia al conectar (así que un plugin instalado a mitad de sesión
    no aparecería), y cuarenta tools compiten por la atención del agente cuando
    hacen falta ocho.
    """
    from backend.mcp.server import TOOLS

    assert {t.name for t in TOOLS} == {
        "describe_installation",
        "list_tools",
        "list_plugins",
        "list_resource_items",
        "describe_extra_params",
        "list_ports",
        "list_users",
        "list_flows",
        "get_flow",
        "list_runs",
        "get_run",
        "get_case_log",
        "write_resource_item",
        "delete_resource_item",
        "check_flow",
        "dry_run_flow",
        "load_plugin",
        "install_plugin",
        "save_flow",
        "run_action",
        "run_flow",
        "plugin_template",
    }


def test_una_tool_desconocida_vuelve_como_error_y_no_tumba_el_servidor():
    """
    Un fallo tiene que llegar como resultado con `is_error`, no como excepción
    de protocolo: así quien llama lo lee y corrige, en vez de recibir un error
    de transporte que no dice nada.
    """
    from backend.mcp.server import call_tool

    resultado = call_tool("no_existe_esta_tool")
    assert resultado.is_error is True
    assert "Disponibles" in resultado.content[0].text


def test_argumentos_invalidos_dan_un_mensaje_util():
    from backend.mcp.server import call_tool

    resultado = call_tool("check_flow", {"parametro_inventado": 1})
    assert resultado.is_error is True
    assert "argumentos inválidos" in resultado.content[0].text


def test_un_resultado_bueno_viaja_estructurado_y_como_texto(tmp_path):
    """
    `structured_content` para consumo automático, `content` en texto para que
    quede legible en la transcripción. Las dos formas del mismo dato.
    """
    from backend.mcp.server import call_tool

    resultado = call_tool("list_ports")
    assert resultado.is_error is not True
    assert "http" in json.dumps(resultado.structured_content)
    assert "http" in resultado.content[0].text


def test_las_instrucciones_documentan_la_sintaxis_del_dsl():
    """
    Un agente que no conoce el DSL no puede escribir un flujo. Las
    instrucciones del servidor son el único lugar donde lo va a leer.
    """
    from backend.mcp.server import INSTRUCCIONES

    for pieza in ("flowchart TD", "§", "|ok|", "{env.", "retry_gate", "ctx.port"):
        assert pieza in INSTRUCCIONES, f"falta documentar: {pieza}"


# ── Ejecución real ──────────────────────────────────────────────────────


def test_run_flow_exige_root(raiz):
    """
    Las demás operaciones caen en `backend/` si nadie dice nada, y en seco casi
    no molesta. Con ejecución real sí: runs de prueba mezclados con los de
    producción, indistinguibles después.
    """
    with pytest.raises(ops.OperationError, match="necesita `root`"):
        ops.run_flow("x.mmd", root="")


def test_run_flow_sin_el_actor_dado_de_alta_explica_como_crearlo(raiz):
    """
    El actor no se autocrea: habilitar algo que actúa sin nadie mirando tiene
    que ser un acto deliberado de quien opera la instalación.
    """
    with pytest.raises(ops.OperationError) as exc:
        ops.run_flow(str(FLOWS / "lineal.mmd"), root=raiz, row={"id": "1"})
    assert "users add" in str(exc.value)


def _alta(raiz, nombre="agente-mcp", kind="agent"):
    ops._cli("users", "add", nombre, "--kind", kind, "--json", root=raiz)


def test_run_flow_ejecuta_de_verdad_y_deja_traza(raiz):
    _alta(raiz)
    resultado = ops.run_flow(str(FLOWS / "lineal.mmd"), root=raiz, row={"id": "0044"})

    assert resultado["status"] == "ok"
    assert resultado["actor"] == "agente-mcp"
    assert resultado["run_id"]
    assert any("procesando 0044" in linea for linea in resultado["logs"])


def test_resumen_de_run_no_pierde_el_error_kind():
    """
    Issue #4: `_resumen_de_run` recorta la traza completa a lo que un agente
    necesita ver -- pero recortar no puede perder la clasificación del error,
    que es justo lo que un disparador automático no tiene hoy.
    """
    resultado = {
        "status": "err",
        "message": "Detenido en \"F\": la sesión ya no sirve",
        "failed_node": "F",
        "error_kind": "sesion_vencida",
        "logs": [],
        "trace": [
            {
                "node_id": "F",
                "fn": "demo.algo",
                "params": {},
                "status": "err",
                "message": "la sesión ya no sirve",
                "error_kind": "sesion_vencida",
            }
        ],
    }
    resumen = ops._resumen_de_run(resultado)
    assert resumen["error_kind"] == "sesion_vencida"
    assert resumen["trace"][0]["error_kind"] == "sesion_vencida"


def test_run_flow_deniega_un_tool_peligroso_sin_tocar_nada(raiz, tmp_path):
    """
    La propiedad que justifica todo el mecanismo: el agente no puede, y el
    mundo queda intacto. Falla en el chequeo previo, antes de ejecutar.
    """
    _alta(raiz)
    flujo = tmp_path / "peligro.mmd"
    destino = tmp_path / "archivo"
    origen = tmp_path / "casos" / "0044"
    origen.mkdir(parents=True)
    (origen / "a.stl").write_text("x")
    destino.mkdir()
    flujo.write_text(
        "flowchart TD\n"
        "    B(inicio)\n"
        f'    M["archivar § demo.mover | origen={origen}, destino={destino}"]\n'
        "    B --> M\n",
        encoding="utf-8",
    )

    with pytest.raises(ops.OperationError) as exc:
        ops.run_flow(str(flujo), root=raiz, plugins={"demo": DEMO}, row={"id": "0044"})

    assert "no tiene permiso" in str(exc.value)
    assert (origen / "a.stl").exists()          # no se movió
    assert list(destino.iterdir()) == []


def _flujo_tool_inexistente(tmp_path):
    flujo = tmp_path / "roto.mmd"
    flujo.write_text(
        "flowchart TD\n"
        "    B(inicio)\n"
        '    N["archivos.copiar"]\n'
        '    E["core.log | message=manejado"]\n'
        "    B --> N\n"
        "    N -->|err| E\n",
        encoding="utf-8",
    )
    return flujo


def test_run_flow_con_tool_inexistente_falla_antes_de_ejecutar(raiz, tmp_path):
    """
    Issue #9: antes esto terminaba en status "ok" con el nodo roto escondido
    detrás de `|err|`. Mismo criterio que un tool peligroso sin permiso: falla
    en el chequeo previo, antes de ejecutar nada.
    """
    _alta(raiz)
    flujo = _flujo_tool_inexistente(tmp_path)

    with pytest.raises(ops.OperationError) as exc:
        ops.run_flow(str(flujo), root=raiz, row={"id": "0044"})

    assert "archivos.copiar" in str(exc.value)


def test_run_flow_allow_broken_corre_igual(raiz, tmp_path):
    _alta(raiz)
    flujo = _flujo_tool_inexistente(tmp_path)

    resultado = ops.run_flow(str(flujo), root=raiz, row={"id": "0044"}, allow_broken=True)

    assert resultado["status"] == "ok"


def test_un_humano_si_puede(raiz, tmp_path):
    _alta(raiz, "operador", "human")
    flujo = tmp_path / "mueve.mmd"
    origen = tmp_path / "casos" / "0044"
    origen.mkdir(parents=True)
    (origen / "a.stl").write_text("x")
    destino = tmp_path / "archivo"
    destino.mkdir()
    flujo.write_text(
        "flowchart TD\n"
        "    B(inicio)\n"
        f'    M["archivar § demo.mover | origen={origen}, destino={destino}"]\n'
        "    B --> M\n",
        encoding="utf-8",
    )

    resultado = ops.run_flow(
        str(flujo), root=raiz, actor="operador", plugins={"demo": DEMO}, row={"id": "0044"}
    )
    assert resultado["status"] == "ok"
    assert (destino / "0044" / "a.stl").exists()


def test_list_users_dice_que_puede_cada_uno(raiz):
    _alta(raiz)
    usuarios = ops.list_users(root=raiz)["users"]
    agente = next(u for u in usuarios if u["name"] == "agente-mcp")
    assert agente["kind"] == "agent"
    assert agente["can_run_dangerous"] is False


def test_las_instrucciones_documentan_la_ejecucion_real():
    from backend.mcp.server import INSTRUCCIONES

    for pieza in ("run_flow", "root", "dangerous", "users allow"):
        assert pieza in INSTRUCCIONES, f"falta documentar: {pieza}"


# ── Defaults del servidor: root y plugins (issue #16) ───────────────────


def test_con_defaults_completa_root_cuando_el_agente_no_lo_manda():
    from backend.mcp.server import con_defaults

    esquema = {"properties": {"flow": {}, "root": {}, "plugins": {}}}
    completos = con_defaults({"flow": "x"}, esquema, root="/instalacion")
    assert completos == {"flow": "x", "root": "/instalacion"}


def test_lo_que_manda_el_agente_le_gana_al_default():
    """Así puede apuntar a otra instalación en vez de a la que sirve la app."""
    from backend.mcp.server import con_defaults

    esquema = {"properties": {"root": {}}}
    completos = con_defaults({"root": "/otra"}, esquema, root="/instalacion")
    assert completos["root"] == "/otra"


def test_los_plugins_se_mergean_con_los_del_agente_arriba():
    """
    No es uno u otro: el servidor declara los de la instalación —que el agente
    no puede conocer— y el agente igual puede sumar o pisar el suyo para
    probar una versión propia sin instalarla.
    """
    from backend.mcp.server import con_defaults

    esquema = {"properties": {"plugins": {}}}
    completos = con_defaults(
        {"plugins": {"demo": "/mio.py"}},
        esquema,
        plugins={"connections": "/app.py", "demo": "/instalado.py"},
    )
    assert completos["plugins"] == {"connections": "/app.py", "demo": "/mio.py"}


def test_una_tool_que_no_acepta_root_no_lo_recibe():
    """
    `list_ports` se responde desde el contrato y no toca la instalación. Si se
    le colara un `root`, fallaría con "argumentos inválidos".
    """
    from backend.mcp.server import TOOLS, con_defaults

    esquema = next(t for t in TOOLS if t.name == "list_ports").input_schema
    assert con_defaults({}, esquema, root="/instalacion", plugins={"x": "/y"}) == {}


def test_parse_args_toma_los_flags():
    from backend.mcp.server import parse_args

    root, plugins = parse_args(["--root", "/inst", "--plugin", "connections=/c.py"])
    assert root == "/inst"
    assert plugins == {"connections": "/c.py"}


def test_parse_args_cae_al_entorno(monkeypatch):
    """Un cliente MCP que sólo deja configurar comando y entorno usa esto."""
    from backend.mcp.server import ENV_PLUGINS, ENV_ROOT, parse_args

    monkeypatch.setenv(ENV_ROOT, "/desde-el-entorno")
    monkeypatch.setenv(ENV_PLUGINS, "connections=/c.py;otro=/o.py")

    root, plugins = parse_args([])
    assert root == "/desde-el-entorno"
    assert plugins == {"connections": "/c.py", "otro": "/o.py"}


def test_el_flag_le_gana_al_entorno(monkeypatch):
    from backend.mcp.server import ENV_ROOT, parse_args

    monkeypatch.setenv(ENV_ROOT, "/entorno")
    root, _ = parse_args(["--root", "/flag"])
    assert root == "/flag"


def test_el_prefijo_no_es_BOT_para_no_pisar_un_setting():
    """
    `BOT_<CLAVE>` ya es el prefijo con el que se pisa un setting de un plugin:
    cualquier `BOT_*` del entorno entra a `effective_config()`. Un `BOT_ROOT`
    dejaría un setting fantasma llamado "ROOT" en toda instalación.
    """
    from backend.core.config import ENV_PREFIX
    from backend.mcp.server import ENV_PLUGINS, ENV_ROOT

    assert not ENV_ROOT.startswith(ENV_PREFIX)
    assert not ENV_PLUGINS.startswith(ENV_PREFIX)
