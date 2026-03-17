"""
API key authentication dependency for voice endpoints (Phase 4.3).

Behaviour:
  - If VOICE_API_KEY is empty (default), auth is **disabled** — all requests pass.
    This keeps local dev and CI friction-free.
  - If VOICE_API_KEY is set, callers must send the header ``X-API-Key: <key>``.
    Missing or wrong key → 401 Unauthorized.

Usage::

    from app.auth import require_api_key

    @router.post("/voice/turn", dependencies=[Depends(require_api_key)])
    async def voice_turn(...): ...
"""

from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import settings
from app.observability import metrics

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    api_key: str | None = Security(_api_key_header),
) -> None:
    """FastAPI dependency – enforce API key when configured.

    No-op when ``VOICE_API_KEY`` is empty (dev / CI mode).
    """
    expected = settings.VOICE_API_KEY
    if not expected:
        # Auth disabled — pass through
        return
    if not api_key or api_key != expected:
        metrics.inc("auth_failures")
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key.",
        )
