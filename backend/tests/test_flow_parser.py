"""
Tests del parser de flujos.

Dos objetivos: que el parser aguante las formas reales del corpus, y que
lo que antes fallaba en silencio ahora produzca un diagnóstico.
"""

from __future__ import annotations

import pathlib
import sys

# La raíz del repo, para que `backend` sea importable como paquete.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.core.flow import (  # noqa: E402
    ActionNode,
    DecisionNode,
    Severity,
    StartNode,
    UnknownNode,
    parse_flow,
    parse_meta,
)

# El corpus de flujos de prueba. Ver tests/flows/README.md.
FLOWS = pathlib.Path(__file__).resolve().parent / "flows"


def _errors(graph) -> list[str]:
    return [d.message for d in graph.errors]


# ── Metadata ────────────────────────────────────────────────────────────


def test_parse_meta_extrae_la_cabecera():
    meta, content = parse_meta(
        "%% folder: FORM\n%% state: disabled\n%% description: hola\nflowchart TD\n    A(inicio)"
    )
    assert (meta.folder, meta.state, meta.description) == ("FORM", "disabled", "hola")
    assert meta.enabled is False
    assert content.startswith("flowchart TD")


def test_meta_por_defecto_es_enabled():
    meta, _ = parse_meta("flowchart TD\n    A(inicio)")
    assert meta.enabled is True
    assert meta.folder == ""


# ── Tipos de nodo ───────────────────────────────────────────────────────


def test_nodo_inicio_y_accion_con_display_y_params():
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    N1["Buscar archivos § demo.buscar | etiqueta=entrada"]\n'
        '    B --> N1\n'
    )
    assert graph.runnable, _errors(graph)
    assert graph.start_node == "B"
    assert isinstance(graph.nodes["B"], StartNode)

    node = graph.nodes["N1"]
    assert isinstance(node, ActionNode)
    assert node.fn == "demo.buscar"
    assert node.params == {"etiqueta": "entrada"}
    assert node.display == "Buscar archivos"


def test_valor_de_param_no_se_trimea():
    """windowsRenameSuffix= - COMPLETADO depende de que el espacio se preserve."""
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    R["windows.rename_folder | windowsRenamePath={movedFolder}, windowsRenameSuffix= - COMPLETADO"]\n'
        '    B --> R\n'
    )
    assert graph.nodes["R"].params["windowsRenameSuffix"] == " - COMPLETADO"
    assert graph.nodes["R"].params["windowsRenamePath"] == "{movedFolder}"


def test_nodo_decision_con_display():
    graph = parse_flow("flowchart TD\n    B(inicio)\n    C{Tipo § referencia}\n    B --> C\n")
    node = graph.nodes["C"]
    assert isinstance(node, DecisionNode)
    assert node.variable == "referencia"
    assert node.display == "Tipo"


def test_accion_sin_params():
    graph = parse_flow('flowchart TD\n    B(inicio)\n    O["demo.crear_orden"]\n    B --> O\n')
    assert graph.nodes["O"].fn == "demo.crear_orden"
    assert graph.nodes["O"].params == {}


# ── Aristas ─────────────────────────────────────────────────────────────


def test_condiciones_de_arista():
    graph = parse_flow(
        "flowchart TD\n"
        "    B(inicio)\n"
        '    A["core.log"]\n'
        '    X["core.log"]\n'
        '    Y["core.log"]\n'
        "    B --> A\n"
        "    A -->|ok| X\n"
        "    A -->|err| Y\n"
        "    A -->|CNC2,CNC3| Y\n"
    )
    conds = {(e.from_, e.to): e.condition for e in graph.edges}
    assert conds[("B", "A")] is None
    assert conds[("A", "X")] == "ok"
    assert conds[("A", "Y")] in {"err", "CNC2,CNC3"}
    assert {e.condition for e in graph.out_edges("A")} == {"ok", "err", "CNC2,CNC3"}


def test_cadena_de_aristas():
    """
    A-->B-->C crea también la arista B→C.

    Forma que usaban varios flujos de testing con hasta 4 eslabones (borrados en
    la limpieza). Se mantiene soportada porque es Mermaid válido.
    """
    graph = parse_flow(
        "flowchart TD\n"
        "    B(inicio)\n"
        '    X["core.log"]\n'
        '    Y["core.log"]\n'
        '    Z["core.log"]\n'
        "    B --> X --> Y --> Z\n"
    )
    assert graph.runnable, _errors(graph)
    pares = {(e.from_, e.to) for e in graph.edges}
    assert pares == {("B", "X"), ("X", "Y"), ("Y", "Z")}


def test_cadena_con_definicion_inline_no_se_trunca_en_silencio():
    """
    `B -->N["fn"] -->C` no está soportado — el motor anterior truncaba la cadena y
    dejaba N sin definir sin avisar. Ningún flujo real usa esta forma, pero si
    alguien la escribe ahora recibe un error en vez de un flujo mutilado.
    """
    graph = parse_flow(
        'flowchart TD\n    B(inicio)\n    B -->limpiar["core.log"] -->refe{referencia}\n'
    )
    assert not graph.runnable
    assert isinstance(graph.nodes["limpiar"], UnknownNode)
    assert "refe" not in graph.nodes  # el segundo eslabón se pierde: por eso es error


def test_definicion_inline_en_linea_de_arista():
    graph = parse_flow('flowchart TD\n    B(inicio)\n    B --> N1["core.wait | seconds=60"]\n')
    assert graph.nodes["N1"].fn == "core.wait"
    assert graph.nodes["N1"].params == {"seconds": "60"}


