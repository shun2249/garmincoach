"""OpenAI 價目表與費用估算（coach / websearch 共用，避免循環匯入）。"""
from __future__ import annotations

from typing import Optional

# 價目表（USD / 1M tokens）：input, cached_input, output — 2026-10 OpenAI 標準價
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (10.0, 1.0, 50.0), "gpt-6.1-sol": (2.0, 0.1, 10.0), "gpt-6-sol": (2.0, 0.2, 10.0), "gpt-6-luna": (0.1, 0.01, 0.5),
    "gpt-5.6-sol": (4.0, 0.4, 20.0), "gpt-5.6-terra": (2.0, 0.2, 12.0), "gpt-5.6-luna": (0.2, 0.02, 1.2),
    "gpt-5.5": (5.0, 0.5, 30.0), "gpt-5.4": (2.5, 0.25, 15.0), "gpt-5.4-mini": (0.75, 0.075, 4.5), "gpt-5.4-nano": (0.2, 0.02, 1.25),
    "gpt-5.2": (1.75, 0.175, 14.0), "gpt-5.1": (1.25, 0.125, 10.0), "gpt-5": (1.25, 0.125, 10.0),
    "gpt-5-mini": (0.25, 0.025, 2.0), "gpt-5-nano": (0.05, 0.005, 0.4),
    "gpt-4.1": (2.0, 0.5, 8.0), "gpt-4.1-mini": (0.4, 0.1, 1.6), "gpt-4o": (2.5, 1.25, 10.0), "gpt-4o-mini": (0.15, 0.075, 0.6),
}


def estimate_cost(usage: dict[str, int], model: str) -> Optional[float]:
    base = next((k for k in sorted(PRICES, key=len, reverse=True) if model.startswith(k)), None)
    if not base:
        return None
    i, c, o = PRICES[base]
    return round((usage["input"] * i + usage["cached_input"] * c + usage["output"] * o) / 1_000_000, 4)
