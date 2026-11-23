// Small, dependency-free display models shared by the UI and its regression tests.
export function limitUsage(value, limit) {
  if (!Number.isFinite(value) || !Number.isFinite(limit) || limit <= 0) return 0;
  return Math.min(100, Math.max(0, Math.abs(value) / limit * 100));
}

export function equityInRange(rows, range, now) {
  const seconds = { day: 86400, week: 7 * 86400, month: 30 * 86400 }[range];
  if (!seconds) return rows;
  return rows.filter((row) => row.ts >= now - seconds);
}

// Compare the same snapshot endpoints shown in the selected chart range.
// `uninvested`: no exposure in any snapshot and no fill since the range began, so the gap is cash vs. QQQ, not skill.
export function equityComparison(rows, lastFillTs = null) {
  const first = rows[0], last = rows.at(-1);
  if (!first || first === last || last.ts <= first.ts || !(first.equity > 0) || !(first.benchmark_equity > 0) ||
      !Number.isFinite(last.equity) || !(last.benchmark_equity > 0)) return null;
  const account = last.equity / first.equity - 1, benchmark = last.benchmark_equity / first.benchmark_equity - 1;
  const uninvested = rows.every((r) => !(r.gross > 0)) && !(lastFillTs >= first.ts);
  // The benchmarks at the desk's own exposure: between snapshots, hold the previous snapshot's stock share
  // of equity in the stock benchmark (QQQ), its crypto share in BTC, and the rest in cash.
  let matched = 1, stockW = 0, cryptoW = 0, span = 0;
  const move = (a, b, key) => a[key] > 0 && b[key] > 0 ? b[key] / a[key] - 1 : 0;
  for (let i = 0; i + 1 < rows.length; i++) {
    const a = rows[i], b = rows[i + 1], dt = b.ts - a.ts;
    if (!(dt > 0)) continue;
    const eq = a.equity > 0 ? a.equity : 0;
    const crypto = eq && a.crypto_gross > 0 ? Math.min(1, a.crypto_gross / eq) : 0;
    const stock = eq && a.gross > 0 ? Math.min(1 - crypto, Math.max(0, (a.gross - (a.crypto_gross || 0)) / eq)) : 0;
    matched *= 1 + stock * move(a, b, "benchmark_equity") + crypto * move(a, b, "crypto_benchmark_equity");
    stockW += stock * dt; cryptoW += crypto * dt; span += dt;
  }
  const stockExposure = span > 0 ? stockW / span : 0, cryptoExposure = span > 0 ? cryptoW / span : 0;
  return { account, benchmark, difference: account - benchmark, uninvested,
           avgExposure: stockExposure + cryptoExposure, stockExposure, cryptoExposure,
           matched: matched - 1, matchedDifference: account - (matched - 1) };
}

export function showAnnualizedReturn(window) {
  return Number.isFinite(window?.start) && Number.isFinite(window?.end) && window.end - window.start >= 90 * 86400;
}

// Old journal rows remain immutable. Translate their display copy alongside newly written rows.
export function journalPresentation(entry) {
  const title = String(entry.title ?? "");
  if (entry.kind !== "decision") return { kind: entry.kind, title };
  if (entry.refs?.probe === true || /^(?:New probe live|Probe (?:started|approved, waiting for room)):/i.test(title)) {
    return { kind: "probe", title: title.replace(/^New probe live:/i, "Probe started:") };
  }
  if (!/^Rejected:/i.test(title)) return { kind: entry.kind, title };
  const body = String(entry.body ?? "");
  // Recover an exact count only from the original rules engine or gate override templates.
  const failed = /\[Risk\/gate engine\] Allocation refused: gates failed \(([a-z0-9_, ]+)\)\./.exec(body)?.[1] ||
    (entry.author === "rules" ? /Failed gates: ([a-z0-9_, ]+)\. I am rejecting it\./.exec(body)?.[1] : null);
  const names = failed?.split(", ") || [];
  const count = names.length && names.every((name) => Object.hasOwn(GATE_LABEL, name)) ? names.length : 0;
  return { kind: entry.kind, title: title.replace(/^Rejected:/i, count ? `Not promoted (missed ${count} ${count === 1 ? "gate" : "gates"}):` : "Not promoted:") };
}

