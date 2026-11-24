"""Rule pack loading and the five rule checks.

The checks are pure functions. The gate gathers the facts they need from the
database (opt outs, earlier sends) and passes them in, which keeps every rule
testable without a database.

Levels: ok < caution < stop. The verdict for a draft is the highest level
any check returned.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

OK, CAUTION, STOP = "ok", "caution", "stop"
_RANK = {OK: 0, CAUTION: 1, STOP: 2}

# Phases: checks run when a draft is submitted or edited ("submit"), when a
# human approves ("approve") and again right before the adapter is called
# ("send"). Quiet hours only stop the send itself.
PHASES = ("submit", "approve", "send")


# A pack marked "sample": true may carry this in place of a review date. The
# gate then records the review date in its own database the first time it
# runs with the pack. See Gate._pack and the README section "Rule pack review".
FIRST_RUN = "first_run"


class RulePackError(ValueError):
    """The rule pack file is missing or malformed."""


@dataclass(frozen=True)
class CheckResult:
    code: str
    level: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BannedPhrase:
    phrase: str
    level: str
    reason: str


@dataclass(frozen=True)
class RulePack:
    version: str
    reviewed_at: Optional[date]  # None only for a sample pack the gate has not dated yet
    max_age_days: int
    hard_max_age_days: int
    quiet_timezone: str
    quiet_start: time
    quiet_end: time
    quiet_channels: tuple
    same_content_window_hours: int
    same_recipient_gap_minutes: int
    banned_phrases: tuple
    sha256: str
    sample: bool = False
    review_date_source: str = "file"

    def summary(self) -> dict:
        return {
            "version": self.version,
            "sample": self.sample,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_date_source": self.review_date_source,
            "max_age_days": self.max_age_days,
            "hard_max_age_days": self.hard_max_age_days,
            "quiet_hours": {
                "timezone": self.quiet_timezone,
                "start": self.quiet_start.strftime("%H:%M"),
                "end": self.quiet_end.strftime("%H:%M"),
                "channels": list(self.quiet_channels),
            },
            "duplicate": {
                "same_content_window_hours": self.same_content_window_hours,
                "same_recipient_gap_minutes": self.same_recipient_gap_minutes,
            },
            "banned_phrases": [asdict(p) for p in self.banned_phrases],
            "sha256": self.sha256,
        }


@dataclass
class Facts:
    """What the database knows that the rules need."""

    opted_out: bool = False
    opt_out_source: Optional[str] = None
    same_content_sent_at: Optional[str] = None
    same_content_draft_id: Optional[int] = None
    recipient_last_sent_at: Optional[str] = None
    open_twin_ids: list = field(default_factory=list)


def _parse_time(value: str, name: str) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError) as exc:
        raise RulePackError(f"{name} must look like HH:MM") from exc


def parse_rule_pack(text: str) -> RulePack:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RulePackError(f"rule pack is not valid JSON: {exc}") from exc
    try:
        quiet = raw["quiet_hours"]
        dup = raw["duplicate"]
        phrases = []
        for item in raw["banned_phrases"]:
            level = item["level"]
            if level not in (CAUTION, STOP):
                raise RulePackError("banned phrase level must be caution or stop")
            phrase = _squash(item["phrase"])
            if not phrase:
                raise RulePackError("banned phrase must not be empty")
            phrases.append(BannedPhrase(phrase, level, item.get("reason", "")))
        sample = raw.get("sample", False)
        if not isinstance(sample, bool):
            raise RulePackError("sample must be true or false")
        if raw["reviewed_at"] == FIRST_RUN:
            if not sample:
                raise RulePackError(
                    'reviewed_at "first_run" is only allowed in a pack marked "sample": true; '
                    "a real pack needs the date a person last reviewed it"
                )
            reviewed_at = None
        else:
            reviewed_at = date.fromisoformat(raw["reviewed_at"])
        pack = RulePack(
            version=str(raw["version"]),
            reviewed_at=reviewed_at,
            max_age_days=int(raw["max_age_days"]),
            hard_max_age_days=int(raw["hard_max_age_days"]),
            quiet_timezone=str(quiet["timezone"]),
            quiet_start=_parse_time(quiet["start"], "quiet_hours.start"),
            quiet_end=_parse_time(quiet["end"], "quiet_hours.end"),
            quiet_channels=tuple(quiet.get("channels", ["sms"])),
            same_content_window_hours=int(dup["same_content_window_hours"]),
            same_recipient_gap_minutes=int(dup["same_recipient_gap_minutes"]),
            banned_phrases=tuple(phrases),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            sample=sample,
            review_date_source="file" if reviewed_at else FIRST_RUN,
        )
    except (KeyError, TypeError) as exc:
        raise RulePackError(f"rule pack is missing a field: {exc}") from exc
    except ValueError as exc:
        if isinstance(exc, RulePackError):
            raise
        raise RulePackError(f"rule pack has a bad value: {exc}") from exc
    if pack.hard_max_age_days < pack.max_age_days:
        raise RulePackError("hard_max_age_days must be at least max_age_days")
    try:
        ZoneInfo(pack.quiet_timezone)
    except Exception as exc:  # zoneinfo raises several types
        raise RulePackError(f"unknown timezone {pack.quiet_timezone!r}") from exc
    return pack


def load_rule_pack(path) -> RulePack:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RulePackError(f"cannot read rule pack at {path}: {exc}") from exc
    return parse_rule_pack(text)


def _squash(text: str) -> str:
    """Casefold and collapse whitespace so line breaks cannot hide a phrase."""
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def in_quiet_hours(pack: RulePack, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(pack.quiet_timezone)).time()
    start, end = pack.quiet_start, pack.quiet_end
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def check_quiet_hours(pack: RulePack, channel: str, now: datetime, phase: str) -> CheckResult:
    code = "quiet_hours"
    window = f"{pack.quiet_start:%H:%M} to {pack.quiet_end:%H:%M} {pack.quiet_timezone}"
    if channel not in pack.quiet_channels:
        return CheckResult(code, OK, f"Quiet hours do not apply to {channel}.")
    if not in_quiet_hours(pack, now):
        return CheckResult(code, OK, f"Outside quiet hours ({window}).")
    if phase == "send":
        return CheckResult(code, STOP, f"Inside quiet hours ({window}). Send when the window opens.")
    return CheckResult(
        code, CAUTION, f"Inside quiet hours ({window}). Approval is allowed but the send will be held."
    )


def check_opt_out(facts: Facts, channel: str, recipient: str) -> CheckResult:
    code = "opt_out"
    if facts.opted_out:
        source = facts.opt_out_source or "unknown source"
        return CheckResult(code, STOP, f"{recipient} opted out of {channel} ({source}).")
    return CheckResult(code, OK, "Recipient is not on the opt out list.")


def check_duplicate(pack: RulePack, facts: Facts) -> CheckResult:
    code = "duplicate_send"
    if facts.same_content_sent_at:
        return CheckResult(
            code,
            STOP,
            f"The same text went to this recipient at {facts.same_content_sent_at} "
            f"(draft {facts.same_content_draft_id}), inside the "
            f"{pack.same_content_window_hours} hour window.",
        )
    if facts.open_twin_ids:
        ids = ", ".join(str(i) for i in facts.open_twin_ids)
        return CheckResult(code, CAUTION, f"Identical open draft(s) exist: {ids}.")
    if facts.recipient_last_sent_at:
        return CheckResult(
            code,
            CAUTION,
            f"A different message went to this recipient at {facts.recipient_last_sent_at}, "
            f"less than {pack.same_recipient_gap_minutes} minutes ago.",
        )
    return CheckResult(code, OK, "No earlier send matches this message.")


def check_banned_phrases(pack: RulePack, body: str) -> CheckResult:
    code = "banned_phrases"
    text = _squash(body)
    hits = []
    for item in pack.banned_phrases:
        if re.search(r"(?<!\w)" + re.escape(item.phrase) + r"(?!\w)", text):
            hits.append(item)
    if not hits:
        return CheckResult(code, OK, "No banned phrases found.")
    level = STOP if any(h.level == STOP for h in hits) else CAUTION
    parts = [f'"{h.phrase}" ({h.level}: {h.reason})' for h in hits]
    return CheckResult(code, level, "Found " + "; ".join(parts))


def check_rule_pack_age(pack: RulePack, now: datetime) -> CheckResult:
    code = "stale_rule_pack"
    if pack.reviewed_at is None:
        # Fail closed: an undated pack must never pass as fresh.
        return CheckResult(code, STOP, f"Rule pack {pack.version} has no review date.")
    age = (pack_today(pack, now) - pack.reviewed_at).days
    if pack.review_date_source == FIRST_RUN:
        label = f"Sample rule pack {pack.version} was dated at first run, {age} day(s) ago"
    else:
        label = f"Rule pack {pack.version} was reviewed {age} day(s) ago"
    if age < 0:
        return CheckResult(code, CAUTION, f"Rule pack {pack.version} has a review date in the future.")
    if age > pack.hard_max_age_days:
        return CheckResult(code, STOP, f"{label}; the hard limit is {pack.hard_max_age_days}. Review the pack.")
    if age > pack.max_age_days:
        return CheckResult(code, CAUTION, f"{label}; the limit is {pack.max_age_days}. Review the pack soon.")
    return CheckResult(code, OK, f"{label}.")


def pack_today(pack: RulePack, now: datetime) -> date:
    """Today's date in the pack's timezone, the calendar the review age uses."""
    return now.astimezone(ZoneInfo(pack.quiet_timezone)).date()


def run_checks(
    pack: RulePack, *, channel: str, recipient: str, body: str, facts: Facts, now: datetime, phase: str
) -> list:
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    return [
        check_quiet_hours(pack, channel, now, phase),
        check_duplicate(pack, facts),
        check_opt_out(facts, channel, recipient),
        check_banned_phrases(pack, body),
        check_rule_pack_age(pack, now),
    ]


def verdict_of(results: list) -> str:
    return max((r.level for r in results), key=_RANK.__getitem__, default=OK)


def duplicate_cutoffs(pack: RulePack, now: datetime) -> tuple:
    """Return (same content cutoff, same recipient cutoff) as datetimes."""
    return (
        now - timedelta(hours=pack.same_content_window_hours),
        now - timedelta(minutes=pack.same_recipient_gap_minutes),
    )
