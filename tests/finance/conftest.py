import os

# The Finance Agent reads its settings when imported, so the test configuration is set first.
os.environ["FINANCE_DATABASE_URL"] = os.environ.get("TEST_FINANCE_DATABASE_URL", "postgresql+asyncpg://expense:expense@localhost:5432/expense_finance_test")
os.environ["FINANCE_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["FINANCE_PUBLIC_URL"] = "http://finance.test"
os.environ["FINANCE_TRUSTED_ISSUER"] = "http://testserver"  # the tracker's issuer in the tests (MCP_PUBLIC_URL)
os.environ["FINANCE_TRACKER_WEBHOOK_URL"] = "http://testserver/a2a/webhook"
os.environ["FINANCE_CODEVECTOR_API_KEY"] = ""
os.environ["FINANCE_CURRENCY_SYMBOL"] = "₹"
os.environ["FINANCE_ALLOWED_MANAGER_IDS"] = ""

from collections import deque  # noqa: E402

import pytest  # noqa: E402

from finance_agent.db.models import Base  # noqa: E402
from finance_agent.db.session import engine  # noqa: E402

RULES = [
    {"id": "hotel", "name": "Hotel stays", "keywords": ["room", "stay", "lodging"], "cap_amount": 6000, "cap_unit": "per_night", "receipt_over": 500},
    {"id": "meals", "name": "Meals", "keywords": ["lunch", "dinner", "food"], "cap_amount": 1000, "cap_unit": "per_day", "conditions": "only during business travel"},
    {"id": "local_travel", "name": "Local travel", "keywords": ["cab", "taxi", "auto"]},
    {"id": "internet", "name": "Internet", "keywords": ["wifi", "broadband"], "percent": 50},
    {"id": "training", "name": "Training", "keywords": ["course", "certification"], "needs_preapproval": True},
    {"id": "gym", "name": "Gym membership", "keywords": ["gym"], "covered": False},
]


class FakeLLM:
    """Answers with what the test queued, in order (and records what it was asked). With nothing queued, `fallback` decides."""

    def __init__(self, fallback=None):
        self.queue: deque = deque()
        self.calls: list[tuple[str, str]] = []
        self.timeouts: list[int | None] = []
        self.fallback = fallback

    def say(self, *answers: dict) -> "FakeLLM":
        self.queue.extend(answers)
        return self

    async def json(self, system: str, user: str, *, timeout: int | None = None) -> dict:
        self.calls.append((system, user))
        self.timeouts.append(timeout)
        if self.queue:
            out = self.queue.popleft()
        elif self.fallback is not None:
            out = self.fallback(system, user)
        else:
            raise AssertionError(f"the LLM was called with nothing queued:\n{user[:300]}")
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture(scope="session", autouse=True)
async def finance_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture
def llm():
    return FakeLLM()
