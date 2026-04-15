"""
Morning report generator using the Anthropic Claude API with tool use.

Uses an agentic loop that lets Claude request live E*TRADE market data
(quotes and technicals) mid-analysis before producing the final JSON report.
"""

import json
import logging
import os
import re
import time
from datetime import date

import anthropic
import requests
from dotenv import load_dotenv

import config
from store import db

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


def _conversation_log_path() -> str:
    return f"./logs/report_conversation_{date.today().strftime('%Y-%m-%d')}.log"


def _write_conversation_log(lines: list[str]) -> None:
    """Overwrite today's conversation log with the current content."""
    with open(_conversation_log_path(), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _format_conversation(messages: list, turns: int, finished: bool) -> list[str]:
    """
    Render the messages list as human-readable lines for the debug log.
    Each API round-trip is labelled by turn number.
    """
    SEP = "=" * 80
    out = [
        SEP,
        f"REPORT CONVERSATION — {date.today().isoformat()}  "
        f"(logged {time.strftime('%H:%M:%S')})",
        SEP,
        "",
    ]

    tool_turn = 0  # counts tool-call rounds
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            if i == 0:
                # Initial prompt — show a truncated version so the log stays readable
                text = content if isinstance(content, str) else str(content)
                preview = text[:800] + (" …[truncated]" if len(text) > 800 else "")
                out += [f"[INITIAL USER PROMPT]", preview, ""]
            else:
                # Tool results
                out.append(f"[TURN {tool_turn - 1} — TOOL RESULTS]")
                items = content if isinstance(content, list) else [{"content": content}]
                for item in items:
                    tid = item.get("tool_use_id", "—")
                    result = item.get("content", "")
                    out.append(f"  tool_use_id: {tid}")
                    out.append(f"  result     : {result}")
                out.append("")

        elif role == "assistant":
            blocks = content if isinstance(content, list) else []
            stop_label = ""  # filled in below when we know stop_reason

            text_blocks = [b for b in blocks if getattr(b, "type", None) == "text"]
            tool_blocks = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
            stop_reason = "tool_use" if tool_blocks else "end_turn"

            if tool_blocks:
                out.append(f"[TURN {tool_turn} — CLAUDE] stop_reason=tool_use")
                tool_turn += 1
            else:
                out.append(f"[FINAL — CLAUDE] stop_reason=end_turn")

            for tb in text_blocks:
                text = tb.text.strip()
                if text:
                    out.append(f"  <text>")
                    for line in text.splitlines():
                        out.append(f"    {line}")
                    out.append(f"  </text>")

            for tb in tool_blocks:
                out.append(f"  <tool_use id={tb.id!r} name={tb.name!r}>")
                out.append(f"    {json.dumps(tb.input)}")
                out.append(f"  </tool_use>")

            out.append("")

        i += 1

    if finished:
        out += [SEP, f"END — {turns} tool call turn(s)", SEP]

    return out

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a seasoned stock market analyst with decades of trading experience. "
    "Your specialty is providing actionable, concise guidance based on current news "
    "and market positions. You cut through noise to identify high-probability setups "
    "and speak plainly about risks. Always return valid JSON with no markdown fences "
    "or any text outside the JSON object.\n\n"
    "You have access to two tools: get_quote and get_technicals. Use them "
    "sparingly — only when live data would materially change your analysis. "
    "You are limited to 3 tool calls total. Do not request data for tickers "
    "not mentioned in the headlines or positions. Do not call the same tool "
    "for the same ticker more than once. When you have enough information, "
    "stop calling tools and produce the final JSON report."
)

