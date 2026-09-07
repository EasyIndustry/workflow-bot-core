"""
Serialización JSON de los valores que van a la base.

Está aparte porque la usan casi todos los stores y porque tiene una regla que no
es la de `json` a secas: **leer nunca levanta**. Una fila con JSON corrupto
devuelve el default y el resto de la consulta sigue viva. Un `JSONDecodeError`
propagándose desde una lectura convertía un item roto en una pantalla en blanco.
"""

from __future__ import annotations

import json
from typing import Any


def dumps(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False)


def loads(texto: str | None, default: Any = None) -> Any:
    """Un JSON corrupto devuelve el default en lugar de tumbar la lectura."""
    if not texto:
        return default
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return default


__all__ = ["dumps", "loads"]
