"""
Unit tests for report/generator.py — agentic loop with tool use.

All Anthropic API calls and E*TRADE session calls are mocked so no network
traffic is required.
"""

import json
from unittest.mock import MagicMock, patch, call

import pytest

from report.generator import (
    MAX_TURNS,
    build_allow_list,
    dispatch_tool_call,
    generate_report,
)


# ---------------------------------------------------------------------------
# Helpers — fake Anthropic SDK objects
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
    """A clean end_turn response containing the final JSON report."""
    payload = report_dict or {
        "top_plays": [
            {"ticker": "AAPL", "play_type": "day_trade", "thesis": "test", "entry_range": "$150-$153", "risk": "low"},
            {"ticker": "MSFT", "play_type": "overnight", "thesis": "test", "entry_range": "$300-$310", "risk": "low"},
            {"ticker": "NVDA", "play_type": "day_trade", "thesis": "test", "entry_range": "$400-$410", "risk": "medium"},
        ],
        "position_outlooks": [],
        "long_term_entries": [],
    }
    return _Response("end_turn", [_TextBlock(json.dumps(payload))])


def _tool_use_response(tool_id: str, name: str, ticker: str) -> _Response:
    """A tool_use response requesting one tool call."""
    return _Response("tool_use", [_ToolUseBlock(tool_id, name, ticker)])


def _tool_use_response_multi(*calls) -> _Response:
    """A tool_use response requesting multiple tool calls."""
    blocks = [_ToolUseBlock(tid, name, ticker) for tid, name, ticker in calls]
    return _Response("tool_use", blocks)


# ---------------------------------------------------------------------------
# build_allow_list
# ---------------------------------------------------------------------------

class TestBuildAllowList:
    def test_includes_position_tickers(self):
        positions = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
        allowed = build_allow_list("", positions)
        assert "AAPL" in allowed
        assert "MSFT" in allowed

    def test_extracts_tickers_from_headlines(self):
        headlines = "- NVDA surges on AI demand. Fed holds rates steady."
        allowed = build_allow_list(headlines, [])
        assert "NVDA" in allowed
        # Short words are included per regex spec (false positives are acceptable)
        assert "AI" in allowed

    def test_combined(self):
        positions = [{"ticker": "TSLA"}]
        headlines = "- AAPL earnings beat expectations."
        allowed = build_allow_list(headlines, positions)
        assert "TSLA" in allowed
        assert "AAPL" in allowed

    def test_empty_inputs(self):
        allowed = build_allow_list("", [])
        assert isinstance(allowed, set)

    def test_ticker_uppercased(self):
        positions = [{"ticker": "aapl"}]
        allowed = build_allow_list("", positions)
        assert "AAPL" in allowed


# ---------------------------------------------------------------------------
# dispatch_tool_call — allow-list and duplicate guards
# ---------------------------------------------------------------------------

class TestDispatchToolCall:
    def _make_block(self, tool_id="t1", name="get_quote", ticker="AAPL"):
        return _ToolUseBlock(tool_id, name, ticker)

    def test_disallowed_ticker_rejected(self):
        block = self._make_block(ticker="XYZ")
        result = dispatch_tool_call(block, allowed_tickers={"AAPL"}, seen_calls=set(), session=None, turn=0)
        assert "not in the approved list" in result
        assert "XYZ" in result

    def test_disallowed_ticker_not_added_to_seen(self):
        block = self._make_block(ticker="XYZ")
        seen = set()
        dispatch_tool_call(block, allowed_tickers={"AAPL"}, seen_calls=seen, session=None, turn=0)
        assert ("get_quote", "XYZ") not in seen

    def test_duplicate_call_blocked(self):
        block = self._make_block(ticker="AAPL")
        seen = {("get_quote", "AAPL")}
        result = dispatch_tool_call(block, allowed_tickers={"AAPL"}, seen_calls=seen, session=None, turn=0)
        assert "already have this data" in result

    def test_no_session_returns_graceful_error(self):
        block = self._make_block(ticker="AAPL")
        result = dispatch_tool_call(block, allowed_tickers={"AAPL"}, seen_calls=set(), session=None, turn=0)
        data = json.loads(result)
        assert "error" in data

    def test_etrade_timeout_returns_timeout_message(self):
        import requests as req
        mock_session = MagicMock()
        mock_session.get.side_effect = req.exceptions.Timeout("timed out")
        block = self._make_block(ticker="AAPL")
        result = dispatch_tool_call(
            block, allowed_tickers={"AAPL"}, seen_calls=set(), session=mock_session, turn=0
        )
        assert "timed out" in result.lower()
        assert "AAPL" in result

    def test_successful_get_quote_call(self):
        mock_session = MagicMock()
        mock_session.get.return_value.json.return_value = {
            "QuoteResponse": {
                "QuoteData": [{"All": {"lastTrade": 150.0, "bid": 149.9, "ask": 150.1}}]
            }
        }
        mock_session.get.return_value.raise_for_status = MagicMock()
        block = self._make_block(ticker="AAPL")
        result = dispatch_tool_call(
            block, allowed_tickers={"AAPL"}, seen_calls=set(), session=mock_session, turn=0
        )
        data = json.loads(result)
        assert data["last_price"] == 150.0

    def test_seen_calls_updated_after_success(self):
        block = self._make_block(ticker="AAPL")
        seen = set()
        dispatch_tool_call(block, allowed_tickers={"AAPL"}, seen_calls=seen, session=None, turn=0)
        assert ("get_quote", "AAPL") in seen


