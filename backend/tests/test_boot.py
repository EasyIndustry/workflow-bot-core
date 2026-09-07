"""
Tests de la configuración de arranque.

La propiedad que sostiene toda la capa: **no puede vivir en la base, porque
hace falta para abrirla**. Dónde está el archivo de datos es, en sí mismo, uno
de estos valores.
"""

from __future__ import annotations

from backend.core import boot


def _escribir(tmp_path, contenido: str):
    (tmp_path / boot.ARCHIVO).write_text(contenido, encoding="utf-8")


# ── Sin nada ────────────────────────────────────────────────────────────


def test_sin_archivo_ni_entorno_arranca_igual(tmp_path):
    """
    Ninguna de las dos fuentes es obligatoria: una instalación recién hecha
    tiene que levantar sin que nadie configure nada.
    """
    c = boot.load(tmp_path, entorno={})
    assert c.storage_path == str(tmp_path / "data" / "bot.db")
    assert c.plugins_dir is None
    assert c.fs_root is None
    assert c.default_actor == "local"
    assert c.efimera is False


# ── El archivo ──────────────────────────────────────────────────────────


def test_lee_el_archivo_y_saltea_comentarios(tmp_path):
    _escribir(tmp_path, """
        # esto es un comentario
        fs_root=/datos

        default_actor=operador
    """.replace("        ", ""))
    c = boot.load(tmp_path, entorno={})
    assert c.fs_root == "/datos"
    assert c.default_actor == "operador"


def test_una_ruta_relativa_se_resuelve_contra_la_raiz(tmp_path):
    _escribir(tmp_path, "plugins_dir=plugins\n")
    assert boot.load(tmp_path, entorno={}).plugins_dir == tmp_path / "plugins"


def test_la_allowlist_se_parte_por_comas(tmp_path):
    _escribir(tmp_path, "process_allowlist=python3, robocopy ,  7z\n")
    assert boot.load(tmp_path, entorno={}).process_allowlist == ("python3", "robocopy", "7z")


def test_un_valor_vacio_es_como_no_estar(tmp_path):
    _escribir(tmp_path, "fs_root=\n")
    assert boot.load(tmp_path, entorno={}).fs_root is None


def test_la_allowlist_vacia_significa_ningun_ejecutable(tmp_path):
    """
    La excepción a la regla de arriba, y a propósito.

    Para el resto de las claves, vacío es "no lo declaré". Para la allowlist la
    lista vacía es la configuración **más restrictiva** que se puede pedir, y
    tratarla como ausente la convertiría en la más permisiva —cualquier
    ejecutable de la máquina— justo en la instalación que quiso lo contrario.
    """
    _escribir(tmp_path, "process_allowlist=\n")
    assert boot.load(tmp_path, entorno={}).process_allowlist == ()

    # Y la clave ausente sigue siendo "cualquiera": no se rompe hacia atrás.
    _escribir(tmp_path, "fs_root=/datos\n")
    assert boot.load(tmp_path, entorno={}).process_allowlist is None


def test_la_allowlist_vacia_sobrevive_a_un_init(tmp_path):
    """Si `render` la comentara, un `init` devolvería un archivo más permisivo."""
    acotada = boot.load(tmp_path, entorno={f"{boot.PREFIJO}process_allowlist": ""})
    _escribir(tmp_path, boot.render(acotada))
    assert boot.load(tmp_path, entorno={}).process_allowlist == ()


# ── Rutas ───────────────────────────────────────────────────────────────


def test_fs_root_relativo_se_resuelve_contra_la_raiz_y_no_contra_el_cwd(tmp_path):
    """
    `fs_root` es el límite de seguridad de la instalación, no una comodidad.

    Resuelto contra el directorio desde el que se arrancó, el mismo `boot.env`
    daría una caja distinta según quién lo levante — y un cliente que escribe
    `datos` obtendría una instalación aparentemente correcta en una carpeta que
    no eligió.
    """
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}fs_root": "datos"})
    assert c.fs_root == str(tmp_path / "datos")


