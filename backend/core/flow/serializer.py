"""
De grafo a Mermaid. El camino inverso de `parser.py`.

Por qué está en el núcleo y no en el front: el ayudante de la app vieja
generaba el texto en JavaScript, con su propia idea de la gramática. Unía los
parámetros con coma, y como la coma **también** separa parámetros al parsear, un
valor que la contenía se partía en dos — el flujo se guardaba roto y nadie se
enteraba hasta la ejecución. Con el serializador acá, la gramática vive en un
solo lugar y `parse_flow(to_mermaid(g))` es un test.

Dos reglas que salen de eso:

1. **Los parámetros se unen con `|`, nunca con coma.** La coma se sigue
   aceptando al parsear —hay 30 nodos escritos así— pero no se escribe más.
2. **Lo que no puede round-trippear se avisa, no se emite y se reza.**
   `verificar()` devuelve los problemas y `to_mermaid(..., strict=True)` levanta
   antes de guardar. Un valor con `|`, con `,`, o con `"` no sobrevive al
   parseo, y guardarlo en silencio es el defecto que este módulo existe para no
   repetir.
"""

from __future__ import annotations

from .parser import (
    DISPLAY_SEP,
    ActionNode,
    DecisionNode,
    FlowEdge,
    FlowGraph,
    FlowMeta,
    StartNode,
    UnknownNode,
)

# Un valor con estos caracteres no vuelve igual del parser: `|` y `,` son
# separadores de parámetros, y `"` cierra la etiqueta del nodo.
PROHIBIDOS_EN_VALOR = ('|', ',', '"')

# Y en una condición de arista, la coma **sí** significa algo: es el operador
# IN, que arma la lista de valores. Ahí lo que no puede aparecer es el pipe,
# porque delimita la condición.
PROHIBIDOS_EN_CONDICION = ('|', '"')


class SerializeError(Exception):
    """El grafo no se puede escribir sin perder información."""


def to_mermaid(
    grafo: FlowGraph,
    meta: FlowMeta | None = None,
    strict: bool = False,
    with_header: bool = False,
) -> str:
    """
    El texto Mermaid del grafo.

    `with_header=False` —el default— **no** escribe la cabecera `%%`. Es lo que
    guarda la base: `WorkflowStore` tiene la carpeta, el estado y la descripción
    en columnas, y `content` es sólo el cuerpo. Escribir la cabecera acá la
    dejaría en los dos lugares, y `to_mmd()` le pondría una segunda encima al
    exportar.

    `with_header=True` es para un archivo suelto: un `.mmd` que alguien copia a
    otra instalación tiene que llevar su carpeta adentro.

    `strict=True` levanta `SerializeError` si algo no round-trippea. Mejor
    rechazar el guardado con un mensaje que dejar un flujo que falla en la
    primera ejecución.
    """
    problemas = verificar(grafo)
    if problemas and strict:
        raise SerializeError("; ".join(problemas))

    lineas = []
    if with_header:
        cabecera = meta if meta is not None else grafo.meta
        lineas = [f"%% {clave}: {valor}" for clave, valor in cabecera.to_dict().items() if valor]
        if lineas:
            lineas.append("")
    lineas.append("flowchart TD")

    # Los nodos primero y las aristas después, en vez de intercalados. El parser
    # acepta las dos formas; separarlas hace que un diff de dos versiones del
    # mismo flujo muestre qué nodo cambió y no un bloque entero movido.
    for node_id in _orden(grafo):
        lineas.append("    " + _nodo(node_id, grafo.nodes[node_id]))

    if grafo.edges:
        lineas.append("")
    for arista in grafo.edges:
        condicion = f"|{arista.condition}| " if arista.condition else ""
        lineas.append(f"    {arista.from_} --> {condicion}{arista.to}")

    return "\n".join(lineas) + "\n"


def from_dict(datos: dict) -> FlowGraph:
    """
    El grafo desde el payload que manda el editor de tarjetas.

    Es la forma que devuelve `FlowGraph.to_dict()`, así que el editor puede leer
    un flujo, mover una tarjeta y mandarlo de vuelta sin traducir nada. Un tipo
    de nodo que no se reconoce se guarda como `UnknownNode`, que `verificar()`
    reporta: un `KeyError` acá sería un 500 en vez de un mensaje.
    """
    grafo = FlowGraph()
    for node_id, crudo in (datos.get("nodes") or {}).items():
        tipo = (crudo or {}).get("type")
        linea = crudo.get("line")
        if tipo == "start":
            grafo.nodes[node_id] = StartNode(label=crudo.get("label") or "inicio", line=linea)
        elif tipo == "action":
            grafo.nodes[node_id] = ActionNode(
                fn=crudo.get("fn") or "",
                # Todo llega como texto: el DSL no tiene tipos, y el motor los
                # convierte según lo que declara el manifest del tool.
                params={k: "" if v is None else str(v) for k, v in (crudo.get("params") or {}).items()},
                display=crudo.get("display") or "",
                line=linea,
            )
        elif tipo == "decision":
            grafo.nodes[node_id] = DecisionNode(
                variable=crudo.get("variable") or "",
                display=crudo.get("display") or "",
                line=linea,
            )
        else:
            grafo.nodes[node_id] = UnknownNode(line=linea)

    for crudo in datos.get("edges") or []:
        condicion = crudo.get("condition")
        grafo.edges.append(FlowEdge(
            from_=crudo.get("from") or "",
            to=crudo.get("to") or "",
            condition=condicion or None,
        ))

    grafo.start_node = datos.get("start_node")
    cabecera = datos.get("meta") or {}
    grafo.meta = FlowMeta(
        folder=cabecera.get("folder") or "",
        state=cabecera.get("state") or "enabled",
        description=cabecera.get("description") or "",
    )
    return grafo


