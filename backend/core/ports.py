"""
Ports — las interfaces con las que el núcleo habla del mundo exterior.

Un port es *sólo la forma*: un `Protocol` y sus objetos de valor. No importa
ninguna librería, no tiene implementación, y no sabe que existe un adapter.
Quien lo implementa vive en `adapters/`, y es el único lugar del repo con
derecho a importar `urllib`, `shutil`, `subprocess` o `sqlite3`.

Por qué existe esta capa
------------------------

Antes, cada plugin importaba su librería directamente: el de HTTP traía
`urllib`, el de archivos `shutil`, el de procesos `subprocess`. Dos consecuencias
que se pagaban todos los días:

- **No se podía testear sin el mundo.** Probar un flujo significaba tener red,
  disco y el reloj de verdad. Un test de reintentos con esperas de 60s tardaba
  minutos reales.
- **Cambiar de librería era tocar todos los plugins.** Pasar de `urllib` a
  `requests` obligaba a abrir cada uno.

Con ports, un test inyecta adapters falsos y el plugin no se entera; y cambiar
de librería es escribir un adapter nuevo y cambiar un binding.

Reglas
------

1. **Un port no captura errores del dominio.** Un fallo de transporte —no hay
   red, el archivo no existe, el comando no arrancó— levanta `PortError`. El
   registry lo convierte en un `ToolResult` con status `err` y su traceback, así
   que sigue sin haber fallo silencioso.
2. **Un resultado del mundo no es un error.** Un HTTP 500 o un exit code 1 son
   *datos*: vuelven en el objeto de valor para que el plugin decida si eso es un
   fallo de negocio o no. Un adapter no ramifica por el usuario.
3. **Los objetos de valor son inmutables y de stdlib.** Nada que obligue a
   instalar algo para leer una respuesta.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence, runtime_checkable


class PortError(Exception):
    """
    Fallo de infraestructura: la operación no se pudo llevar a cabo.

    Es distinto de "se llevó a cabo y dio un resultado malo". No hay red es
    `PortError`; un 404 no lo es. La distinción importa porque la primera no la
    puede manejar el flujo y la segunda sí.
    """


# ── HTTP ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HttpResponse:
    """
    Una respuesta HTTP, tal como vuelve del transporte.

    `text` es el cuerpo crudo. El parseo a JSON es una conveniencia (`json()`) y
    no una promesa: hay APIs que devuelven JSON declarándolo `text/plain`, así
    que confiar en el `Content-Type` dejaba respuestas válidas como string.
    """

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self, default: Any = None) -> Any:
        """
        El cuerpo parseado, o `default` si no es JSON.

        Se intenta parsear si lo declara **o** si empieza como objeto o lista.
        Mirar sólo la cabecera dejaba afuera respuestas que sí eran JSON.
        """
        declara = "json" in self.content_type.lower()
        parece = self.text.lstrip()[:1] in ("{", "[")
        if not (declara or parece):
            return default
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            return default


@runtime_checkable
class HttpPort(Protocol):
    """Un cliente HTTP. Todo lo que el núcleo necesita saber de la red."""

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        """
        Ejecuta el request y devuelve la respuesta.

        Cualquier status HTTP vuelve como `HttpResponse`, incluidos 4xx y 5xx:
        son datos, y el flujo tiene que poder ramificar por ellos. Sólo un fallo
        de transporte (DNS, conexión, timeout) levanta `PortError`.
        """
        ...


# ── Filesystem ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FileInfo:
    """Metadatos de una entrada del filesystem."""

    path: str
    name: str
    is_dir: bool
    size: int = 0
    modified_at: float = 0.0


@runtime_checkable
class FsPort(Protocol):
    """
    Operaciones sobre archivos y carpetas.

    Deliberadamente sin nada de Windows: `os.startfile`, las rutas UNC o el
    explorador son cosa del adapter concreto, no de la forma. Un plugin escrito
    contra este port corre igual en Linux y en Windows.
    """

    # Consulta
    def exists(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
    def stat(self, path: str) -> FileInfo:
        """Metadatos de una entrada. `PortError` si no existe."""
        ...

    def list_dir(self, path: str) -> list[FileInfo]:
        """Contenido directo de una carpeta, ordenado por nombre."""
        ...

    def walk(self, path: str) -> Iterator[FileInfo]:
        """Todas las entradas bajo una carpeta, recursivo."""
        ...

    # Escritura
    def make_dirs(self, path: str) -> str:
        """Crea la carpeta y sus padres. No falla si ya existe."""
        ...

    def move(self, source: str, dest: str) -> str:
        """Mueve un archivo o carpeta. Devuelve la ruta final."""
        ...

    def copy_file(self, source: str, dest: str) -> str: ...
    def copy_tree(self, source: str, dest: str) -> str: ...
    def remove_file(self, path: str) -> None: ...
    def remove_tree(self, path: str) -> None: ...

    def rename(self, path: str, new_name: str) -> str:
        """Renombra dentro de la misma carpeta. Devuelve la ruta nueva."""
        ...

    # Rutas
    #
    # Son operaciones puras —no tocan el disco— y están en el port igualmente:
    # sin ellas, cada plugin resuelve separadores a mano con `rsplit("/")` y se
    # equivoca en los bordes. El caso que lo motivó: un destino sin separador
    # ("salida.txt") hacía que un `rsplit` devolviera el nombre entero como si
    # fuera la carpeta padre, y el plugin terminaba creando un directorio con
    # ese nombre en vez de renombrar el archivo. Fallaba en silencio.

    def parent(self, path: str) -> str:
        """
        Carpeta contenedora. Cadena vacía si la ruta no tiene una explícita.

        Resuelve `/` y `\\` sin importar el sistema donde corra: un plugin
        escrito contra este port maneja rutas de Windows corriendo en Linux.
        """
        ...

    def basename(self, path: str) -> str:
        """El último tramo de la ruta, sin la carpeta."""
        ...

    def join(self, *parts: str) -> str:
        """Une tramos con el separador del sistema, sin duplicarlos."""
        ...

    # Contenido
    def read_text(self, path: str, encoding: str = "utf-8") -> str: ...
    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> None: ...
    def read_bytes(self, path: str) -> bytes: ...
    def write_bytes(self, path: str, content: bytes) -> None: ...


# ── Procesos ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcessResult:
    """
    El resultado de correr un comando.

    Un exit code distinto de cero **no** es un `PortError`: el comando corrió y
    contestó eso. Que sea un fallo o no lo decide quien lo llamó.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    command: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class ProcessPort(Protocol):
    """
    Ejecución de comandos del sistema.

    Es la superficie de riesgo más alta del sistema, y por eso es un port
    explícito: un plugin que lo pide lo declara, y el catálogo lo publica.
    """

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        """
        Corre un comando y espera a que termine.

        `command` es una secuencia, nunca un string: armar la línea a mano es
        cómo se llega a una inyección de shell. El adapter no usa `shell=True`.
        """
        ...


