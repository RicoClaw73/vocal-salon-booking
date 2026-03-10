"""
Deterministic intent extraction for the voice pipeline MVP.

Uses keyword matching and simple regex patterns to classify user utterances
into one of: book, reschedule, cancel, check_availability, unknown.

This is intentionally simple — designed to be replaced by an LLM classifier
in later phases while keeping the same interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.voice_schemas import VoiceIntent


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

_BOOK_PATTERNS: list[re.Pattern] = [
    re.compile(r"(réserv|prendre|book|rdv|rendez[\s-]?vous|appointment)", _FLAGS),
    re.compile(r"(je\s+vou[sd]rais|j['\u2019]aimerais|i['\u2019]?d?\s*like).*(coupe|coiffure|couleur|balayage|mèche|brushing|soin|chignon|mariage|barbe)", _FLAGS),
    # "disponibilités pour un [service]" implies booking intent
    re.compile(r"(disponib).*(coupe|coiffure|couleur|balayage|mèche|brushing|soin|chignon|mariage|barbe)", _FLAGS),
]

_RESCHEDULE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(déplac|reschedul|report|chang|modifi|repousser|avancer|move)", _FLAGS),
    re.compile(r"(nouvelle?\s+date|new\s+date|autre\s+(jour|heure|créneau))", _FLAGS),
]

_CANCEL_PATTERNS: list[re.Pattern] = [
    re.compile(r"(annul|cancel|supprim|delete)", _FLAGS),
]

_AVAILABILITY_PATTERNS: list[re.Pattern] = [
    re.compile(r"(disponib|available|libre|free|créneau|slot|quand|when|horaire)", _FLAGS),
    re.compile(r"(ouvert|open)", _FLAGS),
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

    # Availability check — informational queries
    if any(p.search(text) for p in _AVAILABILITY_PATTERNS):
        return IntentResult(intent=VoiceIntent.check_availability, confidence=1.0, entities=entities)

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
    for keyword, category in sorted(_SERVICE_KEYWORDS.items(), key=lambda x: len(x[0]), reverse=True):
        if keyword in text_lower:
            entities["service_keyword"] = keyword
            entities["service_category"] = category
            break

    # Gender hints
    if re.search(r"\b(homme|man|masculin|monsieur|garçon)\b", text_lower):
        entities["genre"] = "M"
    elif re.search(r"\b(femme|woman|féminin|madame|fille)\b", text_lower):
        entities["genre"] = "F"

    # Hair length hints
    if re.search(r"(courts?\b|short)", text_lower):
        entities["longueur"] = "court"
    elif re.search(r"(mi-long|medium)", text_lower):
        entities["longueur"] = "mi-long"
    elif re.search(r"(?<!mi-)\b(longs?\b)", text_lower):
        entities["longueur"] = "long"

    return entities