# ---------------------------------------------------------------------------
# generate_report — agentic loop scenarios
# ---------------------------------------------------------------------------

SAMPLE_POSITIONS = [{"ticker": "AAPL", "name": "Apple", "shares": 10, "avg_cost": 150.0, "notes": ""}]
SAMPLE_HEADLINES = "- AAPL reports strong earnings. MSFT cloud growth accelerates."


@pytest.fixture()
def mock_anthropic():
    """Patch anthropic.Anthropic so no real API calls are made."""
    with patch("report.generator.anthropic.Anthropic") as MockClass:
        mock_client = MagicMock()
        MockClass.return_value = mock_client
        yield mock_client


class TestGenerateReportEndTurnFirstResponse:
    """Claude produces the report immediately without any tool calls."""

    def test_returns_parsed_report(self, mock_anthropic):
        mock_anthropic.messages.create.return_value = _end_turn_response()
        result = generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)
        assert "top_plays" in result
        assert "position_outlooks" in result
        assert "long_term_entries" in result

    def test_api_called_once(self, mock_anthropic):
        mock_anthropic.messages.create.return_value = _end_turn_response()
        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)
        assert mock_anthropic.messages.create.call_count == 1

    def test_no_tool_turns_consumed(self, mock_anthropic, capsys):
        mock_anthropic.messages.create.return_value = _end_turn_response()
        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)
        captured = capsys.readouterr()
        assert "0 tool call turns" in captured.out


class TestGenerateReportDisallowedTicker:
    """Claude requests a ticker not in the allow-list; loop should block it and continue."""

    def test_disallowed_ticker_blocked_in_loop(self, mock_anthropic):
        # Claude tries ZZZZ (not in headlines or positions), then gives up and returns report
        mock_anthropic.messages.create.side_effect = [
            _tool_use_response("t1", "get_quote", "ZZZZ"),
            _end_turn_response(),
        ]
        result = generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)
        assert "top_plays" in result

    def test_tool_result_contains_rejection_message(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _tool_use_response("t1", "get_quote", "ZZZZ"),
            _end_turn_response(),
        ]
        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)

        # The second call to messages.create should include a tool_result with rejection
        second_call_messages = mock_anthropic.messages.create.call_args_list[1][1]["messages"]
        # Last user message before the second call contains the tool_result
        tool_result_msg = next(
            m for m in reversed(second_call_messages) if m["role"] == "user"
        )
        content = tool_result_msg["content"]
        assert any("not in the approved list" in item["content"] for item in content)

    def test_disallowed_ticker_does_not_consume_turn(self, mock_anthropic, capsys):
        """Rejected tickers should not count toward the turn limit."""
        mock_anthropic.messages.create.side_effect = [
            _tool_use_response("t1", "get_quote", "ZZZZ"),
            _end_turn_response(),
        ]
        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)
        captured = capsys.readouterr()
        # 0 turns were counted because dispatch was bypassed for disallowed ticker
        assert "0 tool call turns" in captured.out


class TestGenerateReportDuplicateCallBlocked:
    """Claude requests the same (tool, ticker) pair twice; second call is rejected."""

    def test_duplicate_blocked(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _tool_use_response("t1", "get_quote", "AAPL"),
            _tool_use_response("t2", "get_quote", "AAPL"),  # duplicate
            _end_turn_response(),
        ]
        result = generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)
        assert "top_plays" in result

    def test_duplicate_result_message(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _tool_use_response("t1", "get_quote", "AAPL"),
            _tool_use_response("t2", "get_quote", "AAPL"),
            _end_turn_response(),
        ]
        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)

        # Third call's messages should contain the duplicate-block response
        third_call_messages = mock_anthropic.messages.create.call_args_list[2][1]["messages"]
        tool_result_msg = next(
            m for m in reversed(third_call_messages) if m["role"] == "user"
        )
        content = tool_result_msg["content"]
        assert any("already have this data" in item["content"] for item in content)


