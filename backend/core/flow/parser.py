"""
Parser de flujos Mermaid → grafo ejecutable.

Parser del DSL de flujos (Mermaid con anotaciones). Las reglas de parseo son las
mismas (ver docs/API DOCS/FLUJO_SPEC.md), así que los 38 flujos existentes se
parsean igual. Lo que cambia es qué pasa cuando el flujo está mal escrito.

El parser original convertía un nodo referenciado pero nunca definido en una
acción cuya función era el propio ID del nodo. Un typo se volvía una llamada a
una función inexistente que el dispatcher logueaba sin marcar error, y el flujo
seguía por la rama de éxito. Acá esos nodos quedan como UnknownNode con un
diagnóstico de error: el editor puede seguir mostrando el grafo, la validación
lo reporta, y el executor falla explícito si lo alcanza.

Verificado sobre workflows/: solo CNC.mmd y "test variables.mmd" tienen nodos
huérfanos, y ambos son `folder: TEST, state: disabled`. Ningún flujo habilitado
se rompe por endurecer esto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Union

# Condiciones de arista que el motor interpreta como guardas de estado.
# Cualquier otra condición se compara contra el valor de un nodo de decisión.
RESERVED_CONDITIONS = frozenset({"ok", "err", "loop"})

# Separador entre el nombre visible y la definición técnica: "Mi nodo § core.log"
DISPLAY_SEP = "§"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Diagnostic:
    """Un problema encontrado al parsear. Los errores impiden ejecutar."""

    severity: Severity
    message: str
    line: int | None = None
    node_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "message": self.message,
            "line": self.line,
            "node_id": self.node_id,
        }


@dataclass(frozen=True)
class StartNode:
    label: str = "inicio"
    line: int | None = None
    type: str = "start"


@dataclass(frozen=True)
class ActionNode:
    fn: str
    params: dict[str, str] = field(default_factory=dict)
    display: str = ""  # nombre visible antes del §; mejora las trazas y la UI
    line: int | None = None
    type: str = "action"


@dataclass(frozen=True)
class DecisionNode:
    variable: str
    display: str = ""
    line: int | None = None
    type: str = "decision"


@dataclass(frozen=True)
class UnknownNode:
    """Nodo referenciado en una arista pero nunca definido. Siempre es un error."""

    line: int | None = None
    type: str = "unknown"


FlowNode = Union[StartNode, ActionNode, DecisionNode, UnknownNode]


def _node_to_dict(node: FlowNode) -> dict:
    """Serializa un nodo omitiendo los campos que no aplican a su tipo."""
    data: dict = {"type": node.type, "line": node.line}
    for attr in ("fn", "params", "variable", "label"):
        if (value := getattr(node, attr, None)) is not None:
            data[attr] = value
    if display := getattr(node, "display", ""):
        data["display"] = display
    return data


@dataclass(frozen=True)
class FlowEdge:
    from_: str
    to: str
    condition: str | None = None
    line: int | None = None

    def to_dict(self) -> dict:
        return {"from": self.from_, "to": self.to, "condition": self.condition}


@dataclass(frozen=True)
class FlowMeta:
    """Cabecera `%% key: value` del archivo .mmd."""

    folder: str = ""
    state: str = "enabled"
    description: str = ""

    @property
    def enabled(self) -> bool:
        return self.state != "disabled"

    def to_dict(self) -> dict:
        return {"folder": self.folder, "state": self.state, "description": self.description}


@dataclass
class FlowGraph:
    nodes: dict[str, FlowNode] = field(default_factory=dict)
    edges: list[FlowEdge] = field(default_factory=list)
    start_node: str | None = None
    meta: FlowMeta = field(default_factory=FlowMeta)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def out_edges(self, node_id: str) -> list[FlowEdge]:
        return [e for e in self.edges if e.from_ == node_id]

    def in_edges(self, node_id: str) -> list[FlowEdge]:
        return [e for e in self.edges if e.to == node_id]

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.WARNING]

    @property
    def runnable(self) -> bool:
        """Un flujo con errores no se ejecuta. Con warnings sí."""
        return not self.errors

    def action_nodes(self) -> list[tuple[str, ActionNode]]:
        return [(nid, n) for nid, n in self.nodes.items() if isinstance(n, ActionNode)]

    def to_dict(self) -> dict:
        """Payload para la UI y el MCP: el grafo tal como el motor lo entiende."""
        return {
            "meta": self.meta.to_dict(),
            "start_node": self.start_node,
            "nodes": {nid: _node_to_dict(node) for nid, node in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "runnable": self.runnable,
        }


# ── Expresiones del DSL (mismas que el motor anterior) ──────────────────────────

_META_RE = re.compile(r"^%%\s+(\w+):\s*(.*)$")
_EDGE_RE = re.compile(r"^(\w+)\s*-->\s*(?:\|([^|]*)\|)?\s*(\w+)(.*)?$")
_CONT_RE = re.compile(r"^-->\s*(?:\|([^|]*)\|)?\s*(\w+)(.*)?$")

_SHAPES = (
    # `.*` y no `[^"]*`: un valor citado (`message="a, b"`) mete comillas
    # adentro de la etiqueta, y greedy + backtracking hace que esto siga
    # tomando la primera y la última como el wrapper de Mermaid, no las de
    # adentro (ver `_dividir_pares`, que es quien realmente las interpreta).
    (re.compile(r'^(\w+)\["(.*)"\]$'), "rect"),
    (re.compile(r"^(\w+)\[([^\]]+)\]$"), "rect"),
    (re.compile(r"^(\w+)\{([^}]+)\}$"), "diamond"),
    (re.compile(r"^(\w+)\(([^)]+)\)$"), "round"),
)

_INLINE_SHAPES = (
    (re.compile(r'^\["(.*)"\]$'), "rect"),
    (re.compile(r"^\[([^\]]+)\]$"), "rect"),
    (re.compile(r"^\{([^}]+)\}$"), "diamond"),
    (re.compile(r"^\(([^)]+)\)$"), "round"),
)


def parse_meta(raw: str) -> tuple[FlowMeta, str]:
    """
    Separa la cabecera `%% key: value` del contenido Mermaid.

    La cabecera es parte del DSL, así
    que su parseo pertenece al núcleo y no a la capa HTTP.
    """
    values = {"folder": "", "state": "enabled", "description": ""}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        m = _META_RE.match(lines[i])
        if not m:
            break
        key, val = m.group(1).lower(), m.group(2).strip()
        if key in values:
            values[key] = val
        i += 1
    return FlowMeta(**values), "\n".join(lines[i:]).lstrip("\n")


def _split_display(label: str) -> tuple[str, str]:
    """Separa "Nombre visible § definición" → (display, definición)."""
    idx = label.find(DISPLAY_SEP)
    if idx < 0:
        return "", label.strip()
    return label[:idx].strip(), label[idx + len(DISPLAY_SEP) :].strip()


def _dividir_pares(texto: str) -> tuple[list[str], bool]:
    """
    Separa por `,` y `|`, salvo dentro de comillas dobles.

    `k="v, con coma"` es un solo segmento: la coma de adentro no separa. La
    comilla sólo abre pegada a un `=` (`key="...`) — en cualquier otro lugar
    del valor es un carácter más, sin efecto. No hay escape para una comilla
    dentro del valor citado: es el límite conocido de esta primera versión.

    Devuelve además si el texto terminó con una comilla sin cerrar, para que
    quien llama pueda avisar en vez de devolver un valor cortado a la mitad.
    """
    segmentos: list[str] = []
    actual: list[str] = []
    en_comillas = False
    for ch in texto:
        if ch == '"' and (en_comillas or not actual or actual[-1] == "="):
            en_comillas = not en_comillas
            actual.append(ch)
        elif ch in ",|" and not en_comillas:
            segmentos.append("".join(actual))
            actual = []
        else:
            actual.append(ch)
    segmentos.append("".join(actual))
    return segmentos, en_comillas


def _parse_action_label(label: str) -> tuple[str, dict[str, str], str, list[str], bool]:
    """
    Parsea "Nombre § fn.id | k=v, k2=v2" → (fn_id, params, display, descartados, sin_cerrar).

    El valor no se trimea, igual que en el motor anterior: preserva sufijos con espacios
    como windowsRenameSuffix= - COMPLETADO. Lo mismo adentro de un valor citado.

    Un valor entre comillas dobles puede contener comas y pipes:
    `message="Hola, mundo"` guarda "Hola, mundo" entero. Sin comillas, la coma
    sigue separando como siempre —`message=Hola, mundo` pierde " mundo"— así
    que ningún flujo existente cambia de comportamiento con esto.
    """
    display, definition = _split_display(label)
    pipe = definition.find("|")
    fn_id = (definition[:pipe] if pipe >= 0 else definition).strip()

    params: dict[str, str] = {}
    descartados: list[str] = []
    sin_cerrar = False
    if pipe >= 0:
        pares, sin_cerrar = _dividir_pares(definition[pipe + 1 :])
        for pair in pares:
            eq = pair.find("=")
            if eq < 0:
                if pair.strip():
                    descartados.append(pair.strip())
                continue
            key = pair[:eq].strip()
            valor = pair[eq + 1 :]
            if len(valor) >= 2 and valor[0] == '"' and valor[-1] == '"':
                valor = valor[1:-1]
            if key:
                params[key] = valor
            elif pair.strip():
                descartados.append(pair.strip())
    return fn_id, params, display, descartados, sin_cerrar


class _Builder:
    """Acumula nodos, aristas y diagnósticos durante el parseo."""

    def __init__(self) -> None:
        self.nodes: dict[str, FlowNode] = {}
        self.edges: list[FlowEdge] = []
        self.diagnostics: list[Diagnostic] = []
        self._defined_at: dict[str, int] = {}

    def warn(self, message: str, line: int | None = None, node_id: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(Severity.WARNING, message, line, node_id))

    def error(self, message: str, line: int | None = None, node_id: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(Severity.ERROR, message, line, node_id))

    def register(self, node_id: str, shape: str, raw_label: str, line: int) -> None:
        """Registra un nodo. El primero gana, igual que en el motor anterior."""
        label = raw_label.replace("\\n", " ").strip()

        if node_id in self.nodes and not isinstance(self.nodes[node_id], UnknownNode):
            self.warn(
                f'Nodo "{node_id}" definido más de una vez; se usa la primera '
                f"definición (línea {self._defined_at.get(node_id)})",
                line,
                node_id,
            )
            return

        if shape == "round":
            node: FlowNode = StartNode(label=label or "inicio", line=line)
        elif shape == "diamond":
            display, variable = _split_display(label)
            if not variable:
                self.error(f'Nodo de decisión "{node_id}" sin variable', line, node_id)
                variable = ""
            node = DecisionNode(variable=variable, display=display, line=line)
        else:
            fn_id, params, display, descartados, sin_cerrar = _parse_action_label(label)
            if not fn_id:
                self.error(f'Nodo de acción "{node_id}" sin función', line, node_id)
            # Error y no warning: un flujo con un valor mutilado queda
            # "runnable" con un texto cortado a la mitad, que es peor que no
            # ejecutar nada. `graph.runnable` (y por lo tanto `run()`) ya
            # respeta la severidad del diagnóstico, así que subirla acá
            # alcanza para que el flujo deje de poder correr así.
            for texto in descartados:
                self.error(
                    f'En "{node_id}" se descartó "{texto}": no tiene forma clave=valor. '
                    f'Si el valor necesita una coma o un pipe, va entre comillas: '
                    f'clave="{texto}".',
                    line,
                    node_id,
                )
            if sin_cerrar:
                self.error(
                    f'En "{node_id}" hay una comilla sin cerrar en el valor de un parámetro.',
                    line,
                    node_id,
                )
            node = ActionNode(fn=fn_id, params=params, display=display, line=line)

        self.nodes[node_id] = node
        self._defined_at[node_id] = line

    def register_inline(self, node_id: str, rest: str, line: int) -> None:
        """Extrae la definición de un nodo escrita al final de una línea de arista."""
        text = rest.strip()
        if not text:
            return
        for pattern, shape in _INLINE_SHAPES:
            m = pattern.match(text)
            if m:
                self.register(node_id, shape, m.group(1), line)
                return


def parse_flow(text: str, *, with_meta: bool = True) -> FlowGraph:
    """
    Parsea un flowchart Mermaid y devuelve el grafo con sus diagnósticos.

    Nunca levanta excepción: un flujo mal escrito produce diagnósticos, para que
    el editor pueda mostrarlo y la validación explicar qué está mal.
    """
    meta = FlowMeta()
    if with_meta:
        meta, text = parse_meta(text)

    b = _Builder()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("%%") or line.startswith("flowchart"):
            continue

        if line.startswith("subgraph") or line == "end":
            b.warn(
                "subgraph no está soportado por el motor; sus nodos se leen "
                "como si estuvieran al mismo nivel",
                lineno,
            )
            continue

        # ── Línea de arista, con posible cadena A-->B-->C ────────────
        m = _EDGE_RE.match(line)
        if m:
            src, raw_cond, dst, rest = m.group(1), m.group(2), m.group(3), m.group(4) or ""
            condition = (raw_cond or "").strip() or None
            b.edges.append(FlowEdge(from_=src, to=dst, condition=condition, line=lineno))
            b.register_inline(dst, rest, lineno)

            chain_from, chain_rest = dst, rest.strip()
            while (cm := _CONT_RE.match(chain_rest)) is not None:
                cond, chain_to, chain_rest = cm.group(1), cm.group(2), (cm.group(3) or "").strip()
                b.edges.append(
                    FlowEdge(
                        from_=chain_from,
                        to=chain_to,
                        condition=(cond or "").strip() or None,
                        line=lineno,
                    )
                )
                b.register_inline(chain_to, chain_rest, lineno)
                chain_from = chain_to
            continue

        # ── Definición de nodo suelta ────────────────────────────────
        for pattern, shape in _SHAPES:
            m = pattern.match(line)
            if m:
                b.register(m.group(1), shape, m.group(2), lineno)
                break
        else:
            b.warn(f"Línea no reconocida por el parser: {line!r}", lineno)

    graph = FlowGraph(nodes=b.nodes, edges=b.edges, meta=meta, diagnostics=b.diagnostics)
    _resolve_orphans(graph)
    _resolve_start(graph)
    _flag_unreachable(graph)
    return graph


def _resolve_orphans(graph: FlowGraph) -> None:
    """
    Nodos usados en aristas pero nunca definidos.

    el motor anterior los convertía en una acción con fn = el ID del nodo, que es cómo
    un typo terminaba ejecutándose como si nada. Acá son un error explícito.
    """
    for edge in graph.edges:
        for node_id, line in ((edge.from_, edge.line), (edge.to, edge.line)):
            if node_id in graph.nodes:
                continue
            graph.nodes[node_id] = UnknownNode(line=line)
            graph.diagnostics.append(
                Diagnostic(
                    Severity.ERROR,
                    f'Nodo "{node_id}" se usa en una arista pero nunca se define. '
                    f"¿Falta su definición, o es un typo?",
                    line,
                    node_id,
                )
            )


def _resolve_start(graph: FlowGraph) -> None:
    """Determina el nodo de inicio. Cero o más de uno son errores."""
    starts = [nid for nid, node in graph.nodes.items() if isinstance(node, StartNode)]

    if not starts:
        graph.diagnostics.append(
            Diagnostic(
                Severity.ERROR,
                'No hay nodo de inicio. Agregá uno con la forma redonda: ID(inicio)',
            )
        )
        return

    graph.start_node = starts[0]
    if len(starts) > 1:
        graph.diagnostics.append(
            Diagnostic(
                Severity.ERROR,
                f"Hay {len(starts)} nodos de inicio ({', '.join(starts)}); "
                f'se usaría "{starts[0]}". Dejá uno solo.',
                graph.nodes[starts[1]].line,
                starts[1],
            )
        )


def _flag_unreachable(graph: FlowGraph) -> None:
    """Nodos a los que el flujo nunca puede llegar desde el inicio."""
    if not graph.start_node:
        return

    reachable: set[str] = set()
    pending = [graph.start_node]
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(e.to for e in graph.out_edges(current))

    for node_id in graph.nodes:
        if node_id not in reachable:
            graph.diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    f'Nodo "{node_id}" es inalcanzable desde el inicio',
                    graph.nodes[node_id].line,
                    node_id,
                )
            )
