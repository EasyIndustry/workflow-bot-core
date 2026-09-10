"""
`WindowPort` sobre AT-SPI — automatización de ventanas nativas de Linux vía
el árbol de accesibilidad que el propio escritorio ya expone.

Mismo criterio que el adapter de Windows: usar el árbol pensado para
lectores de pantalla (buscar un control por nombre) en vez de coordenadas de
píxel, que es frágil ante cualquier cambio de resolución o de tema.

Sólo funciona con un bus de accesibilidad activo (GNOME/KDE con AT-SPI
habilitado, y el paquete del sistema instalado). En cualquier otra máquina,
`available` es False y usarlo de verdad levanta `PortError` explícito — mismo
criterio que el resto de los adapters opcionales. No lo elige un plugin: lo
enruta `adapters.build_default_adapters` según el sistema operativo donde
corre la instalación.
"""

from __future__ import annotations

from backend.core.ports import PortError, WindowInfo

DEFAULT_TIMEOUT = 30.0


class AtspiWindowAdapter:
    """
    Una ventana real del escritorio, manejada por el árbol de accesibilidad.

    Cachea cada ventana encontrada bajo el `handle` que le entrega a quien
    llama, para no tener que recorrer el árbol de nuevo en cada
    `click`/`type_text`/`read_text` — el mismo criterio que el adapter de
    Windows.
    """

    def __init__(self, *, default_timeout: float = DEFAULT_TIMEOUT) -> None:
        self.default_timeout = default_timeout
        self._ventanas: dict[str, object] = {}
        self._contador = 0

    @property
    def available(self) -> bool:
        """Si el binding del árbol de accesibilidad se puede usar en esta máquina."""
        try:
            import pyatspi  # noqa: F401
        except BaseException:
            return False
        return True

    def _registry(self):
        try:
            import pyatspi
        except BaseException as exc:
            raise PortError(
                "No se puede usar el árbol de accesibilidad del escritorio "
                f"({type(exc).__name__}: {exc}). Revisá que el paquete del "
                "sistema esté instalado y el bus de accesibilidad habilitado "
                "antes de usar este port."
            ) from exc
        return pyatspi

    def find_window(self, *, title=None, process=None, timeout=None):
        if not title and not process:
            raise PortError("find_window necesita title o process")

        pyatspi = self._registry()
        try:
            escritorio = pyatspi.Registry.getDesktop(0)
            encontrada = None
            for app in escritorio:
                if process and (not app.name or process.lower() not in app.name.lower()):
                    continue
                for ventana in app:
                    if title and title.lower() not in (ventana.name or "").lower():
                        continue
                    encontrada = (app, ventana)
                    break
                if encontrada:
                    break
        except Exception as exc:
            raise PortError(
                f"fallo consultando el árbol de accesibilidad: {type(exc).__name__}: {exc}"
            ) from exc

        if encontrada is None:
            raise PortError(f"no se encontró una ventana con title={title!r} process={process!r}")

        app, ventana = encontrada
        self._contador += 1
        handle = str(self._contador)
        self._ventanas[handle] = ventana
        return WindowInfo(handle=handle, title=ventana.name or "", process=app.name or "")

    def _resolver(self, window: WindowInfo):
        ventana = self._ventanas.get(window.handle)
        if ventana is None:
            raise PortError(
                f"el handle {window.handle!r} no viene de este adapter "
                "(¿se reusó un WindowInfo entre corridas?)"
            )
        return ventana

    def _buscar_control(self, ventana, control: str):
        pyatspi = self._registry()
        encontrado = pyatspi.utils.findDescendant(
            ventana, lambda acc: acc.name == control, breadth_first=True
        )
        if encontrado is None:
            raise PortError(f"no se encontró el control {control!r}")
        return encontrado

    def click(self, window, control, *, timeout=None):
        objetivo = self._buscar_control(self._resolver(window), control)
        try:
            objetivo.queryAction().doAction(0)
        except Exception as exc:
            raise PortError(
                f"no se pudo clickear {control!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def type_text(self, window, control, text, *, timeout=None):
        objetivo = self._buscar_control(self._resolver(window), control)
        try:
            objetivo.queryEditableText().setTextContents(text)
        except Exception as exc:
            raise PortError(
                f"no se pudo escribir en {control!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def read_text(self, window, control=None, *, timeout=None):
        ventana = self._resolver(window)
        objetivo = self._buscar_control(ventana, control) if control else ventana
        try:
            texto = objetivo.queryText()
            return texto.getText(0, texto.characterCount) or ""
        except NotImplementedError:
            return (objetivo.name or "").strip()
        except Exception as exc:
            raise PortError(
                f"no se pudo leer {control!r}: {type(exc).__name__}: {exc}"
            ) from exc


__all__ = ["DEFAULT_TIMEOUT", "AtspiWindowAdapter"]
