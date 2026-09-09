"""
Adapters falsos — el mundo, bajo control del test.

Es lo que la capa de ports existe para permitir. Con estos, la suite entera
corre sin red, sin disco, sin procesos y sin esperar tiempo real: un flujo con
cincuenta reintentos de sesenta segundos tarda milisegundos.

Cada fake registra lo que le pidieron (`calls`), así que un test puede afirmar
sobre el *efecto* —"se movió esta carpeta a esta otra"— y no sólo sobre el
status que devolvió el tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.core.ports import FileInfo, HttpResponse, PortError, ProcessResult


class FakeHttp:
    """
    Cliente HTTP guionado.

    `responses` mapea URL (o un prefijo) a `HttpResponse` o a una excepción. Una
    URL no guionada levanta `PortError`: un test que no dijo qué esperaba de la
    red tiene un agujero, y fallar es mejor que devolverle un 200 vacío.
    """

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[dict] = []

    def stub(self, url: str, status: int = 200, text: str = "", headers: dict | None = None):
        self.responses[url] = HttpResponse(
            status=status,
            text=text,
            headers={"content-type": "application/json", **(headers or {})},
            url=url,
        )
        return self

    def request(self, url, *, method="GET", headers=None, body=None, timeout=30.0):
        self.calls.append(
            {"url": url, "method": method, "headers": dict(headers or {}), "body": body,
             "timeout": timeout}
        )
        respuesta = self.responses.get(url)
        if respuesta is None:
            respuesta = next(
                (v for k, v in self.responses.items() if url.startswith(k)), None
            )
        if respuesta is None:
            raise PortError(f"FakeHttp: nadie guionó {url}")
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


class FakeFs:
    """
    Filesystem en memoria.

    `files` es `{ruta: contenido}` y `dirs` el conjunto de carpetas. Las rutas se
    normalizan a `/` para que un test escrito con rutas de Windows corra igual en
    Linux — que es justo la portabilidad que el port promete.
    """

    def __init__(self, files: dict | None = None, dirs=()) -> None:
        self.files: dict[str, str] = {_norm(k): v for k, v in (files or {}).items()}
        self.dirs: set[str] = {_norm(d) for d in dirs}
        for ruta in self.files:
            self._asegurar_padres(ruta)
        self.calls: list[tuple] = []

    def _asegurar_padres(self, ruta: str) -> None:
        partes = _norm(ruta).split("/")
        for i in range(1, len(partes)):
            self.dirs.add("/".join(partes[:i]))

    # ── Consulta ────────────────────────────────────────────────────────

    def exists(self, path):
        p = _norm(path)
        return p in self.files or p in self.dirs

    def is_dir(self, path):
        return _norm(path) in self.dirs

    def stat(self, path):
        p = _norm(path)
        if p in self.dirs:
            return FileInfo(path=p, name=p.rsplit("/", 1)[-1], is_dir=True)
        if p in self.files:
            return FileInfo(
                path=p, name=p.rsplit("/", 1)[-1], is_dir=False, size=len(self.files[p])
            )
        raise PortError(f"FakeFs: no existe {path}")

    def list_dir(self, path):
        p = _norm(path)
        if p not in self.dirs:
            raise PortError(f"FakeFs: no es una carpeta: {path}")
        hijos = []
        for ruta in sorted(self.dirs | set(self.files)):
            if ruta.startswith(p + "/") and "/" not in ruta[len(p) + 1:]:
                hijos.append(self.stat(ruta))
        return hijos

    def walk(self, path):
        p = _norm(path)
        for ruta in sorted(self.dirs | set(self.files)):
            if ruta != p and ruta.startswith(p + "/"):
                yield self.stat(ruta)

    # ── Escritura ───────────────────────────────────────────────────────

    def make_dirs(self, path):
        p = _norm(path)
        self.dirs.add(p)
        self._asegurar_padres(p + "/x")
        self.calls.append(("make_dirs", p))
        return p

    def move(self, source, dest):
        origen, destino = _norm(source), _norm(dest)
        final = f"{destino}/{origen.rsplit('/', 1)[-1]}" if destino in self.dirs else destino
        if origen in self.files:
            self.files[final] = self.files.pop(origen)
        elif origen in self.dirs:
            self._mover_arbol(origen, final)
        else:
            raise PortError(f"FakeFs: no existe el origen {source}")
        self._asegurar_padres(final)
        self.calls.append(("move", origen, final))
        return final

    def _mover_arbol(self, origen: str, final: str) -> None:
        self.dirs.discard(origen)
        self.dirs.add(final)
        for ruta in list(self.dirs):
            if ruta.startswith(origen + "/"):
                self.dirs.discard(ruta)
                self.dirs.add(final + ruta[len(origen):])
        for ruta in list(self.files):
            if ruta.startswith(origen + "/"):
                self.files[final + ruta[len(origen):]] = self.files.pop(ruta)

    def copy_file(self, source, dest):
        origen, destino = _norm(source), _norm(dest)
        if origen not in self.files:
            raise PortError(f"FakeFs: no es un archivo: {source}")
        final = f"{destino}/{origen.rsplit('/', 1)[-1]}" if destino in self.dirs else destino
        self.files[final] = self.files[origen]
        self._asegurar_padres(final)
        self.calls.append(("copy_file", origen, final))
        return final

    def copy_tree(self, source, dest):
        origen, destino = _norm(source), _norm(dest)
        final = f"{destino}/{origen.rsplit('/', 1)[-1]}" if destino in self.dirs else destino
        self.dirs.add(final)
        for ruta in list(self.files):
            if ruta.startswith(origen + "/"):
                self.files[final + ruta[len(origen):]] = self.files[ruta]
        self.calls.append(("copy_tree", origen, final))
        return final

    def remove_file(self, path):
        p = _norm(path)
        if p not in self.files:
            raise PortError(f"FakeFs: no existe {path}")
        del self.files[p]
        self.calls.append(("remove_file", p))

    def remove_tree(self, path):
        p = _norm(path)
        if p not in self.dirs:
            raise PortError(f"FakeFs: no es una carpeta: {path}")
        self.dirs = {d for d in self.dirs if d != p and not d.startswith(p + "/")}
        self.files = {k: v for k, v in self.files.items() if not k.startswith(p + "/")}
        self.calls.append(("remove_tree", p))

    def rename(self, path, new_name):
        p = _norm(path)
        final = p.rsplit("/", 1)[0] + "/" + new_name if "/" in p else new_name
        return self.move(p, final)

    # ── Rutas ───────────────────────────────────────────────────────────
    #
    # El fake normaliza todo a `/`, así que alcanza con partir por ahí. El
    # adapter real tiene que resolver los dos separadores.

    def parent(self, path):
        p = _norm(path)
        if "/" not in p:
            return ""
        corte = p.rfind("/")
        return p[0] if corte == 0 else p[:corte]

    def basename(self, path):
        return _norm(path).rsplit("/", 1)[-1]

    def join(self, *parts):
        tramos = [_norm(x) for x in parts if str(x) != ""]
        if not tramos:
            return ""
        return "/".join([tramos[0]] + [x.lstrip("/") for x in tramos[1:]])

    # ── Contenido ───────────────────────────────────────────────────────

    def read_text(self, path, encoding="utf-8"):
        p = _norm(path)
        if p not in self.files:
            raise PortError(f"FakeFs: no existe {path}")
        return self.files[p]

    def write_text(self, path, content, encoding="utf-8"):
        p = _norm(path)
        self.files[p] = content
        self._asegurar_padres(p)
        self.calls.append(("write_text", p))

    def read_bytes(self, path):
        return self.read_text(path).encode("utf-8")

    def write_bytes(self, path, content):
        self.write_text(path, content.decode("utf-8", errors="replace"))


class FakeProcess:
    """
    Ejecutor de comandos guionado.

    `results` mapea el nombre del ejecutable a un `ProcessResult`. Como en
    `FakeHttp`, un comando no guionado levanta: correr algo que el test no
    previó es un agujero, no un caso por defecto.
    """

    def __init__(self, results: dict | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[dict] = []

    def stub(self, executable: str, exit_code: int = 0, stdout: str = "", stderr: str = ""):
        self.results[executable] = ProcessResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr
        )
        return self

    def run(self, command, *, cwd=None, timeout=None, env=None):
        argv = [str(a) for a in command]
        self.calls.append({"command": argv, "cwd": cwd, "timeout": timeout})
        if not argv:
            raise PortError("FakeProcess: comando vacío")
        resultado = self.results.get(argv[0])
        if resultado is None:
            raise PortError(f"FakeProcess: nadie guionó '{argv[0]}'")
        return ProcessResult(
            exit_code=resultado.exit_code,
            stdout=resultado.stdout,
            stderr=resultado.stderr,
            timed_out=resultado.timed_out,
            command=tuple(argv),
        )


@dataclass
class FakeClock:
    """
    Reloj controlado por el test.

    `sleep` no duerme: adelanta el reloj. Es lo que convierte un flujo de
    reintentos de 50 minutos en un test de milisegundos, y además deja
    **verificable** cuánto se esperó (`slept`), que con `time.sleep` real es
    justamente lo que no se puede afirmar.
    """

    epoch: float = 1_700_000_000.0
    ticks: float = 0.0
    slept: list[float] = field(default_factory=list)
    cancel_after: int | None = None

    def now(self) -> float:
        return self.epoch + self.ticks

    def monotonic(self) -> float:
        return self.ticks

    def sleep(self, seconds: float, is_cancelled=None) -> None:
        self.slept.append(seconds)
        # Con `cancel_after`, la n-ésima espera se comporta como cancelada. Es
        # cómo se prueba que cancelar durante una espera larga surte efecto.
        if self.cancel_after is not None and len(self.slept) >= self.cancel_after:
            return
        self.ticks += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


class FakeBrowser:
    """
    Navegador guionado: nunca abre uno de verdad.

    `paginas` mapea una URL a los selectores que se pueden leer ahí, como
    `{url: {selector: texto}}`. Igual que `FakeHttp`: navegar a una URL no
    guionada, o leer un selector que no está en el guion, levanta PortError.
    """

    def __init__(self, paginas: dict | None = None) -> None:
        self.paginas = {url: dict(selectores) for url, selectores in (paginas or {}).items()}
        self.calls: list[dict] = []
        self.cerrado = False
        self._url_actual: str | None = None

    @property
    def available(self) -> bool:
        return True

    def goto(self, url, *, timeout=None):
        self.calls.append({"op": "goto", "url": url, "timeout": timeout})
        if url not in self.paginas:
            raise PortError(f"FakeBrowser: nadie guionó la página {url}")
        self._url_actual = url

    def click(self, selector, *, timeout=None):
        self.calls.append({"op": "click", "selector": selector, "timeout": timeout})
        if self._url_actual is None:
            raise PortError("FakeBrowser: click sin haber navegado antes")

    def leer_texto(self, selector, *, timeout=None):
        self.calls.append({"op": "leer_texto", "selector": selector, "timeout": timeout})
        selectores = self.paginas.get(self._url_actual or "", {})
        if selector not in selectores:
            raise PortError(f"FakeBrowser: nadie guionó el selector {selector!r}")
        return selectores[selector]

    def screenshot(self):
        self.calls.append({"op": "screenshot"})
        return b""

    def close(self):
        self.cerrado = True


def fake_adapters(**overrides) -> dict:
    """Los cinco ports en versión falsa. Se puede pisar cualquiera."""
    adapters = {
        "http": FakeHttp(),
        "fs": FakeFs(),
        "process": FakeProcess(),
        "clock": FakeClock(),
        "browser": FakeBrowser(),
    }
    adapters.update(overrides)
    return adapters


def _norm(path) -> str:
    """Rutas comparables: separadores unificados y sin barra final."""
    return str(path).replace("\\", "/").rstrip("/") or "/"


__all__ = [
    "FakeClock",
    "FakeFs",
    "FakeHttp",
    "FakeProcess",
    "fake_adapters",
]
