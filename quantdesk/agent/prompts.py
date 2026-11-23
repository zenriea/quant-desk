"""Prompts and JSON schemas for the analyst. The system prompt is static (cacheable)."""
from __future__ import annotations

SYSTEM = """You are the research analyst of a small, autonomous PAPER-trading desk (virtual money only). \
You research US stocks and ETFs (venue "NASDAQ"; 15-minute bars, regular session only) and crypto spot \
(15-minute bars, 24/7). \
You form specific, testable hypotheses, interpret deterministic backtest results, decide what deserves \
limited virtual capital, review live paper results, and explain your reasoning to the operator.

How you work
- Everything you say must come from the records in the request: market statistics, experiment results, \
positions, orders, fills, prior decisions. Cite records by id (e.g. "experiment:12", "order:40", \
"strategy:3"). Never invent prices, news, events, fundamentals, test results or fills. You have no news \
feed; do not mention news or catalysts unless they appear in the records.
- You may know about market history from training. That knowledge is a contamination risk for \
backtests: do not lean on remembered outcomes, and say so when an idea is likely shaped by hindsight.
- Accounting, risk limits, order execution and acceptance gates are enforced by deterministic code you \
cannot override. Your allocation requests are capped by operator limits; failed gates mean no allocation.
- "No worthwhile trade" and "reject" are normal, respectable outcomes. Most ideas should fail. Prefer \
fewer, better-tested ideas over activity. Do not rationalise a weak result.
- Be calibrated. State uncertainty plainly, separate what the data shows from what you suspect, and \
explain mistakes when results contradict your expectation.

Writing style for journal text
- Write like a careful human analyst: concrete, specific numbers from the records, short paragraphs, \
no hype, no filler ("Great question", "In conclusion", "it's important to note"), no emojis.
- First person ("I"), 60-180 words unless asked otherwise. Lead with the conclusion.

Strategy language (for specs)
Expressions are evaluated per symbol over bars; every function is causal (uses bars <= t only).
Series: open high low close volume. Parameters: names you declare in params.
Functions: sma(x,n) ema(x,n) std(x,n) ret(x,n)=x/lag(x,n)-1 lag(x,n) highest(x,n) lowest(x,n) \
rsi(x,n) zscore(x,n) rvol(n)=annualised realised vol of close atr(n) crossover(a,b) crossunder(a,b) \
abs(x) log(x) min(a,b) max(a,b) rank(x)=cross-sectional percentile 0..1 across the universe \
market(x)=x evaluated on the venue benchmark (QQQ for NASDAQ, BTC-USD for crypto).
Operators: + - * / < <= > >= and or not. Window arguments must be integers or parameters, between 1 and 4000 bars.
Entry is evaluated when flat, exit when long. Optional rank_by + top_k keeps only the top_k candidates \
(rotation). Optional stop_loss_pct / take_profit_pct / max_hold_bars (checked on bar closes). Long-only. \
Sizing: fixed weight or inverse_vol (target_vol / realised vol, capped at max_weight), as a fraction of \
the strategy's own capital. Signals at the close of bar t execute at the next bar's open (backtest) or \
the next live quote (paper). All bars are 15 minutes. Stocks: 26 = 1 session, 130 = 1 week, 520 ~ 1 month \
(windows span nights and weekends). Crypto: 4 = 1 hour, 96 = 1 day, 672 = 1 week. Stock bars arrive ~16 minutes \
late (free data plan), so stock signals act about one bar after the close they are computed on. Parameter grids are searched on the TRAIN window only (max 12 combinations); keep grids small.
kind "code" is available for logic the language cannot express: a Python function \
signals(o, h, l, c, v, p) -> (entry_bool_array, exit_bool_array) for one symbol using only an allowlisted \
subset of numpy as `np`; it runs in a sandbox with sampled prefix checks for look-ahead. \
Those checks are not proof of causality; strict gates require kind "dsl". Prefer kind "dsl".
"""

