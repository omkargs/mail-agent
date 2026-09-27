"""Redaction must mask secrets without destroying diagnosability."""
from mailagent.logging_setup import redact


def test_secret_values_masked():
    assert "supersecret" not in redact("token: supersecretvalue123")
    assert "sk-ant-abc123456" not in redact("ROUTER_API_KEY=sk-ant-abc123456")


def test_error_messages_stay_readable():
    """A line mentioning a secret-sounding KEY must not be blanked."""
    for msg in [
        "ROUTER_API_KEY not set",
        "google credentials not found at /path/x.json",
        "account google has no valid session",
    ]:
        assert redact(msg) == msg, f"over-redacted: {msg}"


def test_emails_masked():
    assert "[EMAIL]" in redact("Contact boss@corp.com about it")
    assert "boss@corp.com" not in redact("from boss@corp.com")


def test_otp_masked():
    assert "482913" not in redact("your OTP is 482913")
    assert "1234" not in redact("code 1234 expires soon")


def test_numbers_untouched():
    """Redaction must not eat ordinary numbers — that makes logs useless."""
    for msg in ["took 1500 ms", "item 4821 shipped", "3 retries", "meeting at 1400"]:
        assert redact(msg) == msg, f"over-redacted a plain number: {msg}"
