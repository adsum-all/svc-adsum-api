"""Trusted client IP resolution behind the Vercel edge.

A raw ``X-Forwarded-For`` is attacker-controlled: its leftmost value is whatever
the client sent, so trusting it lets an attacker rotate the IP and defeat rate
limiting, or forge the IP recorded as consent proof. Behind Vercel the real
client IP is exposed by ``x-vercel-forwarded-for`` (set by the platform, not the
client) and the proxy APPENDS the real IP to ``X-Forwarded-For`` (so the last hop
is trustworthy, never the first). This resolves the IP from those trusted sources
only.
"""
from __future__ import annotations

from fastapi import Request


def client_ip(request: Request) -> str | None:
    # Platform-set headers: trustworthy because Vercel writes them, the client
    # cannot spoof them at the application layer.
    for header in ("x-vercel-forwarded-for", "x-real-ip"):
        value = request.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            if first:
                return first
    # Fallback: the LAST hop of X-Forwarded-For is the one appended by the trusted
    # proxy; the leftmost is client-supplied and must never be trusted.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else None
