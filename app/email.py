from __future__ import annotations

import logging

import resend

from config import RESEND_API_KEY, EMAIL_FROM

log = logging.getLogger(__name__)


def _send(to: str, subject: str, html: str):
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set, skipping email to %s", to)
        return
    resend.api_key = RESEND_API_KEY
    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        log.info("Email sent to %s: %s", to, subject)
    except Exception as e:
        log.error("Failed to send email to %s: %s", to, e)


def send_approval_email(to: str, name: str):
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0F1117;color:#E6E8EB;border-radius:12px">
        <div style="text-align:center;margin-bottom:24px">
            <div style="font-size:22px;font-weight:700;letter-spacing:1px;color:#E6E8EB">ZENITH<span style="color:#00C896">.</span></div>
        </div>
        <div style="font-size:15px;line-height:1.6">
            <p>Hey {name or 'there'},</p>
            <p>Your account has been <span style="color:#00C896;font-weight:600">approved</span>. You now have full access to the Zenith trading dashboard.</p>
            <div style="text-align:center;margin:24px 0">
                <a href="https://zenithbot.org" style="display:inline-block;padding:10px 28px;background:#00C896;color:#06231b;font-weight:600;font-size:14px;border-radius:6px;text-decoration:none">Open Dashboard</a>
            </div>
        </div>
        <div style="border-top:1px solid #2A2F3A;padding-top:16px;margin-top:24px;font-size:11px;color:#5C636E;text-align:center">
            zenithbot.org
        </div>
    </div>
    """
    _send(to, "Your account has been approved", html)


def send_rejection_email(to: str, name: str):
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0F1117;color:#E6E8EB;border-radius:12px">
        <div style="text-align:center;margin-bottom:24px">
            <div style="font-size:22px;font-weight:700;letter-spacing:1px;color:#E6E8EB">ZENITH<span style="color:#00C896">.</span></div>
        </div>
        <div style="font-size:15px;line-height:1.6">
            <p>Hey {name or 'there'},</p>
            <p>Your account request has been <span style="color:#FF4D4D;font-weight:600">declined</span>. If you think this was a mistake, please contact the admin.</p>
        </div>
        <div style="border-top:1px solid #2A2F3A;padding-top:16px;margin-top:24px;font-size:11px;color:#5C636E;text-align:center">
            zenithbot.org
        </div>
    </div>
    """
    _send(to, "Account request update", html)
