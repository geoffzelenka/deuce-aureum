"""
Unit tests for store/db.py using a per-test temporary SQLite database.
"""

import os
import tempfile
from datetime import UTC, date, datetime, timedelta

import pytest

import store.db as db


# ---------------------------------------------------------------------------
# Fixture: fresh db file for every test
# ---------------------------------------------------------------------------

@pytest.fixture()
def dbpath(tmp_path):
    """Return a path to a fresh, initialised SQLite database."""
    path = str(tmp_path / "test_trading.db")
    db.init_db(path)
    return path


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_headlines_table(self, dbpath):
        with db._connect(dbpath) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='headlines'"
            ).fetchone()
        assert row is not None

    def test_creates_positions_table(self, dbpath):
        with db._connect(dbpath) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
            ).fetchone()
        assert row is not None

    def test_idempotent(self, dbpath):
        db.init_db(dbpath)  # second call must not raise


# ---------------------------------------------------------------------------
# insert_headlines / get_recent_headlines
# ---------------------------------------------------------------------------

class TestHeadlines:
    def test_insert_and_retrieve(self, dbpath):
        today = date(2026, 4, 10)
        n = db.insert_headlines(["Headline A", "Headline B"], today, db_path=dbpath)
        assert n == 2
        results = db.get_recent_headlines(days=1, db_path=dbpath)
        assert "Headline A" in results
        assert "Headline B" in results

    def test_deduplication(self, dbpath):
        today = date(2026, 4, 10)
        db.insert_headlines(["Dup headline"], today, db_path=dbpath)
        n2 = db.insert_headlines(["Dup headline"], today, db_path=dbpath)
        assert n2 == 0
        assert db.get_recent_headlines(days=1, db_path=dbpath).count("Dup headline") == 1

    def test_get_recent_excludes_old(self, dbpath):
        today = date(2026, 4, 10)
        db.insert_headlines(["Recent headline"], today, db_path=dbpath)

        # Back-date the inserted_at timestamp so it falls outside the window.
        old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        with db._connect(dbpath) as conn:
            conn.execute("UPDATE headlines SET inserted_at = ?", (old_ts,))

        results = db.get_recent_headlines(days=7, db_path=dbpath)
        assert results == []

    def test_empty_input(self, dbpath):
        n = db.insert_headlines([], date.today(), db_path=dbpath)
        assert n == 0
        assert db.get_recent_headlines(db_path=dbpath) == []


# ---------------------------------------------------------------------------
# upsert_positions / get_positions
# ---------------------------------------------------------------------------

class TestPositions:
    def _sample_row(self, ticker="AAPL"):
        return {"ticker": ticker, "name": "Apple Inc.", "shares": 10.0, "avg_cost": 150.0, "notes": ""}

    def test_insert_and_retrieve(self, dbpath):
        db.upsert_positions([self._sample_row()], db_path=dbpath)
        positions = db.get_positions(db_path=dbpath)
        assert len(positions) == 1
        assert positions[0]["ticker"] == "AAPL"
        assert positions[0]["shares"] == 10.0

    def test_upsert_updates_existing(self, dbpath):
        db.upsert_positions([self._sample_row()], db_path=dbpath)
        updated = {"ticker": "AAPL", "name": "Apple Inc.", "shares": 20.0, "avg_cost": 160.0, "notes": "updated"}
        db.upsert_positions([updated], db_path=dbpath)
        positions = db.get_positions(db_path=dbpath)
        assert len(positions) == 1
        assert positions[0]["shares"] == 20.0
        assert positions[0]["avg_cost"] == 160.0
        assert positions[0]["notes"] == "updated"

    def test_multiple_tickers(self, dbpath):
        rows = [self._sample_row("AAPL"), self._sample_row("MSFT")]
        db.upsert_positions(rows, db_path=dbpath)
        positions = db.get_positions(db_path=dbpath)
        tickers = [p["ticker"] for p in positions]
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_empty_input(self, dbpath):
        db.upsert_positions([], db_path=dbpath)
        assert db.get_positions(db_path=dbpath) == []


# ---------------------------------------------------------------------------
# parse_headlines_file
# ---------------------------------------------------------------------------