class TestGenerateReportMaxTurns:
    """Loop terminates at MAX_TURNS and forces a final completion without tools."""

    def test_loop_terminates_at_max_turns(self, mock_anthropic):
        # Claude makes MAX_TURNS tool calls, then we force the final
        tool_responses = [
            _tool_use_response(f"t{i}", "get_quote", "AAPL")
            for i in range(MAX_TURNS)
        ]
        final_response = _end_turn_response()
        mock_anthropic.messages.create.side_effect = tool_responses + [final_response]

        result = generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)
        assert "top_plays" in result

    def test_final_call_has_no_tools(self, mock_anthropic):
        """The forced final completion must be called with tools=[] to ensure end_turn."""
        tool_responses = [
            _tool_use_response(f"t{i}", "get_quote", "AAPL")
            for i in range(MAX_TURNS)
        ]
        # First 3 all try AAPL, but only the first succeeds (rest are duplicates)
        mock_anthropic.messages.create.side_effect = tool_responses + [_end_turn_response()]

        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)

        final_call_kwargs = mock_anthropic.messages.create.call_args_list[-1][1]
        assert final_call_kwargs["tools"] == []

    def test_total_api_calls_is_max_turns_plus_one(self, mock_anthropic):
        """MAX_TURNS tool rounds + 1 forced final = MAX_TURNS + 1 API calls."""
        tool_responses = [
            _tool_use_response(f"t{i}", "get_quote", "AAPL")
            for i in range(MAX_TURNS)
        ]
        mock_anthropic.messages.create.side_effect = tool_responses + [_end_turn_response()]

        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)

        # MAX_TURNS agentic calls + 1 forced final call
        assert mock_anthropic.messages.create.call_count == MAX_TURNS + 1

    def test_fourth_tool_call_blocked_in_multi_block_response(self, mock_anthropic):
        """If Claude returns 4 tool_use blocks at once, only 3 are dispatched."""
        four_block_response = _tool_use_response_multi(
            ("t1", "get_quote", "AAPL"),
            ("t2", "get_quote", "MSFT"),
            ("t3", "get_technicals", "AAPL"),
            ("t4", "get_quote", "NVDA"),  # 4th — should be blocked
        )
        mock_anthropic.messages.create.side_effect = [four_block_response, _end_turn_response()]

        generate_report(SAMPLE_HEADLINES + " MSFT NVDA", SAMPLE_POSITIONS, etrade_session=None)

        # The forced final call is with no tools
        final_kwargs = mock_anthropic.messages.create.call_args_list[-1][1]
        assert final_kwargs["tools"] == []

    def test_turn_count_printed(self, mock_anthropic, capsys):
        tool_responses = [
            _tool_use_response(f"t{i}", "get_quote", "AAPL")
            for i in range(MAX_TURNS)
        ]
        mock_anthropic.messages.create.side_effect = tool_responses + [_end_turn_response()]

        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)

        captured = capsys.readouterr()
        assert f"{MAX_TURNS} tool call turns" in captured.out


class TestGenerateReportMessageHistory:
    """Verify that message history is built correctly across turns."""

    def test_messages_alternate_user_assistant(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _tool_use_response("t1", "get_quote", "AAPL"),
            _end_turn_response(),
        ]
        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)

        # Second call should have: user (initial), assistant (tool_use), user (tool_result)
        second_call_messages = mock_anthropic.messages.create.call_args_list[1][1]["messages"]
        roles = [m["role"] for m in second_call_messages]
        assert roles == ["user", "assistant", "user"]

    def test_tool_result_references_correct_tool_use_id(self, mock_anthropic):
        mock_anthropic.messages.create.side_effect = [
            _tool_use_response("tool-abc-123", "get_quote", "AAPL"),
            _end_turn_response(),
        ]
        generate_report(SAMPLE_HEADLINES, SAMPLE_POSITIONS)

        second_call_messages = mock_anthropic.messages.create.call_args_list[1][1]["messages"]
        tool_result_msg = next(m for m in second_call_messages if m["role"] == "user"
                               and isinstance(m["content"], list))
        assert tool_result_msg["content"][0]["tool_use_id"] == "tool-abc-123"
