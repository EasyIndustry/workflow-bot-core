"""
Tests de los dominios que el núcleo persiste: workflows y runs, más el esquema
y sus migraciones.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter
from backend.core.flow.executor import RunResult
from backend.core.jsonio import loads
from backend.core.schema import MIGRATIONS, SCHEMA
from backend.core.stores import RunStore, StoreError, WorkflowStore

FLOWS = pathlib.Path(__file__).resolve().parent / "flows"

FLUJO = 'flowchart TD\n    B(inicio)\n    L["core.log | message=x"]\n    B --> L\n'


def test_migraciones_crean_todo_y_son_idempotentes(db):
    assert db.versions() == SCHEMA
    # Volver a migrar no rompe ni duplica: es lo que permite actualizar el
    # núcleo sobre una instalación existente sin un paso manual.
    assert db.migrate(SCHEMA, MIGRATIONS) == {}
    assert db.versions() == SCHEMA


def test_cada_dominio_versiona_por_separado(db):
    """Una actualización no debe obligar a migrar todo junto."""
    assert set(SCHEMA) == {
        "users",
        "workflows",
        "runs",
        "env",
        "run_logs",
        "settings",
        "plugin_items",
    }
    # `runs` ya migró una vez: es la prueba de que el versionado por dominio
    # sirve para lo que se diseñó — subir uno solo sin tocar los otros seis.
    assert SCHEMA["runs"] == 2
    assert all(isinstance(v, int) and v >= 1 for v in SCHEMA.values())


def test_env_comparte_namespace_entre_variables_y_secretos(db):
    """
    Un nombre no puede ser variable y secreto a la vez.

    Los dos se interpolan como `{env.CLAVE}`, así que comparten el namespace. Si
    vivieran en dos tablas, nada impediría tener `TOKEN` en las dos y el flujo
    resolvería una de las dos sin que nadie sepa cuál.
    """
    db.execute(
        "INSERT INTO env (org, name, value, secret, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("local", "TOKEN", "cifrado", 1, time.time()),
    )
    try:
        db.execute(
            "INSERT INTO env (org, name, value, secret, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("local", "TOKEN", "a la vista", 0, time.time()),
        )
    except Exception:
        pass
    else:
        raise AssertionError("dos filas con el mismo nombre: falta el UNIQUE (org, name)")

    filas = db.query("SELECT name, secret FROM env")
    assert len(filas) == 1
    assert filas[0]["secret"] == 1


def test_json_corrupto_no_tumba_la_lectura(db):
    assert loads("{roto", {"por": "defecto"}) == {"por": "defecto"}
    assert loads(None, []) == []


# ── Workflows ───────────────────────────────────────────────────────────


def test_workflow_se_guarda_y_se_actualiza(db):
    store = WorkflowStore(db)
    store.save("mi flujo", FLUJO, folder="TEST", description="una prueba")
    wf = store.get("mi flujo")
    assert wf.folder == "TEST" and wf.enabled

    store.save("mi flujo", FLUJO, state="disabled")
    assert store.get("mi flujo").state == "disabled"
    assert len(store.list()) == 1  # actualizó, no duplicó


def test_workflow_importa_la_cabecera_del_mmd(db):
    store = WorkflowStore(db)
    store.save_mmd("con meta", "%% folder: FORM\n%% state: disabled\n" + FLUJO)
    wf = store.get("con meta")
    assert wf.folder == "FORM"
    assert wf.enabled is False
    assert not wf.content.startswith("%%")  # la cabecera no queda en el contenido


def test_ida_y_vuelta_a_mmd_conserva_la_cabecera(db):
    """Es lo que permite versionar en git y empaquetar en una release."""
    store = WorkflowStore(db)
    original = "%% folder: FORM\n%% description: hola\n" + FLUJO
    store.save_mmd("ida", original)
    texto = store.to_mmd("ida")
    assert "%% folder: FORM" in texto and "%% description: hola" in texto

    store.save_mmd("vuelta", texto)
    assert store.get("vuelta").folder == "FORM"
    assert store.get("vuelta").content == store.get("ida").content


def test_importar_y_exportar_una_carpeta(db, tmp_path):
    esperados = len(list(FLOWS.glob("*.mmd")))
    store = WorkflowStore(db)
    assert len(store.import_dir(FLOWS)) == esperados

    # Sin overwrite, importar de nuevo no pisa lo que ya está.
    assert store.import_dir(FLOWS) == []

    salida = tmp_path / "export"
    assert len(store.export_dir(salida)) == esperados
    assert len(list(salida.glob("*.mmd"))) == esperados


def test_workflow_filtra_deshabilitados(db):
    store = WorkflowStore(db)
    store.save("activo", FLUJO)
    store.save("apagado", FLUJO, state="disabled")
    assert [w.name for w in store.list(only_enabled=True)] == ["activo"]
    assert len(store.list()) == 2


def test_nombre_invalido_se_rechaza(db):
    store = WorkflowStore(db)
    for malo in ("../fuga", "con/barra", "", "x" * 200):
        try:
            store.save(malo, FLUJO)
            raise AssertionError(f"debió rechazar {malo!r}")
        except StoreError:
            pass


# ── Runs ────────────────────────────────────────────────────────────────


def test_ultimo_run_por_caso_en_una_consulta(db):
    """La tabla pinta el estado de cada fila sin una consulta por fila."""
    store = RunStore(db)
    store.save(RunResult(run_id="r1", case_id="A", status="ok"), flow="f")
    time.sleep(0.01)
    store.save(RunResult(run_id="r2", case_id="A", status="err"), flow="f")
    store.save(RunResult(run_id="r3", case_id="B", status="ok"), flow="f")

    ultimos = store.latest_by_case(["A", "B", "C"])
    assert ultimos["A"].run_id == "r2"
    assert ultimos["B"].run_id == "r3"
    assert "C" not in ultimos


def test_un_dry_run_no_tapa_la_ultima_ejecucion_real(db):
    store = RunStore(db)
    store.save(RunResult(run_id="real", case_id="A", status="ok"), flow="f")
    time.sleep(0.01)
    store.save(RunResult(run_id="seco", case_id="A", status="ok", dry_run=True), flow="f")

    assert store.latest_by_case(["A"])["A"].run_id == "real"
    assert store.latest_by_case(["A"], include_dry=True)["A"].run_id == "seco"


def test_historial_filtra_por_fuente(db):
    store = RunStore(db)
    store.save(RunResult(run_id="r1", case_id="A"), source="Bandeja")
    store.save(RunResult(run_id="r2", case_id="B"), source="Otra")
    assert [s.run_id for s in store.list(source="Bandeja")] == ["r1"]


def test_historial_puede_excluir_los_dry_run(db):
    store = RunStore(db)
    store.save(RunResult(run_id="real", case_id="A"))
    store.save(RunResult(run_id="seco", case_id="A", dry_run=True))
    assert [s.run_id for s in store.list(include_dry=False)] == ["real"]


# ── Issue #11: params opcionales sólo por nombre ─────────────────────────


def test_run_store_list_por_posicion_es_typeerror_no_filtro_mal_hecho(db):
    """
    El bug real: `include_dry=True` llamado por posición cayó en `actor`, y
    `GET /runs` devolvió `[]` sin ningún error. Con `*`, el mismo error de
    quien llama se entera enseguida y no como datos mal filtrados.
    """
    store = RunStore(db)
    with pytest.raises(TypeError):
        store.list("A", 50, False, None, None, True)  # type: ignore[misc]


def test_run_store_save_por_posicion_es_typeerror(db):
    store = RunStore(db)
    with pytest.raises(TypeError):
        store.save(RunResult(run_id="r1", case_id="A"), "flujo", "fuente")  # type: ignore[misc]


def test_workflow_store_save_por_posicion_es_typeerror(db):
    store = WorkflowStore(db)
    with pytest.raises(TypeError):
        store.save("nombre", FLUJO, "carpeta", "enabled", "desc")  # type: ignore[misc]


if __name__ == "__main__":
    fallos = 0
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for nombre, fn in tests.items():
        try:
            fn()
            print(f"  ok   {nombre}")
        except Exception as exc:
            fallos += 1
            print(f"  FALLA {nombre}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - fallos}/{len(tests)} tests pasaron")
    sys.exit(1 if fallos else 0)
