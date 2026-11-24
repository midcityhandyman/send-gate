import json
import sqlite3

import pytest

from conftest import PACK, events
from send_gate.gate import GateError

PHONE = "555-555-0142"
BODY = "Hi Alex, your visit is booked for Tuesday at 9am. Reply here with any questions."


def submit(gate, body=BODY, recipient=PHONE, channel="sms", agent="scheduler-bot"):
    return gate.submit(agent, channel, recipient, body)


def approved(gate, **kwargs):
    draft = submit(gate, **kwargs)
    return gate.approve(draft["id"], "Jordan Example", draft["content_hash"])


def refusal(fn, *args, **kwargs):
    with pytest.raises(GateError) as info:
        fn(*args, **kwargs)
    return info.value


# ------------------------------------------------------------------ happy path


def test_clean_draft_is_approved_sent_and_read_back(gate, adapter):
    draft = submit(gate)
    assert draft["status"] == "pending" and draft["verdict"] == "ok"
    assert [c["level"] for c in draft["checks"]["results"]] == ["ok"] * 5

    done = gate.send(gate.approve(draft["id"], "Jordan Example", draft["content_hash"])["id"], "scheduler-bot")
    assert done["status"] == "verified"
    send = done["sends"][0]
    assert send["provider_message_id"] == "mock-000001"
    assert send["readback_ok"] == 1
    assert send["target_timestamp"] == "2026-11-10T15:00:00Z"
    assert send["readback_hash"] == draft["content_hash"]

    assert adapter.outbox[0].body == BODY and adapter.outbox[0].recipient == "+15555550142"
    assert events(gate, draft["id"]) == [
        "draft_submitted",
        "checks_run",
        "checks_run",
        "approved",
        "checks_run",
        "send_started",
        "sent",
        "readback_verified",
    ]


def test_invalid_drafts_are_refused(gate):
    assert refusal(submit, gate, body="   ").code == "invalid_message"
    assert refusal(submit, gate, recipient="12").code == "invalid_message"
    assert refusal(submit, gate, channel="fax").code == "invalid_message"
    assert refusal(submit, gate, agent=" ").code == "missing_field"
    assert gate.list() == []


# ------------------------------------------------------- approval is for exact text


@pytest.mark.parametrize(
    "change",
    [
        {"body": BODY + " Thanks!"},
        {"body": BODY + " "},
        {"recipient": "555-555-0143"},
        {"channel": "email", "recipient": "alex.sample@example.com"},
    ],
)
def test_any_change_voids_the_approval(gate, adapter, change):
    draft = approved(gate)
    edited = gate.edit(draft["id"], "scheduler-bot", **change)
    assert edited["status"] == "pending"
    assert edited["approval"] is None
    assert edited["content_hash"] != draft["content_hash"]
    assert "approval_voided" in events(gate, draft["id"])
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "not_approved"
    assert adapter.outbox == []


def test_edit_that_changes_nothing_keeps_the_approval(gate):
    draft = approved(gate)
    same = gate.edit(draft["id"], "scheduler-bot", body=BODY, recipient="+1 (555) 555-0142")
    assert same["status"] == "approved" and same["approval"]["approver"] == "Jordan Example"


def test_old_approval_cannot_be_reused_after_edit_back(gate):
    draft = approved(gate)
    gate.edit(draft["id"], "scheduler-bot", body="Something else entirely.")
    back = gate.edit(draft["id"], "scheduler-bot", body=BODY)
    assert back["content_hash"] == draft["content_hash"]
    assert back["status"] == "pending" and back["approval"] is None


def test_approval_needs_the_hash_of_the_text_that_was_reviewed(gate):
    draft = submit(gate)
    gate.edit(draft["id"], "scheduler-bot", body=BODY + " Bring a ladder.")
    err = refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"])
    assert err.code == "text_changed" and err.status == 409
    assert gate.get(draft["id"])["status"] == "pending"
    assert "approve_refused" in events(gate, draft["id"])


def test_agent_cannot_approve_its_own_draft(gate):
    draft = submit(gate)
    assert refusal(gate.approve, draft["id"], "Scheduler-Bot", draft["content_hash"]).code == "self_approval"


def test_change_made_behind_the_gate_is_caught_at_send(gate, adapter):
    draft = approved(gate)
    gate._conn.execute("UPDATE drafts SET body = ? WHERE id = ?", ("Wire the deposit today.", draft["id"]))
    gate._conn.commit()
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "approval_void"
    after = gate.get(draft["id"])
    assert after["status"] == "pending" and after["approval"] is None
    assert adapter.outbox == []


def test_edit_and_reject_are_closed_after_send(gate):
    draft = approved(gate)
    gate.send(draft["id"], "scheduler-bot")
    assert refusal(gate.edit, draft["id"], "scheduler-bot", body="new").code == "not_editable"
    assert refusal(gate.reject, draft["id"], "Jordan Example").code == "not_open"


