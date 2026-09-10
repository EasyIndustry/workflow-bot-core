"""
Tests del executor de flujos.

Cubren el recorrido del grafo, la traza por nodo, y que un fallo corte el flujo
en lugar de seguir por la rama de éxito.
"""

from __future__ import annotations

from backend.core import ToolRegistry
from backend.core.builtins import build_builtin_plugin
from backend.core.contract import (
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    ToolManifest,
    ToolResult,
)
from backend.core.flow import RunContext, execute_flow, parse_flow
from backend.tests.fakes import fake_adapters

# Ports que se le dan al plugin de prueba. Los cuatro, para que un test pueda
# ejercer cualquiera sin declarar un plugin distinto cada vez.
PORTS_DE_PRUEBA = ("http", "fs", "process", "clock")


def _registry(*extra, adapters=None) -> ToolRegistry:
    reg = ToolRegistry(adapters=adapters or fake_adapters())
    reg._add_plugin("core", "builtin", build_builtin_plugin())
    if extra:
        reg._add_plugin(
            "test",
            "test",
            Plugin(
                manifest=PluginManifest(name="test", label="Test", ports=PORTS_DE_PRUEBA),
                tools=list(extra),
            ),
        )
    return reg


def _tool(tool_id: str, fn, params=(), outputs=()) -> FunctionTool:
    return FunctionTool(
        manifest=ToolManifest(
            id=tool_id, label=tool_id, category="TEST", params=tuple(params), outputs=tuple(outputs)
        ),
        fn=fn,
    )


def _run(flow: str, **kwargs):
    kwargs.setdefault("registry", _registry())
    kwargs.setdefault("case_id", "0044")
    return execute_flow(flow, **kwargs)


# ── Interpolación de variables ──────────────────────────────────────────


def test_contexto_resuelve_en_orden_de_precedencia():
    ctx = RunContext(
        row={"x": "row", "solo_row": "R"},
        vars={"x": "var"},
        env={"E": "env"},
        config={"c": "cfg"},
    )
    assert ctx.resolve("{x}") == "var"  # vars gana sobre row
    assert ctx.resolve("{solo_row}") == "R"
    assert ctx.resolve("{env.E}") == "env"
    assert ctx.resolve("{c}") == "cfg"


def test_modificador_parent_con_separadores_de_windows():
    ctx = RunContext(vars={"p": r"D:\casos\0044\archivos"})
    assert ctx.resolve("{p.parent}") == r"D:\casos\0044"
    assert RunContext(vars={"p": "/tmp/a/b"}).resolve("{p.parent}") == "/tmp/a"


def test_traversal_de_objeto():
    ctx = RunContext(vars={"checkLogResult": {"log_file": "x.log", "found": True}})
    assert ctx.resolve("{checkLogResult.log_file}") == "x.log"


def test_variable_sin_resolver_queda_literal_y_se_reporta():
    ctx = RunContext()
    assert ctx.resolve("{noExiste}") == "{noExiste}"
    assert ctx.unresolved("a {noExiste} b {tampoco}") == ["noExiste", "tampoco"]


def test_decision_prioriza_el_row():
    """el motor anterior miraba context antes que state; se preserva."""
    ctx = RunContext(row={"referencia": "CNC4"}, vars={"referencia": "CNC2"})
    assert ctx.decision_value("referencia") == "CNC4"


# ── Recorrido del grafo ─────────────────────────────────────────────────


def test_flujo_lineal_deja_traza_por_nodo():
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    L1["core.log | message=uno"]\n'
        '    L2["core.log | message=dos"]\n'
        '    B --> L1\n'
        '    L1 --> L2\n'
    )
    assert result.status == "ok"
    assert [t.node_id for t in result.trace] == ["L1", "L2"]
    # La traza muestra lo que el tool RECIBIÓ: los params del nodo más los
    # defaults del manifest, ya tipados. No lo que el .mmd escribió.
    assert result.trace[0].params == {"message": "uno", "level": "info"}
    assert result.run_id.startswith("run-")


def test_decision_toma_la_rama_del_valor():
    flow = (
        "flowchart TD\n"
        "    B(inicio)\n"
        "    D{referencia}\n"
        '    A["core.log | message=cnc4"]\n'
        '    C["core.log | message=cnc23"]\n'
        "    B --> D\n"
        "    D -->|CNC4| A\n"
        "    D -->|CNC2,CNC3| C\n"
    )
    assert [t.node_id for t in _run(flow, row={"referencia": "CNC4"}).trace] == ["D", "A"]
    assert [t.node_id for t in _run(flow, row={"referencia": "CNC3"}).trace] == ["D", "C"]


