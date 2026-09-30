from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "COINGECKO_API_KEY",
    "COINGECKO_PLAN",
    "ETHERSCAN_API_KEY",
    "SOLANA_RPC_URL",
]
_MISSING = object()


def _read(name: str) -> dict:
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Config:
    def __init__(self, settings: dict, sectors: dict, whales: dict, env: dict | None = None):
        self.settings = settings
        self.sectors: dict = (sectors or {}).get("sectors") or {}
        self.ecosystems: dict = (sectors or {}).get("ecosystems") or {}
        self.whales: dict = whales or {}
        # أسرار GitHub غير المعرّفة تصل كنص فارغ -> نعتبرها None
        self.env = env if env is not None else {k: (os.environ.get(k) or None) for k in ENV_KEYS}

    def get(self, path: str, default=_MISSING):
        node = self.settings
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif default is _MISSING:
                raise KeyError(f"إعداد مفقود في settings.yaml: {path}")
            else:
                return default
        return node


def load_config() -> Config:
    return Config(_read("settings.yaml"), _read("sectors.yaml"), _read("whales.yaml"))