def test_reject_voids_and_closes(gate):
    draft = approved(gate)
    out = gate.reject(draft["id"], "Jordan Example", "wrong customer")
    assert out["status"] == "rejected" and out["approval"] is None
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "not_approved"


# ------------------------------------------------------------------------ rules


def test_stop_verdict_blocks_approval(gate):
    draft = submit(gate, body="Results guaranteed, book today.")
    assert draft["verdict"] == "stop"
    err = refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"], True)
    assert err.code == "blocked_by_rules"
    assert err.extra["checks"][0]["code"] == "banned_phrases"


def test_fixing_the_text_clears_the_stop(gate):
    draft = submit(gate, body="Results guaranteed, book today.")
    fixed = gate.edit(draft["id"], "scheduler-bot", body="We can book you in today.")
    assert fixed["verdict"] == "ok"
    assert gate.approve(draft["id"], "Jordan Example", fixed["content_hash"])["status"] == "approved"


def test_caution_needs_an_explicit_acknowledgement(gate):
    draft = submit(gate, body="The first estimate is free.")
    assert draft["verdict"] == "caution"
    assert refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"]).code == "cautions_not_acknowledged"
    ok = gate.approve(draft["id"], "Jordan Example", draft["content_hash"], acknowledge_cautions=True)
    assert ok["status"] == "approved" and ok["approval"]["acknowledged_cautions"] == 1


def test_opt_out_blocks_submit_verdict_and_approval(gate):
    gate.add_opt_out("sms", "(555) 555-0142", "STOP reply")
    draft = submit(gate)
    assert draft["verdict"] == "stop"
    assert refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"]).code == "blocked_by_rules"


def test_opt_out_is_per_channel(gate):
    gate.add_opt_out("sms", PHONE)
    assert submit(gate, channel="email", recipient="alex.sample@example.com")["verdict"] == "ok"


def test_opt_out_that_arrives_after_approval_blocks_the_send(gate, adapter):
    draft = approved(gate)
    gate.add_opt_out("sms", PHONE, "STOP reply")
    err = refusal(gate.send, draft["id"], "scheduler-bot")
    assert err.code == "blocked_by_rules" and err.extra["checks"][0]["code"] == "opt_out"
    assert adapter.outbox == []
    assert "send_refused" in events(gate, draft["id"])


def test_quiet_hours_hold_the_send_until_the_window_opens(gate, adapter, clock):
    clock.set_utc(2026, 11, 11, 3, 30)  # 22:30 in New York
    draft = submit(gate)
    assert draft["verdict"] == "caution"
    gate.approve(draft["id"], "Jordan Example", draft["content_hash"], acknowledge_cautions=True)
    err = refusal(gate.send, draft["id"], "scheduler-bot")
    assert err.code == "blocked_by_rules" and err.extra["checks"][0]["code"] == "quiet_hours"
    assert gate.get(draft["id"])["status"] == "approved"  # the approval survives the hold

    clock.set_utc(2026, 11, 11, 13, 5)  # 08:05 in New York
    assert gate.send(draft["id"], "scheduler-bot")["status"] == "verified"
    assert len(adapter.outbox) == 1


def test_same_text_to_same_recipient_inside_window_is_a_stop(gate, clock):
    gate.send(approved(gate)["id"], "scheduler-bot")
    clock.advance(hours=2)
    again = submit(gate)
    assert again["verdict"] == "stop"
    dup = [c for c in again["checks"]["results"] if c["code"] == "duplicate_send"][0]
    assert dup["level"] == "stop"

    clock.advance(hours=23)
    assert gate.recheck(again["id"])["verdict"] == "ok"


def test_second_message_to_same_recipient_minutes_later_is_a_caution(gate, clock):
    gate.send(approved(gate)["id"], "scheduler-bot")
    clock.advance(minutes=3)
    assert submit(gate, body="One more thing: please clear the driveway.")["verdict"] == "caution"
    clock.advance(minutes=10)
    assert submit(gate, body="And the side gate will need to be open.")["verdict"] == "ok"


def test_twin_drafts_cannot_both_be_sent(gate, adapter):
    first = submit(gate)
    twin = submit(gate, agent="follow-up-bot")
    assert twin["verdict"] == "caution"
    gate.approve(first["id"], "Jordan Example", first["content_hash"], True)
    gate.approve(twin["id"], "Jordan Example", twin["content_hash"], True)
    gate.send(first["id"], "scheduler-bot")
    assert refusal(gate.send, twin["id"], "follow-up-bot").code == "blocked_by_rules"
    assert len(adapter.outbox) == 1