_SCHEMA_DESCRIPTION = """{
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

MAX_TURNS = 3

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
# E*TRADE API helpers
# ---------------------------------------------------------------------------

def _fetch_etrade_quote(session, ticker: str) -> dict:
    """Fetch a single-ticker quote from E*TRADE with a 5-second timeout."""
    url = f"{config.BASE_URL}/v1/market/quote/{ticker}"
    resp = session.get(
        url,
        params={"detailFlag": "ALL"},
        headers={"Accept": "application/json"},
        timeout=5,
    )
    resp.raise_for_status()
    quote_data = resp.json().get("QuoteResponse", {}).get("QuoteData", [])
    if not quote_data:
        return {}
    all_data = quote_data[0].get("All", {})
    return {
        "last_price": all_data.get("lastTrade") or all_data.get("last") or all_data.get("lastPrice"),
        "bid": all_data.get("bid"),
        "ask": all_data.get("ask"),
        "volume": all_data.get("totalVolume") or all_data.get("volume"),
        "day_high": all_data.get("high"),
        "day_low": all_data.get("low"),
        "prev_close": all_data.get("previousClose"),
    }


def _fetch_technicals_stub(ticker: str) -> dict:
    """
    Stub implementation of get_technicals.

    Computes SMA-20, SMA-50, SMA-200, and average daily volume over 30 days
    from Yahoo Finance's free chart API (no auth required).  RSI-14 is not
    computed here and is returned as null; replace this stub once a proper
    technical-data source is integrated.

    The ``session`` parameter is accepted for API consistency but is unused.
    """
    import statistics

    YF_URL = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    # 1 year of daily bars covers SMA-200 comfortably
    resp = requests.get(
        YF_URL,
        params={"interval": "1d", "range": "1y"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=5,
    )
    resp.raise_for_status()
    result = resp.json().get("chart", {}).get("result") or []
    if not result:
        return {"error": f"No chart data returned for {ticker}"}

    closes = [c for c in result[0]["indicators"]["quote"][0]["close"] if c is not None]
    volumes = [v for v in result[0]["indicators"]["quote"][0]["volume"] if v is not None]

    def _sma(series: list[float], n: int) -> float | None:
        window = series[-n:]
        return round(statistics.mean(window), 4) if len(window) == n else None

    return {
        "ma20": _sma(closes, 20),
        "ma50": _sma(closes, 50),
        "ma200": _sma(closes, 200),
        "avg_volume_30d": round(statistics.mean(volumes[-30:]), 0) if len(volumes) >= 30 else None,
        "rsi14": None,  # stub — not yet computed
        "data_source": "yahoo_finance_stub",
        "bars_available": len(closes),
    }

# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _log_tool_call(
    tool_name: str,
    ticker: str,
    turn: int,
    allowed: bool,
    result_summary: str,
    elapsed_ms: int,
) -> None:
    _tool_logger.info(
        "tool=%s ticker=%s turn=%d allowed=%s elapsed_ms=%d result=%s",
        tool_name,
        ticker,
        turn,
        "yes" if allowed else "no",
        elapsed_ms,
        result_summary[:200],
    )


def dispatch_tool_call(
    block,
    allowed_tickers: set,
    seen_calls: set,
    session,
    turn: int,
) -> str:
    """
    Validate and execute a single tool-use block.

    Enforces:
    - Ticker allow-list
    - Duplicate-call guard
    - 5-second E*TRADE timeout

    Returns a string suitable for the ``content`` field of a tool_result block.
    """
    tool_name = block.name
    ticker = (block.input or {}).get("ticker", "").upper().strip()
    call_key = (tool_name, ticker)

    # Allow-list check
    if ticker not in allowed_tickers:
        result = (
            f"Ticker {ticker} is not in the approved list for this session. "
            "Do not request it again."
        )
        _log_tool_call(tool_name, ticker, turn, allowed=False, result_summary=result, elapsed_ms=0)
        return result

    # Duplicate check
    if call_key in seen_calls:
        result = "You already have this data. Do not repeat tool calls."
        _log_tool_call(tool_name, ticker, turn, allowed=True, result_summary=result, elapsed_ms=0)
        return result

    seen_calls.add(call_key)

    # Execute with timeout
    t0 = time.monotonic()
    try:
        if session is None:
            data = {"error": "No E*TRADE session available — proceeding with headlines only."}
        elif tool_name == "get_quote":
            data = _fetch_etrade_quote(session, ticker)
        elif tool_name == "get_technicals":
            data = _fetch_technicals_stub(ticker)
        else:
            data = {"error": f"Unknown tool: {tool_name}"}

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result = json.dumps(data)
        _log_tool_call(tool_name, ticker, turn, allowed=True, result_summary=result, elapsed_ms=elapsed_ms)
        return result

    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result = f"E*TRADE quote timed out for {ticker}. Proceed without this data."
        _log_tool_call(tool_name, ticker, turn, allowed=True, result_summary=result, elapsed_ms=elapsed_ms)
        return result

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result = f"E*TRADE API error for {ticker}: {str(exc)[:120]}"
        _log_tool_call(tool_name, ticker, turn, allowed=True, result_summary=result, elapsed_ms=elapsed_ms)
        return result

# ---------------------------------------------------------------------------
# Message / response helpers
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
        f"RECENT HEADLINES (last 7 days):\n{headlines_text}\n\n"
        f"CURRENT POSITIONS:\n{positions_text}\n\n"
        f"Analyse the above and return a JSON object matching this schema exactly:\n"
        f"{_SCHEMA_DESCRIPTION}\n\n"
        "Requirements:\n"
        "- top_plays: exactly 3 entries (best day-trade or overnight opportunities)\n"
        "- position_outlooks: one entry for every position listed above\n"
        "- long_term_entries: 1–3 compelling longer-term setups\n\n"
        "Return only the JSON object. No markdown, no commentary."
    )
    return {"role": "user", "content": content}


def _parse_json_response(text: str) -> dict:
    """Strip optional markdown fences and parse JSON."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        stripped = "\n".join(inner).strip()
    return json.loads(stripped)