# ── Reloj ───────────────────────────────────────────────────────────────


@runtime_checkable
class ClockPort(Protocol):
    """
    El tiempo, como dependencia inyectable.

    Sin esto, un flujo con reintentos duerme de verdad en los tests: el flujo de
    referencia esperaba 60s por intento con hasta 50 intentos. Con el reloj como
    port, un test controla el tiempo y esa misma corrida tarda milisegundos.
    """

    def now(self) -> float:
        """Epoch en segundos. Para timestamps que se guardan."""
        ...

    def monotonic(self) -> float:
        """Reloj monótono. Para medir duraciones, inmune a cambios de hora."""
        ...

    def sleep(self, seconds: float, is_cancelled: Callable[[], bool] | None = None) -> None:
        """
        Pausa. Un adapter de test la puede hacer instantánea.

        Con `is_cancelled`, la espera despierta a chequearlo y vuelve antes si
        pasa a True. Sin eso, cancelar un run que está esperando 60s no surte
        efecto hasta que la espera termina, y desde afuera se ve como que el
        botón de detener no hace nada.
        """
        ...


# ── Navegador ───────────────────────────────────────────────────────────


@runtime_checkable
class BrowserPort(Protocol):
    """
    Un navegador real: navegar, clickear, leer lo que la pantalla ya muestra.

    Sesión, perfil persistente y login son decisión de negocio de quien usa
    este port —un plugin—, nunca del adapter: el adapter no sabe qué es "estar
    logueado" para ninguna app en particular, sólo maneja el navegador.
    """

    def goto(self, url: str, *, timeout: float | None = None) -> None:
        """Navega a `url`. `PortError` si no carga a tiempo."""
        ...

    def click(self, selector: str, *, timeout: float | None = None) -> None:
        """Clickea el primer elemento que matchea `selector`. `PortError` si no aparece a tiempo."""
        ...

    def leer_texto(self, selector: str, *, timeout: float | None = None) -> str:
        """El texto visible del primer elemento que matchea `selector`."""
        ...

    def screenshot(self) -> bytes:
        """La pantalla actual, como PNG."""
        ...

    @property
    def available(self) -> bool:
        """
        Si el adapter puede operar en esta máquina.

        Igual que `CryptoPort.available`: existe para poder decir "falta
        instalar Playwright" antes de que un plugin intente usarlo, en vez de
        fallar recién ahí.
        """
        ...


