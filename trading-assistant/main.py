#!/usr/bin/env python3
"""
trading-assistant — CLI entry point

Usage:
  python main.py login
  python main.py report
  python main.py monitor
  python main.py watch AAPL MSFT
  python main.py web
"""

import argparse
import sys


def cmd_login(_args) -> None:
    from auth.etrade_auth import login
    login()


def cmd_report(_args) -> None:
    _run_report()


def _run_report() -> None:
    """Generate and open the morning report (shared by `report` and `kickoff`)."""
    import itertools
    import json
    import os
    import threading
    from datetime import date

    from report.generator import generate_morning_report
    from report.html_writer import write_html_report

    done = threading.Event()

    def _spinner() -> None:
        for ch in itertools.cycle("|/-\\"):
            if done.is_set():
                break
            print(f"\r  Generating report... {ch}", end="", flush=True)
            done.wait(0.1)
        print("\r  Report generated.          ")

    t = threading.Thread(target=_spinner, daemon=True)
    t.start()
    try:
        report = generate_morning_report()
    finally:
        done.set()
        t.join()

    today_str = date.today().strftime("%Y-%m-%d")
    os.makedirs("./reports", exist_ok=True)

    json_path = f"./reports/{today_str}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    html_path = write_html_report(report)
    print(f"  Saved JSON : {json_path}")
    print(f"  Saved HTML : {html_path}")
    return report


def cmd_monitor(args) -> None:
    import os
    import threading
    from monitor.watcher import monitor_from_report

    t = threading.Thread(
        target=monitor_from_report,
        kwargs={"interval_seconds": args.interval},
        daemon=True,
    )
    t.start()

    from web.app import run as run_web
    port = int(os.getenv("WEB_PORT", "5000"))
    print(f"  Dashboard: http://localhost:{port}")
    run_web(host="0.0.0.0", port=port)


def cmd_watch(args) -> None:
    from monitor.watcher import watch
    watch(args.symbols, interval_seconds=args.interval)


def cmd_web(args) -> None:
    from web.app import run
    run(host=args.host, port=args.port, debug=args.debug)


def cmd_kickoff(args) -> None:
    import os
    import threading
    from datetime import date
    from auth.etrade_auth import get_session
    from store.db import (
        insert_headlines, upsert_positions,
        parse_headlines_file, parse_positions_file,
    )

    if not args.skip_auth:
        get_session()  # raises RuntimeError if not logged in / expired

    n_headlines = 0
    if args.headlines:
        lines = parse_headlines_file(args.headlines)
        n_headlines = insert_headlines(lines, date.today())

    n_positions = 0
    if args.positions:
        rows = parse_positions_file(args.positions)
        upsert_positions(rows)
        n_positions = len(rows)

    print(f"Loaded {n_headlines} headlines, {n_positions} positions.")

    _run_report()

    if not args.no_monitor:
        from monitor.watcher import monitor_from_report
        t = threading.Thread(target=monitor_from_report, daemon=True)
        t.start()

    from web.app import run as run_web
    port = int(os.getenv("WEB_PORT", "5000"))
    print(f"  Dashboard: http://localhost:{port}")
    run_web(host="0.0.0.0", port=port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-assistant",
        description="E*TRADE trading assistant",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # login
    sub.add_parser("login", help="Authenticate with E*TRADE via OAuth1")

    # report
    sub.add_parser("report", help="Generate a morning report from stored headlines and positions")

    # monitor
    p_monitor = sub.add_parser(
        "monitor", help="Watch top-play tickers from today's report and fire alerts"
    )
    p_monitor.add_argument(
        "--interval", type=int, default=60, metavar="SECONDS",
        help="Polling interval in seconds (default: 60)",
    )

    # watch
    p_watch = sub.add_parser("watch", help="Poll prices and trigger alerts")
    p_watch.add_argument("symbols", nargs="+", metavar="SYMBOL")
    p_watch.add_argument(
        "--interval", type=int, default=60, metavar="SECONDS",
        help="Polling interval in seconds (default: 60)"
    )

    # web
    p_web = sub.add_parser("web", help="Start the Flask web UI")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=5000)
    p_web.add_argument("--debug", action="store_true")

    # kickoff
    p_kickoff = sub.add_parser("kickoff", help="Load headlines and positions into the database")
    p_kickoff.add_argument("--headlines", metavar="PATH", help="Path to headlines file")
    p_kickoff.add_argument("--positions", metavar="PATH", help="Path to positions CSV")
    p_kickoff.add_argument("--skip-auth", action="store_true", help="Skip E*TRADE session check")
    p_kickoff.add_argument(
        "--no-monitor", action="store_true",
        help="Skip starting the real-time monitor after the report is generated",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "login": cmd_login,
        "report": cmd_report,
        "monitor": cmd_monitor,
        "watch": cmd_watch,
        "web": cmd_web,
        "kickoff": cmd_kickoff,
    }
    try:
        dispatch[args.command](args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