export function journalMatches(entry, query) {
  const { kind, title } = journalPresentation(entry);
  const refs = Object.entries(entry.refs || {}).flatMap(([key, id]) => [key.replace(/_id$/, ""), `#${id}`]);
  if (entry.llm_call_id) refs.push("model inputs", `#${entry.llm_call_id}`);
  const text = [title, entry.title, entry.body, kind, String(entry.author ?? "").replaceAll("_", " "), ...refs].map(friendlyGates).join(" ").toLowerCase();
  return query.toLowerCase().trim().split(/\s+/).every((word) => text.includes(word));
}

// "<strategy>: waiting for the <time> <bar> bar" is written once per strategy for the same bar.
// Consecutive ones for one bar read as one row: which bar, and who is waiting for it.
const WAITING = /^(.+): waiting for the (.+ bar)$/;
export function groupJournal(entries) {
  const out = [];
  for (const entry of entries) {
    const m = entry.kind === "data" ? WAITING.exec(String(entry.title ?? "")) : null;
    const last = out.at(-1);
    if (m && last?.bar === m[2]) { last.entries.push(entry); last.names.push(m[1]); continue; }
    out.push(m ? { bar: m[2], entries: [entry], names: [m[1]] } : { entry });
  }
  return out.map((item) => item.entries?.length === 1 ? { entry: item.entries[0] } : item);
}

export function deskHealth(s) {
  const c = s.controls;
  if (c.emergency_stop) return { tone: "bad", label: "Emergency stop active" };
  if (c.system_halt) return { tone: "bad", label: "Ledger integrity halt" };
  if (c.drawdown_halt) return { tone: "bad", label: "Drawdown limit reached" };
  if (!s.worker.alive) return { tone: "bad", label: "Worker offline" };
  if (c.paused) return { tone: "warn", label: "Agent paused · exits monitored" };
  if (s.data.problems.length || (!s.mode.alpaca_configured && s.mode.market_data !== "synthetic")) {
    return { tone: "warn", label: "Market data needs attention" };
  }
  if (s.mode.llm !== "demo" && (s.budget.day_remaining <= 0 || s.budget.month_remaining <= 0)) {
    return { tone: "warn", label: "AI budget exhausted" };
  }
  return { tone: "ok", label: "Desk online · paper mode" };
}

// Worker schedules are snapshots and may predate an operator action. Both dashboard
// summaries must prefer the current controls over what the worker last planned.
export function displayedActivity(s) {
  const waiting = (activity, detail, what) => ({ activity, detail, next_actions: [{ what }] });
  if (s.controls.emergency_stop) return waiting("stopped", "Research, new orders and fills are halted.",
    "Waiting for the emergency stop to be cleared.");
  if (!s.worker.alive) return waiting("offline", "The worker is offline.", "Waiting for the worker to reconnect.");
  if (s.controls.paused) return waiting("paused", "No new research or positions. Exits and risk monitoring continue.",
    "Scheduled research resumes when the agent is resumed.");
  return s.agent || {};
}

// ---------------------------------------------------------------- gates
// One vocabulary for gate names everywhere, including text the server or the analyst wrote.
export const GATE_LABEL = {
  validation_sharpe: "Validation Sharpe", test_sharpe: "Test Sharpe", test_trades: "Min trades",
  test_return_cost_x2: "2× costs", test_sharpe_delay_1bar: "1-bar delay", neighbor_positive_frac: "Similar settings",
  subperiod_positive_frac: "Sub-periods", top5_concentration: "Top-5 trades", deflated_psr: "Deflated PSR",
  universe_excess_return: "Beats its universe",
};
export const gateLabel = (name) => GATE_LABEL[name] || String(name ?? "").replaceAll("_", " ");
const GATE_KEYS = Object.keys(GATE_LABEL).sort((a, b) => b.length - a.length);
const GATE_RE = new RegExp(`\\b(${GATE_KEYS.join("|")})\\b`, "g");
export function friendlyGates(text) {
  return String(text ?? "").replace(GATE_RE, (key) => GATE_LABEL[key]);
}
// Always counted the same way: how many passed, out of how many.
export function holdoutLabel(n, days, lifetime) {
  if (!Number.isSafeInteger(n) || n < 0) {
    return Number.isSafeInteger(lifetime) && lifetime >= 0 ? `${lifetime} overlapping ${lifetime === 1 ? "test" : "tests"} overall` : "Test window overlap unknown";
  }
  const scope = Number.isSafeInteger(days) && days > 0 ? `${days}-day lookback` : "lookback not recorded";
  const overall = Number.isSafeInteger(lifetime) && lifetime !== n ? ` · ${lifetime} overall` : "";
  return `${n} overlapping ${n === 1 ? "test" : "tests"} · ${scope}${overall}`;
}

