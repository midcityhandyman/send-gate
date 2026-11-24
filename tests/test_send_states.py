"""No draft may rest in sending, and no draft may reach the adapter twice,
whatever the adapter does."""

import pytest

from conftest import AS_AGENT, AS_HUMAN, events
from send_gate.adapters import MockAdapter, SendReceipt
from send_gate.gate import Gate, GateError

PHONE = "555-555-0142"
BODY = "Hi Alex, your visit is booked for Tuesday at 9am. Reply here with any questions."


class Killed(BaseException):
    """Stands in for the process dying: not an Exception, so nothing handles it."""


class BrokenAdapter(MockAdapter):
    """Raises something that is not a SendError, the way a buggy or half
    configured provider client would."""

    name = "broken"

    def send(self, **kwargs):
        self.send_calls += 1
        raise KeyError("api_key")


class NoReceiptAdapter(MockAdapter):
    """Accepts the call but returns nothing usable."""

    name = "no-receipt"

    def send(self, **kwargs):
        super().send(**kwargs)
        return SendReceipt(message_id="")


class DeafAdapter(MockAdapter):
    """Sends fine. Its read back raises until told otherwise."""

    name = "deaf"

    def __init__(self, clock):
        super().__init__(clock)
        self.down = True
        self.read_back_calls = 0

    def read_back(self, message_id):
        self.read_back_calls += 1
        if self.down:
            raise ConnectionError("read back endpoint unreachable")
        return super().read_back(message_id)


def make_gate(tmp_path, pack_path, clock, adapter):
    return Gate(str(tmp_path / "gate.db"), str(pack_path), adapter=adapter, clock=clock)


def approved(gate, body=BODY, recipient=PHONE):
    draft = gate.submit("scheduler-bot", "sms", recipient, body)
    return gate.approve(draft["id"], "Jordan Example", draft["content_hash"])


def refusal(fn, *args, **kwargs):
    with pytest.raises(GateError) as info:
        fn(*args, **kwargs)
    return info.value


# ------------------------------------------------- the adapter raises unexpectedly


def test_an_unexpected_adapter_error_closes_the_draft_as_send_failed(tmp_path, pack_path, clock):
    adapter = BrokenAdapter(clock)
    gate = make_gate(tmp_path, pack_path, clock, adapter)
    draft = approved(gate)

    err = refusal(gate.send, draft["id"], "scheduler-bot")
    assert err.code == "send_failed" and err.status == 502
    assert "KeyError" in err.message and "unknown whether the message went out" in err.message

    after = gate.get(draft["id"])
    assert after["status"] == "send_failed"
    assert after["sends"][0]["error"].startswith("unexpected adapter error (KeyError)")
    assert after["sends"][0]["provider_message_id"] is None
    last = gate.audit(draft["id"])[-1]
    assert last["event"] == "send_failed" and last["detail"]["unexpected"] is True
    assert gate.list("sending") == []
    assert gate.verify_audit_chain()["ok"]
    gate.close()


def test_an_unexpected_adapter_error_is_never_retried(tmp_path, pack_path, clock):
    adapter = BrokenAdapter(clock)
    gate = make_gate(tmp_path, pack_path, clock, adapter)
    draft = approved(gate)
    refusal(gate.send, draft["id"], "scheduler-bot")

    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "not_approved"
    assert refusal(gate.verify, draft["id"], "scheduler-bot").code == "nothing_to_verify"
    assert refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"]).code == "not_pending"
    assert refusal(gate.edit, draft["id"], "scheduler-bot", BODY + " again").code == "not_editable"
    assert adapter.send_calls == 1
    gate.close()


def test_the_mock_can_raise_any_error_and_the_gate_still_closes_the_draft(gate, adapter):
    draft = approved(gate)
    adapter.raise_next = RuntimeError("connection reset by peer")
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "send_failed"
    assert gate.get(draft["id"])["status"] == "send_failed"
    assert adapter.outbox == [] and adapter.send_calls == 1


def test_a_receipt_without_a_message_id_is_a_failed_send(tmp_path, pack_path, clock):
    adapter = NoReceiptAdapter(clock)
    gate = make_gate(tmp_path, pack_path, clock, adapter)
    draft = approved(gate)
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "send_failed"
    after = gate.get(draft["id"])
    assert after["status"] == "send_failed" and "no message id" in after["sends"][0]["error"]
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "not_approved"
    assert adapter.send_calls == 1
    gate.close()


