"""
El servidor MCP: sólo cableado de protocolo.

Toda la lógica vive en `operations.py`, que no sabe que MCP existe. Acá se
declaran los esquemas de entrada y se traduce el resultado a `CallToolResult`.
Si algo de este archivo empezara a decidir cosas, habría una segunda
implementación de la fachada esperando divergir — el modo de falla contra el
que está diseñado todo el repo.

Qué se expone, y qué no
-----------------------

Diez tools fijas. **La lista no crece cuando se instala un plugin**, y eso es
deliberado:

- Un `Tool` de un plugin es un *nodo de un flujo*. Fuera de un run no tiene
  contexto, ni traza, ni log de fila: la mitad de su manifest deja de
  significar algo. Se expone como **dato** en el catálogo, no como algo
  invocable.
- Una `Action` sí está diseñada para dispararse suelta, así que hay **una**
  tool parametrizada (`run_action`) en vez de una por acción.
- La lista de tools de MCP se negocia al conectar: proyectar plugins la
  volvería inestable a mitad de sesión.

Es la misma frontera que el contrato ya dibuja entre `Tool` y `Action`.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import operations

# Esquema compartido: cómo se pasan plugins locales a casi toda operación.
_PLUGINS = {
    "type": "object",
    "description": (
        "Plugins a cargar desde disco sin instalarlos, como {nombre: ruta}. La "
        "ruta puede ser un .py o una carpeta de paquete. Es lo que permite "
        "validar un plugin recién escrito."
    ),
    "additionalProperties": {"type": "string"},
}

_ROOT = {
    "type": "string",
    "description": "Directorio de la instalación. Por defecto, backend/.",
}


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None):
    return types.Tool(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    )


TOOLS: list[types.Tool] = [
    _tool(
        "list_tools",
        "El catálogo de tools disponibles, con sus params tipados y sus outputs. "
        "Mirar esto ANTES de escribir un flujo o proponer un plugin nuevo: dice "
        "qué existe ya.",
        {"plugins": _PLUGINS, "root": _ROOT},
    ),
    _tool(
        "list_plugins",
        "Los plugins cargados con sus settings, colecciones, acciones y los ports "
        "que usa cada uno.",
        {"plugins": _PLUGINS, "root": _ROOT},
    ),
    _tool(
        "list_resource_items",
        "Los items guardados de una colección de un plugin ('sources'): qué "
        "conexiones/fuentes ya existen, para poder pedir una por nombre con "
        "run_action(item=...). Los campos declarados 'secret' vuelven en None, "
        "nunca en claro. No ejecuta nada.",
        {
            "plugin": {"type": "string"},
            "resource": {
                "type": "string",
                "description": "Nombre de la colección (Resource.name del plugin).",
            },
            "plugins": _PLUGINS,
            "root": _ROOT,
        },
        ["plugin", "resource"],
    ),
    _tool(
        "list_ports",
        "Qué ports puede declarar un plugin y qué ofrece cada uno. Un plugin "
        "nunca importa una librería: pide un port.",
        {},
    ),
    _tool(
        "check_flow",
        "Parsea un .mmd y lo cruza con los tools disponibles, sin ejecutar nada. "
        "Devuelve diagnósticos con severidad, línea y nodo.",
        {
            "flow": {
                "type": "string",
                "description": "Ruta a un .mmd, o el nombre de un flujo guardado.",
            },
            "plugins": _PLUGINS,
            "root": _ROOT,
        },
        ["flow"],
    ),
    _tool(
        "dry_run_flow",
        "Recorre el flujo entero con un row de prueba SIN ejecutar un solo tool. "
        "Resuelve las variables, sigue las ramas y devuelve la traza con los "
        "valores a los que se habría resuelto cada param. No toca red, disco ni "
        "procesos.",
        {
            "flow": {"type": "string", "description": "Ruta a un .mmd, o un flujo guardado."},
            "row": {
                "type": "object",
                "description": "La fila de entrada, p. ej. {\"id\": \"0044\"}.",
                "additionalProperties": True,
            },
            "case_id": {"type": "string"},
            "plugins": _PLUGINS,
            "root": _ROOT,
        },
        ["flow"],
    ),
    _tool(
        "load_plugin",
        "Carga un plugin desde disco y reporta si el núcleo lo acepta. Contesta "
        "sin ejecutar una línea del plugin: detecta ports inexistentes o sin "
        "adapter, ids duplicados o sin namespace, contrato no soportado y "
        "acciones sin declarar. Es el paso de verificación al escribir un plugin.",
        {
            "name": {"type": "string", "description": "Nombre con el que registrarlo."},
            "path": {"type": "string", "description": "Ruta al .py o a la carpeta."},
            "root": _ROOT,
        },
        ["name", "path"],
    ),
    _tool(
        "install_plugin",
        "Instala de verdad un plugin que load_plugin ya aceptó: lo copia a "
        "plugins_dir/<name>/ y anota procedencia al lado. Es el paso que faltaba "
        "para dejar un plugin funcionando en el próximo arranque, no sólo "
        "probado en este proceso.",
        {
            "name": {"type": "string", "description": "Nombre con el que registrarlo."},
            "path": {"type": "string", "description": "Ruta al .py o a la carpeta, ya validada con load_plugin."},
            "plugins_dir": {
                "type": "string",
                "description": "Carpeta de plugins de la instalación destino.",
            },
            "source": {
                "type": "string",
                "description": "De dónde salió: 'agent', 'bucket', lo que corresponda — sólo metadata, no cambia la validación.",
            },
            "root": _ROOT,
        },
        ["name", "path", "plugins_dir", "source"],
    ),
    _tool(
        "save_flow",
        "Persiste un .mmd en la instalación, sin ejecutar nada. Es el paso que "
        "sigue a check_flow/dry_run_flow cuando el flujo ya está probado y se "
        "lo quiere dejar guardado de verdad, sin la ejecución real ni el actor "
        "con permisos que pide run_flow(save=true).",
        {
            "flow": {"type": "string", "description": "Ruta a un .mmd."},
            "name": {
                "type": "string",
                "description": "Nombre con el que guardarlo. Por defecto, el del archivo.",
            },
            "root": _ROOT,
        },
        ["flow"],
    ),
    _tool(
        "run_action",
        "Ejecuta una acción declarada por un plugin ('probar conexión' y "
        "similares). Es la única ejecución REAL de este servidor. Para ejercitar "
        "un tool, escribir un .mmd de un nodo y usar dry_run_flow.",
        {
            "plugin": {"type": "string"},
            "action": {"type": "string"},
            "params": {"type": "object", "additionalProperties": True},
            "item": {
                "type": "string",
                "description": (
                    "Clave de un item ya guardado (ver list_resource_items): "
                    "resuelve los params desde ahí en vez de reconstruirlos a "
                    "mano. `params` explícitos pisan lo que traiga el item."
                ),
            },
            "plugins": _PLUGINS,
            "root": _ROOT,
        },
        ["plugin", "action"],
    ),
    _tool(
        "run_flow",
        "Ejecuta un flujo DE VERDAD: toca red, disco y procesos. `root` es "
        "obligatorio —hay que nombrar la instalación donde se escribe, para no "
        "mezclar pruebas con datos de producción—. El actor determina qué se "
        "puede: por defecto uno de tipo `agent`, que nace sin permiso para "
        "tools peligrosos ni para el port `process`. Un flujo vedado falla "
        "ANTES de ejecutar nada. Para validar sin efectos, usar dry_run_flow.",
        {
            "flow": {"type": "string", "description": "Ruta a un .mmd, o un flujo guardado."},
            "root": {
                "type": "string",
                "description": (
                    "Directorio de la instalación donde se ejecuta y se guarda "
                    "la traza. Sin default: usar uno temporal."
                ),
            },
            "row": {"type": "object", "additionalProperties": True},
            "case_id": {"type": "string"},
            "actor": {
                "type": "string",
                "description": (
                    "Actor registrado. Por defecto 'agente-mcp', que hay que dar "
                    "de alta una vez por instalación: no se autocrea."
                ),
            },
            "plugins": _PLUGINS,
            "save": {
                "type": "boolean",
                "description": "Persiste el flujo en la instalación. Por defecto no.",
            },
            "allow_broken": {
                "type": "boolean",
                "description": (
                    "Corre igual aunque el flujo tenga errores (un tool sin "
                    "instalar, por ejemplo). Por defecto no: sin esto, un flujo "
                    "roto falla ANTES de ejecutar nada en vez de terminar "
                    "'ok' con ese nodo saltado en silencio."
                ),
            },
        },
        ["flow", "root"],
    ),
    _tool(
        "list_users",
        "Los actores registrados y qué puede cada uno. Sirve para entender por "
        "qué un run_flow fue denegado y qué habría que pedirle a quien opera "
        "la instalación.",
        {"root": _ROOT},
    ),
    _tool(
        "plugin_template",
        "El esqueleto de un plugin nuevo: manifest, un tool y los ports ya "
        "declarados. Se genera desde el contrato, así que no queda desactualizado.",
        {
            "name": {"type": "string", "description": "Nombre del plugin, en minúsculas."},
            "ports": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["http", "fs", "process", "clock", "browser"],
                },
                "description": "Los ports que va a necesitar.",
            },
        },
        ["name"],
    ),
]

HANDLERS: dict[str, Callable[..., dict]] = {
    "list_tools": operations.list_tools,
    "list_plugins": operations.list_plugins,
    "list_resource_items": operations.list_resource_items,
    "list_ports": operations.list_ports,
    "check_flow": operations.check_flow,
    "dry_run_flow": operations.dry_run_flow,
    "load_plugin": operations.load_plugin,
    "install_plugin": operations.install_plugin,
    "save_flow": operations.save_flow,
    "run_action": operations.run_action,
    "run_flow": operations.run_flow,
    "list_users": operations.list_users,
    "plugin_template": operations.plugin_template,
}

INSTRUCCIONES = """\
Motor de workflows: se escriben flujos en Mermaid y el núcleo los ejecuta.

