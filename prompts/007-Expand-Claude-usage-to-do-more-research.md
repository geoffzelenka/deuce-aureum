In `report/generator.py` and the SYSTEM_PROMPT, implement a two-phase 
agentic research loop: a broad scan phase (up to 10 candidate tickers) 
followed by a deep research phase (up to 4 tool calls per ticker) before 
producing the final ranked top 3.

---

### New tool: get_options_flow

Add a third tool definition alongside get_quote and get_technicals:

GET_OPTIONS_FLOW_TOOL = {
  "name": "get_options_flow",
  "description": (
    "Fetch options flow summary for a ticker: total call volume, total put "
    "volume, put/call ratio, largest single options trade of the day "
    "(strike, expiry, premium), and unusual activity flag (boolean). "
    "Use this to gauge whether smart money is positioning directionally "
    "ahead of a catalyst. Most useful when a headline suggests an "
    "imminent binary event (earnings, FDA, merger vote)."
  ),
  "input_schema": {
    "type": "object",
    "properties": {
      "ticker": {"type": "string", "description": "Uppercase ticker, e.g. NVDA"}
    },
    "required": ["ticker"]
  }
}

Implement get_options_flow_data(ticker, etrade_session) in 
report/enricher.py. Call the E*TRADE Options Chain endpoint 
(GET /v1/market/optionchains) for the nearest two expiries. 
Compute: total call OI + volume, total put OI + volume, put/call ratio, 
identify the single largest premium trade by volume × last price. 
Set unusual_activity=True if put/call ratio is outside 0.4–1.8 or if 
the largest trade premium exceeds $500k. Apply the same 5-second timeout 
and allow-list validation used by the other tools.

---

### Guardrail overhaul in generate_report()

Replace the existing flat turn counter with a two-phase budget system:

PHASE 1 — SCAN (no tool calls)
Claude reads all headlines and positions and internally identifies up to 
10 candidate tickers. This happens in the first completion call with 
tools available but Claude instructed not to call them yet (enforced via 
the system prompt — see below). Claude must emit a JSON block in its 
thinking with the key "candidates": [list of up to 10 tickers] before 
any tool calls begin. Parse this from the first assistant response.

PHASE 2 — RESEARCH (tool calls)
For each candidate ticker, enforce this budget:
- Calls 1 and 2 are mandatory: get_quote then get_technicals (in that 
  order). Reject out-of-order calls with a tool_result error: 
  "You must call get_quote before get_technicals for {ticker}."
- Calls 3 and 4 are free choice from {get_quote, get_technicals, 
  get_options_flow}. get_quote and get_technicals may be called again 
  for the same ticker only in slots 3 or 4 (e.g. to re-check price 
  after seeing options flow). Duplicate calls in slots 1-2 are still 
  blocked.
- Hard cap: 4 calls per ticker, 40 calls total (10 × 4). Track both 
  counters independently.

Implement this as a TickerBudget dataclass:

```python
@dataclass
class TickerBudget:
    ticker: str
    calls_made: int = 0
    quote_done: bool = False
    technicals_done: bool = False
    
    def can_call(self, tool_name: str) -> tuple[bool, str]:
        if self.calls_made >= 4:
            return False, f"Budget exhausted for {self.ticker} (4/4 calls used)."
        if tool_name == "get_quote" and not self.quote_done:
            return True, ""
        if tool_name == "get_technicals" and not self.quote_done:
            return False, f"Call get_quote for {self.ticker} before get_technicals."
        if tool_name == "get_technicals" and not self.technicals_done:
            return True, ""
        if self.calls_made < 2:
            return False, f"Complete mandatory calls (get_quote, get_technicals) for {self.ticker} first."
        return True, ""
    
    def record_call(self, tool_name: str):
        self.calls_made += 1
        if tool_name == "get_quote" and not self.quote_done:
            self.quote_done = True
        elif tool_name == "get_technicals":
            self.technicals_done = True
```

Store budgets in a dict: ticker_budgets: dict[str, TickerBudget] 
initialized from the candidates list after Phase 1.

Global hard stop: if total tool calls across all tickers reaches 40, 
inject a tool_result: "Global research budget exhausted (40/40 calls). 
Proceed to final ranking now." and make one final completion call with 
tools=[].

---

### Loop structure

