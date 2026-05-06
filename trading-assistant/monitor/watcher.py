"""
Real-time position monitor.

On startup, prefers confirmed top-3 tickers from today's mid-morning
assessment (watchlist DB).  Falls back to the provisional top-3 from the
pre-market report JSON if the mid-morning has not yet run.

Polls the E*TRADE Quote API every `interval_seconds`, tracking price and
volume and firing entry/exit alerts via alerts.notifier.
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
from auth.etrade_auth import get_session, renew_session, session_remaining_seconds
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
RENEW_THRESHOLD_SECONDS = 30 * 60  # auto-renew when < 30 minutes remain


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
            print(f"  Warning: no entry range for {ticker} ({e}) — watching without signals")
            result[ticker] = (0.0, 0.0)
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
    """Apply fresh QuoteData to a TickerState (price, bid, ask, volume, rolling window).

    During extended hours (pre-market / after-hours), lastTrade reflects the
    prior session close.  Prefer ExtendedHourQuoteDetail.lastPrice when present.
    """
    all_data = quote.get("All", {})
    now = time.monotonic()
    cutoff = now - ROLLING_WINDOW_SECONDS

    eh = all_data.get("ExtendedHourQuoteDetail") or {}
    raw_price = eh.get("lastPrice") or all_data.get("lastTrade") or all_data.get("last") or all_data.get("lastPrice")
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

    if len(state.volume_history) >= 1:
        avg_vol = sum(state.volume_history) / len(state.volume_history)
        vol_spike = avg_vol > 0 and volume > VOLUME_SPIKE_FACTOR * avg_vol
    else:
        vol_spike = False

    if state.entry_low > 0:
        if price <= state.entry_low and vol_spike:
            hi, lo = _rolling_high_low(state)
            range_str = f" | 5m: ${lo:.2f}-${hi:.2f}" if (hi and lo) else ""
            reason = (
                f"price ${price:.2f} <= entry_low ${state.entry_low:.2f}"
                f" | vol {volume:,} > {VOLUME_SPIKE_FACTOR}x avg {avg_vol:,.0f}"
                f"{range_str}"
            )
            send_alert(state.ticker, "ENTRY", price, reason)

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
# Session summary helpers
# ---------------------------------------------------------------------------

def get_session_summary(session_date: date | None = None) -> dict:
    """
    Return a dict describing which ticker set the watcher will use.

    Keys:
        mode    — "confirmed", "provisional", or "unavailable"
        tickers — list of ticker strings
        reason  — human-readable explanation
    """
    if session_date is None:
        session_date = date.today()

    try:
        from store.db import get_watchlist, watchlist_exists
    except ImportError:
        return {"mode": "unavailable", "tickers": [], "reason": "DB module unavailable."}

    if watchlist_exists(session_date):
        watchlist = get_watchlist(session_date)
        confirmed = sorted(
            [w for w in watchlist if w.get("confirmed") and w.get("confirmed_rank")],
            key=lambda w: w["confirmed_rank"],
        )
        if confirmed:
            tickers = [c["ticker"] for c in confirmed[:3]]
            return {
                "mode": "confirmed",
                "tickers": tickers,
                "reason": "Mid-morning assessment complete — monitoring confirmed top 3.",
            }

        provisional = sorted(
            [w for w in watchlist if w.get("rank") is not None],
            key=lambda w: w["rank"],
        )[:3]
        if provisional:
            tickers = [p["ticker"] for p in provisional]
            return {
                "mode": "provisional",
                "tickers": tickers,
                "reason": "Monitoring provisional top 3 (mid-morning not yet run).",
            }

    # Fall back to report JSON
    try:
        tickers_ranges = _load_report_tickers()
        tickers = list(tickers_ranges.keys())
        return {
            "mode": "provisional",
            "tickers": tickers,
            "reason": "Monitoring provisional top 3 from report JSON (mid-morning not yet run).",
        }
    except RuntimeError:
        return {"mode": "unavailable", "tickers": [], "reason": "No report found for today."}


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
    """
    states: dict[str, TickerState] = {}
    for sym in symbols:
        low, high = (entry_ranges or {}).get(sym, (0.0, float("inf")))
        states[sym] = TickerState(ticker=sym, entry_low=low, entry_high=high)

    print(f"Watching {', '.join(symbols)} every {interval_seconds}s. Press Ctrl-C to stop.")
    if entry_ranges:
        for sym, (low, high) in entry_ranges.items():
            if sym in states:
                if low > 0:
                    print(
                        f"  {sym}: entry ${low:.2f}–${high:.2f} | "
                        f"profit_target +{PROFIT_TARGET_PCT}% | stop_loss -{STOP_LOSS_PCT}%"
                    )
                else:
                    print(f"  {sym}: watching without signals (no entry range)")
    print()

    try:
        while True:
            tick_start = time.monotonic()
            try:
                remaining = session_remaining_seconds()
                if remaining is not None and remaining < RENEW_THRESHOLD_SECONDS:
                    if renew_session():
                        print(f"  [info] Session renewed — good for another 115 minutes.")
                    else:
                        print(f"  [warn] Session renewal failed — {remaining // 60} min remaining.")
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
    """
    Load today's top-play tickers and start the watch loop.

    Preference order:
    1. Confirmed top 3 from the watchlist DB (mid-morning assessment ran)
    2. Provisional top 3 from the watchlist DB (pre-market ran, no mid-morning yet)
    3. Top 3 from the report JSON file (fallback)
    """
    today = date.today()

    # Try to load entry ranges from report JSON (used for signal thresholds)
    try:
        report_tickers_ranges = _load_report_tickers()
    except RuntimeError:
        report_tickers_ranges = {}

    summary = get_session_summary(today)

    if summary["mode"] == "unavailable":
        raise RuntimeError(
            summary["reason"] + " Run 'report' or 'kickoff' first."
        )

    tickers = summary["tickers"]
    if not tickers:
        raise RuntimeError("No tickers found for today's watch session.")

    print(summary["reason"])

    # Build entry ranges: prefer report JSON values, zero out anything missing
    entry_ranges = {t: report_tickers_ranges.get(t, (0.0, 0.0)) for t in tickers}
    print(f"Loaded {len(tickers)} tickers: {', '.join(tickers)}")

    watch(tickers, entry_ranges=entry_ranges, interval_seconds=interval_seconds)
