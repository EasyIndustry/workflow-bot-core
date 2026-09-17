"""
Configuración de arranque: dónde está todo, y qué puede tocar esta instalación.

La tercera capa de configuración, y la que faltaba:

    boot.env / entorno    BOOTSTRAP_*   dónde está todo        provisioning
    tabla settings        BOT_*         config de plugins      pantalla de config
    tabla env             BOTENV_*      variables y secretos   pantalla de secretos

Por qué no puede vivir en la base
---------------------------------

No es una preferencia de diseño: **hace falta para abrir la base**. Dónde vive
el archivo de datos es, en sí mismo, configuración de arranque. Huevo y gallina.
Lo mismo con la carpeta de plugins, que se necesita antes de que exista el
registro.

Y al revés: la tabla `env` es del **cliente** —las variables y secretos que un
flujo interpola como `{env.CLAVE}`—. Meter ahí "dónde está la carpeta de
plugins" sería infraestructura en un namespace de negocio.

Se lee, no se escribe
---------------------

En runtime esto es de sólo lectura. Un proceso que reescribe su propia
configuración de arranque produce un archivo que deriva solo, que no se puede
versionar y que no se puede provisionar desde un contenedor.

Lo que sí escribe el archivo es un comando explícito (`init`), una vez. Eso es
"configuración inicial", y está bien mientras sea un acto deliberado y no un
efecto secundario de correr.

El entorno del proceso **pisa** al archivo, para que un contenedor o un CI
arranquen sin gestionar ningún archivo.

Sobre el prefijo
----------------

`BOOTSTRAP_` y no `BOOT_`: ya existen `BOT_` (pisa un setting) y `BOTENV_` (una
variable de flujo), y `BOOT_` se diferencia de `BOT_` en una letra. Un
`BOT_pluginsDir` mal tipeado no falla — se guarda como un setting que nadie
consulta, en silencio. Es el modo de falla más caro de diagnosticar, y no vale
la pena ahorrarse cuatro caracteres para exponerse a él.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

PREFIJO = "BOOTSTRAP_"

# Nombre del archivo dentro de la raíz de la instalación.
ARCHIVO = "boot.env"

# Ruta especial que pide una base en memoria: corre de verdad, sin dejar rastro.
EN_MEMORIA = ":memory:"


@dataclass(frozen=True)
class BootConfig:
    """
    Todo lo que hay que saber antes de poder construir nada.

    Los campos de acotación —`fs_root`, `process_allowlist`— están acá y no en
    los settings de un plugin porque son de la **instalación**: definen qué
    puede tocar esta máquina, y un plugin no debería poder ampliarlos.
    """

    root: Path
    # Dónde vive la base. `:memory:` para una instalación efímera.
    storage: str = ""
    # Carpeta de plugins de desarrollo. En producción se usan entry points.
    plugins_dir: Path | None = None
    # Acota el filesystem a un subárbol. Vacío = todo el disco.
    fs_root: str | None = None
    # Varias raíces con alias, para cuando una sola no alcanza -un workspace
    # local y un share de red a la vez (issue #23)-. `None` es el caso normal
    # de una sola raíz, que sigue resolviendo por `fs_root`; con `fs_roots`
    # declarado, la primera entrada es la raíz por defecto (a la que resuelve
    # una ruta relativa) y el resto son alcanzables por alias (`origen:...`).
    fs_roots: dict[str, str] | None = None
    # Ejecutables permitidos. None = cualquiera; () = ninguno.
    process_allowlist: tuple[str, ...] | None = None
    http_timeout: float | None = None
    # Con quién se atribuye un run cuando nadie lo dice.
    default_actor: str = "local"
    # De dónde salió cada valor, para que `doctor` pueda mostrarlo.
    origen: dict = field(default_factory=dict)
    # El texto tal como se leyó, antes de convertir. Lo necesita `validar()`:
    # un `http_timeout=treinta` se convierte en `None`, que es exactamente lo
    # mismo que se ve cuando la clave no está. Sin el crudo no hay forma de
    # distinguir "no lo pidieron" de "lo pidieron mal".
    crudos: dict = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def storage_path(self) -> str:
        if self.storage:
            return self.storage
        return str(self.data_dir / "bot.db")

    @property
    def efimera(self) -> bool:
        return self.storage_path == EN_MEMORIA

    @property
    def fs_roots_efectivos(self) -> dict[str, str]:
        """
        La(s) raíz(es) de verdad para el port `fs`, en una sola forma.

        `fs_roots` gana si está declarado —su primera entrada es la raíz por
        defecto, a la que resuelve una ruta relativa—; si no, `fs_root`
        singular se expresa como una única raíz sin alias (`""`), que es
        exactamente el caso que ya andaba. Vacío si no se declaró ninguno de
        los dos: todo el disco, sin acotar.
        """
        if self.fs_roots:
            return dict(self.fs_roots)
        if self.fs_root:
            return {"": self.fs_root}
        return {}

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "storage": self.storage_path,
            "ephemeral": self.efimera,
            "plugins_dir": str(self.plugins_dir) if self.plugins_dir else None,
            "fs_root": self.fs_root,
            "fs_roots": dict(self.fs_roots) if self.fs_roots else None,
            "process_allowlist": (
                list(self.process_allowlist) if self.process_allowlist is not None else None
            ),
            "http_timeout": self.http_timeout,
            "default_actor": self.default_actor,
            "origen": dict(self.origen),
        }


# Claves reconocidas, con cómo se convierte cada una. Declaradas para poder
# avisar de una clave desconocida: un `BOOTSTRAP_pluginDir` mal escrito no debe
# ignorarse en silencio, que es justo el problema que este prefijo evita.
_CLAVES = {
    "storage": "str",
    "plugins_dir": "path",
    "fs_root": "str",
    "fs_roots": "dict",
    "process_allowlist": "list",
    "http_timeout": "float",
    "default_actor": "str",
}


# La misma regla que `users._NOMBRE_VALIDO`, escrita de nuevo y no importada:
# `boot` no puede importar un store —hace falta para abrir la base, y sería un
# ciclo—. Que las dos no se separen lo cuida un test de arquitectura; si acá
# fuera más laxa, el diagnóstico aprobaría un `default_actor` que el alta
# después rechaza.
_NOMBRE_ACTOR = re.compile(r"^[A-Za-z][A-Za-z0-9_\-.]*$")


def load(root: Path | str, entorno: dict | None = None) -> BootConfig:
    """
    Lee `boot.env` de la raíz y le aplica el entorno por encima.

    Ninguna de las dos fuentes es obligatoria: sin archivo y sin variables, una
    instalación arranca con los valores por defecto y guarda en `<root>/data`.
    """
    raiz = Path(root)
    entorno = os.environ if entorno is None else entorno

    valores: dict = {}
    origen: dict = {}

    archivo = raiz / ARCHIVO
    if archivo.is_file():
        for clave, crudo in _leer_archivo(archivo).items():
            valores[clave] = crudo
            origen[clave] = str(archivo)

    for clave, crudo in _leer_entorno(entorno).items():
        valores[clave] = crudo
        origen[clave] = f"{PREFIJO}{clave}"

    return BootConfig(
        root=raiz,
        storage=_texto(valores.get("storage")),
        plugins_dir=_ruta(valores.get("plugins_dir"), raiz),
        # Como `plugins_dir`: es una ruta, y una ruta relativa se resuelve
        # contra la raíz de la instalación y no contra el directorio desde el
        # que se arrancó. `fs_root` es el límite de seguridad de la
        # instalación; que dependiera del cwd haría que el mismo `boot.env`
        # diera una caja distinta según desde dónde se lo levante.
        fs_root=_texto_ruta(valores.get("fs_root"), raiz),
        fs_roots=_fs_roots(valores.get("fs_roots"), raiz),
        process_allowlist=_lista(valores.get("process_allowlist")),
        http_timeout=_flotante(valores.get("http_timeout")),
        default_actor=_texto(valores.get("default_actor")) or "local",
        origen=origen,
        crudos=dict(valores),
    )


def desconocidas(root: Path | str, entorno: dict | None = None) -> list[str]:
    """
    Claves presentes que el núcleo no reconoce.

    Se reportan en vez de ignorarse: una clave mal escrita que no hace nada y
    no avisa es indistinguible de una que sí funciona.
    """
    raiz = Path(root)
    entorno = os.environ if entorno is None else entorno
    vistas: list[str] = []

    archivo = raiz / ARCHIVO
    if archivo.is_file():
        vistas += [f"{archivo.name}:{k}" for k in _leer_archivo(archivo, filtrar=False)
                   if k not in _CLAVES]
    vistas += [
        f"{PREFIJO}{k}" for k in _leer_entorno(entorno, filtrar=False) if k not in _CLAVES
    ]
    return sorted(vistas)


class BootError(RuntimeError):
    """
    Un valor de arranque no permite construir la instancia.

    Sólo lo levanta lo que **construye** (`Instance.__init__`); `validar()` y
    `fatal()` siguen siendo puro reporte (issue #22). Trae en el mensaje la
    lista completa de problemas fatales, uno por línea, para que el primer
    lugar donde se lee ya diga qué corregir.
    """


def _diagnosticar(config: BootConfig) -> list[tuple[str, bool]]:
    """
    Valores presentes que no van a hacer lo que dicen, con si cada uno es
    **fatal** (la instalación no puede arrancar así) o sólo una degradación
    (arranca, pero con menos de lo declarado).

    Fatal es lo que deja inutilizable un límite de seguridad de la
    instalación: `fs_root`/`plugins_dir` que no resuelven a una carpeta real.
    Todo lo demás —un ejecutable ausente en el `process_allowlist`, un
    `http_timeout` inválido, un `default_actor` mal formado— es una
    degradación: se ignora el valor y se sigue con el default, y eso ya lo
    dice el propio `validar()` de siempre.

    El solapamiento entre `plugins_dir` y una raíz de `fs` YA NO se reporta
    acá (issue #26): antes era fatal —una instalación con `fs_root=D:\\` no
    podía arrancar si `plugins_dir` quedaba adentro, que es casi siempre—,
    pero ahora `LocalFsAdapter` niega `plugins_dir` (y `data_dir` y
    `boot.env`) sin importar qué raíz se declare, así que el solapamiento deja
    de ser un problema de configuración: queda cubierto por construcción, en
    el adapter, no en el chequeo de arranque.
    """
    problemas: list[tuple[str, bool]] = []
    crudo = config.crudos

    if not config.efimera:
        destino = Path(config.storage_path)
        if not escribible(destino.parent):
            problemas.append(
                (f"storage: no se va a poder escribir la base en {destino.parent}", False)
            )

    if config.plugins_dir is not None:
        ruta = Path(config.plugins_dir)
        if not ruta.exists():
            problemas.append((f"plugins_dir: {ruta} no existe", True))
        elif not ruta.is_dir():
            problemas.append((f"plugins_dir: {ruta} no es una carpeta", True))

    # Cada raíz declarada (issue #23: puede ser más de una) tiene que resolver
    # a una carpeta real. Ya no se chequea que no se solape con `plugins_dir`
    # (issue #26): `LocalFsAdapter` lo niega sin importar la raíz, así que una
    # raíz que contenga la instalación entera —`fs_root=D:\`, por ejemplo— es
    # legítima, y el solapamiento deja de ser motivo para no arrancar.
    for alias, cruda in config.fs_roots_efectivos.items():
        etiqueta = "fs_root" if not alias else f"fs_roots[{alias}]"
        ruta = Path(cruda)
        if not ruta.exists():
            mensaje = f"{etiqueta}: {ruta} no existe"
            if (pista := _pista_unc_sin_share(_texto_original_de_raiz(crudo, alias))):
                mensaje = f"{mensaje} — {pista}"
            problemas.append((mensaje, True))
        elif not ruta.is_dir():
            problemas.append((f"{etiqueta}: {ruta} no es una carpeta", True))

    for nombre in config.process_allowlist or ():
        if shutil.which(nombre) is None:
            problemas.append(
                (f"process_allowlist: no se encontró '{nombre}' en esta máquina", False)
            )

    if (texto := _texto(crudo.get("http_timeout"))):
        try:
            if float(texto) <= 0:
                problemas.append((f"http_timeout: {texto} no es un tiempo de espera", False))
        except ValueError:
            problemas.append((f"http_timeout: '{texto}' no es un número; se ignora", False))

    if not _NOMBRE_ACTOR.match(config.default_actor):
        problemas.append((
            f"default_actor: '{config.default_actor}' no es un nombre de actor válido", False
        ))

    return problemas


def validar(config: BootConfig) -> list[str]:
    """
    Valores presentes que no van a hacer lo que dicen.

    El hermano de `desconocidas()`, un escalón más abajo: aquél cuida el
    **nombre** de la clave, éste el **valor**. Un `http_timeout=treinta` no
    falla, no avisa y deja el timeout por defecto; un `plugins_dir` que no
    existe hace que no cargue ningún plugin, y el síntoma aparece lejos de la
    causa.

    Reporta, no levanta —y no toca nada—: es lo que le permite a `doctor`
    mostrarlo y a un instalador revisar los valores **antes** de escribir
    `boot.env`. La lista completa, fatal y degradación por igual: quien
    construye una instancia sólo necesita `fatal()`, más abajo.
    """
    return [mensaje for mensaje, _ in _diagnosticar(config)]


def fatal(config: BootConfig) -> list[str]:
    """
    El subconjunto de `validar()` que no puede arrancar así (issue #22).

    Es lo que `Instance.__init__` levanta como `BootError`: un `fs_root` o un
    `plugins_dir` declarados que no resuelven a una carpeta real dejan
    inutilizable un límite de seguridad de la instalación, así que no tiene
    sentido arrancar y fallar recién adentro de un run, con un mensaje que
    apunta al flujo en vez de a la configuración.
    """
    return [mensaje for mensaje, es_fatal in _diagnosticar(config) if es_fatal]


def _texto_original_de_raiz(crudo: dict, alias: str) -> str | None:
    """
    El texto tal como se escribió para una raíz, antes de resolverla contra
    la raíz de la instalación — lo que necesita `_pista_unc_sin_share` para
    reconocer un UNC sin share.

    Se busca primero en `fs_roots` —que también puede traer un alias vacío
    (`=ruta`), la misma raíz por defecto de un `fs_roots` con varias— y sólo
    si no hay `fs_roots` declarado se cae a `fs_root` singular; buscar en el
    orden inverso perdía la pista cuando la raíz sin alias venía de
    `fs_roots` y no de `fs_root`.
    """
    if _texto(crudo.get("fs_roots")):
        for par in _texto(crudo.get("fs_roots")).split(","):
            clave, sep, ruta = par.strip().partition("=")
            if sep and clave.strip() == alias:
                return ruta.strip()
        return None
    if not alias:
        return crudo.get("fs_root")
    return None


def _pista_unc_sin_share(crudo: str | None) -> str | None:
    """
    Detecta un UNC que declaró sólo el host (`\\\\server-nuevo`), sin el share
    (`\\\\server-nuevo\\share`). Es una ruta bien formada que nunca va a
    existir, y "no existe" a secas manda a revisar el flujo en vez del
    `boot.env` (issue #22).
    """
    if not crudo:
        return None
    texto = crudo.strip()
    if not (texto.startswith("\\\\") or texto.startswith("//")):
        return None
    segmentos = [s for s in re.split(r"[\\/]+", texto[2:].strip("\\/")) if s]
    if len(segmentos) < 2:
        return (
            "una ruta UNC tiene que incluir el share (\\\\servidor\\share), no sólo el host"
        )
    return None


def render(config: BootConfig) -> str:
    """El contenido de un `boot.env`, para que `init` lo escriba."""
    lineas = [
        "# Configuración de arranque de esta instalación.",
        "#",
        "# Se lee al construir la instancia, ANTES de abrir la base y de cargar",
        "# ningún plugin. Por eso no puede vivir en la base: dónde está la base",
        "# es, en sí mismo, uno de estos valores.",
        "#",
        "# Cualquier clave se puede pisar desde el entorno con el prefijo",
        f"# {PREFIJO} — un contenedor o un CI arrancan sin tocar este archivo.",
        "",
    ]
    documentacion = {
        "storage": "Dónde vive la base. ':memory:' para una instalación efímera.",
        "plugins_dir": "Carpeta de plugins de desarrollo. En producción: entry points.",
        "fs_root": "Acota el filesystem a este subárbol. Vacío = todo el disco.",
        "fs_roots": (
            "Varias raíces con alias, para cuando fs_root no alcanza (un workspace "
            "local y un share de red a la vez): alias=ruta, alias2=ruta2. La primera "
            "es la raíz por defecto. Si está presente, gana sobre fs_root."
        ),
        "process_allowlist": (
            "Ejecutables permitidos, separados por coma. Sin la clave = cualquiera; "
            "la clave presente y vacía = ninguno."
        ),
        "http_timeout": "Timeout por defecto de los requests, en segundos.",
        "default_actor": "A quién se le atribuye un run cuando nadie lo dice.",
    }
    # `None` es "la clave no está declarada" y se renderiza comentada. La
    # cadena vacía es un valor escrito: para `process_allowlist` significa
    # "ningún ejecutable", y si se comentara al renderizar, un `init` de una
    # instalación acotada devolvería un archivo más permisivo que el original.
    actual: dict[str, str | None] = {
        "storage": config.storage or None,
        "plugins_dir": str(config.plugins_dir) if config.plugins_dir else None,
        "fs_root": config.fs_root or None,
        "fs_roots": (
            ",".join(f"{a}={r}" for a, r in config.fs_roots.items()) if config.fs_roots else None
        ),
        "process_allowlist": (
            ",".join(config.process_allowlist) if config.process_allowlist is not None else None
        ),
        "http_timeout": None if config.http_timeout is None else str(config.http_timeout),
        "default_actor": config.default_actor or None,
    }
    for clave, doc in documentacion.items():
        lineas.append(f"# {doc}")
        valor = actual.get(clave)
        lineas.append(f"# {clave}=" if valor is None else f"{clave}={valor}")
        lineas.append("")
    return "\n".join(lineas)


# ── Lectura ─────────────────────────────────────────────────────────────


# El BOM que puede traer el archivo, y qué codificación declara.
#
# `boot.env` está pensado para que una persona lo abra y lo edite, y en Windows
# varias de las herramientas más obvias escriben un BOM sin preguntar:
# `Set-Content -Encoding UTF8` en PowerShell 5.1 deja el de UTF-8, y una
# redirección `>` deja UTF-16LE, que es peor porque no se parece a texto.
#
# El de UTF-32 va primero: su firma empieza con la de UTF-16LE, así que al revés
# un archivo UTF-32 se leería como UTF-16 y saldría basura.
_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def _decodificar(crudo: bytes) -> str:
    """
    El texto del archivo, respetando el BOM que haya quedado adelante.

    Sin esto, un BOM se pega al nombre de la primera clave y esa clave deja de
    reconocerse: se descarta en silencio y la instalación arranca como si nunca
    hubiera estado. Si la que se pierde es `fs_root`, pasa de estar acotada a un
    subárbol a poder tocar todo el disco — un límite de seguridad no puede
    desaparecer por un carácter invisible.
    """
    for firma, codec in _BOMS:
        if crudo.startswith(firma):
            try:
                return crudo.decode(codec)
            except UnicodeDecodeError:
                break
    try:
        return crudo.decode("utf-8")
    except UnicodeDecodeError:
        # No se puede decir qué codificación es —un `.env` guardado en cp1252,
        # por ejemplo—. Se lee igual, reemplazando lo que no entra: las claves
        # son ASCII y sobreviven, y un valor que salga mangleado lo levanta
        # `validar()`. Morir acá sería peor: `load()` corre antes que todo, así
        # que ni `doctor` llegaría a arrancar para decir qué pasó.
        return crudo.decode("utf-8", errors="replace")


def _leer_archivo(path: Path, filtrar: bool = True) -> dict:
    valores = {}
    try:
        texto = _decodificar(path.read_bytes())
    except OSError:
        return {}
    for linea in texto.splitlines():
        limpia = linea.strip()
        if not limpia or limpia.startswith("#") or "=" not in limpia:
            continue
        clave, _, valor = limpia.partition("=")
        clave = clave.strip()
        if filtrar and clave not in _CLAVES:
            continue
        valores[clave] = valor.strip().strip('"').strip("'")
    return valores


def _leer_entorno(entorno, filtrar: bool = True) -> dict:
    valores = {}
    for clave, valor in entorno.items():
        if not clave.startswith(PREFIJO) or len(clave) == len(PREFIJO):
            continue
        corta = clave[len(PREFIJO):]
        if filtrar and corta not in _CLAVES:
            continue
        valores[corta] = valor
    return valores


def escribible(carpeta: Path) -> bool:
    """
    Si se va a poder escribir ahí, aunque la carpeta todavía no exista.

    Se sube hasta el primer ancestro que exista: una instalación nueva todavía
    no tiene su `data/`, y lo que decide si la va a poder crear es el permiso
    de quien la va a contener.
    """
    actual = carpeta
    while not actual.exists():
        padre = actual.parent
        if padre == actual:
            return False
        actual = padre
    return actual.is_dir() and os.access(actual, os.W_OK)


def _texto(valor) -> str:
    return str(valor).strip() if valor is not None else ""


def _ruta(valor, raiz: Path) -> Path | None:
    texto = _texto(valor)
    if not texto:
        return None
    destino = Path(texto).expanduser()
    return destino if destino.is_absolute() else (raiz / destino)


def _texto_ruta(valor, raiz: Path) -> str | None:
    destino = _ruta(valor, raiz)
    return str(destino) if destino is not None else None


def _fs_roots(valor, raiz: Path) -> dict[str, str] | None:
    """
    `alias=ruta, alias2=ruta2` — issue #23. Cada ruta se resuelve igual que
    `fs_root`/`plugins_dir`: relativa contra la raíz de la instalación, no
    contra el cwd.

    Un alias vacío (`=ruta`) es válido: es la misma raíz sin nombre que usa
    `fs_roots_efectivos` para representar un `fs_root` singular, y es lo que
    escribe `render()` para la raíz por defecto de un `fs_roots` con varias.
    Sin aceptarlo acá, ese archivo se releía como si a esa raíz nunca la
    hubiera declarado nadie —un viaje de ida y vuelta que pierde la raíz por
    defecto, en silencio, reportado en QA sobre v0.3.1-beta.3.

    Un par sin `=` sí se descarta en silencio —no hay un lugar mejor para
    señalarlo que `validar()`/`fatal()`, que ya reportan cualquier raíz que
    no exista—, así que uno mal escrito termina viéndose como una raíz
    faltante y no como algo ignorado sin dejar rastro.
    """
    texto = _texto(valor)
    if not texto:
        return None
    resultado: dict[str, str] = {}
    for par in texto.split(","):
        par = par.strip()
        if not par:
            continue
        alias, sep, ruta = par.partition("=")
        if not sep:
            continue
        destino = _texto_ruta(ruta.strip(), raiz)
        if destino is not None:
            resultado[alias.strip()] = destino
    return resultado or None


def _lista(valor) -> tuple[str, ...] | None:
    """
    La clave ausente y la clave vacía no significan lo mismo.

    Ausente (`None`) es "cualquier ejecutable" —compatibilidad hacia atrás—.
    Presente y vacía (`()`) es "ninguno", que es lo que necesita declarar una
    instalación acotada y que hasta acá no se podía escribir.
    """
    if valor is None:
        return None
    return tuple(x.strip() for x in _texto(valor).split(",") if x.strip())


def _flotante(valor) -> float | None:
    texto = _texto(valor)
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


__all__ = [
    "ARCHIVO",
    "EN_MEMORIA",
    "PREFIJO",
    "BootConfig",
    "BootError",
    "desconocidas",
    "escribible",
    "fatal",
    "load",
    "render",
    "validar",
]
