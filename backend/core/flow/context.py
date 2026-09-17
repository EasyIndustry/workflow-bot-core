"""
Contexto de variables de un run.

El bus de variables entre nodos. Tres propiedades que lo definen:

1. **Es por run**, no global. Dos casos en paralelo no se pisan.
2. **Un tool no lo escribe**: devuelve outputs y el núcleo los mergea acá.
3. **La interpolación de `{variables}` está implementada una sola vez.** Con
   varias implementaciones —una por handler, como pasaba antes— un
   `{carpeta.parent}` funciona o no según qué nodo lea el parámetro, y eso es
   indistinguible de un bug de datos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# {nombre}, {objeto.campo}, {env.CLAVE}, {ruta.parent}
_VAR_RE = re.compile(r"\{([\w.]+)\}")


def _deep_get(root: Any, keys: list[str]) -> Any:
    """Recorre un camino de claves; None si algún tramo no es indexable."""
    current = root
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _parent_of(path: str) -> str:
    r"""
    Carpeta contenedora de una ruta, sin importar el separador.

    No usa pathlib a propósito: tiene que resolver rutas de Windows corriendo
    en cualquier sistema, y `PurePosixPath` no entiende `\`.
    """
    trimmed = path.rstrip("\\/")
    last = max(trimmed.rfind("\\"), trimmed.rfind("/"))
    return trimmed[:last] if last != -1 else path


@dataclass
class RunContext:
    """
    Namespaces de variables visibles en un run, en orden de precedencia.

    Orden de resolución (mismo que FLUJO_SPEC.md):
        vars (outputs de nodos)  →  row (campos del caso)  →  env  →  config
    """

    row: dict = field(default_factory=dict)
    vars: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    # ── Escritura ───────────────────────────────────────────────────────

    def merge_outputs(self, outputs: dict) -> None:
        """Incorpora los outputs declarados por un tool tras ejecutarlo."""
        self.vars.update(outputs)

    # ── Lectura ─────────────────────────────────────────────────────────

    def lookup(self, name: str) -> Any:
        """Un nombre simple, siguiendo el orden de precedencia."""
        for source in (self.vars, self.row, self.env, self.config):
            if name in source:
                return source[name]
        return None

    def decision_value(self, variable: str) -> Any:
        """
        Valor que usa un nodo de decisión.

        El row antes que las variables computadas: una decisión ramifica por un
        dato del caso, y un output homónimo de un nodo anterior no debería
        cambiar por dónde va el flujo.
        """
        if variable in self.row:
            return self.row[variable]
        return self.vars.get(variable)

    # ── Interpolación ───────────────────────────────────────────────────

    def resolve(self, template: str) -> Any:
        """
        Sustituye cada `{var}` del template. Lo que no resuelve queda literal,
        para que el dry-run pueda señalarlo en lugar de inventar un valor.

        Si el template es exactamente un único placeholder (`{var}` o
        `{a.b}`, sin texto alrededor) y el valor resuelto es una lista o un
        dict, se devuelve el objeto tal cual — no su `str()` — para que un
        param JSON pueda recibir una colección producida por otro nodo. Con
        cualquier otro texto alrededor (`"hay {n} archivos"`) sigue
        interpolando como string, como siempre.
        """
        if not isinstance(template, str) or "{" not in template:
            return template
        full_match = _VAR_RE.fullmatch(template)
        if full_match:
            resolved = self._resolve_one(full_match.group(1), as_object=True)
            if isinstance(resolved, (list, dict)):
                return resolved
        return _VAR_RE.sub(lambda m: str(self._resolve_one(m.group(1))), template)

    def unresolved(self, template: str) -> list[str]:
        """Variables del template que no se pueden resolver. Lo usa el dry-run."""
        if not isinstance(template, str) or "{" not in template:
            return []
        full_match = _VAR_RE.fullmatch(template)
        as_object = full_match is not None
        return [
            expr
            for expr in _VAR_RE.findall(template)
            if self._resolve_one(expr, as_object=as_object) == "{" + expr + "}"
        ]

    def _resolve_one(self, expr: str, as_object: bool = False) -> Any:
        """
        Resuelve un único placeholder. Con `as_object=True` (sólo cuando el
        template es exactamente este placeholder, ver `resolve`), una lista o
        un dict se devuelven tal cual en vez de convertirse a `str`.
        """
        parts = expr.split(".")
        name, mods = parts[0], parts[1:]
        literal = "{" + expr + "}"

        # {env.CLAVE} — acceso explícito a variables de entorno
        if name == "env" and len(parts) == 2:
            value = self.env.get(parts[1])
            return str(value) if value is not None else literal

        # {objeto.campo.subcampo} — traversal
        if mods:
            deep = _deep_get(self.vars, parts)
            if deep is None:
                deep = _deep_get(self.row, parts)
            if deep is not None:
                if isinstance(deep, (dict, list)):
                    return deep if as_object else literal
                return str(deep)

        value = self.lookup(name)
        if value is None:
            return literal

        # Modificadores sobre el valor string: {filesFolder.parent}
        if isinstance(value, str):
            for mod in mods:
                if mod == "parent":
                    value = _parent_of(value)
        if isinstance(value, (dict, list)):
            return value if as_object else str(value)
        return str(value)

    def snapshot(self) -> dict:
        """Estado de las variables, para la traza del run."""
        return {
            "vars": dict(self.vars),
            "row_keys": sorted(self.row),
            "env_keys": sorted(self.env),
        }