// A revision next to the experiment it revises: what changed in the rules, the dates, the costs and the gates.
const RULE_FIELDS = [["entry", "Entry"], ["exit", "Exit"], ["rank_by", "Rank by"], ["top_k", "Top K"], ["universe", "Universe"],
  ["params", "Parameter grid"], ["sizing", "Sizing"], ["stop_loss_pct", "Stop loss"], ["take_profit_pct", "Take profit"],
  ["max_hold_bars", "Max hold (bars)"], ["code", "Custom code"]];
const stable = (v) => v == null || v === "" ? null : typeof v === "object" ? JSON.stringify(Array.isArray(v) ? v : Object.fromEntries(Object.entries(v).sort())) : v;
export function compareExperiments(child, parent) {
  const cs = child.spec || {}, ps = parent.spec || {};
  const rules = RULE_FIELDS.map(([key, label]) => ({ key, label, parent: ps[key] ?? null, child: cs[key] ?? null,
    changed: stable(ps[key]) !== stable(cs[key]) })).filter((r) => r.changed || (r.parent ?? r.child) != null);
  const pu = new Set(ps.universe || []), cu = new Set(cs.universe || []);
  const universe = { added: [...cu].filter((x) => !pu.has(x)), removed: [...pu].filter((x) => !cu.has(x)) };
  rules.push({ key: "chosen_params", label: "Chosen on train", parent: parent.chosen_params ?? null, child: child.chosen_params ?? null,
    changed: stable(parent.chosen_params) !== stable(child.chosen_params) });
  const pw = parent.windows?.test, cw = child.windows?.test;
  const overlap = pw && cw ? Math.max(0, Math.min(pw.end, cw.end) - Math.max(pw.start, cw.start)) : 0;
  const dates = { parent: pw || null, child: cw || null,
    overlap: pw && cw && cw.end > cw.start ? overlap / (cw.end - cw.start) : null };
  const METRICS = [["total_return", "Net return"], ["sharpe", "Sharpe"], ["n_trades", "Trades"], ["max_drawdown", "Max drawdown"],
    ["cost_drag", "Cost drag"], ["fees_paid", "Fees paid"], ["slippage_paid", "Spread + impact paid"], ["exposure", "Exposure"],
    ["deflated_psr", "Deflated PSR"]];
  const metrics = METRICS.map(([key, label]) => ({ key, label, parent: parent.test?.[key] ?? null, child: child.test?.[key] ?? null }));
  const byName = (g) => new Map((g?.checks || []).map((c) => [c.name, c]));
  const pg = byName(parent.gates), cg = byName(child.gates);
  const gates = [...new Set([...pg.keys(), ...cg.keys()])].map((name) => {
    const a = pg.get(name), b = cg.get(name);
    return { name, parent: a ? a.pass : null, child: b ? b.pass : null, parentValue: a?.value ?? null, childValue: b?.value ?? null,
      flipped: a != null && b != null && a.pass !== b.pass };
  });
  return { rules, universe, dates, metrics, gates };
}

export function gateCount(total, failed) {
  return `${Math.max(0, total - failed)} of ${total} passed`;
}

const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
// "1 strategy and 2 probes", never "3 strategies" when some of them are probes.
export function liveLabel(strategies, probes) {
  const part = (n, one, many) => n ? `${n} ${n === 1 ? one : many}` : "";
  return [part(strategies, "strategy", "strategies"), part(probes, "probe", "probes")].filter(Boolean).join(" and ") || "nothing";
}
// A probe on a closed market is not waiting for the next bar anywhere; name the market instead.
const MARKET = { NASDAQ: "stock market", CRYPTO: "crypto market" };
function singleLiveVenue(s) {
  const venues = new Set((s.strategies || [])
    .filter((x) => x.status === "active" || x.status === "incubating")
    .map((x) => x.venue).filter((v) => Object.hasOwn(MARKET, v)));
  return venues.size === 1 ? [...venues][0] : null;
}

