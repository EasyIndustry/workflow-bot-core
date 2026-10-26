"""
Tapar secretos en lo que deja un run: traza y log.

La traza guarda los params **ya resueltos** -es lo que la hace útil para
entender un fallo-, así que un `Bearer {env.TOKEN}` queda escrito con el token
en claro, y la traza sale por la API, la CLI y el MCP. Este módulo es el paso
que lo impide: antes de guardar o devolver un run, todo valor secreto que el
núcleo conoce se reemplaza por `***`.

Se tapa **por valor**, no por param. Un secreto interpolado en un param que no
es secreto (`Authorization: Bearer {env.TOKEN}`) no tiene nada que lo marque
una vez resuelto: es texto. Buscar el valor alcanza también lo que un tool
devolvió en `message`, en `outputs` o en un traceback, sin que el núcleo tenga
que saber nada de ningún plugin.

Qué es secreto lo decide quien lo declara, nunca este módulo: una variable
`secret` de la tabla `env`, un `Setting(secret=True)` o un `Field(secret=True)`
de un `Resource`. `Instance.secret_values()` junta esos valores; acá sólo se
reemplazan.

Un secreto de menos de `MIN_SUBSTRING` caracteres se tapa sólo cuando es el
valor **entero**: buscarlo adentro de cualquier texto taparía cualquier "1" o
"si" de la traza, y un secreto así de corto no protege nada de todos modos.
"""

from __future__ import annotations

from typing import Any, Iterable

TAPADO = "***"

# Largo mínimo para buscar un secreto adentro de un texto más largo.
MIN_SUBSTRING = 4


def secret_set(valores: Iterable[Any]) -> frozenset[str]:
    """Normaliza a texto y descarta vacíos: un secreto vacío no tapa nada."""
    return frozenset(str(v) for v in valores if v is not None and str(v).strip())


def redact(valor: Any, secretos: frozenset[str]) -> Any:
    """
    Una copia de `valor` con los secretos tapados. No modifica el original:
    los mismos objetos pueden estar vivos en el contexto del run o en el
    checkpoint de una decisión manual, que necesitan el valor real.
    """
    if not secretos:
        return valor
    return _redact(valor, secretos, _largos(secretos))


def _largos(secretos: frozenset[str]) -> list[str]:
    # El más largo primero: si un secreto contiene a otro, tapar el corto
    # antes dejaría a la vista el resto del largo.
    return sorted((s for s in secretos if len(s) >= MIN_SUBSTRING), key=len, reverse=True)


def _redact(valor: Any, secretos: frozenset[str], largos: list[str]) -> Any:
    if isinstance(valor, str):
        if valor in secretos:
            return TAPADO
        for s in largos:
            if s in valor:
                valor = valor.replace(s, TAPADO)
        return valor
    if isinstance(valor, dict):
        return {k: _redact(v, secretos, largos) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_redact(v, secretos, largos) for v in valor]
    # Un param tipado: el secreto "1234" llega al tool como el int 1234.
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return TAPADO if str(valor) in secretos else valor
    return valor


def redact_run(resultado, secretos: frozenset[str]) -> None:
    """
    Tapa los secretos de un `RunResult` en el lugar: traza, log, mensaje y
    `waiting`. **No** toca `checkpoint`: es lo que se necesita para retomar
    una decisión manual, nunca sale del núcleo, y por diseño no guarda ni
    `env` ni `config`.
    """
    if not secretos:
        return
    largos = _largos(secretos)

    def tapar(valor):
        return _redact(valor, secretos, largos)

    resultado.message = tapar(resultado.message)
    if resultado.waiting is not None:
        resultado.waiting = tapar(resultado.waiting)
    for t in resultado.trace:
        t.params = tapar(t.params)
        t.message = tapar(t.message)
        t.outputs = tapar(t.outputs)
        t.traceback = tapar(t.traceback)
    for entrada in resultado.logs:
        entrada.message = tapar(entrada.message)
