"""
Tests del serializador: grafo → Mermaid.

El test que importa es el round-trip contra el corpus real. Si
`parse_flow(to_mermaid(g))` no devuelve el mismo grafo, el editor de tarjetas
corrompe flujos al guardar — que es exactamente lo que hacía el ayudante de la
app vieja.
"""

from __future__ import annotations

import pathlib
import sys

# La raíz del repo, para que `backend` sea importable como paquete.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.core.flow.parser import (  # noqa: E402
    ActionNode,
    DecisionNode,
    FlowEdge,
    FlowGraph,
    FlowMeta,
    StartNode,
    parse_flow,
)
from backend.core.flow.serializer import (  # noqa: E402
    SerializeError,
    from_dict,
    to_mermaid,
    verificar,
)

# El corpus vive en tests/flows/ y está escrito contra la arquitectura
# vigente: sólo core.*, flow.* y el plugin de prueba. Ver su README.
FLOWS = pathlib.Path(__file__).resolve().parent / "flows"


def _huella(grafo: FlowGraph):
    """Todo lo que un round-trip tiene que preservar."""
    return (
        {
            nid: (
                nodo.type,
                getattr(nodo, "fn", None),
                getattr(nodo, "params", None),
                getattr(nodo, "display", ""),
                getattr(nodo, "variable", None),
                getattr(nodo, "label", None),
            )
            for nid, nodo in grafo.nodes.items()
        },
        [(e.from_, e.to, e.condition) for e in grafo.edges],
        grafo.start_node,
        grafo.meta.to_dict(),
    )


def _ida_y_vuelta(texto: str) -> tuple[FlowGraph, FlowGraph]:
    """
    Con cabecera: es el caso del archivo suelto. Lo que guarda la base va sin
    ella —la metadata está en columnas— y eso lo cubre
    `test_lo_que_guarda_la_base_va_sin_cabecera`.
    """
    antes = parse_flow(texto, with_meta=True)
    despues = parse_flow(to_mermaid(antes, with_header=True), with_meta=True)
    return antes, despues


# ── El corpus real ──────────────────────────────────────────────────────


def test_el_corpus_round_trippea():
    """
    Los 10 flujos del corpus, uno por uno. Es el test que justifica el módulo:
    guardar un flujo desde el editor no puede cambiarlo.
    """
    archivos = sorted(FLOWS.glob("*.mmd"))
    assert archivos, "el corpus de flujos está vacío"

    for path in archivos:
        antes, despues = _ida_y_vuelta(path.read_text(encoding="utf-8"))
        assert _huella(antes) == _huella(despues), f"{path.stem} no round-trippea"


def test_ningun_flujo_del_corpus_tiene_problemas_de_serializacion():
    for path in sorted(FLOWS.glob("*.mmd")):
        grafo = parse_flow(path.read_text(encoding="utf-8"), with_meta=True)
        assert verificar(grafo) == [], f"{path.stem}: {verificar(grafo)}"


# ── El separador ────────────────────────────────────────────────────────


def test_los_params_se_escriben_con_pipe_y_nunca_con_coma():
    """La coma se sigue leyendo, pero no se escribe más: al parsear también separa."""
    grafo = FlowGraph(
        nodes={"N1": ActionNode(fn="core.log", params={"a": "1", "b": "2"}, line=1)},
    )
    texto = to_mermaid(grafo)
    assert "| a=1| b=2" in texto
    assert "," not in texto


def test_el_separador_no_le_agrega_un_espacio_al_valor_anterior():
    """
    El parser no trimea el valor a propósito: hay params que empiezan o terminan
    con espacio, como `windowsRenameSuffix= - COMPLETADO`. Con `" | "` de
    separador, cada valor seguido de otro param se llevaría un espacio de más y
    el flujo no volvería igual — es la razón de que el espacio vaya **después**
    del pipe.

    (Un espacio al final de la *etiqueta* entera lo come el parser antes de
    llegar acá, así que el caso que se puede preservar es el de en medio.)
    """
    texto = (
        'flowchart TD\n    A(inicio)\n'
        '    N1["windows.rename_folder | suffix= - HECHO | otro=x"]\n    A --> N1\n'
    )
    antes, despues = _ida_y_vuelta(texto)
    assert antes.nodes["N1"].params["suffix"] == " - HECHO "
    assert despues.nodes["N1"].params["suffix"] == " - HECHO "


# ── Comas y pipes: se citan solos, no se avisan más ─────────────────────


def test_un_valor_con_pipe_no_es_problema_se_cita_solo():
    """Issue #10: antes esto no round-trippeaba; ahora se escribe entre comillas."""
    grafo = FlowGraph(nodes={"N1": ActionNode(fn="core.log", params={"msg": "a|b"}, line=1)})
    assert verificar(grafo) == []
    assert '"a|b"' in to_mermaid(grafo)


def test_un_valor_con_coma_no_es_problema_se_cita_solo():
    """Es el defecto original: `message=Hola, mundo` perdía " mundo"."""
    grafo = FlowGraph(nodes={"N1": ActionNode(fn="core.log", params={"msg": "Hola, mundo"}, line=1)})
    assert verificar(grafo) == []
    antes, despues = _ida_y_vuelta(to_mermaid(grafo))
    assert despues.nodes["N1"].params["msg"] == "Hola, mundo"


def test_un_valor_sin_coma_ni_pipe_no_se_cita():
    """No le agrega comillas a los nodos que no las necesitan."""
    grafo = FlowGraph(nodes={"N1": ActionNode(fn="core.log", params={"msg": "hola"}, line=1)})
    assert 'msg="hola"' not in to_mermaid(grafo)
    assert "msg=hola" in to_mermaid(grafo)


