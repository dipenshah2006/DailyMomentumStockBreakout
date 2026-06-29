#!/usr/bin/env python3
"""
Multibagger Report — NSE Strong Momentum Scanner
Scans NSE stocks for ATH Breakout + Multi-TF RSI + MACD + Fibonacci + Darvas Box
Outputs: multibagger_report.html
"""

import os
import sys
import csv
import time
import warnings
import pickle
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import yfinance as yf
import pytz

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_HTML     = "multibagger_report.html"
LOCAL_NSE_CSV   = "india/NSE/NSECash/EQUITY_L.csv"
DATA_YEARS      = 10
BATCH_SIZE      = 20
BATCH_PAUSE     = 1.0
MAX_WORKERS     = 4
CACHE_FILE      = "multibagger_cache.pkl"
USE_CACHE       = True
CACHE_MAX_AGE_H = 6
IST             = pytz.timezone("Asia/Kolkata")

# ── Stock List Loader ─────────────────────────────────────────────────────────
def load_nse_tickers():
    tickers = []
    if os.path.exists(LOCAL_NSE_CSV):
        try:
            with open(LOCAL_NSE_CSV, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym    = (row.get("SYMBOL") or row.get("Symbol") or "").strip()
                    series = (row.get("SERIES") or row.get("Series") or "EQ").strip()
                    name   = (row.get("NAME OF COMPANY") or row.get("Company Name") or sym).strip()
                    if sym and series == "EQ":
                        tickers.append((sym, name))
            print(f"  Loaded {len(tickers)} NSE EQ stocks from CSV")
            return tickers
        except Exception as e:
            print(f"  Warning: CSV error: {e}")

    fallback = [
        ("RELIANCE","Reliance Industries"),("TCS","Tata Consultancy Services"),
        ("HDFCBANK","HDFC Bank"),("INFY","Infosys"),("HINDUNILVR","Hindustan Unilever"),
        ("ICICIBANK","ICICI Bank"),("SBIN","State Bank of India"),("BAJFINANCE","Bajaj Finance"),
        ("BHARTIARTL","Bharti Airtel"),("KOTAKBANK","Kotak Mahindra Bank"),
        ("LICI","LIC of India"),("WIPRO","Wipro"),("HCLTECH","HCL Technologies"),
        ("LT","Larsen & Toubro"),("AXISBANK","Axis Bank"),("ASIANPAINT","Asian Paints"),
        ("MARUTI","Maruti Suzuki"),("TITAN","Titan Company"),("SUNPHARMA","Sun Pharmaceutical"),
        ("NTPC","NTPC"),("POWERGRID","Power Grid"),("ULTRACEMCO","UltraTech Cement"),
        ("TECHM","Tech Mahindra"),("BAJAJFINSV","Bajaj Finserv"),("NESTLEIND","Nestle India"),
        ("HINDALCO","Hindalco"),("ADANIENTS","Adani Enterprises"),("JSWSTEEL","JSW Steel"),
        ("TATAMOTORS","Tata Motors"),("ADANIPORTS","Adani Ports"),
        ("ONGC","ONGC"),("COALINDIA","Coal India"),("GRASIM","Grasim Industries"),
        ("CIPLA","Cipla"),("DIVISLAB","Divi's Laboratories"),("DRREDDY","Dr. Reddy's"),
        ("BPCL","BPCL"),("HEROMOTOCO","Hero MotoCorp"),("TATACONSUM","Tata Consumer"),
        ("INDUSINDBK","IndusInd Bank"),("EICHERMOT","Eicher Motors"),("APOLLOHOSP","Apollo Hospitals"),
        ("BRITANNIA","Britannia"),("BAJAJ-AUTO","Bajaj Auto"),("HDFCLIFE","HDFC Life"),
        ("SBILIFE","SBI Life"),("TATASTEEL","Tata Steel"),("M&M","Mahindra & Mahindra"),
        ("PIDILITIND","Pidilite Industries"),("HAVELLS","Havells India"),
    ]
    print(f"  Using fallback list of {len(fallback)} stocks")
    return fallback

# ── Cache helpers ─────────────────────────────────────────────────────────────
def load_cache():
    if not USE_CACHE or not os.path.exists(CACHE_FILE):
        return {}
    try:
        age_h = (time.time() - os.path.getmtime(CACHE_FILE)) / 3600
        if age_h > CACHE_MAX_AGE_H:
            print(f"  Cache expired ({age_h:.1f}h old), fetching fresh data")
            return {}
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"  Cache loaded: {len(data)} stocks ({age_h:.1f}h old)")
        return data
    except Exception:
        return {}

def save_cache(data):
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(data, f)
    except Exception as e:
        print(f"  Cache save failed: {e}")

# ── RSI ───────────────────────────────────────────────────────────────────────
def calc_rsi(series, period=14):
    if len(series) < period + 1:
        return float("nan")
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return float((100 - (100 / (1 + rs))).iloc[-1])

# ── MACD (ultra-slow) ─────────────────────────────────────────────────────────
def calc_macd(series, fast=34, slow=1000, signal=20):
    if len(series) < slow:
        return float("nan"), float("nan")
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1])

