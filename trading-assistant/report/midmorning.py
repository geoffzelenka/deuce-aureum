"""
Mid-morning assessment (~10:30 AM, market open ~60 minutes).

Loads today's pre-market watchlist from the DB, runs Phase 2 research
with all three tools (including get_options_flow), and produces a
confirmed top-3 report with conviction changes vs the pre-market ranking.
"""

import json
import sys
import time
from datetime import date, datetime

import anthropic
from dotenv import load_dotenv

from store import db
from report.generator import (
    _ALL_TOOLS,
    _build_system_prompt,
    _conversation_log_path,
    _format_conversation,
    _parse_final_report,
    _print_summary,
    _run_research_phase,
    _write_conversation_log,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Mid-morning JSON schema (shown to Claude in the initial user message)
# ---------------------------------------------------------------------------

_MIDMORNING_SCHEMA_DESCRIPTION = """{
  "top_plays": [            // exactly 3 confirmed entries
    {
      "ticker": "AAPL",
      "play_type": "day_trade",          // "day_trade" or "overnight"
      "thesis": "brief reason",
      "risk": "brief risk statement",
      "options_confirmation": "what options flow showed for this ticker",
      "conviction_change": "upgraded"    // "upgraded", "unchanged", or "downgraded"
                                         //   vs pre-market provisional rank
    }
  ],
  "watchlist_dropped": [   // candidates that did not make confirmed top 3
    {
      "ticker": "XYZ",
      "reason": "why it was dropped or not promoted"
    }
  ],
  "position_outlooks": [   // one entry per current position
    {
      "ticker": "AAPL",
      "outlook": "bullish",
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


def _compute_conviction_change(pre_market_rank: int | None, confirmed_rank: int) -> str:
    """
    Derive conviction change by comparing mid-morning confirmed_rank to
    the pre-market provisional rank.

    Lower rank = higher conviction (rank 1 is top pick).
    """
    if pre_market_rank is None:
        return "unchanged"
    if confirmed_rank < pre_market_rank:
        return "upgraded"
    if confirmed_rank > pre_market_rank:
        return "downgraded"
    return "unchanged"


def _build_midmorning_user_message(
    watchlist: list[dict],
    headlines: list[str],
    positions: list[dict],
) -> tuple[dict, list[str]]:
    """
    Build the initial user message for the mid-morning assessment.

    Includes headlines, positions, watchlist candidates, current time,
    research instructions, and JSON schema — everything Claude needs in
    a single user turn (avoids consecutive user messages in the conversation).

    Returns:
        (message dict, candidates list) — the message to send and the
        list of ticker strings extracted from the watchlist.
    """
    today = date.today()
    now_str = datetime.now().strftime("%H:%M")
    day_str = today.strftime("%A, %Y-%m-%d")

    # Format headlines
    headlines_text = (
        "\n".join(f"- {h}" for h in headlines)
        if headlines
        else "(no headlines stored for today)"
    )

    # Format positions
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

    # Format watchlist — provisional top 3 first, then rest
    provisional_top3 = sorted(
        [w for w in watchlist if w.get("rank") is not None and w["rank"] <= 3],
        key=lambda w: w["rank"],
    )
    rest = [w for w in watchlist if w not in provisional_top3]

    watchlist_lines = []
    for w in provisional_top3:
        rank = w.get("rank", "?")
        ticker = w["ticker"]
        catalyst = w.get("catalyst") or "no catalyst recorded"
        score = w.get("pre_market_score") or "unknown"
        watchlist_lines.append(
            f"  #{rank} {ticker} (pre_market_score={score}) — {catalyst}"
        )
    for w in rest:
        rank = w.get("rank", "?")
        ticker = w["ticker"]
        catalyst = w.get("catalyst") or "no catalyst recorded"
        score = w.get("pre_market_score") or "unknown"
        watchlist_lines.append(
            f"  #{rank} {ticker} (pre_market_score={score}) — {catalyst}"
        )

    watchlist_text = "\n".join(watchlist_lines) if watchlist_lines else "(empty)"

    candidates = [w["ticker"] for w in watchlist]

    content = (
        f"Today is {day_str}. Current time: {now_str} (market has been open ~60 minutes).\n\n"
        f"TODAY'S HEADLINES:\n{headlines_text}\n\n"
        f"CURRENT POSITIONS:\n{positions_text}\n\n"
        f"PRE-MARKET WATCHLIST (provisional ranking):\n{watchlist_text}\n\n"
        "TASK: Re-evaluate the watchlist using fresh quotes, technicals, and options flow. "
        "Confirm, re-rank, or replace the provisional top 3.\n\n"
        f"Research these tickers: {candidates}. "
        "Call get_quote then get_technicals for each, then use get_options_flow "
        "for any ticker with a binary catalyst. "
        "When all research is complete, produce the final JSON report.\n\n"
        f"Return a JSON object matching this schema exactly:\n{_MIDMORNING_SCHEMA_DESCRIPTION}\n\n"
        "Requirements:\n"
        "- top_plays: exactly 3 confirmed entries\n"
        "- watchlist_dropped: all watchlist candidates NOT in top_plays\n"
        "- position_outlooks: one entry for every position listed above\n"
        "- long_term_entries: 1–3 longer-term setups\n"
        "- conviction_change: compare your confirmed rank to the provisional pre_market rank above\n\n"
        "Return only the JSON object. No markdown, no commentary."
    )
    return {"role": "user", "content": content}, candidates


def run_midmorning_assessment(etrade_session=None, debug: bool = False) -> dict:
    """
    Run the mid-morning assessment (~10:30 AM).

    1. Checks that a pre-market watchlist exists for today.
    2. Loads the watchlist, today's headlines, and positions from the DB.
    3. Builds a context message including the provisional top 3 and catalysts.
    4. Runs Phase 2 research with all three tools (get_quote, get_technicals,
       get_options_flow).
    5. Calls update_watchlist_confirmation() for each confirmed top-3 ticker.

    Returns:
        The parsed mid-morning report dict.
    """
    today = date.today()

    # Step 1 — ensure pre-market ran first
    if not db.watchlist_exists(today):
        print("No pre-market watchlist found for today. Run kickoff first.")
        sys.exit(1)

    # Step 2 — load context from DB
    watchlist = db.get_watchlist(today)
    headlines = db.get_todays_headlines()
    positions = db.get_positions()

    allowed_tickers = {w["ticker"].upper() for w in watchlist}

    if debug:
        log_path = _conversation_log_path("midmorning")
        print(f"  [debug] conversation log → {log_path}")
    else:
        log_path = None

    # Step 3 — build initial message and run research
    client = anthropic.Anthropic()
    system_prompt = _build_system_prompt("midmorning")
    tools = _ALL_TOOLS

    initial_message, candidates = _build_midmorning_user_message(watchlist, headlines, positions)
    messages: list[dict] = [initial_message]

    if debug:
        _write_conversation_log(
            _format_conversation(messages, "midmorning-setup", finished=False),
            path=log_path,
        )

    # Step 4 — Phase 2 research loop (all tools)
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

    # Step 5 — persist confirmation results
    confirmed_plays = report.get("top_plays", [])[:3]
    for confirmed_rank, play in enumerate(confirmed_plays, start=1):
        ticker = play.get("ticker", "").upper().strip()
        if not ticker:
            continue

        # Determine options_unusual from the play's options_confirmation text
        # (heuristic: treat it as unusual if Claude flagged "unusual" in the text)
        options_conf = play.get("options_confirmation", "") or ""
        options_unusual = "unusual" in options_conf.lower()

        try:
            db.update_watchlist_confirmation(
                ticker=ticker,
                session_date=today,
                confirmed_rank=confirmed_rank,
                options_unusual=options_unusual,
            )
        except Exception as exc:
            print(f"  [warn] watchlist confirmation update failed for {ticker}: {exc}")

    return report