# ------------------------------------------------------------ the read back raises


def test_a_failed_read_back_parks_the_draft_as_unverified(tmp_path, pack_path, clock):
    adapter = DeafAdapter(clock)
    gate = make_gate(tmp_path, pack_path, clock, adapter)
    draft = approved(gate)

    out = gate.send(draft["id"], "scheduler-bot")
    assert out["status"] == "unverified"
    send = out["sends"][0]
    assert send["provider_message_id"] == "mock-000001" and send["sent_at"]
    assert send["readback_ok"] is None and send["readback_hash"] is None
    assert send["error"] == "read back failed (ConnectionError): read back endpoint unreachable"
    assert events(gate, draft["id"])[-3:] == ["send_started", "sent", "readback_error"]
    assert [d["id"] for d in gate.list("unverified")] == [draft["id"]]
    assert gate.list("sending") == []
    gate.close()


def test_verify_retries_the_read_back_and_never_the_send(tmp_path, pack_path, clock):
    adapter = DeafAdapter(clock)
    gate = make_gate(tmp_path, pack_path, clock, adapter)
    draft = approved(gate)
    gate.send(draft["id"], "scheduler-bot")

    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "not_approved"
    assert gate.verify(draft["id"], "Jordan Example")["status"] == "unverified"  # still down
    assert gate.verify(draft["id"], "Jordan Example")["status"] == "unverified"

    adapter.down = False
    done = gate.verify(draft["id"], "Jordan Example")
    assert done["status"] == "verified"
    send = done["sends"][0]
    assert send["readback_ok"] == 1 and send["readback_hash"] == draft["content_hash"] and send["error"] is None
    assert len(done["sends"]) == 1

    assert adapter.send_calls == 1 and len(adapter.outbox) == 1
    assert adapter.read_back_calls == 4
    assert events(gate, draft["id"])[-2:] == ["readback_retry", "readback_verified"]
    assert refusal(gate.verify, draft["id"], "Jordan Example").code == "nothing_to_verify"
    assert gate.verify_audit_chain()["ok"]
    gate.close()


def test_a_retried_read_back_still_catches_altered_text(gate, adapter):
    draft = approved(gate)
    adapter.alter_next = lambda body: body.replace("9am", "9pm")
    adapter.read_back_error = TimeoutError("read back timed out")
    assert gate.send(draft["id"], "scheduler-bot")["status"] == "unverified"

    adapter.read_back_error = None
    out = gate.verify(draft["id"], "Jordan Example")
    assert out["status"] == "verify_failed" and out["sends"][0]["readback_ok"] == 0
    assert gate.audit(draft["id"])[-1]["event"] == "readback_mismatch"


def test_a_retried_read_back_still_catches_a_missing_message(gate, adapter):
    draft = approved(gate)
    adapter.lose_next = True
    adapter.read_back_error = TimeoutError("read back timed out")
    assert gate.send(draft["id"], "scheduler-bot")["status"] == "unverified"

    adapter.read_back_error = None
    assert gate.verify(draft["id"], "Jordan Example")["status"] == "verify_failed"
    assert events(gate, draft["id"])[-1] == "readback_failed"


def test_an_unverified_send_counts_as_sent_for_the_duplicate_check(gate, adapter):
    draft = approved(gate)
    adapter.read_back_error = TimeoutError("read back timed out")
    gate.send(draft["id"], "scheduler-bot")

    again = gate.submit("scheduler-bot", "sms", PHONE, BODY)
    assert again["verdict"] == "stop"
    assert refusal(gate.approve, again["id"], "Jordan Example", again["content_hash"]).code == "blocked_by_rules"
    assert adapter.send_calls == 1


@pytest.mark.parametrize("state", ["pending", "approved", "rejected", "verified", "send_failed", "verify_failed"])
def test_verify_is_refused_when_there_is_no_open_send(gate, adapter, state):
    draft = gate.submit("scheduler-bot", "sms", PHONE, BODY)
    if state == "rejected":
        gate.reject(draft["id"], "Jordan Example")
    elif state != "pending":
        gate.approve(draft["id"], "Jordan Example", draft["content_hash"])
    if state == "send_failed":
        adapter.fail_next = "carrier rejected the number"
        refusal(gate.send, draft["id"], "scheduler-bot")
    elif state == "verify_failed":
        adapter.lose_next = True
        gate.send(draft["id"], "scheduler-bot")
    elif state == "verified":
        gate.send(draft["id"], "scheduler-bot")
    assert gate.get(draft["id"])["status"] == state

    calls = adapter.send_calls
    assert refusal(gate.verify, draft["id"], "Jordan Example").code == "nothing_to_verify"
    assert gate.get(draft["id"])["status"] == state and adapter.send_calls == calls


