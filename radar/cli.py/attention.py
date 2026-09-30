from __future__ import annotations


class Attention:
    """زخم الانتباه: نسبة دوران السيولة (حجم/قيمة سوقية) الحالية إلى خط أساس يتعلّمه الرادار (EWMA).

    نسبة 4.0 تعني قفزة +300% فوق المعتاد. خط الأساس يحتاج ~ساعتين تشغيل ليصبح جاهزاً؛
    قبلها تعيد الدوال None ويتم تعويضها بمؤشر بديل.
    """

    def __init__(self, state_data: dict, cfg, now: float):
        self.b = state_data["baselines"]
        self.alpha = float(cfg.get("attention.alpha"))
        self.warm = int(cfg.get("attention.warmup_runs"))
        self.now = now

    def _obs(self, bucket: str, key: str, value: float):
        store = self.b.setdefault(bucket, {})
        rec = store.get(key)
        if rec is None:
            store[key] = {"v": float(value), "n": 1, "ts": self.now}
            return None
        base = rec["v"]
        ratio = (value / base) if base > 0 else None
        capped = min(value, base * 2.0) if base > 0 else value  # لا نسمح للطفرة بإفساد خط الأساس
        rec["v"] = base * (1 - self.alpha) + capped * self.alpha
        rec["n"] += 1
        rec["ts"] = self.now
        return ratio if rec["n"] > self.warm else None

    def sector(self, key: str, turnover: float):
        if turnover is None or turnover <= 0:
            return None
        return self._obs("sector", key, turnover)

    def coin(self, coin):
        if not coin.mcap or coin.mcap <= 0 or coin.volume <= 0:
            return None
        return self._obs("coin", coin.id, coin.volume / coin.mcap)

    def prune(self, max_age_days: float = 7.0) -> None:
        for bucket in self.b.values():
            for k in [k for k, r in bucket.items() if self.now - r.get("ts", 0) > max_age_days * 86400]:
                del bucket[k]