# ── Lo que antes fallaba en silencio ────────────────────────────────────


def test_nodo_huerfano_es_error_no_una_accion_implicita():
    """
    el motor anterior convertía un nodo no definido en una acción con fn = su ID, y el
    dispatcher la logueaba sin marcar error: el flujo seguía por la rama de éxito.
    """
    graph = parse_flow("flowchart TD\n    B(inicio)\n    B --> SF\n")
    assert not graph.runnable
    assert isinstance(graph.nodes["SF"], UnknownNode)
    assert any('"SF"' in m and "nunca se define" in m for m in _errors(graph))


def test_sin_nodo_de_inicio_es_error():
    graph = parse_flow('flowchart TD\n    A["core.log"]\n    A --> A\n')
    assert not graph.runnable
    assert any("nodo de inicio" in m for m in _errors(graph))


def test_dos_nodos_de_inicio_es_error():
    graph = parse_flow(
        'flowchart TD\n    A(inicio)\n    B(inicio)\n    C["core.log"]\n    A --> C\n    B --> C\n'
    )
    assert not graph.runnable
    assert any("nodos de inicio" in m for m in _errors(graph))


def test_nodo_inalcanzable_es_warning():
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    A["core.log"]\n'
        '    Z["core.log"]\n'
        '    B --> A\n'
    )
    assert graph.runnable  # un warning no impide ejecutar
    assert any('"Z"' in d.message and "inalcanzable" in d.message for d in graph.warnings)


def test_definicion_duplicada_es_warning_y_gana_la_primera():
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    N["core.log | message=primera"]\n'
        '    N["core.log | message=segunda"]\n'
        '    B --> N\n'
    )
    assert graph.nodes["N"].params["message"] == "primera"
    assert any("más de una vez" in d.message for d in graph.warnings)


def test_subgraph_avisa_que_no_esta_soportado():
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    subgraph LOOP["bucle"]\n'
        '    A["core.log"]\n'
        '    end\n'
        '    B --> A\n'
    )
    assert any("subgraph" in d.message for d in graph.warnings)


def test_linea_no_reconocida_avisa():
    graph = parse_flow("flowchart TD\n    B(inicio)\n    esto no es mermaid\n")
    assert any("no reconocida" in d.message for d in graph.warnings)


def test_decision_sin_variable_es_error():
    graph = parse_flow("flowchart TD\n    B(inicio)\n    C{ § }\n    B --> C\n")
    assert not graph.runnable
    assert any("sin variable" in m for m in _errors(graph))


# ── Issue #10: valores citados ───────────────────────────────────────────


def test_valor_citado_preserva_coma_y_pipe():
    """El ejemplo real del issue: un mensaje de log con una coma adentro."""
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    N3["Registrar UY § core.log | message="Caso {id} es de UY, no se copia""]\n'
        '    B --> N3\n'
    )
    assert graph.runnable
    assert graph.nodes["N3"].params["message"] == "Caso {id} es de UY, no se copia"


def test_valor_citado_con_pipe_no_corta_los_params():
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    N["core.log | message="a|b", level=warning"]\n'
        '    B --> N\n'
    )
    assert graph.runnable
    assert graph.nodes["N"].params == {"message": "a|b", "level": "warning"}


def test_comilla_sin_cerrar_es_error():
    graph = parse_flow(
        'flowchart TD\n'
        '    B(inicio)\n'
        '    N["core.log | message="a, b, c"]\n'
        '    B --> N\n'
    )
    assert not graph.runnable
    assert any("comilla sin cerrar" in m for m in _errors(graph))


def test_parser_nunca_levanta_excepcion():
    for basura in ("", "   ", "flowchart TD", "-->|||", "A{{{", 'X["a § b | = "]'):
        graph = parse_flow(basura)
        assert isinstance(graph.diagnostics, list)


# ── Fidelidad sobre los flujos reales ───────────────────────────────────


def test_todos_los_flujos_habilitados_parsean_sin_errores():
    archivos = sorted(FLOWS.glob("*.mmd"))
    assert archivos, "el corpus está vacío: este test no estaría probando nada"

    fallos = []
    for path in archivos:
        graph = parse_flow(path.read_text(encoding="utf-8"))
        if graph.meta.enabled and not graph.runnable:
            fallos.append((path.name, _errors(graph)))
    assert not fallos, f"flujos habilitados con errores: {fallos}"


def test_todos_los_flujos_del_corpus_tienen_inicio_y_acciones():
    for path in sorted(FLOWS.glob("*.mmd")):
        graph = parse_flow(path.read_text(encoding="utf-8"))
        if not graph.meta.enabled:
            continue
        assert graph.start_node, f"{path.name} sin nodo de inicio"
        assert graph.action_nodes(), f"{path.name} sin nodos de acción"


def test_grafo_es_serializable():
    graph = parse_flow(
        'flowchart TD\n    B(inicio)\n    N["Buscar § windows.search_files"]\n    B -->|ok| N\n'
    )
    data = graph.to_dict()
    assert data["start_node"] == "B"
    assert data["nodes"]["N"]["fn"] == "windows.search_files"
    assert data["nodes"]["N"]["display"] == "Buscar"
    assert data["edges"][0] == {"from": "B", "to": "N", "condition": "ok"}
    assert data["runnable"] is True

    import json

    json.dumps(data)  # debe ser serializable para /api y el MCP


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
