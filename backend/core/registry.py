"""
Registro de tools — descubrimiento, inyección de adapters y ejecución.

El núcleo no importa ningún plugin por nombre. Los descubre por entry point, lo
que permite que instalar un paquete lo haga aparecer en el catálogo sin tocar
una línea del núcleo.

Un plugin declara en su pyproject.toml:

    [project.entry-points."bot.tools"]
    mi_plugin = "mi_paquete:PLUGIN"

donde PLUGIN es un Plugin (manifest + tools), o una lista de objetos que cumplan
el Protocol Tool.

El módulo puede llamarse como sea: el entry point declara dónde está, así que un
plugin de terceros no necesita vivir en ningún directorio ni respetar prefijos.

El registry es además quien **resuelve los ports**. Un plugin declara qué
necesita (`PluginManifest.ports`) y acá se comprueba, al cargar, que exista un
adapter para cada uno. Un plugin que pide un port sin adapter no se registra y
queda reportado como error de carga — en vez de fallar a mitad de un run, meses
después, en la máquina de otro.

Durante el desarrollo, register_local() permite cargar un plugin desde una ruta
sin publicarlo.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Iterable

from .contract import (
    CONTRACT_VERSION,
    Action,
    MissingSetting,
    ParamError,
    Plugin,
    PluginManifest,
    Tool,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from .ports import PLUGIN_PORTS

log = logging.getLogger(__name__)

# Grupo de entry points donde se buscan los plugins. Genérico a propósito: el
# núcleo no es de una empresa ni de un dominio.
ENTRY_POINT_GROUP = "bot.tools"

# Versiones de contrato que este núcleo acepta. Mantener N y N-1 es lo que
# permite actualizar el núcleo sin romper plugins de terceros.
SUPPORTED_CONTRACTS = frozenset({CONTRACT_VERSION})


def _module_of(entry_point_value: str) -> object | None:
    """Módulo raíz de un entry point 'paquete.modulo:ATRIBUTO', para leerle la versión."""
    module_name = entry_point_value.partition(":")[0]
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


def _version_of(module: object | None) -> str:
    """Versión declarada por el plugin; 'desconocida' si no expone __version__."""
    return str(getattr(module, "__version__", "") or "desconocida")


@dataclass(frozen=True)
class LoadedPlugin:
    """Un plugin cargado, con su origen para poder diagnosticarlo."""

    name: str
    source: str  # nombre del entry point, o la ruta local
    version: str
    tool_ids: tuple[str, ...]
    manifest: PluginManifest | None = None
    # Ports que se le inyectan. Es su superficie de riesgo, y se publica.
    ports: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadError:
    """Un plugin que no se pudo cargar. Se reporta, no se traga."""

    name: str
    source: str
    error: str


class ToolRegistry:
    """
    Colección de tools disponibles en esta instancia.

    Es la fuente única de verdad del catálogo: la UI lo consume por
    GET /api/tools y el MCP por list_tools. No hay catálogo escrito a mano.
    """

    def __init__(self, adapters: dict | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}  # id viejo -> id actual
        self._actions: dict[tuple[str, str], object] = {}  # (plugin, accion) -> handler
        self._plugins: list[LoadedPlugin] = []
        self._errors: list[LoadError] = []
        # Adapters por nombre de port. Se atan **antes** de cargar plugins: un
        # plugin que pide un port sin adapter tiene que fallar al cargar, y para
        # eso el registry ya tiene que saber con qué cuenta.
        self._adapters: dict[str, object] = dict(adapters or {})

    # ── Adapters ────────────────────────────────────────────────────────

    def bind_adapter(self, port: str, adapter: object) -> "ToolRegistry":
        """
        Ata un adapter a un port. Devuelve self, para encadenar.

        Es el único lugar donde se decide qué implementación se usa. Un plugin
        nunca elige la suya: la recibe. Cambiar de librería —urllib a requests,
        disco local a S3— es cambiar esta línea.
        """
        self._adapters[port] = adapter
        return self

    def bind_adapters(self, adapters: dict) -> "ToolRegistry":
        for port, adapter in (adapters or {}).items():
            self.bind_adapter(port, adapter)
        return self

    @property
    def adapters(self) -> dict:
        return dict(self._adapters)

    def ports_for(self, tool_id: str) -> dict:
        """
        Los adapters que le corresponden a un tool, por el manifest de su plugin.

        Un tool ve **sólo** los ports que su plugin declaró. Es lo que hace que
        la declaración no sea decorativa: un plugin que no pidió `process` no
        puede correr un comando aunque conozca el nombre del port.
        """
        cargado = self.plugin_of(tool_id)
        if cargado is None:
            return {}
        return {p: self._adapters[p] for p in cargado.ports if p in self._adapters}

    # ── Descubrimiento ──────────────────────────────────────────────────

    def discover(self) -> "ToolRegistry":
        """Carga todos los plugins instalados que declaren el entry point."""
        try:
            eps = entry_points(group=ENTRY_POINT_GROUP)
        except TypeError:  # Python <3.10
            eps = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[union-attr]

        for ep in eps:
            try:
                self._add_plugin(ep.name, ep.value, ep.load(), _module_of(ep.value))
            except Exception as exc:
                self._errors.append(LoadError(ep.name, ep.value, f"{type(exc).__name__}: {exc}"))
                log.exception("No se pudo cargar el plugin %s", ep.name)
        return self

    def register_local(self, name: str, dotted_path: str) -> "ToolRegistry":
        """
        Carga un plugin desde un módulo importable, sin instalarlo.

        dotted_path apunta al atributo con la lista de tools,
        ej. "plugins.windows:TOOLS". Es el modo de desarrollo: sin fricción
        para iterar, y sin llegar al bucket compartido.
        """
        module_name, _, attr = dotted_path.partition(":")
        try:
            module = importlib.import_module(module_name)
            self._add_plugin(name, dotted_path, getattr(module, attr or "TOOLS"), module)
        except Exception as exc:
            self._errors.append(LoadError(name, dotted_path, f"{type(exc).__name__}: {exc}"))
            log.exception("No se pudo cargar el plugin local %s", name)
        return self

    def _add_plugin(
        self, name: str, source: str, loaded: object, module: object | None = None
    ) -> None:
        # Un plugin puede exponer un Plugin (manifest + tools) o solo la lista
        # de tools. Lo segundo es el modo mínimo, sin configuración declarada.
        acciones: list = []
        if isinstance(loaded, Plugin):
            plugin_manifest: PluginManifest | None = loaded.manifest
            tools = list(loaded.tools)
            acciones = list(loaded.actions)
        else:
            plugin_manifest = None
            tools = list(loaded) if isinstance(loaded, Iterable) else [loaded]

        if plugin_manifest and plugin_manifest.contract not in SUPPORTED_CONTRACTS:
            raise TypeError(
                f"{name}: el plugin declara contrato v{plugin_manifest.contract}; "
                f"este núcleo soporta {sorted(SUPPORTED_CONTRACTS)}"
            )

        # ── Ports: se validan antes de aceptar un solo tool ─────────────
        #
        # Un plugin que pide algo que no existe no se registra a medias. Media
        # instalación funcionando es peor que ninguna: el flujo corre hasta el
        # nodo que necesitaba el port y recién ahí se cae, en producción.
        pedidos = tuple(plugin_manifest.ports) if plugin_manifest else ()
        desconocidos = [p for p in pedidos if p not in PLUGIN_PORTS]
        if desconocidos:
            self._errors.append(
                LoadError(
                    name,
                    source,
                    f"pide ports que no existen: {', '.join(desconocidos)}. "
                    f"Disponibles: {', '.join(sorted(PLUGIN_PORTS))}",
                )
            )
            return

        sin_adapter = [p for p in pedidos if p not in self._adapters]
        if sin_adapter:
            self._errors.append(
                LoadError(
                    name,
                    source,
                    f"no hay adapter atado para: {', '.join(sin_adapter)}. "
                    f"Atados: {', '.join(sorted(self._adapters)) or 'ninguno'}",
                )
            )
            return

        accepted: list[str] = []

        for tool in tools:
            manifest = getattr(tool, "manifest", None)
            if not isinstance(manifest, ToolManifest):
                raise TypeError(f"{name}: un tool no expone un ToolManifest válido")
            if manifest.contract not in SUPPORTED_CONTRACTS:
                self._errors.append(
                    LoadError(
                        name,
                        source,
                        f"{manifest.id} usa contrato v{manifest.contract}; "
                        f"este núcleo soporta {sorted(SUPPORTED_CONTRACTS)}",
                    )
                )
                continue
            if manifest.id in self._tools:
                self._errors.append(
                    LoadError(name, source, f"id duplicado: {manifest.id} ya estaba registrado")
                )
                continue
            self._tools[manifest.id] = tool  # type: ignore[assignment]
            accepted.append(manifest.id)

            for alias in manifest.aliases:
                if alias in self._tools or alias in self._aliases:
                    self._errors.append(
                        LoadError(name, source, f"alias en conflicto: {alias} ya está en uso")
                    )
                    continue
                self._aliases[alias] = manifest.id

        for handler in acciones:
            accion = getattr(handler, "action", None)
            if not isinstance(accion, Action):
                self._errors.append(
                    LoadError(name, source, "una acción no expone un Action válido")
                )
                continue
            if plugin_manifest and plugin_manifest.action(accion.name) is None:
                # El manifest es lo que se publica; un handler sin declaración
                # sería una acción invisible en el catálogo y ejecutable igual.
                self._errors.append(
                    LoadError(
                        name,
                        source,
                        f"la acción '{accion.name}' no está declarada en el manifest",
                    )
                )
                continue
            self._actions[(name, accion.name)] = handler

        self._plugins.append(
            LoadedPlugin(
                name=name,
                source=source,
                version=(plugin_manifest.version if plugin_manifest else _version_of(module)),
                tool_ids=tuple(accepted),
                manifest=plugin_manifest,
                ports=pedidos,
            )
        )

    # ── Consulta ────────────────────────────────────────────────────────

    def resolve_id(self, tool_id: str) -> str:
        """Id actual de un tool, siguiendo los alias de renombres anteriores."""
        return self._aliases.get(tool_id, tool_id)

    def get(self, tool_id: str) -> Tool | None:
        return self._tools.get(self.resolve_id(tool_id))

    def manifest(self, tool_id: str) -> ToolManifest | None:
        tool = self.get(tool_id)
        return tool.manifest if tool else None

    @property
    def tool_ids(self) -> list[str]:
        return sorted(self._tools)

    @property
    def manifests(self) -> list[ToolManifest]:
        return sorted((t.manifest for t in self._tools.values()), key=lambda m: m.id)

    @property
    def plugins(self) -> list[LoadedPlugin]:
        return list(self._plugins)

    @property
    def errors(self) -> list[LoadError]:
        """Plugins que fallaron al cargar. La UI los muestra; no se ocultan."""
        return list(self._errors)

    def plugin_of(self, tool_id: str) -> LoadedPlugin | None:
        """Plugin que aporta un tool — para saber qué settings le aplican."""
        actual = self.resolve_id(tool_id)
        return next((p for p in self._plugins if actual in p.tool_ids), None)

    def missing_config(self, config: dict, tool_ids: list[str] | None = None) -> dict[str, list[MissingSetting]]:
        """
        Settings obligatorios que faltan, por plugin.

        Con tool_ids se limita a los plugins que esos tools necesitan: es lo que
        permite que un dry-run de un flujo diga "falta configurar X" antes de
        ejecutar nada, en lugar de morir a mitad de camino.
        """
        if tool_ids is None:
            relevantes = [p for p in self._plugins if p.manifest]
        else:
            nombres = {p.name for tid in tool_ids if (p := self.plugin_of(tid))}
            relevantes = [p for p in self._plugins if p.manifest and p.name in nombres]

        faltantes = {}
        for p in relevantes:
            if pendientes := p.manifest.validate_config(config):
                faltantes[p.name] = pendientes
        return faltantes

    def defaults(self) -> dict:
        """Los defaults declarados por todos los manifests instalados."""
        valores: dict = {}
        for p in self._plugins:
            if p.manifest:
                valores.update(p.manifest.defaults())
        return valores

    def secret_setting_keys(self) -> set[str]:
        """
        Los `key` de Setting que algún plugin instalado declaró `secret`.

        Es lo que le permite a `ConfigStore` cifrar en reposo (issue #8) sin
        dejar de ser genérico: no conoce el contrato de `Setting`, sólo
        recibe esta lista de nombres desde quien sí lo conoce.
        """
        return {
            s.key
            for p in self._plugins
            if p.manifest
            for s in p.manifest.settings
            if s.secret
        }

    def effective_config(self, config: dict) -> dict:
        """
        La configuración como la ve el sistema: defaults del manifest, pisados
        por lo que el usuario guardó.

        Sin esto, un setting con default valía distinto según quién lo leyera:
        el tool le aplicaba el default al usarlo, pero el núcleo lo veía vacío
        al guardar un resource — así que un item no se podía crear hasta
        escribir a mano un valor que ya existía en el manifest.
        """
        return {**self.defaults(), **config}

    def catalog(self) -> dict:
        """
        El catálogo completo: lo único que una UI, un MCP o un CLI necesitan
        para dibujarse solos.

        Incluye tools, settings, colecciones, acciones y **los ports que usa
        cada plugin**. Un frontend puede renderizar la configuración entera, el
        ABM de cada colección y los botones de acción sin saber qué plugins
        existen ni tener una línea escrita por integración.

        Los errores de carga viajan acá adentro a propósito: un plugin que no
        cargó tiene que ser visible en la misma pantalla donde se lo esperaba,
        no en un log que nadie abre.
        """
        return {
            "contract": CONTRACT_VERSION,
            "ports": sorted(self._adapters),
            "tools": [m.to_dict() for m in self.manifests],
            "plugins": [
                {
                    "name": p.name,
                    "source": p.source,
                    "version": p.version,
                    "tools": list(p.tool_ids),
                    "ports": list(p.ports),
                    **(
                        {
                            "label": p.manifest.label,
                            "settings": [s.to_dict() for s in p.manifest.settings],
                            "resources": [r.to_dict() for r in p.manifest.resources],
                            "actions": [a.to_dict() for a in p.manifest.actions],
                            "doc": p.manifest.doc,
                        }
                        if p.manifest
                        else {
                            "label": p.name,
                            "settings": [],
                            "resources": [],
                            "actions": [],
                            "doc": "",
                        }
                    ),
                }
                for p in self._plugins
            ],
            "errors": [{"name": e.name, "source": e.source, "error": e.error} for e in self._errors],
        }

    # ── Ejecución ───────────────────────────────────────────────────────

    def execute(self, tool_id: str, ctx_factory) -> ToolResult:
        """
        Ejecuta un tool con aislamiento de fallos.

        Toda salida pasa por acá, y toda salida es un ToolResult con status
        explícito. Es la garantía estructural de que un tool no puede fallar
        en silencio: ni una excepción, ni un tool inexistente, ni un param
        mal tipado avanzan como si hubiera salido bien.

        ctx_factory recibe el manifest y los ports que le corresponden a ese
        tool, y devuelve el ToolContext. Recibe las dos cosas porque el núcleo
        tiene que resolver los params contra el manifest antes de construirlo, y
        porque los ports dependen de qué plugin es dueño del tool.
        """
        tool = self.get(tool_id)
        if tool is None:
            disponibles = ", ".join(sorted(self._tools)[:5])
            return ToolResult.err(
                f"Tool desconocido: '{tool_id}'. ¿Falta instalar el plugin? "
                f"Registrados: {disponibles}…"
            )

        try:
            ctx = ctx_factory(tool.manifest, self.ports_for(tool_id))
        except ParamError as exc:
            return ToolResult.err(str(exc))
        except Exception as exc:
            return ToolResult.from_exception(exc)

        try:
            result = tool.run(ctx)
        except Exception as exc:
            return ToolResult.from_exception(exc)

        if not isinstance(result, ToolResult):
            return ToolResult.err(
                f"{tool_id} devolvió {type(result).__name__} en lugar de un ToolResult"
            )
        if result.status not in tool.manifest.statuses:
            return ToolResult.err(
                f"{tool_id} devolvió status '{result.status}', "
                f"no declarado en su manifest ({', '.join(tool.manifest.statuses)})"
            )
        return result

    def actions_of(self, plugin: str) -> list[Action]:
        """Las acciones declaradas por un plugin."""
        cargado = next((p for p in self._plugins if p.name == plugin), None)
        if cargado is None or cargado.manifest is None:
            return []
        return list(cargado.manifest.actions)

    def execute_action(self, plugin: str, action: str, ctx_factory) -> ToolResult:
        """
        Ejecuta una acción de un plugin, con el mismo aislamiento que un tool.

        Comparte camino con `execute` a propósito: una acción también puede
        reventar, y también tiene que volver como un ToolResult con status
        explícito. Que el disparador sea una persona y no un flujo no cambia la
        garantía de que nada falla en silencio.
        """
        handler = self._actions.get((plugin, action))
        if handler is None:
            disponibles = ", ".join(a.name for a in self.actions_of(plugin)) or "ninguna"
            return ToolResult.err(
                f"El plugin '{plugin}' no tiene la acción '{action}'. "
                f"Disponibles: {disponibles}"
            )

        cargado = next((p for p in self._plugins if p.name == plugin), None)
        ports = {p: self._adapters[p] for p in (cargado.ports if cargado else ()) if p in self._adapters}

        try:
            ctx = ctx_factory(handler.action, ports)
        except ParamError as exc:
            return ToolResult.err(str(exc))
        except Exception as exc:
            return ToolResult.from_exception(exc)

        try:
            result = handler.run(ctx)
        except Exception as exc:
            return ToolResult.from_exception(exc)

        if not isinstance(result, ToolResult):
            return ToolResult.err(
                f"la acción {plugin}.{action} devolvió {type(result).__name__} "
                f"en lugar de un ToolResult"
            )
        return result


def build_default_registry(
    adapters: dict | None = None,
    local_plugins: dict[str, str] | None = None,
) -> ToolRegistry:
    """
    Registro de arranque: adapters atados, después plugins.

    El orden importa y no es negociable: los adapters primero, porque la carga
    de un plugin valida contra ellos. Descubrir plugins con el registry vacío de
    adapters los rechazaría a todos.
    """
    registry = ToolRegistry(adapters=adapters)
    for name, path in (local_plugins or {}).items():
        registry.register_local(name, path)
    registry.discover()
    return registry
