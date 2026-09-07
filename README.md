# bot-core (nombre a definir)

Motor de workflows con trazas. Se escribe un flujo en Mermaid, el motor lo
parsea, lo ejecuta y deja registro de qué pasó en cada nodo.

Esta es la rama `core-export` de `OperacionesMejoras/Bot-Produccion`: un
snapshot de `backend/` pensado para bajarse como ZIP y arrancar con él un repo
nuevo, público, separado del historial de `Bot-Produccion` (que tiene tokens
viejos en commits pasados). Bajar esta rama como ZIP no trae ese historial —
sólo los archivos tal como están acá.

## Estructura del repo

```
backend/    el núcleo completo: motor, contrato, ports, adapters, tests y docs
```

**Todo el backend vive bajo `backend/`**, y no queda nada suyo en la raíz. Es
deliberado: traer el backend a otra rama —o a otro repo— es traer una
carpeta, sin archivos sueltos en la raíz que resolver a mano en cada merge.

```bash
python -m backend.core doctor      # ¿levanta todo?
python -m backend.mcp              # servidor MCP de autoría (stdio)
pytest backend/tests -q
```

La documentación está en [backend/README.md](backend/README.md) y la
arquitectura en [backend/docs/ARQUITECTURA.md](backend/docs/ARQUITECTURA.md).

## Flujo de ramas

- **`main`** — rama estable. Sólo recibe cambios vía PR desde `develop`.
- **`develop`** — rama de trabajo. Todos los cambios en curso se hacen acá
  directamente, sin abrir una rama nueva por tarea.

Cuando algo en `develop` está listo para quedar estable, se abre un PR
`develop → main`. Los releases (tags `vX.Y.Z`) se cortan desde `main`.

## Por qué este repo va a existir separado

Separar el kernel de sus consumidores (la webapp, el installer, lo que venga)
es la idea de fondo: que este código tenga su propio versionado y release, y
que los demás repos lo *importen* pineado a una versión, no que lo forkeen ni
le copien código a mano.

## Estado heredado (qué ya funciona)

- **Cero dependencias obligatorias**: todo corre con stdlib de Python 3.10+.
  `cryptography` (secretos) y `mcp` (servidor de autoría) son opcionales y
  están declaradas así en `backend/requirements.txt` — el núcleo arranca sin
  ellas, sólo pierde esas dos features puntuales.
- **Versión de contrato ya existe**: `CONTRACT_VERSION = 1` en
  `backend/core/contract.py`. Es el número que un plugin declara para decir
  contra qué forma del contrato fue escrito, y `SUPPORTED_CONTRACTS` en
  `registry.py` es lo que el núcleo acepta. Es el gancho natural para el
  versionado semántico de este repo (ver más abajo).
- **Arquitectura ya pensada como capas separables**: `core/` no importa nada
  de `adapters/` ni de `mcp/` a nivel de módulo (hay tests de arquitectura,
  `backend/tests/test_arquitectura.py`, que lo hacen cumplir). Eso es lo que
  hace viable empaquetar `backend/` solo, sin arrastrar nada de encima.
- Incluye los últimos fixes de la rama `core` de origen (todavía en PR al
  momento de este export, no mergeados a `core` pero sí probados):
  - `boot.py:validar()` detecta que `plugins_dir` y `fs_root` se solapen
    (issue #9 de `Bot-Produccion`).
  - `backend/mcp`: tools `install_plugin` y `save_flow`, que cierran el loop
    de autoría sin forzar una ejecución real sólo para persistir algo
    (issue #10 de `Bot-Produccion`).

No se trajo `installer/` ni `web-app/` (viven en
`claude/user-experience-connections-ca9q4v` en `Bot-Produccion`, todavía en
desarrollo). Esos son consumidores del kernel, no parte de él — si en algún
momento se separan también, van a otro repo cada uno, no a este.

## Intención: cómo se libera este kernel

**Fase 1 — empaquetar y taguear, sin infraestructura nueva.**

1. ✅ `pyproject.toml` en la raíz, distribución `bot-core` con `backend/` como
   el paquete instalable (`pip install -e .`, o con extras:
   `pip install -e ".[crypto,mcp]"`). El módulo interno sigue llamándose
   `backend` por ahora — renombrarlo es un cambio de imports en todo el
   código y los ~500 tests, no algo para hacer de paso.
2. ✅ Console scripts: `bot-core` (CLI, antes `python -m backend.core`) y
   `bot-core-mcp` (servidor MCP, antes `python -m backend.mcp`). Las
   invocaciones `python -m` se siguen pudiendo usar igual.
3. Política de versión atada a `CONTRACT_VERSION`:
   - Bump de `CONTRACT_VERSION` (rompe plugins existentes) → major.
   - Tool/port/feature nueva sin romper nada → minor.
   - Fixes como #9/#10 → patch.
4. CI en GitHub Actions: correr la suite en cada push/PR, y en cada tag
   `vX.Y.Z` armar el paquete y publicar un GitHub Release (changelog +
   artefacto). Hoy no hay ningún workflow — se arranca de cero.
5. Los consumidores pinean versión. Sin necesidad de un índice de paquetes
   propio todavía: `pip install git+https://github.com/<org>/<este-repo>@vX.Y.Z`
   ya alcanza. Un índice (GitHub Packages, PyPI privado) es una mejora
   posterior, no un bloqueante para arrancar.

**Fase 2 — recién si hace falta.** Si en algún momento hay ≥2 consumidores
reales con cadencias de release independientes y este esquema por tags se
queda corto, ahí se evalúa un índice de paquetes propio o infraestructura de
distribución más seria. No hay que resolverlo antes de tener el problema.

## Qué NO hacer

- No forkear este repo para la webapp ni para ningún consumidor: un fork
  duplica el código y en dos releases diverge en silencio. Un consumidor
  declara una dependencia pineada a una versión de este repo, punto.
- No mezclar código de UI/instalador acá adentro. Si algo empieza a
  necesitar saber de dónde corre (webapp vs CLI vs otra app), es una señal de
  que se está filtrando lógica de un consumidor hacia el kernel.

## Checklist para quien retome esto

- [ ] Confirmar nombre definitivo del paquete/repo (hoy este README usa
      "bot-core" como placeholder).
- [x] `pyproject.toml` + empaquetado de `backend/` (paquete `bot-core`,
      instalable con `pip install -e .`; ver Fase 1 más abajo).
- [x] Definir versión inicial: arranca en `0.1.0` (el empaquetado con
      `pyproject.toml` todavía no existe; subir a `1.0.0` queda para cuando
      esa fase esté cerrada).
- [ ] Workflow de CI: test + build + release por tag.
- [ ] Decidir si este repo queda público desde el día uno o arranca privado
      hasta el primer release estable.
- [ ] Revisar `backend/requirements.txt` y confirmar que sigue reflejando
      "cero dependencias obligatorias" tal cual quedó documentado ahí.
- [ ] Cuando la webapp/installer se separen de Bot-Produccion, migrarlos para
      que consuman este repo por versión, no por copia de código.
