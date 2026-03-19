"""
Tests for the RGPD vocal consent flow.

Covers POST /twilio/voice and POST /twilio/consent, validating the
CNIL opt-out model:
  - Silence / timeout  = implied consent (Digits empty)
  - DTMF "1"           = explicit refusal → hangup, no session
  - Any other DTMF     = unexpected → re-play consent via redirect

All Twilio signature verification is bypassed because
settings.TWILIO_AUTH_TOKEN is empty in the test environment.
ElevenLabs TTS is disabled (ELEVENLABS_API_KEY="") so TwiML always
falls back to <Say>, making assertions deterministic.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.conversation import conversation_manager
from app.models import VoiceSession
from app.observability import metrics

PREFIX = "/api/v1/twilio"


@pytest.fixture(autouse=True)
def _bypass_twilio_signature():
    """Force signature verification off for all tests in this file.

    Twilio sends a real HMAC-SHA1 signature which we can't reproduce in unit
    tests without knowing the exact public URL.  Setting TWILIO_AUTH_TOKEN=""
    makes _verify_signature() exit early (dev-mode bypass).
    """
    orig = settings.TWILIO_AUTH_TOKEN
    settings.TWILIO_AUTH_TOKEN = ""
    yield
    settings.TWILIO_AUTH_TOKEN = orig


class TestTwilioConsentFlow:
    """Integration tests for RGPD vocal consent (CNIL opt-out model)."""

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _voice_params(call_sid: str, caller: str = "+33612345678") -> dict:
        return {
            "CallSid": call_sid,
            "From": caller,
            "CallerName": "",
            "CallStatus": "ringing",
        }

    @staticmethod
    def _consent_params(call_sid: str, digits: str = "") -> dict:
        return {
            "CallSid": call_sid,
            "From": "+33612345678",
            "CallerName": "",
            "Digits": digits,
        }

    @staticmethod
    def _xml(text: str) -> ET.Element:
        return ET.fromstring(text)

    # ── TC1: Consent ON, new call → DTMF Gather, NO session created ──────────

    @pytest.mark.asyncio
    async def test_voice_consent_enabled_new_call(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """AC1+AC2: /voice with consent enabled returns DTMF Gather; no VoiceSession in DB."""
        call_sid = "CA_consent_tc1"
        orig = settings.CONSENT_ENABLED
        settings.CONSENT_ENABLED = True
        try:
            resp = await client.post(
                f"{PREFIX}/voice",
                data=self._voice_params(call_sid),
            )
        finally:
            settings.CONSENT_ENABLED = orig
            conversation_manager._sessions.pop(call_sid, None)

        assert resp.status_code == 200
        assert "application/xml" in resp.headers["content-type"]

        root = self._xml(resp.text)

        # Must have a DTMF <Gather> (not speech)
        gather = root.find(".//Gather")
        assert gather is not None, "<Gather> missing in consent TwiML"
        assert gather.get("input") == "dtmf", "Gather must collect DTMF, not speech"
        assert "/twilio/consent" in (gather.get("action") or ""), (
            "Gather action must point to /twilio/consent"
        )

        # Redirect fallthrough also points to /consent (timeout path)
        redirect = root.find(".//Redirect")
        assert redirect is not None, "<Redirect> fallthrough missing"
        assert "/twilio/consent" in (redirect.text or "")

        # No session created before consent
        result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.session_id == call_sid)
        )
        assert result.scalars().first() is None, (
            "VoiceSession must NOT be created before consent is given"
        )

        # Metric for new call
        assert metrics._counters.get("telephony_calls_started", 0) == 1

    # ── TC2: Consent OFF, new call → session immediately, speech Gather ───────

    @pytest.mark.asyncio
    async def test_voice_consent_disabled_new_call(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """AC3: /voice with consent disabled creates session immediately; returns speech Gather."""
        call_sid = "CA_consent_tc2"
        orig = settings.CONSENT_ENABLED
        settings.CONSENT_ENABLED = False
        try:
            resp = await client.post(
                f"{PREFIX}/voice",
                data=self._voice_params(call_sid),
            )
        finally:
            settings.CONSENT_ENABLED = orig
            conversation_manager._sessions.pop(call_sid, None)

        assert resp.status_code == 200

        root = self._xml(resp.text)
        gather = root.find(".//Gather")
        assert gather is not None, "<Gather> missing in greeting TwiML"
        assert gather.get("input") == "speech", "No-consent flow must use speech Gather"

        # Session created immediately
        result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.session_id == call_sid)
        )
        row = result.scalars().first()
        assert row is not None, "VoiceSession must be created immediately when consent is disabled"
        assert metrics._counters.get("sessions_started", 0) == 1

    # ── TC3: DTMF "1" → Hangup, no session, consent_refused metric ────────────

    @pytest.mark.asyncio
    async def test_consent_dtmf_refusal(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """AC4: /consent Digits='1' → <Hangup> TwiML, no session created, consent_refused +1."""
        call_sid = "CA_consent_tc3"
        try:
            resp = await client.post(
                f"{PREFIX}/consent",
                data=self._consent_params(call_sid, digits="1"),
            )
        finally:
            conversation_manager._sessions.pop(call_sid, None)

        assert resp.status_code == 200

        root = self._xml(resp.text)

        # Must hang up
        assert root.find(".//Hangup") is not None, "<Hangup> missing on refusal"

        # Must NOT offer a <Gather>
        assert root.find(".//Gather") is None, "<Gather> must be absent on refusal"

        # No session in DB
        result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.session_id == call_sid)
        )
        assert result.scalars().first() is None, "No VoiceSession must be created on refusal"

        # Metric
        assert metrics._counters.get("consent_refused", 0) == 1

    # ── TC4: DTMF empty (timeout) → session created, consent_given=True ───────

    @pytest.mark.asyncio
    async def test_consent_timeout_accepted(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """AC5: /consent Digits='' → session with consent_given=True and consent_at set."""
        call_sid = "CA_consent_tc4"
        try:
            resp = await client.post(
                f"{PREFIX}/consent",
                data=self._consent_params(call_sid, digits=""),
            )
        finally:
            conversation_manager._sessions.pop(call_sid, None)

        assert resp.status_code == 200

        root = self._xml(resp.text)

        # Greeting + speech Gather
        gather = root.find(".//Gather")
        assert gather is not None, "<Gather> missing after consent"
        assert gather.get("input") == "speech", "Post-consent Gather must collect speech"

        # Session in DB with consent fields
        result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.session_id == call_sid)
        )
        row = result.scalars().first()
        assert row is not None, "VoiceSession must be created after implied consent"
        assert row.consent_given is True, "consent_given must be True"
        assert row.consent_at is not None, "consent_at must be set (RGPD audit trail)"

        # Metrics
        assert metrics._counters.get("consent_accepted", 0) == 1
        assert metrics._counters.get("sessions_started", 0) == 1

    # ── TC5: Unexpected DTMF → Redirect to /voice, no session ────────────────

    @pytest.mark.asyncio
    async def test_consent_unexpected_digit_redirects(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """AC6: /consent with unexpected digit ('2') → <Redirect> to /voice, no session."""
        call_sid = "CA_consent_tc5"
        try:
            resp = await client.post(
                f"{PREFIX}/consent",
                data=self._consent_params(call_sid, digits="2"),
            )
        finally:
            conversation_manager._sessions.pop(call_sid, None)

        assert resp.status_code == 200

        root = self._xml(resp.text)

        redirect = root.find(".//Redirect")
        assert redirect is not None, "<Redirect> missing for unexpected digit"
        assert "/twilio/voice" in (redirect.text or ""), (
            "Redirect must point back to /voice to re-play consent"
        )

        # No <Gather>, no <Hangup> — just a redirect
        assert root.find(".//Gather") is None
        assert root.find(".//Hangup") is None

        # No session
        result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.session_id == call_sid)
        )
        assert result.scalars().first() is None, (
            "No VoiceSession must be created for unexpected DTMF"
        )

    # ── TC6: Existing session on /voice → speech Gather, no consent re-play ──

    @pytest.mark.asyncio
    async def test_voice_existing_session_no_consent_replay(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """AC7: /voice with existing session returns silence re-prompt (not consent DTMF)."""
        call_sid = "CA_consent_tc6"
        orig = settings.CONSENT_ENABLED
        settings.CONSENT_ENABLED = True
        try:
            # Step 1: initial call → consent TwiML (no session yet)
            r1 = await client.post(
                f"{PREFIX}/voice",
                data=self._voice_params(call_sid),
            )
            assert r1.status_code == 200
            assert self._xml(r1.text).find(".//Gather").get("input") == "dtmf"  # type: ignore[union-attr]

            # Step 2: caller stays silent → implied consent → session created
            r2 = await client.post(
                f"{PREFIX}/consent",
                data=self._consent_params(call_sid, digits=""),
            )
            assert r2.status_code == 200

            # Step 3: Twilio re-hits /voice (e.g. silence re-entry) with same CallSid
            r3 = await client.post(
                f"{PREFIX}/voice",
                data=self._voice_params(call_sid),
            )
        finally:
            settings.CONSENT_ENABLED = orig
            conversation_manager._sessions.pop(call_sid, None)

        assert r3.status_code == 200

        root = self._xml(r3.text)
        gather = root.find(".//Gather")
        assert gather is not None
        assert gather.get("input") == "speech", (
            "Existing session must NOT re-play consent DTMF Gather"
        )

        # Only one session row (idempotent)
        result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.session_id == call_sid)
        )
        rows = result.scalars().all()
        assert len(rows) == 1, "Twilio replay must not create duplicate sessions"

    # ── TC7: /voice consent ON + metrics after full consent sequence ──────────

    @pytest.mark.asyncio
    async def test_consent_full_sequence_metrics(
        self,
        client: AsyncClient,
    ) -> None:
        """End-to-end: voice → consent accepted → metrics are consistent."""
        call_sid = "CA_consent_tc7"
        orig = settings.CONSENT_ENABLED
        settings.CONSENT_ENABLED = True
        try:
            await client.post(f"{PREFIX}/voice", data=self._voice_params(call_sid))
            await client.post(
                f"{PREFIX}/consent",
                data=self._consent_params(call_sid, digits=""),
            )
        finally:
            settings.CONSENT_ENABLED = orig
            conversation_manager._sessions.pop(call_sid, None)

        assert metrics._counters.get("telephony_calls_started", 0) == 1
        assert metrics._counters.get("consent_accepted", 0) == 1
        assert metrics._counters.get("sessions_started", 0) == 1
        assert metrics._counters.get("consent_refused", 0) == 0
