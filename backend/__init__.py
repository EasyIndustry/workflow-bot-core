"""
El backend: motor de workflows, contrato de plugins y adapters.

Todo el núcleo vive bajo este paquete y nada de lo suyo queda en la raíz del
repo. Es deliberado: hace que traer el backend a otra rama sea traer **una
carpeta**, sin ir a buscar archivos sueltos ni resolver conflictos en la raíz.

    backend/
      core/        motor, contrato, ports, stores, CLI
      adapters/    la única capa que importa una librería externa
      tests/       suite completa, con fakes de cada port
      docs/        arquitectura

Se usa como paquete desde la raíz del repo:

    python -m backend.core doctor
    pytest backend/tests
"""

__all__ = []
