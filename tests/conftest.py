import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from send_gate.adapters import MockAdapter  # noqa: E402
from send_gate.gate import Gate  # noqa: E402

PACK = {
    "version": "test-1",
    "reviewed_at": "2026-11-01",
    "max_age_days": 30,
    "hard_max_age_days": 60,
    "quiet_hours": {"timezone": "America/New_York", "start": "21:00", "end": "08:00", "channels": ["sms"]},
    "duplicate": {"same_content_window_hours": 24, "same_recipient_gap_minutes": 10},
    "banned_phrases": [
        {"phrase": "guaranteed", "level": "stop", "reason": "No guarantees."},
        {"phrase": "act now", "level": "stop", "reason": "Pressure language."},
        {"phrase": "free", "level": "caution", "reason": "Check the offer."},
    ],
}


AGENT_TOKEN = "test-agent-token-0001"
APPROVER_TOKEN = "test-approver-token-0001"
AS_AGENT = {"X-Agent-Token": AGENT_TOKEN}
AS_HUMAN = {"X-Approver-Token": APPROVER_TOKEN}


class FakeClock:
    """A clock the tests move by hand. Starts at 10:00 New York time."""

    def __init__(self):
        self.value = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)

    def set_utc(self, *args):
        self.value = datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def pack_path(tmp_path):
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(PACK), encoding="utf-8")
    return path


@pytest.fixture
def adapter(clock):
    return MockAdapter(clock)


@pytest.fixture
def gate(tmp_path, pack_path, adapter, clock):
    g = Gate(str(tmp_path / "gate.db"), str(pack_path), adapter=adapter, clock=clock)
    yield g
    g.close()


@pytest.fixture
def app(gate):
    from send_gate.api import create_app

    return create_app(gate, agent_token=AGENT_TOKEN, approver_token=APPROVER_TOKEN)


@pytest.fixture
def agent(app):
    """HTTP client holding only the agent token."""
    from fastapi.testclient import TestClient

    return TestClient(app, headers=AS_AGENT)


@pytest.fixture
def human(app):
    """HTTP client holding only the approver token."""
    from fastapi.testclient import TestClient

    return TestClient(app, headers=AS_HUMAN)


@pytest.fixture
def anon(app):
    """HTTP client with no token."""
    from fastapi.testclient import TestClient

    return TestClient(app)


def events(gate, draft_id):
    return [e["event"] for e in gate.audit(draft_id)]