def verificar(grafo: FlowGraph) -> list[str]:
    """
    Qué se perdería al escribir este grafo. Vacío = round-trippea.

    Es la función que el editor llama mientras se escribe: avisar antes de
    guardar es todo el punto.
    """
    problemas: list[str] = []

    for node_id, nodo in grafo.nodes.items():
        if isinstance(nodo, UnknownNode):
            problemas.append(
                f'El nodo "{node_id}" se usa en una arista pero no está definido'
            )
            continue

        if isinstance(nodo, ActionNode):
            if not nodo.fn.strip():
                problemas.append(f'El nodo "{node_id}" no tiene tool')
            for clave, valor in nodo.params.items():
                for caracter in PROHIBIDOS_EN_VALOR:
                    if caracter in str(valor):
                        problemas.append(
                            f'{node_id} › {clave}: el valor contiene "{caracter}", '
                            f"que separa parámetros o cierra la etiqueta y no "
                            f"sobrevive al volver a leer el flujo"
                        )
                if DISPLAY_SEP in str(valor):
                    problemas.append(
                        f'{node_id} › {clave}: el valor contiene "{DISPLAY_SEP}", '
                        f"que separa el nombre visible de la definición"
                    )

        etiqueta = getattr(nodo, "display", "") or getattr(nodo, "label", "") or ""
        if '"' in etiqueta or "]" in etiqueta:
            problemas.append(f'El nombre visible de "{node_id}" tiene un " o un ], que cierran la etiqueta')

    for arista in grafo.edges:
        for caracter in PROHIBIDOS_EN_CONDICION:
            if arista.condition and caracter in arista.condition:
                problemas.append(
                    f'La condición de {arista.from_} → {arista.to} contiene "{caracter}", '
                    f"que delimita la condición"
                )

    return problemas


def _orden(grafo: FlowGraph) -> list[str]:
    """
    Los nodos en el orden en que estaban en el archivo, y los nuevos al final.

    Reordenarlos alfabéticamente haría que guardar un flujo sin cambiar nada
    produjera un diff enorme. Un nodo agregado desde el editor no tiene línea,
    así que va al final: es donde se lo espera.
    """
    con_linea = [(n, node) for n, node in grafo.nodes.items() if node.line is not None]
    sin_linea = [n for n, node in grafo.nodes.items() if node.line is None]
    con_linea.sort(key=lambda par: par[1].line)
    return [n for n, _ in con_linea] + sin_linea


def _nodo(node_id: str, nodo) -> str:
    if isinstance(nodo, StartNode):
        return f"{node_id}({nodo.label})"

    if isinstance(nodo, DecisionNode):
        cuerpo = _con_display(nodo.display, nodo.variable)
        return f"{node_id}{{{cuerpo}}}"

    if isinstance(nodo, ActionNode):
        # El separador es `"| "`, con el espacio **después** del pipe y nunca
        # antes. El parser no trimea el valor —hay params que terminan en espacio
        # a propósito, como `windowsRenameSuffix= - COMPLETADO`— así que un
        # `" | "` le agregaría un espacio final a cada valor y el flujo no
        # volvería igual. Es la misma forma que ya tienen los archivos con coma:
        # `k1=v1, k2=v2`.
        definicion = nodo.fn.strip()
        for clave, valor in nodo.params.items():
            definicion += f"| {clave}={valor}"
        # El espacio va sólo delante del primer pipe, donde no puede pegarse a
        # ningún valor: `fn` ya está trimeado.
        definicion = definicion.replace("|", " |", 1) if nodo.params else definicion
        cuerpo = _con_display(nodo.display, definicion)
        return f'{node_id}["{cuerpo}"]'

    # UnknownNode: no hay forma de escribirlo, y `verificar()` ya lo reportó. Se
    # emite como un nodo vacío para que el resto del flujo siga siendo legible.
    return f'{node_id}["(sin definir)"]'


def _con_display(display: str, definicion: str) -> str:
    display = (display or "").strip()
    return f"{display} {DISPLAY_SEP} {definicion}" if display else definicion
