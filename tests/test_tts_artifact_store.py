"""
Tests for TTS audio artifact persistence scaffold (Phase 5.2).

Covers:
  - Store writes file to correct path
  - Artifact can be retrieved after storing
  - Non-existent artifact returns None
  - text_hash is deterministic
  - artifact_url returns file:// URI
  - store_and_get_url convenience method works
  - Session-scoped directory creation
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.tts_artifact_store import TTSArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> TTSArtifactStore:
    """Artifact store writing to a temporary directory."""
    return TTSArtifactStore(root_dir=tmp_path)


class TestTTSArtifactStore:

    def test_store_creates_file(self, store: TTSArtifactStore):
        audio = b"\x00\x01\x02\x03" * 100
        path = store.store("session_1", "Bonjour", audio, "wav")
        assert path.exists()
        assert path.read_bytes() == audio
        assert path.suffix == ".wav"

    def test_store_creates_session_directory(self, store: TTSArtifactStore):
        store.store("session_abc", "Hello", b"\x00", "mp3")
        session_dir = store.root / "session_abc"
        assert session_dir.is_dir()

    def test_get_artifact_path_found(self, store: TTSArtifactStore):
        store.store("s1", "test text", b"\xFF", "wav")
        path = store.get_artifact_path("s1", "test text", "wav")
        assert path is not None
        assert path.exists()

    def test_get_artifact_path_not_found(self, store: TTSArtifactStore):
        path = store.get_artifact_path("nonexistent", "no such text", "wav")
        assert path is None

    def test_text_hash_deterministic(self, store: TTSArtifactStore):
        h1 = store.text_hash("Bonjour le monde")
        h2 = store.text_hash("Bonjour le monde")
        assert h1 == h2
        assert len(h1) == 16

    def test_text_hash_different_for_different_text(self, store: TTSArtifactStore):
        h1 = store.text_hash("Hello")
        h2 = store.text_hash("Goodbye")
        assert h1 != h2

    def test_artifact_url_format(self, store: TTSArtifactStore):
        path = store.store("s1", "test", b"\x00", "wav")
        url = store.artifact_url(path)
        assert url.startswith("file://")
        assert str(path) in url

    def test_store_and_get_url(self, store: TTSArtifactStore):
        url = store.store_and_get_url("s2", "Merci", b"\x01\x02", "mp3")
        assert url.startswith("file://")
        assert ".mp3" in url

    def test_overwrite_existing_artifact(self, store: TTSArtifactStore):
        """Storing same text twice overwrites the file."""
        store.store("s1", "same text", b"\x01", "wav")
        store.store("s1", "same text", b"\x02\x03", "wav")
        path = store.get_artifact_path("s1", "same text", "wav")
        assert path is not None
        assert path.read_bytes() == b"\x02\x03"

    def test_different_formats_different_files(self, store: TTSArtifactStore):
        """Same text in different formats produces different files."""
        store.store("s1", "text", b"\x01", "wav")
        store.store("s1", "text", b"\x02", "mp3")
        wav_path = store.get_artifact_path("s1", "text", "wav")
        mp3_path = store.get_artifact_path("s1", "text", "mp3")
        assert wav_path is not None
        assert mp3_path is not None
        assert wav_path != mp3_path