def _parse_final_report(response) -> dict:
    raw_text = next(
        (block.text for block in response.content if block.type == "text"),
        "",
    )
    return _parse_json_response(raw_text)

# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def generate_report(
    headlines_text: str,
    positions: list[dict],
    etrade_session=None,
    debug: bool = False,
) -> dict:
    """
    Generate a morning trading report using an agentic Claude loop with tool use.

    Claude may call ``get_quote`` or ``get_technicals`` up to MAX_TURNS (3) times
    total before being forced to produce the final JSON report.

    Args:
        headlines_text: Newline-separated headlines string (pre-formatted).
        positions: List of position dicts from the database.
        etrade_session: Optional OAuth1Session for E*TRADE API calls.
            When None, tool calls return a graceful error and Claude proceeds
            with the data it has from headlines and positions.
        debug: When True, write the full Claude conversation (requests, tool
            calls, and tool results) to ./logs/report_conversation_{date}.log.

    Returns:
        Parsed report dict.

    Raises:
        RuntimeError: If the loop exits without producing a report.
    """
    client = anthropic.Anthropic()
    allowed_tickers = build_allow_list(headlines_text, positions)
    seen_calls: set = set()
    turns = 0

    messages = [_build_initial_user_message(headlines_text, positions)]

    if debug:
        print(f"  [debug] conversation log → {_conversation_log_path()}")

    while turns <= MAX_TURNS:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            tools=[GET_QUOTE_TOOL, GET_TECHNICALS_TOOL],
            messages=list(messages),
        )

        messages.append({"role": "assistant", "content": response.content})

        if debug:
            _write_conversation_log(_format_conversation(
                messages, turns, finished=(response.stop_reason == "end_turn")
            ))

        if response.stop_reason == "end_turn":
            print(f"Report generated in {turns} tool call turns.")
            return _parse_final_report(response)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                if turns >= MAX_TURNS:
                    # 4th+ tool call attempt — block it without consuming a turn
                    result = (
                        "Tool call limit reached. Proceed to generate the report now "
                        "with the data you have."
                    )
                else:
                    ticker = (block.input or {}).get("ticker", "").upper().strip()
                    if ticker not in allowed_tickers:
                        # Rejected before dispatch — does not consume a turn
                        result = (
                            f"Ticker {ticker} is not in the approved list for this session. "
                            "Do not request it again."
                        )
                        _log_tool_call(
                            block.name, ticker, turns,
                            allowed=False, result_summary=result, elapsed_ms=0,
                        )
                    else:
                        result = dispatch_tool_call(
                            block, allowed_tickers, seen_calls, etrade_session, turns
                        )
                        turns += 1

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        if turns >= MAX_TURNS:
            # Force one final non-tool completion so Claude must produce the report.
            final = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                system=_SYSTEM_PROMPT,
                tools=[],  # no tools → forces end_turn
                messages=list(messages),
            )
            messages.append({"role": "assistant", "content": final.content})
            if debug:
                _write_conversation_log(_format_conversation(
                    messages, turns, finished=True
                ))
            print(f"Report generated in {turns} tool call turns.")
            return _parse_final_report(final)

    raise RuntimeError("Agentic loop exited without producing a report.")

# ---------------------------------------------------------------------------
# Public wrapper (backward-compatible with main.py)
# ---------------------------------------------------------------------------

def generate_morning_report(etrade_session=None, debug: bool = False) -> dict:
    """
    Fetch data from the database and run the agentic report loop.

    ``etrade_session`` is optional; if omitted, Claude still generates the
    report from headlines and positions without live quote data.
    ``debug`` writes the full Claude conversation to
    ./logs/report_conversation_{date}.log.
    """
    headlines = db.get_recent_headlines(days=7)
    positions = db.get_positions()
    headlines_text = (
        "\n".join(f"- {h}" for h in headlines)
        if headlines
        else "(no recent headlines stored)"
    )
    return generate_report(headlines_text, positions, etrade_session, debug=debug)