def test_fs_root_absoluto_se_respeta(tmp_path):
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}fs_root": "/datos"})
    assert c.fs_root == "/datos"


# ── El entorno pisa ─────────────────────────────────────────────────────


def test_el_entorno_le_gana_al_archivo(tmp_path):
    """
    Para que un contenedor o un CI arranquen sin gestionar ningún archivo.
    """
    _escribir(tmp_path, "fs_root=/del-archivo\n")
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}fs_root": "/del-entorno"})
    assert c.fs_root == "/del-entorno"


def test_registra_de_donde_salio_cada_valor(tmp_path):
    """
    Sin esto, diagnosticar "por qué está usando esa carpeta" es adivinar entre
    tres fuentes.
    """
    _escribir(tmp_path, "fs_root=/archivo\nplugins_dir=p\n")
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}fs_root": "/entorno"})
    assert c.origen["fs_root"] == f"{boot.PREFIJO}fs_root"
    assert c.origen["plugins_dir"].endswith(boot.ARCHIVO)


def test_una_variable_con_el_prefijo_pelado_se_ignora(tmp_path):
    assert boot.load(tmp_path, entorno={boot.PREFIJO: "x"}).fs_root is None


# ── Instalación efímera ─────────────────────────────────────────────────


def test_la_base_en_memoria_se_declara_desde_el_arranque(tmp_path):
    """
    Corre de verdad y no deja rastro: es lo que hace falta para que un agente
    pruebe algo sin escribir en la base de nadie.
    """
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}storage": boot.EN_MEMORIA})
    assert c.efimera is True
    assert c.storage_path == boot.EN_MEMORIA


# ── Claves desconocidas ─────────────────────────────────────────────────


def test_una_clave_mal_escrita_se_reporta(tmp_path):
    """
    El motivo de que este prefijo exista. Una clave que no hace nada y no avisa
    es indistinguible de una que funciona — y es el modo de falla más caro de
    diagnosticar.
    """
    _escribir(tmp_path, "pluginDir=/x\nfs_root=/ok\n")
    desconocidas = boot.desconocidas(tmp_path, entorno={})
    assert any("pluginDir" in d for d in desconocidas)
    # Y la que sí existe no se reporta.
    assert not any("fs_root" in d for d in desconocidas)


def test_una_variable_de_entorno_mal_escrita_tambien(tmp_path):
    desconocidas = boot.desconocidas(
        tmp_path, entorno={f"{boot.PREFIJO}plugin_dir": "/x"}
    )
    assert desconocidas == [f"{boot.PREFIJO}plugin_dir"]


def test_el_prefijo_no_se_confunde_con_los_otros_dos(tmp_path):
    """
    `BOT_` pisa un setting y `BOTENV_` es una variable de flujo. Si el prefijo
    de arranque fuera `BOOT_`, un typo de una letra caería en otro namespace y
    se guardaría en silencio como algo que nadie consulta.
    """
    assert boot.PREFIJO == "BOOTSTRAP_"
    assert not boot.PREFIJO.startswith("BOT_")
    c = boot.load(tmp_path, entorno={"BOT_fs_root": "/x", "BOTENV_fs_root": "/y"})
    assert c.fs_root is None


# ── init ────────────────────────────────────────────────────────────────


def test_render_produce_un_archivo_que_se_puede_volver_a_leer(tmp_path):
    original = boot.load(
        tmp_path,
        entorno={
            f"{boot.PREFIJO}fs_root": "/datos",
            f"{boot.PREFIJO}process_allowlist": "python3,7z",
            f"{boot.PREFIJO}default_actor": "operador",
        },
    )
    _escribir(tmp_path, boot.render(original))

    releido = boot.load(tmp_path, entorno={})
    assert releido.fs_root == original.fs_root
    assert releido.process_allowlist == original.process_allowlist
    assert releido.default_actor == original.default_actor


def test_lo_renderizado_no_tiene_claves_desconocidas(tmp_path):
    _escribir(tmp_path, boot.render(boot.load(tmp_path, entorno={})))
    assert boot.desconocidas(tmp_path, entorno={}) == []