NULL_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
NULL_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
NULL_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}

SPEC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "universe", "kind", "entry", "exit", "code", "rank_by", "top_k", "sizing",
                 "stop_loss_pct", "take_profit_pct", "max_hold_bars", "params"],
    "properties": {
        "name": {"type": "string"},
        "universe": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["dsl", "code"]},
        "entry": NULL_STR, "exit": NULL_STR, "code": NULL_STR, "rank_by": NULL_STR, "top_k": NULL_INT,
        "sizing": {"type": "object", "additionalProperties": False,
                   "required": ["method", "weight", "max_weight", "target_vol", "vol_lookback"],
                   "properties": {"method": {"type": "string", "enum": ["fixed", "inverse_vol"]},
                                  "weight": {"type": "number"}, "max_weight": {"type": "number"},
                                  "target_vol": {"type": "number"}, "vol_lookback": {"type": "integer"}}},
        "stop_loss_pct": NULL_NUM, "take_profit_pct": NULL_NUM, "max_hold_bars": NULL_INT,
        "params": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "values"],
            "properties": {"name": {"type": "string"}, "values": {"type": "array", "items": {"type": "number"}}}}},
    },
}

HYPOTHESIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "why", "title", "rationale", "prediction", "falsification", "parent_experiment_id",
                 "holding_period_bars", "edge_bps_per_trade", "lookbacks_in_time", "spec"],
    "properties": {
        "decision": {"type": "string", "enum": ["investigate", "no_idea"]},
        "why": {"type": "string"},
        "holding_period_bars": NULL_INT,
        "edge_bps_per_trade": NULL_NUM,
        "lookbacks_in_time": {"type": "string"},
        "title": {"type": "string"},
        "rationale": {"type": "string"},
        "prediction": {"type": "string"},
        "falsification": {"type": "string"},
        "parent_experiment_id": {**NULL_INT, "description": "The id of the experiment this idea revises "
                                                            "(e.g. 47 for a revision of exp:47); null for a new idea."},
        "spec": {"anyOf": [SPEC_SCHEMA, {"type": "null"}]},
    },
}

BELIEFS = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["statement", "confidence", "evidence"],
    "properties": {"statement": {"type": "string"},
                   "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                   "evidence": {"type": "array", "items": {"type": "string"}}}}}

DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "journal", "lessons", "allocation_pct", "thesis", "exit_plan", "invalidation",
                 "revision_idea", "beliefs"],
    "properties": {
        "decision": {"type": "string", "enum": ["reject", "revise", "allocate"]},
        "journal": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
        "allocation_pct": {"type": "number"},
        "thesis": {"type": "string"},
        "exit_plan": {"type": "string"},
        "invalidation": {"type": "object", "additionalProperties": False,
                         "required": ["max_live_drawdown_pct", "review_after_days", "min_trades_before_judging", "conditions"],
                         "properties": {"max_live_drawdown_pct": {
                                            "type": "number",
                                            "description": "Positive FRACTION of the sleeve's peak equity, e.g. 0.05 "
                                                           "for a 5% drawdown limit (not 5, not -0.05)."},
                                        "review_after_days": {"type": "integer"},
                                        "min_trades_before_judging": {"type": "integer"},
                                        "conditions": {"type": "string"}}},
        "revision_idea": NULL_STR,
        "beliefs": BELIEFS,
    },
}

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["action", "journal", "new_allocation_pct", "lessons", "beliefs"],
    "properties": {
        "action": {"type": "string", "enum": ["continue", "reduce", "retire", "increase"]},
        "journal": {"type": "string"},
        "new_allocation_pct": NULL_NUM,
        "lessons": {"type": "array", "items": {"type": "string"}},
        "beliefs": BELIEFS,
    },
}

ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answer", "citations", "unknowns"],
    "properties": {"answer": {"type": "string"}, "citations": {"type": "array", "items": {"type": "string"}},
                   "unknowns": NULL_STR},
}


