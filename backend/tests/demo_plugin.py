"""
Plugin de prueba — la única implementación del contrato que vive en el repo.

No es un plugin de producción y no pretende serlo: existe para que la suite
pueda probar el camino completo (manifest → ports → settings → colecciones →
acciones → ejecución) sin depender de ninguna integración real.

También es la referencia ejecutable de cómo se escribe un plugin bajo esta
arquitectura. Un plugin nuevo se parece a esto:

- declara sus `ports` en el manifest y **nunca** importa una librería,
- pide el adapter con `ctx.port(...)`, que ya viene resuelto,
- devuelve siempre un `ToolResult`,
- no sabe dónde se guardan sus colecciones: las lee con `ctx.resource(...)`.
"""

from __future__ import annotations

from backend.core import ports as port_names
from backend.core.contract import (
    Action,
    Field,
    FunctionAction,
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    Resource,
    Setting,
    ToolContext,
    ToolManifest,
    ToolResult,
)

DESTINOS = Resource(
    name="destinos",
    label="Destinos",
    item_label="Destino",
    key_field="name",
    doc="Rutas con nombre a las que un flujo puede archivar.",
    fields=(
        Field("ruta", ParamType.PATH, label="Ruta", required=True),
        Field("token", ParamType.STR, label="Token", secret=True),
    ),
)

MANIFEST = PluginManifest(
    name="demo",
    label="Demo",
    version="1.0.0",
    doc="Plugin de prueba. Ejercita los cuatro ports.",
    ports=(port_names.HTTP, port_names.FS, port_names.PROCESS, port_names.CLOCK),
    settings=(
        Setting("demoBase", ParamType.PATH, label="Carpeta base", default="/casos"),
        Setting("demoTimeout", ParamType.FLOAT, label="Timeout", default=10.0),
    ),
    resources=(DESTINOS,),
    actions=(
        Action(
            "ping",
            "Probar conexión",
            params=(Param("url", required=True),),
            doc="Pega un GET y reporta el status.",
        ),
        Action(
            "probar_destino",
            "Probar destino",
            params=(Param("ruta"), Param("token")),
            doc="Eco de los params resueltos. Ejercita run_action(item=...).",
            resource="destinos",
        ),
    ),
)


# ── demo.fetch — usa HttpPort ───────────────────────────────────────────

FETCH = ToolManifest(
    id="demo.fetch",
    label="traer",
    category="DEMO",
    doc="GET a una URL. Deja el cuerpo parseado en {response}.",
    params=(Param("url", required=True),),
    outputs=(Output("response", ParamType.JSON), Output("status", ParamType.INT)),
    extra_outputs=True,
    extra_outputs_doc="Los campos del objeto devuelto se mergean al contexto.",
)


def _fetch(ctx: ToolContext) -> ToolResult:
    respuesta = ctx.port(port_names.HTTP).request(
        ctx.params["url"], timeout=float(ctx.config("demoTimeout") or 10.0)
    )
    cuerpo = respuesta.json(default={})
    ctx.log(f"GET {ctx.params['url']} → {respuesta.status}")

    if not respuesta.ok:
        return ToolResult.err(
            f"la API respondió {respuesta.status}",
            status=respuesta.status,
            response=cuerpo,
        )

    extras = cuerpo if isinstance(cuerpo, dict) else {}
    return ToolResult.ok(response=cuerpo, status=respuesta.status, **extras)


# ── demo.buscar — usa FsPort ────────────────────────────────────────────

BUSCAR = ToolManifest(
    id="demo.buscar",
    label="buscar carpeta",
    category="DEMO",
    doc="Busca la carpeta del caso bajo la carpeta base configurada.",
    params=(Param("etiqueta", doc="Sólo informativo, para la traza."),),
    outputs=(Output("carpeta", ParamType.PATH), Output("archivos", ParamType.JSON)),
)


