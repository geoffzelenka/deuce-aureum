"""
Flask web dashboard for the trading assistant.

Routes
------
GET  /             Dashboard with live price cards and alerts feed.
GET  /report       Today's HTML report rendered inline.
GET  /api/quotes   JSON snapshot of the latest quotes from the watcher.
GET  /api/alerts   JSON list of the last 50 lines from logs/alerts.log.
GET  /api/session  JSON session status (logged_in, remaining_seconds).
POST /api/login    Two-step E*TRADE OAuth1 login flow.
                   Step 1 – empty body  → {"auth_url": "..."}
                   Step 2 – {"verifier": "..."} → {"success": true}
"""

import json
import os
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder="templates")


def _web_port() -> int:
    return int(os.getenv("WEB_PORT", "5000"))


def _today_report_exists() -> bool:
    today = date.today().strftime("%Y-%m-%d")
    return Path(f"./reports/{today}.json").exists()


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", has_report=_today_report_exists())


@app.route("/report")
def report_page():
    today = date.today().strftime("%Y-%m-%d")
    path = Path(f"./reports/{today}.html")
    if path.exists():
        return path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}
    return (
        "<html><body style='background:#0d1117;color:#e6edf3;"
        "font-family:monospace;padding:40px'>"
        "<h2>No report for today.</h2>"
        "<p>Run <code>python main.py kickoff ...</code> to generate one.</p>"
        "</body></html>",
        404,
        {"Content-Type": "text/html; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/quotes")
def api_quotes():
    from monitor.watcher import quote_cache, _cache_lock
    with _cache_lock:
        data = dict(quote_cache)
    return jsonify(data)


@app.route("/api/alerts")
def api_alerts():
    log_path = Path("./logs/alerts.log")
    if not log_path.exists():
        return jsonify([])
    lines = log_path.read_text(encoding="utf-8").splitlines()
    return jsonify(lines[-50:])


@app.route("/api/session")
def api_session():
    from auth.etrade_auth import is_logged_in, session_remaining_seconds
    logged_in = is_logged_in()
    return jsonify({
        "logged_in": logged_in,
        "remaining_seconds": session_remaining_seconds() if logged_in else None,
    })


@app.route("/api/muted")
def api_muted():
    from alerts.notifier import get_muted
    return jsonify(get_muted())


@app.route("/api/mute", methods=["POST"])
def api_mute():
    from alerts.notifier import mute_ticker
    body = request.get_json(silent=True) or {}
    ticker = body.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    mute_ticker(ticker)
    return jsonify({"muted": ticker})


@app.route("/api/unmute", methods=["POST"])
def api_unmute():
    from alerts.notifier import unmute_ticker
    body = request.get_json(silent=True) or {}
    ticker = body.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    unmute_ticker(ticker)
    return jsonify({"unmuted": ticker})


@app.route("/api/renew", methods=["POST"])
def api_renew():
    from auth.etrade_auth import renew_session
    try:
        success = renew_session()
        return jsonify({"success": success})
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/login", methods=["POST"])
def api_login():
    from auth.etrade_auth import start_login, complete_login

    body = request.get_json(silent=True) or {}
    verifier = body.get("verifier", "").strip()

    if verifier:
        # Step 2 — exchange verifier for access token
        try:
            complete_login(verifier)
            return jsonify({"success": True})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
    else:
        # Step 1 — fetch request token, return authorization URL
        try:
            auth_url = start_login()
            return jsonify({"auth_url": auth_url})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run(host: str = "0.0.0.0", port: int | None = None, debug: bool = False) -> None:
    if port is None:
        port = _web_port()
    app.run(host=host, port=port, debug=debug, use_reloader=False)
