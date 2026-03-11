"""
Shared test fixtures.

Uses an in-memory SQLite database – no files, no external deps.
Each test function gets a fresh database with seeded reference data.

Phase 4.3: also resets rate-limiter buckets between tests so state
doesn't leak across test boundaries.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.circuit_breaker import stt_circuit_breaker, tts_circuit_breaker
from app.config import settings
from app.database import get_db
from app.models import Base
from app.observability import metrics
from app.rate_limit import _reset_buckets
from app.seed import seed_all
from app.telephony_adapter import idempotency_guard

# ── In-memory test engine ───────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite://"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
test_session_factory = async_sessionmaker(
    test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(scope="session")
def event_loop():
    """Use a single event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def setup_db():
    """Create all tables before each test, drop after.  Reset rate limiter, metrics & circuit breakers."""
    _reset_buckets()
    metrics.reset()
    stt_circuit_breaker.reset()
    tts_circuit_breaker.reset()
    idempotency_guard.reset()

    # Disable LLM provider in tests to prevent real API calls.
    # Tests that need LLM should mock classify_intent_llm explicitly.
    _orig_llm_provider = settings.LLM_PROVIDER
    settings.LLM_PROVIDER = "mock"
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with test_session_factory() as session:
        await seed_all(session)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    _reset_buckets()
    settings.LLM_PROVIDER = _orig_llm_provider


async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        yield session


@pytest.fixture
async def client():
    """Async HTTP test client wired to the FastAPI app with test DB."""
    # Import here to avoid triggering the real lifespan
    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Raw async session for direct DB / slot-engine tests."""
    async with test_session_factory() as session:
        yield session
