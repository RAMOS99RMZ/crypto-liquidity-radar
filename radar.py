#!/usr/bin/env python3
"""
رادار شلال السيولة — Crypto Liquidity Radar  (ملف واحد)

التشغيل:
    python radar.py run --dry-run     # تشغيلة تجريبية بدون تيليجرام
    python radar.py run               # تشغيلة حقيقية (تستخدمها GitHub Actions كل 10 دقائق)
    python radar.py check             # فحص الاتصال والإعدادات
    python radar.py test-telegram     # رسالة اختبار
    python radar.py stats             # إحصائيات التعلّم الذاتي
كل الإعدادات في config.yaml
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import argparse
import html
import json
import logging
import math
import os
import re
import sys
import time

import numpy as np
import requests
import yaml


# ======================================================================
# models.py
# ======================================================================
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


# ======================================================================
# config.py
# ======================================================================
ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "COINGECKO_API_KEY", "COINGECKO_PLAN",
    "ETHERSCAN_API_KEY", "SOLANA_RPC_URL",
]
ROOT = Path(__file__).resolve().parent
_MISSING = object()


class Config:
    def __init__(self, settings: dict, env: dict | None = None):
        self.settings = settings
        self.sectors: dict = settings.get("sectors") or {}
        self.ecosystems: dict = settings.get("ecosystems") or {}
        self.whales: dict = settings.get("whales") or {}
        # أسرار GitHub غير المعرّفة تصل كنص فارغ -> نعتبرها None
        self.env = env if env is not None else {k: ((os.environ.get(k) or "").strip() or None) for k in ENV_KEYS}

    def get(self, path: str, default=_MISSING):
        node = self.settings
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif default is _MISSING:
                raise KeyError(f"إعداد مفقود في config.yaml: {path}")
            else:
                return default
        return node


def load_config() -> Config:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return Config(yaml.safe_load(f) or {})


# ======================================================================
# http.py
# ======================================================================
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


# ======================================================================
# state.py
# ======================================================================
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
        "mcache": {},
        "cg": {"tokens": 30.0, "ts": None, "month": "", "used": 0},
        "trend": {"ts": 0, "ids": []},
        "cg_fail": {},
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


# ======================================================================
# providers/coingecko.py
# ======================================================================
class CoinGecko:
    PUBLIC = "https://api.coingecko.com/api/v3"
    PRO = "https://pro-api.coingecko.com/api/v3"

    def __init__(self, http, api_key: Optional[str] = None, plan: str = "demo"):
        self.http = http
        self.calls = 0
        self.headers: dict = {}
        self.base = self.PUBLIC
        if api_key:
            if (plan or "demo").lower() == "pro":
                self.base = self.PRO
                self.headers["x-cg-pro-api-key"] = api_key
            else:
                self.headers["x-cg-demo-api-key"] = api_key

    def _get(self, path: str, params: Optional[dict] = None):
        self.calls += 1
        return self.http.get_json(self.base + path, params=params, headers=self.headers)

    def markets(self, category: Optional[str] = None, ids: Optional[list] = None, per_page: int = 60,
                sparkline: bool = True) -> list:
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": max(1, min(int(per_page), 250)),
            "page": 1,
            "sparkline": "true" if sparkline else "false",
            "price_change_percentage": "1h,24h,7d",
        }
        if category:
            params["category"] = category
        if ids:
            params["ids"] = ",".join(ids)
        data = self._get("/coins/markets", params)
        if not isinstance(data, list):
            return []
        out = []
        for row in data:
            c = Coin.from_api(row)
            if c:
                out.append(c)
        return out

    def prices(self, ids: list) -> dict:
        out: dict = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            data = self._get("/simple/price", {"ids": ",".join(chunk), "vs_currencies": "usd"})
            if isinstance(data, dict):
                for cid, v in data.items():
                    p = to_float((v or {}).get("usd")) if isinstance(v, dict) else None
                    if p and p > 0:
                        out[cid] = p
        return out

    def trending(self) -> set:
        data = self._get("/search/trending")
        ids: set = set()
        if isinstance(data, dict):
            for row in data.get("coins") or []:
                item = (row or {}).get("item") or {}
                if item.get("id"):
                    ids.add(str(item["id"]))
        return ids

    def category_ids(self) -> list:
        data = self._get("/coins/categories/list")
        if not isinstance(data, list):
            return []
        return [str(x["category_id"]) for x in data if isinstance(x, dict) and x.get("category_id")]


# ======================================================================
# providers/defillama.py
# ======================================================================
def norm_chain(name) -> str:
    n = str(name or "").strip().lower()
    return {"binance": "bsc", "bnb": "bsc", "bnb chain": "bsc"}.get(n, n)


class DefiLlama:
    CHAINS = "https://api.llama.fi/v2/chains"
    STABLES = "https://stablecoins.llama.fi/stablecoinchains"

    def __init__(self, http):
        self.http = http

    PRICES = "https://coins.llama.fi/prices/current/"

    def coin_prices(self, ids: list) -> dict:
        """أسعار حالية مجانية بلا مفتاح ولا سقف شهري، بمعرّفات CoinGecko نفسها: {id: price}"""
        out: dict = {}
        for i in range(0, len(ids), 60):
            keys = ",".join(f"coingecko:{x}" for x in ids[i:i + 60])
            d = self.http.get_json(self.PRICES + keys)
            coins = d.get("coins") if isinstance(d, dict) else None
            if isinstance(coins, dict):
                for k, v in coins.items():
                    p = to_float(v.get("price")) if isinstance(v, dict) else None
                    if p and p > 0 and str(k).startswith("coingecko:"):
                        out[str(k)[len("coingecko:"):]] = p
        return out

    def chains_tvl(self) -> dict:
        """{chain_norm: tvl_usd}"""
        data = self.http.get_json(self.CHAINS)
        out: dict = {}
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("name"):
                    v = to_float(row.get("tvl"))
                    if v is not None:
                        out[norm_chain(row["name"])] = v
        return out

    def stablecoin_supply(self) -> dict:
        """{chain_norm: إجمالي المعروض من العملات المستقرة بالدولار}"""
        data = self.http.get_json(self.STABLES)
        out: dict = {}
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                circ = row.get("totalCirculatingUSD")
                v = to_float(circ.get("peggedUSD")) if isinstance(circ, dict) else None
                if v is not None and v > 0:
                    out[norm_chain(row["name"])] = v
        return out


# ======================================================================
# providers/dexscreener.py
# ======================================================================
def summarize_pair(p) -> Optional[dict]:
    """يحوّل زوج DexScreener إلى قاموس مسطّح وآمن."""
    try:
        base = p.get("baseToken") or {}
        price = to_float(p.get("priceUsd"))
        if not base.get("address") or not price or price <= 0:
            return None
        liq = to_float((p.get("liquidity") or {}).get("usd")) or 0.0
        vol = p.get("volume") or {}
        tx = p.get("txns") or {}
        pc = p.get("priceChange") or {}
        h1 = tx.get("h1") or {}
        return {
            "chain": str(p.get("chainId") or "").lower(),
            "addr": str(base["address"]).lower(),
            "symbol": str(base.get("symbol") or "").upper(),
            "name": str(base.get("name") or ""),
            "price": price,
            "liq": liq,
            "vol_h1": to_float(vol.get("h1")) or 0.0,
            "vol_h24": to_float(vol.get("h24")) or 0.0,
            "buys_h1": int(to_float(h1.get("buys")) or 0),
            "sells_h1": int(to_float(h1.get("sells")) or 0),
            "ch_h1": to_float(pc.get("h1")),
            "ch_h24": to_float(pc.get("h24")),
            "mcap": to_float(p.get("marketCap")) or 0.0,
            "fdv": to_float(p.get("fdv")) or 0.0,
            "url": p.get("url") or "",
        }
    except (AttributeError, TypeError, ValueError):
        return None


def best_by_address(pairs: list) -> dict:
    """أعلى سيولة لكل عقد: {addr_lower: summary}"""
    out: dict = {}
    for p in pairs or []:
        s = summarize_pair(p)
        if s and (s["addr"] not in out or s["liq"] > out[s["addr"]]["liq"]):
            out[s["addr"]] = s
    return out


class DexScreener:
    BASE = "https://api.dexscreener.com"

    def __init__(self, http):
        self.http = http

    def search(self, query: str) -> list:
        d = self.http.get_json(f"{self.BASE}/latest/dex/search", params={"q": query})
        pairs = d.get("pairs") if isinstance(d, dict) else None
        return pairs if isinstance(pairs, list) else []

    def tokens(self, chain: str, addresses: list) -> list:
        if not addresses:
            return []
        d = self.http.get_json(f"{self.BASE}/tokens/v1/{chain}/{','.join(addresses[:30])}")
        return d if isinstance(d, list) else []


# ======================================================================
# providers/onchain.py
# ======================================================================
EVM_CHAIN_IDS = {
    "ethereum": 1,
    "base": 8453,
    "arbitrum": 42161,
    "bsc": 56,
    "polygon": 137,
    "optimism": 10,
    "avalanche": 43114,
}

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class Etherscan:
    """Etherscan API V2: مفتاح واحد لعدة شبكات EVM (توفّر الشبكات يعتمد على خطتك)."""

    BASE = "https://api.etherscan.io/v2/api"

    def __init__(self, http, api_key: str):
        self.http = http
        self.key = api_key

    def token_transfers(self, chain_id: int, address: Optional[str] = None,
                        contract: Optional[str] = None, offset: int = 100):
        """قائمة تحويلات ERC-20. تعيد None عند الفشل و[] عند عدم وجود معاملات."""
        params = {
            "chainid": chain_id, "module": "account", "action": "tokentx",
            "page": 1, "offset": offset, "sort": "desc", "apikey": self.key,
        }
        if address:
            params["address"] = address
        if contract:
            params["contractaddress"] = contract
        d = self.http.get_json(self.BASE, params=params)
        if not isinstance(d, dict):
            return None
        res = d.get("result")
        if isinstance(res, list):
            return res
        if "no transactions" in str(d.get("message", "")).lower():
            return []
        return None


class SolanaRPC:
    def __init__(self, http, url: Optional[str] = None):
        self.http = http
        self.url = url or "https://api.mainnet-beta.solana.com"

    def token_balances(self, owner: str):
        """{mint: amount} لكل حسابات التوكن لدى المحفظة. None عند الفشل."""
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [owner, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}],
        }
        d = self.http.post_json(self.url, payload)
        try:
            values = d["result"]["value"]
        except (TypeError, KeyError):
            return None
        out: dict = {}
        for v in values:
            try:
                info = v["account"]["data"]["parsed"]["info"]
                amt = float(info["tokenAmount"].get("uiAmount") or 0.0)
                if amt > 0:
                    out[info["mint"]] = out.get(info["mint"], 0.0) + amt
            except (KeyError, TypeError, ValueError):
                continue
        return out


# ======================================================================
# analytics.py
# ======================================================================
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


# ======================================================================
# attention.py
# ======================================================================
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

    def _obs(self, bucket: str, key: str, value: float, observe: bool = True):
        store = self.b.setdefault(bucket, {})
        rec = store.get(key)
        if not observe:  # بيانات من الكاش (غير جديدة): لا نحدّث خط الأساس، نعيد آخر نسبة محسوبة
            return rec.get("r") if rec else None
        if rec is None:
            store[key] = {"v": float(value), "n": 1, "ts": self.now}
            return None
        base = rec["v"]
        ratio = (value / base) if base > 0 else None
        capped = min(value, base * 2.0) if base > 0 else value  # لا نسمح للطفرة بإفساد خط الأساس
        rec["v"] = base * (1 - self.alpha) + capped * self.alpha
        rec["n"] += 1
        rec["ts"] = self.now
        rec["r"] = ratio if rec["n"] > self.warm else None
        return rec["r"]

    def sector(self, key: str, turnover: float, observe: bool = True):
        if turnover is None or turnover <= 0:
            return None
        return self._obs("sector", key, turnover, observe)

    def coin(self, coin, observe: bool = True):
        if not coin.mcap or coin.mcap <= 0 or coin.volume <= 0:
            return None
        return self._obs("coin", coin.id, coin.volume / coin.mcap, observe)

    def prune(self, max_age_days: float = 7.0) -> None:
        for bucket in self.b.values():
            for k in [k for k, r in bucket.items() if self.now - r.get("ts", 0) > max_age_days * 86400]:
                del bucket[k]


# ======================================================================
# brain.py
# ======================================================================
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


# ======================================================================
# notifier.py
# ======================================================================
log = logging.getLogger("radar.notify")

KIND_LABEL = {
    "whale": "🐋 تجميع حيتان",
    "bridge": "🌉 تدفق سيولة للشبكة",
    "waterfall": "🌊 شلال سيولة بيئي",
    "sector": "🔁 تدوير قطاعي",
}
KIND_ORDER = ["whale", "bridge", "waterfall", "sector"]


def fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}".rstrip("0")


def fmt_usd(x: float) -> str:
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:.0f}"


def now_label(tz_name: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).strftime("%H:%M")
    except Exception:
        return datetime.now(timezone.utc).strftime("%H:%M UTC")


def primary_kind(kinds) -> str:
    for k in KIND_ORDER:
        if k in kinds:
            return k
    return "sector"


def format_signal(cand, score: float, levels: dict, tz: str) -> str:
    c = cand.coin
    e = html.escape
    reasons = [t for _, t in sorted(cand.reasons, key=lambda r: r[0])][:4]
    lines = [
        f"🟢 <b>إشارة شراء · ${e(c.symbol)}</b>  <i>{e(c.name)}</i>",
        f"{KIND_LABEL[primary_kind(cand.kinds)]}  |  {e(' + '.join(cand.sectors[:2]) or '—')}",
        "━━━━━━━━━━━━",
        "<b>السبب:</b>",
        *[f"• {e(r)}" for r in reasons],
        "━━━━━━━━━━━━",
        f"💵 الدخول: <code>${fmt_price(c.price)}</code>",
        f"🎯 الهدف: <code>${fmt_price(levels['target'])}</code> (+{levels['target_pct']:.1f}%)",
        f"🛑 الوقف: <code>${fmt_price(levels['stop'])}</code> (-{levels['stop_pct']:.1f}%)",
        f"🔥 القوة: <b>{score:.0f}/100</b>  ⏱ {now_label(tz)}",
    ]
    link = cand.meta.get("url")
    if link:
        lines.append(f'🔗 <a href="{e(link, quote=True)}">الرسم والسيولة</a>')
    lines.append("<i>⚠️ إشارة إحصائية وليست نصيحة مالية</i>")
    return "\n".join(lines)


def format_outcome(sig: dict, ret: float, reason: str, hours: float) -> str:
    icon = "✅" if sig["y"] == 1 else "❌"
    why = {"target": "تحقق الهدف", "stop": "ضُرب الوقف", "expired": "انتهت المهلة"}.get(reason, reason)
    return (f"{icon} <b>${html.escape(sig['symbol'])}</b> — {why}: <b>{ret:+.1f}%</b> "
            f"خلال {hours:.1f} ساعة (دخول ${fmt_price(sig['entry'])})")


class Telegram:
    def __init__(self, http, token: str, chat_id: str):
        self.http = http
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        payload = {"chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        d = self.http.post_json(self.url, payload)
        ok = bool(isinstance(d, dict) and d.get("ok"))
        if not ok:
            log.error("Telegram send failed")
        return ok


class ConsoleNotifier:
    """يُستخدم في الوضع التجريبي أو عند غياب بيانات تيليجرام: يطبع الرسالة في السجل."""

    def __init__(self):
        self.sent: list = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        print("\n----- [DRY-RUN] رسالة تيليجرام -----\n" + text + "\n------------------------------------")
        return True


# ======================================================================
# bots/sector_bot.py
# ======================================================================
def sector_analyze(sector_data: dict, cfg, att, fresh=None) -> tuple:
    """يعيد (stats لكل قطاع، قائمة مرشحين)."""
    sc = cfg.get("sector")
    stats: dict = {}
    cands: list = []
    for key, coins in sector_data.items():
        label = cfg.sectors[key].get("label", key)
        summ = group_summary(coins)
        prof = correlation_profile(coins)
        att_ratio = att.sector(f"sector:{key}", summ["turnover"], observe=(fresh is None or key in fresh))
        idx6, idx24 = prof["idx_6h"], prof["idx_24h"]
        ref = max(summ["median_24h"], idx24 if idx24 is not None else summ["median_24h"])
        heat = max(ref, 2.0 * idx6) if idx6 is not None else ref
        hot = heat >= sc["hot_heat"] and summ["breadth"] >= sc["min_breadth"]
        stats[key] = {"label": label, "heat": heat, "hot": hot, "att_ratio": att_ratio,
                      "avg_corr": prof["avg_corr"], "idx_6h": idx6, **summ}
        if not hot:
            continue
        laggards = find_laggards(coins, prof["per_coin"], ref, sc["laggard_gap"], sc["laggard_max_24h"],
                                   sc["min_corr"], sc["max_7d_dump"])
        for lg in laggards[:8]:
            coin = lg["coin"]
            cand = Candidate(coin=coin, kinds={"sector"}, sectors=[label], src={"src": "cg", "id": coin.id})
            cand.features = {
                "sector": clip(heat / 12.0),
                "breadth": clip(summ["breadth"]),
                "gap": clip(lg["gap"] / 15.0),
                "corr": clip(lg["corr"]),
                "momo": clip(max(coin.ch1h or 0.0, 0.0) / 3.0),
            }
            cand.reasons = [
                (3, f"🔥 قطاع {label} ساخن {heat:+.1f}% (اتساع {summ['breadth'] * 100:.0f}%) "
                    f"والعملة متأخرة {coin.ch24h:+.1f}%"),
                (7, f"🔗 ارتباط {lg['corr']:.2f} مع السلة ← احتمال لحاق"),
            ]
            if att_ratio is not None and att_ratio >= 1.5:
                cand.reasons.append((4, f"📡 زخم انتباه القطاع {(att_ratio - 1) * 100:+.0f}% فوق المعتاد"))
            cand.meta = {"att_ratio": att_ratio, "daily_vol": lg["vol"], "sector_key": key}
            cands.append(cand)
    return stats, cands


# ======================================================================
# bots/waterfall_bot.py
# ======================================================================
def _wf_n(x, default: float = -999.0) -> float:
    return default if x is None else x


def waterfall_detect(natives: dict, cfg, flows: dict, vr) -> list:
    """يحدد الشبكات التي انطلق شلالها. vr(coin) = نسبة حجم العملة إلى خط أساسها."""
    w = cfg.get("waterfall")
    out = []
    for key, eco in cfg.ecosystems.items():
        native = natives.get(eco.get("native")) if eco.get("native") else None
        flow = flows.get(norm_chain(eco.get("chain")))
        reasons: list = []
        strength = 0.0
        ratio = None
        if native is not None:
            ratio = vr(native)
            breakout = _wf_n(native.ch24h) >= w["native_24h"] or _wf_n(native.ch1h) >= w["native_1h"]
            vol_ok = ratio is None or ratio >= 1.15 or _wf_n(native.ch24h) >= 1.5 * w["native_24h"]
            if breakout and vol_ok:
                strength += max(_wf_n(native.ch24h, 0.0) / 10.0, _wf_n(native.ch1h, 0.0) / 4.0)
                if ratio:
                    strength += 0.3 * (ratio - 1.0)
                reasons.append(f"{native.symbol} {native.ch24h or 0:+.1f}% (24س) / {native.ch1h or 0:+.1f}% (1س)")
        if flow and flow.get("flag"):
            strength += 1.0 + (flow.get("score") or 0.0)
            reasons.append(f"تدفق مستقرات +${flow['d6_usd'] / 1e6:.0f}M إلى الشبكة خلال 6س")
        if reasons:
            out.append({"key": key, "eco": eco, "native": native, "native_ratio": ratio, "flow": flow,
                        "reasons": reasons, "strength": strength})
    out.sort(key=lambda t: t["strength"], reverse=True)
    return out[: int(w["max_triggers"])]


def waterfall_scan(trig: dict, coins: list, cfg, vr) -> list:
    """يفحص عملات النظام البيئي بحثاً عن الأصول المتأخرة (Lagging) أو "المستيقظة"."""
    w = cfg.get("waterfall")
    sc = cfg.get("sector")
    native = trig["native"]
    label = trig["eco"].get("label", trig["key"])
    coins = [c for c in coins if not (native is not None and c.id == native.id)]
    if len(coins) < 5:
        return []
    summ = group_summary(coins)
    prof = correlation_profile(coins)
    refs = [summ["median_24h"]]
    if prof["idx_24h"] is not None:
        refs.append(prof["idx_24h"])
    if native is not None and native.ch24h is not None:
        refs.append(0.6 * native.ch24h)
    ref = max(refs)
    flow_flag = bool(trig["flow"] and trig["flow"].get("flag"))

    picked: dict = {}
    for lg in find_laggards(coins, prof["per_coin"], ref, w["laggard_gap"], sc["laggard_max_24h"],
                              w["min_corr"], sc["max_7d_dump"])[:8]:
        picked[lg["coin"].id] = ("lag", lg["coin"], lg["gap"], lg["corr"], lg["vol"])
    if flow_flag:  # سيولة داخلة قبل الحركة: نبحث عن عملات بدأت تستيقظ
        for c in coins:
            if c.id in picked or c.ch24h is None or c.ch24h > sc["laggard_max_24h"]:
                continue
            r = vr(c)
            if r is not None and r >= 1.5 and (c.ch1h or 0.0) > 0.5:
                p = prof["per_coin"].get(c.id, {})
                picked[c.id] = ("wake", c, 0.0, max(p.get("corr", 0.4), 0.0), p.get("vol", 0.0))

    out = []
    for mode, coin, gap, corr, dvol in picked.values():
        cand = Candidate(coin=coin, kinds={"waterfall"}, sectors=[f"نظام {label}"],
                         src={"src": "cg", "id": coin.id})
        cand.features = {
            "sector": clip(max(ref, 0.0) / 12.0),
            "breadth": clip(summ["breadth"]),
            "gap": clip(gap / 15.0) if mode == "lag" else 0.3,
            "corr": clip(corr),
            "momo": clip(max(coin.ch1h or 0.0, 0.0) / 3.0),
        }
        head = " | ".join(trig["reasons"])
        tail = "لم تتحرك بعد" if mode == "lag" else "بدأت تستيقظ بحجم تداول مرتفع"
        cand.reasons = [(2, f"🌊 شلال {label}: {head} ← {coin.symbol} {tail} ({coin.ch24h:+.1f}%)")]
        cand.meta = {"att_ratio": trig["native_ratio"], "daily_vol": dvol, "chain": trig["eco"].get("chain"),
                     "eco_key": trig["key"]}
        out.append(cand)
    return out


# ======================================================================
# bots/bridge_bot.py
# ======================================================================
"""بوت جسور العبور: يتتبع صافي تدفق العملات المستقرة (المعروض) إلى كل شبكة عبر الزمن.

