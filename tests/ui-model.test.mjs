import { strict as assert } from "node:assert";
import { test } from "node:test";
import { deskHealth, displayedActivity, equityComparison, equityInRange, journalMatches, journalPresentation, limitUsage, showAnnualizedReturn } from "../web/ui-model.mjs";

const normal = () => ({
  controls: { emergency_stop: false, system_halt: false, drawdown_halt: false, paused: false },
  worker: { alive: true }, data: { problems: [] },
  mode: { alpaca_configured: true, market_data: "live", llm: "configured" },
  budget: { day_remaining: 5, month_remaining: 40 },
});

test("a connected paper desk is explicitly labelled paper", () => {
  assert.deepEqual(deskHealth(normal()), { tone: "ok", label: "Desk online · paper mode" });
});

for (const [flag, label] of [
  ["emergency_stop", "Emergency stop active"], ["system_halt", "Ledger integrity halt"],
  ["drawdown_halt", "Drawdown limit reached"],
]) {
  test(`${flag} cannot be hidden behind a healthy worker`, () => {
    const s = normal(); s.controls[flag] = true;
    assert.deepEqual(deskHealth(s), { tone: "bad", label });
  });
}

test("emergency stop takes precedence over pause and data problems", () => {
  const s = normal(); s.controls.emergency_stop = s.controls.paused = true; s.data.problems.push({});
  assert.equal(deskHealth(s).label, "Emergency stop active");
});

test("an offline worker is never reported as a paused-but-healthy worker", () => {
  const s = normal(); s.worker.alive = false; s.controls.paused = true;
  assert.deepEqual(deskHealth(s), { tone: "bad", label: "Worker offline" });
});

test("pause makes it clear that exit monitoring continues", () => {
  const s = normal(); s.controls.paused = true;
  assert.deepEqual(deskHealth(s), { tone: "warn", label: "Agent paused · exits monitored" });
});

test("stale data warns without calling the desk offline", () => {
  const s = normal(); s.data.problems.push({ key: "bars:AAPL" });
  assert.deepEqual(deskHealth(s), { tone: "warn", label: "Market data needs attention" });
});

test("missing stock-data credentials warn only for non-synthetic data", () => {
  const s = normal(); s.mode.alpaca_configured = false;
  assert.equal(deskHealth(s).tone, "warn");
  s.mode.market_data = "synthetic";
  assert.equal(deskHealth(s).tone, "ok");
});

test("exhausting either live AI budget warns", () => {
  for (const key of ["day_remaining", "month_remaining"]) {
    const s = normal(); s.budget[key] = 0;
    assert.equal(deskHealth(s).label, "AI budget exhausted");
  }
});

test("the rules analyst does not falsely require an AI budget", () => {
  const s = normal(); s.mode.llm = "demo"; s.budget.day_remaining = s.budget.month_remaining = 0;
  assert.equal(deskHealth(s).tone, "ok");
});

test("risk-meter ratios clamp safely, including negative drawdown", () => {
  assert.equal(limitUsage(-0.05, 0.1), 50);
  assert.equal(limitUsage(0.4, 0.8), 50);
  assert.equal(limitUsage(1.2, 0.8), 100);
  assert.equal(limitUsage(0, 0.8), 0);
});

test("missing or invalid limits cannot produce NaN/Infinity CSS widths", () => {
  for (const [value, limit] of [[0.1, 0], [0.1, -1], [null, 1], [NaN, 1], [1, undefined], [Infinity, 1]]) {
    assert.equal(limitUsage(value, limit), 0);
  }
});

const now = 100 * 86400;
const rows = [40, 69, 70, 92, 93, 98, 99, 100].map((day) => ({ ts: day * 86400, equity: day }));
for (const [range, days] of [
  ["day", [99, 100]], ["week", [93, 98, 99, 100]], ["month", [70, 92, 93, 98, 99, 100]],
]) {
  test(`${range} range includes the boundary and preserves chronological order`, () => {
    assert.deepEqual(equityInRange(rows, range, now).map((row) => row.equity), days);
    assert.equal(rows.length, 8); // Filtering must never mutate the cached history.
  });
}

test("All retains every real snapshot without inventing history", () => {
  assert.equal(equityInRange(rows, "all", now), rows);
  assert.deepEqual(equityInRange([], "all", now), []);
});

test("a stale account has an empty recent range rather than relabelled old data", () => {
  assert.deepEqual(equityInRange(rows, "day", now + 2 * 86400), []);
});