def test_decision_sin_rama_falla_con_el_nodo():
    result = _run(
        "flowchart TD\n"
        "    B(inicio)\n"
        "    D{referencia}\n"
        '    A["core.log | message=x"]\n'
        "    B --> D\n"
        "    D -->|CNC4| A\n",
        row={"referencia": "CNC9"},
    )
    assert result.failed
    assert result.failed_node == "D"
    assert "CNC9" in result.message


def test_outputs_de_un_nodo_alimentan_al_siguiente():
    tool = _tool(
        "test.produce",
        lambda ctx: ToolResult.ok(ruta="/tmp/salida"),
        outputs=[Output("ruta", ParamType.PATH)],
    )
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    P["test.produce"]\n'
        '    L["core.log | message=quedo en {ruta}"]\n'
        '    B --> P\n'
        '    P -->|ok| L\n',
        registry=_registry(tool),
    )
    assert result.status == "ok"
    assert result.trace[0].outputs == {"ruta": "/tmp/salida"}
    assert result.trace[1].params == {"message": "quedo en /tmp/salida", "level": "info"}


# ── Fallos: lo que antes seguía por la rama de éxito ────────────────────


def test_error_sin_arista_err_corta_el_flujo():
    tool = _tool("test.falla", lambda ctx: ToolResult.err("se rompió"))
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    F["test.falla"]\n'
        '    L["core.log | message=NO deberia llegar"]\n'
        '    B --> F\n'
        '    F --> L\n',
        registry=_registry(tool),
    )
    assert result.failed
    assert result.failed_node == "F"
    assert [t.node_id for t in result.trace] == ["F"]  # L nunca se ejecutó


def test_error_con_arista_err_ramifica_y_sigue():
    tool = _tool("test.falla", lambda ctx: ToolResult.err("se rompió"))
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    F["test.falla"]\n'
        '    E["core.log | message=manejado"]\n'
        '    B --> F\n'
        '    F -->|err| E\n',
        registry=_registry(tool),
    )
    assert result.status == "ok"
    assert [t.node_id for t in result.trace] == ["F", "E"]


def test_excepcion_en_un_tool_corta_con_traceback():
    def boom(ctx):
        raise RuntimeError("explotó")

    result = _run(
        'flowchart TD\n    B(inicio)\n    X["test.boom"]\n    B --> X\n',
        registry=_registry(_tool("test.boom", boom)),
    )
    assert result.failed
    assert result.failed_node == "X"
    assert "RuntimeError" in result.trace[0].traceback


def test_flujo_con_errores_de_parseo_no_se_ejecuta():
    """Antes arrancaba igual y fallaba de formas raras más adelante."""
    result = _run("flowchart TD\n    B(inicio)\n    B --> NODO_INEXISTENTE\n")
    assert result.failed
    assert result.trace == []
    assert "nunca se define" in result.message


def test_tool_inexistente_corta_el_flujo():
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    X["plugin.no_instalado"]\n'
        '    L["core.log | message=NO deberia llegar"]\n'
        '    B --> X\n'
        '    X --> L\n'
    )
    assert result.failed
    assert [t.node_id for t in result.trace] == ["X"]


def test_param_obligatorio_faltante_corta_el_flujo():
    """
    El nodo falla **antes** de ejecutar el tool, con el nombre del param que
    falta. Es la diferencia entre "arreglalo" y "andá a buscar qué pasó".
    """
    tool = _tool(
        "test.necesita",
        lambda ctx: ToolResult.ok(),
        params=[Param("origen", required=True)],
    )
    result = _run(
        'flowchart TD\n    B(inicio)\n    M["test.necesita"]\n    B --> M\n',
        registry=_registry(tool),
    )
    assert result.failed
    assert "origen" in result.trace[0].message


# ── Loops y reintentos ──────────────────────────────────────────────────


def test_retry_gate_reintenta_hasta_el_limite():
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    G["flow.retry_gate | retryGateKey=proc, retryGateMax=3"]\n'
        '    W["core.log | message=reintento"]\n'
        '    E["core.log | message=agotado"]\n'
        '    B --> G\n'
        '    G -->|loop| W\n'
        '    G -->|err| E\n'
        '    W --> G\n'
    )
    assert result.status == "ok"
    gates = [t for t in result.trace if t.fn == "flow.retry_gate"]
    assert len(gates) == 3
    assert gates[-1].status == "err"
    assert result.trace[-1].node_id == "E"


