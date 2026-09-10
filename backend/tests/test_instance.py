"""
Tests de la instancia: configuración, historial de runs, secretos y el armado
completo con adapters inyectados.

Esta es la capa donde se decide con qué implementación concreta corre la
máquina. Que la suite pueda construirla con una base en memoria y adapters
falsos —sin ninguna rama "modo test" en el núcleo— es la propiedad que se está
probando acá, no un detalle del andamiaje.
"""

from __future__ import annotations

import os

import pytest

from backend.core.config import ConfigStore
from backend.core.flow.executor import RunResult
from backend.core.instance import Instance, WorkflowNotFound
from backend.core.stores import RunStore, StoreError

FLUJO_LOG = (
    "flowchart TD\n"
    "    B(inicio)\n"
    '    L["core.log | message=procesando {id}"]\n'
    "    B --> L\n"
)


# ── Configuración ───────────────────────────────────────────────────────


def test_config_persiste_y_mergea(db):
    config = ConfigStore(db)
    config.update({"a": "1"})
    config.update({"b": "2"})
    assert config.read() == {"a": "1", "b": "2"}


def test_config_write_reemplaza_todo(db):
    config = ConfigStore(db)
    config.update({"a": "1", "b": "2"})
    config.write({"c": "3"})
    assert config.read() == {"c": "3"}


def test_config_conserva_el_tipo(db):
    """
    Un setting tiene tipo: hay float, bool y json. Guardar el `str()` perdería
    el tipo en el viaje de ida y vuelta, y el tool recibiría "30.0" donde
    esperaba 30.0.
    """
    config = ConfigStore(db)
    config.update({"timeout": 30.0, "activo": True, "patrones": [{"label": "x"}]})
    leido = config.read()
    assert leido["timeout"] == 30.0
    assert leido["activo"] is True
    assert leido["patrones"] == [{"label": "x"}]


def test_config_borra_una_clave(db):
    config = ConfigStore(db)
    config.update({"a": "1", "b": "2"})
    config.delete("a")
    assert config.read() == {"b": "2"}


def test_variables_de_entorno_pisan_y_no_se_persisten(db, monkeypatch):
    """
    `BOT_*` existe para que un contenedor o un CI pisen un valor sin tocar la
    base. Si se persistiera, el override quedaría pegado después de sacar la
    variable — que es justo lo contrario de para qué está.
    """
    config = ConfigStore(db)
    config.update({"timeout": 10})

    # Llega como texto, que es lo que el entorno da; el tipado lo hace el
    # contrato al resolver el param contra su `Setting`.
    monkeypatch.setenv("BOT_timeout", "99")
    config.invalidate()
    assert config.read()["timeout"] == "99"

    monkeypatch.delenv("BOT_timeout")
    config.invalidate()
    assert config.read()["timeout"] == 10


# ── Historial de runs ───────────────────────────────────────────────────


def _result(run_id: str, case_id: str = "0044", status: str = "ok") -> RunResult:
    return RunResult(run_id=run_id, case_id=case_id, status=status)


def test_run_se_guarda_y_se_lee(db):
    store = RunStore(db)
    store.save(_result("r1"), flow="f", source="s")
    guardado = store.get("r1")
    assert guardado["case_id"] == "0044"
    assert guardado["flow"] == "f"


def test_un_run_nunca_se_sobrescribe(db):
    """
    La traza es evidencia. Un run que se pisa a sí mismo destruye el registro
    de por qué falló la vez anterior.
    """
    store = RunStore(db)
    store.save(_result("r1", status="err"), flow="f")
    with pytest.raises(StoreError):
        store.save(_result("r1", status="ok"), flow="f")
    assert store.get("r1")["status"] == "err"


def test_historial_ordena_del_mas_reciente(db):
    store = RunStore(db)
    for i in range(3):
        store.save(_result(f"r{i}"), flow="f", started_at=1000.0 + i)
    assert [r.run_id for r in store.list()] == ["r2", "r1", "r0"]


