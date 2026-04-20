# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`deuce-aureum` is a Python project (based on `.gitignore` configuration). 
etrade-tool is  a wrapper to the official Etrade API

## Setup

```bash
source .venv/bin/activate
pip install -r trading-assistant/requirements.txt
```

Always activate the venv before running `python`, `pip`, or `pytest`.

## Running tests

```bash
source .venv/bin/activate
cd trading-assistant
python -m pytest tests/
```

## CLI usage (trading-assistant)

```bash
cd trading-assistant
python main.py login
python main.py kickoff --headlines headlines/2026-04-10.txt --positions positions.txt
python main.py kickoff --skip-auth --headlines headlines/2026-04-10.txt --positions positions.txt
python main.py kickoff --no-monitor --headlines headlines/2026-04-10.txt  # Flask only, no watcher
python main.py kickoff --debug --headlines headlines/2026-04-10.txt --positions positions.txt
python main.py midmorning                  # 10:30 AM assessment (requires kickoff first)
python main.py midmorning --debug          # log full Claude conversation to logs/midmorning_conversation_{date}.log
python main.py report
python main.py report --debug              # log full Claude conversation to logs/report_conversation_{date}.log
python main.py monitor                     # watcher thread + Flask dashboard
python main.py monitor --interval 30
python main.py monitor --session-summary   # print confirmed/provisional status, then exit
python main.py watch AAPL MSFT
python main.py web                         # Flask only (no watcher)
```

The database defaults to `./data/trading.db`. Override with `DB_PATH` in `.env`.

The `report` command requires `ANTHROPIC_API_KEY` in `.env` (see `.env.example`).

The `monitor` and `kickoff` commands start the watcher in a background thread and then start the
Flask web dashboard. `--no-monitor` skips the watcher thread but still starts Flask.

Alert thresholds are configurable in `.env`:
`VOLUME_SPIKE_FACTOR` (default 1.5), `PROFIT_TARGET_PCT` (default 2.0), `STOP_LOSS_PCT` (default 1.0).
Alerts are logged to `./logs/alerts.log`.

## Web dashboard

Dashboard runs at `http://localhost:5000` by default. Override port with `WEB_PORT` in `.env`.

- `GET /` — three-panel dark-mode dashboard (session status, live prices, alerts feed)
- `GET /report` — today's HTML report rendered inline
- `GET /api/quotes` — latest quote snapshot from the watcher (`{}` if watcher not running)
- `GET /api/alerts` — last 50 lines of `./logs/alerts.log`
- `GET /api/session` — `{logged_in, remaining_seconds}`
- `POST /api/login` — two-step OAuth1 login:
  - empty body → `{"auth_url": "..."}` (step 1: get authorization URL)
  - `{"verifier": "..."}` → `{"success": true}` (step 2: complete login)
- `GET /api/muted` — list of currently muted tickers
- `POST /api/mute` — `{"ticker": "AAPL"}` → silence alerts for that ticker (in-memory, clears on restart)
- `POST /api/unmute` — `{"ticker": "AAPL"}` → re-enable alerts

The watcher writes quote snapshots to `monitor.watcher.quote_cache` (protected by `_cache_lock`).
Flask reads from this dict on each `/api/quotes` request.

## Report generation (agentic loop)

`report/generator.py` uses a two-phase agentic tool-use loop. All three tool
implementations live in `report/enricher.py`.

**Tools available to Claude:**
- `get_quote` — live single-ticker quote from E*TRADE
- `get_technicals` — SMA-20/50/200 and 30-day avg volume from Yahoo Finance
  (RSI-14 returns `null` until a proper data source is integrated)
- `get_options_flow` — E*TRADE options chain summary: call/put volume, put/call
  ratio, largest single trade, `unusual_activity` flag (True if ratio outside
  0.4–1.8 or any trade premium > $500k)

**`session_type` controls which tools are offered:**
- `"premarket"` (default) — `get_quote` + `get_technicals` only; `get_options_flow`
  is removed from the tools list entirely so Claude cannot call it
- `"midmorning"` — all three tools available

**Phase 1 — SCAN** (`tool_choice: none`): Claude reads all headlines and
positions and nominates up to 10 candidate tickers, emitting a JSON block:
```json
{"candidates": [{"ticker": "AAPL", "catalyst": "one-sentence reason", "pre_market_score": "high|medium|low"}]}
```
Candidates are filtered against the session allow-list, then saved to the
`watchlist` table via `save_watchlist()` before Phase 2 begins.

**Phase 2 — RESEARCH**: For each candidate, Claude must call `get_quote` then
`get_technicals` (mandatory, in that order), then up to 2 free-choice calls.
After Phase 2, the top-3 tickers' ranks are updated in the watchlist via
`update_watchlist_rank()`.

The shared `_run_research_phase()` helper in `generator.py` handles the loop
and is reused by `report/midmorning.py`.

Guardrails enforced in `generate_report()`:
- **Per-ticker budget** — 4 calls max; `get_quote` must precede `get_technicals`
- **Global cap** — 40 calls total; forces a `tools=[]` final completion when hit
- **Ticker allow-list** — only tickers from positions or extracted from headlines
- **5-second timeout** per E*TRADE call (errors returned as `{"error": "..."}`,
  never raised)

