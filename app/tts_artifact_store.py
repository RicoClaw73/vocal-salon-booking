"""
TTS audio artifact persistence scaffold (Phase 5.2).

Provides a local-first file store for TTS-generated audio.  Each artifact
is saved to ``<store_dir>/<session_id>/<text_hash>.<format>`` and can be
retrieved via a local path or a URL-like reference suitable for the API
response.

Design constraints:
  - No paid dependencies; uses the local filesystem.
  - Thread-safe for single-worker async (FastAPI default).
  - Configurable via ``TTS_ARTIFACT_DIR`` env var (defaults to
    ``./data/tts_artifacts``).
  - Exposes a ``store_artifact`` / ``get_artifact_url`` API consumed by the
    voice router.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.observability import StructuredLogger

_slog = StructuredLogger(__name__)


class TTSArtifactStore:
    """Local filesystem store for TTS audio output.

    Artifacts are organised as::

        <root>/
          <session_id>/
            <text_hash>.<format>

    The ``text_hash`` is a truncated SHA-256 of the synthesised text, ensuring
    deterministic filenames for cache-friendliness.
    """

    def __init__(self, root_dir: str | Path | None = None) -> None:
        if root_dir is None:
            root_dir = Path(__file__).resolve().parent.parent / "data" / "tts_artifacts"
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        _slog.info("tts_artifact_store_init", root=str(self._root))

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def text_hash(text: str) -> str:
        """Deterministic 16-char hex hash of the input text."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def store(
        self,
        session_id: str,
        text: str,
        audio_bytes: bytes,
        audio_format: str = "wav",
    ) -> Path:
        """Persist TTS audio bytes to local storage.

        Returns the absolute path of the stored file.
        """
        h = self.text_hash(text)
        session_dir = self._root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        file_path = session_dir / f"{h}.{audio_format}"
        try:
            file_path.write_bytes(audio_bytes)
        except OSError as exc:
            _slog.error(
                "tts_artifact_write_failed",
                session_id=session_id,
                text_hash=h,
                path=str(file_path),
                error=str(exc),
            )
            raise
        _slog.debug(
            "tts_artifact_stored",
            session_id=session_id,
            text_hash=h,
            format=audio_format,
            size_bytes=len(audio_bytes),
            path=str(file_path),
        )
        return file_path

    def get_artifact_path(
        self,
        session_id: str,
        text: str,
        audio_format: str = "wav",
    ) -> Path | None:
        """Return the path if the artifact exists on disk, else None."""
        h = self.text_hash(text)
        file_path = self._root / session_id / f"{h}.{audio_format}"
        return file_path if file_path.exists() else None

    def artifact_url(self, artifact_path: Path) -> str:
        """Convert a local path to a URL-style reference.

        In local mode this returns a ``file://`` URI.  A future HTTP-served
        mode could return ``/artifacts/tts/...`` instead.
        """
        return f"file://{artifact_path}"

    def store_and_get_url(
        self,
        session_id: str,
        text: str,
        audio_bytes: bytes,
        audio_format: str = "wav",
    ) -> str:
        """Convenience: store artifact and return its URL in one call."""
        path = self.store(session_id, text, audio_bytes, audio_format)
        return self.artifact_url(path)


# ── Module-level singleton ─────────────────────────────────────

def _init_store() -> TTSArtifactStore:
    """Build store from env config, with safe default."""
    import os
    custom = os.environ.get("TTS_ARTIFACT_DIR", "")
    return TTSArtifactStore(root_dir=custom or None)


tts_artifact_store = _init_store()
"""Shared TTS artifact store instance."""
