"""Token rules: who may call what, and that nothing protected is left open."""

import pytest
from fastapi.testclient import TestClient

from conftest import AGENT_TOKEN, APPROVER_TOKEN, AS_AGENT, AS_HUMAN
from send_gate import api
from send_gate.api import ConfigError, create_app

DRAFT = {
    "agent": "scheduler-bot",
    "channel": "sms",
    "recipient": "555-555-0142",
    "body": "Hi Alex, your visit is booked for Tuesday at 9am.",
}
OPT_OUT = {"channel": "sms", "recipient": "555-555-0163", "source": "STOP reply"}

# The only routes anyone may call without a token. They expose no draft data.
PUBLIC = {"/", "/healthz", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}

AGENT_ONLY = [("POST", "/drafts"), ("PATCH", "/drafts/1")]
APPROVER_ONLY = [("POST", "/drafts/1/approve"), ("POST", "/drafts/1/reject"), ("POST", "/opt-outs")]
EITHER = [
    ("POST", "/drafts/1/recheck"),
    ("POST", "/drafts/1/send"),
    ("POST", "/drafts/1/verify"),
    ("GET", "/drafts"),
    ("GET", "/drafts/1"),
    ("GET", "/drafts/1/audit"),
    ("GET", "/audit"),
    ("GET", "/audit/verify"),
    ("GET", "/opt-outs"),
    ("GET", "/rule-pack"),
]
PROTECTED = AGENT_ONLY + APPROVER_ONLY + EITHER

BODIES = {
    "/drafts": DRAFT,
    "/drafts/1": {"actor": "scheduler-bot", "body": "Hi Alex, see you Tuesday."},
    "/drafts/1/approve": {"approver": "Jordan Example", "content_hash": "0" * 64},
    "/drafts/1/reject": {"approver": "Jordan Example", "reason": "no"},
    "/drafts/1/send": {"actor": "scheduler-bot"},
    "/drafts/1/verify": {"actor": "scheduler-bot"},
    "/opt-outs": OPT_OUT,
}


def call(client, method, path, headers=None):
    body = BODIES.get(path) if method != "GET" else None
    return client.request(method, path, json=body, headers=headers or {})


def snapshot(gate):
    """Everything a refused call must leave untouched."""
    return (gate.list(), gate.list_opt_outs(), gate.audit())


@pytest.fixture
def seeded(gate, agent):
    """One pending draft, id 1, so the routes above point at something real."""
    assert agent.post("/drafts", json=DRAFT).json()["id"] == 1
    return gate


def test_the_route_tables_cover_every_route_the_app_serves(app):
    served = set()
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            if method not in ("HEAD", "OPTIONS") and route.path not in PUBLIC:
                served.add((method, route.path.replace("{draft_id}", "1")))
    assert served == set(PROTECTED)


@pytest.mark.parametrize("method,path", PROTECTED)
def test_a_missing_token_is_refused_everywhere_one_is_required(seeded, anon, method, path):
    before = snapshot(seeded)
    response = call(anon, method, path)
    assert response.status_code == 401
    assert response.json()["error"] in ("agent_token_required", "approver_token_required", "token_required")
    assert snapshot(seeded) == before


@pytest.mark.parametrize("method,path", PROTECTED)
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Agent-Token": "not-the-agent-token"},
        {"X-Approver-Token": "not-the-approver-token"},
        {"X-Agent-Token": "", "X-Approver-Token": ""},
        {"X-Agent-Token": AGENT_TOKEN[:-1], "X-Approver-Token": APPROVER_TOKEN + "x"},
        {"Authorization": f"Bearer {APPROVER_TOKEN}"},
    ],
)
def test_a_wrong_token_is_refused_everywhere(seeded, anon, method, path, headers):
    before = snapshot(seeded)
    assert call(anon, method, path, headers).status_code == 401
    assert snapshot(seeded) == before


@pytest.mark.parametrize("method,path", APPROVER_ONLY)
@pytest.mark.parametrize(
    "headers", [AS_AGENT, {"X-Approver-Token": AGENT_TOKEN}, {**AS_AGENT, "X-Approver-Token": AGENT_TOKEN}]
)
def test_the_agent_token_is_refused_on_approver_actions(seeded, anon, method, path, headers):
    before = snapshot(seeded)
    response = call(anon, method, path, headers)
    assert response.status_code == 401 and response.json()["error"] == "approver_token_required"
    assert snapshot(seeded) == before


