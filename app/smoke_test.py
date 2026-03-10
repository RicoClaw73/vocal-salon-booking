"""
Optional real-provider smoke test (Phase 5.1).

Run manually or from CI when secrets are available::

    # Requires env vars: STT_API_KEY, TTS_API_KEY (and corresponding *_PROVIDER)
    python -m app.smoke_test

    # Or via pytest (only runs when SMOKE_TEST_PROVIDERS=1 is set):
    SMOKE_TEST_PROVIDERS=1 pytest tests/test_provider_smoke.py -v

The smoke test:
  1. Checks provider readiness via ``check_provider_readiness``.
  2. Sends a short audio clip (synthesised silence) through STT.
  3. Sends a short French text through TTS.
  4. Reports latency and success/failure for each step.

**No tests run** unless the ``SMOKE_TEST_PROVIDERS`` env flag is set to "1".
This ensures default CI stays fully secret-free.
"""

from __future__ import annotations

import asyncio
import os
import time

from app.config import settings
from app.providers import (
    AudioFormat,
    check_provider_readiness,
    get_stt_provider,
    get_tts_provider,
    safe_synthesize,
    safe_transcribe,
)


async def run_smoke_test() -> dict:
    """
    Execute a lightweight smoke test against the configured providers.

    Returns a dict with readiness info and per-step results.
    """
    results: dict = {"steps": [], "overall": "pass"}

    # Step 1: readiness check
    readiness = check_provider_readiness(
        stt_requested=settings.STT_PROVIDER,
        stt_api_key=settings.STT_API_KEY,
        tts_requested=settings.TTS_PROVIDER,
        tts_api_key=settings.TTS_API_KEY,
    )
    results["readiness"] = readiness

    # Step 2: STT smoke (send 0.5s of silence as WAV-like bytes)
    stt = get_stt_provider(
        settings.STT_PROVIDER,
        api_key=settings.STT_API_KEY,
        model=settings.STT_MODEL or None,
    )
    silence_bytes = b"\x00" * 16000  # ~0.5s at 16kHz 16-bit mono
    t0 = time.monotonic()
    stt_result, stt_outcome = await safe_transcribe(
        stt, silence_bytes, AudioFormat.wav, "fr",
    )
    stt_ms = round((time.monotonic() - t0) * 1000, 1)
    results["steps"].append({
        "step": "stt_transcribe",
        "provider": stt.provider_name,
        "success": stt_outcome.success,
        "fallback_used": stt_outcome.fallback_used,
        "error_kind": stt_outcome.error_kind.value if stt_outcome.error_kind else None,
        "latency_ms": stt_ms,
        "transcript_preview": stt_result.transcript[:80],
    })

    # Step 3: TTS smoke (short French sentence)
    tts = get_tts_provider(
        settings.TTS_PROVIDER,
        api_key=settings.TTS_API_KEY,
        voice_id=settings.TTS_VOICE_ID or None,
        model=settings.TTS_MODEL or None,
    )
    t0 = time.monotonic()
    tts_result, tts_outcome = await safe_synthesize(
        tts, "Bonjour, ceci est un test.", language="fr",
    )
    tts_ms = round((time.monotonic() - t0) * 1000, 1)
    results["steps"].append({
        "step": "tts_synthesize",
        "provider": tts.provider_name,
        "success": tts_outcome.success,
        "fallback_used": tts_outcome.fallback_used,
        "error_kind": tts_outcome.error_kind.value if tts_outcome.error_kind else None,
        "latency_ms": tts_ms,
        "duration_ms": tts_result.duration_ms,
    })

    # Overall verdict
    if any(not s["success"] for s in results["steps"]):
        results["overall"] = "fail"
    elif any(s["fallback_used"] for s in results["steps"]):
        results["overall"] = "degraded"

    return results


def main() -> None:
    """CLI entry point for manual smoke testing."""
    import json

    result = asyncio.run(run_smoke_test())
    print(json.dumps(result, indent=2))
    if result["overall"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