test("check-in and activity ignore a retained worker schedule through stop, clear and resume", () => {
  const s = normal();
  s.agent = { activity: "waiting", next_actions: [{ what: "Next signal evaluation", at: 12345 }] };
  s.controls.emergency_stop = s.controls.paused = true;
  assert.equal(displayedActivity(s).activity, "stopped");
  assert.deepEqual(displayedActivity(s).next_actions, [{ what: "Waiting for the emergency stop to be cleared." }]);
  s.controls.emergency_stop = false;
  assert.equal(displayedActivity(s).activity, "paused");
  assert.equal(displayedActivity(s).next_actions[0].at, undefined);
  s.controls.paused = false;
  assert.deepEqual(displayedActivity(s).next_actions, [{ what: "Next signal evaluation", at: 12345 }]);
  s.worker.alive = false;
  assert.equal(displayedActivity(s).activity, "offline");
  assert.equal(displayedActivity(s).next_actions[0].at, undefined);
});

test("benchmark comparison uses both series' first and last chart snapshots", () => {
  const snapshots = [{ ts: 1, equity: 100, benchmark_equity: 50 }, { ts: 2, equity: 110, benchmark_equity: 60 }];
  const result = equityComparison(snapshots);
  assert.ok(Math.abs(result.account - 0.1) < 1e-12);
  assert.ok(Math.abs(result.benchmark - 0.2) < 1e-12);
  assert.ok(Math.abs(result.difference + 0.1) < 1e-12);
  assert.equal(equityComparison(snapshots.slice(0, 1)), null);
  assert.equal(equityComparison([{ ...snapshots[0], benchmark_equity: null }, snapshots[1]]), null);
});

test("CAGR is never shown for an unknown window or one shorter than 90 days", () => {
  const start = 100, end = start + 90 * 86400;
  assert.equal(showAnnualizedReturn(), false);
  assert.equal(showAnnualizedReturn({ start, end: end - 1 }), false);
  assert.equal(showAnnualizedReturn({ start, end }), true);
});

test("journal display distinguishes both historical and new probes from promotion decisions", () => {
  assert.deepEqual(journalPresentation({ kind: "decision", title: "Rejected: Momentum" }),
    { kind: "decision", title: "Not promoted: Momentum" });
  assert.deepEqual(journalPresentation({ kind: "decision", title: "Rejected: Momentum", author: "rules",
    body: "Failed gates: test_sharpe, test_trades. I am rejecting it." }),
    { kind: "decision", title: "Not promoted (missed 2 gates): Momentum" });
  assert.deepEqual(journalPresentation({ kind: "decision", title: "New probe live: Momentum" }),
    { kind: "probe", title: "Probe started: Momentum" });
  assert.deepEqual(journalPresentation({ kind: "decision", title: "Probe started: Momentum", refs: { probe: true } }),
    { kind: "probe", title: "Probe started: Momentum" });
  assert.deepEqual(journalPresentation({ kind: "order", title: "Rejected: Momentum" }),
    { kind: "order", title: "Rejected: Momentum" });
});

test("journal search matches displayed titles, text, kinds and record IDs without case sensitivity", () => {
  const entry = { kind: "decision", title: "Rejected: Momentum", body: "Missed test_sharpe.", refs: { experiment_id: 17 } };
  assert.equal(journalMatches(entry, "  NOT promoted  #17 "), true);
  assert.equal(journalMatches(entry, "Decision sharpe"), true);
  assert.equal(journalMatches(entry, "momentum #71"), false);
});

test("trading verdict explains why nothing trades", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const base = () => ({ controls: { emergency_stop: false, paused: false }, worker: { alive: true }, positions: [],
    pipeline: { finished: 4, live: { strategies: 0, probes: 0 }, gate_failures: [{ gate: "test_sharpe", n: 4 }],
      candidates: [{ id: 7, test_return: -0.02 }] } });
  assert.match(tradingVerdict(base()).text, /None of the 4 experiments made money.*Test Sharpe \(4×\)/);
  const s = base(); s.pipeline.candidates[0] = { id: 9, test_return: 0.031, probe_blocker: "3 probe(s) already running (limit 3)" };
  assert.match(tradingVerdict(s).text, /#9 made \+3\.1%.*already running/);
  s.pipeline.live.probes = 1; assert.equal(tradingVerdict(s).tone, "probe");  // probes are not evidence: never success green
  s.pipeline.live.strategies = 1; assert.equal(tradingVerdict(s).tone, "ok"); s.pipeline.live.strategies = 0;
  s.positions = [{}]; assert.match(tradingVerdict(s).text, /^Trading: 1 open position across 1 probe\./);
  s.controls.emergency_stop = true; assert.equal(tradingVerdict(s).tone, "bad");
});

