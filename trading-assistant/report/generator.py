"""
Morning report generator using the Anthropic Claude API.
"""

import json
from datetime import date

import anthropic
from dotenv import load_dotenv

from store import db

load_dotenv()

_SYSTEM_PROMPT = (
    "You are a seasoned stock market analyst with decades of trading experience. "
    "Your specialty is providing actionable, concise guidance based on current news "
    "and market positions. You cut through noise to identify high-probability setups "
    "and speak plainly about risks. Always return valid JSON with no markdown fences "
    "or any text outside the JSON object."
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


def _build_user_prompt() -> str:
    today = date.today()
    day_of_week = today.strftime("%A")
    date_str = today.strftime("%Y-%m-%d")

    headlines = db.get_recent_headlines(days=7)
    positions = db.get_positions()

    if headlines:
        headlines_text = "\n".join(f"- {h}" for h in headlines)
    else:
        headlines_text = "(no recent headlines stored)"

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

    return (
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


def _parse_json_response(text: str) -> dict:
    """Strip optional markdown fences and parse JSON."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Drop opening fence line and closing fence
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        stripped = "\n".join(inner).strip()
    return json.loads(stripped)


def generate_morning_report() -> dict:
    """
    Call Claude to generate a morning trading report.

    Returns the parsed report dict.
    Raises RuntimeError if JSON parsing fails after one retry.
    """
    client = anthropic.Anthropic()
    user_prompt = _build_user_prompt()

    messages = [{"role": "user", "content": user_prompt}]

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    raw_text = next(
        (block.text for block in response.content if block.type == "text"),
        "",
    )

    try:
        return _parse_json_response(raw_text)
    except (json.JSONDecodeError, ValueError):
        pass  # fall through to retry

    # Retry once with a corrective prompt
    messages.append({"role": "assistant", "content": raw_text})
    messages.append({
        "role": "user",
        "content": (
            "Your previous response was not valid JSON. "
            "Please respond with only the JSON object — no markdown fences, "
            "no commentary, no extra text."
        ),
    })

    retry_response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    retry_text = next(
        (block.text for block in retry_response.content if block.type == "text"),
        "",
    )

    try:
        return _parse_json_response(retry_text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Failed to parse report JSON after retry: {exc}\n\nRaw response:\n{retry_text}"
        ) from exc