def test_tope_de_visitas_aborta():
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    L["core.log | message=loop"]\n'
        '    B --> L\n'
        '    L --> L\n',
        max_visits=5,
    )
    assert result.failed
    assert "5 veces" in result.message or "se visitó" in result.message


# ── Flujos anidados ─────────────────────────────────────────────────────


def test_flow_ejecutar_anida_y_comparte_contexto():
    hijo = 'flowchart TD\n    B(inicio)\n    H["core.log | message=hijo {id_externo}"]\n    B --> H\n'
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    S["flow.ejecutar | flowName=hijo"]\n'
        '    F["core.log | message=fin"]\n'
        '    B --> S\n'
        '    S -->|ok| F\n',
        row={"id_externo": "0044"},
        load_flow=lambda nombre: hijo,
    )
    assert result.status == "ok"
    ids = [t.node_id for t in result.trace]
    assert ids == ["S", "H", "F"]
    assert result.trace[1].depth == 2
    assert result.trace[1].flow == "hijo"
    assert result.trace[1].params == {"message": "hijo 0044", "level": "info"}


def test_fallo_en_flujo_anidado_corta_el_padre():
    hijo = 'flowchart TD\n    B(inicio)\n    X["test.falla"]\n    B --> X\n'
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    S["flow.ejecutar | flowName=hijo"]\n'
        '    F["core.log | message=NO deberia llegar"]\n'
        '    B --> S\n'
        '    S --> F\n',
        registry=_registry(_tool("test.falla", lambda ctx: ToolResult.err("roto"))),
        load_flow=lambda nombre: hijo,
    )
    assert result.failed
    assert result.failed_node == "X"
    assert "F" not in [t.node_id for t in result.trace]


def test_flow_ejecutar_sin_load_flow_falla_claro():
    result = _run(
        'flowchart TD\n    B(inicio)\n    S["flow.ejecutar | flowName=x"]\n    B --> S\n'
    )
    assert result.failed
    assert "load_flow" in result.trace[0].message


def test_anidamiento_infinito_se_corta():
    recursivo = 'flowchart TD\n    B(inicio)\n    S["flow.ejecutar | flowName=yo"]\n    B --> S\n'
    result = _run(recursivo, load_flow=lambda nombre: recursivo)
    assert result.failed
    assert "anidamiento" in result.message


# ── Dry run ─────────────────────────────────────────────────────────────


def test_dry_run_no_ejecuta_el_tool_pero_traza_sus_params():
    """
    Un dry-run recorre el grafo y resuelve las variables **sin** llamar a
    ningún tool. Es lo que permite revisar un flujo contra datos reales sin
    tocar el mundo.
    """
    ejecutado = []
    tool = _tool(
        "test.efecto",
        lambda ctx: ejecutado.append(ctx.params["destino"]) or ToolResult.ok(),
        params=[Param("destino", required=True)],
    )
    result = _run(
        "flowchart TD\n"
        "    B(inicio)\n"
        '    M["test.efecto | destino={carpeta}"]\n'
        "    B --> M\n",
        registry=_registry(tool),
        row={"carpeta": "/casos/0044"},
        dry_run=True,
    )
    assert result.status == "ok"
    assert result.dry_run
    assert ejecutado == []  # el tool no corrió
    # Pero la traza guarda el valor al que se habría resuelto.
    assert result.trace[0].params["destino"] == "/casos/0044"


def test_dry_run_reporta_variables_sin_resolver():
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    L["core.log | message=valor {noExiste}"]\n'
        '    B --> L\n',
        dry_run=True,
    )
    assert "noExiste" in result.trace[0].message


def test_dry_run_detecta_tool_inexistente():
    result = _run(
        'flowchart TD\n    B(inicio)\n    X["plugin.no_instalado"]\n    B --> X\n',
        dry_run=True,
    )
    assert result.failed
    assert "no existe" in result.message


# ── Builtins ────────────────────────────────────────────────────────────


def test_set_status_err_corta_el_flujo():
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    S["core.set_status | status=err"]\n'
        '    L["core.log | message=NO deberia llegar"]\n'
        '    B --> S\n'
        '    S --> L\n'
    )
    assert result.failed
    assert result.failed_node == "S"


def test_wait_con_cero_no_espera():
    result = _run(
        'flowchart TD\n    B(inicio)\n    W["core.wait | seconds=0"]\n    B --> W\n'
    )
    assert result.status == "ok"


