"""FastAPI surface for the gate, plus the approval page."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .adapters import build_adapter
from .gate import Gate, GateError

ROOT = Path(__file__).resolve().parent
DEFAULT_PACK = ROOT.parent / "rules" / "default_pack.json"


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


def create_app(gate: Optional[Gate] = None, approver_token: Optional[str] = None) -> FastAPI:
    """Build the app. Pass a gate for tests; otherwise it is built from env."""
    gate = gate or gate_from_env()
    token = approver_token if approver_token is not None else os.environ.get("SEND_GATE_APPROVER_TOKEN", "")
    app = FastAPI(title="send-gate", version="0.1.0", description="Human approval gate for agent messages")
    app.state.gate = gate

    @app.exception_handler(GateError)
    async def gate_error_handler(_: Request, exc: GateError):
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.message, **exc.extra})

    def require_approver(x_approver_token: Optional[str] = Header(None)) -> None:
        """Human only actions. Enforced when a token is configured."""
        if token and not hmac.compare_digest(x_approver_token or "", token):
            raise GateError(401, "approver_token_required", "a valid X-Approver-Token header is required")

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(ROOT / "static" / "index.html", media_type="text/html")

    @app.get("/healthz")
    def health():
        return {"ok": True, "adapter": gate.adapter.name, "approver_token_required": bool(token)}

    @app.get("/rule-pack")
    def rule_pack():
        return gate.rule_pack().summary()

    @app.post("/drafts", status_code=201)
    def submit(payload: DraftIn):
        return gate.submit(payload.agent, payload.channel, payload.recipient, payload.body, payload.note)

    @app.get("/drafts")
    def list_drafts(status: Optional[str] = None):
        return gate.list(status)

    @app.get("/drafts/{draft_id}")
    def get_draft(draft_id: int):
        return gate.get(draft_id)

    @app.patch("/drafts/{draft_id}")
    def edit(draft_id: int, payload: DraftEdit):
        return gate.edit(draft_id, payload.actor, payload.body, payload.recipient, payload.channel)

    @app.post("/drafts/{draft_id}/recheck")
    def recheck(draft_id: int):
        return gate.recheck(draft_id)

    @app.post("/drafts/{draft_id}/approve", dependencies=[Depends(require_approver)])
    def approve(draft_id: int, payload: ApproveIn):
        return gate.approve(draft_id, payload.approver, payload.content_hash, payload.acknowledge_cautions)

    @app.post("/drafts/{draft_id}/reject", dependencies=[Depends(require_approver)])
    def reject(draft_id: int, payload: RejectIn):
        return gate.reject(draft_id, payload.approver, payload.reason)

    @app.post("/drafts/{draft_id}/send")
    def send(draft_id: int, payload: ActorIn):
        return gate.send(draft_id, payload.actor)

    @app.get("/drafts/{draft_id}/audit")
    def draft_audit(draft_id: int):
        return gate.audit(draft_id)

    @app.get("/audit")
    def audit():
        return gate.audit()

    @app.get("/audit/verify")
    def audit_verify():
        return gate.verify_audit_chain()

    @app.get("/opt-outs")
    def opt_outs():
        return gate.list_opt_outs()

    @app.post("/opt-outs", status_code=201)
    def add_opt_out(payload: OptOutIn):
        return gate.add_opt_out(payload.channel, payload.recipient, payload.source)

    return app


def app_factory() -> FastAPI:
    """Entry point for: uvicorn send_gate.api:app_factory --factory"""
    return create_app()
