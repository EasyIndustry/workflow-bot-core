"""
`WindowPort` sobre pywinauto — automatización de ventanas nativas de Windows.

Mismo motivo que Playwright para el navegador: encontrar una ventana y un
control por nombre en vez de por coordenadas es un problema que la propia
UI Automation de Windows ya resuelve, y escribirlo de nuevo acá sólo suma
superficie de bugs.

Sólo funciona en Windows: en cualquier otro sistema, `available` es False y
usarlo de verdad levanta `PortError` con un mensaje explícito — mismo
criterio que `PlaywrightBrowserAdapter` cuando el paquete no está instalado.
No lo elige un plugin: lo enruta `adapters.build_default_adapters` según el
sistema operativo donde corre la instalación.
"""

from __future__ import annotations

from backend.core.ports import PortError, WindowInfo

DEFAULT_TIMEOUT = 30.0


class PywinautoWindowAdapter:
    """
    Una ventana Win32 real, manejada por pywinauto (backend UI Automation).

    Cachea cada ventana encontrada bajo el `handle` que le entrega a quien
    llama, para no tener que rebuscarla en cada `click`/`type_text`/`read_text`
    — el mismo criterio que un handle de sistema operativo.
    """

    def __init__(self, *, default_timeout: float = DEFAULT_TIMEOUT) -> None:
        self.default_timeout = default_timeout
        self._ventanas: dict[str, object] = {}
        self._contador = 0

    @property
    def available(self) -> bool:
        """Si el paquete que maneja ventanas de Windows se puede usar en esta máquina."""
        try:
            import pywinauto  # noqa: F401
        except BaseException:
            return False
        return True

    def _application(self):
        try:
            from pywinauto import Application
        except BaseException as exc:
            raise PortError(
                "No se puede usar el paquete que maneja ventanas nativas de "
                f"Windows ({type(exc).__name__}: {exc}). Revisá la instalación "
                "de las dependencias antes de usar este port."
            ) from exc
        return Application

    def find_window(self, *, title=None, process=None, timeout=None):
        if not title and not process:
            raise PortError("find_window necesita title o process")

        Application = self._application()
        espera = timeout or self.default_timeout
        try:
            kwargs = {}
            if title:
                kwargs["title_re"] = f".*{title}.*"
            if process:
                kwargs["path"] = process
            app = Application(backend="uia").connect(timeout=espera, **kwargs)
            ventana = app.top_window()
            ventana.wait("exists", timeout=espera)
        except Exception as exc:
            raise PortError(
                f"no se encontró una ventana con title={title!r} process={process!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        self._contador += 1
        handle = str(self._contador)
        self._ventanas[handle] = ventana
        return WindowInfo(handle=handle, title=ventana.window_text(), process=process or "")

    def _resolver(self, window: WindowInfo):
        ventana = self._ventanas.get(window.handle)
        if ventana is None:
            raise PortError(
                f"el handle {window.handle!r} no viene de este adapter "
                "(¿se reusó un WindowInfo entre corridas?)"
            )
        return ventana

    def click(self, window, control, *, timeout=None):
        ventana = self._resolver(window)
        try:
            ventana.child_window(title=control).click_input()
        except Exception as exc:
            raise PortError(
                f"no se pudo clickear {control!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def type_text(self, window, control, text, *, timeout=None):
        ventana = self._resolver(window)
        try:
            ventana.child_window(title=control).set_text(text)
        except Exception as exc:
            raise PortError(
                f"no se pudo escribir en {control!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def read_text(self, window, control=None, *, timeout=None):
        ventana = self._resolver(window)
        try:
            objetivo = ventana.child_window(title=control) if control else ventana
            return (objetivo.window_text() or "").strip()
        except Exception as exc:
            raise PortError(
                f"no se pudo leer {control!r}: {type(exc).__name__}: {exc}"
            ) from exc


__all__ = ["DEFAULT_TIMEOUT", "PywinautoWindowAdapter"]
