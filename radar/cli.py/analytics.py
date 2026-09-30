from __future__ import annotations

import math

import numpy as np


def clip(x, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def group_summary(coins: list) -> dict:
    ch24 = [c.ch24h for c in coins if c.ch24h is not None]
    ch1 = [c.ch1h for c in coins if c.ch1h is not None]
    ch7 = [c.ch7d for c in coins if c.ch7d is not None]
    mcap = sum(c.mcap for c in coins)
    vol = sum(c.volume for c in coins)
    return {
        "n": len(coins),
        "median_24h": float(np.median(ch24)) if ch24 else 0.0,
        "mean_24h": float(np.mean(ch24)) if ch24 else 0.0,
        "median_1h": float(np.median(ch1)) if ch1 else 0.0,
        "median_7d": float(np.median(ch7)) if ch7 else 0.0,
        "breadth": (sum(1 for x in ch24 if x > 0) / len(ch24)) if ch24 else 0.0,
        "turnover": (vol / mcap) if mcap > 0 else 0.0,
        "mcap": mcap,
        "volume": vol,
    }


def _log_returns(spark: list, n: int):
    if len(spark) < n + 1:
        return None
    a = np.asarray(spark[-(n + 1):], dtype=float)
    if not np.all(np.isfinite(a)) or np.any(a <= 0):
        return None
    return np.diff(np.log(a))


def correlation_profile(coins: list, max_len: int = 96, min_points: int = 49) -> dict:
    """مصفوفة الارتباط بين عملات السلة (عوائد ساعية من sparkline 7 أيام).

    لكل عملة: معامل الارتباط وBeta مقابل مؤشر السلة (بدون العملة نفسها)،
    مع تذبذب يومي مُقدَّر. كما يعيد عائد مؤشر السلة لآخر 6 و24 ساعة.
    """
    empty = {"avg_corr": None, "per_coin": {}, "idx_6h": None, "idx_24h": None, "ids": [], "matrix": None}
    cs = [c for c in coins if len(c.spark) >= min_points]
    if len(cs) < 4:
        return empty
    length = min(max_len, min(len(c.spark) for c in cs) - 1)
    rows, keep = [], []
    for c in cs:
        r = _log_returns(c.spark, length)
        if r is None or float(np.std(r)) <= 1e-12:
            continue
        rows.append(r)
        keep.append(c)
    if len(keep) < 4:
        return empty
    m = np.vstack(rows)
    k = len(keep)
    cmat = np.nan_to_num(np.corrcoef(m))
    avg = float(np.mean(cmat[np.triu_indices(k, 1)]))
    total = m.sum(axis=0)
    per: dict = {}
    for i, c in enumerate(keep):
        idx = (total - m[i]) / (k - 1)
        if float(np.std(idx)) <= 1e-12:
            corr = beta = 0.0
        else:
            corr = float(np.nan_to_num(np.corrcoef(m[i], idx)[0, 1]))
            beta = float(np.cov(m[i], idx)[0, 1] / np.var(idx, ddof=1))
        per[c.id] = {"corr": corr, "beta": beta, "vol": float(np.std(m[i]) * math.sqrt(24) * 100)}
    idx_all = m.mean(axis=0)

    def cum(h: int):
        return float(math.expm1(float(idx_all[-h:].sum())) * 100) if len(idx_all) >= h else None

    return {
        "avg_corr": avg, "per_coin": per, "idx_6h": cum(6), "idx_24h": cum(24),
        "ids": [c.id for c in keep], "matrix": cmat.tolist(),
    }


def find_laggards(coins: list, per_coin: dict, reference: float, min_gap: float, max_24h: float,
                  min_corr: float, max_7d_dump: float = -40.0) -> list:
    """عملات مرتبطة بالسلة لكنها لم تتحرك بعد (Lagging Assets)."""
    out = []
    for c in coins:
        if c.ch24h is None:
            continue
        p = per_coin.get(c.id)
        if not p:
            continue
        gap = reference - c.ch24h
        if gap < min_gap or p["corr"] < min_corr or c.ch24h > max_24h:
            continue
        if c.ch1h is not None and c.ch1h < -2.0:  # ما زالت تنزف
            continue
        if c.ch7d is not None and c.ch7d < max_7d_dump:  # منهارة
            continue
        out.append({"coin": c, "gap": gap, "corr": p["corr"], "beta": p["beta"], "vol": p["vol"]})
    out.sort(key=lambda x: x["gap"] * max(x["corr"], 0.0), reverse=True)
    return out