def test_historial_filtra_por_caso_y_por_fallo(db):
    store = RunStore(db)
    store.save(_result("ok1", case_id="A"), flow="f")
    store.save(_result("err1", case_id="A", status="err"), flow="f")
    store.save(_result("ok2", case_id="B"), flow="f")

    assert {r.run_id for r in store.list(case_id="A")} == {"ok1", "err1"}
    assert {r.run_id for r in store.list(only_failed=True)} == {"err1"}


def test_ultimo_run_del_caso_trae_la_traza(db):
    store = RunStore(db)
    store.save(_result("viejo"), flow="f", started_at=1000.0)
    store.save(_result("nuevo"), flow="f", started_at=2000.0)
    assert store.last_for_case("0044")["run_id"] == "nuevo"


def test_prune_deja_los_mas_recientes(db):
    store = RunStore(db)
    for i in range(10):
        store.save(_result(f"r{i}"), flow="f", started_at=1000.0 + i)
    store.prune(keep=3)
    assert {r.run_id for r in store.list()} == {"r9", "r8", "r7"}


# ── Armado de la instancia ──────────────────────────────────────────────


def test_la_instancia_arma_registro_ports_y_stores(instance):
    """
    Una instalación sin ningún plugin instalado es válida y funcional: quedan
    los builtins del núcleo, que son primitivas del motor.
    """
    assert instance.registry.errors == []
    assert set(instance.registry.tool_ids) == {"core.log", "core.set_status", "core.wait"}
    assert set(instance.registry.adapters) == {"http", "fs", "process", "clock", "browser", "window"}


def test_los_adapters_inyectados_son_los_que_llegan_al_tool(demo_instance, adapters):
    """
    La propiedad central de la arquitectura: el tool corre contra el adapter
    que la instancia ató, sin saber cuál es ni haberlo pedido a nadie.
    """
    adapters["fs"].dirs |= {"/casos/0044", "/archivo"}
    adapters["fs"].files["/casos/0044/pieza.stl"] = "x"

    demo_instance.config.update({"demoBase": "/casos"})
    demo_instance.workflows.save_mmd(
        "archivar",
        "flowchart TD\n"
        "    B(inicio)\n"
        '    S["demo.buscar"]\n'
        '    M["demo.mover | origen={carpeta}, destino=/archivo"]\n'
        "    B --> S\n"
        "    S -->|ok| M\n",
    )

    resultado = demo_instance.run("archivar", "0044")
    assert resultado.status == "ok"
    assert adapters["fs"].files == {"/archivo/0044/pieza.stl": "x"}


def test_run_persiste_la_traza_con_params_resueltos(instance):
    instance.workflows.save_mmd("loguea", FLUJO_LOG)
    resultado = instance.run("loguea", "0044", row={"id": "0044"})

    guardado = instance.run_detail(resultado.run_id)
    assert guardado["status"] == "ok"
    # Lo que hace útil una traza: el valor al que se resolvió, no la plantilla.
    assert guardado["trace"][0]["params"]["message"] == "procesando 0044"


def test_flujo_inexistente_da_error_claro(instance):
    with pytest.raises(WorkflowNotFound, match="no-existe"):
        instance.load_workflow("no-existe")


def test_diagnose_cruza_el_flujo_con_lo_instalado(instance):
    """
    El parser es puro y no conoce el registro: un flujo que llama a un tool
    inexistente parsea sin una queja. Acá se cruzan las dos cosas.
    """
    instance.workflows.save_mmd(
        "roto", 'flowchart TD\n    B(inicio)\n    N["core.lgo | message=x"]\n    B --> N\n'
    )
    _, diagnosticos = instance.diagnose("roto")
    assert any("core.lgo" in d.message for d in diagnosticos)
    # Y sugiere el nombre bueno, que es lo que convierte el error en un arreglo.
    assert any("core.log" in d.message for d in diagnosticos)


