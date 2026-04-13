"""
Alert dispatch — terminal (rich), desktop notification (plyer), system beep, and log file.

Signal types: "ENTRY" | "PROFIT_TARGET" | "STOP_LOSS"
"""

import logging
import os
import platform
import subprocess
import threading
import time

DEBOUNCE_SECONDS = 5 * 60  # 5 minutes

_debounce_lock = threading.Lock()
_last_fired: dict[tuple[str, str], float] = {}  # (ticker, signal_type) -> monotonic time

LOG_DIR = "./logs"
LOG_FILE = os.path.join(LOG_DIR, "alerts.log")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_logger() -> logging.Logger:
    logger = logging.getLogger("trading_alerts")
    if not logger.handlers:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.FileHandler(LOG_FILE)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def _is_debounced(ticker: str, signal_type: str) -> bool:
    """Return True (and skip) if the same signal fired within DEBOUNCE_SECONDS."""
    key = (ticker, signal_type)
    now = time.monotonic()
    with _debounce_lock:
        last = _last_fired.get(key)
        if last is not None and (now - last) < DEBOUNCE_SECONDS:
            return True
        _last_fired[key] = now
        return False


def _terminal_alert(ticker: str, signal_type: str, price: float, reason: str) -> None:
    try:
        from rich.console import Console
        console = Console()
        timestamp = time.strftime("%H:%M:%S")
        if signal_type == "ENTRY":
            style = "bold green"
            icon = "▲"
        elif signal_type == "STOP_LOSS":
            style = "bold red"
            icon = "▼"
        else:  # PROFIT_TARGET
            style = "bold yellow"
            icon = "★"
        console.print(
            f"[{timestamp}] {icon} {signal_type:<14s} {ticker:<8s} @ ${price:.2f}  {reason}",
            style=style,
        )
    except ImportError:
        # rich not installed — plain fallback
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] [ALERT] {signal_type} {ticker} @ ${price:.2f}  {reason}")


def _desktop_notify(ticker: str, signal_type: str, price: float) -> None:
    try:
        from plyer import notification  # type: ignore[import]
        notification.notify(
            title=f"{ticker} — {signal_type}",
            message=f"Price: ${price:.2f}",
            app_name="Trading Assistant",
            timeout=10,
        )
    except Exception:
        pass  # plyer unavailable or notification daemon not running — silent fallback


_SIGNAL_LABELS = {
    "ENTRY":         "Entry",
    "PROFIT_TARGET": "Profit target",
    "STOP_LOSS":     "Stop loss",
}


def _speak_alert(signal_type: str, ticker: str) -> None:
    """Speak the signal type then the ticker letters using the system TTS engine.
    Falls back to a beep if TTS is unavailable."""
    label = _SIGNAL_LABELS.get(signal_type, signal_type)
    # Space out the letters so TTS reads each one individually: "LMT" → "L M T"
    spaced_ticker = " ".join(ticker.upper())
    text = f"{label}. {spaced_ticker}"

    system = platform.system()
    try:
        if system == "Windows":
            # PowerShell SAPI — available on all modern Windows
            ps_cmd = (
                f"Add-Type -AssemblyName System.Speech; "
                f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                check=False, capture_output=True,
            )
        elif system == "Darwin":
            subprocess.run(["say", text], check=False, capture_output=True)
        else:  # Linux — speech-dispatcher
            subprocess.run(["spd-say", "-w", text], check=False, capture_output=True)
    except Exception:
        # TTS unavailable — fall back to a plain beep
        _beep_fallback(signal_type)


def _beep_fallback(signal_type: str) -> None:
    """Simple beep when TTS is not available."""
    system = platform.system()
    try:
        if system == "Windows":
            import winsound  # type: ignore[import]
            if signal_type == "ENTRY":
                winsound.Beep(880, 200); winsound.Beep(1100, 200)
            elif signal_type == "STOP_LOSS":
                winsound.Beep(440, 400); winsound.Beep(330, 400)
            else:
                winsound.Beep(660, 200); winsound.Beep(880, 300)
        elif system == "Darwin":
            sounds = {
                "ENTRY":         "/System/Library/Sounds/Ping.aiff",
                "STOP_LOSS":     "/System/Library/Sounds/Basso.aiff",
                "PROFIT_TARGET": "/System/Library/Sounds/Glass.aiff",
            }
            subprocess.run(
                ["afplay", sounds.get(signal_type, "/System/Library/Sounds/Ping.aiff")],
                check=False, capture_output=True,
            )
        else:  # Linux
            sounds = {
                "ENTRY":         "/usr/share/sounds/alsa/Front_Left.wav",
                "STOP_LOSS":     "/usr/share/sounds/alsa/Rear_Left.wav",
                "PROFIT_TARGET": "/usr/share/sounds/alsa/Front_Center.wav",
            }
            subprocess.run(
                ["aplay", "-q", sounds.get(signal_type, "/usr/share/sounds/alsa/Front_Left.wav")],
                check=False, capture_output=True,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_alert(ticker: str, signal_type: str, price: float, reason: str) -> None:
    """
    Fire an alert on all channels for `ticker`.

    signal_type: "ENTRY" | "PROFIT_TARGET" | "STOP_LOSS"
    Debounced: the same (ticker, signal_type) pair will not re-fire within 5 minutes.
    """
    if _is_debounced(ticker, signal_type):
        return

    _terminal_alert(ticker, signal_type, price, reason)
    _desktop_notify(ticker, signal_type, price)
    _speak_alert(signal_type, ticker)

    _get_logger().info("%-14s | %-8s | price=%.2f | %s", signal_type, ticker, price, reason)
