"""
Tests for the mid-morning assessment workflow.

Covers:
- Midmorning exits cleanly when no pre-market watchlist exists
- get_options_flow excluded from tools in premarket session
- get_options_flow included in tools in midmorning session
- Watcher selects confirmed tickers when they exist in DB
- Watcher falls back to provisional tickers when none are confirmed
- conviction_change correctly computed from rank comparison
"""

import json
import sys
from datetime import date
from unittest.mock import MagicMock, patch, call

import pytest

from report.generator import (
    generate_report,
    GET_OPTIONS_FLOW_TOOL,
    _PREMARKET_TOOLS,
    _ALL_TOOLS,
)
from report.midmorning import _compute_conviction_change, run_midmorning_assessment
from monitor.watcher import get_session_summary


# ---------------------------------------------------------------------------
# Shared fake Anthropic SDK objects (mirror test_generator.py helpers)
# ---------------------------------------------------------------------------

class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, tool_id: str, name: str, ticker: str):
        self.id = tool_id
        self.name = name
        self.input = {"ticker": ticker}


class _Response:
    def __init__(self, stop_reason: str, content: list):
        self.stop_reason = stop_reason
        self.content = content


def _end_turn_response(report_dict: dict | None = None) -> _Response:
    payload = report_dict or {
        "top_plays": [
            {"ticker": "AAPL", "play_type": "day_trade", "thesis": "t", "entry_range": "$150-$153", "risk": "low"},
            {"ticker": "MSFT", "play_type": "overnight", "thesis": "t", "entry_range": "$300-$310", "risk": "low"},
            {"ticker": "NVDA", "play_type": "day_trade", "thesis": "t", "entry_range": "$400-$410", "risk": "medium"},
        ],
        "position_outlooks": [],
        "long_term_entries": [],
    }
    return _Response("end_turn", [_TextBlock(json.dumps(payload))])


def _midmorning_end_turn_response() -> _Response:
    payload = {
        "top_plays": [
            {
                "ticker": "AAPL", "play_type": "day_trade", "thesis": "t", "risk": "low",
                "options_confirmation": "unusual call activity detected",
                "conviction_change": "upgraded",
            },
            {
                "ticker": "NVDA", "play_type": "day_trade", "thesis": "t", "risk": "medium",
                "options_confirmation": "normal flow",
                "conviction_change": "unchanged",
            },
            {
                "ticker": "MSFT", "play_type": "overnight", "thesis": "t", "risk": "low",
                "options_confirmation": "bearish put flow",
                "conviction_change": "downgraded",
            },
        ],
        "watchlist_dropped": [],
        "position_outlooks": [],
        "long_term_entries": [],
    }
    return _Response("end_turn", [_TextBlock(json.dumps(payload))])


def _scan_response(candidates) -> _Response:
    """Phase 1 scan response — candidates may be strings or dicts."""
    text = f'Based on headlines: {{"candidates": {json.dumps(candidates)}}}'
    return _Response("end_turn", [_TextBlock(text)])


SAMPLE_POSITIONS = [{"ticker": "AAPL", "name": "Apple", "shares": 10, "avg_cost": 150.0, "notes": ""}]
SAMPLE_HEADLINES = "- AAPL reports strong earnings. MSFT cloud growth accelerates."


@pytest.fixture()
def mock_anthropic():
    """Patch anthropic.Anthropic so no real API calls are made."""
    with patch("report.generator.anthropic.Anthropic") as MockClass:
        mock_client = MagicMock()
        MockClass.return_value = mock_client
        yield mock_client


@pytest.fixture()
def mock_anthropic_midmorning():
    """Patch anthropic.Anthropic inside midmorning module."""
    with patch("report.midmorning.anthropic.Anthropic") as MockClass:
        mock_client = MagicMock()
        MockClass.return_value = mock_client
        yield mock_client


# ---------------------------------------------------------------------------
# 1. Midmorning exits without watchlist
# ---------------------------------------------------------------------------

