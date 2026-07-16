"""Secret redaction unit tests (§6.5): secrets in a ticket body must be masked
before any agent-written text (comment/log) leaves the system."""

from agent.redaction import redact


def test_masks_password_phrase_and_api_token():
    out = redact("my password is P@ssw0rd-9931 and my token is "
                 "sk-ant-abcd1234EFGHijkl5678MNOP. help.")
    assert "P@ssw0rd-9931" not in out
    assert "sk-ant-abcd1234EFGHijkl5678MNOP" not in out
    assert "[REDACTED]" in out


def test_masks_key_value_forms():
    for text in ("password: hunter2xyz", "token=xoxb-123456789012ab",
                 "api_key = AKIAABCDEFGHIJKLMNOP"):
        assert "[REDACTED]" in redact(text)


def test_leaves_ordinary_text_untouched():
    text = "I've been locked out for 20 minutes and can't get in."
    assert redact(text) == text