# ── Ventana de escritorio ───────────────────────────────────────────────


@dataclass(frozen=True)
class WindowInfo:
    """
    Una ventana ya encontrada, para no tener que rebuscarla en cada llamada.

    `handle` es un token opaco del adapter: se recibe de `find_window` y se
    devuelve tal cual en las llamadas siguientes, nunca se arma a mano ni se
    interpreta. Es lo que permite que el mismo objeto de valor sirva sin
    importar qué hay detrás del adapter.
    """

    handle: str
    title: str
    process: str = ""


@runtime_checkable
class WindowPort(Protocol):
    """
    Una ventana nativa de escritorio: encontrarla, clickear, tipear, leer.

    Deliberadamente sin nombrar sistema operativo ni librería —mismo criterio
    que el resto de los ports—: un plugin escrito contra esta forma no sabe,
    ni le importa, qué hay detrás. Cuál adapter usar lo decide quien arma la
    instalación (`adapters.build_default_adapters`, según el sistema donde
    corre), nunca el plugin: así se declara el port una sola vez y funciona
    igual en cualquier sistema con adapter propio.

    Sin gestión de foco entre llamadas ni de coordenadas de pantalla: igual
    que `BrowserPort` no resuelve sesión, este port no resuelve qué pasa si
    dos automatizaciones compiten por la misma ventana. Eso es decisión de
    quien lo use.
    """

    def find_window(
        self,
        *,
        title: str | None = None,
        process: str | None = None,
        timeout: float | None = None,
    ) -> WindowInfo:
        """
        Busca una ventana por (parte de) su título o por el nombre de su
        proceso. `PortError` si no aparece a tiempo, o si no se da ninguno de
        los dos criterios.
        """
        ...

    def click(self, window: WindowInfo, control: str, *, timeout: float | None = None) -> None:
        """Clickea el control identificado por `control` dentro de la ventana. `PortError` si no aparece a tiempo."""
        ...

    def type_text(
        self, window: WindowInfo, control: str, text: str, *, timeout: float | None = None
    ) -> None:
        """Escribe `text` en el control identificado por `control`."""
        ...

    def read_text(
        self, window: WindowInfo, control: str | None = None, *, timeout: float | None = None
    ) -> str:
        """El texto de `control`, o de la ventana entera si no se da `control`."""
        ...

    @property
    def available(self) -> bool:
        """
        Si el adapter puede operar en esta máquina.

        Mismo criterio que `BrowserPort.available`: existe para poder decir
        "no hay automatización de ventanas en este sistema" antes de que un
        plugin intente usarla, en vez de fallar recién ahí.
        """
        ...


# ── Criptografía ────────────────────────────────────────────────────────