# ── Valores que no hacen lo que dicen ───────────────────────────────────


def test_un_timeout_que_no_es_un_numero_se_reporta(tmp_path):
    """
    El mismo modo de falla que `desconocidas()` evita para el nombre de la
    clave, un escalón más abajo: `http_timeout=treinta` no falla, no avisa y
    deja el timeout por defecto.
    """
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}http_timeout": "treinta"})
    assert c.http_timeout is None
    assert any("http_timeout" in p and "treinta" in p for p in boot.validar(c))


def test_un_timeout_negativo_tampoco_es_un_tiempo_de_espera(tmp_path):
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}http_timeout": "-5"})
    assert any("http_timeout" in p for p in boot.validar(c))


def test_una_carpeta_que_no_existe_se_reporta(tmp_path):
    """
    Un `plugins_dir` inexistente hace que no cargue ningún plugin, y el síntoma
    —"me falta un tool"— aparece lejos de la causa.
    """
    c = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}plugins_dir": "/no/existe",
        f"{boot.PREFIJO}fs_root": "/tampoco",
    })
    problemas = boot.validar(c)
    assert any("plugins_dir" in p for p in problemas)
    assert any("fs_root" in p for p in problemas)


def test_una_carpeta_que_es_un_archivo_no_pasa(tmp_path):
    archivo = tmp_path / "no-soy-carpeta"
    archivo.write_text("x", encoding="utf-8")
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}plugins_dir": str(archivo)})
    assert any("no es una carpeta" in p for p in boot.validar(c))


def test_plugins_dir_igual_a_fs_root_se_reporta(tmp_path):
    """
    Si un flujo con el port `fs` pudiera escribir donde vive el código que se
    carga, un flujo comprometido dejaría un plugin listo para el próximo
    arranque — el mismo agujero que la separación de carpetas existe para
    cerrar.
    """
    compartida = tmp_path / "compartida"
    compartida.mkdir()
    c = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}plugins_dir": str(compartida),
        f"{boot.PREFIJO}fs_root": str(compartida),
    })
    assert any("se solapan" in p for p in boot.validar(c))


def test_plugins_dir_anidado_dentro_de_fs_root_se_reporta(tmp_path):
    fs_root = tmp_path / "workspace"
    plugins_dir = fs_root / "plugins"
    plugins_dir.mkdir(parents=True)
    c = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}plugins_dir": str(plugins_dir),
        f"{boot.PREFIJO}fs_root": str(fs_root),
    })
    assert any("se solapan" in p for p in boot.validar(c))


def test_fs_root_anidado_dentro_de_plugins_dir_se_reporta(tmp_path):
    """
    Importa igual en la otra dirección: si `fs_root` quedara dentro de
    `plugins_dir`, un flujo confinado a `fs_root` alcanzaría el resto de
    `plugins_dir` por ser su padre.
    """
    plugins_dir = tmp_path / "plugins"
    fs_root = plugins_dir / "workspace"
    fs_root.mkdir(parents=True)
    c = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}plugins_dir": str(plugins_dir),
        f"{boot.PREFIJO}fs_root": str(fs_root),
    })
    assert any("se solapan" in p for p in boot.validar(c))


def test_plugins_dir_y_fs_root_hermanos_no_reportan_nada(tmp_path):
    """El caso normal de la instalación: las dos carpetas, una al lado de la otra."""
    fs_root = tmp_path / "workspace"
    plugins_dir = tmp_path / "plugins"
    fs_root.mkdir()
    plugins_dir.mkdir()
    c = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}plugins_dir": str(plugins_dir),
        f"{boot.PREFIJO}fs_root": str(fs_root),
    })
    assert boot.validar(c) == []


def test_sin_uno_de_los_dos_no_hay_nada_que_comparar(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}plugins_dir": str(plugins_dir)})
    assert not any("se solapan" in p for p in boot.validar(c))


