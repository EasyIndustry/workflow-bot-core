"""
Tests del registro de eventos por fila.

Lo que se prueba es lo que la pantalla necesita: el log de una fila **acumulado
entre runs**, y que limpiarlo no se lleve el historial de ejecuciones.
"""

from __future__ import annotations

import time

from backend.core.flow.executor import LogEntry, RunResult
from backend.core.log_store import LogStore


def _run(run_id: str, case_id: str, mensajes, ts: float | None = None) -> RunResult:
    momento = ts if ts is not None else time.time()
    return RunResult(
        run_id=run_id,
        case_id=case_id,
        logs=[
            LogEntry(t="10:00:00", message=m, level="info", ts=momento)
            for m in mensajes
        ],
    )


# ── Escritura y lectura ─────────────────────────────────────────────────


def test_las_lineas_de_un_run_se_guardan_y_se_leen(db):
    store = LogStore(db)
    assert store.append_run(_run("r1", "BW846", ["empieza", "termina"]), flow="f", source="s") == 2

    lineas = store.for_case("BW846")
    assert [l.message for l in lineas] == ["empieza", "termina"]
    assert lineas[0].run_id == "r1"
    assert lineas[0].flow == "f" and lineas[0].source == "s"


def test_el_log_de_una_fila_acumula_entre_runs(db):
    """
    Es la propiedad que justifica la tabla: la pantalla muestra la historia de
    la fila, no la del último run. Reprocesar un caso agrega, no reemplaza.
    """
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["intento 1 falló"]))
    store.append_run(_run("r2", "BW846", ["intento 2 ok"]))

    assert [l.message for l in store.for_case("BW846")] == ["intento 1 falló", "intento 2 ok"]
    assert store.count_for_case("BW846") == 2


def test_cada_fila_tiene_su_propio_log(db):
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["de la 846"]))
    store.append_run(_run("r2", "BX208", ["de la 208"]))
    assert [l.message for l in store.for_case("BW846")] == ["de la 846"]
    assert [l.message for l in store.for_case("BX208")] == ["de la 208"]


def test_for_run_trae_solo_las_de_ese_run(db):
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["a", "b"]))
    store.append_run(_run("r2", "BW846", ["c"]))
    assert [l.message for l in store.for_run("r1")] == ["a", "b"]


def test_un_run_sin_lineas_no_toca_la_base(db):
    store = LogStore(db)
    assert store.append_run(_run("r1", "BW846", [])) == 0
    assert store.total() == 0


def test_el_limite_recorta_las_primeras_no_las_ultimas(db):
    """Cuando algo falla, lo que hay que leer es el final."""
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", [f"linea {i}" for i in range(10)]))
    lineas = store.for_case("BW846", limit=3)
    assert [l.message for l in lineas] == ["linea 7", "linea 8", "linea 9"]


def test_los_conteos_de_muchas_filas_salen_en_una_consulta(db):
    """La grilla los necesita para no ofrecer un botón que abre un modal vacío."""
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["a", "b"]))
    store.append_run(_run("r2", "BX208", ["c"]))
    conteos = store.counts_for_cases(["BW846", "BX208", "SIN_LOG"])
    assert conteos == {"BW846": 2, "BX208": 1}


def test_conteos_con_lista_vacia_no_consulta(db):
    assert LogStore(db).counts_for_cases([]) == {}


# ── Limpieza ────────────────────────────────────────────────────────────


def test_limpiar_una_fila_no_toca_las_otras(db):
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["a", "b"]))
    store.append_run(_run("r2", "BX208", ["c"]))
    assert store.clear_case("BW846") == 2
    assert store.for_case("BW846") == []
    assert store.count_for_case("BX208") == 1


def test_borrar_por_antiguedad(db):
    store = LogStore(db)
    viejo = time.time() - 40 * 86400
    store.append_run(_run("r1", "BW846", ["de hace 40 días"], ts=viejo))
    store.append_run(_run("r2", "BW846", ["de hoy"]))

    assert store.prune_older_than(30) == 1
    assert [l.message for l in store.for_case("BW846")] == ["de hoy"]


def test_antiguedad_en_cero_no_borra_nada(db):
    """0 = sin límite de días, no "borrar todo"."""
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["a"]))
    assert store.prune_older_than(0) == 0
    assert store.total() == 1


def test_el_tope_por_fila_deja_las_ultimas(db):
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", [f"l{i}" for i in range(10)]))
    store.append_run(_run("r2", "BX208", [f"o{i}" for i in range(10)]))

    assert store.prune_keeping(3) == 14  # 7 de cada fila
    assert [l.message for l in store.for_case("BW846")] == ["l7", "l8", "l9"]
    assert [l.message for l in store.for_case("BX208")] == ["o7", "o8", "o9"]


def test_el_tope_en_cero_no_borra_nada(db):
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["a", "b"]))
    assert store.prune_keeping(0) == 0
    assert store.total() == 2


def test_borrar_huerfanos_deja_las_filas_vivas(db):
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["sigue en la fuente"]), source="casos")
    store.append_run(_run("r2", "VIEJO", ["ya no está"]), source="casos")

    assert store.prune_orphans("casos", ["BW846"]) == 1
    assert store.count_for_case("BW846") == 1
    assert store.count_for_case("VIEJO") == 0


def test_borrar_huerfanos_no_toca_otra_fuente(db):
    store = LogStore(db)
    store.append_run(_run("r1", "X", ["de otra fuente"]), source="otra")
    assert store.prune_orphans("casos", ["BW846"]) == 0
    assert store.count_for_case("X") == 1


def test_una_lista_vacia_de_vivos_no_borra_nada(db):
    """
    Una fuente que falló al leer devuelve cero filas. Tomar eso por "no queda
    ninguna" borraría todo el historial por un error de red.
    """
    store = LogStore(db)
    store.append_run(_run("r1", "BW846", ["a"]), source="casos")
    assert store.prune_orphans("casos", []) == 0
    assert store.total() == 1


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
