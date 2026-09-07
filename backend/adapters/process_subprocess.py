"""
`ProcessPort` sobre `subprocess`.

Es la superficie de riesgo más alta del sistema —ejecutar comandos arbitrarios—
y por eso está acotada acá:

- **Nunca `shell=True`.** El comando es una secuencia de argumentos. Armar la
  línea a mano y dejar que el shell la parsee es cómo un `{id_externo}` con un
  `;` adentro se convierte en ejecución arbitraria.
- **`allowlist` opcional.** Si se configura, sólo esos ejecutables se pueden
  correr. Una instalación que sabe qué necesita disparar no tiene por qué poder
  disparar cualquier otra cosa. `None` es "cualquiera"; la lista **vacía** es
  "ninguno", que no es lo mismo y es la configuración de una instalación
  acotada que no ejecuta nada del sistema.
- **Timeout siempre.** Un comando sin tope cuelga el run para siempre; un run
  colgado no se distingue de uno lento.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from backend.core.ports import PortError, ProcessResult

DEFAULT_TIMEOUT = 300.0

# La salida completa de un comando charlatán no aporta y llena la traza.
MAX_OUTPUT_CHARS = 64 * 1024


class SubprocessAdapter:
    """Corre comandos del sistema y espera a que terminen."""

    def __init__(
        self,
        *,
        default_timeout: float = DEFAULT_TIMEOUT,
        allowlist: Sequence[str] | None = None,
    ) -> None:
        self.default_timeout = default_timeout
        # Se compara por el nombre del ejecutable, sin ruta ni extensión: lo que
        # se quiere permitir es "el programa X", no "esa ruta exacta".
        #
        # `is not None` y no un truthy: una lista vacía es la configuración más
        # restrictiva que se puede pedir —ningún ejecutable— y tratarla como
        # ausente la convertiría en la más permisiva, en silencio.
        self.allowlist = (
            {Path(c).stem.lower() for c in allowlist} if allowlist is not None else None
        )

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        argv = [str(a) for a in (command or []) if str(a) != ""]
        if not argv:
            raise PortError("no se indicó ningún comando")

        if self.allowlist is not None and Path(argv[0]).stem.lower() not in self.allowlist:
            permitidos = ", ".join(sorted(self.allowlist)) or "ninguno"
            raise PortError(
                f"'{argv[0]}' no está en la lista de comandos permitidos. Permitidos: {permitidos}"
            )

        if cwd and not Path(cwd).is_dir():
            raise PortError(f"el directorio de trabajo no existe: {cwd}")

        espera = self.default_timeout if timeout is None else timeout

        try:
            completado = subprocess.run(  # noqa: S603 — argv, nunca shell
                argv,
                cwd=cwd or None,
                env=env,
                timeout=espera,
                capture_output=True,
                text=True,
                errors="replace",
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            # El timeout **sí** vuelve como resultado y no como PortError: el
            # comando arrancó, y "tardó demasiado" es una condición por la que
            # un flujo legítimamente quiere ramificar.
            return ProcessResult(
                exit_code=-1,
                stdout=_recortar(exc.stdout),
                stderr=_recortar(exc.stderr),
                timed_out=True,
                command=tuple(argv),
            )
        except FileNotFoundError as exc:
            raise PortError(f"no se encontró el ejecutable: {argv[0]}") from exc
        except OSError as exc:
            raise PortError(f"no se pudo ejecutar {argv[0]}: {exc}") from exc

        return ProcessResult(
            exit_code=completado.returncode,
            stdout=_recortar(completado.stdout),
            stderr=_recortar(completado.stderr),
            command=tuple(argv),
        )


def _recortar(salida) -> str:
    if not salida:
        return ""
    texto = salida if isinstance(salida, str) else salida.decode("utf-8", errors="replace")
    if len(texto) <= MAX_OUTPUT_CHARS:
        return texto
    return texto[:MAX_OUTPUT_CHARS] + f"\n… ({len(texto) - MAX_OUTPUT_CHARS} caracteres más)"


__all__ = ["DEFAULT_TIMEOUT", "SubprocessAdapter"]
