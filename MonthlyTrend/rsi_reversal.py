"""
rsi_reversal.py
===============
Monthly RSI Reversal Projection Engine

When monthly RSI(14) is above 75, this module answers:
  1. At what PRICE will RSI fall to key reversal levels (80, 75, 70, 60, 50)?
  2. How many months of decline needed to reach each level?
  3. Historical context: past RSI>75 episodes on this stock — how far did
     price fall before RSI returned to 70/60/50?
  4. Current RSI momentum: is it accelerating (overbought extension risk)
     or decelerating (reversal imminent)?

Core math:
  RSI uses Wilder's RMA (exponential with alpha=1/N).
  Given RMA state at bar T (avg_gain, avg_loss), the closing price P on
  bar T+1 that produces a target RSI R is:

    RS_target = R / (100 - R)
    if P > close[T]:                        # gain bar
        new_avg_gain = (avg_gain*(N-1) + gain) / N
        new_avg_loss = avg_loss*(N-1) / N
        RS = new_avg_gain / new_avg_loss = RS_target  → solve for P
    else:                                   # loss bar
        new_avg_gain = avg_gain*(N-1) / N
        new_avg_loss = (avg_loss*(N-1) + loss) / N
        solve for P

  For multi-bar projections we simulate a steady price path (constant
  monthly return) and find what return rate drives RSI to each target
  over 1, 2, 3, 6, 12 months.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional
import indicators as ind


# ── RSI RMA state tracker ─────────────────────────────────────────────────────

def _rma_state(close: pd.Series, length: int = 14):
    """
    Returns (prev_avg_gain, prev_avg_loss, prev_close) — the RMA state
    BEFORE the last bar, so we can project next-bar RSI for any price.
    """
    delta     = close.diff()
    up        = delta.clip(lower=0)
    down      = -delta.clip(upper=0)
    roll_up   = ind.rma(up, length)
    roll_down = ind.rma(down, length)

    up_last   = float(roll_up.iloc[-1])
    down_last = float(roll_down.iloc[-1])
    curr_up   = max(float(close.iloc[-1]) - float(close.iloc[-2]), 0)
    curr_down = max(float(close.iloc[-2]) - float(close.iloc[-1]), 0)

    prev_avg_gain = (up_last   * length - curr_up)   / (length - 1)
    prev_avg_loss = (down_last * length - curr_down) / (length - 1)
    return max(prev_avg_gain, 0), max(prev_avg_loss, 0), float(close.iloc[-2])


def _rsi_for_price(price: float, prev_close: float,
                    avg_gain: float, avg_loss: float, length: int = 14) -> float:
    """RSI that would result if next bar closes at `price`."""
    change = price - prev_close
    if change >= 0:
        new_ag = (avg_gain * (length - 1) + change) / length
        new_al = avg_loss * (length - 1) / length
    else:
        new_ag = avg_gain * (length - 1) / length
        new_al = (avg_loss * (length - 1) + abs(change)) / length
    if new_al == 0:
        return 100.0
    return 100 - (100 / (1 + new_ag / new_al))


def _price_for_rsi(target_rsi: float, prev_close: float,
                    avg_gain: float, avg_loss: float, length: int = 14) -> Optional[float]:
    """Price on the next bar that produces `target_rsi`."""
    if target_rsi <= 0 or target_rsi >= 100:
        return None
    rs = target_rsi / (100 - target_rsi)
    # Try gain path: price > prev_close
    # new_ag = (ag*(N-1) + gain) / N,  new_al = al*(N-1)/N
    # RS = new_ag / new_al = rs
    new_al_gain_path = avg_loss * (length - 1) / length
    if new_al_gain_path > 0:
        new_ag_needed = rs * new_al_gain_path
        gain_needed   = new_ag_needed * length - avg_gain * (length - 1)
        if gain_needed >= 0:
            return prev_close + gain_needed

    # Loss path: price < prev_close
    # new_ag = ag*(N-1)/N, new_al = (al*(N-1) + loss) / N
    new_ag_loss_path = avg_gain * (length - 1) / length
    if rs > 0:
        new_al_needed = new_ag_loss_path / rs
        loss_needed   = new_al_needed * length - avg_loss * (length - 1)
        if loss_needed >= 0:
            return prev_close - loss_needed

    return None


# ── Multi-bar simulation ───────────────────────────────────────────────────────

def _simulate_rsi_path(avg_gain: float, avg_loss: float,
                        start_price: float, monthly_return: float,
                        n_months: int, length: int = 14) -> list[tuple[float, float]]:
    """
    Simulate RSI over n_months assuming constant monthly_return.
    Returns list of (price, rsi) per month.
    """
    ag, al, price = avg_gain, avg_loss, start_price
    path = []
    for _ in range(n_months):
        new_price = price * (1 + monthly_return)
        change = new_price - price
        if change >= 0:
            ag = (ag * (length - 1) + change) / length
            al = al * (length - 1) / length
        else:
            ag = ag * (length - 1) / length
            al = (al * (length - 1) + abs(change)) / length
        rsi = 100 - (100 / (1 + ag / al)) if al > 0 else 100.0
        path.append((round(new_price, 2), round(rsi, 2)))
        price = new_price
    return path


def _months_to_rsi(avg_gain: float, avg_loss: float,
                    start_price: float, monthly_return: float,
                    target_rsi: float, max_months: int = 24,
                    length: int = 14) -> Optional[int]:
    """How many months at `monthly_return` until RSI hits `target_rsi`?"""
    ag, al, price = avg_gain, avg_loss, start_price
    for m in range(1, max_months + 1):
        new_price = price * (1 + monthly_return)
        change = new_price - price
        if change >= 0:
            ag = (ag * (length - 1) + change) / length
            al = al * (length - 1) / length
        else:
            ag = ag * (length - 1) / length
            al = (al * (length - 1) + abs(change)) / length
        rsi = 100 - (100 / (1 + ag / al)) if al > 0 else 100.0
        price = new_price
        if rsi <= target_rsi:
            return m
    return None


# ── Historical RSI>75 episodes ────────────────────────────────────────────────

def _historical_episodes(mo: pd.DataFrame, rsi_series: pd.Series,
                           threshold: float = 75.0, length: int = 14) -> list[dict]:
    """
    Find past episodes where monthly RSI crossed above `threshold`,
    then track how far price fell before RSI came back below 70/60/50.
    """
    episodes = []
    rsi = rsi_series.dropna()
    above = rsi >= threshold
    in_ep = False
    ep_start_idx = None

    for i in range(len(rsi)):
        if above.iloc[i] and not in_ep:
            in_ep = True
            ep_start_idx = i
        elif not above.iloc[i] and in_ep:
            in_ep = False
            # Episode ended — measure what happened
            ep_rsi   = rsi.iloc[ep_start_idx:i]
            ep_prices= mo["Close"].iloc[ep_start_idx:i]
            if len(ep_rsi) < 1:
                continue
            peak_rsi   = float(ep_rsi.max())
            peak_price = float(ep_prices.iloc[ep_rsi.values.argmax()])
            end_price  = float(mo["Close"].iloc[i])
            drawdown   = (end_price - peak_price) / peak_price * 100
            duration   = len(ep_rsi)
            # Find where RSI fell to 70, 60, 50 after the episode
            post = rsi.iloc[i:]
            post_p = mo["Close"].iloc[i:]
            def _bars_to(level):
                for j, v in enumerate(post):
                    if v <= level:
                        pct = (float(post_p.iloc[j]) - peak_price) / peak_price * 100
                        return j, round(pct, 1)
                return None, None
            b70, p70 = _bars_to(70)
            b60, p60 = _bars_to(60)
            b50, p50 = _bars_to(50)
            episodes.append({
                "start":       rsi.index[ep_start_idx].strftime("%Y-%m"),
                "end":         rsi.index[i-1].strftime("%Y-%m"),
                "peak_rsi":    round(peak_rsi, 1),
                "peak_price":  round(peak_price, 2),
                "duration_m":  duration,
                "drawdown_pct":round(drawdown, 1),
                "months_to_70":b70, "price_chg_to_70_pct": p70,
                "months_to_60":b60, "price_chg_to_60_pct": p60,
                "months_to_50":b50, "price_chg_to_50_pct": p50,
            })
    return episodes


# ── Main analysis function ────────────────────────────────────────────────────

# RSI reversal target levels
REVERSAL_LEVELS = [85, 80, 75, 70, 65, 60, 50, 40]
# Decline scenarios (monthly return assumptions)
SCENARIOS = {
    "flat":        0.000,    # price stays flat
    "mild_decline":-0.02,    # -2% per month
    "mod_decline": -0.04,    # -4% per month
    "sharp_decline":-0.07,   # -7% per month
}


def analyze_rsi_reversal(symbol: str, mo: pd.DataFrame,
                          rsi_series: pd.Series, length: int = 14) -> dict:
    """
    Full RSI reversal analysis for a stock whose monthly RSI is above 75.

    Returns a dict with:
      current_rsi       : float
      current_price     : float
      next_bar_targets  : {rsi_level: price}   — next-bar projection
      scenario_months   : {scenario: {rsi_level: months}}
      scenario_prices   : {scenario: list of (price, rsi) for 12 months}
      rsi_acceleration  : float (positive = still rising, negative = decelerating)
      overbought_risk   : "High" | "Medium" | "Low"
      historical_episodes: list of past RSI>75 episodes with drawdown stats
      avg_drawdown_to_70 : float (avg % price change when RSI fell to 70)
      avg_drawdown_to_60 : float
      avg_drawdown_to_50 : float
      support_at_70_price : float  (price that would produce RSI=70 next bar)
      support_at_60_price : float
      support_at_50_price : float
    """
    close = mo["Close"]
    if len(close) < length + 5:
        return {}

    rsi_now = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else None
    if rsi_now is None:
        return {}

    current_price = float(close.iloc[-1])

    # RMA state
    ag, al, prev_close = _rma_state(close, length)

    # ── Next-bar price targets for each RSI reversal level ────────────────
    next_bar_targets = {}
    for level in REVERSAL_LEVELS:
        p = _price_for_rsi(level, prev_close, ag, al, length)
        if p and p > 0:
            pct_chg = (p - current_price) / current_price * 100
            next_bar_targets[level] = {
                "price":    round(p, 2),
                "pct_chg":  round(pct_chg, 1),
                "direction":"up" if p > current_price else "down"
            }

    # ── Multi-bar scenario projections ────────────────────────────────────
    scenario_paths  = {}
    scenario_months = {}
    for name, ret in SCENARIOS.items():
        path = _simulate_rsi_path(ag, al, current_price, ret, 12, length)
        scenario_paths[name] = path
        months_per_level = {}
        for level in [70, 60, 50]:
            m = _months_to_rsi(ag, al, current_price, ret, level, 24, length)
            months_per_level[level] = m
        scenario_months[name] = months_per_level

    # ── RSI momentum (acceleration) ───────────────────────────────────────
    rsi_clean = rsi_series.dropna()
    if len(rsi_clean) >= 4:
        recent  = float(rsi_clean.iloc[-1])
        prev1   = float(rsi_clean.iloc[-2])
        prev2   = float(rsi_clean.iloc[-3])
        mom1    = recent - prev1        # last month change
        mom2    = prev1  - prev2        # month before
        rsi_acceleration = round(mom1 - mom2, 2)   # positive = accelerating up
        rsi_momentum     = round(mom1, 2)
    else:
        rsi_acceleration = 0.0
        rsi_momentum     = 0.0

    # Overbought risk assessment
    if rsi_now >= 85 or (rsi_now >= 80 and rsi_acceleration > 0):
        ob_risk = "High"
    elif rsi_now >= 78 or rsi_acceleration < -2:
        ob_risk = "Medium"
    else:
        ob_risk = "Low"

    # ── Historical episodes ───────────────────────────────────────────────
    episodes = _historical_episodes(mo, rsi_series, threshold=75.0, length=length)

    def _safe_avg(lst):
        vals = [x for x in lst if x is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    avg_dd_70 = _safe_avg([e["price_chg_to_70_pct"] for e in episodes])
    avg_dd_60 = _safe_avg([e["price_chg_to_60_pct"] for e in episodes])
    avg_dd_50 = _safe_avg([e["price_chg_to_50_pct"] for e in episodes])
    avg_dur   = _safe_avg([e["duration_m"] for e in episodes])
    avg_peak  = _safe_avg([e["peak_rsi"] for e in episodes])

    # ── Key price support levels ──────────────────────────────────────────
    # Price that would produce RSI=70/60/50 on the next bar
    p70 = next_bar_targets.get(70, {}).get("price")
    p60 = next_bar_targets.get(60, {}).get("price")
    p50 = next_bar_targets.get(50, {}).get("price")

    # ── RSI "stall price" — price at which RSI stays flat ─────────────────
    # RSI stays flat when avg_gain/avg_loss ratio is unchanged → flat price
    flat_price = round(current_price, 2)   # by definition, flat = current

    return {
        "symbol":              symbol,
        "current_rsi":         round(rsi_now, 2),
        "current_price":       round(current_price, 2),
        "rsi_momentum":        rsi_momentum,
        "rsi_acceleration":    rsi_acceleration,
        "overbought_risk":     ob_risk,
        "next_bar_targets":    next_bar_targets,
        "scenario_paths":      {k: [[p, r] for p, r in v] for k, v in scenario_paths.items()},
        "scenario_months":     scenario_months,
        "price_at_rsi70":      p70,
        "price_at_rsi60":      p60,
        "price_at_rsi50":      p50,
        "pct_to_rsi70":        round((p70 - current_price)/current_price*100, 1) if p70 else None,
        "pct_to_rsi60":        round((p60 - current_price)/current_price*100, 1) if p60 else None,
        "pct_to_rsi50":        round((p50 - current_price)/current_price*100, 1) if p50 else None,
        "historical_episodes": episodes,
        "episode_count":       len(episodes),
        "avg_peak_rsi":        avg_peak,
        "avg_duration_months": avg_dur,
        "avg_drawdown_to_70":  avg_dd_70,
        "avg_drawdown_to_60":  avg_dd_60,
        "avg_drawdown_to_50":  avg_dd_50,
        "reversal_levels":     REVERSAL_LEVELS,
        "scenarios":           list(SCENARIOS.keys()),
    }
