"""
TwiML XML builder (Phase 5 — Twilio integration).

Generates TwiML responses without the Twilio SDK.
Uses stdlib xml.etree.ElementTree only.

Usage::

    twiml = TwiML()
    twiml.gather(action="/api/v1/twilio/gather", input="speech", language="fr-FR", timeout="5") \
         .say("Bonjour, comment puis-je vous aider ?")
    twiml.redirect("/api/v1/twilio/voice")
    return twiml.response()

    # SMS
    twiml = TwiML()
    twiml.message("Votre RDV est confirmé !")
    return twiml.response()
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi.responses import Response as FastAPIResponse


class Gather:
    """Builder for the TwiML <Gather> verb (collects speech or DTMF)."""

    def __init__(self, parent: Element, **attrs: str) -> None:
        self._el = SubElement(parent, "Gather", **attrs)

    def say(
        self,
        text: str,
        voice: str = "alice",
        language: str = "fr-FR",
    ) -> "Gather":
        """Add a <Say> inside the <Gather>."""
        el = SubElement(self._el, "Say", voice=voice, language=language)
        el.text = text
        return self

    def play(self, url: str) -> "Gather":
        """Add a <Play> inside the <Gather> (ElevenLabs audio URL)."""
        el = SubElement(self._el, "Play")
        el.text = url
        return self


class TwiML:
    """
    Minimal TwiML builder.

    Methods return `self` (or a Gather builder) for chaining.
    Call `.response()` to get a FastAPI Response with the correct media type.
    """

    def __init__(self) -> None:
        self._root = Element("Response")

    # ── Voice verbs ────────────────────────────────────────────

    def say(
        self,
        text: str,
        voice: str = "alice",
        language: str = "fr-FR",
    ) -> "TwiML":
        """Add a <Say> verb."""
        el = SubElement(self._root, "Say", voice=voice, language=language)
        el.text = text
        return self

    def gather(
        self,
        action: str,
        input: str = "speech",
        language: str = "fr-FR",
        timeout: str = "5",
        speech_timeout: str = "auto",
        method: str = "POST",
        num_digits: str | None = None,
    ) -> Gather:
        """
        Add a <Gather> verb and return a Gather builder.

        Use the builder to nest <Say> inside <Gather>. The parent TwiML
        instance continues to be updated — call methods on the TwiML object
        after gather() to add verbs that follow the <Gather>.
        """
        attrs: dict[str, str] = dict(
            input=input,
            language=language,
            timeout=timeout,
            speechTimeout=speech_timeout,
            action=action,
            method=method,
        )
        if num_digits is not None:
            attrs["numDigits"] = num_digits
        return Gather(self._root, **attrs)

    def play(self, url: str) -> "TwiML":
        """Add a <Play> verb (plays an audio file URL)."""
        el = SubElement(self._root, "Play")
        el.text = url
        return self

    def redirect(self, url: str, method: str = "POST") -> "TwiML":
        """Add a <Redirect> verb."""
        el = SubElement(self._root, "Redirect", method=method)
        el.text = url
        return self

    def hangup(self) -> "TwiML":
        """Add a <Hangup> verb."""
        SubElement(self._root, "Hangup")
        return self

    def dial(self, number: str) -> "TwiML":
        """Add a <Dial> verb (transfer to a phone number)."""
        el = SubElement(self._root, "Dial")
        el.text = number
        return self

    def record(
        self,
        action: str,
        max_length: int = 120,
        timeout: int = 10,
        play_beep: bool = True,
        method: str = "POST",
    ) -> "TwiML":
        """Add a <Record> verb. Records caller audio and POSTs to action URL."""
        SubElement(
            self._root,
            "Record",
            action=action,
            maxLength=str(max_length),
            timeout=str(timeout),
            playBeep="true" if play_beep else "false",
            method=method,
        )
        return self

    # ── SMS verbs ──────────────────────────────────────────────

    def message(self, text: str, to: str = "", from_: str = "") -> "TwiML":
        """Add a <Message> verb for SMS responses."""
        attrs: dict[str, str] = {}
        if to:
            attrs["to"] = to
        if from_:
            attrs["from"] = from_
        el = SubElement(self._root, "Message", **attrs)
        el.text = text
        return self

    # ── Serialisation ──────────────────────────────────────────

    def to_xml(self) -> str:
        """Serialise to a TwiML XML string."""
        body = tostring(self._root, encoding="unicode")
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}'

    def response(self) -> FastAPIResponse:
        """Return a FastAPI Response with Content-Type: application/xml."""
        return FastAPIResponse(
            content=self.to_xml(),
            media_type="application/xml",
        )
