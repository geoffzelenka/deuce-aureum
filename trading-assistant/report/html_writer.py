"""
Write the morning report dict to a styled, self-contained HTML file
and open it in the default browser.
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
    .label { color: #8e8e93; }
    h1 { color: #ffffff; }
    h2 { color: #e8e8ed; border-bottom-color: #3a3a3c; }
    .table td { border-bottom-color: #2c2c2e; }
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


def write_html_report(report: dict, report_date: date | None = None) -> str:
    """
    Render *report* as a self-contained HTML file, write it to
    ``./reports/YYYY-MM-DD.html``, open it in the browser, and return
    the file path.
    """
    if report_date is None:
        report_date = date.today()

    date_str = report_date.strftime("%Y-%m-%d")
    day_str = report_date.strftime("%A, %B %-d, %Y")

    # ── sections ──────────────────────────────────────────────────────────
    plays = report.get("top_plays", [])
    plays_html = "".join(_play_card(p) for p in plays)

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

    lt_entries = report.get("long_term_entries", [])
    lt_html = "".join(_lt_card(e) for e in lt_entries)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Morning Report — {date_str}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">

  <header>
    <h1>Morning Trading Report</h1>
    <p class="subtitle">{day_str}</p>
  </header>

  <section class="section">
    <h2>Top Plays</h2>
    <div class="plays-grid">
      {plays_html}
    </div>
  </section>

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
    out_path = f"./reports/{date_str}.html"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    webbrowser.open(f"file://{os.path.abspath(out_path)}")
    return out_path
