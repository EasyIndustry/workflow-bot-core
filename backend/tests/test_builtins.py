"""
Tests de las primitivas del núcleo (`core.*`).

Acá vive sólo lo que no cabe en un test de flujo (`test_flow_executor.py`) ni
de arquitectura: la resolución de la versión que declara el manifest `core`.
"""

from __future__ import annotations

import backend.core.builtins as builtins


def test_version_del_nucleo_lee_el_paquete_instalado():
    """
    El manifest `core` no puede tener una constante propia: issue #5, un
    número pisado a mano se desincroniza del tag apenas se corta un release.
    """
    from importlib.metadata import version

    assert builtins._version_del_nucleo() == version("bot-core")
    assert builtins.MANIFEST.version == version("bot-core")


def test_version_del_nucleo_tiene_fallback_si_no_esta_instalado(monkeypatch):
    """
    Un `backend/` copiado a mano, sin pasar por pip, no tiene `.dist-info` de
    dónde leer: mejor un fallback explícito que fingir un número o reventar.
    """
    from importlib.metadata import PackageNotFoundError

    def _no_instalado(nombre):
        raise PackageNotFoundError(nombre)

    monkeypatch.setattr(builtins, "version", _no_instalado)

    assert builtins._version_del_nucleo() == "0.0.0+sin-instalar"
