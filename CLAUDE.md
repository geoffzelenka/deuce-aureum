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
python main.py report
python main.py report --debug      # log full Claude conversation to logs/report_conversation_{date}.log
python main.py monitor             # watcher thread + Flask dashboard
python main.py monitor --interval 30
python main.py watch AAPL MSFT
python main.py web                 # Flask only (no watcher)
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

`report/generator.py` uses an agentic tool-use loop. Claude may call two tools mid-analysis
before producing the final JSON:

- `get_quote` — fetches a live single-ticker quote from E*TRADE
- `get_technicals` — computes SMA-20/50/200 and 30-day avg volume from Yahoo Finance
  (stub implementation; RSI-14 returns `null` until a proper data source is integrated)

Guardrails enforced in `generate_report()`:
- **3-turn limit** — forces `end_turn` after 3 tool calls
- **Ticker allow-list** — only tickers from positions or extracted from headlines are permitted
- **Duplicate block** — same (tool, ticker) pair cannot be called twice per session
- **5-second timeout** per E*TRADE call

Every tool call attempt is logged to `./logs/tool_calls.log` (timestamp, ticker, tool name,
turn number, allowed yes/no, elapsed ms, result summary).

`generate_morning_report(etrade_session=None, debug=False)` is the public entry point used by
`main.py`. Pass an active `OAuth1Session` to enable live `get_quote` calls; omit it to run on
headlines and positions alone. `debug=True` writes the full turn-by-turn Claude conversation to
`./logs/report_conversation_{date}.log` (updated after every API round-trip).

## Headlines file format

`parse_headlines_file()` accepts free-form text files. It:
- Skips blank lines and `#` comment lines
- Skips pure Markdown section headers (e.g. `**Company News**`, `**Analyst Actions**`)
- Strips leading `- ` or `* ` bullet markers from each line

This means standard newsletter-style formats (bullet lists with bold section headers) parse
cleanly without feeding formatting noise to Claude.

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