ارتفاع المعروض المستقر على شبكة ما = سيولة دولارية تصل إليها (جسور/سك) قبل أن تتحول إلى شراء.
هذا تدفق مُجمَّع على مستوى الشبكة، وليس تتبعاً لمحفظة بعينها (المحافظ في whale_bot).
"""



def _bridge_at(hist: list, now: float, hours: float):
    """أقرب لقطة تاريخية لـ (الآن - hours) بشرط أن تكون كافية القِدم."""
    target = now - hours * 3600
    best = None
    for h in hist:
        if h[2] is None:
            continue
        if best is None or abs(h[0] - target) < abs(best[0] - target):
            best = h
    if best is None:
        return None
    if now - best[0] < hours * 3600 * 0.75 or abs(best[0] - target) > hours * 3600 * 0.35:
        return None
    return best


def _bridge_analyze(hist: list, now: float, cfg) -> dict:
    b = cfg.get("bridge")
    res = {"flag": False, "score": None, "d1_usd": None, "d6_usd": None, "pct6": None, "tvl_pct6": None}
    cur = next((h for h in reversed(hist) if h[2] is not None), None)
    if cur is None:
        return res
    r6 = _bridge_at(hist, now, 6)
    r1 = _bridge_at(hist, now, 1)
    if r1:
        res["d1_usd"] = cur[2] - r1[2]
    if r6 and r6[2] > 0:
        d6 = cur[2] - r6[2]
        pct6 = d6 / r6[2] * 100.0
        res.update(d6_usd=d6, pct6=pct6)
        if cur[1] is not None and r6[1]:
            res["tvl_pct6"] = (cur[1] / r6[1] - 1.0) * 100.0
        res["score"] = (0.5 * clip(d6 / 100e6) + 0.5 * clip(pct6 / 2.0)) if d6 > 0 else 0.0
        res["flag"] = d6 >= b["min_stable_inflow_usd_6h"] and pct6 >= b["min_stable_inflow_pct_6h"]
    return res


def bridge_update(state_data: dict, tvl: dict, stables: dict, now: float, cfg) -> dict:
    """يضيف لقطة جديدة للتاريخ ويعيد {chain_norm: flow}. نتتبع فقط شبكات ecosystems لتصغير الحالة."""
    chains = state_data.setdefault("chains", {})
    keep_s = float(cfg.get("bridge.history_hours")) * 3600
    tracked = {norm_chain(e.get("chain")) for e in cfg.ecosystems.values() if e.get("chain")}
    flows: dict = {}
    for name in tracked:
        if name not in tvl and name not in stables:
            continue
        rec = chains.setdefault(name, {"hist": []})
        rec["hist"].append([now, tvl.get(name), stables.get(name)])
        rec["hist"] = [h for h in rec["hist"] if now - h[0] <= keep_s][-400:]
        flows[name] = _bridge_analyze(rec["hist"], now, cfg)
    return flows


# ======================================================================
# bots/whale_bot.py
# ======================================================================
log = logging.getLogger("radar.whale")

EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def valid_wallet(w: dict) -> bool:
    chain = str(w.get("chain", "")).lower()
    addr = str(w.get("address", ""))
    if chain == "solana":
        return bool(SOL_ADDR.match(addr))
    return chain in EVM_CHAIN_IDS and bool(EVM_ADDR.match(addr))

SKIP_SYMBOLS = {
    "USDT", "USDC", "DAI", "WETH", "WBTC", "WBNB", "USDE", "FDUSD", "TUSD", "USDS", "PYUSD", "STETH",
    "WSTETH", "CBBTC", "WAVAX", "WMATIC", "USD1", "SOL", "WSOL", "BUSD", "USDD", "FRAX", "LUSD",
}


class WhaleBot:
    def __init__(self, cfg, eth, sol, dex, state_data: dict):
        self.cfg = cfg
        self.w = cfg.get("whales")
        self.eth = eth
        self.sol = sol
        self.dex = dex
        self.state = state_data
        wl = cfg.whales or {}
        self.exchanges = {str(a).lower() for a in (wl.get("exchanges") or {}).values()}
        raw = [x for x in (wl.get("wallets") or []) if isinstance(x, dict) and x.get("address")]
        self.wallets = []
        for x in raw:
            if valid_wallet(x):
                self.wallets.append(x)
            else:
                log.warning("محفظة بعنوان/شبكة غير صالحة تم تجاهلها: %s", x.get("label"))

    @property
    def enabled(self) -> bool:
        return bool(self.wallets)

    @property
    def can_check_flows(self) -> bool:
        return bool(self.eth and self.exchanges and self.w.get("token_flow_check", True))

    # ------------------------------------------------------------------ المحافظ
    def scan_wallets(self, now: float) -> list:
        events: list = []
        for wal in self.wallets[: int(self.w["max_wallets"])]:
            try:
                chain = str(wal.get("chain", "")).lower()
                if chain == "solana" and self.sol:
                    events += self._scan_sol(wal)
                elif chain in EVM_CHAIN_IDS and self.eth:
                    events += self._scan_evm(wal, chain, now)
            except Exception as exc:  # محفظة واحدة لا يجب أن تُسقط التشغيلة
                log.warning("wallet scan failed (%s): %s", wal.get("label"), exc)
        return events

    def _scan_evm(self, wal: dict, chain: str, now: float) -> list:
        addr = str(wal["address"]).lower()
        rec = self.state["wallets"]["evm"].setdefault(
            f"{chain}:{addr}", {"last_ts": now - self.w["first_run_lookback_min"] * 60})
        txs = self.eth.token_transfers(EVM_CHAIN_IDS[chain], address=addr)
        if txs is None:
            return []
        last = rec["last_ts"]
        newest = last
        agg: dict = {}
        for t in txs:
            try:
                ts = int(t["timeStamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts <= last:
                continue
            newest = max(newest, ts)
            sym = str(t.get("tokenSymbol", "")).upper()
            if sym in SKIP_SYMBOLS:
                continue
            try:
                amt = int(t["value"]) / (10 ** int(t.get("tokenDecimal") or 0))
            except (KeyError, TypeError, ValueError):
                continue
            c = str(t.get("contractAddress", "")).lower()
            a = agg.setdefault(c, {"net": 0.0, "ex_in": 0.0, "ex_out": 0.0})
            frm, to = str(t.get("from", "")).lower(), str(t.get("to", "")).lower()
            if to == addr:
                a["net"] += amt
                if frm in self.exchanges:
                    a["ex_out"] += amt  # منصة ← محفظة = سحب من المنصة (تجميع)
            elif frm == addr:
                a["net"] -= amt
                if to in self.exchanges:
                    a["ex_in"] += amt  # محفظة ← منصة = إيداع (تصريف محتمل)
        rec["last_ts"] = newest
        if not agg:
            return []
        info = best_by_address(self.dex.tokens(chain, list(agg)[:30]))
        events = []
        for c, a in agg.items():
            i = info.get(c)
            if not i or i["liq"] < self.w["min_liquidity"] or i["symbol"] in SKIP_SYMBOLS:
                continue
            p, thr = i["price"], self.w["min_usd"]
            kind, usd = None, 0.0
            if a["ex_in"] * p >= thr:
                kind, usd = "exchange_inflow", a["ex_in"] * p
            elif a["ex_out"] * p >= thr:
                kind, usd = "exchange_outflow", a["ex_out"] * p
            elif a["net"] * p >= thr:
                kind, usd = "accumulation", a["net"] * p
            if kind:
                events.append({"chain": chain, "addr": c, "symbol": i["symbol"], "name": i["name"],
                               "kind": kind, "usd": usd, "wallet": wal.get("label", addr[:8]), "info": i,
                               "weight": float(wal.get("weight", 1.0))})
        return events

    def _scan_sol(self, wal: dict) -> list:
        owner = str(wal["address"])
        rec = self.state["wallets"]["sol"].setdefault(owner, {"bal": None})
        bal = self.sol.token_balances(owner)
        if bal is None:
            return []
        prev = rec["bal"]
        rec["bal"] = bal
        if prev is None:  # أول تشغيلة: لقطة أساس فقط
            return []
        inc = {m: v - prev.get(m, 0.0) for m, v in bal.items() if v - prev.get(m, 0.0) > 0}
        if not inc:
            return []
        info = best_by_address(self.dex.tokens("solana", list(inc)[:30]))
        events = []
        for mint, delta in inc.items():
            i = info.get(mint.lower())
            if not i or i["liq"] < self.w["min_liquidity"] or i["symbol"] in SKIP_SYMBOLS:
                continue
            usd = delta * i["price"]
            if usd >= self.w["min_usd"]:
                events.append({"chain": "solana", "addr": mint.lower(), "symbol": i["symbol"], "name": i["name"],
                               "kind": "accumulation", "usd": usd, "wallet": wal.get("label", owner[:6]),
                               "info": i, "weight": float(wal.get("weight", 1.0))})
        return events

    # ------------------------------------------------------ تدفق المنصات لعقد عملة
    def token_exchange_flow(self, chain: str, contract: str, price: float, now: float,
                            window_min: int = 90) -> Optional[dict]:
        cid = EVM_CHAIN_IDS.get(chain)
        if not cid or not self.can_check_flows:
            return None
        txs = self.eth.token_transfers(cid, contract=contract)
        if txs is None:
            return None
        since = now - window_min * 60
        out = inn = 0.0
        for t in txs:
            try:
                if int(t["timeStamp"]) < since:
                    continue
                amt = int(t["value"]) / (10 ** int(t.get("tokenDecimal") or 0))
            except (KeyError, TypeError, ValueError):
                continue
            if str(t.get("from", "")).lower() in self.exchanges:
                out += amt
            if str(t.get("to", "")).lower() in self.exchanges:
                inn += amt
        return {"out_usd": out * price, "in_usd": inn * price}


# ======================================================================
# engine.py
# ======================================================================
log = logging.getLogger("radar.engine")

STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USDD", "BUSD", "SUSD", "FRAX",
    "LUSD", "USD0", "USD1", "GUSD", "USDP", "CRVUSD", "EURC", "EURT", "XAUT", "PAXG",
}


def is_stable(c: Coin) -> bool:
    if c.symbol in STABLE_SYMBOLS:
        return True
    return (0.97 < c.price < 1.03 and abs(c.ch24h or 0.0) < 0.6 and abs(c.ch7d or 0.0) < 1.0
            and c.mcap > 0 and c.volume / c.mcap > 0)  # ربط سعري ثابت بالدولار


def coin_to_row(c: Coin) -> dict:
    return {"id": c.id, "symbol": c.symbol, "name": c.name, "price": c.price, "mcap": c.mcap, "volume": c.volume,
            "ch1h": c.ch1h, "ch24h": c.ch24h, "ch7d": c.ch7d, "spark": [float(f"{x:.6g}") for x in c.spark]}


def row_to_coin(r) -> Optional[Coin]:
    try:
        return Coin(id=r["id"], symbol=r["symbol"], name=r["name"], price=float(r["price"]), mcap=float(r["mcap"]),
                    volume=float(r["volume"]), ch1h=r.get("ch1h"), ch24h=r.get("ch24h"), ch7d=r.get("ch7d"),
                    spark=list(r.get("spark") or []))
    except (KeyError, TypeError, ValueError):
        return None


class Budget:
    """ميزانية طلبات CoinGecko (الخطة المجانية Demo = 10,000 طلب/شهر): دلو رموز يمتلئ ببطء.

    كل طلب يستهلك رمزاً. عند نفاد الرموز يعمل الرادار على آخر بيانات محفوظة في الكاش بدل التوقف.
    """
    CAP = 40.0

    def __init__(self, state_data: dict, cfg, now: float):
        b = state_data.setdefault("cg", {"tokens": 30.0, "ts": None, "month": "", "used": 0})
        self.b = b
        self.monthly = float(cfg.get("coingecko.monthly_budget"))
        rate = self.monthly / (30.0 * 86400.0)
        if b.get("ts"):
            b["tokens"] = min(self.CAP, float(b.get("tokens", 0.0)) + max(0.0, now - b["ts"]) * rate)
        b["ts"] = now
        month = time.strftime("%Y-%m", time.gmtime(now))
        if b.get("month") != month:
            b["month"], b["used"] = month, 0

    def take(self, n: float = 1.0) -> bool:
        if self.b["tokens"] >= n:
            self.b["tokens"] -= n
            self.b["used"] += int(n)
            return True
        return False


class Engine:
    def __init__(self, cfg, state, cg, llama, dex, eth, sol, notifier, now_fn=time.time, dry_run: bool = False):
        self.cfg, self.state = cfg, state
        self.cg, self.llama, self.dex, self.eth, self.sol = cg, llama, dex, eth, sol
        self.notifier = notifier
        self.now_fn = now_fn
        self.dry_run = dry_run
        self._vr_cache: dict = {}
        self.fresh_ids: set = set()
        self.fresh_keys: set = set()
        self.summary: dict = {"sectors": {}, "signals": [], "near_miss": [], "closed": 0}

    # ------------------------------------------------------------------ أدوات مساعدة
    def vr(self, coin: Coin) -> Optional[float]:
        """نسبة حجم العملة إلى خط أساسها (تُحدَّث مرة واحدة لكل عملة في التشغيلة)."""
        if coin.id not in self._vr_cache:
            self._vr_cache[coin.id] = self.att.coin(coin, observe=coin.id in self.fresh_ids)
        return self._vr_cache[coin.id]

    def _universe(self, coins: list) -> list:
        u = self.cfg.get("universe")
        return [c for c in coins
                if c.mcap >= u["min_market_cap"] and c.volume >= u["min_volume_24h"] and not is_stable(c)]

    def _valid_categories(self, now: float):
        vc = self.state.data["valid_categories"]
        failed = self.state.data.setdefault("cg_fail", {})
        backoff = float(self.cfg.get("coingecko.retry_after_fail_minutes")) * 60
        if ((now - vc.get("ts", 0) > 86400 or not vc.get("ids")) and now - failed.get("categories", 0) >= backoff
                and self.budget.take()):
            ids = self.cg.category_ids()
            if ids:
                vc["ts"], vc["ids"] = now, ids
            else:
                failed["categories"] = now
        return set(vc["ids"]) if vc.get("ids") else None

    def _get_rows(self, ck: str, fetch, now: float) -> list:
        """بيانات سوق من الكاش أو من CoinGecko حسب الميزانية والحداثة."""
        cache = self.state.data["mcache"]
        ent = cache.get(ck)
        min_age = float(self.cfg.get("coingecko.min_refresh_minutes")) * 60
        failed = self.state.data.setdefault("cg_fail", {})
        backoff = float(self.cfg.get("coingecko.retry_after_fail_minutes")) * 60
        recently_failed = now - failed.get(ck, 0) < backoff  # لا نحرق الميزانية على طلب فشل للتو
        if (ent is None or now - ent["ts"] >= min_age) and not recently_failed and self.budget.take():
            coins = fetch()
            if coins:
                cache[ck] = {"ts": now, "coins": [coin_to_row(c) for c in coins]}
                self.fresh_keys.add(ck)
                self.fresh_ids |= {c.id for c in coins}
                return coins
            failed[ck] = now
            log.warning("فشل تحديث %s -> استخدام الكاش", ck)
        ent = cache.get(ck)
        if not ent or now - ent["ts"] > float(self.cfg.get("coingecko.max_cache_age_hours")) * 3600:
            return []
        return [c for c in (row_to_coin(r) for r in ent["coins"]) if c]

    def _trending(self, now: float) -> set:
        t = self.state.data["trend"]
        failed = self.state.data.setdefault("cg_fail", {})
        backoff = float(self.cfg.get("coingecko.retry_after_fail_minutes")) * 60
        if (now - t.get("ts", 0) >= float(self.cfg.get("coingecko.trending_refresh_minutes")) * 60
                and now - failed.get("trending", 0) >= backoff and self.budget.take()):
            ids = self.cg.trending()
            if ids:
                t["ts"], t["ids"] = now, sorted(ids)
            else:
                failed["trending"] = now
        return set(t.get("ids") or [])

    def _fresh_price(self, c: Candidate) -> Optional[float]:
        """سعر لحظي قبل التنبيه (DexScreener ثم DefiLlama المجاني) مع فلتر معقولية ±20%."""
        cached = c.coin.price
        cands = []
        info = c.meta.get("dex")
        if info:
            cands.append(info.get("price"))
        if c.src.get("src") == "cg":
            try:
                cands.append(self.llama.coin_prices([c.coin.id]).get(c.coin.id))
            except Exception as exc:
                log.warning("fresh price failed: %s", exc)
        for p in cands:
            if p and cached > 0 and 0.8 <= p / cached <= 1.25:
                return float(p)
        return None

    def _send(self, text: str) -> bool:
        try:
            return bool(self.notifier.send(text))
        except Exception as exc:
            log.error("notifier crashed: %s", exc)
            return False

    # ------------------------------------------------------------------ التشغيل
    def run(self) -> dict:
        now = self.now_fn()
        d = self.state.data
        d["meta"]["runs"] += 1
        d["meta"]["last_run_ts"] = now
        d["meta"]["first_run_ts"] = d["meta"].get("first_run_ts") or now
        self.att = Attention(d, self.cfg, now)
        self.att.prune()
        self.brain = Brain(self.cfg, d)
        self.whale = WhaleBot(self.cfg, self.eth, self.sol, self.dex, d)

        self.budget = Budget(d, self.cfg, now)
        self.fresh_ids, self.fresh_keys = set(), set()
        self._update_outcomes(now)

        valid = self._valid_categories(now)
        per_page = int(self.cfg.get("universe.per_page"))
        native_ids = [e["native"] for e in self.cfg.ecosystems.values() if e.get("native")]
        natives: dict = {}
        if native_ids:
            natives = {c.id: c for c in self._get_rows(
                "natives", lambda: self.cg.markets(ids=native_ids, per_page=len(native_ids), sparkline=False), now)}

        cache = d["mcache"]
        keys = []
        for key, sc in self.cfg.sectors.items():
            cat = sc.get("category")
            if valid is not None and cat not in valid:
                log.warning("فئة CoinGecko غير صالحة وتم تجاهلها: %s (%s)", key, cat)
                continue
            keys.append(key)
        keys.sort(key=lambda k: cache.get(f"s:{k}", {}).get("ts", 0))  # الأقدم بيانات أولاً
        sector_data: dict = {}
        for key in keys:
            cat = self.cfg.sectors[key].get("category")
            coins = self._universe(self._get_rows(
                f"s:{key}", lambda cat=cat: self.cg.markets(category=cat, per_page=per_page), now))
            if len(coins) >= 6:
                sector_data[key] = coins
            else:
                log.info("قطاع %s: بيانات غير كافية (%d)", key, len(coins))

        trending = self._trending(now)

        flows = self._bridge_flows(now)
        fresh_sectors = {k[2:] for k in self.fresh_keys if k.startswith("s:")}
        stats, cands = sector_analyze(sector_data, self.cfg, self.att, fresh=fresh_sectors)
        self.summary["sectors"] = stats

        triggers = waterfall_detect(natives, self.cfg, flows, self.vr)
        for t in triggers:
            cat = t["eco"].get("category")
            if valid is not None and cat not in valid:
                log.warning("فئة النظام البيئي غير صالحة: %s", cat)
                continue
            coins = self._universe(self._get_rows(
                f"e:{t['key']}", lambda cat=cat: self.cg.markets(category=cat, per_page=per_page), now))
            cands += waterfall_scan(t, coins, self.cfg, self.vr)
        self.summary["triggers"] = [t["key"] for t in triggers]

        merged: dict = {}
        for c in cands:
            if c.coin.id in merged:
                merged[c.coin.id].merge(c)
            else:
                merged[c.coin.id] = c

        events = self.whale.scan_wallets(now) if self.whale.enabled else []
        self._add_whale_only(events, merged)

        for c in merged.values():
            self._enrich_basic(c, flows)
            c.meta.setdefault("url", f"https://www.coingecko.com/en/coins/{c.coin.id}"
                              if c.src.get("src") == "cg" else c.meta.get("url", ""))

        eligible = [c for c in merged.values() if self._passes_gates(c, now)]
        eligible.sort(key=lambda c: self.brain.prior(c.features), reverse=True)
        top = eligible[: int(self.cfg.get("run.dex_confirm_top"))]
        for c in top:
            self._confirm_onchain(c, events, flows, now)

        threshold = self.brain.threshold()
        scored = []
        for c in top:
            if c.meta.get("blocked"):
                continue
            s = self.brain.score(c.features) + min(6.0, 3.0 * (len(c.kinds) - 1))
            if c.coin.id in trending:
                s -= float(self.cfg.get("attention.trending_penalty"))
                c.meta["crowded"] = True
            scored.append((s, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        self.summary["near_miss"] = [(round(s, 1), c.coin.symbol, sorted(c.kinds)) for s, c in scored[:6]]
        self.summary["threshold"] = round(threshold, 1)
        self.summary["cg"] = {"fresh": len(self.fresh_keys), "tokens": round(d["cg"]["tokens"], 1),
                              "used_month": d["cg"]["used"], "budget": self.budget.monthly}

        sent = 0
        for s, c in scored:
            if s < threshold or sent >= int(self.cfg.get("run.max_alerts_per_run")):
                continue
            fp = self._fresh_price(c)
            if fp is not None:
                if fp / c.coin.price - 1.0 > 0.04:  # صعدت أكثر من 4% منذ لقطة البيانات: فات الأوان
                    log.info("skip %s: already moved %+.1f%% since snapshot", c.coin.symbol, (fp / c.coin.price - 1) * 100)
                    continue
                c.coin.price = fp
            levels = self._levels(c)
            text = format_signal(c, s, levels, self.cfg.get("run.timezone"))
            if self._send(text):
                self._register(c, s, levels, now)
                self.summary["signals"].append((round(s, 1), c.coin.symbol))
                sent += 1

        self._heartbeat(now, stats)
        return self.summary

    # ------------------------------------------------------------------ مراحل
    def _bridge_flows(self, now: float) -> dict:
        try:
            tvl = self.llama.chains_tvl()
            stables = self.llama.stablecoin_supply()
            if not tvl and not stables:
                return {}
            return bridge_update(self.state.data, tvl, stables, now, self.cfg)
        except Exception as exc:
            log.warning("bridge bot failed: %s", exc)
            return {}

    def _add_whale_only(self, events: list, merged: dict) -> None:
        """عملات أشار إليها الحيتان ولم تظهر في أي قطاع: مرشحون مستقلون (بفلاتر سيولة صارمة)."""
        gates = self.cfg.get("gates")
        for ev in events:
            if ev["kind"] == "exchange_inflow":
                continue
            i = ev["info"]
            cid = f"dex:{ev['chain']}:{ev['addr']}"
            if cid in merged or (i.get("ch_h24") or 0) > gates["max_coin_24h"]:
                continue
            mcap = i["mcap"] or i["fdv"]
            if mcap and mcap > self.cfg.get("universe.max_market_cap"):
                continue
            coin = Coin(id=cid, symbol=i["symbol"], name=i["name"] or i["symbol"], price=i["price"],
                        mcap=mcap or 0.0, volume=i["vol_h24"], ch1h=i.get("ch_h1"), ch24h=i.get("ch_h24"))
            cand = Candidate(coin=coin, kinds={"whale"}, sectors=[f"شبكة {ev['chain']}"],
                             src={"src": "dex", "chain": ev["chain"], "addr": ev["addr"]})
            cand.features = {"whale": clip(ev["usd"] * ev.get("weight", 1.0) / 500000.0)}
            cand.meta = {"dex": i, "url": i.get("url", ""), "whale_only": True, "chain": ev["chain"]}
            merged[cid] = cand

    def _enrich_basic(self, c: Candidate, flows: dict) -> None:
        coin = c.coin
        r = self.vr(coin) if c.src.get("src") == "cg" else None
        if r is not None:
            c.features["vol"] = clip((r - 1.0) / 2.0)
            if r >= 1.5:
                c.reasons.append((5, f"📈 حجم تداول {coin.symbol} ×{r:.1f} فوق معدلها"))
        else:
            turnover = (coin.volume / coin.mcap) if coin.mcap > 0 else 0.0
            c.features["vol"] = clip((turnover - 0.05) / 0.25)
        ar = c.meta.get("att_ratio")
        c.features["attention"] = clip((ar - 1.0) / 3.0) if ar is not None else None
        chain = c.meta.get("chain")
        flow = flows.get(norm_chain(chain)) if chain else None
        c.features["stable"] = flow["score"] if flow and flow.get("score") is not None else None
        if flow and flow.get("flag") and "waterfall" not in c.kinds:
            c.kinds.add("bridge")
        c.features.setdefault("whale", None)
        c.features.setdefault("dex", None)

    def _passes_gates(self, c: Candidate, now: float) -> bool:
        g = self.cfg.get("gates")
        coin = c.coin
        if coin.mcap and coin.mcap > self.cfg.get("universe.max_market_cap"):
            return False
        if coin.ch24h is not None and coin.ch24h > g["max_coin_24h"]:
            return False
        if coin.ch1h is not None and coin.ch1h > g["max_coin_1h"]:
            return False
        cd = self.state.data["cooldown"].get(coin.id)
        if cd and now - cd < float(self.cfg.get("run.cooldown_hours")) * 3600:
            return False
        if any(s["coin_id"] == coin.id for s in self.state.data["open"]):
            return False
        return True

    def _identify_dex(self, coin: Coin) -> Optional[dict]:
        """يبحث عن زوج DexScreener لنفس العملة مع فلتر هوية (رمز + قيمة سوقية) ضد الانتحال."""
        dx = self.cfg.get("dex")
        best = None
        for p in self.dex.search(coin.symbol):
            s = summarize_pair(p)
            if not s or s["symbol"] != coin.symbol or s["liq"] < dx["min_liquidity"]:
                continue
            if coin.mcap > 0:
                ok = any(v > 0 and dx["identity_low"] <= v / coin.mcap <= dx["identity_high"]
                         for v in (s["mcap"], s["fdv"]))
                if not ok:
                    continue
            if best is None or s["liq"] > best["liq"]:
                best = s
        return best

    def _confirm_onchain(self, c: Candidate, events: list, flows: dict, now: float) -> None:
        coin = c.coin
        dx = self.cfg.get("dex")
        info = c.meta.get("dex")
        if info is None and c.src.get("src") == "cg":
            try:
                info = self._identify_dex(coin)
            except Exception as exc:
                log.warning("dex identify failed %s: %s", coin.symbol, exc)
        if info:
            c.meta["dex"] = info
            tx = info["buys_h1"] + info["sells_h1"]
            if tx >= dx["min_txns_h1"]:
                buy = info["buys_h1"] / tx
                accel = (info["vol_h1"] * 24.0 / info["vol_h24"]) if info["vol_h24"] > 0 else 1.0
                c.features["dex"] = 0.6 * clip((buy - 0.5) / 0.2) + 0.4 * clip((accel - 1.0) / 2.0)
                if buy >= 0.58:
                    c.reasons.append((6, f"🟢 ضغط شراء على DEX: {info['buys_h1']} شراء / {info['sells_h1']} بيع (1س)"))
            if not c.meta.get("chain"):
                c.meta["chain"] = info["chain"]
                flow = flows.get(norm_chain(info["chain"]))
                if flow and flow.get("score") is not None:
                    c.features["stable"] = flow["score"]
            key = (info["chain"], info["addr"])
        else:
            key = None

        whale_usd = 0.0
        checked = False
        if key:
            for ev in events:
                if (ev["chain"], ev["addr"]) != key:
                    continue
                checked = True
                if ev["kind"] == "exchange_inflow" and ev["usd"] >= self.cfg.get("whales.sell_block_usd"):
                    c.meta["blocked"] = True
                    log.info("blocked %s: whale exchange inflow %s", coin.symbol, ev["usd"])
                elif ev["kind"] in ("accumulation", "exchange_outflow"):
                    whale_usd += ev["usd"] * ev.get("weight", 1.0)
                    label = "سحب من المنصة" if ev["kind"] == "exchange_outflow" else "تجميع"
                    c.reasons.append((0, f"🐋 {ev['wallet']}: {label} {_usd(ev['usd'])}"))
            if info and info["chain"] in EVM_CHAIN_IDS and self.whale.can_check_flows:
                fl = self.whale.token_exchange_flow(info["chain"], info["addr"], info["price"], now)
                if fl is not None:
                    checked = True
                    net = fl["out_usd"] - fl["in_usd"]
                    if fl["in_usd"] >= self.cfg.get("whales.sell_block_usd") and fl["in_usd"] > 2 * fl["out_usd"]:
                        c.meta["blocked"] = True
                    elif net > 0:
                        whale_usd += net
                        if net >= self.cfg.get("whales.min_usd"):
                            c.reasons.append((0, f"🏦 صافي سحب من المنصات {_usd(net)} (90 دقيقة)"))
        if checked or self.whale.enabled:
            c.features["whale"] = max(c.features.get("whale") or 0.0, clip(whale_usd / 500000.0))
            if c.features["whale"] >= 0.3:
                c.kinds.add("whale")

    def _levels(self, c: Candidate) -> dict:
        r = self.cfg.get("risk")
        dv = c.meta.get("daily_vol") or r["default_daily_vol"]
        stop = clip(1.2 * dv, r["stop_min"], r["stop_max"])
        target = stop * r["rr"]
        p = c.coin.price
        return {"stop_pct": stop, "target_pct": target, "stop": p * (1 - stop / 100), "target": p * (1 + target / 100)}

    def _register(self, c: Candidate, score: float, levels: dict, now: float) -> None:
        d = self.state.data
        d["seq"] += 1
        d["open"].append({
            "id": d["seq"], "coin_id": c.coin.id, "symbol": c.coin.symbol, "name": c.coin.name,
            "entry": c.coin.price, "ts": now, "kinds": sorted(c.kinds), "sectors": c.sectors, "score": score,
            "features": c.features, "target_pct": levels["target_pct"], "stop_pct": levels["stop_pct"],
            "hi": c.coin.price, "lo": c.coin.price, "src": c.src, "miss": 0,
        })
        d["cooldown"][c.coin.id] = now

    # ------------------------------------------------------------------ نتائج الصفقات والتعلّم
    def _fetch_prices(self, open_sigs: list) -> dict:
        prices: dict = {}
        cg_ids = sorted({s["src"]["id"] for s in open_sigs if s["src"].get("src") == "cg"})
        if cg_ids:
            try:
                got = self.llama.coin_prices(cg_ids)
            except Exception as exc:
                log.warning("llama prices failed: %s", exc)
                got = {}
            missing = [i for i in cg_ids if i not in got]
            if missing and self.budget.take():
                got = {**got, **self.cg.prices(missing)}
            prices.update({f"cg:{k}": v for k, v in got.items()})
        by_chain: dict = {}
        for s in open_sigs:
            if s["src"].get("src") == "dex":
                by_chain.setdefault(s["src"]["chain"], set()).add(s["src"]["addr"])
        for chain, addrs in by_chain.items():
            info = best_by_address(self.dex.tokens(chain, sorted(addrs)))
            prices.update({f"dex:{chain}:{a}": i["price"] for a, i in info.items()})
        return prices

    def _update_outcomes(self, now: float) -> None:
        d = self.state.data
        if not d["open"]:
            return
        risk = self.cfg.get("risk")
        horizon = float(risk["horizon_hours"]) * 3600
        prices = self._fetch_prices(d["open"])
        still, samples = [], []
        for s in d["open"]:
            key = f"cg:{s['src']['id']}" if s["src"].get("src") == "cg" else f"dex:{s['src']['chain']}:{s['src']['addr']}"
            p = prices.get(key)
            age = now - s["ts"]
            if p is None:
                s["miss"] = s.get("miss", 0) + 1
                if age > horizon * 1.5:  # لا يمكن تقييمها: نُسقطها دون تعلّم
                    continue
                still.append(s)
                continue
            s["hi"], s["lo"] = max(s["hi"], p), min(s["lo"], p)
            ret = (p / s["entry"] - 1.0) * 100.0
            y = reason = None
            if ret >= s["target_pct"]:
                y, reason = 1, "target"
            elif ret <= -s["stop_pct"]:
                y, reason = 0, "stop"
            elif age >= horizon:
                y, reason = (1 if ret >= 0.4 * s["target_pct"] else 0), "expired"
            if y is None:
                still.append(s)
                continue
            rec = {"id": s["id"], "symbol": s["symbol"], "kinds": s["kinds"], "score": s["score"], "y": y,
                   "ret": round(ret, 2), "mfe": round((s["hi"] / s["entry"] - 1) * 100, 2),
                   "mae": round((s["lo"] / s["entry"] - 1) * 100, 2), "reason": reason,
                   "features": s["features"], "ts": s["ts"], "closed": now}
            d["closed"].append(rec)
            samples.append((s["features"], y))
            self.summary["closed"] += 1
            if self.cfg.get("run.notify_outcomes"):
                self._send(format_outcome({**s, "y": y}, ret, reason, age / 3600.0))
        d["open"] = still
        if samples:
            learned = Brain(self.cfg, d).learn(samples)
            log.info("تعلّم النموذج من %d صفقة مغلقة", learned)

    # ------------------------------------------------------------------ نبضة الحياة
    def _heartbeat(self, now: float, stats: dict) -> None:
        hrs = float(self.cfg.get("run.heartbeat_hours"))
        meta = self.state.data["meta"]
        if hrs <= 0 or not stats or now - meta.get("last_heartbeat", 0) < hrs * 3600:
            return  # لا نبضة إذا فشل جلب بيانات القطاعات (حتى لا نوهم المستخدم بأن كل شيء سليم)
        top = sorted(stats.values(), key=lambda s: s["heat"], reverse=True)[:3]
        wr, n = self.brain.win_rate()
        lines = ["📡 <b>الرادار يعمل</b>",
                 "أسخن القطاعات: " + " | ".join(f"{s['label'].split('·')[0].strip()} {s['heat']:+.1f}%" for s in top),
                 f"صفقات مفتوحة: {len(self.state.data['open'])} | مُقيَّمة: {n}"
                 + (f" | نجاح: {wr * 100:.0f}%" if wr is not None else ""),
                 f"CoinGecko هذا الشهر: {self.state.data['cg']['used']}/{int(self.budget.monthly)} طلب"]
        if self._send("\n".join(lines)):
            meta["last_heartbeat"] = now

    def write_step_summary(self) -> None:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"### رادار السيولة — تشغيلة #{self.state.data['meta']['runs']}\n")
                f.write(f"- الحد الأدنى الحالي: {self.summary.get('threshold')}\n")
                f.write(f"- إشارات مُرسلة: {self.summary['signals']}\n- أقرب المرشحين: {self.summary['near_miss']}\n")
                hot = [s['label'] for s in self.summary['sectors'].values() if s['hot']]
                f.write(f"- قطاعات ساخنة: {hot}\n- شبكات مُفعَّلة: {self.summary.get('triggers')}\n")
        except OSError:
            pass


def _usd(x: float) -> str:
    return fmt_usd(x)


# ======================================================================
# cli.py
# ======================================================================
log = logging.getLogger("radar")
DEFAULT_STATE = os.environ.get("RADAR_STATE", ".radar_state/state.json")


def build(dry_run: bool, state_path: str):
    cfg = load_config()
    env = cfg.env
    interval = cfg.get("network.coingecko_interval_key" if env.get("COINGECKO_API_KEY")
                       else "network.coingecko_interval_free")
    http = Http(intervals={"api.coingecko.com": interval, "pro-api.coingecko.com": interval,
                           "api.etherscan.io": 0.25, "api.dexscreener.com": 0.25,
                           "api.mainnet-beta.solana.com": 1.0})
    cg = CoinGecko(http, env.get("COINGECKO_API_KEY"), env.get("COINGECKO_PLAN") or "demo")
    eth = Etherscan(http, env["ETHERSCAN_API_KEY"]) if env.get("ETHERSCAN_API_KEY") else None
    sol = SolanaRPC(http, env.get("SOLANA_RPC_URL")) if cfg.whales.get("wallets") else None
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if dry_run or not (token and chat):
        if not dry_run:
            missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env.get(k)]
            log.warning("الأسرار المفقودة: %s -> وضع تجريبي (الرسائل تُطبع فقط). "
                        "أضفها في Settings > Secrets and variables > Actions > Repository secrets بنفس الاسم تماماً",
                        ", ".join(missing))
        notifier = ConsoleNotifier()
    else:
        notifier = Telegram(http, token, chat)
    if token and ":" not in token:
        log.warning("TELEGRAM_BOT_TOKEN شكله غير صحيح (التوكن الصحيح يحوي نقطتين :)")
    state = State(state_path)
    engine = Engine(cfg, state, cg, DefiLlama(http), DexScreener(http), eth, sol, notifier, dry_run=dry_run)
    return cfg, http, state, engine, notifier


def cmd_run(args) -> int:
    _, _, state, engine, _ = build(args.dry_run, args.state)
    try:
        summary = engine.run()
        log.info("تم: قطاعات=%d | إشارات=%s | أقرب=%s | حد=%s", len(summary["sectors"]),
                 summary["signals"], summary["near_miss"][:3], summary.get("threshold"))
        engine.write_step_summary()
        return 0
    finally:
        state.save()  # نحفظ الحالة حتى لو حدث خطأ متأخر


def cmd_check(args) -> int:
    cfg, http, _, _, _ = build(True, args.state)
    env = cfg.env
    cg = CoinGecko(http, env.get("COINGECKO_API_KEY"), env.get("COINGECKO_PLAN") or "demo")
    ok = True
    ids = set(cg.category_ids())
    print(f"CoinGecko: {'OK' if ids else 'FAIL'} ({len(ids)} فئة)")
    ok &= bool(ids)
    for key, s in cfg.sectors.items():
        good = s.get("category") in ids
        print(f"  قطاع {key:9s} {s.get('category'):34s} {'✓' if good else '✗ غير صالح'}")
    for key, e in cfg.ecosystems.items():
        good = e.get("category") in ids
        print(f"  نظام {key:12s} {e.get('category'):30s} {'✓' if good else '✗ غير صالح'}")
    dl = DefiLlama(http)
    print(f"DefiLlama chains: {len(dl.chains_tvl())} | stablecoin chains: {len(dl.stablecoin_supply())}")
    print(f"DexScreener search: {len(DexScreener(http).search('SOL'))} أزواج")
    print(f"Etherscan key: {'موجود' if env.get('ETHERSCAN_API_KEY') else 'غير موجود (اختياري)'}")
    print(f"محافظ مراقبة: {len(cfg.whales.get('wallets') or [])}")
    tok = env.get("TELEGRAM_BOT_TOKEN")
    if tok:
        d = http.get_json(f"https://api.telegram.org/bot{tok}/getMe")
        print(f"Telegram getMe: {'OK' if isinstance(d, dict) and d.get('ok') else 'FAIL'}")
    else:
        print("Telegram: TELEGRAM_BOT_TOKEN غير موجود")
    return 0 if ok else 1


def cmd_test_telegram(args) -> int:
    cfg, http, _, _, _ = build(False, args.state)
    tok, chat = cfg.env.get("TELEGRAM_BOT_TOKEN"), cfg.env.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("أضف TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID أولاً")
        return 1
    tg = Telegram(http, tok, chat)
    ok = tg.send("✅ <b>رادار السيولة</b> — اختبار الاتصال ناجح\nالرسالة التالية مثال لشكل التنبيه (ليست إشارة حقيقية).")
    if ok:
        coin = Coin("demo", "DEMO", "Demo Token", 1.2345, 5e7, 3e6, 0.8, -1.2, 3.0)
        cand = Candidate(coin=coin, kinds={"whale", "waterfall"}, sectors=["مثال · RWA"], meta={"url": "https://www.coingecko.com"},
                         reasons=[(0, "🐋 DWF Labs (مشتريات معلنة): تجميع $320K"),
                                  (2, "🌊 شلال Solana: SOL +9.0% (24س) ← DEMO لم تتحرك بعد (-1.2%)"),
                                  (3, "🔥 قطاع RWA ساخن +6.5% (اتساع 78%) والعملة متأخرة -1.2%"),
                                  (6, "🟢 ضغط شراء على DEX: 70 شراء / 20 بيع (1س)")])
        price = coin.price
        tg.send(format_signal(cand, 74.0, {"target": price * 1.1, "stop": price * 0.94, "target_pct": 10.0,
                                          "stop_pct": 6.0}, cfg.get("run.timezone")))
    print("تم الإرسال" if ok else "فشل الإرسال: تحقق من التوكن والـ chat id (وأرسل /start للبوت أو أضف البوت مشرفاً في القناة)")
    return 0 if ok else 1


def cmd_stats(args) -> int:
    cfg = load_config()
    st = State(args.state)
    brain = Brain(cfg, st.data)
    wr, n = brain.win_rate()
    print(f"تشغيلات: {st.data['meta']['runs']} | صفقات مفتوحة: {len(st.data['open'])} | مُقيَّمة: {n}")
    if wr is not None:
        print(f"نسبة النجاح العامة: {wr * 100:.1f}%")
        for kind in ("whale", "bridge", "waterfall", "sector"):
            w, k = brain.win_rate(kind)
            if k:
                print(f"  {kind:10s} {w * 100:5.1f}%  ({k})")
    print(f"وزن النموذج المتعلَّم α = {brain.alpha():.2f} | الحد الأدنى الحالي = {brain.threshold():.1f}")
    print("أوزان النموذج:", {k: round(v, 2) for k, v in brain.m["theta"].items()})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="radar", description="رادار شلال السيولة للكريبتو")
    ap.add_argument("--state", default=DEFAULT_STATE)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="تشغيلة واحدة (تستخدمها GitHub Actions)")
    r.add_argument("--dry-run", action="store_true", help="بدون إرسال تيليجرام")
    sub.add_parser("check", help="فحص الاتصال وصلاحية الفئات")
    sub.add_parser("test-telegram", help="إرسال رسالة اختبار")
    sub.add_parser("stats", help="إحصائيات التعلّم الذاتي")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    cmd = args.cmd or "run"
    if cmd == "run" and not hasattr(args, "dry_run"):
        args.dry_run = False
    return {"run": cmd_run, "check": cmd_check, "test-telegram": cmd_test_telegram, "stats": cmd_stats}[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
