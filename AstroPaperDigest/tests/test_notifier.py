#!/usr/bin/env python3
"""Tests for the email notification / reminder feature (src/notifier.py).

These tests mock smtplib so they run offline and never send a real message.

Usage:
    python tests/test_notifier.py            # offline mocked tests
    python tests/test_notifier.py --live     # REALLY send a test email via config.yaml + .env
    python tests/test_notifier.py --live --to someone@example.com   # override recipient
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.notifier import send_email, send_digest_email  # noqa: E402


BASE_CONFIG = {
    "enabled": True,
    "smtp_server": "smtp.example.com",
    "smtp_port": 587,
    "use_ssl": False,
    "sender": "sender@example.com",
    "recipient": "recipient@example.com",
    "password_env": "EMAIL_APP_PASSWORD",
}


def test_disabled_returns_false_without_smtp():
    print("=== Test: disabled email is a no-op ===")
    cfg = dict(BASE_CONFIG, enabled=False)
    with mock.patch("src.notifier.smtplib.SMTP") as smtp_cls, mock.patch.dict(
        os.environ, {"EMAIL_APP_PASSWORD": "pw"}, clear=True
    ):
        assert send_email("s", "b", cfg) is False
        smtp_cls.assert_not_called()
    print("  PASSED")


def test_incomplete_config_returns_false():
    print("=== Test: incomplete config is rejected ===")
    cases = [
        ("missing sender", dict(BASE_CONFIG, sender="")),
        ("missing recipient", dict(BASE_CONFIG, recipient="")),
        ("missing password", dict(BASE_CONFIG)),
    ]
    for name, cfg in cases:
        with mock.patch("src.notifier.smtplib.SMTP") as smtp_cls, mock.patch.dict(
            os.environ, {}, clear=True
        ):
            assert send_email("s", "b", cfg) is False, name
            smtp_cls.assert_not_called()
        print(f"    {name}: rejected")
    print("  PASSED")


def _mock_smtp_success():
    server = mock.MagicMock()
    server.__enter__.return_value = server
    server.__exit__.return_value = False
    return server


def test_starttls_success_sends_message():
    print("=== Test: STARTTLS path (use_ssl=False) ===")
    server = _mock_smtp_success()
    cfg = dict(BASE_CONFIG, smtp_server="mail.cstnet.cn", smtp_port=587, use_ssl=False)
    with mock.patch("src.notifier.smtplib.SMTP", return_value=server) as smtp_cls, mock.patch.dict(
        os.environ, {"EMAIL_APP_PASSWORD": "secret"}, clear=True
    ):
        ok = send_email("Digest subject", "Digest body line 1\nline 2", cfg)

    assert ok is True
    smtp_cls.assert_called_once_with("mail.cstnet.cn", 587, timeout=30)
    server.starttls.assert_called_once_with()
    server.login.assert_called_once_with("sender@example.com", "secret")

    args = server.sendmail.call_args.args
    assert args[0] == "sender@example.com"
    assert args[1] == "recipient@example.com"
    raw = args[2]
    assert "Subject: Digest subject" in raw
    assert "From: sender@example.com" in raw
    assert "To: recipient@example.com" in raw
    # utf-8 plain body is base64-encoded in the MIME part; check the decoded form too
    from email.parser import Parser
    parsed = Parser().parsestr(raw)
    assert parsed["Subject"] == "Digest subject"
    text_part = parsed.get_payload()[0]
    assert "Digest body line 1" in text_part.get_payload(decode=True).decode("utf-8")
    print("  PASSED (STARTTLS + login + sendmail verified)")


def test_ssl_path_skips_starttls():
    print("=== Test: SSL/TLS path (use_ssl=True, port 465) ===")
    server = _mock_smtp_success()
    cfg = dict(BASE_CONFIG, smtp_port=465, use_ssl=True)
    with mock.patch("src.notifier.smtplib.SMTP_SSL", return_value=server) as ssl_cls, mock.patch(
        "src.notifier.smtplib.SMTP"
    ) as plain_cls, mock.patch.dict(os.environ, {"EMAIL_APP_PASSWORD": "secret"}, clear=True):
        ok = send_email("s", "b", cfg)

    assert ok is True
    ssl_cls.assert_called_once_with("smtp.example.com", 465, timeout=30)
    plain_cls.assert_not_called()
    server.starttls.assert_not_called()
    server.login.assert_called_once()
    server.sendmail.assert_called_once()
    print("  PASSED (SMTP_SSL used, starttls skipped)")


def test_smtp_failure_returns_false():
    print("=== Test: SMTP failure is caught ===")
    server = _mock_smtp_success()
    server.sendmail.side_effect = OSError("connection refused")
    with mock.patch("src.notifier.smtplib.SMTP", return_value=server), mock.patch.dict(
        os.environ, {"EMAIL_APP_PASSWORD": "secret"}, clear=True
    ):
        ok = send_email("s", "b", BASE_CONFIG)
    assert ok is False
    print("  PASSED (sendmail OSError -> False)")


def test_env_vars_override_config():
    print("=== Test: GUI-written .env values override config.yaml ===")
    server = _mock_smtp_success()
    cfg = {
        "enabled": True,
        "smtp_server": "config.example.com",
        "smtp_port": 465,
        "use_ssl": False,
        "sender": "config-sender@example.com",
        "recipient": "config-recipient@example.com",
        "password_env": "EMAIL_APP_PASSWORD",
    }
    env = {
        "EMAIL_APP_PASSWORD": "secret",
        "EMAIL_SENDER": "env-sender@example.com",
        "EMAIL_RECIPIENT": "env-recipient@example.com",
        "SMTP_SERVER": "env.example.com",
        "SMTP_PORT": "2525",
    }
    with mock.patch("src.notifier.smtplib.SMTP", return_value=server) as smtp_cls, mock.patch.dict(
        os.environ, env, clear=True
    ):
        ok = send_email("s", "b", cfg)

    assert ok is True
    smtp_cls.assert_called_once_with("env.example.com", 2525, timeout=30)
    server.login.assert_called_once_with("env-sender@example.com", "secret")
    args = server.sendmail.call_args.args
    assert args[0] == "env-sender@example.com"
    assert args[1] == "env-recipient@example.com"
    assert "From: env-sender@example.com" in args[2]
    assert "To: env-recipient@example.com" in args[2]
    print("  PASSED (env EMAIL_SENDER/RECIPIENT/SMTP_SERVER/SMTP_PORT used)")


def test_default_ssl_when_unspecified():
    print("=== Test: SSL is the default when use_ssl is unspecified ===")
    server = _mock_smtp_success()
    cfg = {
        "enabled": True,
        "smtp_server": "smtp.example.com",
        "sender": "sender@example.com",
        "recipient": "recipient@example.com",
        "password_env": "EMAIL_APP_PASSWORD",
    }
    with mock.patch("src.notifier.smtplib.SMTP_SSL", return_value=server) as ssl_cls, mock.patch(
        "src.notifier.smtplib.SMTP"
    ) as plain_cls, mock.patch.dict(os.environ, {"EMAIL_APP_PASSWORD": "secret"}, clear=True):
        ok = send_email("s", "b", cfg)

    assert ok is True
    ssl_cls.assert_called_once_with("smtp.example.com", 465, timeout=30)
    plain_cls.assert_not_called()
    server.starttls.assert_not_called()
    print("  PASSED (defaulted to SMTP_SSL on port 465)")


def test_smtp_use_ssl_env_override():
    print("=== Test: SMTP_USE_SSL env var overrides config use_ssl ===")
    server = _mock_smtp_success()
    cfg = dict(BASE_CONFIG, use_ssl=False, smtp_port=587)
    env = {
        "EMAIL_APP_PASSWORD": "secret",
        "SMTP_USE_SSL": "true",
        "SMTP_PORT": "465",
    }
    with mock.patch("src.notifier.smtplib.SMTP_SSL", return_value=server) as ssl_cls, mock.patch(
        "src.notifier.smtplib.SMTP"
    ) as plain_cls, mock.patch.dict(os.environ, env, clear=True):
        ok = send_email("s", "b", cfg)

    assert ok is True
    ssl_cls.assert_called_once_with("smtp.example.com", 465, timeout=30)
    plain_cls.assert_not_called()
    print("  PASSED (SMTP_USE_SSL=true forced SSL on 465)")


def test_smtp_username_override():
    print("=== Test: SMTP_USERNAME overrides the login name ===")
    server = _mock_smtp_success()
    cfg = dict(BASE_CONFIG, use_ssl=False)
    env = {
        "EMAIL_APP_PASSWORD": "secret",
        "EMAIL_SENDER": "sender@example.com",
        "EMAIL_RECIPIENT": "recipient@example.com",
        "SMTP_USERNAME": "rzjiang",
    }
    with mock.patch("src.notifier.smtplib.SMTP", return_value=server) as smtp_cls, mock.patch.dict(
        os.environ, env, clear=True
    ):
        ok = send_email("s", "b", cfg)

    assert ok is True
    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
    server.login.assert_called_once_with("rzjiang", "secret")
    args = server.sendmail.call_args.args
    assert args[0] == "sender@example.com"
    assert args[1] == "recipient@example.com"
    print("  PASSED (login used SMTP_USERNAME, From/To used sender/recipient)")


def test_send_digest_email_subject_and_passthrough():
    print("=== Test: send_digest_email builds subject and forwards ===")
    captured = {}

    def fake_send(subject, body, cfg):
        captured["subject"] = subject
        captured["body"] = body
        captured["cfg"] = cfg
        return True

    with mock.patch("src.notifier.send_email", side_effect=fake_send):
        ok = send_digest_email("# Digest\ncontent", BASE_CONFIG, date_str="2026-08-18")

    assert ok is True
    assert captured["subject"] == "AstroPaperDigest - 2026-08-18"
    assert captured["body"] == "# Digest\ncontent"
    assert captured["cfg"] is BASE_CONFIG
    print("  PASSED")


def test_send_digest_email_defaults_to_today():
    print("=== Test: send_digest_email defaults date to today ===")
    captured = {}

    def fake_send(subject, body, cfg):
        captured["subject"] = subject
        return True

    from datetime import date
    with mock.patch("src.notifier.send_email", side_effect=fake_send):
        send_digest_email("body", BASE_CONFIG)
    assert captured["subject"] == f"AstroPaperDigest - {date.today().isoformat()}"
    print("  PASSED")


def _live_send(recipient_override=None):
    """Send a real test email using the project's configured SMTP settings."""
    import yaml
    from dotenv import load_dotenv

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(project_dir, ".env"))
    with open(os.path.join(project_dir, "config.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f)

    email_config = dict(config.get("email", {}))
    if recipient_override:
        email_config["recipient"] = recipient_override

    body = (
        "这是一封 AstroPaperDigest 邮件提醒功能的测试邮件。\n"
        "如果你收到了它，说明 SMTP 配置和邮件发送功能工作正常。\n\n"
        "This is a test email for the AstroPaperDigest email reminder.\n"
        "If you received it, the SMTP configuration and email sending work."
    )
    print(f"  Sending live test email to: {email_config.get('recipient')}")
    print(f"  SMTP: {email_config.get('smtp_server')}:{email_config.get('smtp_port')} "
          f"(ssl={email_config.get('use_ssl', True)})")
    ok = send_digest_email(body, email_config, date_str="test")
    print(f"  Live send result: {'OK' if ok else 'FAILED'}")
    return ok


TESTS = [
    test_disabled_returns_false_without_smtp,
    test_incomplete_config_returns_false,
    test_starttls_success_sends_message,
    test_ssl_path_skips_starttls,
    test_smtp_failure_returns_false,
    test_env_vars_override_config,
    test_default_ssl_when_unspecified,
    test_smtp_use_ssl_env_override,
    test_smtp_username_override,
    test_send_digest_email_subject_and_passthrough,
    test_send_digest_email_defaults_to_today,
]


def main():
    if "--live" in sys.argv:
        to = None
        for arg in sys.argv[1:]:
            if arg.startswith("--to="):
                to = arg.split("=", 1)[1]
        ok = _live_send(recipient_override=to)
        sys.exit(0 if ok else 1)

    print("=" * 60)
    print("AstroPaperDigest - Email Notifier Tests (mocked, offline)")
    print("=" * 60)
    passed = failed = 0
    for test in TESTS:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()
        print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(TESTS)}")
    print("Run 'python tests/test_notifier.py --live' to send a REAL test email.")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
