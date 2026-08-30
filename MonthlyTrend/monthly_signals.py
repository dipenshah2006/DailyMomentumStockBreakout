"""
monthly_signals.py
==================
Signal logic for the Monthly MACD(12,26,9) + CCI(20)/SMA(20) breakout screener.

BUY  = Monthly MACD(12,26,9) bullish cross  AND  Monthly CCI(20) bullish cross SMA(20)

Trend classification (from monthly CCI20 level & momentum):
  - TREND_BEGINNING  : CCI(20) just crossed above SMA20, CCI in range -100 to +150
  - MEDIUM_BULLISH   : CCI(20) > 0 AND > SMA20 AND 0 < CCI <= 200, MACD bullish
  - STRONG_BULLISH   : CCI(20) > 200 AND > SMA20, MACD bullish

Trend metrics added to snapshot:
  - Trend_Start_Date   : date of most recent MACD or CCI bullish cross
  - Trend_Months       : months elapsed since trend start
  - Gain_Since_Start_Pct : % price gain from trend-start close to current close
  - Stop_Loss          : Low of the trend-start bar (trend beginning stop)
  - Risk_To_Stop_Pct   : % downside from current close to stop-loss
  - Upside_127_Pct     : % upside to 127.2% Fib extension
  - Upside_162_Pct     : % upside to 161.8% Fib extension
  - Upside_262_Pct     : % upside to 261.8% Fib extension
  - RR_127 / RR_162    : reward-to-risk ratio (upside% / risk%)
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import indicators as ind
import fib_analysis as fib
import stage_analysis as sta

# ── CCI-trend state constants ──────────────────────────────────────────────
TREND_BEGINNING = "Trend Beginning"
MEDIUM_BULLISH  = "Medium Bullish"
STRONG_BULLISH  = "Strong Bullish"
NO_TREND        = "No Trend"


def build_monthly_signal_table(daily: pd.DataFrame):
    """
    Returns (monthly_df, extras_dict).

    monthly_df  : monthly-resampled OHLCV + indicators
    extras_dict : snapshot values, signals, fib levels, stop-loss, trend metrics
    """
    mo = ind.monthly(daily)
    if len(mo) < 15:
        return mo, {}

    # ── MACD(12,26,9) on monthly ─────────────────────────────────────────
    macd_line, macd_sig, macd_hist = ind.macd(mo["Close"], fast=12, slow=26, signal=9)
    mo["MACD"]        = macd_line
    mo["MACD_Signal"] = macd_sig
    mo["MACD_Hist"]   = macd_hist

    # ── CCI(20) on monthly ───────────────────────────────────────────────
    mo["CCI20"]       = ind.cci(mo, 20)
    mo["CCI20_SMA20"] = ind.sma(mo["CCI20"], 20)

    # ── CCI(34) + SMA(14) for ROCKET_BUY signal ─────────────────────────
    mo["CCI34"]       = ind.cci(mo, 34)
    mo["CCI34_SMA14"] = ind.sma(mo["CCI34"], 14)

    # ── CCI(200) + SMA(20) for SUPER_BULLISH signal ───────────────────
    mo["CCI200"]       = ind.cci(mo, 200)
    mo["CCI200_SMA20"] = ind.sma(mo["CCI200"], 20)

    # ── RSI(14) on monthly ───────────────────────────────────────────────
    mo["RSI14"]       = ind.rsi(mo["Close"], 14)
    mo["RSI14_SMA14"] = ind.sma(mo["RSI14"], 14)

    # ── Cross signals ─────────────────────────────────────────────────────
    mo["MACD_Bull_Cross"]   = ind.crossed_above(mo["MACD"], mo["MACD_Signal"])
    mo["MACD_Bear_Cross"]   = ind.crossed_below(mo["MACD"], mo["MACD_Signal"])
    mo["CCI20_Bull_Cross"]  = ind.crossed_above(mo["CCI20"], mo["CCI20_SMA20"])
    mo["CCI20_Bear_Cross"]  = ind.crossed_below(mo["CCI20"], mo["CCI20_SMA20"])

    # CCI(34) bull-cross SMA(14)
    mo["CCI34_Bull_Cross"]  = ind.crossed_above(mo["CCI34"], mo["CCI34_SMA14"])

    # MACD crossed above zero line (MACD line itself crosses 0)
    mo["MACD_Zero_Cross"]   = ind.crossed_above(mo["MACD"],
                                                 pd.Series(0.0, index=mo.index))

    # CCI(200) cross signals
    mo["CCI200_Bull_Cross"] = ind.crossed_above(mo["CCI200"], mo["CCI200_SMA20"])
    mo["CCI200_Bear_Cross"] = ind.crossed_below(mo["CCI200"], mo["CCI200_SMA20"])

    # ── SUPER_BULLISH: CCI(200) > 100 AND bull-crosses SMA(20) ─────────────
    # Strict: crossover happening now AND CCI200 already > 100
    super_cross_now = mo["CCI200_Bull_Cross"] & (mo["CCI200"] > 100)
    # Confirmed: CCI200 > SMA20, CCI200 > 100, and cross happened in last 3 bars
    super_confirmed = (
        (mo["CCI200"] > mo["CCI200_SMA20"]) &
        (mo["CCI200"] > 100) &
        mo["CCI200_Bull_Cross"].rolling(3).max().fillna(0).astype(bool)
    )
    mo["Super_Bullish"] = super_cross_now | super_confirmed

    # ── Buy = BOTH cross bullish on same bar ──────────────────────────────
    mo["Buy_Signal"] = mo["MACD_Bull_Cross"] & mo["CCI20_Bull_Cross"]

    # ── Near-buy: either cross in last 3 months, both currently bullish ───
    mo["Near_Buy"] = (
        mo["MACD_Bull_Cross"].rolling(3).max().fillna(0).astype(bool) |
        mo["CCI20_Bull_Cross"].rolling(3).max().fillna(0).astype(bool)
    ) & (mo["MACD"] > mo["MACD_Signal"]) & (mo["CCI20"] > mo["CCI20_SMA20"])

    # ── ROCKET_BUY — high-conviction confluence signal ────────────────────
    # Rule 1: CCI(34) bull-crosses SMA(14)  AND  CCI(34) > -10 at cross
    cci34_cross_valid = mo["CCI34_Bull_Cross"] & (mo["CCI34"] >= -10)

    # Rule 2a: MACD(12,26,9) crossed above zero (strongest)
    macd_zero_ok = mo["MACD_Zero_Cross"]

    # Rule 2b: MACD near zero — crossed above in last 3 bars AND currently > 0
    macd_near_zero = (
        mo["MACD_Zero_Cross"].rolling(3).max().fillna(0).astype(bool)
        & (mo["MACD"] > 0)
    )

    # Strict: CCI34 cross AND MACD zero-line cross within same 2-bar window
    rocket_strict = (
        cci34_cross_valid.rolling(2).max().fillna(0).astype(bool) &
        (macd_zero_ok | macd_near_zero)
    )

    # Also catch: CCI34 currently > SMA14 AND > -10, MACD > 0 AND > Signal
    # (already in confirmed bullish posture from the cross)
    rocket_confirmed = (
        (mo["CCI34"] > mo["CCI34_SMA14"]) &
        (mo["CCI34"] >= -10) &
        (mo["MACD"] > 0) &
        (mo["MACD"] > mo["MACD_Signal"]) &
        # either a recent CCI34 cross or a recent MACD zero cross
        (cci34_cross_valid.rolling(3).max().fillna(0).astype(bool) |
         macd_near_zero)
    )

    mo["Rocket_Buy"] = rocket_strict | rocket_confirmed

    # ── Current values ────────────────────────────────────────────────────
    last    = mo.iloc[-1]
    close   = float(last["Close"])
    cci_now = last["CCI20"]       if pd.notna(last["CCI20"])       else None
    cci_sma = last["CCI20_SMA20"] if pd.notna(last["CCI20_SMA20"]) else None
    cci_prev= mo["CCI20"].iloc[-2] if len(mo) > 1 and pd.notna(mo["CCI20"].iloc[-2]) else None
    macd_bull = (bool(last["MACD"] > last["MACD_Signal"])
                 if pd.notna(last["MACD"]) and pd.notna(last["MACD_Signal"]) else False)

    trend_state = _classify_trend(cci_now, cci_sma, cci_prev, macd_bull)

    # ── Trend start: most recent MACD or CCI bullish cross ───────────────
    trend_start_date, trend_start_close, stop_loss = _find_trend_start(mo)

    # ── Trend duration & gain ─────────────────────────────────────────────
    trend_months = None
    gain_pct     = None
    if trend_start_date is not None:
        last_date    = mo.index[-1]
        trend_months = _months_between(trend_start_date, last_date)
        if trend_start_close and trend_start_close > 0:
            gain_pct = round((close - trend_start_close) / trend_start_close * 100, 2)

    # ── Risk to stop-loss ─────────────────────────────────────────────────
    risk_pct = None
    if stop_loss and stop_loss > 0 and close > 0:
        risk_pct = round((close - stop_loss) / close * 100, 2)   # positive = % above stop

    # ── Fibonacci (monthly, 24-bar lookback) ─────────────────────────────
    fib_levels     = ind.fibonacci_levels(mo, lookback=24)
    fib_extensions = {r: p for r, p in fib_levels.items() if r >= 1.0}

    # ── % upside to each fib extension from current close ────────────────
    def _upside(price):
        if price and close and close > 0:
            return round((price - close) / close * 100, 2)
        return None

    f127 = fib_extensions.get(1.272)
    f162 = fib_extensions.get(1.618)
    f262 = fib_extensions.get(2.618)

    up_127 = _upside(f127)
    up_162 = _upside(f162)
    up_262 = _upside(f262)

    # ── Reward-to-risk ratios ─────────────────────────────────────────────
    rr_127 = round(up_127 / risk_pct, 2) if (up_127 and risk_pct and risk_pct > 0) else None
    rr_162 = round(up_162 / risk_pct, 2) if (up_162 and risk_pct and risk_pct > 0) else None

    extras = {
        # signals
        "buy_signal":              bool(last["Buy_Signal"]),
        "near_buy":                bool(last["Near_Buy"]),
        "rocket_buy":              bool(last["Rocket_Buy"]),
        "macd_bull_cross_this_bar":bool(last["MACD_Bull_Cross"]),
        "macd_zero_cross":         bool(last["MACD_Zero_Cross"]),
        "cci_bull_cross_this_bar": bool(last["CCI20_Bull_Cross"]),
        "cci34_bull_cross":        bool(last["CCI34_Bull_Cross"]),
        "super_bullish":           bool(last["Super_Bullish"]),
        "cci200_bull_cross":       bool(last["CCI200_Bull_Cross"]),

        # trend classification
        "trend_state":      trend_state,

        # trend timeline
        "trend_start_date": trend_start_date.strftime("%Y-%m-%d") if trend_start_date is not None else None,
        "trend_months":     trend_months,
        "gain_since_start_pct": gain_pct,

        # stop loss
        "stop_loss":        round(float(stop_loss), 2) if stop_loss is not None else None,
        "risk_to_stop_pct": risk_pct,   # % price is ABOVE stop (positive = safe buffer)

        # fib upside targets
        "fib_127":          round(f127, 2) if f127 else None,
        "fib_162":          round(f162, 2) if f162 else None,
        "fib_262":          round(f262, 2) if f262 else None,
        "upside_127_pct":   up_127,
        "upside_162_pct":   up_162,
        "upside_262_pct":   up_262,
        "rr_127":           rr_127,
        "rr_162":           rr_162,

        # raw indicator values
        "monthly_macd":        round(float(last["MACD"]), 4)          if pd.notna(last["MACD"])          else None,
        "monthly_macd_signal": round(float(last["MACD_Signal"]), 4)   if pd.notna(last["MACD_Signal"])   else None,
        "monthly_macd_hist":   round(float(last["MACD_Hist"]), 4)     if pd.notna(last["MACD_Hist"])     else None,
        "monthly_cci20":       round(float(last["CCI20"]), 2)         if pd.notna(last["CCI20"])         else None,
        "monthly_cci_sma20":   round(float(last["CCI20_SMA20"]), 2)   if pd.notna(last["CCI20_SMA20"])   else None,
        "monthly_cci34":       round(float(last["CCI34"]), 2)         if pd.notna(last["CCI34"])         else None,
        "monthly_cci34_sma14": round(float(last["CCI34_SMA14"]), 2)   if pd.notna(last["CCI34_SMA14"])   else None,
        "monthly_cci200":      round(float(last["CCI200"]), 2)         if pd.notna(last["CCI200"])         else None,
        "monthly_cci200_sma20":round(float(last["CCI200_SMA20"]), 2)   if pd.notna(last["CCI200_SMA20"])   else None,
        "monthly_rsi14":       round(float(last["RSI14"]), 2)         if pd.notna(last["RSI14"])         else None,

        # full fib dicts for chart
        "fib_levels":     {str(r): round(p, 2) for r, p in fib_levels.items()},
        "fib_extensions": {str(r): round(p, 2) for r, p in fib_extensions.items()},
    }

    return mo, extras


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_trend_start(mo: pd.DataFrame):
    """
    Returns (trend_start_date, close_at_start, stop_loss_low).

    trend_start_date : index of the most recent MACD or CCI bullish cross bar
    close_at_start   : closing price on that bar (to measure gain since then)
    stop_loss_low    : Low of that bar (trend-beginning stop)
    """
    cross_mask = mo["MACD_Bull_Cross"] | mo["CCI20_Bull_Cross"]
    cross_bars = mo[cross_mask]

    if not cross_bars.empty:
        bar = cross_bars.iloc[-1]
        return bar.name, float(bar["Close"]), float(bar["Low"])

    # Fallback: first bar of the current continuous MACD-bullish run
    macd_pos = mo["MACD"] > mo["MACD_Signal"]
    if not macd_pos.iloc[-1]:
        return None, None, None

    start_idx = len(mo) - 1
    for i in range(len(mo) - 2, -1, -1):
        if not macd_pos.iloc[i]:
            break
        start_idx = i

    bar = mo.iloc[start_idx]
    return bar.name, float(bar["Close"]), float(bar["Low"])


def _months_between(start, end) -> int:
    """Approximate month count between two Timestamps."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    return max(0, (e.year - s.year) * 12 + (e.month - s.month))


