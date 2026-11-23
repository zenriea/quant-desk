"""Analyst implementations.

``LLMAnalyst`` - an AI model via any OpenAI-compatible API or Claude (see agent/llm.py).
``RulesAnalyst`` - DEMO stand-in used when no AI provider is configured. It is deterministic,
picks hypotheses from a small template library, and writes plain factual notes from the
same records. Every entry it writes is labelled author='rules' and shown as "demo" in the UI.
"""
from __future__ import annotations

from .. import db
from ..research.strategy import MAX_UNIVERSE
from ..units import default_drawdown_limit
from . import prompts
from .llm import LLM, Router, free_helpers


def spec_from_llm(raw: dict, venue: str) -> dict:
    spec = {k: v for k, v in raw.items() if k != "params"}
    spec["venue"] = venue
    spec["params"] = {p["name"]: p["values"] for p in raw.get("params") or []}
    for k in ("entry", "exit", "code", "rank_by", "top_k", "stop_loss_pct", "take_profit_pct", "max_hold_bars"):
        if spec.get(k) in ("", None):
            spec[k] = None
    for k in ("weight", "max_weight", "target_vol"):
        if not spec["sizing"].get(k):
            spec["sizing"][k] = 0.2 if k != "target_vol" else 0.15
    if spec.get("kind") == "dsl":
        spec.pop("code", None)
    return spec


class LLMAnalyst:
    author = "llm"

    def __init__(self, llm: LLM | None = None):
        self.llm = llm or LLM()

    def propose(self, ctx: dict) -> tuple[dict, int]:
        return self.llm.call("hypothesis", prompts.SYSTEM, prompts.hypothesis_prompt(ctx), prompts.HYPOTHESIS_SCHEMA)

    def repair(self, ctx: dict, previous: dict, error: str) -> tuple[dict, int]:
        ctx = dict(ctx, previous_attempt=previous, validation_error=error,
                   instruction="Your previous spec failed validation. Fix this error without changing the idea "
                   "unnecessarily. If you cannot make it testable, return decision='no_idea' with an explanation "
                   "and null spec.")
        return self.llm.call("hypothesis_repair", prompts.SYSTEM, prompts.hypothesis_prompt(ctx), prompts.HYPOTHESIS_SCHEMA)

    def decide(self, ctx: dict) -> tuple[dict, int]:
        user = prompts.decision_prompt(ctx)
        dec, cid = self.llm.call("decision", prompts.SYSTEM, user, prompts.DECISION_SCHEMA)
        # A second, independent model sizes the allocation: if it disagrees, the stake is halved.
        # It cannot block an experiment that passed every gate (the gates decide admission).
        if dec.get("decision") == "allocate" and hasattr(self.llm, "second_opinion"):
            check = self.llm.second_opinion("decision_check", prompts.SYSTEM, user, prompts.DECISION_SCHEMA)
            if check is not None and check[0].get("decision") != "allocate":
                other = check[0]
                half = float(dec.get("allocation_pct") or 0) / 2
                dec = dict(dec, allocation_pct=half, journal=dec["journal"] + f"\n\n[Second opinion: {self.llm.last.model}"
                           f" said {other.get('decision')}, so the allocation is halved to {half:.1%} of equity] "
                           f"{other.get('journal', '')[:600]}")
        return dec, cid

    def review(self, ctx: dict) -> tuple[dict, int]:
        return self.llm.call("review", prompts.SYSTEM, prompts.review_prompt(ctx), prompts.REVIEW_SCHEMA, max_tokens=8000)

    def answer(self, question: str, records: dict, progress=None) -> tuple[dict, int]:
        if progress is None or not hasattr(self.llm, "notify"):
            return self.llm.call("question", prompts.SYSTEM, prompts.answer_prompt(question, records),
                                 prompts.ANSWER_SCHEMA, max_tokens=6000)
        self.llm.notify = progress
        try:
            return self.llm.call("question", prompts.SYSTEM, prompts.answer_prompt(question, records),
                                 prompts.ANSWER_SCHEMA, max_tokens=6000)
        finally:
            self.llm.notify = None


