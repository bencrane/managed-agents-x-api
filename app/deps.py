"""FastAPI dependencies (secret validation, auth)."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import settings


def require_mag_auth(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate using MAGS_AUTH_TOKEN from Doppler.

    This is the inbound auth check for every non-public route in
    managed-agents-x. `MAGS_AUTH_TOKEN` grants access *into* this service;
    callers (future domain services, internal tools, the ops-engine-x
    routing layer) present it when calling `/admin/status` and the future
    `/agents*` surface. On the caller side it is paired with `MAG_API_URL`
    (the pointer at this service's base URL).
    """
    expected = settings.mags_auth_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MAGS_AUTH_TOKEN not configured (check Doppler).",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# Backwards-compatible alias kept so future `/agents*` / `/admin/sync/anthropic`
# route handlers being moved in from ops-engine-x keep working without being
# modified. New code should import `require_mag_auth` directly.
require_admin_token = require_mag_auth
