"""
El modelo de datos del núcleo, como datos.

Acá vive **qué** se guarda; en `adapters/storage_sqlite.py` vive **cómo**. La
separación es la que permite que `core/` no importe ningún driver: el adapter
recibe este diccionario y lo aplica.

Dos decisiones que serían caras de tomar más tarde:

1. **Columna `org` en todo.** Hoy siempre vale 'local'. Cuesta una columna
   ahora; agregarla después es migrar todas las tablas con riesgo de filtrar
   datos entre organizaciones.
2. **Versión de esquema por dominio.** Cada dominio migra por su cuenta, así
   una actualización no obliga a mover todo junto ni rompe en silencio.

La tabla `run_logs` es una fila **por línea de log**, no un JSON por run. Es la
diferencia que importa: el registro de una fila cruza varios runs, y con un blob
por run habría que abrir y parsear todos para armarlo. Con filas, es una
consulta por índice; y limpiar —a mano, por antigüedad, o por tope— es un DELETE
con WHERE en vez de reescribir blobs.

Las tablas `settings` y `plugin_items` son lo que un plugin necesita guardar:
constantes por instalación las primeras, colecciones de items las segundas. **El
plugin no las toca ni sabe que existen**: declara `Setting` y `Resource` en su
manifest y el núcleo persiste. Es lo que permite cambiar el backend sin migrar
un solo plugin, y lo que hace que una política escrita una vez —cifrar un campo
secreto, registrar quién escribió— valga para todos.

`plugin_items.data` es el item entero como JSON y no una columna por campo: los
`fields` los declara cada plugin, así que una columna por campo obligaría a
ejecutar DDL por plugin, justo el mantenimiento que este diseño evita.

`settings.value` guarda JSON y no texto plano porque un setting tiene tipo: hay
`float`, `bool` y `json`. Guardar el `str()` perdería el tipo en el viaje de ida
y vuelta.

La tabla `env` guarda las dos cosas que un flujo interpola como `{env.CLAVE}`:
variables a la vista y secretos. Van juntas y no en dos tablas porque comparten
el namespace —un nombre no puede ser variable y secreto a la vez, y el `UNIQUE
(org, name)` es lo que lo impide—. Las distingue la columna `secret`: cuando
vale 1, `value` guarda el valor cifrado y no se devuelve nunca.
"""

from __future__ import annotations

# Organización por defecto mientras la instalación es de un solo inquilino.
LOCAL_ORG = "local"

# Versión de cada dominio. Subir una obliga a escribir su migración.
#
# **El orden importa.** Las migraciones se aplican recorriendo este diccionario,
# y `runs` referencia a `users`, así que `users` va primero. Es la única
# dependencia entre dominios que existe; si aparece otra, conviene hacerla
# explícita en vez de confiar en el orden de un dict.
SCHEMA = {
    "users": 1,
    "workflows": 1,
    "runs": 2,
    "env": 1,
    "run_logs": 1,
    "settings": 1,
    "plugin_items": 1,
}