def _buscar(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    base = str(ctx.config("demoBase") or "/casos")
    carpeta = f"{base}/{ctx.case_id}"

    if not fs.is_dir(carpeta):
        return ToolResult.err(f"no existe la carpeta del caso: {carpeta}")

    archivos = [e.name for e in fs.list_dir(carpeta) if not e.is_dir]
    ctx.log(f"{carpeta}: {len(archivos)} archivo/s")
    return ToolResult.ok(carpeta=carpeta, archivos=archivos)


# ── demo.mover — usa FsPort, y es destructivo ───────────────────────────

MOVER = ToolManifest(
    id="demo.mover",
    label="mover carpeta",
    category="DEMO",
    doc="Mueve una carpeta. Acepta el nombre de un destino guardado.",
    dangerous=True,
    params=(
        Param("origen", ParamType.PATH, required=True),
        Param("destino", ParamType.PATH, required=True),
    ),
    outputs=(Output("destino", ParamType.PATH),),
)


def _mover(ctx: ToolContext) -> ToolResult:
    destino = ctx.params["destino"]
    # Si coincide con un destino guardado, se usa su ruta. El plugin no sabe
    # dónde está guardada la colección: la pide por nombre.
    guardado = ctx.resource("destinos", destino)
    if guardado is not None:
        destino = guardado["ruta"]

    final = ctx.port(port_names.FS).move(ctx.params["origen"], destino)
    ctx.log(f"movido a {final}")
    return ToolResult.ok(destino=final)


# ── demo.correr — usa ProcessPort ───────────────────────────────────────

CORRER = ToolManifest(
    id="demo.correr",
    label="correr comando",
    category="DEMO",
    dangerous=True,
    # Referencia de un tool sensible al timing (ej. Toothform, por escritorio):
    # mientras corre, ningún otro run puede estar en vuelo.
    concurrency="exclusive_run",
    params=(
        Param("ejecutable", required=True),
        Param("args", doc="Argumentos separados por espacio."),
    ),
    outputs=(Output("exit_code", ParamType.INT), Output("stdout", ParamType.STR)),
)


def _correr(ctx: ToolContext) -> ToolResult:
    argv = [ctx.params["ejecutable"], *(ctx.params.get("args") or "").split()]
    resultado = ctx.port(port_names.PROCESS).run(argv)

    salidas = {"exit_code": resultado.exit_code, "stdout": resultado.stdout}
    if resultado.timed_out:
        return ToolResult.err("el comando no terminó a tiempo", **salidas)
    if not resultado.ok:
        return ToolResult.err(f"salió con código {resultado.exit_code}", **salidas)
    return ToolResult.ok(**salidas)


# ── demo.chequear — devuelve loop, para ejercer retry_gate ──────────────

CHEQUEAR = ToolManifest(
    id="demo.chequear",
    label="chequear",
    category="DEMO",
    doc="Devuelve loop hasta que el archivo señal exista.",
    params=(Param("senal", ParamType.PATH, default="/tmp/listo"),),
)


def _chequear(ctx: ToolContext) -> ToolResult:
    if ctx.port(port_names.FS).exists(ctx.params.get("senal") or "/tmp/listo"):
        return ToolResult.ok("terminó")
    return ToolResult.again("todavía no")


# ── Acción: probar conexión ─────────────────────────────────────────────


def _ping(ctx: ToolContext) -> ToolResult:
    respuesta = ctx.port(port_names.HTTP).request(ctx.params["url"])
    if not respuesta.ok:
        return ToolResult.err(f"respondió {respuesta.status}")
    return ToolResult.ok(f"respondió {respuesta.status}")


def _probar_destino(ctx: ToolContext) -> ToolResult:
    return ToolResult.ok(**ctx.params)


_TOOLS = (
    (FETCH, _fetch),
    (BUSCAR, _buscar),
    (MOVER, _mover),
    (CORRER, _correr),
    (CHEQUEAR, _chequear),
)


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[FunctionTool(manifest=m, fn=f) for m, f in _TOOLS],
        actions=[
            FunctionAction(action=MANIFEST.action("ping"), fn=_ping),
            FunctionAction(action=MANIFEST.action("probar_destino"), fn=_probar_destino),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
