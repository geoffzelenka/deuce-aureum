In `trading-assistant`, refactor `report/generator.py` to use the Anthropic 
SDK's native tool use (function calling) so Claude can request live market 
data mid-analysis. This replaces the single-shot prompt with a controlled 
agentic loop.

---

### Tool definitions

Define two tools that Claude may call:

**get_quote**
- Description: "Fetch the current quote for a single stock ticker from 
  E*TRADE. Use this when you need the current price, volume, bid/ask, 
  day high/low, or previous close for a specific ticker to support your 
  analysis. Do not request a ticker unless it is directly relevant to the 
  headlines or positions provided."
- Input schema: { "ticker": { "type": "string", "description": "Uppercase 
  stock ticker symbol, e.g. AAPL" } }

**get_technicals**
- Description: "Fetch a summary of key technical indicators for a ticker: 
  50-day and 200-day moving averages, RSI-14, and average daily volume 
  over 30 days. Use this only when the headline or position context 
  specifically warrants technical analysis."
- Input schema: { "ticker": { "type": "string" } }

---

### Guardrails (enforce all of these strictly)

1. **Turn limit**: Allow a maximum of 3 tool-call turns total per report 
   generation. Track turns with a counter. If Claude attempts a 4th tool 
   call, stop the loop, inject a tool_result with content: 
   "Tool call limit reached. Proceed to generate the report now with the 
   data you have." and force one final completion call.

2. **Ticker allow-list**: Before executing any tool call, validate that the 
   requested ticker appears in either: (a) the positions list loaded from 
   the DB, or (b) a set of tickers extracted from the headlines text via a 
   simple regex ([A-Z]{1,5}) filtered to known exchange-listed format. 
   If the ticker is not on the allow-list, return a tool_result with: 
   "Ticker {X} is not in the approved list for this session. Do not request 
   it again."

3. **Duplicate request block**: Track which (tool_name, ticker) pairs have 
   already been called this session. If Claude requests the same pair twice, 
   return: "You already have this data. Do not repeat tool calls." 
   This counts toward the turn limit.

4. **Timeout**: Wrap each E*TRADE API call in a 5-second timeout 
   (use `requests` timeout parameter). On timeout, return a tool_result 
   with: "E*TRADE quote timed out for {ticker}. Proceed without this data."

5. **No nested loops**: The agentic loop must be a flat while loop in 
   Python, not recursive. Max iterations enforced by the turn counter.

---

### Loop implementation

```python
def generate_report(headlines, positions, etrade_session):
    MAX_TURNS = 3
    turns = 0
    seen_calls = set()
    allowed_tickers = build_allow_list(headlines, positions)
    
    messages = [build_initial_user_message(headlines, positions)]
    
    while turns <= MAX_TURNS:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=[GET_QUOTE_TOOL, GET_TECHNICALS_TOOL],
            messages=messages
        )
        
        # Append assistant response to messages
        messages.append({"role": "assistant", "content": response.content})
        
        if response.stop_reason == "end_turn":
            # Extract final JSON from response
            return parse_final_report(response)
        
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = dispatch_tool_call(
                    block, allowed_tickers, seen_calls, 
                    etrade_session, turns, MAX_TURNS
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })
                turns += 1
            
            messages.append({"role": "user", "content": tool_results})
        
        if turns >= MAX_TURNS:
            # Force final completion with no tools available
            messages.append({"role": "user", "content": [{
                "type": "tool_result", 
                "tool_use_id": "forced",
                "content": "Tool call limit reached. Generate the final report now."
            }]})
            final = anthropic_client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=[],  # No tools — forces end_turn
                messages=messages
            )
            return parse_final_report(final)
    
    raise ReportGenerationError("Loop exited without producing a report.")
```

---

### Logging

- Log every tool call attempt to `./logs/tool_calls.log` with: timestamp, 
  ticker, tool name, turn number, allowed (yes/no), result summary, 
  and E*TRADE response time in ms.
- After report generation, print a summary line: 
  "Report generated in N tool call turns."

---

### System prompt addition

Add this paragraph to the existing system prompt:

"You have access to two tools: get_quote and get_technicals. Use them 
sparingly — only when live data would materially change your analysis. 
You are limited to 3 tool calls total. Do not request data for tickers 
not mentioned in the headlines or positions. Do not call the same tool 
for the same ticker more than once. When you have enough information, 
stop calling tools and produce the final JSON report."

---

Write unit tests in `tests/test_generator.py` that mock the Anthropic 
client and E*TRADE session to verify: (1) the loop terminates correctly 
at max turns, (2) disallowed tickers are rejected, (3) duplicate calls 
are blocked, (4) a clean end_turn on the first response skips tool calls 
entirely.
