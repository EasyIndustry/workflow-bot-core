"""
Decisión manual (issue #37): la corrida se pausa en una decisión hasta que una
persona elige la rama, sin tomar el hilo, y se retoma desde un checkpoint en la
base con el mismo run_id.

Los tests siguen, en orden, la lista "Qué verificar" del issue.
"""

from __future__ import annotations

import json

import pytest

from backend.adapters.storage_sqlite import IN_MEMORY, SqliteStorageAdapter
from backend.core.cli import main as cli_main
from backend.core.contract import (
    STATUS_ERR,
    STATUS_OK,
    STATUS_WAITING,
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.flow.parser import DecisionNode, parse_flow
from backend.core.flow.serializer import from_dict, to_mermaid, verificar
from backend.core.instance import Instance, PendingDecision
from backend.core.stores import StoreError
from backend.core.users import UserError
from backend.tests.fakes import fake_adapters

FLUJO = """\
flowchart TD
    SN1(inicio)
    N1["Producir § t.producir | valor=hecho-{id}"]
    D1{Revisión § ok | manual | ayuda=Mirá el PDF antes de elegir}
    N2["Mover § t.marca | x=si"]
    N3["Rehacer § t.marca | x=no {codigo}"]
    SN1 --> N1
    N1 --> D1
    D1 -->|si| N2
    D1 -->|no, rehacer| N3
"""

DOS_DECISIONES = """\
flowchart TD
    SN1(inicio)
    D1{Primera § a | manual}
    D2{Segunda § b | manual}
    N1["t.marca | x=uno"]
    N2["t.marca | x={a}-{b}"]
    SN1 --> D1
    D1 -->|si| D2
    D1 -->|no| N1
    D2 -->|si| N2
    D2 -->|no| N1
"""


def _producir(ctx: ToolContext) -> ToolResult:
    return ToolResult.ok(codigo=ctx.params["valor"])


def _marca(ctx: ToolContext) -> ToolResult:
    return ToolResult.ok(visto=ctx.params["x"])


def _plugin() -> Plugin:
    manifest = PluginManifest(name="t", label="Test", version="1.0.0")
    producir = ToolManifest(
        id="t.producir", label="Producir", category="TEST",
        params=(Param("valor", ParamType.STR),),
        outputs=(Output("codigo", ParamType.STR),),
    )
    marca = ToolManifest(
        id="t.marca", label="Marca", category="TEST",
        params=(Param("x", ParamType.STR),),
        outputs=(Output("visto", ParamType.STR),),
    )
    return Plugin(
        manifest=manifest,
        tools=[FunctionTool(manifest=producir, fn=_producir), FunctionTool(manifest=marca, fn=_marca)],
    )


def _instancia(tmp_path, storage=None) -> Instance:
    inst = Instance(
        tmp_path,
        storage=storage if storage is not None else SqliteStorageAdapter(IN_MEMORY),
        adapters=fake_adapters(),
    )
    inst.registry._add_plugin("t", "tests:t", _plugin())
    return inst


@pytest.fixture
def inst(tmp_path):
    instancia = _instancia(tmp_path)
    instancia.workflows.save("revision", FLUJO)
    instancia.workflows.save("dos", DOS_DECISIONES)
    yield instancia
    instancia.close()


def _nodos(resultado) -> list[str]:
    return [t.node_id for t in resultado.trace]


# ── Sintaxis ────────────────────────────────────────────────────────────


def test_parsea_manual_y_ayuda():
    nodo = parse_flow(FLUJO).nodes["D1"]
    assert nodo == DecisionNode(
        variable="ok", display="Revisión", line=nodo.line, manual=True,
        ayuda="Mirá el PDF antes de elegir",
    )


def test_una_decision_comun_no_cambia():
    grafo = parse_flow('flowchart TD\n S(inicio)\n D{Rev § campo}\n S --> D\n D -->|a| S\n')
    nodo = grafo.nodes["D"]
    assert (nodo.variable, nodo.manual, nodo.ayuda) == ("campo", False, "")
    assert "manual" not in grafo.to_dict()["nodes"]["D"]


def test_manual_sin_aristas_con_condicion_es_error():
    grafo = parse_flow(
        'flowchart TD\n S(inicio)\n D{x | manual}\n A["t.marca | x=1"]\n S --> D\n D --> A\n'
    )
    assert not grafo.runnable
    assert any("no tiene ninguna arista con condición" in d.message for d in grafo.errors)


def test_opcion_desconocida_en_una_decision_es_error():
    grafo = parse_flow('flowchart TD\n S(inicio)\n D{x | raro}\n S --> D\n D -->|a| S\n')
    assert any('no se reconoce "raro"' in d.message for d in grafo.errors)


def test_serializer_ida_y_vuelta_con_ayuda_citada():
    grafo = parse_flow(FLUJO)
    datos = grafo.to_dict()
    assert datos["nodes"]["D1"]["manual"] is True
    datos["nodes"]["D1"]["ayuda"] = "Revisá esto, y aquello | también"
    editado = from_dict(datos)
    assert verificar(editado) == []
    vuelta = parse_flow(to_mermaid(editado)).nodes["D1"]
    assert vuelta.manual is True
    assert vuelta.ayuda == "Revisá esto, y aquello | también"


def test_verificar_avisa_la_secuencia_literal_de_una_llave():
    datos = parse_flow(FLUJO).to_dict()
    datos["nodes"]["D1"]["ayuda"] = "no uses #125; así"
    assert any("#125;" in p for p in verificar(from_dict(datos)))


def test_el_catalogo_publica_la_capacidad(inst):
    assert inst.registry.catalog()["capabilities"]["manual_decisions"] is True


# ── 1. Pausa ────────────────────────────────────────────────────────────


def test_1_pausa_con_las_opciones_y_no_corre_las_ramas(inst):
    r = inst.run("revision", "42", row={"id": "42"})

    assert r.status == STATUS_WAITING
    assert not r.failed
    assert _nodos(r) == ["N1", "D1"]
    assert r.waiting["node_id"] == "D1"
    assert r.waiting["variable"] == "ok"
    assert r.waiting["display"] == "Revisión"
    assert r.waiting["ayuda"] == "Mirá el PDF antes de elegir"
    assert [(o["value"], o["to"], o["to_fn"]) for o in r.waiting["options"]] == [
        ("si", "N2", "t.marca"),
        ("no", "N3", "t.marca"),
    ]
    assert r.waiting["options"][1]["condition"] == "no, rehacer"
    assert r.waiting["options"][0]["to_display"] == "Mover"

    guardado = inst.runs.summary(r.run_id)
    assert guardado.status == STATUS_WAITING
    assert inst.run_detail(r.run_id)["waiting"]["node_id"] == "D1"


def test_1b_pausa_aunque_la_variable_ya_tenga_valor(inst):
    r = inst.run("revision", "42", row={"id": "42", "ok": "si"})
    assert r.status == STATUS_WAITING
    assert _nodos(r) == ["N1", "D1"]


# ── 2. El hilo queda libre ──────────────────────────────────────────────


def test_2_otro_caso_corre_mientras_uno_espera(inst):
    esperando = inst.run("revision", "1", row={"id": "1"})
    assert esperando.status == STATUS_WAITING

    otro = inst.run("revision", "2", row={"id": "2"})
    assert otro.status == STATUS_WAITING
    assert otro.run_id != esperando.run_id

    final = inst.resume(otro.run_id, "si")
    assert final.status == STATUS_OK
    assert inst.runs.summary(esperando.run_id).status == STATUS_WAITING


# ── 3 y 4. Retomar ──────────────────────────────────────────────────────


def test_3_resume_sigue_por_la_rama_elegida_con_el_mismo_run(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    final = inst.resume(r.run_id, "no", actor="local")

    assert final.run_id == r.run_id
    assert final.status == STATUS_OK
    assert final.waiting is None
    assert _nodos(final) == ["N1", "D1", "N3"]
    decision = final.trace[1]
    assert decision.decision_value == "no"
    assert decision.decided_by == "local"

    detalle = inst.run_detail(r.run_id)
    assert detalle["status"] == STATUS_OK
    assert [t["node_id"] for t in detalle["trace"]] == ["N1", "D1", "N3"]
    assert detalle["trace"][1]["decided_by"] == "local"
    mensajes = [l["message"] for l in detalle["logs"]]
    assert 'Decisión manual ok = "no" (por local)' in mensajes
    # El log de la primera parte sigue ahí, una sola vez.
    assert sum(m.startswith("Flujo principal:") for m in mensajes) == 1
    assert inst.waits.get(r.run_id) is None


def test_4_las_salidas_anteriores_siguen_disponibles(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    final = inst.resume(r.run_id, "rehacer")
    assert final.trace[-1].node_id == "N3"
    assert final.trace[-1].params == {"x": "no hecho-42"}


# ── 5. Validación ───────────────────────────────────────────────────────


def test_5_valor_que_no_es_rama_no_toca_el_run(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    with pytest.raises(UserError, match='"quizas" no es una rama'):
        inst.resume(r.run_id, "quizas")
    assert inst.runs.summary(r.run_id).status == STATUS_WAITING
    assert inst.waits.get(r.run_id) is not None


def test_5b_resume_de_un_run_que_no_espera(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    inst.resume(r.run_id, "si")
    with pytest.raises(UserError, match="no está esperando"):
        inst.resume(r.run_id, "si")
    with pytest.raises(UserError, match="No existe"):
        inst.resume("run-inexistente", "si")


def test_5c_el_actor_de_resume_tiene_que_existir(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    with pytest.raises(UserError):
        inst.resume(r.run_id, "si", actor="nadie")
    assert inst.runs.summary(r.run_id).status == STATUS_WAITING


# ── 6. Un caso, una espera ──────────────────────────────────────────────


def test_6_run_del_mismo_caso_mientras_espera_es_distinguible(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    with pytest.raises(PendingDecision) as exc:
        inst.run("revision", "42", row={"id": "42"})
    assert exc.value.run_id == r.run_id
    assert r.run_id in str(exc.value)
    # Un dry run no toca nada: se permite igual.
    assert inst.run("revision", "42", row={"id": "42"}, dry_run=True).status == STATUS_OK
    # Retomado, el caso vuelve a poder correr.
    inst.resume(r.run_id, "si")
    assert inst.run("revision", "42", row={"id": "42"}).status == STATUS_WAITING


# ── 7. Sobrevive a un reinicio ──────────────────────────────────────────


def test_7_se_retoma_despues_de_reiniciar_el_proceso(tmp_path):
    base = tmp_path / "bot.db"
    primera = _instancia(tmp_path, SqliteStorageAdapter(base))
    primera.workflows.save("revision", FLUJO)
    r = primera.run("revision", "42", row={"id": "42"})
    primera.close()

    segunda = _instancia(tmp_path, SqliteStorageAdapter(base))
    try:
        final = segunda.resume(r.run_id, "no")
        assert final.status == STATUS_OK
        assert final.trace[-1].params == {"x": "no hecho-42"}
    finally:
        segunda.close()


def test_7b_se_retoma_el_flujo_del_checkpoint_no_el_editado(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    inst.workflows.save("revision", FLUJO.replace("x=no {codigo}", "x=editado"))
    final = inst.resume(r.run_id, "no")
    assert final.trace[-1].params == {"x": "no hecho-42"}


# ── 8. Dos decisiones seguidas ──────────────────────────────────────────


def test_8_dos_decisiones_manuales_seguidas(inst):
    r = inst.run("dos", "7")
    assert r.waiting["node_id"] == "D1"

    medio = inst.resume(r.run_id, "si")
    assert medio.status == STATUS_WAITING
    assert medio.run_id == r.run_id
    assert medio.waiting["node_id"] == "D2"
    assert inst.runs.summary(r.run_id).status == STATUS_WAITING

    final = inst.resume(r.run_id, "si")
    assert final.status == STATUS_OK
    assert _nodos(final) == ["D1", "D2", "N2"]
    assert final.trace[-1].params == {"x": "si-si"}


# ── 9. Ningún env en la base ────────────────────────────────────────────


def test_9_la_espera_no_escribe_env_ni_config(inst):
    inst.env.save("SECRETO", "valor-que-no-se-escribe", secret=True)
    r = inst.run("revision", "42", row={"id": "42"})

    checkpoint = inst.waits.get(r.run_id).checkpoint
    assert "env" not in checkpoint and "config" not in checkpoint
    filas = inst.db.query("SELECT checkpoint FROM run_waits") + inst.db.query("SELECT data FROM runs")
    assert all("valor-que-no-se-escribe" not in json.dumps(f) for f in filas)


# ── 10. Dry run ─────────────────────────────────────────────────────────


def test_10_dry_run_no_pausa_y_explora_la_primera_rama(inst):
    r = inst.run("revision", "42", row={"id": "42"}, dry_run=True)
    assert r.status == STATUS_OK
    assert _nodos(r) == ["N1", "D1", "N2"]
    assert any(
        'decisión manual: en seco se explora la rama "si"' in l.message for l in r.logs
    )


def test_10b_dry_run_sigue_la_rama_de_la_fila(inst):
    r = inst.run("revision", "42", row={"id": "42", "ok": "no"}, dry_run=True)
    assert _nodos(r) == ["N1", "D1", "N3"]


# ── Alrededor ───────────────────────────────────────────────────────────


def test_discard_cierra_la_espera(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    final = inst.discard_wait(r.run_id, actor="local")
    assert final.status == STATUS_ERR
    assert final.error_kind == "discarded"
    assert final.message == "Decisión descartada por local"

    detalle = inst.run_detail(r.run_id)
    assert detalle["status"] == STATUS_ERR
    assert detalle["error_kind"] == "discarded"
    assert inst.waits.get(r.run_id) is None
    with pytest.raises(UserError):
        inst.resume(r.run_id, "si")


def test_listar_esperas_y_filtrar_por_status(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    inst.run("dos", "7")
    inst.resume(inst.list_waiting(case_id="7")[0]["run_id"], "no")

    esperando = inst.list_waiting()
    assert [e["run_id"] for e in esperando] == [r.run_id]
    assert esperando[0]["waiting"]["options"][0]["value"] == "si"
    assert [x["run_id"] for x in inst.list_runs(status="waiting")] == [r.run_id]
    assert inst.list_runs(only_failed=True) == []
    with pytest.raises(StoreError):
        inst.list_runs(status="corriendo")
    assert inst.describe_installation()["runs"]["en_espera"] == 1


def test_la_retencion_no_borra_una_espera(inst):
    r = inst.run("revision", "42", row={"id": "42"})
    terminado = inst.resume(inst.run("revision", "otro", row={"id": "otro"}).run_id, "si")
    antes = len(inst.logs.for_run(r.run_id))
    assert antes > 1

    assert inst.logs.prune_keeping(1) > 0
    assert len(inst.logs.for_run(r.run_id)) == antes

    inst.db.execute("UPDATE run_logs SET ts = 1")
    inst.logs.prune_older_than(1)
    assert len(inst.logs.for_run(r.run_id)) == antes
    assert inst.logs.for_run(terminado.run_id) == []

    assert inst.runs.prune(keep=0) == 1
    assert inst.runs.summary(r.run_id) is not None
    assert inst.runs.summary(terminado.run_id) is None


def test_decision_manual_en_un_subflujo_no_esta_soportada(inst):
    inst.workflows.save("hijo", FLUJO)
    inst.workflows.save(
        "padre",
        'flowchart TD\n S(inicio)\n E["flow.ejecutar | flowName=hijo"]\n S --> E\n',
    )
    r = inst.run("padre", "1", row={"id": "1"})
    assert r.status == STATUS_ERR
    assert r.error_kind == "manual_in_subflow"
    assert "no soportado todavía" in r.message


def test_cli_resume_y_runs_waiting(tmp_path, capsys):
    base = tmp_path / "bot.db"
    flujo = tmp_path / "simple.mmd"
    flujo.write_text(
        'flowchart TD\n S(inicio)\n D{x | manual}\n A["core.log | message=listo"]\n'
        " S --> D\n D -->|si| A\n",
        encoding="utf-8",
    )
    (tmp_path / "boot.env").write_text(f"storage={base}\n", encoding="utf-8")
    raiz = ["--root", str(tmp_path)]

    assert cli_main([*raiz, "run", str(flujo), "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    assert cli_main([*raiz, "runs", "--status", "waiting", "--json"]) == 0
    esperando = json.loads(capsys.readouterr().out)
    assert [e["run_id"] for e in esperando] == [run_id]

    assert cli_main([*raiz, "resume", run_id, "no-existe"]) == 3
    capsys.readouterr()
    assert cli_main([*raiz, "resume", run_id, "si", "--json"]) == 0
    final = json.loads(capsys.readouterr().out)
    assert final["status"] == STATUS_OK
    assert [t["node_id"] for t in final["trace"]] == ["D", "A"]


# ── Issue #38: {variables} en la ayuda ─────────────────────────────────

AYUDA = """\
flowchart TD
    SN1(inicio)
    N1["t.producir | valor=3"]
    D1{¿Seguir? § aprobado | manual | ayuda=#quot;Hay {N1.codigo} fallidos, caso {id}, plano {codigo}, {no_existe}, {env.SECRETO}#quot;}
    N2["t.marca | x=si"]
    N3["t.producir | valor=7"]
    D2{Otra § b | manual | ayuda=Ahora {N3.codigo} y antes {N1.codigo}}
    SN1 --> N1
    N1 --> D1
    D1 -->|si| N2
    D1 -->|no| N3
    N3 --> D2
    D2 -->|si| N2
"""


def test_38_1_a_4_la_ayuda_se_resuelve_al_pausar(inst):
    inst.env.save("SECRETO", "no-se-escribe", secret=True)
    inst.workflows.save("ayuda", AYUDA)
    r = inst.run("ayuda", "42", row={"id": "42"})

    assert r.status == STATUS_WAITING
    assert r.waiting["ayuda"] == (
        "Hay 3 fallidos, caso 42, plano 3, {no_existe}, {env.SECRETO}"
    )
    assert r.waiting["ayuda_plantilla"].startswith("Hay {N1.codigo} fallidos")
    filas = inst.db.query("SELECT data FROM runs") + inst.db.query("SELECT checkpoint FROM run_waits")
    assert all("no-se-escribe" not in json.dumps(f) for f in filas)
    assert inst.run_detail(r.run_id)["waiting"]["ayuda"] == r.waiting["ayuda"]


def test_38_5_editor_ida_y_vuelta_con_llaves():
    datos = parse_flow(AYUDA).to_dict()
    editado = from_dict(datos)
    assert verificar(editado) == []
    texto = to_mermaid(editado)
    # mermaid.js no acepta llaves sueltas adentro del rombo: van como entidad.
    linea = next(l for l in texto.splitlines() if l.strip().startswith("D1{"))
    assert linea.count("{") == 1 and linea.count("}") == 1
    assert "#123;N1.codigo#125;" in linea
    vuelta = parse_flow(texto)
    assert vuelta.runnable
    assert vuelta.nodes["D1"].ayuda == parse_flow(AYUDA).nodes["D1"].ayuda


def test_38_6_la_segunda_decision_resuelve_con_su_contexto(inst):
    inst.workflows.save("ayuda", AYUDA)
    r = inst.run("ayuda", "42", row={"id": "42"})
    medio = inst.resume(r.run_id, "no")
    assert medio.waiting["node_id"] == "D2"
    assert medio.waiting["ayuda"] == "Ahora 7 y antes 3"


def test_38_capacidad(inst):
    assert inst.registry.catalog()["capabilities"]["manual_decision_help_vars"] is True