class TestMidmorningExitsWithoutWatchlist:
    def test_exits_with_error_message(self, capsys):
        with patch("store.db.watchlist_exists", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                run_midmorning_assessment()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No pre-market watchlist found" in captured.out
        assert "kickoff" in captured.out.lower()


# ---------------------------------------------------------------------------
# 2. get_options_flow excluded from premarket tools
# ---------------------------------------------------------------------------

class TestPremarketToolsExcludeOptionsFlow:
    def test_options_flow_not_in_phase1_tools(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _scan_response(["AAPL"]),
            _end_turn_response(),
        ]
        with patch("store.db.save_watchlist"), patch("store.db.update_watchlist_rank"):
            generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS, session_type="premarket")

        phase1_kwargs = mock_anthropic.messages.create.call_args_list[0][1]
        tool_names = [t["name"] for t in phase1_kwargs["tools"]]
        assert "get_options_flow" not in tool_names

    def test_options_flow_not_in_phase2_tools(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _scan_response(["AAPL"]),
            _end_turn_response(),
        ]
        with patch("store.db.save_watchlist"), patch("store.db.update_watchlist_rank"):
            generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS, session_type="premarket")

        for i, api_call in enumerate(mock_anthropic.messages.create.call_args_list):
            tool_names = [t["name"] for t in api_call[1]["tools"]]
            assert "get_options_flow" not in tool_names, (
                f"get_options_flow found in call #{i} tools for premarket session"
            )

    def test_only_quote_and_technicals_in_premarket(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _scan_response(["AAPL"]),
            _end_turn_response(),
        ]
        with patch("store.db.save_watchlist"), patch("store.db.update_watchlist_rank"):
            generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS, session_type="premarket")

        for api_call in mock_anthropic.messages.create.call_args_list:
            tool_names = sorted(t["name"] for t in api_call[1]["tools"])
            assert tool_names == ["get_quote", "get_technicals"]


# ---------------------------------------------------------------------------
# 3. get_options_flow included in midmorning tools
# ---------------------------------------------------------------------------

class TestMidmorningToolsIncludeOptionsFlow:
    def _make_watchlist(self):
        return [
            {"ticker": "AAPL", "rank": 1, "catalyst": "Earnings beat", "pre_market_score": "high",
             "confirmed": 0, "confirmed_rank": None},
            {"ticker": "MSFT", "rank": 2, "catalyst": "Cloud growth", "pre_market_score": "medium",
             "confirmed": 0, "confirmed_rank": None},
        ]

    def test_options_flow_in_midmorning_tools(self, mock_anthropic_midmorning):
        mock_anthropic_midmorning.messages.create.side_effect = [
            _midmorning_end_turn_response(),
        ]
        with (
            patch("store.db.watchlist_exists", return_value=True),
            patch("store.db.get_watchlist", return_value=self._make_watchlist()),
            patch("store.db.get_todays_headlines", return_value=["AAPL earnings beat"]),
            patch("store.db.get_positions", return_value=SAMPLE_POSITIONS),
            patch("store.db.update_watchlist_confirmation"),
        ):
            run_midmorning_assessment()

        # All API calls must include get_options_flow
        for api_call in mock_anthropic_midmorning.messages.create.call_args_list:
            tool_names = [t["name"] for t in api_call[1].get("tools", [])]
            assert "get_options_flow" in tool_names

    def test_all_three_tools_in_midmorning(self, mock_anthropic_midmorning):
        mock_anthropic_midmorning.messages.create.side_effect = [
            _midmorning_end_turn_response(),
        ]
        with (
            patch("store.db.watchlist_exists", return_value=True),
            patch("store.db.get_watchlist", return_value=self._make_watchlist()),
            patch("store.db.get_todays_headlines", return_value=[]),
            patch("store.db.get_positions", return_value=[]),
            patch("store.db.update_watchlist_confirmation"),
        ):
            run_midmorning_assessment()

        for api_call in mock_anthropic_midmorning.messages.create.call_args_list:
            tools = api_call[1].get("tools", [])
            if tools:  # skip forced-final calls with tools=[]
                tool_names = sorted(t["name"] for t in tools)
                assert tool_names == ["get_options_flow", "get_quote", "get_technicals"]