MIGRATIONS: dict[str, dict[int, str]] = {
    "users": {
        1: """
        CREATE TABLE IF NOT EXISTS users (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            org               TEXT NOT NULL DEFAULT 'local',
            name              TEXT NOT NULL,
            kind              TEXT NOT NULL DEFAULT 'human',
            label             TEXT NOT NULL DEFAULT '',
            can_run_dangerous INTEGER NOT NULL DEFAULT 0,
            allowed_ports     TEXT,
            disabled_at       REAL,
            created_at        REAL NOT NULL,
            UNIQUE (org, name)
        );
        CREATE INDEX IF NOT EXISTS idx_users_kind ON users (org, kind);

        -- Dos actores sembrados, porque un run necesita uno desde el primer
        -- minuto y no puede depender de que alguien corra un alta.
        --
        -- `local` es quien opera esta máquina: puede todo, porque ya podía
        -- todo antes de que esta tabla existiera. `system` es el núcleo
        -- actuando por su cuenta —limpiezas, tareas internas— y no ejecuta
        -- nada peligroso porque nadie se lo pidió.
        -- Los permisos van explícitos y espejan DEFAULTS_POR_KIND de users.py.
        -- `allowed_ports` NULL significa "todos": dejarlo sin poner en el
        -- `system` le habría dado más permisos que a un agente.
        INSERT OR IGNORE INTO users
            (org, name, kind, label, can_run_dangerous, allowed_ports, created_at)
        VALUES
            ('local', 'local',  'human',  'Esta máquina', 1, NULL,        0),
            ('local', 'system', 'system', 'El núcleo',    0, '["clock"]', 0);
        """
    },
    "workflows": {
        1: """
        CREATE TABLE IF NOT EXISTS workflows (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'local',
            name        TEXT NOT NULL,
            folder      TEXT NOT NULL DEFAULT '',
            state       TEXT NOT NULL DEFAULT 'enabled',
            description TEXT NOT NULL DEFAULT '',
            content     TEXT NOT NULL,
            updated_at  REAL NOT NULL,
            UNIQUE (org, name)
        );
        CREATE INDEX IF NOT EXISTS idx_workflows_org_state ON workflows (org, state);
        """
    },
    "runs": {
        1: """
        CREATE TABLE IF NOT EXISTS runs (
            run_id      TEXT PRIMARY KEY,
            org         TEXT NOT NULL DEFAULT 'local',
            case_id     TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT '',
            flow        TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT '',
            failed_node TEXT,
            message     TEXT NOT NULL DEFAULT '',
            dry_run     INTEGER NOT NULL DEFAULT 0,
            node_count  INTEGER NOT NULL DEFAULT 0,
            started_at  REAL NOT NULL,
            finished_at REAL NOT NULL,
            data        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_case ON runs (org, case_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_recent ON runs (org, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (org, status, started_at DESC);
        """,
        # Quién ejecutó. Se guarda el **nombre** y no el id de `users` por tres
        # razones: el run se lee solo (`SELECT actor FROM runs` sin join), las
        # filas que ya existen toman el default sin tocarlas, y no queda atado a
        # ids autoincrementales que cambian al restaurar un backup.
        #
        # Sin `REFERENCES` a propósito: SQLite no acepta agregar una columna con
        # clave foránea y default no nulo en un ALTER, y recrear una tabla de
        # auditoría para ganar una comprobación que igual depende de un PRAGMA
        # no vale el riesgo. La integridad la garantiza `Instance`, que resuelve
        # el actor contra `users` **antes** de ejecutar — y así el error dice
        # "no existe el actor X" en vez de un fallo de constraint.
        2: """
        ALTER TABLE runs ADD COLUMN actor TEXT NOT NULL DEFAULT 'local';
        CREATE INDEX IF NOT EXISTS idx_runs_actor ON runs (org, actor, started_at DESC);
        """,
    },
    "run_logs": {
        1: """
        CREATE TABLE IF NOT EXISTS run_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'local',
            run_id      TEXT NOT NULL,
            case_id     TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT '',
            flow        TEXT NOT NULL DEFAULT '',
            ts          REAL NOT NULL,
            level       TEXT NOT NULL DEFAULT 'info',
            node_id     TEXT,
            message     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_logs_case ON run_logs (org, case_id, id);
        CREATE INDEX IF NOT EXISTS idx_run_logs_run ON run_logs (org, run_id, id);
        CREATE INDEX IF NOT EXISTS idx_run_logs_ts ON run_logs (org, ts);
        CREATE INDEX IF NOT EXISTS idx_run_logs_source ON run_logs (org, source, case_id);
        """
    },
    "env": {
        1: """
        CREATE TABLE IF NOT EXISTS env (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'local',
            name        TEXT NOT NULL,
            value       TEXT NOT NULL DEFAULT '',
            secret      INTEGER NOT NULL DEFAULT 0,
            updated_by  TEXT NOT NULL DEFAULT 'local',
            updated_at  REAL NOT NULL,
            UNIQUE (org, name)
        );
        CREATE INDEX IF NOT EXISTS idx_env_org_secret ON env (org, secret);
        """
    },
    "settings": {
        1: """
        CREATE TABLE IF NOT EXISTS settings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'local',
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT 'local',
            updated_at  REAL NOT NULL,
            UNIQUE (org, key)
        );
        """
    },
    "plugin_items": {
        1: """
        CREATE TABLE IF NOT EXISTS plugin_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'local',
            plugin      TEXT NOT NULL,
            resource    TEXT NOT NULL,
            key         TEXT NOT NULL,
            data        TEXT NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT 'local',
            updated_at  REAL NOT NULL,
            UNIQUE (org, plugin, resource, key)
        );
        CREATE INDEX IF NOT EXISTS idx_plugin_items ON plugin_items (org, plugin, resource);
        """
    },
}


__all__ = ["LOCAL_ORG", "MIGRATIONS", "SCHEMA"]
