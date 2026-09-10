# Backend — núcleo de workflows

Motor de workflows con trazas, sin interfaz. Se escribe un flujo en Mermaid, el
núcleo lo parsea, lo ejecuta y deja registro de qué pasó en cada nodo.

```bash
python -m backend.core doctor                       # ¿levanta todo?
python -m backend.core tools                        # catálogo de tools
python -m backend.core plugins                      # plugins, sus ports y su config
python -m backend.core workflows                    # flujos guardados
python -m backend.core add flujo.mmd                # guarda un .mmd en la instalación
python -m backend.core check flujo.mmd              # validar sin ejecutar
python -m backend.core run flujo.mmd --row '{"id":"42"}'
python -m backend.core run flujo.mmd --dry-run      # recorrer sin tocar el mundo
python -m backend.core trace <run_id>               # la traza de una corrida
```

## Las cuatro capas

| Capa | Qué es | Qué conoce | Dónde vive |
|---|---|---|---|
| **Core** | orquesta el run, parsea Mermaid, resuelve variables, decide la arista siguiente | los *ports*, nunca una librería ni un plugin por nombre | `backend/core/` |
| **Port** | una interfaz (`Protocol`) | nada — es sólo la forma | `backend/core/ports.py` |
| **Adapter** | implementación concreta de un port | la librería externa, y sólo ella | `backend/adapters/` |
| **Plugin** | lógica de negocio | los ports que necesita, nunca una librería | instalado aparte |

Las llamadas van **Core → Plugin → Adapter**, siempre. Ninguna capa de abajo
inicia una acción por su cuenta.

La regla en una línea: **el núcleo actúa sobre el run; un plugin actúa sobre el
mundo.**

## Qué hay instalado hoy

**Cero plugins.** El núcleo trae sólo sus primitivas (`core.log`, `core.wait`,
`core.set_status`) y el control de flujo nativo (`flow.ejecutar`,
`flow.retry_gate`). Toda integración concreta —una API, un programa de
escritorio, un formato de archivo— se escribe como plugin y se instala por
entry point.

Los ports con adapter incluido:

| Port | Adapter | Librería |
|---|---|---|
| `http` | `UrllibHttpAdapter` | `urllib` |
| `fs` | `LocalFsAdapter` | `os` + `shutil` |
| `process` | `SubprocessAdapter` | `subprocess` |
| `clock` | `SystemClockAdapter` | `time` |
| `browser` | `PlaywrightBrowserAdapter` | `playwright` |
| `storage` | `SqliteStorageAdapter` | `sqlite3` |
| `crypto` | `FernetCryptoAdapter` | `cryptography` |

`browser` maneja un navegador real (navegar, clickear, leer lo que la pantalla
ya muestra) pero **no** sabe de sesiones: perfil persistente y login son
responsabilidad del plugin que lo use, nunca del adapter. El adapter por
defecto no persiste nada entre corridas; una instalación que necesite sesión
entre corridas inyecta su propio `PlaywrightBrowserAdapter(user_data_dir=...)`
vía `Instance(root, adapters={...})`.

`storage` y `crypto` son del núcleo: un plugin no los puede pedir. Uno con
acceso al almacenamiento elegiría dónde persisten sus datos —exactamente lo que
`Resource` existe para impedir— y uno con acceso al cifrado podría leer secretos
que no le corresponden.

## Escribir un plugin

`backend/tests/demo_plugin.py` es la referencia ejecutable. En resumen:

```python
MANIFEST = PluginManifest(
    name="mi_plugin",
    label="Mi plugin",
    ports=("http",),                       # se declara, no se importa
    settings=(Setting("timeout", ParamType.FLOAT, default=30.0),),
    resources=(Resource(name="conexiones", label="Conexiones", fields=(...)),),
    actions=(Action("probar", "Probar conexión", params=(...)),),
)

def _traer(ctx: ToolContext) -> ToolResult:
    respuesta = ctx.port("http").request(ctx.params["url"])   # ya resuelto
    if not respuesta.ok:
        return ToolResult.err(f"respondió {respuesta.status}")
    return ToolResult.ok(response=respuesta.json())

PLUGIN = Plugin(manifest=MANIFEST, tools=[FunctionTool(manifest=..., fn=_traer)])
```

Y en su `pyproject.toml`:

```toml
[project.entry-points."bot.tools"]
mi_plugin = "mi_paquete:PLUGIN"
```

Reglas que el núcleo hace cumplir, no sugerencias:

- Un plugin **nunca importa una librería externa**: pide un port.
- Un plugin que pide un port sin adapter **no carga**, y queda reportado.
- Un tool que usa un port que su manifest no declara **falla**.
- Toda salida es un `ToolResult`. Una excepción se convierte en `err` con su
  traceback: nada falla en silencio.
- Un plugin **no sabe dónde se guardan sus datos**: los lee con
  `ctx.resource(...)`.
- Un plugin **no escribe HTML**: declara `Setting`, `Resource.fields` y
  `Action`, y quien construya la UI los renderiza desde `registry.catalog()`.

