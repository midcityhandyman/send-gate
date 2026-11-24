"""The shipped sample pack must keep working on a fresh install years from
now, and the stale pack check must stay as strict as ever for real packs."""

import json
from datetime import date

import pytest

from conftest import PACK, FakeClock
from send_gate import rules
from send_gate.__main__ import main as cli
from send_gate.adapters import MockAdapter
from send_gate.api import DEFAULT_PACK
from send_gate.gate import Gate, GateError

BODY = "Hi Alex, your visit is booked for Tuesday at 9am."


def far_future():
    clock = FakeClock()
    clock.set_utc(2031, 6, 3, 15, 0)  # 11:00 in New York, years after the pack was written
    return clock


def open_gate(tmp_path, pack, clock, adapter=None):
    return Gate(str(tmp_path / "gate.db"), str(pack), adapter=adapter or MockAdapter(clock), clock=clock)


def stale_check(draft):
    return [c for c in draft["checks"]["results"] if c["code"] == "stale_rule_pack"][0]


def write_real_pack(tmp_path, **overrides):
    path = tmp_path / "real_pack.json"
    path.write_text(json.dumps({**PACK, **overrides}), encoding="utf-8")
    return path


# ------------------------------------------------------------- the sample pack


def test_the_shipped_pack_is_marked_as_a_sample_with_no_fixed_date():
    raw = json.loads(DEFAULT_PACK.read_text(encoding="utf-8"))
    assert raw["sample"] is True and raw["reviewed_at"] == "first_run"
    pack = rules.load_rule_pack(DEFAULT_PACK)
    assert pack.sample and pack.reviewed_at is None and pack.review_date_source == "first_run"


def test_a_fresh_install_far_in_the_future_still_works(tmp_path):
    clock = far_future()
    gate = open_gate(tmp_path, DEFAULT_PACK, clock)
    try:
        summary = gate.rule_pack().summary()
        assert summary["sample"] is True and summary["reviewed_at"] == "2031-06-03"
        assert summary["review_date_source"] == "first_run"

        draft = gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)
        assert draft["verdict"] == "ok" and stale_check(draft)["level"] == "ok"
        assert "Sample rule pack" in stale_check(draft)["detail"]
        gate.approve(draft["id"], "Jordan Example", draft["content_hash"])
        assert gate.send(draft["id"], "scheduler-bot")["status"] == "verified"

        dated = [e for e in gate.audit() if e["event"] == "sample_pack_dated"]
        assert len(dated) == 1 and dated[0]["detail"]["reviewed_at"] == "2031-06-03"
        assert gate.verify_audit_chain()["ok"]
    finally:
        gate.close()


def test_the_sample_pack_still_goes_stale_counted_from_first_run(tmp_path):
    clock = far_future()
    gate = open_gate(tmp_path, DEFAULT_PACK, clock)
    draft = gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)

    clock.advance(days=90)
    assert gate.recheck(draft["id"])["verdict"] == "ok"
    clock.advance(days=1)
    cautioned = gate.recheck(draft["id"])
    assert cautioned["verdict"] == "caution" and stale_check(cautioned)["level"] == "caution"
    assert refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"]).code == (
        "cautions_not_acknowledged"
    )

    clock.advance(days=90)
    stopped = gate.recheck(draft["id"])
    assert stopped["verdict"] == "stop" and "hard limit is 180" in stale_check(stopped)["detail"]
    assert refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"], True).code == (
        "blocked_by_rules"
    )
    gate.close()


def test_restarting_does_not_reset_the_sample_date(tmp_path):
    clock = far_future()
    open_gate(tmp_path, DEFAULT_PACK, clock).close()

    clock.advance(days=200)
    gate = open_gate(tmp_path, DEFAULT_PACK, clock)
    try:
        assert gate.rule_pack().reviewed_at == date(2031, 6, 3)
        assert gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)["verdict"] == "stop"
        assert len([e for e in gate.audit() if e["event"] == "sample_pack_dated"]) == 1
    finally:
        gate.close()


def test_editing_the_sample_pack_does_not_reset_its_date(tmp_path):
    clock = far_future()
    pack_path = tmp_path / "sample.json"
    raw = json.loads(DEFAULT_PACK.read_text(encoding="utf-8"))
    pack_path.write_text(json.dumps(raw), encoding="utf-8")
    gate = open_gate(tmp_path, pack_path, clock)

    clock.advance(days=200)
    pack_path.write_text(json.dumps({**raw, "version": "sample-edited"}), encoding="utf-8")
    draft = gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)
    assert draft["verdict"] == "stop" and draft["checks"]["rule_pack_version"] == "sample-edited"
    gate.close()


def test_restamp_is_an_explicit_audited_act_and_only_for_a_sample(tmp_path):
    clock = far_future()
    gate = open_gate(tmp_path, DEFAULT_PACK, clock)
    draft = gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)
    clock.advance(days=200)
    assert gate.recheck(draft["id"])["verdict"] == "stop"

    summary = gate.restamp_sample_pack("Jordan Example")
    assert summary["reviewed_at"] == "2031-12-20"
    assert gate.recheck(draft["id"])["verdict"] == "ok"
    last = [e for e in gate.audit() if e["event"] == "sample_pack_dated"][-1]
    assert last["actor"] == "Jordan Example" and last["detail"]["reason"] == "sample pack reviewed again"
    gate.close()

    real = open_gate(tmp_path, write_real_pack(tmp_path), clock)
    assert refusal(real.restamp_sample_pack, "Jordan Example").code == "not_a_sample_pack"
    real.close()


