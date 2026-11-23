"""SYNTHETIC market data for demo mode and tests. Never presented as real: every journal
entry, quote and the UI banner carry the 'synthetic' label when this provider is active.

Prices follow a seeded regime-switching random walk, so results are reproducible.
"""
from __future__ import annotations

import hashlib
import math
from functools import lru_cache

import numpy as np

from .. import clock
from ..venues import VENUES
from .base import Bar, Provider, Quote

STEPS = {"15m": 900, "1h": 3600, "1d": 86400}


def _seed(sym: str) -> int:
    return int(hashlib.sha256(sym.encode()).hexdigest()[:8], 16)


class SyntheticProvider(Provider):
    name = "synthetic"
    synthetic = True

    def __init__(self, venue: str):
        self.venue = venue

    def _series(self, sym: str, step: int, start: int, end: int) -> list[Bar]:
        anchor = 1_560_000_000 - (1_560_000_000 % step)  # mid-2019: covers every backfill window
        n_total = (end - anchor) // step + 1
        o, h, l, c, v = _path(sym, step, int(n_total))
        first = max(0, (start - anchor + step - 1) // step)
        return [Bar(anchor + i * step, float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(v[i]))
                for i in range(int(first), int(n_total))]

    def fetch_bars(self, symbols, timeframe, start, end):
        step = STEPS[timeframe]
        return {s: [b for b in self._series(s, step, start, end) if b.ts + step <= end] for s in symbols}

    def fetch_quotes(self, symbols):
        out = {}
        now = clock.now()
        for s in symbols:
            step = VENUES[self.venue].bar_seconds
            bars = self._series(s, step, int(now) - 3 * step, int(now))
            last = bars[-1].close if bars else 100.0
            # small deterministic wiggle within the bar
            wig = math.sin(now / 60.0 + _seed(s) % 7) * 0.001
            mid = last * (1 + wig)
            half = mid * 0.0002
            out[s] = Quote(s, now, mid - half, mid + half, 100, 100, mid, now, "synthetic")
        return out


@lru_cache(maxsize=64)
def _path(sym: str, step: int, n: int):
    # Independent streams keep every past value fixed when n grows. Reusing one
    # generator for arrays of n-dependent lengths moves the starting RNG state of
    # later arrays, silently rewriting history (and even resembling stock splits).
    lengths_rng, drift_rng, shock_rng, high_rng, low_rng, volume_rng = (
        np.random.default_rng(seed) for seed in np.random.SeedSequence(_seed(sym) + step).spawn(6))
    vol = 0.5 / math.sqrt(365 * 86400 / step)
    lengths = lengths_rng.integers(50, 400, size=n // 50 + 2)
    drifts = drift_rng.normal(0.0, vol * 0.03, size=lengths.size)
    drift = np.repeat(drifts, lengths)[:n]
    shocks = shock_rng.standard_t(5, size=n) / math.sqrt(5 / 3)
    r = drift + vol * shocks
    c = (100.0 + (_seed(sym) % 400)) * np.exp(np.cumsum(r))
    o = np.concatenate([[c[0] / math.exp(r[0])], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(high_rng.normal(0, vol * 0.4, size=n)))
    l = np.minimum(o, c) * np.exp(-np.abs(low_rng.normal(0, vol * 0.4, size=n)))
    dollar = 5e9 * step / 86400 * volume_rng.lognormal(0.0, 0.4, size=n)
    v = dollar / c
    return o, h, l, c, v
