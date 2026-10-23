"""App settings stored in SQLite, with secrets masked on the way out."""
from __future__ import annotations

import copy
import json
import threading

MASK = "********"
SECRET_KEYS = {"cert_id", "token", "webhook_url", "bot_token", "api_token", "user_key", "password"}

DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

DEFAULTS: dict = {
    "ebay": {"app_id": "", "cert_id": "", "marketplace": "EBAY_US"},
    "http": {"user_agent": DEFAULT_UA, "request_delay_s": 2.0},
    "app_base_url": "",
    "notify": {
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "token": "", "priority": "default"},
        "discord": {"enabled": False, "webhook_url": ""},
        "slack": {"enabled": False, "webhook_url": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "pushover": {"enabled": False, "api_token": "", "user_key": ""},
        "email": {
            "enabled": False, "host": "", "port": 587, "security": "starttls",
            "username": "", "password": "", "from_addr": "", "to_addr": "",
        },
        "webhook": {"enabled": False, "webhook_url": ""},
    },
}


def deep_merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _coerce(val, tmpl):
    if isinstance(tmpl, bool):
        if not isinstance(val, bool):
            raise ValueError("expected true/false")
        return val
    if isinstance(tmpl, int):
        return int(val)
    if isinstance(tmpl, float):
        return float(val)
    if isinstance(tmpl, str):
        return str(val).strip()
    return val


def _filter(patch: dict, tmpl: dict) -> dict:
    """Keep only known keys, coerce types, ignore masked secret placeholders."""
    out: dict = {}
    for k, v in patch.items():
        if k not in tmpl:
            continue
        if isinstance(tmpl[k], dict):
            if isinstance(v, dict):
                out[k] = _filter(v, tmpl[k])
        else:
            if k in SECRET_KEYS and v == MASK:
                continue
            out[k] = _coerce(v, tmpl[k])
    return out


def _mask(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _mask(v)
        elif k in SECRET_KEYS and v:
            out[k] = MASK
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict = {}
        self.reload()

    def reload(self) -> None:
        data = copy.deepcopy(DEFAULTS)
        row = self.db.one("SELECT value FROM settings WHERE key='all'")
        if row:
            deep_merge(data, json.loads(row["value"]))
        with self._lock:
            self._cache = data

    def get(self) -> dict:
        with self._lock:
            return self._cache

    def public(self) -> dict:
        return _mask(copy.deepcopy(self.get()))

    def update(self, patch: dict) -> dict:
        clean = _filter(patch, DEFAULTS)
        cur = copy.deepcopy(self.get())
        deep_merge(cur, clean)
        if cur["http"]["request_delay_s"] < 0.5:
            raise ValueError("request delay must be at least 0.5 seconds")
        self.db.x(
            "INSERT INTO settings(key,value) VALUES('all',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(cur),),
        )
        self.reload()
        return self.public()
