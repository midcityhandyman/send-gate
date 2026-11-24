"""Send adapters.

An adapter does two things: it hands a message to a target system, and it
reads a message back from that target by id. The gate never trusts the
adapter's own return value as proof of what was delivered. It calls
read_back and compares what the target holds against the approved text.

Only a mock adapter ships here. It keeps an in-memory "target" and has
switches to simulate the failures a real provider can produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol


class SendError(Exception):
    """The target refused the message or could not be reached."""


@dataclass(frozen=True)
class SendReceipt:
    message_id: str


@dataclass(frozen=True)
class TargetMessage:
    """A message as the target system reports it."""

    message_id: str
    channel: str
    recipient: str
    body: str
    timestamp: str


class SendAdapter(Protocol):
    name: str

    def send(self, *, channel: str, recipient: str, body: str, idempotency_key: str) -> SendReceipt: ...

    def read_back(self, message_id: str) -> Optional[TargetMessage]: ...


class MockAdapter:
    """Offline stand-in for an SMS or email provider."""

    name = "mock"

    def __init__(self, clock: Optional[Callable[[], datetime]] = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._messages: dict = {}
        self._by_key: dict = {}
        self._counter = 0
        self.fail_next: Optional[str] = None
        self.alter_next: Optional[Callable[[str], str]] = None
        self.lose_next = False

    def send(self, *, channel: str, recipient: str, body: str, idempotency_key: str) -> SendReceipt:
        if self.fail_next:
            reason, self.fail_next = self.fail_next, None
            raise SendError(reason)
        if idempotency_key in self._by_key:
            return SendReceipt(self._by_key[idempotency_key])
        self._counter += 1
        message_id = f"mock-{self._counter:06d}"
        stored_body = body
        if self.alter_next:
            stored_body, self.alter_next = self.alter_next(body), None
        stamp = self._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.lose_next:
            self.lose_next = False
        else:
            self._messages[message_id] = TargetMessage(message_id, channel, recipient, stored_body, stamp)
        self._by_key[idempotency_key] = message_id
        return SendReceipt(message_id)

    def read_back(self, message_id: str) -> Optional[TargetMessage]:
        return self._messages.get(message_id)

    @property
    def outbox(self) -> list:
        return list(self._messages.values())


def build_adapter(name: str, clock: Optional[Callable[[], datetime]] = None) -> SendAdapter:
    if (name or "mock").lower() == "mock":
        return MockAdapter(clock)
    raise ValueError(f"unknown adapter {name!r}; only 'mock' ships with this repository")
