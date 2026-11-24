import json
from datetime import datetime, timezone

import pytest

from conftest import PACK
from send_gate import rules
from send_gate.rules import CAUTION, OK, STOP, Facts


def make_pack(**overrides):
    return rules.parse_rule_pack(json.dumps({**PACK, **overrides}))


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# 2026-11-10 is in standard time, so New York is UTC minus 5.
DAY = utc(2026, 11, 10, 15, 0)  # 10:00 local
NIGHT = utc(2026, 11, 11, 3, 30)  # 22:30 local
EARLY = utc(2026, 11, 10, 12, 59)  # 07:59 local
OPEN = utc(2026, 11, 10, 13, 0)  # 08:00 local


def test_quiet_hours_window_wraps_midnight():
    pack = make_pack()
    assert not rules.in_quiet_hours(pack, DAY)
    assert rules.in_quiet_hours(pack, NIGHT)
    assert rules.in_quiet_hours(pack, EARLY)
    assert not rules.in_quiet_hours(pack, OPEN)


def test_quiet_hours_same_day_window():
    quiet = {**PACK["quiet_hours"], "start": "12:00", "end": "13:00"}
    pack = make_pack(quiet_hours=quiet)
    assert rules.in_quiet_hours(pack, utc(2026, 11, 10, 17, 30))
    assert not rules.in_quiet_hours(pack, utc(2026, 11, 10, 18, 0))


def test_quiet_hours_caution_before_send_and_stop_at_send():
    pack = make_pack()
    assert rules.check_quiet_hours(pack, "sms", NIGHT, "submit").level == CAUTION
    assert rules.check_quiet_hours(pack, "sms", NIGHT, "approve").level == CAUTION
    assert rules.check_quiet_hours(pack, "sms", NIGHT, "send").level == STOP
    assert rules.check_quiet_hours(pack, "sms", DAY, "send").level == OK


def test_quiet_hours_skip_channels_not_listed():
    assert rules.check_quiet_hours(make_pack(), "email", NIGHT, "send").level == OK


def test_quiet_hours_follow_daylight_saving():
    # 2026-07-01 12:30 UTC is 08:30 in New York in summer: outside the window.
    assert not rules.in_quiet_hours(make_pack(), utc(2026, 7, 1, 12, 30))
    # The same UTC time in winter is 07:30 local: inside.
    assert rules.in_quiet_hours(make_pack(), utc(2026, 12, 1, 12, 30))


@pytest.mark.parametrize(
    "body,level",
    [
        ("Your slot is confirmed for Tuesday.", OK),
        ("Results are GUARANTEED.", STOP),
        ("Please act\n   now to keep the slot.", STOP),
        ("The first visit is free.", CAUTION),
        ("It is free and guaranteed.", STOP),
        ("We run a freeze check on the pipes.", OK),
        ("That was a transaction now closed.", OK),
    ],
)
def test_banned_phrases(body, level):
    assert rules.check_banned_phrases(make_pack(), body).level == level


def test_banned_phrase_detail_names_every_hit():
    detail = rules.check_banned_phrases(make_pack(), "Free and guaranteed").detail
    assert '"free"' in detail and '"guaranteed"' in detail


@pytest.mark.parametrize(
    "reviewed,level", [("2026-11-01", OK), ("2026-10-11", OK), ("2026-10-10", CAUTION), ("2026-09-01", STOP)]
)
def test_rule_pack_age_levels(reviewed, level):
    assert rules.check_rule_pack_age(make_pack(reviewed_at=reviewed), DAY).level == level


def test_rule_pack_with_future_review_date_is_a_caution():
    assert rules.check_rule_pack_age(make_pack(reviewed_at="2027-01-01"), DAY).level == CAUTION


def test_duplicate_levels():
    pack = make_pack()
    assert rules.check_duplicate(pack, Facts()).level == OK
    sent = Facts(same_content_sent_at="2026-11-10T14:00:00Z", same_content_draft_id=3)
    assert rules.check_duplicate(pack, sent).level == STOP
    assert rules.check_duplicate(pack, Facts(open_twin_ids=[4])).level == CAUTION
    assert rules.check_duplicate(pack, Facts(recipient_last_sent_at="2026-11-10T14:55:00Z")).level == CAUTION


def test_opt_out_is_a_stop():
    hit = rules.check_opt_out(Facts(opted_out=True, opt_out_source="STOP reply"), "sms", "+15555550142")
    assert hit.level == STOP and "STOP reply" in hit.detail
    assert rules.check_opt_out(Facts(), "sms", "+15555550142").level == OK


def test_run_checks_returns_all_five_and_verdict_is_the_worst():
    results = rules.run_checks(
        make_pack(), channel="sms", recipient="+15555550142", body="It is free.", facts=Facts(), now=DAY, phase="submit"
    )
    assert [r.code for r in results] == [
        "quiet_hours",
        "duplicate_send",
        "opt_out",
        "banned_phrases",
        "stale_rule_pack",
    ]
    assert rules.verdict_of(results) == CAUTION
    assert rules.verdict_of([]) == OK


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"version": "x"}),
        json.dumps({**PACK, "reviewed_at": "last week"}),
        json.dumps({**PACK, "hard_max_age_days": 1}),
        json.dumps({**PACK, "quiet_hours": {**PACK["quiet_hours"], "timezone": "Mars/Olympus"}}),
        json.dumps({**PACK, "quiet_hours": {**PACK["quiet_hours"], "start": "9pm"}}),
        json.dumps({**PACK, "banned_phrases": [{"phrase": "x", "level": "maybe"}]}),
    ],
)
def test_broken_rule_packs_are_refused(text):
    with pytest.raises(rules.RulePackError):
        rules.parse_rule_pack(text)


def test_shipped_default_pack_parses():
    from send_gate.api import DEFAULT_PACK

    pack = rules.load_rule_pack(DEFAULT_PACK)
    assert pack.version and len(pack.sha256) == 64
