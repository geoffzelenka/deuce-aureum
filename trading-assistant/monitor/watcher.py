"""
Real-time position monitor.

Loads the top-3 tickers from today's morning report, then polls the
E*TRADE Quote API every `interval_seconds`, tracking price and volume
and firing entry/exit alerts via alerts.notifier.
"""

import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import config
from auth.etrade_auth import get_session
from alerts.notifier import send_alert

# ---------------------------------------------------------------------------
# Shared quote cache — written by the watcher, read by the Flask web UI
# ---------------------------------------------------------------------------

quote_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Thresholds — all overridable via .env
# ---------------------------------------------------------------------------

VOLUME_SPIKE_FACTOR = float(os.getenv("VOLUME_SPIKE_FACTOR", "1.5"))
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "2.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "1.0"))

ROLLING_WINDOW_SECONDS = 5 * 60  # 5-minute high/low window
VOLUME_PERIODS = 5               # ticks used for volume moving average


# ---------------------------------------------------------------------------
# Per-ticker state
# ---------------------------------------------------------------------------

@dataclass
class TickerState:
    ticker: str
    entry_low: float   # lower bound of entry_range from the report
    entry_high: float  # upper bound of entry_range from the report

    # Latest quote values
    last_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None

    # Rolling 5-minute price window: deque of (monotonic_time, price)
    rolling: deque = field(default_factory=deque)

    # Previous VOLUME_PERIODS volume readings for spike detection.
    # Populated AFTER the signal check each tick so the current reading
    # is always compared against prior history.
    volume_history: deque = field(default_factory=lambda: deque(maxlen=VOLUME_PERIODS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_entry_range(entry_range: str) -> tuple[float, float]:
    """Parse "$485-$492" or "$38-$41" → (low, high)."""
    m = re.match(r'\$?([\d,.]+)\s*-\s*\$?([\d,.]+)', entry_range.strip())
    if not m:
        raise ValueError(f"Cannot parse entry_range: {entry_range!r}")
    return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))


def _load_report_tickers() -> dict[str, tuple[float, float]]:
    """Read today's report JSON; return {ticker: (entry_low, entry_high)} for top_plays."""
    today = date.today().strftime("%Y-%m-%d")
    path = f"./reports/{today}.json"
    if not os.path.exists(path):
        raise RuntimeError(
            f"No report found for today ({path}). "
            "Run 'report' or 'kickoff' first."
        )
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    result: dict[str, tuple[float, float]] = {}
    for play in data.get("top_plays", [])[:3]:
        ticker = play["ticker"]
        try:
            result[ticker] = _parse_entry_range(play["entry_range"])
        except ValueError as e:
            print(f"  Warning: skipping {ticker}: {e}")
    return result


