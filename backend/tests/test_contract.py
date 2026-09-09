"""
Tests del contrato de plugins.

Cubren las garantías que el modelo anterior no daba. Corren con pytest, o
directamente con `python tests/test_contract.py`.
"""

from __future__ import annotations

import pytest

from backend.core.contract import (
    Action,
    Field,
    FunctionAction,
    FunctionTool,
    Param,
    ParamError,
    ParamType,
    Plugin,
    PluginManifest,
    PortNotDeclared,
    Resource,
    Setting,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.registry import ToolRegistry
from backend.core.resources import ResourceError, store_for
from backend.tests.demo_plugin import build_plugin as build_demo_plugin
from backend.tests.fakes import FakeFs, fake_adapters


def _registry(adapters=None):
    """Registro con el plugin de prueba cargado, igual que lo haría un entry point."""
    reg = ToolRegistry(adapters=adapters or fake_adapters())
    reg._add_plugin("demo", "tests.demo_plugin:PLUGIN", build_demo_plugin())
    return reg


def _ctx_factory(
    node_params: dict,
    config: dict | None = None,
    context: dict | None = None,
    ports: dict | None = None,
    resources=None,
):
    def factory(declaracion, ports_del_registry=None) -> ToolContext:
        # El mismo factory sirve para un tool y para una acción: una acción
        # declara `params` igual, sólo que no tiene manifest de tool.
        if isinstance(declaracion, Action):
            declaracion = ToolManifest(
                id=f"action.{declaracion.name}",
                label=declaracion.label,
                category="ACCIÓN",
                params=declaracion.params,
            )
        return ToolContext(
            run_id="run-test",
            case_id="0044",
            params=declaracion.resolve_params(node_params, config or {}),
            config=config or {},
            context=context or {},
            log=lambda m, level="info": None,
            resources=resources,
            ports=ports if ports is not None else (ports_del_registry or {}),
        )

    return factory


def _bare_registry(tool_id: str, fn, **manifest_kwargs) -> ToolRegistry:
    reg = ToolRegistry(adapters=fake_adapters())
    manifest = ToolManifest(id=tool_id, label="t", category="X", **manifest_kwargs)
    reg._add_plugin("t", "test", [FunctionTool(manifest=manifest, fn=fn)])
    return reg


def _run_bare(reg: ToolRegistry, tool_id: str) -> ToolResult:
    return reg.execute(
        tool_id,
        lambda m, ports: ToolContext(
            run_id="r", case_id="c", params={}, config={}, context={},
            log=lambda *_: None, ports=ports,
        ),
    )


# ── Descubrimiento ──────────────────────────────────────────────────────


def test_descubre_el_plugin():
    reg = _registry()
    assert not reg.errors
    assert reg.plugins[0].version == "1.0.0"
    assert "demo.mover" in {m.id for m in reg.manifests}


def test_catalogo_es_serializable():
    catalog = _registry().catalog()
    assert catalog["contract"] == 1
    tool = next(t for t in catalog["tools"] if t["id"] == "demo.mover")
    assert [p["name"] for p in tool["params"]] == ["origen", "destino"]
    assert [o["name"] for o in tool["outputs"]] == ["destino"]
    assert tool["dangerous"] is True


def test_concurrency_por_defecto_es_concurrent():
    manifest = ToolManifest(id="x.y", label="t", category="X")
    assert manifest.concurrency == "concurrent"
    assert manifest.to_dict()["concurrency"] == "concurrent"


def test_concurrency_exclusive_run_se_declara_y_viaja_en_el_catalogo():
    catalog = _registry().catalog()
    correr = next(t for t in catalog["tools"] if t["id"] == "demo.correr")
    assert correr["concurrency"] == "exclusive_run"


def test_el_catalogo_declara_extra_outputs():
    """
    Sin este flag en el catálogo, un validador marcaría como error toda
    variable que venga de un tool con outputs abiertos.
    """
    catalog = _registry().catalog()
    fetch = next(t for t in catalog["tools"] if t["id"] == "demo.fetch")
    assert fetch["extra_outputs"] is True
    assert fetch["extra_outputs_doc"]


def test_rechaza_ids_duplicados():
    reg = _registry()
    reg._add_plugin("demo_otra_vez", "test", build_demo_plugin())
    assert any("duplicado" in e.error for e in reg.errors)


def test_plugin_sin_manifest_igual_carga():
    """Modo mínimo: exponer sólo la lista de tools, sin configuración declarada."""
    reg = ToolRegistry(adapters=fake_adapters())
    reg._add_plugin("suelto", "test", list(build_demo_plugin().tools))
    assert not reg.errors
    assert reg.plugins[0].manifest is None
    assert reg.plugins[0].ports == ()
    assert "demo.mover" in {m.id for m in reg.manifests}


# ── Settings del plugin: lo que alimenta el wizard ──────────────────────


def test_el_catalogo_expone_los_settings_del_plugin():
    """La UI de configuración se dibuja con esto, no con HTML por integración."""
    plugin = _registry().catalog()["plugins"][0]
    assert plugin["label"] == "Demo"
    claves = {s["key"]: s for s in plugin["settings"]}
    assert claves["demoBase"]["type"] == "path"
    assert claves["demoTimeout"]["default"] == 10.0


def test_config_faltante_se_reporta_por_plugin():
    manifest = PluginManifest(
        name="p", label="P", settings=(Setting("obligatorio", required=True),)
    )
    reg = ToolRegistry(adapters=fake_adapters())
    reg._add_plugin("p", "test", Plugin(manifest=manifest))
    faltantes = reg.missing_config({})
    assert list(faltantes) == ["p"]
    assert [m.key for m in faltantes["p"]] == ["obligatorio"]


def test_config_completa_no_reporta_faltantes():
    assert _registry().missing_config({}) == {}


def test_setting_con_valor_de_tipo_invalido_se_reporta():
    manifest = PluginManifest(
        name="p",
        label="P",
        settings=(Setting("timeout", ParamType.INT, required=True),),
    )
    faltantes = manifest.validate_config({"timeout": "un rato"})
    assert [m.key for m in faltantes] == ["timeout"]
    assert "no es un entero" in faltantes[0].reason


def test_missing_config_se_limita_a_los_plugins_del_flujo():
    """
    Un dry-run debe avisar sólo de la config que el flujo realmente necesita:
    "falta configurar X" para un plugin que este flujo ni usa es ruido.
    """
    manifest = PluginManifest(
        name="otro", label="Otro", settings=(Setting("falta", required=True),)
    )
    tool = FunctionTool(
        manifest=ToolManifest(id="otro.hacer", label="h", category="X"),
        fn=lambda ctx: ToolResult.ok(),
    )
    reg = _registry()
    reg._add_plugin("otro", "test", Plugin(manifest=manifest, tools=[tool]))

    assert list(reg.missing_config({}, ["otro.hacer"])) == ["otro"]
    assert list(reg.missing_config({}, ["demo.mover"])) == []


def test_defaults_precargan_la_configuracion_inicial():
    defaults = _registry().plugins[0].manifest.defaults()
    assert defaults == {"demoBase": "/casos", "demoTimeout": 10.0}


def test_plugin_con_contrato_no_soportado_se_rechaza():
    reg = ToolRegistry()
    manifest = PluginManifest(name="futuro", label="Futuro", contract=99)
    try:
        reg._add_plugin("futuro", "test", Plugin(manifest=manifest, tools=[]))
        raise AssertionError("debió rechazar el contrato v99")
    except TypeError as exc:
        assert "contrato v99" in str(exc)


# ── Nada falla en silencio ──────────────────────────────────────────────


def test_tool_inexistente_da_error():
    """El typo en un ID de nodo: antes el flujo seguía por la rama de éxito."""
    result = _registry().execute("SF", _ctx_factory({}))
    assert result.status == "err"
    assert "SF" in result.message


def test_excepcion_se_convierte_en_error_con_traceback():
    def boom(ctx):
        raise RuntimeError("explotó")

    result = _run_bare(_bare_registry("t.boom", boom), "t.boom")
    assert result.status == "err"
    assert "RuntimeError: explotó" in result.message
    assert result.traceback and "RuntimeError" in result.traceback


def test_status_no_declarado_se_rechaza():
    reg = _bare_registry("t.raro", lambda ctx: ToolResult(status="quizas"))
    assert _run_bare(reg, "t.raro").status == "err"


def test_tool_que_no_devuelve_result_se_rechaza():
    reg = _bare_registry("t.malo", lambda ctx: "listo")
    result = _run_bare(reg, "t.malo")
    assert result.status == "err"
    assert "ToolResult" in result.message


def test_param_obligatorio_ausente_da_error():
    result = _registry().execute("demo.mover", _ctx_factory({}))
    assert result.status == "err"
    assert "origen" in result.message


def test_tool_desconocido_sugiere_lo_que_hay():
    result = _registry().execute("demo.mvoer", _ctx_factory({}))
    assert result.status == "err"
    assert "demo.mover" in result.message


# ── Sin contaminación entre nodos ───────────────────────────────────────


def test_params_no_declarados_se_descartan():
    """Sin esto, un param sobrante de un nodo se filtra al tool siguiente."""
    manifest = _registry().manifest("demo.mover")
    resolved = manifest.resolve_params(
        {"origen": "a", "destino": "b", "sobrante": "no debería llegar"}, {}
    )
    assert set(resolved) == {"origen", "destino"}


# ── Precedencia y tipos ─────────────────────────────────────────────────


def test_precedencia_nodo_config_default():
    manifest = ToolManifest(
        id="t.y",
        label="y",
        category="X",
        params=(Param("modo", config_key="modoPorDefecto", default="rapido"),),
    )
    assert manifest.resolve_params({}, {})["modo"] == "rapido"
    assert manifest.resolve_params({}, {"modoPorDefecto": "lento"})["modo"] == "lento"
    assert manifest.resolve_params({"modo": "manual"}, {"modoPorDefecto": "lento"})["modo"] == "manual"


def test_coercion_de_tipos():
    manifest = ToolManifest(
        id="t.x",
        label="x",
        category="X",
        params=(
            Param("n", ParamType.INT),
            Param("f", ParamType.FLOAT),
            Param("b", ParamType.BOOL),
        ),
    )
    resolved = manifest.resolve_params({"n": "60", "f": "0.8", "b": "si"}, {})
    assert resolved == {"n": 60, "f": 0.8, "b": True}


def test_int_invalido_da_param_error():
    manifest = ToolManifest(
        id="t.x", label="x", category="X", params=(Param("n", ParamType.INT),)
    )
    try:
        manifest.resolve_params({"n": "sesenta"}, {})
        raise AssertionError("debió rechazar 'sesenta'")
    except ParamError as exc:
        assert "no es un entero" in str(exc)


def test_enum_invalido_da_param_error():
    manifest = ToolManifest(
        id="t.x",
        label="x",
        category="X",
        params=(Param("modo", ParamType.ENUM, choices=("rapido", "lento")),),
    )
    try:
        manifest.resolve_params({"modo": "turbo"}, {})
        raise AssertionError("debió rechazar 'turbo'")
    except ParamError as exc:
        assert "rapido, lento" in str(exc)


def test_id_de_tool_sin_namespace_se_rechaza():
    try:
        ToolManifest(id="sinpunto", label="x", category="X")
        raise AssertionError("debió rechazar un id sin namespace")
    except ValueError as exc:
        assert "namespace" in str(exc)


# ── Camino feliz, con los ports inyectados ──────────────────────────────


def test_un_tool_usa_el_port_que_declaro_y_devuelve_su_output():
    """
    El plugin nunca instancia su adapter: lo recibe. Este test es la prueba de
    que la inyección llega hasta adentro del tool.
    """
    adapters = fake_adapters()
    adapters["fs"] = FakeFs(files={"/casos/0044/a.stl": "x"}, dirs=["/archivo"])
    reg = _registry(adapters)

    result = reg.execute(
        "demo.mover",
        _ctx_factory({"origen": "/casos/0044", "destino": "/archivo"}),
    )
    assert result.status == "ok"
    assert result.outputs["destino"] == "/archivo/0044"
    assert adapters["fs"].files == {"/archivo/0044/a.stl": "x"}


def test_un_tool_lee_su_coleccion_sin_saber_donde_esta_guardada():
    adapters = fake_adapters()
    adapters["fs"] = FakeFs(dirs=["/casos/0044", "/deposito"])
    reg = _registry(adapters)

    result = reg.execute(
        "demo.mover",
        _ctx_factory(
            {"origen": "/casos/0044", "destino": "archivo-frio"},
            resources=lambda coleccion: [{"name": "archivo-frio", "ruta": "/deposito"}],
        ),
    )
    assert result.status == "ok"
    assert result.outputs["destino"] == "/deposito/0044"


def test_un_fallo_del_port_se_convierte_en_err_con_traceback():
    """
    Un `PortError` no se escapa: el registry lo convierte en un ToolResult con
    status err. Es la garantía de que nada falla en silencio, aplicada a la
    capa de infraestructura.
    """
    reg = _registry()  # FakeFs vacío: la carpeta no existe
    result = reg.execute(
        "demo.mover",
        _ctx_factory({"origen": "/casos/no-existe", "destino": "/x"}),
    )
    assert result.status == "err"
    assert "no existe el origen" in result.message.lower()
    assert result.traceback and "PortError" in result.traceback


def test_un_status_http_de_error_es_dato_y_no_excepcion():
    """
    Un 500 vuelve como respuesta, no como PortError: el flujo tiene que poder
    ramificar por |err| y la traza guardar el cuerpo.
    """
    adapters = fake_adapters()
    adapters["http"].stub("https://api.test/casos/1", status=500, text='{"error": "boom"}')
    reg = _registry(adapters)

    result = reg.execute("demo.fetch", _ctx_factory({"url": "https://api.test/casos/1"}))
    assert result.status == "err"
    assert result.outputs["status"] == 500
    assert result.outputs["response"] == {"error": "boom"}
    assert result.traceback is None  # no fue una excepción


def test_un_tool_no_puede_usar_un_port_que_su_plugin_no_declaro():
    """
    Si alcanzara con pedirlo en runtime, la declaración del manifest sería
    decorativa y el catálogo mentiría sobre qué puede hacer cada plugin.
    """
    def curioso(ctx):
        ctx.port("process").run(["rm", "-rf", "/"])
        return ToolResult.ok()

    manifest = PluginManifest(name="curioso", label="Curioso", ports=("fs",))
    tool = FunctionTool(
        manifest=ToolManifest(id="curioso.espiar", label="x", category="X"), fn=curioso
    )
    reg = ToolRegistry(adapters=fake_adapters())
    reg._add_plugin("curioso", "test", Plugin(manifest=manifest, tools=[tool]))

    result = reg.execute("curioso.espiar", _ctx_factory({}, ports=None))
    assert result.status == "err"
    assert "no declaró el port 'process'" in result.message


def test_un_plugin_que_pide_un_port_sin_adapter_no_carga():
    """
    Falla al cargar, no a mitad de un run. Media instalación funcionando es
    peor que ninguna: el flujo corre hasta el nodo que necesitaba el port y
    recién ahí se cae, en producción.
    """
    reg = ToolRegistry(adapters={"clock": fake_adapters()["clock"]})
    reg._add_plugin("demo", "test", build_demo_plugin())

    assert reg.tool_ids == []  # ni un tool a medias
    assert any("no hay adapter atado" in e.error for e in reg.errors)
    assert "http" in reg.errors[0].error


def test_un_plugin_que_pide_un_port_inexistente_no_carga():
    manifest = PluginManifest(name="raro", label="Raro", ports=("telepatia",))
    reg = ToolRegistry(adapters=fake_adapters())
    reg._add_plugin("raro", "test", Plugin(manifest=manifest))

    assert any("no existen" in e.error for e in reg.errors)


def test_el_catalogo_publica_que_ports_usa_cada_plugin():
    """La superficie de riesgo de cada plugin, visible sin leer su código."""
    catalogo = _registry().catalog()
    plugin = catalogo["plugins"][0]
    assert set(plugin["ports"]) == {"http", "fs", "process", "clock"}
    assert set(catalogo["ports"]) == {"http", "fs", "process", "clock", "browser"}


# ── Acciones ────────────────────────────────────────────────────────────


def test_una_accion_se_declara_en_el_manifest_y_se_ejecuta():
    adapters = fake_adapters()
    adapters["http"].stub("https://api.test/ping", status=200, text="{}")
    reg = _registry(adapters)

    assert [a.name for a in reg.actions_of("demo")] == ["ping"]
    result = reg.execute_action(
        "demo", "ping", _ctx_factory({"url": "https://api.test/ping"})
    )
    assert result.status == "ok"
    assert "200" in result.message


def test_una_accion_inexistente_da_error_con_las_disponibles():
    result = _registry().execute_action("demo", "inventada", _ctx_factory({}))
    assert result.status == "err"
    assert "ping" in result.message


def test_una_accion_sin_declarar_en_el_manifest_no_se_registra():
    """
    El manifest es lo que se publica. Un handler sin declaración sería una
    acción invisible en el catálogo y ejecutable igual.
    """
    manifest = PluginManifest(name="p", label="P")
    plugin = Plugin(
        manifest=manifest,
        actions=[
            FunctionAction(action=Action("oculta", "Oculta"), fn=lambda ctx: ToolResult.ok())
        ],
    )
    reg = ToolRegistry(adapters=fake_adapters())
    reg._add_plugin("p", "test", plugin)

    assert any("no está declarada en el manifest" in e.error for e in reg.errors)
    assert reg.execute_action("p", "oculta", _ctx_factory({})).status == "err"


def test_result_again_activa_el_loop():
    reg = _bare_registry("t.poll", lambda ctx: ToolResult.again("todavía no"))
    result = _run_bare(reg, "t.poll")
    assert result.status == "ok"
    assert result.loop is True


# ── Resources: cómo un plugin aporta su propia pantalla ─────────────────


def test_plugin_declara_su_resource(db):
    resource = _registry().plugins[0].manifest.resources[0]
    assert resource.name == "destinos"
    assert resource.key_field == "name"
    assert [f.name for f in resource.fields] == ["ruta", "token"]


def test_catalogo_expone_los_resources():
    plugin = _registry().catalog()["plugins"][0]
    resource = plugin["resources"][0]
    assert resource["label"] == "Destinos"
    assert resource["fields"][0]["required"] is True
    # Un campo marcado como secreto se declara como tal en el catálogo, para
    # que quien dibuje el formulario sepa que no lo tiene que mostrar en claro.
    assert resource["fields"][1]["secret"] is True


def test_resource_valida_items_contra_su_esquema():
    resource = _registry().plugins[0].manifest.resources[0]
    assert resource.validate_item({"ruta": "/x"}) == []
    assert "obligatorio" in resource.validate_item({"token": "t"})[0]


def _store_de_prueba(db, resource, plugin="demo"):
    return store_for(db, plugin, resource)


def test_store_generico_hace_abm_en_la_tabla(db):
    resource = _registry().plugins[0].manifest.resources[0]
    store = _store_de_prueba(db, resource)
    assert store.list_keys() == []

    store.write("mi destino", {"ruta": "/x"})
    assert store.list_keys() == ["mi destino"]
    assert store.read("mi destino")["ruta"] == "/x"

    # La clave vive en su columna, no dentro del item.
    assert "name" not in store.read("mi destino")

    store.delete("mi destino")
    assert store.list_keys() == []
    with pytest.raises(ResourceError):
        store.read("mi destino")


def test_store_rechaza_items_invalidos(db):
    resource = _registry().plugins[0].manifest.resources[0]
    store = _store_de_prueba(db, resource)
    with pytest.raises(ResourceError, match="obligatorio"):
        store.write("mala", {"token": "t"})


def test_guardar_no_necesita_configurar_nada(db):
    """
    Que un resource tuviera que declarar una carpeta antes de poder guardar
    significaba que no se podía crear un item hasta configurar dónde. Una
    instalación recién hecha guarda desde el primer minuto.
    """
    resource = _registry().plugins[0].manifest.resources[0]
    store = _store_de_prueba(db, resource)
    assert store.write("primero", {"ruta": "/x"})["name"] == "primero"


def test_dos_plugins_no_se_pisan_una_coleccion_con_el_mismo_nombre(db):
    """La tabla es una sola, así que el aislamiento por plugin tiene que ser real."""
    resource = _registry().plugins[0].manifest.resources[0]
    uno = store_for(db, "demo", resource)
    otro = store_for(db, "otro-plugin", resource)

    uno.write("compartida", {"ruta": "/uno"})
    otro.write("compartida", {"ruta": "/otro"})

    assert uno.read("compartida")["ruta"] == "/uno"
    assert otro.read("compartida")["ruta"] == "/otro"

    uno.delete("compartida")
    assert otro.list_keys() == ["compartida"]


def test_el_item_guarda_cuando_cambio(db):
    resource = _registry().plugins[0].manifest.resources[0]
    store = _store_de_prueba(db, resource)
    guardado = store.write("c", {"ruta": "/x"})
    assert guardado["_updated_at"] > 0


# ── Alias: renombrar sin romper flujos ──────────────────────────────────


def test_alias_de_tool_resuelve_al_id_nuevo():
    """
    Un plugin puede renombrar un tool sin romper los flujos ya escritos. Es lo
    que permite corregir un nombre en vez de convivir con él para siempre.
    """
    tool = FunctionTool(
        manifest=ToolManifest(
            id="p.nombre_nuevo", label="x", category="X", aliases=("p.nombre_viejo",)
        ),
        fn=lambda ctx: ToolResult.ok(),
    )
    reg = ToolRegistry(adapters=fake_adapters())
    reg._add_plugin("p", "test", [tool])

    assert reg.resolve_id("p.nombre_viejo") == "p.nombre_nuevo"
    assert reg.manifest("p.nombre_viejo").id == "p.nombre_nuevo"


def test_alias_de_param_sigue_funcionando():
    manifest = ToolManifest(
        id="p.x",
        label="x",
        category="X",
        params=(Param("destino", aliases=("carpetaDestino",)),),
    )
    viejo = manifest.resolve_params({"carpetaDestino": "/x"}, {})
    nuevo = manifest.resolve_params({"destino": "/x"}, {})
    assert viejo["destino"] == nuevo["destino"] == "/x"


def test_alias_en_conflicto_se_reporta():
    """Dos plugins reclamando el mismo alias no puede resolverse en silencio."""
    def _tool(tool_id):
        return FunctionTool(
            manifest=ToolManifest(id=tool_id, label="x", category="X", aliases=("viejo.id",)),
            fn=lambda ctx: ToolResult.ok(),
        )

    reg = ToolRegistry(adapters=fake_adapters())
    reg._add_plugin("uno", "test", [_tool("uno.hacer")])
    reg._add_plugin("dos", "test", [_tool("dos.hacer")])

    assert any("alias en conflicto" in e.error for e in reg.errors)


# ── Params abiertos ─────────────────────────────────────────────────────


def test_extra_params_van_aparte_de_los_declarados():
    """
    Un tool que ejecuta un request guardado recibe campos del payload que
    dependen de cuál sea. Los no declarados llegan por `extras`, separados de
    los declarados, para que el tool sepa cuáles son cuáles.
    """
    manifest = ToolManifest(
        id="p.enviar",
        label="x",
        category="X",
        extra_params=True,
        params=(Param("conexion", required=True),),
    )
    declarados, extras = manifest.split_params(
        {"conexion": "C", "id_externo": "0044", "texto": "hola"}, {}
    )
    assert declarados == {"conexion": "C"}
    assert extras == {"id_externo": "0044", "texto": "hola"}


def test_sin_extra_params_lo_no_declarado_se_descarta():
    manifest = _registry().manifest("demo.mover")
    _, extras = manifest.split_params(
        {"origen": "a", "destino": "b", "sobrante": "x"}, {}
    )
    assert extras == {}