# ── Fibonacci Extensions ──────────────────────────────────────────────────────
def calc_fib(df):
    swing_low  = float(df["Low"].rolling(252).min().iloc[-1])
    swing_high = float(df["High"].rolling(252).max().iloc[-1])
    rng = swing_high - swing_low
    return {
        "low":    swing_low,
        "high":   swing_high,
        "f0618":  swing_high + rng * 0.618,
        "f1618":  swing_high + rng * 1.618,
        "f2618":  swing_high + rng * 2.618,
        "f4236":  swing_high + rng * 4.236,
    }

# ── Darvas Box ────────────────────────────────────────────────────────────────
def calc_darvas_box(df, n_confirm=3, lookback=60):
    """
    Darvas Box for a given OHLC DataFrame.
    Scans the last `lookback` bars to find the most recent valid box.
    A box top is confirmed when a high is NOT exceeded for n_confirm consecutive bars.
    A box bottom is confirmed when a low holds up for n_confirm consecutive bars after top.
    Returns dict: top, bottom, status ('BREAKOUT'|'BREAKDOWN'|'INSIDE'|'FORMING'|'N/A'), width_pct
    """
    if df is None or len(df) < n_confirm + 5:
        return {"top": None, "bottom": None, "status": "N/A", "width_pct": None}

    data = df.tail(max(lookback, n_confirm + 10)).reset_index(drop=True)
    highs  = data["High"].values
    lows   = data["Low"].values
    close  = float(data["Close"].iloc[-1])
    n      = len(data)

    box_top    = None
    box_bottom = None
    top_idx    = None

    # Scan right-to-left to find the most recent confirmed box top
    for i in range(n - n_confirm - 1, 0, -1):
        if all(highs[i + 1: i + n_confirm + 1] < highs[i]):
            box_top = float(highs[i])
            top_idx = i
            break

    if box_top is None:
        return {"top": None, "bottom": None, "status": "FORMING", "width_pct": None}

    # Find box bottom: lowest low in the formation window that holds for n_confirm bars
    sub_lows = lows[top_idx:]
    for j in range(len(sub_lows) - n_confirm):
        if all(sub_lows[j + 1: j + n_confirm + 1] > sub_lows[j]):
            box_bottom = float(sub_lows[j])
            break

    if box_bottom is None:
        box_bottom = float(np.min(sub_lows))

    # Determine status
    if close > box_top:
        status = "BREAKOUT"
    elif close < box_bottom:
        status = "BREAKDOWN"
    else:
        status = "INSIDE"

    width_pct = round((box_top - box_bottom) / box_bottom * 100, 1) if box_bottom else None

    return {
        "top":       round(box_top, 2),
        "bottom":    round(box_bottom, 2),
        "status":    status,
        "width_pct": width_pct,
    }

def darvas_for_timeframes(df_daily):
    """Compute Darvas Box for Daily, Weekly, Monthly resampled data."""
    results = {}
    for tf, freq in [("Daily", "B"), ("Weekly", "W"), ("Monthly", "ME")]:
        try:
            if freq == "B":
                df_tf = df_daily.copy()
            else:
                df_tf = df_daily.resample(freq).agg({
                    "Open":   "first",
                    "High":   "max",
                    "Low":    "min",
                    "Close":  "last",
                    "Volume": "sum",
                }).dropna()
            results[tf] = calc_darvas_box(df_tf, n_confirm=3 if tf == "Daily" else 2, lookback=60)
        except Exception:
            results[tf] = {"top": None, "bottom": None, "status": "N/A", "width_pct": None}
    return results

# ── Signal + Score ────────────────────────────────────────────────────────────
def compute_signal(drsi, wrsi, mrsi, macd_v, macd_s, ath_pct, vol_ratio, darvas_d):
    signal = "WATCH"
    score  = 0

    if not np.isnan(mrsi)  and mrsi  > 60: score += 20
    if not np.isnan(mrsi)  and mrsi  > 70: score += 10
    if not np.isnan(wrsi)  and wrsi  > 55: score += 15
    if not np.isnan(drsi)  and drsi  > 60: score += 10
    if not np.isnan(macd_v) and macd_v > 0: score += 10
    if not np.isnan(macd_v) and macd_s and macd_v > macd_s: score += 5
    if ath_pct >= -2:  score += 20
    if vol_ratio > 2.5: score += 10
    if darvas_d.get("status") == "BREAKOUT": score += 15
    if darvas_d.get("status") == "INSIDE":   score += 5

    # Signal label
    if ath_pct >= -2 and not np.isnan(mrsi) and mrsi > 70:
        signal = "🚀 STRONG BUY"
    elif not np.isnan(macd_v) and macd_v > 0 and not np.isnan(mrsi) and mrsi > 60:
        signal = "🌊 MACD MEGA BUY"
    elif not np.isnan(drsi) and drsi > 60 and not np.isnan(wrsi) and wrsi > 55 and vol_ratio > 1.5:
        signal = "✅ BUY"
    elif vol_ratio > 2.5:
        signal = "🔥 VOL BUY"
    elif not np.isnan(mrsi) and mrsi > 70:
        signal = "💜 M-RSI BUY"
    elif darvas_d.get("status") == "BREAKOUT":
        signal = "📦 DARVAS BUY"

    return signal, score

