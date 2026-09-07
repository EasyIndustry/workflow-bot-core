"""
`CryptoPort` sobre Fernet (`cryptography`).

Fernet y no un cifrado propio: criptografía escrita a mano es la peor clase de
código propio. Fernet es AES-128-CBC con HMAC-SHA256 y timestamp, en una API que
no se puede usar mal por accidente.

**La llave vive fuera de la base**, en un archivo al lado. Es la decisión que
hace que un backup de la base no lleve los secretos adentro: quien restaure el
`.db` en otra máquina no recupera nada, y esa es la idea. Como no se pueden
leer, tampoco se pueden filtrar por ahí.

Lo que esto **no** protege: a alguien con acceso al disco entero, ni a alguien
que llegue al proceso corriendo. Ese es el límite y conviene tenerlo claro.

Se genera sola la primera vez que hace falta, no al arrancar, así que una
instalación que nunca carga un secreto no deja ningún archivo de llave dando
vueltas.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.core.ports import PortError


class FernetCryptoAdapter:
    """Cifrado simétrico con la llave en un archivo local."""

    def __init__(self, key_path: str | Path) -> None:
        self.key_path = Path(key_path)
        self._fernet = None

    @property
    def available(self) -> bool:
        """
        Si `cryptography` se puede usar en esta máquina. El diagnóstico lo
        consulta para avisar antes de que alguien intente guardar un secreto.

        Se ataja `BaseException` y no sólo `ImportError` porque una instalación
        **rota** —la librería presente pero sin su backend nativo— no falla con
        un `ImportError`: el binding levanta un `PanicException`, que cuelga de
        `BaseException` y se escaparía. Esta property existe para contestar sí
        o no; si además pudiera tumbar al que pregunta, el diagnóstico se caería
        exactamente en la máquina que tiene el problema que viene a reportar.
        """
        try:
            import cryptography.fernet  # noqa: F401
        except BaseException:
            return False
        return True

    @property
    def key_exists(self) -> bool:
        return self.key_path.is_file()

    def _cifrador(self):
        if self._fernet is not None:
            return self._fernet

        try:
            from cryptography.fernet import Fernet
        except BaseException as exc:  # pragma: no cover - depende del entorno
            # Igual que en `available`: falta o está rota, y las dos se ven
            # desde acá como "no se va a poder cifrar". Que suba como PortError
            # y no como un panic del binding es lo que hace que el run lo
            # reporte en vez de morirse sin decir qué pasó.
            raise PortError(
                "No se puede usar el paquete `cryptography`, que es lo que "
                f"cifra los secretos ({type(exc).__name__}: {exc}). Revisá la "
                "instalación de las dependencias antes de cargar uno."
            ) from exc

        if self.key_path.is_file():
            llave = self.key_path.read_bytes().strip()
        else:
            llave = Fernet.generate_key()
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(llave)
            # Sólo el dueño. En Windows no hace nada, y ahí el permiso lo da la
            # ACL de la carpeta: no hay forma portable de hacer más.
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass

        try:
            self._fernet = Fernet(llave)
        except (ValueError, TypeError) as exc:
            raise PortError(
                f"La llave de {self.key_path} no es válida. Si se editó a mano, "
                "restaurá el archivo original: sin esa llave los secretos "
                "guardados no se pueden descifrar."
            ) from exc
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        return self._cifrador().encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._cifrador().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except PortError:
            raise
        except Exception as exc:
            # Un token cifrado con otra llave, o corrupto. No se distingue de
            # uno alterado a propósito, y no debería: los dos son "no confío".
            raise PortError("no se pudo descifrar el valor con esta llave") from exc


__all__ = ["FernetCryptoAdapter"]
