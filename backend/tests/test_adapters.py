"""
Tests de los adapters reales.

Son los únicos que tocan el mundo: disco de verdad (en un temporal), procesos de
verdad, y un servidor HTTP en localhost. Todo lo demás corre contra fakes.

Lo que se prueba no es que `shutil` funcione, sino que el adapter **cumpla el
contrato del port**: qué traduce a `PortError`, qué devuelve como dato, y que
los límites que declara (raíz del filesystem, allowlist de comandos) sean
efectivos y no decorativos.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.adapters.browser_playwright import PlaywrightBrowserAdapter
from backend.adapters.clock_system import SystemClockAdapter
from backend.adapters.crypto_fernet import FernetCryptoAdapter
from backend.adapters.fs_local import LocalFsAdapter
from backend.adapters.geometry_null import NullGeometryAdapter
from backend.adapters.http_urllib import UrllibHttpAdapter
from backend.adapters.process_subprocess import SubprocessAdapter
from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter
from backend.adapters.window_atspi import AtspiWindowAdapter
from backend.adapters.window_pywinauto import PywinautoWindowAdapter
from backend.adapters.window_unsupported import UnsupportedWindowAdapter
from backend.core.ports import PortError, WindowInfo


# ── HTTP ────────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    def _responder(self):
        if self.path == "/error":
            cuerpo = json.dumps({"error": "roto"}).encode()
            self.send_response(500)
        elif self.path == "/texto":
            cuerpo = b"hola"
            self.send_response(200)
        else:
            cuerpo = json.dumps({"ok": True, "path": self.path}).encode()
            self.send_response(200)
        # `text/plain` a propósito incluso para el JSON: hay APIs que lo hacen,
        # y el adapter tiene que parsear igual.
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    do_GET = do_POST = _responder

    def log_message(self, *args):
        pass


@pytest.fixture
def servidor():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    hilo = threading.Thread(target=httpd.serve_forever, daemon=True)
    hilo.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    # `shutdown` para el loop pero no cierra el socket de escucha.
    httpd.server_close()


def test_http_trae_el_cuerpo_y_lo_parsea(servidor):
    respuesta = UrllibHttpAdapter().request(f"{servidor}/casos")
    assert respuesta.status == 200
    assert respuesta.ok
    assert respuesta.json() == {"ok": True, "path": "/casos"}


def test_http_parsea_json_declarado_como_texto_plano(servidor):
    """
    Confiar sólo en el `Content-Type` dejaba respuestas válidas como string, y
    después el camino a los datos fallaba con "no tiene el campo X" sobre una
    respuesta que sí lo tenía.
    """
    respuesta = UrllibHttpAdapter().request(f"{servidor}/casos")
    assert respuesta.content_type.startswith("text/plain")
    assert respuesta.json()["ok"] is True


def test_un_texto_que_no_es_json_vuelve_como_texto(servidor):
    respuesta = UrllibHttpAdapter().request(f"{servidor}/texto")
    assert respuesta.text == "hola"
    assert respuesta.json(default="nada") == "nada"


def test_un_500_es_dato_y_no_excepcion(servidor):
    """
    El servidor contestó. Que sea un fallo de negocio lo decide el flujo, no el
    adapter: por eso vuelve como respuesta y el cuerpo queda en la traza.
    """
    respuesta = UrllibHttpAdapter().request(f"{servidor}/error")
    assert respuesta.status == 500
    assert not respuesta.ok
    assert respuesta.json() == {"error": "roto"}


def test_no_conectar_es_port_error():
    with pytest.raises(PortError, match="no se pudo conectar"):
        # Puerto cerrado: no hay nadie escuchando.
        UrllibHttpAdapter().request("http://127.0.0.1:1/nada", timeout=2.0)


def test_solo_http_y_https():
    """
    `urllib` abre `file://` y `ftp://`. Una URL armada desde una variable de un
    flujo no debería poder leer el disco.
    """
    with pytest.raises(PortError, match="esquema no soportado"):
        UrllibHttpAdapter().request("file:///etc/passwd")


# ── Filesystem ──────────────────────────────────────────────────────────


def test_fs_mueve_una_carpeta_y_devuelve_la_ruta_final(tmp_path):
    origen = tmp_path / "caso"
    origen.mkdir()
    (origen / "a.txt").write_text("x")
    destino = tmp_path / "archivo"
    destino.mkdir()

    final = LocalFsAdapter().move(str(origen), str(destino))

    assert final == str(destino / "caso")
    assert (destino / "caso" / "a.txt").read_text() == "x"
    assert not origen.exists()


def test_fs_copiar_no_borra_el_origen(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    final = LocalFsAdapter().copy_file(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
    assert (tmp_path / "a.txt").exists()
    assert final == str(tmp_path / "b.txt")


def test_fs_listar_y_recorrer(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "hondo.txt").write_text("x")
    (tmp_path / "arriba.txt").write_text("x")
    fs = LocalFsAdapter()

    directos = {e.name for e in fs.list_dir(str(tmp_path))}
    assert directos == {"sub", "arriba.txt"}

    todos = {e.name for e in fs.walk(str(tmp_path))}
    assert "hondo.txt" in todos


def test_fs_walk_con_max_depth_no_baja_mas_del_limite(tmp_path):
    """
    Issue #33: sobre un share grande, bajar el árbol entero para pedir sólo
    los hijos directos paga minutos de stat que después se descartan.
    `max_depth=1` tiene que devolver exactamente eso -- nombres de acá abajo,
    nada más hondo -- sin tocar lo que hay adentro de "sub".
    """
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "hondo.txt").write_text("x")
    (tmp_path / "arriba.txt").write_text("x")
    fs = LocalFsAdapter()

    directos = {e.name for e in fs.walk(str(tmp_path), max_depth=1)}
    assert directos == {"sub", "arriba.txt"}

    sin_limite = {e.name for e in fs.walk(str(tmp_path))}
    assert "hondo.txt" in sin_limite  # sin max_depth, sigue siendo el árbol entero


def test_fs_walk_max_depth_dos_llega_un_nivel_mas(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "hondo.txt").write_text("x")
    (tmp_path / "sub" / "mas_hondo").mkdir()
    (tmp_path / "sub" / "mas_hondo" / "muy_hondo.txt").write_text("x")
    fs = LocalFsAdapter()

    nivel_dos = {e.name for e in fs.walk(str(tmp_path), max_depth=2)}
    assert nivel_dos == {"sub", "hondo.txt", "mas_hondo"}
    assert "muy_hondo.txt" not in nivel_dos


def test_fs_un_error_del_sistema_lleva_la_ruta_adentro(tmp_path):
    """
    Un FileNotFoundError pelado no dice *cuál* archivo, que es lo único que se
    quiere saber al leer la traza de un run que falló.
    """
    with pytest.raises(PortError, match="no-existe"):
        LocalFsAdapter().stat(str(tmp_path / "no-existe"))


def test_fs_con_raiz_no_deja_salir_del_arbol(tmp_path):
    """
    Sin raíz, una ruta armada desde una variable de un flujo puede apuntar a
    cualquier parte del disco.
    """
    encierro = tmp_path / "permitido"
    encierro.mkdir()
    fs = LocalFsAdapter(root=encierro)

    fs.write_text("adentro.txt", "ok")
    assert (encierro / "adentro.txt").read_text() == "ok"

    with pytest.raises(PortError, match="fuera del árbol"):
        fs.read_text("../afuera.txt")
    with pytest.raises(PortError, match="fuera del árbol"):
        fs.read_text("/etc/passwd")


# ── Múltiples raíces con alias (issue #23) ───────────────────────────────


def test_fs_con_varias_raices_alcanza_las_dos_por_alias(tmp_path):
    """Un workspace local y un share de red, a la vez -- lo que motivó el issue."""
    casa = tmp_path / "workspace"
    origen = tmp_path / "server-nuevo" / "CASOS TERMINADOS"
    casa.mkdir()
    origen.mkdir(parents=True)
    (origen / "pieza.stl").write_text("x")

    fs = LocalFsAdapter(roots={"casa": casa, "origen": origen})

    fs.write_text("intermedio.txt", "ok")  # relativa: raíz por defecto (casa)
    assert (casa / "intermedio.txt").read_text() == "ok"

    assert fs.read_text("origen:pieza.stl") == "x"


def test_fs_con_varias_raices_una_no_alcanza_a_la_otra(tmp_path):
    casa = tmp_path / "workspace"
    origen = tmp_path / "server-nuevo"
    casa.mkdir()
    origen.mkdir()
    fs = LocalFsAdapter(roots={"casa": casa, "origen": origen})

    with pytest.raises(PortError, match="fuera del árbol permitido de 'casa'"):
        fs.read_text("casa:../server-nuevo/x")


def test_fs_dos_puntos_sin_alias_declarado_no_se_interpreta_como_alias(tmp_path):
    """
    Un ':' en la ruta sólo separa un alias si ESE alias está declarado --
    nunca una letra de unidad de Windows (`C:\\...`), que no es un alias que
    ninguna instalación declara. Se prueba contra `_alias_y_resto` en vez de
    contra una ruta absoluta real: `Path("C:\\...").is_absolute()` sólo es
    cierto en Windows, y la propiedad que importa -que "C" no se confunda con
    un alias- no depende del SO.
    """
    fs = LocalFsAdapter(roots={"casa": tmp_path})

    assert fs._alias_y_resto(r"C:\Windows\System32") == (None, r"C:\Windows\System32")
    assert fs._alias_y_resto("casa:pieza.stl") == ("casa", "pieza.stl")


def test_fs_un_alias_con_typo_es_error_y_no_una_escritura_silenciosa(tmp_path):
    """
    QA sobre v0.3.1-beta.3: `origne:pieza.stl` (typo de `origen`) no daba
    error -- se trataba como una ruta relativa a la raíz por defecto. En
    Linux eso escribe un archivo llamado literalmente "origne:pieza.stl"
    adentro del workspace, sin ningún aviso; sólo en Windows fallaba, y por
    el SO (":" no es válido en un nombre de archivo), no por el port.
    """
    casa = tmp_path / "workspace"
    origen = tmp_path / "casos"
    casa.mkdir()
    origen.mkdir()
    fs = LocalFsAdapter(roots={"casa": casa, "origen": origen})

    with pytest.raises(PortError, match="raíz desconocida: 'origne'"):
        fs.write_text("origne:pieza.stl", "x")
    assert not (casa / "origne:pieza.stl").exists()


def test_fs_ruta_absoluta_bajo_cualquier_raiz_declarada_entra(tmp_path):
    """Una ruta absoluta no necesita el alias si cae bajo alguna de las raíces."""
    casa = tmp_path / "workspace"
    origen = tmp_path / "server-nuevo"
    casa.mkdir()
    origen.mkdir()
    (origen / "pieza.stl").write_text("x")
    fs = LocalFsAdapter(roots={"casa": casa, "origen": origen})

    assert fs.read_text(str(origen / "pieza.stl")) == "x"


def test_fs_una_sola_raiz_via_roots_se_comporta_como_root(tmp_path):
    """`roots={"": ruta}` es lo mismo que `root=ruta` -- el caso de siempre."""
    encierro = tmp_path / "permitido"
    encierro.mkdir()
    fs = LocalFsAdapter(roots={"": encierro})

    fs.write_text("adentro.txt", "ok")
    assert (encierro / "adentro.txt").read_text() == "ok"
    with pytest.raises(PortError, match="fuera del árbol"):
        fs.read_text("/etc/passwd")


# ── denied: subárboles que nunca se alcanzan (issue #26) ─────────────────


def test_fs_denied_gana_sobre_una_raiz_que_lo_contiene(tmp_path):
    """
    El caso del issue: `fs_root=D:\\` con la instalación adentro. La raíz es
    todo el disco (acá, `tmp_path`), pero la carpeta de la instalación queda
    afuera del alcance de un flujo.
    """
    instalacion = tmp_path / "User" / "Bot"
    data = instalacion / "data"
    data.mkdir(parents=True)
    (data / "bot.db").write_text("secreto", encoding="utf-8")
    (tmp_path / "afuera.txt").write_text("ok", encoding="utf-8")

    fs = LocalFsAdapter(root=tmp_path, denied=[data])

    assert fs.read_text("afuera.txt") == "ok"
    with pytest.raises(PortError, match="fuera del alcance de un flujo"):
        fs.read_text(str(data / "bot.db"))


def test_fs_denied_no_se_confunde_con_fuera_del_arbol():
    """El mensaje se distingue a propósito: agregar una raíz no arregla esto."""
    fs = LocalFsAdapter(root="/permitido", denied=["/permitido/data"])
    try:
        fs._p("/permitido/data/bot.db")
        raise AssertionError("debió rechazar la ruta negada")
    except PortError as exc:
        assert "fuera del alcance de un flujo" in str(exc)
        assert "fuera del árbol permitido" not in str(exc)


def test_fs_denied_gana_con_cualquier_alias(tmp_path):
    """Ninguna raíz declarada, tenga alias o no, puede alcanzar lo negado."""
    plugins = tmp_path / "plugins"
    origen = tmp_path / "casos"
    plugins.mkdir()
    origen.mkdir()
    fs = LocalFsAdapter(roots={"casa": tmp_path, "origen": origen}, denied=[plugins])

    with pytest.raises(PortError, match="fuera del alcance de un flujo"):
        fs.read_text(str(plugins / "x.py"))          # absoluta, sin alias
    with pytest.raises(PortError, match="fuera del alcance de un flujo"):
        fs.read_text("casa:plugins/x.py")             # relativa, con alias


def test_fs_denied_gana_incluso_sin_ninguna_raiz_declarada(tmp_path):
    """`fs_root` vacío es "todo el disco" -- lo negado sigue sin alcanzarse."""
    secreta = tmp_path / "secreta"
    secreta.mkdir()
    fs = LocalFsAdapter(denied=[secreta])

    with pytest.raises(PortError, match="fuera del alcance de un flujo"):
        fs.read_text(str(secreta / "x"))


def test_sin_denied_no_cambia_nada(tmp_path):
    """Sin declarar nada negado, el comportamiento es exactamente el de siempre."""
    permitido = tmp_path / "permitido"
    permitido.mkdir()
    fs = LocalFsAdapter(root=permitido)
    fs.write_text("x.txt", "ok")
    assert (permitido / "x.txt").read_text() == "ok"


def test_fs_renombrar_no_acepta_separadores(tmp_path):
    """Un separador acá sería un `move` encubierto, y con eso una fuga del árbol."""
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(PortError, match="nombre inválido"):
        LocalFsAdapter().rename(str(tmp_path / "a.txt"), "../b.txt")


def test_fs_parent_y_basename_resuelven_los_dos_separadores():
    """
    Son puras y están en el port por una razón concreta: sin ellas cada plugin
    parte rutas a mano con `rsplit("/")` y se equivoca en el borde. El caso que
    lo motivó fue un destino sin separador, donde `rsplit` devuelve el nombre
    entero como si fuera la carpeta padre.
    """
    fs = LocalFsAdapter()

    # El borde que rompía: sin separador no hay carpeta padre.
    assert fs.parent("salida.txt") == ""
    assert fs.basename("salida.txt") == "salida.txt"

    assert fs.parent("/tmp/v/a.txt") == "/tmp/v"
    assert fs.basename("/tmp/v/a.txt") == "a.txt"

    # Rutas de Windows, corriendo en cualquier sistema.
    assert fs.parent(r"C:\casos\0044\a.stl") == r"C:\casos\0044"
    assert fs.basename(r"C:\casos\0044\a.stl") == "a.stl"

    # La barra final no cuenta como un tramo vacío.
    assert fs.parent("/tmp/v/") == "/tmp"
    assert fs.basename("/tmp/v/") == "v"

    # Y la raíz no se come a sí misma.
    assert fs.parent("/x") == "/"


def test_fs_join_no_duplica_separadores():
    fs = LocalFsAdapter()
    esperado = fs.join("/tmp/v", "sub", "a.txt")
    assert fs.join("/tmp/v/", "/sub/", "a.txt") == esperado
    assert "//" not in esperado


def test_las_operaciones_de_ruta_no_tocan_el_disco():
    """
    Se necesitan justamente sobre rutas que todavía no existen —para saber qué
    carpeta crear— así que no pueden depender de que el destino esté.
    """
    fs = LocalFsAdapter()
    assert fs.parent("/no/existe/nada/a.txt") == "/no/existe/nada"
    assert fs.basename("/no/existe/nada/a.txt") == "a.txt"


# ── Procesos ────────────────────────────────────────────────────────────


def test_process_corre_y_captura_la_salida():
    resultado = SubprocessAdapter().run([sys.executable, "-c", "print('hola')"])
    assert resultado.ok
    assert resultado.exit_code == 0
    assert "hola" in resultado.stdout


def test_un_exit_code_distinto_de_cero_es_dato(tmp_path):
    """El comando corrió y contestó eso. Que sea un fallo lo decide quien llamó."""
    resultado = SubprocessAdapter().run([sys.executable, "-c", "raise SystemExit(3)"])
    assert resultado.exit_code == 3
    assert not resultado.ok


def test_el_timeout_vuelve_como_resultado_no_como_excepcion():
    """
    "Tardó demasiado" es una condición por la que un flujo legítimamente quiere
    ramificar, así que no puede llegar como un PortError.
    """
    resultado = SubprocessAdapter().run(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
    )
    assert resultado.timed_out
    assert not resultado.ok


def test_un_ejecutable_inexistente_es_port_error():
    with pytest.raises(PortError, match="no se encontró el ejecutable"):
        SubprocessAdapter().run(["no-existe-este-comando-xyz"])


def test_la_allowlist_es_efectiva():
    adapter = SubprocessAdapter(allowlist=["python3"])
    with pytest.raises(PortError, match="no está en la lista"):
        adapter.run(["rm", "-rf", "/"])


def test_la_allowlist_vacia_no_deja_correr_nada():
    """
    La configuración más restrictiva que se puede pedir no puede ser
    indistinguible de la más permisiva.

    Es lo que necesita una instalación acotada —una carpeta del escritorio, sin
    ejecutar nada del sistema—: una pantalla que prometiera eso sin este
    comportamiento estaría mintiendo.
    """
    with pytest.raises(PortError, match="no está en la lista"):
        SubprocessAdapter(allowlist=[]).run([sys.executable, "-c", "pass"])


def test_sin_allowlist_se_puede_correr_cualquier_cosa():
    """Ausente sigue siendo "cualquiera": la compatibilidad hacia atrás."""
    assert SubprocessAdapter(allowlist=None).run([sys.executable, "-c", "pass"]).ok


def test_no_hay_shell_asi_que_no_hay_inyeccion(tmp_path):
    """
    El argumento se pasa tal cual, no lo interpreta un shell. Sin esto, un
    `{id}` con un `;` adentro se convierte en ejecución arbitraria.
    """
    centinela = tmp_path / "no-deberia-existir"
    resultado = SubprocessAdapter().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", f"; touch {centinela}"]
    )
    assert f"; touch {centinela}" in resultado.stdout
    assert not centinela.exists()


# ── Reloj ───────────────────────────────────────────────────────────────


def test_el_reloj_avanza_y_la_espera_se_puede_cancelar():
    reloj = SystemClockAdapter()
    assert reloj.now() > 0
    empezo = reloj.monotonic()

    # Cancelada de entrada: vuelve enseguida en vez de dormir los 30s.
    reloj.sleep(30, lambda: True)
    assert reloj.monotonic() - empezo < 1.0


# ── Almacenamiento ──────────────────────────────────────────────────────


def test_el_storage_devuelve_dicts_y_no_filas_del_driver():
    """
    Si los stores recibieran filas del driver, el núcleo quedaría atado a él por
    la puerta de atrás aunque el import viviera acá.
    """
    almacen = SqliteStorageAdapter(IN_MEMORY)
    almacen.migrate({"t": 1}, {"t": {1: "CREATE TABLE t (a TEXT, b INTEGER);"}})
    almacen.execute("INSERT INTO t (a, b) VALUES (?, ?)", ("x", 1))

    filas = almacen.query("SELECT * FROM t")
    assert filas == [{"a": "x", "b": 1}]
    assert type(filas[0]) is dict
    almacen.close()


def test_una_migracion_faltante_es_un_error_explicito():
    almacen = SqliteStorageAdapter(IN_MEMORY)
    with pytest.raises(PortError, match="Falta la migración"):
        almacen.migrate({"t": 2}, {"t": {1: "CREATE TABLE t (a TEXT);"}})
    almacen.close()


def test_sql_invalido_es_port_error():
    almacen = SqliteStorageAdapter(IN_MEMORY)
    with pytest.raises(PortError):
        almacen.query("SELECT * FROM tabla_que_no_existe")
    almacen.close()


def test_columns_devuelve_los_nombres_en_orden():
    """
    Issue #6: un visor de la base necesita esto sin hablar SQLite -- que lo
    resuelva el adapter (acá, PRAGMA table_info) y no quien lo usa.
    """
    almacen = SqliteStorageAdapter(IN_MEMORY)
    almacen.migrate({"t": 1}, {"t": {1: "CREATE TABLE t (id INTEGER, a TEXT, b INTEGER);"}})

    assert almacen.columns("t") == ["id", "a", "b"]
    almacen.close()


def test_columns_de_una_tabla_inexistente_es_lista_vacia():
    """
    Mismo criterio que `versions()`: las tablas válidas ya salen de
    `schema.SCHEMA`, así que "no existe" no necesita ser un caso especial
    para quien llama.
    """
    almacen = SqliteStorageAdapter(IN_MEMORY)
    assert almacen.columns("no_existe") == []
    almacen.close()


def test_columns_rechaza_un_nombre_que_no_es_un_identificador():
    """
    PRAGMA no acepta parámetros posicionales: `table` se interpola en el SQL
    sí o sí. Sin esta validación, un `table` que dejara de venir sólo de
    `schema.SCHEMA` sería una inyección directa.
    """
    almacen = SqliteStorageAdapter(IN_MEMORY)
    with pytest.raises(PortError, match="inválido"):
        almacen.columns("t); DROP TABLE t; --")
    almacen.close()


# ── Cifrado ─────────────────────────────────────────────────────────────


def test_available_contesta_aunque_la_libreria_este_rota(tmp_path, monkeypatch):
    """
    Una instalación **rota** de `cryptography` —presente pero sin su backend
    nativo— no falla con `ImportError`: el binding levanta algo que cuelga de
    `BaseException` y se escapa de un `except ImportError`.

    Esta property existe para contestar sí o no. Si además pudiera tumbar al que
    pregunta, el diagnóstico se caería exactamente en la máquina que tiene el
    problema que viene a reportar.
    """
    import builtins

    original = builtins.__import__

    def _romper(nombre, *args, **kwargs):
        if nombre.startswith("cryptography"):
            raise BaseException("el binding nativo explotó")  # noqa: TRY002
        return original(nombre, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _romper)
    adapter = FernetCryptoAdapter(tmp_path / "secret.key")

    assert adapter.available is False
    # Y al usarlo de verdad, sube como PortError y no como el panic pelado.
    with pytest.raises(PortError, match="cryptography"):
        adapter.encrypt("hola")


# ── Navegador ───────────────────────────────────────────────────────────


def test_browser_no_disponible_si_playwright_no_esta_instalado_o_roto(monkeypatch):
    """
    Mismo criterio que `CryptoPort.available`: sin el paquete, o con uno roto,
    `available` contesta que no en vez de levantar, y usarlo de verdad sube
    como `PortError` explícito.
    """
    import builtins

    original = builtins.__import__

    def _romper(nombre, *args, **kwargs):
        if nombre.startswith("playwright"):
            raise BaseException("el binding nativo explotó")  # noqa: TRY002
        return original(nombre, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _romper)
    adapter = PlaywrightBrowserAdapter()

    assert adapter.available is False
    with pytest.raises(PortError, match="playwright"):
        adapter.goto("https://example.com")
    adapter.close()


# ── Ventana de escritorio ───────────────────────────────────────────────
#
# Ninguno de los dos paquetes reales (pywinauto, pyatspi) está instalado en
# esta suite -- el primero sólo tiene sentido en Windows, y el segundo no es
# instalable con pip -- así que `available` da False de forma natural, sin
# necesidad de mockear el import como en el de arriba.


def test_window_pywinauto_no_disponible_sin_el_paquete():
    adapter = PywinautoWindowAdapter()

    assert adapter.available is False
    with pytest.raises(PortError, match="Windows"):
        adapter.find_window(title="Bloc de notas")


def test_window_atspi_no_disponible_sin_el_paquete():
    adapter = AtspiWindowAdapter()

    assert adapter.available is False
    with pytest.raises(PortError, match="accesibilidad"):
        adapter.find_window(title="Archivos")


def test_window_find_window_exige_titulo_o_proceso():
    for adapter in (PywinautoWindowAdapter(), AtspiWindowAdapter()):
        with pytest.raises(PortError, match="title.*process|find_window"):
            adapter.find_window()


class _FakeControlConInvoke:
    def __init__(self):
        self.invoked = False
        self.clicked = False

    def invoke(self):
        self.invoked = True

    def click_input(self):
        self.clicked = True


class _FakeControlSinInvoke:
    def __init__(self):
        self.clicked = False

    def click_input(self):
        self.clicked = True


class _FakeControlInvokeFalla:
    def __init__(self):
        self.clicked = False

    def invoke(self):
        raise RuntimeError("el control no soporta Invoke")

    def click_input(self):
        self.clicked = True


class _FakeVentanaPywinauto:
    def __init__(self, control, descendientes=()):
        self._control = control
        self._descendientes = list(descendientes)
        self.criterios = None

    def child_window(self, **criterios):
        self.criterios = criterios
        return self._control

    def descendants(self, **_criterios):
        return self._descendientes

    def window_text(self):
        return "ventana"


def test_window_pywinauto_click_prefiere_invoke_sobre_mover_el_mouse():
    """
    Issue #13: `click_input()` mueve el cursor físico (`SetCursorPos`) y falla
    en una máquina con algo que se apropia del cursor (un KVM por software, un
    cliente de acceso remoto). El patrón Invoke de UI Automation no toca el
    mouse, así que va primero.
    """
    adapter = PywinautoWindowAdapter()
    control = _FakeControlConInvoke()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(control)

    adapter.click(WindowInfo(handle="1", title="x"), "Export")

    assert control.invoked is True
    assert control.clicked is False


def test_window_pywinauto_click_cae_a_simular_el_mouse_sin_invoke():
    adapter = PywinautoWindowAdapter()
    control = _FakeControlSinInvoke()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(control)

    adapter.click(WindowInfo(handle="1", title="x"), "campo")

    assert control.clicked is True


def test_window_pywinauto_click_cae_a_simular_el_mouse_si_invoke_falla():
    """No todo control soporta Invoke (un campo de texto, por ejemplo): cae al mouse."""
    adapter = PywinautoWindowAdapter()
    control = _FakeControlInvokeFalla()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(control)

    adapter.click(WindowInfo(handle="1", title="x"), "campo")

    assert control.clicked is True


# ── Issue #25: click con botón, y read_state ─────────────────────────────


class _FakeControlConBoton:
    def __init__(self):
        self.clicked_con = None

    def invoke(self):
        raise AssertionError("un click distinto de left no debería pasar por Invoke")

    def click_input(self, button="left"):
        self.clicked_con = button


def test_window_pywinauto_click_derecho_no_pasa_por_invoke_y_usa_el_mouse():
    """
    Issue #25: un click derecho no tiene patrón de UIA equivalente a Invoke
    -- un menú contextual es un evento de mouse --, así que sale directo por
    `click_input(button="right")`, sin intentar Invoke primero.
    """
    adapter = PywinautoWindowAdapter()
    control = _FakeControlConBoton()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(control)

    adapter.click(WindowInfo(handle="1", title="x"), "Export", button="right")

    assert control.clicked_con == "right"


class _FakeControlToggle:
    def __init__(self, estado: int):
        self._estado = estado

    def get_toggle_state(self):
        return self._estado


class _FakeControlSeleccionable:
    def __init__(self, seleccionado: bool):
        self._seleccionado = seleccionado

    def is_selected(self):
        return self._seleccionado


class _FakeControlSinEstado:
    pass


@pytest.mark.parametrize("crudo,esperado", [(0, "off"), (1, "on"), (2, "indeterminate")])
def test_window_pywinauto_read_state_de_un_checkbox(crudo, esperado):
    adapter = PywinautoWindowAdapter()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(_FakeControlToggle(crudo))

    assert adapter.read_state(WindowInfo(handle="1", title="x"), "Lip Flat") == esperado


@pytest.mark.parametrize("seleccionado,esperado", [(True, "on"), (False, "off")])
def test_window_pywinauto_read_state_de_un_radio(seleccionado, esperado):
    adapter = PywinautoWindowAdapter()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(_FakeControlSeleccionable(seleccionado))

    assert adapter.read_state(WindowInfo(handle="1", title="x"), "Metric") == esperado


def test_window_pywinauto_read_state_de_un_control_sin_estado_es_none():
    """Un botón o una etiqueta no tienen estado: `None`, no `PortError`."""
    adapter = PywinautoWindowAdapter()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(_FakeControlSinEstado())

    assert adapter.read_state(WindowInfo(handle="1", title="x"), "Aceptar") is None


def test_window_pywinauto_sugiere_proceso_elevado_cuando_no_se_encuentra(monkeypatch):
    """
    Issue #13: un proceso corriendo como administrador no lo puede abrir un
    Bot sin elevar, y pywinauto lo reporta como "no encontrado" -- el tipo de
    la excepción (`ProcessNotFoundError`) es la única pista de que es esto y
    no una ventana que realmente no está.
    """

    class ProcessNotFoundError(Exception):
        pass

    class _AplicacionRota:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, **kwargs):
            raise ProcessNotFoundError("no matching process")

    adapter = PywinautoWindowAdapter()
    monkeypatch.setattr(adapter, "_application", lambda: _AplicacionRota)

    with pytest.raises(PortError, match="administrador"):
        adapter.find_window(title="Export")


# ── Issue #14: selector de control con tipo ─────────────────────────────


def test_selector_sin_tipo_es_solo_titulo_como_siempre():
    from backend.adapters.window_pywinauto import _criterios

    assert _criterios("Seleccionar carpeta") == {"title": "Seleccionar carpeta"}


def test_selector_con_tipo_agrega_control_type():
    from backend.adapters.window_pywinauto import _criterios

    assert _criterios("Button:Seleccionar carpeta") == {
        "control_type": "Button",
        "title": "Seleccionar carpeta",
    }


def test_selector_con_tipo_y_sin_titulo_busca_solo_por_tipo():
    """`"Document:"` es el editor de un Notepad, que no tiene título propio."""
    from backend.adapters.window_pywinauto import _criterios

    assert _criterios("Document:") == {"control_type": "Document"}


def test_un_titulo_que_termina_en_dos_puntos_sigue_siendo_un_titulo():
    """
    El caso que obliga a que la lista de tipos sea cerrada: `"Carpeta:"` es un
    rótulo real de un diálogo de Windows, y tratarlo como tipo rompería lo que
    ya funcionaba.
    """
    from backend.adapters.window_pywinauto import _criterios

    assert _criterios("Carpeta:") == {"title": "Carpeta:"}
    # Y con el tipo delante, el título conserva sus propios ":".
    assert _criterios("Edit:Carpeta:") == {"control_type": "Edit", "title": "Carpeta:"}


def test_window_pywinauto_pasa_el_tipo_del_selector_a_child_window():
    adapter = PywinautoWindowAdapter()
    ventana = _FakeVentanaPywinauto(_FakeControlConInvoke())
    adapter._ventanas["1"] = ventana

    adapter.click(WindowInfo(handle="1", title="x"), "Button:Seleccionar carpeta")

    assert ventana.criterios == {"control_type": "Button", "title": "Seleccionar carpeta"}


def test_window_pywinauto_explica_un_selector_ambiguo_en_vez_de_repetir_el_error_crudo():
    """
    Issue #14: en un diálogo estándar `"Carpeta:"` matchea el rótulo y el
    campo. `ElementAmbiguousError: There are 2 elements…` no dice qué hacer;
    la salida es agregarle el tipo al selector, así que el mensaje lo dice.
    """

    class ElementAmbiguousError(Exception):
        pass

    class _ControlAmbiguo:
        def set_text(self, texto):
            raise ElementAmbiguousError("There are 2 elements that match")

    class _Candidato:
        def __init__(self, tipo):
            self.element_info = type("info", (), {"control_type": tipo})()

    adapter = PywinautoWindowAdapter()
    adapter._ventanas["1"] = _FakeVentanaPywinauto(
        _ControlAmbiguo(), descendientes=[_Candidato("Edit"), _Candidato("Text")]
    )

    with pytest.raises(PortError) as exc:
        adapter.type_text(WindowInfo(handle="1", title="x"), "Carpeta:", "/ruta")

    mensaje = str(exc.value)
    assert "2 controles matchean" in mensaje
    assert "Edit, Text" in mensaje
    assert '"Edit:Carpeta:"' in mensaje


def test_window_atspi_click_derecho_no_soportado():
    """
    Issue #25: AT-SPI dispara la acción por defecto del control, no un evento
    de mouse -- no hay un "right click" que pedirle, ni siquiera cayendo a
    simular el mouse como hace el adapter de Windows.
    """
    adapter = AtspiWindowAdapter()
    with pytest.raises(PortError, match="right click"):
        adapter.click(WindowInfo(handle="1", title="x"), "Guardar", button="right")


class _FakeEstadoSet:
    def __init__(self, estados: frozenset):
        self._estados = estados

    def contains(self, estado):
        return estado in self._estados


class _FakeAccesibleConEstado:
    def __init__(self, role: str, estados: frozenset = frozenset()):
        self._role = role
        self._estados = estados

    def getRoleName(self):
        return self._role

    def getState(self):
        return _FakeEstadoSet(self._estados)


class _FakePyatspiModulo:
    STATE_CHECKED = "checked"
    STATE_INDETERMINATE = "indeterminate"


def _adapter_atspi_con_control(monkeypatch, control):
    """Un `AtspiWindowAdapter` que resuelve cualquier `read_state` a `control`, sin tocar pyatspi de verdad."""
    adapter = AtspiWindowAdapter()
    adapter._ventanas["1"] = object()
    monkeypatch.setattr(adapter, "_registry", lambda: _FakePyatspiModulo)
    monkeypatch.setattr(adapter, "_buscar_control", lambda ventana, sel: control)
    return adapter


def test_window_atspi_read_state_checkbox_tildado(monkeypatch):
    control = _FakeAccesibleConEstado("check box", frozenset({"checked"}))
    adapter = _adapter_atspi_con_control(monkeypatch, control)

    assert adapter.read_state(WindowInfo(handle="1", title="x"), "Lip Flat") == "on"


def test_window_atspi_read_state_checkbox_destildado(monkeypatch):
    control = _FakeAccesibleConEstado("check box", frozenset())
    adapter = _adapter_atspi_con_control(monkeypatch, control)

    assert adapter.read_state(WindowInfo(handle="1", title="x"), "Lip Flat") == "off"


def test_window_atspi_read_state_indeterminado(monkeypatch):
    control = _FakeAccesibleConEstado("check box", frozenset({"indeterminate"}))
    adapter = _adapter_atspi_con_control(monkeypatch, control)

    assert adapter.read_state(WindowInfo(handle="1", title="x"), "Lip Flat") == "indeterminate"


def test_window_atspi_read_state_de_un_control_sin_estado_es_none(monkeypatch):
    """Un botón no tiene el rol de nada "con estado": `None`, no `PortError`."""
    control = _FakeAccesibleConEstado("push button")
    adapter = _adapter_atspi_con_control(monkeypatch, control)

    assert adapter.read_state(WindowInfo(handle="1", title="x"), "Aceptar") is None


def test_window_atspi_mapea_el_tipo_al_rol_de_accesibilidad():
    """El mismo selector, traducido al vocabulario del árbol de accesibilidad."""
    from backend.adapters.window_atspi import _selector

    assert _selector("Button:Guardar") == ("push button", "Guardar")
    assert _selector("Edit:Carpeta:") == ("text", "Carpeta:")
    assert _selector("Document:") == ("document frame", "")
    # Sin tipo conocido delante, todo es título -- como siempre.
    assert _selector("Guardar") == (None, "Guardar")
    assert _selector("Carpeta:") == (None, "Carpeta:")


def test_window_unsupported_en_un_sistema_operativo_sin_adapter():
    """El de reserva de un sistema sin automatización real, no un mock: es la forma tal cual."""
    adapter = UnsupportedWindowAdapter("Darwin")

    assert adapter.available is False
    ventana = WindowInfo(handle="1", title="cualquiera")
    with pytest.raises(PortError, match="Darwin"):
        adapter.find_window(title="x")
    with pytest.raises(PortError, match="Darwin"):
        adapter.click(ventana, "boton")
    with pytest.raises(PortError, match="Darwin"):
        adapter.type_text(ventana, "campo", "texto")
    with pytest.raises(PortError, match="Darwin"):
        adapter.read_text(ventana)
    with pytest.raises(PortError, match="Darwin"):
        adapter.read_state(ventana, "checkbox")


# ── Geometría (issue #19) ─────────────────────────────────────────────


def test_geometry_null_no_tiene_ningun_computo_real_detras():
    """
    El de reserva que trae el core, no un mock: `available` es False siempre,
    porque el núcleo deliberadamente no bundlea ningún adapter de geometría
    de fábrica (issue #19). Usarlo de verdad falla explícito.
    """
    adapter = NullGeometryAdapter()

    assert adapter.available is False
    with pytest.raises(PortError, match="geometría"):
        adapter.nearest_on_surface(b"stl-falso", [(0.0, 0.0, 0.0)])
