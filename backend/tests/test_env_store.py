"""
Tests del almacén de variables y secretos.

Lo que se prueba acá no es "guarda y lee": es que un secreto **no** se pueda
leer. Cada test que dice "no devuelve el valor" es la propiedad del almacén, no
un detalle de implementación.
"""

from __future__ import annotations

from backend.core.env_store import EnvError, EnvStore, referencias


# ── Variables ───────────────────────────────────────────────────────────


def test_una_variable_se_guarda_en_claro_y_se_lee(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    store.save("CASOS_ROOT", "D:/casos")
    v = store.get("CASOS_ROOT")
    assert v is not None
    assert v.secret is False
    assert v.value == "D:/casos"
    assert v.has_value is True
    assert store.resolve()["CASOS_ROOT"] == "D:/casos"


def test_una_variable_puede_quedar_vacia_para_declarar_el_nombre(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    store.save("PENDIENTE", "")
    v = store.get("PENDIENTE")
    assert v is not None and v.has_value is False


def test_guardar_de_nuevo_reemplaza_y_no_duplica(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    store.save("MAQUINA", "cnc4")
    store.save("MAQUINA", "cnc5")
    assert [v.name for v in store.list()] == ["MAQUINA"]
    assert store.get("MAQUINA").value == "cnc5"


# ── Secretos: la propiedad que importa ──────────────────────────────────


def test_un_secreto_no_devuelve_su_valor_ni_en_get_ni_en_list(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    store.save("API_TOKEN", "tok-abc123", secret=True)

    v = store.get("API_TOKEN")
    assert v.secret is True
    assert v.value is None       # no lo lleva el objeto
    assert v.has_value is True   # pero sí hay valor cargado

    # Y tampoco aparece en el dict que sale por la API: la clave no existe,
    # en vez de existir en null.
    assert "value" not in v.to_dict()

    (listado,) = store.list()
    assert listado.value is None


def test_un_secreto_no_queda_en_claro_en_la_base(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    store.save("API_TOKEN", "tok-abc123", secret=True)
    crudo = store.db.one("SELECT value FROM env WHERE name = 'API_TOKEN'")["value"]
    assert "tok-abc123" not in crudo
    assert crudo != ""


def test_resolve_es_el_unico_camino_al_valor_de_un_secreto(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    store.save("API_TOKEN", "tok-abc123", secret=True)
    assert store.resolve()["API_TOKEN"] == "tok-abc123"


def test_un_secreto_sin_valor_se_rechaza(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    try:
        store.save("VACIO", "", secret=True)
        raise AssertionError("debió rechazar un secreto sin valor")
    except EnvError:
        pass


def test_una_variable_se_puede_pasar_a_secreto(db, crypto, tmp_path):
    """Es lo que hay que hacer cuando algo se cargó en claro por error."""
    store = EnvStore(db, crypto)
    store.save("TOKEN", "en-claro")
    store.save("TOKEN", "en-claro", secret=True)
    v = store.get("TOKEN")
    assert v.secret is True and v.value is None
    assert store.resolve()["TOKEN"] == "en-claro"


# ── La llave ────────────────────────────────────────────────────────────


def test_la_llave_no_se_crea_hasta_que_hace_falta(db, crypto):
    """
    Una instalación que nunca carga un secreto no deja un archivo de llave
    dando vueltas.
    """
    store = EnvStore(db, crypto)
    store.save("SOLO_UNA_VARIABLE", "x")
    assert crypto.key_exists is False
    store.save("UN_SECRETO", "x", secret=True)
    assert crypto.key_exists is True


def test_la_llave_vive_fuera_de_la_base(db, crypto, tmp_path):
    """
    Es la decisión que hace que un backup de la base no lleve los secretos
    adentro: quien restaure el `.db` en otra máquina no recupera nada.
    """
    store = EnvStore(db, crypto)
    store.save("TOKEN", "x", secret=True)
    assert crypto.key_path.is_file()
    assert crypto.key_path.parent == tmp_path
    # Nada de la llave quedó guardado en la tabla.
    llave = crypto.key_path.read_bytes().decode()
    filas = store.db.query("SELECT value FROM env")
    assert all(llave not in (f["value"] or "") for f in filas)


def test_sin_la_llave_original_el_secreto_no_se_descifra_y_se_dice(db, crypto, tmp_path):
    """
    Un backup de la base sin la llave no restaura los secretos. Se informa como
    ilegible en vez de fingir que está vacío, que mandaría a cargarlo de nuevo
    sin explicar por qué.
    """
    from backend.adapters.crypto_fernet import FernetCryptoAdapter

    store = EnvStore(db, crypto)
    store.save("TOKEN", "tok-abc123", secret=True)

    # Se pierde la llave y se genera otra, como en una máquina nueva.
    crypto.key_path.unlink()
    otro = EnvStore(db, FernetCryptoAdapter(crypto.key_path))

    v = otro.get("TOKEN")
    assert v.has_value is True
    assert v.unreadable is True
    # Y no aparece en resolve: el flujo va a fallar al interpolar, que es un
    # error mucho más legible que una excepción de criptografía.
    assert "TOKEN" not in otro.resolve()


# ── Nombres ─────────────────────────────────────────────────────────────


def test_nombre_invalido_se_rechaza(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    # El punto y la llave romperían la interpolación {env.CLAVE}; el resto
    # no se puede referenciar de ninguna forma.
    for malo in ("con.punto", "con-guion", "1EMPIEZA_CON_NUMERO", "_guion", "", "con espacio", "con}llave"):
        try:
            store.save(malo, "x")
            raise AssertionError(f"debió rechazar {malo!r}")
        except EnvError:
            pass


def test_borrar_dice_si_habia_algo(db, crypto, tmp_path):
    store = EnvStore(db, crypto)
    store.save("X", "1")
    assert store.delete("X") is True
    assert store.delete("X") is False
    assert store.get("X") is None


# ── Referencias ─────────────────────────────────────────────────────────


def test_referencias_encuentra_los_nombres_de_un_texto(db, crypto, tmp_path):
    texto = 'Authorization: Bearer {env.API_TOKEN} y {env.OTRA} y {env.API_TOKEN}'
    assert referencias(texto) == {"API_TOKEN", "OTRA"}


def test_referencias_no_confunde_una_interpolacion_normal(db, crypto, tmp_path):
    """`{caseId}` y `{fila.campo}` no son variables de entorno."""
    assert referencias("{caseId} {fila.campo} {env}") == set()


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