# ---------------------------------------------------------------------------
# 4 & 5. Watcher uses confirmed vs provisional tickers
# ---------------------------------------------------------------------------

class TestWatcherSessionSummary:
    def _make_watchlist_confirmed(self):
        return [
            {"ticker": "AAPL", "rank": 1, "confirmed": 1, "confirmed_rank": 1,
             "catalyst": "Earnings", "pre_market_score": "high"},
            {"ticker": "MSFT", "rank": 2, "confirmed": 1, "confirmed_rank": 2,
             "catalyst": "Cloud", "pre_market_score": "medium"},
            {"ticker": "NVDA", "rank": 3, "confirmed": 1, "confirmed_rank": 3,
             "catalyst": "AI demand", "pre_market_score": "high"},
        ]

    def _make_watchlist_provisional(self):
        return [
            {"ticker": "AAPL", "rank": 1, "confirmed": 0, "confirmed_rank": None,
             "catalyst": "Earnings", "pre_market_score": "high"},
            {"ticker": "MSFT", "rank": 2, "confirmed": 0, "confirmed_rank": None,
             "catalyst": "Cloud", "pre_market_score": "medium"},
            {"ticker": "NVDA", "rank": 3, "confirmed": 0, "confirmed_rank": None,
             "catalyst": "AI demand", "pre_market_score": "high"},
        ]

    def test_selects_confirmed_when_available(self):
        with (
            patch("store.db.watchlist_exists", return_value=True),
            patch("store.db.get_watchlist", return_value=self._make_watchlist_confirmed()),
        ):
            summary = get_session_summary(date.today())

        assert summary["mode"] == "confirmed"
        assert summary["tickers"] == ["AAPL", "MSFT", "NVDA"]
        assert "confirmed" in summary["reason"].lower()

    def test_falls_back_to_provisional_when_no_confirmed(self):
        with (
            patch("store.db.watchlist_exists", return_value=True),
            patch("store.db.get_watchlist", return_value=self._make_watchlist_provisional()),
        ):
            summary = get_session_summary(date.today())

        assert summary["mode"] == "provisional"
        assert "AAPL" in summary["tickers"]
        assert "mid-morning not yet run" in summary["reason"].lower()

    def test_confirmed_tickers_ordered_by_confirmed_rank(self):
        # Shuffle the watchlist to make sure ordering is by confirmed_rank, not list order
        watchlist = [
            {"ticker": "NVDA", "rank": 3, "confirmed": 1, "confirmed_rank": 1},
            {"ticker": "AAPL", "rank": 1, "confirmed": 1, "confirmed_rank": 3},
            {"ticker": "MSFT", "rank": 2, "confirmed": 1, "confirmed_rank": 2},
        ]
        with (
            patch("store.db.watchlist_exists", return_value=True),
            patch("store.db.get_watchlist", return_value=watchlist),
        ):
            summary = get_session_summary(date.today())

        assert summary["tickers"] == ["NVDA", "MSFT", "AAPL"]

    def test_falls_back_to_report_json_when_no_watchlist(self, tmp_path, monkeypatch):
        """When no watchlist exists, fall back to the report JSON."""
        import os
        # Write a minimal report JSON
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        today_str = date.today().strftime("%Y-%m-%d")
        report_data = {
            "top_plays": [
                {"ticker": "GOOG", "entry_range": "$100-$105"},
                {"ticker": "AMZN", "entry_range": "$200-$210"},
            ]
        }
        (reports_dir / f"{today_str}.json").write_text(json.dumps(report_data))

        # Patch the path used by _load_report_tickers to use tmp_path
        with (
            patch("store.db.watchlist_exists", return_value=False),
            patch("monitor.watcher._load_report_tickers", return_value={"GOOG": (100.0, 105.0), "AMZN": (200.0, 210.0)}),
        ):
            summary = get_session_summary(date.today())

        assert summary["mode"] == "provisional"
        assert "GOOG" in summary["tickers"]
        assert "AMZN" in summary["tickers"]

    def test_unavailable_when_no_watchlist_and_no_report(self):
        with (
            patch("store.db.watchlist_exists", return_value=False),
            patch("monitor.watcher._load_report_tickers", side_effect=RuntimeError("no report")),
        ):
            summary = get_session_summary(date.today())

        assert summary["mode"] == "unavailable"
        assert summary["tickers"] == []


