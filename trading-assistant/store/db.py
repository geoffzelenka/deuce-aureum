"""
SQLite headline and position storage.
"""

import csv
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "./data/trading.db")


def init_db(db_path: str = DB_PATH) -> None:
    """Create tables if they do not already exist."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS headlines (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                text        TEXT    NOT NULL UNIQUE,
                source_date TEXT    NOT NULL,
                inserted_at TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                ticker   TEXT PRIMARY KEY,
                name     TEXT,
                shares   REAL,
                avg_cost REAL,
                notes    TEXT
            )
            """
        )


def insert_headlines(lines: list[str], source_date: date, db_path: str = DB_PATH) -> int:
    """Insert headlines, skipping duplicates. Returns count of newly inserted rows."""
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    date_str = source_date.isoformat()
    inserted = 0
    with _connect(db_path) as conn:
        for text in lines:
            cur = conn.execute(
                "INSERT OR IGNORE INTO headlines (text, source_date, inserted_at) VALUES (?, ?, ?)",
                (text, date_str, now),
            )
            inserted += cur.rowcount
    return inserted


def get_recent_headlines(days: int = 7, db_path: str = DB_PATH) -> list[str]:
    """Return headline texts inserted within the last `days` days."""
    init_db(db_path)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT text FROM headlines WHERE inserted_at >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
    return [r["text"] for r in rows]


def upsert_positions(rows: list[dict], db_path: str = DB_PATH) -> None:
    """Insert or replace positions keyed by ticker."""
    init_db(db_path)
    with _connect(db_path) as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO positions (ticker, name, shares, avg_cost, notes)
                VALUES (:ticker, :name, :shares, :avg_cost, :notes)
                ON CONFLICT(ticker) DO UPDATE SET
                    name     = excluded.name,
                    shares   = excluded.shares,
                    avg_cost = excluded.avg_cost,
                    notes    = excluded.notes
                """,
                row,
            )


def get_positions(db_path: str = DB_PATH) -> list[dict]:
    """Return all positions as a list of dicts."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ticker, name, shares, avg_cost, notes FROM positions ORDER BY ticker"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Input file parsers
# ---------------------------------------------------------------------------

def parse_headlines_file(path: str) -> list[str]:
    """Read a headlines file: one per line.

    Skips:
    - Blank lines
    - Lines starting with '#' (comments)
    - Markdown section headers: lines whose text is entirely bold/italic
      formatting, e.g. '**Company News**' or '**Defense / Energy**'

    Cleans:
    - Strips leading '- ' or '* ' bullet markers
    """
    import re
    _MARKDOWN_HEADER = re.compile(r'^\*{1,2}[^*]+\*{1,2}$')

    lines = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if _MARKDOWN_HEADER.match(stripped):
                continue
            # Strip leading bullet marker
            if stripped.startswith("- ") or stripped.startswith("* "):
                stripped = stripped[2:].strip()
            if stripped:
                lines.append(stripped)
    return lines


def parse_positions_file(path: str) -> list[dict]:
    """Read a positions CSV: header row ticker,name,shares,avg_cost,notes.

    The header's first field may be prefixed with '#' (treated as a comment
    marker on the header line, not a skip directive).  Whitespace around field
    names and values is stripped.
    """
    rows = []
    with open(path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh, skipinitialspace=True)
        # Strip a leading '#' from the first fieldname if present.
        if reader.fieldnames and reader.fieldnames[0].lstrip("#").strip() == "ticker":
            reader.fieldnames[0] = "ticker"
        for row in reader:
            rows.append({
                "ticker": row["ticker"].strip(),
                "name": (row.get("name") or "").strip(),
                "shares": float(row["shares"]) if (row.get("shares") or "").strip() else None,
                "avg_cost": float(row["avg_cost"]) if (row.get("avg_cost") or "").strip() else None,
                "notes": (row.get("notes") or "").strip(),
            })
    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
