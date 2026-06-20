"""
Thin Resend API wrapper for GrailSweep transactional email.

All callers use gs_send_email() — replaces direct smtplib usage.
Reads RESEND_API_KEY from environment (provided by Modal secret 'resend-api-key').
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "GrailSweep <support@grailsweep.com>"
DEFAULT_REPLY_TO = "support@grailsweep.com"


def gs_send_email(
    to: str,
    subject: str,
    html: str,
    text: str = "",
    reply_to: str = DEFAULT_REPLY_TO,
    from_address: str = DEFAULT_FROM,
) -> str:
    """
    Send a transactional email via Resend.

    Args:
        to: recipient address
        subject: email subject
        html: HTML body
        text: optional plain-text body (recommended for deliverability)
        reply_to: Reply-To header (defaults to support@grailsweep.com)
        from_address: From header (defaults to 'GrailSweep <support@grailsweep.com>')

    Returns:
        Resend message ID (string) on success.

    Raises:
        KeyError: if RESEND_API_KEY env var is missing.
        requests.HTTPError: on non-2xx response from Resend.
        requests.RequestException: on network failure.
    """
    api_key = os.environ["RESEND_API_KEY"]

    payload = {
        "from": from_address,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text or "",
        "reply_to": reply_to,
    }

    try:
        r = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        msg_id = r.json().get("id", "")
        logger.info(f"[RESEND] Sent to {to} subject='{subject}' id={msg_id}")
        return msg_id
    except requests.HTTPError as e:
        body = e.response.text if e.response else "no body"
        logger.error(f"[RESEND] HTTPError sending to {to}: {e} body={body}")
        raise
    except Exception as e:
        logger.error(f"[RESEND] Failed sending to {to}: {e}")
        raise
