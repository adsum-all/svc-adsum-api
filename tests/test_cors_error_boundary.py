"""Proof that an unhandled server error still carries the CORS header.

Regression guard for the "Erreur reseau" masking bug: when an endpoint raised an
unhandled exception, the 500 escaped to the CORS-less server layer and reached the
browser without ``Access-Control-Allow-Origin``. The browser then reported an opaque
network failure and the front showed a generic network error that hid the real cause.
The CorsSafeErrorBoundaryMiddleware turns that exception into a real 500 Response that
travels back out through the CORS middleware, so the header is present and the client
receives a genuine, actionable error instead of a masked one.

Two independent checks, neither of which mutates the shared production ``app``:
1. Behaviour: a throwaway app wired with the SAME middleware order actually raises,
   and the 500 carries the CORS header and leaks no stack trace.
2. Wiring: the real production app has the boundary as the innermost application
   middleware and CORS outermost, so the behaviour above applies to it.
No database is required.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.main import app as production_app
from app.middleware import (
    BodySizeLimitMiddleware,
    CorsSafeErrorBoundaryMiddleware,
    SecurityHeadersMiddleware,
)

_ORIGIN = "https://adsum-back-office.pages.dev"


def _throwaway_app() -> FastAPI:
    """A minimal app wired exactly like app.main: boundary added first (innermost),
    CORS added last (outermost). Kept separate from the production ``app`` so the
    test never grafts a route onto the shared singleton."""
    app = FastAPI()
    app.add_middleware(CorsSafeErrorBoundaryMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/boom")
    def _boom() -> dict[str, str]:
        # Simulates an endpoint hitting an unexpected failure (e.g. a transient
        # database error during a serverless cold start), previously a header-less 500.
        raise RuntimeError("simulated unhandled failure")

    return app


def test_unhandled_error_keeps_cors_header_and_returns_clean_500() -> None:
    # raise_server_exceptions=False so the ASGI stack (and the boundary) actually runs,
    # exactly as it would behind the real server, instead of re-raising into the test.
    client = TestClient(_throwaway_app(), raise_server_exceptions=False)
    res = client.get("/boom", headers={"Origin": _ORIGIN})

    assert res.status_code == 500
    # The header the browser needs: without it the call is reported as a network failure.
    acao = res.headers.get("access-control-allow-origin")
    assert acao in ("*", _ORIGIN), f"missing CORS header on 500, got {acao!r}"
    # The client receives a clean, actionable JSON detail, never a stack trace.
    body = res.json()
    assert isinstance(body.get("detail"), str) and body["detail"]
    assert "Traceback" not in res.text and "RuntimeError" not in res.text


def test_production_app_wires_boundary_inside_cors() -> None:
    # add_middleware inserts at position 0, so user_middleware[0] is the OUTERMOST.
    # CORS must be outermost (smaller index) and the error boundary the innermost
    # application middleware (larger index), so the boundary's 500 travels back out
    # through CORS and receives the Access-Control-Allow-Origin header.
    classes = [m.cls for m in production_app.user_middleware]
    assert CorsSafeErrorBoundaryMiddleware in classes, "error boundary not wired into the app"
    assert CORSMiddleware in classes, "CORS middleware not wired into the app"
    assert classes.index(CORSMiddleware) < classes.index(CorsSafeErrorBoundaryMiddleware), (
        "CORS must be outermost and the error boundary innermost"
    )


def test_preflight_still_authorised_for_the_browser_origin() -> None:
    client = TestClient(production_app)
    res = client.options(
        "/api/v1/admin/evenements",
        headers={
            "Origin": _ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert res.status_code in (200, 204)
    assert res.headers.get("access-control-allow-origin") in ("*", _ORIGIN)