Bucle de trabajo:

  1. list_tools        qué tools existen ya
  2. escribir el .mmd
  3. check_flow        ¿parsea? ¿los params cierran contra los manifests?
  4. falta un tool     -> plugin_template, escribir el plugin, load_plugin
  5. check_flow        de nuevo, ahora con plugins={nombre: ruta}
  6. dry_run_flow      recorrido completo con un row, sin tocar el mundo
  7. save_flow         deja el .mmd guardado en la instalación, sin ejecutarlo
  8. install_plugin    si hizo falta un plugin nuevo, deja instalado el que load_plugin aceptó
  9. run_flow          ejecución real, sólo si hace falta y con root propio

Guardar sin ejecutar
---------------------

`save_flow` y `install_plugin` son el punto de llegada de la autoría: dejan un
flujo o un plugin instalados de verdad para el próximo arranque, **sin**
ejecutar un solo tool ni pedir el `root`/actor con permisos que exige
`run_flow`. Antes de estos dos, la única forma de persistir algo era ejecutarlo
de verdad contra una instalación — ya no hace falta.

Ejecución real
--------------

`dry_run_flow` no toca nada y alcanza para casi todo. `run_flow` sí ejecuta, y
por eso pide dos cosas:

  - `root` obligatorio: la instalación donde escribe. Usar un directorio
    temporal, nunca la de producción.
  - un actor con permisos. El default `agente-mcp` es de tipo `agent`: no puede
    ejecutar tools marcados `dangerous` ni plugins que declaren el port
    `process`. No se autocrea —hay que darlo de alta una vez por instalación,
    `python -m backend.core users add agente-mcp --kind agent`— y si hace
    falta más, lo habilita quien opera la instalación con `users allow`; no se
    pide desde acá.

