"""Process configuration.

Two layers:
  * ``Config`` - static, read once from environment variables / ``.env``. Holds secrets,
    paths and *hard ceilings* that no runtime setting can exceed.
  * Operator settings (risk limits, schedules, budget, universe) live in the database
    ``settings`` table and are edited only through the authenticated operator API.
    Defaults for them are defined in ``DEFAULT_SETTINGS`` below. The agent code has no
    write path to that table (see quantdesk/settings.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlsplit


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    # --- AI ---
    anthropic_api_key: str
    llm_provider: str  # "anthropic" | "openai_compatible" | "none"
    llm_base_url: str  # openai_compatible: e.g. https://api.openai.com/v1
    llm_api_key: str   # openai_compatible key (may be empty for a local Ollama server)
    llm_price_in: float   # USD per 1M input tokens (openai_compatible; used for the budget)
    llm_price_out: float  # USD per 1M output tokens
    llm_model: str
    llm_effort: str
    llm_fallbacks: bool
    llm_knowledge_cutoff: str  # ISO date; used for leakage disclosure
    # --- market data ---
    alpaca_key_id: str
    alpaca_secret_key: str
    alpaca_feed: str  # realtime quote feed: iex (free) or sip (paid)
    alpaca_hist_feed: str  # historical bars feed
    market_data_mode: str  # "live" or "synthetic"
    # --- web ---
    admin_password: str
    viewer_password: str  # optional second password: a read-only view for people you show results to
    session_secret: str
    bind_host: str
    bind_port: int
    cookie_secure: bool
    trusted_proxies: str
    public_origin: str
    # --- sandbox ---
    sandbox_mode: str  # "disabled", "local" or "spool"
    sandbox_spool_dir: Path
    # --- hard ceilings (cannot be exceeded by operator settings either) ---
    ceiling_max_position_pct: float = 0.35
    ceiling_max_gross_pct: float = 1.0  # spot, no leverage
    ceiling_max_strategy_pct: float = 0.5
    ceiling_max_order_usd: float = 50_000.0
    ceiling_monthly_llm_usd: float = 500.0
    initial_capital: float = 100_000.0
    extra: dict = field(default_factory=dict)

    @property
    def loopback_bind(self) -> bool:
        return self.bind_host in ("127.0.0.1", "::1", "localhost")

    def check_api_bind(self) -> None:
        if not self.admin_password and not self.loopback_bind:
            raise ValueError("QD_ADMIN_PASSWORD is required when the API listens beyond loopback")

    @property
    def demo_llm(self) -> bool:
        """True when no AI provider is configured: a deterministic rules analyst stands in."""
        if self.llm_provider == "anthropic":
            return not self.anthropic_api_key
        if self.llm_provider == "openai_compatible":
            return not (self.llm_base_url and self.llm_model)
        return True

    @property
    def llm_label(self) -> str:
        if self.demo_llm:
            return "demo"
        host = self.llm_base_url.split("//")[-1].split("/")[0] if self.llm_provider == "openai_compatible" else "anthropic"
        return f"{self.llm_model} ({host})"

    @property
    def alpaca_configured(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret_key)


_CONFIG: Config | None = None


def _resolve_provider() -> str:
    """QD_LLM_PROVIDER wins; otherwise infer: generic OpenAI-compatible settings, then an
    Anthropic key, else none (demo rules analyst)."""
    p = _env("QD_LLM_PROVIDER").lower()
    if p in ("openai", "openai_compatible", "openai-compatible", "gemini", "groq", "openrouter", "ollama",
             "mistral", "deepseek", "together"):
        return "openai_compatible"
    if p in ("anthropic", "claude"):
        return "anthropic"
    if p == "none":
        return "none"
    if _env("QD_LLM_API_KEY") or _env("QD_LLM_BASE_URL"):
        return "openai_compatible"
    if _env("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "none"


MIN_ADMIN_PASSWORD_LEN = 12
# Values published in this public repo (.env.example, docs). Anyone could log in with them.
_PUBLISHED_PASSWORDS = {"change-me-to-a-long-passphrase", "<long password for the dashboard>",
                        "<your own password for the dashboard, at least 12 characters>"}


def _check_admin_password(pw: str) -> str:
    """An empty password is allowed (local-only mode). Anything else must be real."""
    if not pw:
        return pw
    if pw in _PUBLISHED_PASSWORDS:
        raise ValueError("QD_ADMIN_PASSWORD is still the example value from .env.example; "
                         "set your own password before starting QuantDesk")
    if len(pw) < MIN_ADMIN_PASSWORD_LEN:
        raise ValueError(f"QD_ADMIN_PASSWORD is too short ({len(pw)} characters); "
                         f"use at least {MIN_ADMIN_PASSWORD_LEN}")
    return pw


def _check_viewer_password(pw: str, admin: str) -> str:
    """Optional. Same strength rules as the operator password, and never the same value."""
    if not pw:
        return pw
    if not admin:
        raise ValueError("QD_VIEWER_PASSWORD needs QD_ADMIN_PASSWORD; without it the desk is local-only")
    if pw in _PUBLISHED_PASSWORDS or len(pw) < MIN_ADMIN_PASSWORD_LEN:
        raise ValueError(f"QD_VIEWER_PASSWORD must be your own value of at least {MIN_ADMIN_PASSWORD_LEN} characters")
    if pw == admin:
        raise ValueError("QD_VIEWER_PASSWORD must differ from QD_ADMIN_PASSWORD")
    return pw


def _trusted_proxies() -> str:
    proxies = _env("QD_TRUSTED_PROXIES")
    for proxy in proxies.split(",") if proxies else ():
        try:
            network = ip_network(proxy.strip())
            if network.prefixlen == 0:
                raise ValueError
        except ValueError:
            raise ValueError("QD_TRUSTED_PROXIES must list specific proxy IPs or CIDRs") from None
    return proxies


def _public_origin() -> str:
    origin = _env("QD_PUBLIC_ORIGIN").rstrip("/")
    if not origin:
        return ""
    try:
        url = urlsplit(origin)
        port = url.port
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.path or "?" in origin or "#" in origin or port == 0
                or any(c.isspace() for c in origin)):
            raise ValueError
    except ValueError:
        raise ValueError("QD_PUBLIC_ORIGIN must be an HTTPS origin without credentials or a path") from None
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    return f"https://{host}:{port}" if port and port != 443 else f"https://{host}"


def load_config(reload: bool = False, **overrides) -> Config:
    global _CONFIG
    if _CONFIG is not None and not reload and not overrides:
        return _CONFIG
    root = Path(__file__).resolve().parent.parent
    _load_dotenv(Path(_env("QD_ENV_FILE", str(root / ".env"))))
    data_dir = Path(_env("QD_DATA_DIR", str(root / "data")))
    cfg = dict(
        data_dir=data_dir,
        db_path=Path(_env("QD_DB_PATH", str(data_dir / "quantdesk.db"))),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        llm_provider=_resolve_provider(),
        llm_base_url=_env("QD_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_api_key=_env("QD_LLM_API_KEY"),
        llm_price_in=_env_float("QD_LLM_PRICE_IN", 5.0),
        llm_price_out=_env_float("QD_LLM_PRICE_OUT", 20.0),
        llm_model=_env("QD_LLM_MODEL", "claude-opus-5-5" if _resolve_provider() == "anthropic" else ""),
        llm_effort=_env("QD_LLM_EFFORT", "medium"),
        llm_fallbacks=_env_bool("QD_LLM_FALLBACKS", True),
        llm_knowledge_cutoff=_env("QD_LLM_KNOWLEDGE_CUTOFF", "2026-06-01"),
        alpaca_key_id=_env("ALPACA_API_KEY_ID"),
        alpaca_secret_key=_env("ALPACA_API_SECRET_KEY"),
        alpaca_feed=_env("ALPACA_FEED", "iex"),
        alpaca_hist_feed=_env("ALPACA_HIST_FEED", "sip"),
        market_data_mode=_env("QD_MARKET_DATA", "live"),
        admin_password=_check_admin_password(_env("QD_ADMIN_PASSWORD")),
        viewer_password=_check_viewer_password(_env("QD_VIEWER_PASSWORD"), _env("QD_ADMIN_PASSWORD")),
        session_secret=_env("QD_SESSION_SECRET"),
        bind_host=_env("QD_BIND_HOST", "127.0.0.1"),
        bind_port=_env_int("QD_PORT", 8000),
        cookie_secure=_env_bool("QD_COOKIE_SECURE", False),
        trusted_proxies=_trusted_proxies(),
        public_origin=_public_origin(),
        sandbox_mode=_env("QD_SANDBOX_MODE", "local"),
        sandbox_spool_dir=Path(_env("QD_SANDBOX_SPOOL", str(data_dir / "sandbox-spool"))),
        initial_capital=_env_float("QD_INITIAL_CAPITAL", 100_000.0),
    )
    cfg.update(overrides)
    _CONFIG = Config(**cfg)
    return _CONFIG


# ---------------------------------------------------------------------------
# Operator-adjustable settings and their defaults.
# Every key here is editable only via the authenticated operator API, and each
# numeric limit is clamped to the hard ceilings in Config.
# ---------------------------------------------------------------------------
GATE_PRESETS = {
    # Admission to exploratory paper trading, NOT evidence of a statistical edge.
    "exploratory": {
        "min_test_trades": 8, "min_test_sharpe": 0.0, "min_val_sharpe": -0.5,
        "min_cost_stress_return": -0.05, "min_delay_stress_sharpe": -0.5,
        "min_neighbor_positive_frac": 0.34, "min_subperiod_positive_frac": 0.34,
        "max_top5_concentration": 1.0, "min_deflated_psr": 0.25,
        "require_causal_dsl": False,
        "require_universe_excess": False,  # reported in the journal, not required
    },
    # More selective screening, still not a substitute for forward-paper evidence.
    "strict": {
        "min_test_trades": 30, "min_test_sharpe": 0.5, "min_val_sharpe": 0.25,
        "min_cost_stress_return": 0.0, "min_delay_stress_sharpe": 0.0,
        "min_neighbor_positive_frac": 0.67, "min_subperiod_positive_frac": 0.75,
        "max_top5_concentration": 0.5, "min_deflated_psr": 0.95,
        "require_causal_dsl": True,
        "require_universe_excess": True,
    },
}

DEFAULT_SETTINGS: dict = {
    "universe": {
        # US equities (the venue is named NASDAQ but any Alpaca-listed stock/ETF works, NYSE too)
        # Defaults only: liquid large caps and broad unleveraged ETFs; crypto majors. No single-name
        # lottery tickets, leveraged or inverse ETFs, or memecoins. Saved operator settings are kept.
        "NASDAQ": ["QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO",
                   "TSLA", "COST", "NFLX", "AMD", "PEP", "ADBE", "CSCO",
                   "INTC", "MU", "TSM", "UBER", "PYPL", "CRWD",
                   "SPY", "IWM", "DIA", "SMH", "XLF", "XLE"],
        "CRYPTO": ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD", "AVAX-USD",
                   "LINK-USD", "LTC-USD", "DOT-USD", "BCH-USD"],
    },
    "benchmarks": {"NASDAQ": "QQQ", "CRYPTO": "BTC-USD", "account": "QQQ"},
    "risk": {
        "max_position_pct": 0.15,       # per symbol, of account equity
        "max_gross_pct": 0.80,          # total long exposure / equity
        "max_crypto_pct": 0.25,         # crypto exposure / equity
        "max_strategy_pct": 0.25,       # capital allocated to one strategy / equity
        "max_total_allocation_pct": 0.80,
        "max_order_usd": 20_000.0,
        "min_order_usd": 25.0,
        "daily_loss_limit_pct": 0.025,  # vs. start-of-UTC-day equity; blocks new risk
        "max_drawdown_pct": 0.10,       # vs. peak equity; halts new risk until operator resets
        "max_orders_per_hour": 120,
        "max_quote_age_sec": 120,
        "max_participation": 0.01,      # of average daily volume per order
        "max_spread_bps": 60.0,         # wider quotes are treated as unusable
    },
    "costs": {
        "NASDAQ": {
            "commission_per_share": 0.0,
            "sec_fee_per_million": 27.80,   # on sells; SEC Section 31 rate changes - verify
            "finra_taf_per_share": 0.000166,  # on sells
            "finra_taf_max": 8.30,
            "base_slippage_bps": 1.0,
            "impact_coef": 0.6,             # square-root impact coefficient
            "backtest_half_spread_bps": 2.0,
        },
        "CRYPTO": {
            "taker_fee_bps": 25.0,
            "base_slippage_bps": 2.0,
            "impact_coef": 0.8,
            "backtest_half_spread_bps": 1.5,
        },
    },
    "execution": {
        "latency_sec": 2.0,
        "order_ttl_sec": 600,
        "fractional_shares": False,
        "nasdaq_trade_delay_min": 5,   # minutes after the open before orders queued overnight go out
    },
    "schedule": {
        "quote_poll_sec": 20,
        "bar_refresh_min": 15,
        # Each venue has its own research clock. Both trade on 15-minute bars (stocks only in the
        # regular session); the daily LLM budget is the real cap on how much research runs.
        "crypto_research_hours": 2,
        "nasdaq_research_hours": 2,
        "review_hour_utc": 22,
        "max_research_per_day": 12,     # per venue
    },
    "budget": {
        "monthly_usd": 40.0,
        "daily_usd": 4.0,
    },
    "research": {
        "train_frac": 0.60,
        "val_frac": 0.15,
        "max_param_combos": 12,
        "incubation_fraction": 0.5,  # new strategies start with this share of approved allocation
        "min_live_days_before_increase": 10,
        "observation_blinding": True,  # hypothesis context excludes data the test window could use
        # Deflated PSR counts earlier experiments whose test window overlaps this one. Counting every
        # experiment ever run ratchets the bar up forever (after ~100 trials it needs Sharpe 3-4), so
        # only trials from this many recent days count.
        "deflation_window_days": 7,
    },
    # Probe strategies: small forward-paper trials of ideas that made money on their sealed test but
    # did not clear every gate. They exist to gather live evidence, are labelled PROBE everywhere,
    # and retire themselves after lifetime_days.
    "probe": {
        "enabled": True,
        "allocation_pct": 0.03,        # of equity, per probe (no incubation haircut)
        "max_active": 3,
        "min_test_return": 0.0,        # sealed-test net return must be above this
        "per_venue_per_day": 1,
        "lifetime_days": 14,
    },
    "gates": dict(GATE_PRESETS["exploratory"]),
    "agent": {"paused": False, "emergency_stop": False},
}

# Bounds used to validate operator edits. (key path) -> (min, max)
SETTING_BOUNDS: dict = {
    ("risk", "max_position_pct"): (0.0, "ceiling_max_position_pct"),
    ("risk", "max_gross_pct"): (0.0, "ceiling_max_gross_pct"),
    ("risk", "max_crypto_pct"): (0.0, "ceiling_max_gross_pct"),
    ("risk", "max_strategy_pct"): (0.0, "ceiling_max_strategy_pct"),
    ("risk", "max_total_allocation_pct"): (0.0, "ceiling_max_gross_pct"),
    ("risk", "max_order_usd"): (0.0, "ceiling_max_order_usd"),
    ("risk", "min_order_usd"): (1.0, 10_000.0),
    ("risk", "daily_loss_limit_pct"): (0.001, 0.25),
    ("risk", "max_drawdown_pct"): (0.01, 0.5),
    ("risk", "max_orders_per_hour"): (1, 500),
    ("risk", "max_quote_age_sec"): (5, 3600),
    ("risk", "max_participation"): (0.0001, 0.1),
    ("risk", "max_spread_bps"): (1.0, 500.0),
    ("budget", "monthly_usd"): (0.0, "ceiling_monthly_llm_usd"),
    ("budget", "daily_usd"): (0.0, "ceiling_monthly_llm_usd"),
    ("schedule", "crypto_research_hours"): (0.5, 168),
    ("schedule", "nasdaq_research_hours"): (0.5, 168),
    ("schedule", "max_research_per_day"): (0, 48),
    ("schedule", "quote_poll_sec"): (5, 600),
    ("schedule", "bar_refresh_min"): (1, 1440),
    ("schedule", "review_hour_utc"): (0, 23),
    ("execution", "latency_sec"): (0.0, 300.0),
    ("execution", "order_ttl_sec"): (1, 86400),
    ("execution", "nasdaq_trade_delay_min"): (0, 120),
    ("research", "train_frac"): (0.1, 0.85),
    ("research", "val_frac"): (0.05, 0.5),
    ("research", "max_param_combos"): (1, 12),
    ("research", "incubation_fraction"): (0.01, 1.0),
    ("research", "min_live_days_before_increase"): (1, 365),
    ("research", "deflation_window_days"): (1, 3650),
    ("probe", "allocation_pct"): (0.0, "ceiling_max_strategy_pct"),
    ("probe", "max_active"): (0, 20),
    ("probe", "min_test_return"): (-1.0, 10.0),
    ("probe", "per_venue_per_day"): (0, 24),
    ("probe", "lifetime_days"): (1, 365),
    ("gates", "min_test_trades"): (1, 1000),
    ("gates", "min_test_sharpe"): (-5.0, 5.0),
    ("gates", "min_val_sharpe"): (-5.0, 5.0),
    ("gates", "min_cost_stress_return"): (-1.0, 1.0),
    ("gates", "min_delay_stress_sharpe"): (-5.0, 5.0),
    ("gates", "min_neighbor_positive_frac"): (0.0, 1.0),
    ("gates", "min_subperiod_positive_frac"): (0.0, 1.0),
    ("gates", "max_top5_concentration"): (0.0, 100.0),
    ("gates", "min_deflated_psr"): (0.0, 1.0),
}

for _venue, _costs in DEFAULT_SETTINGS["costs"].items():
    for _key in _costs:
        SETTING_BOUNDS[("costs", _venue, _key)] = (0.0, 1000.0 if _key == "impact_coef" else 10000.0)

# Prices per 1M tokens (input, output, cache_write, cache_read). Editable here if pricing
# changes; unknown models fall back to the most expensive row so budgets stay conservative.
MODEL_PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-fable-5-2": (8.00, 40.00, 10.00, 0.20),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
}
FALLBACK_PRICING = (10.00, 50.00, 12.50, 1.00)