def test_missing_config_solo_de_los_plugins_del_flujo(demo_instance):
    demo_instance.workflows.save_mmd(
        "solo_log", 'flowchart TD\n    B(inicio)\n    N["core.log | message=x"]\n    B --> N\n'
    )
    assert demo_instance.missing_config_for("solo_log") == {}


# ── Variables y secretos ────────────────────────────────────────────────


def test_env_vars_salen_de_la_base(instance):
    instance.env.save("CASOS_ROOT", "/casos")
    assert instance.env_vars()["CASOS_ROOT"] == "/casos"


def test_el_entorno_le_gana_a_la_base(instance, monkeypatch):
    """
    `BOTENV_*` con prioridad para que un contenedor o un CI corran sin
    gestionar ni un archivo ni la base.
    """
    instance.env.save("TOKEN_URL", "de-la-base")
    monkeypatch.setenv("BOTENV_TOKEN_URL", "del-entorno")
    assert instance.env_vars()["TOKEN_URL"] == "del-entorno"


def test_un_secreto_se_resuelve_al_ejecutar_pero_no_sale_por_la_api(instance):
    """
    Las dos mitades de la regla, en un solo test: el valor llega al motor
    —si no, no serviría de nada— y no aparece en lo que se puede leer.
    """
    instance.env.save("API_TOKEN", "s3cr3to", secret=True)

    assert instance.env_vars()["API_TOKEN"] == "s3cr3to"

    guardado = instance.env.get("API_TOKEN")
    assert guardado.secret is True
    assert guardado.value is None
    # Ni siquiera como clave con valor nulo: la clave no existe en la respuesta.
    assert "value" not in guardado.to_dict()


def test_una_referencia_env_dentro_de_una_coleccion_se_resuelve_en_el_servidor(
    demo_instance,
):
    """
    El caso canónico de la regla de secretos: un item guarda `{env.TOKEN}`,
    nunca el token. Si esto no se resolviera, el literal viajaría hasta el otro
    extremo y el request fallaría con un 401 imposible de diagnosticar.
    """
    demo_instance.env.save("DEPOSITO_TOKEN", "abc123", secret=True)
    resource = demo_instance.resource_definition("demo", "destinos")
    demo_instance.resource_store("demo", resource).write(
        "frio", {"ruta": "/deposito", "token": "Bearer {env.DEPOSITO_TOKEN}"}
    )

    # Guardado: la referencia, tal cual.
    crudo = demo_instance.resource_store("demo", resource).read("frio")
    assert crudo["token"] == "Bearer {env.DEPOSITO_TOKEN}"

    # Entregado al plugin: ya resuelto.
    item = demo_instance.resource_items("demo", "destinos")[0]
    assert item["token"] == "Bearer abc123"


def test_resource_items_masked_tapa_los_campos_secret_sin_resolver_env(demo_instance):
    """
    Issue #7: lo que puede salir por el servidor MCP (u otra API) nunca lleva
    un valor `secret` en claro, tenga o no una referencia `{env.CLAVE}`
    adentro. Y a diferencia de `resource_items`, acá tampoco se resuelve
    ninguna referencia — no tiene sentido resolver algo que se tapa igual.
    """
    demo_instance.env.save("ALGO", "resuelto")
    resource = demo_instance.resource_definition("demo", "destinos")
    demo_instance.resource_store("demo", resource).write(
        "frio", {"ruta": "{env.ALGO}", "token": "Bearer secreto"}
    )

    items = demo_instance.resource_items_masked("demo", "destinos")

    assert len(items) == 1
    item = items[0]
    assert item["name"] == "frio"
    assert item["ruta"] == "{env.ALGO}"  # sin resolver
    assert item["token"] is None  # tapado, no "Bearer secreto"


