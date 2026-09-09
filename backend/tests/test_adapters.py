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
from backend.adapters.http_urllib import UrllibHttpAdapter
from backend.adapters.process_subprocess import SubprocessAdapter
from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter
from backend.core.ports import PortError


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
