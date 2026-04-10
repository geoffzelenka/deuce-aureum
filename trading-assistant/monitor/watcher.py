"""
Price-polling loop (stub).
"""

import time

from auth.etrade_auth import get_session


def watch(symbols: list[str], interval_seconds: int = 60) -> None:
    """
    Poll E*TRADE for quotes on the given symbols every `interval_seconds`.
    Runs until interrupted with Ctrl-C.

    TODO:
      - Call E*TRADE market/quote endpoint
      - Compare against alert thresholds
      - Dispatch alerts via alerts.notifier
    """
    print(f"Watching {symbols} every {interval_seconds}s. Press Ctrl-C to stop.")
    try:
        while True:
            session = get_session()  # noqa: F841 — will be used in full implementation
            print(f"[{time.strftime('%H:%M:%S')}] Tick — price polling not yet implemented.")
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