# ----------------------------------------------------------------------------- demo
TEMPLATES = {
    "NASDAQ": [
        {"key": "trend_200", "title": "Multi-day trend filter on US stocks",
         "rationale": "Slow-moving institutional flows can make trends persist; holding names above their long "
                      "moving average and stepping aside below it may cut drawdowns at modest cost.",
         "entry": "close > sma(close, slow) and sma(close, fast) > sma(close, slow)",
         "exit": "close < sma(close, slow)", "params": {"fast": [26, 78], "slow": [260, 520]},
         "sizing": {"method": "fixed", "weight": 0.15, "max_weight": 0.15, "target_vol": 0.15, "vol_lookback": 26}},
        {"key": "momentum_rotation", "title": "Cross-sectional momentum rotation with a market filter",
         "rationale": "Relative strength among large caps has historically persisted for months; a QQQ trend "
                      "filter avoids holding momentum through broad sell-offs.",
         "entry": "ret(close, look) > 0 and market(close) > market(sma(close, 520))",
         "exit": "ret(close, look) < 0 or market(close) < market(sma(close, 520))",
         "rank_by": "ret(close, look)", "top_k": 3, "params": {"look": [26, 130]},
         "sizing": {"method": "fixed", "weight": 0.3, "max_weight": 0.3, "target_vol": 0.15, "vol_lookback": 26}},
        {"key": "rsi2_reversion", "title": "Short-term pullbacks in uptrends (RSI-2 mean reversion)",
         "rationale": "Liquidity-driven short-term sell-offs in names that are in long-term uptrends often "
                      "partially reverse within days.",
         "entry": "rsi(close, 2) < th and close > sma(close, 520)", "exit": "rsi(close, 2) > 70",
         "max_hold_bars": 26, "params": {"th": [5, 10]},
         "sizing": {"method": "fixed", "weight": 0.15, "max_weight": 0.15, "target_vol": 0.15, "vol_lookback": 26}},
        {"key": "breakout_55", "title": "Donchian breakout with volatility-scaled sizing",
         "rationale": "New multi-week highs can mark the start of trends; sizing by inverse volatility keeps risk "
                      "per position roughly equal.",
         "entry": "close > lag(highest(high, n), 1)", "exit": "close < lag(lowest(low, m), 1)",
         "params": {"n": [26, 78], "m": [13, 26]},
         "sizing": {"method": "inverse_vol", "weight": 0.2, "max_weight": 0.2, "target_vol": 0.04, "vol_lookback": 26}},
    ],
    "CRYPTO": [
        {"key": "crypto_trend", "title": "Intraday trend following on crypto",
         "rationale": "Crypto returns have shown time-series momentum at multi-day horizons; staying long only "
                      "above a weekly trend may capture it while avoiding extended declines.",
         "entry": "close > ema(close, slow) and ema(close, fast) > ema(close, slow)",
         "exit": "close < ema(close, slow)", "params": {"fast": [96, 288], "slow": [672, 1344]},
         "sizing": {"method": "inverse_vol", "weight": 0.3, "max_weight": 0.3, "target_vol": 0.25, "vol_lookback": 288}},
        {"key": "crypto_vol_breakout", "title": "Breakouts after volatility compression (crypto)",
         "rationale": "Periods of compressed volatility in crypto are often followed by directional moves; a "
                      "breakout above the recent range may be the start of one.",
         "entry": "close > lag(highest(high, n), 1) and rvol(192) < rvol(1344)",
         "exit": "close < lag(lowest(low, m), 1)", "params": {"n": [192, 384], "m": [96, 192]},
         "sizing": {"method": "fixed", "weight": 0.3, "max_weight": 0.3, "target_vol": 0.25, "vol_lookback": 288}},
        {"key": "crypto_reversion", "title": "Fading sharp intraday drops in uptrends (crypto)",
         "rationale": "Liquidation cascades can overshoot; in a prevailing uptrend, deeply oversold 15-minute "
                      "readings may revert within a day or two.",
         "entry": "zscore(close, 96) < -z and close > ema(close, 1344)", "exit": "zscore(close, 96) > 0",
         "max_hold_bars": 192, "params": {"z": [2, 2.5]},
         "sizing": {"method": "fixed", "weight": 0.25, "max_weight": 0.25, "target_vol": 0.25, "vol_lookback": 288}},
    ],
}


