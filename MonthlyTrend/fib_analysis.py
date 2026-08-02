"""
fib_analysis.py
===============
Per-stock Fibonacci analysis: retracement, extension (trend-based), and time zones.

For each stock, automatically identifies:
  P1 = most significant multi-year cycle low  (from full history)
  P2 = most significant cycle high after P1
  P3 = deepest correction low after P2

Then computes:
  Tool 1: Retracement levels from P1→P2
  Tool 2: Extension targets from P1→P2→P3
  Tool 3: Fibonacci time zones from P1 and from P3
  Confluence: price-time overlap windows
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from typing import Optional

FIB_RET  = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_EXT  = [0.382, 0.5, 0.618, 1.0, 1.272, 1.618, 2.0, 2.618]
FIB_TIME = [1.0, 1.618, 2.0, 2.618, 3.0, 4.236, 5.0, 6.854]


def _find_cycle_points(df: pd.DataFrame, order: int = 10):
    """
    Find P1, P2, P3 automatically from monthly data:
      P1 = lowest pivot in full history
      P2 = highest pivot after P1
      P3 = lowest pivot after P2 (deepest correction)
    """
    if len(df) < order * 3:
        return None, None, None

    hi_idx = argrelextrema(df["High"].values, np.greater_equal, order=order)[0]
    lo_idx = argrelextrema(df["Low"].values,  np.less_equal,   order=order)[0]

    if len(lo_idx) < 1 or len(hi_idx) < 1:
        return None, None, None

    # P1: overall lowest trough (ignore last `order` bars — may not be confirmed)
    valid_lo = lo_idx[lo_idx < len(df) - order]
    if len(valid_lo) == 0:
        valid_lo = lo_idx
    p1_pos = valid_lo[np.argmin(df["Low"].values[valid_lo])]
    p1_date  = df.index[p1_pos]
    p1_price = float(df["Low"].values[p1_pos])

    # P2: highest high AFTER P1
    hi_after = hi_idx[hi_idx > p1_pos]
    if len(hi_after) == 0:
        return None, None, None
    p2_pos  = hi_after[np.argmax(df["High"].values[hi_after])]
    p2_date  = df.index[p2_pos]
    p2_price = float(df["High"].values[p2_pos])

    # P3: lowest low AFTER P2 (confirmed correction)
    lo_after = lo_idx[lo_idx > p2_pos]
    if len(lo_after) == 0:
        # fallback: use the lowest close after P2
        sub = df.iloc[p2_pos:]
        p3_pos_rel = int(sub["Low"].idxmin() if hasattr(sub["Low"].idxmin(), 'iloc') else sub["Low"].values.argmin())
        p3_date  = sub.index[p3_pos_rel] if isinstance(p3_pos_rel, int) else p3_pos_rel
        p3_price = float(sub["Low"].min())
    else:
        p3_pos  = lo_after[np.argmin(df["Low"].values[lo_after])]
        p3_date  = df.index[p3_pos]
        p3_price = float(df["Low"].values[p3_pos])

    return (p1_date, p1_price), (p2_date, p2_price), (p3_date, p3_price)


def _months_between(d1, d2) -> int:
    s, e = pd.Timestamp(d1), pd.Timestamp(d2)
    return abs((e.year - s.year) * 12 + (e.month - s.month))


def _add_months(d, n: int) -> pd.Timestamp:
    ts = pd.Timestamp(d)
    m  = ts.month - 1 + n
    return pd.Timestamp(year=ts.year + m // 12, month=m % 12 + 1, day=1)


def analyze_fibonacci(symbol: str, mo: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """
    Full Fibonacci analysis for one stock on monthly data.
    Returns a rich dict with all levels, targets, time zones, and confluence.
    """
    if len(mo) < 24:
        return {}

    close = float(daily.iloc[-1]["Close"])

    # ── Find cycle points ────────────────────────────────────────────────────
    p1, p2, p3 = _find_cycle_points(mo, order=max(3, len(mo) // 15))
    if p1 is None or p2 is None or p3 is None:
        return {}

    p1_date, p1_price = p1
    p2_date, p2_price = p2
    p3_date, p3_price = p3

    # Sanity: P2 must be higher than P1
    if p2_price <= p1_price:
        return {}

    impulse = p2_price - p1_price

    # ── Tool 1: Retracement ──────────────────────────────────────────────────
    retracements = {}
    for r in FIB_RET:
        price = round(p2_price - impulse * r, 2)
        pct   = round((price - close) / close * 100, 1)
        retracements[r] = {"price": price, "pct_from_close": pct,
                           "label": f"{r*100:.1f}%"}

    # Which retracement level is CMP closest to?
    closest_ret = min(FIB_RET, key=lambda r: abs(retracements[r]["price"] - close))

    # Current retracement level (where is price in the retrace?)
    ret_pct = round((p2_price - close) / impulse * 100, 1) if impulse > 0 else None

    # ── Tool 2: Extensions from P3 ───────────────────────────────────────────
    extensions = {}
    for r in FIB_EXT:
        price = round(p3_price + impulse * r, 2)
        pct   = round((price - close) / close * 100, 1)
        extensions[r] = {"price": price, "pct_from_close": pct,
                          "label": f"{r:.3f}×"}

    # Nearest extension above close
    ext_above = {r: v for r, v in extensions.items() if v["price"] > close}
    nearest_ext = min(ext_above, key=lambda r: ext_above[r]["price"]) if ext_above else None

    # ── Tool 3: Time zones from P1 ───────────────────────────────────────────
    base_months = _months_between(p1_date, p2_date)
    time_zones_from_p1 = {}
    today = pd.Timestamp.today()
    for r in FIB_TIME:
        n_months = round(base_months * r)
        target_date = _add_months(p1_date, n_months)
        months_away = _months_between(today, target_date)
        direction   = "future" if target_date > today else "past"
        time_zones_from_p1[r] = {
            "months_from_p1": n_months,
            "date":           target_date.strftime("%b %Y"),
            "months_away":    months_away if direction == "future" else -months_away,
            "direction":      direction,
        }

    # Time zones from P3
    time_zones_from_p3 = {}
    for r in FIB_TIME:
        n_months    = round(base_months * r)
        target_date = _add_months(p3_date, n_months)
        months_away = _months_between(today, target_date)
        direction   = "future" if target_date > today else "past"
        time_zones_from_p3[r] = {
            "months_from_p3": n_months,
            "date":           target_date.strftime("%b %Y"),
            "months_away":    months_away if direction == "future" else -months_away,
            "direction":      direction,
        }

    # ── Confluence: price-time overlap ───────────────────────────────────────
    # Find future time zones within ±3 months of each other (P1 + P3 overlap)
    confluence_windows = []
    for r1, tz1 in time_zones_from_p1.items():
        if tz1["direction"] != "future":
            continue
        for r2, tz2 in time_zones_from_p3.items():
            if tz2["direction"] != "future":
                continue
            # Parse dates
            try:
                d1 = pd.Timestamp(tz1["date"])
                d2 = pd.Timestamp(tz2["date"])
                gap = abs(_months_between(d1, d2))
                if gap <= 3 and tz1["months_away"] >= 0:
                    # Find price extension nearest to that time window
                    # Use CMP projected forward at 5% per year as rough check
                    avg_date_months = (tz1["months_away"] + tz2["months_away"]) / 2
                    mid_date = _add_months(today, int(avg_date_months))
                    # Find nearest extension target
                    near_ext_r = nearest_ext
                    near_ext_p = extensions[nearest_ext]["price"] if nearest_ext else None
                    confluence_windows.append({
                        "p1_ratio":    r1,
                        "p3_ratio":    r2,
                        "date_range":  f"{tz1['date']} – {tz2['date']}",
                        "months_away": round(avg_date_months),
                        "mid_date":    mid_date.strftime("%b %Y"),
                        "nearest_ext_ratio": near_ext_r,
                        "nearest_ext_price": near_ext_p,
                    })
            except Exception:
                pass

    # Sort by soonest
    confluence_windows.sort(key=lambda x: x["months_away"])

    # ── P3 retrace depth ─────────────────────────────────────────────────────
    p3_retrace_pct = round((p2_price - p3_price) / impulse * 100, 1)

    # ── Summary numbers for report columns ───────────────────────────────────
    t1_price = extensions.get(1.0, {}).get("price")    # 100% extension
    t2_price = extensions.get(1.618, {}).get("price")  # 161.8%
    t3_price = extensions.get(2.618, {}).get("price")  # 261.8%
    ret_382  = retracements.get(0.382, {}).get("price")
    ret_618  = retracements.get(0.618, {}).get("price")

    return {
        "symbol":           symbol,
        # Anchor points
        "P1_date":          p1_date.strftime("%b %Y"),
        "P1_price":         round(p1_price, 2),
        "P2_date":          p2_date.strftime("%b %Y"),
        "P2_price":         round(p2_price, 2),
        "P3_date":          p3_date.strftime("%b %Y"),
        "P3_price":         round(p3_price, 2),
        "impulse":          round(impulse, 2),
        "P3_retrace_pct":   p3_retrace_pct,
        "base_months":      base_months,
        # Retracement
        "retracements":     retracements,
        "ret_382":          ret_382,
        "ret_618":          ret_618,
        "closest_ret_level":closest_ret,
        "current_ret_pct":  ret_pct,
        # Extensions
        "extensions":       extensions,
        "nearest_ext_ratio":nearest_ext,
        "nearest_ext_price":extensions[nearest_ext]["price"] if nearest_ext else None,
        "ext_100":          t1_price,
        "ext_162":          t2_price,
        "ext_262":          t3_price,
        "ext_100_pct":      extensions.get(1.0, {}).get("pct_from_close"),
        "ext_162_pct":      extensions.get(1.618, {}).get("pct_from_close"),
        "ext_262_pct":      extensions.get(2.618, {}).get("pct_from_close"),
        # Time zones
        "time_zones_p1":    time_zones_from_p1,
        "time_zones_p3":    time_zones_from_p3,
        "confluence":       confluence_windows[:5],
        # Next time zone
        "next_tz_date":     next((v["date"] for v in time_zones_from_p1.values()
                                  if v["direction"] == "future"), None),
        "next_tz_months":   next((v["months_away"] for v in time_zones_from_p1.values()
                                  if v["direction"] == "future"), None),
    }
