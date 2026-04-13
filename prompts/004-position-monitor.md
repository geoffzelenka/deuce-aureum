In the `trading-assistant` project, implement the real-time position monitor.

**monitor/watcher.py**
- After the morning report is generated, extract the 3 top-play tickers from `./reports/<today>.json`.
- Poll the E*TRADE Quote API (`GET /v1/market/quote/{symbols}`) every 60 seconds using the live OAuth1 session from `auth/etrade_auth.py`. Call `get_session()` before every request to ensure the session hasn't expired.
- For each ticker track: last price, bid, ask, volume, and a rolling 5-minute high/low.
- Entry signal logic (configurable via .env thresholds):
  - Price drops to or below the lower bound of `entry_range` from the report JSON.
  - Volume spike: current volume > 1.5x the 5-period average (configurable as `VOLUME_SPIKE_FACTOR`).
- Exit signal logic:
  - Price rises 2% above entry price (configurable as `PROFIT_TARGET_PCT`, default 2.0).
  - Price drops 1% below entry price (configurable as `STOP_LOSS_PCT`, default 1.0).
- When a signal fires, call `alerts/notifier.py`.

**alerts/notifier.py**
- Implement three alert channels, all triggered together on a signal:
  1. Terminal: print a formatted, color-coded line using `rich` library. Green for entry, red for stop-loss, yellow for profit target.
  2. Desktop notification: use `plyer` library (`plyer.notification.notify`). Title should be the ticker and signal type. Falls back gracefully if `plyer` is unavailable.
  3. Sound: use system TTS to speak the alert type followed by the ticker letters individually (e.g. "Entry. L M T"). Use `spd-say -w` on Linux, `say` on macOS, PowerShell SAPI on Windows. Fall back to a beep (`winsound` / `afplay` / `aplay`) if TTS is unavailable.
- Log every alert to `./logs/alerts.log` with timestamp, ticker, signal type, price, and reason.
- Debounce: do not re-fire the same signal for the same ticker within 5 minutes.

**main.py additions**
- Add a `monitor` subcommand that starts the watcher loop. It should run until Ctrl+C.
- `kickoff` should automatically chain into `monitor` after the report is generated, unless `--no-monitor` flag is passed.
