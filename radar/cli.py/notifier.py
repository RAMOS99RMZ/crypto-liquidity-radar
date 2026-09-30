from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

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