## Para quien construya la interfaz

Todo lo necesario sale de un solo lugar:

```python
from backend.core.instance import Instance
Instance("backend").registry.catalog()
```

Trae contrato, ports atados, tools con sus params y outputs tipados, y por cada
plugin: settings, colecciones con su esquema, acciones y qué ports usa. No hay
que mantener ninguna lista propia — ese fue el error de la versión anterior, con
el catálogo escrito a mano en tres archivos distintos.

`Instance` es la fachada: `run()`, `diagnose()`, `run_action()`, `case_log()`,
`run_detail()`, más los stores. Un servidor HTTP encima de esto es una capa
delgada, no un segundo motor.

## Quién ejecuta: actores y permisos

Cada run queda atribuido a un actor, y el actor **decide qué se puede**. No es
sólo trazabilidad: un `kind` que sólo se registra es decoración, y la decoración
se desincroniza del comportamiento.

```bash
python -m backend.core users                      # quién hay y qué puede
python -m backend.core users add bot --kind agent # nace restringido
python -m backend.core users allow bot --dangerous
python -m backend.core --actor bot run flujo.mmd
```

| kind | tools peligrosos | ports |
|---|---|---|
| `human` | sí | todos |
| `agent` | **no** | http, fs, clock |
| `schedule` | sí | todos |
| `system` | no | clock |

Un agente nace sin permiso para lo destructivo ni para el port `process`. No es
desconfianza: el default de algo que actúa sin nadie mirando tiene que ser el
conservador, y habilitarlo tiene que ser deliberado. Ampliar permisos es un
`UPDATE`, no un deploy.

Un flujo vedado falla **antes** de ejecutar nada, y dice qué nodo y qué regla lo
impidió. El chequeo se repite por nodo dentro del executor, porque un subflujo
que `flow.ejecutar` resuelve en runtime no se puede inspeccionar de antemano.

**La granularidad de ports es por plugin, no por tool.** Los ports se declaran
en `PluginManifest` y se inyectan a todos los tools de ese plugin, así que
cualquiera podría usar cualquiera. Se deniega de más a propósito, y el mensaje
lo dice para que no parezca un error.

**Identidad, no autenticación.** La tabla guarda quién dice ser, nunca cómo se
prueba: no hay contraseñas, tokens ni sesiones. Quién establece la identidad es
la capa de arriba.

## Las tres capas de configuración

```
boot.env / entorno    BOOTSTRAP_*   dónde está todo        provisioning
tabla settings        BOT_*         config de plugins      pantalla de config
tabla env             BOTENV_*      variables y secretos   pantalla de secretos
```

```bash
python -m backend.core init    # escribe boot.env
python -m backend.core boot    # qué está en efecto, y de dónde salió cada valor
```

La primera **no puede** vivir en la base: hace falta para abrirla — dónde está
el archivo de datos es, en sí mismo, uno de estos valores. Contiene también los
límites de la instalación (`fs_root`, `process_allowlist`), que son de la
máquina y no settings que un plugin pueda ampliar.

`BOOTSTRAP_` y no `BOOT_` a propósito: `BOT_` ya existe, y un typo de una letra
caería en otro namespace y se guardaría en silencio como algo que nadie
consulta. Por lo mismo, el módulo reporta dos clases de problema en vez de
ignorarlas: `desconocidas()` para una **clave** que no existe, y `validar()`
para un **valor** que no va a hacer lo que dice —una carpeta que no está, un
timeout que no es un número—. Las dos las muestran `boot` y `doctor`.

Dos detalles de los límites, que no son cosméticos:

- `process_allowlist` **ausente** es "cualquier ejecutable"; **presente y
  vacía** es "ninguno". Sin esa distinción, la configuración más restrictiva
  que se puede pedir sería indistinguible de la más permisiva.
- `fs_root` y `plugins_dir` resuelven una ruta relativa contra la raíz de la
  instalación, no contra el directorio desde el que se arrancó: `fs_root` es un
  límite de seguridad, y el mismo `boot.env` tiene que dar la misma caja
  siempre.

El archivo se lee respetando el BOM que traiga —UTF-8, UTF-16 o UTF-32—, y una
codificación que no se pueda adivinar se lee igual en vez de tumbar el arranque.
No es tolerancia gratuita: es un archivo que se edita a mano, y en Windows las
herramientas más obvias dejan un BOM sin preguntar. `init` lo escribe **con**
BOM, que es lo que hace que Notepad y PowerShell no rompan los acentos al
abrirlo ni al guardarlo.

## Servidor MCP de autoría

```bash
pip install mcp
python -m backend.mcp      # habla por stdio
```

Expone el motor a un agente para **escribir** flujos y plugins. No es una
interfaz de operación: no ejecuta flujos de verdad.

