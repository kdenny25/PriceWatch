"""Notification channels. Every message is plain text built from untrusted listing titles,
so nothing here uses markup modes that could be abused (no @everyone, no HTML)."""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

import requests

CHANNELS = ["ntfy", "discord", "slack", "telegram", "pushover", "email", "webhook"]


def default_post(url, **kw):
    r = requests.post(url, timeout=15, **kw)
    r.raise_for_status()
    return r


def enabled_channels(settings: dict) -> list[str]:
    return [c for c in CHANNELS if settings["notify"][c].get("enabled")]


def _ntfy(c, title, body, url, post):
    if not c["topic"]:
        raise ValueError("ntfy topic is not set")
    prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(c.get("priority", "default"), 3)
    payload = {"topic": c["topic"], "title": title, "message": body[:3800], "priority": prio, "tags": ["moneybag"]}
    if url:
        payload["click"] = url
    headers = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
    post((c["server"] or "https://ntfy.sh").rstrip("/"), json=payload, headers=headers)


def _discord(c, title, body, url, post):
    if not c["webhook_url"]:
        raise ValueError("Discord webhook URL is not set")
    text = f"**{title}**\n{body}"[:1900]
    post(c["webhook_url"], json={"content": text, "allowed_mentions": {"parse": []}})


def _slack(c, title, body, url, post):
    if not c["webhook_url"]:
        raise ValueError("Slack webhook URL is not set")
    post(c["webhook_url"], json={"text": f"*{title}*\n{body}"[:3500]})


def _telegram(c, title, body, url, post):
    if not (c["bot_token"] and c["chat_id"]):
        raise ValueError("Telegram bot token / chat id not set")
    post(f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
         data={"chat_id": c["chat_id"], "text": f"{title}\n\n{body}"[:4000]})


def _pushover(c, title, body, url, post):
    if not (c["api_token"] and c["user_key"]):
        raise ValueError("Pushover API token / user key not set")
    data = {"token": c["api_token"], "user": c["user_key"], "title": title[:250], "message": body[:1000]}
    if url:
        data["url"] = url
    post("https://api.pushover.net/1/messages.json", data=data)


def _email(c, title, body, url, post):
    if not (c["host"] and c["to_addr"]):
        raise ValueError("SMTP host / recipient not set")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = c["from_addr"] or c["username"] or "pricewatch@localhost"
    msg["To"] = c["to_addr"]
    msg.set_content(body)
    port = int(c["port"])
    if c["security"] == "ssl":
        s = smtplib.SMTP_SSL(c["host"], port, timeout=20)
    else:
        s = smtplib.SMTP(c["host"], port, timeout=20)
        if c["security"] == "starttls":
            s.starttls()
    try:
        if c["username"]:
            s.login(c["username"], c["password"])
        s.send_message(msg)
    finally:
        s.quit()


def _webhook(c, title, body, url, post):
    if not c["webhook_url"]:
        raise ValueError("Webhook URL is not set")
    post(c["webhook_url"], json={"app": "pricewatch", "title": title, "body": body, "url": url})


SENDERS = {"ntfy": _ntfy, "discord": _discord, "slack": _slack, "telegram": _telegram,
           "pushover": _pushover, "email": _email, "webhook": _webhook}


async def send(settings: dict, title: str, body: str, url: str | None = None,
               channels: list[str] | None = None, post=None, force: bool = False) -> dict:
    """Send to channels; returns {channel: "ok" | error text}. `force` ignores the enabled flag
    (used by the Settings page 'send test' buttons)."""
    post = post or default_post
    names = channels if channels is not None else enabled_channels(settings)
    out: dict[str, str] = {}
    for name in names:
        c = settings["notify"].get(name)
        if c is None or (not force and not c.get("enabled")):
            continue
        try:
            await asyncio.to_thread(SENDERS[name], c, title, body, url, post)
            out[name] = "ok"
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            msg = str(e) or type(e).__name__
            for v in c.values():           # never echo secrets (e.g. Telegram token in a URL)
                if isinstance(v, str) and len(v) >= 6:
                    msg = msg.replace(v, "***")
            out[name] = msg[:200]
    return out