def test_run_action_con_item_resuelve_params_desde_el_resource(demo_instance):
    """
    La otra mitad del issue #7: `run_action` puede resolver los params de una
    acción atada a un `Resource` (`Action.resource`) desde un item ya
    guardado, en vez de que quien llama reconstruya la config a mano.
    """
    resource = demo_instance.resource_definition("demo", "destinos")
    demo_instance.resource_store("demo", resource).write(
        "frio", {"ruta": "/deposito", "token": "secreto-de-verdad"}
    )

    resultado, _ = demo_instance.run_action("demo", "probar_destino", item="frio")

    assert resultado.status == "ok"
    assert resultado.outputs["ruta"] == "/deposito"
    assert resultado.outputs["token"] == "secreto-de-verdad"


def test_run_action_con_item_y_params_explicitos_estos_pisan(demo_instance):
    resource = demo_instance.resource_definition("demo", "destinos")
    demo_instance.resource_store("demo", resource).write(
        "frio", {"ruta": "/deposito", "token": "secreto-de-verdad"}
    )

    resultado, _ = demo_instance.run_action(
        "demo", "probar_destino", params={"ruta": "/otro"}, item="frio"
    )

    assert resultado.outputs["ruta"] == "/otro"
    assert resultado.outputs["token"] == "secreto-de-verdad"


def test_run_action_con_item_inexistente_es_err_y_no_revienta(demo_instance):
    resultado, _ = demo_instance.run_action("demo", "probar_destino", item="no-existe")

    assert resultado.status == "err"
    assert "no-existe" in resultado.message


def test_run_action_con_item_en_accion_sin_resource_es_err(demo_instance):
    """`ping` no declara `resource`: pedirle un item no tiene sentido y se avisa."""
    resultado, _ = demo_instance.run_action("demo", "ping", item="lo-que-sea")

    assert resultado.status == "err"
    assert "no está atada a ninguna colección" in resultado.message


def test_run_action_params_e_item_por_posicion_es_typeerror(demo_instance):
    """Issue #11: mismo criterio que los stores, acá recién agregado con `item`."""
    with pytest.raises(TypeError):
        demo_instance.run_action("demo", "ping", {"url": "https://api.test/ping"})


def test_los_usos_se_cuentan_sobre_flujos_y_colecciones(demo_instance):
    """
    Contar sólo los flujos daría 0 usos justo para el secreto que más se usa:
    el caso típico vive en un item de una colección, no en un `.mmd`.
    """
    demo_instance.env.save("EN_FLUJO", "x")
    demo_instance.env.save("EN_COLECCION", "y", secret=True)

    demo_instance.workflows.save_mmd(
        "usa", 'flowchart TD\n    B(inicio)\n    N["core.log | message={env.EN_FLUJO}"]\n    B --> N\n'
    )
    resource = demo_instance.resource_definition("demo", "destinos")
    demo_instance.resource_store("demo", resource).write(
        "d", {"ruta": "/x", "token": "{env.EN_COLECCION}"}
    )

    usos = demo_instance.env_usage()
    assert usos["EN_FLUJO"] == 1
    assert usos["EN_COLECCION"] == 1


def test_un_nombre_referenciado_y_no_cargado_aparece_igual(instance):
    """
    Es el que va a romper la ejecución. Si sólo listáramos lo cargado, sería
    justo el que no se ve.
    """
    instance.workflows.save_mmd(
        "usa", 'flowchart TD\n    B(inicio)\n    N["core.log | message={env.SIN_CARGAR}"]\n    B --> N\n'
    )
    fila = next(f for f in instance.env_listing() if f["name"] == "SIN_CARGAR")
    assert fila["undeclared"] is True
    assert fila["uses"] == 1


# ── Registro de eventos ─────────────────────────────────────────────────


def test_ejecutar_deja_el_log_de_la_fila(instance):
    instance.workflows.save_mmd("loguea", FLUJO_LOG)
    instance.run("loguea", "0044", row={"id": "0044"})

    registro = instance.case_log("0044")
    assert registro["total"] > 0
    assert any("procesando 0044" in e["message"] for e in registro["entries"])


