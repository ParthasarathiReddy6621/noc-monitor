"""
Notification channels: console (always), Telegram and email.

Secrets come from environment variables (or a .env file next to the scripts):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_FROM, ALERT_EMAIL_TO
Messages are sent from a background thread so a slow network never delays checks.
"""

import os
import queue
import smtplib
import threading
import time
from email.message import EmailMessage

import requests

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "critical": 3}

ICONS = {"PROBLEM": "🔴", "RESOLVED": "🟢", "FLAPPING": "🟠", "STABLE": "🔵",
         "REMINDER": "⏰", "TEST": "🧪", "REPORT": "📊"}


class Notifier:
    def __init__(self, channels_cfg=None):
        # Default: console only. Telegram/email are enabled when listed in hosts.yaml
        self.channels = channels_cfg or {"console": {"min_severity": "info"}}
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ------------------------------------------------------------ public
    def notify(self, event, severity, title, body, force=False):
        """Queue a message for every channel whose min_severity allows it.
        force=True sends to every channel (used for reports and tests)."""
        icon = ICONS.get(event, "")
        text = f"{icon} {event} [{severity.upper()}] {title}\n{body}".strip()
        subject = f"[NOC][{severity.upper()}] {event}: {title}"
        for name, cfg in self.channels.items():
            min_sev = (cfg or {}).get("min_severity", "info")
            # RESOLVED/STABLE follow the original problem's severity, so they pass the same filter
            if force or SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(min_sev, 0):
                self._queue.put((name, subject, text))

    def flush(self, timeout=15):
        """Wait until queued messages are sent (used by tests and the CLI)."""
        end = time.time() + timeout
        while not self._queue.empty() and time.time() < end:
            time.sleep(0.1)
        time.sleep(0.3)

    # ------------------------------------------------------------ worker
    def _run(self):
        while True:
            name, subject, text = self._queue.get()
            for attempt in range(3):
                try:
                    getattr(self, f"_send_{name}")(subject, text)
                    break
                except AttributeError:
                    print(f"[notifier] unknown channel '{name}'", flush=True)
                    break
                except Exception as e:
                    print(f"[notifier] {name} failed (attempt {attempt + 1}/3): {e}", flush=True)
                    time.sleep(2 * (attempt + 1))

    # ------------------------------------------------------------ channels
    def _send_console(self, subject, text):
        print(f"\n{'=' * 60}\nALERT {text}\n{'=' * 60}", flush=True)

    def _send_telegram(self, subject, text):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        api = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org")
        r = requests.post(f"{api}/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text,
                                "disable_web_page_preview": True},
                          timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:150]}")

    def _send_email(self, subject, text):
        host = os.environ.get("SMTP_HOST")
        to = os.environ.get("ALERT_EMAIL_TO")
        if not host or not to:
            raise RuntimeError("SMTP_HOST / ALERT_EMAIL_TO not set")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("ALERT_EMAIL_FROM", user or "noc-monitor@localhost")
        msg["To"] = to
        msg.set_content(text)

        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if os.environ.get("SMTP_STARTTLS", "true").lower() != "false":
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
