"""
`FsPort` sobre el filesystem local (`os`, `shutil`, `pathlib`).

Todo lo específico de un sistema operativo vive acá y sólo acá: las rutas de
Windows con `\\`, los nombres reservados, los separadores mezclados. Un plugin
escrito contra `FsPort` no sabe en qué SO corre.

Toda excepción del SO se traduce a `PortError` con la ruta adentro. Un
`FileNotFoundError` pelado subiendo por el stack de un run no dice *cuál*
archivo, que es lo único que se quiere saber al leer la traza.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator

from backend.core.ports import FileInfo, PortError


class LocalFsAdapter:
    """
    Acceso al disco de esta máquina.

    `root` acota todas las operaciones a un subárbol. Sin él, una ruta armada
    desde una variable de un flujo puede apuntar a cualquier parte del disco;
    con él, salirse levanta `PortError` antes de tocar nada. Es opcional porque
    una instalación legítima puede necesitar todo el disco, pero un test o un
    entorno acotado debería usarlo siempre.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).resolve() if root else None

    # ── Resolución de rutas ─────────────────────────────────────────────

    def _p(self, path: str) -> Path:
        crudo = str(path or "").strip()
        if not crudo:
            raise PortError("ruta vacía")
        resuelta = Path(crudo).expanduser()
        if self.root is not None:
            absoluta = (self.root / resuelta).resolve() if not resuelta.is_absolute() else resuelta.resolve()
            if not _dentro(absoluta, self.root):
                raise PortError(f"ruta fuera del árbol permitido: {crudo}")
            return absoluta
        return resuelta

    # ── Consulta ────────────────────────────────────────────────────────

    def exists(self, path: str) -> bool:
        try:
            return self._p(path).exists()
        except OSError:
            return False

    def is_dir(self, path: str) -> bool:
        try:
            return self._p(path).is_dir()
        except OSError:
            return False

    def stat(self, path: str) -> FileInfo:
        destino = self._p(path)
        try:
            info = destino.stat()
        except OSError as exc:
            raise PortError(f"no se pudo leer {destino}: {exc.strerror or exc}") from exc
        return _info(destino, info)

    def list_dir(self, path: str) -> list[FileInfo]:
        destino = self._p(path)
        try:
            entradas = sorted(destino.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            raise PortError(f"no se pudo listar {destino}: {exc.strerror or exc}") from exc
        return [_info(e, _stat_o_none(e)) for e in entradas]

    def walk(self, path: str) -> Iterator[FileInfo]:
        destino = self._p(path)
        if not destino.is_dir():
            raise PortError(f"no es una carpeta: {destino}")
        for base, carpetas, archivos in os.walk(destino):
            carpetas.sort()
            archivos.sort()
            raiz = Path(base)
            for nombre in carpetas:
                yield _info(raiz / nombre, None, is_dir=True)
            for nombre in archivos:
                entrada = raiz / nombre
                yield _info(entrada, _stat_o_none(entrada), is_dir=False)

    # ── Escritura ───────────────────────────────────────────────────────

    def make_dirs(self, path: str) -> str:
        destino = self._p(path)
        try:
            destino.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PortError(f"no se pudo crear {destino}: {exc.strerror or exc}") from exc
        return str(destino)

    def move(self, source: str, dest: str) -> str:
        origen, destino = self._p(source), self._p(dest)
        if not origen.exists():
            raise PortError(f"no existe el origen: {origen}")
        # `shutil.move` a una carpeta existente mueve *dentro* de ella; a una
        # ruta inexistente, renombra. Se explicita para no depender de eso.
        final = destino / origen.name if destino.is_dir() else destino
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(origen), str(final))
        except (OSError, shutil.Error) as exc:
            raise PortError(f"no se pudo mover {origen} a {final}: {exc}") from exc
        return str(final)

    def copy_file(self, source: str, dest: str) -> str:
        origen, destino = self._p(source), self._p(dest)
        if not origen.is_file():
            raise PortError(f"no es un archivo: {origen}")
        final = destino / origen.name if destino.is_dir() else destino
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(origen), str(final))
        except (OSError, shutil.Error) as exc:
            raise PortError(f"no se pudo copiar {origen} a {final}: {exc}") from exc
        return str(final)

    def copy_tree(self, source: str, dest: str) -> str:
        origen, destino = self._p(source), self._p(dest)
        if not origen.is_dir():
            raise PortError(f"no es una carpeta: {origen}")
        final = destino / origen.name if destino.is_dir() else destino
        try:
            shutil.copytree(str(origen), str(final), dirs_exist_ok=True)
        except (OSError, shutil.Error) as exc:
            raise PortError(f"no se pudo copiar {origen} a {final}: {exc}") from exc
        return str(final)

    def remove_file(self, path: str) -> None:
        destino = self._p(path)
        try:
            destino.unlink()
        except OSError as exc:
            raise PortError(f"no se pudo borrar {destino}: {exc.strerror or exc}") from exc

    def remove_tree(self, path: str) -> None:
        destino = self._p(path)
        if not destino.is_dir():
            raise PortError(f"no es una carpeta: {destino}")
        try:
            shutil.rmtree(str(destino))
        except OSError as exc:
            raise PortError(f"no se pudo borrar {destino}: {exc.strerror or exc}") from exc

    def rename(self, path: str, new_name: str) -> str:
        origen = self._p(path)
        nombre = str(new_name or "").strip()
        if not nombre or any(sep in nombre for sep in ("/", "\\", os.sep)):
            # Renombrar es dentro de la misma carpeta. Un separador acá sería
            # un `move` encubierto, y con eso una ruta relativa podría escapar.
            raise PortError(f"nombre inválido para renombrar: {new_name!r}")
        final = origen.parent / nombre
        try:
            origen.rename(final)
        except OSError as exc:
            raise PortError(f"no se pudo renombrar {origen}: {exc.strerror or exc}") from exc
        return str(final)

    # ── Rutas ───────────────────────────────────────────────────────────
    #
    # Puras: no tocan el disco y no pasan por `_p`, así que funcionan sobre una
    # ruta que todavía no existe — que es justo cuando se necesitan.

    def parent(self, path: str) -> str:
        recortada = _sin_barra_final(path)
        corte = max(recortada.rfind("\\"), recortada.rfind("/"))
        if corte < 0:
            return ""          # "salida.txt" no tiene carpeta explícita
        if corte == 0:
            return recortada[0]  # "/x" -> "/", la raíz
        return recortada[:corte]

    def basename(self, path: str) -> str:
        recortada = _sin_barra_final(path)
        corte = max(recortada.rfind("\\"), recortada.rfind("/"))
        return recortada[corte + 1:] if corte >= 0 else recortada

    def join(self, *parts: str) -> str:
        tramos = [str(x) for x in parts if str(x) != ""]
        if not tramos:
            return ""
        salida = _sin_barra_final(tramos[0]) or tramos[0]
        for tramo in tramos[1:]:
            salida = f"{salida}{os.sep}{_sin_barra_final(tramo).lstrip('/').lstrip(chr(92))}"
        return salida

    # ── Contenido ───────────────────────────────────────────────────────

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        destino = self._p(path)
        try:
            return destino.read_text(encoding=encoding, errors="replace")
        except OSError as exc:
            raise PortError(f"no se pudo leer {destino}: {exc.strerror or exc}") from exc

    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> None:
        destino = self._p(path)
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(content, encoding=encoding)
        except OSError as exc:
            raise PortError(f"no se pudo escribir {destino}: {exc.strerror or exc}") from exc

    def read_bytes(self, path: str) -> bytes:
        destino = self._p(path)
        try:
            return destino.read_bytes()
        except OSError as exc:
            raise PortError(f"no se pudo leer {destino}: {exc.strerror or exc}") from exc

    def write_bytes(self, path: str, content: bytes) -> None:
        destino = self._p(path)
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(content)
        except OSError as exc:
            raise PortError(f"no se pudo escribir {destino}: {exc.strerror or exc}") from exc


def _sin_barra_final(path) -> str:
    """La ruta sin su separador final, pero sin comerse la raíz."""
    texto = str(path or "")
    recortada = texto.rstrip("\\/")
    return recortada if recortada else texto[:1]


def _dentro(candidata: Path, raiz: Path) -> bool:
    return candidata == raiz or raiz in candidata.parents


def _stat_o_none(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def _info(path: Path, stat_result, is_dir: bool | None = None) -> FileInfo:
    return FileInfo(
        path=str(path),
        name=path.name,
        is_dir=path.is_dir() if is_dir is None else is_dir,
        size=getattr(stat_result, "st_size", 0) or 0,
        modified_at=getattr(stat_result, "st_mtime", 0.0) or 0.0,
    )


__all__ = ["LocalFsAdapter"]