export function checkinSummary(s, verdict = tradingVerdict(s)) {
  const actions = displayedActivity(s).next_actions || [];
  const venue = singleLiveVenue(s);
  const normal = s.worker.alive && !["paused", "emergency_stop", "drawdown_halt", "system_halt"].some(k => s.controls[k]);
  const marketEvent = normal && venue && actions.find(a => a.venue === venue && ["signal", "market_open"].includes(a.kind));
  let text = verdict.text;
  if (normal && s.positions.length === 0 && s.account?.gross === 0 && Number.isFinite(s.account.cash)) {
    const cash = s.account.cash.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 0, maximumFractionDigits: 2 });
    text = `All ${cash} is in cash. ${text}`;
    if (s.counts?.orders === 0 && s.counts?.fills === 0) text += " No orders or fills yet.";
  }
  return { text, next: marketEvent || actions[0], nextLabel: marketEvent ? `Next · ${venue}` : "Next desk event" };
}

// One sentence that answers "why is (or isn't) the desk trading right now?".
// Green is kept for evidence (a strategy that passed every gate); probes get their own tone.
export function tradingVerdict(s) {
  const c = s.controls, p = s.pipeline || {};
  const strategies = p.live?.strategies || 0, probes = p.live?.probes || 0, live = strategies + probes;
  const liveTone = strategies ? "ok" : "probe";
  const fmt = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(1) + "%";
  if (c.emergency_stop) return { tone: "bad", text: "Emergency stop is on, so nothing trades." };
  if (!s.worker.alive) return { tone: "bad", text: "The worker is offline, so nothing is researched or traded." };
  if (c.drawdown_halt || c.system_halt) return { tone: "bad", text: "A risk halt blocks new positions; exits still run." };
  const n = s.positions.length, held = `${n} open position${n === 1 ? "" : "s"}`;
  // Candidates arrive ranked by the server: current bar size, then passed-not-allocated, then probe-eligible.
  const best = (p.candidates || [])[0];
  const current = best && !best.bar_size_mismatch ? best : null;
  const idlePass = current?.passed && !current.allocated ? current : null;
  const passNote = idlePass ? ` #${idlePass.id} passed every gate (test ${fmt(idlePass.test_return)}) but was not allocated.` : "";
  // Pause outranks "live": a paused desk must never read as if it is about to trade.
  if (c.paused) return { tone: "warn", text: `Paused: no research and no new entries.${n ? ` ${held} ${n === 1 ? "is" : "are"} held; exits still run.` :
    live ? ` ${liveLabel(strategies, probes)} keep${live === 1 ? "s" : ""} ${live === 1 ? "its" : "their"} allocation; exits still run.` : ""}` };
  if (n) return { tone: liveTone, text: `Trading: ${held} across ${liveLabel(strategies, probes)}.` +
    (strategies ? "" : " Probes are forward paper tests, not evidence of an edge.") + passNote };
  if (live) {
    // The check-in pairs this verdict with the next desk event, which may be in another
    // market. When every live strategy is on one closed venue, say so instead of implying
    // the next bar close will fill it.
    const venue = singleLiveVenue(s), session = venue ? s.sessions?.[venue] : null;
    if (session && session.open === false) return { tone: liveTone, text:
      `${cap(liveLabel(strategies, probes))} live, but the ${MARKET[venue]} is closed, so entries wait for the next session.` +
      (strategies ? "" : " Forward test only, not evidence of an edge.") + passNote };
    return { tone: liveTone, text: `${cap(liveLabel(strategies, probes))} live. Waiting for entry signals on the next bar close.` +
      (strategies ? "" : " Forward test only, not evidence of an edge.") + passNote };
  }
  if (!p.finished) return { tone: "info", text: "No finished experiment in the last 7 days yet. The first research cycle needs market data." };
  const top = (p.gate_failures || [])[0];
  if (idlePass) return { tone: "warn", text: `Experiment #${idlePass.id} passed every gate (test ${fmt(idlePass.test_return)}, Sharpe ${Number(idlePass.sharpe ?? 0).toFixed(2)}) but was not allocated, so nothing trades.` };
  if (current && current.test_return > 0) {
    if (current.probe_blocker) return { tone: "warn", text: `Best idea #${current.id} made ${fmt(current.test_return)} on its sealed test, but no probe started: ${current.probe_blocker}.` };
    return { tone: "info", text: `Best idea #${current.id} made ${fmt(current.test_return)} on test; a probe starts after the next research decision.` };
  }
  const stale = (p.profitable || 0) - (p.profitable_current_bar ?? p.profitable ?? 0);
  if (stale > 0) return { tone: "warn", text: `No experiment on the current bar size made money on its sealed test. ${stale} profitable one${stale === 1 ? " was" : "s were"} tested on another bar size and cannot trade.` +
    (top ? ` Most common failure: ${gateLabel(top.gate)} (${top.n}×).` : "") };
  return { tone: "warn", text: `None of the ${p.finished} experiment${p.finished === 1 ? "" : "s"} made money on its sealed test (after costs), so nothing qualifies.` +
    (top ? ` Most common failure: ${gateLabel(top.gate)} (${top.n}×).` : "") };
}

