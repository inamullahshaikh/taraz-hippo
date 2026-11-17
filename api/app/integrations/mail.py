"""Email (SMTP) and Teams (incoming webhook). Each message is tried 3 times."""
import logging
import smtplib
import time
from email.message import EmailMessage

import httpx

from ..settings import S

log = logging.getLogger("bomtree.mail")


def send(to, subject, body):
    for attempt in range(3):
        try:
            if S.SMTP_HOST and to:
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = S.MAIL_FROM, ", ".join(to), f"[BOMTree] {subject}"
                msg.set_content(body.replace("\n/", f"\n{S.PUBLIC_URL}/"))
                with smtplib.SMTP(S.SMTP_HOST, S.SMTP_PORT, timeout=20) as smtp:
                    smtp.starttls()
                    if S.SMTP_USER:
                        smtp.login(S.SMTP_USER, S.SMTP_PASSWORD)
                    smtp.send_message(msg)
            if S.TEAMS_WEBHOOK_URL:
                httpx.post(S.TEAMS_WEBHOOK_URL, json={"text": f"**{subject}**\n\n{body}"}, timeout=20).raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001 - any failure is retried, then logged
            log.warning("send failed (try %s): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    return False


def alert(subject, body):
    """For Admin: failed jobs, backups, app down."""
    return send(S.ALERT_EMAILS, subject, body)