@runtime_checkable
class CryptoPort(Protocol):
    """
    Cifrado simétrico para los secretos en reposo.

    Es un port y no una función suelta en el núcleo por la razón de siempre —
    `cryptography` es una librería externa y no puede vivir en `core/`— pero
    también por una propia: la gestión de la llave (dónde vive, cómo se genera,
    qué permisos tiene) es una decisión de infraestructura. Una instalación con
    un KMS o un HSM escribe otro adapter y el núcleo no se entera.

    `decrypt` levanta `PortError` si el texto no se puede descifrar. Quien
    llama decide qué hacer: el almacén de secretos lo omite, para que el flujo
    falle al interpolar —un error legible— en vez de reventar con una excepción
    de criptografía en medio de una ejecución.
    """

    def encrypt(self, plaintext: str) -> str:
        """Texto cifrado, listo para guardar como string."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """El texto original. `PortError` si no se puede descifrar."""
        ...

    @property
    def available(self) -> bool:
        """
        Si el adapter puede operar.

        Existe para poder decir "falta instalar la dependencia de cifrado"
        **antes** de que alguien intente guardar un secreto, en vez de fallar
        recién al guardarlo.
        """
        ...


# ── Almacenamiento ──────────────────────────────────────────────────────


@runtime_checkable
class StoragePort(Protocol):
    """
    La base de datos, como interfaz.

    Cierra la deuda que el núcleo tenía declarada: `sqlite3` ya no se importa en
    `core/`. Los stores hablan con este port y el adapter decide el motor.

    **Límite honesto y conocido:** el SQL sigue escrito en los stores del
    núcleo, así que el port abstrae la *conexión*, no el *dialecto*. Portar esto
    a Postgres exige revisar las sentencias (`ON CONFLICT`, `AUTOINCREMENT`), no
    sólo escribir otro adapter. Se documenta en vez de fingir lo contrario: el
    valor que sí entrega hoy es que el núcleo no importe un driver y que un test
    pueda correr entero en memoria.
    """

    def migrate(self, schema: dict[str, int], migrations: dict[str, dict[int, str]]) -> dict[str, int]:
        """Lleva cada dominio a su versión. Devuelve las versiones aplicadas."""
        ...

    def versions(self) -> dict[str, int]:
        """Versión actual de cada dominio, para diagnóstico."""
        ...

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        """Filas como dicts. Nunca filas del driver: eso ataría al núcleo a él."""
        ...

    def one(self, sql: str, params: Iterable = ()) -> dict | None: ...

    def execute(self, sql: str, params: Iterable = ()) -> int:
        """Ejecuta y devuelve cuántas filas afectó."""
        ...

    def executemany(self, sql: str, rows: Iterable[Iterable]) -> int: ...

    def columns(self, table: str) -> list[str]:
        """
        Los nombres de columna de `table`, en orden. Lista vacía si la tabla
        no existe: las tablas válidas ya salen de `schema.SCHEMA`, así que
        quien llama no necesita distinguir "no existe" de "no tiene
        columnas" —ninguna tabla real del núcleo tiene cero columnas—.

        Mismo límite honesto que el resto del port: en SQLite se resuelve
        con `PRAGMA table_info`, así que portar el dialecto sigue siendo
        trabajo de quien escriba el adapter nuevo, no algo que esto evite.
        """
        ...

    def close(self) -> None: ...


# ── Catálogo de ports conocidos ─────────────────────────────────────────
#
# Los nombres con los que un plugin pide un port en su manifest. Están acá y no
# en `registry.py` para que exista un solo lugar donde mirar qué se puede pedir.

HTTP = "http"
FS = "fs"
PROCESS = "process"
CLOCK = "clock"
BROWSER = "browser"
WINDOW = "window"
STORAGE = "storage"
CRYPTO = "crypto"

PORTS: dict[str, type] = {
    HTTP: HttpPort,
    FS: FsPort,
    PROCESS: ProcessPort,
    CLOCK: ClockPort,
    BROWSER: BrowserPort,
    WINDOW: WindowPort,
    STORAGE: StoragePort,
    CRYPTO: CryptoPort,
}

# Ports que un plugin puede pedir. `storage` y `crypto` no están: los dos son
# del núcleo. Un plugin con acceso al almacenamiento elegiría dónde persisten
# sus datos —exactamente lo que `Resource` existe para impedir— y uno con
# acceso al cifrado podría leer secretos que no le corresponden.
PLUGIN_PORTS = frozenset({HTTP, FS, PROCESS, CLOCK, BROWSER, WINDOW})


__all__ = [
    "BROWSER",
    "CLOCK",
    "CRYPTO",
    "FS",
    "HTTP",
    "PLUGIN_PORTS",
    "PORTS",
    "PROCESS",
    "STORAGE",
    "WINDOW",
    "BrowserPort",
    "ClockPort",
    "CryptoPort",
    "FileInfo",
    "FsPort",
    "HttpPort",
    "HttpResponse",
    "PortError",
    "ProcessPort",
    "ProcessResult",
    "StoragePort",
    "WindowInfo",
    "WindowPort",
]