def test_stale_rule_pack_cautions_then_stops(gate, pack_path):
    pack_path.write_text(json.dumps({**PACK, "reviewed_at": "2026-10-01"}), encoding="utf-8")
    draft = submit(gate)
    stale = [c for c in draft["checks"]["results"] if c["code"] == "stale_rule_pack"][0]
    assert draft["verdict"] == "caution" and stale["level"] == "caution"

    pack_path.write_text(json.dumps({**PACK, "reviewed_at": "2026-08-01"}), encoding="utf-8")
    assert gate.recheck(draft["id"])["verdict"] == "stop"

    pack_path.write_text(json.dumps({**PACK, "reviewed_at": "2026-11-09", "version": "test-2"}), encoding="utf-8")
    fresh = gate.recheck(draft["id"])
    assert fresh["verdict"] == "ok" and fresh["checks"]["rule_pack_version"] == "test-2"


def test_unreadable_rule_pack_refuses_instead_of_passing(gate, pack_path):
    pack_path.write_text("{", encoding="utf-8")
    err = refusal(submit, gate)
    assert err.code == "rule_pack_unavailable" and err.status == 503
    assert gate.list() == []  # nothing unchecked is stored
    assert gate.audit() == []


def test_unreadable_rule_pack_blocks_approval_and_send(gate, pack_path, adapter):
    waiting = submit(gate, body="A second, different message.")
    ready = approved(gate)
    good = pack_path.read_text(encoding="utf-8")
    pack_path.write_text("{", encoding="utf-8")
    assert refusal(gate.approve, waiting["id"], "Jordan Example", waiting["content_hash"]).status == 503
    assert refusal(gate.send, ready["id"], "scheduler-bot").status == 503
    assert adapter.outbox == [] and gate.get(ready["id"])["status"] == "approved"

    pack_path.write_text(good, encoding="utf-8")
    assert gate.send(ready["id"], "scheduler-bot")["status"] == "verified"


# ----------------------------------------------------------------- send and read back


def test_a_draft_is_sent_at_most_once(gate, adapter):
    draft = approved(gate)
    gate.send(draft["id"], "scheduler-bot")
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "not_approved"
    assert len(adapter.outbox) == 1


def test_adapter_failure_is_recorded_and_closes_the_draft(gate, adapter):
    draft = approved(gate)
    adapter.fail_next = "carrier rejected the number"
    err = refusal(gate.send, draft["id"], "scheduler-bot")
    assert err.code == "send_failed" and err.status == 502
    after = gate.get(draft["id"])
    assert after["status"] == "send_failed"
    assert after["sends"][0]["error"] == "carrier rejected the number"
    assert after["sends"][0]["provider_message_id"] is None
    assert events(gate, draft["id"])[-1] == "send_failed"


def test_failed_send_does_not_count_as_a_duplicate(gate, adapter):
    adapter.fail_next = "timeout"
    refusal(gate.send, approved(gate)["id"], "scheduler-bot")
    assert submit(gate)["verdict"] == "ok"


def test_read_back_catches_text_altered_by_the_target(gate, adapter):
    draft = approved(gate)
    adapter.alter_next = lambda body: body[:20]
    out = gate.send(draft["id"], "scheduler-bot")
    assert out["status"] == "verify_failed"
    send = out["sends"][0]
    assert send["readback_ok"] == 0 and send["readback_hash"] != draft["content_hash"]
    last = gate.audit(draft["id"])[-1]
    assert last["event"] == "readback_mismatch" and last["detail"]["target_body"] == BODY[:20]


def test_read_back_catches_a_message_the_target_does_not_have(gate, adapter):
    draft = approved(gate)
    adapter.lose_next = True
    out = gate.send(draft["id"], "scheduler-bot")
    assert out["status"] == "verify_failed"
    assert out["sends"][0]["provider_message_id"] == "mock-000001"
    assert events(gate, draft["id"])[-1] == "readback_failed"


# ------------------------------------------------------------------------ audit


def test_audit_chain_verifies_and_detects_tampering(gate, tmp_path):
    gate.send(approved(gate)["id"], "scheduler-bot")
    report = gate.verify_audit_chain()
    assert report["ok"] and report["entries"] == len(gate.audit())

    raw = sqlite3.connect(str(tmp_path / "gate.db"))
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("UPDATE audit_events SET actor = 'someone else' WHERE id = 2")
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("DELETE FROM audit_events WHERE id = 2")

    # A determined editor can drop the trigger. The chain still shows the edit.
    raw.execute("DROP TRIGGER audit_no_update")
    raw.execute("UPDATE audit_events SET actor = 'someone else' WHERE id = 4")
    raw.commit()
    raw.close()
    assert gate.verify_audit_chain() == {"ok": False, "entries": 3, "broken_at_id": 4}


def test_audit_records_the_exact_text_and_who_did_what(gate):
    draft = approved(gate)
    trail = gate.audit(draft["id"])
    assert trail[0]["actor"] == "scheduler-bot" and trail[0]["detail"]["body"] == BODY
    approval = [e for e in trail if e["event"] == "approved"][0]
    assert approval["actor"] == "Jordan Example"
    assert approval["detail"]["content_hash"] == draft["content_hash"]


def test_unknown_draft_is_a_404(gate):
    assert refusal(gate.get, 999).status == 404
    assert refusal(gate.audit, 999).status == 404
