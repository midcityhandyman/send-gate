import pytest
from fastapi.testclient import TestClient

from send_gate.api import create_app

DRAFT = {
    "agent": "scheduler-bot",
    "channel": "sms",
    "recipient": "555-555-0142",
    "body": "Hi Alex, your visit is booked for Tuesday at 9am.",
    "note": "appointment confirmation",
}


@pytest.fixture
def client(gate):
    return TestClient(create_app(gate, approver_token=""))


def test_full_flow_over_http(client):
    created = client.post("/drafts", json=DRAFT)
    assert created.status_code == 201
    draft = created.json()
    assert draft["verdict"] == "ok" and draft["recipient"] == "+15555550142"

    pending = client.get("/drafts", params={"status": "pending"}).json()
    assert [d["id"] for d in pending] == [draft["id"]]

    approve = client.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    assert approve.status_code == 200 and approve.json()["status"] == "approved"

    sent = client.post(f"/drafts/{draft['id']}/send", json={"actor": "scheduler-bot"}).json()
    assert sent["status"] == "verified"
    assert sent["sends"][0]["provider_message_id"].startswith("mock-")

    trail = client.get(f"/drafts/{draft['id']}/audit").json()
    assert trail[-1]["event"] == "readback_verified"
    assert client.get("/audit/verify").json()["ok"] is True
    assert len(client.get("/audit").json()) == len(trail)


def test_edit_over_http_voids_approval_and_send_is_refused(client):
    draft = client.post("/drafts", json=DRAFT).json()
    client.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    edited = client.patch(f"/drafts/{draft['id']}", json={"actor": "scheduler-bot", "body": DRAFT["body"] + " PS"})
    assert edited.json()["status"] == "pending" and edited.json()["approval"] is None

    refused = client.post(f"/drafts/{draft['id']}/send", json={"actor": "scheduler-bot"})
    assert refused.status_code == 409 and refused.json()["error"] == "not_approved"

    stale = client.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    assert stale.status_code == 409 and stale.json()["error"] == "text_changed"
    assert stale.json()["current_hash"] == edited.json()["content_hash"]


def test_opt_out_endpoints_and_stop_verdict(client):
    added = client.post("/opt-outs", json={"channel": "sms", "recipient": "(555) 555-0142", "source": "STOP reply"})
    assert added.status_code == 201 and added.json()["added"] is True
    assert client.get("/opt-outs").json()[0]["recipient"] == "+15555550142"

    draft = client.post("/drafts", json=DRAFT).json()
    assert draft["verdict"] == "stop"
    blocked = client.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    assert blocked.status_code == 409 and blocked.json()["error"] == "blocked_by_rules"

    rejected = client.post(f"/drafts/{draft['id']}/reject", json={"approver": "Jordan Example", "reason": "opted out"})
    assert rejected.json()["status"] == "rejected"


def test_errors_map_to_http_status_codes(client):
    assert client.get("/drafts/41").status_code == 404
    assert client.post("/drafts", json={**DRAFT, "channel": "fax"}).status_code == 422
    assert client.post("/drafts", json={"agent": "x"}).status_code == 422
    assert client.get("/drafts", params={"status": "nonsense"}).status_code == 422


def test_approver_token_guards_human_actions(gate):
    client = TestClient(create_app(gate, approver_token="local-test-token"))
    draft = client.post("/drafts", json=DRAFT).json()
    body = {"approver": "Jordan Example", "content_hash": draft["content_hash"]}

    assert client.post(f"/drafts/{draft['id']}/approve", json=body).status_code == 401
    wrong = client.post(f"/drafts/{draft['id']}/approve", json=body, headers={"X-Approver-Token": "guess"})
    assert wrong.status_code == 401
    assert client.post(f"/drafts/{draft['id']}/reject", json={"approver": "x"}).status_code == 401
    assert client.get(f"/drafts/{draft['id']}").json()["status"] == "pending"

    good = client.post(f"/drafts/{draft['id']}/approve", json=body, headers={"X-Approver-Token": "local-test-token"})
    assert good.status_code == 200
    assert client.get("/healthz").json()["approver_token_required"] is True


def test_page_and_meta_endpoints(client):
    page = client.get("/")
    assert page.status_code == 200 and "send-gate approvals" in page.text
    assert "innerHTML =" not in page.text
    assert client.get("/healthz").json() == {"ok": True, "adapter": "mock", "approver_token_required": False}
    assert client.get("/rule-pack").json()["version"] == "test-1"
