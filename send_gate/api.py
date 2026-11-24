"""FastAPI surface for the gate, plus the approval page.

Two credentials, both header tokens read from the environment:

    X-Agent-Token      submit and edit drafts
    X-Approver-Token   approve, reject, change the opt out list

Recheck, send, verify and every read accept either one. Nothing but the
approver token reaches approve, so an agent credential can never approve.
The app refuses to start unless both tokens are set and differ.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .adapters import build_adapter
from .gate import Gate, GateError

ROOT = Path(__file__).resolve().parent
DEFAULT_PACK = ROOT.parent / "rules" / "default_pack.json"
MIN_TOKEN_LENGTH = 16


class ConfigError(RuntimeError):
    """The service was started without usable tokens."""


def check_tokens(agent_token: str, approver_token: str) -> None:
    for name, value in (("SEND_GATE_AGENT_TOKEN", agent_token), ("SEND_GATE_APPROVER_TOKEN", approver_token)):
        if len(value or "") < MIN_TOKEN_LENGTH:
            raise ConfigError(f"{name} must be set to at least {MIN_TOKEN_LENGTH} characters; see .env.example")
    if hmac.compare_digest(agent_token.encode("utf-8"), approver_token.encode("utf-8")):
        raise ConfigError("SEND_GATE_AGENT_TOKEN and SEND_GATE_APPROVER_TOKEN must differ")


def _matches(presented: Optional[str], expected: str) -> bool:
    """Constant time comparison. Bytes, because compare_digest rejects non-ASCII str."""
    return hmac.compare_digest((presented or "").encode("utf-8"), expected.encode("utf-8"))


class DraftIn(BaseModel):
    agent: str = Field(..., description="Name of the agent submitting the draft")
    channel: str = Field(..., description="sms or email")
    recipient: str
    body: str
    note: Optional[str] = Field(None, description="Why the agent wants to send this")


class DraftEdit(BaseModel):
    actor: str
    body: Optional[str] = None
    recipient: Optional[str] = None
    channel: Optional[str] = None


class ApproveIn(BaseModel):
    approver: str
    content_hash: str = Field(..., description="Hash of the exact text the approver reviewed")
    acknowledge_cautions: bool = False


class RejectIn(BaseModel):
    approver: str
    reason: str = ""


class ActorIn(BaseModel):
    actor: str


class OptOutIn(BaseModel):
    channel: str
    recipient: str
    source: str = "manual"


def gate_from_env() -> Gate:
    return Gate(
        db_path=os.environ.get("SEND_GATE_DB", "send_gate.db"),
        rule_pack_path=os.environ.get("SEND_GATE_RULE_PACK", str(DEFAULT_PACK)),
        adapter=build_adapter(os.environ.get("SEND_GATE_ADAPTER", "mock")),
    )


def create_app(
    gate: Optional[Gate] = None, *, agent_token: Optional[str] = None, approver_token: Optional[str] = None
) -> FastAPI:
    """Build the app. Pass a gate and tokens for tests; otherwise they come from env."""
    agent_secret = agent_token if agent_token is not None else os.environ.get("SEND_GATE_AGENT_TOKEN", "")
    approver_secret = (
        approver_token if approver_token is not None else os.environ.get("SEND_GATE_APPROVER_TOKEN", "")
    )
    check_tokens(agent_secret, approver_secret)  # before the gate, so a bad start touches no database
    gate = gate or gate_from_env()
    app = FastAPI(title="send-gate", version=__version__, description="Human approval gate for agent messages")
    app.state.gate = gate

    @app.exception_handler(GateError)
    async def gate_error_handler(_: Request, exc: GateError):
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.message, **exc.extra})

    def require_agent(x_agent_token: Optional[str] = Header(None)) -> None:
        """Writing drafts. The approver token is not accepted here."""
        if not _matches(x_agent_token, agent_secret):
            raise GateError(401, "agent_token_required", "a valid X-Agent-Token header is required")

    def require_approver(x_approver_token: Optional[str] = Header(None)) -> None:
        """Human only actions. The agent token is not accepted here, in either header."""
        if not _matches(x_approver_token, approver_secret):
            raise GateError(401, "approver_token_required", "a valid X-Approver-Token header is required")

    def require_any(
        x_agent_token: Optional[str] = Header(None), x_approver_token: Optional[str] = Header(None)
    ) -> None:
        """Reads, and actions that can only carry out what a person already approved."""
        as_agent = _matches(x_agent_token, agent_secret)
        as_approver = _matches(x_approver_token, approver_secret)
        if not (as_agent or as_approver):
            raise GateError(401, "token_required", "a valid X-Agent-Token or X-Approver-Token header is required")

    agent_only = [Depends(require_agent)]
    approver_only = [Depends(require_approver)]
    any_token = [Depends(require_any)]

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(ROOT / "static" / "index.html", media_type="text/html")

    @app.get("/healthz")
    def health():
        return {"ok": True, "adapter": gate.adapter.name, "auth_required": True}

    @app.get("/rule-pack", dependencies=any_token)
    def rule_pack():
        return gate.rule_pack().summary()

    @app.post("/drafts", status_code=201, dependencies=agent_only)
    def submit(payload: DraftIn):
        return gate.submit(payload.agent, payload.channel, payload.recipient, payload.body, payload.note)

    @app.get("/drafts", dependencies=any_token)
    def list_drafts(status: Optional[str] = None):
        return gate.list(status)

    @app.get("/drafts/{draft_id}", dependencies=any_token)
    def get_draft(draft_id: int):
        return gate.get(draft_id)

    @app.patch("/drafts/{draft_id}", dependencies=agent_only)
    def edit(draft_id: int, payload: DraftEdit):
        return gate.edit(draft_id, payload.actor, payload.body, payload.recipient, payload.channel)

    @app.post("/drafts/{draft_id}/recheck", dependencies=any_token)
    def recheck(draft_id: int):
        return gate.recheck(draft_id)

    @app.post("/drafts/{draft_id}/approve", dependencies=approver_only)
    def approve(draft_id: int, payload: ApproveIn):
        return gate.approve(draft_id, payload.approver, payload.content_hash, payload.acknowledge_cautions)

    @app.post("/drafts/{draft_id}/reject", dependencies=approver_only)
    def reject(draft_id: int, payload: RejectIn):
        return gate.reject(draft_id, payload.approver, payload.reason)

    @app.post("/drafts/{draft_id}/send", dependencies=any_token)
    def send(draft_id: int, payload: ActorIn):
        return gate.send(draft_id, payload.actor)

    @app.post("/drafts/{draft_id}/verify", dependencies=any_token)
    def verify(draft_id: int, payload: ActorIn):
        return gate.verify(draft_id, payload.actor)

    @app.get("/drafts/{draft_id}/audit", dependencies=any_token)
    def draft_audit(draft_id: int):
        return gate.audit(draft_id)

    @app.get("/audit", dependencies=any_token)
    def audit():
        return gate.audit()

    @app.get("/audit/verify", dependencies=any_token)
    def audit_verify():
        return gate.verify_audit_chain()

    @app.get("/opt-outs", dependencies=any_token)
    def opt_outs():
        return gate.list_opt_outs()

    @app.post("/opt-outs", status_code=201, dependencies=approver_only)
    def add_opt_out(payload: OptOutIn):
        return gate.add_opt_out(payload.channel, payload.recipient, payload.source)

    return app


def app_factory() -> FastAPI:
    """Entry point for: uvicorn send_gate.api:app_factory --factory"""
    return create_app()