def test_el_log_acumula_entre_ejecuciones_de_la_misma_fila(instance):
    """
    La propiedad que justifica la tabla: la pantalla muestra la historia de la
    fila, no la del último run. Reprocesar un caso agrega, no reemplaza.
    """
    instance.workflows.save_mmd("loguea", FLUJO_LOG)
    instance.run("loguea", "0044", row={"id": "0044"})
    primero = instance.case_log("0044")["total"]
    instance.run("loguea", "0044", row={"id": "0044"})
    assert instance.case_log("0044")["total"] > primero


def test_el_tope_por_fila_se_aplica_siempre(instance):
    """
    Es el que impide que un flujo con reintentos largos se coma la base, y por
    eso no depende del modo de retención.
    """
    instance.config.update({"logMaxPerCase": 2, "logRetentionMode": "manual"})
    instance.workflows.save_mmd("loguea", FLUJO_LOG)
    for _ in range(4):
        instance.run("loguea", "0044", row={"id": "0044"})
    assert instance.case_log("0044")["total"] <= 2


# ── Diagnóstico ─────────────────────────────────────────────────────────


def test_storage_info_cuenta_desde_la_base(instance):
    """
    Las cuentas salen de la base y no de un contador aparte: un contador aparte
    es una segunda fuente de verdad esperando desincronizarse.
    """
    instance.workflows.save_mmd("uno", FLUJO_LOG)
    info = instance.storage_info()
    assert info["counts"]["workflows"] == 1
    assert info["versions"]["workflows"] >= 1


# ── Actores y autorización ──────────────────────────────────────────────
#
# La propiedad que se prueba acá no es que quede registrado quién corrió: es
# que un actor sin permiso **no pueda**, y que el mundo quede intacto.

FLUJO_PELIGROSO = (
    "flowchart TD\n"
    "    B(inicio)\n"
    '    M["archivar § demo.mover | origen=/casos/0044, destino=/archivo"]\n'
    "    B --> M\n"
)


def test_un_run_queda_atribuido_a_su_actor(instance):
    instance.workflows.save_mmd("loguea", FLUJO_LOG)
    resultado = instance.run("loguea", "0044", row={"id": "0044"}, actor="local")

    assert resultado.actor == "local"
    assert instance.runs.get(resultado.run_id)["actor"] == "local"
    assert instance.runs.list(actor="local")[0].run_id == resultado.run_id


def test_sin_actor_se_usa_el_del_arranque(instance):
    instance.workflows.save_mmd("loguea", FLUJO_LOG)
    assert instance.run("loguea", "0044").actor == instance.boot.default_actor


def test_un_actor_inexistente_no_ejecuta_nada(instance):
    from backend.core.users import UserError

    instance.workflows.save_mmd("loguea", FLUJO_LOG)
    with pytest.raises(UserError, match="no existe el actor"):
        instance.run("loguea", "0044", actor="fantasma")
    # Y no quedó ningún run a medias.
    assert instance.runs.list() == []


def test_un_agente_no_puede_correr_un_flujo_con_tools_peligrosos(demo_instance, adapters):
    """
    Falla **antes** de ejecutar: el adapter no registra una sola llamada. Es la
    diferencia entre una política y un aviso.
    """
    from backend.core.users import AGENT, UserError

    adapters["fs"].dirs |= {"/casos/0044", "/archivo"}
    demo_instance.users.create("agente", kind=AGENT)
    demo_instance.workflows.save_mmd("peligroso", FLUJO_PELIGROSO)

    with pytest.raises(UserError) as exc:
        demo_instance.run("peligroso", "0044", actor="agente")

    assert "peligroso" in str(exc.value)
    assert "M:" in str(exc.value)      # nombra el nodo
    assert adapters["fs"].calls == []  # no tocó nada


def test_un_humano_corre_el_mismo_flujo(demo_instance, adapters):
    adapters["fs"].dirs |= {"/casos/0044", "/archivo"}
    demo_instance.workflows.save_mmd("peligroso", FLUJO_PELIGROSO)

    resultado = demo_instance.run("peligroso", "0044", actor="local")
    assert resultado.status == "ok"
    assert any(c[0] == "move" for c in adapters["fs"].calls)


