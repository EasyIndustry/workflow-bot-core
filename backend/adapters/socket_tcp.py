"""
`SocketPort` sobre `socket`/`ssl` de la stdlib.

Mismo criterio que `UrllibHttpAdapter`: ninguna librería curada, sólo lo que
trae Python. No sabe de Postgres, de Redis, ni de ningún protocolo de
aplicación -- eso es trabajo del plugin que use este port (issue #39, ver el
docstring de `SocketPort`).
"""

from __future__ import annotations

import socket
import ssl
import threading
import uuid

from backend.core.ports import PortError, SocketConnection

DEFAULT_TIMEOUT = 30.0


class TcpSocketAdapter:
    """
    Una conexión TCP por handle.

    El socket real nunca sale del adapter -- `connect()` devuelve un
    `SocketConnection` opaco y el adapter lo resuelve internamente, mismo
    patrón que `WindowPort` con `WindowInfo`.
    """

    def __init__(self, default_timeout: float = DEFAULT_TIMEOUT) -> None:
        self.default_timeout = default_timeout
        self._lock = threading.Lock()
        self._sockets: dict[str, socket.socket] = {}

    def connect(
        self, host: str, port: int, *, tls: bool = False, timeout: float | None = None
    ) -> SocketConnection:
        host = (host or "").strip()
        if not host:
            raise PortError("no se indicó un host")

        espera = self.default_timeout if timeout is None else timeout
        try:
            crudo = socket.create_connection((host, port), timeout=espera)
            if tls:
                contexto = ssl.create_default_context()
                crudo = contexto.wrap_socket(crudo, server_hostname=host)
        except (OSError, ssl.SSLError) as exc:
            raise PortError(f"no se pudo conectar a {host}:{port}: {exc}") from exc

        handle = uuid.uuid4().hex
        with self._lock:
            self._sockets[handle] = crudo
        return SocketConnection(handle=handle)

    def _socket(self, conn: SocketConnection) -> socket.socket:
        crudo = self._sockets.get(conn.handle)
        if crudo is None:
            raise PortError(f"conexión ya cerrada o inexistente: {conn.handle}")
        return crudo

    def send(self, conn: SocketConnection, data: bytes) -> None:
        crudo = self._socket(conn)
        try:
            crudo.sendall(data)
        except OSError as exc:
            raise PortError(f"no se pudo enviar: {exc}") from exc

    def recv(self, conn: SocketConnection, size: int, *, timeout: float | None = None) -> bytes:
        crudo = self._socket(conn)
        if timeout is not None:
            crudo.settimeout(timeout)
        try:
            return crudo.recv(size)
        except socket.timeout as exc:
            raise PortError(f"timeout esperando hasta {size} bytes") from exc
        except OSError as exc:
            raise PortError(f"no se pudo recibir: {exc}") from exc

    def close(self, conn: SocketConnection | None = None) -> None:
        """
        Cierra `conn`. Sin argumento, cierra todas las conexiones abiertas --
        es lo que usa `Instance.close()` al apagar, que llama `close()` de
        cada adapter sin saber cuál es cuál (ver `core/instance.py`). El port
        siempre llama con `conn`; el modo "cerrar todo" es un detalle de este
        adapter, no parte del contrato que ve un plugin.
        """
        with self._lock:
            if conn is None:
                crudos = list(self._sockets.values())
                self._sockets.clear()
            else:
                crudo = self._sockets.pop(conn.handle, None)
                crudos = [crudo] if crudo is not None else []
        for crudo in crudos:
            crudo.close()


__all__ = ["DEFAULT_TIMEOUT", "TcpSocketAdapter"]