# ── Lo que no se puede escribir ─────────────────────────────────────────


def test_un_valor_con_comilla_se_avisa():
    """Sin escape para una comilla dentro de un valor citado, esto sigue roto."""
    grafo = FlowGraph(nodes={"N1": ActionNode(fn="core.log", params={"msg": 'di "hola"'}, line=1)})
    assert verificar(grafo)


def test_la_coma_en_una_condicion_no_es_un_problema():
    """En una arista la coma es el operador IN, y ahí sí significa algo."""
    grafo = FlowGraph(
        nodes={"A": StartNode(line=1), "B": ActionNode(fn="core.log", line=2)},
        edges=[FlowEdge(from_="A", to="B", condition="stage=Produccion,Impresion")],
    )
    assert verificar(grafo) == []
    assert "|stage=Produccion,Impresion|" in to_mermaid(grafo)


def test_un_pipe_en_una_condicion_si_es_un_problema():
    grafo = FlowGraph(
        nodes={"A": StartNode(line=1), "B": ActionNode(fn="core.log", line=2)},
        edges=[FlowEdge(from_="A", to="B", condition="a|b")],
    )
    assert verificar(grafo)


def test_strict_levanta_antes_de_guardar_algo_roto():
    grafo = FlowGraph(nodes={"N1": ActionNode(fn="core.log", params={"msg": 'di "hola"'}, line=1)})
    try:
        to_mermaid(grafo, strict=True)
        raise AssertionError("debió levantar")
    except SerializeError as exc:
        assert "msg" in str(exc)


def test_sin_strict_escribe_igual_y_deja_decidir():
    """Guardar a medias mientras se escribe está permitido; hacerlo callado, no."""
    grafo = FlowGraph(nodes={"N1": ActionNode(fn="core.log", params={"msg": 'di "hola"'}, line=1)})
    assert "core.log" in to_mermaid(grafo)


def test_un_nodo_sin_tool_se_avisa():
    grafo = FlowGraph(nodes={"N1": ActionNode(fn="", line=1)})
    assert any("no tiene tool" in p for p in verificar(grafo))


# ── Formas y orden ──────────────────────────────────────────────────────


def test_cada_tipo_de_nodo_usa_su_forma():
    grafo = FlowGraph(nodes={
        "A": StartNode(line=1),
        "B": ActionNode(fn="core.log", display="Log", line=2),
        "C": DecisionNode(variable="stage", display="Etapa", line=3),
    })
    texto = to_mermaid(grafo)
    assert "A(inicio)" in texto
    assert 'B["Log § core.log"]' in texto
    assert "C{Etapa § stage}" in texto


def test_el_orden_del_archivo_se_conserva_y_los_nuevos_van_al_final():
    """
    Reordenar alfabéticamente haría que guardar sin cambiar nada produjera un
    diff enorme. Un nodo agregado desde el editor no tiene línea.
    """
    grafo = FlowGraph(nodes={
        "Z": ActionNode(fn="core.log", line=5),
        "nuevo": ActionNode(fn="core.log", line=None),
        "A": ActionNode(fn="core.log", line=1),
    })
    lineas = [l.strip() for l in to_mermaid(grafo).splitlines() if l.startswith("    ")]
    assert [l.split("[")[0] for l in lineas] == ["A", "Z", "nuevo"]


def test_la_cabecera_solo_lleva_lo_que_tiene_valor():
    grafo = FlowGraph(
        nodes={"A": StartNode(line=1)},
        meta=FlowMeta(folder="FORM/CNC4", state="disabled", description=""),
    )
    texto = to_mermaid(grafo, with_header=True)
    assert "%% folder: FORM/CNC4" in texto
    assert "%% state: disabled" in texto
    assert "description" not in texto


def test_lo_que_guarda_la_base_va_sin_cabecera():
    """
    `WorkflowStore` tiene carpeta, estado y descripción en columnas, y `content`
    es sólo el cuerpo. Escribir la cabecera adentro la dejaría duplicada, y
    `to_mmd()` le pondría una segunda encima al exportar.
    """
    grafo = FlowGraph(
        nodes={"A": StartNode(line=1)},
        meta=FlowMeta(folder="FORM/CNC4", state="disabled"),
    )
    texto = to_mermaid(grafo)
    assert "%%" not in texto
    assert texto.startswith("flowchart TD")


# ── Desde el payload del editor ─────────────────────────────────────────


def test_from_dict_acepta_lo_que_devuelve_to_dict():
    original = parse_flow(
        (FLOWS / "reintentos.mmd").read_text(encoding="utf-8"), with_meta=True
    )
    assert to_mermaid(from_dict(original.to_dict())) == to_mermaid(original)


def test_from_dict_convierte_los_params_a_texto():
    """El DSL no tiene tipos: un número que llega como número se escribe igual."""
    grafo = from_dict({"nodes": {"N1": {"type": "action", "fn": "flow.wait", "params": {"segundos": 5}}}})
    assert grafo.nodes["N1"].params == {"segundos": "5"}


def test_from_dict_con_un_tipo_desconocido_lo_reporta_en_vez_de_explotar():
    grafo = from_dict({"nodes": {"N1": {"type": "marciano"}}})
    assert verificar(grafo)


def test_from_dict_tolera_un_payload_vacio():
    grafo = from_dict({})
    assert grafo.nodes == {} and grafo.edges == []
    assert "flowchart TD" in to_mermaid(grafo)


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
