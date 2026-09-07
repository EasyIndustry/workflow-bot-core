"""
`HttpPort` sobre `urllib`.

`urllib` y no `requests` por una sola razón: viene en la stdlib. Una instalación
en una máquina de producción sin salida a PyPI arranca igual. El día que haga
falta algo que `urllib` no da —sesiones, reintentos con backoff, HTTP/2— se
escribe `http_requests.py` al lado y se cambia el binding; ningún plugin se
entera, que es exactamente para lo que existe el port.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from backend.core.ports import HttpResponse, PortError

DEFAULT_TIMEOUT = 30.0

# Un cuerpo enorme en la traza de un run no ayuda a nadie y llena la base.
MAX_BODY_BYTES = 2 * 1024 * 1024


class UrllibHttpAdapter:
    """Cliente HTTP mínimo. No mantiene estado entre requests."""

    def __init__(self, default_timeout: float = DEFAULT_TIMEOUT) -> None:
        self.default_timeout = default_timeout

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        url = (url or "").strip()
        if not url:
            raise PortError("no se indicó una URL")
        if not url.lower().startswith(("http://", "https://")):
            # `urllib` abre `file://` y `ftp://`. Una URL armada desde una
            # variable de un flujo no debería poder leer el disco.
            raise PortError(f"esquema no soportado en la URL: {url!r}")

        method = (method or "GET").upper()
        data = body.encode("utf-8") if isinstance(body, str) else body
        if method in ("GET", "HEAD"):
            data = None

        req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
        espera = self.default_timeout if timeout is None else timeout

        try:
            with urllib.request.urlopen(req, timeout=espera) as respuesta:
                return _respuesta(respuesta.status, respuesta.headers, respuesta.read(), url)
        except urllib.error.HTTPError as exc:
            # Un 4xx/5xx **no** es un PortError: el servidor contestó. Vuelve
            # como respuesta para que el flujo pueda ramificar por el status y
            # la traza guarde el cuerpo del error.
            cuerpo = b""
            try:
                cuerpo = exc.read()
            except Exception:
                pass
            return _respuesta(exc.code, exc.headers, cuerpo, url)
        except urllib.error.URLError as exc:
            raise PortError(f"no se pudo conectar con {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise PortError(f"timeout tras {espera:g}s contra {url}") from exc


def _respuesta(status: int, headers, raw: bytes, url: str) -> HttpResponse:
    if len(raw) > MAX_BODY_BYTES:
        raw = raw[:MAX_BODY_BYTES]
    cabeceras = {k.lower(): v for k, v in (headers.items() if headers else [])}
    return HttpResponse(
        status=status,
        headers=cabeceras,
        text=raw.decode("utf-8", errors="replace"),
        url=url,
    )


__all__ = ["DEFAULT_TIMEOUT", "UrllibHttpAdapter"]
