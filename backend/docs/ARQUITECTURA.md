# Resumen de arquitectura — para el agente de código

Brief operativo. No es narrativa de decisiones, son las reglas que cualquier
cambio de código tiene que respetar.

---

## Principio

El núcleo es **solo** dos cosas: motor de workflows (parsear/ejecutar Mermaid)
y registro de logs/trazas. Todo lo demás — cualquier integración, librería
externa, o lógica específica de un caso de uso — es plugin o adapter.

Regla de una línea: **el núcleo actúa sobre el run. Un plugin actúa sobre el
mundo.**

---

## Las cuatro capas

| Capa | Qué es | Qué conoce | Ejemplo |
|---|---|---|---|
| **Core** | orquesta el run, parsea Mermaid, resuelve variables, decide la siguiente arista | los *ports*, nunca una librería ni un plugin por nombre | `backend/core/flow/executor.py` |
| **Port** | una interfaz (`Protocol`) | nada — es solo la forma | `backend/core/ports.py` |
| **Adapter** | implementación concreta de un port | la librería externa, y solo ella | `backend/adapters/http_urllib.py` |
| **Plugin** | lógica de negocio | los ports que necesita, nunca una librería directamente | se instala por entry point |

Los ports que el núcleo define, con su adapter incluido: `http` (urllib), `fs`
(os/shutil), `process` (subprocess), `clock` (time), `storage` (sqlite3) y
`crypto` (cryptography).

`storage` y `crypto` son los dos que un plugin **no** puede pedir: son del
núcleo. Un plugin con acceso al almacenamiento elegiría dónde persisten sus
datos —lo que `Resource` existe para impedir— y uno con acceso al cifrado podría
leer secretos que no le corresponden.

Dirección de las llamadas: **Core → Plugin → Adapter**, siempre. Las
respuestas vuelven como valores de retorno, nunca como llamadas nuevas hacia
atrás. Ninguna capa de abajo inicia una acción por su cuenta.

Un plugin nunca elige ni instancia su propio adapter: **lo declara en su
manifest** (`PluginManifest.ports`) y lo recibe ya resuelto, por
`ctx.port("http")`. Declararlo hace dos cosas que pedirlo en runtime no haría:

- el núcleo valida **al cargar** que exista un adapter para cada port pedido, y
  rechaza el plugin entero si falta uno — en vez de fallar a mitad de un run,
  meses después, en la máquina de otro;
- el catálogo publica qué ports usa cada plugin, o sea su superficie de riesgo
  real: quién sale a la red, quién toca el disco, quién corre comandos.

Un tool que pide un port que su manifest no declara levanta `PortNotDeclared`.
Sin eso, la declaración sería decorativa.

Esto es lo que permite mockear adapters enteros en tests —la suite corre sin
red, sin disco y sin reloj real— y cambiar de librería sin tocar el plugin.

---

## Qué NO va en el core

Nada que conozca una integración específica, un cliente concreto, o el modelo
de datos de un caso particular. El test rápido: si al eliminar el core dejara
de existir el motor y el registro de logs, es core. Si lo que deja de existir
es "poder mover un archivo" o "poder hacer un HTTP request", es plugin.

`process.run`, aunque genérico y usado por casi toda instalación, **no es
core**: sale al sistema operativo, no manipula el run. Como plugin, se puede
desinstalar y su superficie de riesgo (ejecución de comandos) es opcional, no
universal.

---

## El contrato Core↔Plugin

Un plugin expone:

- **Manifest** (`ToolManifest`): id (`namespace.accion`), params tipados,
  outputs declarados, si es `dangerous`, aliases para renombrar sin romper
  flujos viejos.
- **Settings** (`PluginManifest.settings`): configuración por instalación,
  compartida por todos los tools del plugin. El núcleo la persiste y renderiza
  su pantalla solo; el plugin nunca escribe HTML de configuración.
- **Resources**: colecciones con ABM propio. Declaran esquema (`fields`), el
  núcleo dibuja el CRUD y persiste. **El plugin nunca sabe ni decide dónde se
  guardan sus datos** — los lee con `ctx.resource(coleccion, clave)`, ya con
  las referencias `{env.CLAVE}` resueltas.
- **Ports** (`PluginManifest.ports`): qué necesita del mundo exterior.
- **`run(ctx) -> ToolResult`**: recibe params ya resueltos y tipados, nunca
  toca el grafo, nunca escribe estado global.

Garantías no negociables:

1. **Nada falla en silencio.** Toda excepción no capturada se convierte en
   `ToolResult` con status `err` y traceback.
2. **Sin contaminación entre nodos.** Params no declarados en el manifest se
   descartan; nada de estado global compartido tipo "variable de sesión".
3. **Outputs declarados** habilitan validar un flujo antes de correrlo.
4. **Un solo catálogo**, `registry.catalog()`. Ni una UI, ni un MCP, ni la CLI
   mantienen su propia lista.
