#!/usr/bin/env python3
"""
رادار شلال السيولة — Crypto Liquidity Radar  (ملف واحد)

    python radar.py run --dry-run     # تشغيلة تجريبية بدون تيليجرام
    python radar.py run               # تشغيلة حقيقية (GitHub Actions كل 10 دقائق)
    python radar.py selftest          # التحقق الذاتي من الكود + فحص المزوّدين
    python radar.py check             # فحص الاتصال وحساب ميزانية CoinGecko
    python radar.py backtest          # اختبار رجعي وحفظ knowledge.json (ارتباط العملات وانتقال السيولة)
    python radar.py test-telegram     # رسائل اختبار بشكل التنبيهات
    python radar.py stats             # إحصائيات التعلّم الذاتي
كل الإعدادات في config.yaml
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import logging
import math
import os
import re
import sys
import time
import traceback
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import numpy as np
import requests
import yaml

log = logging.getLogger("radar")


# ======================================================================
# models.py
# ======================================================================
FEATURES = ["sector", "breadth", "gap", "corr", "vol", "attention", "stable", "whale", "dex", "momo", "context", "rotation"]


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
    live: bool = False      # تم تحديث السعر لحظياً من بورصة عامة
    xvol: float = 0.0       # حجم 24س في البورصة (USDT)
    age_h: float = 0.0      # عمر بيانات الكاش بالساعات

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
    "ETHERSCAN_API_KEY", "SOLANA_RPC_URL", "ANTHROPIC_API_KEY",
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


def redact(path: str) -> str:
    """يخفي الأسرار من المسارات قبل تسجيلها (توكن تيليجرام داخل المسار)."""
    return re.sub(r"/bot[^/]+", "/bot***", path or "")


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

    def request(self, method, url, params=None, headers=None, json_body=None, retries: int = 3, text: bool = False):
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
                if text:
                    return r.text
                try:
                    return r.json()
                except ValueError:
                    log.warning("invalid JSON from %s%s", host, redact(parsed.path))
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
            log.warning("HTTP %s from %s%s %s", r.status_code, host, redact(parsed.path), redact(r.text[:160].replace("\n", " ")))
            return None
        self._fails[host] = self._fails.get(host, 0) + 1
        return None

    def get_text(self, url, params=None, headers=None, retries: int = 3):
        return self.request("GET", url, params=params, headers=headers, retries=retries, text=True)

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
        "px": {},
        "kl": {},
        "sh": {},
        "members": {"ts": 0, "sectors": {}, "eco": {}, "tags": {}},
        "snap": {"ts": 0, "rows": []},
        "news": {"ts": 0, "items": []},
        "protocols": {"ts": 0, "map": {}},
        "discovery": {},
        "warned": {},
        "last_error_ts": 0,
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
        d["warned"] = {k: v for k, v in d.get("warned", {}).items() if now - v < 3 * 86400}

    def save(self) -> None:
        self.trim()
        if len(json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))) > 9_000_000:  # حماية حجم الكاش
            for k in ("kl", "mcache", "px", "news"):
                self.data.pop(k, None)
            self.data.update({"mcache": {}, "px": {}, "news": {"ts": 0, "items": []}})
            log.warning("الحالة كبيرة جداً: تم تفريغ الكاش المؤقت")
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

    def protocols(self) -> dict:
        """تغيّر TVL للبروتوكولات (مجاني بلا مفتاح). مفاتيح: gecko_id و"sym:الرمز"، مع الفئة والشبكات."""
        data = self.http.get_json("https://api.llama.fi/protocols")
        out: dict = {}
        for p in data if isinstance(data, list) else []:
            if not isinstance(p, dict):
                continue
            tvl = to_float(p.get("tvl"))
            if not tvl or tvl < 2e6:
                continue
            info = {"tvl": tvl, "d1": to_float(p.get("change_1d")), "d7": to_float(p.get("change_7d")),
                    "cat": str(p.get("category") or ""), "chains": [str(c) for c in (p.get("chains") or [])][:12]}
            keys = []
            if p.get("gecko_id"):
                keys.append(str(p["gecko_id"]))
            if p.get("symbol") and str(p["symbol"]) not in ("-", ""):
                keys.append("sym:" + str(p["symbol"]).upper())
            for k in keys:
                if k not in out or tvl > out[k]["tvl"]:
                    out[k] = info
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
            "pair": str(p.get("pairAddress") or "").lower(),
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

    def block_by_time(self, chain_id: int, ts: int) -> Optional[int]:
        d = self.http.get_json(self.BASE, params={"chainid": chain_id, "module": "block", "action": "getblocknobytime",
                                                  "timestamp": int(ts), "closest": "before", "apikey": self.key})
        try:
            return int(d["result"])
        except (TypeError, KeyError, ValueError):
            return None

    def token_transfers(self, chain_id: int, address: Optional[str] = None,
                        contract: Optional[str] = None, offset: int = 100, startblock: Optional[int] = None,
                        endblock: Optional[int] = None, sort: str = "desc"):
        """قائمة تحويلات ERC-20. تعيد None عند الفشل و[] عند عدم وجود معاملات."""
        params = {
            "chainid": chain_id, "module": "account", "action": "tokentx",
            "page": 1, "offset": offset, "sort": sort, "apikey": self.key,
        }
        if startblock is not None:
            params["startblock"] = startblock
        if endblock is not None:
            params["endblock"] = endblock
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
# providers/extra
# ======================================================================
class Exchanges:
    """بورصات عامة بلا مفتاح وبلا سقف شهري: أسعار لحظية + شموع + دفتر أوامر.

    تجرّب المصادر بالترتيب وتتذكر أول مصدر يعمل (Binance محجوبة من خوادم GitHub الأمريكية، لذا
    نبدأ بـ data-api.binance.vision ثم MEXC ثم Gate ثم KuCoin). الفشل لا يوقف الرادار.
    """

    SOURCES = {
        "binance_vision": {"base": "https://data-api.binance.vision", "style": "binance"},
        "mexc": {"base": "https://api.mexc.com", "style": "binance"},
        "gate": {"base": "https://api.gateio.ws/api/v4", "style": "gate"},
        "kucoin": {"base": "https://api.kucoin.com", "style": "kucoin"},
    }
    BAD = re.compile(r"(\d+[LS]|BULL|BEAR)$")
    OK_BASE = re.compile(r"^[A-Z0-9]{2,15}$")

    def __init__(self, http, order=None):
        self.http = http
        self.order = [o for o in (order or list(self.SOURCES)) if o in self.SOURCES]
        self.src: Optional[str] = None

    # ------------------------------------------------------------- أسعار لحظية لكل الأزواج
    def _add(self, out: dict, base: str, last, ch, qv, name: str) -> None:
        base = str(base).upper()
        if not self.OK_BASE.match(base) or self.BAD.search(base):
            return
        last, qv = to_float(last), to_float(qv)
        if not last or last <= 0 or not qv or qv <= 0:
            return
        if base in out and out[base]["qvol"] >= qv:
            return
        out[base] = {"price": last, "ch24": to_float(ch), "qvol": qv, "src": name}

    def _tickers_from(self, name: str) -> dict:
        cfg = self.SOURCES[name]
        out: dict = {}
        if cfg["style"] == "binance":
            rows = self.http.get_json(cfg["base"] + "/api/v3/ticker/24hr", retries=2)
            for r in rows if isinstance(rows, list) else []:
                sym = str(r.get("symbol", ""))
                if not sym.endswith("USDT"):
                    continue
                last, op = to_float(r.get("lastPrice")), to_float(r.get("openPrice"))
                ch = (last / op - 1.0) * 100.0 if last and op else None
                self._add(out, sym[:-4], last, ch, r.get("quoteVolume"), name)
        elif cfg["style"] == "gate":
            rows = self.http.get_json(cfg["base"] + "/spot/tickers", retries=2)
            for r in rows if isinstance(rows, list) else []:
                pair = str(r.get("currency_pair", ""))
                if pair.endswith("_USDT"):
                    self._add(out, pair[:-5], r.get("last"), r.get("change_percentage"), r.get("quote_volume"), name)
        elif cfg["style"] == "kucoin":
            d = self.http.get_json(cfg["base"] + "/api/v1/market/allTickers", retries=2)
            rows = ((d or {}).get("data") or {}).get("ticker") if isinstance(d, dict) else None
            for r in rows if isinstance(rows, list) else []:
                sym = str(r.get("symbol", ""))
                rate = to_float(r.get("changeRate"))
                if sym.endswith("-USDT"):
                    self._add(out, sym[:-5], r.get("last"), rate * 100.0 if rate is not None else None,
                              r.get("volValue"), name)
        return out

    def tickers(self) -> dict:
        names = ([self.src] if self.src else []) + [o for o in self.order if o != self.src]
        for name in names:
            try:
                data = self._tickers_from(name)
            except Exception as exc:
                log.warning("exchange %s tickers failed: %s", name, exc)
                continue
            if len(data) >= 50:
                self.src = name
                return data
        self.src = None
        return {}

    # ------------------------------------------------------------- شموع تاريخية
    def klines(self, base: str, limit: int = 500, pages: int = 1) -> list:
        """[[ts_ms, open, high, low, close, quote_vol], ...] تصاعدياً. يعيد [] عند الفشل."""
        names = ([self.src] if self.src else []) + [o for o in self.order if o != self.src]
        for name in names:
            try:
                rows = self._klines_from(name, base, limit, pages)
            except Exception as exc:
                log.warning("klines %s %s failed: %s", name, base, exc)
                continue
            if rows:
                return rows
        return []

    def _klines_from(self, name: str, base: str, limit: int, pages: int) -> list:
        cfg = self.SOURCES[name]
        out: list = []
        if cfg["style"] == "binance":
            per = 500 if name == "mexc" else 1000
            iv = "60m" if name == "mexc" else "1h"
            end = None
            for _ in range(max(1, pages)):
                params = {"symbol": base + "USDT", "interval": iv, "limit": min(limit, per)}
                if end:
                    params["endTime"] = end
                rows = self.http.get_json(cfg["base"] + "/api/v3/klines", params=params, retries=2)
                if not isinstance(rows, list) or not rows:
                    break
                chunk = []
                for r in rows:
                    try:
                        chunk.append([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                                      float(r[7]) if len(r) > 7 else 0.0])
                    except (TypeError, ValueError, IndexError):
                        continue
                if not chunk:
                    break
                out = chunk + out
                end = chunk[0][0] - 1
                if len(out) >= limit:
                    break
            return out[-limit:] if limit else out
        if cfg["style"] == "gate":
            rows = self.http.get_json(cfg["base"] + "/spot/candlesticks", retries=2,
                                      params={"currency_pair": f"{base}_USDT", "interval": "1h", "limit": min(limit, 1000)})
            for r in rows if isinstance(rows, list) else []:
                try:  # [ts, quote_vol, close, high, low, open, base_vol, closed]
                    out.append([int(float(r[0])) * 1000, float(r[5]), float(r[3]), float(r[4]), float(r[2]), float(r[1])])
                except (TypeError, ValueError, IndexError):
                    continue
            return out[-limit:]
        if cfg["style"] == "kucoin":
            d = self.http.get_json(cfg["base"] + "/api/v1/market/candles", retries=2,
                                   params={"type": "1hour", "symbol": f"{base}-USDT"})
            rows = d.get("data") if isinstance(d, dict) else None
            for r in reversed(rows) if isinstance(rows, list) else []:
                try:  # [time, open, close, high, low, volume, turnover] الأحدث أولاً
                    out.append([int(float(r[0])) * 1000, float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[6])])
                except (TypeError, ValueError, IndexError):
                    continue
            return out[-limit:]
        return out

    # ------------------------------------------------------------- دفتر الأوامر
    def orderbook(self, base: str, limit: int = 100) -> Optional[dict]:
        names = ([self.src] if self.src else []) + [o for o in self.order if o != self.src]
        for name in names:
            try:
                book = self._book_from(name, base, limit)
            except Exception as exc:
                log.warning("orderbook %s %s failed: %s", name, base, exc)
                continue
            if book and book["asks"] and book["bids"]:
                return book
        return None

    def _book_from(self, name: str, base: str, limit: int) -> Optional[dict]:
        cfg = self.SOURCES[name]
        if cfg["style"] == "binance":
            d = self.http.get_json(cfg["base"] + "/api/v3/depth", params={"symbol": base + "USDT", "limit": limit}, retries=2)
        elif cfg["style"] == "gate":
            d = self.http.get_json(cfg["base"] + "/spot/order_book", retries=2,
                                   params={"currency_pair": f"{base}_USDT", "limit": limit})
        else:
            d = self.http.get_json(cfg["base"] + "/api/v1/market/orderbook/level2_100", params={"symbol": f"{base}-USDT"}, retries=2)
            d = d.get("data") if isinstance(d, dict) else None
        if not isinstance(d, dict):
            return None

        def norm(side):
            res = []
            for lv in d.get(side) or []:
                try:
                    p, q = float(lv[0]), float(lv[1])
                    if p > 0 and q > 0:
                        res.append((p, q))
                except (TypeError, ValueError, IndexError):
                    continue
            return res

        asks, bids = sorted(norm("asks")), sorted(norm("bids"), reverse=True)
        return {"asks": asks, "bids": bids}


EVM_CHAIN_IDS_FREE = {"ethereum", "arbitrum", "polygon"}  # شبكات يُرجَّح دعمها في خطة Etherscan المجانية
BLOCKSCOUT = {
    "ethereum": "https://eth.blockscout.com",
    "base": "https://base.blockscout.com",
    "optimism": "https://optimism.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
}


class Blockscout:
    """Blockscout العام (بلا مفتاح): بديل مجاني لـ Etherscan على Base/Optimism وغيرها."""

    def __init__(self, http):
        self.http = http

    @staticmethod
    def _ts(v) -> int:
        try:
            return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return 0

    def _norm(self, items) -> list:
        out = []
        for it in items or []:
            try:
                tok = it.get("token") or {}
                total = it.get("total") or {}
                if tok.get("type") not in (None, "ERC-20"):
                    continue
                addr = tok.get("address_hash") or tok.get("address")
                dec = total.get("decimals") if total.get("decimals") is not None else tok.get("decimals")
                out.append({
                    "timeStamp": str(self._ts(it.get("timestamp"))),
                    "from": str((it.get("from") or {}).get("hash", "")),
                    "to": str((it.get("to") or {}).get("hash", "")),
                    "value": str(total.get("value", "0")),
                    "tokenDecimal": str(dec or 0),
                    "tokenSymbol": str(tok.get("symbol") or ""),
                    "contractAddress": str(addr or ""),
                })
            except (AttributeError, TypeError):
                continue
        return out

    def _get(self, chain: str, path: str, params=None):
        base = BLOCKSCOUT.get(chain)
        if not base:
            return None
        d = self.http.get_json(base + path, params=params, retries=2)
        return self._norm(d.get("items")) if isinstance(d, dict) and "items" in d else None

    def wallet_transfers(self, chain: str, address: str):
        return self._get(chain, f"/api/v2/addresses/{address}/token-transfers", {"type": "ERC-20"})

    def contract_transfers(self, chain: str, contract: str):
        return self._get(chain, f"/api/v2/tokens/{contract}/transfers")


class Transfers:
    """واجهة موحدة: Etherscan (إن وُجد مفتاح) ثم Blockscout المجاني كبديل."""

    def __init__(self, eth, bs):
        self.eth, self.bs = eth, bs

    def supports(self, chain: str) -> bool:
        return bool((self.eth and chain in EVM_CHAIN_IDS_FREE) or (self.bs and chain in BLOCKSCOUT))

    def wallet(self, chain: str, addr: str):
        if self.eth and chain in EVM_CHAIN_IDS_FREE:
            r = self.eth.token_transfers(EVM_CHAIN_IDS[chain], address=addr)
            if r is not None:
                return r
        if self.bs and chain in BLOCKSCOUT:
            return self.bs.wallet_transfers(chain, addr)
        return None

    def contract(self, chain: str, contract: str):
        if self.eth and chain in EVM_CHAIN_IDS_FREE:
            r = self.eth.token_transfers(EVM_CHAIN_IDS[chain], contract=contract)
            if r is not None:
                return r
        if self.bs and chain in BLOCKSCOUT:
            return self.bs.contract_transfers(chain, contract)
        return None


NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]


class News:
    """عناوين أخبار الكريبتو من RSS عامة (بلا مفتاح)."""

    def __init__(self, http, feeds=None):
        self.http = http
        self.feeds = feeds or NEWS_FEEDS

    def fetch(self) -> list:
        items = []
        for url in self.feeds:
            txt = self.http.get_text(url, retries=2)
            if not txt or len(txt) > 3_000_000 or "<!ENTITY" in txt or "<!DOCTYPE" in txt[:400].replace("<!DOCTYPE html", ""):
                continue
            try:
                root = ET.fromstring(txt)
            except ET.ParseError:
                continue
            host = urlparse(url).netloc
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                try:
                    ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
                except (TypeError, ValueError):
                    ts = 0.0
                if title:
                    items.append({"t": title, "ts": ts, "src": host})
        return items

    @staticmethod
    def match(items: list, coin, hours: float, now: float) -> list:
        sym, name = coin.symbol, coin.name
        pats = []
        if len(sym) >= 3:
            pats.append(re.compile(r"(?<![A-Za-z0-9])\$?" + re.escape(sym) + r"(?![A-Za-z0-9])"))  # حساس لحالة الأحرف
        if len(name) >= 4:
            pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.I))
        hits = []
        for it in items:
            if it["ts"] and now - it["ts"] > hours * 3600:
                continue
            if any(p.search(it["t"]) for p in pats):
                hits.append(it["t"])
        return hits[:3]


class LLMJudge:
    """مراجع اختياري بنموذج Claude (يلزم ANTHROPIC_API_KEY). يحكم على جودة الإشارة: go / caution / skip."""

    URL = "https://api.anthropic.com/v1/messages"
    SYSTEM = ("You are a strict risk reviewer for crypto trading signals produced by a quantitative radar. "
              "Given the data, answer ONLY with compact JSON: "
              '{"verdict":"go|caution|skip","confidence":0-100,"note":"<=140 chars, Arabic"}. '
              "Choose skip for likely scams, wash-traded or already-exhausted moves, caution for weak evidence.")

    def __init__(self, http, api_key: str, model: str):
        self.http, self.key, self.model = http, api_key, model

    @staticmethod
    def parse(text: str) -> Optional[dict]:
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return None
        v = str(d.get("verdict", "")).lower()
        if v not in ("go", "caution", "skip"):
            return None
        return {"verdict": v, "confidence": to_float(d.get("confidence")) or 50.0, "note": str(d.get("note", ""))[:160]}

    def review(self, summary: str) -> Optional[dict]:
        payload = {"model": self.model, "max_tokens": 300, "system": self.SYSTEM,
                   "messages": [{"role": "user", "content": summary[:6000]}]}
        d = self.http.post_json(self.URL, payload, headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                                                            "content-type": "application/json"}, retries=2)
        try:
            return self.parse(d["content"][0]["text"])
        except (TypeError, KeyError, IndexError):
            return None


# ======================================================================
# providers/paprika
# ======================================================================
class Paprika:
    """مصدر بيانات مجاني بلا مفتاح (CoinPaprika: 20,000 طلب/شهر). طلب واحد لكل تشغيلة يعيد كل العملات.

    لا يوفّر sparkline، لذلك تُبنى الشموع الساعية من البورصات العامة (انظر Engine._attach_sparks)."""

    BASE = "https://api.coinpaprika.com/v1"
    bulk = True

    def __init__(self, http):
        self.http = http
        self.calls = 0
        self.last: dict = {}

    def _get(self, path: str, params: Optional[dict] = None):
        self.calls += 1
        return self.http.get_json(self.BASE + path, params=params, retries=2)

    @staticmethod
    def to_coin(t) -> Optional[Coin]:
        try:
            q = (t.get("quotes") or {}).get("USD") or {}
            price = to_float(q.get("price"))
            if not t.get("id") or not price or price <= 0:
                return None
            return Coin(id=str(t["id"]), symbol=str(t.get("symbol") or "").upper(), name=str(t.get("name") or t["id"]),
                        price=price, mcap=to_float(q.get("market_cap")) or 0.0, volume=to_float(q.get("volume_24h")) or 0.0,
                        ch1h=to_float(q.get("percent_change_1h")), ch24h=to_float(q.get("percent_change_24h")),
                        ch7d=to_float(q.get("percent_change_7d")))
        except (AttributeError, TypeError):
            return None

    def snapshot(self) -> list:
        d = self._get("/tickers", {"quotes": "USD", "limit": 2000})
        coins = [c for c in (self.to_coin(t) for t in d) if c] if isinstance(d, list) else []
        if coins:
            self.last = {c.id: c.price for c in coins}
        return coins

    def tags(self) -> list:
        """[{id, name, coins: [ids] | None}]"""
        d = self._get("/tags", {"additional_fields": "coins"})
        out = []
        for t in d if isinstance(d, list) else []:
            if isinstance(t, dict) and t.get("id"):
                coins = t.get("coins")
                out.append({"id": str(t["id"]), "name": str(t.get("name") or ""),
                            "coins": [str(x) for x in coins] if isinstance(coins, list) else None})
        return out

    def tag_coins(self, tag_id: str) -> list:
        d = self._get(f"/tags/{tag_id}", {"additional_fields": "coins"})
        coins = d.get("coins") if isinstance(d, dict) else None
        return [str(x) for x in coins] if isinstance(coins, list) else []

    # واجهة متوافقة مع CoinGecko للأجزاء المشتركة
    def prices(self, ids: list) -> dict:
        return {i: self.last[i] for i in ids if i in self.last}

    def trending(self) -> set:
        return set()

    def category_ids(self) -> list:
        return []


def _toks(s: str) -> set:
    out = set()
    for t in re.split(r"[^a-z0-9]+", str(s).lower()):
        if t:
            out.add(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return out


def tag_matches(keywords: list, tag: dict) -> bool:
    """كلمة مفتاحية تطابق وسماً إذا كانت كل مفرداتها ضمن مفردات اسم/معرّف الوسم."""
    tt = _toks(tag["id"]) | _toks(tag["name"])
    return any(_toks(k) and _toks(k) <= tt for k in keywords)


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
# targets
# ======================================================================
def swing_points(arr: list, w: int = 3, kind: str = "high") -> list:
    pts = []
    for i in range(w, len(arr) - w):
        seg = arr[i - w:i + w + 1]
        if (kind == "high" and arr[i] == max(seg)) or (kind == "low" and arr[i] == min(seg)):
            pts.append(arr[i])
    return pts


def cluster_levels(levels: list, tol: float = 0.012) -> list:
    out: list = []
    for v in sorted(levels):
        if out and abs(v / out[-1]["p"] - 1.0) <= tol:
            c = out[-1]
            c["p"] = (c["p"] * c["n"] + v) / (c["n"] + 1)
            c["n"] += 1
        else:
            out.append({"p": v, "n": 1})
    return out


def resistance_levels(price: float, spark: list, w: int = 3) -> list:
    """مقاومات فوق السعر من قمم 7 أيام، الأقرب أولاً."""
    if len(spark) < 2 * w + 3:
        return []
    highs = swing_points(spark, w, "high") + [max(spark)]
    return cluster_levels([h for h in highs if h > price * 1.01])


def support_levels(price: float, spark: list, w: int = 3) -> list:
    """دعوم تحت السعر، الأقرب (الأعلى) أولاً."""
    if len(spark) < 2 * w + 3:
        return []
    lows = swing_points(spark, w, "low") + [min(spark)]
    return sorted(cluster_levels([x for x in lows if x < price * 0.985]), key=lambda c: -c["p"])


def book_walls(levels: list, price: float, side: str, min_usd: float, band_pct: float) -> list:
    """تجمعات أوامر ضخمة (جدران) داخل نطاق band_pct% من أول مستوى في التجمع."""
    groups, cur = [], None
    for p, q in levels:
        usd = p * q
        if cur and abs(p / cur["start"] - 1.0) * 100.0 <= band_pct:
            cur["usd"] += usd
            cur["wp"] += p * usd
        else:
            if cur:
                groups.append(cur)
            cur = {"start": p, "usd": usd, "wp": p * usd}
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        if g["usd"] < min_usd:
            continue
        wp = g["wp"] / g["usd"]
        dist = (wp / price - 1.0) * 100.0
        if (side == "ask" and dist >= 0.3) or (side == "bid" and dist <= -0.3):
            out.append({"price": wp, "usd": g["usd"], "dist_pct": dist})
    return out


def absorb_price(asks: list, flow_usd: float) -> Optional[float]:
    """السعر الذي يبلغه شراء بقيمة flow_usd عند استهلاك أوامر البيع الظاهرة. None إذا فاق التدفق عمق الدفتر."""
    cum = 0.0
    for p, q in asks:
        cum += p * q
        if cum >= flow_usd:
            return p
    return None


def dex_push_price(price: float, liq_usd: float, flow_usd: float) -> float:
    """أثر شراء على مجمع AMM بصيغة x*y=k: السعر ∝ (Q+Δ)²/Q² حيث Q نصف السيولة."""
    q = max(liq_usd / 2.0, 1.0)
    return price * ((q + flow_usd) / q) ** 2


def estimate_flow(whale_usd: float, volume_ref: float, share: float) -> float:
    return max(float(whale_usd or 0.0), share * float(volume_ref or 0.0))


def build_plan(price: float, spark: list, dv: float, gap_pct: Optional[float], flow_usd: float, book: Optional[dict],
               liq_usd: float, wall_usd: float, tcfg: dict, rcfg: dict, mfe: Optional[dict] = None) -> dict:
    """أهداف متدرجة مبنية على: مقاومات 7 أيام، جدران البيع، سدّ فجوة القطاع، امتصاص السيولة،
    وإحصاءات تاريخية. الوقف تحت أقرب دعم أو جدار شراء ضمن حدود المخاطرة."""
    min_t1, cap = float(tcfg["min_t1_pct"]), float(tcfg["max_target_pct"])
    res = resistance_levels(price, spark)
    sup = support_levels(price, spark)
    walls_up = walls_dn = []
    asks = (book or {}).get("asks") or []
    bids = (book or {}).get("bids") or []
    if asks:
        walls_up = book_walls(asks, price, "ask", wall_usd, tcfg["wall_band_pct"])
        walls_dn = book_walls(bids, price, "bid", wall_usd, tcfg["wall_band_pct"])

    cands: list = []
    for r in res[:4]:
        cands.append((r["p"] * 0.995, f"مقاومة 7 أيام (لُمست {r['n']}×)"))
    for w in walls_up[:3]:
        cands.append((w["price"] * 0.995, f"قبل جدار بيع {fmt_usd(w['usd'])}"))
    if gap_pct and gap_pct >= min_t1:
        cands.append((price * (1 + gap_pct / 100.0), "سدّ فجوة القطاع/الشبكة"))
    if flow_usd > 0:
        if asks:
            ap = absorb_price(asks, flow_usd)
            if ap:
                cands.append((ap, f"امتصاص تدفق ≈{fmt_usd(flow_usd)} لأوامر البيع"))
            elif liq_usd:
                cands.append((dex_push_price(price, liq_usd, flow_usd), f"تدفق ≈{fmt_usd(flow_usd)} يفوق عمق الدفتر"))
            else:
                cands.append((asks[-1][0] * 1.02, f"تدفق ≈{fmt_usd(flow_usd)} يفوق عمق الدفتر"))
        elif liq_usd:
            cands.append((dex_push_price(price, liq_usd, flow_usd), f"أثر تدفق ≈{fmt_usd(flow_usd)} على مجمع السيولة"))
    if mfe and mfe.get("p75"):
        cands.append((price * (1 + mfe["p75"] / 100.0), "ثلاثة أرباع الحالات المشابهة بلغته تاريخياً"))
    cands.append((price * (1 + 1.5 * dv / 100.0), "تذبذب العملة اليومي"))

    # أول جدار بيع لا يستطيع التدفق ابتلاعه = سقف واقعي
    ceiling = None
    for w in sorted(walls_up, key=lambda x: x["price"]):
        if w["usd"] > flow_usd * 0.8:
            ceiling = w
            break
    pool = []
    for tp, basis in cands:
        pct = (tp / price - 1.0) * 100.0
        if pct < min_t1 or pct > cap:
            continue
        if ceiling and tp > ceiling["price"] * 0.997:
            continue
        pool.append((tp, basis))
    if ceiling and (ceiling["price"] * 0.995 / price - 1.0) * 100.0 >= min_t1:
        pool.append((ceiling["price"] * 0.995, f"سقف واقعي: جدار بيع {fmt_usd(ceiling['usd'])} لا يبتلعه التدفق"))
    pool.sort(key=lambda x: x[0])
    targets: list = []
    for tp, basis in pool:
        if targets and (tp / targets[-1]["price"] - 1.0) * 100.0 < 3.0:
            continue
        targets.append({"price": tp, "basis": basis})
        if len(targets) == 4:
            break
    if not targets:
        step = max(min_t1, dv)
        targets = [{"price": price * (1 + step / 100.0), "basis": "تذبذب العملة اليومي"}]
    while len(targets) < 2:
        nxt = targets[-1]["price"] * 1.06
        if ceiling and nxt > ceiling["price"] * 0.997:
            break
        targets.append({"price": nxt, "basis": "امتداد تدريجي"})
    for i, t in enumerate(targets, 1):
        t["k"] = i
        t["pct"] = (t["price"] / price - 1.0) * 100.0

    # الوقف: تحت أقرب دعم هيكلي ضمن الحدود، وإلا حسب التذبذب
    smin, smax = float(rcfg["stop_min"]), float(rcfg["stop_max"])
    stop_p, stop_basis = None, ""
    struct = [(s["p"] * 0.997, "تحت دعم 7 أيام") for s in sup[:3]]
    struct += [(w["price"] * 0.997, f"تحت جدار شراء {fmt_usd(w['usd'])}") for w in walls_dn[:2]]
    for sp, basis in sorted(struct, key=lambda x: -x[0]):
        pct = (1.0 - sp / price) * 100.0
        if smin <= pct <= smax:
            stop_p, stop_basis = sp, basis
            break
    if stop_p is None:
        pct = clip(float(tcfg["stop_atr_mult"]) * dv, smin, smax)
        stop_p, stop_basis = price * (1 - pct / 100.0), "حسب تذبذب العملة"
    stop_pct = (1.0 - stop_p / price) * 100.0
    return {"targets": targets, "stop": stop_p, "stop_pct": stop_pct, "stop_basis": stop_basis,
            "rr": [t["pct"] / stop_pct for t in targets], "walls_up": walls_up[:2], "walls_dn": walls_dn[:2],
            "ceiling": ceiling, "flow_usd": flow_usd, "has_book": bool(asks)}


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

    def sector(self, key: str, turnover: float, observe: bool = True, x: bool = False):
        if turnover is None or turnover <= 0:
            return None
        return self._obs("xsector" if x else "sector", key, turnover, observe)

    def coin(self, coin, observe: bool = True):
        if coin.live and coin.xvol > 0 and coin.mcap > 0:  # حجم البورصة اللحظي: يتجدد كل تشغيلة
            return self._obs("xcoin", coin.id, coin.xvol / coin.mcap, True)
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
    "sector": 0.16, "breadth": 0.06, "gap": 0.14, "corr": 0.08, "vol": 0.11,
    "attention": 0.08, "stable": 0.07, "whale": 0.13, "dex": 0.05, "momo": 0.03, "context": 0.03, "rotation": 0.06,
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
        for k in FEATURES:  # ترحيل نماذج محفوظة من إصدارات أقدم
            self.m["theta"].setdefault(k, 6.0 * PRIOR[k])
            self.m["theta0"].setdefault(k, 6.0 * PRIOR[k])

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
# notifier
# ======================================================================
KIND_LABEL = {
    "whale": "🐋 تجميع حيتان",
    "bridge": "🌉 تدفق سيولة للشبكة",
    "waterfall": "🌊 شلال سيولة بيئي",
    "sector": "🔁 تدوير قطاعي",
}
KIND_ORDER = ["whale", "bridge", "waterfall", "sector"]
NUM = ["①", "②", "③", "④", "⑤"]
LINE = "━━━━━━━━━━━━━━━━━━"


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


def fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:+.1f}%"


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


def bar(score: float) -> str:
    n = max(0, min(10, round(score / 10.0)))
    return "▰" * n + "▱" * (10 - n)


def confidence_label(features: dict) -> str:
    n = sum(1 for k in ("whale", "stable", "dex", "attention", "vol", "context") if (features.get(k) or 0) >= 0.5)
    return "عالية" if n >= 3 else ("متوسطة" if n >= 2 else "منخفضة")


def format_signal(cand, score: float, plan: dict, tz: str, extra: Optional[dict] = None) -> str:
    extra = extra or {}
    c = cand.coin
    e = html.escape
    kinds = " · ".join(KIND_LABEL[k] for k in KIND_ORDER if k in cand.kinds) or KIND_LABEL["sector"]
    reasons = [t for _, t in sorted(cand.reasons, key=lambda r: r[0])][:5]
    L = [f"🟢 <b>إشارة شراء</b> · <b>#{e(c.symbol)}</b>  <i>{e(c.name)}</i>", LINE,
         f"🏷 <b>النوع:</b> {kinds}",
         f"🧭 <b>القطاع:</b> {e(' + '.join(cand.sectors[:2]) or '—')}",
         f"🔥 <b>القوة:</b> {bar(score)} <b>{score:.0f}/100</b>  ·  الثقة: <b>{confidence_label(cand.features)}</b>",
         "", "📌 <b>لماذا الآن؟</b>"]
    L += [f"{NUM[i]} {e(r)}" for i, r in enumerate(reasons)]
    dex = cand.meta.get("dex") or {}
    xr = extra.get("xratio")
    mk = [f"• السعر: <code>${fmt_price(c.price)}</code>  (1س {fmt_pct(c.ch1h)} · 24س {fmt_pct(c.ch24h)} · 7أ {fmt_pct(c.ch7d)})"]
    vol = c.xvol or c.volume
    mk.append(f"• القيمة السوقية: {fmt_usd(c.mcap) if c.mcap else '—'} · حجم 24س: {fmt_usd(vol)}"
              + (f" (×{xr:.1f} فوق المعتاد)" if xr else ""))
    if dex:
        tx = dex["buys_h1"] + dex["sells_h1"]
        mk.append(f"• سيولة DEX: {fmt_usd(dex['liq'])}" + (f" · ضغط الشراء {dex['buys_h1'] / tx * 100:.0f}% (1س)" if tx else ""))
    L += ["", "📊 <b>السوق</b>"] + mk
    L += ["", "🎯 <b>الأهداف</b>  <i>(مبنية على المقاومات والسيولة)</i>"]
    for t in plan["targets"]:
        L.append(f"T{t['k']}  <code>${fmt_price(t['price'])}</code>  <b>{t['pct']:+.1f}%</b>  ← {e(t['basis'])}")
    L.append(f"🛑 <b>الوقف:</b> <code>${fmt_price(plan['stop'])}</code>  <b>−{plan['stop_pct']:.1f}%</b>  ← {e(plan['stop_basis'])}")
    L.append("⚖️ عائد/مخاطرة: " + " · ".join(f"T{t['k']} 1:{r:.1f}" for t, r in zip(plan["targets"], plan["rr"])))
    walls = []
    if plan["walls_up"]:
        w = plan["walls_up"][0]
        walls.append(f"🧱 جدار بيع ${fmt_price(w['price'])} (~{fmt_usd(w['usd'])}، {w['dist_pct']:+.1f}%)")
    if plan["walls_dn"]:
        w = plan["walls_dn"][0]
        walls.append(f"🛡 جدار شراء ${fmt_price(w['price'])} (~{fmt_usd(w['usd'])})")
    if walls:
        L.append(" · ".join(walls))
    if plan["flow_usd"]:
        L.append(f"💧 تدفق شراء مقدّر: {fmt_usd(plan['flow_usd'])}" + ("" if plan["has_book"] else " (بدون دفتر أوامر)"))
    hist = extra.get("history") or []
    if hist:
        L += ["", "📚 <b>تاريخياً</b>"] + [f"• {e(h)}" for h in hist[:3]]
    news = extra.get("news") or []
    if news:
        L += ["", "📰 <b>أخبار</b>"] + [f"• {e(n[:110])}" for n in news[:2]]
    if extra.get("llm"):
        L += ["", f"🤖 <b>المراجعة الذكية:</b> {e(extra['llm'])}"]
    foot = f"⏱ {now_label(tz)}"
    link = safe_url(cand.meta.get("url"))
    if link:
        foot += f" · <a href=\"{e(link, quote=True)}\">الرسم والسيولة</a>"
    L += ["", foot, "<i>⚠️ تقديرات إحصائية وليست نصيحة مالية — التزم بالوقف وأدر حجم المخاطرة.</i>"]
    return safe_join(L, 3900)


def safe_join(lines: list, limit: int) -> str:
    """يقصّ الرسالة عند حدود الأسطر فقط، حتى لا ينقطع وسم HTML فيرفضها تيليجرام."""
    out, size = [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out)


def safe_url(u) -> str:
    u = str(u or "")
    return u if u.startswith("https://") and len(u) < 400 else ""


def format_target_hit(pos: dict, t: dict, ret: float, stop_cur: float) -> str:
    return (f"🎯 <b>#{html.escape(pos['symbol'])}</b> حقق <b>T{t['k']}</b>  ({ret:+.1f}% من الدخول)\n"
            f"السعر الآن: <code>${fmt_price(pos['last'])}</code>\n"
            f"🛡 الوقف المتحرك: <code>${fmt_price(stop_cur)}</code>"
            + ("\n💡 فكّر بجني جزء من الربح وترك الباقي مع الوقف المتحرك." if t["k"] == 1 else ""))


def format_close(pos: dict, ret: float, reason: str, hours: float) -> str:
    icon = "✅" if pos["y"] == 1 else "❌"
    why = {"target": "تحققت كل الأهداف", "trail": "أُغلقت بالوقف المتحرك بربح", "stop": "ضُرب الوقف",
           "expired": "انتهت المهلة"}.get(reason, reason)
    return (f"{icon} <b>#{html.escape(pos['symbol'])}</b> — {why}\n"
            f"النتيجة: <b>{ret:+.1f}%</b> خلال {hours:.1f} ساعة · أعلى هدف: T{pos.get('hit', 0) or 0}"
            f" · دخول <code>${fmt_price(pos['entry'])}</code>")


def format_warning(sym: str, kind: str, lines: list, has_pos: bool, tz: str) -> str:
    title = {"dump": "تحذير تصريف محتمل", "climax": "ذروة صعود وانعكاس", "momentum": "ضعف الزخم",
             "cooling": "تبريد القطاع", "outflow": "خروج سيولة من الشبكة"}.get(kind, "تحذير")
    L = [f"🔴 <b>{title}</b> · <b>#{html.escape(sym)}</b>", LINE]
    L += [f"• {html.escape(x)}" for x in lines[:4]]
    L.append("")
    L.append("📍 <b>لديك صفقة مفتوحة عليها:</b> فكّر بجني الربح أو رفع الوقف." if has_pos
             else "👀 للمراقبة: تجنّب الدخول الجديد حتى يهدأ التصريف.")
    L.append(f"⏱ {now_label(tz)}")
    return "\n".join(L)


class Telegram:
    def __init__(self, http, token: str, chat_id: str):
        self.http = http
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
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
def sector_analyze(sector_data: dict, cfg, att, fresh=None, know=None) -> tuple:
    """يعيد (stats لكل قطاع، قائمة مرشحين)."""
    sc = cfg.get("sector")
    stats: dict = {}
    cands: list = []
    for key, coins in sector_data.items():
        label = cfg.sectors[key].get("label", key)
        summ = group_summary(coins)
        prof = correlation_profile(coins)
        live = [c for c in coins if c.live and c.xvol > 0]
        if coins and len(live) / len(coins) >= 0.6:  # حجم البورصة اللحظي يتجدد كل تشغيلة
            xt = sum(c.xvol for c in live) / max(sum(c.mcap for c in live), 1.0)
            att_ratio = att.sector(f"sector:{key}", xt, observe=True, x=True)
        else:
            att_ratio = att.sector(f"sector:{key}", summ["turnover"], observe=(fresh is None or key in fresh))
        idx6, idx24 = prof["idx_6h"], prof["idx_24h"]
        ref = max(summ["median_24h"], idx24 if idx24 is not None else summ["median_24h"])
        heat = max(ref, 2.0 * idx6) if idx6 is not None else ref
        hot = heat >= sc["hot_heat"] and summ["breadth"] >= sc["min_breadth"]
        stats[key] = {"label": label, "heat": heat, "hot": hot, "att_ratio": att_ratio,
                      "avg_corr": prof["avg_corr"], "idx_6h": idx6, **summ}
        if hot and know is not None and know.followers(key):
            f0 = know.followers(key)[0]
            stats[key]["next"] = f0
            log.info("قطاع %s ساخن؛ تاريخياً تتبعه سيولة %s بعد ~%sس", key, f0["to"], f0["lag_h"])
        if not hot:
            continue
        laggards = find_laggards(coins, prof["per_coin"], ref, sc["laggard_gap"], sc["laggard_max_24h"],
                                   sc["min_corr"], sc["max_7d_dump"])
        for lg in laggards[:8]:
            coin = lg["coin"]
            cand = Candidate(coin=coin, kinds={"sector"}, sectors=[label], src={"src": "cg", "id": coin.id})
            cand.meta = {}
            rel = know.sector_rel(coin.symbol, key) if know is not None else None
            corr = lg["corr"] if not rel else 0.5 * lg["corr"] + 0.5 * max(rel["corr"], 0.0)  # مزج الحي بالتاريخي
            cand.features = {
                "sector": clip(heat / 12.0),
                "breadth": clip(summ["breadth"]),
                "gap": clip(lg["gap"] / 15.0),
                "corr": clip(corr),
                "momo": clip(max(coin.ch1h or 0.0, 0.0) / 3.0),
            }
            cand.reasons = [
                (3, f"🔥 قطاع {label} ساخن {heat:+.1f}% (اتساع {summ['breadth'] * 100:.0f}%) "
                    f"والعملة متأخرة {coin.ch24h:+.1f}%"),
                (7, f"🔗 ارتباط {lg['corr']:.2f} مع السلة ← احتمال لحاق"),
            ]
            if rel and rel.get("lag_h", 0) >= 1 and rel.get("lag_corr", 0) >= 0.2:
                cand.reasons.append((8, f"تاريخياً تلحق {label.split('·')[0].strip()} بعد ~{rel['lag_h']}س (ارتباط {rel['lag_corr']:.2f})"))
                cand.meta["history"] = [f"تلحق قطاعها عادةً بعد ~{rel['lag_h']} ساعة (ارتباط تاريخي {rel['lag_corr']:.2f})"]
            if att_ratio is not None and att_ratio >= 1.5:
                cand.reasons.append((4, f"📡 زخم انتباه القطاع {(att_ratio - 1) * 100:+.0f}% فوق المعتاد"))
            cand.meta = {"att_ratio": att_ratio, "daily_vol": lg["vol"], "sector_key": key, "gap_pct": lg["gap"],
                         "heat": heat, **({"history": cand.meta["history"]} if "history" in cand.meta else {})}
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
        nid = eco.get("native_id") or eco.get("native")
        native = natives.get(nid) if nid else None
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


def waterfall_scan(trig: dict, coins: list, cfg, vr, know=None) -> list:
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
                     "eco_key": trig["key"], "gap_pct": gap if mode == "lag" else None, "heat": max(ref, 0.0)}
        er = know.eco(trig["key"]) if know is not None else None
        if er and er.get("events", 0) >= 3:
            cand.meta["history"] = [f"بعد اختراق عملة {label} الأم: متوسط حركة نظامها +{er['mean_fwd24_pct']:.1f}% خلال 24س "
                                    f"(قمته غالباً بعد ~{er['peak_h']}س، {er['events']} حالات)"]
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
    def __init__(self, cfg, xfer, sol, dex, state_data: dict, extra_wallets=None):
        self.cfg = cfg
        self.w = cfg.get("whales")
        self.xfer = xfer
        self.sol = sol
        self.dex = dex
        self.state = state_data
        self._warned_chains: set = set()
        wl = cfg.whales or {}
        self.exchanges = {str(a).lower() for a in (wl.get("exchanges") or {}).values()}
        raw = [x for x in (wl.get("wallets") or []) if isinstance(x, dict) and x.get("address")] + list(extra_wallets or [])
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
        return bool(self.xfer and self.exchanges and self.w.get("token_flow_check", True))

    # ------------------------------------------------------------------ المحافظ
    def scan_wallets(self, now: float) -> list:
        events: list = []
        for wal in self.wallets[: int(self.w["max_wallets"])]:
            try:
                chain = str(wal.get("chain", "")).lower()
                if chain == "solana" and self.sol:
                    events += self._scan_sol(wal)
                elif chain in EVM_CHAIN_IDS and self.xfer and self.xfer.supports(chain):
                    events += self._scan_evm(wal, chain, now)
                elif chain in EVM_CHAIN_IDS and chain not in self._warned_chains:
                    self._warned_chains.add(chain)
                    log.warning("شبكة %s غير مدعومة مجاناً: تم تجاهل محافظها", chain)
            except Exception as exc:  # محفظة واحدة لا يجب أن تُسقط التشغيلة
                log.warning("wallet scan failed (%s): %s", wal.get("label"), exc)
        return events

    def _scan_evm(self, wal: dict, chain: str, now: float) -> list:
        addr = str(wal["address"]).lower()
        rec = self.state["wallets"]["evm"].setdefault(
            f"{chain}:{addr}", {"last_ts": now - self.w["first_run_lookback_min"] * 60})
        txs = self.xfer.wallet(chain, addr)
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
        if not self.can_check_flows or not self.xfer.supports(chain):
            return None
        txs = self.xfer.contract(chain, contract)
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
# rotation
# ======================================================================
STAGE_LABEL = {
    "quiet": "هادئ", "leaders": "① قيادة (الكبار يتحركون أولاً)", "broadening": "② اتساع (السيولة تنزل للمتوسطة)",
    "late": "③ متأخرة (الصغار تلحق والكبار تتعب)", "cooling": "④ تبريد (السيولة تخرج)", "running": "جارٍ",
}


def _med(vals: list) -> Optional[float]:
    v = [x for x in vals if x is not None]
    return float(np.median(v)) if v else None


def tercile_stats(coins: list) -> dict:
    """ثلاثة مستويات حسب القيمة السوقية: كبار/متوسطة/صغار. يكشف أين تقف السيولة داخل القطاع."""
    cs = sorted((c for c in coins if c.mcap > 0), key=lambda c: -c.mcap)
    n = len(cs)
    if n < 6:
        return {}
    k = n // 3
    parts = {"large": cs[:k], "mid": cs[k:n - k], "small": cs[n - k:]}
    out = {}
    for name, g in parts.items():
        out[f"{name}24"] = _med([c.ch24h for c in g])
        out[f"{name}1"] = _med([c.ch1h for c in g])
    return out


def wave_stage(st: dict, hist: list) -> str:
    """مرحلة موجة القطاع: leaders → broadening → late → cooling (تدفق السيولة من الكبار إلى الصغار ثم خروجها)."""
    heat, br = st["heat"], st["breadth"]
    peak = max([h[1] for h in hist[-24:]] + [heat])
    if peak >= 5.0 and heat < 0.5 * peak and br < 0.5:
        return "cooling"
    if heat < 2.0 and peak < 4.0:
        return "quiet"
    L, M, S = st.get("large24"), st.get("mid24"), st.get("small24")
    if None in (L, M, S):
        return "running" if heat >= 3.0 and br >= 0.55 else "quiet"
    if L >= 3.0 and M < 2.0 and S < 2.0:
        return "leaders"
    if L >= 2.0 and M >= 2.0 and S < M - 1.0:
        return "broadening"
    l1 = st.get("large1")
    if S >= 3.0 and br >= 0.65 and l1 is not None and l1 < 0:
        return "late"
    return "running" if heat >= 3.0 and br >= 0.55 else "quiet"


def rotation_annotate(stats: dict, sector_data: dict, state_data: dict, now: float) -> None:
    """يضيف المراحل ويحدّث تاريخ القطاعات (لقطة كل ~30 دقيقة، آخر 36 ساعة)."""
    sh = state_data.setdefault("sh", {})
    for key, st in stats.items():
        st.update(tercile_stats(sector_data.get(key, [])))
        hist = sh.setdefault(key, [])
        st["stage"] = wave_stage(st, hist)
        if not hist or now - hist[-1][0] >= 1500:
            hist.append([now, round(st["heat"], 2), round(st["breadth"], 3), st.get("large24"), st.get("mid24"),
                         st.get("small24")])
            sh[key] = hist[-72:]
        st["hot_since_h"] = None
        for h in hist:  # منذ متى القطاع ساخن؟
            if h[1] >= 4.0:
                st["hot_since_h"] = (now - h[0]) / 3600.0
                break
    for k in [k for k in sh if k not in stats and not sh[k]]:
        del sh[k]


def rotation_forecast(stats: dict, know, cfg, now: float) -> list:
    """يتوقع القطاعات التي ستصلها السيولة قبل أن تتحرك، من قطاعات في مرحلة اتساع/قيادة/تأخر.

    المصدر الأول: ترتيب الانتقال التاريخي المستخرج من الاختبار الرجعي (knowledge.json).
    المصدر الثاني: ترتيب افتراضي عام من الإعدادات إن لم تتوفر معرفة تاريخية للقطاع.
    """
    rc = cfg.get("rotation")
    if not rc["enabled"]:
        return []
    hot_heat = float(cfg.get("sector.hot_heat"))
    prior = {}
    for a, b in rc.get("prior_pairs") or []:
        prior.setdefault(a, []).append({"to": b, "lag_h": rc["prior_lag_h"], "corr": rc["prior_corr"], "src": "prior"})
    out: dict = {}
    for a, sa in stats.items():
        if sa.get("stage") not in ("leaders", "broadening", "late", "running") or sa["heat"] < rc["min_source_heat"]:
            continue
        links = [dict(f, src="history") for f in know.followers(a)] or prior.get(a, [])
        for lk in links:
            b = lk["to"]
            sb = stats.get(b)
            if not sb or sb.get("stage") not in ("quiet",) or sb["heat"] >= hot_heat:
                continue
            lag = float(lk["lag_h"])
            since = sa.get("hot_since_h")
            in_window = since is None or (rc["window_low"] * lag <= since <= rc["window_high"] * lag + 6.0)
            stage_w = {"leaders": 0.6, "broadening": 1.0, "late": 0.9, "running": 0.7}[sa["stage"]]
            conf = clip(sa["heat"] / 10.0) * clip(lk["corr"] / 0.35) * stage_w * (1.0 if in_window else 0.55)
            if lk["src"] == "prior":
                conf *= 0.6  # افتراض عام أضعف من معرفة تاريخية
            cur = out.get(b)
            if cur is None or conf > cur["conf"]:
                out[b] = {"to": b, "from": a, "lag_h": lag, "corr": lk["corr"], "src": lk["src"], "conf": conf,
                          "stage": sa["stage"], "from_heat": sa["heat"], "since_h": since, "in_window": in_window}
    rows = sorted(out.values(), key=lambda r: -r["conf"])
    return [r for r in rows if r["conf"] >= rc["min_conf"]]


def rotation_early(coins: list, sector_stats: dict, vr, cfg, limit: int) -> list:
    """عملات القطاع المتوقع وصول السيولة إليه والتي بدأت تستيقظ قبل الباقي (حجم أعلى، قوة نسبية، لم ترتفع بعد)."""
    cap = float(cfg.get("sector.laggard_max_24h"))
    med = sector_stats.get("median_24h", 0.0)
    rows = []
    for c in coins:
        if c.ch24h is None or c.ch24h > cap or (c.ch1h is not None and c.ch1h < -1.5):
            continue
        r = vr(c)
        rs = c.ch24h - med
        score = (0.0 if r is None else clip((r - 1.0) / 2.0)) * 0.5 + clip(rs / 4.0 + 0.5) * 0.3 \
            + clip(max(c.ch1h or 0.0, 0.0) / 2.0) * 0.2
        rows.append((score, c, r))
    rows.sort(key=lambda x: -x[0])
    return rows[:limit]


def format_rotation(fc: dict, label_from: str, label_to: str, picks: list, tz: str) -> str:
    e = html.escape
    src = "تاريخي من الاختبار الرجعي" if fc["src"] == "history" else "افتراض عام (شغّل backtest لدقة أعلى)"
    lo, hi = max(1.0, fc["lag_h"] * 0.5), fc["lag_h"] * 2.0
    L = ["🔭 <b>توقّع انتقال السيولة</b>", LINE,
         f"من: <b>{e(label_from)}</b>  ({STAGE_LABEL.get(fc['stage'], fc['stage'])} · {fc['from_heat']:+.1f}%)",
         f"إلى: <b>{e(label_to)}</b>  (ما زال هادئاً)",
         f"⏳ النافذة المتوقعة: بعد ~{lo:.0f}–{hi:.0f} ساعة  ·  الثقة: <b>{fc['conf'] * 100:.0f}%</b>",
         f"📚 المصدر: {src} (ارتباط {fc['corr']:.2f})"]
    if fc.get("since_h") is not None:
        L.append(f"🕒 القطاع المصدر ساخن منذ ~{fc['since_h']:.0f} ساعة" + ("" if fc["in_window"] else " (خارج النافذة المعتادة)"))
    if picks:
        L += ["", "👀 <b>عملات للمراقبة المبكرة:</b>"]
        for c, r in picks:
            L.append(f"• <b>#{e(c.symbol)}</b>  1س {fmt_pct(c.ch1h)} · 24س {fmt_pct(c.ch24h)}" + (f" · حجم ×{r:.1f}" if r else ""))
    L += ["", f"⏱ {now_label(tz)}", "<i>توقّع احتمالي مبني على أنماط تاريخية، وليس إشارة شراء. انتظر تأكيد الحجم والسيولة.</i>"]
    return safe_join(L, 3900)


# ======================================================================
# knowledge
# ======================================================================
KNOWLEDGE_PATH = ROOT / "knowledge.json"


class Knowledge:
    """معرفة محفوظة من الاختبار الرجعي: ارتباط العملات بقطاعاتها وتأخرها الزمني، ترتيب انتقال السيولة بين
    القطاعات، استجابة الأنظمة البيئية لاختراق عملتها الأم، وإحصاءات الصعود التاريخية."""

    def __init__(self, data: Optional[dict] = None):
        self.d = data or {}

    @classmethod
    def load(cls, path=KNOWLEDGE_PATH) -> "Knowledge":
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return cls({})

    @property
    def ready(self) -> bool:
        return bool(self.d.get("relations"))

    def sector_rel(self, symbol: str, sector_key: str) -> Optional[dict]:
        return ((self.d.get("relations") or {}).get(symbol) or {}).get("sectors", {}).get(sector_key)

    def followers(self, sector_key: str) -> list:
        return (self.d.get("sector_flow") or {}).get(sector_key) or []

    def eco(self, key: str) -> Optional[dict]:
        return (self.d.get("eco_response") or {}).get(key)

    def mfe(self) -> Optional[dict]:
        return (self.d.get("backtest") or {}).get("mfe_q")

    def tuned(self) -> Optional[dict]:
        return (self.d.get("backtest") or {}).get("tuned")


# ======================================================================= الاختبار الرجعي
def _nancorr(a, b, min_n: int = 60) -> float:
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < min_n:
        return 0.0
    x, y = a[m], b[m]
    if float(np.std(x)) < 1e-12 or float(np.std(y)) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _align(series: dict):
    """يحاذي كل السلاسل على محور زمني واحد. يعيد (syms, C, H, L, Q)."""
    if not series:
        return [], None, None, None, None
    ref = max(series.values(), key=len)
    ts = [r[0] for r in ref]
    pos = {t: i for i, t in enumerate(ts)}
    syms = sorted(series)
    shape = (len(syms), len(ts))
    C, H, L, Q = (np.full(shape, np.nan) for _ in range(4))
    for si, s in enumerate(syms):
        for r in series[s]:
            j = pos.get(r[0])
            if j is not None:
                H[si, j], L[si, j], C[si, j], Q[si, j] = r[2], r[3], r[4], r[5]
    return syms, C, H, L, Q


def _simulate_group(Cm, Hm, Lm, syms, bt, rr: float, smin: float, smax: float) -> list:
    """محاكاة قاعدة تدوير القطاعات على بيانات تاريخية بأضعف العتبات؛ تُفلتر لاحقاً بالعتبات المختلفة."""
    events = []
    k, T = Cm.shape
    warm, win, hz = 96, 72, int(bt["horizon_hours"])
    last_t = {}
    for t in range(warm, T - 2):
        w = Cm[:, t - win:t + 1]
        valid = np.all(np.isfinite(w), axis=1)
        if valid.sum() < 6:
            continue
        ch24 = (Cm[:, t] / Cm[:, t - 24] - 1.0) * 100.0
        v24 = ch24[valid]
        med, breadth = float(np.median(v24)), float(np.mean(v24 > 0))
        rets = np.diff(np.log(w[valid]), axis=1)
        idx = rets.mean(axis=0)
        idx24 = (math.expm1(float(idx[-24:].sum()))) * 100.0
        idx6 = (math.expm1(float(idx[-6:].sum()))) * 100.0
        ref = max(med, idx24)
        heat = max(ref, 2.0 * idx6)
        if heat < 2.5 or breadth < 0.5:
            continue
        ids = np.where(valid)[0]
        m = len(ids)
        tot = rets.sum(axis=0)
        for j, i in enumerate(ids):
            gap = ref - float(ch24[i])
            if gap < 2.5 or ch24[i] > 7.0:
                continue
            ch1 = (Cm[i, t] / Cm[i, t - 1] - 1.0) * 100.0
            if ch1 < -2.0 or t - last_t.get(i, -999) < 12:
                continue
            loo = (tot - rets[j]) / (m - 1)
            if float(np.std(loo)) < 1e-12 or float(np.std(rets[j])) < 1e-12:
                continue
            corr = float(np.corrcoef(rets[j], loo)[0, 1])
            if not corr >= 0.3:
                continue
            fh, fl = Hm[i, t + 1:t + 1 + hz], Lm[i, t + 1:t + 1 + hz]
            if len(fh) < 12 or not np.all(np.isfinite(fh)) or not np.all(np.isfinite(fl)):
                continue
            entry = float(Cm[i, t])
            dv = float(np.std(rets[j]) * math.sqrt(24) * 100.0)
            stop = min(max(1.2 * dv, smin), smax)
            tgt = stop * rr
            R, win_flag, ret = None, 0, 0.0
            for a in range(len(fh)):
                if fl[a] <= entry * (1 - stop / 100.0):
                    R, win_flag, ret = -1.0, 0, -stop
                    break
                if fh[a] >= entry * (1 + tgt / 100.0):
                    R, win_flag, ret = rr, 1, tgt
                    break
            if R is None:
                end = Cm[i, min(t + hz, T - 1)]
                ret = (float(end) / entry - 1.0) * 100.0 if np.isfinite(end) else 0.0
                R = ret / stop
                win_flag = 1 if ret >= 0.4 * tgt else 0
            last_t[i] = t
            events.append({"sym": syms[i], "t": int(t), "heat": round(heat, 2), "breadth": round(breadth, 3),
                           "gap": round(gap, 2), "corr": round(corr, 3), "R": round(R, 3), "win": win_flag,
                           "mfe": round((float(np.max(fh)) / entry - 1.0) * 100.0, 2)})
    return events


def _tune(events: list, min_trades: int) -> dict:
    best, grid = None, []
    for hh in (3.0, 4.0, 5.0, 6.0, 8.0):
        for gg in (3.0, 4.0, 6.0, 8.0):
            sel = [e for e in events if e["heat"] >= hh and e["gap"] >= gg]
            if len(sel) < min_trades:
                continue
            exp = float(np.mean([e["R"] for e in sel]))
            row = {"hot_heat": hh, "laggard_gap": gg, "n": len(sel), "win_rate": round(float(np.mean([e["win"] for e in sel])), 3),
                   "expectancy_R": round(exp, 3)}
            grid.append(row)
            if best is None or exp > best["expectancy_R"]:
                best = row
    grid.sort(key=lambda r: -r["expectancy_R"])
    return {"best": best, "top": grid[:5]}


def run_backtest(cfg, cg, ex, say=print) -> dict:
    """يبني المعرفة: علاقات العملات، انتقال السيولة بين القطاعات، استجابة الأنظمة، ومحاكاة قاعدة التدوير."""
    bt = cfg.get("backtest")
    u = cfg.get("universe")
    rr, smin, smax = float(cfg.get("risk.rr")), float(cfg.get("risk.stop_min")), float(cfg.get("risk.stop_max"))
    groups: dict = {}
    natives: dict = {}
    for kind, items in (("sector", cfg.sectors), ("eco", cfg.ecosystems)):
        for key, it in items.items():
            coins = [c for c in cg.markets(category=it.get("category"), per_page=60, sparkline=False)
                     if c.mcap >= u["min_market_cap"] and not is_stable(c)]
            coins.sort(key=lambda c: c.volume, reverse=True)
            coins = coins[: int(bt["top_per_group"])]
            if len(coins) >= 5:
                groups[f"{kind}:{key}"] = [c.symbol for c in coins]
            say(f"  {kind}:{key:12s} {len(coins)} عملة")
    nat_ids = [e["native"] for e in cfg.ecosystems.values() if e.get("native")]
    for c in cg.markets(ids=nat_ids, per_page=max(1, len(nat_ids)), sparkline=False) if nat_ids else []:
        natives[c.id] = c.symbol
    need = sorted({s for g in groups.values() for s in g} | set(natives.values()) | {"BTC"})
    say(f"جلب شموع {len(need)} عملة من البورصة…")
    series = {}
    for s in need:
        rows = ex.klines(s, limit=int(bt["candles"]), pages=int(bt["pages"]))
        if len(rows) >= 200:
            series[s] = rows
    syms, C, H, L, Q = _align(series)
    if C is None or len(syms) < 20:
        say("❌ بيانات تاريخية غير كافية (تحقق من وصول البورصات: python radar.py check)")
        return {}
    with np.errstate(all="ignore"):
        R = np.diff(np.log(C), axis=1)
    sidx = {s: i for i, s in enumerate(syms)}
    T = C.shape[1]
    say(f"تمت المحاذاة: {len(syms)} عملة × {T} ساعة (~{T / 24:.0f} يوماً)")

    relations: dict = {}
    baskets: dict = {}
    for gkey, gs in groups.items():
        kind, key = gkey.split(":", 1)
        ids = [sidx[s] for s in gs if s in sidx]
        if len(ids) < 5:
            continue
        Rm = R[ids]
        fin = np.isfinite(Rm)
        S = np.nansum(Rm, axis=0)
        N = fin.sum(axis=0)
        baskets[gkey] = np.where(N > 0, S / np.maximum(N, 1), np.nan)
        for j, i in enumerate(ids):
            own = Rm[j]
            loo = (S - np.nan_to_num(own)) / np.maximum(N - np.isfinite(own), 1)
            c0 = _nancorr(own, loo)
            m = np.isfinite(own) & np.isfinite(loo)
            beta = float(np.cov(own[m], loo[m])[0, 1] / np.var(loo[m], ddof=1)) if m.sum() > 60 and np.var(loo[m]) > 0 else 0.0
            best_k, best_c = 0, c0
            for k in range(1, 13):
                ck = _nancorr(own[k:], loo[:-k])
                if ck > best_c + 0.01:
                    best_k, best_c = k, ck
            rec = relations.setdefault(syms[i], {})
            slot = rec.setdefault("sectors" if kind == "sector" else "eco", {})
            slot[key] = {"corr": round(c0, 3), "beta": round(beta, 2), "lag_h": best_k, "lag_corr": round(best_c, 3)}
    # انتقال السيولة بين القطاعات (ارتباط متأخر على مجاميع 3 ساعات)
    sector_flow: dict = {}
    smooth = {k: np.convolve(np.nan_to_num(v), np.ones(3), mode="same") for k, v in baskets.items() if k.startswith("sector:")}
    for a, va in smooth.items():
        rows = []
        for b, vb in smooth.items():
            if a == b:
                continue
            c0 = _nancorr(vb, va)
            bk, bc = 0, c0
            for k in range(1, 25):
                ck = _nancorr(vb[k:], va[:-k])
                if ck > bc:
                    bk, bc = k, ck
            if bk >= 1 and bc >= 0.15:
                rows.append({"to": b.split(":", 1)[1], "lag_h": bk, "corr": round(bc, 3)})
        rows.sort(key=lambda r: -r["corr"])
        sector_flow[a.split(":", 1)[1]] = rows[:3]
    # استجابة الأنظمة البيئية لاختراق العملة الأم
    eco_resp: dict = {}
    thr = float(cfg.get("waterfall.native_24h"))
    for key, eco in cfg.ecosystems.items():
        nsym = natives.get(eco.get("native"))
        gs = [sidx[s] for s in groups.get(f"eco:{key}", []) if s in sidx and s != nsym]
        if not nsym or nsym not in sidx or len(gs) < 5:
            continue
        n = sidx[nsym]
        fwd24, peaks, last, base = [], [], -999, []
        for t in range(24, T - 49):
            rel = C[gs, t + 24] / C[gs, t] - 1.0
            if np.isfinite(rel).sum() >= 5:
                base.append(float(np.nanmedian(rel)))
            ret = C[n, t] / C[n, t - 24] - 1.0
            if not (ret * 100.0 >= thr) or t - last < 24:
                continue
            last = t
            curve = [float(np.nanmedian(C[gs, t + h] / C[gs, t] - 1.0)) for h in range(1, 49)]
            if np.all(np.isfinite(curve)):
                fwd24.append(curve[23])
                peaks.append(int(np.argmax(curve)) + 1)
        if fwd24:
            eco_resp[key] = {"events": len(fwd24), "mean_fwd24_pct": round(float(np.mean(fwd24)) * 100.0, 2),
                             "hit_rate": round(float(np.mean([x > 0 for x in fwd24])), 3),
                             "peak_h": int(np.median(peaks)),
                             "baseline_fwd24_pct": round(float(np.mean(base)) * 100.0, 2) if base else 0.0}
    # محاكاة قاعدة التدوير
    events = []
    for gkey, gs in groups.items():
        if not gkey.startswith("sector:"):
            continue
        ids = [sidx[s] for s in gs if s in sidx]
        if len(ids) >= 6:
            ev = _simulate_group(C[ids], H[ids], L[ids], [syms[i] for i in ids], bt, rr, smin, smax)
            for e in ev:
                e["sector"] = gkey.split(":", 1)[1]
            events += ev
    seen, uniq = set(), []
    for e in events:  # نفس العملة داخل نفس نافذة 12 ساعة تُحتسب مرة واحدة
        k = (e["sym"], e["t"] // 12)
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    tuned = _tune(uniq, int(bt["min_trades"]))
    result = {"n_events": len(uniq),
              "win_rate": round(float(np.mean([e["win"] for e in uniq])), 3) if uniq else None,
              "expectancy_R": round(float(np.mean([e["R"] for e in uniq])), 3) if uniq else None,
              "grid_top": tuned["top"], "tuned": None, "mfe_q": None}
    if tuned["best"] and tuned["best"]["expectancy_R"] > 0:
        result["tuned"] = {"hot_heat": tuned["best"]["hot_heat"], "laggard_gap": tuned["best"]["laggard_gap"]}
        sel = [e for e in uniq if e["heat"] >= tuned["best"]["hot_heat"] and e["gap"] >= tuned["best"]["laggard_gap"]]
        mf = [e["mfe"] for e in sel if e["mfe"] > 0]
        if len(mf) >= 10:
            result["mfe_q"] = {q: round(float(np.percentile(mf, p)), 1) for q, p in (("p50", 50), ("p75", 75), ("p90", 90))}
    by_sector: dict = {}
    for e in uniq:
        by_sector.setdefault(e["sector"], []).append(e)
    result["by_sector"] = {k: {"n": len(v), "win_rate": round(float(np.mean([x["win"] for x in v])), 3),
                               "expectancy_R": round(float(np.mean([x["R"] for x in v])), 3)}
                           for k, v in by_sector.items() if len(v) >= 5}
    know = {"version": 2, "generated": int(time.time()), "interval": "1h", "hours": int(T), "coins": len(syms),
            "relations": relations, "sector_flow": sector_flow, "eco_response": eco_resp, "backtest": result}
    return know


# ======================================================================
# engine
# ======================================================================
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

    def __init__(self, state_data: dict, cfg, now: float, monthly: Optional[float] = None):
        b = state_data.setdefault("cg", {"tokens": 30.0, "ts": None, "month": "", "used": 0})
        self.b = b
        self.monthly = float(monthly if monthly is not None else cfg.get("coingecko.monthly_budget"))
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


def estimate_paprika_usage(cfg) -> dict:
    month = 144 * 30 + 31 + 60  # لقطة كل تشغيلة + وسوم يومياً + هامش
    budget = float(cfg.get("paprika.monthly_budget"))
    return {"per_month": month, "budget": budget, "limit": 20000, "ok": month <= budget}


def estimate_cg_usage(cfg, live_ok: bool) -> dict:
    """حساب استهلاك CoinGecko الشهري المتوقع مقابل الحد المجاني (10,000)."""
    groups = len(cfg.sectors) + 1  # + العملات الأم
    minutes = float(cfg.get("coingecko.refresh_minutes_live" if live_ok else "coingecko.refresh_minutes_nolive"))
    refreshes_day = 1440.0 / max(minutes, 1.0)
    eco_day = min(len(cfg.ecosystems), int(cfg.get("waterfall.max_triggers"))) * refreshes_day * 0.15  # الأنظمة المفعَّلة فقط
    day = groups * refreshes_day + eco_day + 24.0 + 1.0  # + trending كل ساعة + قائمة الفئات
    month = day * 30.0
    budget = float(cfg.get("coingecko.monthly_budget"))
    return {"groups": groups, "per_day": round(day), "per_month": round(month), "budget": budget,
            "limit": 10000, "ok": month <= budget, "note": "الميزانية تضبط التوزيع تلقائياً؛ الفائض يعمل من الكاش"}


class Services:
    """حاوية المزوّدين (تسهّل الاختبار بمزوّدين وهميين)."""

    NAMES = ("cg", "llama", "dex", "ex", "xfer", "sol", "news", "judge", "notifier", "know", "eth")

    def __init__(self, **kw):
        for k in self.NAMES:
            setattr(self, k, kw.get(k))


class Discovery:
    """اكتشاف محافظ ذكية تلقائياً: أوائل المشترين الكبار لعملات نجحت إشاراتنا عليها؛ من يتكرر يُضاف للمراقبة."""

    def __init__(self, cfg, state_data: dict, eth):
        self.c = cfg.get("discovery")
        self.d = state_data.setdefault("discovery", {})
        self.eth = eth
        self.exchanges = {str(a).lower() for a in (cfg.whales.get("exchanges") or {}).values()}
        self.known = {str(w.get("address", "")).lower() for w in (cfg.whales.get("wallets") or [])}

    def on_win(self, pos: dict) -> int:
        ct = pos.get("contract")
        if not (self.c.get("enabled") and self.eth and ct and ct.get("chain") == "ethereum"):
            return 0
        t0 = int(pos["ts"])
        b0, b1 = self.eth.block_by_time(1, t0 - 3 * 3600), self.eth.block_by_time(1, t0)
        if not b0 or not b1:
            return 0
        txs = self.eth.token_transfers(1, contract=ct["addr"], offset=300, startblock=b0, endblock=b1, sort="asc")
        if not txs:
            return 0
        net: dict = {}
        for t in txs:
            try:
                amt = int(t["value"]) / (10 ** int(t.get("tokenDecimal") or 0))
            except (KeyError, TypeError, ValueError):
                continue
            frm, to = str(t.get("from", "")).lower(), str(t.get("to", "")).lower()
            net[to] = net.get(to, 0.0) + amt
            net[frm] = net.get(frm, 0.0) - amt
        skip = {"0x" + "0" * 40, ct["addr"].lower(), str(ct.get("pair", "")).lower()} | self.exchanges | self.known
        added = 0
        for addr, amt in net.items():
            if addr in skip or not addr.startswith("0x") or amt <= 0 or amt * pos["entry"] < self.c["min_usd"]:
                continue
            rec = self.d.setdefault(addr, {"hits": 0, "tokens": []})
            if pos["symbol"] not in rec["tokens"]:
                rec["tokens"].append(pos["symbol"])
                rec["hits"] += 1
                added += 1
        if len(self.d) > 400:  # حماية حجم الحالة
            for a in sorted(self.d, key=lambda x: self.d[x]["hits"])[: len(self.d) - 400]:
                del self.d[a]
        return added

    def wallets(self) -> list:
        good = [(a, r) for a, r in self.d.items() if r["hits"] >= int(self.c["min_hits"])]
        good.sort(key=lambda x: -x[1]["hits"])
        return [{"label": f"مكتشف ({'/'.join(r['tokens'][:2])})", "chain": "ethereum", "address": a,
                 "weight": float(self.c["weight"])} for a, r in good[: int(self.c["max_wallets"])]]


class Engine:
    def __init__(self, cfg, state, svc, now_fn=time.time, dry_run: bool = False):
        self.cfg, self.state, self.s = cfg, state, svc
        self.now_fn, self.dry_run = now_fn, dry_run
        self.K = svc.know or Knowledge({})
        self.summary: dict = {"sectors": {}, "signals": [], "near_miss": [], "closed": 0, "errors": [], "warnings": [],
                              "triggers": []}
        self._vr_cache: dict = {}
        self.fresh_ids: set = set()
        self.fresh_keys: set = set()
        self.coin_index: dict = {}
        self.tickers: dict = {}
        self.live_ok = False
        self._ob_calls = 0
        self.deadline = time.time() + 600.0
        self.members: dict = {}
        self.byid: dict = {}
        self.protocols: dict = {}
        self.news_items: list = []
        self.stats: dict = {}

    def _reset(self) -> None:
        """يعيد تهيئة الحالة المؤقتة (للاستخدام خارج run() مثل الفحص والاختبار الرجعي)."""
        self.__init__(self.cfg, self.state, self.s, self.now_fn, self.dry_run)

    # ------------------------------------------------------------------ أدوات مساعدة
    def _stage(self, name: str, fn, default=None):
        """ينفّذ مرحلة؛ أي خطأ يُسجَّل ويُكمل الرادار بباقي المراحل."""
        try:
            return fn()
        except Exception as exc:
            log.exception("مرحلة %s فشلت", name)
            self.summary["errors"].append(f"{name}: {exc.__class__.__name__}: {exc}")
            return default

    def vr(self, coin: Coin) -> Optional[float]:
        if coin.id not in self._vr_cache:
            self._vr_cache[coin.id] = self.att.coin(coin, observe=coin.id in self.fresh_ids)
        return self._vr_cache[coin.id]

    def _universe(self, coins: list) -> list:
        u = self.cfg.get("universe")
        return [c for c in coins
                if c.mcap >= u["min_market_cap"] and c.volume >= u["min_volume_24h"] and not is_stable(c)]

    def _send(self, text: str) -> bool:
        try:
            return bool(self.s.notifier.send(text))
        except Exception as exc:
            log.error("notifier crashed: %s", exc)
            return False

    def _refresh_s(self) -> float:
        k = "coingecko.refresh_minutes_live" if self.live_ok else "coingecko.refresh_minutes_nolive"
        return float(self.cfg.get(k)) * 60

    def _max_age_s(self) -> float:
        k = "coingecko.max_cache_age_hours_live" if self.live_ok else "coingecko.max_cache_age_hours_nolive"
        return float(self.cfg.get(k)) * 3600

    def _cg_ok(self, key: str, now: float) -> bool:
        failed = self.state.data.setdefault("cg_fail", {})
        return now - failed.get(key, 0) >= float(self.cfg.get("coingecko.retry_after_fail_minutes")) * 60

    def _cg_fail(self, key: str, now: float) -> None:
        self.state.data.setdefault("cg_fail", {})[key] = now

    # ------------------------------------------------------------------ بيانات السوق (كاش + ميزانية + طبقة لحظية)
    def _valid_categories(self, now: float):
        vc = self.state.data["valid_categories"]
        if (now - vc.get("ts", 0) > 86400 or not vc.get("ids")) and self._cg_ok("categories", now) and self.budget.take():
            ids = self.s.cg.category_ids()
            if ids:
                vc["ts"], vc["ids"] = now, ids
            else:
                self._cg_fail("categories", now)
        return set(vc["ids"]) if vc.get("ids") else None

    def _get_rows(self, ck: str, fetch, now: float) -> list:
        cache = self.state.data["mcache"]
        ent = cache.get(ck)
        if (ent is None or now - ent["ts"] >= self._refresh_s()) and self._cg_ok(ck, now) and self.budget.take():
            coins = fetch()
            if coins:
                cache[ck] = {"ts": now, "coins": [coin_to_row(c) for c in coins]}
                self.fresh_keys.add(ck)
                self.fresh_ids |= {c.id for c in coins}
                return coins
            self._cg_fail(ck, now)
            log.warning("فشل تحديث %s -> استخدام الكاش", ck)
        ent = cache.get(ck)
        if not ent or now - ent["ts"] > self._max_age_s():
            return []
        out = []
        for r in ent["coins"]:
            c = row_to_coin(r)
            if c:
                c.age_h = (now - ent["ts"]) / 3600.0
                out.append(c)
        return out

    def _overlay(self, coins: list, now: float, seen: set) -> None:
        """يحدّث السعر والتغير 24س وحجم البورصة لحظياً، ويحسب تغير الساعة من لقطاتنا الخاصة."""
        if not self.tickers:
            return
        tol0 = float(self.cfg.get("live.match_tolerance"))
        keep_s = float(self.cfg.get("live.snapshot_hours")) * 3600
        px = self.state.data.setdefault("px", {})
        for c in coins:
            tk = self.tickers.get(c.symbol)
            if not tk or c.price <= 0:
                continue
            if abs(tk["price"] / c.price - 1.0) > min(0.6, tol0 + 0.012 * c.age_h):
                continue  # رمز مشترك لعملة مختلفة
            hist = px.get(c.id) or []
            c1 = None
            if hist:
                cand = min(hist, key=lambda h: abs((now - h[0]) - 3600))
                if 2400 <= now - cand[0] <= 6000 and cand[1] > 0:
                    c1 = (tk["price"] / cand[1] - 1.0) * 100.0
            c.price, c.live, c.xvol = tk["price"], True, tk["qvol"]
            c.ch1h = c1 if c1 is not None else (c.ch1h if c.age_h < 0.5 else None)
            if tk["ch24"] is not None:
                c.ch24h = tk["ch24"]
            self.coin_index[c.id] = c
            if c.id not in seen:
                seen.add(c.id)
                px[c.id] = [h for h in hist if now - h[0] <= keep_s] + [[now, tk["price"]]]

    def _load_market(self, now: float, valid) -> tuple:
        per_page = int(self.cfg.get("universe.per_page"))
        native_ids = [e["native"] for e in self.cfg.ecosystems.values() if e.get("native")]
        natives: dict = {}
        if native_ids:
            natives = {c.id: c for c in self._get_rows(
                "natives", lambda: self.s.cg.markets(ids=native_ids, per_page=len(native_ids), sparkline=False), now)}
        cache = self.state.data["mcache"]
        keys = []
        for key, sc in self.cfg.sectors.items():
            if valid is not None and sc.get("category") not in valid:
                log.warning("فئة CoinGecko غير صالحة وتم تجاهلها: %s (%s)", key, sc.get("category"))
                continue
            keys.append(key)
        keys.sort(key=lambda k: cache.get(f"s:{k}", {}).get("ts", 0))  # الأقدم بيانات أولاً
        sector_data: dict = {}
        for key in keys:
            cat = self.cfg.sectors[key].get("category")
            coins = self._universe(self._get_rows(
                f"s:{key}", lambda cat=cat: self.s.cg.markets(category=cat, per_page=per_page), now))
            if len(coins) >= 6:
                sector_data[key] = coins
            else:
                log.info("قطاع %s: بيانات غير كافية (%d)", key, len(coins))
        seen: set = set()
        self._overlay(list(natives.values()), now, seen)
        for coins in sector_data.values():
            self._overlay(coins, now, seen)
        return sector_data, natives

    def _trending(self, now: float) -> set:
        t = self.state.data["trend"]
        if (now - t.get("ts", 0) >= float(self.cfg.get("coingecko.trending_refresh_minutes")) * 60
                and self._cg_ok("trending", now) and self.budget.take()):
            ids = self.s.cg.trending()
            if ids:
                t["ts"], t["ids"] = now, sorted(ids)
            else:
                self._cg_fail("trending", now)
        return set(t.get("ids") or [])

    def _bridge_flows(self, now: float) -> dict:
        tvl = self.s.llama.chains_tvl()
        stables = self.s.llama.stablecoin_supply()
        if not tvl and not stables:
            return {}
        return bridge_update(self.state.data, tvl, stables, now, self.cfg)

    def _protocols(self, now: float) -> dict:
        p = self.state.data["protocols"]
        if now - p.get("ts", 0) >= 7200 and self._cg_ok("protocols", now):
            m = self.s.llama.protocols()
            if m:
                p["ts"], p["map"] = now, m
            else:
                self._cg_fail("protocols", now)
        return p.get("map") or {}

    def _news(self, now: float) -> list:
        if not self.s.news:
            return []
        n = self.state.data["news"]
        if now - n.get("ts", 0) >= 1800 and self._cg_ok("news", now):
            items = self.s.news.fetch()
            if items:
                items.sort(key=lambda x: -x["ts"])
                n["ts"], n["items"] = now, items[:300]
            else:
                self._cg_fail("news", now)
        return n.get("items") or []

    # ------------------------------------------------------------------ وضع المصدر المجاني (بلا مفتاح)
    @property
    def bulk(self) -> bool:
        return bool(getattr(self.s.cg, "bulk", False))

    def _time_left(self) -> float:
        return self.deadline - time.time()

    def _snapshot(self, now: float) -> list:
        """كل العملات بطلب واحد؛ عند الفشل نستخدم آخر لقطة محفوظة (≤ ساعتين)."""
        sn = self.state.data.setdefault("snap", {"ts": 0, "rows": []})
        coins: list = []
        if self._cg_ok("snapshot", now) and self.budget.take():
            coins = self.s.cg.snapshot()
            if coins:
                top = sorted(coins, key=lambda c: -c.mcap)[:1500]
                sn["ts"], sn["rows"] = now, [coin_to_row(c) for c in top]
                self.fresh_ids |= {c.id for c in coins}
                return coins
            self._cg_fail("snapshot", now)
        if now - sn.get("ts", 0) <= 7200:
            out = []
            for r in sn.get("rows", []):
                c = row_to_coin(r)
                if c:
                    c.age_h = (now - sn["ts"]) / 3600.0
                    out.append(c)
            return out
        return []

    def _membership(self, snap: list, now: float) -> dict:
        """عضوية القطاعات والأنظمة من وسوم CoinPaprika + فئات/شبكات DefiLlama (تُحدَّث يومياً)."""
        m = self.state.data.setdefault("members", {"ts": 0, "sectors": {}, "eco": {}, "tags": {}})
        if now - m.get("ts", 0) < 86400 and m.get("sectors") or not self._cg_ok("members", now):
            return m
        tags = self.s.cg.tags() if self.budget.take() else []
        if not tags:
            self._cg_fail("members", now)
            return m
        best: dict = {}
        for c in snap:
            if c.symbol not in best or c.mcap > best[c.symbol].mcap:
                best[c.symbol] = c
        proto = self.protocols or {}
        fetched = 0

        def ids_for(keywords: list, llama_cats: list, chain: Optional[str]) -> tuple:
            nonlocal fetched
            ids, used = set(), []
            for t in tags:
                if keywords and tag_matches(keywords, t):
                    coins = t["coins"]
                    if coins is None and fetched < 40 and self.budget.take():
                        coins, fetched = self.s.cg.tag_coins(t["id"]), fetched + 1
                    ids |= set(coins or [])
                    used.append(t["id"])
            for k, info in proto.items():
                if not k.startswith("sym:"):
                    continue
                hit = (llama_cats and info.get("cat") in llama_cats) or (chain and chain in (info.get("chains") or []))
                if hit and k[4:] in best:
                    ids.add(best[k[4:]].id)
            return sorted(ids), used

        for key, sc in self.cfg.sectors.items():
            m["sectors"][key], m["tags"][key] = ids_for(sc.get("tags") or [], sc.get("llama") or [], None)
        for key, e in self.cfg.ecosystems.items():
            m["eco"][key], m["tags"]["eco:" + key] = ids_for(e.get("tags") or [], [], e.get("chain"))
        m["ts"] = now
        return m

    def _group_coins(self, ids: list, byid: dict) -> list:
        u = self.cfg.get("universe")
        coins = [byid[i] for i in ids if i in byid]
        coins = [c for c in coins if c.mcap >= u["min_market_cap"] and c.volume >= u["min_volume_24h"] and not is_stable(c)]
        coins.sort(key=lambda c: -c.volume)
        return coins[: int(u["per_page"])]

    def _load_market_bulk(self, now: float) -> tuple:
        snap = self._snapshot(now)
        if not snap:
            return {}, {}
        byid = {c.id: c for c in snap}
        best: dict = {}
        for c in snap:
            if c.symbol not in best or c.mcap > best[c.symbol].mcap:
                best[c.symbol] = c
        natives: dict = {}
        for e in self.cfg.ecosystems.values():
            c = best.get(str(e.get("symbol") or "").upper())
            if c:
                e["native_id"] = c.id
                natives[c.id] = c
        mem = self._membership(snap, now)
        self.members = mem
        sector_data: dict = {}
        for key in self.cfg.sectors:
            coins = self._group_coins(mem.get("sectors", {}).get(key, []), byid)
            if len(coins) >= 6:
                sector_data[key] = coins
        seen: set = set()
        self._overlay(list(natives.values()), now, seen)
        for coins in sector_data.values():
            self._overlay(coins, now, seen)
        self.byid = byid
        return sector_data, natives

    def _attach_sparks(self, sector_data: dict, natives: dict, now: float) -> None:
        """يبني شموع 7 أيام (ساعية) من البورصات العامة للقطاعات الأسخن أولاً، ويحفظها مؤقتاً في الحالة."""
        kl = self.state.data.setdefault("kl", {})
        for sym in [k for k, v in kl.items() if now - v.get("ts", 0) > 3 * 86400]:
            del kl[sym]
        if not (self.live_ok and self.s.ex):
            return
        order = sorted(sector_data, key=lambda k: -(group_summary(sector_data[k])["median_24h"]))
        queue, seen = [], set()
        for key in order:
            for c in sector_data[key]:
                ent = kl.get(c.symbol)
                if c.symbol not in seen and c.symbol in self.tickers and (not ent or now - ent["ts"] > 12 * 3600):
                    seen.add(c.symbol)
                    queue.append(c.symbol)
        for _ in range(int(self.cfg.get("live.klines_per_run"))):
            if not queue or self._time_left() < 150:
                break
            sym = queue.pop(0)
            rows = self.s.ex.klines(sym, limit=170, pages=1)
            if len(rows) >= 60:
                kl[sym] = {"ts": now, "c": [float(f"{r[4]:.6g}") for r in rows]}
            else:
                kl[sym] = {"ts": now - 11 * 3600, "c": []}  # لا نعيد المحاولة قبل ساعة
        for coins in list(sector_data.values()) + [list(natives.values())]:
            for c in coins:
                ent = kl.get(c.symbol)
                if ent and len(ent["c"]) >= 49 and c.price > 0 and 0.65 <= ent["c"][-1] / c.price <= 1.55:
                    c.spark = ent["c"]

    def _eco_coins(self, t: dict, now: float) -> list:
        if self.bulk:
            return self._group_coins(self.members.get("eco", {}).get(t["key"], []), self.byid)
        cat = t["eco"].get("category")
        return self._universe(self._get_rows(
            f"e:{t['key']}", lambda: self.s.cg.markets(category=cat, per_page=int(self.cfg.get("universe.per_page"))), now))

    # ------------------------------------------------------------------ توقع انتقال السيولة
    def _rotation(self, sector_data: dict, stats: dict, now: float) -> list:
        rotation_annotate(stats, sector_data, self.state.data, now)
        if not self.cfg.get("rotation.enabled"):
            return []
        rc = self.cfg.get("rotation")
        warned = self.state.data["warned"]
        cands, sent = [], 0
        for fc in rotation_forecast(stats, self.K, self.cfg, now):
            b = fc["to"]
            picks = rotation_early(sector_data.get(b, []), stats[b], self.vr, self.cfg, int(rc["max_coins"]))
            if not picks:
                continue
            self.summary["rotation"] = self.summary.get("rotation", []) + [(fc["from"], b, round(fc["conf"], 2), fc["src"])]
            for score, coin, r in picks[:3]:
                cand = Candidate(coin=coin, kinds={"rotation"}, sectors=[stats[b]["label"]],
                                 src={"src": "cg", "id": coin.id})
                cand.features = {"sector": clip(fc["conf"]), "breadth": clip(stats[b]["breadth"]), "gap": 0.25,
                                 "corr": clip(fc["corr"] / 0.5), "momo": clip(max(coin.ch1h or 0.0, 0.0) / 3.0),
                                 "rotation": clip(fc["conf"] * (0.6 + 0.4 * score))}
                cand.reasons = [(1, f"🔭 توقّع انتقال سيولة من {stats[fc['from']]['label'].split('·')[0].strip()} "
                                    f"إلى {stats[b]['label'].split('·')[0].strip()} (ثقة {fc['conf'] * 100:.0f}%)")]
                cand.meta = {"att_ratio": stats[b].get("att_ratio"), "sector_key": b, "heat": stats[b]["heat"],
                             "history": [f"تاريخياً بعد ~{fc['lag_h']:.0f}س من سخونة {fc['from']} تتحرك {b} (ارتباط {fc['corr']:.2f})"]
                             if fc["src"] == "history" else None}
                if cand.meta["history"] is None:
                    del cand.meta["history"]
                cands.append(cand)
            key = f"rot:{fc['from']}>{b}"
            if sent < int(rc["max_alerts_per_run"]) and now - warned.get(key, 0) >= float(rc["cooldown_hours"]) * 3600:
                msg = format_rotation(fc, stats[fc["from"]]["label"], stats[b]["label"], [(c, r) for _, c, r in picks],
                                      self.cfg.get("run.timezone"))
                if self._send(msg):
                    warned[key] = now
                    sent += 1
        return cands

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
        self.deadline = time.time() + float(self.cfg.get("run.time_budget_sec"))
        self.budget = Budget(d, self.cfg, now, monthly=float(self.cfg.get("paprika.monthly_budget")) if self.bulk else None)
        self.members, self.byid = {}, {}
        self.discovery = Discovery(self.cfg, d, self.s.eth)
        self.whale = WhaleBot(self.cfg, self.s.xfer, self.s.sol, self.s.dex, d, extra_wallets=self.discovery.wallets())

        if self.s.ex and self.cfg.get("live.enabled"):
            self.tickers = self._stage("tickers", self.s.ex.tickers, {}) or {}
        self.live_ok = bool(self.tickers)
        self.summary["live"] = (self.s.ex.src if self.live_ok else None)

        self.protocols = self._stage("protocols", lambda: self._protocols(now), {})
        valid = None if self.bulk else self._stage("categories", lambda: self._valid_categories(now))
        if self.bulk:
            sector_data, natives = self._stage("market", lambda: self._load_market_bulk(now), ({}, {}))
            self._stage("sparks", lambda: self._attach_sparks(sector_data, natives, now))
            trending = set()
        else:
            sector_data, natives = self._stage("market", lambda: self._load_market(now, valid), ({}, {}))
            trending = self._stage("trending", lambda: self._trending(now), set())
        flows = self._stage("bridge", lambda: self._bridge_flows(now), {})
        self.news_items = self._stage("news", lambda: self._news(now), [])

        self._stage("positions", lambda: self._track_positions(now))

        fresh_sectors = {k[2:] for k in self.fresh_keys if k.startswith("s:")}
        stats, cands = self._stage("sector", lambda: sector_analyze(
            sector_data, self.cfg, self.att, fresh=fresh_sectors, know=self.K), ({}, []))
        self.summary["sectors"] = stats
        self.stats = stats
        cands += self._stage("rotation", lambda: self._rotation(sector_data, stats, now), [])

        def waterfall():
            out = []
            triggers = waterfall_detect(natives, self.cfg, flows, self.vr)
            self.summary["triggers"] = [t["key"] for t in triggers]
            seen: set = set()
            for t in triggers:
                cat = t["eco"].get("category")
                if valid is not None and cat not in valid:
                    log.warning("فئة النظام البيئي غير صالحة: %s", cat)
                    continue
                coins = self._eco_coins(t, now)
                self._overlay(coins, now, seen)
                if self.bulk:
                    self._attach_sparks({t["key"]: coins}, {}, now)
                out += waterfall_scan(t, coins, self.cfg, self.vr, know=self.K)
            return out

        cands += self._stage("waterfall", waterfall, [])

        merged: dict = {}
        for c in cands:
            if c.coin.id in merged:
                merged[c.coin.id].merge(c)
            else:
                merged[c.coin.id] = c

        events = self._stage("whales", lambda: self.whale.scan_wallets(now) if self.whale.enabled else [], [])
        self._stage("distribution", lambda: self._distribution(now, events, flows))
        self._stage("whale_only", lambda: self._add_whale_only(events, merged))

        for c in merged.values():
            self._stage("enrich", lambda c=c: self._enrich_basic(c, flows))
            base = "https://coinpaprika.com/coin/" if self.bulk else "https://www.coingecko.com/en/coins/"
            c.meta.setdefault("url", f"{base}{c.coin.id}" if c.src.get("src") == "cg" else c.meta.get("url", ""))

        eligible = [c for c in merged.values() if self._passes_gates(c, now)]
        eligible.sort(key=lambda c: self.brain.prior(c.features), reverse=True)
        top = eligible[: int(self.cfg.get("run.dex_confirm_top"))]
        for c in top:
            if self._time_left() < 40:  # لا نخاطر بتجاوز مهلة GitHub: نكتفي بما تم
                log.warning("اقتراب نفاد زمن التشغيلة: تخطي فحوص السلسلة المتبقية")
                break
            self._stage("onchain", lambda c=c: self._confirm_onchain(c, events, flows, now))

        threshold = self.brain.threshold()
        scored = []
        for c in top:
            if c.meta.get("blocked"):
                continue
            s = self.brain.score(c.features) + min(6.0, 3.0 * (len(c.kinds) - 1))
            if c.coin.id in trending:
                s -= float(self.cfg.get("attention.trending_penalty"))
                c.meta["crowded"] = True
            stage = (self.stats.get(c.meta.get("sector_key") or "") or {}).get("stage")
            if stage in ("leaders", "broadening"):
                s += 3.0  # السيولة في طريقها للنزول نحو الأصغر
                c.reasons.append((2, f"🌊 مرحلة الموجة: {STAGE_LABEL[stage]}"))
            elif stage in ("late", "cooling"):
                s -= 8.0 if stage == "cooling" else 4.0  # آخر الموجة: مخاطرة أعلى
                c.meta["late_stage"] = stage
            scored.append((s, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        self.summary["near_miss"] = [(round(s, 1), c.coin.symbol, sorted(c.kinds)) for s, c in scored[:6]]
        self.summary["threshold"] = round(threshold, 1)

        sent = 0
        judged = 0
        for s, c in scored:
            if s < threshold or sent >= int(self.cfg.get("run.max_alerts_per_run")):
                continue
            plan = self._stage("plan", lambda c=c: self._plan(c, now))
            if not plan:
                continue
            extra = {"xratio": self.vr(c.coin) if c.coin.live else None, "history": c.meta.get("history"),
                     "news": c.meta.get("news")}
            if self.s.judge and judged < int(self.cfg.get("context.llm.max_calls_per_run")):
                judged += 1
                verdict = self._stage("llm", lambda c=c, s=s, plan=plan: self.s.judge.review(self._llm_summary(c, s, plan)))
                if verdict:
                    if verdict["verdict"] == "skip":
                        log.info("LLM skip %s: %s", c.coin.symbol, verdict["note"])
                        continue
                    s += 4.0 if verdict["verdict"] == "go" else -8.0
                    extra["llm"] = f"{verdict['note']} ({verdict['verdict']}، ثقة {verdict['confidence']:.0f}%)"
                    if s < threshold:
                        continue
            text = format_signal(c, s, plan, self.cfg.get("run.timezone"), extra)
            if self._send(text):
                self._register(c, s, plan, now)
                self.summary["signals"].append((round(s, 1), c.coin.symbol))
                sent += 1

        self._heartbeat(now, stats)
        self._report_errors(now)
        self.summary["cg"] = {"fresh": len(self.fresh_keys), "tokens": round(d["cg"]["tokens"], 1),
                              "used_month": d["cg"]["used"], "budget": self.budget.monthly}
        return self.summary

    # ------------------------------------------------------------------ مراحل التحليل
    def _add_whale_only(self, events: list, merged: dict) -> None:
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
            cand.meta = {"dex": i, "url": i.get("url", ""), "whale_only": True, "chain": ev["chain"],
                         "daily_vol": None}
            merged[cid] = cand

    def _context(self, c: Candidate, now: float) -> Optional[float]:
        """سياق أساسي: تغيّر TVL للبروتوكول + ذكر العملة في الأخبار."""
        coin = c.coin
        score, got = 0.0, False
        p = (self.protocols.get(coin.id) or self.protocols.get("sym:" + coin.symbol)) if self.protocols else None
        if p:
            got = True
            d7 = p.get("d7") or 0.0
            if d7 >= 8.0:
                s = clip(d7 / 40.0)
                if coin.mcap and p["tvl"] > 0 and coin.mcap / p["tvl"] < 1.5:
                    s = min(1.0, s + 0.2)
                score = max(score, s)
                c.reasons.append((5, f"🏦 TVL البروتوكول {d7:+.0f}% خلال 7 أيام ({fmt_usd(p['tvl'])})"))
        if self.news_items:
            got = True
            hits = News.match(self.news_items, coin, float(self.cfg.get("context.news_hours")), now)
            if hits:
                score = max(score, clip(0.4 + 0.2 * len(hits)))
                c.meta["news"] = hits
                c.reasons.append((8, f"📰 ذُكرت في {len(hits)} خبر خلال 24س"))
        return score if got else None

    def _enrich_basic(self, c: Candidate, flows: dict) -> None:
        coin = c.coin
        now = self.now_fn()
        r = self.vr(coin) if c.src.get("src") == "cg" else None
        if r is not None:
            c.features["vol"] = clip((r - 1.0) / 2.0)
            if r >= 1.5:
                c.reasons.append((5, f"📈 حجم تداول {coin.symbol} ×{r:.1f} فوق معدلها"))
        else:
            vol = coin.xvol or coin.volume
            turnover = (vol / coin.mcap) if coin.mcap > 0 else 0.0
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
        c.features["context"] = self._context(c, now)

    def _passes_gates(self, c: Candidate, now: float) -> bool:
        g = self.cfg.get("gates")
        coin = c.coin
        if coin.mcap and coin.mcap > self.cfg.get("universe.max_market_cap"):
            return False
        if not coin.live and coin.age_h > 3.0 and not c.meta.get("dex") and not c.meta.get("whale_only"):
            c.meta["stale"] = True  # بيانات قديمة غير مؤكدة لحظياً: لا نخاطر بها
            return False
        if coin.ch24h is not None and coin.ch24h > g["max_coin_24h"]:
            return False
        if coin.ch1h is not None and coin.ch1h > g["max_coin_1h"]:
            return False
        cd = self.state.data["cooldown"].get(coin.id)
        if cd and now - cd < float(self.cfg.get("run.cooldown_hours")) * 3600:
            return False
        return not any(s["coin_id"] == coin.id for s in self.state.data["open"])

    def _identify_dex(self, coin: Coin) -> Optional[dict]:
        dx = self.cfg.get("dex")
        best = None
        for p in self.s.dex.search(coin.symbol):
            s = summarize_pair(p)
            if not s or s["symbol"] != coin.symbol or s["liq"] < dx["min_liquidity"]:
                continue
            if coin.mcap > 0:
                ok = any(v > 0 and dx["identity_low"] <= v / coin.mcap <= dx["identity_high"] for v in (s["mcap"], s["fdv"]))
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
        key = None
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
        whale_usd, checked = 0.0, False
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
                    c.reasons.append((0, f"🐋 {ev['wallet']}: {label} {fmt_usd(ev['usd'])}"))
            if self.whale.can_check_flows and self.s.xfer.supports(info["chain"]):
                fl = self.whale.token_exchange_flow(info["chain"], info["addr"], info["price"], now)
                if fl is not None:
                    checked = True
                    net = fl["out_usd"] - fl["in_usd"]
                    if fl["in_usd"] >= self.cfg.get("whales.sell_block_usd") and fl["in_usd"] > 2 * fl["out_usd"]:
                        c.meta["blocked"] = True
                    elif net > 0:
                        whale_usd += net
                        if net >= self.cfg.get("whales.min_usd"):
                            c.reasons.append((0, f"🏦 صافي سحب من المنصات {fmt_usd(net)} (90 دقيقة)"))
        c.meta["whale_usd"] = whale_usd
        if checked or self.whale.enabled:
            c.features["whale"] = max(c.features.get("whale") or 0.0, clip(whale_usd / 500000.0))
            if c.features["whale"] >= 0.3:
                c.kinds.add("whale")

    # ------------------------------------------------------------------ الأهداف والوقف
    def _plan(self, c: Candidate, now: float) -> Optional[dict]:
        coin = c.coin
        tc, rc = self.cfg.get("targets"), self.cfg.get("risk")
        if not coin.live:  # بدون سعر بورصة لحظي: نحاول سعراً طازجاً من DexScreener/DefiLlama
            fp = self._fresh_price(c)
            if fp:
                coin.price = fp
        book = None
        if coin.live and self.s.ex and self._ob_calls < int(tc["orderbook_top"]):
            self._ob_calls += 1
            book = self._stage("orderbook", lambda: self.s.ex.orderbook(coin.symbol, int(tc["depth_limit"])))
        dex = c.meta.get("dex") or {}
        vol_ref = coin.xvol or coin.volume or dex.get("vol_h24", 0.0)
        flow = estimate_flow(c.meta.get("whale_usd", 0.0), vol_ref, float(tc["flow_share"]))
        wall_usd = max(float(tc["wall_min_usd"]), float(tc["wall_vs_volume"]) * vol_ref)
        dv = c.meta.get("daily_vol") or float(rc["default_daily_vol"])
        plan = build_plan(coin.price, coin.spark, dv, c.meta.get("gap_pct"), flow, book, dex.get("liq", 0.0), wall_usd,
                          tc, rc, self.K.mfe())
        c.meta["plan"] = plan
        return plan

    def _fresh_price(self, c: Candidate) -> Optional[float]:
        cached = c.coin.price
        cands = []
        info = c.meta.get("dex")
        if info:
            cands.append(info.get("price"))
        if c.src.get("src") == "cg":
            try:
                cands.append(self.s.llama.coin_prices([c.coin.id]).get(c.coin.id))
            except Exception as exc:
                log.warning("fresh price failed: %s", exc)
        for p in cands:
            if p and cached > 0 and 0.8 <= p / cached <= 1.25:
                return float(p)
        return None

    def _llm_summary(self, c: Candidate, score: float, plan: dict) -> str:
        coin, dex = c.coin, c.meta.get("dex") or {}
        return json.dumps({
            "symbol": coin.symbol, "name": coin.name, "score": round(score), "kinds": sorted(c.kinds),
            "sector": c.sectors, "reasons": [t for _, t in sorted(c.reasons)][:6], "price": coin.price,
            "ch1h": coin.ch1h, "ch24h": coin.ch24h, "ch7d": coin.ch7d, "mcap": coin.mcap,
            "volume_24h": coin.xvol or coin.volume, "dex_liquidity": dex.get("liq"),
            "targets": [{"pct": round(t["pct"], 1), "basis": t["basis"]} for t in plan["targets"]],
            "stop_pct": round(plan["stop_pct"], 1), "news": c.meta.get("news") or [],
            "crowded": bool(c.meta.get("crowded"))}, ensure_ascii=False)

    def _register(self, c: Candidate, score: float, plan: dict, now: float) -> None:
        d = self.state.data
        d["seq"] += 1
        dex = c.meta.get("dex") or {}
        d["open"].append({
            "id": d["seq"], "coin_id": c.coin.id, "symbol": c.coin.symbol, "name": c.coin.name,
            "entry": c.coin.price, "last": c.coin.price, "ts": now, "kinds": sorted(c.kinds), "sectors": c.sectors,
            "score": score, "features": c.features,
            "targets": [{"k": t["k"], "price": t["price"], "pct": t["pct"], "basis": t["basis"], "hit": False}
                        for t in plan["targets"]],
            "stop": plan["stop"], "stop_cur": plan["stop"], "stop_pct": plan["stop_pct"], "hit": 0,
            "hi": c.coin.price, "lo": c.coin.price, "src": c.src, "miss": 0, "warned": 0,
            "sector_key": c.meta.get("sector_key"), "heat0": c.meta.get("heat0"), "chain": c.meta.get("chain"),
            "contract": ({"chain": dex["chain"], "addr": dex["addr"], "pair": dex.get("pair", "")} if dex else None),
        })
        d["cooldown"][c.coin.id] = now

    # ------------------------------------------------------------------ تتبع الصفقات المفتوحة
    def _prices_for(self, open_sigs: list) -> dict:
        out: dict = {}
        need = []
        for s in open_sigs:
            tk = self.tickers.get(s["symbol"])
            ref = s.get("last") or s["entry"]
            if tk and ref and abs(tk["price"] / ref - 1.0) <= 0.4:
                out[s["id"]] = tk["price"]
            else:
                need.append(s)
        cg_ids = sorted({s["src"]["id"] for s in need if s["src"].get("src") == "cg"})
        got: dict = {}
        if cg_ids:
            try:
                got = self.s.llama.coin_prices(cg_ids)
            except Exception as exc:
                log.warning("llama prices failed: %s", exc)
            missing = [i for i in cg_ids if i not in got]
            if missing and self.budget.take():
                got = {**got, **self.s.cg.prices(missing)}
        by_chain: dict = {}
        for s in need:
            if s["src"].get("src") == "dex":
                by_chain.setdefault(s["src"]["chain"], set()).add(s["src"]["addr"])
        dexp: dict = {}
        for chain, addrs in by_chain.items():
            info = best_by_address(self.s.dex.tokens(chain, sorted(addrs)))
            dexp.update({(chain, a): i["price"] for a, i in info.items()})
        for s in need:
            p = got.get(s["src"]["id"]) if s["src"].get("src") == "cg" else dexp.get((s["src"].get("chain"), s["src"].get("addr")))
            if p:
                out[s["id"]] = p
        return out

    @staticmethod
    def _ensure_plan(s: dict) -> None:
        """ترحيل صفقات محفوظة بصيغة قديمة (هدف واحد)."""
        if "targets" not in s:
            s["targets"] = [{"k": 1, "price": s["entry"] * (1 + s.get("target_pct", 8.0) / 100.0),
                             "pct": s.get("target_pct", 8.0), "basis": "هدف قديم", "hit": False}]
            s["stop"] = s["stop_cur"] = s["entry"] * (1 - s.get("stop_pct", 6.0) / 100.0)
            s["hit"] = 0
            s.setdefault("warned", 0)
            s.setdefault("last", s["entry"])

    def _track_positions(self, now: float) -> None:
        d = self.state.data
        if not d["open"]:
            return
        risk, pc = self.cfg.get("risk"), self.cfg.get("positions")
        horizon, max_hold = float(risk["horizon_hours"]) * 3600, float(pc["max_hold_hours"]) * 3600
        prices = self._prices_for(d["open"])
        still, samples = [], []
        for s in d["open"]:
            self._ensure_plan(s)
            p = prices.get(s["id"])
            age = now - s["ts"]
            if p is None:
                s["miss"] = s.get("miss", 0) + 1
                if age <= max_hold * 1.5:
                    still.append(s)
                continue
            s["last"] = p
            s["hi"], s["lo"] = max(s["hi"], p), min(s["lo"], p)
            ret = (p / s["entry"] - 1.0) * 100.0
            for t in s["targets"]:
                if not t["hit"] and p >= t["price"]:
                    t["hit"] = True
                    s["hit"] = max(s["hit"], t["k"])
                    if pc["trail_after_t1"]:
                        s["stop_cur"] = max(s["stop_cur"], s["entry"] * 1.002 if t["k"] == 1 else s["targets"][t["k"] - 2]["price"])
                    self._send(format_target_hit(s, t, ret, s["stop_cur"]))
            reason = None
            if all(t["hit"] for t in s["targets"]):
                reason = "target"
            elif p <= s["stop_cur"]:
                reason = "trail" if s["hit"] else "stop"
            elif age >= (max_hold if s["hit"] else horizon):
                reason = "expired"
            if reason is None:
                still.append(s)
                continue
            t1 = s["targets"][0]["pct"]
            y = 1 if (s["hit"] >= 1 or (reason == "expired" and ret >= 0.4 * t1)) else 0
            rec = {"id": s["id"], "symbol": s["symbol"], "kinds": s["kinds"], "score": s["score"], "y": y,
                   "ret": round(ret, 2), "hit": s["hit"], "mfe": round((s["hi"] / s["entry"] - 1) * 100, 2),
                   "mae": round((s["lo"] / s["entry"] - 1) * 100, 2), "reason": reason,
                   "features": s["features"], "ts": s["ts"], "closed": now}
            d["closed"].append(rec)
            samples.append((s["features"], y))
            self.summary["closed"] += 1
            if self.cfg.get("run.notify_outcomes"):
                self._send(format_close({**s, "y": y}, ret, reason, age / 3600.0))
            if y == 1:
                self._stage("discovery", lambda s=s: self.discovery.on_win(s))
        d["open"] = still
        if samples:
            log.info("تعلّم النموذج من %d صفقة مغلقة", Brain(self.cfg, d).learn(samples))

    # ------------------------------------------------------------------ كشف التصريف
    def _warn(self, key: str, now: float) -> bool:
        w = self.state.data.setdefault("warned", {})
        if now - w.get(key, 0) < float(self.cfg.get("distribution.warn_cooldown_hours")) * 3600:
            return False
        w[key] = now
        for k in [k for k, v in w.items() if now - v > 3 * 86400]:
            del w[k]
        return True

    def _distribution(self, now: float, events: list, flows: dict) -> None:
        dc = self.cfg.get("distribution")
        if not dc.get("enabled"):
            return
        tz = self.cfg.get("run.timezone")
        left = int(dc["max_warnings_per_run"])
        open_syms = {s["symbol"] for s in self.state.data["open"]}
        # أ) محافظ الحيتان تودع في المنصات
        for ev in events:
            if left <= 0:
                break
            if ev["kind"] == "exchange_inflow" and ev["usd"] >= dc["exchange_inflow_usd"] and self._warn(f"inf:{ev['chain']}:{ev['addr']}", now):
                lines = [f"{ev['wallet']} حوّلت {fmt_usd(ev['usd'])} من #{ev['symbol']} إلى منصة تداول",
                         "إيداع الحيتان في المنصات يسبق غالباً البيع",
                         f"السيولة في الحوض: {fmt_usd(ev['info']['liq'])}"]
                if self._send(format_warning(ev["symbol"], "dump", lines, ev["symbol"] in open_syms, tz)):
                    left -= 1
        # ب) صفقاتنا المفتوحة: ضعف الزخم / ذروة وانعكاس / خروج سيولة الشبكة / تبريد القطاع
        pc = self.cfg.get("positions")
        for s in self.state.data["open"]:
            if left <= 0:
                break
            p, hi, entry = s.get("last", s["entry"]), s["hi"], s["entry"]
            lines, kind = [], None
            dd = (1.0 - p / hi) * 100.0 if hi > 0 else 0.0
            if hi / entry - 1.0 >= 0.03 and dd >= float(pc["momentum_break_pct"]):
                kind = "momentum"
                lines.append(f"تراجع {dd:.1f}% عن قمة الصفقة ({fmt_price(hi)}) بعد صعود {(hi / entry - 1) * 100:.1f}%")
            coin = self.coin_index.get(s["coin_id"])
            if coin and (coin.ch24h or 0) >= dc["climax_24h"] and (coin.ch1h is not None and coin.ch1h <= dc["climax_pullback_1h"]):
                kind = "climax"
                lines.append(f"صعود 24س {coin.ch24h:+.1f}% ثم انعكاس {coin.ch1h:+.1f}% في ساعة")
            fl = flows.get(norm_chain(s.get("chain"))) if s.get("chain") else None
            if fl and fl.get("d6_usd") is not None and fl["d6_usd"] <= -float(self.cfg.get("bridge.min_stable_inflow_usd_6h")):
                kind = kind or "outflow"
                lines.append(f"خروج عملات مستقرة من الشبكة: {fmt_usd(-fl['d6_usd'])} خلال 6س")
            st = self.stats.get(s.get("sector_key")) if s.get("sector_key") else None
            if st and s.get("heat0") and st["heat"] < 0.4 * s["heat0"] and st["breadth"] < 0.45:
                kind = kind or "cooling"
                lines.append(f"القطاع يبرد: السخونة {st['heat']:+.1f}% بعد {s['heat0']:+.1f}% عند الدخول")
            if kind and self._warn(f"pos:{s['id']}:{kind}", now):
                lines.append(f"السعر الآن ${fmt_price(p)} ({(p / entry - 1) * 100:+.1f}% من دخولك)")
                if self._send(format_warning(s["symbol"], kind, lines, True, tz)):
                    left -= 1
        # ج) عملات أُشير لها مؤخراً (72س): ذروة وانعكاس
        recent = {cid for cid, ts in self.state.data["cooldown"].items() if now - ts <= 72 * 3600}
        for cid in recent:
            if left <= 0:
                break
            coin = self.coin_index.get(cid)
            if not coin or any(s["coin_id"] == cid for s in self.state.data["open"]):
                continue
            r = self.vr(coin)
            if ((coin.ch24h or 0) >= dc["climax_24h"] and coin.ch1h is not None and coin.ch1h <= dc["climax_pullback_1h"]
                    and (r or 0) >= dc["climax_vol_ratio"] and self._warn(f"climax:{cid}", now)):
                lines = [f"صعود 24س {coin.ch24h:+.1f}% ثم انعكاس {coin.ch1h:+.1f}% في ساعة",
                         f"حجم التداول ×{r:.1f} فوق المعتاد = احتمال تصريف عند القمة"]
                if self._send(format_warning(coin.symbol, "climax", lines, False, tz)):
                    left -= 1

    # ------------------------------------------------------------------ نبضة الحياة وتقرير الأخطاء
    def _heartbeat(self, now: float, stats: dict) -> None:
        hrs = float(self.cfg.get("run.heartbeat_hours"))
        meta = self.state.data["meta"]
        if hrs <= 0 or not stats or now - meta.get("last_heartbeat", 0) < hrs * 3600:
            return
        top = sorted(stats.values(), key=lambda s: s["heat"], reverse=True)[:3]
        wr, n = self.brain.win_rate()
        lines = ["📡 <b>الرادار يعمل</b>",
                 "🔥 أسخن القطاعات: " + " | ".join(f"{s['label'].split('·')[0].strip()} {s['heat']:+.1f}%" for s in top),
                 f"📂 صفقات مفتوحة: {len(self.state.data['open'])} | مُقيَّمة: {n}"
                 + (f" | نجاح: {wr * 100:.0f}%" if wr is not None else ""),
                 f"🔌 بيانات لحظية: {self.summary.get('live') or 'غير متاحة (CoinGecko فقط)'}",
                 f"📊 CoinGecko هذا الشهر: {self.state.data['cg']['used']}/{int(self.budget.monthly)} طلب"]
        if self._send("\n".join(lines)):
            meta["last_heartbeat"] = now

    def _report_errors(self, now: float) -> None:
        errs = self.summary["errors"]
        if not errs or not self.cfg.get("run.notify_errors"):
            return
        d = self.state.data
        if now - d.get("last_error_ts", 0) < 6 * 3600:
            return
        if self._send("⚠️ <b>أخطاء في تشغيل الرادار</b>\n" + "\n".join(f"• {html.escape(e[:160])}" for e in errs[:5])):
            d["last_error_ts"] = now

    def write_step_summary(self) -> None:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"### رادار السيولة — تشغيلة #{self.state.data['meta']['runs']}\n")
                f.write(f"- بيانات لحظية: {self.summary.get('live')} | الحد الأدنى: {self.summary.get('threshold')}\n")
                f.write(f"- إشارات: {self.summary['signals']}\n- أقرب المرشحين: {self.summary['near_miss']}\n")
                f.write(f"- قطاعات ساخنة: {[s['label'] for s in self.summary['sectors'].values() if s['hot']]}\n")
                f.write(f"- شبكات مُفعَّلة: {self.summary.get('triggers')}\n- CoinGecko: {self.summary.get('cg')}\n")
                if self.summary["errors"]:
                    f.write(f"- ⚠️ أخطاء: {self.summary['errors']}\n")
        except OSError:
            pass


# ======================================================================
# selftest
# ======================================================================
REQUIRED_KEYS = {
    "run": ["min_score", "max_alerts_per_run", "cooldown_hours", "timezone", "heartbeat_hours", "notify_outcomes",
            "notify_errors", "dex_confirm_top", "time_budget_sec"],
    "universe": ["min_market_cap", "max_market_cap", "min_volume_24h", "per_page"],
    "gates": ["max_coin_24h", "max_coin_1h"],
    "live": ["enabled", "exchanges", "match_tolerance", "snapshot_hours", "klines_per_run"],
    "paprika": ["monthly_budget"],
    "rotation": ["enabled", "min_source_heat", "min_conf", "window_low", "window_high", "max_alerts_per_run", "cooldown_hours",
                 "max_coins", "prior_lag_h", "prior_corr"],
    "sector": ["hot_heat", "min_breadth", "laggard_gap", "laggard_max_24h", "min_corr", "max_7d_dump"],
    "waterfall": ["native_24h", "native_1h", "max_triggers", "min_corr", "laggard_gap"],
    "attention": ["alpha", "warmup_runs", "trending_penalty"],
    "bridge": ["min_stable_inflow_usd_6h", "min_stable_inflow_pct_6h", "history_hours"],
    "targets": ["min_t1_pct", "max_target_pct", "wall_min_usd", "wall_vs_volume", "wall_band_pct", "depth_limit",
                "orderbook_top", "flow_share", "stop_atr_mult"],
    "risk": ["default_daily_vol", "stop_min", "stop_max", "rr", "horizon_hours"],
    "positions": ["trail_after_t1", "max_hold_hours", "momentum_break_pct"],
    "distribution": ["enabled", "warn_cooldown_hours", "max_warnings_per_run", "exchange_inflow_usd", "climax_24h",
                     "climax_pullback_1h", "climax_vol_ratio"],
    "whales": ["min_usd", "min_liquidity", "first_run_lookback_min", "max_wallets", "sell_block_usd", "token_flow_check"],
    "discovery": ["enabled", "min_usd", "min_hits", "max_wallets", "weight"],
    "context": ["news_hours"],
    "dex": ["min_liquidity", "identity_low", "identity_high", "min_txns_h1"],
    "learning": ["min_samples", "max_alpha", "alpha_k", "lr", "l2", "win_window"],
    "backtest": ["candles", "pages", "top_per_group", "horizon_hours", "min_trades", "apply_tuned"],
    "coingecko": ["monthly_budget", "refresh_minutes_live", "refresh_minutes_nolive", "max_cache_age_hours_live",
                  "max_cache_age_hours_nolive", "trending_refresh_minutes", "retry_after_fail_minutes"],
    "network": ["coingecko_interval_free", "coingecko_interval_key"],
}
SLUG = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


def validate_config(cfg) -> tuple:
    """يفحص config.yaml: مفاتيح ناقصة، عناوين خاطئة، قيم غير منطقية. يعيد (أخطاء، تحذيرات)."""
    errors, warns = [], []
    for sec, keys in REQUIRED_KEYS.items():
        for k in keys:
            try:
                cfg.get(f"{sec}.{k}")
            except KeyError:
                errors.append(f"مفتاح ناقص: {sec}.{k}")
    for p in ("context.llm.model", "context.llm.max_calls_per_run"):
        try:
            cfg.get(p)
        except KeyError:
            errors.append(f"مفتاح ناقص: {p}")
    if not cfg.sectors:
        errors.append("قسم sectors فارغ")
    cats = {}
    for k, s in cfg.sectors.items():
        c = s.get("category") if isinstance(s, dict) else None
        if not c or not SLUG.match(str(c)):
            errors.append(f"قطاع {k}: category غير صالح ({c})")
        elif c in cats:
            warns.append(f"قطاع {k} يكرر فئة {cats[c]}")
        cats[c] = k
        if not s.get("label"):
            warns.append(f"قطاع {k} بلا label")
    for k, e in cfg.ecosystems.items():
        if not (e.get("category") and SLUG.match(str(e["category"]))) or not e.get("chain"):
            errors.append(f"نظام {k}: category/chain غير صالح")
    seen = set()
    for w in cfg.whales.get("wallets") or []:
        if not valid_wallet(w):
            errors.append(f"محفظة غير صالحة: {w.get('label')} {w.get('chain')} {w.get('address')}")
            continue
        key = (str(w["chain"]).lower(), str(w["address"]).lower())
        if key in seen:
            warns.append(f"محفظة مكررة: {w.get('label')}")
        seen.add(key)
        if not 0 < float(w.get("weight", 1.0)) <= 1.0:
            errors.append(f"وزن خارج (0,1]: {w.get('label')}")
    for n, a in (cfg.whales.get("exchanges") or {}).items():
        if not EVM_ADDR.match(str(a)):
            errors.append(f"عنوان منصة غير صالح: {n}")
    try:
        bad = [x for x in cfg.get("live.exchanges") if x not in Exchanges.SOURCES]
        if bad:
            errors.append(f"live.exchanges مصادر غير معروفة: {bad}")
        if not 0 < float(cfg.get("run.min_score")) < 100:
            errors.append("run.min_score يجب أن يكون بين 0 و100")
        if float(cfg.get("coingecko.monthly_budget")) > 10000:
            warns.append("monthly_budget يتجاوز حد الخطة المجانية (10,000)")
        if float(cfg.get("targets.max_target_pct")) < float(cfg.get("targets.min_t1_pct")):
            errors.append("targets.max_target_pct أصغر من min_t1_pct")
    except (KeyError, TypeError, ValueError):
        pass
    return errors, warns


# ---------------------------------------------------------------------- بيانات اصطناعية وبدائل
def _st_spark(rng, factor, drift=0.0, n=168):
    r = factor + rng.normal(0, 0.004, n)
    r[-24:] += drift
    return list(np.exp(np.cumsum(r)) * 10.0)


def _st_row(cid, sym, sp, ch1, ch24, ch7, mcap=2e8, vol=2e7):
    return {"id": cid, "symbol": sym, "name": sym.title(), "current_price": sp[-1], "market_cap": mcap,
            "total_volume": vol, "price_change_percentage_1h_in_currency": ch1,
            "price_change_percentage_24h_in_currency": ch24, "price_change_percentage_7d_in_currency": ch7,
            "sparkline_in_7d": {"price": sp}}


def _st_hot(seed=1, prefix="r"):
    rng = np.random.default_rng(seed)
    f = rng.normal(0, 0.01, 168)
    rows = [_st_row(f"{prefix}{i}", f"{prefix.upper()}{i}", _st_spark(rng, f, 0.006), 0.4, 14 + rng.normal(0, 2), 10)
            for i in range(11)]
    rows.append(_st_row(f"{prefix}lag", "LAG", _st_spark(rng, f, 0.0), 0.8, -1.0, 2.0, mcap=8e7, vol=8e6))
    return rows


def _st_flat(seed, prefix):
    rng = np.random.default_rng(seed)
    f = rng.normal(0, 0.004, 168)
    return [_st_row(f"{prefix}{i}", f"{prefix.upper()}{i}", _st_spark(rng, f), 0.0, rng.normal(0, 1), 0.0)
            for i in range(10)]


class _FakeCG:
    def __init__(self, cats, natives=(), prices=None):
        self.cats, self.natives, self._prices, self.calls = cats, list(natives), prices or {}, 0

    def markets(self, category=None, ids=None, per_page=60, sparkline=True):
        self.calls += 1
        rows = self.cats.get(category, []) if category else [r for r in self.natives if r["id"] in (ids or [])]
        return [c for c in (Coin.from_api(r) for r in rows) if c]

    def prices(self, ids):
        self.calls += 1
        return {i: self._prices[i] for i in ids if i in self._prices}

    def trending(self):
        self.calls += 1
        return set()

    def category_ids(self):
        self.calls += 1
        return []


class _FakeLlama:
    def __init__(self, stables=None):
        self.stables = stables or {"ethereum": 1.6e11}

    def chains_tvl(self):
        return {"ethereum": 5e10}

    def stablecoin_supply(self):
        return dict(self.stables)

    def coin_prices(self, ids):
        return {}

    def protocols(self):
        return {}


class _FakeDex:
    def __init__(self, price, tokens=None):
        self.price, self._tokens = price, tokens or []

    def search(self, q):
        if q != "LAG":
            return []
        return [{"chainId": "ethereum", "pairAddress": "0xpair", "baseToken": {"address": "0xLAG", "symbol": "LAG", "name": "Lag"},
                 "priceUsd": str(self.price), "liquidity": {"usd": 600000}, "marketCap": 8e7, "fdv": 9e7,
                 "volume": {"h1": 400000, "h24": 3000000}, "txns": {"h1": {"buys": 70, "sells": 20}},
                 "priceChange": {"h1": 0.8, "h24": -1.0}, "url": "https://dexscreener.com/x"}]

    def tokens(self, chain, addrs):
        return list(self._tokens)


class _FakeEx:
    src = "fake"

    def __init__(self, tickers=None, book=None):
        self._t, self._book = tickers or {}, book

    def tickers(self):
        return dict(self._t)

    def orderbook(self, base, limit=100):
        return self._book

    def klines(self, base, limit=500, pages=1):
        n = min(int(limit) * max(1, int(pages)), 600)
        grp = zlib.crc32(base[:2].encode())
        f = np.random.default_rng(grp).normal(0, 0.012, n)
        idio = np.random.default_rng(zlib.crc32(base.encode())).normal(0, 0.006, n)
        c = 10.0 * np.exp(np.cumsum(f * 0.8 + idio))
        t0 = 1_700_000_000_000
        rows, prev = [], c[0]
        for i in range(n):
            rows.append([t0 + i * 3600_000, prev, c[i] * 1.004, c[i] * 0.996, c[i], 5e5])
            prev = c[i]
        return rows


class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class _FakeXfer:
    def __init__(self, wallet_txs=None, contract_txs=None):
        self.w, self.c = wallet_txs, contract_txs

    def supports(self, chain):
        return True

    def wallet(self, chain, addr):
        return self.w

    def contract(self, chain, contract):
        return self.c


class _FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def _find(self, url):
        for k, v in self.routes.items():
            if k in url:
                return v
        return None

    def get_json(self, url, params=None, headers=None, retries=3):
        return self._find(url)

    def get_text(self, url, params=None, headers=None, retries=3):
        return self._find(url)

    def post_json(self, url, payload, headers=None, retries=3):
        return self._find(url)


NOW0 = 1_800_000_000.0


def _st_cfg(**over):
    base = load_config()
    cfg = Config(copy.deepcopy(base.settings), env={})
    cfg.settings["run"].update(min_score=50, heartbeat_hours=0)
    cfg.settings["coingecko"]["monthly_budget"] = 10 ** 9
    for sec, kv in over.items():
        cfg.settings[sec].update(kv)
    cfg.whales = cfg.settings["whales"]
    return cfg


def _st_cats():
    cats = {"real-world-assets-rwa": _st_hot(1)}
    for i, cat in enumerate(["zero-knowledge-zk", "artificial-intelligence", "depin", "layer-2"]):
        cats[cat] = _st_flat(10 + i, f"f{i}")
    return cats


def _st_tickers(rows, lag_price=None):
    t = {}
    for r in rows:
        c = Coin.from_api(r)
        t[c.symbol] = {"price": c.price, "ch24": c.ch24h, "qvol": c.volume * 0.3, "src": "fake"}
    if lag_price:
        t["LAG"]["price"] = lag_price
    return t


def _st_book(p):
    asks = [(p * (1 + 0.001 * (i + 1)), 30.0 / p) for i in range(40)]          # عمق خفيف ~ $30 لكل مستوى
    asks += [(p * (1.06 + 0.001 * i), 50000.0 / p) for i in range(5)]           # جدار بيع ≈ $250K عند +6%
    bids = [(p * (1 - 0.001 * (i + 1)), 30.0 / p) for i in range(40)]
    bids += [(p * (0.95 - 0.001 * i), 40000.0 / p) for i in range(5)]
    return {"asks": sorted(asks), "bids": sorted(bids, reverse=True)}


def _st_engine(tmp, cats=None, tickers=None, book=None, dex_price=None, now=NOW0, cfg=None, xfer=None, dex_tokens=None,
               state=None, judge=None, news=None):
    cfg = cfg or _st_cfg()
    state = state or State(Path(tmp) / "state.json")
    holder = {"t": now}
    rows = _st_hot(1)
    lag = Coin.from_api(rows[-1]).price
    svc = Services(cg=_FakeCG(cats if cats is not None else _st_cats()), llama=_FakeLlama(),
                   dex=_FakeDex(dex_price or lag, dex_tokens),
                   ex=_FakeEx(tickers if tickers is not None else _st_tickers(rows), book if book is not None else _st_book(lag)),
                   xfer=xfer, notifier=_FakeNotifier(), judge=judge, news=news)
    return Engine(cfg, state, svc, now_fn=lambda: holder["t"]), svc, state, holder


# ---------------------------------------------------------------------- الاختبارات
def _t_config():
    errs, _ = validate_config(load_config())
    assert not errs, errs
    broken = Config({"run": {}}, env={})
    assert validate_config(broken)[0], "يجب أن يكتشف الإعدادات الناقصة"


def _t_analytics():
    coins = [c for c in (Coin.from_api(r) for r in _st_hot(1)) if c]
    prof = correlation_profile(coins)
    assert prof["avg_corr"] > 0.5 and prof["idx_24h"] > 5 and prof["per_coin"]["rlag"]["corr"] > 0.5
    lags = find_laggards(coins, prof["per_coin"], group_summary(coins)["median_24h"], 4.0, 6.0, 0.4)
    assert [x["coin"].symbol for x in lags] == ["LAG"]


def _t_targets():
    p = 100.0
    spark = [100 + 8 * math.sin(i / 6.0) for i in range(168)]
    book = _st_book(p)
    tc, rc = load_config().get("targets"), load_config().get("risk")
    small = build_plan(p, spark, 6.0, 20.0, 100_000, book, 600_000, 160_000, tc, rc)
    big = build_plan(p, spark, 6.0, 20.0, 3_000_000, book, 600_000, 160_000, tc, rc)
    assert small["walls_up"] and 5.0 < small["walls_up"][0]["dist_pct"] < 7.0
    assert small["ceiling"] is not None and small["targets"][-1]["price"] < small["ceiling"]["price"]  # تدفق صغير: سقف عند الجدار
    assert big["ceiling"] is None and big["targets"][-1]["pct"] > small["targets"][-1]["pct"]          # تدفق ضخم: يبتلع الجدار
    for plan in (small, big):
        assert plan["stop"] < p and 0 < plan["stop_pct"] <= 12.0
        prices = [t["price"] for t in plan["targets"]]
        assert prices == sorted(prices) and all(t["pct"] <= tc["max_target_pct"] for t in plan["targets"])
    assert abs(absorb_price([(101, 1000), (102, 1000)], 150_000) - 102) < 1e-9
    assert dex_push_price(1.0, 100_000, 10_000) > 1.2 and absorb_price([(101, 10)], 1e6) is None
    only_dex = build_plan(p, [], 6.0, None, 50_000, None, 400_000, 40_000, tc, rc)
    assert only_dex["targets"] and not only_dex["has_book"]


def _t_pipeline(tmp):
    eng, svc, state, _ = _st_engine(tmp)
    summary = eng.run()
    assert summary["sectors"]["rwa"]["hot"] and not summary["errors"], summary["errors"]
    assert summary["live"] == "fake"
    msgs = [m for m in svc.notifier.sent if "إشارة شراء" in m and "LAG" in m]
    assert msgs, summary["near_miss"]
    m = msgs[0]
    for needle in ("لماذا الآن", "الأهداف", "T1", "T2", "الوقف", "عائد/مخاطرة", "جدار بيع", "السوق"):
        assert needle in m, needle
    assert len(m) < 4000
    pos = state.data["open"][0]
    assert pos["symbol"] == "LAG" and len(pos["targets"]) >= 2 and pos["stop"] < pos["entry"] < pos["targets"][0]["price"]
    t1 = pos["targets"][0]["pct"]
    assert 2.9 <= t1 <= 7.0, t1  # الهدف الأول قبل جدار البيع (+6%) وليس بعده


def _t_lifecycle(tmp):
    eng, svc, state, holder = _st_engine(tmp)
    eng.run()
    pos = state.data["open"][0]
    tick = _st_tickers(_st_hot(1))
    tick["LAG"]["price"] = pos["targets"][0]["price"] * 1.001
    holder["t"] += 600
    e2 = Engine(eng.cfg, state, Services(**{**svc.__dict__, "ex": _FakeEx(tick, None)}), now_fn=lambda: holder["t"])
    e2.run()
    pos = state.data["open"][0]
    assert pos["hit"] == 1 and pos["stop_cur"] >= pos["entry"], "بعد T1 ينتقل الوقف لنقطة الدخول"
    assert any("حقق" in m and "T1" in m for m in svc.notifier.sent)
    tick["LAG"]["price"] = pos["entry"] * 0.995
    holder["t"] += 600
    Engine(eng.cfg, state, Services(**{**svc.__dict__, "ex": _FakeEx(tick, None)}), now_fn=lambda: holder["t"]).run()
    assert not state.data["open"] and state.data["closed"][-1]["y"] == 1 and state.data["closed"][-1]["reason"] == "trail"
    assert state.data["model"]["n"] == 1 and any("✅" in m for m in svc.notifier.sent)


def _t_stop_loss(tmp):
    eng, svc, state, holder = _st_engine(tmp)
    eng.run()
    pos = state.data["open"][0]
    tick = _st_tickers(_st_hot(1))
    tick["LAG"]["price"] = pos["stop"] * 0.99
    holder["t"] += 600
    Engine(eng.cfg, state, Services(**{**svc.__dict__, "ex": _FakeEx(tick, None)}), now_fn=lambda: holder["t"]).run()
    assert state.data["closed"][-1]["y"] == 0 and state.data["closed"][-1]["reason"] == "stop"


def _t_distribution(tmp):
    cfg = _st_cfg()
    cfg.whales["wallets"] = [{"label": "DWF", "chain": "ethereum", "address": "0x" + "ab" * 20, "weight": 1.0}]
    cfg.whales["exchanges"] = {"Binance": "0x" + "cd" * 20}
    tx = [{"timeStamp": str(int(NOW0) - 60), "from": "0x" + "ab" * 20, "to": "0x" + "cd" * 20,
           "value": str(int(100_000 * 1e18)), "tokenDecimal": "18", "tokenSymbol": "TOK", "contractAddress": "0xTOK"}]
    pair = [{"chainId": "ethereum", "baseToken": {"address": "0xtok", "symbol": "TOK", "name": "Tok"}, "priceUsd": "2.0",
             "liquidity": {"usd": 900000}, "marketCap": 5e7, "fdv": 6e7, "volume": {}, "txns": {}, "priceChange": {}}]
    eng, svc, state, _ = _st_engine(tmp, cfg=cfg, xfer=_FakeXfer(tx), dex_tokens=pair)
    eng.run()
    w = [m for m in svc.notifier.sent if "تحذير تصريف" in m]
    assert w and "TOK" in w[0], svc.notifier.sent


def _t_position_warning(tmp):
    eng, svc, state, holder = _st_engine(tmp)
    eng.run()
    pos = state.data["open"][0]
    tick = _st_tickers(_st_hot(1))
    up = pos["entry"] * 1.04
    tick["LAG"]["price"] = up
    holder["t"] += 600
    e = Engine(eng.cfg, state, Services(**{**svc.__dict__, "ex": _FakeEx(tick, None)}), now_fn=lambda: holder["t"])
    e.run()
    tick["LAG"]["price"] = up * 0.95
    holder["t"] += 600
    Engine(eng.cfg, state, Services(**{**svc.__dict__, "ex": _FakeEx(tick, None)}), now_fn=lambda: holder["t"]).run()
    assert any("ضعف الزخم" in m for m in svc.notifier.sent), [m[:40] for m in svc.notifier.sent]


def _t_budget_cache(tmp):
    cats = _st_cats()
    eng, svc, state, holder = _st_engine(tmp, cats=cats, tickers={})  # بدون طبقة لحظية
    eng.run()
    calls = svc.cg.calls
    eng.cfg.settings["coingecko"]["monthly_budget"] = 1
    state.data["cg"]["tokens"] = 0.0
    holder["t"] += 4 * 3600
    s = Engine(eng.cfg, state, svc, now_fn=lambda: holder["t"]).run()
    assert svc.cg.calls == calls and s["sectors"]["rwa"]["hot"], "عند نفاد الميزانية يعمل من الكاش"
    cfg = load_config()
    est = estimate_cg_usage(cfg, True)
    assert est["per_month"] < 10000 and est["ok"], est
    st = {"cg": {"tokens": 0.0, "ts": NOW0, "month": "", "used": 0}}
    b = Budget(st, _st_cfg(coingecko={"monthly_budget": 9000}), NOW0 + 600)
    assert 1.9 < st["cg"]["tokens"] < 2.3 and b.take() and b.take() and not b.take()


def _t_live_overlay(tmp):
    rows = _st_hot(1)
    tick = _st_tickers(rows)
    tick["R0"]["price"] *= 3.0  # رمز مشترك لعملة مختلفة: يجب رفضه
    eng, svc, state, _ = _st_engine(tmp, tickers=tick)
    eng.run()
    c = eng.coin_index
    assert "r0" not in c and "rlag" in c and c["rlag"].live and c["rlag"].xvol > 0


def _t_brain():
    cfg = load_config()
    b = Brain(cfg, {"closed": []})
    rng = np.random.default_rng(0)
    samples = [({"whale": w, "sector": 0.5, "gap": 0.5}, int(w == 1.0)) for w in (float(rng.random() > 0.5) for _ in range(60))]
    d0 = b.model_p({"whale": 1.0}) - b.model_p({"whale": 0.0})
    for i in range(0, 60, 10):
        b.learn(samples[i:i + 10])
    assert b.model_p({"whale": 1.0}) - b.model_p({"whale": 0.0}) > d0 and b.alpha() > 0
    assert Brain(cfg, {"closed": [{"y": 0}] * 20}).threshold() > cfg.get("run.min_score")
    assert Brain(cfg, {"closed": [{"y": 1}] * 20}).threshold() < cfg.get("run.min_score")
    old = {"closed": [], "model": {"theta": {"sector": 1.0}, "theta0": {"sector": 1.0}, "bias": -3.0, "bias0": -3.0, "n": 0}}
    Brain(cfg, old).score({"sector": 0.5, "context": 0.4})  # ترحيل نموذج قديم لا يكسر


def _t_bridge():
    cfg = load_config()
    st = {"chains": {"ethereum": {"hist": [[NOW0 - 6 * 3600, 5e10, 1.0e11]]}}}
    f = bridge_update(st, {"ethereum": 5.1e10}, {"ethereum": 1.006e11}, NOW0, cfg)["ethereum"]
    assert f["flag"] and f["d6_usd"] > 3e7 and 0 < f["score"] <= 1
    assert bridge_update({}, {"ethereum": 5e10}, {"ethereum": 1e11}, NOW0, cfg)["ethereum"]["score"] is None


def _t_whale_bot():
    cfg = _st_cfg()
    cfg.whales["exchanges"] = {"Binance": "0x" + "cd" * 20}
    cfg.whales["wallets"] = [{"label": "MM", "chain": "ethereum", "address": "0x" + "ab" * 20, "weight": 0.5},
                             {"label": "bad", "chain": "ethereum", "address": "0x123"}]
    tx = [{"timeStamp": str(int(NOW0) - 60), "from": "0x" + "cd" * 20, "to": "0x" + "ab" * 20, "value": str(int(200000 * 1e18)),
           "tokenDecimal": "18", "tokenSymbol": "TOK", "contractAddress": "0xTOK"},
          {"timeStamp": str(int(NOW0) - 90), "from": "0xs", "to": "0x" + "ab" * 20, "value": "5", "tokenDecimal": "18",
           "tokenSymbol": "USDT", "contractAddress": "0xusdt"}]
    pair = [{"chainId": "ethereum", "baseToken": {"address": "0xtok", "symbol": "TOK", "name": "Tok"}, "priceUsd": "2.0",
             "liquidity": {"usd": 900000}, "marketCap": 5e7, "fdv": 6e7, "volume": {}, "txns": {}, "priceChange": {}}]
    wb = WhaleBot(cfg, _FakeXfer(tx, tx), None, _FakeDex(1.0, pair), {"wallets": {"evm": {}, "sol": {}}})
    assert len(wb.wallets) == 1
    ev = wb.scan_wallets(NOW0)
    assert len(ev) == 1 and ev[0]["kind"] == "exchange_outflow" and abs(ev[0]["usd"] - 400000) < 1 and ev[0]["weight"] == 0.5
    assert wb.scan_wallets(NOW0) == []
    fl = wb.token_exchange_flow("ethereum", "0xtok", 2.0, NOW0)
    assert abs(fl["out_usd"] - 400000) < 1


def _t_exchange_parsers():
    n = 60

    def rows_binance():
        return [{"symbol": f"C{i}USDT", "lastPrice": "2", "openPrice": "1.9", "quoteVolume": "1000000"} for i in range(n)] + \
               [{"symbol": "BTC3LUSDT", "lastPrice": "1", "openPrice": "1", "quoteVolume": "9"}, {"symbol": "ETHBTC", "lastPrice": "1"}]

    for name in ("binance_vision", "mexc"):
        ex = Exchanges(_FakeHttp({"ticker/24hr": rows_binance(),
                                   "klines": [[1700000000000 + i * 3600000, "1", "2", "0.5", "1.5", "10", 0, "15"] for i in range(3)],
                                   "depth": {"bids": [["1", "5"]], "asks": [["2", "3"]]}}), [name])
        t = ex.tickers()
        assert len(t) == n and abs(t["C0"]["ch24"] - 5.263) < 0.01 and "BTC3L" not in t and ex.src == name
        k = ex.klines("C0", limit=3)
        assert len(k) == 3 and k[0][4] == 1.5 and k[0][5] == 15.0
        assert ex.orderbook("C0")["asks"] == [(2.0, 3.0)]
    ex = Exchanges(_FakeHttp({"spot/tickers": [{"currency_pair": f"C{i}_USDT", "last": "2", "change_percentage": "3.5", "quote_volume": "1e6"} for i in range(n)],
                               "candlesticks": [[str(1700000000 + i * 3600), "15", "1.5", "2", "0.5", "1", "10", "true"] for i in range(3)],
                               "order_book": {"asks": [["2", "3"]], "bids": [["1", "5"]]}}), ["gate"])
    t = ex.tickers()
    assert len(t) == n and t["C1"]["ch24"] == 3.5
    k = ex.klines("C0", limit=3)
    assert k[0][1] == 1.0 and k[0][4] == 1.5 and k[0][5] == 15.0 and k[0][0] == 1700000000000
    ex = Exchanges(_FakeHttp({"allTickers": {"data": {"ticker": [{"symbol": f"C{i}-USDT", "last": "2", "changeRate": "0.035", "volValue": "1e6"} for i in range(n)]}},
                               "market/candles": {"data": [[str(1700007200 - i * 3600), "1", "1.5", "2", "0.5", "10", "15"] for i in range(3)]},
                               "level2_100": {"data": {"asks": [["2", "3"]], "bids": [["1", "5"]]}}}), ["kucoin"])
    t = ex.tickers()
    assert len(t) == n and abs(t["C0"]["ch24"] - 3.5) < 1e-9
    k = ex.klines("C0", limit=3)
    assert [r[0] for r in k] == sorted(r[0] for r in k) and k[0][4] == 1.5
    assert Exchanges(_FakeHttp({}), ["gate"]).tickers() == {}  # فشل كل المصادر لا يرمي استثناء


def _t_transfers_news_llm():
    bs = Blockscout(_FakeHttp({"token-transfers": {"items": [{
        "timestamp": "2026-10-01T10:00:00.000000Z", "from": {"hash": "0xa"}, "to": {"hash": "0xb"},
        "token": {"address_hash": "0xtok", "symbol": "TOK", "type": "ERC-20"}, "total": {"value": "1000000", "decimals": "6"}}]}}))
    r = bs.wallet_transfers("base", "0xb")
    assert r and r[0]["tokenSymbol"] == "TOK" and r[0]["tokenDecimal"] == "6" and int(r[0]["timeStamp"]) > 1_700_000_000
    assert Transfers(None, bs).supports("base") and not Transfers(None, bs).supports("bsc")
    rss = ("<rss><channel><item><title>Ondo Finance expands RWA with ONDO listing</title>"
           "<pubDate>Wed, 01 Oct 2026 10:00:00 GMT</pubDate></item><item><title>Unrelated news</title></item></channel></rss>")
    items = News(_FakeHttp({"feed": rss}), ["https://x/feed"]).fetch()
    assert len(items) == 2
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    hit = News.match(items, Coin("ondo", "ONDO", "Ondo Finance", 1, 1, 1), 24, now)
    assert len(hit) == 1
    p = LLMJudge.parse('```json\n{"verdict":"go","confidence":80,"note":"جيدة"}\n```')
    assert p["verdict"] == "go" and LLMJudge.parse("garbage") is None and LLMJudge.parse('{"verdict":"x"}') is None


def _t_llm_in_pipeline(tmp):
    class J:
        def review(self, s):
            assert "LAG" in s
            return {"verdict": "go", "confidence": 60.0, "note": "ملاحظة اختبار"}

    eng, svc, state, _ = _st_engine(tmp, judge=J())
    eng.run()
    m = [x for x in svc.notifier.sent if "إشارة شراء" in x]
    assert m and "المراجعة الذكية" in m[0] and "ملاحظة اختبار" in m[0]

    class Caution:
        def review(self, s):
            return {"verdict": "caution", "confidence": 60.0, "note": "ضعيف"}

    import tempfile as _tf
    engc, svcc, _, _ = _st_engine(_tf.mkdtemp(), judge=Caution())
    engc.run()  # 55 - 8 < 50 => يُرفض بعد المراجعة
    assert not [x for x in svcc.notifier.sent if "إشارة شراء" in x]

    class Skip:
        def review(self, s):
            return {"verdict": "skip", "confidence": 90.0, "note": "x"}

    eng2, svc2, _, _ = _st_engine(_tf.mkdtemp(), judge=Skip())
    eng2.run()
    assert not [x for x in svc2.notifier.sent if "إشارة شراء" in x]


def _t_discovery():
    class E:
        def block_by_time(self, cid, ts):
            return 100

        def token_transfers(self, cid, address=None, contract=None, offset=100, startblock=None, endblock=None, sort="desc"):
            amt = str(int(100_000 * 1e18))
            return [{"timeStamp": "1", "from": "0xpair", "to": "0x" + "11" * 20, "value": amt, "tokenDecimal": "18"},
                    {"timeStamp": "2", "from": "0x" + "11" * 20, "to": "0x" + "22" * 20, "value": str(int(60_000 * 1e18)), "tokenDecimal": "18"}]

    cfg = _st_cfg()
    d = {}
    disc = Discovery(cfg, d, E())
    pos = {"ts": NOW0, "entry": 1.0, "symbol": "AAA", "contract": {"chain": "ethereum", "addr": "0xtok", "pair": "0xpair"}}
    assert disc.on_win(pos) >= 1 and not disc.wallets()
    disc.on_win({**pos, "symbol": "BBB"})
    w = disc.wallets()
    assert w and all(x["chain"] == "ethereum" and valid_wallet(x) for x in w)


def _t_backtest():
    cfg = _st_cfg(backtest={"candles": 600, "pages": 1, "top_per_group": 10, "min_trades": 1})
    cfg.sectors = {"s1": {"label": "S1", "category": "cat-aa"}, "s2": {"label": "S2", "category": "cat-bb"},
                   "s3": {"label": "S3", "category": "cat-cc"}}
    cfg.ecosystems = {"e1": {"label": "E1", "native": "nat-dd", "category": "cat-dd", "chain": "X"}}

    class CG:
        def markets(self, category=None, ids=None, per_page=60, sparkline=True):
            if ids:
                return [Coin(i, "DDN", "N", 10.0, 1e9, 1e8) for i in ids]
            p = category.split("-")[1].upper()[:2]
            return [Coin(f"{p}{i}", f"{p}{i}", f"{p}{i}", 10.0, 1e8, 1e7 + i) for i in range(10)]

    k = run_backtest(cfg, CG(), _FakeEx(), say=lambda *_: None)
    assert k["relations"] and k["sector_flow"] and "backtest" in k and k["backtest"]["n_events"] >= 0
    rel = next(iter(k["relations"].values()))["sectors"]
    assert all(set(v) >= {"corr", "beta", "lag_h"} for v in rel.values())
    json.dumps(k)
    K = Knowledge(k)
    assert K.ready and K.mfe() is None or K.mfe()["p75"] > 0
    assert Knowledge({}).sector_rel("X", "y") is None and not Knowledge({}).ready


def _t_state(tmp):
    p = Path(tmp) / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    s = State(p)
    assert s.data["meta"]["runs"] == 0
    s.save()
    assert json.loads(p.read_text(encoding="utf-8"))["version"] == 1


def _t_messages(tmp):
    coin = Coin("a", "A<B>", "Evil & Co", 0.000123, 1e7, 1e6, 1.0, -2.0, 3.0)
    cand = Candidate(coin=coin, kinds={"sector", "whale"}, sectors=["RWA"], reasons=[(3, "سبب <b>")], meta={"url": "https://x.io/?a=1&b=2"},
                     features={"whale": 0.9, "dex": 0.8, "vol": 0.7})
    plan = {"targets": [{"k": 1, "price": 0.00014, "pct": 14.0, "basis": "مقاومة"}, {"k": 2, "price": 0.0002, "pct": 62.0, "basis": "تدفق"}],
            "stop": 0.00011, "stop_pct": 10.6, "stop_basis": "دعم", "rr": [1.3, 5.8], "walls_up": [{"price": 0.00016, "usd": 90000, "dist_pct": 30.0}],
            "walls_dn": [], "flow_usd": 500000, "has_book": True}
    txt = format_signal(cand, 77.0, plan, "Europe/Istanbul", {"history": ["معلومة"], "llm": "ok"})
    assert "&lt;B&gt;" in txt and "Evil &amp; Co" in txt and "0.000123" in txt and "77/100" in txt and "▰▰▰▰▰▰▰▰▱▱" in txt
    assert len(txt) < 4000 and "الثقة: <b>عالية</b>" in txt
    assert "تحذير" in format_warning("A", "dump", ["x"], False, "UTC") and format_close({"symbol": "A", "y": 1, "entry": 1.0, "hit": 2}, 5.0, "trail", 3.0)


def _t_stablecoin():
    assert is_stable(Coin("usd-coin", "USDC", "USDC", 1.0, 3e10, 5e9, 0.0, 0.01, 0.0))
    assert is_stable(Coin("x", "XYZ", "Xyz", 1.001, 5e8, 5e7, 0.0, 0.1, 0.2))
    assert not is_stable(Coin("y", "REAL", "Real", 1.0, 5e8, 5e7, 3.0, 12.0, 30.0))


def _t_secrets(tmp):
    os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = " 123:abc \n", "   "
    try:
        cfg = Config({"sectors": {}, "ecosystems": {}, "whales": {}})
        assert cfg.env["TELEGRAM_BOT_TOKEN"] == "123:abc" and cfg.env["TELEGRAM_CHAT_ID"] is None
    finally:
        del os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]


def _t_resilience(tmp):
    """مزوّد يرمي استثناءات في كل مكان: الرادار يكمل ويسجل الأخطاء بدل الانهيار."""
    class Boom:
        def __getattr__(self, name):
            def f(*a, **k):
                raise RuntimeError("boom")
            return f

    svc = Services(cg=Boom(), llama=Boom(), dex=Boom(), ex=Boom(), notifier=_FakeNotifier())
    eng = Engine(_st_cfg(), State(Path(tmp) / "r.json"), svc, now_fn=lambda: NOW0)
    s = eng.run()
    assert s["errors"] and s["signals"] == []


# ---------------------------------------------------------------------- اختبارات الوضع المجاني والتوقع
class _FakePaprika:
    bulk = True

    def __init__(self, rows, tag_ids):
        self.rows, self.tag_ids, self.calls = rows, tag_ids, 0

    def snapshot(self):
        self.calls += 1
        return [c for c in (Coin.from_api(r) for r in self.rows) if c]

    def tags(self):
        return [{"id": "real-world-assets", "name": "Real World Assets (RWA)", "coins": list(self.tag_ids)},
                {"id": "unrelated", "name": "Something else", "coins": ["x1"]}]

    def tag_coins(self, tag_id):
        return list(self.tag_ids)

    def prices(self, ids):
        return {}

    def trending(self):
        return set()

    def category_ids(self):
        return []


class _BulkEx(_FakeEx):
    def __init__(self, tickers, book, rows):
        super().__init__(tickers, book)
        self.by_sym = {r["symbol"].upper(): r["sparkline_in_7d"]["price"] for r in rows}

    def klines(self, base, limit=500, pages=1):
        sp = self.by_sym.get(base)
        if not sp:
            return []
        return [[1_700_000_000_000 + i * 3600_000, p, p * 1.003, p * 0.997, p, 5e5] for i, p in enumerate(sp)]


def _t_bulk_pipeline(tmp):
    rows = _st_hot(1)
    lag = Coin.from_api(rows[-1]).price
    cg = _FakePaprika(rows, [r["id"] for r in rows])
    ex = _BulkEx(_st_tickers(rows), _st_book(lag), rows)
    cfg = _st_cfg()
    svc = Services(cg=cg, llama=_FakeLlama(), dex=_FakeDex(lag), ex=ex, notifier=_FakeNotifier())
    eng = Engine(cfg, State(Path(tmp) / "s.json"), svc, now_fn=lambda: NOW0)
    s = eng.run()
    assert not s["errors"], s["errors"]
    assert s["sectors"]["rwa"]["hot"], list(s["sectors"])
    assert eng.state.data["kl"], "لم تُبنَ الشموع"
    assert [m for m in svc.notifier.sent if "LAG" in m and "إشارة شراء" in m], s["near_miss"]
    assert eng.budget.monthly == float(cfg.get("paprika.monthly_budget")) and cg.calls == 1
    eng2 = Engine(cfg, eng.state, svc, now_fn=lambda: NOW0 + 600)  # تشغيلة ثانية: عضوية القطاعات من الكاش
    eng2.run()
    assert cg.calls == 2


def _t_membership_tags():
    t = {"id": "real-world-assets", "name": "Real World Assets (RWA)"}
    assert tag_matches(["rwa"], t) and tag_matches(["real world"], t) and not tag_matches(["meme"], t)
    assert tag_matches(["zero knowledge", "zk"], {"id": "zk-rollups", "name": "ZK Rollups"})
    assert not tag_matches(["ai"], {"id": "chain", "name": "Main chain"})


def _t_rotation():
    cfg = _st_cfg()
    now = NOW0
    coins = []
    for i, ch in enumerate([8, 7, 6, 3, 2.5, 2, 1, 0.5, 0.2]):  # كبار يتحركون والصغار ما زالوا
        coins.append(Coin(f"c{i}", f"C{i}", f"C{i}", 1.0, 1e9 / (i + 1), 1e7, 0.2, ch, 1.0))
    tstat = tercile_stats(coins)
    assert tstat["large24"] > tstat["small24"] + 3
    st = {"heat": 6.0, "breadth": 0.8, **tstat}
    assert wave_stage(st, []) in ("leaders", "broadening")
    assert wave_stage({"heat": 6.0, "breadth": 0.7, "large24": 6.0, "mid24": 5.0, "small24": 4.5, "large1": -0.5}, []) == "late"
    assert wave_stage({"heat": 1.5, "breadth": 0.4, "large24": 0.0, "mid24": 0.0, "small24": 0.0}, [[0, 9.0, 0.8]]) == "cooling"
    stats = {"a": {"label": "A", "heat": 8.0, "breadth": 0.8, "stage": "broadening", "hot_since_h": 3.0},
             "b": {"label": "B", "heat": 0.5, "breadth": 0.5, "stage": "quiet"},
             "c": {"label": "C", "heat": 9.0, "breadth": 0.8, "stage": "running"}}
    know = Knowledge({"sector_flow": {"a": [{"to": "b", "lag_h": 6, "corr": 0.3}, {"to": "c", "lag_h": 4, "corr": 0.4}]}})
    fc = rotation_forecast(stats, know, cfg, now)
    assert [f["to"] for f in fc] == ["b"] and fc[0]["src"] == "history" and fc[0]["conf"] >= 0.25  # c ساخن أصلاً فلا يُتوقّع
    cfg2 = _st_cfg()
    cfg2.settings["rotation"]["prior_pairs"] = [["a", "b"]]
    stats["a"]["hot_since_h"] = 8.0  # داخل النافذة المعتادة (12س × [0.5 ، 2])
    fp = rotation_forecast(stats, Knowledge({}), cfg2, now)
    assert fp and fp[0]["src"] == "prior" and fp[0]["conf"] < fc[0]["conf"]
    msg = format_rotation(fc[0], "A", "B", [(coins[8], 1.8)], "UTC")
    assert "توقّع انتقال السيولة" in msg and "بعد ~3" in msg and len(msg) < 4000


def _t_rotation_pipeline(tmp):
    eng, svc, state, _ = _st_engine(tmp)
    eng.K = Knowledge({"sector_flow": {"rwa": [{"to": "zk", "lag_h": 6, "corr": 0.3}]}})
    s = eng.run()
    assert not s["errors"], s["errors"]
    assert [m for m in svc.notifier.sent if "توقّع انتقال السيولة" in m], (s.get("rotation"), {k: v.get("stage") for k, v in s["sectors"].items()})
    eng2 = Engine(eng.cfg, state, svc, now_fn=lambda: NOW0 + 600)
    eng2.K = eng.K
    eng2.run()
    assert sum("توقّع انتقال السيولة" in m for m in svc.notifier.sent) == 1, "تكرار التنبيه رغم فترة الانتظار"


def _t_hardening():
    assert redact("/bot123:ABC/sendMessage") == "/bot***/sendMessage"
    assert safe_url("https://x.io/a") and not safe_url("javascript:alert(1)") and not safe_url("http://x.io")
    lines = [f"<b>{i}</b> " + "ب" * 60 for i in range(100)]
    out = safe_join(lines, 3900)
    assert len(out) <= 3900 and out.count("<b>") == out.count("</b>")
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'

    class H:
        def get_text(self, url, retries=2):
            return bomb

    assert News(H(), ["https://f.io/x"]).fetch() == []
    st = default_state()
    st["kl"] = {f"S{i}": {"ts": 0, "c": [1.0] * 168} for i in range(10)}
    assert "kl" in st


def _t_backtest_keyless(tmp):
    rows = _st_hot(1)
    cfg = _st_cfg(backtest={"candles": 170, "pages": 1, "top_per_group": 12, "min_trades": 1})
    cfg.sectors = {"rwa": cfg.sectors["rwa"]}
    cfg.ecosystems = {}
    svc = Services(cg=_FakePaprika(rows, [r["id"] for r in rows]), llama=_FakeLlama(), dex=_FakeDex(1.0),
                   ex=_BulkEx(_st_tickers(rows), None, rows), notifier=_FakeNotifier())
    eng, snap, mem = _bulk_context(cfg, svc)
    assert snap and len(mem["sectors"]["rwa"]) >= 6, mem["tags"]
    ad = _BulkAdapter(cfg, eng, snap, mem)
    got = ad.markets(category=cfg.sectors["rwa"]["category"])
    assert len(got) >= 6 and all(c.mcap >= 1e7 for c in got)


def run_offline_selftest(verbose: bool = True) -> tuple:
    import tempfile
    tmp = tempfile.mkdtemp()
    checks = [("الإعدادات (config.yaml)", _t_config, False), ("التحليل: الارتباط والعملات المتأخرة", _t_analytics, False),
              ("محرك الأهداف: مقاومات وجدران وسيولة", _t_targets, False), ("خط الإنتاج الكامل + رسالة تيليجرام", _t_pipeline, True),
              ("دورة حياة الصفقة: T1 ← وقف متحرك ← إغلاق", _t_lifecycle, True), ("إغلاق بالوقف وتعلّم", _t_stop_loss, True),
              ("كشف تصريف الحيتان (إيداع منصة)", _t_distribution, True), ("تحذير ضعف الزخم لصفقة مفتوحة", _t_position_warning, True),
              ("ميزانية CoinGecko والكاش", _t_budget_cache, True), ("الطبقة اللحظية ورفض الرموز المتضاربة", _t_live_overlay, True),
              ("العقل المتعلّم والحد المتكيّف", _t_brain, False), ("تدفق المستقرات (الجسور)", _t_bridge, False),
              ("بوت الحيتان", _t_whale_bot, False), ("قراءة ردود البورصات (4 مصادر)", _t_exchange_parsers, False),
              ("Blockscout والأخبار ومراجعة LLM", _t_transfers_news_llm, False), ("مراجعة LLM داخل الخط", _t_llm_in_pipeline, True),
              ("اكتشاف المحافظ الذكية", _t_discovery, False), ("الاختبار الرجعي وحفظ المعرفة", _t_backtest, False),
              ("الحالة التالفة", _t_state, True), ("تنسيق الرسائل وسلامة HTML", _t_messages, True),
              ("فلتر العملات المستقرة", _t_stablecoin, False), ("تنظيف الأسرار", _t_secrets, True),
              ("المرونة: انهيار كل المزوّدين", _t_resilience, True),
              ("الوضع المجاني بلا مفتاح (CoinPaprika + شموع البورصات)", _t_bulk_pipeline, True),
              ("مطابقة وسوم القطاعات", _t_membership_tags, False), ("مراحل الموجة وتوقع انتقال السيولة", _t_rotation, False),
              ("الاختبار الرجعي بالوضع المجاني", _t_backtest_keyless, True),
              ("توقع الانتقال داخل الخط الكامل بلا تكرار", _t_rotation_pipeline, True), ("التحصين الأمني (أسرار، HTML، XML)", _t_hardening, False)]
    passed, failed = 0, []
    for name, fn, needs_tmp in checks:
        sub = tempfile.mkdtemp(dir=tmp)
        try:
            logging.disable(logging.CRITICAL)
            fn(sub) if needs_tmp else fn()
            passed += 1
            if verbose:
                print(f"  ✅ {name}")
        except Exception as exc:
            failed.append((name, exc))
            if verbose:
                print(f"  ❌ {name}\n     {exc.__class__.__name__}: {str(exc)[:300]}")
                traceback.print_exc(limit=3, file=sys.stdout)
        finally:
            logging.disable(logging.NOTSET)
    return passed, failed


def run_live_checks(cfg, http) -> list:
    """فحوص اتصال حقيقية بالمزوّدين. تعيد [(الحالة, الاسم, تفصيل)] حيث الحالة ok / warn / fail."""
    env, out = cfg.env, []

    def add(status, name, detail=""):
        out.append((status, name, detail))

    prov = make_market_provider(cfg, http)
    ids = set()
    if getattr(prov, "bulk", False):
        snap = prov.snapshot()
        tags = prov.tags()
        add("ok" if len(snap) > 100 else "fail", "CoinPaprika (مجاني بلا مفتاح)",
            f"{len(snap)} عملة، {len(tags)} وسماً" if snap else "لا رد من api.coinpaprika.com")
    else:
        ids = set(prov.category_ids())
    if not getattr(prov, "bulk", False) and not ids:
        add("fail", "CoinGecko", "لا رد (مفتاح خاطئ أو 429؟ تأكد من COINGECKO_API_KEY)" if env.get("COINGECKO_API_KEY")
            else "لا رد — أضف COINGECKO_API_KEY أو اترك data_source: auto ليستخدم المصدر المجاني")
    elif not getattr(prov, "bulk", False):
        add("ok", "CoinGecko", f"{len(ids)} فئة" + ("" if env.get("COINGECKO_API_KEY") else " (بلا مفتاح: حدود ضيقة)"))
        bad = [f"{k}:{s['category']}" for k, s in cfg.sectors.items() if s["category"] not in ids]
        bad += [f"eco:{k}:{e['category']}" for k, e in cfg.ecosystems.items() if e["category"] not in ids]
        add("ok" if not bad else "warn", "فئات القطاعات", "كلها صالحة" if not bad else f"{len(bad)} غير صالحة وستُتجاهل: {', '.join(bad[:12])}")
    ex = Exchanges(http, cfg.get("live.exchanges"))
    t = ex.tickers()
    add("ok" if t else "fail", "بورصات (أسعار لحظية)", f"{ex.src}: {len(t)} زوج" if t else "كل المصادر فشلت — ستعمل الأداة على CoinGecko فقط")
    if t:
        add("ok" if ex.klines("BTC", limit=50) else "warn", "بورصات (شموع للاختبار الرجعي)")
        add("ok" if ex.orderbook("BTC") else "warn", "بورصات (دفتر الأوامر للأهداف)")
    dl = DefiLlama(http)
    add("ok" if dl.chains_tvl() else "warn", "DefiLlama (TVL)")
    add("ok" if dl.stablecoin_supply() else "warn", "DefiLlama (مستقرات الشبكات)")
    add("ok" if dl.coin_prices(["bitcoin"]) else "warn", "DefiLlama (أسعار مجانية)")
    add("ok" if dl.protocols() else "warn", "DefiLlama (بروتوكولات TVL)")
    add("ok" if DexScreener(http).search("SOL") else "warn", "DexScreener")
    wl = [w for w in cfg.whales.get("wallets") or [] if valid_wallet(w) and w["chain"] != "solana"]
    bs = Blockscout(http)
    probe = next((w for w in wl if w["chain"] in BLOCKSCOUT), None)
    if probe:
        r = bs.wallet_transfers(probe["chain"], probe["address"])
        add("ok" if r is not None else "warn", "Blockscout (محافظ مجاناً)", f"{probe['chain']}: {len(r or [])} تحويل")
    if env.get("ETHERSCAN_API_KEY") and wl:
        r = Etherscan(http, env["ETHERSCAN_API_KEY"]).token_transfers(1, address=next(w["address"] for w in wl if w["chain"] == "ethereum"))
        add("ok" if r is not None else "fail", "Etherscan", "المفتاح يعمل" if r is not None else "المفتاح مرفوض أو تجاوزت الحد")
    elif not env.get("ETHERSCAN_API_KEY"):
        add("warn", "Etherscan", "بلا مفتاح: ستُستخدم Blockscout فقط (اكتشاف المحافظ التلقائي معطّل)")
    if any(w.get("chain") == "solana" for w in cfg.whales.get("wallets") or []):
        d = http.post_json(env.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com",
                           {"jsonrpc": "2.0", "id": 1, "method": "getVersion"}, retries=2)
        ok = isinstance(d, dict) and "result" in d
        add("ok" if ok else "warn", "Solana RPC" + (" (Helius)" if env.get("SOLANA_RPC_URL") else " (عام)"),
            "" if ok else "لا رد — أضف SOLANA_RPC_URL من Helius")
    news = News(http).fetch()
    add("ok" if news else "warn", "أخبار RSS", f"{len(news)} عنوان")
    tok = env.get("TELEGRAM_BOT_TOKEN")
    if tok:
        d = http.get_json(f"https://api.telegram.org/bot{tok}/getMe", retries=1)
        add("ok" if isinstance(d, dict) and d.get("ok") else "fail", "Telegram (التوكن)")
        add("ok" if env.get("TELEGRAM_CHAT_ID") else "fail", "Telegram (chat id)")
    else:
        add("fail", "Telegram", "TELEGRAM_BOT_TOKEN مفقود (Repository secrets)")
    add("ok" if env.get("ANTHROPIC_API_KEY") else "warn", "مراجعة LLM", "مفعّلة" if env.get("ANTHROPIC_API_KEY") else "اختيارية: بلا ANTHROPIC_API_KEY")
    return out


# ======================================================================
# cli
# ======================================================================
log = logging.getLogger("radar")
DEFAULT_STATE = os.environ.get("RADAR_STATE", ".radar_state/state.json")
HOST_INTERVALS = {"api.etherscan.io": 0.25, "api.dexscreener.com": 0.25, "api.mainnet-beta.solana.com": 1.0,
                  "data-api.binance.vision": 0.1, "api.mexc.com": 0.1, "api.gateio.ws": 0.1, "api.kucoin.com": 0.1,
                  "eth.blockscout.com": 0.4, "base.blockscout.com": 0.4, "optimism.blockscout.com": 0.4,
                  "arbitrum.blockscout.com": 0.4, "polygon.blockscout.com": 0.4, "coins.llama.fi": 0.2, "api.coinpaprika.com": 0.2, "api.llama.fi": 0.2}


def make_http(cfg) -> Http:
    interval = cfg.get("network.coingecko_interval_key" if cfg.env.get("COINGECKO_API_KEY")
                       else "network.coingecko_interval_free")
    return Http(intervals={**HOST_INTERVALS, "api.coingecko.com": interval, "pro-api.coingecko.com": interval})


def make_market_provider(cfg, http):
    """auto: CoinGecko إن وُجد مفتاح، وإلا CoinPaprika المجاني بلا مفتاح."""
    src = str(cfg.get("data_source")).lower()
    key = cfg.env.get("COINGECKO_API_KEY")
    if src == "coingecko" or (src == "auto" and key):
        return CoinGecko(http, key, cfg.env.get("COINGECKO_PLAN") or "demo")
    return Paprika(http)


def make_services(cfg, http, dry_run: bool = False, notifier=None) -> Services:
    env = cfg.env
    eth = Etherscan(http, env["ETHERSCAN_API_KEY"]) if env.get("ETHERSCAN_API_KEY") else None
    has_sol = any(str(w.get("chain")).lower() == "solana" for w in cfg.whales.get("wallets") or [])
    sol = SolanaRPC(http, env.get("SOLANA_RPC_URL")) if has_sol else None
    judge = None
    llm = cfg.get("context.llm")
    if env.get("ANTHROPIC_API_KEY") and str(llm.get("enabled", "auto")).lower() in ("auto", "true"):
        judge = LLMJudge(http, env["ANTHROPIC_API_KEY"], llm["model"])
    if notifier is None:
        token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if dry_run or not (token and chat):
            if not dry_run:
                missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env.get(k)]
                log.warning("الأسرار المفقودة: %s -> وضع تجريبي (الرسائل تُطبع فقط). أضفها في Settings > Secrets and "
                            "variables > Actions > Repository secrets بنفس الاسم تماماً", ", ".join(missing))
            notifier = ConsoleNotifier()
        else:
            notifier = Telegram(http, token, chat)
        if token and ":" not in token:
            log.warning("TELEGRAM_BOT_TOKEN شكله غير صحيح (التوكن الصحيح يحوي نقطتين :)")
    return Services(cg=make_market_provider(cfg, http),
                    llama=DefiLlama(http), dex=DexScreener(http),
                    ex=Exchanges(http, cfg.get("live.exchanges")) if cfg.get("live.enabled") else None,
                    xfer=Transfers(eth, Blockscout(http)), eth=eth, sol=sol, news=News(http), judge=judge,
                    notifier=notifier, know=Knowledge.load())


def capabilities(cfg, svc) -> list:
    env = cfg.env
    return [("أسعار لحظية (بورصات)", bool(svc.ex)), ("Etherscan", bool(svc.eth)), ("Blockscout (مجاني)", True),
            ("Helius/Solana", bool(svc.sol) and bool(env.get("SOLANA_RPC_URL"))), ("أخبار RSS", bool(svc.news)),
            ("مراجعة LLM", bool(svc.judge)), ("تيليجرام", isinstance(svc.notifier, Telegram)),
            ("معرفة تاريخية", svc.know.ready),
            ("مصدر السوق", True if getattr(svc.cg, "bulk", False) else bool(env.get("COINGECKO_API_KEY")))]


def cmd_run(args) -> int:
    cfg = load_config()
    errors, warns = validate_config(cfg)
    for w in warns:
        log.warning("config: %s", w)
    if errors:
        for e in errors:
            log.error("config: %s", e)
        log.error("أوقفت التشغيل: أصلح config.yaml (أو شغّل: python radar.py selftest)")
        return 2
    http = make_http(cfg)
    svc = make_services(cfg, http, dry_run=args.dry_run)
    log.info("القدرات: %s", " | ".join(f"{n} {'✓' if ok else '✗'}" for n, ok in capabilities(cfg, svc)))
    state = State(args.state)
    engine = Engine(cfg, state, svc, dry_run=args.dry_run)
    try:
        s = engine.run()
        log.info("تم: قطاعات=%d | إشارات=%s | تحذيرات=%s | أقرب=%s | حد=%s | أسعار=%s | أخطاء=%d", len(s["sectors"]),
                 s["signals"], s["warnings"], s["near_miss"][:3], s.get("threshold"), s.get("live"), len(s["errors"]))
        engine.write_step_summary()
        return 0
    finally:
        state.save()  # نحفظ الحالة حتى لو حدث خطأ متأخر


def _print_live(rows: list) -> int:
    icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    for status, name, detail in rows:
        print(f"  {icon[status]} {name}" + (f" — {detail}" if detail else ""))
    return sum(1 for r in rows if r[0] == "fail")


def cmd_check(args) -> int:
    cfg = load_config()
    http = make_http(cfg)
    print("— فحص الاتصال بالمزوّدين —")
    fails = _print_live(run_live_checks(cfg, http))
    svc = make_services(cfg, http, dry_run=True)
    if getattr(svc.cg, "bulk", False):
        u = estimate_paprika_usage(cfg)
        print(f"— حساب CoinPaprika (مجاني بلا مفتاح): ~{u['per_month']} طلب/شهر من {u['limit']} → {'آمن ✅' if u['ok'] else '⚠️'}")
        print("— عضوية القطاعات (الوسوم المطابقة وعدد العملات) —")
        eng, snap, mem = _bulk_context(cfg, svc)
        byid = {c.id: c for c in snap}
        empty = 0
        for k in cfg.sectors:
            n = len(eng._group_coins(mem["sectors"].get(k, []), byid))
            empty += n < 6
            print(f"  {'✅' if n >= 6 else '⚠️ '} {k:12s} {n:3d} عملة  وسوم: {', '.join(mem['tags'].get(k, [])[:4]) or '—'}")
        if empty:
            print(f"  ⚠️ {empty} قطاع بأقل من 6 عملات سيُتجاهل. عدّل كلمات tags/llama في config.yaml لهذه القطاعات.")
    else:
        for lv in (True, False):
            u = estimate_cg_usage(cfg, lv)
            print(f"— حساب CoinGecko ({'مع' if lv else 'بدون'} الطبقة اللحظية): ~{u['per_month']} طلب/شهر "
                  f"مقابل ميزانيتك {int(u['budget'])} والحد المجاني {u['limit']} → {'آمن ✅' if u['ok'] else 'يتجاوز؛ سيعمل من الكاش ⚠️'}")
    return 1 if fails else 0


def cmd_selftest(args) -> int:
    print("═══ التحقق الذاتي من الكود (بيانات اصطناعية، بلا إنترنت) ═══")
    cfg = load_config()
    errors, warns = validate_config(cfg)
    for w in warns:
        print(f"  ⚠️  {w}")
    for e in errors:
        print(f"  ❌ {e}")
    passed, failed = run_offline_selftest(verbose=True)
    total = passed + len(failed)
    print(f"\nالنتيجة: {passed}/{total} فحص ناجح" + ("  ✅ الكود سليم" if not failed and not errors else "  ❌ يوجد خلل"))
    code = 1 if (failed or errors) else 0
    u = estimate_paprika_usage(cfg)
    print(f"  📊 CoinPaprika (المصدر المجاني بلا مفتاح): ~{u['per_month']} طلب/شهر من {u['limit']} → {'آمن ✅' if u['ok'] else '⚠️'}")
    for lv in (True, False):
        u = estimate_cg_usage(cfg, lv)
        print(f"  📊 CoinGecko (إن أضفت مفتاحاً، {'مع' if lv else 'بدون'} الطبقة اللحظية): ~{u['per_month']} طلب/شهر | ميزانيتك "
              f"{int(u['budget'])} | الحد المجاني {u['limit']} → {'آمن ✅' if u['ok'] else 'سيعمل من الكاش ⚠️'}")
    if not args.offline:
        print("\n═══ فحوص الاتصال الحي (معلوماتية) ═══")
        fails = _print_live(run_live_checks(cfg, make_http(cfg)))
        if fails and args.strict:
            code = 1
        print("\nملاحظة: ❌ هنا تعني مزوّداً/سراً غير جاهز، وليس خللاً في الكود." if fails else "\nكل المزوّدين المطلوبين يعملون ✅")
    return code


def cmd_test_telegram(args) -> int:
    cfg = load_config()
    tok, chat = cfg.env.get("TELEGRAM_BOT_TOKEN"), cfg.env.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("أضف TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID أولاً (Repository secrets)")
        return 1
    tg = Telegram(make_http(cfg), tok, chat)
    ok = tg.send("✅ <b>رادار السيولة</b> — اختبار الاتصال ناجح\nالرسائل التالية أمثلة لشكل التنبيهات (ليست إشارات حقيقية).")
    if ok:
        coin = Coin("demo", "DEMO", "Demo Token", 1.2345, 5e7, 3e6, 0.8, -1.2, 3.0, xvol=3.2e6, live=True)
        cand = Candidate(coin=coin, kinds={"whale", "waterfall", "sector"}, sectors=["مثال · RWA"],
                         meta={"url": "https://www.coingecko.com", "dex": {"liq": 6e5, "buys_h1": 70, "sells_h1": 20}},
                         features={"whale": 0.8, "stable": 0.5, "dex": 0.7, "attention": 0.6, "vol": 0.7},
                         reasons=[(0, "🐋 DWF Labs (مشتريات معلنة): تجميع $320K"),
                                  (2, "🌊 شلال Solana: SOL +9.0% (24س) ← DEMO لم تتحرك بعد (-1.2%)"),
                                  (3, "🔥 قطاع RWA ساخن +6.5% (اتساع 78%) والعملة متأخرة -1.2%"),
                                  (6, "🟢 ضغط شراء على DEX: 70 شراء / 20 بيع (1س)")])
        p = coin.price
        t = [{"k": 1, "price": p * 1.06, "pct": 6.0, "basis": "مقاومة 7 أيام (لُمست 2×)"},
             {"k": 2, "price": p * 1.17, "pct": 17.0, "basis": "سدّ فجوة القطاع/الشبكة"},
             {"k": 3, "price": p * 1.46, "pct": 46.0, "basis": "امتصاص تدفق ≈$320K لأوامر البيع"}]
        plan = {"targets": t, "stop": p * 0.94, "stop_pct": 6.0, "stop_basis": "تحت دعم 7 أيام", "rr": [1.0, 2.8, 7.7],
                "walls_up": [{"price": p * 1.07, "usd": 180000.0, "dist_pct": 7.0}],
                "walls_dn": [{"price": p * 0.96, "usd": 90000.0, "dist_pct": -4.0}], "ceiling": None,
                "flow_usd": 320000.0, "has_book": True}
        tz = cfg.get("run.timezone")
        tg.send(format_signal(cand, 74.0, plan, tz, {"xratio": 2.1, "history": ["تلحق قطاعها عادةً بعد ~6 ساعات (ارتباط تاريخي 0.62)"]}))
        tg.send(format_warning("DEMO", "dump", ["محفظة DWF Labs أودعت $410K من DEMO في منصة مركزية",
                                                "إيداع الحيتان في المنصات غالباً يسبق البيع"], True, tz))
    print("تم الإرسال" if ok else "فشل الإرسال: تحقق من التوكن والـ chat id (وأرسل /start للبوت أو أضف البوت مشرفاً في القناة)")
    return 0 if ok else 1


def _bulk_context(cfg, svc):
    """لقطة السوق وعضوية القطاعات في الوضع المجاني (للفحص والاختبار الرجعي)."""
    import tempfile
    st = State(os.path.join(tempfile.mkdtemp(), "radar_tmp.json"))
    eng = Engine(cfg, st, svc)
    eng._reset()
    now = time.time()
    eng.att, eng.budget = Attention(st.data, cfg, now), Budget(st.data, cfg, now, monthly=1e9)
    eng.protocols = eng._protocols(now)
    snap = eng._snapshot(now)
    mem = eng._membership(snap, now) if snap else {"sectors": {}, "eco": {}, "tags": {}}
    return eng, snap, mem


class _BulkAdapter:
    """يحاكي markets() الخاصة بـ CoinGecko فوق لقطة CoinPaprika (يستخدمه الاختبار الرجعي)."""

    def __init__(self, cfg, eng, snap, mem):
        self.cfg, self.eng, self.mem = cfg, eng, mem
        self.byid = {c.id: c for c in snap}
        self.best: dict = {}
        for c in snap:
            if c.symbol not in self.best or c.mcap > self.best[c.symbol].mcap:
                self.best[c.symbol] = c

    def markets(self, category=None, ids=None, per_page=60, sparkline=False):
        if category:
            for key, sc in self.cfg.sectors.items():
                if sc.get("category") == category:
                    return self.eng._group_coins(self.mem["sectors"].get(key, []), self.byid)[:per_page]
            for key, e in self.cfg.ecosystems.items():
                if e.get("category") == category:
                    return self.eng._group_coins(self.mem["eco"].get(key, []), self.byid)[:per_page]
            return []
        out = []
        for e in self.cfg.ecosystems.values():
            c = self.best.get(str(e.get("symbol") or "").upper())
            if c and e.get("native") in (ids or []):
                out.append(Coin(id=e["native"], symbol=c.symbol, name=c.name, price=c.price, mcap=c.mcap, volume=c.volume))
        return out


def cmd_backtest(args) -> int:
    cfg = load_config()
    http = make_http(cfg)
    svc = make_services(cfg, http, dry_run=True)
    if not svc.ex:
        print("❌ live.enabled=false: الاختبار الرجعي يحتاج البورصات العامة")
        return 1
    print("═══ الاختبار الرجعي: بناء معرفة الارتباط وانتقال السيولة ═══")
    provider = svc.cg
    if getattr(provider, "bulk", False):
        eng, snap, mem = _bulk_context(cfg, svc)
        if not snap:
            print("❌ تعذّر جلب لقطة السوق من CoinPaprika")
            return 1
        provider = _BulkAdapter(cfg, eng, snap, mem)
    know = run_backtest(cfg, provider, svc.ex)
    if not know:
        return 1
    Path(args.out).write_text(json.dumps(know, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    bt = know["backtest"]
    print(f"\n✅ حُفظت المعرفة في {args.out}: {len(know['relations'])} عملة، {know['hours']} ساعة")
    print(f"حالات محاكاة قاعدة التدوير: {bt['n_events']} | نسبة النجاح: {bt['win_rate']} | التوقع (R): {bt['expectancy_R']}")
    if bt.get("tuned"):
        print(f"العتبات المضبوطة: {bt['tuned']} | الصعود التاريخي: {bt.get('mfe_q')}")
    else:
        print("⚠️ لم يُثبت الاختبار ميزة إحصائية موجبة؛ أُبقيت عتباتك كما هي (هذه نتيجة صادقة وليست عطلاً)")
    for k, v in list(know["sector_flow"].items())[:6]:
        if v:
            print(f"  بعد {k} غالباً: " + "، ".join(f"{r['to']} (~{r['lag_h']}س)" for r in v))
    return 0


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
    print(f"محافظ مكتشفة تلقائياً: {len(st.data.get('discovery', {}))} | استهلاك CoinGecko: {st.data['cg']['used']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="radar", description="رادار شلال السيولة للكريبتو")
    ap.add_argument("--state", default=DEFAULT_STATE)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="تشغيلة واحدة (تستخدمها GitHub Actions)")
    r.add_argument("--dry-run", action="store_true", help="بدون إرسال تيليجرام")
    sub.add_parser("check", help="فحص الاتصال وحساب ميزانية CoinGecko")
    s = sub.add_parser("selftest", help="التحقق الذاتي من الكود + فحص المزوّدين")
    s.add_argument("--offline", action="store_true", help="بدون فحوص الإنترنت")
    s.add_argument("--strict", action="store_true", help="اعتبر فشل المزوّدين فشلاً")
    sub.add_parser("test-telegram", help="إرسال رسائل اختبار بشكل التنبيهات")
    b = sub.add_parser("backtest", help="الاختبار الرجعي وحفظ knowledge.json")
    b.add_argument("--out", default=str(KNOWLEDGE_PATH))
    sub.add_parser("stats", help="إحصائيات التعلّم الذاتي")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    cmd = args.cmd or "run"
    if cmd == "run" and not hasattr(args, "dry_run"):
        args.dry_run = False
    handlers = {"run": cmd_run, "check": cmd_check, "selftest": cmd_selftest, "test-telegram": cmd_test_telegram,
                "backtest": cmd_backtest, "stats": cmd_stats}
    return handlers[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
