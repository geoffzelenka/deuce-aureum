"""
Morning report generator (stub).
"""

from auth.etrade_auth import get_session


def generate_morning_report(symbols: list[str]) -> str:
    """
    Build a morning summary for the given symbols.
    Returns a formatted string report.

    TODO:
      - Fetch quotes for each symbol via E*TRADE market API
      - Pull recent headlines from the local DB
      - Summarise overnight moves and news
    """
    session = get_session()  # noqa: F841 — will be used in full implementation
    lines = ["=== Morning Report ===", ""]
    for symbol in symbols:
        lines.append(f"  {symbol}: (data not yet implemented)")
    lines.append("")
    lines.append("Report generation is a stub — implement fetch logic here.")
    return "\n".join(lines)
