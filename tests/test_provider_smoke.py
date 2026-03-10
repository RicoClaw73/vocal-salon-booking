"""
Real-provider smoke tests (Phase 5.1).

**These tests are SKIPPED by default.** They only run when the env var
``SMOKE_TEST_PROVIDERS=1`` is set, ensuring CI stays secret-free.

Usage::

    # With real credentials:
    SMOKE_TEST_PROVIDERS=1 STT_PROVIDER=deepgram STT_API_KEY=dg_... \
        TTS_PROVIDER=elevenlabs TTS_API_KEY=sk_... \
        pytest tests/test_provider_smoke.py -v
"""

from __future__ import annotations

import os

import pytest

# Skip entire module unless opt-in flag is set
pytestmark = pytest.mark.skipif(
    os.environ.get("SMOKE_TEST_PROVIDERS") != "1",
    reason="SMOKE_TEST_PROVIDERS not set — skipping real-provider smoke tests",
)


@pytest.mark.asyncio
async def test_real_provider_smoke():
    """Run the full smoke test against configured real providers."""
    from app.smoke_test import run_smoke_test

    result = await run_smoke_test()
    assert result["overall"] in ("pass", "degraded")
    for step in result["steps"]:
        # At minimum, each step should succeed (possibly with fallback)
        assert step["success"] is True, f"Step {step['step']} failed: {step}"