Every tool call attempt is logged to `./logs/tool_calls.log` (phase, ticker,
tool name, allowed yes/no, elapsed ms, budget status, `unusual_activity` flag
when applicable).

`generate_morning_report(etrade_session=None, debug=False, session_type="premarket")`
is the public entry point used by `main.py`. Pass an active `OAuth1Session` to
enable live E*TRADE calls; omit it to run on headlines and positions alone.
`debug=True` writes the full turn-by-turn Claude conversation to
`./logs/report_conversation_{date}.log` (updated after every API round-trip).

## Mid-morning assessment (report/midmorning.py)

`run_midmorning_assessment(etrade_session, debug=False)` is called by
`python main.py midmorning`. It:

1. Checks `watchlist_exists(today)` — exits with an error if kickoff hasn't run
2. Loads the watchlist, today's headlines, and positions from the DB
3. Builds a single user message with all context + research instructions
   (no Phase 1 scan — candidates come from the persisted watchlist)
4. Runs `_run_research_phase()` with all three tools and the same budget rules
5. Calls `update_watchlist_confirmation()` for each confirmed top-3 play,
   setting `confirmed=1`, `confirmed_rank`, and `options_unusual`

The mid-morning JSON schema adds `options_confirmation` and `conviction_change`
fields to each top play, and a `watchlist_dropped` array. Reports are saved to
`./reports/YYYY-MM-DD-midmorning.{json,html}`.

`_compute_conviction_change(pre_market_rank, confirmed_rank)` is a pure helper
that returns `"upgraded"`, `"unchanged"`, or `"downgraded"`.

## Watchlist DB (store/db.py)

The `watchlist` table persists the pre-market scan candidates and mid-morning
confirmation results for each trading day.

Key functions:
- `save_watchlist(candidates, session_date)` — upserts after Phase 1; each dict
  needs `ticker`, optionally `rank`, `catalyst`, `pre_market_score`
- `get_watchlist(session_date)` — returns rows ordered by rank
- `watchlist_exists(session_date)` — used by midmorning to gate on kickoff having run
- `update_watchlist_confirmation(ticker, session_date, confirmed_rank, options_unusual)`
- `update_watchlist_rank(ticker, session_date, rank)` — called after Phase 2 to
  finalize the provisional top-3 ranks

## Headlines file format

`parse_headlines_file()` accepts free-form text files. It:
- Skips blank lines and `#` comment lines
- Skips pure Markdown section headers (e.g. `**Company News**`, `**Analyst Actions**`)
- Strips leading `- ` or `* ` bullet markers from each line

This means standard newsletter-style formats (bullet lists with bold section headers) parse
cleanly without feeding formatting noise to Claude.

## Watcher — confirmed vs provisional top 3

On startup, `monitor_from_report()` calls `get_session_summary()` which checks:
1. **Confirmed top 3** from the watchlist DB (`confirmed=1`) — used if mid-morning ran
2. **Provisional top 3** from the watchlist DB (`rank` 1–3) — used if only kickoff ran
3. **Report JSON fallback** (`./reports/YYYY-MM-DD.json`) — if no watchlist exists

The startup log prints `"Monitoring confirmed top 3"` or
`"Monitoring provisional top 3 (mid-morning not yet run)."`.

`python main.py monitor --session-summary` prints the current mode and tickers
without starting the monitor loop.

## Watcher — tickers without a valid entry range

When Claude returns a vague `entry_range` (e.g. `"current price on pullback"`), the regex
parser raises `ValueError`. The ticker is **not skipped** — it is added to the watchlist with
`entry_low=0`. All signal checks (ENTRY, PROFIT_TARGET, STOP_LOSS) are gated on
`entry_low > 0`, so the ticker appears in the UI and receives live price updates but fires no
alerts. The startup summary prints `watching without signals (no entry range)` for these.

## E*TRADE API notes

- The quote endpoint (`/v1/market/quote/{symbols}`) returns **XML by default**. Always pass
  `headers={"Accept": "application/json"}` or the response will fail to parse.
- The OAuth1 session token is persisted to `./data/session.json` after login and reloaded
  automatically by `get_session()` / `is_logged_in()`. Sessions expire 115 minutes after the
  original login wall-clock time regardless of restarts.
- **Extended hours (pre-market / after-hours):** `All.lastTrade` equals `previousClose` and
  is stale until the regular session opens. Live pre-market/after-hours price is in the nested
  `All.ExtendedHourQuoteDetail` object — fields: `lastPrice`, `change`, `percentChange`,
  `bid`, `ask`, `volume`, `quoteStatus` (e.g. `"EH_REALTIME"`). `get_quote_data()` and
  `watcher._update_state()` both prefer `ExtendedHourQuoteDetail.lastPrice` over `lastTrade`.
  The returned dict includes `extended_hours: True`, `eh_status`, and `eh_change_pct` when
  the extended-hours block is present.
