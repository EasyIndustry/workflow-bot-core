"""
`ClockPort` sobre `time`.

Parece trivial —tres líneas envolviendo la stdlib— y no lo es: es lo que hace
que un flujo con reintentos se pueda testear. El flujo de referencia esperaba
60s por intento con hasta 50 intentos; con `time.sleep` incrustado, ese test
tarda 50 minutos y nadie lo corre. Con el reloj inyectado, tarda milisegundos.

`sleep` es interrumpible: recibe un `is_cancelled` y despierta a chequearlo en
tramos cortos. Sin eso, cancelar un run que está esperando 60s no hace nada
hasta que la espera termina, y desde afuera se ve como que el botón no funciona.
"""

from __future__ import annotations

import time
from typing import Callable

# Cada cuánto despierta una espera larga a mirar si la cancelaron.
_GRANO = 0.25


class SystemClockAdapter:
    """El reloj real de la máquina."""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float, is_cancelled: Callable[[], bool] | None = None) -> None:
        """
        Duerme `seconds`. Con `is_cancelled`, vuelve antes si pasa a True.

        Devuelve normalmente en los dos casos: quién llamó decide si una espera
        cortada es un error. El port no ramifica por nadie.
        """
        if seconds <= 0:
            return
        if is_cancelled is None:
            time.sleep(seconds)
            return

        fin = time.monotonic() + seconds
        while (restante := fin - time.monotonic()) > 0:
            if is_cancelled():
                return
            time.sleep(min(_GRANO, restante))


__all__ = ["SystemClockAdapter"]
