# Corpus de flujos de prueba

Flujos escritos contra la arquitectura vigente: sólo `core.*` (primitivas del
núcleo), `flow.*` (control de flujo nativo) y `demo.*` (el plugin de prueba que
declaran los tests).

Existen para ejercitar el parser, el serializador y el executor sobre formas
reales —decisiones, reintentos, ramas de error, flujos anidados— y no sobre
ejemplos de tres nodos que no encuentran nada.

Cualquier flujo acá tiene que:

- parsear sin errores,
- sobrevivir un round-trip `parse → to_mermaid → parse`,
- no usar ningún tool de una integración concreta.
