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

# Los tipos de UI Automation que un selector puede nombrar delante del título,
# separados por ":" -- `"Button:Seleccionar carpeta"` (issue #14).
#
# Es una lista cerrada porque UIA la define cerrada, y porque es lo único que
# permite distinguir un tipo de un título que termina en ":": `"Carpeta:"` es
# el título de un rótulo real de Windows, y `"Document:"` es un tipo sin
# título. Un prefijo que no esté acá se trata como parte del título, que es el
# comportamiento de siempre.
_TIPOS_UIA = frozenset({
    "Button", "Calendar", "CheckBox", "ComboBox", "Custom", "DataGrid", "DataItem",
    "Document", "Edit", "Group", "Header", "HeaderItem", "Hyperlink", "Image", "List",
    "ListItem", "Menu", "MenuBar", "MenuItem", "Pane", "ProgressBar", "RadioButton",
    "ScrollBar", "Separator", "Slider", "Spinner", "SplitButton", "StatusBar", "Tab",
    "TabItem", "Table", "Text", "Thumb", "TitleBar", "ToolBar", "ToolTip", "Tree",
    "TreeItem", "Window",
})


def _criterios(control: str) -> dict:
    """
    El selector de un control, como los criterios que `child_window` espera.

        "Seleccionar carpeta"        -> title
        "Button:Seleccionar carpeta" -> title + control_type
        "Document:"                  -> sólo control_type (un control sin título)

    El corte es en el PRIMER ":": un título puede tener los suyos
    ("Carpeta:"), el nombre de un tipo no.
    """
    tipo, separador, titulo = control.partition(":")
    if not separador or tipo not in _TIPOS_UIA:
        return {"title": control}
    return {"control_type": tipo, **({"title": titulo} if titulo else {})}


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
            # Un proceso corriendo "como administrador" no lo puede abrir un Bot
            # sin elevar (OpenProcess deniega el acceso), y pywinauto lo reporta
            # como "no encontrado" -- aunque el PID exista -- porque no pudo
            # confirmar que ese proceso matchea (issue #13). Es la causa más común
            # de este error cuando la ventana está, a la vista, en la pantalla.
            pista = (
                " (si la ventana está abierta pero no aparece, puede estar "
                "corriendo como administrador: un proceso no elevado no puede "
                "verla ni controlarla)"
                if type(exc).__name__ == "ProcessNotFoundError"
                else ""
            )
            raise PortError(
                f"no se encontró una ventana con title={title!r} process={process!r}: "
                f"{type(exc).__name__}: {exc}{pista}"
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

    def _detalle(self, ventana, control: str, exc: Exception) -> str:
        """
        Qué salió mal, y -si el selector matcheó más de un control- cuántos y
        de qué tipo.

        `ElementAmbiguousError` crudo ("There are 2 elements that match…") no
        dice qué hacer, y en un diálogo estándar de Windows es el caso más
        común: `"Carpeta:"` matchea el rótulo y el campo (issue #14). La
        salida casi siempre es agregarle el tipo al selector, así que el
        mensaje lo dice. Enumerar es otra llamada a una UI que ya se está
        portando raro: si falla, vuelve el mensaje original en vez de tapar
        el error real.
        """
        if type(exc).__name__ != "ElementAmbiguousError":
            return f"{type(exc).__name__}: {exc}"
        try:
            candidatos = ventana.descendants(**_criterios(control))
            tipos = sorted({c.element_info.control_type for c in candidatos})
        except Exception:
            return f"{type(exc).__name__}: {exc}"

        detalle = f"{len(candidatos)} controles matchean (de tipo: {', '.join(tipos)})"
        if "control_type" not in _criterios(control) and tipos:
            detalle += f'. Agregá el tipo al selector, por ejemplo "{tipos[0]}:{control}"'
        return detalle

    def click(self, window, control, *, button="left", timeout=None):
        """
        Clickea `control`.

        Preferí el patrón Invoke de UI Automation (`invoke()`) sobre simular el
        mouse (`click_input()`, que mueve el cursor físico con `SetCursorPos`):
        en una máquina con algo que se apropia del cursor -- un KVM por
        software, un cliente de acceso remoto -- `SetCursorPos` falla para
        cualquier proceso de esa máquina, Bot incluido (issue #13). Invoke no
        toca el mouse. Sólo cae a simularlo si el control no expone ese patrón
        (no todos los controles lo soportan, ej. un campo de texto).

        `button` distinto de `"left"` (issue #25) no tiene patrón de UIA
        equivalente a Invoke -- un menú contextual es un evento de mouse, no
        una acción del control -- así que sale directo por `click_input`, con
        la misma exposición a `SetCursorPos` que Invoke existe para evitar en
        el caso de siempre. No hay forma de rodearlo: documentado a propósito,
        no un descuido.
        """
        ventana = self._resolver(window)
        objetivo = ventana.child_window(**_criterios(control))

        if button != "left":
            try:
                objetivo.click_input(button=button)
            except Exception as exc:
                raise PortError(
                    f"no se pudo hacer {button} click en {control!r}: "
                    f"{self._detalle(ventana, control, exc)}"
                ) from exc
            return

        invocar = getattr(objetivo, "invoke", None)
        if callable(invocar):
            try:
                invocar()
                return
            except Exception as exc:
                # Un selector ambiguo falla igual con click_input, y con un
                # mensaje peor: se corta acá, con el que explica qué hacer.
                if type(exc).__name__ == "ElementAmbiguousError":
                    raise PortError(
                        f"no se pudo clickear {control!r}: "
                        f"{self._detalle(ventana, control, exc)}"
                    ) from exc
                # Cualquier otra cosa: el control no soporta Invoke, cae al mouse.

        try:
            objetivo.click_input()
        except Exception as exc:
            raise PortError(
                f"no se pudo clickear {control!r}: {self._detalle(ventana, control, exc)}"
            ) from exc

    def type_text(self, window, control, text, *, timeout=None):
        ventana = self._resolver(window)
        try:
            ventana.child_window(**_criterios(control)).set_text(text)
        except Exception as exc:
            raise PortError(
                f"no se pudo escribir en {control!r}: {self._detalle(ventana, control, exc)}"
            ) from exc

    # 0/1/2 es lo que UI Automation define para `TogglePattern.ToggleState`
    # (`ToggleState_Off`/`On`/`Indeterminate`), y lo que `get_toggle_state()`
    # de pywinauto devuelve tal cual (issue #25).
    _ESTADOS_TOGGLE = {0: "off", 1: "on", 2: "indeterminate"}

    def read_state(self, window, control, *, timeout=None):
        """
        `"on"`/`"off"`/`"indeterminate"` de un control con estado, `None` si
        no tiene (issue #25).

        Un checkbox expone `TogglePattern` (`get_toggle_state()` en el
        wrapper de pywinauto); un radio button no tiene toggle -- expone
        `is_selected()` en cambio. Ninguno de los dos existe en un control
        sin estado (un botón, una etiqueta), y ahí no hay nada raro que
        reportar: se devuelve `None`, no `PortError`.
        """
        ventana = self._resolver(window)
        try:
            objetivo = ventana.child_window(**_criterios(control))
        except Exception as exc:
            raise PortError(
                f"no se pudo leer el estado de {control!r}: {self._detalle(ventana, control, exc)}"
            ) from exc

        obtener_toggle = getattr(objetivo, "get_toggle_state", None)
        if callable(obtener_toggle):
            try:
                return self._ESTADOS_TOGGLE.get(obtener_toggle())
            except Exception as exc:
                raise PortError(
                    f"no se pudo leer el estado de {control!r}: {type(exc).__name__}: {exc}"
                ) from exc

        es_seleccionado = getattr(objetivo, "is_selected", None)
        if callable(es_seleccionado):
            try:
                return "on" if es_seleccionado() else "off"
            except Exception as exc:
                raise PortError(
                    f"no se pudo leer el estado de {control!r}: {type(exc).__name__}: {exc}"
                ) from exc

        return None

    def read_text(self, window, control=None, *, timeout=None):
        """
        El texto de `control`, o -sin `control`- el de la ventana entera.

        "El de la ventana entera" es lo que pywinauto expone como
        `window_text()` para un top-level: el título de la barra, no el
        contenido que muestra. Para leer lo que un Notepad tiene escrito, por
        ejemplo, hay que pedir el control (`"Edit:"`/`"Document:"`, según la
        app), no la ventana.
        """
        ventana = self._resolver(window)
        try:
            objetivo = ventana.child_window(**_criterios(control)) if control else ventana
            return (objetivo.window_text() or "").strip()
        except Exception as exc:
            raise PortError(
                f"no se pudo leer {control!r}: {self._detalle(ventana, control or '', exc)}"
            ) from exc


__all__ = ["DEFAULT_TIMEOUT", "PywinautoWindowAdapter"]
