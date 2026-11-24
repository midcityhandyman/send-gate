import pytest

from send_gate.fingerprint import InvalidMessage, content_hash, normalize_channel, normalize_recipient


def test_sms_recipient_forms_normalise_to_one_value():
    forms = ["(555) 555-0142", "555-555-0142", "+1 555 555 0142", "15555550142"]
    assert {normalize_recipient("sms", f) for f in forms} == {"+15555550142"}


def test_email_recipient_is_lowercased():
    assert normalize_recipient("email", "  Alex.Sample@Example.COM ") == "alex.sample@example.com"


@pytest.mark.parametrize("channel,value", [("sms", "12"), ("sms", "call me"), ("email", "nope"), ("email", "a@b")])
def test_bad_recipients_are_refused(channel, value):
    with pytest.raises(InvalidMessage):
        normalize_recipient(channel, value)


def test_unknown_channel_is_refused():
    with pytest.raises(InvalidMessage):
        normalize_channel("fax")


def test_hash_changes_with_each_field_and_with_whitespace():
    base = content_hash("sms", "+15555550142", "See you at 9.")
    assert base == content_hash("sms", "+15555550142", "See you at 9.")
    assert base != content_hash("email", "+15555550142", "See you at 9.")
    assert base != content_hash("sms", "+15555550143", "See you at 9.")
    assert base != content_hash("sms", "+15555550142", "See you at 9. ")
    assert base != content_hash("sms", "+15555550142", "see you at 9.")


def test_hash_fields_cannot_bleed_into_each_other():
    assert content_hash("sms", "+1555", "5550142 hi") != content_hash("sms", "+15555550142", " hi")
