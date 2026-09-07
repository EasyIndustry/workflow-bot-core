"""
Tests de actores y de la política de ejecución.

Lo que se prueba no es "guarda y lee": es que `kind` **impida** cosas. Una
etiqueta que sólo se registra es decoración, y la decoración se desincroniza
del comportamiento sin que nadie se entere.
"""

from __future__ import annotations

import pytest

from backend.core.contract import ToolManifest
from backend.core.users import (
    AGENT,
    DEFAULTS_POR_KIND,
    HUMAN,
    KINDS,
    SYSTEM,
    RunPolicy,
    UserError,
    UserStore,
)

PELIGROSO = ToolManifest(id="fs.borrar", label="borrar", category="X", dangerous=True)
INOCUO = ToolManifest(id="core.log", label="log", category="X")


@pytest.fixture
def users(db):
    return UserStore(db)


# ── Siembra ─────────────────────────────────────────────────────────────


def test_la_migracion_siembra_local_y_system(users):
    """
    Un run necesita un actor desde el primer minuto: no puede depender de que
    alguien corra un alta antes.
    """
    nombres = {u.name for u in users.list()}
    assert nombres == {"local", "system"}


def test_local_puede_todo_porque_ya_podia(users):
    """
    Es quien opera la máquina. Antes de que esta tabla existiera podía todo, y
    la migración no puede quitarle permisos a una instalación en marcha.
    """
    local = users.get("local")
    assert local.can_run_dangerous is True
    assert local.allowed_ports is None


def test_el_seed_espeja_los_defaults_por_kind(users):
    """
    Si el INSERT de la migración y `DEFAULTS_POR_KIND` divergen, un actor
    sembrado tiene permisos distintos que uno creado con el mismo tipo — y la
    diferencia no se ve hasta que algo pasa.
    """
    system = users.get("system")
    esperado = DEFAULTS_POR_KIND[SYSTEM]
    assert system.can_run_dangerous is esperado["can_run_dangerous"]
    assert list(system.allowed_ports) == esperado["allowed_ports"]


# ── Alta ────────────────────────────────────────────────────────────────


def test_un_agente_nace_restringido(users):
    """
    El default de algo que actúa sin nadie mirando tiene que ser el
    conservador. Habilitarlo es un acto deliberado, y queda registrado.
    """
    u = users.create("agente", kind=AGENT)
    assert u.can_run_dangerous is False
    assert "process" not in u.allowed_ports


def test_los_defaults_se_pueden_pisar_al_crear(users):
    """Habilitar un agente puntual no debería cambiar la política de todos."""
    u = users.create("agente-pleno", kind=AGENT, can_run_dangerous=True)
    assert u.can_run_dangerous is True
    # Y no arrastra el resto del default de su tipo.
    assert users.create("otro", kind=AGENT).can_run_dangerous is False


def test_nombre_duplicado_se_rechaza(users):
    users.create("uno", kind=AGENT)
    with pytest.raises(UserError, match="ya existe"):
        users.create("uno", kind=AGENT)


def test_kind_invalido_se_rechaza(users):
    with pytest.raises(UserError, match="tipo de actor inválido"):
        users.create("x", kind="robot")


def test_nombre_invalido_se_rechaza(users):
    for malo in ("", "  ", "1empieza-con-numero", "con espacio", "con/barra"):
        with pytest.raises(UserError):
            users.create(malo, kind=AGENT)


# ── La política impide ──────────────────────────────────────────────────


def test_un_agente_no_ejecuta_un_tool_peligroso(users):
    users.create("agente", kind=AGENT)
    motivo = users.policy_for("agente").deniega(PELIGROSO, ("fs",))
    assert motivo and "peligroso" in motivo


def test_un_agente_no_usa_un_port_que_no_tiene(users):
    users.create("agente", kind=AGENT)
    motivo = users.policy_for("agente").deniega(INOCUO, ("process",), plugin="riesgo")
    assert motivo
    # El mensaje nombra al plugin: la granularidad de ports es por plugin, no
    # por tool, y sin decirlo el bloqueo parece un error.
    assert "riesgo" in motivo and "process" in motivo


