"""
Diagnóstico de la instalación.

Responde "¿levanta todo?" antes de levantar nada. Es Python puro y sin
dependencias, así que corre en cualquier lado y se puede testear entero.

Los dos chequeos que más valor tienen son los últimos: cruzar los tools que usan
los flujos contra los instalados —responde "¿me falta un plugin?" sin ejecutar
nada— y verificar que cada plugin tenga adapter para los ports que pidió.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import boot as bootstrap
from .flow import parse_flow
from .ports import PLUGIN_PORTS
from .registry import ToolRegistry

MIN_PYTHON = (3, 10)


class Level(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass
class Check:
    """Resultado de un chequeo. `detail` son líneas para expandir en la UI."""

    name: str
    level: Level
    message: str
    detail: list[str] = field(default_factory=list)
    fix: str = ""  # qué hacer para resolverlo

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "level": self.level.value,
            "message": self.message,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.ERROR]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.WARN]

    @property
    def ok(self) -> bool:
        """Sin errores. Los warnings no impiden arrancar."""
        return not self.errors

    @property
    def summary(self) -> str:
        if self.errors:
            return f"{len(self.errors)} error(es), {len(self.warnings)} advertencia(s)"
        if self.warnings:
            return f"Todo listo, con {len(self.warnings)} advertencia(s)"
        return "Todo listo"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


# ── Chequeos ────────────────────────────────────────────────────────────


def check_python() -> Check:
    actual = sys.version_info[:3]
    if actual[:2] < MIN_PYTHON:
        return Check(
            "Python",
            Level.ERROR,
            f"{'.'.join(map(str, actual))} — se necesita {'.'.join(map(str, MIN_PYTHON))} o superior",
            fix=f"Instalar Python {'.'.join(map(str, MIN_PYTHON))}+",
        )
    return Check("Python", Level.OK, ".".join(map(str, actual)), detail=[sys.executable])


def check_env(root: Path) -> Check:
    """
    `.env` es opcional.

    La configuración de la instancia vive en la base (tabla `settings`); `.env`
    sirve para secretos y para pisar un valor en una máquina puntual. Que no
    exista es lo normal, no un problema.
    """
    if (root / ".env").exists():
        return Check("Archivo .env", Level.OK, "presente")
    return Check(
        "Archivo .env",
        Level.OK,
        "no existe (es opcional)",
        detail=["La configuración vive en la base"],
    )


def check_plugins(registry: ToolRegistry) -> Check:
    nombres = [f"{p.name} v{p.version} ({len(p.tool_ids)} tools)" for p in registry.plugins]

    if registry.errors:
        return Check(
            "Plugins",
            Level.ERROR,
            f"{len(registry.errors)} no cargaron",
            detail=[f"{e.name}: {e.error}" for e in registry.errors] + nombres,
            fix="Revisar el plugin, o desinstalarlo",
        )
    if not registry.plugins:
        return Check("Plugins", Level.WARN, "ninguno instalado", fix="Instalar al menos uno")

    total = len(registry.manifests)
    return Check("Plugins", Level.OK, f"{len(registry.plugins)} cargados, {total} tools", nombres)


def check_adapters(registry: ToolRegistry) -> Check:
    """
    Que cada port pedido por un plugin tenga adapter atado.

    El registry ya rechaza al cargar un plugin sin adapter, así que un fallo acá
    es raro; el valor del chequeo es el inverso: **mostrar** qué puede hacer
    cada plugin instalado. Quién sale a la red, quién toca el disco, quién corre
    comandos. Esa lista antes no existía en ninguna parte y había que leer los
    imports de cada plugin para reconstruirla.
    """
    atados = registry.adapters
    if not atados:
        return Check(
            "Adapters",
            Level.ERROR,
            "ninguno atado",
            detail=["Ningún plugin que necesite un port va a poder cargar"],
            fix="Construir la instancia con build_default_adapters()",
        )

    huerfanos = [p for p in atados if p not in PLUGIN_PORTS]
    detalle = [f"{port}: {type(adapter).__name__}" for port, adapter in sorted(atados.items())]

    for plugin in registry.plugins:
        if plugin.ports:
            detalle.append(f"{plugin.name} usa: {', '.join(plugin.ports)}")

    if huerfanos:
        return Check(
            "Adapters",
            Level.WARN,
            f"{len(atados)} atados, {len(huerfanos)} para ports que un plugin no puede pedir",
            detail=detalle + [f"no pedible por un plugin: {', '.join(huerfanos)}"],
        )
    return Check("Adapters", Level.OK, f"{len(atados)} atados", detalle)


def check_config(registry: ToolRegistry, config: dict) -> Check:
    faltantes = registry.missing_config(config)
    if not faltantes:
        return Check("Configuración", Level.OK, "completa")

    detalle = [
        f"{plugin}: {m.label} ({m.key}) — {m.reason}"
        for plugin, pendientes in faltantes.items()
        for m in pendientes
    ]
    return Check(
        "Configuración",
        Level.WARN,
        f"faltan {len(detalle)} valor(es)",
        detalle,
        fix="Completarlos en Configuración",
    )


def check_workflows(workflows) -> Check:
    """
    Que los flujos guardados parseen.

    Recibe los flujos, no una carpeta: viven en la base, no en el disco. Un
    chequeo que mirara `workflows/*.mmd` informaría sobre archivos sueltos que
    ya nadie ejecuta — peor que no chequear, porque da confianza falsa.
    """
    if not workflows:
        return Check("Flujos", Level.WARN, "no hay ninguno guardado")

    rotos, habilitados = [], 0
    for wf in workflows:
        graph = parse_flow(wf.content)
        if wf.enabled:
            habilitados += 1
        if graph.errors:
            estado = "habilitado" if wf.enabled else "deshabilitado"
            primero = graph.errors[0]
            ubicacion = f"L{primero.line}: " if primero.line else ""
            rotos.append(f"{wf.name} ({estado}) — {ubicacion}{primero.message}")

    resumen = f"{len(workflows)} flujos, {habilitados} habilitados"
    if rotos:
        return Check(
            "Flujos",
            Level.ERROR,
            f"{resumen}; {len(rotos)} con errores",
            rotos,
            fix="Corregirlos y volver a guardarlos",
        )
    return Check("Flujos", Level.OK, resumen)


def check_tools_used(registry: ToolRegistry, workflows) -> Check:
    """
    Cruza los tools que usan los flujos contra los instalados.

    Es el chequeo que responde "¿me falta un plugin?" sin ejecutar nada, y el
    que convierte un fallo de producción en un aviso al arrancar.
    """
    if not workflows:
        return Check("Tools de los flujos", Level.WARN, "no hay flujos para revisar")

    usados: dict[str, set[str]] = {}
    for wf in workflows:
        if not wf.enabled:
            continue
        for _, node in parse_flow(wf.content).action_nodes():
            usados.setdefault(node.fn, set()).add(wf.name)

    # flow.* lo resuelve el executor, no el registry.
    nativos = {"flow.ejecutar", "flow.retry_gate"}
    faltantes = {
        fn: flujos
        for fn, flujos in usados.items()
        if fn not in nativos and registry.get(fn) is None
    }

    if not faltantes:
        return Check(
            "Tools de los flujos",
            Level.OK,
            f"los {len(usados)} tools usados están disponibles",
        )

    detalle = [
        f"{fn} — lo usan: {', '.join(sorted(flujos))}" for fn, flujos in sorted(faltantes.items())
    ]
    return Check(
        "Tools de los flujos",
        Level.ERROR,
        f"{len(faltantes)} tool(s) sin plugin instalado",
        detalle,
        fix="Instalar el plugin que los provee, o editar los flujos",
    )


def check_storage(boot: bootstrap.BootConfig) -> Check:
    """
    Que se vaya a poder guardar, y dónde.

    Va **antes** de abrir la base a propósito: una instalación nueva en una
    carpeta sin permiso de escritura no falla al arrancar, falla la primera vez
    que alguien guarda algo, que es cuando ya nadie está mirando el diagnóstico.
    """
    if boot.efimera:
        return Check(
            "Almacenamiento",
            Level.WARN,
            "en memoria — no queda nada al cerrar",
            detail=["Declarado con storage=:memory: en el arranque"],
            fix="Quitar storage=:memory: para que la instalación persista",
        )

    archivo = Path(boot.storage_path)
    if not bootstrap.escribible(archivo.parent):
        return Check(
            "Almacenamiento",
            Level.ERROR,
            f"no se puede escribir en {archivo.parent}",
            detail=[f"La base iría a {archivo}"],
            fix="Elegir otra carpeta con storage=, o dar permiso de escritura",
        )

    detalle = [str(archivo)]
    if archivo.is_file():
        detalle.append(f"{_peso(archivo.stat().st_size)} ocupados")
        return Check("Almacenamiento", Level.OK, "base presente y escribible", detalle)
    return Check(
        "Almacenamiento",
        Level.OK,
        "carpeta escribible; la base se crea al arrancar",
        detalle,
    )


def check_crypto(crypto) -> Check:
    """
    Que los secretos se vayan a poder guardar.

    `cryptography` es una dependencia real y puede no estar. Sin este chequeo,
    el cliente se entera cuando intenta cargar su primer token — es decir,
    tarde, y en una pantalla que no es la de instalación.
    """
    if crypto is None:
        return Check(
            "Cifrado",
            Level.ERROR,
            "no hay cifrado atado",
            detail=["Ningún secreto se va a poder guardar"],
            fix="Construir la instancia sin pisar `crypto`",
        )

    nombre = type(crypto).__name__
    if not getattr(crypto, "available", True):
        # Warning y no error: sin `cryptography` la instalación levanta y los
        # flujos que no tocan un secreto corren igual. Lo que no va a poder
        # hacer es guardar uno, y de eso se avisa acá y no cuando el cliente
        # ya está tipeando su token en la pantalla de secretos.
        return Check(
            "Cifrado",
            Level.WARN,
            "no disponible: falta el paquete `cryptography` o está roto",
            detail=[
                f"{nombre} no puede cifrar nada sin él",
                "Ningún secreto se va a poder guardar; el resto funciona",
            ],
            fix="pip install cryptography",
        )

    llave = getattr(crypto, "key_path", None)
    if getattr(crypto, "key_exists", False):
        return Check(
            "Cifrado",
            Level.OK,
            "disponible, con la llave ya generada",
            detail=[str(llave)] if llave else [],
        )
    return Check(
        "Cifrado",
        Level.OK,
        "disponible; la llave se genera con el primer secreto",
        # La llave vive fuera de la base para que un backup del `.db` no lleve
        # los secretos adentro. El corolario es que un backup **sólo** de la
        # base no los recupera, y eso hay que decirlo antes y no después.
        detail=([str(llave)] if llave else []) + [
            "Sin ese archivo, los secretos guardados no se pueden descifrar",
        ],
    )


def check_boot(boot: bootstrap.BootConfig) -> Check:
    """
    Valores de arranque que no van a hacer lo que dicen.

    `boot.validar()` reporta y no levanta, así que esto es un warning y no un
    error: la instalación arranca igual, con el valor por defecto o sin cargar
    nada, y de eso justamente se trata el aviso.
    """
    problemas = bootstrap.validar(boot)
    if not problemas:
        return Check("Arranque", Level.OK, "los valores declarados son usables")
    return Check(
        "Arranque",
        Level.WARN,
        f"{len(problemas)} valor(es) dudoso(s)",
        problemas,
        fix=f"Corregirlos en {bootstrap.ARCHIVO} o en el entorno",
    )


def run_checks(
    *,
    root: Path,
    registry: ToolRegistry,
    config: dict | None = None,
    workflows: list | None = None,
    boot: bootstrap.BootConfig | None = None,
    crypto=None,
) -> Report:
    """
    Corre todos los chequeos y devuelve el informe.

    `boot` y `crypto` son opcionales para no obligar a un test a construir una
    instalación entera; cuando están, se agregan los dos chequeos que más
    importan en una máquina nueva —dónde se guarda y si se puede cifrar—, que
    son los que un instalador corre antes de crear nada.
    """
    flujos = list(workflows or [])
    report = Report()
    report.add(check_python())
    report.add(check_env(root))
    if boot is not None:
        report.add(check_boot(boot))
        report.add(check_storage(boot))
    if crypto is not None:
        report.add(check_crypto(crypto))
    report.add(check_adapters(registry))
    report.add(check_plugins(registry))
    report.add(check_config(registry, config or {}))
    report.add(check_workflows(flujos))
    report.add(check_tools_used(registry, flujos))
    return report


def _peso(bytes_: int) -> str:
    unidad = float(bytes_)
    for sufijo in ("B", "KB", "MB", "GB"):
        if unidad < 1024 or sufijo == "GB":
            return f"{unidad:.0f} {sufijo}" if sufijo == "B" else f"{unidad:.1f} {sufijo}"
        unidad /= 1024
    return f"{unidad:.1f} GB"


def format_text(report: Report, color: bool = True) -> str:
    """Informe para consola."""
    simbolos = {Level.OK: "  ok  ", Level.WARN: " warn ", Level.ERROR: "ERROR "}
    colores = {Level.OK: "\033[32m", Level.WARN: "\033[33m", Level.ERROR: "\033[31m"}
    reset = "\033[0m"

    lineas = []
    for check in report.checks:
        marca = simbolos[check.level]
        if color and (tono := colores.get(check.level)):
            marca = f"{tono}{marca}{reset}"
        lineas.append(f"[{marca}] {check.name}: {check.message}")
        for item in check.detail:
            lineas.append(f"           · {item}")
        if check.fix and check.level is not Level.OK:
            lineas.append(f"           → {check.fix}")

    lineas.append("")
    lineas.append(report.summary)
    return "\n".join(lineas)


def main() -> int:
    """`python -m backend.core.doctor` — el diagnóstico sin levantar nada más."""
    # La raíz de la instalación es `backend/`: ahí vive `data/`.
    root = Path(__file__).resolve().parent.parent

    from .instance import Instance

    instancia = Instance(root)
    try:
        report = run_checks(
            root=root,
            registry=instancia.registry,
            config=instancia.effective_config(),
            workflows=instancia.workflows.list(),
            boot=instancia.boot,
            crypto=instancia.crypto,
        )
    finally:
        instancia.close()

    print(format_text(report, color=sys.stdout.isatty()))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
