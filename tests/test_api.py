DRAFT = {
    "agent": "scheduler-bot",
    "channel": "sms",
    "recipient": "555-555-0142",
    "body": "Hi Alex, your visit is booked for Tuesday at 9am.",
    "note": "appointment confirmation",
}


def test_full_flow_over_http(agent, human):
    created = agent.post("/drafts", json=DRAFT)
    assert created.status_code == 201
    draft = created.json()
    assert draft["verdict"] == "ok" and draft["recipient"] == "+15555550142"

    pending = human.get("/drafts", params={"status": "pending"}).json()
    assert [d["id"] for d in pending] == [draft["id"]]

    approve = human.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    assert approve.status_code == 200 and approve.json()["status"] == "approved"

    sent = agent.post(f"/drafts/{draft['id']}/send", json={"actor": "scheduler-bot"}).json()
    assert sent["status"] == "verified"
    assert sent["sends"][0]["provider_message_id"].startswith("mock-")

    trail = human.get(f"/drafts/{draft['id']}/audit").json()
    assert trail[-1]["event"] == "readback_verified"
    assert human.get("/audit/verify").json()["ok"] is True
    assert len(human.get("/audit").json()) == len(trail)


def test_edit_over_http_voids_approval_and_send_is_refused(agent, human):
    draft = agent.post("/drafts", json=DRAFT).json()
    human.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    edited = agent.patch(f"/drafts/{draft['id']}", json={"actor": "scheduler-bot", "body": DRAFT["body"] + " PS"})
    assert edited.json()["status"] == "pending" and edited.json()["approval"] is None

    refused = agent.post(f"/drafts/{draft['id']}/send", json={"actor": "scheduler-bot"})
    assert refused.status_code == 409 and refused.json()["error"] == "not_approved"

    stale = human.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    assert stale.status_code == 409 and stale.json()["error"] == "text_changed"
    assert stale.json()["current_hash"] == edited.json()["content_hash"]


def test_opt_out_endpoints_and_stop_verdict(agent, human):
    added = human.post("/opt-outs", json={"channel": "sms", "recipient": "(555) 555-0142", "source": "STOP reply"})
    assert added.status_code == 201 and added.json()["added"] is True
    assert human.get("/opt-outs").json()[0]["recipient"] == "+15555550142"

    draft = agent.post("/drafts", json=DRAFT).json()
    assert draft["verdict"] == "stop"
    blocked = human.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    assert blocked.status_code == 409 and blocked.json()["error"] == "blocked_by_rules"

    rejected = human.post(f"/drafts/{draft['id']}/reject", json={"approver": "Jordan Example", "reason": "opted out"})
    assert rejected.json()["status"] == "rejected"


def test_errors_map_to_http_status_codes(agent):
    assert agent.get("/drafts/41").status_code == 404
    assert agent.post("/drafts", json={**DRAFT, "channel": "fax"}).status_code == 422
    assert agent.post("/drafts", json={"agent": "x"}).status_code == 422
    assert agent.get("/drafts", params={"status": "nonsense"}).status_code == 422


def test_approver_token_guards_human_actions(agent, anon):
    draft = agent.post("/drafts", json=DRAFT).json()
    body = {"approver": "Jordan Example", "content_hash": draft["content_hash"]}

    assert anon.post(f"/drafts/{draft['id']}/approve", json=body).status_code == 401
    wrong = anon.post(f"/drafts/{draft['id']}/approve", json=body, headers={"X-Approver-Token": "guess"})
    assert wrong.status_code == 401
    assert anon.post(f"/drafts/{draft['id']}/reject", json={"approver": "x"}).status_code == 401
    assert agent.get(f"/drafts/{draft['id']}").json()["status"] == "pending"

    good = anon.post(
        f"/drafts/{draft['id']}/approve", json=body, headers={"X-Approver-Token": "test-approver-token-0001"}
    )
    assert good.status_code == 200
    assert anon.get("/healthz").json()["auth_required"] is True


def test_page_and_meta_endpoints(anon, human):
    page = anon.get("/")
    assert page.status_code == 200 and "send-gate approvals" in page.text
    assert "innerHTML =" not in page.text
    assert anon.get("/healthz").json() == {"ok": True, "adapter": "mock", "auth_required": True}
    assert human.get("/rule-pack").json()["version"] == "test-1"
