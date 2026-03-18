"""Tests for deterministic intent extraction (app.intent)."""

from __future__ import annotations

import pytest

from app.intent import extract_intent, extract_entities
from app.voice_schemas import VoiceIntent


# ── Intent classification ────────────────────────────────────

class TestIntentClassification:
    """Test that utterances map to the correct intent."""

    @pytest.mark.parametrize("text,expected", [
        # French booking intents
        ("Je voudrais prendre rendez-vous pour une coupe", VoiceIntent.book),
        ("J'aimerais réserver un créneau pour une couleur", VoiceIntent.book),
        ("Je veux un rdv pour un brushing", VoiceIntent.book),
        ("Est-ce qu'il y a des disponibilités pour un balayage ?", VoiceIntent.book),
        # English booking intents
        ("I'd like to book a haircut", VoiceIntent.book),
        ("I want to make an appointment", VoiceIntent.book),
    ])
    def test_book_intent(self, text: str, expected: VoiceIntent):
        result = extract_intent(text)
        assert result.intent == expected
        assert result.confidence == 1.0

    @pytest.mark.parametrize("text,expected", [
        ("Je voudrais déplacer mon rendez-vous", VoiceIntent.reschedule),
        ("Peut-on changer la date de ma réservation ?", VoiceIntent.reschedule),
        ("I need to reschedule my appointment", VoiceIntent.reschedule),
        ("Je souhaite reporter mon rdv à une autre date", VoiceIntent.reschedule),
        ("Est-ce que je peux modifier mon rendez-vous ?", VoiceIntent.reschedule),
    ])
    def test_reschedule_intent(self, text: str, expected: VoiceIntent):
        result = extract_intent(text)
        assert result.intent == expected
        assert result.confidence == 1.0

    @pytest.mark.parametrize("text,expected", [
        ("Je voudrais annuler mon rendez-vous", VoiceIntent.cancel),
        ("Annuler la réservation #5", VoiceIntent.cancel),
        ("I want to cancel my booking", VoiceIntent.cancel),
        ("Supprimez mon rendez-vous s'il vous plaît", VoiceIntent.cancel),
    ])
    def test_cancel_intent(self, text: str, expected: VoiceIntent):
        result = extract_intent(text)
        assert result.intent == expected
        assert result.confidence == 1.0

    @pytest.mark.parametrize("text,expected", [
        # Slot availability — check_availability
        ("Y a-t-il des créneaux libres jeudi ?", VoiceIntent.check_availability),
        ("Êtes-vous disponible vendredi matin ?", VoiceIntent.check_availability),
        # Hours/opening queries — correctly routed to get_info
        ("Quand est-ce que le salon est ouvert ?", VoiceIntent.get_info),
        ("Quels sont vos horaires ?", VoiceIntent.get_info),
    ])
    def test_availability_and_info_intent(self, text: str, expected: VoiceIntent):
        result = extract_intent(text)
        assert result.intent == expected

    @pytest.mark.parametrize("text", [
        "Bonjour",
        "Merci beaucoup",
        "Au revoir",
        "Comment ça va ?",
    ])
    def test_unknown_intent(self, text: str):
        result = extract_intent(text)
        assert result.intent == VoiceIntent.unknown
        assert result.confidence == 0.0

    def test_cancel_priority_over_book(self):
        """Cancel should win when both cancel and book keywords are present."""
        result = extract_intent("Je voudrais annuler mon rendez-vous de coupe")
        assert result.intent == VoiceIntent.cancel

    def test_reschedule_priority_over_availability(self):
        """Reschedule should win over availability check."""
        result = extract_intent("Je voudrais changer mon rendez-vous, quand êtes-vous disponible ?")
        assert result.intent == VoiceIntent.reschedule


# ── Entity extraction ────────────────────────────────────────

class TestEntityExtraction:

    def test_extract_iso_date(self):
        entities = extract_entities("Je voudrais un rendez-vous le 2025-03-15")
        assert entities["date"] == "2025-03-15"

    def test_extract_eu_date(self):
        entities = extract_entities("Le 15/03/2025 s'il vous plaît")
        assert entities["date"] == "15/03/2025"

    def test_extract_time_h_format(self):
        entities = extract_entities("À 14h30 si possible")
        assert entities["time"] == "14:30"

    def test_extract_time_h_no_minutes(self):
        entities = extract_entities("À 9h s'il vous plaît")
        assert entities["time"] == "09:00"

    def test_extract_time_colon_format(self):
        entities = extract_entities("à 14:30")
        assert entities["time"] == "14:30"

    def test_extract_booking_id(self):
        entities = extract_entities("Annuler la réservation #42")
        assert entities["booking_id"] == 42

    def test_extract_booking_id_no_hash(self):
        entities = extract_entities("Mon rendez-vous 7 doit être modifié")
        assert entities["booking_id"] == 7

    def test_extract_service_keyword_coupe(self):
        entities = extract_entities("Je veux une coupe")
        assert entities["service_keyword"] == "coupe"
        assert entities["service_category"] == "coupe"

    def test_extract_service_keyword_couleur(self):
        entities = extract_entities("Je voudrais une coloration")
        assert entities["service_keyword"] == "coloration"
        assert entities["service_category"] == "couleur"

    def test_extract_genre_homme(self):
        entities = extract_entities("Coupe pour homme")
        assert entities["genre"] == "M"

    def test_extract_genre_femme(self):
        entities = extract_entities("Coupe pour femme cheveux courts")
        assert entities["genre"] == "F"
        assert entities["longueur"] == "court"

    def test_extract_longueur_long(self):
        entities = extract_entities("Couleur cheveux long")
        assert entities["longueur"] == "long"

    def test_no_entities(self):
        entities = extract_entities("Bonjour")
        assert entities == {}

    def test_combined_extraction(self):
        """Test multiple entities from a single utterance."""
        result = extract_intent(
            "Je voudrais réserver une coupe femme le 2025-04-10 à 14h30"
        )
        assert result.intent == VoiceIntent.book
        assert result.entities["date"] == "2025-04-10"
        assert result.entities["time"] == "14:30"
        assert result.entities["service_category"] == "coupe"
        assert result.entities["genre"] == "F"