def test_an_agent_cannot_approve_its_own_draft_by_any_route(seeded, agent, anon, human):
    draft = agent.get("/drafts/1").json()
    body = {"approver": "Robin Example", "content_hash": draft["content_hash"], "acknowledge_cautions": True}

    assert agent.post("/drafts/1/approve", json=body).status_code == 401
    assert anon.post("/drafts/1/approve", json=body, headers={"X-Approver-Token": AGENT_TOKEN}).status_code == 401
    assert agent.post("/drafts/1/send", json={"actor": "scheduler-bot"}).json()["error"] == "not_approved"
    assert agent.get("/drafts/1").json()["status"] == "pending"
    assert agent.get("/drafts/1").json()["approval"] is None
    assert seeded.adapter.outbox == []

    assert human.post("/drafts/1/approve", json=body).status_code == 200
    assert agent.post("/drafts/1/send", json={"actor": "scheduler-bot"}).json()["status"] == "verified"


@pytest.mark.parametrize("method,path", AGENT_ONLY)
@pytest.mark.parametrize("headers", [AS_HUMAN, {"X-Agent-Token": APPROVER_TOKEN}])
def test_the_approver_token_cannot_write_drafts(seeded, anon, method, path, headers):
    before = snapshot(seeded)
    response = call(anon, method, path, headers)
    assert response.status_code == 401 and response.json()["error"] == "agent_token_required"
    assert snapshot(seeded) == before


@pytest.mark.parametrize("method,path", EITHER)
@pytest.mark.parametrize("headers", [AS_AGENT, AS_HUMAN])
def test_reads_and_follow_through_accept_either_token(seeded, anon, method, path, headers):
    # 200 for reads and recheck; 409 where the draft is not in the right state.
    # Either way the token was accepted.
    assert call(anon, method, path, headers).status_code in (200, 409)


def test_each_token_does_its_own_job(seeded, agent, human):
    assert agent.patch("/drafts/1", json=BODIES["/drafts/1"]).status_code == 200
    assert human.post("/opt-outs", json=OPT_OUT).status_code == 201
    draft = human.get("/drafts/1").json()
    approve = {"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    assert human.post("/drafts/1/approve", json=approve).status_code == 200
    assert human.post("/drafts/1/send", json={"actor": "Jordan Example"}).json()["status"] == "verified"


def test_public_routes_need_no_token_and_show_no_drafts(seeded, anon):
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/").status_code == 200
    assert DRAFT["body"] not in anon.get("/").text
    assert anon.get("/openapi.json").status_code == 200


@pytest.mark.parametrize(
    "agent_token,approver_token",
    [
        ("", ""),
        (AGENT_TOKEN, ""),
        ("", APPROVER_TOKEN),
        ("short", APPROVER_TOKEN),
        (AGENT_TOKEN, "short"),
        (AGENT_TOKEN, AGENT_TOKEN),
    ],
)
def test_the_app_refuses_to_start_without_two_distinct_tokens(gate, agent_token, approver_token):
    with pytest.raises(ConfigError):
        create_app(gate, agent_token=agent_token, approver_token=approver_token)


def test_tokens_come_from_the_environment(gate, monkeypatch):
    monkeypatch.delenv("SEND_GATE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("SEND_GATE_APPROVER_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        create_app(gate)

    monkeypatch.setenv("SEND_GATE_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("SEND_GATE_APPROVER_TOKEN", APPROVER_TOKEN)
    client = TestClient(create_app(gate))
    assert client.post("/drafts", json=DRAFT).status_code == 401
    assert client.post("/drafts", json=DRAFT, headers=AS_AGENT).status_code == 201


def test_a_bad_start_creates_no_database(tmp_path, monkeypatch):
    db = tmp_path / "never.db"
    monkeypatch.setenv("SEND_GATE_DB", str(db))
    monkeypatch.delenv("SEND_GATE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("SEND_GATE_APPROVER_TOKEN", APPROVER_TOKEN)
    with pytest.raises(ConfigError):
        create_app()
    assert not db.exists()


def test_token_comparison_handles_odd_input():
    assert api._matches(AGENT_TOKEN, AGENT_TOKEN)
    assert not api._matches(None, AGENT_TOKEN)
    assert not api._matches("", AGENT_TOKEN)
    assert not api._matches("tést-agent-token-0001", AGENT_TOKEN)
    assert not api._matches(AGENT_TOKEN + " ", AGENT_TOKEN)
