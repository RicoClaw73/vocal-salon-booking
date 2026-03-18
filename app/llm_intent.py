"""
LLM-powered intent detection via OpenAI GPT-4o.

Provides structured intent classification with entity extraction, designed as
a drop-in async layer above the existing rule-based engine.  When the LLM call
succeeds and returns valid JSON, we use it; on any error (timeout, invalid
response, network issue, rate limit) we fall back transparently to the
deterministic rule-based engine.

Security: API keys are never logged.  Only the first 4 characters of the key
are included in diagnostic logs (masked).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.voice_schemas import VoiceIntent

logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────

LLM_TIMEOUT_SECONDS: float = 5.0  # Aggressive timeout — voice pipeline is latency-sensitive
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

_VALID_INTENTS = {e.value for e in VoiceIntent}


# ── Response schema (Pydantic validation) ──────────────────

class _LLMResponseSchema(BaseModel):
    """Expected shape of the JSON returned by the LLM."""

    intent: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    entities: dict[str, Any] = Field(default_factory=dict)


# ── System prompt ───────────────────────────────────────────

SYSTEM_PROMPT = """\
Tu es le module NLU d'un assistant vocal pour un salon de coiffure haut de gamme \
(Maison Éclat). À partir de la phrase du client, tu dois extraire :

