"""Email notification for daily paper digest."""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


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
    
    sender = email_config.get("sender", "")
    recipient = email_config.get("recipient", "")
    password_env = email_config.get("password_env", "EMAIL_APP_PASSWORD")
    password = os.environ.get(password_env)
    
    if not sender or not recipient or not password:
        print(f"  Email config incomplete. Need sender, recipient, and {password_env} env var.")
        return False
    
    smtp_server = email_config.get("smtp_server", "smtp.gmail.com")
    smtp_port = email_config.get("smtp_port", 587)
    use_ssl = email_config.get("use_ssl", False)
    
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
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(sender, password)
                server.sendmail(sender, recipient, msg.as_string())
        else:
            # STARTTLS connection (port 587)
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(sender, password)
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
    
    subject = f"ArXivDailyDigest - {date_str}"
    return send_email(subject, digest_content, email_config)
