"""
stage_analysis.py
=================
Stan Weinstein Stage Analysis — detects which of the 4 stages a stock is in,
using weekly data (Weinstein's preferred timeframe) with monthly confirmation.

Stage definitions:
  Stage 1  — Basing / Accumulation
    Price flat/sideways, 30-week MA flattening, volume declining, RS neutral
    Substages: 1A (early base), 1B (late base / coiling)

  Stage 2  — Advancing / Markup
    Price above rising 30-week MA, MA slopes up, volume on rallies > declines
    Substages: 2A (early advance — best entry), 2B (mid advance), 2C (late — extended)

  Stage 3  — Topping / Distribution
    Price sideways near highs, 30-week MA flattening at top, churning volume
    Substages: 3A (early top), 3B (late top / breakdown imminent)

  Stage 4  — Declining / Markdown
    Price below declining 30-week MA, MA slopes down, volume on declines
    Substages: 4A (early decline), 4B (late decline / capitulation)

Key indicators used:
  - 30-week SMA (MA30W) — the central Weinstein indicator
  - 10-week SMA (MA10W) — short-term trend
  - Weekly RSI(14)
  - Volume ratio (recent avg / longer avg)
  - MA slope (direction and acceleration)
  - Price position vs MA30W
  - Distance from 52-week high/low
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import indicators as ind

# ── Stage constants ────────────────────────────────────────────────────────────
STAGE_1A = "Stage 1A"   # Early base — flat MA, low volume
STAGE_1B = "Stage 1B"   # Late base — MA curling, coiling
STAGE_2A = "Stage 2A"   # Early advance — best buy zone ✅
STAGE_2B = "Stage 2B"   # Mid advance — trending
STAGE_2C = "Stage 2C"   # Late advance — extended, caution
STAGE_3A = "Stage 3A"   # Early top — distribution
STAGE_3B = "Stage 3B"   # Late top — breakdown near
STAGE_4A = "Stage 4A"   # Early decline
STAGE_4B = "Stage 4B"   # Late decline / capitulation

STAGE_ORDER = {
    STAGE_1A: 1, STAGE_1B: 2,
    STAGE_2A: 3, STAGE_2B: 4, STAGE_2C: 5,
    STAGE_3A: 6, STAGE_3B: 7,
    STAGE_4A: 8, STAGE_4B: 9,
}

# Icons and colors for the report
STAGE_META = {
    STAGE_1A: {"icon": "⬜", "color": "#90a4ae", "bg": "#eceff1", "class": "s1a", "label": "Stage 1A — Early Base"},
    STAGE_1B: {"icon": "🔵", "color": "#1565c0", "bg": "#e3f2fd", "class": "s1b", "label": "Stage 1B — Late Base"},
    STAGE_2A: {"icon": "🟢", "color": "#1b5e20", "bg": "#e8f5e9", "class": "s2a", "label": "Stage 2A — Early Advance ✅"},
    STAGE_2B: {"icon": "🟩", "color": "#2e7d32", "bg": "#c8e6c9", "class": "s2b", "label": "Stage 2B — Mid Advance"},
    STAGE_2C: {"icon": "🟡", "color": "#e65100", "bg": "#fff3e0", "class": "s2c", "label": "Stage 2C — Late Advance ⚠️"},
    STAGE_3A: {"icon": "🟠", "color": "#bf360c", "bg": "#fbe9e7", "class": "s3a", "label": "Stage 3A — Early Top"},
    STAGE_3B: {"icon": "🔴", "color": "#b71c1c", "bg": "#ffebee", "class": "s3b", "label": "Stage 3B — Distribution"},
    STAGE_4A: {"icon": "⬛", "color": "#4a148c", "bg": "#f3e5f5", "class": "s4a", "label": "Stage 4A — Early Decline"},
    STAGE_4B: {"icon": "🟣", "color": "#6a1b9a", "bg": "#ede7f6", "class": "s4b", "label": "Stage 4B — Capitulation"},
}


def _ma_slope(series: pd.Series, n: int = 4) -> float:
    """Slope of last n bars of a series, normalised to % per bar."""
    s = series.dropna()
    if len(s) < n + 1:
        return 0.0
    vals = s.iloc[-n:].values.astype(float)
    if vals[0] == 0:
        return 0.0
    return float((vals[-1] - vals[0]) / vals[0] / n * 100)


def _vol_ratio(volume: pd.Series, short: int = 4, long: int = 26) -> float:
    """Ratio of short-term avg volume to long-term avg volume."""
    v = volume.dropna()
    if len(v) < long:
        return 1.0
    va_short = float(v.iloc[-short:].mean())
    va_long  = float(v.iloc[-long:].mean())
    return round(va_short / va_long, 2) if va_long > 0 else 1.0


def detect_stage(daily: pd.DataFrame) -> dict:
    """
    Detects Weinstein stage from weekly data derived from daily.
    Returns a dict with stage, substage, conviction, and supporting data.
    """
    # Resample to weekly
    wk = ind.weekly(daily)
    if len(wk) < 35:
        return _empty_stage()

    close    = wk["Close"]
    high     = wk["High"]
    low      = wk["Low"]
    volume   = wk["Volume"]

    # ── Core MAs ─────────────────────────────────────────────────────────────
    ma10  = ind.sma(close, 10)
    ma30  = ind.sma(close, 30)
    ma40  = ind.sma(close, 40)   # for extra confirmation

    last_close = float(close.iloc[-1])
    last_ma10  = float(ma10.iloc[-1])  if pd.notna(ma10.iloc[-1])  else None
    last_ma30  = float(ma30.iloc[-1])  if pd.notna(ma30.iloc[-1])  else None
    last_ma40  = float(ma40.iloc[-1])  if pd.notna(ma40.iloc[-1])  else None

    if last_ma30 is None:
        return _empty_stage()

    # ── Slopes ───────────────────────────────────────────────────────────────
    slope_ma30_4w  = _ma_slope(ma30, 4)    # 4-week slope
    slope_ma30_13w = _ma_slope(ma30, 13)   # 13-week slope (quarterly trend)
    slope_ma10_4w  = _ma_slope(ma10, 4)

    # ── Price position relative to MA30 ──────────────────────────────────────
    pct_above_ma30 = (last_close - last_ma30) / last_ma30 * 100

    # ── 52-week high/low ─────────────────────────────────────────────────────
    sub52 = wk.iloc[-52:] if len(wk) >= 52 else wk
    high52 = float(sub52["High"].max())
    low52  = float(sub52["Low"].min())
    pct_from_52h = (last_close - high52) / high52 * 100
    pct_from_52l = (last_close - low52)  / low52  * 100

    # ── Volume ratio ─────────────────────────────────────────────────────────
    vol_ratio_4_26 = _vol_ratio(volume, 4, 26)

    # ── RSI ──────────────────────────────────────────────────────────────────
    rsi14 = ind.rsi(close, 14)
    last_rsi = float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else 50.0

    # ── MA30 flatness: std of last 13 weeks of MA30 / mean ───────────────────
    ma30_recent = ma30.iloc[-13:].dropna()
    ma30_cv = float(ma30_recent.std() / ma30_recent.mean() * 100) if len(ma30_recent) > 3 and ma30_recent.mean() != 0 else 0

    # ── How long above/below MA30 consecutively ───────────────────────────────
    above_ma30 = close > ma30
    runs_above = 0
    for v in reversed(above_ma30.values):
        if v:
            runs_above += 1
        else:
            break
    runs_below = 0
    for v in reversed(above_ma30.values):
        if not v:
            runs_below += 1
        else:
            break

    # ── MA10 vs MA30 ─────────────────────────────────────────────────────────
    ma10_above_ma30 = last_ma10 > last_ma30 if last_ma10 else False

    # ── CLASSIFY ─────────────────────────────────────────────────────────────
    stage, conviction, notes = _classify(
        last_close, last_ma30, last_ma10,
        slope_ma30_4w, slope_ma30_13w, slope_ma10_4w,
        pct_above_ma30, pct_from_52h, pct_from_52l,
        vol_ratio_4_26, last_rsi, ma30_cv,
        runs_above, runs_below, ma10_above_ma30
    )

    meta = STAGE_META.get(stage, STAGE_META[STAGE_1A])

    return {
        "stage":           stage,
        "stage_label":     meta["label"],
        "stage_icon":      meta["icon"],
        "stage_color":     meta["color"],
        "stage_bg":        meta["bg"],
        "stage_class":     meta["class"],
        "conviction":      conviction,          # "High" | "Medium" | "Low"
        "notes":           notes,
        # Raw data for display
        "close":           round(last_close, 2),
        "ma30w":           round(last_ma30, 2),
        "ma10w":           round(last_ma10, 2) if last_ma10 else None,
        "pct_above_ma30":  round(pct_above_ma30, 1),
        "slope_ma30_4w":   round(slope_ma30_4w, 3),
        "slope_ma30_13w":  round(slope_ma30_13w, 3),
        "vol_ratio":       vol_ratio_4_26,
        "weekly_rsi":      round(last_rsi, 1),
        "pct_from_52h":    round(pct_from_52h, 1),
        "pct_from_52l":    round(pct_from_52l, 1),
        "weeks_above_ma30":runs_above,
        "weeks_below_ma30":runs_below,
        "ma30_flatness_cv":round(ma30_cv, 2),
    }


def _classify(close, ma30, ma10,
               slope30_4w, slope30_13w, slope10_4w,
               pct_above_ma30, pct_from_52h, pct_from_52l,
               vol_ratio, rsi, ma30_cv,
               runs_above, runs_below, ma10_above_ma30):
    """Core Weinstein classification logic."""
    notes = []

    # ── STAGE 4: Below declining MA30 ────────────────────────────────────────
    if close < ma30 and slope30_4w < -0.1:
        notes.append(f"Below MA30 ({pct_above_ma30:+.1f}%), MA30 declining")
        if slope30_13w < -0.15 and pct_from_52h < -30:
            # Deep decline
            sub = STAGE_4B
            conv = "High" if slope30_4w < -0.3 and vol_ratio > 1.2 else "Medium"
            notes.append("Steep decline, far from 52w high")
        else:
            sub = STAGE_4A
            conv = "High" if slope30_4w < -0.2 else "Medium"
            notes.append("Early decline, MA30 just turned down")
        return sub, conv, "; ".join(notes)

    # ── STAGE 3: Topping / Distribution ──────────────────────────────────────
    if (close < ma30 * 1.08 and close > ma30 * 0.93 and
            abs(slope30_4w) < 0.15 and slope30_13w < 0.05 and
            pct_from_52h > -15):
        notes.append("Price near flat MA30, near highs")
        if pct_from_52h > -8 and slope30_4w < 0:
            sub = STAGE_3B
            conv = "High" if vol_ratio > 1.1 and slope30_4w < -0.05 else "Medium"
            notes.append("MA30 turning down from top")
        else:
            sub = STAGE_3A
            conv = "Medium"
            notes.append("Distribution phase, MA30 still flat")
        return sub, conv, "; ".join(notes)

    # ── STAGE 2: Advancing / Markup ───────────────────────────────────────────
    if close > ma30 and slope30_4w > 0.05:
        notes.append(f"Above rising MA30 (+{pct_above_ma30:.1f}%)")
        if pct_from_52h > -5:
            # Near 52w high — late stage 2
            sub = STAGE_2C
            conv = "High" if slope30_4w > 0.2 and rsi > 70 else "Medium"
            notes.append("Extended — near 52w high, caution")
        elif runs_above < 26 and pct_above_ma30 < 20 and slope30_13w > 0.05:
            # Fresh breakout, not over-extended
            sub = STAGE_2A
            conv = "High" if (ma10_above_ma30 and vol_ratio > 1.0
                               and slope30_13w > 0.1) else "Medium"
            notes.append("Early advance, fresh breakout")
        else:
            sub = STAGE_2B
            conv = "High" if slope30_4w > 0.15 and vol_ratio > 0.9 else "Medium"
            notes.append("Mid advance, trending well")
        return sub, conv, "; ".join(notes)

    # ── STAGE 1: Basing / Accumulation ───────────────────────────────────────
    # Price near flat MA30 at lower levels
    flat_ma = ma30_cv < 3.0 and abs(slope30_13w) < 0.12
    if flat_ma or (abs(pct_above_ma30) < 10 and abs(slope30_4w) < 0.12):
        notes.append("MA30 flat, price consolidating")
        if (slope30_4w > 0.02 or (ma10_above_ma30 and close > ma30)):
            # MA starting to curl up
            sub = STAGE_1B
            conv = "Medium"
            notes.append("MA30 curling up — late base")
        else:
            sub = STAGE_1A
            conv = "Low"
            notes.append("Early basing, no direction yet")
        return sub, conv, "; ".join(notes)

    # Fallback
    if close > ma30:
        notes.append("Above MA30 but slope weak")
        return STAGE_2A, "Low", "; ".join(notes)
    else:
        notes.append("Below MA30, MA30 not declining steeply")
        return STAGE_4A, "Low", "; ".join(notes)


def _empty_stage() -> dict:
    return {
        "stage": STAGE_1A, "stage_label": "Unknown", "stage_icon": "⬜",
        "stage_color": "#aaa", "stage_bg": "#f5f5f5", "stage_class": "s1a",
        "conviction": "Low", "notes": "Insufficient data",
        "close": None, "ma30w": None, "ma10w": None,
        "pct_above_ma30": None, "slope_ma30_4w": None, "slope_ma30_13w": None,
        "vol_ratio": None, "weekly_rsi": None,
        "pct_from_52h": None, "pct_from_52l": None,
        "weeks_above_ma30": 0, "weeks_below_ma30": 0, "ma30_flatness_cv": None,
    }


# Numeric sort order for report
def stage_sort_key(stage_str: str) -> int:
    return STAGE_ORDER.get(stage_str, 99)
