"""
`WindowPort` de reserva para un sistema operativo sin adapter propio.

`build_default_adapters()` tiene que ofrecer siempre algo bajo el port
`window` —lo exige `test_cada_port_declarado_tiene_su_adapter_por_defecto`—,
incluso en un sistema (hoy: cualquiera que no sea Windows o Linux) donde
todavía no hay una automatización real. `available` es False; usarlo de
verdad levanta `PortError` explícito en vez de fallar con un error confuso
más abajo.
"""

from __future__ import annotations

from backend.core.ports import PortError


class UnsupportedWindowAdapter:
    """Ningún adapter de ventanas para el sistema operativo detectado."""

    def __init__(self, sistema: str) -> None:
        self.sistema = sistema

    @property
    def available(self) -> bool:
        return False

    def _no_soportado(self):
        raise PortError(
            f"no hay un adapter de ventanas para este sistema operativo ({self.sistema})"
        )

    def find_window(self, *, title=None, process=None, timeout=None):
        self._no_soportado()

    def click(self, window, control, *, timeout=None):
        self._no_soportado()

    def type_text(self, window, control, text, *, timeout=None):
        self._no_soportado()

    def read_text(self, window, control=None, *, timeout=None):
        self._no_soportado()


__all__ = ["UnsupportedWindowAdapter"]
