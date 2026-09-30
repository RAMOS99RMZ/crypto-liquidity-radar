"""العقل: تسجيل الإشارات + تعلّم ذاتي (انحدار لوجستي مباشر Online) من نتائج الصفقات السابقة."""
from __future__ import annotations

import math

from .analytics import clip
from .models import FEATURES

PRIOR = {
    "sector": 0.18, "breadth": 0.06, "gap": 0.16, "corr": 0.08, "vol": 0.12,
    "attention": 0.10, "stable": 0.08, "whale": 0.14, "dex": 0.05, "momo": 0.03,
}
NEUTRAL = 0.3  # قيمة تعويضية لميزة غير متاحة داخل النموذج المتعلَّم


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


class Brain:
    def __init__(self, cfg, state_data: dict):
        self.cfg = cfg
        self.state = state_data
        self.lc = cfg.get("learning")
        if not state_data.get("model"):
            state_data["model"] = {
                "theta": {k: 6.0 * PRIOR[k] for k in FEATURES},
                "theta0": {k: 6.0 * PRIOR[k] for k in FEATURES},
                "bias": -3.0, "bias0": -3.0, "n": 0,
            }
        self.m = state_data["model"]

    # ---- تسجيل أولي بالأوزان اليدوية: المصادر غير المتاحة تُخصم نصف وزنها فقط
    def prior(self, f: dict) -> float:
        num = avail = missing = 0.0
        for k, w in PRIOR.items():
            v = f.get(k)
            if v is None:
                missing += w
            else:
                num += w * clip(v)
                avail += w
        den = avail + 0.5 * missing
        return num / den if den > 0 else 0.0

    def _x(self, f: dict) -> dict:
        return {k: (clip(f[k]) if f.get(k) is not None else NEUTRAL) for k in FEATURES}

    def model_p(self, f: dict) -> float:
        x = self._x(f)
        return _sigmoid(self.m["bias"] + sum(self.m["theta"][k] * x[k] for k in FEATURES))

    def alpha(self) -> float:
        n = int(self.m["n"])
        if n < int(self.lc["min_samples"]):
            return 0.0
        return min(float(self.lc["max_alpha"]), n / (n + float(self.lc["alpha_k"])))

    def score(self, f: dict) -> float:
        a = self.alpha()
        p = self.prior(f)
        if a > 0:
            p = (1 - a) * p + a * self.model_p(f)
        return 100.0 * p

    # ---- التعلّم
    def learn(self, samples: list) -> int:
        """samples = [(features, y)] حيث y=1 نجاح و0 فشل. يعيد عدد العينات المتعلَّمة."""
        if not samples:
            return 0
        lr, l2 = float(self.lc["lr"]), float(self.lc["l2"])
        for _ in range(3):
            for f, y in samples:
                x = self._x(f)
                g = self.model_p(f) - float(y)
                for k in FEATURES:
                    reg = l2 * (self.m["theta"][k] - self.m["theta0"][k])  # شدّ نحو الأوزان الأولية
                    self.m["theta"][k] = max(-10.0, min(10.0, self.m["theta"][k] - lr * (g * x[k] + reg)))
                self.m["bias"] = max(-10.0, min(10.0, self.m["bias"] - lr * g))
        self.m["n"] += len(samples)
        return len(samples)

    # ---- حد أدنى متكيّف مع نسبة النجاح الأخيرة
    def threshold(self) -> float:
        base = float(self.cfg.get("run.min_score"))
        recent = self.state["closed"][-int(self.lc["win_window"]):]
        if len(recent) < 10:
            return base
        wr = sum(1 for c in recent if c.get("y") == 1) / len(recent)
        if wr < 0.35:
            return base + min(10.0, (0.35 - wr) * 40.0)
        if wr > 0.60:
            return max(base - 5.0, base - (wr - 0.60) * 20.0)
        return base

    def win_rate(self, kind: str | None = None):
        rows = [c for c in self.state["closed"] if kind is None or kind in c.get("kinds", [])]
        if not rows:
            return None, 0
        return sum(1 for c in rows if c.get("y") == 1) / len(rows), len(rows)