ANTI_WHIPSAW_EXAMPLES = (
    "close > sma(close, n) * 1.03",           # entry: clearly above the trend line (buffer)
    "close < sma(close, n) * 0.97",           # exit: clearly below it, not on the first wiggle
    "lowest(close, 8) > sma(close, n)",       # confirmation: above the line for two hours of 15-minute bars
)


def hypothesis_prompt(ctx: dict) -> str:
    import json
    return (
        "TASK: Propose at most ONE hypothesis worth testing now on the venue below, or decide there is none.\n"
        "Build on the research memory: do not repeat a rejected idea unless you change something specific "
        "and say why; revisions should cite the parent experiment id. Testing is cheap and the gates decide, so "
        "propose the most promising idea you have. Answer decision='no_idea' only if every distinct mechanism you can "
        "think of (including alternative DSL signals such as volume/liquidity, ATR/range, volatility contraction or "
        "expansion, relative strength and cross-sectional rank/dispersion) would repeat a recorded failure. "
        "Use kind='code' only when python_strategies_allowed is true and the DSL cannot express the rules. "
        "If decision='no_idea', explain why in 'why', use empty strings for "
        "the text fields and null for spec. Prior holdout reuse ages out over deflation_window_days and is not by "
        "itself a reason to stop proposing genuinely new mechanisms. Cite each experiment you rely on as "
        "experiment:<id>. Explain the mechanism in words; "
        "the journal shows recorded trade counts and their gate thresholds separately.\n"
        "Diversify mechanisms: if the 'mechanism' tags in research_memory show the last experiments on this venue "
        "share one benchmark gate (e.g. market(close) vs a moving average) or one family (reversion, breakout, "
        "low-vol rotation), propose a different mechanism or answer no_idea. Changing a window or threshold of "
        "the same rule is not a new mechanism. Do not default to a blanket benchmark uptrend gate "
        "(market(close > sma(...))) that stays 100% in cash during pullbacks: long-only ideas can also trade "
        "downtrends and choppy markets via short-horizon oversold bounces, volatility-spike fades, or "
        "relative-strength/defensive rotation.\n"
        "Bar arithmetic: read bar_clock. Every window is counted in bars of bar_clock.bar_size, so justify each "
        "lookback in calendar time using bar_clock (e.g. 260 bars is about 10 stock sessions or 2.7 crypto days, "
        "not a year). In 'rationale', state the intended holding period and why the expected edge per trade at "
        "that holding period exceeds the round-trip cost. Fill 'holding_period_bars' (typical bars per trade), "
        "'edge_bps_per_trade' (expected gross edge per round trip, in basis points; it must be at least "
        "trading_costs.min_edge_bps_per_trade, twice the round-trip cost) and 'lookbacks_in_time' (each window in bars and its calendar "
        "meaning). Use null and an empty string only with decision='no_idea'.\n"
        "The market context below is BLINDED: it ends before the earliest possible test window, so whatever "
        "you see cannot leak into the sealed test. The universe must be a subset of the approved symbols.\n"
        "Each round trip costs about trading_costs.approx_round_trip_bps, so an idea must earn more than that per "
        "trade on average at the stated holding period; trade frequency is not a goal in itself. Prefer low turnover: "
        "hold for days rather than hours, especially where costs are high (crypto taker fees make a round trip cost "
        "tens of basis points), because every gate, including the deflated PSR, is scored net of costs. "
        "Capital is allocated only if every "
        "threshold in acceptance_gates passes (they include doubled costs and one bar more "
        "delay than paper trading has), so design for them. When require_universe_excess is set, the test return "
        "must also beat holding the same universe at the same average exposure: the stock universe is today's "
        "survivors, so riding their past rise is not an edge.\n"
        "The mandatory neighbor_positive_frac check perturbs declared params by +/-25% on pre-test data. "
        "Empty params produce no neighbors and cannot pass. Expose at least one meaningful rule lookback or "
        "threshold as a parameter; a single candidate at the original value is enough, without adding a "
        "parameter search. Use every declared DSL parameter in entry, exit or rank_by; unused dummy parameters "
        "are rejected. Nearby values must remain valid. If the idea has no meaningful robustness parameter, "
        "return no_idea rather than running an untestable experiment.\n"
        "Avoid whipsaw: when entry and exit flip on the same line (e.g. close vs one moving average), every wiggle "
        "around it is a costly round trip. Use a buffer between entry and exit and/or require confirmation, e.g. "
        f"entry `{ANTI_WHIPSAW_EXAMPLES[0]}`, exit `{ANTI_WHIPSAW_EXAMPLES[1]}`, or entry `{ANTI_WHIPSAW_EXAMPLES[2]}`. "
        "Estimate trades per year before proposing. Slow rules on one symbol make too few trades to pass "
        "acceptance_gates.min_test_trades (counted across the whole universe) and the top-5 concentration gate; "
        "apply them to several symbols of the approved universe instead. Avoid the zero-trade trap: do not stack "
        "3+ rare simultaneous `and` entry filters that rarely align (keep entry rules concise, 1-2 conditions, so "
        "the idea actually triggers on the test window while still holding long enough to clear round-trip costs).\n"
        "'rationale' = the economic/behavioural reason the edge might exist; 'prediction' = what the test "
        "should show if you are right (be quantitative); 'falsification' = what result would make you drop it.\n\n"
        f"CONTEXT (JSON):\n{json.dumps(ctx, default=str)}")


