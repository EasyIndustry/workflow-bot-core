"""
Contrato de plugins — versión 1.

Define la frontera entre el núcleo y los plugins. El núcleo no conoce ninguna
integración, ningún proveedor y ningún caso de uso: solo conoce este contrato.

Reglas del contrato:

1. Un tool declara un ToolManifest: qué params acepta (con tipo, si es obligatorio,
   y de qué clave de configuración toma su default) y qué outputs escribe.
2. Un tool implementa run(ctx) -> ToolResult. Recibe params ya resueltos y
   convertidos de tipo; devuelve outputs. No escribe estado global.
3. El núcleo es el único que resuelve variables, mergea outputs al contexto del
   run, y decide la arista siguiente. Un tool nunca toca el grafo.
4. Una excepción no capturada dentro de un tool se convierte en status "err"
   con su traceback en la traza. Un tool no puede fallar en silencio.

5. Un plugin declara qué ports necesita y los recibe ya resueltos. Nunca elige
   ni instancia un adapter, ni importa una librería externa.

El manifest es serializable a JSON: quien construya una UI o un servidor MCP
consume ese catálogo en vez de mantener una lista propia.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

CONTRACT_VERSION = 1

# Statuses que el núcleo entiende al resolver aristas del flujo.
STATUS_OK = "ok"
STATUS_ERR = "err"


class ParamType(str, Enum):
    """Tipos soportados en params y outputs."""

    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    PATH = "path"
    ENUM = "enum"
    JSON = "json"  # estructuras (listas de patrones, reglas de renombre, etc.)


class ParamError(ValueError):
    """Param obligatorio ausente, o valor que no convierte al tipo declarado."""


class PortNotDeclared(RuntimeError):
    """Un tool pidió un port que su plugin no declaró en el manifest."""


_TRUE = {"1", "true", "t", "yes", "si", "sí", "on"}
_FALSE = {"0", "false", "f", "no", "off", ""}


def _coerce(name: str, raw: Any, ptype: ParamType, choices: Sequence[str]) -> Any:
    """Convierte un valor crudo (casi siempre str, viene del .mmd) al tipo declarado."""
    if raw is None:
        return None

    if ptype in (ParamType.STR, ParamType.PATH):
        return str(raw).strip() if ptype is ParamType.PATH else str(raw)

    if ptype is ParamType.ENUM:
        val = str(raw).strip()
        if choices and val not in choices:
            raise ParamError(
                f"{name}: '{val}' no es un valor válido. Esperado uno de: {', '.join(choices)}"
            )
        return val

    if ptype is ParamType.INT:
        try:
            return int(str(raw).strip())
        except ValueError:
            raise ParamError(f"{name}: '{raw}' no es un entero") from None

    if ptype is ParamType.FLOAT:
        try:
            return float(str(raw).strip())
        except ValueError:
            raise ParamError(f"{name}: '{raw}' no es un número") from None

    if ptype is ParamType.BOOL:
        if isinstance(raw, bool):
            return raw
        val = str(raw).strip().lower()
        if val in _TRUE:
            return True
        if val in _FALSE:
            return False
        raise ParamError(f"{name}: '{raw}' no es un booleano")

    # JSON: ya viene estructurado desde el store de configuración.
    return raw


@dataclass(frozen=True)
class Param:
    """
    Un parámetro de entrada de un tool.

    config_key implementa la regla de la spec: el param del nodo es un override
    y la configuración de la instancia es el default. Si el nodo no lo trae, el
    núcleo lo busca en la config bajo esa clave antes de caer al default.
    """

    name: str
    type: ParamType = ParamType.STR
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    config_key: str | None = None
    # Nombres anteriores del parámetro, para renombrar sin romper flujos.
    aliases: tuple[str, ...] = ()
    doc: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type.value,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
            "config_key": self.config_key,
            "aliases": list(self.aliases),
            "doc": self.doc,
        }

    def read_from(self, node_params: dict) -> Any:
        """Valor del param en un nodo, aceptando también sus nombres anteriores."""
        if self.name in node_params:
            return node_params[self.name]
        for alias in self.aliases:
            if alias in node_params:
                return node_params[alias]
        return None

    def present_in(self, node_params: dict) -> bool:
        return self.name in node_params or any(a in node_params for a in self.aliases)


@dataclass(frozen=True)
class Output:
    """
    Una variable que el tool escribe al contexto del run.

    Declararlas es lo que permite validar un workflow antes de ejecutarlo: el
    núcleo puede verificar que {checkLogResult.log_file} referencie un output
    que algún nodo anterior realmente produce.
    """

    name: str
    type: ParamType = ParamType.STR
    doc: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type.value, "doc": self.doc}


@dataclass(frozen=True)
class ToolManifest:
    """Descripción declarativa de un tool. Fuente única de verdad."""

    id: str  # "archivos.mover" — namespace.accion
    label: str
    category: str
    # Ids anteriores que siguen resolviendo a este tool. Permite renombrar sin
    # romper los flujos ya escritos, propios o de terceros.
    aliases: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    outputs: tuple[Output, ...] = ()
    statuses: tuple[str, ...] = (STATUS_OK, STATUS_ERR)
    doc: str = ""
    dangerous: bool = False  # destructivo: el núcleo lo audita y puede exigir confirmación
    # "concurrent" (default, sin restricción) | "exclusive_run": mientras un run
    # que use este tool esté en curso, ningún otro run puede estar en vuelo.
    # Quien orquesta arranques (la webapp) decide qué hacer con esto; el core
    # sólo carga la declaración y la ofrece para consulta, igual que `dangerous`.
    concurrency: str = "concurrent"
    # Algunos tools tienen params abiertos por diseño: un tool que ejecuta un
    # request guardado recibe los campos de ese payload, que dependen de cuál
    # sea. Los no declarados llegan por ToolContext.extras, no se descartan.
    extra_params: bool = False
    extra_params_doc: str = ""
    # Idem para los outputs: un tool que vuelca al contexto lo que devolvió una
    # API no conoce esos campos de antemano. Sin declararlo, un validador
    # marcaría como error toda variable que venga de ahí.
    extra_outputs: bool = False
    extra_outputs_doc: str = ""
    contract: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if "." not in self.id:
            raise ValueError(f"id de tool inválido: '{self.id}' (esperado namespace.accion)")

    @property
    def namespace(self) -> str:
        return self.id.split(".", 1)[0]

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "aliases": list(self.aliases),
            "params": [p.to_dict() for p in self.params],
            "outputs": [o.to_dict() for o in self.outputs],
            "statuses": list(self.statuses),
            "doc": self.doc,
            "dangerous": self.dangerous,
            "concurrency": self.concurrency,
            "extra_params": self.extra_params,
            "extra_params_doc": self.extra_params_doc,
            # Sin estos dos, un validador que consuma el catálogo marcaría como
            # error toda variable que venga de un tool con outputs abiertos —
            # que es exactamente lo que el flag existe para evitar.
            "extra_outputs": self.extra_outputs,
            "extra_outputs_doc": self.extra_outputs_doc,
            "contract": self.contract,
        }

    def split_params(self, node_params: dict, config: dict) -> tuple[dict, dict]:
        """
        Separa los params declarados de los abiertos.

        Devuelve (declarados, extras). Si el tool no declara extra_params, los
        no declarados se descartan. Es la garantía de que no hay contaminación
        entre nodos: un param sobrante no se filtra al tool siguiente.
        """
        declarados = self.resolve_params(node_params, config)
        if not self.extra_params:
            return declarados, {}

        conocidos = {p.name for p in self.params}
        conocidos.update(a for p in self.params for a in p.aliases)
        extras = {k: v for k, v in node_params.items() if k not in conocidos}
        return declarados, extras

    def resolve_params(self, node_params: dict, config: dict) -> dict:
        """
        Aplica el orden de precedencia del contrato y convierte tipos:
            param del nodo  →  config de la instancia  →  default del manifest

        Levanta ParamError si falta un obligatorio o si un valor no convierte.
        Los params no declarados en el manifest se descartan: sin eso, un param
        sobrante de un nodo se filtraría al siguiente.
        """
        resolved: dict = {}
        for p in self.params:
            if p.present_in(node_params):
                raw = p.read_from(node_params)
            elif p.config_key and p.config_key in config:
                raw = config[p.config_key]
            else:
                raw = p.default

            if raw is None or (isinstance(raw, str) and not raw.strip() and p.type is not ParamType.STR):
                if p.required:
                    where = f" (o configurá {p.config_key})" if p.config_key else ""
                    raise ParamError(f"{self.id}: falta el parámetro obligatorio '{p.name}'{where}")
                resolved[p.name] = p.default
                continue

            resolved[p.name] = _coerce(p.name, raw, p.type, p.choices)
        return resolved


@dataclass(frozen=True)
class Setting:
    """
    Un valor de configuración que el plugin necesita de la instancia.

    Declarado acá, la pantalla de configuración se renderiza sola desde el
    catálogo y el instalador sabe qué pedir cuando alguien instala el plugin.
    Nadie escribe un formulario a mano por integración.

    A diferencia de Param (que es por nodo del flujo), un Setting es por
    instalación y lo comparten todos los tools del plugin.
    """

    key: str  # "folderSearchPath"
    type: ParamType = ParamType.STR
    label: str = ""
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    secret: bool = False  # nunca se loguea ni se devuelve en claro
    group: str = ""  # agrupa campos dentro del panel del plugin
    doc: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "type": self.type.value,
            "label": self.label or self.key,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
            "secret": self.secret,
            "group": self.group,
            "doc": self.doc,
        }


@dataclass(frozen=True)
class Field:
    """Un campo de un item de Resource. Igual que Setting, pero por item."""

    name: str
    type: ParamType = ParamType.STR
    label: str = ""
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    secret: bool = False
    multiline: bool = False  # pista de render: textarea en vez de input
    doc: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type.value,
            "label": self.label or self.name,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
            "secret": self.secret,
            "multiline": self.multiline,
            "doc": self.doc,
        }


@dataclass(frozen=True)
class Resource:
    """
    Una colección de items que el plugin administra, con su propia pantalla.

    Es la respuesta a "cómo un plugin aporta UI" en su forma más barata: no
    trae HTML, declara el esquema de sus items y el núcleo renderiza un ABM
    genérico. Un plugin instalado obtiene su pantalla sin que nadie escriba una
    vista, y sin que el núcleo tenga que ejecutar código de UI de un tercero.

    **No declara dónde se guarda.** El núcleo lo persiste y el plugin lo lee por
    nombre con `ctx.resource(...)`. Que un plugin supiera —y eligiera— dónde
    vivían sus datos es exactamente lo que impedía cambiar el backend de
    almacenamiento sin tocar todos los plugins.
    """

    name: str  # "connections" — id en la URL
    label: str  # "Conexiones" — título de la pantalla
    item_label: str = ""  # "Conexión" — singular, para los botones
    fields: tuple[Field, ...] = ()
    key_field: str = "name"  # campo que identifica al item
    doc: str = ""

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    def validate_item(self, item: dict) -> list[str]:
        """Errores de un item contra el esquema. Lo usa el ABM antes de guardar."""
        errores = []
        for f in self.fields:
            raw = item.get(f.name, f.default)
            vacio = raw is None or (isinstance(raw, str) and not raw.strip())
            if vacio:
                if f.required:
                    errores.append(f"{f.label or f.name}: es obligatorio")
                continue
            try:
                _coerce(f.name, raw, f.type, f.choices)
            except ParamError as exc:
                errores.append(str(exc))
        return errores

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "item_label": self.item_label or self.label,
            "fields": [f.to_dict() for f in self.fields],
            "key_field": self.key_field,
            "doc": self.doc,
        }


@dataclass(frozen=True)
class MissingSetting:
    """Un setting obligatorio que falta o no convierte. Lo reporta el wizard."""

    key: str
    label: str
    reason: str

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "reason": self.reason}


@dataclass(frozen=True)
class Action:
    """
    Una operación suelta del plugin, disparada por una persona.

    Es el nivel 2 de la UI de un plugin: cuando el esquema (`Setting`,
    `Resource.fields`) no alcanza porque hace falta *hacer* algo —probar una
    conexión, previsualizar filas, listar lo que hay del otro lado— pero
    escribir una pantalla propia sería desproporcionado. El núcleo dibuja botón,
    formulario y resultado desde esta declaración.

    No es un tool: un tool es un nodo de un flujo y se puede escribir en un
    `.mmd`. Una acción no entra en un flujo, la ejecuta alguien mirando la
    pantalla. Mezclarlas llenaría el autocompletado del editor de flujos de
    cosas que ahí no sirven.
    """

    name: str  # "test" — id en la URL
    label: str  # "Probar conexión"
    params: tuple[Param, ...] = ()
    doc: str = ""
    dangerous: bool = False
    # Colección sobre cuyo item corre la acción, si aplica: el botón aparece en
    # la fila del ABM en vez de suelto en la pantalla del plugin.
    resource: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "params": [p.to_dict() for p in self.params],
            "doc": self.doc,
            "dangerous": self.dangerous,
            "resource": self.resource,
        }


@dataclass(frozen=True)
class PluginManifest:
    """
    Identidad y configuración de un plugin.

    Lo que el instalador necesita saber para pedirle datos al usuario sin que
    nadie escriba una pantalla a mano.
    """

    name: str  # "archivos"
    label: str  # "Archivos"
    version: str = "0.0.0"
    settings: tuple[Setting, ...] = ()
    # Colecciones con pantalla propia, renderizada por el núcleo desde el esquema.
    resources: tuple[Resource, ...] = ()
    # Acciones sueltas, con botón y formulario dibujados por el núcleo.
    actions: tuple[Action, ...] = ()
    # Ports que este plugin necesita, por nombre ("http", "fs", "process",
    # "clock"). El núcleo inyecta **sólo** estos, y falla al cargar el plugin si
    # pide uno que no existe o para el que no hay adapter. Declararlo hace dos
    # cosas: permite validar al arrancar en vez de a mitad de un run, y publica
    # en el catálogo la superficie de riesgo real de cada plugin — quién puede
    # salir a la red, quién puede tocar el disco, quién puede correr comandos.
    ports: tuple[str, ...] = ()
    doc: str = ""
    contract: int = CONTRACT_VERSION

    def setting(self, key: str) -> Setting | None:
        return next((s for s in self.settings if s.key == key), None)

    def resource(self, name: str) -> Resource | None:
        return next((r for r in self.resources if r.name == name), None)

    def action(self, name: str) -> Action | None:
        return next((a for a in self.actions if a.name == name), None)

    def validate_config(self, config: dict) -> list[MissingSetting]:
        """
        Qué le falta a esta instalación para poder usar el plugin.

        El wizard lo llama al instalar; el dry-run lo llama antes de ejecutar,
        para fallar con "falta configurar X" en lugar de a mitad de un flujo.
        """
        faltantes: list[MissingSetting] = []
        for s in self.settings:
            raw = config.get(s.key, s.default)
            vacio = raw is None or (isinstance(raw, str) and not raw.strip())
            if vacio:
                if s.required:
                    faltantes.append(
                        MissingSetting(s.key, s.label or s.key, "sin valor configurado")
                    )
                continue
            try:
                _coerce(s.key, raw, s.type, s.choices)
            except ParamError as exc:
                faltantes.append(MissingSetting(s.key, s.label or s.key, str(exc)))
        return faltantes

    def defaults(self) -> dict:
        """Configuración inicial que el wizard puede precargar."""
        return {s.key: s.default for s in self.settings if s.default is not None}

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "version": self.version,
            "settings": [s.to_dict() for s in self.settings],
            "resources": [r.to_dict() for r in self.resources],
            "actions": [a.to_dict() for a in self.actions],
            "ports": list(self.ports),
            "doc": self.doc,
            "contract": self.contract,
        }


@dataclass
class Plugin:
    """
    Lo que un plugin expone por su entry point.

        MANIFEST = PluginManifest(name="fs", label="Archivos", ports=("fs",))
        PLUGIN = Plugin(manifest=MANIFEST, tools=build_tools())

    `actions` son las acciones sueltas del plugin —"probar conexión",
    "previsualizar"— que no son nodos de un flujo. Van acá y no en `tools`
    porque no se pueden poner en un `.mmd`: las dispara una persona desde la
    pantalla del plugin.
    """

    manifest: PluginManifest
    tools: list = field(default_factory=list)
    actions: list = field(default_factory=list)

    def __iter__(self):
        """Permite tratarlo como la lista de tools, para compatibilidad."""
        return iter(self.tools)


@dataclass
class ToolResult:
    """
    Resultado de ejecutar un tool.

    outputs se mergea al contexto del run bajo los nombres declarados en el
    manifest. loop activa la arista |loop| del flujo (reemplaza el flag global
    loopNext que antes vivía en el estado del navegador).

    error_kind clasifica un error para quien dispara el run y no puede ver el
    `message` de texto libre (issue #4): "red", "sesion_vencida", lo que el
    tool quiera. String libre y no un enum cerrado a propósito — cada plugin
    conoce sus propias causas de falla, y una taxonomía impuesta desde acá
    envejecería mal. `None` es "sin clasificar", no "sin error": un `status`
    err con `error_kind=None` sigue siendo un error.
    """

    status: str = STATUS_OK
    outputs: dict = field(default_factory=dict)
    message: str = ""
    loop: bool = False
    traceback: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.status != STATUS_OK

    @classmethod
    def ok(cls, message: str = "", **outputs: Any) -> "ToolResult":
        return cls(status=STATUS_OK, outputs=outputs, message=message)

    @classmethod
    def err(cls, message: str, *, error_kind: str | None = None, **outputs: Any) -> "ToolResult":
        return cls(status=STATUS_ERR, outputs=outputs, message=message, error_kind=error_kind)

    @classmethod
    def again(cls, message: str = "", **outputs: Any) -> "ToolResult":
        """Éxito parcial: el flujo debe tomar la arista |loop| y reintentar."""
        return cls(status=STATUS_OK, outputs=outputs, message=message, loop=True)

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ToolResult":
        """Una excepción siempre es un error explícito, nunca un fallo silencioso."""
        return cls(
            status=STATUS_ERR,
            message=f"{type(exc).__name__}: {exc}",
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "outputs": self.outputs,
            "message": self.message,
            "loop": self.loop,
            "traceback": self.traceback,
            "error_kind": self.error_kind,
        }


class ToolContext:
    """
    Lo único que un tool ve del núcleo.

    Deliberadamente angosto: params ya resueltos, config de solo lectura, las
    colecciones que el propio plugin declaró, los ports que pidió en su
    manifest, un logger que escribe a la traza del run, y un chequeo de
    cancelación. No hay acceso al grafo, ni al estado de otros casos, ni a
    estado global, ni a la base.

    Las colecciones se leen, no se escriben: el ABM es del núcleo. Un tool que
    pudiera reescribir sus propias conexiones en medio de un run cambiaría la
    configuración de la instalación sin que nadie lo pidiera ni lo viera.

    Los ports llegan **ya resueltos**: un plugin nunca elige ni instancia su
    adapter. Es lo que permite mockear el mundo entero en un test y cambiar de
    librería sin abrir un solo plugin.
    """

    def __init__(
        self,
        *,
        run_id: str,
        case_id: str,
        params: dict,
        config: dict,
        context: dict,
        log: Callable[[str, str], None],
        is_cancelled: Callable[[], bool] = lambda: False,
        extras: dict | None = None,
        resources: Callable[[str], list[dict]] | None = None,
        ports: dict | None = None,
    ) -> None:
        self.run_id = run_id
        self.case_id = case_id
        self.params = params
        # Params no declarados, solo si el manifest los habilita.
        self.extras = dict(extras or {})
        self._config = dict(config)
        self._context = dict(context)
        self._log = log
        self._is_cancelled = is_cancelled
        self._resources = resources or (lambda _collection: [])
        # Sólo los que el manifest declaró: el registry ya filtró.
        self._ports = dict(ports or {})

    def config(self, key: str, default: Any = None) -> Any:
        """Configuración de la instancia (server-side, no del navegador)."""
        return self._config.get(key, default)

    def var(self, name: str, default: Any = None) -> Any:
        """Variable del contexto del run: campo del row u output de un nodo anterior."""
        return self._context.get(name, default)

    def log(self, message: str, level: str = "info") -> None:
        self._log(message, level)

    # ── Ports ───────────────────────────────────────────────────────────

    def port(self, name: str):
        """
        El adapter de un port, ya resuelto e inyectado.

            respuesta = ctx.port("http").request(url, method="POST", body=cuerpo)

        Pedir un port que el manifest no declara levanta `PortNotDeclared`. Es
        deliberado y no una molestia: si alcanzara con pedirlo en runtime, la
        declaración sería decorativa y el catálogo mentiría sobre qué puede
        hacer cada plugin.
        """
        adapter = self._ports.get(name)
        if adapter is None:
            declarados = ", ".join(sorted(self._ports)) or "ninguno"
            raise PortNotDeclared(
                f"el plugin no declaró el port '{name}' en su manifest "
                f"(declara: {declarados})"
            )
        return adapter

    @property
    def ports(self) -> tuple[str, ...]:
        """Los ports disponibles en este contexto."""
        return tuple(sorted(self._ports))

    # ── Colecciones del propio plugin ───────────────────────────────────

    def resources(self, collection: str) -> list[dict]:
        """Todos los items de una colección que declaró este plugin."""
        return self._resources(collection)

    def resource(self, collection: str, key: str, key_field: str = "name") -> dict | None:
        """Un item por su clave, o None. El plugin no sabe dónde está guardado."""
        objetivo = (key or "").strip()
        return next(
            (i for i in self._resources(collection) if str(i.get(key_field, "")) == objetivo),
            None,
        )

    def resource_keys(self, collection: str, key_field: str = "name") -> list[str]:
        """Las claves disponibles, para poder decir "no existe X, hay Y y Z"."""
        return [str(i.get(key_field, "")) for i in self._resources(collection)]

    @property
    def cancelled(self) -> bool:
        """Los tools de larga duración deben chequearlo y devolver err si es True."""
        return self._is_cancelled()

    @property
    def is_cancelled_check(self) -> Callable[[], bool]:
        """
        El chequeo en sí, para pasárselo a un port que bloquea.

        `ctx.port("clock").sleep(60, ctx.is_cancelled_check)` despierta a mirar
        si cancelaron. Sin esto, un tool que espera sólo puede chequear antes y
        después, y cancelar durante la espera no surte efecto hasta que termina.
        """
        return self._is_cancelled


@runtime_checkable
class Tool(Protocol):
    """
    Un tool es cualquier objeto con un manifest y un run.

    Los plugins exponen una lista de instancias de Tool vía su entry point. El
    núcleo nunca los importa por nombre.
    """

    manifest: ToolManifest

    def run(self, ctx: ToolContext) -> ToolResult: ...


@dataclass
class FunctionTool:
    """
    Adaptador para escribir un tool como una función suelta.

        MOVER = FunctionTool(manifest=..., fn=lambda ctx: ToolResult.ok(...))
    """

    manifest: ToolManifest
    fn: Callable[[ToolContext], ToolResult]

    def run(self, ctx: ToolContext) -> ToolResult:
        return self.fn(ctx)


@runtime_checkable
class ActionHandler(Protocol):
    """Una acción ejecutable: su declaración y qué hace."""

    action: Action

    def run(self, ctx: ToolContext) -> ToolResult: ...


@dataclass
class FunctionAction:
    """
    Adaptador para escribir una acción como una función suelta.

        PROBAR = FunctionAction(
            action=Action("test", "Probar conexión"),
            fn=lambda ctx: ToolResult.ok("respondió 200"),
        )

    Recibe el mismo `ToolContext` que un tool, con `run_id` y `case_id` vacíos:
    una acción no corre dentro de un run. Reusar el contexto y no inventar uno
    paralelo es deliberado — un plugin no tiene que aprender dos formas de leer
    su configuración, sus colecciones y sus ports.
    """

    action: Action
    fn: Callable[[ToolContext], ToolResult]

    def run(self, ctx: ToolContext) -> ToolResult:
        return self.fn(ctx)
