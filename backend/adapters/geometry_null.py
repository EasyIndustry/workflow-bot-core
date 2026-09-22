"""
`GeometryPort` de reserva: siempre bindeado, nunca con cómputo real detrás.

`build_default_adapters()` tiene que ofrecer siempre algo bajo el port
`geometry` —lo exige `test_cada_port_declarado_tiene_su_adapter_por_defecto`—,
igual que `UnsupportedWindowAdapter` para `window`. La diferencia es que acá
no hay "sistema soportado" que esperar: el core deliberadamente no bundlea
ningún adapter de geometría real (issue #19) -- `trimesh`/`numpy` son
librerías curadas por cada instalación, no algo que el paquete `bot-core`
traiga de fábrica. `available` es False siempre; usarlo de verdad levanta
`PortError` explícito, para que un tool lo distinga de "la malla está mal" y
devuelva un error accionable en vez de un `ImportError` a mitad de un cómputo.
"""

from __future__ import annotations

from backend.core.ports import PortError


class NullGeometryAdapter:
    """Ningún adapter de geometría configurado en esta instalación."""

    @property
    def available(self) -> bool:
        return False

    def nearest_on_surface(self, mesh_bytes: bytes, points):
        raise PortError(
            "no hay un adapter de geometría configurado en esta instalación. "
            "El core no bundlea ninguno de fábrica (issue #19): instalá/escribí "
            "uno propio (por ejemplo sobre `trimesh`) y bindealo al port "
            "`geometry` en `Instance(adapters={...})`."
        )


__all__ = ["NullGeometryAdapter"]