# ── Backtest ──────────────────────────────────────────────────────────────────
def run_backtest(df_daily):
    trades  = []
    in_trade = None
    close    = df_daily["Close"]
    highs    = df_daily["High"]
    lows     = df_daily["Low"]
    vol      = df_daily["Volume"]
    avg_vol  = vol.rolling(20).mean()

    for i in range(50, len(df_daily) - 1):
        if in_trade:
            entry_price = in_trade["entry"]
            # Exit: stop-loss -8% or take-profit +25%
            if lows.iloc[i] < entry_price * 0.92:
                ret = round((entry_price * 0.92 / entry_price - 1) * 100, 1)
                trades.append({**in_trade, "exit_date": df_daily.index[i].strftime("%d %b %Y"),
                                "exit_price": round(entry_price * 0.92, 1), "return_pct": ret,
                                "days": (df_daily.index[i] - pd.Timestamp(in_trade["entry_date"])).days})
                in_trade = None
            elif highs.iloc[i] > entry_price * 1.25:
                ret = round((entry_price * 1.25 / entry_price - 1) * 100, 1)
                trades.append({**in_trade, "exit_date": df_daily.index[i].strftime("%d %b %Y"),
                                "exit_price": round(entry_price * 1.25, 1), "return_pct": ret,
                                "days": (df_daily.index[i] - pd.Timestamp(in_trade["entry_date"])).days})
                in_trade = None
            continue

        sub  = close.iloc[:i+1]
        drsi = calc_rsi(sub.tail(60), 14)
        vr   = vol.iloc[i] / avg_vol.iloc[i] if avg_vol.iloc[i] > 0 else 1

        if not np.isnan(drsi) and drsi > 60 and vr > 1.5:
            in_trade = {"signal": "✅ BUY", "entry_date": df_daily.index[i].strftime("%d %b %Y"),
                        "entry": round(float(close.iloc[i]), 1)}
        elif vr > 2.5:
            in_trade = {"signal": "🔥 VOL BUY", "entry_date": df_daily.index[i].strftime("%d %b %Y"),
                        "entry": round(float(close.iloc[i]), 1)}

    stats = {}
    if trades:
        rets = [t["return_pct"] for t in trades]
        wins = [r for r in rets if r > 0]
        stats = {
            "trades":   len(trades),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "avg_ret":  round(sum(rets) / len(rets), 1),
            "best_ret": round(max(rets), 1),
        }
    return trades, stats