```python
def generate_report(headlines, positions, etrade_session):
    allowed_tickers = build_allow_list(headlines, positions)
    total_calls = 0
    GLOBAL_MAX = 40
    PER_TICKER_MAX = 4

    # --- Phase 1: scan, no tool calls ---
    messages = [build_initial_user_message(headlines, positions)]
    scan_response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[GET_QUOTE_TOOL, GET_TECHNICALS_TOOL, GET_OPTIONS_FLOW_TOOL],
        tool_choice={"type": "none"},  # Disables tool calls for phase 1
        messages=messages
    )
    candidates = parse_candidates(scan_response)  # extract up to 10 tickers
    candidates = [t for t in candidates if t in allowed_tickers]
    ticker_budgets = {t: TickerBudget(ticker=t) for t in candidates}
    messages.append({"role": "assistant", "content": scan_response.content})

    # Instruct Claude to begin research phase
    messages.append({"role": "user", "content": (
        f"Candidates confirmed: {candidates}. "
        "Begin research phase. Call get_quote then get_technicals for each "
        "candidate, then use your remaining 2 calls per ticker as you see fit. "
        "When all research is complete, produce the final JSON report."
    )})

    # --- Phase 2: research loop ---
    while True:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=[GET_QUOTE_TOOL, GET_TECHNICALS_TOOL, GET_OPTIONS_FLOW_TOOL],
            messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return parse_final_report(response)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                ticker = block.input.get("ticker", "").upper()
                tool_name = block.name

                # Global cap check
                if total_calls >= GLOBAL_MAX:
                    result = ("Global research budget exhausted (40/40 calls). "
                              "Produce the final JSON report now.")
                else:
                    # Allow-list check
                    if ticker not in allowed_tickers:
                        result = (f"Ticker {ticker} is not on the approved list. "
                                  "Do not request it.")
                    else:
                        # Per-ticker budget check
                        if ticker not in ticker_budgets:
                            ticker_budgets[ticker] = TickerBudget(ticker=ticker)
                        budget = ticker_budgets[ticker]
                        allowed, reason = budget.can_call(tool_name)
                        if not allowed:
                            result = reason
                        else:
                            result = dispatch_tool_call(
                                tool_name, ticker, etrade_session
                            )
                            budget.record_call(tool_name)
                            total_calls += 1

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

            messages.append({"role": "user", "content": tool_results})

        # Force final if global cap hit mid-loop
        if total_calls >= GLOBAL_MAX:
            messages.append({"role": "user", "content": [{
                "type": "text",
                "text": "Research budget exhausted. Produce the final JSON report now."
            }]})
            final = anthropic_client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=[],
                messages=messages
            )
            return parse_final_report(final)
```

---

### System prompt — Tool use guidance section replacement

Replace the TOOL USE GUIDANCE section in SYSTEM_PROMPT with:

"""
TOOL USE GUIDANCE

You work in two phases:

PHASE 1 — SCAN (current phase when you first receive the prompt)
Read all headlines and positions. Identify up to 10 candidate tickers 
that have a clear news catalyst worthy of investigation. Output your 
candidates list in your response as a JSON block with key "candidates". 
Do not call any tools in this phase. Be selective — only include tickers 
where a catalyst is evident in the headlines or where a held position 
warrants live data to assess.

PHASE 2 — RESEARCH (begins after candidates are confirmed)
For each candidate ticker you must follow this call order:
  1. get_quote — always first. Establishes whether the catalyst is 
     already priced in (stock already up/down significantly).
  2. get_technicals — always second. Confirms trend direction and 
     whether the setup has technical support.
  3 & 4. Your choice from get_quote, get_technicals, or get_options_flow. 
     Use get_options_flow for binary catalyst events (earnings, FDA, 
     merger votes, Fed decisions) to gauge smart money positioning. 
     Use a second get_quote if significant time has passed. Use a second 
     get_technicals only if the first result was ambiguous.

Budget rules:
- Maximum 4 tool calls per ticker.
- Maximum 40 tool calls total across all tickers.
- Do not call tools for tickers not in your confirmed candidates list.
- When your research on all candidates is complete, stop calling tools 
  and produce the final JSON report immediately.

RANKING GUIDANCE
After researching all candidates, rank your top 3 plays using this 
priority order:
  1. Strongest unpriced catalyst (stock has not yet moved on the news)
  2. Options flow confirmation (unusual activity signals smart money 
     positioning ahead of you)
  3. Technical setup aligned with catalyst direction (trend, support, 
     moving averages confirm the trade)
  4. Clean risk narrative (one specific, avoidable risk — not generic)

A play with all four factors is a high-conviction pick. A play missing 
factor 1 (catalyst already priced in) should be dropped regardless of 
how good the technicals look.
"""

---

### Logging additions

Extend the tool call log to include phase (scan/research), ticker budget 
status (e.g. "2/4 calls used"), and options flow unusual_activity flag 
when applicable. After report generation print:
"Research complete: {N} candidates scanned, {M} total tool calls used, 
top 3 selected."

---

### Tests

Add to tests/test_generator.py:
- Test that Phase 1 uses tool_choice: "none" 
- Test that get_technicals is rejected if get_quote hasn't been called 
  first for that ticker
- Test that a ticker at 4/4 calls is blocked from a 5th call
- Test that the global 40-call cap triggers the forced final completion
- Test that candidates not in the allow-list are filtered out after 
  Phase 1
- Test get_options_flow returns unusual_activity=True when put/call 
  ratio is outside 0.4–1.8
