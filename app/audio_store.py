"""
Audio file store for TTS artifacts (Phase 5 — ElevenLabs integration).

Saves ElevenLabs MP3 responses to disk and serves them via FastAPI StaticFiles.
Includes automatic cleanup of files older than AUDIO_MAX_AGE_HOURS.

File naming: {unix_ts}_{session_id[:8]}_{turn}.mp3
- Sortable by age (unix_ts prefix)
- Unique per turn
- No collision across sessions

Cleanup runs:
  - On app startup
  - Every hour via background asyncio task
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# ── TTS text normalisation ────────────────────────────────────

_MONTHS_FR = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _time_colon(m: re.Match) -> str:
    h, mn = int(m.group(1)), int(m.group(2))
    return f"{h} heures" if mn == 0 else f"{h} heures {mn}"


def _time_h(m: re.Match) -> str:
    h = int(m.group(1))
    mn = int(m.group(2)) if m.group(2) else 0
    return f"{h} heures" if mn == 0 else f"{h} heures {mn}"


def _iso_date(m: re.Match) -> str:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{d} {_MONTHS_FR[mo]} {y}"


def normalize_for_tts(text: str) -> str:
    """
    Normalise text so French TTS pronounces times and dates correctly.

    Converts:
      2026-03-18  →  18 mars 2026
      09:00       →  9 heures
      14:30       →  14 heures 30
      9h00        →  9 heures
      14h30       →  14 heures 30
      9h          →  9 heures
    """
    # ISO dates first (contains digits + hyphens, must run before time patterns)
    text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", _iso_date, text)
    # HH:MM colon format
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", _time_colon, text)
    # XhMM or Xh shorthand
    text = re.sub(r"\b(\d{1,2})h(\d{2})?\b", _time_h, text)
    return text

# ── ElevenLabs API ───────────────────────────────────────────

_ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
_DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"   # Sarah — à remplacer par une voix FR
_DEFAULT_MODEL = "eleven_turbo_v2_5"           # Turbo : 2-3x plus rapide, qualité suffisante pour téléphonie


async def synthesize_to_file(
    text: str,
    audio_dir: Path,
    api_key: str,
    session_id: str,
    turn: int,
    voice_id: str = "",
    model: str = "",
    filename: str | None = None,
) -> str | None:
    """
    Call ElevenLabs, save MP3 to disk, return the filename.

    Returns None on any error (caller should fall back to Twilio <Say>).
    """
    if not api_key:
        return None

    text = normalize_for_tts(text)
    effective_voice = voice_id or _DEFAULT_VOICE_ID
    effective_model = model or _DEFAULT_MODEL
    url = f"{_ELEVENLABS_URL}/{effective_voice}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={
                    "text": text,
                    "model_id": effective_model,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                headers={
                    "xi-api-key": api_key,
                    "Accept": "audio/mpeg",
                },
            )
            resp.raise_for_status()
            audio_bytes = resp.content

        # Save to disk
        audio_dir.mkdir(parents=True, exist_ok=True)
        if not filename:
            ts = int(time.time())
            sid_short = session_id[:8].replace("/", "_")
            filename = f"{ts}_{sid_short}_{turn}.mp3"
        filepath = audio_dir / filename
        filepath.write_bytes(audio_bytes)
        logger.info("TTS saved: %s (%d bytes)", filename, len(audio_bytes))
        return filename

    except httpx.TimeoutException:
        logger.warning("ElevenLabs timeout for session=%s turn=%d", session_id, turn)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning("ElevenLabs HTTP %d: %s", e.response.status_code, e.response.text[:200])
        return None
    except Exception as e:
        logger.warning("ElevenLabs error: %s", e)
        return None


# ── Cleanup ──────────────────────────────────────────────────


def cleanup_old_files(audio_dir: Path, max_age_hours: int = 1) -> int:
    """
    Delete MP3 files older than max_age_hours. Returns count deleted.

    Safe to call at any time — ignores files that are in use or already deleted.
    """
    if not audio_dir.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0
    for f in audio_dir.glob("*.mp3"):
        # Skip files that don't start with a unix timestamp (e.g. greeting.mp3)
        if not f.name[0].isdigit():
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            pass  # Already deleted or locked

    if deleted:
        logger.info("Audio cleanup: deleted %d files older than %dh", deleted, max_age_hours)
    return deleted


async def cleanup_loop(audio_dir: Path, max_age_hours: int = 1) -> None:
    """Background asyncio task — cleans up every hour indefinitely."""
    while True:
        await asyncio.sleep(3600)
        try:
            cleanup_old_files(audio_dir, max_age_hours)
        except Exception as e:
            logger.warning("Audio cleanup error: %s", e)
