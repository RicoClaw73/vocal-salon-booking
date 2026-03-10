"""
In-memory rate limiter scaffold for voice endpoints (Phase 4.3).

Strategy: fixed-window counter per client IP, configurable via
``RATE_LIMIT_PER_MINUTE``.  When the setting is 0 the limiter is disabled.

Limitations (documented):
  - In-memory only — resets on process restart.
  - Not shared across workers.  For multi-worker production, swap to
    Redis-backed sliding window (e.g. via ``slowapi`` or custom).
  - Uses client IP from ``request.client.host``; behind a reverse proxy
    make sure ``X-Forwarded-For`` / ``--proxy-headers`` are configured.

Usage::

    from app.rate_limit import rate_limit_dependency

    @router.post("/voice/turn", dependencies=[Depends(rate_limit_dependency)])
    async def voice_turn(...): ...
"""

from __future__ import annotations

import time
import logging
from collections import defaultdict

from fastapi import Depends, HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)

# ── Storage ──────────────────────────────────────────────────
# { ip: (window_start_timestamp, count) }
_buckets: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))
_WINDOW_SECONDS = 60.0


def _reset_buckets() -> None:
    """Clear all rate-limit state (for tests)."""
    _buckets.clear()


async def rate_limit_dependency(request: Request) -> None:
    """FastAPI dependency — enforce per-IP rate limit when configured.

    No-op when ``RATE_LIMIT_PER_MINUTE`` is 0.
    """
    limit = settings.RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return

    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window_start, count = _buckets[client_ip]

    # New window?
    if now - window_start >= _WINDOW_SECONDS:
        _buckets[client_ip] = (now, 1)
        return

    if count >= limit:
        logger.warning("Rate limit exceeded for %s (%d/%d)", client_ip, count, limit)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
        )

    _buckets[client_ip] = (window_start, count + 1)