# ---------------------------------------------------------------------------
# 6. conviction_change computation
# ---------------------------------------------------------------------------

class TestComputeConvictionChange:
    def test_upgraded_when_rank_improves(self):
        # Was #3 pre-market, now #1 — upgraded
        assert _compute_conviction_change(pre_market_rank=3, confirmed_rank=1) == "upgraded"

    def test_downgraded_when_rank_worsens(self):
        # Was #1 pre-market, now #3 — downgraded
        assert _compute_conviction_change(pre_market_rank=1, confirmed_rank=3) == "downgraded"

    def test_unchanged_when_rank_same(self):
        assert _compute_conviction_change(pre_market_rank=2, confirmed_rank=2) == "unchanged"

    def test_unchanged_when_no_pre_market_rank(self):
        assert _compute_conviction_change(pre_market_rank=None, confirmed_rank=1) == "unchanged"

    def test_upgraded_for_new_top_entry(self):
        # Ticker was rank 5 pre-market, confirmed as #1
        assert _compute_conviction_change(pre_market_rank=5, confirmed_rank=1) == "upgraded"

    def test_downgraded_from_first_to_third(self):
        assert _compute_conviction_change(pre_market_rank=1, confirmed_rank=3) == "downgraded"


# ---------------------------------------------------------------------------
# 7. Watchlist saved after Phase 1 in premarket session
# ---------------------------------------------------------------------------

class TestWatchlistSavedAfterPhase1:
    def test_save_watchlist_called_with_filtered_candidates(self, mock_anthropic):
        candidates = [
            {"ticker": "AAPL", "catalyst": "Earnings beat", "pre_market_score": "high"},
            {"ticker": "MSFT", "catalyst": "Cloud growth", "pre_market_score": "medium"},
        ]
        mock_anthropic.messages.create.side_effect = [
            _scan_response(candidates),
            _end_turn_response(),
        ]
        with (
            patch("store.db.save_watchlist") as mock_save,
            patch("store.db.update_watchlist_rank"),
        ):
            generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS, session_type="premarket")

        assert mock_save.called
        saved_candidates = mock_save.call_args[0][0]
        tickers_saved = [c["ticker"] for c in saved_candidates]
        # AAPL is in headlines/positions; MSFT is in SAMPLE_HEADLINES
        assert "AAPL" in tickers_saved
        assert "MSFT" in tickers_saved

    def test_catalyst_preserved_in_watchlist_save(self, mock_anthropic):
        candidates = [
            {"ticker": "AAPL", "catalyst": "Strong Q4 earnings beat estimates", "pre_market_score": "high"},
        ]
        mock_anthropic.messages.create.side_effect = [
            _scan_response(candidates),
            _end_turn_response(),
        ]
        with (
            patch("store.db.save_watchlist") as mock_save,
            patch("store.db.update_watchlist_rank"),
        ):
            generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS, session_type="premarket")

        saved = mock_save.call_args[0][0]
        aapl = next(c for c in saved if c["ticker"] == "AAPL")
        assert aapl["catalyst"] == "Strong Q4 earnings beat estimates"
        assert aapl["pre_market_score"] == "high"

    def test_watchlist_not_saved_for_midmorning(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _scan_response(["AAPL"]),
            _end_turn_response(),
        ]
        with patch("store.db.save_watchlist") as mock_save:
            generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS, session_type="midmorning")

        assert not mock_save.called