| Tool | Qué hace |
|---|---|
| `list_tools` | el catálogo, con params tipados y outputs |
| `list_plugins` | plugins cargados, sus settings, colecciones y ports |
| `list_resource_items` | items guardados de una colección ("sources"), secrets tapados |
| `list_ports` | qué puede declarar un plugin, y qué le da cada port |
| `check_flow` | valida un `.mmd`: severidad, línea y nodo |
| `dry_run_flow` | recorre entero con un row, sin ejecutar un solo tool |
| `load_plugin` | ¿el núcleo acepta este plugin? sin ejecutarlo |
| `run_action` | ejecuta una `Action` declarada por un plugin. `item=` resuelve params desde un item guardado |
| `run_flow` | ejecución real. `root` obligatorio, y el actor limita qué puede |
| `list_users` | qué puede cada actor, para entender una denegación |
| `plugin_template` | esqueleto con los ports ya declarados |

El bucle que habilita:

```
list_tools → escribir el .mmd → check_flow → falta un tool
   → plugin_template → escribir el plugin → load_plugin
   → check_flow otra vez → dry_run_flow → verde
```

`run_flow` pide `root` sin default —hay que nombrar dónde se escribe, para no
mezclar pruebas con producción— y corre como un actor de tipo `agent`, que no
puede disparar tools peligrosos. Para casi todo alcanza `dry_run_flow`.

**La lista de tools es fija y no crece con los plugins.** Un `Tool` de plugin es
un nodo de un flujo: fuera de un run no tiene contexto, ni traza, ni log de
fila. Se expone como dato en el catálogo, no como algo invocable. Una `Action`
sí está diseñada para dispararse suelta, así que hay una tool parametrizada en
vez de una por acción — la misma frontera que el contrato ya dibuja.

Dos decisiones que no son detalles:

- **Cada operación corre en un subproceso.** `register_local` usa
  `importlib.import_module`, que cachea en `sys.modules`: al corregir un plugin
  y recargarlo se recibiría el módulo viejo, y se iteraría contra código que ya
  no existe en disco. Un proceso nuevo por intento lo evita, y además acota el
  daño de un plugin generado que se cuelgue al importarse.
- **`operations.py` no importa el SDK.** La lógica se testea sin levantar nada;
  `server.py` es sólo cableado.

Va contra un checkout con su propio `--root`, nunca contra la instalación de
producción: un plugin recién generado escribiendo en la base real es una mala
tarde.

## Estructura

Todo el backend vive bajo `backend/`. No queda nada suyo en la raíz del repo:
traerlo a otra rama es traer **una carpeta**, sin archivos sueltos que resolver.

```
backend/
  core/
    contract.py  el contrato Core↔Plugin (manifests, ToolContext, ToolResult)
    ports.py     las interfaces del mundo exterior
    registry.py  descubrimiento, validación de ports, ejecución
    instance.py  la instalación: acá se eligen los adapters
    builtins.py  core.log / core.wait / core.set_status
    schema.py    el modelo de datos, como datos
    users.py     actores y política de ejecución
    boot.py      configuración de arranque
    cli.py       python -m backend.core
    flow/        parser, serializador, contexto de variables, executor
  adapters/      la única capa que importa una librería (hacia afuera)
  mcp/           servidor MCP de autoría (hacia adentro)
    operations.py  la lógica, sin nada de MCP
    server.py      sólo cableado de protocolo
  tests/
    fakes.py     adapters falsos: la suite corre sin red, disco ni reloj
    demo_plugin.py  plugin de referencia, ejercita los cuatro ports
    flows/       corpus de flujos de prueba
  docs/          arquitectura
  data/          la base de esta instalación (no versionada)
```

## Tests

```bash
pip install pytest && pytest backend/tests -q
```

Todo corre contra una base en memoria y adapters falsos. Los únicos que tocan el
mundo son los de `tests/test_adapters.py`, y lo hacen en un temporal y contra
localhost. Un flujo con cinco esperas de sesenta segundos tarda milisegundos:
eso es para lo que existe el `ClockPort`.

## Deuda conocida

- **El `StoragePort` abstrae la conexión, no el dialecto.** El SQL sigue escrito
  en los stores del núcleo, así que portar a Postgres exige revisar las
  sentencias (`ON CONFLICT`, `AUTOINCREMENT`), no sólo escribir otro adapter.
- **Los campos `secret` de un `Resource` no se cifran en reposo.** Se declaran y
  el catálogo los marca, y las referencias `{env.CLAVE}` sí se resuelven en el
  servidor al ejecutar; pero el cifrado a nivel item todavía no está. Sólo la
  tabla `env` cifra.
- **No hay panel de plugin (nivel 3 de UI).** Están el esquema y las acciones;
  el iframe con puente acotado queda para cuando el esquema no alcance.
- **Los permisos de port son por plugin, no por tool.** Denegar de más es la
  postura segura, pero un manifest que declarara ports por tool sería más
  preciso.
- **`settings`, `env` y `plugin_items` tienen `updated_by` sin relación con
  `users`.** Deberían referenciar la misma tabla; si no, quedan dos vocabularios
  de actor conviviendo.