class RulesAnalyst:
    author = "rules"

    def propose(self, ctx: dict) -> tuple[dict, None]:
        venue = ctx["venue"]
        tried = {m.get("template") for m in ctx.get("research_memory", [])}
        options = [t for t in TEMPLATES[venue] if t["key"] not in tried]
        if not options:
            return {"decision": "no_idea", "why": f"Every {venue} template in the demo library has been tested "
                    "already; without an LLM I have no new ideas to propose. Waiting for new data.",
                    "title": "", "rationale": "", "prediction": "", "falsification": "",
                    "parent_experiment_id": None, "spec": None}, None
        t = options[0]
        uni = [s for s in ctx["approved_universe"] if s != ctx["benchmark"]] or ctx["approved_universe"]
        uni = uni[:8] if venue == "NASDAQ" else uni[:MAX_UNIVERSE]
        obs = ctx.get("market_context", {}).get("symbols", {}).get(ctx["benchmark"], {})
        facts = ", ".join(f"{k} {v:+.1%}" if isinstance(v, float) and k.startswith(("ret", "vs", "from")) else ""
                          for k, v in obs.items() if v is not None)
        facts = ", ".join(x for x in facts.split(", ") if x)
        spec = {"name": t["key"], "venue": venue, "universe": uni, "kind": "dsl", "entry": t["entry"],
                "exit": t["exit"], "rank_by": t.get("rank_by"), "top_k": t.get("top_k"),
                "sizing": t["sizing"], "stop_loss_pct": t.get("stop_loss_pct"),
                "take_profit_pct": None, "max_hold_bars": t.get("max_hold_bars"), "params": t["params"],
                "template": t["key"]}
        return {"decision": "investigate",
                "why": f"Next untested idea in the demo library. Blinded {ctx['benchmark']} context: {facts or 'n/a'}.",
                "title": t["title"], "rationale": t["rationale"],
                "prediction": "Positive net Sharpe on the sealed test window that survives doubled costs and a one-bar delay.",
                "falsification": "Test Sharpe below the gate, or the edge disappears under cost/delay stress.",
                "parent_experiment_id": None, "spec": spec}, None

    def repair(self, ctx, previous, error):
        raise RuntimeError(f"demo template failed validation: {error}")

    def decide(self, ctx: dict) -> tuple[dict, None]:
        r = ctx["results"]
        t, ch, g = r["test"], r["challenge"], ctx["gates"]
        lessons = []
        if ch["test_cost_x2"]["total_return"] is not None and t["total_return"] is not None:
            lessons.append(f"Doubling costs moved the test return from {t['total_return']:+.2%} to "
                           f"{ch['test_cost_x2']['total_return']:+.2%}.")
        if ch.get("neighbor_positive_frac") is not None:
            lessons.append(f"{ch['neighbor_positive_frac']:.0%} of neighbouring parameter sets had a positive "
                           "pre-test Sharpe.")
        bench = t.get("benchmark_venue") or {}
        body = (f"Test window: net return {t['total_return']:+.2%}, Sharpe {t['sharpe']:.2f}, max drawdown "
                f"{t['max_drawdown']:.1%}, {t['n_trades']} trades; {ctx['benchmark']} buy-and-hold returned "
                f"{bench.get('total_return', 0):+.2%} (Sharpe {bench.get('sharpe', 0):.2f}). ")
        if g["passed"]:
            alloc = min(0.10, ctx["limits"]["max_strategy_pct"])
            body += (f"All acceptance gates passed. I am allocating {alloc:.0%} of equity, starting at the "
                     f"incubation size, and will judge it on live results rather than this backtest.")
            dec = "allocate"
        else:
            alloc = 0.0
            body += "Failed gates: " + ", ".join(g["failed"]) + ". I am rejecting it."
            dec = "reject"
        return {"decision": dec, "journal": body, "lessons": lessons, "allocation_pct": alloc,
                "thesis": ctx["hypothesis"]["rationale"],
                "exit_plan": "Exit per the tested rules; retire if live drawdown exceeds 1.5x the test drawdown.",
                "invalidation": {"max_live_drawdown_pct": default_drawdown_limit(t["max_drawdown"]),
                                 "review_after_days": 14, "min_trades_before_judging": 5,
                                 "conditions": "Live return falls more than 2.5 standard deviations below the test expectation."},
                "revision_idea": None,
                "beliefs": [{"statement": (f"{ctx['hypothesis']['title']}: passed every acceptance gate (test Sharpe "
                                           f"{t['sharpe']:.2f}); still unproven live." if g["passed"] else
                                           f"{ctx['hypothesis']['title']}: not a reliable edge on this evidence "
                                           f"(test Sharpe {t['sharpe']:.2f}, but failed {', '.join(g['failed'])})."),
                             "confidence": "low", "evidence": [f"experiment:{ctx['experiment_id']}"]}]}, None

    def review(self, ctx: dict) -> tuple[dict, None]:
        live = ctx["live"]
        action = "continue"
        text = (f"Live for {live['days_live']:.1f} days: sleeve return {live['return']:+.2%} on "
                f"${ctx['strategy']['allocation']:,.0f}, {live['closed_trades']} closed trades. ")
        if ctx["auto_flags"]:
            action = "retire" if any("drawdown" in f for f in ctx["auto_flags"]) else "reduce"
            text += "Flags: " + "; ".join(ctx["auto_flags"]) + "."
        else:
            text += "Within expectations; too early to conclude anything. Continuing unchanged."
        return {"action": action, "journal": text, "new_allocation_pct": None, "lessons": [], "beliefs": []}, None

    def answer(self, question: str, records: dict, progress=None) -> tuple[dict, None]:
        from .qa import deterministic_answer
        return deterministic_answer(question, records), None