# ── Analyse one stock ─────────────────────────────────────────────────────────
def analyse(symbol, name, cache):
    yf_sym = symbol + ".NS"
    if yf_sym in cache:
        return cache[yf_sym]

    try:
        end   = datetime.today()
        start = end - timedelta(days=DATA_YEARS * 365 + 90)
        df = yf.download(
            yf_sym,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False, auto_adjust=True
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 50:
            return None

        df = df[["Open","High","Low","Close","Volume"]].dropna()
        close = df["Close"]
        vol   = df["Volume"]

        # ATH
        ath_price  = float(df["High"].max())
        last_close = float(close.iloc[-1])
        is_ath     = last_close >= ath_price * 0.99
        ath_pct    = round((last_close / ath_price - 1) * 100, 1)

        # RSI (daily / weekly / monthly resample)
        drsi = calc_rsi(close.tail(100), 14)

        weekly  = close.resample("W").last().dropna()
        wrsi    = calc_rsi(weekly.tail(104), 14)

        monthly = close.resample("ME").last().dropna()
        mrsi    = calc_rsi(monthly.tail(60), 14)

        # MACD ultra-slow (monthly close)
        macd_v, macd_s = calc_macd(monthly, 34, 1000, 20)

        # Fibonacci
        fib = calc_fib(df)

        # Volume ratio
        avg_vol  = float(vol.rolling(20).mean().iloc[-1])
        last_vol = float(vol.iloc[-1])
        vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 1.0

        # ── Darvas Box (Daily / Weekly / Monthly) ──
        darvas = darvas_for_timeframes(df)

        # Blast-off: all three timeframes show breakout
        darvas_statuses = [darvas[tf]["status"] for tf in ["Daily","Weekly","Monthly"]]
        blast_off = all(s == "BREAKOUT" for s in darvas_statuses)

        # Signal & Score
        signal, score = compute_signal(drsi, wrsi, mrsi, macd_v, macd_s, ath_pct, vol_ratio, darvas["Daily"])
        if blast_off:
            score += 20
            signal = "💥 DARVAS BLAST"

        # Backtest
        trades, bt = run_backtest(df)

        result = {
            "symbol": symbol, "name": name, "close": round(last_close, 2),
            "ath_pct": ath_pct, "is_ath": is_ath,
            "drsi": round(drsi, 1) if not np.isnan(drsi) else None,
            "wrsi": round(wrsi, 1) if not np.isnan(wrsi) else None,
            "mrsi": round(mrsi, 1) if not np.isnan(mrsi) else None,
            "macd": round(macd_v, 4) if not np.isnan(macd_v) else None,
            "fib": fib, "vol_ratio": vol_ratio,
            "darvas": darvas, "blast_off": blast_off,
            "signal": signal, "score": score,
            "trades": trades, "bt": bt,
        }
        cache[yf_sym] = result
        return result

    except Exception as e:
        print(f"  Error {symbol}: {e}")
        return None

# ── HTML helpers ──────────────────────────────────────────────────────────────
def _rsi_color(v):
    if v is None: return "#8b949e"
    if v >= 70:   return "#26d07c"
    if v >= 55:   return "#f0b429"
    return "#ff6b6b"

def _signal_badge(sig):
    colors = {
        "🚀 STRONG BUY":    ("#26d07c","#0d2615","#26d07c44"),
        "🌊 MACD MEGA BUY": ("#00d4ff","#001a20","#00d4ff44"),
        "✅ BUY":            ("#26d07c","#0d2615","#26d07c44"),
        "🔥 VOL BUY":       ("#fbbf24","#2a1e00","#fbbf2444"),
        "💜 M-RSI BUY":     ("#c084fc","#1a0d2a","#c084fc44"),
        "📦 DARVAS BUY":    ("#60a5fa","#0d1a2a","#60a5fa44"),
        "💥 DARVAS BLAST":  ("#ff8c00","#1a0900","#ff8c0044"),
        "WATCH":            ("#8b949e","#161b22","#30363d"),
    }
    fg, bg, bd = colors.get(sig, ("#8b949e","#161b22","#30363d"))
    return (f'<span style="background:{bg};color:{fg};border:1px solid {bd};border-radius:12px;'
            f'padding:2px 10px;font-size:11px;font-weight:700;white-space:nowrap">{sig}</span>')

def _darvas_badge(status):
    colors = {
        "BREAKOUT":  ("#26d07c","#0d2615","#26d07c44"),
        "INSIDE":    ("#f0b429","#1a1200","#f0b42944"),
        "BREAKDOWN": ("#ff6b6b","#200808","#ff6b6b44"),
        "FORMING":   ("#8b949e","#161b22","#30363d"),
        "N/A":       ("#555","#161b22","#333"),
    }
    fg, bg, bd = colors.get(status, ("#555","#161b22","#333"))
    label = {"BREAKOUT":"▲ OUT","INSIDE":"▬ IN","BREAKDOWN":"▼ DOWN","FORMING":"~ FORM","N/A":"—"}.get(status, status)
    return (f'<span style="background:{bg};color:{fg};border:1px solid {bd};border-radius:10px;'
            f'padding:2px 8px;font-size:10px;font-weight:700;white-space:nowrap">{label}</span>')

def _trade_table(trades):
    if not trades:
        return '<p style="color:#8b949e;font-size:12px;margin-top:12px">No completed backtest trades found.</p>'
    rows = []
    for t in trades[-10:]:
        rc = "#26d07c" if t["return_pct"] > 0 else "#ff6b6b"
        rows.append(
            f'<tr>'
            f'<td>{t["entry_date"]}</td>'
            f'<td>{_signal_badge(t["signal"])}</td>'
            f'<td style="text-align:right">₹{t["entry"]}</td>'
            f'<td>{t.get("exit_date","Open")}</td>'
            f'<td style="text-align:right">₹{t.get("exit_price","—")}</td>'
            f'<td style="text-align:right;color:{rc};font-weight:700">'
            f'{"+" if t["return_pct"]>0 else ""}{t["return_pct"]}%</td>'
            f'<td style="text-align:right;color:#8b949e">{t.get("days","—")}d</td>'
            f'</tr>'
        )
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:11.5px">'
        '<tr style="color:#8b949e;font-size:10px">'
        '<th style="text-align:left;padding:4px 8px">Entry Date</th>'
        '<th style="text-align:left;padding:4px 8px">Signal</th>'
        '<th style="text-align:right;padding:4px 8px">Entry ₹</th>'
        '<th style="text-align:left;padding:4px 8px">Exit Date</th>'
        '<th style="text-align:right;padding:4px 8px">Exit ₹</th>'
        '<th style="text-align:right;padding:4px 8px">Return</th>'
        '<th style="text-align:right;padding:4px 8px">Days</th>'
        '</tr>' + "".join(rows) + '</table>'
    )

def _darvas_detail_card(tf, dv):
    top    = f"₹{dv['top']:,.1f}"    if dv["top"]    else "—"
    bottom = f"₹{dv['bottom']:,.1f}" if dv["bottom"] else "—"
    width  = f"{dv['width_pct']}%"   if dv["width_pct"] else "—"
    status = dv["status"]
    status_col = {"BREAKOUT":"#26d07c","BREAKDOWN":"#ff6b6b","INSIDE":"#f0b429",
                  "FORMING":"#8b949e","N/A":"#555"}.get(status,"#8b949e")
    tf_icon = {"Daily":"📅","Weekly":"📆","Monthly":"🗓️"}.get(tf,"📊")
    return (
        f'<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;min-width:160px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">'
        f'{tf_icon} {tf} Darvas</div>'
        f'<div style="color:{status_col};font-size:13px;font-weight:700;margin-bottom:6px">{status}</div>'
        f'<div style="display:flex;justify-content:space-between;font-size:11px;color:#8b949e;margin-bottom:2px">'
        f'<span>Box Top:</span><span style="color:#e6edf3;font-weight:600">{top}</span></div>'
        f'<div style="display:flex;justify-content:space-between;font-size:11px;color:#8b949e;margin-bottom:2px">'
        f'<span>Box Bottom:</span><span style="color:#e6edf3;font-weight:600">{bottom}</span></div>'
        f'<div style="display:flex;justify-content:space-between;font-size:11px;color:#8b949e">'
        f'<span>Width:</span><span style="color:#c084fc;font-weight:600">{width}</span></div>'
        f'</div>'
    )