# ------------------------------------------------------------------ real packs


def test_a_stale_real_pack_is_still_cautioned_and_then_stopped(tmp_path):
    clock = FakeClock()  # 2026-11-10; the pack below was reviewed 2026-11-01, limits 30 and 60 days
    gate = open_gate(tmp_path, write_real_pack(tmp_path), clock)
    draft = gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)
    assert draft["verdict"] == "ok" and "Sample" not in stale_check(draft)["detail"]

    clock.set_utc(2026, 12, 15, 15, 0)
    cautioned = gate.recheck(draft["id"])
    assert cautioned["verdict"] == "caution" and stale_check(cautioned)["level"] == "caution"

    clock.set_utc(2027, 2, 1, 15, 0)
    stopped = gate.recheck(draft["id"])
    assert stopped["verdict"] == "stop" and stale_check(stopped)["level"] == "stop"
    assert refusal(gate.approve, draft["id"], "Jordan Example", draft["content_hash"], True).code == (
        "blocked_by_rules"
    )
    gate.close()


def test_a_real_pack_gets_no_first_run_date_on_a_fresh_install(tmp_path):
    clock = far_future()
    gate = open_gate(tmp_path, write_real_pack(tmp_path), clock)
    try:
        assert gate.rule_pack().summary()["reviewed_at"] == "2026-11-01"
        draft = gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)
        assert draft["verdict"] == "stop" and stale_check(draft)["level"] == "stop"
        assert [e for e in gate.audit() if e["event"] == "sample_pack_dated"] == []
    finally:
        gate.close()


def test_a_real_pack_ignores_a_sample_date_already_in_the_database(tmp_path):
    clock = far_future()
    open_gate(tmp_path, DEFAULT_PACK, clock).close()  # the database now holds a fresh sample date

    gate = open_gate(tmp_path, write_real_pack(tmp_path), clock)
    try:
        assert gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)["verdict"] == "stop"
    finally:
        gate.close()


def test_a_real_pack_that_goes_stale_after_approval_stops_the_send(tmp_path):
    clock = FakeClock()
    adapter = MockAdapter(clock)
    gate = open_gate(tmp_path, write_real_pack(tmp_path), clock, adapter)
    draft = gate.submit("scheduler-bot", "sms", "555-555-0142", BODY)
    gate.approve(draft["id"], "Jordan Example", draft["content_hash"])

    clock.set_utc(2027, 2, 1, 15, 0)
    assert refusal(gate.send, draft["id"], "scheduler-bot").code == "blocked_by_rules"
    assert adapter.outbox == [] and adapter.send_calls == 0
    gate.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"reviewed_at": "first_run"},  # not marked as a sample
        {"reviewed_at": "first_run", "sample": False},
        {"reviewed_at": "first_run", "sample": "yes"},
        {"reviewed_at": None},
        {"reviewed_at": None, "sample": True},
        {"reviewed_at": "", "sample": True},
    ],
)
def test_a_pack_cannot_drop_its_review_date_unless_it_is_a_declared_sample(overrides):
    with pytest.raises(rules.RulePackError):
        rules.parse_rule_pack(json.dumps({**PACK, **overrides}))


def test_a_sample_flag_does_not_excuse_a_dated_pack():
    pack = rules.parse_rule_pack(json.dumps({**PACK, "sample": True, "reviewed_at": "2026-08-01"}))
    clock = FakeClock()
    assert pack.review_date_source == "file"
    assert rules.check_rule_pack_age(pack, clock()).level == rules.STOP


def test_an_undated_pack_fails_closed_in_the_check_itself():
    pack = rules.load_rule_pack(DEFAULT_PACK)
    result = rules.check_rule_pack_age(pack, far_future()())
    assert result.level == rules.STOP and "no review date" in result.detail


# ------------------------------------------------------------------------- cli


def test_init_command_dates_the_sample_pack_once_and_restamps_on_request(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("SEND_GATE_DB", str(db))
    monkeypatch.delenv("SEND_GATE_RULE_PACK", raising=False)
    monkeypatch.delenv("SEND_GATE_ADAPTER", raising=False)

    assert cli(["init"]) == 0
    first = capsys.readouterr().out
    assert "sample-1 (sample)" in first and "set by this database at first run" in first and "ok:" in first
    assert db.exists()

    assert cli(["init"]) == 0
    assert cli(["init", "--restamp", "--actor", "Jordan Example"]) == 0
    check = Gate(str(db), str(DEFAULT_PACK))
    dated = [e for e in check.audit() if e["event"] == "sample_pack_dated"]
    check.close()
    assert [e["actor"] for e in dated] == ["gate", "Jordan Example"]


def test_init_command_reports_a_stale_real_pack_and_will_not_restamp_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEND_GATE_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("SEND_GATE_RULE_PACK", str(write_real_pack(tmp_path, reviewed_at="2020-01-01")))

    assert cli(["init"]) == 1
    assert "stop:" in capsys.readouterr().out
    assert cli(["init", "--restamp"]) == 2
    assert "not_a_sample_pack" in capsys.readouterr().err


def refusal(fn, *args, **kwargs):
    with pytest.raises(GateError) as info:
        fn(*args, **kwargs)
    return info.value
