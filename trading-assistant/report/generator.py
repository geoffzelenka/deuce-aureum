"""
Morning report generator using the Anthropic Claude API with tool use.

Two-phase agentic loop:
  Phase 1 — SCAN:  Claude reads all headlines/positions and nominates up to
                   10 candidate tickers without calling any tools.
  Phase 2 — RESEARCH: For each candidate Claude calls get_quote then
                   get_technicals (mandatory, in that order), then up to 2
                   more free-choice calls from the available tools.
                   Hard caps: 4 calls per ticker, 40 calls total.

session_type controls which tools are available:
  "premarket"  — get_quote + get_technicals only (no options flow)
  "midmorning" — all three tools including get_options_flow
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date

import anthropic
from dotenv import load_dotenv

import config
from store import db
from report.enricher import get_quote_data, get_technicals_data, get_options_flow_data

load_dotenv()

# ---------------------------------------------------------------------------
# Logging — tool call audit trail + optional conversation debug log
# ---------------------------------------------------------------------------

os.makedirs("./logs", exist_ok=True)
_tool_logger = logging.getLogger("tool_calls")
if not _tool_logger.handlers:
    _handler = logging.FileHandler("./logs/tool_calls.log")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _tool_logger.addHandler(_handler)
    _tool_logger.setLevel(logging.INFO)


def _api_call_with_retry(fn, max_retries: int = 4, base_delay: float = 5.0):
    """Retry an Anthropic API call on transient 529 overloaded errors."""
    for attempt in range(max_retries):
        try:
            return fn()
        except anthropic.OverloadedError:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"  [warn] Anthropic API overloaded (529); retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)
        except anthropic.AuthenticationError as e:
            raise RuntimeError(
                "Anthropic API key is invalid or missing — check ANTHROPIC_API_KEY in .env"
            ) from e
        except anthropic.PermissionDeniedError as e:
            raise RuntimeError(
                "Anthropic API returned 403 — check your credit balance at console.anthropic.com/settings/billing"
            ) from e


def _conversation_log_path(prefix: str = "report") -> str:
    return f"./logs/{prefix}_conversation_{date.today().strftime('%Y-%m-%d')}.log"


def _write_conversation_log(lines: list[str], path: str | None = None) -> None:
    """Overwrite the conversation log with the current content."""
    target = path or _conversation_log_path()
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _format_conversation(messages: list, phase: str, finished: bool) -> list[str]:
    """
    Render the messages list as human-readable lines for the debug log.
    """
    SEP = "=" * 80
    out = [
        SEP,
        f"REPORT CONVERSATION — {date.today().isoformat()}  "
        f"(logged {time.strftime('%H:%M:%S')})  phase={phase}",
        SEP,
        "",
    ]

    tool_turn = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            if i == 0:
                text = content if isinstance(content, str) else str(content)
                preview = text[:800] + (" …[truncated]" if len(text) > 800 else "")
                out += ["[INITIAL USER PROMPT]", preview, ""]
            elif isinstance(content, str):
                out += [f"[USER — phase transition]", content, ""]
            elif isinstance(content, list):
                # Could be tool results or a phase-transition text block
                first = content[0] if content else {}
                if isinstance(first, dict) and first.get("type") == "tool_result":
                    out.append(f"[TURN {tool_turn - 1} — TOOL RESULTS]")
                    for item in content:
                        tid = item.get("tool_use_id", "—")
                        result = item.get("content", "")
                        out.append(f"  tool_use_id: {tid}")
                        out.append(f"  result     : {result}")
                    out.append("")
                else:
                    out += [f"[USER MESSAGE]", str(content), ""]

        elif role == "assistant":
            blocks = content if isinstance(content, list) else []
            text_blocks = [b for b in blocks if getattr(b, "type", None) == "text"]
            tool_blocks = [b for b in blocks if getattr(b, "type", None) == "tool_use"]

            if tool_blocks:
                out.append(f"[TURN {tool_turn} — CLAUDE] stop_reason=tool_use")
                tool_turn += 1
            else:
                out.append("[FINAL — CLAUDE] stop_reason=end_turn")

            for tb in text_blocks:
                text = tb.text.strip()
                if text:
                    out.append("  <text>")
                    for line in text.splitlines():
                        out.append(f"    {line}")
                    out.append("  </text>")

            for tb in tool_blocks:
                out.append(f"  <tool_use id={tb.id!r} name={tb.name!r}>")
                out.append(f"    {json.dumps(tb.input)}")
                out.append("  </tool_use>")

            out.append("")

        i += 1

    if finished:
        out += [SEP, "END OF CONVERSATION", SEP]

    return out

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SESSION_CONTEXT_PREMARKET = (
    "SESSION CONTEXT: Pre-market analysis (~45 minutes before open).\n"
    "Options flow data is NOT available at this time — do not attempt to "
    "call get_options_flow. Your top 3 picks are provisional and will be "
    "confirmed at 10:30 AM once options flow is available.\n"
    "When identifying candidates in Phase 1, note the specific catalyst "
    "and assign a pre_market_score of high/medium/low based on how "
    "unambiguous and unpriced the catalyst appears to be.\n\n"
)

_SESSION_CONTEXT_MIDMORNING = (
    "SESSION CONTEXT: Mid-morning assessment (~10:30 AM, market open ~60 min).\n"
    "You have access to get_options_flow. Options volume is now meaningful — "
    "use it for any candidate with a binary catalyst (earnings, FDA, merger, "
    "macro event). The watchlist below was built pre-market. Your job is to "
    "validate, re-rank, and confirm the top 3 using live data.\n"
    "For each candidate, check: has the price moved significantly since "
    "pre-market? Does options flow confirm the directional thesis or "
    "contradict it? Upgrade conviction if options flow is unusual and "
    "aligned. Downgrade or drop if the catalyst is already fully priced in "
    "or options flow is contradictory.\n"
    "Skip Phase 1 — candidates are already identified. Proceed directly to "
    "Phase 2 research on the watchlist tickers provided.\n\n"
)

_BASE_SYSTEM_PROMPT = (
    "You are a seasoned stock market analyst with decades of trading experience. "
    "Your specialty is providing actionable, concise guidance based on current news "
    "and market positions. You cut through noise to identify high-probability setups "
    "and speak plainly about risks. Always return valid JSON with no markdown fences "
    "or any text outside the JSON object.\n\n"

    "TOOL USE GUIDANCE\n\n"

    "You work in two phases:\n\n"

    "PHASE 1 — SCAN (current phase when you first receive the prompt)\n"
    "Read all headlines and positions. Identify up to 10 candidate tickers "
    "that have a clear news catalyst worthy of investigation. Output your "
    "candidates list in your response as a JSON block:\n"
    '{"candidates": [{"ticker": "AAPL", "catalyst": "one-sentence reason", '
    '"pre_market_score": "high|medium|low"}, ...]}\n'
    "Do not call any tools in this phase. Be selective — only include tickers "
    "where a catalyst is evident in the headlines or where a held position "
    "warrants live data to assess. Assign pre_market_score based on how "
    "unambiguous and unpriced the catalyst appears.\n\n"

    "PHASE 2 — RESEARCH (begins after candidates are confirmed)\n"
    "For each candidate ticker you must follow this call order:\n"
    "  1. get_quote — always first. Establishes whether the catalyst is "
    "already priced in (stock already up/down significantly).\n"
    "  2. get_technicals — always second. Confirms trend direction and "
    "whether the setup has technical support.\n"
    "  3 & 4. Your choice from available tools. "
    "Use get_options_flow (when available) for binary catalyst events "
    "(earnings, FDA, merger votes, Fed decisions) to gauge smart money "
    "positioning. Use a second get_quote if significant time has passed. "
    "Use a second get_technicals only if the first result was ambiguous.\n\n"

    "Budget rules:\n"
    "- Maximum 4 tool calls per ticker.\n"
    "- Maximum 40 tool calls total across all tickers.\n"
    "- Do not call tools for tickers not in your confirmed candidates list.\n"
    "- When your research on all candidates is complete, stop calling tools "
    "and produce the final JSON report immediately.\n\n"

    "RANKING GUIDANCE\n"
    "After researching all candidates, rank your top 3 plays using this "
    "priority order:\n"
    "  1. Strongest unpriced catalyst (stock has not yet moved on the news)\n"
    "  2. Options flow confirmation (unusual activity signals smart money "
    "positioning ahead of you)\n"
    "  3. Technical setup aligned with catalyst direction (trend, support, "
    "moving averages confirm the trade)\n"
    "  4. Clean risk narrative (one specific, avoidable risk — not generic)\n\n"

    "A play with all four factors is a high-conviction pick. A play missing "
    "factor 1 (catalyst already priced in) should be dropped regardless of "
    "how good the technicals look."
)


def _build_system_prompt(session_type: str) -> str:
    """Prepend the appropriate SESSION CONTEXT block to the base system prompt."""
    if session_type == "premarket":
        return _SESSION_CONTEXT_PREMARKET + _BASE_SYSTEM_PROMPT
    if session_type == "midmorning":
        return _SESSION_CONTEXT_MIDMORNING + _BASE_SYSTEM_PROMPT
    return _BASE_SYSTEM_PROMPT


_PREMARKET_SCHEMA_DESCRIPTION = """{
  "top_plays": [            // exactly 3 entries
    {
      "ticker": "AAPL",
      "play_type": "day_trade",   // "day_trade" or "overnight"
      "thesis": "brief reason",
      "entry_range": "$150-$153",
      "risk": "brief risk statement"
    }
  ],
  "position_outlooks": [   // one entry per current position
    {
      "ticker": "AAPL",
      "outlook": "bullish",       // "bullish", "neutral", or "bearish"
      "summary": "brief summary",
      "action": "hold / add / trim / exit"
    }
  ],
  "long_term_entries": [   // 1–3 entries
    {
      "ticker": "NVDA",
      "thesis": "why to enter",
      "suggested_entry": "$400-$420",
      "time_horizon": "3–6 months"
    }
  ]
}"""

# Keep the old name as an alias for backward compatibility
_SCHEMA_DESCRIPTION = _PREMARKET_SCHEMA_DESCRIPTION

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

GET_QUOTE_TOOL = {
    "name": "get_quote",
    "description": (
        "Fetch the current quote for a single stock ticker from E*TRADE. "
        "Use this when you need the current price, volume, bid/ask, day high/low, "
        "or previous close for a specific ticker to support your analysis. "
        "Do not request a ticker unless it is directly relevant to the headlines "
        "or positions provided."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Uppercase stock ticker symbol, e.g. AAPL",
            }
        },
        "required": ["ticker"],
    },
}

GET_TECHNICALS_TOOL = {
    "name": "get_technicals",
    "description": (
        "Fetch a summary of key technical indicators for a ticker: "
        "50-day and 200-day moving averages, RSI-14, and average daily volume "
        "over 30 days. Use this only when the headline or position context "
        "specifically warrants technical analysis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
            }
        },
        "required": ["ticker"],
    },
}

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
        "required": ["ticker"],
    },
}

_ALL_TOOLS = [GET_QUOTE_TOOL, GET_TECHNICALS_TOOL, GET_OPTIONS_FLOW_TOOL]
_PREMARKET_TOOLS = [GET_QUOTE_TOOL, GET_TECHNICALS_TOOL]

# Budget constants
GLOBAL_MAX = 40
PER_TICKER_MAX = 4

# ---------------------------------------------------------------------------
# TickerBudget
# ---------------------------------------------------------------------------

@dataclass
class TickerBudget:
    ticker: str
    calls_made: int = 0
    quote_done: bool = False
    technicals_done: bool = False

    def can_call(self, tool_name: str) -> tuple[bool, str]:
        if self.calls_made >= PER_TICKER_MAX:
            return False, f"Budget exhausted for {self.ticker} ({PER_TICKER_MAX}/{PER_TICKER_MAX} calls used)."
        if tool_name == "get_quote" and not self.quote_done:
            return True, ""
        if tool_name == "get_technicals" and not self.quote_done:
            return False, f"Call get_quote for {self.ticker} before get_technicals."
        if tool_name == "get_technicals" and not self.technicals_done:
            return True, ""
        if self.calls_made < 2:
            return False, f"Complete mandatory calls (get_quote, get_technicals) for {self.ticker} first."
        return True, ""

    def record_call(self, tool_name: str) -> None:
        self.calls_made += 1
        if tool_name == "get_quote" and not self.quote_done:
            self.quote_done = True
        elif tool_name == "get_technicals":
            self.technicals_done = True

    @property
    def budget_status(self) -> str:
        return f"{self.calls_made}/{PER_TICKER_MAX} calls used"

# ---------------------------------------------------------------------------
# Allow-list builder
# ---------------------------------------------------------------------------

def build_allow_list(headlines_text: str, positions: list[dict]) -> set[str]:
    """
    Build a set of approved ticker symbols for this session from:
    - tickers in the positions list
    - uppercase letter sequences [A-Z]{1,5} extracted from headlines
    """
    allowed = {p["ticker"].upper() for p in positions if p.get("ticker")}
    for match in re.finditer(r'\b([A-Z]{1,5})\b', headlines_text):
        allowed.add(match.group(1))
    return allowed

# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _log_tool_call(
    tool_name: str,
    ticker: str,
    phase: str,
    allowed: bool,
    result_summary: str,
    elapsed_ms: int,
    budget_status: str = "",
    unusual_activity: bool | None = None,
) -> None:
    extras = f" budget={budget_status!r}" if budget_status else ""
    if unusual_activity is not None:
        extras += f" unusual_activity={unusual_activity}"
    _tool_logger.info(
        "phase=%s tool=%s ticker=%s allowed=%s elapsed_ms=%d%s result=%s",
        phase,
        tool_name,
        ticker,
        "yes" if allowed else "no",
        elapsed_ms,
        extras,
        result_summary[:200],
    )


def dispatch_tool_call(tool_name: str, ticker: str, etrade_session) -> str:
    """
    Execute a validated tool call.

    Preconditions (enforced by the caller):
    - ticker is in the session allow-list
    - TickerBudget.can_call() returned (True, "")
    - global total_calls < GLOBAL_MAX

    Returns a JSON string (or a plain-text error message) suitable for the
    ``content`` field of a tool_result block.
    """
    if tool_name == "get_quote":
        data = get_quote_data(ticker, etrade_session)
    elif tool_name == "get_technicals":
        data = get_technicals_data(ticker)
    elif tool_name == "get_options_flow":
        data = get_options_flow_data(ticker, etrade_session)
    else:
        data = {"error": f"Unknown tool: {tool_name}"}

    return json.dumps(data)

# ---------------------------------------------------------------------------
# Candidate parsing
# ---------------------------------------------------------------------------

def parse_candidates_full(response) -> list[dict]:
    """
    Extract full candidate metadata from a Phase 1 scan response.

    Handles both the new dict format:
        {"candidates": [{"ticker": "AAPL", "catalyst": "...", "pre_market_score": "high"}, ...]}
    and the legacy plain-string format:
        {"candidates": ["AAPL", "MSFT"]}

    Returns up to 10 dicts with keys: ticker, rank, catalyst, pre_market_score.
    """
    text = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        "",
    )
    match = re.search(r'"candidates"\s*:\s*(\[.*?\])', text, re.DOTALL)
    if match:
        try:
            items = json.loads(match.group(1))
            if isinstance(items, list):
                result = []
                for i, item in enumerate(items[:10]):
                    if isinstance(item, str) and item.strip():
                        result.append({
                            "ticker": item.upper().strip(),
                            "rank": i + 1,
                            "catalyst": None,
                            "pre_market_score": None,
                        })
                    elif isinstance(item, dict):
                        ticker = str(item.get("ticker", "")).upper().strip()
                        if ticker:
                            result.append({
                                "ticker": ticker,
                                "rank": i + 1,
                                "catalyst": item.get("catalyst"),
                                "pre_market_score": item.get("pre_market_score"),
                            })
                return result
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def parse_candidates(response) -> list[str]:
    """
    Extract the candidates list from a Phase 1 scan response.

    Handles both the new dict format (with catalyst/pre_market_score) and
    the legacy plain-string format.  Returns up to 10 uppercase ticker strings.
    """
    return [c["ticker"] for c in parse_candidates_full(response)]

# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

def _build_initial_user_message(headlines_text: str, positions: list[dict]) -> dict:
    today = date.today()
    day_of_week = today.strftime("%A")
    date_str = today.strftime("%Y-%m-%d")

    position_lines = []
    for p in positions:
        parts = [f"  {p['ticker']}"]
        if p.get("name"):
            parts[0] += f" ({p['name']})"
        if p.get("shares") is not None:
            parts.append(f"{p['shares']:.4g} shares")
        if p.get("avg_cost") is not None:
            parts.append(f"avg cost ${p['avg_cost']:.2f}")
        if p.get("notes"):
            parts.append(f"[{p['notes']}]")
        position_lines.append(" — ".join(parts))
    positions_text = "\n".join(position_lines) if position_lines else "(no positions on file)"

    content = (
        f"Today is {day_of_week}, {date_str}.\n\n"
        f"TODAY'S HEADLINES:\n{headlines_text}\n\n"
        f"CURRENT POSITIONS:\n{positions_text}\n\n"
        f"Analyse the above and return a JSON object matching this schema exactly:\n"
        f"{_PREMARKET_SCHEMA_DESCRIPTION}\n\n"
        "Requirements:\n"
        "- top_plays: exactly 3 entries (best day-trade or overnight opportunities)\n"
        "- position_outlooks: one entry for every position listed above\n"
        "- long_term_entries: 1–3 compelling longer-term setups\n\n"
        "Return only the JSON object. No markdown, no commentary."
    )
    return {"role": "user", "content": content}


def _parse_json_response(text: str) -> dict:
    """
    Extract and parse a JSON object from Claude's response text.

    Handles:
    - Bare JSON (most common in production)
    - Markdown fences (```json ... ```)
    - Leading prose before the opening brace
    """
    stripped = text.strip()

    # Strip markdown fences
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        stripped = "\n".join(inner).strip()

    # Find the first { and extract the matching JSON object by tracking brace depth.
    # This handles trailing prose or multiple JSON blocks in the response.
    start = stripped.find("{")
    if start < 0:
        raise ValueError("No JSON object found in response")
    depth = 0
    in_string = False
    escape_next = False
    end = start
    for i, ch in enumerate(stripped[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    return json.loads(stripped[start : end + 1])


def _parse_final_report(response) -> dict:
    raw_text = next(
        (block.text for block in response.content if block.type == "text"),
        "",
    )
    return _parse_json_response(raw_text)

# ---------------------------------------------------------------------------
# Shared Phase 2 research loop
# ---------------------------------------------------------------------------

def _run_research_phase(
    client,
    messages: list[dict],
    system_prompt: str,
    tools: list,
    allowed_tickers: set[str],
    candidates: list[str],
    etrade_session,
    debug: bool = False,
    conversation_log_path: str | None = None,
) -> dict:
    """
    Execute the Phase 2 research loop and return the parsed report dict.

    Modifies ``messages`` in-place, appending assistant and user turns.
    Returns when Claude emits end_turn or the global call cap is hit.
    """
    ticker_budgets: dict[str, TickerBudget] = {t: TickerBudget(ticker=t) for t in candidates}
    total_calls = 0

    while True:
        response = _api_call_with_retry(lambda: client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=list(messages),
        ))
        messages.append({"role": "assistant", "content": response.content})

        if debug and conversation_log_path:
            _write_conversation_log(
                _format_conversation(messages, "research", finished=(response.stop_reason == "end_turn")),
                path=conversation_log_path,
            )

        if response.stop_reason == "end_turn":
            _print_summary(len(candidates), total_calls)
            return _parse_final_report(response)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                ticker = (block.input or {}).get("ticker", "").upper().strip()
                tool_name = block.name

                # Global cap check
                if total_calls >= GLOBAL_MAX:
                    result = (
                        "Global research budget exhausted (40/40 calls). "
                        "Produce the final JSON report now."
                    )
                    _log_tool_call(
                        tool_name, ticker, "research",
                        allowed=False, result_summary=result, elapsed_ms=0,
                    )
                elif ticker not in allowed_tickers:
                    result = (
                        f"Ticker {ticker} is not on the approved list. "
                        "Do not request it."
                    )
                    _log_tool_call(
                        tool_name, ticker, "research",
                        allowed=False, result_summary=result, elapsed_ms=0,
                    )
                else:
                    # Per-ticker budget check
                    if ticker not in ticker_budgets:
                        ticker_budgets[ticker] = TickerBudget(ticker=ticker)
                    budget = ticker_budgets[ticker]
                    can, reason = budget.can_call(tool_name)

                    if not can:
                        result = reason
                        _log_tool_call(
                            tool_name, ticker, "research",
                            allowed=False, result_summary=result, elapsed_ms=0,
                            budget_status=budget.budget_status,
                        )
                    else:
                        t0 = time.monotonic()
                        result = dispatch_tool_call(tool_name, ticker, etrade_session)
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        budget.record_call(tool_name)
                        total_calls += 1

                        unusual = None
                        if tool_name == "get_options_flow":
                            try:
                                unusual = json.loads(result).get("unusual_activity")
                            except (json.JSONDecodeError, AttributeError):
                                pass

                        _log_tool_call(
                            tool_name, ticker, "research",
                            allowed=True, result_summary=result, elapsed_ms=elapsed_ms,
                            budget_status=budget.budget_status,
                            unusual_activity=unusual,
                        )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        # Force final if global cap hit
        if total_calls >= GLOBAL_MAX:
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "Research budget exhausted. Produce the final JSON report now.",
                }],
            })
            final = _api_call_with_retry(lambda: client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                system=system_prompt,
                tools=[],
                messages=list(messages),
            ))
            messages.append({"role": "assistant", "content": final.content})
            if debug and conversation_log_path:
                _write_conversation_log(
                    _format_conversation(messages, "research", finished=True),
                    path=conversation_log_path,
                )
            _print_summary(len(candidates), total_calls)
            return _parse_final_report(final)

# ---------------------------------------------------------------------------
# Two-phase agentic loop
# ---------------------------------------------------------------------------

def generate_report(
    headlines_text: str,
    positions: list[dict],
    etrade_session=None,
    debug: bool = False,
    session_type: str = "premarket",
) -> dict:
    """
    Generate a morning trading report using a two-phase agentic Claude loop.

    Phase 1 — SCAN: Claude identifies up to 10 candidate tickers without
    making any tool calls. Candidates are saved to the watchlist table.

    Phase 2 — RESEARCH: For each candidate Claude calls get_quote then
    get_technicals (mandatory), then up to 2 more free-choice calls.
    Hard caps: 4 calls/ticker, 40 calls total.

    Args:
        headlines_text: Newline-separated headlines string.
        positions: List of position dicts from the database.
        etrade_session: Optional OAuth1Session for E*TRADE API calls.
        debug: When True, write the full Claude conversation to
            ./logs/report_conversation_{date}.log.
        session_type: "premarket" (no options flow) or "midmorning" (all tools).

    Returns:
        Parsed report dict.

    Raises:
        RuntimeError: If the loop exits without producing a report.
    """
    client = anthropic.Anthropic()
    allowed_tickers = build_allow_list(headlines_text, positions)
    system_prompt = _build_system_prompt(session_type)
    tools = _PREMARKET_TOOLS if session_type == "premarket" else _ALL_TOOLS

    if debug:
        log_path = _conversation_log_path("report")
        print(f"  [debug] conversation log → {log_path}")
    else:
        log_path = None

    # -----------------------------------------------------------------------
    # Phase 1 — SCAN
    # -----------------------------------------------------------------------
    messages: list[dict] = [_build_initial_user_message(headlines_text, positions)]

    scan_response = _api_call_with_retry(lambda: client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=system_prompt,
        tools=tools,
        tool_choice={"type": "none"},
        messages=list(messages),
    ))
    messages.append({"role": "assistant", "content": scan_response.content})

    if debug:
        _write_conversation_log(
            _format_conversation(messages, "scan", finished=False),
            path=log_path,
        )

    # Parse candidates (full metadata)
    candidates_full = parse_candidates_full(scan_response)
    filtered_full = [c for c in candidates_full if c["ticker"] in allowed_tickers]
    candidates = [c["ticker"] for c in filtered_full]
    ticker_budgets: dict[str, TickerBudget] = {t: TickerBudget(ticker=t) for t in candidates}

    # Save candidates to watchlist immediately after Phase 1
    if session_type == "premarket" and filtered_full:
        try:
            db.save_watchlist(filtered_full, date.today())
        except Exception as exc:
            print(f"  [warn] watchlist save failed: {exc}")

    # Instruct Claude to begin the research phase
    messages.append({
        "role": "user",
        "content": (
            f"Candidates confirmed: {candidates}. "
            "Begin research phase. Call get_quote then get_technicals for each "
            "candidate, then use your remaining 2 calls per ticker as you see fit. "
            "When all research is complete, produce the final JSON report."
        ),
    })

    # -----------------------------------------------------------------------
    # Phase 2 — RESEARCH loop
    # -----------------------------------------------------------------------
    report = _run_research_phase(
        client=client,
        messages=messages,
        system_prompt=system_prompt,
        tools=tools,
        allowed_tickers=allowed_tickers,
        candidates=candidates,
        etrade_session=etrade_session,
        debug=debug,
        conversation_log_path=log_path,
    )

    # After Phase 2 (premarket): update watchlist ranks for the provisional top 3
    if session_type == "premarket":
        try:
            for i, play in enumerate(report.get("top_plays", [])[:3]):
                ticker = play.get("ticker", "").upper().strip()
                if ticker:
                    db.update_watchlist_rank(ticker, date.today(), i + 1)
        except Exception as exc:
            print(f"  [warn] watchlist rank update failed: {exc}")

    return report


def _print_summary(n_candidates: int, total_calls: int) -> None:
    print(
        f"Research complete: {n_candidates} candidates scanned, "
        f"{total_calls} total tool calls used, top 3 selected."
    )

# ---------------------------------------------------------------------------
# Public wrapper (backward-compatible with main.py)
# ---------------------------------------------------------------------------

def generate_morning_report(
    etrade_session=None,
    debug: bool = False,
    session_type: str = "premarket",
) -> dict:
    """
    Fetch data from the database and run the agentic report loop.

    ``etrade_session`` is optional; if omitted, Claude still generates the
    report from headlines and positions without live quote data.
    ``debug`` writes the full Claude conversation to
    ./logs/report_conversation_{date}.log.
    ``session_type`` is "premarket" (default) or "midmorning".
    """
    headlines = db.get_todays_headlines()
    positions = db.get_positions()
    headlines_text = (
        "\n".join(f"- {h}" for h in headlines)
        if headlines
        else "(no headlines stored for today)"
    )
    return generate_report(headlines_text, positions, etrade_session, debug=debug, session_type=session_type)