def test_cancelacion_detiene_entre_nodos():
    llamadas = {"n": 0}

    def cancelado():
        llamadas["n"] += 1
        return llamadas["n"] > 2

    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    L["core.log | message=x"]\n'
        '    B --> L\n'
        '    L --> L\n',
        is_cancelled=cancelado,
    )
    assert not result.failed
    assert any("Detenido por el usuario" in e.message for e in result.logs)


# ── Serialización ───────────────────────────────────────────────────────


def test_resultado_es_serializable():
    import json

    result = _run(
        'flowchart TD\n    B(inicio)\n    L["core.log | message=x"]\n    B --> L\n'
    )
    data = result.to_dict()
    json.dumps(data)
    assert data["status"] == "ok"
    assert data["trace"][0]["fn"] == "core.log"


# ── El footgun de la coma ───────────────────────────────────────────────


def test_valor_con_coma_sin_comillas_es_error_no_warning():
    """
    Issue #10: la coma sigue separando params fuera de comillas —ningún flujo
    existente cambia—, pero perder texto a mitad de palabra ya no deja el
    flujo "runnable": es peor que no ejecutar nada.
    """
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    L["core.log | message=Hola, mundo"]\n'
        '    B --> L\n'
    )
    assert not graph.runnable
    assert graph.nodes["L"].params["message"] == "Hola"
    assert any("mundo" in d.message for d in graph.errors)


def test_valor_con_coma_entre_comillas_se_preserva_entero():
    """La forma de escribir la coma a propósito: no se pierde nada."""
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    L["core.log | message="Hola, mundo""]\n'
        '    B --> L\n'
    )
    assert graph.runnable
    assert graph.nodes["L"].params["message"] == "Hola, mundo"


if __name__ == "__main__":
    fallos = 0
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for nombre, fn in tests.items():
        try:
            fn()
            print(f"  ok   {nombre}")
        except Exception as exc:
            fallos += 1
            print(f"  FALLA {nombre}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - fallos}/{len(tests)} tests pasaron")
    sys.exit(1 if fallos else 0)


def test_la_traza_guarda_los_params_ya_tipados():
    """
    La traza existe para responder "por qué falló". Si mostrara los valores
    previos al tipado, un `true` de texto en un param BOOL haría pensar que el
    tool recibe strings — y lleva a escribir normalización defensiva contra un
    problema que no existe.

    Muestra lo que el tool recibió: tipos convertidos y defaults aplicados.
    """
    visto = {}
    tool = _tool(
        "test.tipos",
        lambda ctx: visto.update(ctx.params) or ToolResult.ok(),
        params=[
            Param("activo", ParamType.BOOL, default=False),
            Param("veces", ParamType.INT, default=1),
            Param("nombre", default="anonimo"),
        ],
    )
    result = _run(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    T["test.tipos | activo=true, veces=7"]\n'
        '    B --> T\n',
        registry=_registry(tool),
    )

    assert result.status == "ok"
    esperado = {"activo": True, "veces": 7, "nombre": "anonimo"}
    assert visto == esperado           # lo que el tool vio
    assert result.trace[0].params == esperado   # y lo que la traza dice


def test_un_param_que_no_convierte_deja_el_valor_crudo_en_la_traza():
    """
    Cuando el tipado falla, lo que interesa ver es el valor que no convirtió.
    Mostrar el resultado de una conversión que no ocurrió sería peor que inútil.
    """
    tool = _tool(
        "test.entero",
        lambda ctx: ToolResult.ok(),
        params=[Param("n", ParamType.INT)],
    )
    result = _run(
        'flowchart TD\n    B(inicio)\n    T["test.entero | n=siete"]\n    B --> T\n',
        registry=_registry(tool),
    )
    assert result.failed
    assert result.trace[0].params == {"n": "siete"}
    assert "no es un entero" in result.trace[0].message


def test_el_dry_run_tambien_tipa_los_params():
    """
    Un dry run que pasa sobre una entrada que el run real rechaza no sirve de
    nada: el punto del dry run es enterarse antes.
    """
    tool = _tool(
        "test.espera",
        lambda ctx: ToolResult.ok(),
        params=[Param("seconds", ParamType.FLOAT)],
    )
    result = _run(
        'flowchart TD\n    B(inicio)\n    T["test.espera | seconds=abc"]\n    B --> T\n',
        registry=_registry(tool),
        dry_run=True,
    )
    assert result.failed
    assert "no es un número" in result.trace[0].message