# ── Build HTML row ────────────────────────────────────────────────────────────
def build_row(idx, r):
    sym   = r["symbol"]
    name  = r["name"]
    close = r["close"]
    fib   = r["fib"]
    bt    = r["bt"]
    darvas = r["darvas"]

    ath_col = "#26d07c" if r["is_ath"] else ("#f0b429" if r["ath_pct"] >= -5 else "#ff6b6b")
    ath_str = ("🔥 ATH" if r["is_ath"] else f'{r["ath_pct"]}%')

    drsi_str = f'{r["drsi"]}' if r["drsi"] else "—"
    wrsi_str = f'{r["wrsi"]}' if r["wrsi"] else "—"
    mrsi_str = f'{r["mrsi"]}' if r["mrsi"] else "—"
    macd_str = f'{r["macd"]:.3f}' if r["macd"] is not None else "—"
    macd_col = "#26d07c" if (r["macd"] and r["macd"] > 0) else "#ff6b6b"

    blast_cell = ('💥' if r["blast_off"] else '—')

    d_dv = darvas.get("Daily",   {})
    w_dv = darvas.get("Weekly",  {})
    m_dv = darvas.get("Monthly", {})

    trades_str  = str(bt.get("trades","—"))
    winrate_str = f'{bt.get("win_rate","—")}%' if "win_rate" in bt else "—"
    avgret_str  = (f'+{bt["avg_ret"]}%' if bt.get("avg_ret",0)>0 else f'{bt.get("avg_ret","—")}%') if "avg_ret" in bt else "—"
    bestret_str = f'+{bt["best_ret"]}%' if "best_ret" in bt else "—"
    avgret_col  = "#26d07c" if bt.get("avg_ret",0) > 0 else "#ff6b6b"

    detail_id = f"d{idx}"

    # Detail section HTML
    detail_html = (
        f'<tr id="{detail_id}" style="display:none"><td colspan="23" style="background:#010409;padding:20px 24px">'
        # Fib targets row
        f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px">'
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Swing Low</div>'
        f'<div style="color:#e6edf3;font-size:16px;font-weight:700">₹{fib["low"]:,.1f}</div></div>'
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Swing High</div>'
        f'<div style="color:#e6edf3;font-size:16px;font-weight:700">₹{fib["high"]:,.1f}</div></div>'
        f'<div style="background:#002d1a;border:1px solid #26d07c44;border-radius:8px;padding:12px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Fib 1.618x Target</div>'
        f'<div style="color:#26d07c;font-size:16px;font-weight:700">₹{fib["f1618"]:,.1f}</div></div>'
        f'<div style="background:#2a1200;border:1px solid #dc262644;border-radius:8px;padding:12px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Fib 4.236x Target</div>'
        f'<div style="color:#dc2626;font-size:16px;font-weight:700">₹{fib["f4236"]:,.1f}</div></div>'
        f'</div>'
        # ── Darvas Box detail ──
        f'<div style="margin-bottom:16px">'
        f'<h4 style="color:#60a5fa;font-size:12px;margin-bottom:10px">📦 Darvas Box Analysis — Daily · Weekly · Monthly</h4>'
        f'<div style="display:flex;gap:12px;flex-wrap:wrap">'
        + _darvas_detail_card("Daily",   d_dv)
        + _darvas_detail_card("Weekly",  w_dv)
        + _darvas_detail_card("Monthly", m_dv)
        + f'</div></div>'
        # Backtest trades
        f'<div><h4 style="color:#8b949e;font-size:12px;margin-bottom:8px">📋 Backtest Trades (last 10)</h4>'
        + _trade_table(r["trades"])
        + f'</div>'
        f'</td></tr>'
    )

    row_html = (
        f'<tr data-name="{name.lower()}" data-score="{r["score"]}" '
        f'data-mrsi="{r["mrsi"] or 0}" data-wrsi="{r["wrsi"] or 0}" '
        f'data-drsi="{r["drsi"] or 0}" data-ath="{r["ath_pct"]}">'
        f'<td>{idx}</td>'
        f'<td><b style="color:#e6edf3">{sym}</b><br>'
        f'<span style="color:#8b949e;font-size:11px">{name[:30]}</span></td>'
        f'<td style="text-align:right">₹{close:,.2f}</td>'
        f'<td style="text-align:right;color:{ath_col}">{ath_str}</td>'
        f'<td style="text-align:right;color:{_rsi_color(r["drsi"])}">{drsi_str}</td>'
        f'<td style="text-align:right;color:{_rsi_color(r["wrsi"])}">{wrsi_str}</td>'
        f'<td style="text-align:right;color:{_rsi_color(r["mrsi"])}">{mrsi_str}</td>'
        f'<td style="text-align:right;color:{macd_col}">{macd_str}</td>'
        # Darvas Box columns (Daily / Weekly / Monthly)
        f'<td style="text-align:center">{_darvas_badge(d_dv.get("status","N/A"))}</td>'
        f'<td style="text-align:center">{_darvas_badge(w_dv.get("status","N/A"))}</td>'
        f'<td style="text-align:center">{_darvas_badge(m_dv.get("status","N/A"))}</td>'
        # Fib columns
        f'<td style="text-align:right;color:#8b949e">₹{fib["f0618"]:,.0f}</td>'
        f'<td style="text-align:right;color:#26d07c">₹{fib["f1618"]:,.0f}</td>'
        f'<td style="text-align:right;color:#f0b429">₹{fib["f2618"]:,.0f}</td>'
        f'<td style="text-align:right;color:#ff6b6b">₹{fib["f4236"]:,.0f}</td>'
        # Signal / Score
        f'<td style="text-align:left">{_signal_badge(r["signal"])}</td>'
        f'<td style="text-align:right;color:#00d4ff;font-weight:700">{r["score"]}</td>'
        f'<td style="text-align:center;font-size:16px">{blast_cell}</td>'
        # Backtest stats
        f'<td style="text-align:right;color:#8b949e">{trades_str}</td>'
        f'<td style="text-align:right;color:#8b949e">{winrate_str}</td>'
        f'<td style="text-align:right;color:{avgret_col}">{avgret_str}</td>'
        f'<td style="text-align:right;color:#26d07c">{bestret_str}</td>'
        # Expand
        f'<td><button onclick="toggleDetail(\'{detail_id}\')" '
        f'style="background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:8px;'
        f'padding:3px 10px;cursor:pointer;font-size:11px">▼ Detail</button></td>'
        f'</tr>'
        + detail_html
    )
    return row_html