def test_habilitar_al_agente_lo_desbloquea(demo_instance, adapters):
    """
    Cambiar lo que puede un actor es un UPDATE, no un deploy. Es lo que hace
    que la política sea operable sin tocar código.
    """
    from backend.core.users import AGENT

    adapters["fs"].dirs |= {"/casos/0044", "/archivo"}
    demo_instance.users.create("agente", kind=AGENT)
    demo_instance.users.update_policy("agente", can_run_dangerous=True, clear_ports=True)
    demo_instance.workflows.save_mmd("peligroso", FLUJO_PELIGROSO)

    assert demo_instance.run("peligroso", "0044", actor="agente").status == "ok"


def test_la_autorizacion_tambien_aplica_en_dry_run(demo_instance):
    """
    Un recorrido en seco que aprueba lo que el run real va a rechazar no sirve
    para nada: el punto del dry-run es enterarse antes.
    """
    from backend.core.users import AGENT, UserError

    demo_instance.users.create("agente", kind=AGENT)
    demo_instance.workflows.save_mmd("peligroso", FLUJO_PELIGROSO)

    with pytest.raises(UserError):
        demo_instance.run("peligroso", "0044", actor="agente", dry_run=True)


FLUJO_TOOL_INEXISTENTE = (
    'flowchart TD\n'
    '    B(inicio)\n'
    '    N["archivos.copiar"]\n'
    '    E["core.log | message=manejado"]\n'
    '    B --> N\n'
    '    N -->|err| E\n'
)


def test_un_tool_inexistente_no_ejecuta_nada(instance):
    """
    Issue #9: antes, el nodo fallaba, el executor seguía por `|err|` (pensada
    para fallas de runtime, no de configuración) y el run completo terminaba
    "ok" con el trabajo real sin hacer, sin que nada lo señalara. Ahora falla
    antes de tocar nada, mismo criterio que un actor sin permiso.
    """
    from backend.core.users import UserError

    instance.workflows.save_mmd("roto", FLUJO_TOOL_INEXISTENTE)

    with pytest.raises(UserError, match="archivos.copiar"):
        instance.run("roto", "0044")

    assert instance.runs.list() == []


def test_un_tool_inexistente_tambien_bloquea_el_dry_run(instance):
    """Mismo criterio que la autorización: el punto del dry-run es enterarse antes."""
    from backend.core.users import UserError

    instance.workflows.save_mmd("roto", FLUJO_TOOL_INEXISTENTE)

    with pytest.raises(UserError, match="archivos.copiar"):
        instance.run("roto", "0044", dry_run=True)


def test_allow_broken_corre_igual_por_el_camino_de_antes(instance):
    """El escape hatch: quien sepa lo que hace puede correrlo igual."""
    instance.workflows.save_mmd("roto", FLUJO_TOOL_INEXISTENTE)

    resultado = instance.run("roto", "0044", allow_broken=True)

    assert resultado.status == "ok"
    assert [t.node_id for t in resultado.trace] == ["N", "E"]


def test_authorize_reporta_todos_los_nodos_vedados_de_una(demo_instance):
    """
    El chequeo previo existe para dar el panorama completo en vez de morir en
    el primer nodo. Corregir de a uno es el peor bucle posible.
    """
    from backend.core.flow.parser import parse_flow
    from backend.core.users import AGENT

    demo_instance.users.create("agente", kind=AGENT)
    graph = parse_flow(
        "flowchart TD\n"
        "    B(inicio)\n"
        '    M1["a § demo.mover | origen=/a, destino=/b"]\n'
        '    M2["b § demo.mover | origen=/c, destino=/d"]\n'
        "    B --> M1\n"
        "    M1 --> M2\n"
    )
    vedados = demo_instance.authorize(graph, demo_instance.policy_for("agente"))
    assert len(vedados) == 2
    assert vedados[0].startswith("M1:") and vedados[1].startswith("M2:")


