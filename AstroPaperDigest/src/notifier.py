"""Email notification for daily paper digest."""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _env_or_config(env_name: str, email_config: dict, key: str, default=""):
    """Return a value from the GUI-written .env file, falling back to config.yaml.

    The setup/settings page writes EMAIL_SENDER, EMAIL_RECIPIENT, SMTP_SERVER
    and SMTP_PORT into .env.  Prefer those so editing .env alone takes effect;
    config.yaml's email section stays as a fallback for values not in .env.
    """
    value = os.environ.get(env_name)
    if value is not None and value.strip():
        return value.strip()
    return email_config.get(key, default)


def _parse_port(value, default: int = 587) -> int:
    """Parse and validate an SMTP port, returning *default* on bad input."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    if not 1 <= port <= 65535:
        return default
    return port


def _parse_bool(value, default: bool = True) -> bool:
    """Parse a bool-ish value ('true'/'ssl'/'1' -> True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on", "ssl"):
            return True
        if v in ("0", "false", "no", "off", "starttls"):
            return False
    return default


def send_email(
    subject: str,
    body: str,
    email_config: dict,
) -> bool:
    """Send an email notification with the digest content.
    
    Args:
        subject: email subject
        body: email body (plain text or HTML)
        email_config: dict with smtp_server, smtp_port, sender, recipient, password_env

    Returns:
        True if sent successfully, False otherwise
    """
    if not email_config.get("enabled", False):
        print("  Email notification disabled.")
        return False
    
    # The setup page writes sender/recipient/server/port to .env; read those
    # first and fall back to config.yaml so editing .env alone also works.
    sender = _env_or_config("EMAIL_SENDER", email_config, "sender")
    recipient = _env_or_config("EMAIL_RECIPIENT", email_config, "recipient")
    smtp_server = _env_or_config("SMTP_SERVER", email_config, "smtp_server", "smtp.gmail.com")
    # Some servers (e.g. CAS/CSTNet) accept only the local part as the login
    # name.  Allow an explicit SMTP_USERNAME / config "username" override,
    # defaulting to the sender address.
    login_user = (
        os.environ.get("SMTP_USERNAME")
        or email_config.get("username")
        or sender
    )
    password_env = email_config.get("password_env", "EMAIL_APP_PASSWORD")
    password = os.environ.get(password_env)
    
    if not sender or not recipient or not password:
        print(f"  Email config incomplete. Need sender, recipient, and {password_env} env var.")
        return False
    
    # SSL (SMTP_SSL on port 465) is the default; config.yaml's use_ssl or the
    # SMTP_USE_SSL env var can switch it to STARTTLS (port 587).
    use_ssl = _parse_bool(
        os.environ.get("SMTP_USE_SSL"),
        _parse_bool(email_config.get("use_ssl"), True),
    )
    default_port = 465 if use_ssl else 587
    smtp_port = _parse_port(
        os.environ.get("SMTP_PORT"),
        _parse_port(email_config.get("smtp_port", default_port), default_port),
    )
    timeout = 30
    
    # Create message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    
    # Add body as plain text
    msg.attach(MIMEText(body, "plain", "utf-8"))
    
    try:
        if use_ssl:
            # SSL/TLS connection (port 465)
            with smtplib.SMTP_SSL(
                smtp_server,
                smtp_port,
                timeout=timeout,
            ) as server:
                server.login(login_user, password)
                server.sendmail(sender, recipient, msg.as_string())
        else:
            # STARTTLS connection (port 587)
            with smtplib.SMTP(
                smtp_server,
                smtp_port,
                timeout=timeout,
            ) as server:
                server.starttls()
                server.login(login_user, password)
                server.sendmail(sender, recipient, msg.as_string())
        print(f"  Email sent to {recipient}")
        return True
    except Exception as e:
        print(f"  Failed to send email: {e}")
        return False


def send_digest_email(
    digest_content: str,
    email_config: dict,
    date_str: str = None,
) -> bool:
    """Send the markdown digest via email.
    
    Args:
        digest_content: markdown digest string
        email_config: email configuration dict
        date_str: date string for subject line

    Returns:
        True if sent successfully
    """
    from datetime import date
    if date_str is None:
        date_str = date.today().isoformat()
    
    subject = f"AstroPaperDigest - {date_str}"
    return send_email(subject, digest_content, email_config)
