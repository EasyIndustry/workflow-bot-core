# bot-core (nombre a definir)

Motor de workflows con trazas. Se escribe un flujo en Mermaid, el motor lo
parsea, lo ejecuta y deja registro de qué pasó en cada nodo.

## Estructura del repo

```
backend/    el núcleo completo: motor, contrato, ports, adapters, tests y docs
```

**Todo el backend vive bajo `backend/`**, y no queda nada suyo en la raíz. Es
deliberado: traer el backend a otra rama —o a otro repo— es traer una
carpeta, sin archivos sueltos en la raíz que resolver a mano en cada merge.

```bash
pip install -e .                   # instala bot-core (extras: [crypto,mcp,dev])
bot-core doctor                    # ¿levanta todo?
bot-core-mcp                       # servidor MCP de autoría (stdio)
pytest backend/tests -q
```

La documentación está en [backend/README.md](backend/README.md) y la
arquitectura en [backend/docs/ARQUITECTURA.md](backend/docs/ARQUITECTURA.md).

## Flujo de ramas

- **`main`** — rama estable. Sólo recibe cambios vía PR desde `develop`.
- **`develop`** — rama de trabajo. Todos los cambios en curso se hacen acá
  directamente, sin abrir una rama nueva por tarea.

Cuando algo en `develop` está listo para quedar estable, se abre un PR
`develop → main`. Los releases (tags `vX.Y.Z`) se cortan desde `main`, y
`.github/workflows/release.yml` arma el paquete y publica el GitHub Release
solo con pushear el tag.

## Qué se puede hacer con esto

- **Escribir un flujo en Mermaid** y ejecutarlo con trazas: cada nodo queda
  registrado, con reintentos, ramas condicionales y sub-flujos anidados.
- **Escribir plugins** que declaran qué *ports* necesitan (`http`, `fs`,
  `process`, `clock`, `storage`, `crypto`) sin importar ninguna librería
  externa directamente — el núcleo inyecta el *adapter* concreto.
- **Actores con permisos**: cada ejecución queda atribuida a un actor
  (`human`, `agent`, `schedule`, `system`), y el actor decide qué tools y
  qué ports puede usar. Un flujo vedado falla antes de ejecutar nada.
- **Servidor MCP de autoría**: un agente puede escribir flujos y plugins
  contra el catálogo real del núcleo (`list_tools`, `check_flow`,
  `dry_run_flow`, `plugin_template`, etc.), sin necesidad de ejecutar nada
  de verdad hasta que el flujo esté listo.
- **Configuración en tres capas** (arranque, settings de plugin, secretos),
  cada una con su propia forma de resolverse y de reportar errores.
- **Cero dependencias obligatorias**: todo corre con la stdlib de Python
  3.10+. `cryptography` (secretos) y `mcp` (servidor de autoría) son
  opcionales.

El detalle completo de cada uno de estos puntos está en
[backend/README.md](backend/README.md).