class TestParseHeadlinesFile:
    def _write(self, tmp_path, content):
        p = tmp_path / "headlines.txt"
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_basic_parsing(self, tmp_path):
        path = self._write(tmp_path, "Fed raises rates\nMarkets tumble\n")
        assert db.parse_headlines_file(path) == ["Fed raises rates", "Markets tumble"]

    def test_skips_blank_lines(self, tmp_path):
        path = self._write(tmp_path, "\nFed raises rates\n\nMarkets tumble\n\n")
        assert db.parse_headlines_file(path) == ["Fed raises rates", "Markets tumble"]

    def test_skips_comment_lines(self, tmp_path):
        path = self._write(tmp_path, "# This is a comment\nFed raises rates\n# Another\n")
        assert db.parse_headlines_file(path) == ["Fed raises rates"]

    def test_empty_file(self, tmp_path):
        path = self._write(tmp_path, "")
        assert db.parse_headlines_file(path) == []

    def test_strips_bullet_dash(self, tmp_path):
        path = self._write(tmp_path, "- Fed raises rates\n- Markets tumble\n")
        assert db.parse_headlines_file(path) == ["Fed raises rates", "Markets tumble"]

    def test_strips_bullet_asterisk(self, tmp_path):
        path = self._write(tmp_path, "* Fed raises rates\n")
        assert db.parse_headlines_file(path) == ["Fed raises rates"]

    def test_skips_markdown_bold_headers(self, tmp_path):
        content = "**Company News**\nFed raises rates\n**Analyst Actions**\nMarkets tumble\n"
        path = self._write(tmp_path, content)
        assert db.parse_headlines_file(path) == ["Fed raises rates", "Markets tumble"]

    def test_skips_markdown_italic_headers(self, tmp_path):
        path = self._write(tmp_path, "*Notables*\nFed raises rates\n")
        assert db.parse_headlines_file(path) == ["Fed raises rates"]

    def test_keeps_inline_bold_in_headline(self, tmp_path):
        # A headline that contains bold but is not purely a header should be kept
        path = self._write(tmp_path, "$AAPL reports **record** earnings\n")
        assert db.parse_headlines_file(path) == ["$AAPL reports **record** earnings"]

    def test_real_world_format(self, tmp_path):
        content = (
            "**Headlines**\n"
            "- $NVDA chips power new AI push\n"
            "- Fed holds rates steady\n"
            "**Company News**\n"
            "**Technology**\n"
            "$MSFT expands cloud capacity\n"
        )
        path = self._write(tmp_path, content)
        assert db.parse_headlines_file(path) == [
            "$NVDA chips power new AI push",
            "Fed holds rates steady",
            "$MSFT expands cloud capacity",
        ]


# ---------------------------------------------------------------------------
# parse_positions_file
# ---------------------------------------------------------------------------

class TestParsePositionsFile:
    def _write_csv(self, tmp_path, rows, header="ticker,name,shares,avg_cost,notes"):
        p = tmp_path / "positions.csv"
        lines = [header] + [",".join(str(v) for v in row) for row in rows]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def test_basic_parsing(self, tmp_path):
        path = self._write_csv(tmp_path, [("AAPL", "Apple Inc.", "10", "150.0", "long-term")])
        result = db.parse_positions_file(path)
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["shares"] == 10.0
        assert result[0]["avg_cost"] == 150.0
        assert result[0]["notes"] == "long-term"

    def test_multiple_rows(self, tmp_path):
        path = self._write_csv(tmp_path, [
            ("AAPL", "Apple Inc.", "10", "150.0", ""),
            ("MSFT", "Microsoft", "5", "300.0", ""),
        ])
        result = db.parse_positions_file(path)
        assert len(result) == 2
        tickers = [r["ticker"] for r in result]
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_empty_optional_fields(self, tmp_path):
        path = self._write_csv(tmp_path, [("TSLA", "", "", "", "")])
        result = db.parse_positions_file(path)
        assert result[0]["shares"] is None
        assert result[0]["avg_cost"] is None

    def test_header_only(self, tmp_path):
        path = self._write_csv(tmp_path, [])
        assert db.parse_positions_file(path) == []
