"""Operator settings store.

Reads: anyone. Writes: ONLY ``operator_update`` - called exclusively by the authenticated
operator API (quantdesk/api/app.py). The agent package never imports ``operator_update``;
tests/test_risk.py enforces that by scanning the agent sources.
"""
from __future__ import annotations

import copy
import math
import re

from . import clock, db
from .config import DEFAULT_SETTINGS, GATE_PRESETS, SETTING_BOUNDS, load_config


def _merged(key: str) -> dict:
    base = copy.deepcopy(DEFAULT_SETTINGS[key])
    row = db.q1("SELECT value FROM settings WHERE key=?", (key,))
    if row:
        stored = db.loads(row["value"], {})
        if key == "gates" and isinstance(stored, dict):
            stored = _upgrade_gates(stored)
        if isinstance(base, dict) and isinstance(stored, dict):
            _deep_update(base, stored)
        else:
            base = stored
    return base


def _upgrade_gates(stored: dict) -> dict:
    """Gate settings saved before a preset gained a key keep that preset's meaning."""
    for preset in GATE_PRESETS.values():
        missing = {k: v for k, v in preset.items() if k not in stored}
        if missing and all(stored.get(k) == v for k, v in preset.items() if k in stored):
            return {**stored, **missing}
    return stored


def _deep_update(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v


def get(key: str) -> dict:
    return _merged(key)


def all_settings() -> dict:
    return {k: _merged(k) for k in DEFAULT_SETTINGS}


class SettingsError(ValueError):
    pass


def _bound(value, bound):
    cfg = load_config()
    return getattr(cfg, bound) if isinstance(bound, str) else bound


def validate(key: str, value: dict) -> dict:
    if key not in DEFAULT_SETTINGS:
        raise SettingsError(f"unknown settings group {key!r}")
    current = _merged(key)
    if not isinstance(value, dict):
        raise SettingsError("settings value must be an object")
    def check(v, template, path):
        label = ".".join(path)
        if isinstance(template, dict):
            if not isinstance(v, dict):
                raise SettingsError(f"{label} must be an object")
            out = {}
            for sub, item in v.items():
                if sub not in template:
                    raise SettingsError(f"unknown setting {label}.{sub}")
                out[sub] = check(item, template[sub], (*path, sub))
            return out
        if isinstance(template, bool):
            if not isinstance(v, bool):
                raise SettingsError(f"{label} must be a boolean")
        elif isinstance(template, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise SettingsError(f"{label} must be a finite number")
            if isinstance(template, int):
                if int(v) != v:
                    raise SettingsError(f"{label} must be an integer")
                v = int(v)
            bounds = SETTING_BOUNDS.get(path)
            if bounds is None:
                raise SettingsError(f"{label} has no configured bounds")
            lo, hi = _bound(v, bounds[0]), _bound(v, bounds[1])
            if not lo <= v <= hi:
                raise SettingsError(f"{label}={v} outside allowed range [{lo}, {hi}]")
        elif isinstance(template, list):
            if not isinstance(v, list) or len(v) > 200 or any(not isinstance(s, str) for s in v):
                raise SettingsError(f"{label} must be a list of at most 200 symbols")
            if len(set(v)) != len(v):
                raise SettingsError(f"{label} must not contain duplicate symbols")
            for symbol in v:
                _validate_symbol(symbol, path[-1])
        elif isinstance(template, str):
            if not isinstance(v, str):
                raise SettingsError(f"{label} must be a string")
            _validate_symbol(v, path[-1] if path[-1] != "account" else None)
        return v

    cleaned = check(value, DEFAULT_SETTINGS[key], (key,))
    merged = copy.deepcopy(current)
    _deep_update(merged, cleaned)
    if key == "research" and merged["train_frac"] + merged["val_frac"] > 0.95 + 1e-12:
        raise SettingsError("train_frac + val_frac must leave at least 5% for the test window")
    return merged


def _validate_symbol(symbol: str, venue: str | None) -> None:
    stock = bool(re.fullmatch(r"[A-Z][A-Z0-9.]{0,19}", symbol))
    crypto = bool(re.fullmatch(r"[A-Z0-9]{1,20}-USD", symbol))
    if not (stock if venue == "NASDAQ" else crypto if venue == "CRYPTO" else stock or crypto):
        raise SettingsError(f"invalid {venue or 'benchmark'} symbol {symbol!r}")


def operator_update(key: str, value: dict, actor: str = "operator") -> dict:
    """Persist an operator edit with an audit record. Never called by agent code."""
    ts = clock.now()
    with db.tx():
        merged = validate(key, value)  # read/merge/write under the same writer lock
        db.ex("INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
              "updated_by=excluded.updated_by", (key, db.dumps(merged), ts, actor))
        db.ex("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)",
              (ts, actor, f"settings.{key}", db.dumps(value)))
    return merged