def test_lo_permitido_pasa(users):
    users.create("agente", kind=AGENT)
    assert users.policy_for("agente").deniega(INOCUO, ("http", "fs")) is None


def test_un_humano_no_tiene_restricciones(users):
    assert users.policy_for("local").deniega(PELIGROSO, ("process",)) is None


def test_la_politica_es_inmutable_durante_el_run():
    """
    Se arma una vez al empezar. Nada de lo que pase durante la ejecución puede
    ampliar los permisos con los que arrancó.
    """
    policy = RunPolicy(actor="x", kind=AGENT, can_run_dangerous=False)
    with pytest.raises(Exception):
        policy.can_run_dangerous = True  # type: ignore[misc]


# ── Cambiar permisos es un dato, no un deploy ───────────────────────────


def test_habilitar_un_agente_es_un_update(users):
    users.create("agente", kind=AGENT)
    assert users.policy_for("agente").deniega(PELIGROSO) is not None

    users.update_policy("agente", can_run_dangerous=True)
    assert users.policy_for("agente").deniega(PELIGROSO) is None


def test_abrir_todos_los_ports(users):
    users.create("agente", kind=AGENT)
    users.update_policy("agente", clear_ports=True)
    assert users.policy_for("agente").deniega(INOCUO, ("process",)) is None


# ── Baja lógica ─────────────────────────────────────────────────────────


def test_deshabilitar_no_borra(users):
    """
    Un run apunta a su actor por nombre. Borrarlo dejaría evidencia histórica
    apuntando a la nada, o —si el nombre se recreara— atribuida a otro.
    """
    users.create("temporal", kind=AGENT)
    users.disable("temporal")

    u = users.get("temporal")
    assert u is not None            # la fila sigue
    assert u.enabled is False
    assert u.disabled_at > 0


def test_un_actor_deshabilitado_no_puede_correr(users):
    users.create("temporal", kind=AGENT)
    users.disable("temporal")
    with pytest.raises(UserError, match="deshabilitado"):
        users.policy_for("temporal")


def test_se_puede_volver_a_habilitar(users):
    users.create("temporal", kind=AGENT)
    users.disable("temporal")
    users.enable("temporal")
    assert users.policy_for("temporal").actor == "temporal"


def test_listar_puede_excluir_los_deshabilitados(users):
    users.create("temporal", kind=AGENT)
    users.disable("temporal")
    assert "temporal" not in {u.name for u in users.list(include_disabled=False)}
    assert "temporal" in {u.name for u in users.list()}


# ── Errores útiles ──────────────────────────────────────────────────────


def test_un_actor_inexistente_dice_cuales_hay_y_como_crearlo(users):
    """
    Un "no existe" a secas manda a adivinar. Acá el mensaje trae la lista y el
    comando exacto.
    """
    with pytest.raises(UserError) as exc:
        users.policy_for("fantasma")
    mensaje = str(exc.value)
    assert "local" in mensaje
    assert "users add" in mensaje


def test_todos_los_kinds_tienen_default(users):
    """Un kind sin entrada en DEFAULTS_POR_KIND reventaría recién al crear uno."""
    assert set(DEFAULTS_POR_KIND) == set(KINDS)
    for kind in KINDS:
        assert users.create(f"actor-{kind}", kind=kind).kind == kind


def test_el_actor_es_serializable(users):
    datos = users.create("agente", kind=AGENT).to_dict()
    assert datos["kind"] == AGENT
    assert datos["enabled"] is True
    assert isinstance(datos["allowed_ports"], list)


def test_la_politica_es_serializable(users):
    datos = users.policy_for("local").to_dict()
    assert datos == {
        "actor": "local",
        "kind": HUMAN,
        "can_run_dangerous": True,
        "allowed_ports": None,
    }