1. **intent** — une des valeurs suivantes exactement :
   - "book" : le client veut prendre un rendez-vous
   - "reschedule" : le client veut déplacer/modifier un rendez-vous existant
   - "cancel" : le client veut annuler un rendez-vous
   - "check_availability" : le client demande les disponibilités sans vouloir réserver
   - "get_info" : le client pose une question sur le salon (adresse, horaires, tarifs, équipe, \
paiement, parking, produits, politique d'annulation, WiFi, bons cadeaux, etc.)
   - "unknown" : impossible de déterminer l'intention

2. **confidence** — un nombre décimal entre 0.0 et 1.0 reflétant ta certitude.

3. **entities** — un objet JSON avec les champs optionnels suivants :
   - "service" : nom du service mentionné (coupe, couleur, balayage, brushing, etc.)
   - "date" : date mentionnée au format YYYY-MM-DD si possible, sinon texte brut
   - "time" : heure mentionnée au format HH:MM si possible, sinon texte brut
   - "booking_id" : identifiant numérique d'un rendez-vous existant si mentionné
   - "employee" : prénom du coiffeur/coiffeuse mentionné(e) (valeurs possibles : Sophie, Karim, Léa, Hugo, Amira)
   - "info_topic" : sujet de la demande d'info (valeurs : address, hours, price, team, payment, \
policy, parking, products, contact, faq_wifi, faq_animals, faq_loyalty, faq_gift, services)

Règles :
- Réponds UNIQUEMENT avec un objet JSON valide, sans texte avant ou après.
- N'invente pas d'entités non mentionnées dans la phrase.
- Si le client mentionne « annuler » même avec un service, l'intent est "cancel".
- Si le client mentionne « déplacer/changer/modifier » un rendez-vous, l'intent est "reschedule".
- Les questions sur l'adresse, les horaires, les prix, l'équipe, le parking, le WiFi, \
les animaux, les bons cadeaux, la fidélité, les produits utilisés → intent "get_info".
"""


# ── Structured result ───────────────────────────────────────

@dataclass(frozen=True)
class LLMIntentResult:
    """Parsed result from the LLM intent classification."""

    intent: VoiceIntent
    confidence: float
    entities: dict[str, Any]
    llm_raw: dict[str, Any]  # Original parsed JSON for debug/audit
    latency_ms: float


# ── Exceptions ──────────────────────────────────────────────

class LLMIntentError(Exception):
    """Base error for LLM intent detection failures."""


class LLMTimeoutError(LLMIntentError):
    """LLM call exceeded timeout."""


class LLMResponseError(LLMIntentError):
    """LLM returned unparseable or structurally invalid JSON."""


class LLMProviderError(LLMIntentError):
    """HTTP-level error from the LLM provider."""


# ── Helpers ─────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    """Mask API key for logging — show first 4 chars only."""
    if len(key) <= 4:
        return "****"
    return key[:4] + "****"


def _parse_llm_response(raw_text: str) -> dict[str, Any]:
    """
    Parse the LLM text response into a structured dict.

    Handles common issues:
    - Markdown fences (```json ... ```)
    - Leading/trailing whitespace
    """
    text = raw_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
        # Remove closing fence
        if text.endswith("```"):
            text = text[:-3].strip()

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise LLMResponseError(f"Expected JSON object, got {type(parsed).__name__}")

    # Validate structure via Pydantic — catches missing/bad fields early
    try:
        _LLMResponseSchema.model_validate(parsed)
    except ValidationError as exc:
        raise LLMResponseError(f"LLM response schema validation failed: {exc}") from exc

    return parsed


def _validate_and_build(parsed: dict[str, Any], latency_ms: float) -> LLMIntentResult:
    """Validate parsed LLM JSON and build a typed result."""
    raw_intent = parsed.get("intent", "unknown")
    if not isinstance(raw_intent, str) or raw_intent not in _VALID_INTENTS:
        raise LLMResponseError(f"Invalid intent value: {raw_intent!r}")

    intent = VoiceIntent(raw_intent)

    # Confidence: clamp to [0.0, 1.0]
    raw_confidence = parsed.get("confidence", 0.5)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    # Entities: must be a dict (or absent)
    raw_entities = parsed.get("entities", {})
    if not isinstance(raw_entities, dict):
        raw_entities = {}

    return LLMIntentResult(
        intent=intent,
        confidence=confidence,
        entities=raw_entities,
        llm_raw=parsed,
        latency_ms=latency_ms,
    )


# ── Core async client ──────────────────────────────────────

async def classify_intent_llm(
    text: str,
    *,
    timeout: float = LLM_TIMEOUT_SECONDS,
    api_key: str | None = None,
    model: str | None = None,
) -> LLMIntentResult:
    """
    Classify user intent via OpenAI chat completion.

    Args:
        text: User utterance to classify.
        timeout: HTTP timeout in seconds.
        api_key: OpenAI API key (defaults to settings.OPENAI_API_KEY).
        model: Model name (defaults to settings.LLM_MODEL).

    Returns:
        LLMIntentResult with intent, confidence, entities.

    Raises:
        LLMTimeoutError: If the call exceeds timeout.
        LLMProviderError: If OpenAI returns a non-2xx status.
        LLMResponseError: If the response cannot be parsed/validated.
        LLMIntentError: For other unexpected failures.
    """
    resolved_key = api_key or settings.OPENAI_API_KEY
    resolved_model = model or settings.LLM_MODEL

    if not resolved_key:
        raise LLMIntentError("OPENAI_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,  # Deterministic for intent classification
        "max_tokens": 256,  # Intent JSON is small
        "response_format": {"type": "json_object"},  # Force JSON mode
    }

    t0 = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(OPENAI_CHAT_URL, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        logger.warning(
            "LLM intent timeout after %.0fms (key=%s, model=%s)",
            latency_ms,
            _mask_key(resolved_key),
            resolved_model,
        )
        raise LLMTimeoutError(f"OpenAI timeout after {latency_ms:.0f}ms") from exc
    except httpx.HTTPError as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        logger.warning(
            "LLM intent HTTP error after %.0fms: %s",
            latency_ms,
            str(exc)[:200],
        )
        raise LLMProviderError(f"HTTP error: {exc}") from exc

    latency_ms = (time.monotonic() - t0) * 1000

    if response.status_code != 200:
        # Log status + truncated body (no secrets in body)
        body_preview = response.text[:300] if response.text else "(empty)"
        logger.warning(
            "LLM intent error: status=%d, latency=%.0fms, body=%s",
            response.status_code,
            latency_ms,
            body_preview,
        )
        raise LLMProviderError(
            f"OpenAI returned {response.status_code}: {body_preview}"
        )

    # Parse the response
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.warning(
            "LLM intent: malformed API response structure after %.0fms",
            latency_ms,
        )
        raise LLMResponseError(f"Malformed OpenAI response: {exc}") from exc

    try:
        parsed = _parse_llm_response(content)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "LLM intent: invalid JSON in completion after %.0fms: %s",
            latency_ms,
            str(exc)[:200],
        )
        raise LLMResponseError(f"Invalid JSON from LLM: {exc}") from exc

    result = _validate_and_build(parsed, latency_ms)

    logger.info(
        "LLM intent: intent=%s confidence=%.2f latency=%.0fms model=%s",
        result.intent.value,
        result.confidence,
        latency_ms,
        resolved_model,
    )
    return result


# ── Availability check ──────────────────────────────────────

def is_llm_available() -> bool:
    """Check whether LLM intent detection is configured and should be used."""
    return (
        settings.LLM_PROVIDER == "openai"
        and bool(settings.OPENAI_API_KEY)
    )