def test_requires_exclusive_run_true_si_algun_tool_lo_declara(demo_instance):
    """"mundo" usa demo.correr, declarado concurrency="exclusive_run"."""
    assert demo_instance.requires_exclusive_run("mundo") is True


def test_requires_exclusive_run_false_si_ningun_tool_lo_declara(demo_instance):
    assert demo_instance.requires_exclusive_run("lineal") is False


def test_requires_exclusive_run_false_para_un_flujo_solo_de_nativos(demo_instance):
    demo_instance.workflows.save_mmd(
        "solo_nativos",
        "flowchart TD\n"
        "    B(inicio)\n"
        '    N1["log § core.log | message=hola"]\n'
        "    B --> N1\n",
    )
    assert demo_instance.requires_exclusive_run("solo_nativos") is False


def test_un_actor_deshabilitado_no_ejecuta(instance):
    from backend.core.users import AGENT, UserError

    instance.users.create("temporal", kind=AGENT)
    instance.users.disable("temporal")
    instance.workflows.save_mmd("loguea", FLUJO_LOG)

    with pytest.raises(UserError, match="deshabilitado"):
        instance.run("loguea", "0044", actor="temporal")


# ── Bootstrap ───────────────────────────────────────────────────────────


def test_la_instancia_lee_su_configuracion_de_arranque(tmp_path, adapters):
    """
    `fs_root` y la allowlist son de la instalación, no settings de un plugin:
    definen qué puede tocar esta máquina y un plugin no debería ampliarlos.
    """
    from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter
    from backend.core import boot as bootstrap
    from backend.core.instance import Instance

    config = bootstrap.load(tmp_path, entorno={"BOOTSTRAP_default_actor": "operador"})
    inst = Instance(tmp_path, storage=SqliteStorageAdapter(IN_MEMORY), adapters=adapters,
                    boot=config)
    try:
        assert inst.boot.default_actor == "operador"
    finally:
        inst.close()


def test_la_carpeta_de_plugins_se_carga_sola(tmp_path, adapters):
    """
    Comodidad de autoría, opt-in. En producción los plugins entran por entry
    point; esto existe para no nombrar cada uno en cada invocación.
    """
    from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter
    from backend.core import boot as bootstrap
    from backend.core.instance import Instance

    carpeta = tmp_path / "plugins"
    carpeta.mkdir()
    (carpeta / "suelto.py").write_text(
        "from backend.core.contract import (\n"
        "    FunctionTool, Plugin, PluginManifest, ToolManifest, ToolResult,\n"
        ")\n"
        'MANIFEST = PluginManifest(name="suelto", label="S")\n'
        "PLUGIN = Plugin(manifest=MANIFEST, tools=[FunctionTool(\n"
        '    manifest=ToolManifest(id="suelto.hacer", label="h", category="X"),\n'
        "    fn=lambda ctx: ToolResult.ok())])\n",
        encoding="utf-8",
    )
    config = bootstrap.load(
        tmp_path, entorno={"BOOTSTRAP_plugins_dir": str(carpeta)}
    )
    inst = Instance(tmp_path, storage=SqliteStorageAdapter(IN_MEMORY), adapters=adapters,
                    boot=config)
    try:
        assert "suelto.hacer" in inst.registry.tool_ids
        # Y el catálogo dice de dónde salió: una carpeta que ejecuta lo que le
        # caiga adentro no puede ser silenciosa.
        cargado = next(p for p in inst.registry.plugins if p.name == "suelto")
        assert "suelto" in cargado.source
    finally:
        inst.close()


def test_close_cierra_los_adapters_que_lo_declaran(instance, adapters):
    """
    `http`/`fs`/`process`/`clock` no tienen nada que cerrar; `browser` sí
    —un proceso de navegador real—. `Instance.close()` no sabe cuál es cuál:
    cierra el que declare `close()` y listo.
    """
    instance.close()

    assert adapters["browser"].cerrado is True
