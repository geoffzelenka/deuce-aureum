"""
Tool implementations for the report generator's agentic loop.

All three Claude tools live here:
  get_quote_data        — live E*TRADE single-ticker quote
  get_technicals_data   — SMA-20/50/200 and avg volume from Yahoo Finance (RSI stub)
  get_options_flow_data — E*TRADE options chain summary with unusual-activity flag
"""

import statistics

import requests

import config


def get_quote_data(ticker: str, etrade_session) -> dict:
    """
    Fetch a single-ticker quote from E*TRADE with a 5-second timeout.

    Returns a dict of price/volume fields.  Never raises — a missing session
    returns {"error": "..."}.
    """
    if etrade_session is None:
        return {"error": "No E*TRADE session available — proceeding with headlines only."}

    try:
        url = f"{config.BASE_URL}/v1/market/quote/{ticker}"
        resp = etrade_session.get(
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
    except Exception as exc:
        return {"error": f"Quote request failed for {ticker}: {str(exc)[:120]}"}


def get_technicals_data(ticker: str) -> dict:
    """
    Compute SMA-20/50/200 and average daily volume (30-day) from Yahoo Finance.

    RSI-14 is returned as null until a proper data source is integrated.
    Never raises — errors are returned as {"error": "..."}.
    """
    try:
        YF_URL = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
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
    except Exception as exc:
        return {"error": f"Technicals request failed for {ticker}: {str(exc)[:120]}"}


def get_options_flow_data(ticker: str, etrade_session) -> dict:
    """
    Fetch options flow summary for a ticker using E*TRADE Options Chain endpoint.

    Calls GET /v1/market/optionchains for the nearest two expiries and computes:
    - total call volume / OI
    - total put volume / OI
    - put/call ratio
    - largest single options trade by volume × last price
    - unusual_activity flag (True if put/call ratio outside 0.4–1.8 or
      largest trade premium > $500k)

    Returns a dict suitable for JSON serialisation.  Never raises — errors are
    returned as {"error": "..."}.
    """
    if etrade_session is None:
        return {"error": "No E*TRADE session available for options flow."}

    url = f"{config.BASE_URL}/v1/market/optionchains"
    try:
        resp = etrade_session.get(
            url,
            params={
                "symbol": ticker,
                "chainType": "CALLPUT",
                "optionCategory": "STANDARD",
                "priceType": "ALL",
                "noOfStrikes": 10,
                "expiryCount": 2,
            },
            headers={"Accept": "application/json"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {"error": f"Options chain request failed for {ticker}: {str(exc)[:120]}"}

    option_pairs = (
        data.get("OptionChainResponse", {}).get("OptionPair", []) or []
    )

    total_call_vol = 0
    total_put_vol = 0
    total_call_oi = 0
    total_put_oi = 0
    largest_premium = 0.0
    largest_trade = None

    for pair in option_pairs:
        call = pair.get("Call") or {}
        put = pair.get("Put") or {}

        call_vol = int(call.get("volume") or 0)
        call_oi = int(call.get("openInterest") or 0)
        put_vol = int(put.get("volume") or 0)
        put_oi = int(put.get("openInterest") or 0)

        total_call_vol += call_vol
        total_call_oi += call_oi
        total_put_vol += put_vol
        total_put_oi += put_oi

        # Identify largest single trade: volume × last price × 100 (one contract = 100 shares)
        for opt, opt_type in ((call, "call"), (put, "put")):
            last = float(opt.get("lastPrice") or 0)
            vol = int(opt.get("volume") or 0)
            premium = vol * last * 100
            if premium > largest_premium:
                largest_premium = premium
                largest_trade = {
                    "strike": opt.get("strikePrice"),
                    "expiry": opt.get("expirationDate"),
                    "premium": round(premium, 2),
                    "type": opt_type,
                }

    put_call_ratio: float | None = None
    if total_call_vol > 0:
        put_call_ratio = round(total_put_vol / total_call_vol, 3)

    unusual_activity = False
    if put_call_ratio is not None and (put_call_ratio < 0.4 or put_call_ratio > 1.8):
        unusual_activity = True
    if largest_premium > 500_000:
        unusual_activity = True

    return {
        "total_call_volume": total_call_vol,
        "total_put_volume": total_put_vol,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "put_call_ratio": put_call_ratio,
        "largest_trade": largest_trade,
        "unusual_activity": unusual_activity,
    }
