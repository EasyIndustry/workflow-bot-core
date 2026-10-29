"""
Arma `que-es-bot-core.docx` a partir de `que-es-bot-core.md`.

El .md es la fuente: los diagramas viven ahí como bloques ```mermaid, con una
primera línea `%% nombre: <archivo>`. Este script los dibuja como PNG en
`img/` y pasa el texto a .docx con los estilos por defecto de pandoc, para
que se abra igual en Word y en Google Docs.

    python backend/docs/descripcion/generar.py

Necesita `pandoc` y `npx` (baja @mermaid-js/mermaid-cli la primera vez).
"""

from __future__ import annotations

import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
FUENTE = AQUI / "que-es-bot-core.md"
SALIDA = AQUI / "que-es-bot-core.docx"
IMAGENES = AQUI / "img"
MERMAID_CLI = "@mermaid-js/mermaid-cli@11.4.2"
ESCALA = 2  # el PNG sale al doble y se muestra a la mitad: queda nítido
MAX_ANCHO_CM, MAX_ALTO_CM = 16.0, 10.0  # que entre en la hoja junto al texto

BLOQUE = re.compile(r"```mermaid\n(.*?)```", re.S)
NOMBRE = re.compile(r"^%%\s*nombre:\s*(\S+)\s*$", re.M)


def dibujar(codigo: str, destino: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fuente = Path(tmp) / "diagrama.mmd"
        fuente.write_text(codigo, encoding="utf-8")
        subprocess.run(
            ["npx", "-y", MERMAID_CLI, "-i", str(fuente), "-o", str(destino),
             "-s", str(ESCALA), "-b", "white", "-q"],
            check=True,
        )


def ancho_en_hoja(png: Path) -> str:
    """El ancho con que va a la hoja: tamaño natural, achicado si no entra."""
    ancho_px, alto_px = struct.unpack(">II", png.read_bytes()[16:24])
    cm_por_px = 2.54 / (96 * ESCALA)
    ancho, alto = ancho_px * cm_por_px, alto_px * cm_por_px
    factor = min(1.0, MAX_ANCHO_CM / ancho, MAX_ALTO_CM / alto)
    return f"{ancho * factor:.1f}cm"


def main() -> int:
    texto = FUENTE.read_text(encoding="utf-8")
    IMAGENES.mkdir(exist_ok=True)

    def reemplazar(m: re.Match) -> str:
        codigo = m.group(1)
        nombre = NOMBRE.search(codigo)
        if not nombre:
            raise SystemExit(f"falta '%% nombre:' en el diagrama:\n{codigo}")
        png = IMAGENES / f"{nombre.group(1)}.png"
        dibujar(codigo, png)
        print(f"  {png.relative_to(AQUI)}")
        return f"![]({png.relative_to(AQUI)}){{width={ancho_en_hoja(png)}}}\n"

    con_imagenes = BLOQUE.sub(reemplazar, texto)

    subprocess.run(
        ["pandoc", "-f", "markdown", "-t", "docx", "-o", str(SALIDA),
         "--resource-path", str(AQUI)],
        input=con_imagenes, text=True, check=True, cwd=AQUI,
    )
    print(f"  {SALIDA.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
