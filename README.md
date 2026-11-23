# QuantDesk: an autonomous AI research and paper-trading desk
https://quantdesk-trade.up.railway.app/
An AI analyst that researches NASDAQ equities and liquid crypto on its own. It forms testable
hypotheses, backtests them with realistic costs on a sealed chronological test window, challenges
the results, and allocates **virtual** money only when deterministic gates pass. It then trades
through an independent risk engine and reviews live outcomes against what it expected. A live
journal explains every step, and you can ask it questions that it answers from its stored records.

**Paper trading only.** No code path sends orders to a broker. Starting capital is $100,000 of
virtual money.

![workspace](docs/workspace-light.png)

---

## What you see

| Panel | Shows |
|---|---|
| Top bar | PAPER badge, analyst mode (AI model and provider, or **DEMO rules engine**), data mode, worker health, NASDAQ session and countdown, API spend vs. budget, Pause / Emergency stop |
| KPIs | Equity, P&L **after costs**, exposure and drawdown vs. their limits |
| Trading pipeline | One sentence on why the desk is or is not trading (naming a closed market when every live strategy is on it), research over the last 7 days (tested → profitable on test → passed gates) next to trading now (funded strategies, probes, open positions), the ideas closest to trading with a **Start manual probe** button, and which gates block ideas most often |
| Positions | Click a row to see why it entered (written *before* the fill), the exact exit rules, and the exit plan; probe sleeves are labelled |
| Equity chart | Account vs. QQQ and BTC buy-and-hold of the same $100k, plus excess return, the same comparison against QQQ held at the account's own average exposure (the rest in cash), and fees |
| Agent activity | Current activity, open orders and scheduled next actions |
| Beliefs | Current views, each linked to the experiments or trades that support it (beliefs without verifiable evidence are discarded) |
| Research | Every experiment: hypothesis, frozen rules, train/validation/test split, metrics, stress tests, gate checklist, leakage notes, decision, lessons; test-window reuse in the deflation lookback and over the whole history; re-judge, start a probe or compare a revision with the experiment it revises (rules, test dates, costs, gates that flipped) from the drawer |
| Strategies | Live strategies and probes: allocation, live return vs. expectation, slippage, reviews, and a bar-by-bar replay of paper trading against the frozen rules' backtest |
| Orders & fills | Intents (with rationale and risk-engine checks) kept separate from simulated fills (with the quote they hit) |
| Market | Candles with your fills marked, plus the live quote |
| Limits & system | Operator-only limits grouped as risk, probes, gates and AI budget/schedule, controls, ledger integrity, sandbox isolation, LLM call audit (prompt and response), jobs, audit log |
| Journal + Ask | Live feed (server-sent events), filterable, searchable across the full history (words, names or `#ID`); repeated waits for one bar collapse into one row; questions are answered only from records, with citations |

## How the agent works

```
Observe ─► Investigate ─► Test ─► Challenge ─► Decide ─► Trade ─► Review
 stats     1 hypothesis   train→   cost×2,       gates +    risk     live vs.
 & changes + spec (DSL)   val→     +1 bar delay,  analyst    engine   expectation,
 (blinded)               FREEZE→  neighbours,    (reject/   → paper  shadow backtest,
                         test     sub-periods,    revise/    exchange continue/reduce/
                         (once)   concentration,  allocate)           retire
                                  deflated Sharpe
```

* **Observe** (`research/observe.py`): returns, volatility shifts, regime flips, new highs and
  outsized moves, all computed from stored bars. The context the LLM uses to *form* a hypothesis is
  **blinded**: it ends before the earliest possible test window, so nothing it has seen overlaps the
  test.
* **Investigate**: the analyst proposes one hypothesis (rationale, quantitative prediction,
  falsification criterion) plus a strategy spec in a small, causal expression language
  (`research/dsl.py`), for example `close > sma(close, slow) and market(close) > market(sma(close, 200))`.
  Saying "nothing worth testing" is a recorded decision. When the language can't express an idea,
  the model may write Python. That code runs in a **sandbox**, described below.
  Before an experiment is created, proposals must support the mandatory neighbor-robustness check:
  at least one meaningful rule parameter is required (a single candidate at its original value is
  enough). Empty or unused DSL parameters and grids with untestable candidates get one AI repair
  attempt, then are discarded without opening the test window. A repair may also decline the idea.
