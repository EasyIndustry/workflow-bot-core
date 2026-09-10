"""
Variables y secretos: lo que un flujo interpola como `{env.CLAVE}`.

Las dos cosas viven en la misma tabla porque comparten el namespace — un nombre
no puede ser variable y secreto a la vez — y las distingue la columna `secret`.
Ver el docstring de `storage.py`.

La diferencia de trato es toda acá:

- Una **variable** se guarda en claro y se lee. La UI la muestra y la edita.
- Un **secreto** se guarda cifrado y **no se vuelve a leer por API**. `list()`
  devuelve el nombre, si tiene valor y cuándo cambió; nunca el valor. El único
  camino al valor en claro es `resolve()`, que corre en el servidor al ejecutar
  un flujo.

La llave vive en un archivo **fuera de la base** (`data/secret.key`), porque una
llave adentro de lo que cifra no cifra nada. Un backup de la base sin la llave
no restaura los secretos, y esa es la idea: como no se pueden leer, tampoco se
"recuperan" — se vuelven a cargar a mano.

Qué compra el cifrado, sin adornos: protege la base, un dump y un backup. No
protege a alguien con acceso a los archivos de la máquina, que tiene también la
llave, ni a alguien que llegue al puerto, que puede *usar* los secretos sin
verlos. El acceso a la API es un problema distinto y se resuelve aparte.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .ports import CryptoPort, PortError, StoragePort
from .schema import LOCAL_ORG

# `{env.CLAVE}` se parte por el punto, así que un nombre no puede tener uno. Se
# pide además que empiece por letra para que no haya nombres tipo `1` o `_`.
NOMBRE_VALIDO = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# Cómo se referencia una variable desde un flujo. Se usa para contar usos.
REFERENCIA = re.compile(r"\{env\.([A-Za-z][A-Za-z0-9_]*)\}")


class EnvError(Exception):
    """Un problema que la UI puede mostrar tal cual."""


@dataclass(frozen=True)
class EnvVar:
    """
    Una variable o un secreto, **como se puede contar**.

    `value` es `None` para un secreto: no es que no tenga valor, es que este
    objeto no lo lleva. Para saber si tiene, está `has_value`.
    """

    name: str
    secret: bool
    value: str | None
    has_value: bool
    updated_by: str
    updated_at: float
    # Un secreto guardado con otra llave: hay valor, pero no se puede descifrar.
    # Se dice en vez de fingir que está vacío, que mandaría a cargarlo de nuevo
    # sin explicar por qué.
    unreadable: bool = False

    def to_dict(self, usos: int = 0) -> dict:
        return {
            "name": self.name,
            "secret": self.secret,
            # El valor de un secreto no sale de acá ni por error: la clave no
            # existe en el dict, en vez de existir en null.
            **({} if self.secret else {"value": self.value}),
            "has_value": self.has_value,
            "unreadable": self.unreadable,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
            "uses": usos,
        }


class EnvStore:
    """
    El almacén. Una instancia por base.

    La llave se genera sola la primera vez que hace falta —no en el arranque—
    así que una instalación que nunca carga un secreto no deja ningún archivo
    de llave dando vueltas.
    """

    def __init__(self, db: StoragePort, crypto: CryptoPort, org: str = LOCAL_ORG) -> None:
        self.db = db
        # El cifrado entra por un port: cómo se cifra y dónde vive la llave son
        # decisiones de infraestructura. Una instalación con un KMS escribe otro
        # adapter y este archivo no cambia.
        self.crypto = crypto
        self.org = org

    def _cifrar(self, valor: str) -> str:
        try:
            return self.crypto.encrypt(valor)
        except PortError as exc:
            raise EnvError(str(exc)) from exc

    def _descifrar(self, valor: str) -> str | None:
        """El valor en claro, o None si no se puede descifrar."""
        try:
            return self.crypto.decrypt(valor)
        except Exception:
            return None

    # ── Lectura ─────────────────────────────────────────────────────────

    def list(self) -> list[EnvVar]:
        """Todo lo cargado. Los secretos vienen **sin** su valor."""
        filas = self.db.query(
            "SELECT name, value, secret, updated_by, updated_at FROM env "
            "WHERE org = ? ORDER BY secret DESC, name",
            (self.org,),
        )
        return [self._desde_fila(f, con_valor=False) for f in filas]

    def get(self, name: str) -> EnvVar | None:
        fila = self.db.one(
            "SELECT name, value, secret, updated_by, updated_at FROM env "
            "WHERE org = ? AND name = ?",
            (self.org, name),
        )
        return None if fila is None else self._desde_fila(fila, con_valor=False)

    def resolve(self) -> dict[str, str]:
        """
        Nombre → valor en claro, para ejecutar un flujo.

        Es el único camino al valor de un secreto, y corre en el servidor. Un
        secreto que no se puede descifrar **se omite**: el flujo va a fallar al
        interpolar, que es un error mucho más fácil de leer que una excepción
        de criptografía en medio de una ejecución.
        """
        valores: dict[str, str] = {}
        for fila in self.db.query(
            "SELECT name, value, secret FROM env WHERE org = ?", (self.org,)
        ):
            if not fila["secret"]:
                valores[fila["name"]] = fila["value"]
                continue
            if not fila["value"]:
                continue
            claro = self._descifrar(fila["value"])
            if claro is not None:
                valores[fila["name"]] = claro
        return valores

    def _desde_fila(self, fila, con_valor: bool) -> EnvVar:
        es_secreto = bool(fila["secret"])
        crudo = fila["value"] or ""
        # Un secreto cifrado con otra llave: hay valor guardado, pero no se
        # puede leer. Decirlo es la diferencia entre "revisá la llave" y un
        # flujo que falla más adelante sin explicación.
        ilegible = es_secreto and bool(crudo) and self._descifrar(crudo) is None
        return EnvVar(
            name=fila["name"],
            secret=es_secreto,
            value=None if es_secreto else crudo,
            has_value=bool(crudo),
            updated_by=fila["updated_by"],
            updated_at=fila["updated_at"],
            unreadable=ilegible,
        )

    # ── Escritura ───────────────────────────────────────────────────────

    def save(
        self,
        name: str,
        value: str,
        *,
        secret: bool = False,
        updated_by: str = LOCAL_ORG,
    ) -> EnvVar:
        """
        Da de alta o reemplaza. Un secreto entra cifrado.

        Cambiar el flag de un nombre que ya existe está permitido y es
        deliberado: pasar una variable a secreto es exactamente lo que hay que
        hacer cuando algo se cargó en claro por error.
        """
        nombre = (name or "").strip()
        if not NOMBRE_VALIDO.match(nombre):
            raise EnvError(
                f'"{nombre}" no sirve como nombre. Se referencian como '
                "{env.CLAVE}, así que tienen que empezar con una letra y llevar "
                "sólo letras, números y guión bajo."
            )
        if secret and not value:
            raise EnvError(
                "Un secreto sin valor no se guarda. Para dejar el nombre "
                "declarado y sin valor, cargalo como variable vacía."
            )

        guardado = self._cifrar(value) if secret else value

        self.db.execute(
            "INSERT INTO env (org, name, value, secret, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (org, name) DO UPDATE SET "
            "value = excluded.value, secret = excluded.secret, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (self.org, nombre, guardado, 1 if secret else 0, updated_by, time.time()),
        )
        resultado = self.get(nombre)
        assert resultado is not None
        return resultado

    def delete(self, name: str) -> bool:
        afectadas = self.db.execute(
            "DELETE FROM env WHERE org = ? AND name = ?", (self.org, name)
        )
        return afectadas > 0


def referencias(texto: str) -> set[str]:
    """Los nombres que un texto interpola como `{env.CLAVE}`."""
    return set(REFERENCIA.findall(texto or ""))
