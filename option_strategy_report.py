#!/usr/bin/env python3
"""Daily NIFTY paper-trading strategy report.

This is an analysis and paper-trading tool only. It never places orders.
It combines NSE option-chain quotes with NIFTY trend, support/resistance and
Fibonacci context to compare defined-risk directional and non-directional
weekly/monthly strategies.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from TradingViewTypeCharts.indicators import (
    fibonacci_levels,
    macd,
    monthly,
    rsi,
    support_resistance,
    weekly,
)


BASE_DIR = Path(__file__).resolve().parent
REPORT_FILE = BASE_DIR / "option_strategy_report.html"
STATE_FILE = BASE_DIR / "option_paper_state.json"
NSE_HOME = "https://www.nseindia.com"
NSE_CHAIN = f"{NSE_HOME}/api/option-chain-indices"
DEFAULT_LOT_SIZE = int(os.getenv("NIFTY_LOT_SIZE", "65"))
NIFTY_STRIKE_STEP = float(os.getenv("NIFTY_STRIKE_STEP", "50"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/",
}


def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _num(value, default=None):
    return float(value) if _finite(value) else default


def _parse_expiry(value: str) -> date:
    return datetime.strptime(value.strip().title(), "%d-%b-%Y").date()


def _iso_expiry(value: str) -> str:
    return _parse_expiry(value).isoformat()


def _fetch_option_chain(symbol: str = "NIFTY", attempts: int = 3) -> dict:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            session = requests.Session()
            session.headers.update(HEADERS)
            landing = session.get(NSE_HOME, timeout=20)
            landing.raise_for_status()
            response = session.get(NSE_CHAIN, params={"symbol": symbol}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            records = payload.get("records") or {}
            expiries = sorted(
                {_iso_expiry(x) for x in records.get("expiryDates", [])},
            )
            spot = _num(records.get("underlyingValue"))
            quotes = {}
            for row in records.get("data", []):
                expiry_raw = row.get("expiryDate")
                strike = _num(row.get("strikePrice"))
                if not expiry_raw or strike is None:
                    continue
                expiry = _iso_expiry(expiry_raw)
                for option_type in ("CE", "PE"):
                    raw = row.get(option_type) or {}
                    last = _num(raw.get("lastPrice"))
                    bid = _num(raw.get("bidprice"))
                    ask = _num(raw.get("askPrice"))
                    if bid is not None and ask is not None and bid > 0 and ask > 0:
                        mark = (bid + ask) / 2
                    else:
                        mark = last
                    if mark is None or mark <= 0:
                        continue
                    quotes[(expiry, option_type, round(strike, 2))] = {
                        "expiry": expiry,
                        "type": option_type,
                        "strike": round(strike, 2),
                        "price": round(mark, 2),
                        "bid": bid,
                        "ask": ask,
                        "iv": _num(raw.get("impliedVolatility")),
                        "oi": _num(raw.get("openInterest"), 0),
                    }
            if spot is None or not quotes:
                raise RuntimeError("NSE returned no underlying value or option quotes")
            print(
                f"Option chain loaded: {symbol}, spot={spot:.2f}, "
                f"expiries={len(expiries)}, quotes={len(quotes)}"
            )
            return {"symbol": symbol, "spot": spot, "expiries": expiries, "quotes": quotes}
        except Exception as exc:
            last_error = exc
            print(f"[WARN] NSE option-chain attempt {attempt}/{attempts}: {exc}")
            if attempt < attempts:
                time.sleep(3 * attempt)
    raise RuntimeError(f"Could not load NSE option chain after {attempts} attempts: {last_error}")


def _fetch_nifty_history(attempts: int = 3) -> pd.DataFrame:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            raw = yf.download(
                "^NSEI", period="3y", interval="1d",
                auto_adjust=True, progress=False, threads=False,
            )
            if isinstance(raw.columns, pd.MultiIndex):
                if "^NSEI" in raw.columns.get_level_values(0):
                    raw = raw["^NSEI"]
                elif "^NSEI" in raw.columns.get_level_values(1):
                    raw = raw.xs("^NSEI", axis=1, level=1)
                else:
                    raw = raw.droplevel(1, axis=1)
            columns = {str(c).title(): c for c in raw.columns}
            if "Close" not in columns:
                raise RuntimeError("NIFTY history has no Close column")
            close = pd.to_numeric(raw[columns["Close"]], errors="coerce")
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            out = pd.DataFrame(index=pd.to_datetime(close.index))
            out["Close"] = close.values
            for name in ("Open", "High", "Low", "Volume"):
                if name in columns:
                    series = raw[columns[name]]
                    if isinstance(series, pd.DataFrame):
                        series = series.iloc[:, 0]
                    out[name] = pd.to_numeric(series.values, errors="coerce")
                else:
                    out[name] = out["Close"]
            out = out.dropna(subset=["Close"]).sort_index()
            if len(out) < 220:
                raise RuntimeError(f"only {len(out)} NIFTY daily bars returned")
            print(f"NIFTY history loaded: {len(out)} daily bars")
            return out
        except Exception as exc:
            last_error = exc
            print(f"[WARN] NIFTY history attempt {attempt}/{attempts}: {exc}")
            if attempt < attempts:
                time.sleep(3 * attempt)
    raise RuntimeError(f"Could not load NIFTY history after {attempts} attempts: {last_error}")


def _technical_context(frame: pd.DataFrame, timeframe: str) -> dict:
    frame = frame.dropna(subset=["Close"]).copy()
    close = frame["Close"]
    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    rsi14 = rsi(close, 14)
    macd_line, macd_signal, macd_hist = macd(close, 34, 200, 9)
    score = 0
    signals = []
    latest = float(close.iloc[-1])
    if _finite(ema20.iloc[-1]):
        if latest > float(ema20.iloc[-1]):
            score += 1
            signals.append("price above EMA20")
        else:
            score -= 1
            signals.append("price below EMA20")
    if _finite(ema20.iloc[-1]) and _finite(ema50.iloc[-1]):
        if float(ema20.iloc[-1]) > float(ema50.iloc[-1]):
            score += 1
            signals.append("EMA20 above EMA50")
        else:
            score -= 1
            signals.append("EMA20 below EMA50")
    if _finite(rsi14.iloc[-1]):
        value = float(rsi14.iloc[-1])
        if value >= 55:
            score += 1
            signals.append(f"RSI14 {value:.1f} bullish")
        elif value <= 45:
            score -= 1
            signals.append(f"RSI14 {value:.1f} bearish")
        else:
            signals.append(f"RSI14 {value:.1f} neutral")
    if _finite(macd_hist.iloc[-1]):
        if float(macd_hist.iloc[-1]) > 0:
            score += 1
            signals.append("MACD histogram positive")
        else:
            score -= 1
            signals.append("MACD histogram negative")
    slope = float(ema20.iloc[-1] - ema20.iloc[-6]) if len(ema20) >= 6 and _finite(ema20.iloc[-6]) else 0
    if slope > 0:
        score += 1
        signals.append("EMA20 rising")
    elif slope < 0:
        score -= 1
        signals.append("EMA20 falling")

    supports, resistances = support_resistance(
        frame, order=5, lookback=min(250, len(frame)), n_levels=3
    )
    fib = fibonacci_levels(frame, lookback=min(120, len(frame)))
    direction = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "RANGE-BOUND"
    return {
        "timeframe": timeframe,
        "close": round(latest, 2),
        "score": score,
        "direction": direction,
        "confidence": round(min(abs(score) / 5, 1.0) * 100),
        "rsi": round(float(rsi14.iloc[-1]), 2) if _finite(rsi14.iloc[-1]) else None,
        "supports": [round(float(x), 2) for x in supports],
        "resistances": [round(float(x), 2) for x in resistances],
        "fibonacci": {str(k): round(float(v), 2) for k, v in fib.items()},
        "signals": signals,
    }


def _all_contexts(history: pd.DataFrame) -> dict:
    return {
        "Daily": _technical_context(history, "Daily"),
        "Weekly": _technical_context(weekly(history), "Weekly"),
        "Monthly": _technical_context(monthly(history), "Monthly"),
    }


def _strike_step(strikes: list[float]) -> float:
    diffs = np.diff(sorted(set(strikes)))
    valid = diffs[diffs > 0]
    return float(np.median(valid)) if len(valid) else NIFTY_STRIKE_STEP


def _strike_near(strikes: list[float], target: float, direction: str = "nearest") -> float | None:
    values = sorted(set(float(x) for x in strikes))
    if not values:
        return None
    if direction == "below":
        eligible = [x for x in values if x < target]
        return max(eligible) if eligible else None
    if direction == "above":
        eligible = [x for x in values if x > target]
        return min(eligible) if eligible else None
    return min(values, key=lambda x: abs(x - target))


def _quote(chain: dict, expiry: str, option_type: str, strike: float) -> dict | None:
    return chain["quotes"].get((expiry, option_type, round(float(strike), 2)))


def _leg(side: str, option_type: str, strike: float, quote: dict) -> dict:
    return {
        "side": side,
        "type": option_type,
        "strike": float(strike),
        "price": float(quote["price"]),
    }


def _strategy(
    name: str,
    horizon: str,
    bias: str,
    expiry: str,
    legs: list[dict],
    spot: float,
    lot_size: int,
    max_profit_points: float | None,
    max_loss_points: float,
    breakevens: list[float],
    rationale: str,
) -> dict:
    cashflow_points = sum(
        (-1 if leg["side"] == "BUY" else 1) * leg["price"] for leg in legs
    )
    return {
        "trade_id": f"NIFTY-{horizon}-{name.replace(' ', '-')}-{expiry}",
        "underlying": "NIFTY",
        "horizon": horizon,
        "strategy": name,
        "bias": bias,
        "expiry": expiry,
        "entry_spot": round(spot, 2),
        "lot_size": lot_size,
        "legs": legs,
        "net_premium_points": round(-cashflow_points, 2),
        "net_cashflow": round(cashflow_points * lot_size, 2),
        "max_profit": round(max_profit_points * lot_size, 2) if max_profit_points is not None else None,
        "max_loss": round(max_loss_points * lot_size, 2),
        "breakevens": [round(x, 2) for x in breakevens],
        "rationale": rationale,
        "status": "OPEN",
        "entry_date": date.today().isoformat(),
        "current_pnl": 0.0,
        "last_spot": round(spot, 2),
    }


def _build_candidates(chain: dict, contexts: dict, lot_size: int) -> list[dict]:
    today = date.today()
    expiries = [x for x in chain["expiries"] if _parse_expiry(x) >= today]
    if not expiries:
        raise RuntimeError("NSE option chain has no current or future expiries")
    weekly_expiry = expiries[0]
    grouped = {}
    for expiry in expiries:
        grouped.setdefault(expiry[:7], []).append(expiry)
    current_group = grouped[weekly_expiry[:7]]
    later_groups = [key for key in sorted(grouped) if key > weekly_expiry[:7]]
    if len(current_group) > 1:
        monthly_expiry = max(current_group)
    elif later_groups:
        monthly_expiry = max(grouped[later_groups[0]])
    else:
        monthly_expiry = weekly_expiry

    spot = float(chain["spot"])
    strikes = sorted({key[2] for key in chain["quotes"] if key[0] == weekly_expiry})
    if not strikes:
        raise RuntimeError(f"No usable quotes for weekly expiry {weekly_expiry}")
    step = _strike_step(strikes)
    atm = _strike_near(strikes, spot)
    if atm is None:
        raise RuntimeError("Could not find an ATM strike")

    daily = contexts["Daily"]
    supports = [x for x in daily["supports"] if x < spot]
    resistances = [x for x in daily["resistances"] if x > spot]
    fib_values = [float(x) for x in daily["fibonacci"].values()]
    fib_below = [x for x in fib_values if x < spot]
    fib_above = [x for x in fib_values if x > spot]
    support_anchor = max(supports + fib_below, default=spot * 0.98)
    resistance_anchor = min(resistances + fib_above, default=spot * 1.02)
    candidates = []

    for horizon, expiry in (("Weekly", weekly_expiry), ("Monthly", monthly_expiry)):
        expiry_strikes = sorted({key[2] for key in chain["quotes"] if key[0] == expiry})
        if not expiry_strikes:
            continue
        atm_h = _strike_near(expiry_strikes, spot)
        if atm_h is None:
            continue
        width = max(step * 4, round(spot * 0.02 / step) * step)

        # Bull call spread: use resistance/Fibonacci as the short-call target.
        long_call = atm_h
        short_call = _strike_near(expiry_strikes, max(resistance_anchor, spot + width), "above")
        q1, q2 = _quote(chain, expiry, "CE", long_call), _quote(chain, expiry, "CE", short_call or 0)
        if q1 and q2 and short_call and short_call > long_call:
            debit = q1["price"] - q2["price"]
            spread = short_call - long_call
            if 0 < debit < spread:
                candidates.append(_strategy(
                    "Bull Call Spread", horizon, "BULLISH", expiry,
                    [_leg("BUY", "CE", long_call, q1), _leg("SELL", "CE", short_call, q2)],
                    spot, lot_size, spread - debit, debit,
                    [long_call + debit],
                    f"Trend {daily['direction']}; upside reference uses resistance/Fibonacci near ₹{resistance_anchor:.2f}.",
                ))

        # Bear put spread: use support/Fibonacci as the short-put target.
        long_put = atm_h
        short_put = _strike_near(expiry_strikes, min(support_anchor, spot - width), "below")
        q1, q2 = _quote(chain, expiry, "PE", long_put), _quote(chain, expiry, "PE", short_put or 0)
        if q1 and q2 and short_put and short_put < long_put:
            debit = q1["price"] - q2["price"]
            spread = long_put - short_put
            if 0 < debit < spread:
                candidates.append(_strategy(
                    "Bear Put Spread", horizon, "BEARISH", expiry,
                    [_leg("BUY", "PE", long_put, q1), _leg("SELL", "PE", short_put, q2)],
                    spot, lot_size, spread - debit, debit,
                    [long_put - debit],
                    f"Trend {daily['direction']}; downside reference uses support/Fibonacci near ₹{support_anchor:.2f}.",
                ))

        # Iron condor: short strikes are placed around technical boundaries.
        short_put = _strike_near(expiry_strikes, min(support_anchor, spot - width), "below")
        long_put = _strike_near(expiry_strikes, (short_put or spot - width) - width, "below")
        short_call = _strike_near(expiry_strikes, max(resistance_anchor, spot + width), "above")
        long_call = _strike_near(expiry_strikes, (short_call or spot + width) + width, "above")
        qs = [
            _quote(chain, expiry, "PE", short_put or 0),
            _quote(chain, expiry, "PE", long_put or 0),
            _quote(chain, expiry, "CE", short_call or 0),
            _quote(chain, expiry, "CE", long_call or 0),
        ]
        if all(qs) and long_put < short_put < spot < short_call < long_call:
            credit = qs[0]["price"] - qs[1]["price"] + qs[2]["price"] - qs[3]["price"]
            put_width, call_width = short_put - long_put, long_call - short_call
            max_width = max(put_width, call_width)
            if credit > 0 and credit < max_width:
                candidates.append(_strategy(
                    "Iron Condor", horizon, "RANGE-BOUND", expiry,
                    [_leg("SELL", "PE", short_put, qs[0]), _leg("BUY", "PE", long_put, qs[1]),
                     _leg("SELL", "CE", short_call, qs[2]), _leg("BUY", "CE", long_call, qs[3])],
                    spot, lot_size, credit, max_width - credit,
                    [short_put - credit, short_call + credit],
                    f"Non-directional setup bounded by support ₹{support_anchor:.2f} and resistance ₹{resistance_anchor:.2f}.",
                ))

        # Long straddle: non-directional breakout alternative.
        ce, pe = _quote(chain, expiry, "CE", atm_h), _quote(chain, expiry, "PE", atm_h)
        if ce and pe:
            debit = ce["price"] + pe["price"]
            candidates.append(_strategy(
                "Long Straddle", horizon, "NON-DIRECTIONAL", expiry,
                [_leg("BUY", "CE", atm_h, ce), _leg("BUY", "PE", atm_h, pe)],
                spot, lot_size, None, debit,
                [atm_h - debit, atm_h + debit],
                "Non-directional volatility trade; profitable after a move beyond either breakeven.",
            ))
    if not candidates:
        raise RuntimeError("No complete option strategies could be priced from the NSE chain")
    return candidates


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"version": 1, "open": [], "closed": [], "updated_at": None}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        state.setdefault("open", [])
        state.setdefault("closed", [])
        return state
    except Exception as exc:
        print(f"[WARN] Paper state reset: {exc}")
        return {"version": 1, "open": [], "closed": [], "updated_at": None}


def _mark_to_market(trade: dict, chain: dict, spot: float) -> float:
    value = 0.0
    for leg in trade["legs"]:
        quote = _quote(chain, trade["expiry"], leg["type"], leg["strike"])
        price = quote["price"] if quote else leg["price"]
        value += (-1 if leg["side"] == "SELL" else 1) * price * trade["lot_size"]
    return round(float(trade.get("net_cashflow", 0)) + value, 2)


def _expiry_payoff(trade: dict, spot: float) -> float:
    intrinsic = 0.0
    for leg in trade["legs"]:
        amount = max(spot - leg["strike"], 0) if leg["type"] == "CE" else max(leg["strike"] - spot, 0)
        intrinsic += (-1 if leg["side"] == "SELL" else 1) * amount * trade["lot_size"]
    return round(float(trade.get("net_cashflow", 0)) + intrinsic, 2)


def _update_paper_trades(candidates: list[dict], chain: dict, spot: float) -> tuple[list[dict], list[dict]]:
    state = _load_state()
    today = date.today()
    candidate_by_id = {x["trade_id"]: x for x in candidates}
    open_trades = []
    closed = list(state.get("closed", []))
    seen = set()

    for trade in state.get("open", []):
        trade_id = trade.get("trade_id")
        if not trade_id or trade_id in seen:
            continue
        seen.add(trade_id)
        expiry = _parse_expiry(trade["expiry"]) if "expiry" in trade else today
        if today >= expiry:
            trade["status"] = "CLOSED"
            trade["exit_date"] = today.isoformat()
            trade["exit_reason"] = "EXPIRY"
            trade["exit_spot"] = round(spot, 2)
            trade["current_pnl"] = _expiry_payoff(trade, spot)
            closed.insert(0, trade)
            continue
        trade["current_pnl"] = _mark_to_market(trade, chain, spot)
        trade["last_spot"] = round(spot, 2)
        trade["last_mark_date"] = today.isoformat()
        max_loss = float(trade.get("max_loss") or 0)
        max_profit = trade.get("max_profit")
        if max_loss and trade["current_pnl"] <= -0.75 * max_loss:
            trade["status"] = "CLOSED"
            trade["exit_date"] = today.isoformat()
            trade["exit_reason"] = "RISK_STOP_75PCT"
            trade["exit_spot"] = round(spot, 2)
            closed.insert(0, trade)
        elif max_profit is not None and max_profit > 0 and trade["current_pnl"] >= 0.75 * max_profit:
            trade["status"] = "CLOSED"
            trade["exit_date"] = today.isoformat()
            trade["exit_reason"] = "PROFIT_TARGET_75PCT"
            trade["exit_spot"] = round(spot, 2)
            closed.insert(0, trade)
        else:
            open_trades.append(trade)

    for candidate in candidates:
        if candidate["trade_id"] in seen:
            continue
        candidate["last_mark_date"] = today.isoformat()
        open_trades.append(candidate)

    state = {
        "version": 1,
        "open": open_trades,
        "closed": closed[:100],
        "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    return open_trades, state["closed"]


def _money(value) -> str:
    if value is None:
        return "Unlimited"
    return f"₹{float(value):,.0f}"


def _leg_text(legs: list[dict]) -> str:
    return " · ".join(
        f"{leg['side']} {leg['type']} {leg['strike']:.0f} @ ₹{leg['price']:.2f}" for leg in legs
    )


def _recommendations(candidates: list[dict], contexts: dict) -> list[dict]:
    out = []
    direction = contexts["Daily"]["direction"]
    for horizon in ("Weekly", "Monthly"):
        pool = [x for x in candidates if x["horizon"] == horizon]
        preferred_name = (
            "Bull Call Spread" if direction == "BULLISH"
            else "Bear Put Spread" if direction == "BEARISH"
            else "Iron Condor"
        )
        preferred = next((x for x in pool if x["strategy"] == preferred_name), None)
        defined = [x for x in pool if x["max_profit"] is not None]
        highest_profit = max(defined, key=lambda x: x["max_profit"], default=None)
        out.append({
            "horizon": horizon,
            "preferred": preferred,
            "highest_defined_profit": highest_profit,
            "conclusion": (
                f"{preferred_name} aligns with the {direction.lower()} daily trend."
                if preferred else
                f"No complete {preferred_name} quote was available; compare the priced alternatives below."
            ),
        })
    return out


def _render_report(
    candidates: list[dict], open_trades: list[dict], closed: list[dict],
    contexts: dict, spot: float, recommendations: list[dict], lot_size: int,
) -> None:
    def esc(value) -> str:
        return html.escape(str(value))

    direction = contexts["Daily"]["direction"]
    rec_cards = []
    for rec in recommendations:
        preferred = rec["preferred"]
        highest = rec["highest_defined_profit"]
        rec_cards.append(
            f"<article class='card'><h3>{esc(rec['horizon'])} recommendation</h3>"
            f"<strong>{esc(preferred['strategy'] if preferred else 'Compare alternatives')}</strong>"
            f"<p>{esc(rec['conclusion'])}</p>"
            f"<div class='metrics'>Preferred max profit: <b>{_money(preferred['max_profit']) if preferred else '—'}</b>"
            f" · Max loss: <b>{_money(preferred['max_loss']) if preferred else '—'}</b></div>"
            f"<p class='muted'>Highest defined max profit: "
            f"{esc(highest['strategy']) if highest else '—'} ({_money(highest['max_profit']) if highest else '—'})</p></article>"
        )

    def strategy_row(trade: dict, closed_row=False) -> str:
        status = trade.get("status", "OPEN")
        pnl = trade.get("current_pnl", 0)
        breakeven = " / ".join(f"₹{float(x):,.0f}" for x in trade.get("breakevens", []))
        reason = trade.get("exit_reason", "")
        return (
            "<tr>"
            f"<td>{esc(trade.get('horizon'))}</td><td><b>{esc(trade.get('strategy'))}</b><br>"
            f"<span class='muted'>{esc(trade.get('bias'))}</span></td>"
            f"<td>{esc(trade.get('expiry'))}</td><td>{esc(_leg_text(trade.get('legs', [])))}</td>"
            f"<td>{_money(trade.get('net_premium_points', 0) * trade.get('lot_size', lot_size))}</td>"
            f"<td>{_money(trade.get('max_profit'))}</td><td>{_money(trade.get('max_loss'))}</td>"
            f"<td>{esc(breakeven)}</td><td class='{status.lower()}'>{esc(status)}"
            f"{' · ' + esc(reason) if reason else ''}</td><td>{_money(pnl)}</td>"
            "</tr>"
        )

    strategy_rows = "".join(strategy_row(x) for x in candidates)
    open_rows = "".join(strategy_row(x) for x in open_trades)
    closed_rows = "".join(strategy_row(x, True) for x in closed[:30])

    technical_rows = ""
    for label, ctx in contexts.items():
        fib_items = list(ctx["fibonacci"].items())[:6]
        fib_text = ", ".join(f"{k}: ₹{v:,.0f}" for k, v in fib_items) or "—"
        technical_rows += (
            f"<tr><td><b>{esc(label)}</b></td><td>{esc(ctx['direction'])} "
            f"({ctx['score']:+d}/5, {ctx['confidence']}%)</td>"
            f"<td>{', '.join('₹' + format(x, ',.0f') for x in ctx['supports']) or '—'}</td>"
            f"<td>{', '.join('₹' + format(x, ',.0f') for x in ctx['resistances']) or '—'}</td>"
            f"<td>{esc(fib_text)}</td><td>{esc('; '.join(ctx['signals']))}</td></tr>"
        )

    html_body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NIFTY Option Strategy Paper Trade Report</title>
<style>
body{{font-family:Inter,system-ui,-apple-system,sans-serif;background:#f4f7fb;color:#172033;margin:0;padding:24px}}
main{{max-width:1500px;margin:auto}} h1{{margin:0 0 4px}} h2{{margin-top:28px}}
.muted{{color:#637083;font-size:.9em}} .notice{{background:#fff4d6;border:1px solid #e6c76b;padding:12px 16px;border-radius:8px}}
.hero,.cards{{display:flex;gap:12px;flex-wrap:wrap}} .metric,.card{{background:#fff;border:1px solid #dfe5ee;border-radius:10px;padding:14px;box-shadow:0 2px 8px #1720330d}}
.metric{{min-width:145px}} .metric b{{display:block;font-size:1.35rem;margin-top:4px}} .cards .card{{flex:1;min-width:260px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;display:block;overflow-x:auto}}
th,td{{padding:9px 10px;border-bottom:1px solid #e8edf3;text-align:left;white-space:nowrap;font-size:.86rem}} th{{background:#edf2f7;position:sticky;top:0}}
.open{{color:#087443;font-weight:700}} .closed{{color:#9b2c2c;font-weight:700}} .bull{{color:#087443}} .bear{{color:#a12626}}
footer{{margin-top:30px;color:#637083;font-size:.85rem}}
</style></head><body><main>
<h1>NIFTY Option Strategy Paper Trade Report</h1>
<p class="muted">Generated {esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))} · Spot ₹{spot:,.2f} · Lot size assumption: {lot_size}</p>
<div class="notice"><b>Paper trading only:</b> These are analytical scenarios, not orders or investment advice.
Prices use NSE option-chain marks; slippage, brokerage, taxes, liquidity and gap risk are not included.
“Max profit” is the expiry payoff ceiling for defined-risk spreads/condors. A long straddle has unlimited upside but a defined debit risk.</div>
<div class="hero" style="margin-top:14px">
<div class="metric">Daily direction<b class="{direction.lower().replace('-', '')}">{esc(direction)}</b></div>
<div class="metric">Weekly view<b>{esc(contexts['Weekly']['direction'])}</b></div>
<div class="metric">Monthly view<b>{esc(contexts['Monthly']['direction'])}</b></div>
<div class="metric">Open paper trades<b>{len(open_trades)}</b></div>
<div class="metric">Closed history<b>{len(closed)}</b></div></div>
<h2>Conclusion by horizon</h2><div class="cards">{''.join(rec_cards)}</div>
<h2>Trend, support/resistance and Fibonacci context</h2>
<table><thead><tr><th>Timeframe</th><th>Direction</th><th>Support</th><th>Resistance</th><th>Fibonacci levels</th><th>Signals</th></tr></thead>
<tbody>{technical_rows}</tbody></table>
<h2>Strategy comparison</h2>
<table><thead><tr><th>Horizon</th><th>Strategy / bias</th><th>Expiry</th><th>Legs</th><th>Premium / credit</th><th>Max profit</th><th>Max loss</th><th>Breakevens</th><th>Status</th><th>Current P&amp;L</th></tr></thead>
<tbody>{strategy_rows}</tbody></table>
<h2>Open paper trades</h2>
<table><thead><tr><th>Horizon</th><th>Strategy / bias</th><th>Expiry</th><th>Legs</th><th>Premium / credit</th><th>Max profit</th><th>Max loss</th><th>Breakevens</th><th>Status</th><th>Current P&amp;L</th></tr></thead>
<tbody>{open_rows or '<tr><td colspan="10">No open paper trades.</td></tr>'}</tbody></table>
<h2>Closed paper trades</h2>
<table><thead><tr><th>Horizon</th><th>Strategy / bias</th><th>Expiry</th><th>Legs</th><th>Premium / credit</th><th>Max profit</th><th>Max loss</th><th>Breakevens</th><th>Status</th><th>Current P&amp;L</th></tr></thead>
<tbody>{closed_rows or '<tr><td colspan="10">No closed paper trades yet.</td></tr>'}</tbody></table>
<footer>State is persisted in option_paper_state.json. Trades close at expiry, at a 75% defined-risk stop, or at a 75% defined-profit target. This report does not place trades.</footer>
</main></body></html>"""
    REPORT_FILE.write_text(html_body, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily NIFTY option strategy paper-trade report")
    parser.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE)
    args = parser.parse_args()
    if args.lot_size <= 0:
        raise SystemExit("--lot-size must be positive")

    chain = _fetch_option_chain()
    history = _fetch_nifty_history()
    contexts = _all_contexts(history)
    candidates = _build_candidates(chain, contexts, args.lot_size)
    open_trades, closed = _update_paper_trades(candidates, chain, chain["spot"])
    recommendations = _recommendations(candidates, contexts)
    _render_report(candidates, open_trades, closed, contexts, chain["spot"], recommendations, args.lot_size)
    print(f"Report written to: {REPORT_FILE}")
    print(f"Paper state written to: {STATE_FILE}")
    print(f"Priced strategies: {len(candidates)} | open: {len(open_trades)} | closed: {len(closed)}")


if __name__ == "__main__":
    main()