* **Test** (`research/experiment.py`): the parameter grid (≤ 12 combinations) is searched on
  **train only**, then confirmed on validation. The spec and chosen parameters are then **frozen and
  persisted**, and only after that is the sealed **test** window evaluated, exactly once. Fills
  happen at the first bar open paper trading can reach (the next bar for crypto; one bar later for
  stocks, whose bars arrive ~16 min after they close) with half-spread, square-root market impact,
  venue fees and a cap on participation in average daily volume, on entries and exits alike.
* **Challenge**: doubled costs, one more bar of execution delay, ±25 % parameter perturbations, four
  pre-test sub-periods, top-5-trade concentration, and a **Deflated Sharpe** (PSR against the
  expected maximum Sharpe of N zero-skill trials, where N counts earlier experiments from the last
  `research.deflation_window_days` (default 7) whose test windows overlap this one; counting every
  experiment ever run would raise the bar without limit, to a Sharpe of 3-4 after ~100 trials).
* **Decide**: the gates in `research/gates.py` (operator-set thresholds) decide admission. When
  every gate passes, the idea is always allocated paper capital; the analyst only sets the size
  (at least 5 % of equity), and a dissenting second model halves it instead of blocking it.
  **Default exploratory gates admit paper experiments; they do not establish a statistically
  validated edge.** Select strict gates in Limits & system for more selective screening. Allocations
  are capped by operator limits. New strategies start at an **incubation** size (50 % of approval).
* **Probe**: an idea that missed a gate but made money on its sealed test (net of costs, enough
  trades) starts a small, clearly-labelled **probe**: 3 % of equity, at most 3 at once and one per
  venue per day, retired automatically after 14 days. The operator can also start a probe from any
  finished experiment (Pipeline card or the experiment drawer). Probes exist to collect a forward
  paper record; they are not evidence of an edge. All probe settings are under Limits & system.
* **Trade** (`trading/live.py`): on every newly closed bar, the frozen rules run through the
  **same** `compute_signals` and `step()` code as the backtester. Each order becomes an *intent*
  with a rationale built from actual rule values, then passes the independent risk engine, then
  fills in the paper exchange.
* **Review** (`agent/review.py`, daily): live P&L vs. the expected band from the test, a shadow
  backtest over the live period (implementation shortfall) and the recorded invalidation
  conditions. A breached drawdown limit always retires the strategy. Increases are allowed only
  after the minimum live period and only up to the approved amount. **Replay live vs. backtest**
  in a strategy's drawer lines the two up per signal bar and opens on the first bar where they
  disagree: the rule values paper trading saw, the same rule on today's stored data, when the bar
  was usable, the expected fill, and the actual fill or the reason there was none.

"Learning" means the research memory. Each prompt includes prior experiments, their outcomes,
which gates failed and the recorded lessons, and revisions link to their parent experiment. The
Research tab is that record.

### US stocks vs. crypto

The stock venue is called NASDAQ in the code and UI, but it covers any Alpaca-listed US stock or ETF
(NYSE listings too); all of them trade in the same regular session. The default universe is liquid large
caps and broad unleveraged ETFs, plus crypto majors; saved operator settings are kept until edited.

