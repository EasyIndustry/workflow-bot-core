"""
Actores: quién ejecuta, y qué tiene permitido.

La tabla se llama `users` por convención, pero guarda **actores**: personas,
agentes autónomos, tareas programadas y el propio núcleo. Conviene tenerlo
presente al leer `kind`.

Por qué existe
--------------

Empezó como trazabilidad —"¿quién corrió esto?"— y se convirtió en otra cosa al
exponer la ejecución real a un agente. Un `kind` que sólo se guarda es
decoración, y la decoración se desincroniza. Acá es el sujeto de una política:
un actor `agent` no dispara un tool `dangerous` salvo que alguien se lo habilite
explícitamente.

Eso convierte la columna en algo que **impide** cosas, no que las describe.

Identidad, no autenticación
---------------------------

Esta tabla guarda **quién dice ser**, nunca cómo se prueba. No hay contraseñas,
ni tokens, ni sesiones. Quién establece la identidad es la capa de arriba: la
CLI confía en quien la ejecuta, el servidor MCP en su configuración, una API
futura en su propio mecanismo.

Es una línea dura. En cuanto el núcleo crezca autenticación deja de ser "motor y
trazas", y encima estaríamos escribiendo criptografía en `core/` — justo lo que
mandamos a `adapters/`. Si hace falta, es un port o vive en la capa HTTP.

Baja lógica, nunca `DELETE`
---------------------------

Un run es evidencia y apunta a su actor por nombre. Borrar un actor dejaría
runs históricos apuntando a alguien que ya no existe, o —peor, si volviera a
crearse el nombre— atribuidos a otro. `disabled_at` cierra las dos.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .jsonio import dumps, loads
from .ports import StoragePort
from .schema import LOCAL_ORG

# Los tipos de actor que el núcleo entiende. Es un vocabulario cerrado a
# propósito: lo que cambia entre instalaciones son los permisos de cada actor,
# no las categorías.
HUMAN = "human"
AGENT = "agent"
SCHEDULE = "schedule"
SYSTEM = "system"

KINDS = (HUMAN, AGENT, SCHEDULE, SYSTEM)

# Permisos con los que nace cada tipo. Se aplican **al crear** y después viven
# en la fila: cambiar lo que puede un actor es un UPDATE, no un deploy.
#
# Un agente nace sin permiso para lo destructivo y sin el port `process`. No es
# desconfianza hacia una tecnología: es que el default de algo que actúa sin
# nadie mirando tiene que ser el conservador, y habilitarlo tiene que ser un
# acto deliberado que quede registrado.
DEFAULTS_POR_KIND = {
    HUMAN: {"can_run_dangerous": True, "allowed_ports": None},
    AGENT: {"can_run_dangerous": False, "allowed_ports": ["http", "fs", "clock"]},
    SCHEDULE: {"can_run_dangerous": True, "allowed_ports": None},
    # El núcleo actuando por su cuenta puede esperar, y nada más: no sale a la
    # red, no toca el disco y no corre comandos. Nadie se lo pidió.
    SYSTEM: {"can_run_dangerous": False, "allowed_ports": ["clock"]},
}

_NOMBRE_VALIDO = re.compile(r"^[A-Za-z][A-Za-z0-9_\-.]*$")


class UserError(Exception):
    """Alta inválida, o un actor que no existe o está deshabilitado."""


@dataclass(frozen=True)
class User:
    """Un actor, como se lo puede contar."""

    name: str
    kind: str
    label: str = ""
    can_run_dangerous: bool = False
    # None = todos los ports. Lista vacía = ninguno.
    allowed_ports: tuple[str, ...] | None = None
    disabled_at: float | None = None
    created_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.disabled_at is None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "label": self.label or self.name,
            "can_run_dangerous": self.can_run_dangerous,
            "allowed_ports": list(self.allowed_ports) if self.allowed_ports is not None else None,
            "enabled": self.enabled,
            "disabled_at": self.disabled_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RunPolicy:
    """
    Lo que un actor tiene permitido en una corrida.

    Se arma una vez al empezar el run y se consulta por nodo. Es un valor
    inmutable a propósito: nada de lo que pase durante la ejecución puede
    ampliar los permisos con los que arrancó.
    """

    actor: str
    kind: str
    can_run_dangerous: bool = True
    allowed_ports: frozenset[str] | None = None

    @classmethod
    def de(cls, user: User) -> "RunPolicy":
        return cls(
            actor=user.name,
            kind=user.kind,
            can_run_dangerous=user.can_run_dangerous,
            allowed_ports=(
                None if user.allowed_ports is None else frozenset(user.allowed_ports)
            ),
        )

    @classmethod
    def sin_restricciones(cls, actor: str = "local", kind: str = HUMAN) -> "RunPolicy":
        """
        La política de quien puede todo.

        Es el comportamiento que había antes de que esto existiera, y sigue
        siendo el de una persona operando su propia máquina.
        """
        return cls(actor=actor, kind=kind, can_run_dangerous=True, allowed_ports=None)

    def deniega(
        self, manifest, ports: tuple[str, ...] = (), plugin: str = ""
    ) -> str | None:
        """
        Por qué este actor no puede ejecutar ese tool, o None si puede.

        Devuelve el motivo en vez de un booleano: un "no podés" sin explicación
        manda a adivinar, y acá siempre se sabe exactamente qué regla aplicó.

        **La granularidad de ports es por plugin, no por tool.** Los ports se
        declaran en `PluginManifest` y el núcleo se los inyecta a todos los
        tools de ese plugin, así que cualquiera de ellos *podría* usar
        cualquiera. Denegar de más es la postura correcta —no hay forma de saber
        cuál lo usa sin leer el código— pero el mensaje lo dice, para que no
        parezca un error: se bloquea porque su plugin declaró el port.
        """
        if manifest.dangerous and not self.can_run_dangerous:
            return (
                f'"{manifest.id}" está marcado como peligroso y el actor '
                f'"{self.actor}" ({self.kind}) no tiene permiso para ejecutarlo'
            )

        if self.allowed_ports is not None:
            prohibidos = [p for p in ports if p not in self.allowed_ports]
            if prohibidos:
                permitidos = ", ".join(sorted(self.allowed_ports)) or "ninguno"
                dueño = f'el plugin "{plugin}"' if plugin else "su plugin"
                return (
                    f'{dueño} declara el port {", ".join(prohibidos)}, que el actor '
                    f'"{self.actor}" ({self.kind}) no tiene permitido — así que no '
                    f'puede ejecutar "{manifest.id}". Permitidos: {permitidos}'
                )
        return None

    def to_dict(self) -> dict:
        return {
            "actor": self.actor,
            "kind": self.kind,
            "can_run_dangerous": self.can_run_dangerous,
            "allowed_ports": (
                None if self.allowed_ports is None else sorted(self.allowed_ports)
            ),
        }


class UserStore:
    """Alta, baja y consulta de actores."""

    def __init__(self, db: StoragePort, org: str = LOCAL_ORG) -> None:
        self.db = db
        self.org = org

    def list(self, include_disabled: bool = True) -> list[User]:
        sql = "SELECT * FROM users WHERE org = ?"
        if not include_disabled:
            sql += " AND disabled_at IS NULL"
        sql += " ORDER BY kind, name"
        return [_user(f) for f in self.db.query(sql, (self.org,))]

    def get(self, name: str) -> User | None:
        fila = self.db.one(
            "SELECT * FROM users WHERE org = ? AND name = ?", (self.org, name)
        )
        return _user(fila) if fila else None

    def create(
        self,
        name: str,
        kind: str = HUMAN,
        label: str = "",
        can_run_dangerous: bool | None = None,
        allowed_ports: list[str] | None = None,
    ) -> User:
        """
        Da de alta un actor con los permisos por defecto de su tipo.

        `can_run_dangerous` y `allowed_ports` en None toman el default del
        `kind`; pasarlos explícitamente los pisa. Es lo que permite habilitar un
        agente puntual sin cambiar la política de todos.
        """
        name = _validar_nombre(name)
        if kind not in KINDS:
            raise UserError(
                f"tipo de actor inválido: {kind!r}. Esperado uno de: {', '.join(KINDS)}"
            )
        if self.get(name) is not None:
            raise UserError(f'ya existe un actor llamado "{name}"')

        base = DEFAULTS_POR_KIND[kind]
        peligroso = base["can_run_dangerous"] if can_run_dangerous is None else can_run_dangerous
        ports = base["allowed_ports"] if allowed_ports is None else allowed_ports

        self.db.execute(
            "INSERT INTO users (org, name, kind, label, can_run_dangerous, "
            "allowed_ports, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.org,
                name,
                kind,
                label,
                1 if peligroso else 0,
                None if ports is None else dumps(list(ports)),
                time.time(),
            ),
        )
        return self.get(name)  # type: ignore[return-value]

    def update_policy(
        self,
        name: str,
        can_run_dangerous: bool | None = None,
        allowed_ports: list[str] | None = None,
        clear_ports: bool = False,
    ) -> User:
        """
        Cambia los permisos de un actor. `clear_ports` los abre a todos.

        Existe para que ampliar lo que puede un agente sea un cambio de datos y
        no de código: queda con fecha, se puede revertir, y no obliga a
        redesplegar nada.
        """
        actual = self.get(name)
        if actual is None:
            raise UserError(f'no existe el actor "{name}"')

        if can_run_dangerous is not None:
            self.db.execute(
                "UPDATE users SET can_run_dangerous = ? WHERE org = ? AND name = ?",
                (1 if can_run_dangerous else 0, self.org, name),
            )
        if clear_ports:
            self.db.execute(
                "UPDATE users SET allowed_ports = NULL WHERE org = ? AND name = ?",
                (self.org, name),
            )
        elif allowed_ports is not None:
            self.db.execute(
                "UPDATE users SET allowed_ports = ? WHERE org = ? AND name = ?",
                (dumps(list(allowed_ports)), self.org, name),
            )
        return self.get(name)  # type: ignore[return-value]

    def disable(self, name: str) -> User:
        """
        Baja lógica. Nunca hay un `DELETE`: los runs apuntan acá por nombre y
        borrar dejaría evidencia histórica apuntando a la nada.
        """
        if self.get(name) is None:
            raise UserError(f'no existe el actor "{name}"')
        self.db.execute(
            "UPDATE users SET disabled_at = ? WHERE org = ? AND name = ? "
            "AND disabled_at IS NULL",
            (time.time(), self.org, name),
        )
        return self.get(name)  # type: ignore[return-value]

    def enable(self, name: str) -> User:
        if self.get(name) is None:
            raise UserError(f'no existe el actor "{name}"')
        self.db.execute(
            "UPDATE users SET disabled_at = NULL WHERE org = ? AND name = ?",
            (self.org, name),
        )
        return self.get(name)  # type: ignore[return-value]

    def policy_for(self, name: str) -> RunPolicy:
        """
        La política de un actor, para arrancar un run.

        Un actor inexistente o deshabilitado levanta acá, **antes** de ejecutar
        nada. Es deliberado: el mensaje dice qué falta y cómo arreglarlo, en vez
        de dejar que reviente como un fallo de integridad a mitad de camino.
        """
        user = self.get(name)
        if user is None:
            conocidos = ", ".join(u.name for u in self.list()) or "ninguno"
            raise UserError(
                f'no existe el actor "{name}". Registrados: {conocidos}. '
                f"Se da de alta con: python -m backend.core users add {name} --kind agent"
            )
        if not user.enabled:
            raise UserError(f'el actor "{name}" está deshabilitado')
        return RunPolicy.de(user)


def _validar_nombre(name: str) -> str:
    name = (name or "").strip()
    if not _NOMBRE_VALIDO.match(name):
        raise UserError(
            f"nombre de actor inválido: {name!r} (letras, números, guiones y "
            f"puntos; tiene que empezar con una letra)"
        )
    return name


def _user(fila) -> User:
    ports = loads(fila["allowed_ports"], None) if fila["allowed_ports"] else None
    return User(
        name=fila["name"],
        kind=fila["kind"],
        label=fila["label"],
        can_run_dangerous=bool(fila["can_run_dangerous"]),
        allowed_ports=tuple(ports) if ports is not None else None,
        disabled_at=fila["disabled_at"],
        created_at=fila["created_at"],
    )


__all__ = [
    "AGENT",
    "DEFAULTS_POR_KIND",
    "HUMAN",
    "KINDS",
    "SCHEDULE",
    "SYSTEM",
    "RunPolicy",
    "User",
    "UserError",
    "UserStore",
]
