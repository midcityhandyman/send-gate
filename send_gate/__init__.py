"""send-gate: a human approval gate for agent outbound messages."""

from .adapters import MockAdapter, SendAdapter, SendError
from .gate import Gate, GateError

__all__ = ["Gate", "GateError", "MockAdapter", "SendAdapter", "SendError"]
__version__ = "0.2.0"