| | Stocks and ETFs (Alpaca data) | Crypto majors (Coinbase public data) |
|---|---|---|
| Research bars | 15-minute, regular session only, split- and dividend-adjusted, 4 years | 15-minute, 24/7, 3 years |
| Research schedule | Every 2 h while new bars arrive | Every 2 h |
| Decisions | Each 15-minute bar, ~16 min after it ends (free plan's SIP delay) | Each 15-minute bar |
| Execution | Same session; orders decided outside it wait for the next open (+5 min), XNAS calendar with holidays and early closes | 24/7 |
| Costs | $0 commission (configurable), SEC fee + FINRA TAF on sells, quoted spread, impact | 25 bp taker fee, quoted spread, impact |
| Benchmark | QQQ | BTC-USD |

## Safeguards

* **Deterministic accounting** (`trading/ledger.py`): cash comes from an append-only ledger and
  positions are tracked per strategy. `integrity_check()` rebuilds both from fills at every start;
  on a mismatch, new risk is halted.
* **Independent risk engine** (`trading/risk.py`), a pure function: emergency stop, pause,
  drawdown halt, daily loss limit, session open, quote freshness, bar freshness, order rate, cash
  (no leverage), no shorting, per-symbol, gross, crypto, per-strategy and per-order caps. Buys are
  resized to fit or rejected. Queued buys reserve cash and gross, symbol, crypto and strategy
  exposure. Buy exposure and loss limits are checked again at the fee-inclusive fill price,
  excluding only that order's own reservation. A resized fill cancels its remainder.
  Sells stay allowed under loss limits. Kill switches are re-checked at fill time.
* **Durable exits**: retiring/flattening cancels queued entries before requesting sells. Exit
  requests survive restarts and retry expired or liquidity-capped remainders, including for
  retired strategies. Flatten-all pauses new entries and covers retired holdings as well.
  Removing a symbol from the research universe does not stop data updates needed by existing
  strategies, positions, orders or benchmarks.
* **The agent cannot loosen limits.** Settings are written only by `settings.operator_update`,
  called only from the authenticated API, and clamped to hard ceilings in `config.py` (e.g. gross
  exposure ≤ 100 %). A test fails the build if agent, research, trading or market-data code
  references the settings writer.
* **No duplicate orders**: `client_order_id = hash(strategy, symbol, signal bar, side)` with a
  UNIQUE constraint, so replaying a cycle after a crash is a no-op.
* **Restart and outage handling**: a single-worker DB lock with a heartbeat thread, downtime
  journaled, interrupted intents cancelled, half-run experiments marked interrupted (never resumed
  on mixed data), missed bars **not** replayed, order TTLs, and per-venue backoff when a data
  provider is down.
* **Stock splits**: every bar refresh re-fetches recently stored bars. If the provider has re-adjusted
  history, the stored history is replaced. When the change matches a standard split ratio, every
  holding's share count, cost basis and stop reference are adjusted atomically (value unchanged),
  open orders in that symbol are cancelled for re-sizing, and the integrity rebuild replays the split.
  Dividend-sized changes only refresh history; unexplained jumps are flagged and leave positions alone.
* **Cash dividends**: every 6 hours the worker reads Alpaca's corporate actions for stocks any
  strategy held recently. On the ex-date, each strategy that held the stock before that session's
  open is credited rate × shares (cash and realised P&L), once per symbol and ex-date. This matches
  the dividend-adjusted bars the backtests use, and the integrity rebuild replays the credits.
* **Stale data stops dependent actions**: missing or old bars mean no signal evaluation for that
  strategy, and stale quotes mean no fill. Both are journaled once per transition.
* **Sandbox for model-written code**: AST allowlist, a separate process with a cleared environment,
  and CPU, memory, file-size and process limits. Local execution requires both a private Linux
  network namespace and a drop to `nobody`. Its **sampled causality check** reruns at three truncated
  histories and rejects code whose past signals change there. This is a heuristic, not proof that arbitrary Python is
  causal. Strict gates require the restricted causal DSL instead. In Docker it runs in its own
  container with `network_mode: none`, a read-only filesystem, no secrets and dropped capabilities.
* **Remote access**: a password login sets an expiring, HttpOnly SameSite=Strict cookie. Logout
  revokes the server-side session. Writes require an `X-QD` header and a matching browser origin
  when supplied. Login is rate-limited, and the API requires a password to listen beyond loopback.
  Users with cookies issued by an earlier version must sign in again after upgrading.
* **Read-only view**: set `QD_VIEWER_PASSWORD` (12+ characters, different from the operator
  password) to give people you show results to their own login. They see the desk, journal and
  records, but no controls, limits, audit log or model prompts; the server refuses every write.
* **Public view**: set `QD_PUBLIC_VIEW=1` to show that same read-only view to anyone with the URL,
  no password needed. Your strategies, positions, journal and research become public. The operator
  uses **Sign in** at the top for controls. Requires `QD_ADMIN_PASSWORD`.
* **Keyboard**: press `?` in the dashboard for shortcuts. Emergency stop deliberately has none.
  Records tabs have their own addresses, e.g. `/#limits` opens Limits & system.
* **Grounded language**: every journal entry stores the record IDs it refers to. LLM-written
  entries link to the exact prompt and response (`llm_calls`). Q&A drops citations to records that
  don't exist. Fills, intents, data and risk entries are written by deterministic code, never by
  the model.

### Gate presets

Limits & system labels the current gates **exploratory**, **strict**, or **custom**. Preset buttons
replace gate settings through the authenticated operator API and create an audit record. Existing
settings are not silently migrated; the one exception is a saved preset that predates a newer gate,
which gains that preset's value for it so it keeps its label. Presets affect future research decisions, not existing allocations.

**Re-judging is one-way.** Re-judge re-applies today's gates to an experiment's stored results without
a new backtest. It is refused when any threshold is looser than the one the experiment was first judged
under, because loosening a gate after seeing a result picks the bar to fit the score. The first
judgement is kept in the record and shown in the experiment drawer. Experiments re-judged before the
first judgement was kept have no baseline, so they cannot be re-judged again. To get forward evidence
for a near miss, start a manual probe instead.

**History length sets the gates' power.** The sealed test is the last ~25 % of stored history, and the
deflated PSR needs roughly *test Sharpe × √(test years)* to beat the best of N zero-skill trials. So
the test window's calendar length matters, not its bar count. The desk keeps 3 years of crypto and 4
years of stock history (about 9-month and 1-year tests). Older installs extend backwards one symbol
per tick, and research waits until that finishes. Running fewer experiments per day also keeps N, and
with it the bar, lower.

| Gate | Exploratory (default) | Strict |
| --- | --- | --- |
| Completed test trades | ≥ 8 | ≥ 30 |
| Test / validation Sharpe | ≥ 0 / ≥ −0.5 | ≥ 0.5 / ≥ 0.25 |
| Test return at doubled costs | > −5% | > 0% |
| Test Sharpe with one more bar of delay | > −0.5 | > 0 |
| Positive neighbors / sub-periods | ≥ 34% / ≥ 34% | ≥ 67% / ≥ 75% |
| Top-5-trade concentration | ≤ 100% | ≤ 50% |
| Deflated PSR score | ≥ 0.25 | ≥ 0.95 |
| Strategy language | DSL or sampled-check Python | DSL only |
| Beats its universe (survivorship check) | reported in the journal | test return > average exposure × universe buy-and-hold |

Even strict screening is **not a profitability guarantee**. PSR/deflation uses simplifying sampling
assumptions; autocorrelation, adaptive holdout reuse, survivorship and LLM hindsight remain concerns.
A sustained, timestamped forward-paper record is still required.

### Limitations and leakage you should know about

* **LLM hindsight.** The model may remember how markets moved before its knowledge cutoff
  (`QD_LLM_KNOWLEDGE_CUTOFF`). Every experiment reports the fraction of its test window that
  precedes the cutoff. Treat those results as optimistic. Only post-cutoff tests and live paper
  results are clean.
* **Holdout reuse.** Later experiments are informed by earlier test outcomes. Each experiment
  counts the overlapping test windows and deflates its significance accordingly; this mitigates
  the problem but doesn't remove it. Trials older than `research.deflation_window_days` stop
  counting, so a shorter window lowers the bar without making the test cleaner.
* **Survivorship.** The default NASDAQ universe is today's surviving liquid large caps and broad ETFs, so backtests overstate returns.
  Every experiment compares its sealed-test return with an equal-weight buy-and-hold of the same
  universe at the same average exposure, which inflates by about as much; strict gates require the
  strategy to beat it, and exploratory runs journal a warning when it doesn't. This removes most of
  the drift but not survivor-driven signals (e.g. momentum among names that later failed is never
  seen). A real fix needs point-in-time membership and delisted-stock history.
* **Free-plan data.** Alpaca's free real-time quotes come from IEX only (a few percent of volume),
  so spreads can be wider or one-sided. The simulator then uses a fresh last trade plus a modeled
  spread and labels the fill accordingly. Historical spreads are modeled, not observed. Coinbase
  volume (one venue) is used for liquidity, which is conservative.
* **Simplifications.** Research and signal evaluation use 15-minute bars; stops are checked on bar closes; dividends are
  credited on the ex-date rather than the payable date (matching adjusted prices), and only
  for stocks held within the dividend check's look-back; queue position and intraday auctions are not modeled. Splits are
  *inferred* from re-adjusted price history rather than read from a corporate-actions feed, and only
  standard ratios are applied automatically (anything else is flagged for you).
* **Cost parameters are assumptions.** Verify the SEC Section 31 rate (it changes) and your fee tier.
* **Backtest/paper execution differences.** Stock backtests fill at the open two bars after the
  signal bar, which is about two minutes before paper can act (the bar arrives 16 minutes after its
  close). Signals from a session's last bar fill at the second bar of the next session in the
  backtest, but at the open + 5 min in paper. Both sides cap each order, entries and exits, at the
  operator's `max_participation` × ADV. Paper cancels a capped exit's remainder and the next
  decision cycle re-issues it, while the backtest carries it to the next bar's open.
* **Strategy reviews.** `max_live_drawdown_pct` is a positive fraction (0.05 = 5 %). Analysts have
  written it as a percentage (`-3.0` for 3 %), which compared with a fractional drawdown never
  fired; any value above 1 is now read as a percentage, and the worker repairs stored limits at
  start-up with a journal entry per strategy. It compares current sleeve equity to its
  high-water mark sampled at account snapshots and reviews. For positive sleeve equity,
  allocation changes rescale the peak so adding or removing capital alone is not a drawdown.
  Existing desks start from current allocation and observed equity; earlier peaks are not
  reconstructed. The separate cumulative sleeve return still uses current allocation.
* **AI spending is an estimated cap, not a billing guarantee.** Pre-call checks use token
  estimates, configured prices and recorded completed calls. Concurrent worker/API calls do
  not reserve budget against each other; uncertain provider usage, retries and fallback
  models can make actual charges exceed the estimate. Set a provider-side spending limit too.
* **Liquidity estimates.** Backtests and paper execution share a causal 20-trading-day ADV/daily-volatility
  estimator, with partial windows allowed after 10 valid observations. Future or unfinished bars
  are excluded. Earlier backtests used a one-day estimator; rerun experiments rather than rewriting
  stored records when comparing results across this change.

## Setup

### 1. Credentials

* **AI provider** (optional). Without one, a clearly labeled **demo rules engine** runs the same
  pipeline from a small template library at $0. Any **OpenAI-compatible** chat API works: OpenAI,
  Google Gemini (free tier available), Groq, OpenRouter, Mistral, DeepSeek, or a free local model via
  Ollama. Set `QD_LLM_PROVIDER`, `QD_LLM_BASE_URL`, `QD_LLM_API_KEY` and `QD_LLM_MODEL`; presets are in
  `.env.example`. Also set `QD_LLM_PRICE_IN` / `QD_LLM_PRICE_OUT` (USD per 1M tokens) so the spending cap
  matches your model. If the provider can't enforce a JSON schema, the app validates each reply itself and
  asks once for a corrected one; it also adapts automatically to providers that reject strict-schema mode
  or the `max_tokens` parameter. Claude is still supported (`ANTHROPIC_API_KEY`).
* **Alpaca** (free account → API keys) for NASDAQ data: `ALPACA_API_KEY_ID`,
  `ALPACA_API_SECRET_KEY`. Only market data is used. Without keys, NASDAQ shows "not configured"
  and only crypto runs.
* **Coinbase** needs no key.

### 2. Local run

```bash
git clone https://github.com/zenriea/quant-desk && cd quant-desk
```
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```
```bash
cp .env.example .env   # then edit .env
```
```bash
python -m quantdesk.worker      # terminal 1: research, trading, risk (the only writer)
```
```bash
python -m quantdesk.api.app     # terminal 2: UI at http://127.0.0.1:8000
```

For a $0 demo with fake data that is labeled as fake everywhere, set `QD_MARKET_DATA=synthetic`.
Synthetic paths now preserve historical OHLCV values as more bars arrive. This changes their
seeded values from earlier versions; use a fresh demo data directory instead of mixing old
synthetic histories and positions with the new generator. Real market-data histories are unaffected.

### Windows: test the local changes without touching Railway

In the project folder, double-click **`Start Local Preview.cmd`**. Keep its terminal window open.
It opens **http://127.0.0.1:8127**, runs the API **and worker**, and prepares a small synthetic
universe with six months of history. The first startup takes longer while that history is generated.

* The preview **does not load `.env`**, use provider keys, call a paid AI model, or access Railway.
* Its separate database is **`data/local-preview-v1/quantdesk.db`**, never `data/quantdesk.db`.
* Preview settings and history persist between launches; nothing is reset automatically.
* Try **Research**, **Market**, **Limits & system**, the gate presets, Pause/Resume and Emergency stop.
  No trades is a valid outcome: the demo still enforces the ordinary gates and risk checks.
* Press **Ctrl+C** in the preview terminal to stop the API and its worker. Do not close your
  production service or change Railway variables for this test.

Double-click **`Run Tests.cmd`** to run the automated regression suite in temporary test databases.
It leaves the result visible in the terminal. Linux-only sandbox tests are skipped on Windows.
The suite also runs the dashboard's JavaScript display-model tests when Node.js is installed;
that check is explicitly skipped when Node.js is unavailable.
Both launchers use the project's existing `.venv`; development dependencies are installed with
`.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt` if needed.

The equivalent command for other platforms is `python -m quantdesk.preview`. Use `--no-browser`
to keep the browser closed, or `--port 8128` if the default port is occupied.

The refreshed dashboard has a workspace navigation rail, account/risk summary cards, an equity
chart with **1D / 1W / 1M / All** views, and a live analyst journal. Chart ranges filter recorded
snapshots, not backtest history; a new preview can legitimately have only a short, flat account
history. Fees and benchmark-relative results are shown below the chart and remain since-inception
metrics regardless of the selected chart range. Dark/light theme preferences are saved locally.

### 3. Always-on server (Docker)

On any Linux VPS with Docker (2 vCPU / 2–4 GB is enough):

```bash
git clone https://github.com/zenriea/quant-desk && cd quant-desk && cp .env.example .env
```
Edit `.env`: set `QD_ADMIN_PASSWORD` (at least 12 characters), the Alpaca
keys, and one AI provider block (or leave them all out to run the $0 demo analyst). Then:
```bash
docker compose up -d --build
```

This starts `api` (bound to `127.0.0.1:8000`), `worker` and `sandbox`, all with
`restart: unless-stopped`, so they come back after crashes and reboots. State lives in the
`qd-data` volume.

**Remote access**, pick one:
* SSH tunnel (simplest, nothing exposed): `ssh -L 8000:127.0.0.1:8000 you@server`, then open
  http://127.0.0.1:8000
* HTTPS: point a DNS record at the server, set `QD_DOMAIN=desk.yourdomain.com` and
  `QD_COOKIE_SECURE=1` in `.env`, then run:
  ```bash
  docker compose --profile tls up -d
  ```
  Caddy obtains the certificate automatically. Compose trusts forwarding headers from Caddy's
  address on its dedicated network. If the subnet overlaps an existing Docker network, set
  `QD_PROXY_SUBNET` and `QD_CADDY_IP` together to an unused subnet and an address within it.

**Operate**:
```bash
docker compose logs -f worker
```
```bash
docker compose exec worker python -m quantdesk.backup /data/backup.db
```
```bash
git pull && docker compose up -d --build
```

Without Docker, the systemd units in `deploy/` run the desk as an unprivileged user and research
strategies in the DSL. Use the dedicated Compose sandbox for model-written Python.

### 3b. Railway (no server to manage)

The repo includes `railway.json`, which builds the Dockerfile and starts `python -m quantdesk.run_all`:
web UI and worker in **one** container, with the worker restarted automatically if it crashes. It uses
about 200 MB of memory, so the 1 GB trial or Hobby plan is plenty. The single-container image
defaults to DSL strategies because it lacks a dedicated sandbox. Existing Python strategies need a
dedicated sandbox to continue evaluating signals.

1. **New Project → Deploy from GitHub repo**, authorize GitHub, pick `zenriea/quant-desk` (`main`).
2. Open the service → **Variables** and add:
   ```
   QD_ADMIN_PASSWORD=<your own password for the dashboard, at least 12 characters>
   QD_SESSION_SECRET=<random string; e.g. from python3 -c "import secrets;print(secrets.token_hex(32))">
   QD_COOKIE_SECURE=1
   RAILWAY_RUN_UID=0
   ALPACA_API_KEY_ID=<your Alpaca key ID>
   ALPACA_API_SECRET_KEY=<your Alpaca secret>
   QD_LLM_PROVIDER=gemini
   QD_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
   QD_LLM_API_KEY=<your Gemini key>
   QD_LLM_MODEL=gemini-3.5-flash
   QD_LLM_PRICE_IN=0.5
   QD_LLM_PRICE_OUT=3
   ```
   `RAILWAY_RUN_UID=0` lets the app write to the volume.
3. **Add a volume** to the service (right-click the service, or the command palette → *Add Volume*)
   with mount path **`/data`**. Without it, all history is lost on every redeploy.
4. Service → **Settings → Networking → Generate Domain**. Set `QD_PUBLIC_ORIGIN` to the full HTTPS
   origin of that domain, then open it and sign in. Replace any old `QD_TRUSTED_PROXIES=*` setting.
   Leave the allowlist empty unless you have verified source IPs or CIDRs for the ingress. With an
   empty allowlist, login attempts from clients sharing an ingress address share its rate limit.
5. Check **Deployments → Logs** for `worker ... started`. Redeploys happen automatically when `main`
   changes; state survives because it lives on the volume.

### 4. Tests

```bash
pip install -r requirements-dev.txt && python -m pytest -q
```

Runtime and development installs use the exact versions in `requirements-lock.txt`.
The GitHub Actions workflow runs the suite on Linux with Python 3.12, 3.13 and 3.14,
including the Linux-only sandbox tests, dependency checks and source syntax checks.
The local Python sandbox requires Linux; use Docker or Linux/WSL for model-written code.
The strict preset does not admit Python strategies even if their sampled checks pass.
The historical verification notes below describe the original build, not a CI run of this revision.

## Operating costs (estimates)

| Item | Cost |
|---|---|
| Server | ~$5–25/month (any small VPS) |
| Market data | $0 (Coinbase public; Alpaca free plan). Alpaca's paid plan gives full SIP quotes |
| AI analyst | About **2 model calls per research cycle** (hypothesis and decision; ~15–20k input and ~5–10k output tokens), plus daily reviews and your questions. Cost depends entirely on the provider, model and schedule; $0 for a local Ollama model or a free tier within its limits. Both venues default to a 2-hour research interval, with up to 12 experiments per venue per day when new data is available. Pre-call **estimated budget checks** use `$40/month` and `$4/day` defaults (changeable in Limits), using the prices you configure. See the spending limitations above; enforce a provider-side cap as well. |
| Cheaper | A smaller model; research less often (Limits: e.g. every 8 h instead of the default 2 h); or demo mode ($0) |

These AI cost figures are estimates from prompt sizes. No live AI call was made while building
this (see below). The Limits tab shows the real per-call tokens and cost once it
runs.

## What was verified while building this, and what wasn't

**Verified**

These notes come from the original build, when research ran on hourly bars (and daily bars for
NASDAQ). Research and live decisions now use 15-minute bars; see [US stocks vs. crypto](#us-stocks-vs-crypto).

* **Automated tests: 353 pass** on the current revision (103 at the original build), plus 4 Playwright
  browser tests that skip locally without Playwright and run in their own CI job. They cover:
  * ledger math and integrity checks, including split replay
  * split, dividend and unexplained re-adjustment of price history
  * every risk rule
  * execution timing: latency, fresh-quote requirement, NASDAQ off-hours queuing, TTL expiry, liquidity cap, wide-quote fallback, emergency cancel
  * duplicate-order prevention, restart recovery, worker-lock takeover
  * no replay of missed bars; stale data stops evaluation
  * DSL causality for every function (prefix test); next-bar execution
  * freezing before the test window; holdout-reuse deflation
  * sandbox policy, look-ahead detection, and no secrets or network inside it
  * AI provider handling: budget pre-check, refusal and truncation, cost accounting, OpenAI-compatible fallbacks and JSON repair
  * gate enforcement even when the model asks to allocate
  * API authentication, CSRF, rate limiting, settings ceilings
  * per-venue research scheduling, retries and caps; unexpected research/review errors are journaled
  * crypto history extension; strategies waiting for allocation room; backtest speed (linear in history)
* **Live crypto data**: backfilled 8,754 hourly Coinbase bars per pair and polled live quotes. A
  full research cycle ran on real data (demo analyst). It rejected a crypto trend idea whose test
  window looked great (+12.6 %, Sharpe 2.1) but which lost money in train and validation and had
  0 % positive neighbors.
* **Real-quote execution** (on a scratch copy of the DB with a harness strategy): the buy filled at
  the real Coinbase ask + 3.0 bp impact + 25 bp fee, after the latency wait. The sell filled at the
  real bid. The ledger rebuilt from fills matched.
* **Alpaca NASDAQ data**: backfilled 1,506 split/dividend-adjusted daily SIP bars (6 years) for all 15
  symbols and fetched live IEX snapshots. After hours IEX often shows one-sided quotes (ask missing),
  which is the case the simulator's last-trade fallback handles. A full NASDAQ research cycle ran on
  real data: the long-term trend filter returned +2.0 % net (Sharpe 0.18) on the sealed test window
  vs. +20.1 % for QQQ buy-and-hold, and was rejected.
* **Real AI analyst (Google Gemini, `gemini-3.5-flash`)**: a full NASDAQ research cycle ran with live
  Gemini calls. Gemini proposed a momentum-rotation hypothesis with a quantitative prediction, the sealed
  test failed (−10.5 % vs. QQQ +20.1 %, 302 trades), and Gemini diagnosed the churn, recorded lessons and
  proposed a specific revision. A question was answered from records with a correct citation. Both calls
  returned valid JSON in strict-schema mode (two calls, ~12k tokens). Some Gemini models were overloaded
  (503) or need billing; transient errors are retried with backoff, and a failed cycle pauses research
  for 10 minutes.
* **Upgrade of an existing install** (Nov 15, 2026, real Coinbase/Alpaca data, live Gemini calls): the
  1-year crypto history was extended to 3 years (26,269 hourly bars per pair, one pair per minute) before
  crypto research resumed; NASDAQ and crypto then ran on their own clocks. The crypto sealed test window
  grew from 2,141 to 6,478 bars. A 12-combination crypto experiment on 3 years runs in ~3 s (it took ~40 s
  before the backtester fix), with byte-identical results.
* **Restarts**: graceful stop and restart; in Docker, SIGKILL of the worker. The container
  restarted automatically, recovery ran and integrity held.
* **Docker Compose** stack built and ran (api, worker, no-network sandbox). The model-code spool
  round trip across containers worked, and the network outage was journaled with backoff while the
  heartbeat stayed healthy. (Building here needed a CA-trust tweak specific to this build
  environment's proxy; it is *not* in the repo and isn't needed on a normal server.)
* **UI** in Chromium: light, dark, 390 px phone width (no horizontal scroll), no console errors.

**Not verified**, so check these on first deployment:
* **Claude.** No Anthropic key was available, so the Claude path is tested only against a fake client
  mimicking the SDK's responses.
* **NASDAQ execution.** No live NASDAQ session was observed (it was a weekend, and there were no
  keys). Session logic is covered by calendar-based tests.
* **Promotion to live trading with real research results.** On the live desk (Nov 15–23, 2026),
  none of 80 experiments passed the gates in force when it ran. Two (#42 and #47) were allocated
  after the gates were loosened and they were re-judged, which this version now refuses. Neither
  had filled an order by Nov 23. A gate-passing strategy's first live trade has still not been seen.
  Most ideas lost money on the sealed test: 36 of 44 crypto tests had negative Sharpe, with a median
  cost drag of 13.6 %, and a live stock probe saw ~16 bp spreads against 2 bp modelled.
* Multi-day unattended operation, the Caddy TLS profile and the systemd units were not run.
* Railway itself was not used; the single-container mode was tested locally in Docker as Railway runs it
  (root user, volume at `/data`, `PORT`, `run_all`): health, sandbox, worker crash-restart and graceful stop.

## License

MIT; see [LICENSE](LICENSE). This is research and paper-trading software, not financial advice.
