"""
CLI del núcleo — `python -m backend.core`

Existe para ejercitar el motor sin UI, sin servidor y sin launcher. Es la
interfaz "en crudo" mientras el frontend no existe, y sigue siendo útil después:
un flujo se puede correr desde una terminal, un CI o un script, y sale lo mismo
que saldría por cualquier otra vía porque construye el mismo `Instance`.

No es una segunda implementación de nada. Todo lo que hace es leer del catálogo
y llamar a `Instance`. Si algo acá tuviera lógica propia, ya estaríamos de vuelta
en el problema de dos motores divergiendo.

    python -m backend.core doctor
    python -m backend.core tools
    python -m backend.core plugins
    python -m backend.core workflows
    python -m backend.core add flujo.mmd
    python -m backend.core check flujo.mmd --json
    python -m backend.core check flujo.mmd --plugin mio=./mi_plugin.py
    python -m backend.core run flujo.mmd --row '{"id": "42"}'
    python -m backend.core run mi-flujo --case 42 --dry-run
    python -m backend.core action mi_plugin probar --params '{"url": "..."}'
    python -m backend.core trace <run_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import boot
from .doctor import format_text, run_checks
from .flow.parser import Severity, parse_flow
from .instance import Instance, WorkflowNotFound
from .users import KINDS, UserError

# La raíz de la instalación es `backend/`: ahí vive `data/`.
ROOT = Path(__file__).resolve().parent.parent


# ── Comandos ────────────────────────────────────────────────────────────


def cmd_doctor(inst: Instance, args) -> int:
    report = run_checks(
        root=ROOT,
        registry=inst.registry,
        config=inst.effective_config(),
        workflows=inst.workflows.list(),
        boot=inst.boot,
        crypto=inst.crypto,
    )
    print(format_text(report, color=sys.stdout.isatty()))
    return 0 if report.ok else 1


def cmd_tools(inst: Instance, args) -> int:
    catalogo = inst.registry.catalog()
    if args.json:
        print(json.dumps(catalogo, indent=2, ensure_ascii=False))
        return 0

    if not catalogo["tools"]:
        print("No hay ningún tool registrado.")
        return 0

    for tool in catalogo["tools"]:
        marca = " [peligroso]" if tool["dangerous"] else ""
        print(f"{tool['id']}{marca}")
        print(f"  {tool['label']} · {tool['category']}")
        if tool["doc"]:
            print(f"  {tool['doc']}")
        for p in tool["params"]:
            obligatorio = "*" if p["required"] else " "
            por_defecto = f" = {p['default']!r}" if p["default"] is not None else ""
            print(f"   {obligatorio} {p['name']}: {p['type']}{por_defecto}")
        if tool["extra_params"]:
            print("   + acepta parámetros no declarados")
        for o in tool["outputs"]:
            print(f"   → {o['name']}: {o['type']}")
        if tool["extra_outputs"]:
            print("   → + escribe variables no declaradas")
        print()
    return 0


def cmd_plugins(inst: Instance, args) -> int:
    catalogo = inst.registry.catalog()
    if args.json:
        print(json.dumps(catalogo, indent=2, ensure_ascii=False))
        return 1 if catalogo["errors"] else 0

    print(f"contrato v{catalogo['contract']} · ports atados: {', '.join(catalogo['ports'])}\n")

    for p in catalogo["plugins"]:
        usa = f" · usa {', '.join(p['ports'])}" if p["ports"] else " · no usa ports"
        print(f"{p['name']} v{p['version']} ({p['source']}){usa}")
        print(f"  {len(p['tools'])} tools, {len(p['settings'])} settings, "
              f"{len(p['resources'])} colecciones, {len(p['actions'])} acciones")
        for s in p["settings"]:
            obligatorio = "*" if s["required"] else " "
            print(f"   {obligatorio} {s['key']}: {s['type']}")
        for r in p["resources"]:
            campos = ", ".join(f["name"] for f in r["fields"])
            print(f"   [{r['name']}] {r['label']} ({campos})")
        for a in p["actions"]:
            print(f"   ▸ {a['name']}: {a['label']}")
        print()

    if catalogo["errors"]:
        print("Plugins que NO cargaron:")
        for e in catalogo["errors"]:
            print(f"  {e['name']} ({e['source']}): {e['error']}")
        return 1
    return 0


def cmd_workflows(inst: Instance, args) -> int:
    flujos = inst.list_workflows()
    if not flujos:
        print("No hay flujos guardados. Agregá uno con: python -m backend.core add archivo.mmd")
        return 0
    for w in flujos:
        estado = "" if w.get("enabled", True) else " (deshabilitado)"
        carpeta = f"{w['folder']}/" if w.get("folder") else ""
        print(f"{carpeta}{w['name']}{estado}")
        if w.get("description"):
            print(f"  {w['description']}")
    return 0


def cmd_add(inst: Instance, args) -> int:
    path = Path(args.file)
    if not path.is_file():
        mensaje = f"No existe el archivo: {path}"
        if args.json:
            print(json.dumps({"ok": False, "error": mensaje}, ensure_ascii=False))
            return 2
        print(mensaje, file=sys.stderr)
        return 2

    wf = inst.workflows.save_mmd(args.name or path.stem, path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps({
            "ok": True,
            "name": wf.name,
            "folder": wf.folder,
            "state": wf.state,
            "description": wf.description,
        }, ensure_ascii=False))
        return 0

    print(f"Guardado como \"{wf.name}\".")
    return 0


def cmd_check(inst: Instance, args) -> int:
    """
    Parsea un flujo y lo cruza con los tools instalados.

    Acepta una ruta a un `.mmd` o el nombre de un flujo guardado. La ruta es lo
    que hace útil el comando mientras se escribe un flujo: se valida sin
    guardarlo primero.
    """
    texto, origen = _cargar_flujo(inst, args.flow)
    if texto is None:
        print(origen, file=sys.stderr)
        return 2

    graph = parse_flow(texto, with_meta=True)
    diagnosticos = sorted(
        list(graph.errors) + list(graph.warnings) + inst.check_graph(graph),
        key=lambda d: (d.line or 0),
    )
    hay_errores = any(d.severity is Severity.ERROR for d in diagnosticos)

    if args.json:
        # Estructurado para que un consumidor automático sepa qué línea tocar,
        # en vez de tener que parsear castellano.
        print(json.dumps({
            "source": origen,
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "ok": not hay_errores,
            "meta": graph.meta.to_dict(),
            "diagnostics": [d.to_dict() for d in diagnosticos],
        }, indent=2, ensure_ascii=False))
        return 1 if hay_errores else 0

    print(f"{origen}: {len(graph.nodes)} nodos, {len(graph.edges)} aristas")
    if not diagnosticos:
        print("Sin problemas.")
        return 0

    for d in diagnosticos:
        etiqueta = "ERROR" if d.severity is Severity.ERROR else "aviso"
        ubicacion = f"L{d.line}" if d.line else "—"
        nodo = f" [{d.node_id}]" if d.node_id else ""
        print(f"  {etiqueta} {ubicacion}{nodo}: {d.message}")

    return 1 if hay_errores else 0


def cmd_run(inst: Instance, args) -> int:
    texto, origen = _cargar_flujo(inst, args.flow)
    if texto is None:
        print(origen, file=sys.stderr)
        return 2

    try:
        row = json.loads(args.row) if args.row else {}
    except json.JSONDecodeError as exc:
        print(f"--row no es JSON válido: {exc}", file=sys.stderr)
        return 2
    if not isinstance(row, dict):
        print("--row tiene que ser un objeto JSON", file=sys.stderr)
        return 2

    # Correr un archivo suelto NO lo guarda. Antes sí, en silencio, para que
    # flow.ejecutar pudiera resolver subflujos por nombre: validar un archivo te
    # escribía en la base sin decirlo. Ahora se registra sólo mientras dura el
    # comando, y persistirlo es `add` o `--save`, que son explícitos.
    nombre, efimero = _registrar_flujo(inst, args.flow, texto, guardar=args.save)
    case_id = args.case or str(row.get("id") or "cli")

    try:
        resultado = inst.run(
            nombre,
            case_id,
            row=row,
            actor=args.actor,
            dry_run=args.dry_run,
            persist=not args.no_persist,
        )
    except UserError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    finally:
        if efimero:
            inst.workflows.delete(nombre)

    if args.json:
        print(json.dumps(resultado.to_dict(), indent=2, ensure_ascii=False))
        return 0 if not resultado.failed else 1

    for entrada in resultado.logs:
        marca = {"info": " ", "warning": "!", "error": "×"}.get(entrada.level, " ")
        print(f"{entrada.t} {marca} {entrada.message}")

    print()
    print(f"run {resultado.run_id} · {resultado.status}"
          + (" (dry run)" if resultado.dry_run else ""))
    if resultado.failed:
        print(f"falló en {resultado.failed_node}: {resultado.message}")
    return 0 if not resultado.failed else 1


def cmd_action(inst: Instance, args) -> int:
    """
    Ejecuta una acción declarada por un plugin ("probar conexión", etc.).

    Es la única ejecución **real** que la CLI ofrece fuera de un run, y está
    acotada por diseño: una `Action` es lo que el contrato define como
    disparable por una persona, a diferencia de un `Tool`, que es un nodo de un
    flujo y sin run no tiene contexto, ni traza, ni log de fila.
    """
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"--params no es JSON válido: {exc}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("--params tiene que ser un objeto JSON", file=sys.stderr)
        return 2

    resultado, registro = inst.run_action(args.plugin, args.action, params, item=args.item)

    if args.json:
        print(json.dumps({
            **resultado.to_dict(),
            "log": [{"message": m, "level": n} for m, n in registro],
        }, indent=2, ensure_ascii=False))
        return 0 if not resultado.failed else 1

    for mensaje, nivel in registro:
        marca = {"info": " ", "warning": "!", "error": "×"}.get(nivel, " ")
        print(f"{marca} {mensaje}")
    print(f"\n{resultado.status}" + (f": {resultado.message}" if resultado.message else ""))
    return 0 if not resultado.failed else 1


def cmd_resources(inst: Instance, args) -> int:
    """
    Items de una colección de un plugin, con los campos `secret` tapados.

    Es lo que hace falta antes de poder pedir un item por nombre: sin esto, un
    agente no tiene forma de saber qué "sources" existen ya guardadas. Nunca
    ejecuta nada ni resuelve `{env.CLAVE}` — para eso está `action --item`.
    """
    definicion = inst.resource_definition(args.plugin, args.resource)
    if definicion is None:
        print(
            f'no hay una colección "{args.resource}" en el plugin "{args.plugin}"',
            file=sys.stderr,
        )
        return 2

    items = inst.resource_items_masked(args.plugin, args.resource)

    if args.json:
        print(json.dumps({
            "plugin": args.plugin,
            "resource": args.resource,
            "key_field": definicion.key_field,
            "fields": [f.to_dict() for f in definicion.fields],
            "items": items,
        }, indent=2, ensure_ascii=False))
        return 0

    print(f"{args.plugin}.{args.resource}: {len(items)} item(s)")
    for item in items:
        print(f"  - {item.get(definicion.key_field, '?')}")
    return 0


def cmd_users(inst: Instance, args) -> int:
    """
    Alta, baja y consulta de actores.

    Toda rama respeta `--json`. Que un flag declarado se ignore en algunas
    ramas y en otras no es el peor modo de falla para un consumidor
    automático: no da error, devuelve otra cosa, y se entera lejos de la causa.
    """
    accion = args.accion
    ports = args.ports.split(",") if args.ports else None

    try:
        if accion == "add":
            afectado = inst.users.create(
                args.name,
                kind=args.kind,
                label=args.label or "",
                can_run_dangerous=args.dangerous,
                allowed_ports=ports,
            )
        elif accion == "allow":
            afectado = inst.users.update_policy(
                args.name,
                can_run_dangerous=args.dangerous,
                allowed_ports=ports,
                clear_ports=args.all_ports,
            )
        elif accion == "disable":
            afectado = inst.users.disable(args.name)
        elif accion == "enable":
            afectado = inst.users.enable(args.name)
        else:
            afectado = None
    except UserError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(str(exc), file=sys.stderr)
        return 2

    usuarios = [afectado] if afectado is not None else inst.users.list()

    if args.json:
        print(json.dumps([u.to_dict() for u in usuarios], indent=2, ensure_ascii=False))
        return 0

    for u in usuarios:
        estado = "" if u.enabled else "  (deshabilitado)"
        permitidos = ", ".join(u.allowed_ports) if u.allowed_ports is not None else "todos"
        print(f"{u.name}  [{u.kind}]{estado}")
        print(f"    peligrosos: {'sí' if u.can_run_dangerous else 'no'} · ports: {permitidos}")
    return 0


def cmd_init(inst: Instance, args) -> int:
    """
    Escribe `boot.env` con la configuración de arranque de esta instalación.

    Es un comando y no un efecto secundario de correr: un proceso que reescribe
    su propia config de arranque produce un archivo que deriva solo, que no se
    puede versionar ni provisionar desde un contenedor.
    """
    destino = Path(args.root) / boot.ARCHIVO
    if destino.exists() and not args.force:
        print(f"Ya existe {destino}. Usá --force para reescribirlo.", file=sys.stderr)
        return 2

    destino.parent.mkdir(parents=True, exist_ok=True)
    # Con BOM: el archivo se escribe para que una persona lo abra y lo edite, y
    # en Windows —el destino del instalador— Notepad y PowerShell 5.1 leen
    # UTF-8 sin BOM como cp1252 y muestran los acentos rotos. Peor: al guardar,
    # esas mismas herramientas devuelven el archivo con el mote roto adentro.
    # Leer tolera las dos formas, así que esto no divide el formato en dos.
    destino.write_text(boot.render(inst.boot), encoding="utf-8-sig")
    print(f"Escrito {destino}")
    print("Se lee al arrancar; cualquier clave se pisa desde el entorno con "
          f"{boot.PREFIJO}<clave>.")
    return 0


def cmd_boot(inst: Instance, args) -> int:
    """Qué configuración de arranque está en efecto, y de dónde salió cada valor."""
    datos = inst.boot.to_dict()
    if args.json:
        print(json.dumps(datos, indent=2, ensure_ascii=False))
        return 0

    origen = datos.pop("origen")
    for clave, valor in datos.items():
        procedencia = origen.get(clave)
        marca = f"   ← {procedencia}" if procedencia else "   (por defecto)"
        print(f"{clave:20} {valor}{marca}")

    salida = 0

    if desconocidas := boot.desconocidas(inst.root):
        print()
        print("Claves que el núcleo no reconoce (¿un typo?):")
        for clave in desconocidas:
            print(f"  {clave}")
        salida = 1

    # El nombre de la clave está bien pero el valor no va a hacer lo que dice.
    # Es el mismo modo de falla un escalón más abajo, y se reporta igual.
    if problemas := boot.validar(inst.boot):
        print()
        print("Valores que no van a hacer lo que dicen:")
        for problema in problemas:
            print(f"  {problema}")
        salida = 1

    return salida


def cmd_trace(inst: Instance, args) -> int:
    detalle = inst.run_detail(args.run_id)
    if detalle is None:
        print(f"No existe el run {args.run_id}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(detalle, indent=2, ensure_ascii=False))
        return 0

    for nodo in detalle.get("trace", []):
        estado = nodo["status"]
        duracion = f"{nodo['duration_ms']}ms"
        print(f"[{estado:3}] {nodo['node_id']} · {nodo.get('fn') or nodo['node_type']} ({duracion})")
        for clave, valor in (nodo.get("params") or {}).items():
            print(f"        {clave} = {valor!r}")
        if nodo.get("message"):
            print(f"        → {nodo['message']}")
        if nodo.get("traceback"):
            print("        " + nodo["traceback"].replace("\n", "\n        ").rstrip())
    return 0


# ── Ayudas ──────────────────────────────────────────────────────────────


def _plugins_locales(especificaciones: list[str] | None) -> dict[str, str]:
    """
    `--plugin nombre=ruta` → lo que `Instance` espera en `local_plugins`.

    Es lo que permite validar un plugin recién escrito sin instalarlo: se apunta
    a un archivo o a una carpeta y el registry lo carga como cualquier otro,
    con las mismas validaciones de ports, ids y contrato.

    La carpeta contenedora entra en `sys.path` porque un plugin de varios
    módulos necesita poder importarse a sí mismo. Es responsabilidad de esta
    capa —un adapter de entrada— y no del registry, que sólo resuelve nombres.
    """
    resueltos: dict[str, str] = {}
    for especificacion in especificaciones or []:
        nombre, _, ruta = especificacion.partition("=")
        nombre, ruta = nombre.strip(), ruta.strip()
        if not nombre or not ruta:
            raise ValueError(
                f"--plugin inválido: {especificacion!r} (se espera nombre=ruta)"
            )

        destino = Path(ruta).expanduser().resolve()
        if destino.is_dir():
            # Un paquete: se importa por su nombre de carpeta.
            modulo, contenedor = destino.name, destino.parent
        elif destino.is_file():
            modulo, contenedor = destino.stem, destino.parent
        else:
            raise ValueError(f"--plugin {nombre}: no existe {destino}")

        if str(contenedor) not in sys.path:
            sys.path.insert(0, str(contenedor))
        resueltos[nombre] = f"{modulo}:PLUGIN"
    return resueltos


def _cargar_flujo(inst: Instance, referencia: str) -> tuple[str | None, str]:
    """
    El texto de un flujo, sea una ruta a un `.mmd` o un nombre guardado.

    Devuelve `(texto, origen)`, o `(None, mensaje_de_error)`. Aceptar las dos
    formas es lo que hace usable la CLI mientras se escribe un flujo: no hay que
    guardarlo para probarlo.
    """
    path = Path(referencia)
    if path.is_file():
        return path.read_text(encoding="utf-8"), str(path)
    try:
        return inst.load_workflow(referencia), f'flujo "{referencia}"'
    except WorkflowNotFound:
        guardados = ", ".join(w["name"] for w in inst.list_workflows()) or "ninguno"
        return None, (
            f'No hay ni un archivo ni un flujo guardado llamado "{referencia}". '
            f"Guardados: {guardados}"
        )


def _registrar_flujo(
    inst: Instance, referencia: str, texto: str, guardar: bool = False
) -> tuple[str, bool]:
    """
    Deja el flujo disponible por nombre y dice si hay que sacarlo después.

    El executor resuelve `flow.ejecutar` consultando el store por nombre, así
    que un flujo que viene de un archivo tiene que estar registrado mientras
    dura la corrida. Lo que cambió es que **se limpia al terminar**, salvo que
    se pida `--save`.

    Archivos para autoría, store para runtime, y un puente explícito en el
    medio. Que correr un archivo lo persistiera era ese puente ocurriendo solo.
    """
    path = Path(referencia)
    if not path.is_file():
        return referencia, False

    nombre = path.stem
    ya_estaba = inst.workflows.get(nombre) is not None
    inst.workflows.save_mmd(nombre, texto)
    # Si ya existía con ese nombre no se borra: sería destruir lo del usuario.
    return nombre, not (guardar or ya_estaba)


# ── Entrada ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.core",
        description="Motor de workflows — sin UI, sin servidor.",
    )
    parser.add_argument(
        "--root",
        default=str(ROOT),
        help="Directorio de la instalación (default: backend/).",
    )
    parser.add_argument(
        "--actor",
        help=(
            "Quién ejecuta. Determina qué tiene permitido, no sólo qué queda "
            "registrado. Por defecto, el actor declarado en el arranque."
        ),
    )
    parser.add_argument(
        "--plugin",
        # `dest` explícito: el subcomando `action` tiene un positional llamado
        # `plugin`, y sin esto lo pisaría. argparse no avisa de la colisión —
        # simplemente gana el último, y el error aparece lejos de la causa.
        dest="local_plugins",
        action="append",
        metavar="NOMBRE=RUTA",
        help=(
            "Carga un plugin sin instalarlo, desde un archivo o una carpeta. "
            "Repetible. Ej: --plugin mio=./mi_plugin.py"
        ),
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("doctor", help="Diagnóstico de la instalación.")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("tools", help="Catálogo de tools disponibles.")
    p.add_argument("--json", action="store_true", help="El catálogo crudo.")
    p.set_defaults(fn=cmd_tools)

    p = sub.add_parser("plugins", help="Plugins cargados, sus ports y su configuración.")
    p.add_argument("--json", action="store_true", help="El catálogo crudo.")
    p.set_defaults(fn=cmd_plugins)

    p = sub.add_parser("workflows", help="Flujos guardados.")
    p.set_defaults(fn=cmd_workflows)

    p = sub.add_parser("add", help="Guarda un .mmd en la instalación.")
    p.add_argument("file")
    p.add_argument("--name", help="Nombre con el que guardarlo (default: el del archivo).")
    p.add_argument("--json", action="store_true", help="Resultado estructurado.")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("check", help="Valida un flujo contra los tools instalados.")
    p.add_argument("flow", help="Ruta a un .mmd, o el nombre de un flujo guardado.")
    p.add_argument("--json", action="store_true", help="Diagnósticos estructurados.")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("run", help="Ejecuta un flujo.")
    p.add_argument("flow", help="Ruta a un .mmd, o el nombre de un flujo guardado.")
    p.add_argument("--row", help='Fila de entrada, como JSON: \'{"id": "42"}\'')
    p.add_argument("--case", help="Id del caso (default: row.id, o 'cli').")
    p.add_argument("--dry-run", action="store_true", help="Valida y recorre sin ejecutar tools.")
    p.add_argument("--no-persist", action="store_true", help="No guarda el run ni su log.")
    p.add_argument(
        "--save",
        action="store_true",
        help="Guarda el flujo en la instalación. Sin esto, correr un archivo no lo persiste.",
    )
    p.add_argument("--json", action="store_true", help="El resultado completo, con la traza.")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("users", help="Actores: quién puede ejecutar, y qué.")
    p.add_argument(
        "accion",
        nargs="?",
        default="list",
        choices=("list", "add", "allow", "disable", "enable"),
    )
    p.add_argument("name", nargs="?")
    p.add_argument("--kind", default="human", choices=KINDS)
    p.add_argument("--label", help="Nombre para mostrar.")
    p.add_argument(
        "--dangerous",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Permite ejecutar tools marcados como peligrosos.",
    )
    p.add_argument("--ports", help="Ports permitidos, separados por coma.")
    p.add_argument("--all-ports", action="store_true", help="Permite todos los ports.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_users)

    p = sub.add_parser("init", help="Escribe boot.env con la config de arranque.")
    p.add_argument("--force", action="store_true", help="Reescribe si ya existe.")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("boot", help="Config de arranque en efecto, y de dónde salió.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_boot)

    p = sub.add_parser("action", help="Ejecuta una acción declarada por un plugin.")
    p.add_argument("plugin")
    p.add_argument("action")
    p.add_argument("--params", help='Parámetros, como JSON: \'{"url": "..."}\'')
    p.add_argument(
        "--item",
        help="Clave de un item ya guardado: resuelve los params desde ahí. "
             "--params explícitos pisan lo que traiga el item.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_action)

    p = sub.add_parser(
        "resources", help="Items de una colección de un plugin (secrets tapados)."
    )
    p.add_argument("plugin")
    p.add_argument("resource")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_resources)

    p = sub.add_parser("trace", help="La traza de un run ya ejecutado.")
    p.add_argument("run_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_trace)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        locales = _plugins_locales(args.local_plugins)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    instancia = Instance(args.root, local_plugins=locales)
    try:
        return args.fn(instancia, args)
    finally:
        instancia.close()


__all__ = ["build_parser", "main"]
