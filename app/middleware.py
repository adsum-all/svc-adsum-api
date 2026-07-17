"""HTTP security hardening: OWASP response headers and a request-body size cap.

The response headers reduce clickjacking, MIME sniffing, referrer leakage and
force HTTPS. The body-size cap bounds how much a single request can make the
serverless function buffer, independently of any infrastructure limit.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("adsum.api")

_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# Every request body handled by this API is JSON: files never transit the API,
# they are uploaded straight to object storage through signed URLs. The largest
# legitimate body (a full questionnaire submission) stays under a couple of MB,
# so this ceiling is well above any real request while it caps a hostile
# multi-gigabyte body before it is ever buffered or parsed.
_MAX_BODY_BYTES = 5 * 1024 * 1024


class CorsSafeErrorBoundaryMiddleware:
    """Turn any unhandled exception into a clean JSON 500 that still travels back
    through the CORS middleware.

    Without this boundary an unhandled exception escapes to Starlette's built-in
    ServerErrorMiddleware, which sits OUTSIDE the CORS middleware: its 500 ships
    with no ``Access-Control-Allow-Origin`` header. A browser then reports the
    call as an opaque network failure ("Failed to fetch"), and the front shows a
    generic "Erreur reseau" that hides the real server error and makes a working
    feature look broken. Placed as the innermost application middleware, this
    boundary catches the exception first and returns a real Response, so the CORS
    middleware (outermost) adds its header on the way out and the client receives
    a genuine, actionable 500 instead of a masked one. The true error is logged
    server side; the stack trace is never leaked to the client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:  # noqa: BLE001 - last-resort boundary; must never leak a trace to the client
            logger.exception(
                "unhandled exception on %s %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
            )
            if response_started:
                # The response has already begun streaming; we cannot rewrite its
                # status or headers, so let the server layer finish handling it.
                raise
            body = json.dumps(
                {"detail": "Service momentanément indisponible. Réessayez dans un instant."},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        for key, value in _HEADERS.items():
            response.headers.setdefault(key, value)
        return response


class _BodyTooLarge(Exception):
    """Raised from the wrapped receive channel when the body exceeds the cap."""


class BodySizeLimitMiddleware:
    """Reject a request whose body exceeds the limit, before it is buffered whole.

    Two layers, so neither a lying nor an absent Content-Length can bypass it:
    a declared Content-Length over the cap is refused up front, and the actual
    bytes streamed on the ASGI receive channel are counted, so a chunked body
    (no Content-Length) is also stopped as soon as it crosses the cap. Bodyless
    requests (GET and similar) stream nothing and pass through untouched.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = _MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._too_large(send)
            return

        total = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            # Only safe to emit our own response if the app had not started one.
            if not response_started:
                await self._too_large(send)

    async def _too_large(self, send: Send) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