def _fetch_quotes(session, symbols: list[str]) -> list[dict]:
    """
    GET /v1/market/quote/{symbols} with detailFlag=ALL.
    Returns the QuoteData list, or [] on any error (watcher continues).
    """
    url = f"{config.BASE_URL}/v1/market/quote/{','.join(symbols)}"
    try:
        resp = session.get(
            url,
            params={"detailFlag": "ALL"},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        try:
            return resp.json().get("QuoteResponse", {}).get("QuoteData", [])
        except Exception as exc:
            print(f"  [warn] Quote fetch failed (JSON parse): {exc}")
            print(f"  [warn] Response body: {resp.text[:300]}")
            return []
    except Exception as exc:
        print(f"  [warn] Quote fetch failed: {exc}")
        return []


def _update_state(state: TickerState, quote: dict) -> None:
    """Apply fresh QuoteData to a TickerState (price, bid, ask, volume, rolling window)."""
    all_data = quote.get("All", {})
    now = time.monotonic()
    cutoff = now - ROLLING_WINDOW_SECONDS

    # Price — E*TRADE may use lastTrade or last depending on detailFlag
    raw_price = all_data.get("lastTrade") or all_data.get("last") or all_data.get("lastPrice")
    if raw_price is not None:
        state.last_price = float(raw_price)
        state.rolling.append((now, state.last_price))
        while state.rolling and state.rolling[0][0] < cutoff:
            state.rolling.popleft()

    raw_bid = all_data.get("bid")
    if raw_bid is not None:
        state.bid = float(raw_bid)

    raw_ask = all_data.get("ask")
    if raw_ask is not None:
        state.ask = float(raw_ask)

    raw_vol = (
        all_data.get("totalVolume")
        or all_data.get("volume")
        or all_data.get("cumulativeVolume")
    )
    if raw_vol is not None:
        state.volume = int(raw_vol)


def _rolling_high_low(state: TickerState) -> tuple[Optional[float], Optional[float]]:
    if not state.rolling:
        return None, None
    prices = [p for _, p in state.rolling]
    return max(prices), min(prices)


def _check_signals(state: TickerState) -> None:
    """
    Evaluate entry and exit signals for one ticker, fire alerts if triggered,
    then update the volume history for next tick's comparison.
    """
    if state.last_price is None or state.volume is None:
        return

    price = state.last_price
    volume = state.volume

    # --- Volume spike: current volume vs average of previous VOLUME_PERIODS readings ---
    if len(state.volume_history) >= 1:
        avg_vol = sum(state.volume_history) / len(state.volume_history)
        vol_spike = avg_vol > 0 and volume > VOLUME_SPIKE_FACTOR * avg_vol
    else:
        vol_spike = False  # not enough history yet

    # --- Entry signal ---
    if price <= state.entry_low and vol_spike:
        hi, lo = _rolling_high_low(state)
        range_str = f" | 5m: ${lo:.2f}-${hi:.2f}" if (hi and lo) else ""
        reason = (
            f"price ${price:.2f} <= entry_low ${state.entry_low:.2f}"
            f" | vol {volume:,} > {VOLUME_SPIKE_FACTOR}x avg {avg_vol:,.0f}"
            f"{range_str}"
        )
        send_alert(state.ticker, "ENTRY", price, reason)

    # --- Exit signals (referenced from entry_low as the entry price anchor) ---
    entry_ref = state.entry_low

    profit_price = entry_ref * (1 + PROFIT_TARGET_PCT / 100)
    if price >= profit_price:
        reason = (
            f"price ${price:.2f} >= profit target ${profit_price:.2f}"
            f" (+{PROFIT_TARGET_PCT}% above ${entry_ref:.2f})"
        )
        send_alert(state.ticker, "PROFIT_TARGET", price, reason)

    stop_price = entry_ref * (1 - STOP_LOSS_PCT / 100)
    if price <= stop_price:
        reason = (
            f"price ${price:.2f} <= stop loss ${stop_price:.2f}"
            f" (-{STOP_LOSS_PCT}% below ${entry_ref:.2f})"
        )
        send_alert(state.ticker, "STOP_LOSS", price, reason)

    # Append current volume AFTER signal check — becomes "previous" on next tick
    state.volume_history.append(volume)


def _write_cache(state: "TickerState") -> None:
    """Snapshot the latest TickerState into quote_cache for the web dashboard."""
    entry_low = state.entry_low
    profit_target = round(entry_low * (1 + PROFIT_TARGET_PCT / 100), 2) if entry_low > 0 else None
    stop_loss = round(entry_low * (1 - STOP_LOSS_PCT / 100), 2) if entry_low > 0 else None
    snapshot = {
        "ticker": state.ticker,
        "last_price": state.last_price,
        "bid": state.bid,
        "ask": state.ask,
        "volume": state.volume,
        "entry_low": entry_low,
        "entry_high": state.entry_high,
        "profit_target": profit_target,
        "stop_loss": stop_loss,
        "updated_at": time.strftime("%H:%M:%S"),
    }
    with _cache_lock:
        quote_cache[state.ticker] = snapshot


def _print_status(states: dict[str, "TickerState"]) -> None:
    timestamp = time.strftime("%H:%M:%S")
    parts = []
    for sym, st in states.items():
        if st.last_price is not None:
            hi, lo = _rolling_high_low(st)
            range_str = f" [5m ${lo:.2f}-${hi:.2f}]" if (hi and lo) else ""
            bid_ask = f" ({st.bid:.2f}/{st.ask:.2f})" if (st.bid and st.ask) else ""
            vol_str = f" vol {st.volume:,}" if st.volume else ""
            parts.append(f"{sym} ${st.last_price:.2f}{bid_ask}{range_str}{vol_str}")
        else:
            parts.append(f"{sym} --")
    print(f"[{timestamp}]  " + "   ".join(parts))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def watch(
    symbols: list[str],
    entry_ranges: Optional[dict[str, tuple[float, float]]] = None,
    interval_seconds: int = 60,
) -> None:
    """
    Poll E*TRADE for quotes on the given symbols every `interval_seconds`.
    Runs until interrupted with Ctrl-C.

    entry_ranges: {ticker: (entry_low, entry_high)} from the morning report.
                  When None, entry/exit signal thresholds are not enforced.
    """
    states: dict[str, TickerState] = {}
    for sym in symbols:
        low, high = (entry_ranges or {}).get(sym, (0.0, float("inf")))
        states[sym] = TickerState(ticker=sym, entry_low=low, entry_high=high)

    print(f"Watching {', '.join(symbols)} every {interval_seconds}s. Press Ctrl-C to stop.")
    if entry_ranges:
        for sym, (low, high) in entry_ranges.items():
            if sym in states:
                print(
                    f"  {sym}: entry ${low:.2f}–${high:.2f} | "
                    f"profit_target +{PROFIT_TARGET_PCT}% | stop_loss -{STOP_LOSS_PCT}%"
                )
    print()

    try:
        while True:
            tick_start = time.monotonic()
            try:
                session = get_session()
                quotes = _fetch_quotes(session, symbols)
                for qd in quotes:
                    sym = qd.get("Product", {}).get("symbol", "")
                    if sym in states:
                        _update_state(states[sym], qd)
                        _check_signals(states[sym])
                        _write_cache(states[sym])
                _print_status(states)
            except RuntimeError as exc:
                print(f"  [error] {exc}")

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, interval_seconds - elapsed))

    except KeyboardInterrupt:
        print("\nWatcher stopped.")


def monitor_from_report(interval_seconds: int = 60) -> None:
    """Load today's top-play tickers from the morning report and start the watch loop."""
    tickers_ranges = _load_report_tickers()
    if not tickers_ranges:
        raise RuntimeError("No tickers found in today's report.")
    symbols = list(tickers_ranges.keys())
    print(f"Loaded {len(symbols)} tickers from today's report: {', '.join(symbols)}")
    watch(symbols, entry_ranges=tickers_ranges, interval_seconds=interval_seconds)
