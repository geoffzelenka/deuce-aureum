In the `trading-assistant` project, implement a Flask web dashboard.

**web/app.py**
- Flask app on port 5000 (configurable via .env as `WEB_PORT`).
- Routes:
  - `GET /` — dashboard showing live prices for monitored tickers, updated via polling.
  - `GET /report` — renders today's HTML report inline (load from `./reports/<today>.html`).
  - `GET /api/quotes` — JSON endpoint returning the latest quote data held in memory by the watcher.
  - `GET /api/alerts` — JSON endpoint returning the last 50 alerts from `./logs/alerts.log`.
  - `POST /api/login` — triggers the E*TRADE OAuth1 login flow and returns success/failure.

**Dashboard page (templates/index.html)**
- Three-panel layout:
  - Left panel: session status (logged in / time remaining before expiry, with a "Re-login" button).
  - Center panel: live price cards for each monitored ticker. Each card shows ticker, last price, bid/ask, volume, and colored signal indicators (entry zone, profit target, stop-loss levels). Auto-refresh via `fetch('/api/quotes')` every 10 seconds.
  - Right panel: recent alerts feed, auto-refreshing every 15 seconds.
- Use plain HTML + CSS + vanilla JS only (no frontend framework). Responsive layout using CSS Grid.
- Dark mode by default using CSS custom properties.

**Integration with watcher**
- The watcher loop (`monitor/watcher.py`) should write its latest quote snapshot to a shared in-memory dict (use a module-level `quote_cache` dict).
- The Flask app and watcher should run concurrently in the same process: start the watcher in a `threading.Thread(daemon=True)` when the `kickoff` or `monitor` subcommand is used, then start Flask.
- On startup, `web/app.py` should check if today's report JSON exists and load it; if not, show a "No report yet — run kickoff first" message on the dashboard.

Run the full app with `python main.py kickoff --headlines headlines.txt --positions positions.txt` and verify the dashboard is reachable at http://localhost:5000.