5. **Un plugin que pide un port sin adapter no se registra a medias.** Ni uno
   de sus tools entra. Media instalación funcionando es peor que ninguna.

Precedencia de un param: **valor en el nodo del flujo → config de la
instancia → default del manifest.**

---

## Persistencia: el plugin declara, el núcleo guarda

Un plugin nunca recibe una conexión a la base, una ruta de archivo, ni un
store propio. Escribe su manifest (settings + resources) y el núcleo decide
cómo y dónde persistirlo. Esto es lo que permite cambiar de motor de
almacenamiento (SQLite → Postgres → lo que sea) sin migrar ningún plugin.

`StoragePort` ya existe: `backend/core/` no importa ningún driver, el modelo de datos
vive en `backend/core/schema.py` como datos, y `backend/adapters/storage_sqlite.py`
lo aplica.

**Límite honesto:** el port abstrae la *conexión*, no el *dialecto*. El SQL sigue
escrito en los stores del núcleo, así que portar a Postgres exige revisar las
sentencias (`ON CONFLICT`, `AUTOINCREMENT`), no sólo escribir otro adapter. Lo
que sí entrega hoy: el núcleo no importa un driver, y un test corre entero
contra una base en memoria.

---

## UI de un plugin: tres niveles, en este orden

1. **Esquema** (default): `Setting`, `Resource.fields` → el núcleo dibuja
   formulario y ABM genérico. Cubre casi todo.
2. **Acciones**: `Action(name, label, params)` → el núcleo dibuja botón +
   formulario + resultado. Para "probar conexión", "previsualizar filas", etc.
3. **Panel**: HTML propio del plugin, en un **iframe** con puente acotado
   (`postMessage`) que solo puede tocar sus propios settings/resources/
   acciones. Es el último recurso, no el primero.

Reglas duras de este nivel:

- Un plugin **no escribe pantallas** salvo que el esquema realmente no
  alcance. Si puede evitar el panel, debe evitarlo.
- El panel nunca reemplaza el render de settings/resources, solo agrega lo
  que el esquema no puede expresar.
- Si el panel falla o no carga, se cae al formulario genérico — nunca una
  pantalla en blanco.
- El plugin ocupa una **pestaña fija** (Configuración · Colecciones · Tools ·
  [panel]), nunca se inyecta en puntos arbitrarios del layout del núcleo.

---

## Log vs. trace — son dos cosas distintas

| | Qué es | Alcance | Responde |
|---|---|---|---|
| **trace** | un nodo, sus params resueltos, su estado | un run | "¿por qué falló esta corrida?" |
| **log** | narración en líneas | una fila, cruzando runs | "¿qué viene pasando con este caso?" |

Ninguna reemplaza a la otra. El trace vive con el run (append-only). El log es
tabla propia, una fila por línea (no un blob por run), para poder consultar y
limpiar sin reescribir ni parsear nada.

---

## Secretos

- Se **referencian**, nunca se embeben: una conexión guarda
  `{env.NOMBRE_TOKEN}`, no el valor. Se resuelve en el servidor al ejecutar.
- Nunca viajan al navegador. `GET` de un secreto no devuelve su valor (ni
  `null`: la clave directamente no existe en la respuesta).
- No hay edición de un secreto, solo reemplazo.
- Cifrados en reposo; la llave vive fuera de la base de datos.

---

## Versionado

- **Núcleo**: semver, soporta el contrato `N` y `N-1`.
- **Contrato**: entero simple, solo sube con cambios incompatibles.
- Dos canales de release separados: núcleo (lento, "casi no se toca") y
  plugins (rápido, ahí se desarrolla).

---

## Checklist rápido para revisar cualquier PR/feature

- [ ] ¿Este código importa una librería externa fuera de `backend/adapters/`? →
      mover. (`backend/tests/test_arquitectura.py` lo verifica solo.)
- [ ] ¿El core hace `if plugin == X` o conoce un nombre de integración? → fuga
      de lógica de negocio al core.
- [ ] ¿Un plugin decide dónde persiste sus datos, o abre su propia conexión a
      la base? → romper y pasar por `ctx.resource`/`ctx.config`.
- [ ] ¿Un tool devuelve algo sin pasar por `ToolResult`, o puede fallar sin
      levantar `err`? → no cumple el contrato.
- [ ] ¿Un plugin escribe HTML/CSS propio pudiendo resolverlo con
      `Setting`/`Resource`/`Action`? → sobreingeniería de ese plugin.
- [ ] ¿Un plugin usa un port que no declaró en su manifest? → el catálogo
      miente sobre lo que ese plugin puede hacer.
- [ ] ¿Un adapter decide si algo es un error de negocio? → no le corresponde.
      Fallo de transporte = `PortError`; un 500 o un exit code 1 son datos.