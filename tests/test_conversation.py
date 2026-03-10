"""Tests for the in-memory conversation state manager (app.conversation)."""

from __future__ import annotations

from app.conversation import ConversationManager, ConversationState
from app.voice_schemas import SessionStatus, VoiceIntent


class TestConversationManager:

    def test_create_session(self):
        mgr = ConversationManager()
        state = mgr.create_session(client_name="Alice", client_phone="+33600000000")
        assert isinstance(state, ConversationState)
        assert len(state.session_id) == 12
        assert state.status == SessionStatus.active
        assert state.client_name == "Alice"
        assert state.turns == 0

    def test_get_session(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        retrieved = mgr.get_session(state.session_id)
        assert retrieved is state

    def test_get_missing_session(self):
        mgr = ConversationManager()
        assert mgr.get_session("nonexistent") is None

    def test_end_session(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        ended = mgr.end_session(state.session_id)
        assert ended.status == SessionStatus.completed
        assert mgr.active_count == 0

    def test_end_missing_session(self):
        mgr = ConversationManager()
        assert mgr.end_session("nonexistent") is None

    def test_remove_session(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        mgr.remove_session(state.session_id)
        assert mgr.get_session(state.session_id) is None

    def test_active_count(self):
        mgr = ConversationManager()
        mgr.create_session()
        mgr.create_session()
        s3 = mgr.create_session()
        mgr.end_session(s3.session_id)
        assert mgr.active_count == 2

    def test_list_sessions(self):
        mgr = ConversationManager()
        mgr.create_session()
        mgr.create_session()
        assert len(mgr.list_sessions()) == 2


class TestConversationState:

    def test_increment_turn(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        state.increment_turn()
        state.increment_turn()
        assert state.turns == 2

    def test_update_draft(self):
        mgr = ConversationManager()
        state = mgr.create_session(client_name="Bob")
        state.update_draft(service_id="coupe_homme", date="2025-04-10")
        assert state.booking_draft.service_id == "coupe_homme"
        assert state.booking_draft.date == "2025-04-10"
        # client_name should auto-propagate
        assert state.booking_draft.client_name == "Bob"

    def test_missing_booking_fields_all_missing(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        missing = state.missing_booking_fields()
        assert "service_id" in missing
        assert "date" in missing
        assert "time" in missing

    def test_missing_booking_fields_partial(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        state.update_draft(service_id="coupe_homme")
        missing = state.missing_booking_fields()
        assert "service_id" not in missing
        assert "date" in missing
        assert "time" in missing

    def test_missing_booking_fields_none(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        state.update_draft(service_id="coupe_homme", date="2025-04-10", time="14:30")
        assert state.missing_booking_fields() == []

    def test_duration_seconds(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        # Duration should be >= 0
        assert state.duration_seconds >= 0.0

    def test_update_draft_ignores_unknown_fields(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        state.update_draft(nonexistent_field="value")
        # Should not raise; draft unchanged
        assert state.booking_draft.service_id is None

    def test_current_intent_default_none(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        assert state.current_intent is None

    def test_set_intent(self):
        mgr = ConversationManager()
        state = mgr.create_session()
        state.current_intent = VoiceIntent.book
        assert state.current_intent == VoiceIntent.book