Un flujo vedado falla antes de ejecutar nada y dice qué nodo y qué regla lo
impidió. `list_users` muestra qué puede cada actor.

Sintaxis de un flujo:

    %% folder: CARPETA
    %% description: qué hace
    flowchart TD
        SN(inicio)
        N1["etiqueta § tool.id | param=valor, otro={variable}"]
        D1{¿Pregunta? § campo_del_row}
        SN --> N1
        N1 -->|ok| OTRO
        N1 -->|err| RAMA_DE_ERROR
        D1 -->|valor1, valor2| N1

  - `§` separa el nombre visible del tool. `|` separa los params.
  - Las aristas se resuelven por prioridad: |loop| -> |ok|/|err| -> incondicional.
  - `{variables}` sale del row del caso o de outputs de nodos anteriores.
    `{env.CLAVE}` para secretos; `{ruta.parent}` para la carpeta contenedora.
  - Un nodo que falla sin arista |err| corta el flujo.

Esperar a que algo termine — el patrón más frecuente. Un tool devuelve
`ToolResult.again(...)` para pedir otra vuelta, y eso toma la arista |loop|;
`flow.retry_gate` lleva la cuenta y corta al llegar al tope:

        CHEQUEAR["¿ya terminó? § mi.chequear"]
        PUERTA["¿queda intento? § flow.retry_gate | retryGateKey=espera, retryGateMax=5"]
        ESPERAR["esperar § core.wait | seconds=60"]
        CHEQUEAR -->|ok| LISTO
        CHEQUEAR -->|loop| PUERTA
        PUERTA -->|ok| ESPERAR
        PUERTA -->|err| AGOTADO
        ESPERAR --> CHEQUEAR