test("probe-only positions are labelled as forward tests, not an edge", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const s = { controls: {}, worker: { alive: true }, positions: [{}], pipeline: { live: { strategies: 0, probes: 1 } } };
  const v = tradingVerdict(s);
  assert.equal(v.tone, "probe");
  assert.match(v.text, /not evidence of an edge/);
});

test("raw gate keys in server text become the same labels the pipeline uses", async () => {
  const { friendlyGates, gateLabel, gateCount } = await import("../web/ui-model.mjs");
  assert.equal(friendlyGates("Failed gates: validation_sharpe, test_sharpe_delay_1bar, test_sharpe."),
    "Failed gates: Validation Sharpe, 1-bar delay, Test Sharpe.");
  assert.equal(gateLabel("test_trades"), "Min trades");
  assert.equal(gateLabel("brand_new_gate"), "brand new gate");
  assert.equal(gateCount(9, 4), "5 of 9 passed");
  assert.equal(gateCount(9, 0), "9 of 9 passed");
});

test("limits are shown as percentages and read back as exact fractions", async () => {
  const { toDisplay, fromDisplay, formatLimit } = await import("../web/ui-model.mjs");
  assert.equal(toDisplay(0.07, "pct"), "7");
  assert.equal(toDisplay(0.025, "pct"), "2.5");
  assert.deepEqual(fromDisplay("7", "pct"), { value: 0.07 });
  assert.deepEqual(fromDisplay("12.5%", "pct", [0, 1]), { value: 0.125 });
  assert.match(fromDisplay("150", "pct", [0, 1]).error, /Allowed range is 0% to 100%/);
  assert.match(fromDisplay("", "pct").error, /Enter a value/);
  assert.match(fromDisplay("abc", "usd").error, /Enter a number/);
  assert.match(fromDisplay("2.5", "int").error, /whole number/);
  assert.equal(formatLimit(20000, "usd"), "$20,000");
  assert.equal(formatLimit(7, "hour"), "07:00 UTC");
});

test("loosening is detected in the direction that adds risk", async () => {
  const { changeDirection, LIMIT_FIELDS } = await import("../web/ui-model.mjs");
  const f = (k) => LIMIT_FIELDS.find((x) => x.k === k);
  assert.equal(changeDirection(f("max_drawdown_pct"), 0.1, 0.25), "loosens");
  assert.equal(changeDirection(f("max_drawdown_pct"), 0.1, 0.05), "tightens");
  assert.equal(changeDirection(f("min_test_sharpe"), 0.5, 0), "loosens");
  assert.equal(changeDirection(f("min_test_trades"), 8, 20), "tightens");
  assert.equal(changeDirection(f("enabled"), false, true), "loosens");
  assert.equal(changeDirection(f("crypto_research_hours"), 2, 8), "changes");
  for (const field of LIMIT_FIELDS) assert.ok([-1, 0, 1].includes(field.loosens), field.k);
});

test("a paused desk never reads as waiting to trade, in any live state", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const mk = (live, positions) => ({ controls: { paused: true }, worker: { alive: true }, positions,
    pipeline: { finished: 3, live, candidates: [], gate_failures: [] } });
  for (const [live, positions] of [[{ strategies: 0, probes: 0 }, []], [{ strategies: 0, probes: 1 }, []],
    [{ strategies: 2, probes: 1 }, []], [{ strategies: 1, probes: 0 }, [{}, {}]]]) {
    const v = tradingVerdict(mk(live, positions));
    assert.equal(v.tone, "warn");
    assert.match(v.text, /^Paused: no research and no new entries\./);
    assert.doesNotMatch(v.text, /Waiting for entry|Trading:/);
  }
  assert.match(tradingVerdict(mk({ strategies: 0, probes: 1 }, [])).text, /1 probe keeps its allocation/);
  assert.match(tradingVerdict(mk({ strategies: 1, probes: 0 }, [{}, {}])).text, /2 open positions are held/);
});

test("each control state outranks the next: stop, worker, halt, pause", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const s = { controls: { emergency_stop: true, paused: true, drawdown_halt: true }, worker: { alive: false }, positions: [{}],
    pipeline: { live: { strategies: 1, probes: 0 } } };
  assert.match(tradingVerdict(s).text, /^Emergency stop/);
  s.controls.emergency_stop = false; assert.match(tradingVerdict(s).text, /worker is offline/);
  s.worker.alive = true; assert.match(tradingVerdict(s).text, /risk halt/);
  s.controls.drawdown_halt = false; assert.match(tradingVerdict(s).text, /^Paused/);
  s.controls.paused = false; assert.match(tradingVerdict(s).text, /^Trading: 1 open position across 1 strategy\./);
});