# ------------------------------------------------------------ the process is killed


def test_a_process_killed_inside_the_adapter_is_settled_without_a_second_send(tmp_path, pack_path, clock):
    adapter = MockAdapter(clock)
    gate = make_gate(tmp_path, pack_path, clock, adapter)
    draft = approved(gate)
    adapter.raise_next = Killed()
    with pytest.raises(Killed):
        gate.send(draft["id"], "scheduler-bot")
    gate.close()

    # A new process opens the same database and finds the draft still claimed.
    reopened = make_gate(tmp_path, pack_path, clock, adapter)
    assert reopened.get(draft["id"])["status"] == "sending"
    assert refusal(reopened.send, draft["id"], "scheduler-bot").code == "not_approved"

    settled = reopened.verify(draft["id"], "Jordan Example")
    assert settled["status"] == "send_failed"
    assert "unknown whether the message went out" in settled["sends"][0]["error"]
    last = reopened.audit(draft["id"])[-1]
    assert last["event"] == "send_failed" and last["detail"]["settled_by"] == "Jordan Example"
    assert adapter.send_calls == 1 and reopened.list("sending") == []
    assert reopened.verify_audit_chain()["ok"]
    reopened.close()


def test_a_process_killed_inside_the_read_back_keeps_the_target_id_and_can_verify(tmp_path, pack_path, clock):
    adapter = MockAdapter(clock)
    gate = make_gate(tmp_path, pack_path, clock, adapter)
    draft = approved(gate)
    adapter.read_back_error = Killed()
    with pytest.raises(Killed):
        gate.send(draft["id"], "scheduler-bot")
    gate.close()

    adapter.read_back_error = None
    reopened = make_gate(tmp_path, pack_path, clock, adapter)
    stuck = reopened.get(draft["id"])
    assert stuck["status"] == "sending" and stuck["sends"][0]["provider_message_id"] == "mock-000001"

    done = reopened.verify(draft["id"], "Jordan Example")
    assert done["status"] == "verified" and done["sends"][0]["readback_ok"] == 1
    assert adapter.send_calls == 1 and len(adapter.outbox) == 1
    reopened.close()


# ------------------------------------------------------------------------ over http


def test_unverified_and_verify_over_http(gate, adapter, agent, human, anon):
    draft = agent.post(
        "/drafts", json={"agent": "scheduler-bot", "channel": "sms", "recipient": PHONE, "body": BODY}
    ).json()
    human.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    adapter.read_back_error = TimeoutError("read back timed out")
    parked = agent.post(f"/drafts/{draft['id']}/send", json={"actor": "scheduler-bot"})
    assert parked.status_code == 200 and parked.json()["status"] == "unverified"
    assert [d["id"] for d in human.get("/drafts", params={"status": "unverified"}).json()] == [draft["id"]]

    assert anon.post(f"/drafts/{draft['id']}/verify", json={"actor": "x"}).status_code == 401
    adapter.read_back_error = None
    done = anon.post(f"/drafts/{draft['id']}/verify", json={"actor": "Jordan Example"}, headers=AS_HUMAN)
    assert done.status_code == 200 and done.json()["status"] == "verified"
    again = anon.post(f"/drafts/{draft['id']}/verify", json={"actor": "scheduler-bot"}, headers=AS_AGENT)
    assert again.status_code == 409 and again.json()["error"] == "nothing_to_verify"
    assert adapter.send_calls == 1


def test_an_unexpected_adapter_error_over_http_is_a_502_not_a_500(gate, adapter, agent, human):
    draft = agent.post(
        "/drafts", json={"agent": "scheduler-bot", "channel": "sms", "recipient": PHONE, "body": BODY}
    ).json()
    human.post(
        f"/drafts/{draft['id']}/approve", json={"approver": "Jordan Example", "content_hash": draft["content_hash"]}
    )
    adapter.raise_next = ValueError("bad payload")
    failed = agent.post(f"/drafts/{draft['id']}/send", json={"actor": "scheduler-bot"})
    assert failed.status_code == 502 and failed.json()["error"] == "send_failed"
    assert agent.get(f"/drafts/{draft['id']}").json()["status"] == "send_failed"
