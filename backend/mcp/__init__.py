"""
Servidor MCP de autoría.

Expone el motor a un agente para **escribir** flujos y plugins: descubrir qué
tools existen, validar un `.mmd`, generar y verificar un plugin, y recorrer un
flujo en seco. No es una interfaz de operación — no ejecuta flujos de verdad.

Es un adapter *de entrada*: el mundo llamando al motor, igual que la CLI. Los
adapters de `backend/adapters/` son la dirección contraria, el motor saliendo al
mundo. Por eso este paquete vive afuera de `core/` y no adentro: necesita el SDK
de MCP, y el núcleo no tiene dependencias.

    backend/mcp/
      operations.py   la lógica, sin nada de MCP. Testeable sin el SDK.
      server.py       sólo cableado de protocolo.

Se arranca con `python -m backend.mcp`, hablando por stdio.
"""

from .operations import OperationError

__all__ = ["OperationError"]