// ---------------------------------------------------------------- research universe
// "aapl, msft\nspy" -> ["AAPL", "MSFT", "SPY"]: upper-cased, split on commas or whitespace, duplicates dropped.
export function parseSymbols(text) {
  return [...new Set(String(text ?? "").toUpperCase().split(/[\s,;]+/).filter(Boolean))];
}
export function symbolDiff(before, after) {
  const b = new Set(before), a = new Set(after);
  return { added: after.filter((s) => !b.has(s)), removed: before.filter((s) => !a.has(s)) };
}

// ---------------------------------------------------------------- operator limits
// Stored values are fractions; the form shows percentages. `loosens` says which direction relaxes
// a control: +1 when raising it loosens, -1 when lowering it loosens, 0 when neither is riskier.
export const LIMIT_FIELDS = [
  { g: "risk", k: "max_position_pct", label: "Max position", unit: "pct", hint: "of equity, per symbol", loosens: 1 },
  { g: "risk", k: "max_gross_pct", label: "Max gross exposure", unit: "pct", hint: "of equity", loosens: 1 },
  { g: "risk", k: "max_crypto_pct", label: "Max crypto exposure", unit: "pct", hint: "of equity", loosens: 1 },
  { g: "risk", k: "max_strategy_pct", label: "Max per strategy", unit: "pct", hint: "of equity", loosens: 1 },
  { g: "risk", k: "max_total_allocation_pct", label: "Max total allocated", unit: "pct", hint: "of equity, all strategies", loosens: 1 },
  { g: "risk", k: "max_order_usd", label: "Max order size", unit: "usd", loosens: 1 },
  { g: "risk", k: "daily_loss_limit_pct", label: "Daily loss limit", unit: "pct", hint: "of equity", loosens: 1 },
  { g: "risk", k: "max_drawdown_pct", label: "Max drawdown", unit: "pct", hint: "halts new risk when reached", loosens: 1 },
  { g: "risk", k: "max_orders_per_hour", label: "Max orders per hour", unit: "int", loosens: 1 },
  { g: "risk", k: "max_quote_age_sec", label: "Max quote age", unit: "sec", loosens: 1 },
  { g: "risk", k: "max_participation", label: "Max share of daily volume", unit: "pct", hint: "per order, of average daily volume", loosens: 1 },
  { g: "risk", k: "max_spread_bps", label: "Max usable spread", unit: "bp", loosens: 1 },
  { g: "execution", k: "fractional_shares", label: "Fractional stock shares", unit: "bool", loosens: 0 },
  { g: "probe", k: "enabled", label: "Probes enabled", unit: "bool", loosens: 1 },
  { g: "probe", k: "allocation_pct", label: "Probe size", unit: "pct", hint: "of equity", loosens: 1 },
  { g: "probe", k: "max_active", label: "Max probes at once", unit: "int", loosens: 1 },
  { g: "probe", k: "per_venue_per_day", label: "Max new probes per venue per day", unit: "int", loosens: 1 },
  { g: "probe", k: "min_test_return", label: "Min sealed-test return", unit: "pct", loosens: -1 },
  { g: "probe", k: "lifetime_days", label: "Probe lifetime", unit: "days", loosens: 1 },
  { g: "gates", k: "min_test_sharpe", label: "Min test Sharpe", unit: "num", loosens: -1 },
  { g: "gates", k: "min_val_sharpe", label: "Min validation Sharpe", unit: "num", loosens: -1 },
  { g: "gates", k: "min_test_trades", label: "Min test trades", unit: "int", loosens: -1 },
  { g: "gates", k: "min_deflated_psr", label: "Min deflated PSR", unit: "num", hint: "probabilistic Sharpe, 0–1", loosens: -1 },
  { g: "gates", k: "min_cost_stress_return", label: "Min return at 2× costs", unit: "pct", loosens: -1 },
  { g: "gates", k: "min_delay_stress_sharpe", label: "Min Sharpe with 1-bar delay", unit: "num", loosens: -1 },
  { g: "gates", k: "min_neighbor_positive_frac", label: "Min share of positive nearby params", unit: "pct", loosens: -1 },
  { g: "gates", k: "min_subperiod_positive_frac", label: "Min share of positive sub-periods", unit: "pct", loosens: -1 },
  { g: "gates", k: "max_top5_concentration", label: "Max top-5 trade concentration", unit: "pct", hint: "share of sealed-test profit", loosens: 1 },
  { g: "research", k: "deflation_window_days", label: "Deflated PSR counts trials from the last", unit: "days", loosens: -1 },
  { g: "budget", k: "monthly_usd", label: "AI budget per month", unit: "usd", loosens: 1 },
  { g: "budget", k: "daily_usd", label: "AI budget per day", unit: "usd", loosens: 1 },
  { g: "schedule", k: "crypto_research_hours", label: "Crypto research every", unit: "hours", loosens: 0 },
  { g: "schedule", k: "nasdaq_research_hours", label: "Stock research every", unit: "hours", loosens: 0 },
  { g: "schedule", k: "max_research_per_day", label: "Max experiments per venue per day", unit: "int", loosens: 0 },
  { g: "schedule", k: "review_hour_utc", label: "Daily review hour (UTC)", unit: "hour", loosens: 0 },
];
export const UNIT_SUFFIX = { pct: "%", usd: "$", bp: "bp", sec: "s", hours: "h", days: "d", hour: ":00 UTC" };
const INTEGER_UNITS = new Set(["int", "sec", "hours", "days", "hour"]);
const clean = (v) => Number(v.toPrecision(12));

