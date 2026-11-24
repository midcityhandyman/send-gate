"""Offline walk through of the gate.

Runs the real FastAPI app in process against an in-memory SQLite database, the
mock send adapter and a clock the script moves by hand, so the output is the
same on every run, byte for byte, on every platform. No network, no keys.
Rules come from demo/demo_pack.json. The two tokens below are demo values.

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
AGENT_TOKEN = "demo-agent-token-not-a-secret"
APPROVER_TOKEN = "demo-approver-token-not-a-secret"
AS_AGENT = {"X-Agent-Token": AGENT_TOKEN}
AS_HUMAN = {"X-Approver-Token": APPROVER_TOKEN}
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
    api = TestClient(create_app(gate, agent_token=AGENT_TOKEN, approver_token=APPROVER_TOKEN))

    def submit(body, recipient="555-555-0142", channel="sms", agent=BOT, headers=AS_AGENT, label="submit"):
        payload = {"agent": agent, "channel": channel, "recipient": recipient, "body": body}
        draft = show(label, api.post("/drafts", json=payload, headers=headers))
        if "checks" in draft and "id" in draft:
            show_checks(draft)
        return draft

    def approve(draft, ack=False, seen_hash=None, headers=AS_HUMAN, approver=HUMAN):
        payload = {"approver": approver, "content_hash": seen_hash or draft["content_hash"], "acknowledge_cautions": ack}
        return show("approve", api.post(f"/drafts/{draft['id']}/approve", json=payload, headers=headers))

    def send(draft, actor=BOT):
        return show("send", api.post(f"/drafts/{draft['id']}/send", json={"actor": actor}, headers=AS_AGENT))

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
    edited = show("edit", api.patch(f"/drafts/{second['id']}", json=edit, headers=AS_AGENT))
    out(f"      live approval after edit: {edited['approval']}")
    send(second)
    out("   The human tries to approve using the hash of the text they saw before the edit:")
    approve(edited, seen_hash=second["content_hash"])
    out("   They reload, read the new text and reject it:")
    reason = {"approver": HUMAN, "reason": "not what we agreed"}
    show("reject", api.post(f"/drafts/{second['id']}/reject", json=reason, headers=AS_HUMAN))

    out("\n3. Banned phrases: one stops the draft, one only cautions")
    third = submit("Our work is guaranteed. Book today.", recipient="pat.sample@example.com", channel="email")
    approve(third, ack=True)
    softer = {"actor": BOT, "body": "The first estimate is free."}
    fixed = show("edit", api.patch(f"/drafts/{third['id']}", json=softer, headers=AS_AGENT))
    show_checks(fixed)
    approve(fixed)
    approve(fixed, ack=True)
    send(fixed)

    out("\n4. Opt out list: a recipient who replied STOP")
    stop_reply = {"channel": "sms", "recipient": "(555) 555-0163", "source": "STOP reply"}
    refused = api.post("/opt-outs", json=stop_reply, headers=AS_AGENT)
    out(f"   add opt out with the agent token: HTTP {refused.status_code} {refused.json()['error']}")
    added = api.post("/opt-outs", json=stop_reply, headers=AS_HUMAN)
    out(f"   add opt out with the approver token: HTTP {added.status_code} added={added.json()['added']}")
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
    mismatch = api.get(f"/drafts/{seventh['id']}/audit", headers=AS_HUMAN).json()[-1]
    out(f"      target holds: {mismatch['detail']['target_body']!r}")

    out("\n8. Tokens: the agent's credential cannot approve, and nothing works without one")
    text = "Hi Morgan, your quote is ready. Reply here and we will send it over."
    submit(text, recipient="555-555-0112", headers={}, label="submit with no token")
    submit(text, recipient="555-555-0112", headers=AS_HUMAN, label="submit with the approver token")
    eighth = submit(text, recipient="555-555-0112")
    out("   The agent tries to approve its own draft, under another name, with its own token:")
    approve(eighth, headers=AS_AGENT, approver="Robin Example")
    out("   It tries again with its token in the approver header:")
    approve(eighth, headers={"X-Approver-Token": AGENT_TOKEN}, approver="Robin Example")
    listing = api.get("/drafts")
    out(f"   list drafts with no token: HTTP {listing.status_code} {listing.json()['error']}")
    out("   The human approves with the approver token:")
    approve(eighth)
    send(eighth)

    out("\n9. Adapter trouble: a draft never stays in sending and is never sent twice")
    out("   The adapter throws an error the gate was not written to expect:")
    ninth = submit("Hi Taylor, the parts arrived. We can finish on Monday.", recipient="555-555-0125")
    approve(ninth)
    adapter.raise_next = RuntimeError("connection reset by peer")
    send(ninth)
    closed = api.get(f"/drafts/{ninth['id']}", headers=AS_AGENT).json()
    out(f"      draft is now {closed['status']}")
    send(ninth)
    out("   The target accepts a message, then its read back API goes down:")
    tenth = submit("Hi Quinn, we are running 20 minutes late today.", recipient="555-555-0136")
    approve(tenth)
    adapter.read_back_error = TimeoutError("read back timed out")
    calls_before = adapter.send_calls
    parked = send(tenth)
    out(f"      send record error: {parked['sends'][0]['error']}")
    send(tenth)
    retry = {"actor": HUMAN}
    show("verify", api.post(f"/drafts/{tenth['id']}/verify", json=retry, headers=AS_HUMAN))
    adapter.read_back_error = None
    out("   The read back API returns:")
    healed = show("verify", api.post(f"/drafts/{tenth['id']}/verify", json=retry, headers=AS_HUMAN))
    out(f"      read back ok={bool(healed['sends'][0]['readback_ok'])}, "
        f"adapter send calls for this draft: {adapter.send_calls - calls_before}")

    out("\n10. Stale rule pack: nobody has reviewed the rules for a long time")
    clock.set_utc(2026, 12, 15, 15, 0)
    eleventh = submit("Hi Jamie, checking in on the estimate we sent.", recipient="555-555-0190")
    clock.set_utc(2027, 2, 1, 15, 0)
    show_checks(show("recheck", api.post(f"/drafts/{eleventh['id']}/recheck", headers=AS_AGENT)))
    approve(eleventh, ack=True)

    out("\n11. Audit trail for draft 2 (the edited one)")
    for event in api.get(f"/drafts/{second['id']}/audit", headers=AS_HUMAN).json():
        detail = {k: v for k, v in event["detail"].items() if k in ("changed", "reason", "verdict", "code", "phase")}
        out(f"   {event['ts']}  {event['actor']:15} {event['event']:16} {json.dumps(detail, sort_keys=True)}")

    out("\n12. Final state")
    for draft in reversed(api.get("/drafts", headers=AS_HUMAN).json()):
        out(f"   draft {draft['id']:2}: {draft['status']:13} verdict={draft['verdict']:7} to {draft['recipient']}")
    out(f"   messages on the mock target: {len(adapter.outbox)}")
    chain = api.get("/audit/verify", headers=AS_HUMAN).json()
    out(f"   audit chain: ok={chain['ok']} entries={chain['entries']}")

    gate.close()
    if save:
        # Bytes, so the file has LF line endings on Windows too.
        (REPO / "demo" / "output.txt").write_bytes(("\n".join(LINES) + "\n").encode("utf-8"))
    return 0 if chain["ok"] and len(adapter.outbox) == 6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
