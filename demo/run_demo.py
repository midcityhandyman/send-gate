"""Offline walk through of the gate.

Runs the real FastAPI app in process against an in-memory SQLite database, the
mock send adapter and a clock the script moves by hand, so the output is the
same on every run. No network, no keys. Rules come from demo/demo_pack.json.

    python demo/run_demo.py            # prints and saves demo/output.txt
    python demo/run_demo.py --no-save  # prints only
"""

from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")

from fastapi.testclient import TestClient  # noqa: E402

from send_gate.adapters import MockAdapter  # noqa: E402
from send_gate.api import create_app  # noqa: E402
from send_gate.gate import Gate  # noqa: E402

PACK_PATH = REPO / "demo" / "demo_pack.json"
HUMAN = "Jordan Example"
BOT = "scheduler-bot"
LINES: list = []


def out(text: str = "") -> None:
    LINES.append(text)
    print(text)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def set_utc(self, *args):
        self.value = datetime(*args, tzinfo=timezone.utc)
        out(f"   [clock] now {self.value:%Y-%m-%d %H:%M} UTC")


def show(label: str, response) -> dict:
    data = response.json()
    if response.status_code >= 400:
        out(f"   {label}: HTTP {response.status_code} {data['error']}: {data['message']}")
        for check in data.get("checks", []):
            out(f"      {check['level'].upper():7} {check['code']}: {check['detail']}")
        return data
    out(f"   {label}: HTTP {response.status_code} status={data['status']} verdict={data['verdict']}")
    return data


def show_checks(draft: dict) -> None:
    for check in draft["checks"]["results"]:
        if check["level"] != "ok":
            out(f"      {check['level'].upper():7} {check['code']}: {check['detail']}")


def main() -> int:
    save = "--no-save" not in sys.argv
    clock = Clock()
    adapter = MockAdapter(clock)
    gate = Gate(":memory:", str(PACK_PATH), adapter=adapter, clock=clock)
    api = TestClient(create_app(gate, approver_token=""))

    def submit(body, recipient="555-555-0142", channel="sms", agent=BOT):
        payload = {"agent": agent, "channel": channel, "recipient": recipient, "body": body}
        draft = show("submit", api.post("/drafts", json=payload))
        show_checks(draft)
        return draft

    def approve(draft, ack=False, seen_hash=None):
        payload = {"approver": HUMAN, "content_hash": seen_hash or draft["content_hash"], "acknowledge_cautions": ack}
        return show("approve", api.post(f"/drafts/{draft['id']}/approve", json=payload))

    def send(draft, actor=BOT):
        return show("send", api.post(f"/drafts/{draft['id']}/send", json={"actor": actor}))

    out("send-gate demo (offline, mock adapter, fixed clock)")
    out("=" * 60)
    out("   [clock] now 2026-11-10 15:00 UTC, which is 10:00 in New York")

    out("\n1. Clean draft: checks pass, human approves, gate sends and reads back")
    first = submit("Hi Alex, your visit is booked for Tuesday at 9am.")
    approve(first)
    done = send(first)
    record = done["sends"][0]
    out(f"      target id {record['provider_message_id']} at {record['target_timestamp']}, "
        f"read back ok={bool(record['readback_ok'])}")

    out("\n2. Edit after approval: the approval is void, the send is refused")
    second = submit("Hi Sam, the crew arrives Wednesday between 8 and 10.", recipient="555-555-0157")
    approve(second)
    edit = {"actor": BOT, "body": "Hi Sam, the crew arrives Wednesday between 8 and 10. Please pay in cash."}
    edited = show("edit", api.patch(f"/drafts/{second['id']}", json=edit))
    out(f"      live approval after edit: {edited['approval']}")
    send(second)
    out("   The human tries to approve using the hash of the text they saw before the edit:")
    approve(edited, seen_hash=second["content_hash"])
    out("   They reload, read the new text and reject it:")
    show("reject", api.post(f"/drafts/{second['id']}/reject", json={"approver": HUMAN, "reason": "not what we agreed"}))

    out("\n3. Banned phrases: one stops the draft, one only cautions")
    third = submit("Our work is guaranteed. Book today.", recipient="pat.sample@example.com", channel="email")
    approve(third, ack=True)
    fixed = show("edit", api.patch(f"/drafts/{third['id']}", json={"actor": BOT, "body": "The first estimate is free."}))
    show_checks(fixed)
    approve(fixed)
    approve(fixed, ack=True)
    send(fixed)

    out("\n4. Opt out list: a recipient who replied STOP")
    api.post("/opt-outs", json={"channel": "sms", "recipient": "(555) 555-0163", "source": "STOP reply"})
    fourth = submit("Hi Riley, a quick reminder about Friday.", recipient="555-555-0163")
    approve(fourth)

    out("\n5. Duplicate send: the same text to the same recipient two hours later")
    clock.set_utc(2026, 11, 10, 17, 0)
    fifth = submit("Hi Alex, your visit is booked for Tuesday at 9am.")
    approve(fifth)

    out("\n6. Quiet hours: approval is allowed at night, the send is held until morning")
    clock.set_utc(2026, 11, 11, 3, 30)
    sixth = submit("Hi Casey, we can start Thursday. Does 8am work?", recipient="555-555-0171")
    approve(sixth, ack=True)
    send(sixth)
    clock.set_utc(2026, 11, 11, 13, 5)
    send(sixth)

    out("\n7. Read back: the target stored something other than the approved text")
    seventh = submit("Hi Drew, your invoice total is 240 dollars, due on the 30th.", recipient="555-555-0188")
    approve(seventh)
    adapter.alter_next = lambda body: body.replace("240", "2400")
    bad = send(seventh)
    out(f"      send record error: {bad['sends'][0]['error']}")
    mismatch = api.get(f"/drafts/{seventh['id']}/audit").json()[-1]
    out(f"      target holds: {mismatch['detail']['target_body']!r}")

    out("\n8. Stale rule pack: nobody has reviewed the rules for a long time")
    clock.set_utc(2026, 12, 15, 15, 0)
    eighth = submit("Hi Jamie, checking in on the estimate we sent.", recipient="555-555-0190")
    clock.set_utc(2027, 2, 1, 15, 0)
    show_checks(show("recheck", api.post(f"/drafts/{eighth['id']}/recheck")))
    approve(eighth, ack=True)

    out("\n9. Audit trail for draft 2 (the edited one)")
    for event in api.get(f"/drafts/{second['id']}/audit").json():
        detail = {k: v for k, v in event["detail"].items() if k in ("changed", "reason", "verdict", "code", "phase")}
        out(f"   {event['ts']}  {event['actor']:15} {event['event']:16} {json.dumps(detail, sort_keys=True)}")

    out("\n10. Final state")
    for draft in reversed(api.get("/drafts").json()):
        out(f"   draft {draft['id']}: {draft['status']:13} verdict={draft['verdict']:7} to {draft['recipient']}")
    out(f"   messages on the mock target: {len(adapter.outbox)}")
    chain = api.get("/audit/verify").json()
    out(f"   audit chain: ok={chain['ok']} entries={chain['entries']}")

    gate.close()
    if save:
        (REPO / "demo" / "output.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
    return 0 if chain["ok"] and len(adapter.outbox) == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