def get_analyst():
    llm = Router(LLM(), free_helpers())
    return LLMAnalyst(llm) if llm.available else RulesAnalyst()


def mechanism_tags(spec: dict) -> set[str]:
    """Coarse mechanism of a rule set, so the desk can tell when it keeps testing one idea in new windows."""
    import re
    rules = " ".join(spec.get(k) or "" for k in ("entry", "exit"))
    rank = spec.get("rank_by") or ""
    tags = set()
    if re.search(r"(sma|ema)\(\s*market\(|market\(\s*(sma|ema)\(|market\([^)]*\)\s*[<>]", rules):
        tags.add("benchmark trend gate")
    if re.search(r"\b(zscore|rsi)\(", rules):
        tags.add("reversion")
    if re.search(r"\b(highest|lowest|crossover|crossunder)\(", rules):
        tags.add("breakout")
    if re.search(r"\b(rvol|std|atr)\(", rank):
        tags.add("low-vol rotation")
    return tags


def memory(venue: str, limit: int = 15) -> list[dict]:
    """Compact research memory: prior experiments, outcomes and recorded lessons."""
    out = []
    for e in db.q("SELECT e.*, h.title, h.status AS hstatus FROM experiments e JOIN hypotheses h ON h.id=e.hypothesis_id "
                  "WHERE e.venue=? ORDER BY e.id DESC LIMIT ?", (venue, limit)):
        spec = db.loads(e["spec"], {})
        test = db.loads(e["test"], {}) or {}
        gates = db.loads(e["gates"], {}) or {}
        dec = db.q1("SELECT kind, details FROM decisions WHERE subject_type='experiment' AND subject_id=? "
                    "ORDER BY id DESC LIMIT 1", (e["id"],))
        lessons = (db.loads(dec["details"], {}) or {}).get("lessons", []) if dec else []
        out.append({"experiment_id": e["id"], "title": e["title"], "status": e["status"], "template": spec.get("template"),
                    "entry": spec.get("entry"), "exit": spec.get("exit"), "rank_by": spec.get("rank_by"),
                    "mechanism": sorted(mechanism_tags(spec)), "bar_seconds": e["bar_seconds"],
                    "universe": spec.get("universe"), "chosen_params": db.loads(e["chosen_params"]),
                    "test_sharpe": test.get("sharpe"), "test_return": test.get("total_return"),
                    "test_trades": test.get("n_trades"), "failed_gates": gates.get("failed"),
                    "decision": dec["kind"] if dec else None, "lessons": lessons, "error": e["error"]})
    return out
