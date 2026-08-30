"""
monthly_signals.py
==================
Signal logic for the Monthly MACD(12,26,9) + CCI(20)/SMA(20) breakout screener.

BUY  = Monthly MACD(12,26,9) bullish cross  AND  Monthly CCI(20) bullish cross SMA(20)

Trend classification (from monthly CCI20 level & momentum):
  - TREND_BEGINNING  : CCI(20) just crossed above SMA20, CCI in range -100 to +100
  - MEDIUM_BULLISH   : CCI(20) > 0 AND > SMA20 AND 0 < CCI < 200
  - STRONG_BULLISH   : CCI(20) > 200 AND > SMA20

Stop-loss basis:
  Monthly trend beginning = the low of the monthly bar when the MACD or CCI bullish
  cross occurred (whichever is more recent), used as the hard stop level.

Fibonacci targets are computed on monthly swing high/low (lookback=24 months).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import indicators as ind

# ── CCI-trend state constants ──────────────────────────────────────────────
TREND_BEGINNING = "Trend Beginning"
MEDIUM_BULLISH  = "Medium Bullish"
STRONG_BULLISH  = "Strong Bullish"
NO_TREND        = "No Trend"


def build_monthly_signal_table(daily: pd.DataFrame):
    """
    Returns (monthly_df, monthly_macd_cross_dates, snapshot_extras).

    monthly_df  : monthly-resampled OHLCV + indicators
    """
    mo = ind.monthly(daily)
    if len(mo) < 30:
        return mo, [], {}

    # ── MACD(12,26,9) on monthly ────────────────────────────────────────────
    macd_line, macd_sig, macd_hist = ind.macd(mo["Close"], fast=12, slow=26, signal=9)
    mo["MACD"]        = macd_line
    mo["MACD_Signal"] = macd_sig
    mo["MACD_Hist"]   = macd_hist

    # ── CCI(20) on monthly ──────────────────────────────────────────────────
    mo["CCI20"]       = ind.cci(mo, 20)
    mo["CCI20_SMA20"] = ind.sma(mo["CCI20"], 20)

    # ── RSI(14) on monthly (for reference) ──────────────────────────────────
    mo["RSI14"]       = ind.rsi(mo["Close"], 14)
    mo["RSI14_SMA14"] = ind.sma(mo["RSI14"], 14)

    # ── Cross signals ────────────────────────────────────────────────────────
    mo["MACD_Bull_Cross"] = ind.crossed_above(mo["MACD"], mo["MACD_Signal"])
    mo["MACD_Bear_Cross"] = ind.crossed_below(mo["MACD"], mo["MACD_Signal"])
    mo["CCI20_Bull_Cross"] = ind.crossed_above(mo["CCI20"], mo["CCI20_SMA20"])
    mo["CCI20_Bear_Cross"] = ind.crossed_below(mo["CCI20"], mo["CCI20_SMA20"])

    # ── Buy = BOTH cross bullish on same bar ─────────────────────────────────
    mo["Buy_Signal"]  = mo["MACD_Bull_Cross"] & mo["CCI20_Bull_Cross"]

    # ── Near-buy: either cross in last 3 months (more lenient catch) ─────────
    mo["Near_Buy"]    = (
        mo["MACD_Bull_Cross"].rolling(3).max().fillna(0).astype(bool) |
        mo["CCI20_Bull_Cross"].rolling(3).max().fillna(0).astype(bool)
    ) & (mo["MACD"] > mo["MACD_Signal"]) & (mo["CCI20"] > mo["CCI20_SMA20"])

    # ── CCI-based trend state (latest bar) ───────────────────────────────────
    last = mo.iloc[-1]
    cci_now  = last["CCI20"]   if pd.notna(last["CCI20"])   else None
    cci_sma  = last["CCI20_SMA20"] if pd.notna(last["CCI20_SMA20"]) else None
    cci_prev = mo["CCI20"].iloc[-2] if len(mo) > 1 and pd.notna(mo["CCI20"].iloc[-2]) else None

    trend_state = _classify_trend(cci_now, cci_sma, cci_prev,
                                  bool(last["MACD"] > last["MACD_Signal"])
                                  if pd.notna(last["MACD"]) and pd.notna(last["MACD_Signal"]) else False)

    # ── Stop-loss: low of the bar that triggered the most recent bullish cross ─
    stop_loss = _find_stop_loss(mo)

    # ── Fibonacci (monthly, 24-bar lookback) ─────────────────────────────────
    fib_levels = ind.fibonacci_levels(mo, lookback=24)

    # ── Fibonacci extension targets (ratio > 1.0) for the report ─────────────
    fib_extensions = {r: p for r, p in fib_levels.items() if r >= 1.0}

    extras = {
        "trend_state":    trend_state,
        "stop_loss":      round(float(stop_loss), 2) if stop_loss is not None else None,
        "fib_levels":     {str(r): round(p, 2) for r, p in fib_levels.items()},
        "fib_extensions": {str(r): round(p, 2) for r, p in fib_extensions.items()},
        "monthly_macd":   round(float(last["MACD"]), 4)    if pd.notna(last["MACD"])   else None,
        "monthly_macd_signal": round(float(last["MACD_Signal"]), 4) if pd.notna(last["MACD_Signal"]) else None,
        "monthly_macd_hist":   round(float(last["MACD_Hist"]), 4)   if pd.notna(last["MACD_Hist"])   else None,
        "monthly_cci20":  round(float(last["CCI20"]), 2)   if pd.notna(last["CCI20"])  else None,
        "monthly_cci_sma20": round(float(last["CCI20_SMA20"]), 2) if pd.notna(last["CCI20_SMA20"]) else None,
        "monthly_rsi14":  round(float(last["RSI14"]), 2)   if pd.notna(last["RSI14"])  else None,
        "buy_signal":     bool(last["Buy_Signal"]),
        "near_buy":       bool(last["Near_Buy"]),
        "macd_bull_cross_this_bar": bool(last["MACD_Bull_Cross"]),
        "cci_bull_cross_this_bar":  bool(last["CCI20_Bull_Cross"]),
    }

    return mo, extras


def _classify_trend(cci: float | None, cci_sma: float | None,
                    cci_prev: float | None, macd_bull: bool) -> str:
    """CCI-based trend classification."""
    if cci is None or cci_sma is None:
        return NO_TREND
    above_sma = cci > cci_sma
    if not above_sma:
        return NO_TREND
    # rising CCI
    rising = (cci > cci_prev) if cci_prev is not None else True
    if cci > 200 and macd_bull:
        return STRONG_BULLISH
    if 0 < cci <= 200 and macd_bull:
        return MEDIUM_BULLISH
    if -100 <= cci <= 150 and rising:
        return TREND_BEGINNING
    if above_sma and rising:
        return MEDIUM_BULLISH
    return NO_TREND


def _find_stop_loss(mo: pd.DataFrame) -> float | None:
    """
    Returns the Low of the most recent bar where MACD or CCI had a bullish
    cross - this is the 'trend beginning' stop loss level.
    """
    cross_bars = mo[mo["MACD_Bull_Cross"] | mo["CCI20_Bull_Cross"]]
    if cross_bars.empty:
        # Fall back: low of the bar where MACD first went positive in current run
        positive = mo[mo["MACD"] > mo["MACD_Signal"]]
        if positive.empty:
            return None
        # First bar of the most recent continuous positive run
        macd_pos = mo["MACD"] > mo["MACD_Signal"]
        # find start of current run
        for i in range(len(mo) - 1, -1, -1):
            if not macd_pos.iloc[i]:
                start = i + 1
                if start < len(mo):
                    return float(mo["Low"].iloc[start])
                return None
        return float(mo["Low"].iloc[0])
    return float(cross_bars["Low"].iloc[-1])


def latest_monthly_snapshot(symbol: str, daily: pd.DataFrame, mo: pd.DataFrame,
                             extras: dict) -> dict:
    """Builds the flat dict for the HTML report row."""
    last_daily = daily.iloc[-1]
    close = round(float(last_daily["Close"]), 2)

    return {
        "Symbol":             symbol,
        "Close":              close,
        "Monthly_RSI14":      extras.get("monthly_rsi14"),
        "Monthly_CCI20":      extras.get("monthly_cci20"),
        "Monthly_CCI_SMA20":  extras.get("monthly_cci_sma20"),
        "Monthly_MACD":       extras.get("monthly_macd"),
        "Monthly_MACD_Signal":extras.get("monthly_macd_signal"),
        "Monthly_MACD_Hist":  extras.get("monthly_macd_hist"),
        "Trend_State":        extras.get("trend_state", NO_TREND),
        "Buy_Signal":         extras.get("buy_signal", False),
        "Near_Buy":           extras.get("near_buy", False),
        "MACD_Bull_Cross":    extras.get("macd_bull_cross_this_bar", False),
        "CCI20_Bull_Cross":   extras.get("cci_bull_cross_this_bar", False),
        "Stop_Loss":          extras.get("stop_loss"),
        "Fib_Extensions":     extras.get("fib_extensions", {}),
        "Fib_Levels":         extras.get("fib_levels", {}),
    }
