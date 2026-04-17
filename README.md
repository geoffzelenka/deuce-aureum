# deuce-aureum

Stock market trading assistant — E*TRADE quote monitoring, Claude-powered morning reports, and a live web dashboard.

---

## Setup

```bash
# Create and activate the virtual environment
source .venv/bin/activate
pip install -r trading-assistant/requirements.txt

# Copy and fill in credentials
cp .env.example .env
```

Required `.env` values:

| Variable | Description |
|---|---|
| `ETRADE_CONSUMER_KEY` | From [developer.etrade.com](https://developer.etrade.com) |
| `ETRADE_CONSUMER_SECRET` | From [developer.etrade.com](https://developer.etrade.com) |
| `ETRADE_ENV` | `sandbox` or `production` |
| `ANTHROPIC_API_KEY` | From [console.anthropic.com](https://console.anthropic.com) |

Optional `.env` values:

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `./data/trading.db` | SQLite database path |
| `VOLUME_SPIKE_FACTOR` | `1.5` | Entry signal: volume must exceed N × 5-period average |
| `PROFIT_TARGET_PCT` | `2.0` | Exit signal: % gain above entry_low |
| `STOP_LOSS_PCT` | `1.0` | Exit signal: % loss below entry_low |
| `WEB_PORT` | `5000` | Web dashboard port |

---

## End-to-end workflow

The daily workflow is split into two sessions:

| Time | Command | What it does |
|---|---|---|
| ~9:00 AM | `kickoff` | Pre-market scan — provisional top 3, **no** options flow |
| ~10:30 AM | `midmorning` | Confirms/re-ranks top 3 with live options flow |

### 1. Authenticate with E*TRADE

```bash
cd trading-assistant
python main.py login
```

Runs the three-legged OAuth1 flow:

1. Fetches a request token from E*TRADE
2. Prints an authorization URL — open it in your browser, log in, and E*TRADE shows you a verifier code
3. Paste the verifier back into the terminal to complete the exchange

The session token is saved to `./data/session.json` and reloaded automatically on the next command, so you don't need to re-run `login` between `login` and `kickoff`. The session expires 115 minutes after the original login regardless of restarts. Delete `session.json` or run `login` again to start a new session.

---

### 2. Prepare input files

**Headlines file** — one headline per line. Plain text, bullet lists, and newsletter-style
formats with bold section headers are all accepted:

```
LMT wins $4.76B Army contract for Patriot missile systems
Fed signals two rate cuts in 2026 amid cooling inflation
AMZN announces $25B Mississippi data center investment
```

or:

```
**Top Stories**
- LMT wins $4.76B Army contract for Patriot missile systems
- Fed signals two rate cuts in 2026 amid cooling inflation

**Analyst Actions**
- AMZN raised to Buy at Goldman, PT $250
```

The parser strips `- `/`* ` bullet markers and skips pure Markdown section headers
(`**...**`) so only the actual headline text reaches Claude.

**Positions file** — CSV with `ticker` and `shares` columns:

```
ticker,shares
AMZN,10
NVDA,5
```

---

### 3. Run kickoff (pre-market, ~9:00 AM)

```bash
python main.py kickoff --headlines headlines/2026-04-13.txt --positions positions.txt
```

This does four things in order:

1. **Validates the E*TRADE session** — raises immediately if not logged in or expired
2. **Loads data** — parses the headlines and positions files into SQLite (`./data/trading.db`)
3. **Generates the pre-market report** — two-phase agentic Claude loop:
   - *Phase 1 — SCAN*: Claude nominates up to 10 candidate tickers with a catalyst and `pre_market_score` (high/medium/low). Candidates are saved to the `watchlist` table immediately.
   - *Phase 2 — RESEARCH*: Claude calls `get_quote` then `get_technicals` (mandatory, in that order) for each candidate, with up to 2 more free calls per ticker. `get_options_flow` is **excluded** pre-market (options volume is not meaningful until ~30 min after open).
   - Report saved to `./reports/YYYY-MM-DD.json` and `./reports/YYYY-MM-DD.html`. The HTML includes a provisional banner and a full watchlist table showing all candidates with their catalysts.
   - **Top 3 are provisional** — the HTML shows an amber banner: *"Run: python main.py midmorning"*
4. **Starts the watcher + web dashboard** — the watcher monitors the provisional top 3; Flask starts in the foreground at `http://localhost:5000`

Flags:

```bash
python main.py kickoff --skip-auth   ...   # skip the session check (e.g. sandbox testing)
python main.py kickoff --no-monitor  ...   # start Flask only, no watcher thread
python main.py kickoff --debug       ...   # log full Claude conversation to logs/
```

---

### 4. Run midmorning assessment (~10:30 AM)

```bash
python main.py midmorning
```

Requires `kickoff` to have run first today. This:

1. **Loads today's watchlist** from the DB — no Phase 1 re-scan
2. **Runs Phase 2 research** with all three tools including `get_options_flow`. Uses the same 4-calls-per-ticker / 40-calls-total budget as kickoff.
3. **Confirms the top 3** — each play gets an `options_confirmation` note and a `conviction_change` badge (upgraded / unchanged / downgraded vs the pre-market provisional rank).
4. **Updates the watchlist DB** — sets `confirmed=1`, `confirmed_rank`, and `options_unusual` for each confirmed pick.
5. **Saves** `./reports/YYYY-MM-DD-midmorning.json` and `./reports/YYYY-MM-DD-midmorning.html`, then opens the HTML.

After midmorning runs, the watcher automatically switches to the confirmed top 3 on its next startup.

```bash
python main.py midmorning --debug   # log full Claude conversation to logs/
```

---

### 5. Monitor signals

The watcher loads today's tickers and polls E*TRADE's quote API on each interval. It prefers **confirmed** top 3 tickers from the mid-morning assessment; if midmorning hasn't run yet it falls back to the **provisional** top 3 from kickoff. The startup message says which set is active. For each ticker it tracks:

- Last price, bid, ask, volume
- 5-minute rolling price high/low
- 5-period volume moving average

Three signals are checked each tick:

| Signal | Condition |
|---|---|
| **ENTRY** | Price ≤ entry_low AND volume > `VOLUME_SPIKE_FACTOR` × 5-period average |
| **PROFIT_TARGET** | Price ≥ entry_low × (1 + `PROFIT_TARGET_PCT` / 100) |
| **STOP_LOSS** | Price ≤ entry_low × (1 − `STOP_LOSS_PCT` / 100) |

When a signal fires:
- Rich-formatted output in the terminal
- Desktop notification (via `plyer`)
- Audio alert (TTS via `say` / `spd-say`, or system beep fallback)
- Line appended to `./logs/alerts.log`

Signals are debounced — the same ticker/signal pair won't re-fire within 5 minutes.

If Claude returns a vague entry range (e.g. `"current price on pullback"`) that can't be
parsed, the ticker is still watched and shown in the dashboard — it just fires no signals.
The startup summary marks these as `watching without signals (no entry range)`.

---

### 6. Web dashboard

Open `http://localhost:5000` in your browser.

**Left panel — Session status**
- Green dot + countdown when logged in; red when not
- "Re-login" button runs the OAuth flow inline without touching the terminal:
  1. Click Re-login → an authorization URL appears; open it in your browser
  2. Paste the verifier code into the field and click Complete Login

**Center panel — Live prices** *(auto-refreshes every 10 seconds)*
- One card per monitored ticker showing last price, bid, ask, volume, and last-updated time
- Three signal badges (Entry zone / Profit target / Stop loss) — highlighted when the current price crosses the threshold, dimmed otherwise
- **"Mute alerts" button** — silences all alerts for that ticker until you click "Unmute alerts". Muted cards are dimmed and show an "Alerts muted" label. Mute state is in-memory and clears on restart.

**Right panel — Alerts feed** *(auto-refreshes every 15 seconds)*
- Last 50 alerts from `./logs/alerts.log`, newest first
- Color-coded by signal type: green = ENTRY, blue = PROFIT_TARGET, red = STOP_LOSS

`GET /report` renders today's full HTML report inline.

| Endpoint | Description |
|---|---|
| `GET /api/quotes` | Live quote snapshot (`{}` if watcher not running) |
| `GET /api/alerts` | Last 50 lines of `./logs/alerts.log` |
| `GET /api/session` | `{logged_in, remaining_seconds}` |
| `POST /api/login` | Two-step OAuth1 login (see setup) |
| `GET /api/muted` | List of currently muted tickers |
| `POST /api/mute` | `{"ticker": "AAPL"}` — silence alerts for a ticker |
| `POST /api/unmute` | `{"ticker": "AAPL"}` — re-enable alerts |

---

## Other commands

```bash
# Generate a report without loading new data
python main.py report
python main.py report --debug   # also write full Claude conversation to logs/

# Run the mid-morning assessment
python main.py midmorning
python main.py midmorning --debug

# Start the watcher + dashboard without re-running kickoff
# (session is reloaded from data/session.json automatically)
python main.py monitor
python main.py monitor --interval 30
python main.py monitor --session-summary   # print confirmed/provisional status, then exit

# Watch specific tickers (no report needed)
python main.py watch AAPL MSFT

# Start the dashboard only (no watcher)
python main.py web
```

---

## Running tests

```bash
source .venv/bin/activate
cd trading-assistant
python -m pytest tests/
```

---

## Project structure

```
trading-assistant/
├── auth/           # E*TRADE OAuth1 flow
├── alerts/         # Multi-channel alert dispatch (terminal, desktop, TTS, log)
├── monitor/        # Quote polling loop and shared quote_cache for the web UI
├── report/         # Claude API call, JSON parsing, HTML report writer; midmorning.py
├── store/          # SQLite helpers (headlines, positions, watchlist)
├── web/            # Flask dashboard (app.py + templates/index.html)
├── headlines/      # Input headline files
├── reports/        # Generated JSON + HTML reports
├── logs/           # alerts.log, tool_calls.log
├── data/           # trading.db, session.json (gitignored)
├── main.py         # CLI entry point
└── config.py       # Environment / API config
```