test("a live probe on a closed market is not described as waiting for the next bar", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const s = { controls: {}, worker: { alive: true }, positions: [], pipeline: { finished: 2, live: { strategies: 0, probes: 1 },
    candidates: [], gate_failures: [] }, strategies: [{ venue: "NASDAQ", status: "active", probe: 1 }],
    sessions: { NASDAQ: { open: false, opens_at: 123 }, CRYPTO: { open: true } } };
  const closed = tradingVerdict(s);
  assert.equal(closed.tone, "probe");
  assert.match(closed.text, /^1 probe live, but the stock market is closed, so entries wait for the next session\./);
  assert.doesNotMatch(closed.text, /next bar close/);
  s.sessions.NASDAQ.open = true;
  assert.match(tradingVerdict(s).text, /Waiting for entry signals on the next bar close/);
  // Mixed venues keep the desk-wide sentence; no single market explains the wait.
  s.sessions.NASDAQ.open = false;
  s.strategies.push({ venue: "CRYPTO", status: "active", probe: 1 });
  s.pipeline.live.probes = 2;
  assert.match(tradingVerdict(s).text, /^2 probes live\. Waiting for entry signals on the next bar close/);
});

test("live labels count strategies and probes separately", async () => {
  const { liveLabel } = await import("../web/ui-model.mjs");
  assert.equal(liveLabel(1, 2), "1 strategy and 2 probes");
  assert.equal(liveLabel(0, 1), "1 probe");
  assert.equal(liveLabel(3, 0), "3 strategies");
});