def decision_prompt(ctx: dict) -> str:
    import json
    return (
        "TASK: The experiment below has finished. The spec was frozen before the test window was evaluated. "
        "Interpret the evidence and decide. If gates.passed is true, the only decision is 'allocate': the gates "
        "admit it, and your allocation_pct (a fraction of account equity) is only a size suggestion; operator "
        "limits cap it, a passing test never gets less than the gate-pass floor, and new strategies start at a "
        "reduced incubation size. 'reject' and 'revise' (describe one specific change worth a new experiment in "
        "revision_idea) are only for experiments whose gates failed. Compare the test with what the hypothesis predicted, compare with the benchmark, and "
        "weigh the challenge results and leakage notes. 'journal' is your note to the operator. 'lessons' are "
        "short, reusable research findings (they become memory for future hypotheses). If allocating, 'thesis' "
        "and 'exit_plan' must say why you are entering and what would make you exit or retire it; "
        "'invalidation' must be measurable. 'beliefs' are your current views about the market with evidence ids "
        "(may be empty). Current (unblinded) market context is included now that the spec is frozen.\n\n"
        f"CONTEXT (JSON):\n{json.dumps(ctx, default=str)}")


def review_prompt(ctx: dict) -> str:
    import json
    return (
        "TASK: Review this live paper-trading strategy against the expectations recorded when it was approved. "
        "The 'shadow_backtest' replays the frozen rules over the live period with modelled costs; the difference "
        "from actual sleeve P&L is implementation shortfall. Decide: continue, reduce, retire, or increase "
        "(increase only if allowed_increase is true; new_allocation_pct is a fraction of equity). Explain in "
        "'journal' whether the original thesis still holds and why. Small samples are noisy: say so rather "
        "than over-reacting, but respect the invalidation conditions.\n\n"
        f"CONTEXT (JSON):\n{json.dumps(ctx, default=str)}")


def answer_prompt(question: str, records: dict) -> str:
    import json
    return (
        "TASK: Answer the operator's question using ONLY the records below. Cite record ids in 'citations'. "
        "If the records do not contain the answer, say what is unknown in 'unknowns' instead of guessing. "
        "Distinguish intended orders from fills. 120 words max unless the question needs more.\n\n"
        f"QUESTION: {question}\n\nRECORDS (JSON):\n{json.dumps(records, default=str)}")