def _classify_trend(cci, cci_sma, cci_prev, macd_bull: bool) -> str:
    if cci is None or cci_sma is None:
        return NO_TREND
    above_sma = cci > cci_sma
    if not above_sma:
        return NO_TREND
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


def _52week_stats(daily: pd.DataFrame, close: float) -> dict:
    """Compute 52-week high/low and % distance from current close."""
    last_date = daily.index[-1]
    start_52  = last_date - pd.DateOffset(weeks=52)
    sub = daily[daily.index >= start_52]
    if sub.empty:
        return {"W52_High": None, "W52_Low": None,
                "W52_High_Pct": None, "W52_Low_Pct": None}
    h = round(float(sub["High"].max()), 2)
    l = round(float(sub["Low"].min()), 2)
    # % above 52w low  (positive = how much above)
    low_pct  = round((close - l) / l * 100, 2) if l > 0 else None
    # % below 52w high (negative = how far below high, positive = above)
    high_pct = round((close - h) / h * 100, 2) if h > 0 else None
    return {
        "W52_High":     h,
        "W52_Low":      l,
        "W52_High_Pct": high_pct,   # negative = % below 52w high
        "W52_Low_Pct":  low_pct,    # positive = % above 52w low
    }


def latest_monthly_snapshot(symbol: str, daily: pd.DataFrame, mo: pd.DataFrame,
                             extras: dict) -> dict:
    """Builds the flat dict for the HTML report row."""
    close = round(float(daily.iloc[-1]["Close"]), 2)
    w52   = _52week_stats(daily, close)

    return {
        "Symbol":               symbol,
        "Close":                close,

        # 52-week stats
        "W52_High":             w52["W52_High"],
        "W52_High_Pct":         w52["W52_High_Pct"],   # % vs 52w high (negative = below)
        "W52_Low":              w52["W52_Low"],
        "W52_Low_Pct":          w52["W52_Low_Pct"],    # % vs 52w low (positive = above)

        # Trend info
        "Trend_State":          extras.get("trend_state", NO_TREND),
        "Trend_Start":          extras.get("trend_start_date"),
        "Trend_Months":         extras.get("trend_months"),
        "Gain_Pct":             extras.get("gain_since_start_pct"),

        # Stop loss
        "Stop_Loss":            extras.get("stop_loss"),
        "Risk_Pct":             extras.get("risk_to_stop_pct"),

        # Fib extension targets + upside %
        "Fib_127":              extras.get("fib_127"),
        "Upside_127_Pct":       extras.get("upside_127_pct"),
        "Fib_162":              extras.get("fib_162"),
        "Upside_162_Pct":       extras.get("upside_162_pct"),
        "Fib_262":              extras.get("fib_262"),
        "Upside_262_Pct":       extras.get("upside_262_pct"),

        # Reward:risk
        "RR_127":               extras.get("rr_127"),
        "RR_162":               extras.get("rr_162"),

        # Signals
        "Super_Bullish":        extras.get("super_bullish", False),
        "Rocket_Buy":           extras.get("rocket_buy", False),
        "Buy_Signal":           extras.get("buy_signal", False),
        "Near_Buy":             extras.get("near_buy", False),
        "MACD_Bull_Cross":      extras.get("macd_bull_cross_this_bar", False),
        "MACD_Zero_Cross":      extras.get("macd_zero_cross", False),
        "CCI20_Bull_Cross":     extras.get("cci_bull_cross_this_bar", False),
        "CCI34_Bull_Cross":     extras.get("cci34_bull_cross", False),

        # Indicators
        "Monthly_RSI14":        extras.get("monthly_rsi14"),
        "Monthly_CCI20":        extras.get("monthly_cci20"),
        "Monthly_CCI_SMA20":    extras.get("monthly_cci_sma20"),
        "Monthly_CCI34":        extras.get("monthly_cci34"),
        "Monthly_CCI34_SMA14":  extras.get("monthly_cci34_sma14"),
        "Monthly_CCI200":       extras.get("monthly_cci200"),
        "Monthly_CCI200_SMA20": extras.get("monthly_cci200_sma20"),
        "Monthly_MACD":         extras.get("monthly_macd"),
        "Monthly_MACD_Signal":  extras.get("monthly_macd_signal"),
        "Monthly_MACD_Hist":    extras.get("monthly_macd_hist"),

        # For chart
        "Fib_Extensions":       extras.get("fib_extensions", {}),
        "Fib_Levels":           extras.get("fib_levels", {}),
    }


