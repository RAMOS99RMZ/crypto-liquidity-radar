from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("radar.state")


def default_state() -> dict:
    return {
        "version": 1,
        "seq": 0,
        "meta": {"runs": 0, "first_run_ts": None, "last_run_ts": None, "last_heartbeat": 0},
        "baselines": {"sector": {}, "coin": {}},
        "chains": {},
        "open": [],
        "closed": [],
        "cooldown": {},
        "model": None,
        "wallets": {"evm": {}, "sol": {}},
        "valid_categories": {"ts": 0, "ids": []},
    }


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        base = default_state()
        if not self.path.exists():
            return base
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("state is not an object")
            for k, v in loaded.items():
                base[k] = v
            for k, v in default_state().items():  # ضمان وجود كل المفاتيح
                base.setdefault(k, v)
            return base
        except Exception as exc:  # ملف تالف -> نبدأ من جديد مع نسخة احتياطية
            log.error("state corrupted (%s) - starting fresh", exc)
            try:
                self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}"))
            except OSError:
                pass
            return default_state()

    def trim(self, now: float | None = None) -> None:
        d = self.data
        now = now or time.time()
        d["closed"] = d["closed"][-1500:]
        d["open"] = d["open"][-200:]
        d["cooldown"] = {k: v for k, v in d["cooldown"].items() if now - v < 7 * 86400}

    def save(self) -> None:
        self.trim()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)