export function toDisplay(value, unit) {
  if (value == null || !Number.isFinite(Number(value))) return "";
  return String(unit === "pct" ? clean(Number(value) * 100) : clean(Number(value)));
}
// Returns { value } or { error } with a message that names the problem.
export function fromDisplay(text, unit, bounds) {
  const raw = String(text ?? "").trim().replace(/[%$,\s]/g, "");
  if (!raw) return { error: "Enter a value." };
  const n = Number(raw);
  if (!Number.isFinite(n)) return { error: "Enter a number." };
  if (INTEGER_UNITS.has(unit) && !Number.isInteger(n)) return { error: "Enter a whole number." };
  const value = unit === "pct" ? clean(n / 100) : n;
  if (bounds) {
    const [lo, hi] = bounds;
    if (value < lo - 1e-12 || value > hi + 1e-12) return { error: `Allowed range is ${formatLimit(lo, unit)} to ${formatLimit(hi, unit)}.` };
  }
  return { value };
}
export function formatLimit(value, unit) {
  if (unit === "bool") return value ? "on" : "off";
  const d = toDisplay(value, unit);
  if (d === "") return "–";
  if (unit === "usd") return "$" + Number(d).toLocaleString("en-US");
  if (unit === "hour") return `${d.padStart(2, "0")}:00 UTC`;
  return d + (UNIT_SUFFIX[unit] ? (unit === "pct" ? "%" : " " + UNIT_SUFFIX[unit]) : "");
}
export const sameValue = (a, b) => a === b || (typeof a === "number" && typeof b === "number" && Math.abs(a - b) <= 1e-12 * Math.max(1, Math.abs(b)));
// "loosens" | "tightens" | "changes" for an edit from `before` to `after`.
export function changeDirection(field, before, after) {
  if (field.unit === "bool") return after ? (field.loosens > 0 ? "loosens" : "tightens") : (field.loosens > 0 ? "tightens" : "loosens");
  if (!field.loosens || sameValue(before, after)) return "changes";
  return (after > before) === (field.loosens > 0) ? "loosens" : "tightens";
}
