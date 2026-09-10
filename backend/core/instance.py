"""
La instalación como un objeto.

Junta almacenamiento, adapters, registro de plugins, configuración, workflows y
runs. Es el **único** lugar donde se decide con qué implementación concreta
corre esta máquina: qué base, qué cliente HTTP, qué filesystem, qué reloj.

Todo lo que se construya encima —un servidor HTTP, un MCP, la CLI, un test—
construye este mismo `Instance` y ve exactamente lo mismo. Es deliberado: el
modo de falla histórico de este repo fue tener dos motores y tres frontends
divergiendo en silencio, y la arquitectura está diseñada contra eso.

Orden de armado, que no es negociable:

    almacenamiento → adapters → registry (valida ports) → plugins

Un plugin que pide un port sin adapter tiene que fallar **al cargar**. Si los
plugins se descubrieran antes de atar los adapters, todos serían rechazados.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import boot as bootstrap
from .config import ConfigStore
from .env_store import EnvStore, referencias
from .flow.executor import NATIVE_FNS, RunResult, execute_flow
from .flow.parser import Diagnostic, FlowGraph, Severity, parse_flow
from .jsonio import dumps
from .log_store import MODO_EXPIRACION, LogStore
from .ports import CryptoPort, StoragePort
from .registry import ToolRegistry
from .resources import TableStore, store_for
from .schema import MIGRATIONS, SCHEMA
from .stores import RunStore, StoreError, WorkflowStore
from .users import RunPolicy, UserError, UserStore


class WorkflowNotFound(StoreError):
    pass


class Instance:
    """Todo lo que hace falta para ejecutar y diagnosticar en esta máquina."""

    def __init__(
        self,
        root: Path | str,
        *,
        storage: StoragePort | None = None,
        adapters: dict | None = None,
        crypto: CryptoPort | None = None,
        local_plugins: dict[str, str] | None = None,
        boot: bootstrap.BootConfig | None = None,
    ) -> None:
        """
        Todo lo concreto se puede inyectar. Es lo que permite que un test corra
        contra una base en memoria y adapters falsos sin tocar red, disco ni
        reloj — y sin que el núcleo tenga una rama "modo test".

        `boot` trae lo que hace falta saber **antes** de construir nada: dónde
        está la base, qué carpeta de plugins mirar, y con qué límites se atan
        los adapters. Si no viene, se lee de `<root>/boot.env` y del entorno.
        """
        self.root = Path(root)
        self.boot = boot if boot is not None else bootstrap.load(self.root)
        self.data_dir = self.boot.data_dir

        self.db = storage if storage is not None else _default_storage(self.boot)
        self.db.migrate(SCHEMA, MIGRATIONS)

        self.config = ConfigStore(self.db)
        self.workflows = WorkflowStore(self.db)
        self.runs = RunStore(self.db)
        self.logs = LogStore(self.db)

        self.users = UserStore(self.db)
        self.adapters = (
            dict(adapters) if adapters is not None else _default_adapters(self.boot)
        )
        # `crypto` y `storage` son del núcleo: no se le pasan a ningún plugin,
        # así que se resuelven acá y no entran al registry.
        self.crypto = crypto if crypto is not None else _default_crypto(self.data_dir)
        self.env = EnvStore(self.db, self.crypto)

        self.registry = _build_registry(
            self.adapters, {**self._plugins_de_la_carpeta(), **(local_plugins or {})}
        )

    def _plugins_de_la_carpeta(self) -> dict[str, str]:
        """
        Los `.py` y paquetes de la carpeta de plugins de desarrollo.

        Es una comodidad de autoría: sin esto hay que nombrar cada plugin en
        cada invocación. Es **opt-in** —sólo si `plugins_dir` está declarado— y
        el catálogo publica de dónde salió cada uno, porque una carpeta que
        ejecuta cualquier archivo que le caiga adentro no puede ser silenciosa.

        En producción no se usa: ahí los plugins entran por entry point, que es
        explícito, versionado y auditable.
        """
        carpeta = self.boot.plugins_dir
        if carpeta is None or not carpeta.is_dir():
            return {}

        if str(carpeta) not in sys.path:
            sys.path.insert(0, str(carpeta))

        encontrados: dict[str, str] = {}
        for entrada in sorted(carpeta.iterdir()):
            if entrada.name.startswith((".", "_")):
                continue
            if entrada.is_file() and entrada.suffix == ".py":
                encontrados[entrada.stem] = f"{entrada.stem}:PLUGIN"
            elif entrada.is_dir() and (entrada / "__init__.py").is_file():
                encontrados[entrada.name] = f"{entrada.name}:PLUGIN"
        return encontrados

    # ── Workflows ───────────────────────────────────────────────────────

    def load_workflow(self, name: str) -> str:
        wf = self.workflows.get(name)
        if wf is None:
            raise WorkflowNotFound(f'No existe el flujo "{name}"')
        return wf.content

    def list_workflows(self) -> list[dict]:
        return [w.to_dict(with_content=False) for w in self.workflows.list()]

    def parse(self, name: str) -> FlowGraph:
        return parse_flow(self.load_workflow(name), with_meta=False)

    def diagnose(self, name: str) -> tuple[FlowGraph, list[Diagnostic]]:
        """
        El grafo con sus diagnósticos **más** los que dependen de qué hay
        instalado.

        El parser es puro: no conoce el registro, así que un flujo que llama a
        un tool inexistente parsea sin una queja y recién falla al ejecutarse.
        Acá se cruzan las dos cosas, que es lo que hace falta para señalar el
        problema mientras se escribe el flujo.
        """
        graph = self.parse(name)
        return graph, self.check_graph(graph)

    def check_graph(self, graph: FlowGraph) -> list[Diagnostic]:
        """Diagnósticos de un grafo contra los tools realmente instalados."""
        extra: list[Diagnostic] = []

        for node_id, node in graph.action_nodes():
            # Las nativas las resuelve el motor: no están en el registro y no
            # tienen manifest que chequear.
            if node.fn in NATIVE_FNS:
                continue
            tool_id = self.registry.resolve_id(node.fn)
            if self.registry.get(tool_id) is None:
                parecidos = _parecidos(node.fn, self.registry.tool_ids)
                sugerencia = f" ¿Quisiste decir {' o '.join(parecidos)}?" if parecidos else ""
                extra.append(Diagnostic(
                    Severity.ERROR,
                    f'No hay ningún tool "{node.fn}" instalado.{sugerencia}',
                    node.line,
                    node_id,
                ))
                continue

            manifest = self.registry.manifest(tool_id)
            if manifest is None:
                continue
            # Un param que el tool no declara se ignora en silencio al
            # ejecutar. Decirlo acá evita el "puse el parámetro y no hace nada".
            if not manifest.extra_params:
                declarados = {p.name for p in manifest.params}
                declarados |= {a for p in manifest.params for a in p.aliases}
                for clave in node.params:
                    if clave not in declarados:
                        extra.append(Diagnostic(
                            Severity.WARNING,
                            f'"{tool_id}" no declara el parámetro "{clave}"; se ignora.',
                            node.line,
                            node_id,
                        ))
            for p in manifest.params:
                if p.required and p.name not in node.params \
                        and not any(a in node.params for a in p.aliases) \
                        and p.default is None and not p.config_key:
                    extra.append(Diagnostic(
                        Severity.ERROR,
                        f'"{tool_id}" necesita el parámetro "{p.name}".',
                        node.line,
                        node_id,
                    ))

        return extra

    # ── Configuración ───────────────────────────────────────────────────

    def effective_config(self) -> dict:
        """
        La configuración que ven los plugins: defaults del manifest pisados por
        lo que guardó el usuario.

        Un solo lugar donde se resuelve. Si cada tool le aplicara el default a
        mano al leer su setting, el mismo setting valdría distinto según quién
        lo mire.
        """
        return self.registry.effective_config(self.config.read())

    # ── Colecciones de los plugins ──────────────────────────────────────

    def resource_store(self, plugin: str, resource) -> TableStore:
        """Store de una colección. El plugin no lo construye ni lo ve."""
        return store_for(self.db, plugin, resource)

    def resource_definition(self, plugin: str, collection: str):
        """El `Resource` que declaró ese plugin, o None."""
        cargado = next((p for p in self.registry.plugins if p.name == plugin), None)
        if cargado is None or cargado.manifest is None:
            return None
        return cargado.manifest.resource(collection)

    def resource_items(self, plugin: str, collection: str) -> list[dict]:
        """
        Items de una colección, listos para que el plugin los use en ejecución.

        Dos cosas pasan acá y ninguna la ve el plugin:

        1. **Se resuelven las referencias `{env.CLAVE}`.** Un item guarda
           `Bearer {env.TOKEN}`, nunca el token. La sustitución ocurre en el
           servidor, en el momento de usarlo. Sin este paso, un secreto
           referenciado viajaría literal hasta el otro extremo.
        2. Es lo único que un tool ve de su almacenamiento: una lista de dicts.
           No recibe la base, ni el store, ni una ruta.
        """
        definicion = self.resource_definition(plugin, collection)
        if definicion is None:
            return []
        items = self.resource_store(plugin, definicion).list_items()
        entorno = self.env_vars()
        return [_resolver_env(item, entorno) for item in items]

    def _resource_reader(self, plugin: str):
        """Lector ligado a un plugin, que es lo que recibe su contexto."""
        return lambda coleccion: self.resource_items(plugin, coleccion)

    def resource_items_masked(self, plugin: str, collection: str) -> list[dict]:
        """
        Items de una colección, para listar — nunca para ejecutar.

        A diferencia de `resource_items`, acá cada campo que el `Resource`
        declaró `secret` queda en `None` (mismo criterio que `EnvStore.list`
        con una variable marcada secreta: el valor no sale de acá ni por
        error). Tampoco resuelve `{env.CLAVE}` — mostrar la referencia sin
        resolver es lo que corresponde en un listado, resolverla sería
        trabajo de más para algo que después se tapa igual.

        Es lo que puede salir por el servidor MCP o cualquier otra API: un
        agente necesita saber qué items existen para poder pedir uno por
        nombre (`run_action(..., item=...)`), pero nunca sus secretos.
        """
        definicion = self.resource_definition(plugin, collection)
        if definicion is None:
            return []
        secretos = {f.name for f in definicion.fields if f.secret}
        items = self.resource_store(plugin, definicion).list_items()
        return [
            {clave: (None if clave in secretos else valor) for clave, valor in item.items()}
            for item in items
        ]

    # ── Ejecución ───────────────────────────────────────────────────────

    def authorize(self, graph: FlowGraph, policy: RunPolicy) -> list[str]:
        """
        Qué nodos de este grafo le están vedados al actor. Vacío = puede correrlo.

        Es un chequeo **previo**: se mira el grafo entero antes de ejecutar
        nada, para poder decir "estos tres nodos no" de una vez en vez de morir
        en el primero. El executor vuelve a chequear por nodo —un subflujo
        resuelto en runtime no se puede ver desde acá—, así que esto es para el
        mensaje, no para la garantía.
        """
        motivos: list[str] = []
        for node_id, node in graph.action_nodes():
            if node.fn in NATIVE_FNS:
                continue
            manifest = self.registry.manifest(node.fn)
            if manifest is None:
                continue
            dueño = self.registry.plugin_of(node.fn)
            motivo = policy.deniega(
                manifest, dueño.ports if dueño else (), dueño.name if dueño else ""
            )
            if motivo:
                motivos.append(f"{node_id}: {motivo}")
        return motivos

    def requires_exclusive_run(self, flow_name: str) -> bool:
        """
        True si algún tool usado en el flujo declara concurrency="exclusive_run".

        No hace cumplir nada — sólo informa. Quien orquesta arranques (la
        webapp) decide, con esto, si deja correr un run junto a otros o espera
        a que no haya ninguno en vuelo.
        """
        graph = self.parse(flow_name)
        return any(
            (manifest := self.registry.manifest(node.fn)) is not None
            and manifest.concurrency == "exclusive_run"
            for _, node in graph.action_nodes()
            if node.fn not in NATIVE_FNS
        )

    def policy_for(self, actor: str | None) -> RunPolicy:
        """La política del actor, o la del actor por defecto del arranque."""
        return self.users.policy_for(actor or self.boot.default_actor)

    def run(
        self,
        flow_name: str,
        case_id: str,
        *,
        row: dict | None = None,
        source: str = "",
        actor: str | None = None,
        dry_run: bool = False,
        is_cancelled=None,
        persist: bool = True,
    ) -> RunResult:
        """
        Ejecuta un flujo y guarda su traza.

        `actor` dice quién lo ejecuta, y no es sólo trazabilidad: determina qué
        tiene permitido. Entra por acá y por ningún otro lado — un flujo no
        puede declararlo y un plugin no puede cambiarlo, porque si fuera
        falsificable desde adentro no serviría para autorizar.

        Un actor inexistente o sin permiso falla **antes** de ejecutar nada.

        Los dry-run también se guardan: sirven para ver qué se validó y cuándo.
        """
        texto = self.load_workflow(flow_name)
        policy = self.policy_for(actor)

        vedados = self.authorize(parse_flow(texto, with_meta=False), policy)
        if vedados:
            raise UserError(
                f'El actor "{policy.actor}" no puede ejecutar "{flow_name}":\n  '
                + "\n  ".join(vedados)
            )

        empezado = self._ahora()

        resultado = execute_flow(
            texto,
            case_id=case_id,
            registry=self.registry,
            row=row or {},
            config=self.effective_config(),
            env=self.env_vars(),
            load_flow=self.load_workflow,
            is_cancelled=is_cancelled,
            resources=self.resource_items,
            policy=policy,
            dry_run=dry_run,
        )

        if persist:
            try:
                self.runs.save(resultado, flow=flow_name, source=source, started_at=empezado)
                # El log va a su propia tabla, en una escritura.
                self.logs.append_run(resultado, flow=flow_name, source=source)
                self._limpiar_logs()
            except (StoreError, OSError):
                # Un fallo al guardar no puede tumbar un run que ya se ejecutó.
                pass
        return resultado

    def run_action(
        self, plugin: str, action: str, params: dict | None = None, item: str | None = None
    ):
        """
        Ejecuta una acción de un plugin ("probar conexión", "previsualizar").

        No es parte de ningún run: la dispara una persona. Aun así vuelve como
        `ToolResult`, con el mismo aislamiento de fallos que un tool, porque la
        garantía de que nada falla en silencio no depende de quién apretó.

        `item` resuelve los params desde un item ya guardado de la colección
        que declara la acción (`Action.resource`), en vez de reconstruirlos a
        mano en cada llamada — "previsualizar" contra una conexión guardada
        con su url/headers/auth, por ejemplo. `params` explícitos pisan lo que
        traiga el item, campo por campo.
        """
        from .contract import ToolContext, ToolResult

        base: dict = {}
        if item:
            declarada = next(
                (a for a in self.registry.actions_of(plugin) if a.name == action), None
            )
            if declarada is None or not declarada.resource:
                return (
                    ToolResult.err(
                        f'"{action}" de "{plugin}" no está atada a ninguna colección: '
                        "no acepta `item`."
                    ),
                    [],
                )
            definicion = self.resource_definition(plugin, declarada.resource)
            encontrado = definicion and next(
                (
                    i
                    for i in self.resource_items(plugin, declarada.resource)
                    if i.get(definicion.key_field) == item
                ),
                None,
            )
            if not encontrado:
                return (
                    ToolResult.err(f'no existe "{item}" en {declarada.resource} de {plugin}'),
                    [],
                )
            base = {
                k: v
                for k, v in encontrado.items()
                if k != definicion.key_field and not k.startswith("_")
            }

        config = self.effective_config()
        registro: list[tuple[str, str]] = []

        def ctx_factory(declaracion, ports):
            return ToolContext(
                run_id="",
                case_id="",
                params=_resolver_action_params(declaracion, {**base, **(params or {})}, config),
                config=config,
                context={},
                log=lambda mensaje, nivel="info": registro.append((mensaje, nivel)),
                resources=self._resource_reader(plugin),
                ports=ports,
            )

        resultado = self.registry.execute_action(plugin, action, ctx_factory)
        return resultado, registro

    def missing_config_for(self, flow_name: str) -> dict:
        """Config que le falta a los plugins que ese flujo necesita."""
        graph = self.parse(flow_name)
        usados = [node.fn for _, node in graph.action_nodes()]
        return self.registry.missing_config(self.effective_config(), usados)

    # ── Registro de eventos ─────────────────────────────────────────────

    def _limpiar_logs(self) -> int:
        """
        Aplica la política de retención. Se llama después de guardar un run.

        El tope por fila se aplica **siempre**, en cualquier modo: es el que
        impide que un flujo con reintentos largos se coma la base. Los días
        sólo con `expiration`.
        """
        config = self.effective_config()
        borradas = 0

        tope = int(config.get("logMaxPerCase") or 0)
        if tope > 0:
            borradas += self.logs.prune_keeping(tope)

        if (config.get("logRetentionMode") or "manual") == MODO_EXPIRACION:
            borradas += self.logs.prune_older_than(float(config.get("logRetentionDays") or 0))

        return borradas

    def run_detail(self, run_id: str) -> dict | None:
        """
        Un run con su log pegado de vuelta.

        El blob de `runs` guarda el trace y no el log —el log vive en su propia
        tabla, una fila por línea— así que se junta al leer.
        """
        datos = self.runs.get(run_id)
        if datos is None:
            return None
        datos["logs"] = [linea.to_dict() for linea in self.logs.for_run(run_id)]
        return datos

    def case_log(self, case_id: str, limit: int = 500) -> dict:
        """Todo el registro de una fila, cruzando todos sus runs."""
        lineas = self.logs.for_case(case_id, limit=limit)
        total = self.logs.count_for_case(case_id)
        return {
            "case_id": str(case_id),
            "entries": [linea.to_dict() for linea in lineas],
            "total": total,
            "truncated": total > len(lineas),
        }

    # ── Variables y secretos ────────────────────────────────────────────

    def env_vars(self) -> dict:
        """
        Lo que un flujo ve como `{env.CLAVE}`, ya resuelto.

        Dos capas: la tabla `env` y el entorno del proceso. El entorno gana para
        que un contenedor o un CI puedan pisar un valor sin tocar la base.
        """
        valores = dict(self.env.resolve())
        valores.update(_env_vars_del_entorno())
        return valores

    def env_usage(self) -> dict[str, int]:
        """
        Cuántas veces se referencia cada nombre en toda la instalación.

        Se cruzan los flujos y los items de cada colección de plugin. Los dos
        importan: el caso canónico —una cabecera `Bearer {env.TOKEN}`— vive en
        un **item de una colección**, no en un flujo, así que contar sólo los
        flujos daría 0 usos justo para el secreto que más se usa.

        Un nombre con 0 usos sobra; una referencia sin valor va a fallar en
        medio de una ejecución. Las dos cosas se ven con este conteo.
        """
        usos: dict[str, int] = {}

        def contar(texto: str) -> None:
            for nombre in referencias(texto):
                usos[nombre] = usos.get(nombre, 0) + 1

        for wf in self.workflows.list():
            contar(wf.content)

        for plugin in self.registry.plugins:
            if plugin.manifest is None:
                continue
            for recurso in plugin.manifest.resources:
                for item in self.resource_store(plugin.name, recurso).list_items():
                    contar(dumps(item))

        return usos

    def env_listing(self) -> list[dict]:
        """Cada nombre con su conteo de usos. Nunca el valor de un secreto."""
        usos = self.env_usage()
        cargados = self.env.list()
        conocidos = {v.name for v in cargados}
        filas = [v.to_dict(usos.get(v.name, 0)) for v in cargados]

        # Un nombre que un flujo referencia y nadie cargó tiene que aparecer:
        # es el que va a romper la ejecución, y si sólo listáramos lo cargado
        # sería justo el que no se ve.
        for nombre, cantidad in sorted(usos.items()):
            if nombre in conocidos:
                continue
            filas.append({
                "name": nombre, "secret": False, "value": None,
                "has_value": False, "unreadable": False,
                "updated_by": "", "updated_at": 0.0, "uses": cantidad,
                "undeclared": True,
            })
        return filas

    # ── Diagnóstico ─────────────────────────────────────────────────────

    def storage_info(self) -> dict:
        """
        Qué hay guardado y cuánto. Las cuentas salen de la base y no de un
        contador aparte, que sería una segunda fuente de verdad esperando
        desincronizarse.
        """
        tablas: dict[str, int | None] = {}
        for dominio in self.db.versions():
            try:
                fila = self.db.one(f"SELECT COUNT(*) AS n FROM {dominio}")  # noqa: S608
                tablas[dominio] = fila["n"] if fila else 0
            except Exception:
                # Un dominio migrado sin tabla propia no es un error: se informa
                # como desconocido en vez de tumbar la pantalla entera.
                tablas[dominio] = None

        return {
            "versions": self.db.versions(),
            "counts": tablas,
            "crypto": type(self.crypto).__name__,
            "crypto_available": bool(getattr(self.crypto, "available", True)),
        }

    def _ahora(self) -> float:
        """La hora, por el port si está atado; si no, la del sistema."""
        from . import ports as port_names

        reloj = self.adapters.get(port_names.CLOCK)
        if reloj is not None:
            return reloj.now()
        import time

        return time.time()

    def close(self) -> None:
        self.db.close()
        # La mayoría de los adapters no tiene estado que cerrar (http/fs/
        # process/clock son stdlib sin proceso propio); el que sí lo tiene
        # —hoy, el navegador— declara su propio `close()`, y acá se cierra
        # sin que el núcleo sepa cuál es cuál.
        for adapter in self.adapters.values():
            cerrar = getattr(adapter, "close", None)
            if callable(cerrar):
                cerrar()


# ── Armado por defecto ──────────────────────────────────────────────────


def _default_storage(boot: bootstrap.BootConfig) -> StoragePort:
    """
    El adapter de almacenamiento por defecto.

    El import es local a propósito: `core/` no importa `adapters/` a nivel de
    módulo. La dependencia va en la dirección correcta —el adapter conoce el
    port, no al revés— y este import perezoso es el único puente, acotado al
    armado por defecto de una instalación real. Un test que inyecta su propio
    storage nunca lo ejecuta.
    """
    from backend.adapters.storage_sqlite import SqliteStorageAdapter

    return SqliteStorageAdapter(boot.storage_path)


def _default_adapters(boot: bootstrap.BootConfig) -> dict:
    """
    Los adapters, atados con los límites que declaró el arranque.

    `fs_root` y `process_allowlist` acotan qué puede tocar esta instalación, y
    vienen de la capa bootstrap y no de un `Setting`: son de la máquina, y un
    plugin no debería poder ampliarlos escribiendo en la configuración.
    """
    from backend.adapters import build_default_adapters

    return build_default_adapters(
        fs_root=boot.fs_root,
        http_timeout=boot.http_timeout,
        # `is not None`: la allowlist vacía es "ningún ejecutable", y colapsarla
        # a `None` acá desharía, en el último tramo, lo que declaró el arranque.
        process_allowlist=(
            list(boot.process_allowlist) if boot.process_allowlist is not None else None
        ),
    )


def _default_crypto(data_dir: Path) -> CryptoPort:
    """
    El cifrado por defecto: Fernet con la llave en un archivo al lado de la base.

    La llave va **fuera** de la base para que un backup del `.db` no lleve los
    secretos adentro: quien lo restaure en otra máquina no recupera nada.
    """
    from backend.adapters.crypto_fernet import FernetCryptoAdapter

    return FernetCryptoAdapter(data_dir / "secret.key")


def _build_registry(adapters: dict, local_plugins: dict[str, str] | None) -> ToolRegistry:
    """
    El núcleo primero, después lo instalado.

    Los builtins se registran como un plugin más —mismo manifest, mismo
    contrato, mismos ports declarados—. No es cosmético: garantiza que el camino
    del plugin esté siempre ejercitado, porque el propio núcleo lo transita.
    """
    from .builtins import build_builtin_plugin

    registry = ToolRegistry(adapters=adapters)
    registry._add_plugin("core", "builtin", build_builtin_plugin())
    for nombre, ruta in (local_plugins or {}).items():
        registry.register_local(nombre, ruta)
    registry.discover()
    return registry


# Prefijo del override por máquina de una variable de flujo. Es distinto del
# `BOT_` de la configuración a propósito: ese mapea a una clave de setting
# (`BOT_httpTimeout`), y si compartieran prefijo un setting y una variable con
# el mismo nombre se pisarían sin que nadie lo pueda ver.
ENV_PREFIX = "BOTENV_"


def _env_vars_del_entorno() -> dict:
    """
    `BOTENV_<CLAVE>` del entorno del proceso.

    Existe para que un contenedor o un CI corran sin gestionar ni un archivo ni
    la base: si la variable está, gana. No se persiste.
    """
    return {
        clave[len(ENV_PREFIX):]: valor
        for clave, valor in os.environ.items()
        if clave.startswith(ENV_PREFIX) and len(clave) > len(ENV_PREFIX)
    }


def _resolver_env(valor, entorno: dict):
    """
    Sustituye `{env.CLAVE}` en todos los strings de una estructura.

    Recursivo porque un item de una colección es JSON libre: la referencia puede
    estar en la URL, en un header anidado o en el payload, y resolver sólo el
    primer nivel dejaría medio item sin resolver de formas difíciles de ver.
    """
    from .flow.context import RunContext

    contexto = RunContext(env=dict(entorno))
    return _mapear(valor, contexto.resolve)


def _mapear(valor, fn):
    if isinstance(valor, str):
        return fn(valor)
    if isinstance(valor, dict):
        return {k: _mapear(v, fn) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_mapear(v, fn) for v in valor]
    return valor


def _resolver_action_params(accion, crudos: dict, config: dict) -> dict:
    """Params de una acción, con la misma precedencia y tipado que los de un tool."""
    from .contract import ToolManifest

    manifest = ToolManifest(
        id=f"action.{accion.name}",
        label=accion.label,
        category="ACCIÓN",
        params=accion.params,
    )
    return manifest.resolve_params(crudos, config)


def _parecidos(nombre: str, disponibles: list[str], maximo: int = 2) -> list[str]:
    """Tools con nombre parecido, para sugerir ante un typo."""
    import difflib

    return [f'"{c}"' for c in difflib.get_close_matches(nombre, disponibles, n=maximo, cutoff=0.6)]


__all__ = ["Instance", "WorkflowNotFound"]
