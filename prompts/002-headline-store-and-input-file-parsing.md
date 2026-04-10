In the `trading-assistant` project, implement the headline store and input file parsing.

**store/db.py**
- Use SQLite (standard library `sqlite3`). Database file path configurable via .env as `DB_PATH` (default: `./data/trading.db`).
- Create a `headlines` table: id (PK), text (unique), source_date (date), inserted_at (timestamp).
- Create a `positions` table: ticker (PK), name, shares, avg_cost, notes.
- Provide functions: `insert_headlines(lines: list[str], date: date)`, `get_recent_headlines(days=7) -> list[str]`, `upsert_positions(rows: list[dict])`, `get_positions() -> list[dict]`.
- Deduplicate headlines on insert by the `text` column (use INSERT OR IGNORE).

**Input file formats**
- Headlines file: one headline per line, blank lines and lines starting with `#` are ignored.
- Positions file: CSV with columns `ticker, name, shares, avg_cost, notes`. First row is a header.

**main.py additions**
- Add a `kickoff` subcommand that accepts `--headlines <path>` and `--positions <path>`.
- It should: (1) call `get_session()` to confirm auth is valid, (2) parse and store the headlines file, (3) parse and store the positions file, (4) print a summary: "Loaded N headlines, M positions."

Write unit tests in `tests/test_store.py` using pytest and an in-memory SQLite database.
