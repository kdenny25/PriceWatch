"""Polite HTTP client: per-host spacing, retries, and clear errors for blocks.

Uses `requests` in worker threads. The low-level `transport` is injectable so tests
never touch the network.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .models import Blocked, ScrapeError


@dataclass
class Response:
    status_code: int
    text: str
    headers: dict

    def json(self):
        return json.loads(self.text)


def real_transport(method, url, headers, params, data, json_body, timeout) -> Response:
    r = requests.request(
        method, url, headers=headers, params=params, data=data, json=json_body,
        timeout=timeout, allow_redirects=True,
    )
    return Response(r.status_code, r.text, dict(r.headers))


def _retry_after(r: Response) -> float | None:
    for k, v in r.headers.items():
        if k.lower() == "retry-after":
            try:
                return float(v)
            except ValueError:
                return None
    return None


class Http:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self._transport = transport or real_transport
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def request(self, method: str, url: str, *, params=None, headers=None, data=None,
                      json_body=None, delay: float | None = None, timeout: float = 30) -> Response:
        cfg = self.settings.get()["http"]
        host = urlparse(url).hostname or ""
        spacing = float(cfg["request_delay_s"]) if delay is None else delay
        h = {"User-Agent": cfg["user_agent"], "Accept-Language": "en-US,en;q=0.9"}
        h.update(headers or {})
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            for attempt in range(3):
                wait = self._last.get(host, 0.0) + spacing - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await asyncio.to_thread(
                        self._transport, method, url, h, params, data, json_body, timeout)
                except (requests.RequestException, OSError) as e:
                    self._last[host] = time.monotonic()
                    if attempt == 2:
                        raise ScrapeError(f"network error talking to {host}: {e}") from e
                    await asyncio.sleep(min(2 ** attempt, 5) * (0 if spacing == 0 else 1))
                    continue
                self._last[host] = time.monotonic()
                if r.status_code in (429, 503):
                    if attempt == 2:
                        raise Blocked(f"{host} answered {r.status_code} (rate limited)")
                    pause = _retry_after(r) or 5 * (attempt + 1)
                    await asyncio.sleep(min(pause, 60) * (0 if spacing == 0 else 1))
                    continue
                if r.status_code == 403:
                    raise Blocked(f"{host} answered 403 (access blocked)")
                return r
        raise ScrapeError("request failed")  # pragma: no cover
