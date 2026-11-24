"""The gate: draft lifecycle, approval binding, send and read back.

Lifecycle of a draft:

    pending -> approved -> sending -> verified
       |          |           |-> send_failed      adapter refused or broke
       |          |           |-> verify_failed    target holds other text, or none
       |          |           `-> unverified       sent, read back did not answer
       `----------`-> rejected                         |
                                                       `-> verify() retries the
                                                           read back, never the send

No draft rests in sending. Whatever the adapter does, the send ends in one of
the four states on the right, and a draft left in sending by a killed process
is settled by verify().

Rules that hold everywhere in this module:

* An approval is bound to one content hash (channel, recipient, body).
* Any edit that changes that hash voids the approval and returns the draft
  to pending. The hash is also recomputed from the stored row right before
  a send, so a change made behind the gate's back is caught too.
* A stop verdict blocks approval and blocks the send.
* After the adapter accepts a message the gate reads it back from the
  target and compares it to the approved hash before calling it verified.
* A draft is handed to the adapter at most once. Nothing after the claim
  puts it back to approved.
* Every step, including every refusal, lands in the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Callable, Optional

from . import rules
from .adapters import MockAdapter, SendAdapter, SendError
from .db import connect
from .fingerprint import (
    InvalidMessage,
    content_hash,
    normalize_channel,
    normalize_recipient,
    validate_body,
)

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
SENDING, VERIFIED = "sending", "verified"
SEND_FAILED, VERIFY_FAILED, UNVERIFIED = "send_failed", "verify_failed", "unverified"
STATUSES = (PENDING, APPROVED, REJECTED, SENDING, VERIFIED, SEND_FAILED, VERIFY_FAILED, UNVERIFIED)
EDITABLE = (PENDING, APPROVED)

SAMPLE_REVIEW_KEY = "sample_pack_reviewed_at"

GENESIS_HASH = "0" * 64


class GateError(Exception):
    """A refusal with an HTTP style status and a stable machine code."""

    def __init__(
        self, status: int, code: str, message: str, extra: Optional[dict] = None, rollback: bool = False
    ):
        super().__init__(message)
        self.rollback = rollback
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_name(value: str, field: str) -> str:
    name = (value or "").strip()
    if not name:
        raise GateError(422, "missing_field", f"{field} is required")
    return name


class Gate:
    def __init__(
        self,
        db_path: str,
        rule_pack_path: str,
        adapter: Optional[SendAdapter] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = connect(db_path)
        self._lock = threading.RLock()
        self._pack_path = rule_pack_path
        self.adapter = adapter or MockAdapter(self._clock)
        rules.load_rule_pack(rule_pack_path)  # fail fast on a broken pack
        with self._tx():
            self._pack()  # dates a sample pack on the first run with this database

    # ------------------------------------------------------------------ helpers

    def close(self) -> None:
        self._conn.close()

    def now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def rule_pack(self) -> rules.RulePack:
        """The pack in force, with its review date resolved."""
        with self._tx():
            return self._pack()

    def _pack(self) -> rules.RulePack:
        """Read the pack fresh each time so an edited file takes effect at once.

        A real pack carries its own review date and is returned as read. A
        sample pack ("sample": true, "reviewed_at": "first_run") has no date
        in the file; its date is the day this database first saw a sample
        pack, stored in the settings table. From there it ages like any
        other pack. Call inside _tx.
        """
        try:
            pack = rules.load_rule_pack(self._pack_path)
        except rules.RulePackError as exc:
            raise GateError(503, "rule_pack_unavailable", str(exc), rollback=True) from exc
        if pack.reviewed_at is None:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (SAMPLE_REVIEW_KEY,)).fetchone()
            if row is None:
                reviewed = self._date_sample_pack(pack, "gate", "first run with a sample rule pack")
            else:
                reviewed = date.fromisoformat(row["value"])
            pack = replace(pack, reviewed_at=reviewed)
        return pack

    def _date_sample_pack(self, pack: rules.RulePack, actor: str, reason: str) -> date:
        today = rules.pack_today(pack, self.now())
        self._conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SAMPLE_REVIEW_KEY, today.isoformat(), iso(self.now())),
        )
        self._audit(
            None,
            actor,
            "sample_pack_dated",
            {"reviewed_at": today.isoformat(), "rule_pack": pack.version, "pack_sha256": pack.sha256, "reason": reason},
        )
        return today

    def restamp_sample_pack(self, actor: str) -> dict:
        """Record that a person re-read the sample pack today.

        Only for a sample pack. A real pack is reviewed by editing
        reviewed_at in its file, so the date travels with the rules.
        """
        with self._tx():
            actor = _require_name(actor, "actor")
            pack = self._pack()
            if pack.review_date_source != rules.FIRST_RUN:
                raise GateError(
                    409, "not_a_sample_pack", "this pack carries its own review date; edit reviewed_at in the file"
                )
            reviewed = self._date_sample_pack(pack, actor, "sample pack reviewed again")
            return replace(pack, reviewed_at=reviewed).summary()

    @contextmanager
    def _tx(self):
        """Serialise one operation. Refusals still commit their audit rows.

        The exception is a refusal marked rollback (the rule pack could not
        be read): nothing may be stored that was never checked.
        """
        with self._lock:
            try:
                yield
            except GateError as exc:
                if exc.rollback:
                    self._conn.rollback()
                else:
                    self._conn.commit()
                raise
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _audit(self, draft_id: Optional[int], actor: str, event: str, detail: Optional[dict] = None) -> None:
        detail_json = json.dumps(detail or {}, sort_keys=True, ensure_ascii=False)
        ts = iso(self.now())
        row = self._conn.execute("SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["entry_hash"] if row else GENESIS_HASH
        entry = _chain_hash(prev, draft_id, ts, actor, event, detail_json)
        self._conn.execute(
            "INSERT INTO audit_events (draft_id, ts, actor, event, detail, prev_hash, entry_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (draft_id, ts, actor, event, detail_json, prev, entry),
        )

    def _draft(self, draft_id: int):
        row = self._conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            raise GateError(404, "not_found", f"draft {draft_id} does not exist")
        return row

    def _active_approval(self, draft_id: int):
        return self._conn.execute(
            "SELECT * FROM approvals WHERE draft_id = ? AND voided_at IS NULL ORDER BY id DESC LIMIT 1",
            (draft_id,),
        ).fetchone()

    def _void_approval(self, draft_id: int, actor: str, reason: str) -> bool:
        approval = self._active_approval(draft_id)
        if approval is None:
            return False
        self._conn.execute(
            "UPDATE approvals SET voided_at = ?, void_reason = ? WHERE id = ?",
            (iso(self.now()), reason, approval["id"]),
        )
        self._audit(
            draft_id,
            actor,
            "approval_voided",
            {"approval_id": approval["id"], "approver": approval["approver"], "reason": reason},
        )
        return True

    def _set_status(self, draft_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?", (status, iso(self.now()), draft_id)
        )

    def _facts(self, draft, pack: rules.RulePack) -> rules.Facts:
        now = self.now()
        content_cutoff, recipient_cutoff = rules.duplicate_cutoffs(pack, now)
        facts = rules.Facts()
        opt = self._conn.execute(
            "SELECT source FROM opt_outs WHERE channel = ? AND recipient = ?",
            (draft["channel"], draft["recipient"]),
        ).fetchone()
        if opt:
            facts.opted_out, facts.opt_out_source = True, opt["source"]
        same = self._conn.execute(
            "SELECT draft_id, sent_at FROM sends WHERE content_hash = ? AND draft_id != ? "
            "AND provider_message_id IS NOT NULL AND sent_at >= ? ORDER BY sent_at DESC LIMIT 1",
            (draft["content_hash"], draft["id"], iso(content_cutoff)),
        ).fetchone()
        if same:
            facts.same_content_sent_at, facts.same_content_draft_id = same["sent_at"], same["draft_id"]
        recent = self._conn.execute(
            "SELECT s.sent_at FROM sends s JOIN drafts d ON d.id = s.draft_id "
            "WHERE d.channel = ? AND d.recipient = ? AND s.draft_id != ? AND s.content_hash != ? "
            "AND s.provider_message_id IS NOT NULL AND s.sent_at >= ? ORDER BY s.sent_at DESC LIMIT 1",
            (draft["channel"], draft["recipient"], draft["id"], draft["content_hash"], iso(recipient_cutoff)),
        ).fetchone()
        if recent:
            facts.recipient_last_sent_at = recent["sent_at"]
        twins = self._conn.execute(
            "SELECT id FROM drafts WHERE content_hash = ? AND id != ? AND status IN (?, ?, ?) ORDER BY id",
            (draft["content_hash"], draft["id"], PENDING, APPROVED, SENDING),
        ).fetchall()
        facts.open_twin_ids = [t["id"] for t in twins]
        return facts

    def _run_checks(self, draft_id: int, phase: str, actor: str) -> tuple:
        """Run every rule, store the run, update the draft verdict."""
        draft = self._draft(draft_id)
        pack = self._pack()
        results = rules.run_checks(
            pack,
            channel=draft["channel"],
            recipient=draft["recipient"],
            body=draft["body"],
            facts=self._facts(draft, pack),
            now=self.now(),
            phase=phase,
        )
        verdict = rules.verdict_of(results)
        self._conn.execute(
            "INSERT INTO check_runs (draft_id, phase, ran_at, verdict, content_hash, pack_version, "
            "pack_sha256, results) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                phase,
                iso(self.now()),
                verdict,
                draft["content_hash"],
                pack.version,
                pack.sha256,
                json.dumps([r.to_dict() for r in results]),
            ),
        )
        self._conn.execute("UPDATE drafts SET verdict = ? WHERE id = ?", (verdict, draft_id))
        flagged = {r.code: r.level for r in results if r.level != rules.OK}
        self._audit(
            draft_id,
            actor,
            "checks_run",
            {"phase": phase, "verdict": verdict, "flagged": flagged, "rule_pack": pack.version},
        )
        return verdict, results

    # --------------------------------------------------------------- operations

    def submit(self, agent: str, channel: str, recipient: str, body: str, note: Optional[str] = None) -> dict:
        with self._tx():
            agent = _require_name(agent, "agent")
            try:
                channel = normalize_channel(channel)
                recipient = normalize_recipient(channel, recipient)
                body = validate_body(body)
            except InvalidMessage as exc:
                raise GateError(422, "invalid_message", str(exc)) from exc
            digest = content_hash(channel, recipient, body)
            stamp = iso(self.now())
            cursor = self._conn.execute(
                "INSERT INTO drafts (agent, channel, recipient, body, note, status, verdict, content_hash, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent, channel, recipient, body, note, PENDING, rules.OK, digest, stamp, stamp),
            )
            draft_id = cursor.lastrowid
            self._audit(
                draft_id,
                agent,
                "draft_submitted",
                {"channel": channel, "recipient": recipient, "content_hash": digest, "body": body},
            )
            self._run_checks(draft_id, "submit", "gate")
            return self._view(draft_id)

    def edit(
        self,
        draft_id: int,
        actor: str,
        body: Optional[str] = None,
        recipient: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """Change body, recipient or channel. Any real change voids the approval."""
        with self._tx():
            actor = _require_name(actor, "actor")
            draft = self._draft(draft_id)
            if draft["status"] not in EDITABLE:
                raise GateError(409, "not_editable", f"a {draft['status']} draft cannot be edited")
            try:
                new_channel = normalize_channel(channel) if channel is not None else draft["channel"]
                raw_recipient = recipient if recipient is not None else draft["recipient"]
                new_recipient = normalize_recipient(new_channel, raw_recipient)
                new_body = validate_body(body) if body is not None else draft["body"]
            except InvalidMessage as exc:
                raise GateError(422, "invalid_message", str(exc)) from exc
            digest = content_hash(new_channel, new_recipient, new_body)
            if digest == draft["content_hash"]:
                return self._view(draft_id)
            changed = [
                name
                for name, old, new in (
                    ("channel", draft["channel"], new_channel),
                    ("recipient", draft["recipient"], new_recipient),
                    ("body", draft["body"], new_body),
                )
                if old != new
            ]
            self._conn.execute(
                "UPDATE drafts SET channel = ?, recipient = ?, body = ?, content_hash = ?, status = ?, "
                "updated_at = ? WHERE id = ?",
                (new_channel, new_recipient, new_body, digest, PENDING, iso(self.now()), draft_id),
            )
            self._audit(
                draft_id,
                actor,
                "draft_edited",
                {
                    "changed": changed,
                    "old_hash": draft["content_hash"],
                    "new_hash": digest,
                    "channel": new_channel,
                    "recipient": new_recipient,
                    "body": new_body,
                },
            )
            self._void_approval(draft_id, "gate", "content changed: " + ", ".join(changed))
            self._run_checks(draft_id, "submit", "gate")
            return self._view(draft_id)

    def recheck(self, draft_id: int, actor: str = "gate") -> dict:
        with self._tx():
            draft = self._draft(draft_id)
            if draft["status"] not in EDITABLE:
                raise GateError(409, "not_open", f"a {draft['status']} draft is closed")
            self._run_checks(draft_id, "submit", actor)
            return self._view(draft_id)

    def approve(self, draft_id: int, approver: str, seen_hash: str, acknowledge_cautions: bool = False) -> dict:
        """Approve the exact text the approver was shown.

        seen_hash is the content hash displayed next to the text the human
        read. If the draft changed in between, the hashes differ and the
        approval is refused.
        """
        with self._tx():
            approver = _require_name(approver, "approver")
            draft = self._draft(draft_id)

            def refuse(status: int, code: str, message: str, extra: Optional[dict] = None):
                self._audit(draft_id, approver, "approve_refused", {"code": code, "reason": message})
                raise GateError(status, code, message, extra)

            if draft["status"] != PENDING:
                refuse(409, "not_pending", f"draft is {draft['status']}, not pending")
            if approver.casefold() == draft["agent"].casefold():
                refuse(403, "self_approval", "the submitting agent cannot approve its own draft")
            current = content_hash(draft["channel"], draft["recipient"], draft["body"])
            if (seen_hash or "").strip().lower() != current:
                refuse(
                    409,
                    "text_changed",
                    "the text you reviewed is not the current text; reload and review again",
                    {"current_hash": current},
                )
            verdict, results = self._run_checks(draft_id, "approve", "gate")
            flagged = [r.to_dict() for r in results if r.level != rules.OK]
            if verdict == rules.STOP:
                refuse(409, "blocked_by_rules", "a rule check returned stop", {"checks": flagged})
            if verdict == rules.CAUTION and not acknowledge_cautions:
                refuse(409, "cautions_not_acknowledged", "acknowledge the cautions to approve", {"checks": flagged})
            stamp = iso(self.now())
            cursor = self._conn.execute(
                "INSERT INTO approvals (draft_id, approver, content_hash, acknowledged_cautions, approved_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (draft_id, approver, current, int(verdict == rules.CAUTION), stamp),
            )
            self._set_status(draft_id, APPROVED)
            self._audit(
                draft_id,
                approver,
                "approved",
                {
                    "approval_id": cursor.lastrowid,
                    "content_hash": current,
                    "verdict": verdict,
                    "acknowledged": [c["code"] for c in flagged],
                },
            )
            return self._view(draft_id)

    def reject(self, draft_id: int, approver: str, reason: str = "") -> dict:
        with self._tx():
            approver = _require_name(approver, "approver")
            draft = self._draft(draft_id)
            if draft["status"] not in EDITABLE:
                raise GateError(409, "not_open", f"a {draft['status']} draft cannot be rejected")
            self._void_approval(draft_id, approver, "draft rejected")
            self._set_status(draft_id, REJECTED)
            self._audit(draft_id, approver, "rejected", {"reason": reason})
            return self._view(draft_id)

    def send(self, draft_id: int, actor: str) -> dict:
        """Send an approved draft, then read it back from the target."""
        with self._tx():
            actor = _require_name(actor, "actor")
            draft = self._draft(draft_id)

            def refuse(code: str, message: str, extra: Optional[dict] = None):
                self._audit(draft_id, actor, "send_refused", {"code": code, "reason": message})
                raise GateError(409, code, message, extra)

            if draft["status"] != APPROVED:
                refuse("not_approved", f"draft is {draft['status']}, not approved")
            approval = self._active_approval(draft_id)
            if approval is None:
                self._set_status(draft_id, PENDING)
                refuse("no_approval", "no live approval exists for this draft")
            current = content_hash(draft["channel"], draft["recipient"], draft["body"])
            if current != approval["content_hash"] or current != draft["content_hash"]:
                self._void_approval(draft_id, "gate", "stored content no longer matches the approved hash")
                self._conn.execute("UPDATE drafts SET content_hash = ? WHERE id = ?", (current, draft_id))
                self._set_status(draft_id, PENDING)
                refuse("approval_void", "content changed after approval; a new approval is needed")
            verdict, results = self._run_checks(draft_id, "send", "gate")
            if verdict == rules.STOP:
                flagged = [r.to_dict() for r in results if r.level == rules.STOP]
                refuse("blocked_by_rules", "a rule check returned stop at send time", {"checks": flagged})

            claimed = self._conn.execute(
                "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (SENDING, iso(self.now()), draft_id, APPROVED),
            )
            if claimed.rowcount != 1:
                refuse("not_approved", "another send already claimed this draft")
            key = f"draft-{draft_id}-{current[:16]}"
            cursor = self._conn.execute(
                "INSERT INTO sends (draft_id, approval_id, content_hash, idempotency_key, adapter, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (draft_id, approval["id"], current, key, self.adapter.name, iso(self.now())),
            )
            send_id = cursor.lastrowid
            self._audit(
                draft_id,
                actor,
                "send_started",
                {"send_id": send_id, "adapter": self.adapter.name, "approval_id": approval["id"]},
            )
            self._conn.commit()  # the claim survives a crash inside the adapter

            try:
                receipt = self.adapter.send(
                    channel=draft["channel"], recipient=draft["recipient"], body=draft["body"], idempotency_key=key
                )
                message_id = receipt.message_id
                if not isinstance(message_id, str) or not message_id:
                    raise TypeError("adapter returned no message id")
            except SendError as exc:
                self._close_failed_send(draft_id, send_id, str(exc))
                raise GateError(502, "send_failed", f"adapter refused the message: {exc}") from exc
            except Exception as exc:
                # Anything else the adapter throws: a bug, a dropped socket, a
                # malformed receipt. The message may or may not have gone out,
                # so the draft is closed the same way and never retried here.
                error = f"unexpected adapter error ({type(exc).__name__}): {exc}"
                self._close_failed_send(draft_id, send_id, error, unexpected=True)
                raise GateError(
                    502, "send_failed", f"{error}; it is unknown whether the message went out"
                ) from exc

            sent_at = iso(self.now())
            self._conn.execute(
                "UPDATE sends SET provider_message_id = ?, sent_at = ? WHERE id = ?",
                (message_id, sent_at, send_id),
            )
            self._audit(draft_id, "gate", "sent", {"send_id": send_id, "provider_message_id": message_id})
            self._conn.commit()  # the target id survives a crash inside the read back
            self._read_back(draft_id, send_id, message_id, current)
            return self._view(draft_id)

    def _close_failed_send(self, draft_id: int, send_id: Optional[int], error: str, **detail) -> None:
        if send_id is not None:
            self._conn.execute("UPDATE sends SET error = ? WHERE id = ?", (error, send_id))
        self._set_status(draft_id, SEND_FAILED)
        self._audit(draft_id, "gate", "send_failed", {"send_id": send_id, "error": error, **detail})

    def verify(self, draft_id: int, actor: str) -> dict:
        """Finish a send that did not reach a final state. Never calls adapter.send.

        unverified: the target accepted the message but the read back raised.
        Reading is safe to repeat, so this tries it again.

        sending: only a killed process leaves a draft here, because every
        send runs to a final state under the gate's lock. If the target id
        was recorded the read back is retried. If not, the gate cannot know
        whether the message went out and closes the draft as send_failed.
        """
        with self._tx():
            actor = _require_name(actor, "actor")
            draft = self._draft(draft_id)
            if draft["status"] not in (UNVERIFIED, SENDING):
                raise GateError(409, "nothing_to_verify", f"draft is {draft['status']}; there is no open send")
            send = self._conn.execute(
                "SELECT * FROM sends WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (draft_id,)
            ).fetchone()
            if send is None or not send["provider_message_id"]:
                self._close_failed_send(
                    draft_id,
                    send["id"] if send else None,
                    "interrupted before the adapter answered; it is unknown whether the message went out",
                    settled_by=actor,
                )
                return self._view(draft_id)
            self._audit(
                draft_id,
                actor,
                "readback_retry",
                {"send_id": send["id"], "provider_message_id": send["provider_message_id"]},
            )
            self._read_back(draft_id, send["id"], send["provider_message_id"], send["content_hash"])
            return self._view(draft_id)

    def _read_back(self, draft_id: int, send_id: int, message_id: str, approved_hash: str) -> None:
        """Fetch the message from the target and compare it to what was approved."""
        try:
            seen = self.adapter.read_back(message_id)
            seen_hash = content_hash(seen.channel, seen.recipient, seen.body) if seen is not None else None
        except Exception as exc:
            # The message is out but unconfirmed. Not a failure of the text,
            # so not verify_failed: park it where verify() can try again.
            error = f"read back failed ({type(exc).__name__}): {exc}"
            self._conn.execute(
                "UPDATE sends SET readback_at = ?, error = ? WHERE id = ?", (iso(self.now()), error, send_id)
            )
            self._set_status(draft_id, UNVERIFIED)
            self._audit(
                draft_id,
                "gate",
                "readback_error",
                {"send_id": send_id, "provider_message_id": message_id, "error": error},
            )
            return
        stamp = iso(self.now())
        if seen is None:
            self._conn.execute(
                "UPDATE sends SET readback_at = ?, readback_ok = 0, error = ? WHERE id = ?",
                (stamp, "message not found on target", send_id),
            )
            self._set_status(draft_id, VERIFY_FAILED)
            self._audit(
                draft_id,
                "gate",
                "readback_failed",
                {"send_id": send_id, "provider_message_id": message_id, "reason": "not found on target"},
            )
            return
        matches = seen_hash == approved_hash and seen.message_id == message_id
        self._conn.execute(
            "UPDATE sends SET readback_at = ?, target_timestamp = ?, readback_hash = ?, readback_ok = ?, "
            "error = NULL WHERE id = ?",
            (stamp, seen.timestamp, seen_hash, int(matches), send_id),
        )
        detail = {
            "send_id": send_id,
            "provider_message_id": seen.message_id,
            "target_timestamp": seen.timestamp,
            "readback_hash": seen_hash,
        }
        if matches:
            self._set_status(draft_id, VERIFIED)
            self._audit(draft_id, "gate", "readback_verified", detail)
        else:
            detail["approved_hash"] = approved_hash
            detail["target_body"] = seen.body
            self._conn.execute(
                "UPDATE sends SET error = ? WHERE id = ?", ("target content differs from approved text", send_id)
            )
            self._set_status(draft_id, VERIFY_FAILED)
            self._audit(draft_id, "gate", "readback_mismatch", detail)

    # ------------------------------------------------------------------ opt outs

    def add_opt_out(self, channel: str, recipient: str, source: str = "manual") -> dict:
        with self._tx():
            try:
                channel = normalize_channel(channel)
                recipient = normalize_recipient(channel, recipient)
            except InvalidMessage as exc:
                raise GateError(422, "invalid_message", str(exc)) from exc
            source = (source or "manual").strip() or "manual"
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO opt_outs (channel, recipient, source, created_at) VALUES (?, ?, ?, ?)",
                (channel, recipient, source, iso(self.now())),
            )
            if cursor.rowcount:
                self._audit(None, source, "opt_out_added", {"channel": channel, "recipient": recipient})
            return {"channel": channel, "recipient": recipient, "source": source, "added": bool(cursor.rowcount)}

    def list_opt_outs(self) -> list:
        with self._lock:
            rows = self._conn.execute("SELECT channel, recipient, source, created_at FROM opt_outs ORDER BY id")
            return [dict(r) for r in rows.fetchall()]

    # --------------------------------------------------------------------- reads

    def get(self, draft_id: int) -> dict:
        with self._lock:
            return self._view(draft_id)

    def list(self, status: Optional[str] = None) -> list:
        with self._lock:
            if status:
                if status not in STATUSES:
                    raise GateError(422, "bad_status", f"status must be one of {', '.join(STATUSES)}")
                rows = self._conn.execute("SELECT id FROM drafts WHERE status = ? ORDER BY id DESC", (status,))
            else:
                rows = self._conn.execute("SELECT id FROM drafts ORDER BY id DESC")
            return [self._view(r["id"]) for r in rows.fetchall()]

    def _view(self, draft_id: int) -> dict:
        draft = dict(self._draft(draft_id))
        run = self._conn.execute(
            "SELECT * FROM check_runs WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (draft_id,)
        ).fetchone()
        draft["checks"] = None
        if run:
            draft["checks"] = {
                "phase": run["phase"],
                "ran_at": run["ran_at"],
                "verdict": run["verdict"],
                "rule_pack_version": run["pack_version"],
                "results": json.loads(run["results"]),
            }
        approval = self._active_approval(draft_id)
        draft["approval"] = dict(approval) if approval else None
        sends = self._conn.execute("SELECT * FROM sends WHERE draft_id = ? ORDER BY id", (draft_id,)).fetchall()
        draft["sends"] = [dict(s) for s in sends]
        return draft

    def audit(self, draft_id: Optional[int] = None) -> list:
        with self._lock:
            if draft_id is None:
                rows = self._conn.execute("SELECT * FROM audit_events ORDER BY id")
            else:
                self._draft(draft_id)
                rows = self._conn.execute("SELECT * FROM audit_events WHERE draft_id = ? ORDER BY id", (draft_id,))
            out = []
            for row in rows.fetchall():
                item = dict(row)
                item["detail"] = json.loads(item["detail"])
                out.append(item)
            return out

    def verify_audit_chain(self) -> dict:
        """Recompute the hash chain. Any edited, removed or reordered row breaks it."""
        with self._lock:
            prev = GENESIS_HASH
            count = 0
            for row in self._conn.execute("SELECT * FROM audit_events ORDER BY id"):
                expected = _chain_hash(prev, row["draft_id"], row["ts"], row["actor"], row["event"], row["detail"])
                if row["prev_hash"] != prev or row["entry_hash"] != expected:
                    return {"ok": False, "entries": count, "broken_at_id": row["id"]}
                prev = row["entry_hash"]
                count += 1
            return {"ok": True, "entries": count, "head": prev}


def _chain_hash(prev: str, draft_id, ts: str, actor: str, event: str, detail_json: str) -> str:
    payload = json.dumps([prev, draft_id, ts, actor, event, detail_json], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
