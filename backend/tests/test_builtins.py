"""
Tests de las primitivas del núcleo (`core.*`).

Acá vive sólo lo que no cabe en un test de flujo (`test_flow_executor.py`) ni
de arquitectura: la resolución de la versión que declara el manifest `core`.
"""

from __future__ import annotations

import pytest

import backend.core.builtins as builtins


def test_version_del_nucleo_lee_el_paquete_instalado():
    """
    El manifest `core` no puede tener una constante propia: issue #5, un
    número pisado a mano se desincroniza del tag apenas se corta un release.

    Sólo tiene sentido en un árbol instalado con pip (editable o no) -- en
    uno vendorizado desde el tarball de un release, como el de la webapp,
    `importlib.metadata` no tiene `.dist-info` de dónde leer nada, y ese
    caso lo cubre el test del archivo VERSION de más abajo.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        esperado = version("bot-core")
    except PackageNotFoundError:
        pytest.skip("bot-core no está instalado con pip en este árbol")

    assert builtins._version_del_nucleo() == esperado
    assert builtins.MANIFEST.version == esperado


def test_version_del_nucleo_lee_el_archivo_version_si_no_esta_instalado(monkeypatch, tmp_path):
    """
    Issue #5 (seguimiento): un `backend/` vendorizado desde el tarball de un
    release -- copiado a mano, sin pasar por pip, como lo hace la webapp --
    no tiene `.dist-info`. `release.yml` deja un `backend/VERSION` con el
    tag pelado antes de empaquetar justo para este caso.
    """
    from importlib.metadata import PackageNotFoundError

    def _no_instalado(nombre):
        raise PackageNotFoundError(nombre)

    monkeypatch.setattr(builtins, "version", _no_instalado)
    archivo = tmp_path / "VERSION"
    archivo.write_text("0.3.0-beta.2\n", encoding="utf-8")
    monkeypatch.setattr(builtins, "_ARCHIVO_VERSION", archivo)

    assert builtins._version_del_nucleo() == "0.3.0-beta.2"


def test_version_del_nucleo_tiene_fallback_si_no_hay_ni_paquete_ni_archivo(monkeypatch, tmp_path):
    """
    Ni `.dist-info` ni `backend/VERSION`: mejor un fallback explícito que
    fingir un número o reventar.
    """
    from importlib.metadata import PackageNotFoundError

    def _no_instalado(nombre):
        raise PackageNotFoundError(nombre)

    monkeypatch.setattr(builtins, "version", _no_instalado)
    monkeypatch.setattr(builtins, "_ARCHIVO_VERSION", tmp_path / "no-existe")

    assert builtins._version_del_nucleo() == "0.0.0+sin-instalar"
