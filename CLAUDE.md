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
python main.py report
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

The watcher writes quote snapshots to `monitor.watcher.quote_cache` (protected by `_cache_lock`).
Flask reads from this dict on each `/api/quotes` request.

## E*TRADE API notes

- The quote endpoint (`/v1/market/quote/{symbols}`) returns **XML by default**. Always pass
  `headers={"Accept": "application/json"}` or the response will fail to parse.
- The OAuth1 session token is persisted to `./data/session.json` after login and reloaded
  automatically by `get_session()` / `is_logged_in()`. Sessions expire 115 minutes after the
  original login wall-clock time regardless of restarts.