test("verdict names an unallocated gate pass, never a dead bar-size winner", async () => {
  const { tradingVerdict } = await import("../web/ui-model.mjs");
  const dead = { id: 11, test_return: 0.21, bar_size_mismatch: "it was tested on a different bar size" };
  const s = { controls: { emergency_stop: false, paused: false }, worker: { alive: true }, positions: [],
    pipeline: { finished: 60, profitable: 19, profitable_current_bar: 18, live: { strategies: 0, probes: 0 }, gate_failures: [],
      candidates: [{ id: 47, test_return: 0.059, sharpe: 1.17, passed: true, allocated: false }, dead] } };
  const v = tradingVerdict(s).text;
  assert.match(v, /#47 passed every gate \(test \+5\.9%, Sharpe 1\.17\) but was not allocated/);
  assert.doesNotMatch(v, /#11|waiting/i);
  s.pipeline.live.probes = 1;
  assert.match(tradingVerdict(s).text, /#47 passed every gate .* not allocated\./);
  s.pipeline = { finished: 3, profitable: 1, profitable_current_bar: 0, live: { strategies: 0, probes: 0 }, gate_failures: [],
    candidates: [dead] };
  assert.match(tradingVerdict(s).text, /^No experiment on the current bar size made money.*1 profitable one was tested on another bar size/);
  assert.doesNotMatch(tradingVerdict(s).text, /#11/);
});

test("an uninvested account is not said to beat or trail the benchmark", () => {
  const rows = [{ ts: 10, equity: 100000, benchmark_equity: 100, gross: 0 }, { ts: 20, equity: 100000, benchmark_equity: 98, gross: 0 }];
  assert.equal(equityComparison(rows, null).uninvested, true);
  assert.equal(equityComparison(rows, 5).uninvested, true);     // last fill before the range
  assert.equal(equityComparison(rows, 15).uninvested, false);   // a fill inside the range
  assert.equal(equityComparison([rows[0], { ...rows[1], gross: 500 }], null).uninvested, false);
});

test("universe text parses to clean symbols and a diff", async () => {
  const { parseSymbols, symbolDiff } = await import("../web/ui-model.mjs");
  assert.deepEqual(parseSymbols(" aapl, msft\nSPY;; aapl  btc-usd "), ["AAPL", "MSFT", "SPY", "BTC-USD"]);
  assert.deepEqual(parseSymbols(""), []);
  assert.deepEqual(symbolDiff(["QQQ", "TQQQ", "AAPL"], ["QQQ", "AAPL", "MSFT"]), { added: ["MSFT"], removed: ["TQQQ"] });
});
import { holdoutLabel } from "../web/ui-model.mjs";
import { checkinSummary } from "../web/ui-model.mjs";

test("cash-only NASDAQ probe shows its own market event unless paused", () => {
  const crypto = { what: "Crypto bar", venue: "CRYPTO", kind: "signal" };
  const stocks = { what: "Stock market opens", venue: "NASDAQ", kind: "market_open" };
  const s = { ...normal(), positions: [], account: { cash: 100000, gross: 0 }, counts: { orders: 0, fills: 0 },
    strategies: [{ venue: "NASDAQ", status: "active" }], pipeline: { live: { probes: 1 } },
    agent: { next_actions: [crypto, stocks] } };
  assert.equal(checkinSummary(s).next, stocks);
  assert.match(checkinSummary(s).text, /^All \$100,000 is in cash\..*No orders or fills yet\.$/);
  s.counts.orders = 1;
  assert.doesNotMatch(checkinSummary(s).text, /No orders/);
  s.agent.next_actions.pop();
  assert.equal(checkinSummary(s).nextLabel, "Next desk event");
  s.controls.paused = true;
  assert.match(checkinSummary(s).text, /^Paused/);
  assert.match(checkinSummary(s).next.what, /resumed/);
});

test("window overlap distinguishes recorded zero from missing evidence", () => {
  assert.equal(holdoutLabel(0, 7), "0 overlapping tests · 7-day lookback");
  assert.equal(holdoutLabel(null), "Test window overlap unknown");
  assert.equal(holdoutLabel(undefined), "Test window overlap unknown");
  assert.equal(holdoutLabel(1), "1 overlapping test · lookback not recorded");
  assert.equal(holdoutLabel(29, 60), "29 overlapping tests · 60-day lookback");
});

import { gateValue, gateThreshold, gateFailCounts, scatterLayout, headroom, paletteMatch } from "../web/ui-model.mjs";

test("gate values read in their own units", () => {
  assert.equal(gateValue("test_trades", 12.0), "12");
  assert.equal(gateValue("test_sharpe", -0.314), "−0.31");
  assert.equal(gateValue("test_return_cost_x2", 0.0234), "+2.3%");
  assert.equal(gateValue("neighbor_positive_frac", 0.667), "67%");
  assert.equal(gateValue("deflated_psr", 0.25), "0.25");
  assert.equal(gateValue("test_sharpe", null), "–");
  assert.equal(gateThreshold({ name: "top5_concentration", op: "<=", threshold: 1 }), "≤ 100%");
});

test("gate failure counts ignore unevaluated experiments and unknown gates", () => {
  const n = gateFailCounts([{ gates: { failed: ["deflated_psr", "test_trades"] } }, { gates: { failed: ["deflated_psr", "causal_dsl"] } }, { gates: null }]);
  assert.equal(n.deflated_psr, 2);
  assert.equal(n.test_trades, 1);
  assert.equal(n.test_sharpe, 0);
  assert.equal("causal_dsl" in n, false);
});

test("scatter keeps break-even inside the box and skips untested ideas", () => {
  const { points, x0, y0 } = scatterLayout([{ test: { sharpe: 1, total_return: 0.1 } }, { test: { sharpe: 2, total_return: 0.2 } }, { test: null }]);
  assert.equal(points.length, 2);
  assert.equal(x0, 8); assert.equal(y0, 92);  // all-positive data: zero sits at the lower-left edge
  assert.ok(points.every((p) => p.x >= 8 && p.x <= 92 && p.y >= 8 && p.y <= 92));
  assert.deepEqual(scatterLayout([]), { points: [], x0: null, y0: null });
});

test("headroom shows the share of each cap in use", () => {
  const h = Object.fromEntries(headroom({ account: { equity: 100000, exposure: 0.4, crypto_gross: 5000, drawdown: -0.05 },
    limits: { max_gross_pct: 0.8, max_crypto_pct: 0.25, max_total_allocation_pct: 0.8, max_drawdown_pct: 0.1 },
    budget: { month_spent: 1, monthly_cap: 5 }, strategies: [{ allocation: 20000 }, { allocation: 4000 }] }).map((g) => [g.key, g.used]));
  assert.deepEqual(h, { gross: 50, crypto: 20, alloc: 30, drawdown: 50, ai: 20 });
  assert.equal(headroom({}).find((g) => g.key === "ai").used, null);
});

test("palette needs every word", () => {
  assert.ok(paletteMatch({ group: "Experiment", label: "#12 Intraday trend BTC" }, "exp trend"));
  assert.ok(!paletteMatch({ group: "Screen", label: "Research" }, "research btc"));
});
