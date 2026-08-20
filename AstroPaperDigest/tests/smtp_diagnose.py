#!/usr/bin/env python3
"""Diagnose SMTP authentication for the configured email server.

Connects to the server, prints its greeting and advertised AUTH mechanisms,
then tries to log in with several username forms using the configured
password.  The password is NEVER printed.

Usage:
    python tests/smtp_diagnose.py
    SMTP_USERNAME=rzjiang python tests/smtp_diagnose.py   # force a specific login name
"""

import os
import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _redact(value):
    return "(unset)" if not value else f"(set, {len(value)} chars)"


def main() -> int:
    load_dotenv(PROJECT_DIR / ".env")
    config = yaml.safe_load((PROJECT_DIR / "config.yaml").read_text(encoding="utf-8"))
    email = config.get("email", {}) or {}

    sender = os.environ.get("EMAIL_SENDER") or email.get("sender", "")
    server_name = os.environ.get("SMTP_SERVER") or email.get("smtp_server", "")
    port_raw = os.environ.get("SMTP_PORT") or email.get("smtp_port", 465)
    use_ssl_raw = os.environ.get("SMTP_USE_SSL")
    if use_ssl_raw is not None and use_ssl_raw.strip():
        use_ssl = use_ssl_raw.strip().lower() in ("1", "true", "yes", "on", "ssl")
    else:
        use_ssl = email.get("use_ssl", True)
    password_env = email.get("password_env", "EMAIL_APP_PASSWORD")
    password = os.environ.get(password_env)

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 465 if use_ssl else 587

    print("=" * 60)
    print("SMTP authentication diagnostic")
    print("=" * 60)
    print(f"Server:   {server_name or '(unset)'}:{port} ({'SSL' if use_ssl else 'STARTTLS'})")
    print(f"Sender:   {sender or '(unset)'}")
    print(f"Password: {password_env} {_redact(password)}")
    print()

    if not sender or not password:
        print("Missing sender or password - fix .env / config.yaml first.")
        return 1

    local = sender.split("@", 1)[0] if "@" in sender else sender
    candidates = []
    override = os.environ.get("SMTP_USERNAME")
    if override:
        candidates.append(override)
    candidates.append(sender)
    if local != sender:
        candidates.append(local)
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(server_name, port, timeout=30)
        else:
            server = smtplib.SMTP(server_name, port, timeout=30)
            server.starttls()
    except Exception as e:
        print(f"Connection failed: {e}")
        print("Check the SMTP server/port and network (CSTNet may need the campus network).")
        return 2

    with server:
        code, _resp = server.ehlo()
        print(f"EHLO code: {code}")
        auth = getattr(server, "esmtp_features", {}).get("auth", "")
        print(f"AUTH methods advertised: {auth or '(none)'}")
        print()

        for name in candidates:
            print(f"Trying login as {name!r} ...")
            try:
                server.login(name, password)
                print(f"  SUCCESS with username {name!r}")
                print("  -> Authentication works. The 535 means the password or login name was wrong.")
                print("  -> Use this exact login name/password in your .env / config.")
                return 0
            except smtplib.SMTPAuthenticationError as e:
                print(f"  AUTH FAILED: {e}")
            except Exception as e:
                print(f"  ERROR: {e}")

    print()
    print("All login attempts failed. Likely causes:")
    print("  1. Wrong password - CSTNet/CAS mail may need a 'client-specific password' (client")
    print("     authorization code), not the web-login password.")
    print("  2. The account has two-factor authentication enabled and needs an app")
    print("     password for SMTP.")
    print("  3. The mailbox has SMTP access disabled.")
    print("  4. Too many failed attempts -> temporary lockout; wait and retry.")
    print()
    print("Help: https://help.cstnet.cn/changjianwenti/youjianshoufa/xitongcanshu.html")
    return 3


if __name__ == "__main__":
    sys.exit(main())
