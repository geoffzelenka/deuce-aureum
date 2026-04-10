"""
Alert dispatch (stub).
"""


def send_alert(symbol: str, message: str) -> None:
    """
    Dispatch an alert for `symbol`.

    TODO: implement delivery channels (email, SMS, desktop notification, etc.)
    """
    print(f"[ALERT] {symbol}: {message}")
