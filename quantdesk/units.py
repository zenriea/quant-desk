"""One reading of the strategy drawdown limit, shared by the code that stores it and the code that enforces it.

``max_live_drawdown_pct`` is a positive fraction of the sleeve's high-water mark (0.05 = 5 %). Analysts
have written it as a percentage and with a sign (-3.0 for 3 %); compared with a fractional drawdown,
-3.0 reads as 300 % and the limit never fires. A fraction is at most 1, so any larger magnitude can only
be a percentage.
"""
from __future__ import annotations

import math


def drawdown_fraction(value) -> float | None:
    """The limit as a fraction in (0, 1], or None when it is missing or meaningless."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    a = abs(float(value))
    if not math.isfinite(a) or a == 0:
        return None
    if a > 1:
        a /= 100.0
    return a if 0 < a <= 1 else None


def normalize_invalidation(inval: dict | None) -> dict:
    out = dict(inval or {})
    if "max_live_drawdown_pct" in out:
        out["max_live_drawdown_pct"] = drawdown_fraction(out["max_live_drawdown_pct"])
    return out


def describe_drawdown_limit(inval: dict | None) -> str:
    f = drawdown_fraction((inval or {}).get("max_live_drawdown_pct"))
    return f"retire at a {f:.1%} live drawdown from the sleeve's peak" if f else "no drawdown limit recorded"
