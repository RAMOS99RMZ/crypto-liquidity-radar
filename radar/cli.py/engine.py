"""المحرّك: ينسّق كل البوتات ثم يقيّم ويرسل التنبيهات ويتعلّم من النتائج."""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from . import analytics as A
from .attention import Attention
from .bots import bridge_bot, sector_bot, waterfall_bot
from .bots.whale_bot import WhaleBot
from .brain import Brain
from .models import Candidate, Coin
from .notifier import format_outcome, format_signal, primary_kind
from .providers.defillama import norm_chain
from .providers.dexscreener import best_by_address, summarize_pair
from .providers.onchain import EVM_CHAIN_IDS

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


class Engine:
    def __init__(self, cfg, state, cg, llama, dex, eth, sol, notifier, now_fn=time.time, dry_run: bool = False):
        self.cfg, self.state = cfg, state
        self.cg, self.llama, self.dex, self.eth, self.sol = cg, llama, dex, eth, sol
        self.notifier = notifier
        self.now_fn = now_fn
        self.dry_run = dry_run
        self._vr_cache: dict = {}
        self.summary: dict = {"sectors": {}, "signals": [], "near_miss": [], "closed": 0}

    # ------------------------------------------------------------------ أدوات مساعدة
    def vr(self, coin: Coin) -> Optional[float]:
        """نسبة حجم العملة إلى خط أساسها (تُحدَّث مرة واحدة لكل عملة في التشغيلة)."""
        if coin.id not in self._vr_cache:
            self._vr_cache[coin.id] = self.att.coin(coin)
        return self._vr_cache[coin.id]

    def _universe(self, coins: list) -> list:
        u = self.cfg.get("universe")
        return [c for c in coins
                if c.mcap >= u["min_market_cap"] and c.volume >= u["min_volume_24h"] and not is_stable(c)]

    def _valid_categories(self, now: float):
        vc = self.state.data["valid_categories"]
        if now - vc.get("ts", 0) > 86400 or not vc.get("ids"):
            ids = self.cg.category_ids()
            if ids:
                vc["ts"], vc["ids"] = now, ids
        return set(vc["ids"]) if vc.get("ids") else None

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

        self._update_outcomes(now)

        valid = self._valid_categories(now)
        per_page = int(self.cfg.get("universe.per_page"))
        sector_data: dict = {}
        for key, sc in self.cfg.sectors.items():
            cat = sc.get("category")
            if valid is not None and cat not in valid:
                log.warning("فئة CoinGecko غير صالحة وتم تجاهلها: %s (%s)", key, cat)
                continue
            coins = self._universe(self.cg.markets(category=cat, per_page=per_page))
            if len(coins) >= 6:
                sector_data[key] = coins
            else:
                log.info("قطاع %s: بيانات غير كافية (%d)", key, len(coins))

        native_ids = [e["native"] for e in self.cfg.ecosystems.values() if e.get("native")]
        natives = {c.id: c for c in self.cg.markets(ids=native_ids, per_page=len(native_ids) or 1,
                                                     sparkline=False)} if native_ids else {}
        trending = self.cg.trending()

        flows = self._bridge_flows(now)
        stats, cands = sector_bot.analyze(sector_data, self.cfg, self.att)
        self.summary["sectors"] = stats

        triggers = waterfall_bot.detect(natives, self.cfg, flows, self.vr)
        for t in triggers:
            cat = t["eco"].get("category")
            if valid is not None and cat not in valid:
                log.warning("فئة النظام البيئي غير صالحة: %s", cat)
                continue
            coins = self._universe(self.cg.markets(category=cat, per_page=per_page))
            cands += waterfall_bot.scan(t, coins, self.cfg, self.vr)
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

        sent = 0
        for s, c in scored:
            if s < threshold or sent >= int(self.cfg.get("run.max_alerts_per_run")):
                continue
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
            return bridge_bot.update(self.state.data, tvl, stables, now, self.cfg)
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
            cand.features = {"whale": A.clip(ev["usd"] / 500000.0)}
            cand.meta = {"dex": i, "url": i.get("url", ""), "whale_only": True, "chain": ev["chain"]}
            merged[cid] = cand

    def _enrich_basic(self, c: Candidate, flows: dict) -> None:
        coin = c.coin
        r = self.vr(coin) if c.src.get("src") == "cg" else None
        if r is not None:
            c.features["vol"] = A.clip((r - 1.0) / 2.0)
            if r >= 1.5:
                c.reasons.append((5, f"📈 حجم تداول {coin.symbol} ×{r:.1f} فوق معدلها"))
        else:
            turnover = (coin.volume / coin.mcap) if coin.mcap > 0 else 0.0
            c.features["vol"] = A.clip((turnover - 0.05) / 0.25)
        ar = c.meta.get("att_ratio")
        c.features["attention"] = A.clip((ar - 1.0) / 3.0) if ar is not None else None
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
                c.features["dex"] = 0.6 * A.clip((buy - 0.5) / 0.2) + 0.4 * A.clip((accel - 1.0) / 2.0)
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
                    whale_usd += ev["usd"]
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
            c.features["whale"] = max(c.features.get("whale") or 0.0, A.clip(whale_usd / 500000.0))
            if c.features["whale"] >= 0.3:
                c.kinds.add("whale")

    def _levels(self, c: Candidate) -> dict:
        r = self.cfg.get("risk")
        dv = c.meta.get("daily_vol") or r["default_daily_vol"]
        stop = A.clip(1.2 * dv, r["stop_min"], r["stop_max"])
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
            got = self.cg.prices(cg_ids)
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
                 + (f" | نجاح: {wr * 100:.0f}%" if wr is not None else "")]
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
    from .notifier import fmt_usd
    return fmt_usd(x)
