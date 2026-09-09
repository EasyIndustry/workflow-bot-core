"""
`BrowserPort` sobre Playwright.

Playwright y no Selenium por lo mismo que `cryptography` en vez de cifrado
propio: automatizar un navegador a mano —levantar el proceso, hablar el
protocolo de depuración— es una clase de código que ya está resuelta, y
resolverla de nuevo acá sólo suma superficie de bugs.

**Sesión y perfil son decisión de quien arma la instalación, no de este
archivo.** `user_data_dir` es opcional: si se da, Playwright abre un contexto
persistente en esa carpeta y las cookies (sesión incluida) sobreviven entre
corridas; si no, cada uso arranca un navegador en blanco. Qué carpeta usar, y
qué hacer cuando la sesión ya no sirve —hoy: relogueo manual—, es lógica de
negocio y vive en el plugin que use este port, nunca acá.

Por eso el adapter por defecto (`build_default_adapters`) no persiste nada:
una instalación que sí necesite sesión persistente inyecta la suya propia
con `Instance(root, adapters={**build_default_adapters(), "browser": ...})`.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.ports import PortError

DEFAULT_TIMEOUT = 30.0


class PlaywrightBrowserAdapter:
    """
    Un navegador Chromium real, manejado por Playwright.

    Arranca perezoso: no hay proceso de navegador hasta el primer `goto`,
    `click`, `leer_texto` o `screenshot`. `close()` lo apaga; sin llamarlo, el
    proceso queda vivo hasta que el intérprete termina.
    """

    def __init__(
        self,
        user_data_dir: str | Path | None = None,
        *,
        headless: bool = True,
        default_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.user_data_dir = str(user_data_dir) if user_data_dir else None
        self.headless = headless
        self.default_timeout = default_timeout
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def available(self) -> bool:
        """
        Si el paquete `playwright` se puede usar en esta máquina.

        Sólo el paquete Python: los navegadores (`playwright install
        chromium`) son un paso aparte, y su ausencia se ve recién al intentar
        arrancar uno, con un `PortError` explícito y no acá.
        """
        try:
            import playwright.sync_api  # noqa: F401
        except BaseException:
            return False
        return True

    def _pagina(self):
        if self._page is not None:
            return self._page

        try:
            from playwright.sync_api import sync_playwright
        except BaseException as exc:
            raise PortError(
                "No se puede usar el paquete `playwright`, que es lo que "
                f"maneja el navegador ({type(exc).__name__}: {exc}). Revisá "
                "la instalación de las dependencias antes de usar este port."
            ) from exc

        self._playwright = sync_playwright().start()
        try:
            if self.user_data_dir:
                self._context = self._playwright.chromium.launch_persistent_context(
                    self.user_data_dir, headless=self.headless
                )
                self._page = (
                    self._context.pages[0] if self._context.pages else self._context.new_page()
                )
            else:
                self._browser = self._playwright.chromium.launch(headless=self.headless)
                self._context = self._browser.new_context()
                self._page = self._context.new_page()
        except Exception as exc:
            self.close()
            raise PortError(
                f"no se pudo arrancar el navegador: {type(exc).__name__}: {exc}. "
                "¿Están instalados los navegadores de Playwright? "
                "(`playwright install chromium`)"
            ) from exc

        self._page.set_default_timeout(self.default_timeout * 1000)
        return self._page

    def goto(self, url: str, *, timeout: float | None = None) -> None:
        pagina = self._pagina()
        try:
            pagina.goto(url, timeout=(timeout or self.default_timeout) * 1000)
        except Exception as exc:
            raise PortError(f"no se pudo navegar a {url}: {type(exc).__name__}: {exc}") from exc

    def click(self, selector: str, *, timeout: float | None = None) -> None:
        pagina = self._pagina()
        try:
            pagina.click(selector, timeout=(timeout or self.default_timeout) * 1000)
        except Exception as exc:
            raise PortError(
                f"no se pudo clickear {selector!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def leer_texto(self, selector: str, *, timeout: float | None = None) -> str:
        pagina = self._pagina()
        try:
            elemento = pagina.wait_for_selector(
                selector, timeout=(timeout or self.default_timeout) * 1000
            )
        except Exception as exc:
            raise PortError(
                f"no se encontró {selector!r}: {type(exc).__name__}: {exc}"
            ) from exc
        return (elemento.text_content() or "").strip()

    def screenshot(self) -> bytes:
        return self._pagina().screenshot()

    def close(self) -> None:
        """Apaga el navegador. Nunca levanta: cerrar algo que ya está roto no es un error nuevo."""
        for obj in (self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None


__all__ = ["DEFAULT_TIMEOUT", "PlaywrightBrowserAdapter"]
