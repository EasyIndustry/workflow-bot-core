"""
Executor de flujos.

Recorre el grafo: prioridad de aristas loop → status → incondicional, tope de
visitas por nodo, anidamiento por flow.ejecutar.

Garantías que sostiene este módulo:

- **Sin contaminación entre nodos.** Los params llegan al tool por el contrato.
  No hay estado global compartido, así que dos casos en paralelo no se pisan.
- **Todo queda trazado.** Cada nodo deja una entrada con sus params *resueltos*,
  su status, su duración y su traceback si falló. Es lo que hace respondible
  "¿por qué falló esta corrida?".
- **Nada falla en silencio.** Un tool que no existe o que revienta corta el
  flujo con un error explícito, en lugar de loguearlo y seguir por la rama de
  éxito.
- **El plugin recibe sus ports acá.** El executor arma el ToolContext con los
  adapters que el registry resolvió para ese tool. Un plugin nunca los busca.

flow.* se resuelve nativamente porque es control de flujo, no integración:
retry_gate necesita contadores del run y ejecutar necesita recursión.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from ..contract import STATUS_ERR, STATUS_OK, ParamError, ToolContext, ToolResult
from ..registry import ToolRegistry
from .context import RunContext
from .parser import (
    ActionNode,
    DecisionNode,
    FlowGraph,
    StartNode,
    UnknownNode,
    parse_flow,
)

# Tope de visitas por nodo: la red de seguridad contra un ciclo infinito. Un
# flujo con reintentos legítimamente vuelve muchas veces al mismo nodo, así que
# el número es alto a propósito; lo que corta un ciclo *intencional* es
# flow.retry_gate, no esto.
MAX_VISITS = 100

# Profundidad máxima de flujos anidados vía flow.ejecutar.
MAX_DEPTH = 5

# Control de flujo resuelto por el executor, no por el registry de tools.
FLOW_EJECUTAR = "flow.ejecutar"
FLOW_RETRY_GATE = "flow.retry_gate"
NATIVE_FNS = frozenset({FLOW_EJECUTAR, FLOW_RETRY_GATE})


@dataclass
class LogEntry:
    t: str
    message: str
    level: str = "info"
    node_id: str | None = None
    # El epoch además de la hora `HH:MM:SS`: con sólo la hora no se puede
    # ordenar entre días ni borrar por antigüedad, que es lo que hace la
    # limpieza del registro.
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "t": self.t, "message": self.message, "level": self.level,
            "node_id": self.node_id, "ts": self.ts,
        }


@dataclass
class NodeTrace:
    """
    Lo que pasó en un nodo. El registro que antes no existía.

    params guarda los valores **ya resueltos**, que es lo que hace falta para
    entender un fallo: no sirve saber que el nodo pedía {filesFolder} sino a qué
    ruta se resolvió esa vez.
    """

    node_id: str
    node_type: str
    fn: str | None = None
    display: str = ""
    params: dict = field(default_factory=dict)
    status: str = STATUS_OK
    message: str = ""
    outputs: dict = field(default_factory=dict)
    loop: bool = False
    traceback: str | None = None
    error_kind: str | None = None
    duration_ms: int = 0
    visit: int = 1
    depth: int = 1
    flow: str = ""
    decision_value: str | None = None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "fn": self.fn,
            "display": self.display,
            "params": self.params,
            "status": self.status,
            "message": self.message,
            "outputs": self.outputs,
            "loop": self.loop,
            "traceback": self.traceback,
            "error_kind": self.error_kind,
            "duration_ms": self.duration_ms,
            "visit": self.visit,
            "depth": self.depth,
            "flow": self.flow,
            "decision_value": self.decision_value,
        }


@dataclass
class RunResult:
    """Resultado de un run completo, con su traza."""

    run_id: str
    case_id: str
    actor: str = "local"
    status: str = STATUS_OK
    message: str = ""
    failed_node: str | None = None
    error_kind: str | None = None
    trace: list[NodeTrace] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    dry_run: bool = False

    @property
    def failed(self) -> bool:
        return self.status != STATUS_OK

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "actor": self.actor,
            "status": self.status,
            "message": self.message,
            "failed_node": self.failed_node,
            "error_kind": self.error_kind,
            "dry_run": self.dry_run,
            "trace": [t.to_dict() for t in self.trace],
            "logs": [entry.to_dict() for entry in self.logs],
        }


class _Run:
    """Estado mutable de un run. Vive mientras el run corre."""

    def __init__(
        self,
        *,
        run_id: str,
        case_id: str,
        registry: ToolRegistry,
        context: RunContext,
        load_flow: Callable[[str], str] | None,
        is_cancelled: Callable[[], bool],
        resources: Callable[[str, str], list[dict]],
        policy,
        dry_run: bool,
        max_visits: int,
    ) -> None:
        self.result = RunResult(run_id=run_id, case_id=case_id, dry_run=dry_run)
        self.policy = policy
        self.case_id = case_id
        self.registry = registry
        self.context = context
        self.load_flow = load_flow
        self.is_cancelled = is_cancelled
        self.resources = resources
        self.dry_run = dry_run
        self.max_visits = max_visits
        self.retry_counters: dict[str, int] = {}
        # Status de la última acción: define qué arista |ok|/|err| se toma.
        self.last_status = STATUS_OK
        self.last_loop = False

    def log(self, message: str, level: str = "info", node_id: str | None = None) -> None:
        self.result.logs.append(
            LogEntry(t=time.strftime("%H:%M:%S"), message=message, level=level, node_id=node_id)
        )

    def fail(
        self, message: str, node_id: str | None = None, *, error_kind: str | None = None
    ) -> None:
        """
        Marca el run como fallido.

        Preserva el PRIMER mensaje, el primer nodo y su error_kind: al
        desapilar flujos anidados, cada nivel querría reportar su propio
        "terminó con error" y taparía la causa raíz.
        """
        primero = not self.result.failed
        self.result.status = STATUS_ERR
        if primero:
            self.result.message = message
            self.result.error_kind = error_kind
        if node_id and not self.result.failed_node:
            self.result.failed_node = node_id
        if message:
            self.log(message, "error", node_id)


def execute_flow(
    flow_text: str,
    *,
    case_id: str,
    registry: ToolRegistry,
    row: dict | None = None,
    config: dict | None = None,
    env: dict | None = None,
    run_id: str | None = None,
    load_flow: Callable[[str], str] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    resources: Callable[[str, str], list[dict]] | None = None,
    policy: "RunPolicy | None" = None,
    dry_run: bool = False,
    max_visits: int = MAX_VISITS,
) -> RunResult:
    """
    Ejecuta un flujo completo y devuelve su resultado con la traza.

    load_flow(nombre) -> texto del flujo, para resolver flow.ejecutar. Sin él,
    un nodo flow.ejecutar falla con un error explicativo en lugar de colgarse.

    resources(plugin, coleccion) -> items, para que un tool lea las colecciones
    que su plugin declaró sin saber dónde están guardadas. Sin él, un tool que
    las use ve una colección vacía y lo dice; no revienta.

    policy acota qué puede ejecutar el actor de este run. Sin ella no hay
    restricciones, que es el comportamiento de una persona operando su propia
    máquina. El chequeo ocurre **por nodo**, no sólo al empezar: un subflujo
    resuelto en runtime por flow.ejecutar no se puede inspeccionar de antemano.
    """
    from ..users import RunPolicy as _RunPolicy

    context = RunContext(row=dict(row or {}), config=dict(config or {}), env=dict(env or {}))
    run = _Run(
        policy=policy if policy is not None else _RunPolicy.sin_restricciones(),
        run_id=run_id or f"run-{uuid.uuid4().hex[:12]}",
        case_id=case_id,
        registry=registry,
        context=context,
        load_flow=load_flow,
        is_cancelled=is_cancelled or (lambda: False),
        resources=resources or (lambda _plugin, _collection: []),
        dry_run=dry_run,
        max_visits=max_visits,
    )
    run.result.actor = run.policy.actor

    _walk(run, flow_text, depth=1, flow_name="")
    return run.result


def _walk(run: _Run, flow_text: str, *, depth: int, flow_name: str) -> None:
    """Recorre un grafo. Se reentra a sí misma por flow.ejecutar."""
    if depth > MAX_DEPTH:
        run.fail(f"Máximo nivel de anidamiento de flujos alcanzado ({MAX_DEPTH})")
        return

    graph = parse_flow(flow_text)
    etiqueta = f'"{flow_name}"' if flow_name else "principal"

    # Un flujo con errores de parseo no se ejecuta: es la diferencia con el
    # motor viejo, que arrancaba igual y fallaba de formas raras más adelante.
    if not graph.runnable:
        detalle = "; ".join(
            f"L{d.line}: {d.message}" if d.line else d.message for d in graph.errors
        )
        run.fail(f"El flujo {etiqueta} tiene errores y no se puede ejecutar — {detalle}")
        return

    for warning in graph.warnings:
        run.log(
            f"L{warning.line}: {warning.message}" if warning.line else warning.message,
            "warning",
            warning.node_id,
        )

    run.log(
        f"Flujo {etiqueta}: {len(graph.nodes)} nodos, {len(graph.edges)} aristas"
        + (" (dry run)" if run.dry_run else "")
    )

    visits: dict[str, int] = {}
    current: str | None = graph.start_node

    while current:
        if run.is_cancelled():
            run.result.status = STATUS_OK
            run.log("Detenido por el usuario", "warning", current)
            return

        node = graph.nodes[current]
        visits[current] = visits.get(current, 0) + 1
        if visits[current] > run.max_visits:
            run.fail(
                f'Abortado: el nodo "{current}" se visitó {visits[current]} veces '
                f"(tope {run.max_visits})",
                current,
            )
            return

        current = _step(
            run,
            graph,
            current,
            node,
            visit=visits[current],
            depth=depth,
            flow_name=flow_name,
        )
        if run.result.failed:
            return

    run.log(f"Flujo {etiqueta} completado")


def _step(
    run: _Run,
    graph: FlowGraph,
    node_id: str,
    node,
    *,
    visit: int,
    depth: int,
    flow_name: str,
) -> str | None:
    """Ejecuta un nodo y devuelve el siguiente, o None si el flujo termina."""

    if isinstance(node, StartNode):
        edge = next((e for e in graph.out_edges(node_id) if e.condition is None), None)
        return edge.to if edge else None

    if isinstance(node, UnknownNode):
        # No debería llegar acá: parse_flow lo marca como error y _walk no
        # ejecuta un grafo con errores. Queda como red de seguridad.
        run.fail(f'Nodo "{node_id}" no está definido', node_id)
        return None

    if isinstance(node, DecisionNode):
        value = run.context.decision_value(node.variable)
        run.result.trace.append(
            NodeTrace(
                node_id=node_id,
                node_type="decision",
                display=node.display,
                depth=depth,
                visit=visit,
                flow=flow_name,
                decision_value="" if value is None else str(value),
            )
        )
        run.log(f'Decisión {node.variable} = "{value if value is not None else ""}"', node_id=node_id)

        nxt = _resolve_decision(graph, node_id, value)
        if nxt is not None:
            return nxt

        # En dry-run el valor casi nunca se conoce: los nodos que lo producen no
        # se ejecutaron. Frenar acá dejaría sin revisar todo lo que sigue, que es
        # justamente lo que el dry-run tiene que revisar. Se sigue por la primera
        # rama y se avisa.
        if run.dry_run:
            primera = next(
                (e for e in graph.out_edges(node_id) if e.condition is not None), None
            )
            if primera is not None:
                run.log(
                    f'[DRY] {node.variable} sin valor conocido; se explora la rama '
                    f'"{primera.condition}"',
                    "warning",
                    node_id,
                )
                return primera.to

        run.fail(
            f'Sin rama para "{node.variable} = {value}" en el nodo "{node_id}"',
            node_id,
        )
        return None

    if isinstance(node, ActionNode):
        return _run_action(
            run, graph, node_id, node, visit=visit, depth=depth, flow_name=flow_name
        )

    run.fail(f'Tipo de nodo desconocido en "{node_id}"', node_id)
    return None


def _run_action(
    run: _Run,
    graph: FlowGraph,
    node_id: str,
    node: ActionNode,
    *,
    visit: int,
    depth: int,
    flow_name: str,
) -> str | None:
    started = time.monotonic()
    trace = NodeTrace(
        node_id=node_id,
        node_type="action",
        fn=node.fn,
        display=node.display,
        visit=visit,
        depth=depth,
        flow=flow_name,
    )
    run.result.trace.append(trace)

    # Params con sus `{variables}` sustituidas. Todavía son texto: el tipado lo
    # aplica el manifest más abajo, y la traza se actualiza con el resultado.
    resueltos, pendientes = _resolve_raw_params(run, node)
    trace.params = resueltos
    if pendientes:
        trace.message = f"variables sin resolver: {', '.join(pendientes)}"
        run.log(
            f"{node.fn}: variables sin resolver: {', '.join(pendientes)}", "warning", node_id
        )

    # ── Autorización ──────────────────────────────────────────────────
    #
    # Por nodo, y no sólo al empezar el run. `Instance` hace un chequeo previo
    # sobre el grafo para fallar temprano con el panorama completo, pero un
    # subflujo que flow.ejecutar resuelve en runtime —con un nombre que puede
    # venir de una variable— no se puede inspeccionar de antemano. Éste es el
    # que garantiza; aquél es el que da un buen mensaje.
    #
    # Vale también en dry-run: un recorrido en seco que aprueba lo que el run
    # real va a rechazar no sirve para nada.
    if node.fn not in NATIVE_FNS:
        manifest = run.registry.manifest(node.fn)
        if manifest is not None:
            dueño = run.registry.plugin_of(node.fn)
            motivo = run.policy.deniega(
                manifest, dueño.ports if dueño else (), dueño.name if dueño else ""
            )
            if motivo:
                trace.status = STATUS_ERR
                trace.message = motivo
                run.fail(f'Detenido en "{node_id}": {motivo}', node_id)
                return None

    # ── Dry run: valida y sigue, sin ejecutar nada ────────────────────
    if run.dry_run:
        if node.fn not in NATIVE_FNS and run.registry.get(node.fn) is None:
            trace.status = STATUS_ERR
            trace.message = f"tool desconocido: {node.fn}"
            run.fail(f'El nodo "{node_id}" usa un tool que no existe: {node.fn}', node_id)
            return None

        # Los params se tipan igual que en una corrida real. Un `seconds=abc`
        # tiene que salir acá y no reventar recién en producción: un dry run que
        # pasa sobre una entrada que el run real rechaza no sirve de nada.
        manifest = run.registry.manifest(node.fn)
        if manifest is not None:
            try:
                trace.params = manifest.resolve_params(resueltos, run.context.config)
            except ParamError as exc:
                trace.status = STATUS_ERR
                trace.message = str(exc)
                run.fail(f'Detenido en "{node_id}" ({node.fn}): {exc}', node_id)
                return None

        run.log(
            f"[DRY] {node.fn}" + (f" | {_fmt(trace.params)}" if trace.params else ""),
            node_id=node_id,
        )
        trace.duration_ms = int((time.monotonic() - started) * 1000)
        run.last_status, run.last_loop = STATUS_OK, False
        return _resolve_action_next(graph, node_id, STATUS_OK, False)

    # ── Control de flujo nativo ───────────────────────────────────────
    if node.fn == FLOW_RETRY_GATE:
        result = _retry_gate(run, resueltos)
    elif node.fn == FLOW_EJECUTAR:
        result = _ejecutar_flujo(run, resueltos, depth=depth)
    else:
        # La traza tiene que mostrar lo que el tool **recibió**, no lo previo al
        # tipado. Mostrar `"true"` donde el tool ve `True` induce a escribir
        # código defensivo contra un problema que no existe.
        result = _invoke_tool(
            run, node, node_id, resueltos, on_params=lambda p: setattr(trace, "params", p)
        )

    trace.status = result.status
    trace.message = result.message
    trace.outputs = dict(result.outputs)
    trace.loop = result.loop
    trace.traceback = result.traceback
    trace.error_kind = result.error_kind
    trace.duration_ms = int((time.monotonic() - started) * 1000)

    if result.outputs:
        run.context.merge_outputs(result.outputs)

    if result.message:
        run.log(
            f"{node.fn}: {result.message}",
            "error" if result.failed else "info",
            node_id,
        )

    run.last_status, run.last_loop = result.status, result.loop

    if result.failed:
        # Sin arista |err| el flujo corta. Con ella, ramifica y sigue.
        if not any(e.condition == STATUS_ERR for e in graph.out_edges(node_id)):
            run.fail(
                f'Detenido en "{node_id}" ({node.fn}): '
                f"{result.message or 'terminó con error'}",
                node_id,
                error_kind=result.error_kind,
            )
            return None

    return _resolve_action_next(graph, node_id, result.status, result.loop)




def _resolve_raw_params(run: _Run, node: ActionNode) -> tuple[dict, list[str]]:
    """Resuelve los `{var}` de los params crudos y reporta los que quedaron."""
    resueltos: dict[str, str] = {}
    pendientes: list[str] = []
    for key, raw in node.params.items():
        resueltos[key] = run.context.resolve(raw)
        pendientes.extend(run.context.unresolved(raw))
    return resueltos, pendientes


def _invoke_tool(
    run: _Run,
    node: ActionNode,
    node_id: str,
    resueltos: dict,
    on_params: Callable[[dict], None] | None = None,
) -> ToolResult:
    """
    Llama a un tool a través del registry.

    El registry convierte cualquier fallo en un ToolResult con status err, así
    que acá no hace falta un try: no hay camino por el que un fallo se pierda.

    `on_params` recibe los params ya tipados, para que la traza registre lo que
    el tool realmente vio. Se llama sólo si el tipado tuvo éxito: cuando falla,
    lo que interesa ver es el valor crudo que no convirtió.
    """

    def ctx_factory(manifest, ports):
        declarados, extras = manifest.split_params(resueltos, run.context.config)
        if on_params is not None:
            on_params({**declarados, **extras})
        # El lector queda ligado al plugin dueño del tool: un tool ve sus
        # colecciones y no las de otro plugin.
        dueño = run.registry.plugin_of(manifest.id)
        nombre_plugin = dueño.name if dueño else ""
        return ToolContext(
            run_id=run.result.run_id,
            case_id=run.case_id,
            params=declarados,
            extras=extras,
            config=run.context.config,
            context={**run.context.row, **run.context.vars},
            log=lambda message, level="info": run.log(message, level, node_id),
            is_cancelled=run.is_cancelled,
            resources=lambda coleccion: run.resources(nombre_plugin, coleccion),
            # Ya filtrados por el registry contra el manifest del plugin: acá
            # llega lo que ese plugin declaró, ni más ni menos.
            ports=ports,
        )

    return run.registry.execute(node.fn, ctx_factory)


def _retry_gate(run: _Run, params: dict) -> ToolResult:
    """
    Contador de reintentos por run, combinado con una arista |loop|.

    Vive en el executor porque necesita estado del run, que el contrato
    deliberadamente no le da a los tools.
    """
    key = (params.get("retryGateKey") or "retryCount").strip()
    raw_max = params.get("retryGateMax") or "10"
    try:
        limite = int(str(raw_max).strip())
    except ValueError:
        return ToolResult.err(f"retryGateMax inválido: {raw_max!r}")

    intento = run.retry_counters.get(key, 0) + 1
    run.retry_counters[key] = intento

    if intento >= limite:
        return ToolResult.err(f"[{key}] límite de {limite} intentos alcanzado")
    return ToolResult.again(f"[{key}] intento {intento}/{limite}")


def _ejecutar_flujo(run: _Run, params: dict, *, depth: int) -> ToolResult:
    """Ejecuta un flujo anidado. Comparte contexto, traza y contadores."""
    nombre = (params.get("flowName") or "").strip()
    if not nombre:
        nombre = str(run.context.config.get("defaultFlowName") or "").strip()
    if not nombre:
        return ToolResult.err("no se indicó flowName ni hay un flujo por defecto configurado")

    if run.load_flow is None:
        return ToolResult.err(
            f'no se puede cargar el flujo "{nombre}": el run se creó sin load_flow'
        )

    try:
        texto = run.load_flow(nombre)
    except Exception as exc:
        return ToolResult.err(f'no se pudo cargar el flujo "{nombre}": {exc}')

    _walk(run, texto, depth=depth + 1, flow_name=nombre)
    if run.result.failed:
        # El fallo ya quedó registrado con su nodo; no se duplica el mensaje.
        return ToolResult(status=STATUS_ERR, message="")
    return ToolResult.ok()


def _resolve_action_next(
    graph: FlowGraph, node_id: str, status: str, loop: bool
) -> str | None:
    """Prioridad de aristas desde una acción: loop → status → incondicional."""
    edges = graph.out_edges(node_id)

    if loop:
        if (edge := next((e for e in edges if e.condition == "loop"), None)) is not None:
            return edge.to

    if (edge := next((e for e in edges if e.condition == status), None)) is not None:
        return edge.to

    edge = next((e for e in edges if e.condition is None), None)
    return edge.to if edge else None


def _resolve_decision(graph: FlowGraph, node_id: str, value) -> str | None:
    """Compara el valor contra los tokens de cada arista, separados por coma."""
    normalized = ("" if value is None else str(value)).strip().lower()

    for edge in graph.out_edges(node_id):
        if edge.condition is None:
            continue
        tokens = [t.strip().lower() for t in edge.condition.split(",")]
        if normalized in tokens:
            return edge.to

    edge = next((e for e in graph.out_edges(node_id) if e.condition is None), None)
    return edge.to if edge else None


def _fmt(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


__all__ = [
    "MAX_DEPTH",
    "MAX_VISITS",
    "LogEntry",
    "NodeTrace",
    "RunResult",
    "execute_flow",
]
