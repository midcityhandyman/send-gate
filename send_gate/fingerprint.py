"""Content fingerprint and recipient normalisation.

An approval is bound to one fingerprint. The fingerprint covers exactly the
three things a human signs off on: channel, recipient and body. The body is
hashed byte for byte, with no trimming, so a changed space is a changed
message.
"""

from __future__ import annotations

import hashlib
import json
import re

CHANNELS = ("sms", "email")


class InvalidMessage(ValueError):
    """The channel, recipient or body is not usable."""


def normalize_channel(channel: str) -> str:
    value = (channel or "").strip().lower()
    if value not in CHANNELS:
        raise InvalidMessage(f"channel must be one of {', '.join(CHANNELS)}")
    return value


def normalize_recipient(channel: str, recipient: str) -> str:
    """Return the canonical form used for storage, opt out lookups and hashing."""
    raw = (recipient or "").strip()
    if channel == "sms":
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 7 or len(digits) > 15:
            raise InvalidMessage("sms recipient must contain 7 to 15 digits")
        if len(digits) == 10:
            digits = "1" + digits
        return "+" + digits
    if channel == "email":
        value = raw.lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise InvalidMessage("email recipient is not a valid address")
        return value
    raise InvalidMessage(f"unknown channel {channel!r}")


def validate_body(body: str) -> str:
    if not isinstance(body, str) or not body.strip():
        raise InvalidMessage("body must not be empty")
    return body


def content_hash(channel: str, recipient: str, body: str) -> str:
    """SHA-256 over the canonical JSON of (channel, recipient, body)."""
    payload = json.dumps([channel, recipient, body], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
