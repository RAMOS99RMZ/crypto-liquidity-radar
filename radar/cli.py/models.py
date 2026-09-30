from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

FEATURES = ["sector", "breadth", "gap", "corr", "vol", "attention", "stable", "whale", "dex", "momo"]


def to_float(x) -> Optional[float]:
    """تحويل آمن إلى float (يعيد None لأي قيمة غير صالحة)."""
    try:
        if x is None:
            return None
        v = float(x)
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    except (TypeError, ValueError):
        return None


@dataclass
class Coin:
    id: str
    symbol: str
    name: str
    price: float
    mcap: float
    volume: float
    ch1h: Optional[float] = None
    ch24h: Optional[float] = None
    ch7d: Optional[float] = None
    spark: list = field(default_factory=list)

    @classmethod
    def from_api(cls, d) -> Optional["Coin"]:
        if not isinstance(d, dict):
            return None
        cid = d.get("id")
        price = to_float(d.get("current_price"))
        if not cid or not price or price <= 0:
            return None
        raw = (d.get("sparkline_in_7d") or {}).get("price") or []
        spark = [v for v in (to_float(x) for x in raw) if v is not None]
        ch24 = to_float(d.get("price_change_percentage_24h_in_currency"))
        if ch24 is None:
            ch24 = to_float(d.get("price_change_percentage_24h"))
        return cls(
            id=str(cid),
            symbol=str(d.get("symbol") or "").upper(),
            name=str(d.get("name") or cid),
            price=price,
            mcap=to_float(d.get("market_cap")) or 0.0,
            volume=to_float(d.get("total_volume")) or 0.0,
            ch1h=to_float(d.get("price_change_percentage_1h_in_currency")),
            ch24h=ch24,
            ch7d=to_float(d.get("price_change_percentage_7d_in_currency")),
            spark=spark,
        )


@dataclass
class Candidate:
    coin: Coin
    kinds: set = field(default_factory=set)
    sectors: list = field(default_factory=list)
    reasons: list = field(default_factory=list)  # [(priority, text)]
    features: dict = field(default_factory=dict)  # feature -> 0..1 or None (source unavailable)
    meta: dict = field(default_factory=dict)
    src: dict = field(default_factory=dict)  # price source for outcome tracking

    def merge(self, other: "Candidate") -> None:
        self.kinds |= other.kinds
        for s in other.sectors:
            if s not in self.sectors:
                self.sectors.append(s)
        seen = {t for _, t in self.reasons}
        for r in other.reasons:
            if r[1] not in seen:
                self.reasons.append(r)
        for k, v in other.features.items():
            a = self.features.get(k)
            if a is None:
                self.features[k] = v
            elif v is not None:
                self.features[k] = max(a, v)
        for k, v in other.meta.items():
            self.meta.setdefault(k, v)