El contador vive en el run, no en el tool: por eso `flow.retry_gate` lo resuelve
el motor y no está en el catálogo de tools.

Reglas de un plugin, que el núcleo hace cumplir:

  - Nunca importa una librería externa: declara ports y usa ctx.port(nombre).
  - Devuelve siempre ToolResult; una excepción se convierte en err con traceback.
  - No sabe dónde se guardan sus datos: los lee con ctx.resource(...).
  - No escribe HTML: declara Setting, Resource.fields y Action.

Este servidor es para AUTORÍA. No ejecuta flujos de verdad: dry_run_flow recorre
sin tocar nada, y run_action es la única ejecución real, acotada a lo que el
contrato define como disparable por una persona.\
"""


def call_tool(name: str, arguments: dict | None = None) -> types.CallToolResult:
    """
    Despacha una tool y envuelve el resultado. Síncrono y sin protocolo.

    Está separado del handler asíncrono a propósito: es donde vive la única
    decisión de este archivo —qué hacer cuando algo falla— y así se puede
    probar sin levantar un servidor ni hablar stdio.

    **Nada puede tumbar el servidor.** Todo fallo vuelve como resultado con
    `is_error`, no como excepción de protocolo: así quien llama lo lee y puede
    corregir, en vez de recibir un error de transporte que no dice nada.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        disponibles = ", ".join(sorted(HANDLERS))
        return _error(f"Tool desconocida: {name}. Disponibles: {disponibles}")

    try:
        resultado = handler(**(arguments or {}))
    except operations.OperationError as exc:
        return _error(str(exc))
    except TypeError as exc:
        # Argumentos que no encajan con la firma: es un error de quien llama, y
        # decirlo así es más útil que un traceback.
        return _error(f"argumentos inválidos para {name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - ni un fallo puede cortar la sesión
        return _error(f"{type(exc).__name__}: {exc}")

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_json(resultado))],
        structured_content=resultado,
    )


def build_server() -> Server:
    """El servidor, con sus handlers cableados. Separado de `main` para testear."""

    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOLS)

    async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        return call_tool(params.name, params.arguments)

    return Server(
        "bot-workflows",
        version="0.1.0",
        title="Motor de workflows",
        instructions=INSTRUCCIONES,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _error(mensaje: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=mensaje)],
        is_error=True,
    )


def _json(valor: Any) -> str:
    return json.dumps(valor, indent=2, ensure_ascii=False)


async def serve() -> None:
    servidor = build_server()
    async with stdio_server() as (lectura, escritura):
        await servidor.run(lectura, escritura, servidor.create_initialization_options())


def main() -> int:
    import anyio

    anyio.run(serve)
    return 0


__all__ = ["HANDLERS", "TOOLS", "build_server", "call_tool", "main", "serve"]
