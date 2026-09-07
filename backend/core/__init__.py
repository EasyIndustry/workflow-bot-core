"""
Núcleo — motor de flujos, trazas y registro de plugins.

El núcleo hace exactamente dos cosas: **ejecutar workflows** (parsear Mermaid,
resolver variables, decidir la arista siguiente) y **dejar registro** de lo que
pasó (trazas por run, log por fila). Nada más.

Todo lo demás —cualquier integración, cualquier librería externa, cualquier
lógica de un caso de uso— es un plugin o un adapter. La regla en una línea:
**el núcleo actúa sobre el run; un plugin actúa sobre el mundo.**

Las capas, y qué conoce cada una:

    core     el run. Conoce los ports, nunca una librería ni un plugin.
    ports    nada: son sólo la forma (Protocol).
    adapters una librería externa, y sólo ella.
    plugins  los ports que necesitan, nunca una librería directamente.

Las llamadas van **Core → Plugin → Adapter**, siempre. Ninguna capa de abajo
inicia una acción por su cuenta, y las respuestas vuelven como valores de
retorno.

Mantener el núcleo chico es lo que hace que el mantenimiento se concentre acá y
que un plugin roto no se lleve puesto el motor.
"""

from .contract import (
    CONTRACT_VERSION,
    STATUS_ERR,
    STATUS_OK,
    Action,
    ActionHandler,
    Field,
    FunctionAction,
    FunctionTool,
    MissingSetting,
    Output,
    Param,
    ParamError,
    ParamType,
    Plugin,
    PluginManifest,
    PortNotDeclared,
    Resource,
    Setting,
    Tool,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from .ports import (
    CLOCK,
    FS,
    HTTP,
    PLUGIN_PORTS,
    PROCESS,
    STORAGE,
    ClockPort,
    FileInfo,
    FsPort,
    HttpPort,
    HttpResponse,
    PortError,
    ProcessPort,
    ProcessResult,
    StoragePort,
)
from .registry import ToolRegistry, build_default_registry
from .resources import ResourceError, TableStore, store_for

__all__ = [
    "CLOCK",
    "CONTRACT_VERSION",
    "FS",
    "HTTP",
    "PLUGIN_PORTS",
    "PROCESS",
    "STATUS_ERR",
    "STATUS_OK",
    "STORAGE",
    "Action",
    "ActionHandler",
    "ClockPort",
    "Field",
    "FileInfo",
    "FsPort",
    "FunctionAction",
    "FunctionTool",
    "HttpPort",
    "HttpResponse",
    "MissingSetting",
    "Output",
    "Param",
    "ParamError",
    "ParamType",
    "Plugin",
    "PluginManifest",
    "PortError",
    "PortNotDeclared",
    "ProcessPort",
    "ProcessResult",
    "Resource",
    "ResourceError",
    "Setting",
    "StoragePort",
    "TableStore",
    "Tool",
    "ToolContext",
    "ToolManifest",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "store_for",
]