def enrich_snapshot_with_stage_fib(snap: dict, daily: pd.DataFrame, mo: pd.DataFrame) -> dict:
    """
    Called after latest_monthly_snapshot — adds Stage Analysis and full
    Fibonacci analysis (retracement, extension, time zones) to the snapshot.
    """
    # ── Stage Analysis ────────────────────────────────────────────────────
    try:
        stage = sta.detect_stage(daily)
        snap["Stage"]           = stage.get("stage", sta.STAGE_1A)
        snap["Stage_Label"]     = stage.get("stage_label", "")
        snap["Stage_Icon"]      = stage.get("stage_icon", "⬜")
        snap["Stage_Color"]     = stage.get("stage_color", "#aaa")
        snap["Stage_Bg"]        = stage.get("stage_bg", "#f5f5f5")
        snap["Stage_Conviction"]= stage.get("conviction", "Low")
        snap["Stage_Notes"]     = stage.get("notes", "")
        snap["MA30W"]           = stage.get("ma30w")
        snap["Pct_Above_MA30"]  = stage.get("pct_above_ma30")
        snap["Slope_MA30_4W"]   = stage.get("slope_ma30_4w")
        snap["Vol_Ratio"]       = stage.get("vol_ratio")
        snap["Weekly_RSI"]      = stage.get("weekly_rsi")
        snap["Weeks_Above_MA30"]= stage.get("weeks_above_ma30")
        snap["Stage_Sort"]      = sta.stage_sort_key(stage.get("stage", sta.STAGE_1A))
    except Exception as e:
        snap["Stage"] = sta.STAGE_1A
        snap["Stage_Label"] = "Error"
        snap["Stage_Icon"] = "⬜"
        snap["Stage_Color"] = "#aaa"
        snap["Stage_Bg"] = "#f5f5f5"
        snap["Stage_Conviction"] = "Low"
        snap["Stage_Notes"] = str(e)
        snap["Stage_Sort"] = 99

    # ── Fibonacci Analysis ────────────────────────────────────────────────
    try:
        fib_data = fib.analyze_fibonacci(snap["Symbol"], mo, daily)
        snap["Fib_P1"]          = f"{fib_data.get('P1_date','?')} ₹{fib_data.get('P1_price','?')}" if fib_data else None
        snap["Fib_P2"]          = f"{fib_data.get('P2_date','?')} ₹{fib_data.get('P2_price','?')}" if fib_data else None
        snap["Fib_P3"]          = f"{fib_data.get('P3_date','?')} ₹{fib_data.get('P3_price','?')}" if fib_data else None
        snap["Fib_Ret_382"]     = fib_data.get("ret_382")   if fib_data else None
        snap["Fib_Ret_618"]     = fib_data.get("ret_618")   if fib_data else None
        snap["Fib_Ext_100"]     = fib_data.get("ext_100")   if fib_data else None
        snap["Fib_Ext_100_Pct"] = fib_data.get("ext_100_pct") if fib_data else None
        snap["Fib_Ext_162"]     = fib_data.get("ext_162")   if fib_data else None
        snap["Fib_Ext_162_Pct"] = fib_data.get("ext_162_pct") if fib_data else None
        snap["Fib_Ext_262"]     = fib_data.get("ext_262")   if fib_data else None
        snap["Fib_Next_TZ"]     = fib_data.get("next_tz_date") if fib_data else None
        snap["Fib_Next_TZ_Mo"]  = fib_data.get("next_tz_months") if fib_data else None
        snap["Fib_Ret_Pct"]     = fib_data.get("current_ret_pct") if fib_data else None
        snap["Fib_Data_JSON"]   = fib_data if fib_data else {}
    except Exception as e:
        snap["Fib_Data_JSON"] = {}

    return snap
