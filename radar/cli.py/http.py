from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger("radar.http")


class Http:
    """عميل HTTP آمن: تحديد سرعة لكل مضيف + إعادة محاولة + لا يرمي استثناءات أبداً."""

    def __init__(self, timeout: float = 25.0, intervals: Optional[dict] = None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "crypto-liquidity-radar/1.0", "Accept": "application/json"})
        self.timeout = timeout
        self.intervals = intervals or {}
        self._last: dict = {}
        self._fails: dict = {}  # قاطع دائرة: إخفاقات متتالية لكل مضيف

    MAX_HOST_FAILS = 3

    def _throttle(self, host: str) -> None:
        gap = self.intervals.get(host, 0.0)
        if gap:
            wait = self._last.get(host, 0.0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()

    @staticmethod
    def _retry_after(resp, default: float) -> float:
        try:
            return min(float(resp.headers.get("Retry-After", default)), 45.0)
        except (TypeError, ValueError):
            return min(default, 45.0)

    def request(self, method, url, params=None, headers=None, json_body=None, retries: int = 3):
        parsed = urlparse(url)
        host = parsed.netloc
        if self._fails.get(host, 0) >= self.MAX_HOST_FAILS:
            log.warning("skipping %s (circuit open after repeated failures)", host)
            return None
        for attempt in range(retries):
            self._throttle(host)
            try:
                r = self.s.request(method, url, params=params, headers=headers, json=json_body, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("network error %s (%s) attempt %d", host, exc.__class__.__name__, attempt + 1)
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                self._fails[host] = 0
                try:
                    return r.json()
                except ValueError:
                    log.warning("invalid JSON from %s%s", host, parsed.path)
                    return None
            if r.status_code == 429:
                wait = self._retry_after(r, 15.0 * (attempt + 1))
                log.warning("429 from %s, sleeping %.0fs", host, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
                continue
            self._fails[host] = 0  # الخادم حيّ (خطأ 4xx خاص بالطلب)
            log.warning("HTTP %s from %s%s %s", r.status_code, host, parsed.path, r.text[:160].replace("\n", " "))
            return None
        self._fails[host] = self._fails.get(host, 0) + 1
        return None

    def get_json(self, url, params=None, headers=None, retries: int = 3):
        return self.request("GET", url, params=params, headers=headers, retries=retries)

    def post_json(self, url, payload, headers=None, retries: int = 3):
        return self.request("POST", url, json_body=payload, headers=headers, retries=retries)
