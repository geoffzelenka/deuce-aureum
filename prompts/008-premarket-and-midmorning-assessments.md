In `trading-assistant`, split the report workflow into two distinct 
sessions. The existing `kickoff` command becomes the pre-market session. 
Add a new `midmorning` command for the 10:30 AM assessment.

---

### DB schema additions (store/db.py)

Add a `watchlist` table:
  id INTEGER PRIMARY KEY,
  session_date DATE NOT NULL,
  ticker TEXT NOT NULL,
  rank INTEGER,              -- 1-10, pre-market conviction order
  catalyst TEXT,             -- one-sentence reason from Phase 1 scan
  pre_market_score TEXT,     -- 'high'|'medium'|'low' pre-market conviction
  confirmed BOOLEAN DEFAULT 0,  -- set to 1 after mid-morning confirms
  confirmed_rank INTEGER,    -- 1-3 after mid-morning, NULL until then
  options_unusual BOOLEAN,   -- populated during mid-morning session
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

Unique constraint on (session_date, ticker) — one entry per ticker per day.

Add functions:
  save_watchlist(candidates: list[dict], session_date: date)
  get_watchlist(session_date: date) -> list[dict]
  update_watchlist_confirmation(ticker: str, session_date: date, 
                                confirmed_rank: int, 
                                options_unusual: bool)
  watchlist_exists(session_date: date) -> bool

---

### Pre-market session changes (report/generator.py)

In generate_report(), make these changes:

1. Pass a session_type: str = "premarket" parameter.

2. When session_type == "premarket", remove get_options_flow from the 
   tools list entirely. Do not pass it to the API call. This ensures 
   Claude cannot attempt to call it even if prompted to.

3. After Phase 1 (scan), save the candidates to the watchlist table 
   immediately via save_watchlist(), before Phase 2 research begins. 
   Include the catalyst text Claude identified for each candidate, 
   parsed from the candidates JSON block.

4. The candidates JSON block Claude emits in Phase 1 should now match 
   this schema:
   {
     "candidates": [
       {
         "ticker": "AAPL",
         "catalyst": "One sentence describing the specific news driver",
         "pre_market_score": "high"|"medium"|"low"
       }
     ]
   }

5. After the full report is generated, update the watchlist rows with 
   the provisional top 3 ranks (confirmed=0, confirmed_rank=NULL).

6. In the pre-market HTML report, add a "Watchlist" section below the 
   top 3 plays showing all 10 candidates with their catalyst and 
   pre_market_score. Each row should show a "Pending options 
   confirmation" badge in amber.

7. Add a prominent banner at the top of the pre-market HTML report:
   "Top 3 plays are provisional — options flow confirmation available 
   after 10:30 AM. Run: python main.py midmorning"

---

### Mid-morning session (new file: report/midmorning.py)

Implement run_midmorning_assessment(etrade_session) with this flow:

1. Check watchlist_exists(today) — if False, print error:
   "No pre-market watchlist found for today. Run kickoff first." 
   and exit.

2. Load the watchlist via get_watchlist(today). These are the candidates 
   — do not re-run Phase 1. Skip building an allow-list from headlines; 
   use the persisted watchlist tickers as the allow-list directly.

3. Build a mid-morning context message for Claude that includes:
   - The original headlines (reload from DB via get_recent_headlines)
   - The original positions
   - The pre-market provisional top 3 and their catalysts from the 
     watchlist
   - Current time and a note that the market has been open ~60 minutes
   - Explicit instruction: "Re-evaluate the watchlist using fresh quotes, 
     technicals, and options flow. Confirm, re-rank, or replace the 
     provisional top 3."

4. Run Phase 2 research loop with all three tools available 
   (get_quote, get_technicals, get_options_flow). Use the same 
   TickerBudget and global cap logic (4 calls per ticker, 40 total).

5. The final JSON schema for the mid-morning report adds one field 
   per top play:
   {
     "top_plays": [
       {
         "ticker": str,
         "play_type": "day_trade"|"overnight",
         "thesis": str,
         "risk": str,
         "options_confirmation": str,  -- NEW: what options flow showed
         "conviction_change": "upgraded"|"unchanged"|"downgraded"
            -- vs pre-market provisional rank
       }
     ],
     "watchlist_dropped": [
       {
         "ticker": str,
         "reason": str  -- why it didn't make the confirmed top 3
       }
     ],
     ... (position_outlooks and long_term_entries unchanged)
   }

6. After generating the report, call update_watchlist_confirmation() 
   for each of the confirmed top 3. Set confirmed=1, confirmed_rank, 
   and options_unusual from the options flow results.

7. Update the watcher (monitor/watcher.py) to prefer the confirmed 
   top 3 tickers if a mid-morning report exists for today. On startup, 
   watcher should check: if confirmed top 3 exists in DB for today, 
   use those. Otherwise fall back to pre-market provisional top 3. 
   Log which set is being used: "Monitoring confirmed top 3" or 
   "Monitoring provisional top 3 (mid-morning not yet run)."

---

### System prompt additions (SYSTEM_PROMPT)

Add a SESSION CONTEXT section at the top of the system prompt, 
populated dynamically based on session_type:

For premarket:
"""
SESSION CONTEXT: Pre-market analysis (~45 minutes before open).
Options flow data is NOT available at this time — do not attempt to 
call get_options_flow. Your top 3 picks are provisional and will be 
confirmed at 10:30 AM once options flow is available.
When identifying candidates in Phase 1, note the specific catalyst 
and assign a pre_market_score of high/medium/low based on how 
unambiguous and unpriced the catalyst appears to be.
"""

For midmorning:
"""
SESSION CONTEXT: Mid-morning assessment (~10:30 AM, market open ~60 min).
You have access to get_options_flow. Options volume is now meaningful — 
use it for any candidate with a binary catalyst (earnings, FDA, merger, 
macro event). The watchlist below was built pre-market. Your job is to 
validate, re-rank, and confirm the top 3 using live data.
For each candidate, check: has the price moved significantly since 
pre-market? Does options flow confirm the directional thesis or 
contradict it? Upgrade conviction if options flow is unusual and 
aligned. Downgrade or drop if the catalyst is already fully priced in 
or options flow is contradictory.
"""

---

### main.py additions

Add midmorning subcommand:
  python main.py midmorning
  - Calls get_session() to confirm auth
  - Calls run_midmorning_assessment(etrade_session)
  - Generates and opens the mid-morning HTML report
  - Prints: "Mid-morning assessment complete. Watcher updated to 
    confirmed top 3."

Add a --session-summary flag to the monitor subcommand that prints 
which set of tickers (confirmed or provisional) is currently being 
watched and why.

---

### Tests (tests/test_midmorning.py)

- Test that midmorning exits cleanly if no watchlist exists for today
- Test that get_options_flow is excluded from tools in premarket session
- Test that get_options_flow is included in tools in midmorning session
- Test that watcher selects confirmed tickers when they exist in DB
- Test that watcher falls back to provisional when confirmed is absent
- Test conviction_change is correctly set to "upgraded"/"downgraded" 
  by comparing confirmed_rank to pre-market provisional rank
