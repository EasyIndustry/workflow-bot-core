"""
De punta a punta: un flujo real, con los cuatro ports inyectados.

Estos tests corren el corpus de `tests/flows/` contra el plugin de prueba y
adapters falsos. Es la prueba de que las capas encastran —Core → Plugin →
Adapter— y de que el mundo entero es sustituible desde afuera.

El test de reintentos es el que justifica el `ClockPort`: cinco esperas de
sesenta segundos, en milisegundos, y además **verificando cuánto se esperó** —
algo que con `time.sleep` incrustado no se puede afirmar de ninguna manera.
"""

from __future__ import annotations

from backend.tests.fakes import FakeFs


def test_el_flujo_lineal_corre_con_los_builtins(demo_instance):
    resultado = demo_instance.run("lineal", "0044", row={"id": "0044"})
    assert resultado.status == "ok"
    assert any("procesando 0044" in e.message for e in resultado.logs)


def test_una_decision_toma_la_rama_de_su_etiqueta(demo_instance):
    resultado = demo_instance.run(
        "decision", "0044", row={"id": "0044", "tipo": "express"}
    )
    assert resultado.status == "ok"
    assert any("urgente: 0044" in e.message for e in resultado.logs)


def test_una_decision_sin_coincidencia_cae_en_la_rama_por_defecto(demo_instance):
    resultado = demo_instance.run(
        "decision", "0044", row={"id": "0044", "tipo": "cualquier cosa"}
    )
    assert resultado.status == "ok"
    assert any("normal: 0044" in e.message for e in resultado.logs)


def test_la_rama_de_error_corre_y_el_flujo_termina_en_err(demo_instance, adapters):
    """
    Un fallo con arista |err| ramifica en vez de cortar; el nodo de la rama
    decide si el caso queda marcado como fallido.
    """
    # FakeFs vacío: la carpeta del caso no existe, así que demo.buscar falla.
    resultado = demo_instance.run("ramas", "0044", row={"id": "0044"})

    assert resultado.status == "err"
    assert any("no se encontró la carpeta" in e.message for e in resultado.logs)
    # Recorrió los cuatro nodos: no se cortó en el que falló.
    assert [t.node_id for t in resultado.trace] == ["BUSCAR", "FALTA", "CORTAR"]


def test_el_flujo_anidado_comparte_contexto_y_traza(demo_instance):
    resultado = demo_instance.run("anidado", "0044", row={"id": "0044"})
    assert resultado.status == "ok"
    # La traza del subflujo se pega a la del padre, con su profundidad.
    profundidades = {t.depth for t in resultado.trace}
    assert profundidades == {1, 2}
    assert any(t.flow == "lineal" for t in resultado.trace)


def test_los_reintentos_no_esperan_tiempo_real(demo_instance, adapters):
    """
    Cinco intentos de sesenta segundos: cinco minutos de espera declarada, cero
    de espera real. Y el total queda **verificable**, que es lo que `time.sleep`
    incrustado no permite afirmar.
    """
    reloj = adapters["clock"]
    resultado = demo_instance.run("reintentos", "0044", row={"id": "0044"})

    assert resultado.status == "err"
    assert any("se agotaron los intentos" in e.message for e in resultado.logs)
    assert reloj.slept == [60.0] * 4
    assert reloj.total_slept == 240.0


def test_los_reintentos_terminan_cuando_aparece_la_senal(demo_instance, adapters):
    adapters["fs"].write_text("/tmp/listo", "")
    resultado = demo_instance.run("reintentos", "0044", row={"id": "0044"})

    assert resultado.status == "ok"
    assert adapters["clock"].slept == []  # no esperó ni una vez


def test_un_flujo_usa_los_cuatro_ports_en_una_corrida(demo_instance, adapters):
    """
    El caso completo: HTTP para traer el caso, filesystem para archivarlo,
    proceso para disparar el trabajo, reloj para esperar. Ningún plugin importó
    una librería, y ningún adapter real corrió.
    """
    adapters["fs"] = FakeFs(files={"/casos/0044/pieza.stl": "x"}, dirs=["/archivo"])
    adapters["http"].stub(
        "https://api.test/casos/0044",
        text='{"pais": "AR", "prioridad": "alta"}',
    )
    adapters["process"].stub("procesar", exit_code=0, stdout="listo")
    demo_instance.registry.bind_adapters(adapters)
    demo_instance.config.update({"demoBase": "/casos"})

    resultado = demo_instance.run("mundo", "0044", row={"id": "0044"})

    assert resultado.status == "ok", resultado.message
    # HTTP: los campos de la respuesta se mergearon al contexto del run.
    assert resultado.trace[0].outputs["response"] == {"pais": "AR", "prioridad": "alta"}
    # Filesystem: la carpeta se movió de verdad, en el fake.
    assert adapters["fs"].files == {"/archivo/0044/pieza.stl": "x"}
    # Proceso: se disparó el comando que decía el flujo.
    assert adapters["process"].calls[0]["command"] == ["procesar"]
    # Reloj: esperó lo declarado, sin gastar tiempo.
    assert adapters["clock"].total_slept == 30.0
    # Y el último log interpoló una variable que produjo un nodo anterior.
    assert any("archivado en /archivo/0044" in e.message for e in resultado.logs)


def test_si_la_api_falla_el_flujo_toma_la_rama_de_error(demo_instance, adapters):
    adapters["http"].stub("https://api.test/casos/0044", status=404, text='{"e": "no está"}')

    resultado = demo_instance.run("mundo", "0044", row={"id": "0044"})

    assert resultado.status == "err"
    assert resultado.failed_node == "ERROR"
    # El cuerpo del error quedó en la traza, que es para lo que existe.
    assert resultado.trace[0].outputs["response"] == {"e": "no está"}


def test_un_dry_run_del_corpus_no_toca_ningun_adapter(demo_instance, adapters):
    """
    Un dry-run recorre y valida sin ejecutar. Que ningún fake registre una
    llamada es la forma más directa de comprobarlo.
    """
    for nombre in ("lineal", "ramas", "mundo", "reintentos"):
        resultado = demo_instance.run(nombre, "0044", row={"id": "0044"}, dry_run=True)
        assert resultado.dry_run

    assert adapters["http"].calls == []
    assert adapters["fs"].calls == []
    assert adapters["process"].calls == []
    assert adapters["clock"].slept == []


def test_cada_flujo_del_corpus_pasa_el_diagnostico(demo_instance):
    """
    Con el plugin de prueba instalado, ningún flujo del corpus debe tener un
    error de diagnóstico. Es lo que mantiene el corpus honesto: si alguien
    agrega un flujo con un tool inventado, este test lo dice.
    """
    from backend.core.flow.parser import Severity

    for wf in demo_instance.workflows.list():
        _, diagnosticos = demo_instance.diagnose(wf.name)
        errores = [d for d in diagnosticos if d.severity is Severity.ERROR]
        assert errores == [], f"{wf.name}: {[d.message for d in errores]}"