# ── HTML Report ───────────────────────────────────────────────────────────────
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multibagger Report — {run_ts}</title>
<style>
:root {{
  --bg:#0d1117; --card:#161b22; --border:#21262d; --text:#e6edf3;
  --sub:#8b949e; --cyan:#00d4ff; --green:#26d07c; --gold:#f0b429;
  --red:#ff6b6b; --purple:#c084fc; --blue:#60a5fa;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}}
.header{{background:#010409;border-bottom:2px solid var(--border);padding:18px 24px 14px}}
.header h1{{font-size:21px;font-weight:700;color:var(--cyan);letter-spacing:1px}}
.subtitle{{color:var(--sub);font-size:11.5px;margin-top:4px}}
.nav-links{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}
.nav-link{{display:inline-flex;align-items:center;gap:5px;background:var(--card);border:1px solid var(--border);
           color:var(--text);border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;
           text-decoration:none;transition:all .15s}}
.nav-link:hover{{border-color:var(--cyan);color:var(--cyan)}}
.nav-link.active{{background:var(--cyan);color:#000;border-color:var(--cyan)}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;padding:16px 24px}}
.stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 18px;min-width:110px}}
.stat .val{{font-size:24px;font-weight:700}}
.stat .lbl{{font-size:10px;color:var(--sub);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}}
.strategy-box{{margin:0 24px 16px;background:#0a1628;border:1px solid #1e3a5f;border-radius:10px;padding:14px 18px}}
.strategy-box h3{{color:var(--cyan);font-size:13px;margin-bottom:8px}}
.strategy-box ul{{color:var(--sub);font-size:11.5px;line-height:1.8;padding-left:18px}}
.strategy-box li span{{color:var(--text)}}
.darvas-box{{margin:0 24px 16px;background:#060d1a;border:1px solid #1a3055;border-radius:10px;padding:14px 18px}}
.darvas-box h3{{color:var(--blue);font-size:13px;margin-bottom:8px}}
.darvas-box ul{{color:var(--sub);font-size:11.5px;line-height:1.8;padding-left:18px}}
.darvas-box li span{{color:var(--text)}}
.filter-bar{{padding:10px 24px;background:#010409;border-bottom:1px solid var(--border);
             position:sticky;top:0;z-index:1000;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.filter-bar input{{flex:1;min-width:180px;max-width:260px;background:var(--card);border:1px solid var(--border);
                   color:var(--text);border-radius:20px;padding:5px 14px;font-size:12px;outline:none}}
.filter-bar input:focus{{border-color:var(--cyan)}}
.sort-btn{{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;
           padding:4px 12px;cursor:pointer;font-size:11.5px;transition:all .15s;font-weight:600}}
.sort-btn:hover,.sort-btn.active{{background:var(--cyan);color:#000;border-color:var(--cyan)}}
table{{width:100%;border-collapse:collapse}}
th{{background:#010409;padding:9px 10px;text-align:right;font-size:11px;color:var(--sub);
    text-transform:uppercase;letter-spacing:.5px;cursor:pointer;white-space:nowrap;border-bottom:1px solid var(--border)}}
th:hover{{color:var(--cyan)}}
th.asc::after{{content:" ▲"}}th.desc::after{{content:" ▼"}}
th:first-child,th:nth-child(2),th:last-child{{text-align:left}}
td{{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr:hover td{{background:#161b2288}}
.footer{{text-align:center;padding:24px;color:var(--sub);font-size:11px;border-top:1px solid var(--border);margin-top:20px}}
</style>
</head>
<body>
<div class="header">
  <h1>🏆 Multibagger Report — NSE Strong Momentum Scanner</h1>
  <div class="subtitle">
    Strategy: ATH Breakout + Monthly RSI&gt;70 + Ultra-Slow MACD(34,1000,20) + Fibonacci + 📦 Darvas Box (D/W/M)
    &nbsp;|&nbsp; {run_ts} IST &nbsp;|&nbsp; Multi-Timeframe: Daily · Weekly · Monthly
  </div>
  <div class="nav-links">
    <a class="nav-link" href="/">📊 Full RSI Report</a>
    <a class="nav-link" href="/ath">🏆 ATH Breakout</a>
    <a class="nav-link active" href="/multibagger">💎 Multibagger</a>
    <a class="nav-link" href="/rocket">🚀 Rocket Scanner</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="val" style="color:var(--cyan)">{n_total}</div><div class="lbl">Stocks Analysed</div></div>
  <div class="stat"><div class="val" style="color:var(--green)">{n_ath}</div><div class="lbl">ATH Breakouts</div></div>
  <div class="stat"><div class="val" style="color:var(--red)">{n_mrsi}</div><div class="lbl">M-RSI &gt; 70</div></div>
  <div class="stat"><div class="val" style="color:var(--gold)">{n_strong}</div><div class="lbl">Strong Signals</div></div>
  <div class="stat"><div class="val" style="color:var(--blue)">{n_darvas_out}</div><div class="lbl">Darvas Breakouts</div></div>
  <div class="stat"><div class="val" style="color:var(--purple)">{avg_score:.1f}</div><div class="lbl">Avg Score</div></div>
</div>

<div class="strategy-box">
  <h3>📌 Multibagger Strategy — How to Ride 5x–10x Trends</h3>
  <ul>
    <li><span>🚀 STRONG BUY</span> — ATH Breakout (new all-time high) AND Monthly RSI&gt;70: Highest conviction signal for mega-trends</li>
    <li><span>🌊 MACD MEGA BUY</span> — Ultra-slow MACD(34,1000,20) crosses above zero + M-RSI&gt;60: Structural trend confirmation</li>
    <li><span>✅ BUY</span> — Daily RSI crosses 60 + Weekly RSI&gt;55 + Volume surge: Fresh momentum entry in established uptrend</li>
    <li><span>🔥 VOL BUY</span> — Volume &gt;2.5x average + price above all EMAs: Institutional accumulation signal</li>
    <li><span>💜 M-RSI BUY</span> — Monthly RSI freshly crosses 70 + price above 50 EMA: Monthly momentum ignition</li>
    <li><span>💥 DARVAS BLAST</span> — Breakout confirmed on ALL 3 timeframes (Daily + Weekly + Monthly): Ultra-high conviction</li>
    <li><span>Fib Targets</span> — 0.618x (conservative), 1.618x (standard), 2.618x (aggressive), 4.236x (moonshot 5x–10x)</li>
  </ul>
</div>

<div class="darvas-box">
  <h3>📦 Darvas Box Strategy — Breakout Box Trading</h3>
  <ul>
    <li><span>How it works:</span> A Darvas Box forms when a price high is NOT exceeded for 3 consecutive bars (top confirmed), then a low holds for 3 bars (bottom confirmed)</li>
    <li><span>▲ BREAKOUT</span> — Price closes above the box top: Strong entry signal, momentum continuation expected</li>
    <li><span>▬ INSIDE</span> — Price is within the box: Coiling/consolidation phase, watch for breakout</li>
    <li><span>▼ BREAKDOWN</span> — Price closes below the box bottom: Caution, downward pressure</li>
    <li><span>💥 BLAST</span> — Breakout confirmed on Daily + Weekly + Monthly simultaneously: Mega-momentum signal</li>
    <li><span>Timeframes:</span> Daily boxes capture short-term swings; Weekly boxes show medium-term structure; Monthly boxes reveal the biggest institutional accumulation zones</li>
  </ul>
</div>

<div class="filter-bar">
  <input type="text" id="searchInput" placeholder="🔍 Search ticker or company…" oninput="filterTable()">
  <button class="sort-btn" onclick="sortBy('score')">Sort: Score</button>
  <button class="sort-btn" onclick="sortBy('mrsi')">Sort: M-RSI</button>
  <button class="sort-btn" onclick="sortBy('ath')">Sort: ATH%</button>
  <button class="sort-btn" onclick="sortBy('wrsi')">Sort: W-RSI</button>
  <button class="sort-btn" onclick="sortBy('drsi')">Sort: D-RSI</button>
  <span id="countInfo" style="color:var(--sub);font-size:11px;margin-left:4px"></span>
</div>

<table id="mainTable">
<thead>
<tr>
  <th onclick="thSort(0)">#</th>
  <th onclick="thSort(1)" style="text-align:left">Ticker / Company</th>
  <th onclick="thSort(2)">Close</th>
  <th onclick="thSort(3)">ATH%</th>
  <th onclick="thSort(4)">D-RSI</th>
  <th onclick="thSort(5)">W-RSI</th>
  <th onclick="thSort(6)">M-RSI</th>
  <th onclick="thSort(7)">MACD</th>
  <th onclick="thSort(8)" style="color:#60a5fa">📦 D-Box</th>
  <th onclick="thSort(9)" style="color:#60a5fa">📆 W-Box</th>
  <th onclick="thSort(10)" style="color:#60a5fa">🗓️ M-Box</th>
  <th onclick="thSort(11)">Fib 0.618x</th>
  <th onclick="thSort(12)">Fib 1.618x</th>
  <th onclick="thSort(13)">Fib 2.618x</th>
  <th onclick="thSort(14)">Fib 4.236x</th>
  <th onclick="thSort(15)" style="text-align:left">Signal</th>
  <th onclick="thSort(16)">Score</th>
  <th onclick="thSort(17)" style="color:#ff8c00">💥 Blast</th>
  <th onclick="thSort(18)">Trades</th>
  <th onclick="thSort(19)">Win Rate</th>
  <th onclick="thSort(20)">Avg Ret</th>
  <th onclick="thSort(21)">Best Ret</th>
  <th style="text-align:left">Detail</th>
</tr>
</thead>
<tbody id="tableBody">
{rows_html}
</tbody>
</table>

<div class="footer">
  Multibagger + Darvas Box Report v2.0 &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp;
  <b>Not financial advice.</b> For educational purposes only.<br>
  Strategy: ATH Breakout + M-RSI&gt;70 + Ultra-Slow MACD(34,1000,20) + Fibonacci Extensions + Darvas Box (Daily/Weekly/Monthly)
</div>

<script>
function toggleDetail(id){{
  const el=document.getElementById(id);
  el.style.display=el.style.display==='none'?'table-row':'none';
}}
function filterTable(){{
  const q=document.getElementById('searchInput').value.toLowerCase();
  const rows=document.querySelectorAll('#tableBody tr[data-name]');
  let shown=0;
  rows.forEach(r=>{{
    const match=r.dataset.name.toLowerCase().includes(q)||
                r.querySelector('b').textContent.toLowerCase().includes(q);
    r.style.display=match?'':'none';
    const nextId=r.querySelector('button')?.getAttribute('onclick')?.match(/'(d\\d+)'/)?.[1];
    if(nextId){{document.getElementById(nextId).style.display='none';}}
    if(match)shown++;
  }});
  document.getElementById('countInfo').textContent=`Showing ${{shown}} of {n_total}`;
}}
function sortBy(field){{
  const rows=[...document.querySelectorAll('#tableBody tr[data-name]')];
  const map={{score:'score',mrsi:'mrsi',wrsi:'wrsi',drsi:'drsi',ath:'ath'}};
  rows.sort((a,b)=>parseFloat(b.dataset[map[field]]||0)-parseFloat(a.dataset[map[field]]||0));
  const tbody=document.getElementById('tableBody');
  rows.forEach(r=>{{tbody.appendChild(r);}});
  document.querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
}}
let lastThSort=-1, lastThDir=1;
function thSort(col){{
  const tbody=document.getElementById('tableBody');
  const rows=[...tbody.querySelectorAll('tr[data-name]')];
  const dir=lastThSort===col?-lastThDir:1;
  lastThSort=col; lastThDir=dir;
  rows.sort((a,b)=>{{
    const av=a.cells[col]?.textContent.replace(/[₹,%+▲▼▬~ ]/g,'').trim()||'';
    const bv=b.cells[col]?.textContent.replace(/[₹,%+▲▼▬~ ]/g,'').trim()||'';
    const an=parseFloat(av), bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn)) return dir*(an-bn);
    return dir*av.localeCompare(bv);
  }});
  rows.forEach(r=>tbody.appendChild(r));
  document.querySelectorAll('th').forEach((h,i)=>{{
    h.className=i===col?(dir===1?'asc':'desc'):'';
  }});
}}
document.addEventListener('DOMContentLoaded',()=>{{
  document.getElementById('countInfo').textContent=`Showing {n_total} stocks`;
}});
</script>
</body>
</html>'''


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Multibagger Report + Darvas Box (D/W/M)")
    print("=" * 60)

    tickers = load_nse_tickers()
    cache   = load_cache()
    results = []
    errors  = 0

    print(f"\n  Scanning {len(tickers)} stocks with {MAX_WORKERS} workers…\n")

    def process(sym_name):
        sym, name = sym_name
        return analyse(sym, name, cache)

    batches = [tickers[i:i+BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    done    = 0

    for batch in batches:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(process, sn): sn for sn in batch}
            for fut in as_completed(futures):
                r = fut.result()
                done += 1
                if r:
                    results.append(r)
                    print(f"  [{done}/{len(tickers)}] {r['symbol']:12s} "
                          f"close=₹{r['close']:>9,.2f}  score={r['score']:3d}  "
                          f"signal={r['signal']:<20s}  "
                          f"D-Box={r['darvas']['Daily']['status']:<9s}"
                          f"W-Box={r['darvas']['Weekly']['status']:<9s}"
                          f"M-Box={r['darvas']['Monthly']['status']}")
                else:
                    errors += 1
                    print(f"  [{done}/{len(tickers)}] {futures[fut][0]:12s}  — skipped")
        save_cache(cache)
        if batch != batches[-1]:
            time.sleep(BATCH_PAUSE)

    # Sort by score desc
    results.sort(key=lambda x: x["score"], reverse=True)

    # Stats
    n_total      = len(results)
    n_ath        = sum(1 for r in results if r["is_ath"])
    n_mrsi       = sum(1 for r in results if r["mrsi"] and r["mrsi"] > 70)
    n_strong     = sum(1 for r in results if "BUY" in r["signal"] or "BLAST" in r["signal"])
    n_darvas_out = sum(1 for r in results
                       if any(r["darvas"][tf]["status"] == "BREAKOUT" for tf in ["Daily","Weekly","Monthly"]))
    avg_score    = sum(r["score"] for r in results) / n_total if n_total else 0

    run_ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M")

    # Build rows
    rows_html = ""
    for i, r in enumerate(results, 1):
        rows_html += build_row(i, r)

    html = HTML_TEMPLATE.format(
        run_ts=run_ts,
        n_total=n_total,
        n_ath=n_ath,
        n_mrsi=n_mrsi,
        n_strong=n_strong,
        n_darvas_out=n_darvas_out,
        avg_score=avg_score,
        rows_html=rows_html,
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n{'='*60}")
    print(f"  ✅ Report saved → {OUTPUT_HTML}")
    print(f"  Stocks: {n_total} | ATH: {n_ath} | M-RSI>70: {n_mrsi} | Strong: {n_strong} | Darvas Breakouts: {n_darvas_out}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
