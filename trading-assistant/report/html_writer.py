"""
Write the trading report dict to a styled, self-contained HTML file
and open it in the default browser.

session_type controls the layout:
  "report"     — standard morning report (no banner, no watchlist section)
  "premarket"  — adds provisional banner + watchlist candidate table
  "midmorning" — midmorning card layout with options_confirmation,
                 conviction_change, and watchlist_dropped section
"""

import os
import webbrowser
from datetime import date

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #f5f5f7;
    color: #1d1d1f;
    padding: 2rem 1rem;
    line-height: 1.6;
}

@media (prefers-color-scheme: dark) {
    body { background: #0d0d0f; color: #e8e8ed; }
    .section { background: #1c1c1e; border-color: #2c2c2e; }
    .card { background: #2c2c2e; border-color: #3a3a3c; }
    .card h3 { color: #ffffff; }
    .badge-day { background: #0a3d62; color: #7fc8f8; }
    .badge-overnight { background: #3b1f5e; color: #d4a8ff; }
    .badge-bullish { background: #0d3b2a; color: #4cd964; }
    .badge-neutral { background: #3a3a1a; color: #f5c518; }
    .badge-bearish { background: #3b0d0d; color: #ff6b6b; }
    .badge-high { background: #0d3b2a; color: #4cd964; }
    .badge-medium { background: #3a3a1a; color: #f5c518; }
    .badge-low { background: #3b0d0d; color: #ff6b6b; }
    .badge-pending { background: #3b2d00; color: #f5c518; }
    .badge-confirmed { background: #0d3b2a; color: #4cd964; }
    .badge-upgraded { background: #0d3b2a; color: #4cd964; }
    .badge-downgraded { background: #3b0d0d; color: #ff6b6b; }
    .badge-unchanged { background: #3a3a1a; color: #f5c518; }
    .label { color: #8e8e93; }
    h1 { color: #ffffff; }
    h2 { color: #e8e8ed; border-bottom-color: #3a3a3c; }
    .table td { border-bottom-color: #2c2c2e; }
    .banner { background: #3b2d00; border-color: #a07000; color: #f5c518; }
    .banner code { background: #2c2200; color: #ffd060; }
}

.container { max-width: 960px; margin: 0 auto; }

header { margin-bottom: 2.5rem; }
h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.5px; }
.subtitle { color: #6e6e73; margin-top: 0.25rem; font-size: 0.95rem; }

h2 {
    font-size: 1.25rem;
    font-weight: 600;
    margin-bottom: 1.25rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid #d2d2d7;
    letter-spacing: -0.25px;
}

.section {
    background: #ffffff;
    border: 1px solid #d2d2d7;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1.75rem;
}

/* ── Pre-market provisional banner ── */
.banner {
    background: #fff8e1;
    border: 1px solid #f5c518;
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 1.75rem;
    font-size: 0.95rem;
    color: #5c4200;
}
.banner strong { font-weight: 700; }
.banner code {
    background: #fef3c7;
    border-radius: 4px;
    padding: 0.1rem 0.4rem;
    font-family: "SF Mono", "Fira Code", "Consolas", monospace;
    font-size: 0.88rem;
}

/* ── Top Plays ── */
.plays-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 1rem;
}

.card {
    background: #f5f5f7;
    border: 1px solid #d2d2d7;
    border-radius: 10px;
    padding: 1.1rem 1.2rem;
}

.card-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.75rem;
    flex-wrap: wrap;
}

.card h3 { font-size: 1.15rem; font-weight: 700; color: #1d1d1f; }

.badge {
    display: inline-block;
    font-size: 0.72rem;
    font-weight: 600;
    padding: 0.15rem 0.5rem;
    border-radius: 20px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    white-space: nowrap;
}

.badge-day { background: #e1f0ff; color: #0070c9; }
.badge-overnight { background: #f0e8ff; color: #7a37d0; }

/* pre_market_score badges */
.badge-high     { background: #d4f5e2; color: #1d7a43; }
.badge-medium   { background: #fef9e1; color: #8a6c00; }
.badge-low      { background: #fde8e8; color: #c0392b; }

/* watchlist status */
.badge-pending    { background: #fff3cd; color: #7c5200; }
.badge-confirmed  { background: #d4f5e2; color: #1d7a43; }

/* conviction change */
.badge-upgraded   { background: #d4f5e2; color: #1d7a43; }
.badge-unchanged  { background: #fef9e1; color: #8a6c00; }
.badge-downgraded { background: #fde8e8; color: #c0392b; }

.card-row { margin-top: 0.45rem; font-size: 0.9rem; }
.label { font-weight: 600; color: #6e6e73; font-size: 0.78rem; text-transform: uppercase;
         letter-spacing: 0.05em; display: block; margin-bottom: 0.1rem; }

/* ── Position Outlooks table ── */
.table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
.table th {
    text-align: left;
    font-size: 0.78rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #6e6e73;
    padding: 0.4rem 0.75rem 0.6rem;
}
.table td { padding: 0.7rem 0.75rem; vertical-align: top; border-bottom: 1px solid #d2d2d7; }
.table tr:last-child td { border-bottom: none; }
.ticker-cell { font-weight: 700; font-size: 1rem; white-space: nowrap; }

.badge-bullish { background: #d4f5e2; color: #1d7a43; }
.badge-neutral  { background: #fef9e1; color: #8a6c00; }
.badge-bearish  { background: #fde8e8; color: #c0392b; }

/* ── Long-Term Entries ── */
.lt-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 1rem;
}

.lt-card {
    background: #f5f5f7;
    border: 1px solid #d2d2d7;
    border-radius: 10px;
    padding: 1.1rem 1.2rem;
}

.lt-card h3 { font-size: 1.1rem; font-weight: 700; margin-bottom: 0.75rem; }
.lt-card .card-row { font-size: 0.9rem; margin-top: 0.4rem; }

footer {
    text-align: center;
    font-size: 0.8rem;
    color: #6e6e73;
    margin-top: 2rem;
}
"""


def _badge(cls: str, text: str) -> str:
    return f'<span class="badge badge-{cls}">{text}</span>'


def _play_card(play: dict) -> str:
    """Card for pre-market / standard top play (no options fields)."""
    play_type = play.get("play_type", "day_trade")
    badge_cls = "day" if play_type == "day_trade" else "overnight"
    badge_label = "Day Trade" if play_type == "day_trade" else "Overnight"

    return (
        '<div class="card">'
        f'<div class="card-header"><h3>{play.get("ticker", "")}</h3>'
        f'{_badge(badge_cls, badge_label)}</div>'
        f'<div class="card-row"><span class="label">Thesis</span>{play.get("thesis", "")}</div>'
        f'<div class="card-row"><span class="label">Entry Range</span>{play.get("entry_range", "")}</div>'
        f'<div class="card-row"><span class="label">Risk</span>{play.get("risk", "")}</div>'
        "</div>"
    )


def _midmorning_play_card(play: dict) -> str:
    """Card for mid-morning top play (includes options_confirmation + conviction_change)."""
    play_type = play.get("play_type", "day_trade")
    type_badge_cls = "day" if play_type == "day_trade" else "overnight"
    type_badge_label = "Day Trade" if play_type == "day_trade" else "Overnight"

    conviction = play.get("conviction_change", "unchanged").lower()
    if conviction not in ("upgraded", "downgraded", "unchanged"):
        conviction = "unchanged"
    conviction_labels = {"upgraded": "Upgraded", "downgraded": "Downgraded", "unchanged": "Unchanged"}

    options_conf = play.get("options_confirmation", "")

    html = (
        '<div class="card">'
        f'<div class="card-header">'
        f'<h3>{play.get("ticker", "")}</h3>'
        f'{_badge(type_badge_cls, type_badge_label)}'
        f'{_badge(conviction, conviction_labels[conviction])}'
        f'</div>'
        f'<div class="card-row"><span class="label">Thesis</span>{play.get("thesis", "")}</div>'
    )
    if options_conf:
        html += f'<div class="card-row"><span class="label">Options Flow</span>{options_conf}</div>'
    html += (
        f'<div class="card-row"><span class="label">Risk</span>{play.get("risk", "")}</div>'
        "</div>"
    )
    return html


def _outlook_row(pos: dict) -> str:
    outlook = pos.get("outlook", "neutral").lower()
    badge_cls = outlook if outlook in ("bullish", "bearish", "neutral") else "neutral"

    return (
        "<tr>"
        f'<td class="ticker-cell">{pos.get("ticker", "")}</td>'
        f'<td>{_badge(badge_cls, outlook.capitalize())}</td>'
        f'<td>{pos.get("summary", "")}</td>'
        f'<td><strong>{pos.get("action", "")}</strong></td>'
        "</tr>"
    )


def _lt_card(entry: dict) -> str:
    return (
        '<div class="lt-card">'
        f'<h3>{entry.get("ticker", "")}</h3>'
        f'<div class="card-row"><span class="label">Thesis</span>{entry.get("thesis", "")}</div>'
        f'<div class="card-row"><span class="label">Suggested Entry</span>{entry.get("suggested_entry", "")}</div>'
        f'<div class="card-row"><span class="label">Time Horizon</span>{entry.get("time_horizon", "")}</div>'
        "</div>"
    )


def _score_badge(score: str | None) -> str:
    if not score:
        return ""
    score_lower = score.lower()
    if score_lower not in ("high", "medium", "low"):
        return _badge("medium", score)
    labels = {"high": "High", "medium": "Medium", "low": "Low"}
    return _badge(score_lower, labels[score_lower])


def _watchlist_section(watchlist: list[dict]) -> str:
    """Render the pre-market watchlist candidate table."""
    if not watchlist:
        return ""

    rows = []
    for entry in watchlist:
        ticker = entry.get("ticker", "")
        catalyst = entry.get("catalyst") or "—"
        score_html = _score_badge(entry.get("pre_market_score"))
        status_html = _badge("pending", "Pending options confirmation")
        rows.append(
            "<tr>"
            f'<td class="ticker-cell">{ticker}</td>'
            f"<td>{catalyst}</td>"
            f"<td>{score_html}</td>"
            f"<td>{status_html}</td>"
            "</tr>"
        )

    rows_html = "\n".join(rows)
    return (
        '<section class="section">'
        "<h2>Watchlist — All Candidates</h2>"
        '<table class="table">'
        "<thead><tr>"
        "<th>Ticker</th><th>Catalyst</th><th>Pre-Market Score</th><th>Status</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
        "</section>"
    )


def _watchlist_dropped_section(dropped: list[dict]) -> str:
    """Render the mid-morning 'dropped from watchlist' table."""
    if not dropped:
        return ""

    rows = []
    for entry in dropped:
        ticker = entry.get("ticker", "")
        reason = entry.get("reason", "—")
        rows.append(
            "<tr>"
            f'<td class="ticker-cell">{ticker}</td>'
            f"<td>{reason}</td>"
            "</tr>"
        )

    rows_html = "\n".join(rows)
    return (
        '<section class="section">'
        "<h2>Watchlist Dropped</h2>"
        '<table class="table">'
        "<thead><tr><th>Ticker</th><th>Reason</th></tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
        "</section>"
    )


def write_html_report(
    report: dict,
    report_date: date | None = None,
    session_type: str = "report",
    watchlist: list[dict] | None = None,
) -> str:
    """
    Render *report* as a self-contained HTML file, write it to disk,
    open it in the browser, and return the file path.

    Args:
        report: The report dict from the agentic loop.
        report_date: Defaults to today.
        session_type: "report", "premarket", or "midmorning".
        watchlist: Watchlist rows from the DB (used when session_type="premarket").
    """
    if report_date is None:
        report_date = date.today()

    date_str = report_date.strftime("%Y-%m-%d")
    day_str = report_date.strftime("%A, %B %-d, %Y")

    is_midmorning = session_type == "midmorning"
    is_premarket = session_type == "premarket"

    # ── title / header text ───────────────────────────────────────────────
    if is_midmorning:
        title = f"Mid-Morning Assessment — {date_str}"
        h1_text = "Mid-Morning Assessment"
    else:
        title = f"Morning Report — {date_str}"
        h1_text = "Morning Trading Report"

    # ── pre-market provisional banner ────────────────────────────────────
    if is_premarket:
        banner_html = (
            '<div class="banner">'
            "<strong>Top 3 plays are provisional</strong> — options flow confirmation "
            "available after 10:30 AM. "
            "Run: <code>python main.py midmorning</code>"
            "</div>"
        )
    else:
        banner_html = ""

    # ── top plays ─────────────────────────────────────────────────────────
    plays = report.get("top_plays", [])
    if is_midmorning:
        plays_html = "".join(_midmorning_play_card(p) for p in plays)
    else:
        plays_html = "".join(_play_card(p) for p in plays)

    # ── position outlooks ─────────────────────────────────────────────────
    outlooks = report.get("position_outlooks", [])
    if outlooks:
        rows_html = "".join(_outlook_row(p) for p in outlooks)
        outlooks_html = (
            '<table class="table">'
            "<thead><tr><th>Ticker</th><th>Outlook</th><th>Summary</th><th>Action</th></tr></thead>"
            f"<tbody>{rows_html}</tbody>"
            "</table>"
        )
    else:
        outlooks_html = "<p>No current positions on file.</p>"

    # ── long-term entries ─────────────────────────────────────────────────
    lt_entries = report.get("long_term_entries", [])
    lt_html = "".join(_lt_card(e) for e in lt_entries)

    # ── pre-market watchlist section ──────────────────────────────────────
    if is_premarket and watchlist:
        watchlist_section_html = _watchlist_section(watchlist)
    else:
        watchlist_section_html = ""

    # ── mid-morning dropped section ───────────────────────────────────────
    if is_midmorning:
        dropped_section_html = _watchlist_dropped_section(report.get("watchlist_dropped", []))
    else:
        dropped_section_html = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">

  <header>
    <h1>{h1_text}</h1>
    <p class="subtitle">{day_str}</p>
  </header>

  {banner_html}

  <section class="section">
    <h2>Top Plays</h2>
    <div class="plays-grid">
      {plays_html}
    </div>
  </section>

  {watchlist_section_html}

  {dropped_section_html}

  <section class="section">
    <h2>Position Outlooks</h2>
    {outlooks_html}
  </section>

  <section class="section">
    <h2>Long-Term Entries</h2>
    <div class="lt-grid">
      {lt_html}
    </div>
  </section>

  <footer>Generated by trading-assistant &middot; {date_str}</footer>

</div>
</body>
</html>"""

    os.makedirs("./reports", exist_ok=True)
    suffix = "-midmorning" if is_midmorning else ""
    out_path = f"./reports/{date_str}{suffix}.html"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    webbrowser.open(f"file://{os.path.abspath(out_path)}")
    return out_path
