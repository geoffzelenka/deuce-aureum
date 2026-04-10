In the `trading-assistant` project, implement the morning report generator.

**report/generator.py**
- Call the Anthropic Claude API (use the `anthropic` Python SDK, model `claude-opus-4-5`).
- The system prompt should establish Claude as a seasoned stock market analyst focused on actionable, concise guidance.
- Build a user prompt that includes:
  1. Today's date and day of week.
  2. The last 7 days of stored headlines (from `store/db.get_recent_headlines(days=7)`).
  3. The full current positions list (from `store/db.get_positions()`).
- Ask Claude to return a JSON object with this exact schema:
  {
    "top_plays": [
      { "ticker": str, "play_type": "day_trade"|"overnight", "thesis": str, "entry_range": str, "risk": str }
    ],  // exactly 3 entries
    "position_outlooks": [
      { "ticker": str, "outlook": "bullish"|"neutral"|"bearish", "summary": str, "action": str }
    ],
    "long_term_entries": [
      { "ticker": str, "thesis": str, "suggested_entry": str, "time_horizon": str }
    ]
  }
- Parse the JSON response strictly; if parsing fails, retry once with a corrective prompt.

**report/html_writer.py**
- Accept the parsed report dict and write a styled, self-contained HTML file to `./reports/YYYY-MM-DD.html`.
- The HTML should have three clearly labeled sections matching the three JSON keys above.
- Top plays section should display each play as a card showing ticker, play type badge, thesis, entry range, and risk level.
- Use clean inline CSS — no external dependencies. Dark-mode friendly using `prefers-color-scheme`.
- After writing the file, automatically open it in the default browser using `webbrowser.open()`.

**main.py additions**
- Add a `report` subcommand (also called automatically at the end of `kickoff`).
- Print a progress indicator while Claude is generating the report.
- Save the raw JSON output alongside the HTML as `./reports/YYYY-MM-DD.json` for later reuse.
