"""
Motor de flujos — parseo, validación y ejecución.

Es la mitad del valor del núcleo (la otra es la traza de los runs). Vive en
Python y no en el navegador, para que un flujo se pueda validar, ejecutar y
diagnosticar sin una pestaña abierta.
"""

from .context import RunContext
from .executor import (
    MAX_DEPTH,
    MAX_VISITS,
    LogEntry,
    NodeTrace,
    RunResult,
    execute_flow,
)
from .parser import (
    RESERVED_CONDITIONS,
    ActionNode,
    DecisionNode,
    Diagnostic,
    FlowEdge,
    FlowGraph,
    FlowMeta,
    FlowNode,
    Severity,
    StartNode,
    UnknownNode,
    parse_flow,
    parse_meta,
)

__all__ = [
    "MAX_DEPTH",
    "MAX_VISITS",
    "RESERVED_CONDITIONS",
    "ActionNode",
    "DecisionNode",
    "Diagnostic",
    "FlowEdge",
    "FlowGraph",
    "FlowMeta",
    "FlowNode",
    "LogEntry",
    "NodeTrace",
    "RunContext",
    "RunResult",
    "Severity",
    "StartNode",
    "UnknownNode",
    "execute_flow",
    "parse_flow",
    "parse_meta",
]