def test_un_ejecutable_de_la_allowlist_que_no_esta_se_reporta(tmp_path):
    c = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}process_allowlist": "no-existe-este-programa",
    })
    assert any("process_allowlist" in p for p in boot.validar(c))


def test_una_instalacion_por_defecto_no_tiene_nada_que_reportar(tmp_path):
    """Lo normal es que no haya nada que decir; si no, el aviso pierde valor."""
    assert boot.validar(boot.load(tmp_path, entorno={})) == []


def test_validar_no_toca_nada(tmp_path):
    """
    Reporta, no arregla: es lo que le permite a un instalador revisar los
    valores **antes** de escribir nada.
    """
    c = boot.load(tmp_path, entorno={})
    boot.validar(c)
    assert not c.data_dir.exists()


def test_una_base_en_memoria_no_pide_carpeta_escribible(tmp_path):
    c = boot.load(tmp_path, entorno={f"{boot.PREFIJO}storage": boot.EN_MEMORIA})
    assert boot.validar(c) == []


# ── Codificación del archivo ────────────────────────────────────────────
#
# `boot.env` se edita a mano, y en Windows —el destino del instalador— las
# herramientas más obvias dejan un BOM sin preguntar.


def test_un_bom_no_se_come_la_primera_clave(tmp_path):
    """
    Pegado al nombre, el BOM hacía que la clave no se reconociera y se
    descartara en silencio.

    Si la que se pierde es `fs_root`, la instalación pasa de estar acotada a un
    subárbol a poder tocar todo el disco, y no hay cómo notarlo: para el núcleo
    la clave simplemente no está, que es una configuración válida. Un límite de
    seguridad no puede desaparecer por un carácter invisible.
    """
    (tmp_path / boot.ARCHIVO).write_bytes(b"\xef\xbb\xbffs_root=/datos\n")
    assert boot.load(tmp_path, entorno={}).fs_root == "/datos"
    # Y tampoco queda reportada como una clave rara: ya no lo es.
    assert boot.desconocidas(tmp_path, entorno={}) == []


def test_un_archivo_utf16_no_tumba_el_arranque(tmp_path):
    """
    Lo que deja una redirección `>` en PowerShell 5.1, que es la forma más
    corriente de escribir un archivo desde un script en Windows.

    Antes explotaba con un `UnicodeDecodeError` sin atajar. Y como `load()`
    corre antes que todo, ni `doctor` llegaba a arrancar para decir qué pasó.
    """
    (tmp_path / boot.ARCHIVO).write_bytes(
        b"\xff\xfe" + "fs_root=/datos\n".encode("utf-16-le")
    )
    assert boot.load(tmp_path, entorno={}).fs_root == "/datos"


def test_una_codificacion_ilegible_no_tumba_el_arranque(tmp_path):
    """
    No se puede adivinar qué es, pero las claves son ASCII y sobreviven: la
    instalación arranca y `validar()` levanta el valor que salió mangleado.
    Morir acá dejaría al cliente sin diagnóstico y sin pista.
    """
    (tmp_path / boot.ARCHIVO).write_bytes("fs_root=/datos/ñ\n".encode("cp1252"))
    config = boot.load(tmp_path, entorno={})
    assert config.fs_root is not None
    assert any("fs_root" in p for p in boot.validar(config))


def test_lo_que_escribe_init_se_vuelve_a_leer_igual(tmp_path):
    """
    `init` escribe con BOM para que Notepad y PowerShell no rompan los acentos.
    Lo que no puede pasar es que el archivo que el propio comando genera no se
    relea igual — que es justo el bug, en la otra punta.
    """
    original = boot.load(tmp_path, entorno={
        f"{boot.PREFIJO}fs_root": "/datos",
        f"{boot.PREFIJO}process_allowlist": "",
    })
    destino = tmp_path / boot.ARCHIVO
    destino.write_text(boot.render(original), encoding="utf-8-sig")

    releido = boot.load(tmp_path, entorno={})
    assert releido.fs_root == original.fs_root
    assert releido.process_allowlist == ()
    assert boot.desconocidas(tmp_path, entorno={}) == []
