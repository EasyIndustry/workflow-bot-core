"""
Secretos tapados en la traza y el log de un run.

La traza guarda los params ya resueltos. Sin `redact.py`, un `{env.TOKEN}`
interpolado en un param común quedaba en claro en la base, y salía por la API,
la CLI y el MCP. Lo mismo un `Setting` secreto que llega a un param por
`config_key`, o el campo secreto de un `Resource` que un tool mete en su
mensaje. El tool, en cambio, sigue recibiendo el valor real.
"""

from __future__ import annotations

import json

from backend.core.contract import (
    Field,
    FunctionTool,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    Resource,
    Setting,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.redact import TAPADO, redact, secret_set

TOKEN = "tok-9f8e7d6c"
CLAVE = "clave-api-1234"
DEPOSITO = "deposito-xyz-987"


# ── redact(), suelto ────────────────────────────────────────────────────


def test_tapa_un_secreto_interpolado_adentro_de_un_texto():
    assert redact("Bearer tok-9f8e7d6c", secret_set([TOKEN])) == "Bearer ***"


def test_recorre_dicts_y_listas_sin_tocar_el_original():
    original = {"headers": {"Authorization": f"Bearer {TOKEN}"}, "ids": [TOKEN, "otro"]}
    tapado = redact(original, secret_set([TOKEN]))
    assert tapado == {"headers": {"Authorization": "Bearer ***"}, "ids": [TAPADO, "otro"]}
    # El contexto del run y el checkpoint siguen necesitando el valor real.
    assert original["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_un_secreto_corto_se_tapa_solo_entero():
    """Buscar "si" adentro de todo dejaría la traza ilegible."""
    secretos = secret_set(["si"])
    assert redact("si", secretos) == TAPADO
    assert redact("sigue", secretos) == "sigue"


def test_el_mas_largo_se_tapa_primero():
    secretos = secret_set(["abcd", "abcdefgh"])
    assert redact("x abcdefgh x", secretos) == "x *** x"


def test_un_param_tipado_tambien_se_tapa():
    """El secreto "123456" llega al tool como el int 123456."""
    assert redact({"pin": 123456, "ok": True}, secret_set(["123456"])) == {"pin": TAPADO, "ok": True}


def test_sin_secretos_no_cambia_nada():
    valor = {"a": "b"}
    assert redact(valor, secret_set([None, "", "  "])) is valor


# ── En una corrida ──────────────────────────────────────────────────────

FLUJO = """\
flowchart TD
    SN1(inicio)
    N1["Llamar § s.llamar | auth=Bearer {env.API_TOKEN} | base={env.BASE_URL}"]
    SN1 --> N1
"""


def _plugin(recibido: dict) -> Plugin:
    """Un tool que anota lo que recibió y lo devuelve por todas las salidas."""

    def _llamar(ctx: ToolContext) -> ToolResult:
        destino = ctx.resource("destinos", "frio") or {}
        recibido.update(ctx.params, deposito=destino.get("token"))
        ctx.log(f"llamando con {ctx.params['auth']} y {destino.get('token')}")
        return ToolResult.ok(
            f"usé {ctx.params['clave']}",
            eco=dict(ctx.params),
            deposito=destino.get("token"),
        )

    manifest = PluginManifest(
        name="s",
        label="Secretos",
        settings=(Setting("apiKey", ParamType.STR, secret=True),),
        resources=(
            Resource(
                name="destinos",
                label="Destinos",
                fields=(Field("ruta"), Field("token", ParamType.STR, secret=True)),
            ),
        ),
    )
    tool = FunctionTool(
        manifest=ToolManifest(
            id="s.llamar",
            label="llamar",
            category="TEST",
            params=(
                Param("auth"),
                Param("base"),
                Param("clave", config_key="apiKey"),
            ),
            extra_outputs=True,
        ),
        fn=_llamar,
    )
    return Plugin(manifest=manifest, tools=[tool])


def _preparar(instance) -> dict:
    recibido: dict = {}
    instance.registry._add_plugin("s", "tests:s", _plugin(recibido))
    instance.env.save("API_TOKEN", TOKEN, secret=True)
    instance.env.save("BASE_URL", "https://api.ejemplo")
    instance.env.save("DEPOSITO_TOKEN", DEPOSITO, secret=True)
    instance.config.update({"apiKey": CLAVE})
    instance.write_resource_item(
        "s", "destinos", "frio", {"name": "frio", "ruta": "/x", "token": "{env.DEPOSITO_TOKEN}"}
    )
    instance.workflows.save("secretos", FLUJO)
    return recibido


def _todo_lo_que_sale(instance, resultado) -> str:
    """Todo lo que la API, la CLI y el MCP pueden leer de esa corrida."""
    return json.dumps(
        [
            resultado.to_dict(),
            instance.run_detail(resultado.run_id),
            instance.case_log(resultado.case_id),
        ],
        ensure_ascii=False,
        default=str,
    )


def test_la_traza_guardada_no_tiene_los_secretos(instance):
    recibido = _preparar(instance)

    resultado = instance.run("secretos", "caso-1")

    assert resultado.status == "ok", resultado.message
    # El tool recibió los valores reales: tapar es para lo que se guarda.
    assert recibido["auth"].strip() == f"Bearer {TOKEN}"
    assert recibido["clave"] == CLAVE
    assert recibido["deposito"] == DEPOSITO

    salida = _todo_lo_que_sale(instance, resultado)
    for secreto in (TOKEN, CLAVE, DEPOSITO):
        assert secreto not in salida

    nodo = instance.run_detail(resultado.run_id)["trace"][-1]
    assert nodo["params"]["auth"].strip() == "Bearer ***"
    assert nodo["params"]["clave"] == TAPADO
    assert nodo["outputs"]["deposito"] == TAPADO
    assert nodo["message"] == "usé ***"
    # Una variable que no es secreta queda en claro: la traza sigue sirviendo.
    assert nodo["params"]["base"] == "https://api.ejemplo"


def test_un_dry_run_tampoco_los_deja_en_el_log(instance):
    _preparar(instance)

    resultado = instance.run("secretos", "caso-1", dry_run=True)

    salida = _todo_lo_que_sale(instance, resultado)
    assert TOKEN not in salida
    assert any("[DRY]" in e.message and "Bearer ***" in e.message for e in resultado.logs)


def test_un_botenv_que_pisa_un_secreto_tambien_se_tapa(instance, monkeypatch):
    _preparar(instance)
    monkeypatch.setenv("BOTENV_API_TOKEN", "otro-token-del-entorno")

    resultado = instance.run("secretos", "caso-1")

    assert "otro-token-del-entorno" not in _todo_lo_que_sale(instance, resultado)


def test_una_corrida_retomada_tambien_se_tapa(instance):
    """`resume` es el otro camino que escribe una traza (issue #37)."""
    _preparar(instance)
    instance.workflows.save(
        "con_espera",
        """\
flowchart TD
    SN1(inicio)
    D1{Revisión § ok | manual}
    N1["Llamar § s.llamar | auth=Bearer {env.API_TOKEN}"]
    SN1 --> D1
    D1 -->|si| N1
""",
    )
    pausado = instance.run("con_espera", "caso-1")
    assert pausado.status == "waiting"

    resultado = instance.resume(pausado.run_id, "si")

    assert resultado.status == "ok", resultado.message
    salida = _todo_lo_que_sale(instance, resultado)
    assert TOKEN not in salida
    assert CLAVE not in salida
