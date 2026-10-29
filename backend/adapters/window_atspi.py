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

# El selector de un control admite un tipo delante del título, separado por
# ":" (issue #14). Los nombres son los de UI Automation -- los que muestra
# cualquier inspector de Windows, y los que el port documenta-- y acá se
# mapean al rol equivalente del árbol de accesibilidad. Un prefijo que no esté
# en esta tabla se trata como parte del título, igual que siempre.
_ROLES = {
    "Button": "push button",
    "CheckBox": "check box",
    "ComboBox": "combo box",
    "Document": "document frame",
    "Edit": "text",
    "Group": "panel",
    "Hyperlink": "link",
    "Image": "image",
    "List": "list",
    "ListItem": "list item",
    "Menu": "menu",
    "MenuBar": "menu bar",
    "MenuItem": "menu item",
    "ProgressBar": "progress bar",
    "RadioButton": "radio button",
    "Slider": "slider",
    "StatusBar": "status bar",
    "Tab": "page tab list",
    "TabItem": "page tab",
    "Table": "table",
    "Text": "label",
    "ToolBar": "tool bar",
    "Tree": "tree",
    "TreeItem": "tree item",
    "Window": "frame",
}


def _selector(control: str) -> tuple[str | None, str]:
    """
    `"Button:Guardar"` -> ("push button", "Guardar"); `"Guardar"` -> (None, "Guardar").

    El corte es en el PRIMER ":": un título puede tener los suyos, el nombre
    de un tipo no.
    """
    tipo, separador, titulo = control.partition(":")
    if not separador or tipo not in _ROLES:
        return None, control
    return _ROLES[tipo], titulo


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
        """
        El control por título y -si el selector lo dice- por rol.

        Un título sin rol alcanza en la mayoría de las apps, pero en un
        diálogo estándar el mismo texto aparece dos veces (el rótulo y el
        campo que rotula), y sin el rol se toma el primero que aparezca
        (issue #14).
        """
        pyatspi = self._registry()
        rol, titulo = _selector(control)

        def coincide(acc) -> bool:
            if titulo and (acc.name or "") != titulo:
                return False
            return rol is None or acc.getRoleName() == rol

        encontrado = pyatspi.utils.findDescendant(ventana, coincide, breadth_first=True)
        if encontrado is None:
            raise PortError(f"no se encontró el control {control!r}")
        return encontrado

    def click(self, window, control, *, button="left", timeout=None):
        """
        Clickea `control` disparando su acción de accesibilidad
        (`queryAction().doAction(0)`), no moviendo el mouse: no hay
        `SetCursorPos` que romperse en una máquina con algo que se apropia del
        cursor (issue #13, visto en el adapter de Windows).

        Sólo el click principal (issue #25): la interfaz de acción de AT-SPI
        dispara la acción por defecto del control -- lo que un lector de
        pantalla activaría con Enter --, no un evento de mouse con un botón
        particular. No hay un "click derecho" que disparar por acá: a
        diferencia del adapter de Windows, que puede caer a `click_input`
        moviendo el mouse, acá no existe ese camino en absoluto.
        """
        if button != "left":
            raise PortError(
                f"este adapter no puede hacer {button} click: AT-SPI dispara la "
                "acción por defecto del control, no un evento de mouse con un "
                "botón particular"
            )
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

    # Roles con noción de estado tildado -- el resto (un botón, una etiqueta)
    # no tiene nada que `read_state` pueda contestar (issue #25).
    _ROLES_CON_ESTADO = frozenset({"check box", "radio button", "toggle button", "check menu item"})

    def read_state(self, window, control, *, timeout=None):
        """
        `"on"`/`"off"`/`"indeterminate"` de un control con estado, `None` si
        no tiene (issue #25).

        El estado vive en el state set del elemento (`STATE_CHECKED`/
        `STATE_INDETERMINATE`), no en su nombre accesible -- que es lo que
        `read_text` devuelve y no cambia si el checkbox se tilda o no.
        """
        pyatspi = self._registry()
        objetivo = self._buscar_control(self._resolver(window), control)
        if objetivo.getRoleName() not in self._ROLES_CON_ESTADO:
            return None
        try:
            estados = objetivo.getState()
        except Exception as exc:
            raise PortError(
                f"no se pudo leer el estado de {control!r}: {type(exc).__name__}: {exc}"
            ) from exc
        if estados.contains(pyatspi.STATE_INDETERMINATE):
            return "indeterminate"
        return "on" if estados.contains(pyatspi.STATE_CHECKED) else "off"

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
