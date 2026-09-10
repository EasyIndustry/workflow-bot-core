"""
Las operaciones que el servidor MCP expone, sin nada de MCP.

Funciones puras de `params -> dict`. No importan el SDK, no conocen el
protocolo, y se pueden testear sin levantar un servidor. `server.py` sólo las
cablea.

Por qué cada operación corre en un subproceso
---------------------------------------------

Podrían llamar a `Instance` en el mismo proceso. Sería más rápido y está mal,
por una razón concreta: `registry.register_local` usa `importlib.import_module`,
que pega contra `sys.modules`.

En un bucle de autoría eso es fatal. Se escribe `mi_plugin.py`, se carga, se ve
el error, **se corrige y se vuelve a cargar** — y Python devuelve el módulo
viejo, cacheado. Se estaría iterando contra una versión que ya no existe en
disco, viendo errores fantasma de código ya arreglado.

`importlib.reload` es un parche que se rompe en cuanto el plugin tiene varios
módulos o deja estado global. La solución robusta es un proceso nuevo por
intento, y la CLI ya la da: cada operación es un `subprocess` limpio.

Efecto secundario que también importa: un plugin recién generado que se cuelga
en un bucle al importarse, o que llama a `sys.exit()`, se lleva puesto un
subproceso descartable en vez del servidor.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

# Tope de espera por comando. Un plugin generado puede colgarse al importarse;
# sin timeout, el servidor MCP se queda esperando para siempre y desde afuera
# se ve como que el agente dejó de responder.
TIMEOUT = 120.0


class OperationError(Exception):
    """La operación no se pudo llevar a cabo. Distinto de "dio un resultado malo"."""


def _cli(
    *argv: str,
    root: str | None = None,
    plugins: dict | None = None,
    actor: str | None = None,
) -> dict:
    """
    Corre la CLI en un subproceso y devuelve su JSON.

    El código de salida **no** se trata como error: la CLI usa 1 para "el flujo
    tiene errores", que es información legítima y esperada en este bucle. Sólo
    un fallo real —no se pudo ejecutar, no devolvió JSON— levanta.
    """
    comando = [sys.executable, "-m", "backend.core"]
    if root:
        comando += ["--root", root]
    if actor:
        comando += ["--actor", actor]
    for nombre, ruta in (plugins or {}).items():
        comando += ["--plugin", f"{nombre}={ruta}"]
    comando += list(argv)

    try:
        completado = subprocess.run(
            comando,
            cwd=str(RAIZ),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OperationError(
            f"el comando no terminó en {TIMEOUT:g}s. Si acabás de escribir un "
            f"plugin, revisá que no haga trabajo pesado al importarse."
        ) from exc
    except OSError as exc:
        raise OperationError(f"no se pudo ejecutar la CLI: {exc}") from exc

    salida = completado.stdout.strip()
    if not salida:
        detalle = completado.stderr.strip() or "sin salida"
        raise OperationError(f"la CLI no devolvió nada (código {completado.returncode}): {detalle}")

    try:
        datos = json.loads(salida)
    except json.JSONDecodeError as exc:
        raise OperationError(
            f"la CLI no devolvió JSON (código {completado.returncode}): {salida[:400]}"
        ) from exc

    # Algunos comandos devuelven una lista (`users --json`), así que el aviso
    # sólo se pega cuando hay dónde.
    if completado.stderr.strip() and isinstance(datos, dict):
        datos["_stderr"] = completado.stderr.strip()
    return datos


# ── Descubrimiento ──────────────────────────────────────────────────────


def list_tools(plugins: dict | None = None, root: str | None = None) -> dict:
    """
    El catálogo entero: tools con params tipados, outputs, y qué ports usa cada
    plugin.

    Es lo primero que conviene mirar antes de escribir un flujo o proponer un
    plugin nuevo: dice qué existe ya. Sin esto se inventan nombres de tools que
    no están.
    """
    catalogo = _cli("tools", "--json", root=root, plugins=plugins)
    return {
        "contract": catalogo["contract"],
        "ports_disponibles": catalogo["ports"],
        "tools": catalogo["tools"],
        "nativos": [
            {
                "id": "flow.ejecutar",
                "doc": "Ejecuta otro flujo por nombre. Comparte contexto y traza.",
                "params": ["flowName"],
            },
            {
                "id": "flow.retry_gate",
                "doc": "Contador de reintentos por run. Combina con una arista |loop|.",
                "params": ["retryGateKey", "retryGateMax"],
            },
        ],
    }


def list_plugins(plugins: dict | None = None, root: str | None = None) -> dict:
    """Los plugins cargados con sus settings, colecciones, acciones y ports."""
    catalogo = _cli("plugins", "--json", root=root, plugins=plugins)
    return {
        "ports_disponibles": catalogo["ports"],
        "plugins": catalogo["plugins"],
        "errors": catalogo["errors"],
    }


def list_resource_items(
    plugin: str, resource: str, plugins: dict | None = None, root: str | None = None
) -> dict:
    """
    Los items guardados de una colección de un plugin ("sources"), con los
    campos `secret` tapados.

    Es el paso que faltaba antes de `run_action`: sin esto, un agente no tenía
    forma de saber qué conexiones/fuentes ya existen para poder pedir una por
    nombre. No ejecuta nada, y nunca devuelve un secreto en claro.
    """
    return _cli("resources", plugin, resource, "--json", root=root, plugins=plugins)


def list_ports(**_) -> dict:
    """
    Qué puede pedir un plugin en su manifest, y qué le da cada port.

    Se responde desde el contrato y no desde una lista escrita a mano: si mañana
    aparece un port nuevo, esto lo refleja solo.
    """
    from backend.core import ports as p

    docs = {
        p.HTTP: "request(url, method, headers, body, timeout) -> HttpResponse",
        p.FS: "exists, is_dir, stat, list_dir, walk, make_dirs, move, copy_file, "
              "copy_tree, remove_file, remove_tree, rename, read_text, write_text",
        p.PROCESS: "run(command: Sequence[str], cwd, timeout, env) -> ProcessResult",
        p.CLOCK: "now(), monotonic(), sleep(seconds, is_cancelled)",
        p.BROWSER: "goto(url), click(selector), leer_texto(selector), screenshot() -> bytes",
        p.WINDOW: "find_window(title|process) -> WindowInfo, click(window, control), "
                  "type_text(window, control, text), read_text(window, control=None)",
    }
    return {
        "pedibles_por_un_plugin": [
            {"name": nombre, "superficie": docs.get(nombre, "")}
            for nombre in sorted(p.PLUGIN_PORTS)
        ],
        "reservados_al_nucleo": sorted(set(p.PORTS) - p.PLUGIN_PORTS),
        "nota": (
            "Un plugin declara los ports que necesita en PluginManifest.ports y "
            "los recibe con ctx.port(nombre). Nunca importa una librería ni "
            "instancia un adapter. Pedir un port no declarado levanta "
            "PortNotDeclared; declarar uno sin adapter impide que el plugin cargue."
        ),
    }


# ── Validación ──────────────────────────────────────────────────────────


def check_flow(flow: str, plugins: dict | None = None, root: str | None = None) -> dict:
    """
    Parsea un `.mmd` y lo cruza con los tools disponibles, sin ejecutar nada.

    `flow` es una ruta a un archivo o el nombre de un flujo guardado. Devuelve
    diagnósticos con severidad, línea y nodo: lo que hace falta para corregir.
    """
    return _cli("check", flow, "--json", root=root, plugins=plugins)


def dry_run_flow(
    flow: str,
    row: dict | None = None,
    case_id: str | None = None,
    plugins: dict | None = None,
    root: str | None = None,
) -> dict:
    """
    Recorre el flujo entero con un row de prueba **sin ejecutar un solo tool**.

    Resuelve todas las variables, sigue las ramas, y deja la traza con los
    valores a los que *se habría* resuelto cada param. Es la forma de verificar
    un flujo contra datos reales sin tocar el mundo: está probado que ningún
    adapter registra una llamada durante un dry-run.

    Las variables que no se pudieron resolver se reportan como avisos — suelen
    ser el error real, no un detalle.
    """
    argv = ["run", flow, "--dry-run", "--json", "--no-persist"]
    if row:
        argv += ["--row", json.dumps(row, ensure_ascii=False)]
    if case_id:
        argv += ["--case", case_id]

    resultado = _cli(*argv, root=root, plugins=plugins)
    return _resumen_de_run(resultado)


def run_flow(
    flow: str,
    root: str,
    row: dict | None = None,
    case_id: str | None = None,
    actor: str = "agente-mcp",
    plugins: dict | None = None,
    save: bool = False,
    allow_broken: bool = False,
) -> dict:
    """
    Ejecuta un flujo **de verdad**: toca red, disco y procesos.

    Tres guardas que no son opcionales:

    **`root` es obligatorio, sin default.** Las demás operaciones caen en
    `backend/` si nadie dice nada, y en seco eso casi no molesta. Con ejecución
    real sí: runs de prueba, logs de prueba y flujos de prueba mezclados con los
    de producción, indistinguibles después. Acá hay que nombrar dónde se escribe.
    Usar un directorio temporal, o `:memory:` vía BOOTSTRAP_storage para no
    dejar rastro.

    **El actor decide qué se puede.** Por defecto `agente-mcp`, pensado como
    `kind=agent`: sin permiso para tools `dangerous` ni para el port `process`.
    No se autocrea —el actor no existe hasta que alguien lo da de alta a
    propósito— así que hay que registrarlo una vez por instalación:
    `python -m backend.core users add agente-mcp --kind agent`. Ampliar sus
    permisos es un alta explícita del lado de quien opera la instalación
    —`users allow`— y no algo que se pida desde acá.

    Un flujo que el actor no puede correr falla **antes** de ejecutar nada, y
    dice qué nodo y qué regla lo impidió. Lo mismo si el flujo llama a un tool
    que no está instalado: sin `allow_broken`, falla antes de tocar nada en
    vez de terminar `status: ok` con ese nodo silenciosamente saltado por la
    arista `|err|`.
    """
    if not str(root or "").strip():
        raise OperationError(
            "run_flow necesita `root`: es la instalación donde se va a escribir. "
            "No tiene default a propósito — un run real contra la instalación de "
            "producción mezcla pruebas con datos de verdad."
        )

    argv = ["run", flow, "--json"]
    if row:
        argv += ["--row", json.dumps(row, ensure_ascii=False)]
    if case_id:
        argv += ["--case", case_id]
    if save:
        argv.append("--save")
    if allow_broken:
        argv.append("--allow-broken")

    resultado = _cli(*argv, root=root, plugins=plugins, actor=actor)
    resumen = _resumen_de_run(resultado)
    resumen["actor"] = resultado.get("actor", actor)
    resumen["run_id"] = resultado.get("run_id", "")
    return resumen


def save_flow(flow: str, name: str | None = None, root: str | None = None) -> dict:
    """
    Persiste un `.mmd` en la instalación, sin ejecutar nada.

    Es el paso que sigue a `check_flow`/`dry_run_flow` cuando el flujo ya está
    probado y se lo quiere dejar guardado de verdad — el mismo momento del
    bucle de autoría que `install_plugin` cubre para un plugin. A diferencia
    de `run_flow(..., save=True)`, no toca red, disco ni procesos del lado de
    sus tools, ni pide un actor con permisos: sólo escribe la fila en la base
    de la instalación, tomando `folder`/`state`/`description` de la cabecera
    `%%` del archivo si las tiene.
    """
    argv = ["add", flow, "--json"]
    if name:
        argv += ["--name", name]
    return _cli(*argv, root=root)


def list_users(root: str | None = None) -> dict:
    """
    Los actores registrados y qué puede cada uno.

    Sirve para entender por qué un `run_flow` fue denegado, y para saber qué
    habría que pedirle a quien opera la instalación.
    """
    return {"users": _cli("users", "--json", root=root)}


def _resumen_de_run(resultado: dict) -> dict:
    return {
        "status": resultado["status"],
        "message": resultado["message"],
        "failed_node": resultado["failed_node"],
        "sin_resolver": sorted({
            entrada["message"].split("variables sin resolver: ", 1)[1]
            for entrada in resultado.get("logs", [])
            if "variables sin resolver: " in entrada["message"]
        }),
        "trace": [
            {
                "node_id": t["node_id"],
                "fn": t["fn"],
                "params": t["params"],
                "status": t["status"],
                "message": t["message"],
            }
            for t in resultado.get("trace", [])
        ],
        "logs": [e["message"] for e in resultado.get("logs", [])],
    }


def load_plugin(name: str, path: str, root: str | None = None) -> dict:
    """
    Carga un plugin desde disco y reporta si el núcleo lo acepta.

    Es el paso de verificación del bucle de autoría, y contesta **sin ejecutar
    una línea del plugin**: el registry valida al importar. Detecta que pida un
    port inexistente, que pida uno sin adapter, ids duplicados o sin namespace,
    una versión de contrato no soportada, una acción sin declarar en el
    manifest, y aliases en conflicto.

    Un plugin que falla no se registra a medias: no entra ni uno de sus tools.
    """
    catalogo = _cli("plugins", "--json", root=root, plugins={name: path})
    errores = [e for e in catalogo["errors"] if e["name"] == name]
    cargado = next((p for p in catalogo["plugins"] if p["name"] == name), None)

    if errores or cargado is None:
        return {
            "ok": False,
            "errors": [e["error"] for e in errores] or ["el plugin no se registró"],
        }

    return {
        "ok": True,
        "plugin": {
            "name": cargado["name"],
            "version": cargado["version"],
            "ports": cargado["ports"],
            "tools": cargado["tools"],
            "settings": [s["key"] for s in cargado["settings"]],
            "resources": [r["name"] for r in cargado["resources"]],
            "actions": [a["name"] for a in cargado["actions"]],
        },
        "errors": [],
    }


ARCHIVO_PROCEDENCIA = ".procedencia.json"


def install_plugin(
    name: str, path: str, plugins_dir: str, source: str, root: str | None = None
) -> dict:
    """
    Instala un plugin ya validado: lo copia a `plugins_dir/<name>/` y anota su
    procedencia (`source`, versión, cuándo) en un `.procedencia.json` al lado.

    Es el paso que sigue a `load_plugin` cuando se decide dejarlo instalado de
    verdad, no sólo probado. Reusa el mismo criterio de validación —la misma
    llamada a la CLI que ya hace `load_plugin`— así que lo que ahí se acepta,
    esto lo instala, y lo que se rechaza no llega a tocar disco.

    Un plugin de un solo archivo se copia como `__init__.py` adentro de
    `plugins_dir/<name>/`: es la única forma de paquete que el núcleo escanea
    ahí (`Instance._plugins_de_la_carpeta`), y así un plugin de archivo suelto
    y uno que ya es un paquete terminan en la misma forma instalada, con la
    misma procedencia al lado.

    La copia arma una carpeta temporal al lado y recién al final la renombra
    sobre el destino, que en el mismo filesystem es atómico: una instalación
    que se corta a mitad de copiar deja la versión anterior intacta, nunca una
    carpeta a medio escribir con la que el núcleo intentaría cargar en el
    próximo arranque.

    `source` es sólo metadata —de dónde salió esta carpeta ('agent', 'bucket',
    lo que corresponda—: no cambia la validación de arriba, que es la misma
    para cualquier valor.

    Dos guardas contra un `path`/`plugins_dir` mal armados (issue #2: un
    agente puede confundir el checkout de desarrollo con una instalación
    separada):

    - Si `origen` es igual a, contiene, o está contenido en `destino`, la
      instalación se rechaza antes de tocar nada: copiar y después reemplazar
      `destino` de por medio borraría archivos de `origen` que nunca se
      llegaron a copiar (por ejemplo, un `__init__.py` de re-export o una
      carpeta de fixtures al lado del archivo que sí se instala).
    - Un `destino` existente nunca se borra: se renombra a
      `<name>.reemplazado-<timestamp>` al lado, y ahí queda como red de
      seguridad recuperable a mano. No se limpia solo.
    """
    resultado = load_plugin(name, path, root=root)
    if not resultado["ok"]:
        return {"ok": False, "errors": resultado["errors"]}

    origen = Path(path).resolve()
    destino_base = Path(plugins_dir).resolve()
    destino_base.mkdir(parents=True, exist_ok=True)
    destino = destino_base / name
    temporal = destino_base / f"{name}.instalando"

    if origen == destino or origen in destino.parents or destino in origen.parents:
        raise OperationError(
            f'"{path}" y el destino de instalación "{destino}" se pisan: uno '
            "contiene al otro. Instalar ahí borraría archivos de origen que "
            "nunca se copian (todo lo que no sea el plugin instalable). Usar "
            "un `plugins_dir` que sea una instalación separada del checkout "
            "de desarrollo."
        )

    if temporal.exists():
        shutil.rmtree(temporal)
    if origen.is_dir():
        shutil.copytree(origen, temporal)
    else:
        temporal.mkdir()
        shutil.copy2(origen, temporal / "__init__.py")

    (temporal / ARCHIVO_PROCEDENCIA).write_text(
        json.dumps(
            {
                "name": name,
                "version": resultado["plugin"]["version"],
                "source": source,
                "installed_at": time.time(),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    backup = None
    if destino.exists():
        backup = destino_base / f"{name}.reemplazado-{time.time_ns()}"
        destino.rename(backup)
    temporal.rename(destino)

    return {
        "ok": True,
        "name": name,
        "version": resultado["plugin"]["version"],
        "ports": resultado["plugin"]["ports"],
        "path": str(destino),
        "source": source,
        "backup": str(backup) if backup else None,
        "errors": [],
    }


# ── Ejecución acotada ───────────────────────────────────────────────────


def run_action(
    plugin: str,
    action: str,
    params: dict | None = None,
    item: str | None = None,
    plugins: dict | None = None,
    root: str | None = None,
) -> dict:
    """
    Ejecuta una acción declarada por un plugin: "probar conexión" y similares.

    Es la **única** ejecución real que este servidor expone, y está acotada por
    el propio contrato: una `Action` es lo que se define como disparable por
    una persona fuera de un run. Un `Tool` no lo es —es un nodo de un flujo, y
    sin run no tiene contexto, ni traza, ni log de fila—.

    `item` resuelve los params desde un item ya guardado de la colección que
    declara la acción (ver `list_resource_items`), en vez de reconstruirlos a
    mano: "previsualizar" contra una conexión guardada, por ejemplo. `params`
    explícitos pisan lo que traiga el item.

    Para ejercitar un tool: escribir un `.mmd` de un nodo y pasarlo por
    `dry_run_flow`.
    """
    argv = ["action", plugin, action, "--json"]
    if params:
        argv += ["--params", json.dumps(params, ensure_ascii=False)]
    if item:
        argv += ["--item", item]
    return _cli(*argv, root=root, plugins=plugins)


# ── Andamio ─────────────────────────────────────────────────────────────


def plugin_template(name: str, ports: list[str] | None = None, **_) -> dict:
    """
    El esqueleto de un plugin: manifest, un tool, y los ports ya declarados.

    Se genera desde el contrato, así que no puede quedar desactualizado
    respecto de lo que el núcleo acepta.
    """
    from backend.core import ports as p

    pedidos = [x for x in (ports or []) if x in p.PLUGIN_PORTS]
    desconocidos = [x for x in (ports or []) if x not in p.PLUGIN_PORTS]

    declaracion = ", ".join(f'port_names.{x.upper()}' for x in pedidos)
    tupla = f"({declaracion},)" if len(pedidos) == 1 else f"({declaracion})" if pedidos else "()"

    uso = ""
    if p.HTTP in pedidos:
        uso = (
            '    respuesta = ctx.port(port_names.HTTP).request(ctx.params["url"])\n'
            "    if not respuesta.ok:\n"
            '        return ToolResult.err(f"la API respondió {respuesta.status}")\n'
            "    return ToolResult.ok(response=respuesta.json())\n"
        )
    elif p.FS in pedidos:
        uso = (
            '    destino = ctx.port(port_names.FS).move(ctx.params["origen"], "/destino")\n'
            '    ctx.log(f"movido a {destino}")\n'
            "    return ToolResult.ok(destino=destino)\n"
        )
    else:
        uso = '    ctx.log("hola")\n    return ToolResult.ok()\n'

    codigo = f'''"""
Plugin {name}.

Reglas que el núcleo hace cumplir, no sugerencias:

- No importar una librería externa. Pedir un port y usarlo con ctx.port(...).
- Declarar en `ports` todo lo que se vaya a usar; pedir uno sin declarar falla.
- Devolver siempre un ToolResult. Una excepción se convierte en err con su
  traceback: nada falla en silencio.
- No saber dónde se guardan los datos: leerlos con ctx.resource(...).
- No escribir HTML: declarar Setting, Resource.fields y Action.
"""

from __future__ import annotations

from backend.core import ports as port_names
from backend.core.contract import (
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    Setting,
    ToolContext,
    ToolManifest,
    ToolResult,
)

MANIFEST = PluginManifest(
    name="{name}",
    label="{name.title()}",
    version="0.1.0",
    doc="TODO: qué hace este plugin.",
    ports={tupla},
    settings=(
        Setting("{name}Timeout", ParamType.FLOAT, label="Timeout", default=30.0),
    ),
)

HACER = ToolManifest(
    id="{name}.hacer",
    label="hacer algo",
    category="{name.upper()}",
    doc="TODO: qué hace este tool.",
    params=(Param("url", required=True, doc="Admite {{variables}}"),),
    outputs=(Output("response", ParamType.JSON),),
)


def _hacer(ctx: ToolContext) -> ToolResult:
{uso}

def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[FunctionTool(manifest=HACER, fn=_hacer)],
    )


PLUGIN = build_plugin()
'''

    return {
        "filename": f"{name}.py",
        "code": codigo,
        "ports_declarados": pedidos,
        "ports_ignorados": desconocidos,
        "siguiente_paso": (
            f"Guardar el archivo y verificarlo con load_plugin(name='{name}', "
            f"path='<ruta>'). Después escribir un .mmd que lo use y pasarlo por "
            f"check_flow y dry_run_flow."
        ),
    }


__all__ = [
    "OperationError",
    "check_flow",
    "dry_run_flow",
    "install_plugin",
    "list_plugins",
    "list_ports",
    "list_tools",
    "load_plugin",
    "plugin_template",
    "run_action",
    "save_flow",
]
