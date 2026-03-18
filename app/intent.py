"""
Intent extraction for the voice pipeline.

Two engines are available:

1. **Rule-based** (deterministic) — keyword matching and regex patterns.
   Always available, no external dependencies.  Used as fallback.

2. **LLM-first** (Phase 6) — OpenAI GPT-4o structured classification.
   Activated when ``LLM_PROVIDER=openai`` and ``OPENAI_API_KEY`` is set.
   Falls back transparently to rule-based on any error/timeout.

Public API:
  - ``extract_intent(text)``       — synchronous, rule-based only (backward compat)
  - ``extract_intent_async(text)``  — async, LLM-first with rule-based fallback
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.voice_schemas import VoiceIntent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentResult:
    """Result of intent extraction from user text."""
    intent: VoiceIntent
    confidence: float  # 0.0–1.0 (deterministic: 1.0 for match, 0.0 for unknown)
    entities: dict  # Extracted entities (date, time, service keywords, etc.)


# ── Keyword patterns (French + English for dev/testing) ──────
# Note: \b doesn't work well with accented chars in Python re.
# We use (?i) and simple substring search via re.search which is sufficient.

_FLAGS = re.IGNORECASE | re.UNICODE

_SERVICES_RE = r"coupe|coiffure|couleur|balayage|mèche|brushing|soin|chignon|mariage|barbe"

_BOOK_PATTERNS: list[re.Pattern] = [
    re.compile(r"(réserv|prendre|book|rdv|rendez[\s-]?vous|appointment)", _FLAGS),
    re.compile(
        rf"(je\s+vou[sd]rais|je\s+veux|j['\u2019]aimerais|j['\u2019]ai\s+besoin|i['\u2019]?d?\s*like).*({_SERVICES_RE})",
        _FLAGS,
    ),
    # "disponibilités pour un [service]" implies booking intent
    re.compile(rf"(disponib).*({_SERVICES_RE})", _FLAGS),
]

_RESCHEDULE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(déplac|reschedul|report|chang|modifi|repousser|avancer|move)", _FLAGS),
    re.compile(r"(nouvelle?\s+date|new\s+date|autre\s+(jour|heure|créneau))", _FLAGS),
]

_CANCEL_PATTERNS: list[re.Pattern] = [
    re.compile(r"(annul|cancel|supprim|delete)", _FLAGS),
]

_AVAILABILITY_PATTERNS: list[re.Pattern] = [
    re.compile(r"(disponib|available|libre|free|créneau|slot)", _FLAGS),
]

_INFO_PATTERNS: list[re.Pattern] = [
    re.compile(r"(adresse|o[uù]\s+(êtes|se\s+trou|trouver)|chemin|itinéraire|locat)", _FLAGS),
    re.compile(r"(horaire|ouvr|ferm[eé]|fermeture|ouverture|quand\b.*ouv)", _FLAGS),
    re.compile(r"(tarif|prix|co[uû]t|combien|cher|budget)", _FLAGS),
    re.compile(r"(équipe|coiffeur|coiffeuse|qui\s+(est|sont|fait|travail)|personnel|staff)", _FLAGS),
    re.compile(r"(paiement|payer|espèce|chèque|apple.pay|google.pay|carte\s+bancaire|moyen\s+de\s+paiement|acceptez.vous)", _FLAGS),
    re.compile(r"(politique\s+annulation|conditions\s+annulation|frais\s+annulation|acompte)", _FLAGS),
    re.compile(r"(produit|marque|wella|kérastase|olaplex)", _FLAGS),
    re.compile(r"(parking|vélo|vélib|bus|rer\b|métro|comment\s+venir)", _FLAGS),
    re.compile(r"(téléphone|numéro|email|instagram|facebook|joindre|nous\s+appeler)", _FLAGS),
    re.compile(r"(wifi|wi-fi|animaux?|extensions?|fidélité|bon\s+cadeau)", _FLAGS),
    re.compile(r"(que\s+propos|qu['\u2019]est[- ]ce\s+que\s+vous|catalogue\s+de\s+prestation)", _FLAGS),
]

# ── Entity extraction patterns ───────────────────────────────

_DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"           # ISO: 2025-03-15
    r"|"
    r"\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b"  # FR/EU: 15/03/2025
)

_TIME_PATTERN = re.compile(
    r"\b(\d{1,2})\s*[hH:]\s*(\d{2})?\b"  # 14h30, 14H00, 14:30, 9h
)

_BOOKING_ID_PATTERN = re.compile(
    r"\b(?:réservation|booking|rdv|rendez[\s-]?vous)\s*#?\s*(\d+)\b", re.IGNORECASE
)

# Employee first-name → display-name mapping (case-insensitive lookup)
_EMPLOYEE_FIRST_NAMES: dict[str, str] = {
    "sophie": "Sophie",
    "karim": "Karim",
    "léa": "Léa",
    "lea": "Léa",
    "hugo": "Hugo",
    "amira": "Amira",
}

# Service keyword → service_id prefix mapping (fuzzy match for MVP)
_SERVICE_KEYWORDS: dict[str, str] = {
    "coupe": "coupe",
    "haircut": "coupe",
    "couleur": "couleur",
    "color": "couleur",
    "coloration": "couleur",
    "balayage": "balayage",
    "highlight": "balayage",
    "mèche": "meches",
    "meche": "meches",
    "brushing": "brushing",
    "blowout": "brushing",
    "blow-dry": "brushing",
    "soin": "soin",
    "treatment": "soin",
    "chignon": "chignon",
    "updo": "chignon",
    "mariage": "mariage",
    "wedding": "mariage",
    "barbe": "barbe",
    "beard": "barbe",
    "permanente": "permanente",
    "perm": "permanente",
    "décoloration": "decoloration",
    "bleach": "decoloration",
}


# ── Public API ───────────────────────────────────────────────

def extract_intent(text: str) -> IntentResult:
    """
    Extract the primary intent and entities from a user utterance.

    Priority order (first match wins):
      1. cancel     — highest priority (destructive action)
      2. reschedule — modification intent
      3. book       — creation intent
      4. check_availability — informational
      5. unknown    — fallback

    Returns IntentResult with intent, confidence, and extracted entities.
    """
    entities = _extract_entities(text)

    # Cancel has highest priority (unambiguous destructive keyword)
    if any(p.search(text) for p in _CANCEL_PATTERNS):
        return IntentResult(intent=VoiceIntent.cancel, confidence=1.0, entities=entities)

    # Reschedule — modifier keywords
    if any(p.search(text) for p in _RESCHEDULE_PATTERNS):
        return IntentResult(intent=VoiceIntent.reschedule, confidence=1.0, entities=entities)

    # Book — creation keywords
    if any(p.search(text) for p in _BOOK_PATTERNS):
        # Disambiguate: if user just asks "when is available" without booking verbs,
        # it might be availability check rather than booking.
        # Heuristic: if they mention a service or say "prendre rendez-vous", it's book.
        return IntentResult(intent=VoiceIntent.book, confidence=1.0, entities=entities)

    # Availability check — informational queries about slots
    if any(p.search(text) for p in _AVAILABILITY_PATTERNS):
        return IntentResult(
            intent=VoiceIntent.check_availability, confidence=1.0, entities=entities,
        )

    # Salon info — questions about address, hours, price, team, etc.
    if any(p.search(text) for p in _INFO_PATTERNS):
        return IntentResult(
            intent=VoiceIntent.get_info, confidence=1.0, entities=entities,
        )

    return IntentResult(intent=VoiceIntent.unknown, confidence=0.0, entities=entities)


def extract_entities(text: str) -> dict:
    """Public wrapper for entity extraction (for testing / reuse)."""
    return _extract_entities(text)


# ── Internal helpers ─────────────────────────────────────────

def _extract_entities(text: str) -> dict:
    """Pull structured data from free text."""
    entities: dict = {}

    # Date
    date_match = _DATE_PATTERN.search(text)
    if date_match:
        entities["date"] = date_match.group(1) or date_match.group(2)

    # Time
    time_match = _TIME_PATTERN.search(text)
    if time_match:
        hour = time_match.group(1)
        minute = time_match.group(2) or "00"
        entities["time"] = f"{int(hour):02d}:{minute}"

    # Booking ID
    bid_match = _BOOKING_ID_PATTERN.search(text)
    if bid_match:
        entities["booking_id"] = int(bid_match.group(1))

    # Service keywords (check longest keywords first to avoid partial matches)
    text_lower = text.lower()
    sorted_kw = sorted(_SERVICE_KEYWORDS.items(), key=lambda x: len(x[0]), reverse=True)
    for keyword, category in sorted_kw:
        if keyword in text_lower:
            entities["service_keyword"] = keyword
            entities["service_category"] = category
            break

    # Gender hints
    if re.search(r"\b(homme|man|masculin|monsieur|garçon)\b", text_lower):
        entities["genre"] = "M"
    elif re.search(r"\b(femme|woman|féminin|madame|fille)\b", text_lower):
        entities["genre"] = "F"

    # Employee preference ("avec Sophie", "je veux Léa", "Karim si possible", etc.)
    for normalized, display in _EMPLOYEE_FIRST_NAMES.items():
        if re.search(rf"\b{re.escape(normalized)}\b", text_lower):
            entities["employee_name"] = display
            break

    # Info topic hints (for get_info intent routing)
    if re.search(r"(adresse|o[uù]\s+(êtes|se\s+trou|trouver)|chemin|itinéraire|localisation)", text_lower):
        entities["info_topic"] = "address"
    elif re.search(r"(horaire|quand\s+ouvr|quand\s+ferm|heure\s+d[' \u2019]ouverture)", text_lower):
        entities["info_topic"] = "hours"
    elif re.search(r"(tarif|prix|co[uû]t|combien|cher|budget)", text_lower):
        entities["info_topic"] = "price"
    elif re.search(r"(équipe|qui\s+(est|sont|fait|travail)|personnel|staff)", text_lower):
        entities["info_topic"] = "team"
    elif re.search(r"(paiement|payer|espèce|chèque|apple.pay|google.pay|carte\s+bancaire|moyen\s+de\s+paiement)", text_lower):
        entities["info_topic"] = "payment"
    elif re.search(r"(annulation|politique|conditions?|retard|acompte)", text_lower):
        entities["info_topic"] = "policy"
    elif re.search(r"(parking|vélo|vélib|bus|rer\b|métro|comment\s+venir)", text_lower):
        entities["info_topic"] = "parking"
    elif re.search(r"(produit|marque|wella|kérastase|olaplex)", text_lower):
        entities["info_topic"] = "products"
    elif re.search(r"(votre\s+téléphone|votre\s+numéro|email|instagram|facebook|nous\s+contacter|nous\s+joindre)", text_lower):
        entities["info_topic"] = "contact"
    elif re.search(r"(wifi|wi-fi)", text_lower):
        entities["info_topic"] = "faq_wifi"
    elif re.search(r"(animaux?|chien|chat)", text_lower):
        entities["info_topic"] = "faq_animals"
    elif re.search(r"(fidélité|carte\s+fid)", text_lower):
        entities["info_topic"] = "faq_loyalty"
    elif re.search(r"(bon\s+cadeau|gift\s+card|cadeau)", text_lower):
        entities["info_topic"] = "faq_gift"

    # Hair length hints
    if re.search(r"(courts?\b|short)", text_lower):
        entities["longueur"] = "court"
    elif re.search(r"(mi-long|medium)", text_lower):
        entities["longueur"] = "mi-long"
    elif re.search(r"(?<!mi-)\b(longs?\b)", text_lower):
        entities["longueur"] = "long"

    return entities


# ── Async LLM-first dispatcher ──────────────────────────────

async def extract_intent_async(text: str) -> IntentResult:
    """
    Async intent extraction — LLM-first with rule-based fallback.

    When LLM is configured (LLM_PROVIDER=openai + OPENAI_API_KEY set):
      1. Call GPT-4o for structured intent classification.
      2. If successful, merge LLM entities with rule-based entities
         (rule-based entities fill gaps the LLM might miss, e.g. service_category).
      3. On ANY failure (timeout, invalid response, network error),
         fall back transparently to the deterministic engine.

    When LLM is NOT configured:
      Delegates directly to the synchronous rule-based ``extract_intent()``.
    """
    from app.llm_intent import LLMIntentError, classify_intent_llm, is_llm_available

    # Fast path: LLM not configured → rule-based only
    if not is_llm_available():
        return extract_intent(text)

    # LLM-first path
    try:
        llm_result = await classify_intent_llm(text)
    except LLMIntentError as exc:
        logger.warning("LLM intent fallback to rule-based: %s", exc)
        return extract_intent(text)
    except Exception as exc:
        # Catch-all: never let an unexpected LLM error break the voice pipeline
        logger.error("LLM intent unexpected error, falling back: %s", exc)
        return extract_intent(text)

    # Merge: start with rule-based entities (service_category, genre, longueur, etc.)
    # then overlay LLM-extracted entities on top.
    rule_entities = _extract_entities(text)

    merged_entities = {**rule_entities}

    # Map LLM entity names to our internal names
    llm_ent = llm_result.entities
    if "service" in llm_ent and llm_ent["service"]:
        merged_entities.setdefault("service_keyword", llm_ent["service"])
    if "date" in llm_ent and llm_ent["date"]:
        merged_entities["date"] = llm_ent["date"]
    if "time" in llm_ent and llm_ent["time"]:
        merged_entities["time"] = llm_ent["time"]
    if "booking_id" in llm_ent and llm_ent["booking_id"]:
        try:
            merged_entities["booking_id"] = int(llm_ent["booking_id"])
        except (TypeError, ValueError):
            pass  # Keep rule-based booking_id if LLM gave garbage
    if "employee" in llm_ent and llm_ent["employee"]:
        merged_entities.setdefault("employee_name", llm_ent["employee"])
    if "info_topic" in llm_ent and llm_ent["info_topic"]:
        merged_entities.setdefault("info_topic", llm_ent["info_topic"])

    return IntentResult(
        intent=llm_result.intent,
        confidence=llm_result.confidence,
        entities=merged_entities,
    )
