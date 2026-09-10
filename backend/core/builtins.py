"""
Tools nativos del núcleo — namespace `core.*`

Son primitivas del motor, no integraciones: loguear, esperar, forzar un status.
No dependen de ningún sistema externo, así que viven en el núcleo y no en un
plugin. El test es el del documento de arquitectura: si al sacar el núcleo dejan
de existir, son del núcleo.

Se llamaban `webapp.*`, por una UI que ya no existe. El nombre nuevo dice lo que
son: primitivas del núcleo.

`flow.ejecutar` y `flow.retry_gate` NO están acá: los resuelve el executor,
porque necesitan estado del run (recursión y contadores) que el contrato
deliberadamente no le expone a un tool.

El núcleo se registra como un plugin más —mismo manifest, mismo contrato, mismos
ports declarados—. No es cosmético: es lo que garantiza que el camino del plugin
esté siempre ejercitado, porque el propio núcleo lo transita.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from . import ports as port_names
from .contract import (
    FunctionTool,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    Setting,
    ToolContext,
    ToolManifest,
    ToolResult,
)


def _version_del_nucleo() -> str:
    """
    La versión instalada de `bot-core`, para que este manifest no declare una
    constante que se desincroniza del tag apenas se corta un release (issue
    #5: `pyproject.toml` decía 0.1.0 con tags ya en v0.2.1-beta1, y acá había
    otra constante más, "0.2.0", sin relación con ninguna de las dos).

    `importlib.metadata` lee el `.dist-info` que deja `pip install` —de un
    release bajado o de un editable install—, la misma fuente que
    `pip show bot-core`. Un `backend/` copiado a mano, sin pasar por pip, no
    tiene de dónde sacarla: por eso el fallback explícito en vez de fingir un
    número.
    """
    try:
        return version("bot-core")
    except PackageNotFoundError:
        return "0.0.0+sin-instalar"


LOG = ToolManifest(
    id="core.log",
    label="registrar log",
    category="NÚCLEO",
    doc="Escribe un mensaje en la traza del run y en el registro de la fila.",
    params=(
        Param("message", required=True, doc="Admite {variables}"),
        Param(
            "level",
            ParamType.ENUM,
            choices=("info", "warning", "error"),
            default="info",
        ),
    ),
)


def _log(ctx: ToolContext) -> ToolResult:
    ctx.log(ctx.params["message"], ctx.params.get("level") or "info")
    # Loguear un error no hace fallar al nodo: para cortar el flujo está
    # core.set_status. Si loguear en nivel error cortara, no habría forma de
    # dejar constancia de algo malo y seguir igual, que es un caso legítimo.
    return ToolResult.ok()


SET_STATUS = ToolManifest(
    id="core.set_status",
    label="setear status",
    category="NÚCLEO",
    doc=(
        "Fuerza el resultado del nodo. Sólo ok y err dirigen aristas; cualquier "
        "otro valor se trata como ok."
    ),
    params=(
        Param(
            "status",
            ParamType.ENUM,
            required=True,
            choices=("ok", "err"),
        ),
    ),
)


def _set_status(ctx: ToolContext) -> ToolResult:
    if str(ctx.params["status"]).strip().lower() == "err":
        return ToolResult.err("status forzado a err")
    return ToolResult.ok("status forzado a ok")


WAIT = ToolManifest(
    id="core.wait",
    label="esperar",
    category="NÚCLEO",
    doc="Pausa el flujo. Se interrumpe si se cancela el run.",
    params=(Param("seconds", ParamType.FLOAT, default=60.0),),
)


def _wait(ctx: ToolContext) -> ToolResult:
    segundos = float(ctx.params.get("seconds") or 0.0)
    if segundos <= 0:
        return ToolResult.ok()

    ctx.log(f"Esperando {segundos:g}s")
    # El reloj entra por el port: es lo que hace que un flujo con reintentos de
    # 60s se pueda testear en milisegundos en vez de dormir de verdad.
    ctx.port(port_names.CLOCK).sleep(segundos, ctx.is_cancelled_check)

    if ctx.cancelled:
        return ToolResult.err("espera interrumpida por cancelación")
    return ToolResult.ok()


# El núcleo declara settings como cualquier plugin, así que su pantalla se
# dibuja sola desde el mismo esquema. Es lo que evita escribir a mano una
# sección de configuración por cada cosa que el núcleo necesite saber.
MANIFEST = PluginManifest(
    name="core",
    label="Núcleo",
    version=_version_del_nucleo(),
    doc="Primitivas del motor de flujos. No se puede desinstalar.",
    ports=(port_names.CLOCK,),
    settings=(
        Setting(
            "logRetentionMode",
            ParamType.ENUM,
            label="Cuándo se limpia el registro",
            default="manual",
            choices=("manual", "expiration", "orphan"),
            group="Registro de eventos",
            doc=(
                "manual: sólo cuando alguien lo pide. "
                "expiration: además borra lo más viejo que los días de abajo. "
                "orphan: además borra el registro de una fila cuando esa fila ya "
                "no aparece en su origen."
            ),
        ),
        Setting(
            "logRetentionDays",
            ParamType.FLOAT,
            label="Días que se guarda",
            default=30.0,
            group="Registro de eventos",
            doc="Sólo se aplica con el modo expiration. 0 = sin límite de días.",
        ),
        Setting(
            "logMaxPerCase",
            ParamType.INT,
            label="Líneas por fila, como máximo",
            default=500,
            group="Registro de eventos",
            doc=(
                "Techo duro, en cualquier modo: un flujo con reintentos largos "
                "puede escribir cientos de líneas de una, y sin tope una sola "
                "fila se come la base. 0 = sin tope."
            ),
        ),
    ),
)

_DEFINITIONS = (
    (LOG, _log),
    (SET_STATUS, _set_status),
    (WAIT, _wait),
)


def build_builtin_plugin() -> Plugin:
    """Los tools nativos, con la misma forma que cualquier plugin externo."""
    return Plugin(
        manifest=MANIFEST,
        tools=[FunctionTool(manifest=m, fn=f) for m, f in _DEFINITIONS],
    )


__all__ = ["MANIFEST", "build_builtin_plugin"]
