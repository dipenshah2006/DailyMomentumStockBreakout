#!/usr/bin/env python3
"""
Multibagger Report v3.0 — NSE Cash + SME
Features:
  • ATH Breakout + Multi-TF RSI + MACD + Fibonacci + Darvas Box
  • Support / Resistance + Trend Channel (weekly & monthly)
  • Daily Blast — price breaks above S/R or trend-channel top on daily TF
  • Charts with overlays saved to charts/
  • Pushes output to GitHub when GITHUB_TOKEN is set
"""

import os, sys, csv, time, warnings, pickle, re, subprocess
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import pytz
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_HTML      = "multibagger_report.html"
NSE_CASH_CSV     = "india/NSE/NSECash/EQUITY_L.csv"
NSE_SME_CSV      = "india/NSE/NSESME/MW-SME-05-May-2026.csv"
DATA_YEARS       = 6
BATCH_SIZE       = 20
BATCH_PAUSE      = 1.0
MAX_WORKERS      = 4
CACHE_FILE       = "multibagger_cache.pkl"
USE_CACHE        = True
CACHE_MAX_AGE_H  = 6
CHARTS_DIR       = "charts"
IST              = pytz.timezone("Asia/Kolkata")

os.makedirs(CHARTS_DIR, exist_ok=True)

# ── Stock loaders ─────────────────────────────────────────────────────────────
def load_nse_cash():
    rows = []
    if not os.path.exists(NSE_CASH_CSV):
        return rows
    try:
        with open(NSE_CASH_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym    = (row.get("SYMBOL") or "").strip()
                series = (row.get("SERIES") or row.get(" SERIES") or "EQ").strip()
                name   = (row.get("NAME OF COMPANY") or sym).strip()
                if sym and series == "EQ":
                    rows.append((sym, name, "CASH"))
        print(f"  NSE Cash: {len(rows)} EQ stocks")
    except Exception as e:
        print(f"  NSE Cash error: {e}")
    return rows

def load_nse_sme():
    rows = []
    if not os.path.exists(NSE_SME_CSV):
        return rows
    try:
        with open(NSE_SME_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = (row.get("SYMBOL ") or row.get("SYMBOL") or "").strip().strip('"')
                if sym:
                    rows.append((sym, sym + " (SME)", "SME"))
        print(f"  NSE SME: {len(rows)} stocks")
    except Exception as e:
        print(f"  NSE SME error: {e}")
    return rows

NIFTY50_FALLBACK = [
    ("RELIANCE","Reliance Industries","CASH"),("TCS","Tata Consultancy Services","CASH"),
    ("HDFCBANK","HDFC Bank","CASH"),("INFY","Infosys","CASH"),("ICICIBANK","ICICI Bank","CASH"),
    ("SBIN","State Bank of India","CASH"),("BAJFINANCE","Bajaj Finance","CASH"),
    ("BHARTIARTL","Bharti Airtel","CASH"),("KOTAKBANK","Kotak Mahindra Bank","CASH"),
    ("LT","Larsen & Toubro","CASH"),("ASIANPAINT","Asian Paints","CASH"),
    ("TITAN","Titan Company","CASH"),("SUNPHARMA","Sun Pharmaceutical","CASH"),
    ("NTPC","NTPC","CASH"),("ULTRACEMCO","UltraTech Cement","CASH"),
    ("TECHM","Tech Mahindra","CASH"),("MARUTI","Maruti Suzuki","CASH"),
    ("WIPRO","Wipro","CASH"),("HCLTECH","HCL Technologies","CASH"),
    ("ADANIENTS","Adani Enterprises","CASH"),("TATAMOTORS","Tata Motors","CASH"),
    ("HINDALCO","Hindalco","CASH"),("JSWSTEEL","JSW Steel","CASH"),
    ("ONGC","ONGC","CASH"),("COALINDIA","Coal India","CASH"),
    ("PIDILITIND","Pidilite Industries","CASH"),("HAVELLS","Havells India","CASH"),
    ("APOLLOHOSP","Apollo Hospitals","CASH"),("DIVISLAB","Divi's Lab","CASH"),
    ("DRREDDY","Dr. Reddy's","CASH"),
]

def load_all_tickers():
    cash = load_nse_cash()
    sme  = load_nse_sme()
    all_tickers = cash + sme
    if not all_tickers:
        print("  Using Nifty 50 fallback list")
        all_tickers = NIFTY50_FALLBACK
    seen = set()
    deduped = []
    for t in all_tickers:
        if t[0] not in seen:
            seen.add(t[0])
            deduped.append(t)
    print(f"  Total unique tickers: {len(deduped)}")
    return deduped

# ── Cache ─────────────────────────────────────────────────────────────────────
def load_cache():
    if not USE_CACHE or not os.path.exists(CACHE_FILE):
        return {}
    try:
        age_h = (time.time() - os.path.getmtime(CACHE_FILE)) / 3600
        if age_h > CACHE_MAX_AGE_H:
            print(f"  Cache expired ({age_h:.1f}h old)")
            return {}
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"  Cache: {len(data)} stocks ({age_h:.1f}h old)")
        return data
    except Exception:
        return {}

def save_cache(data):
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(data, f)
    except Exception as e:
        print(f"  Cache save failed: {e}")

# ── Technical indicators ──────────────────────────────────────────────────────
def calc_rsi(series, period=14):
    if len(series) < period + 1:
        return float("nan")
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])

def calc_macd(series, fast=34, slow=1000, signal=20):
    if len(series) < slow:
        return float("nan"), float("nan")
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return float(macd.iloc[-1]), float(sig.iloc[-1])

def calc_fib(df):
    sw_low  = float(df["Low"].rolling(252).min().iloc[-1])
    sw_high = float(df["High"].rolling(252).max().iloc[-1])
    rng     = sw_high - sw_low
    return {"low": sw_low, "high": sw_high,
            "f0618": sw_high + rng * 0.618, "f1618": sw_high + rng * 1.618,
            "f2618": sw_high + rng * 2.618, "f4236": sw_high + rng * 4.236}

# ── Support / Resistance ──────────────────────────────────────────────────────
def find_pivot_highs(series, n=3):
    """Swing highs: high[i] > all highs in window of n on each side."""
    vals, pivots = series.values, []
    for i in range(n, len(vals) - n):
        window = list(range(i - n, i)) + list(range(i + 1, i + n + 1))
        if all(vals[i] > vals[j] for j in window):
            pivots.append((i, vals[i]))
    return pivots

def find_pivot_lows(series, n=3):
    vals, pivots = series.values, []
    for i in range(n, len(vals) - n):
        window = list(range(i - n, i)) + list(range(i + 1, i + n + 1))
        if all(vals[i] < vals[j] for j in window):
            pivots.append((i, vals[i]))
    return pivots

def cluster_levels(levels, tol_pct=0.015):
    """Merge nearby levels within tol_pct of each other."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters = [[levels[0]]]
    for v in levels[1:]:
        if (v - clusters[-1][-1]) / clusters[-1][-1] < tol_pct:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [float(np.mean(c)) for c in clusters]

def calc_sr_levels(df_tf, n_pivot=3, n_levels=4):
    """Return top-N resistance and support levels from pivot analysis."""
    ph = find_pivot_highs(df_tf["High"], n_pivot)
    pl = find_pivot_lows(df_tf["Low"],  n_pivot)
    resist_raw = [v for _, v in ph]
    support_raw = [v for _, v in pl]
    resist = cluster_levels(resist_raw)[-n_levels:]
    support = cluster_levels(support_raw)[:n_levels]
    return resist, support

# ── Trend Channel ─────────────────────────────────────────────────────────────
def calc_trend_channel(df_tf, window=52):
    """Linear regression channel on last `window` bars of Close."""
    sub = df_tf["Close"].tail(window).dropna()
    if len(sub) < 10:
        return None
    x  = np.arange(len(sub))
    m, b = np.polyfit(x, sub.values, 1)
    fitted = m * x + b
    residuals = sub.values - fitted
    std = np.std(residuals)
    return {
        "slope":  m,
        "intercept": b,
        "upper": fitted + 2 * std,
        "lower": fitted - 2 * std,
        "mid":   fitted,
        "last_upper": float(fitted[-1] + 2 * std),
        "last_lower": float(fitted[-1] - 2 * std),
        "last_mid":   float(fitted[-1]),
        "n": len(sub),
    }

# ── Darvas Box ────────────────────────────────────────────────────────────────
def calc_darvas_box(df, n_confirm=3, lookback=60):
    if df is None or len(df) < n_confirm + 5:
        return {"top": None, "bottom": None, "status": "N/A", "width_pct": None}
    data   = df.tail(max(lookback, n_confirm + 10)).reset_index(drop=True)
    highs  = data["High"].values
    lows   = data["Low"].values
    close  = float(data["Close"].iloc[-1])
    n      = len(data)
    box_top = box_bottom = top_idx = None
    for i in range(n - n_confirm - 1, 0, -1):
        if all(highs[i+1:i+n_confirm+1] < highs[i]):
            box_top, top_idx = float(highs[i]), i
            break
    if box_top is None:
        return {"top": None, "bottom": None, "status": "FORMING", "width_pct": None}
    sub_lows = lows[top_idx:]
    for j in range(len(sub_lows) - n_confirm):
        if all(sub_lows[j+1:j+n_confirm+1] > sub_lows[j]):
            box_bottom = float(sub_lows[j])
            break
    if box_bottom is None:
        box_bottom = float(np.min(sub_lows))
    status = "BREAKOUT" if close > box_top else ("BREAKDOWN" if close < box_bottom else "INSIDE")
    width_pct = round((box_top - box_bottom) / box_bottom * 100, 1) if box_bottom else None
    return {"top": round(box_top, 2), "bottom": round(box_bottom, 2),
            "status": status, "width_pct": width_pct}

def darvas_for_timeframes(df_daily):
    results = {}
    for tf, freq in [("Daily","B"), ("Weekly","W"), ("Monthly","ME")]:
        try:
            df_tf = df_daily if freq == "B" else df_daily.resample(freq).agg(
                {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
            results[tf] = calc_darvas_box(df_tf, n_confirm=3 if tf=="Daily" else 2, lookback=60)
        except Exception:
            results[tf] = {"top": None, "bottom": None, "status": "N/A", "width_pct": None}
    return results

# ── Daily Blast Detection ─────────────────────────────────────────────────────
def detect_daily_blast(df_daily, w_resist, m_resist, w_channel, m_channel):
    """
    Blast = daily close today breaks above ANY of:
      - Weekly resistance level
      - Monthly resistance level
      - Weekly trend-channel upper band
      - Monthly trend-channel upper band
    Also checks the prior 5 days to catch very recent breakouts.
    Returns: (is_blast, reasons[])
    """
    if df_daily is None or len(df_daily) < 10:
        return False, []

    recent = df_daily["Close"].tail(5)
    prev5_high = float(df_daily["High"].iloc[-6:-1].max()) if len(df_daily) >= 6 else None
    last_close = float(df_daily["Close"].iloc[-1])
    reasons = []

    # Check weekly resistance
    for lvl in w_resist:
        if prev5_high is not None and prev5_high < lvl <= last_close:
            reasons.append(f"W-Resist ₹{lvl:,.0f}")
        elif last_close > lvl * 1.001:
            reasons.append(f"Above W-Resist ₹{lvl:,.0f}")

    # Check monthly resistance
    for lvl in m_resist:
        if prev5_high is not None and prev5_high < lvl <= last_close:
            reasons.append(f"M-Resist ₹{lvl:,.0f}")
        elif last_close > lvl * 1.001:
            reasons.append(f"Above M-Resist ₹{lvl:,.0f}")

    # Check weekly channel upper
    if w_channel and last_close > w_channel["last_upper"]:
        reasons.append(f"W-Channel Top ₹{w_channel['last_upper']:,.0f}")

    # Check monthly channel upper
    if m_channel and last_close > m_channel["last_upper"]:
        reasons.append(f"M-Channel Top ₹{m_channel['last_upper']:,.0f}")

    # Deduplicate
    seen, dedup = set(), []
    for r in reasons:
        key = r.split("₹")[0].strip()
        if key not in seen:
            seen.add(key)
            dedup.append(r)

    return len(dedup) > 0, dedup[:3]

# ── Chart generation ──────────────────────────────────────────────────────────
def draw_chart(symbol, df_daily, w_resist, w_support, m_resist, m_support,
               w_channel, m_channel, darvas, is_blast):
    """
    Generate a 2-panel chart (Weekly | Monthly) with S/R + channel overlays.
    Saved to charts/{symbol}_mb.png
    """
    try:
        chart_path = os.path.join(CHARTS_DIR, f"{symbol}_mb.png")

        df_w = df_daily.resample("W").agg(
            {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna().tail(78)
        df_m = df_daily.resample("ME").agg(
            {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna().tail(36)

        fig, axes = plt.subplots(2, 1, figsize=(14, 9),
                                 facecolor="#0d1117", gridspec_kw={"hspace": 0.38})
        fig.suptitle(f"{symbol} — Weekly & Monthly S/R + Trend Channel",
                     color="#e6edf3", fontsize=13, fontweight="bold", y=0.98)

        for ax, df_tf, resist, support, channel, label, color_ch in [
            (axes[0], df_w, w_resist, w_support, w_channel, "Weekly",  "#60a5fa"),
            (axes[1], df_m, m_resist, m_support, m_channel, "Monthly", "#c084fc"),
        ]:
            ax.set_facecolor("#0d1117")
            ax.tick_params(colors="#8b949e", labelsize=8)
            for spine in ax.spines.values():
                spine.set_edgecolor("#21262d")
            ax.grid(color="#21262d", linewidth=0.4, linestyle="--", alpha=0.6)

            idx   = np.arange(len(df_tf))
            close = df_tf["Close"].values
            highs = df_tf["High"].values
            lows  = df_tf["Low"].values

            # Candle wicks
            for i in range(len(idx)):
                c = "#26d07c" if close[i] >= df_tf["Open"].values[i] else "#ff6b6b"
                ax.plot([idx[i], idx[i]], [lows[i], highs[i]], color=c, linewidth=0.8, alpha=0.7)
                h = abs(close[i] - df_tf["Open"].values[i])
                bot = min(close[i], df_tf["Open"].values[i])
                ax.bar(idx[i], h, bottom=bot, color=c, width=0.6, alpha=0.85)

            # Trend Channel
            if channel:
                n = channel["n"]
                x_ch = np.arange(max(0, len(df_tf) - n), len(df_tf))
                up  = channel["upper"][-len(x_ch):]
                mid = channel["mid"][-len(x_ch):]
                lo  = channel["lower"][-len(x_ch):]
                ax.fill_between(x_ch, lo, up, color=color_ch, alpha=0.08)
                ax.plot(x_ch, up,  color=color_ch, linewidth=1.2, linestyle="--", alpha=0.8, label="Channel Top")
                ax.plot(x_ch, mid, color=color_ch, linewidth=0.8, linestyle=":",  alpha=0.6, label="Channel Mid")
                ax.plot(x_ch, lo,  color=color_ch, linewidth=1.2, linestyle="--", alpha=0.8, label="Channel Bot")

            # Support / Resistance lines
            price_range = highs.max() - lows.min() if len(highs) > 0 else 1
            for lvl in resist[-3:]:
                if lows.min() - price_range*0.1 < lvl < highs.max() + price_range*0.1:
                    ax.axhline(lvl, color="#ff6b6b", linewidth=1.1, linestyle="--", alpha=0.85)
                    ax.annotate(f"R ₹{lvl:,.0f}", xy=(len(idx)-1, lvl),
                                xytext=(2, 2), textcoords="offset points",
                                color="#ff6b6b", fontsize=7, fontweight="bold")
            for lvl in support[:3]:
                if lows.min() - price_range*0.1 < lvl < highs.max() + price_range*0.1:
                    ax.axhline(lvl, color="#26d07c", linewidth=1.1, linestyle="--", alpha=0.85)
                    ax.annotate(f"S ₹{lvl:,.0f}", xy=(len(idx)-1, lvl),
                                xytext=(2, -8), textcoords="offset points",
                                color="#26d07c", fontsize=7, fontweight="bold")

            # Darvas Box on last bars
            dv = darvas.get(label.replace("ly",""), darvas.get(label, {}))
            if dv.get("top") and dv.get("bottom"):
                ax.axhline(dv["top"],    color="#60a5fa", linewidth=1.2, linestyle="-.", alpha=0.7)
                ax.axhline(dv["bottom"], color="#60a5fa", linewidth=1.2, linestyle="-.", alpha=0.7)

            # Blast marker
            if is_blast and label == "Weekly":
                ax.axvline(len(idx)-1, color="#ff8c00", linewidth=2.0, alpha=0.9)
                ax.annotate("💥 BLAST", xy=(len(idx)-1, close[-1]),
                            xytext=(-40, 12), textcoords="offset points",
                            color="#ff8c00", fontsize=9, fontweight="bold")

            ax.set_title(f"{label} — S/R + Trend Channel + Darvas",
                         color="#8b949e", fontsize=9, pad=4)
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))

            # X-axis date labels (every N bars)
            dates = df_tf.index
            step = max(1, len(dates) // 8)
            ax.set_xticks(idx[::step])
            ax.set_xticklabels([dates[i].strftime("%b'%y") for i in idx[::step]],
                               rotation=30, ha="right", fontsize=7)

        plt.tight_layout(rect=[0, 0, 1, 0.97])
        plt.savefig(chart_path, dpi=110, bbox_inches="tight",
                    facecolor="#0d1117", edgecolor="none")
        plt.close(fig)
        return chart_path
    except Exception as e:
        print(f"  Chart error {symbol}: {e}")
        plt.close("all")
        return None

# ── Signal & Score ────────────────────────────────────────────────────────────
def compute_signal(drsi, wrsi, mrsi, macd_v, macd_s, ath_pct, vol_ratio, darvas_d, is_blast):
    score, signal = 0, "WATCH"
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
    if is_blast: score += 25

    if is_blast:
        signal = "💥 DAILY BLAST"
    elif ath_pct >= -2 and not np.isnan(mrsi) and mrsi > 70:
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
    trades, in_trade = [], None
    close = df_daily["Close"]
    highs = df_daily["High"]
    lows  = df_daily["Low"]
    vol   = df_daily["Volume"]
    avg_vol = vol.rolling(20).mean()
    for i in range(50, len(df_daily) - 1):
        if in_trade:
            ep = in_trade["entry"]
            if lows.iloc[i] < ep * 0.92:
                ret = round((ep * 0.92 / ep - 1) * 100, 1)
                trades.append({**in_trade, "exit_date": df_daily.index[i].strftime("%d %b %Y"),
                                "exit_price": round(ep * 0.92, 1), "return_pct": ret,
                                "days": (df_daily.index[i] - pd.Timestamp(in_trade["entry_date"])).days})
                in_trade = None
            elif highs.iloc[i] > ep * 1.25:
                ret = round((ep * 1.25 / ep - 1) * 100, 1)
                trades.append({**in_trade, "exit_date": df_daily.index[i].strftime("%d %b %Y"),
                                "exit_price": round(ep * 1.25, 1), "return_pct": ret,
                                "days": (df_daily.index[i] - pd.Timestamp(in_trade["entry_date"])).days})
                in_trade = None
            continue
        sub = close.iloc[:i+1]
        drsi = calc_rsi(sub.tail(60), 14)
        vr = vol.iloc[i] / avg_vol.iloc[i] if avg_vol.iloc[i] > 0 else 1
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
        stats = {"trades": len(trades), "win_rate": round(len(wins)/len(trades)*100, 1),
                 "avg_ret": round(sum(rets)/len(rets), 1), "best_ret": round(max(rets), 1)}
    return trades, stats

# ── Analyse one stock ─────────────────────────────────────────────────────────
def analyse(symbol, name, segment, cache):
    yf_sym = symbol + (".NS" if segment == "CASH" else ".NS")
    cache_key = yf_sym + "_v3"
    if cache_key in cache:
        return cache[cache_key]

    try:
        end   = datetime.today()
        start = end - timedelta(days=DATA_YEARS * 365 + 90)
        df = yf.download(yf_sym, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 60:
            return None
        df = df[["Open","High","Low","Close","Volume"]].dropna()

        close = df["Close"]
        vol   = df["Volume"]

        # ATH
        ath_price  = float(df["High"].max())
        last_close = float(close.iloc[-1])
        is_ath     = last_close >= ath_price * 0.99
        ath_pct    = round((last_close / ath_price - 1) * 100, 1)

        # Multi-TF RSI
        drsi = calc_rsi(close.tail(100), 14)
        wrsi = calc_rsi(close.resample("W").last().dropna().tail(104), 14)
        mrsi = calc_rsi(close.resample("ME").last().dropna().tail(60), 14)

        # MACD ultra-slow (monthly)
        monthly_close = close.resample("ME").last().dropna()
        macd_v, macd_s = calc_macd(monthly_close, 34, 1000, 20)

        # Fibonacci
        fib = calc_fib(df)

        # Volume ratio
        avg_vol_20 = float(vol.rolling(20).mean().iloc[-1])
        vol_ratio  = round(float(vol.iloc[-1]) / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

        # Weekly / Monthly S/R and Channel
        df_w = df.resample("W").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        df_m = df.resample("ME").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()

        w_resist, w_support = calc_sr_levels(df_w.tail(104), n_pivot=3, n_levels=4)
        m_resist, m_support = calc_sr_levels(df_m.tail(60),  n_pivot=2, n_levels=4)

        w_channel = calc_trend_channel(df_w.tail(78),  window=52)
        m_channel = calc_trend_channel(df_m.tail(36),  window=24)

        # Darvas Box
        darvas     = darvas_for_timeframes(df)
        darvas_all = all(darvas[tf]["status"] == "BREAKOUT" for tf in ["Daily","Weekly","Monthly"])

        # Daily Blast
        is_blast, blast_reasons = detect_daily_blast(df, w_resist, m_resist, w_channel, m_channel)

        # Signal & Score
        signal, score = compute_signal(drsi, wrsi, mrsi, macd_v, macd_s,
                                       ath_pct, vol_ratio, darvas["Daily"], is_blast)
        if darvas_all and not is_blast:
            score += 20
            signal = "💥 DARVAS BLAST"

        # Backtest
        trades, bt = run_backtest(df)

        # Chart
        chart_file = draw_chart(symbol, df, w_resist, w_support, m_resist, m_support,
                                 w_channel, m_channel, darvas, is_blast)

        result = {
            "symbol": symbol, "name": name, "segment": segment,
            "close": round(last_close, 2), "ath_pct": ath_pct, "is_ath": is_ath,
            "drsi": round(drsi, 1) if not np.isnan(drsi) else None,
            "wrsi": round(wrsi, 1) if not np.isnan(wrsi) else None,
            "mrsi": round(mrsi, 1) if not np.isnan(mrsi) else None,
            "macd": round(macd_v, 4) if not np.isnan(macd_v) else None,
            "fib": fib, "vol_ratio": vol_ratio,
            "darvas": darvas, "darvas_all": darvas_all,
            "w_resist": w_resist, "w_support": w_support,
            "m_resist": m_resist, "m_support": m_support,
            "w_channel": {"last_upper": w_channel["last_upper"],
                          "last_mid":   w_channel["last_mid"],
                          "last_lower": w_channel["last_lower"]} if w_channel else None,
            "m_channel": {"last_upper": m_channel["last_upper"],
                          "last_mid":   m_channel["last_mid"],
                          "last_lower": m_channel["last_lower"]} if m_channel else None,
            "is_blast": is_blast, "blast_reasons": blast_reasons,
            "signal": signal, "score": score,
            "trades": trades, "bt": bt,
            "chart": chart_file,
        }
        cache[cache_key] = result
        return result

    except Exception as e:
        print(f"  Error {symbol}: {e}")
        return None

# ── HTML helpers ──────────────────────────────────────────────────────────────
def _rsi_color(v):
    if v is None: return "#8b949e"
    if v >= 70: return "#26d07c"
    if v >= 55: return "#f0b429"
    return "#ff6b6b"

def _signal_badge(sig):
    M = {
        "💥 DAILY BLAST":  ("#ff8c00","#1a0900","#ff8c0044"),
        "🚀 STRONG BUY":   ("#26d07c","#0d2615","#26d07c44"),
        "🌊 MACD MEGA BUY":("#00d4ff","#001a20","#00d4ff44"),
        "✅ BUY":           ("#26d07c","#0d2615","#26d07c44"),
        "🔥 VOL BUY":      ("#fbbf24","#2a1e00","#fbbf2444"),
        "💜 M-RSI BUY":    ("#c084fc","#1a0d2a","#c084fc44"),
        "📦 DARVAS BUY":   ("#60a5fa","#0d1a2a","#60a5fa44"),
        "💥 DARVAS BLAST": ("#ff8c00","#1a0900","#ff8c0044"),
        "WATCH":           ("#8b949e","#161b22","#30363d"),
    }
    fg, bg, bd = M.get(sig, ("#8b949e","#161b22","#30363d"))
    return (f'<span style="background:{bg};color:{fg};border:1px solid {bd};border-radius:12px;'
            f'padding:2px 10px;font-size:11px;font-weight:700;white-space:nowrap">{sig}</span>')

def _darvas_badge(status):
    M = {"BREAKOUT":("#26d07c","#0d2615","#26d07c44"),
         "INSIDE":  ("#f0b429","#1a1200","#f0b42944"),
         "BREAKDOWN":("#ff6b6b","#200808","#ff6b6b44"),
         "FORMING": ("#8b949e","#161b22","#30363d"),
         "N/A":     ("#555","#161b22","#333")}
    fg, bg, bd = M.get(status, ("#555","#161b22","#333"))
    L = {"BREAKOUT":"▲OUT","INSIDE":"▬IN","BREAKDOWN":"▼DN","FORMING":"~FORM","N/A":"—"}
    return (f'<span style="background:{bg};color:{fg};border:1px solid {bd};border-radius:10px;'
            f'padding:2px 7px;font-size:10px;font-weight:700;white-space:nowrap">{L.get(status,status)}</span>')

def _blast_badge(is_blast, reasons):
    if not is_blast:
        return '<span style="color:#333;font-size:12px">—</span>'
    tip = " | ".join(reasons)
    return (f'<span title="{tip}" style="background:#1a0900;color:#ff8c00;border:1px solid #ff8c0066;'
            f'border-radius:12px;padding:3px 10px;font-size:11px;font-weight:700;'
            f'cursor:help;white-space:nowrap">💥 BLAST</span>')

def _sr_mini(levels, color, label):
    if not levels:
        return f'<span style="color:#555">—</span>'
    vals = " · ".join(f"₹{v:,.0f}" for v in levels[-3:])
    return f'<span style="color:{color};font-size:11px">{vals}</span>'

def _trade_table(trades):
    if not trades:
        return '<p style="color:#8b949e;font-size:12px;margin-top:12px">No completed backtest trades.</p>'
    rows = []
    for t in trades[-10:]:
        rc = "#26d07c" if t["return_pct"] > 0 else "#ff6b6b"
        rows.append(
            f'<tr><td>{t["entry_date"]}</td><td>{_signal_badge(t["signal"])}</td>'
            f'<td style="text-align:right">₹{t["entry"]}</td><td>{t.get("exit_date","Open")}</td>'
            f'<td style="text-align:right">₹{t.get("exit_price","—")}</td>'
            f'<td style="text-align:right;color:{rc};font-weight:700">{"+" if t["return_pct"]>0 else ""}{t["return_pct"]}%</td>'
            f'<td style="text-align:right;color:#8b949e">{t.get("days","—")}d</td></tr>')
    return ('<table style="width:100%;border-collapse:collapse;font-size:11.5px">'
            '<tr style="color:#8b949e;font-size:10px">'
            '<th style="text-align:left;padding:4px 8px">Entry</th>'
            '<th style="text-align:left;padding:4px 8px">Signal</th>'
            '<th style="text-align:right;padding:4px 8px">Entry ₹</th>'
            '<th style="text-align:left;padding:4px 8px">Exit</th>'
            '<th style="text-align:right;padding:4px 8px">Exit ₹</th>'
            '<th style="text-align:right;padding:4px 8px">Return</th>'
            '<th style="text-align:right;padding:4px 8px">Days</th></tr>'
            + "".join(rows) + '</table>')

def _darvas_card(tf, dv):
    top    = f"₹{dv['top']:,.1f}"    if dv.get("top")    else "—"
    bottom = f"₹{dv['bottom']:,.1f}" if dv.get("bottom") else "—"
    width  = f"{dv['width_pct']}%"   if dv.get("width_pct") else "—"
    sc = {"BREAKOUT":"#26d07c","BREAKDOWN":"#ff6b6b","INSIDE":"#f0b429",
          "FORMING":"#8b949e","N/A":"#555"}.get(dv.get("status","N/A"),"#8b949e")
    ico = {"Daily":"📅","Weekly":"📆","Monthly":"🗓️"}.get(tf,"📊")
    return (f'<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px;min-width:145px">'
            f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase;margin-bottom:4px">{ico} {tf} Darvas</div>'
            f'<div style="color:{sc};font-size:12px;font-weight:700;margin-bottom:4px">{dv.get("status","N/A")}</div>'
            f'<div style="font-size:10px;color:#8b949e">Top: <b style="color:#e6edf3">{top}</b></div>'
            f'<div style="font-size:10px;color:#8b949e">Bot: <b style="color:#e6edf3">{bottom}</b></div>'
            f'<div style="font-size:10px;color:#8b949e">Width: <b style="color:#c084fc">{width}</b></div>'
            f'</div>')

# ── HTML row builder ──────────────────────────────────────────────────────────
def build_row(idx, r):
    sym    = r["symbol"]
    name   = r["name"]
    seg    = r["segment"]
    fib    = r["fib"]
    bt     = r["bt"]
    darvas = r["darvas"]
    is_blast = r["is_blast"]
    blast_reasons = r.get("blast_reasons", [])

    ath_col = "#26d07c" if r["is_ath"] else ("#f0b429" if r["ath_pct"] >= -5 else "#ff6b6b")
    ath_str = "🔥 ATH" if r["is_ath"] else f'{r["ath_pct"]}%'
    macd_col = "#26d07c" if (r["macd"] and r["macd"] > 0) else "#ff6b6b"
    macd_str = f'{r["macd"]:.3f}' if r["macd"] is not None else "—"

    d_dv = darvas.get("Daily",  {})
    w_dv = darvas.get("Weekly", {})
    m_dv = darvas.get("Monthly",{})

    bt_trades  = str(bt.get("trades","—"))
    bt_wr      = f'{bt["win_rate"]}%' if "win_rate" in bt else "—"
    bt_avg     = (f'+{bt["avg_ret"]}%' if bt.get("avg_ret",0)>0 else f'{bt.get("avg_ret","—")}%') if "avg_ret" in bt else "—"
    bt_best    = f'+{bt["best_ret"]}%' if "best_ret" in bt else "—"
    bt_avg_col = "#26d07c" if bt.get("avg_ret",0) > 0 else "#ff6b6b"

    seg_badge = (f'<span style="background:#0a1a0a;color:#26d07c;border:1px solid #26d07c44;'
                 f'border-radius:8px;padding:1px 6px;font-size:9px;font-weight:700">CASH</span>'
                 if seg == "CASH" else
                 f'<span style="background:#1a1000;color:#f0b429;border:1px solid #f0b42944;'
                 f'border-radius:8px;padding:1px 6px;font-size:9px;font-weight:700">SME</span>')

    # Chart link
    chart_link = ""
    if r.get("chart") and os.path.exists(r["chart"]):
        chart_url = f'/charts/{sym}_mb.png'
        chart_link = (f'<a href="{chart_url}" target="_blank" '
                      f'style="color:#60a5fa;font-size:11px;text-decoration:none">📈 Chart</a>')

    # S/R summary for detail row
    w_r_str = " · ".join(f"₹{v:,.0f}" for v in r.get("w_resist",[])[-3:]) or "—"
    w_s_str = " · ".join(f"₹{v:,.0f}" for v in r.get("w_support",[])[:3]) or "—"
    m_r_str = " · ".join(f"₹{v:,.0f}" for v in r.get("m_resist",[])[-3:]) or "—"
    m_s_str = " · ".join(f"₹{v:,.0f}" for v in r.get("m_support",[])[:3]) or "—"

    wch = r.get("w_channel") or {}
    mch = r.get("m_channel") or {}

    blast_detail = ""
    if is_blast and blast_reasons:
        blast_detail = (
            f'<div style="background:#1a0900;border:1px solid #ff8c0044;border-radius:8px;'
            f'padding:10px 14px;margin-bottom:12px">'
            f'<div style="color:#ff8c00;font-size:12px;font-weight:700;margin-bottom:4px">💥 Daily Blast Triggers</div>'
            + "".join(f'<div style="color:#fbbf24;font-size:11px">• {r}</div>' for r in blast_reasons)
            + f'</div>'
        )

    detail_id = f"d{idx}"
    detail_html = (
        f'<tr id="{detail_id}" style="display:none">'
        f'<td colspan="26" style="background:#010409;padding:18px 22px">'
        + blast_detail +
        f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px">'
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Swing Low</div>'
        f'<div style="color:#e6edf3;font-size:15px;font-weight:700">₹{fib["low"]:,.1f}</div></div>'
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Swing High</div>'
        f'<div style="color:#e6edf3;font-size:15px;font-weight:700">₹{fib["high"]:,.1f}</div></div>'
        f'<div style="background:#002d1a;border:1px solid #26d07c44;border-radius:8px;padding:10px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Fib 1.618x</div>'
        f'<div style="color:#26d07c;font-size:15px;font-weight:700">₹{fib["f1618"]:,.1f}</div></div>'
        f'<div style="background:#2a1200;border:1px solid #dc262644;border-radius:8px;padding:10px">'
        f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase">Fib 4.236x</div>'
        f'<div style="color:#dc2626;font-size:15px;font-weight:700">₹{fib["f4236"]:,.1f}</div></div>'
        f'</div>'
        # S/R levels
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">'
        f'<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px">'
        f'<div style="color:#60a5fa;font-size:11px;font-weight:700;margin-bottom:4px">📆 Weekly S/R</div>'
        f'<div style="font-size:11px;color:#8b949e">Resistance: <span style="color:#ff6b6b">{w_r_str}</span></div>'
        f'<div style="font-size:11px;color:#8b949e">Support: <span style="color:#26d07c">{w_s_str}</span></div>'
        + (f'<div style="font-size:10px;color:#8b949e;margin-top:4px">Channel: '
           f'<span style="color:#60a5fa">₹{wch["last_lower"]:,.0f} – ₹{wch["last_upper"]:,.0f}</span></div>'
           if wch else "")
        + f'</div>'
        f'<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px">'
        f'<div style="color:#c084fc;font-size:11px;font-weight:700;margin-bottom:4px">🗓️ Monthly S/R</div>'
        f'<div style="font-size:11px;color:#8b949e">Resistance: <span style="color:#ff6b6b">{m_r_str}</span></div>'
        f'<div style="font-size:11px;color:#8b949e">Support: <span style="color:#26d07c">{m_s_str}</span></div>'
        + (f'<div style="font-size:10px;color:#8b949e;margin-top:4px">Channel: '
           f'<span style="color:#c084fc">₹{mch["last_lower"]:,.0f} – ₹{mch["last_upper"]:,.0f}</span></div>'
           if mch else "")
        + f'</div></div>'
        # Darvas cards
        f'<div style="margin-bottom:14px">'
        f'<div style="color:#60a5fa;font-size:11px;font-weight:700;margin-bottom:8px">📦 Darvas Box</div>'
        f'<div style="display:flex;gap:10px;flex-wrap:wrap">'
        + _darvas_card("Daily", d_dv) + _darvas_card("Weekly", w_dv) + _darvas_card("Monthly", m_dv)
        + f'</div></div>'
        # Backtest
        f'<div><div style="color:#8b949e;font-size:11px;font-weight:700;margin-bottom:6px">📋 Backtest Trades (last 10)</div>'
        + _trade_table(r["trades"])
        + f'</div>'
        f'</td></tr>'
    )

    blast_data = "1" if is_blast else "0"

    row_html = (
        f'<tr data-name="{name.lower()}" data-score="{r["score"]}" '
        f'data-mrsi="{r["mrsi"] or 0}" data-wrsi="{r["wrsi"] or 0}" '
        f'data-drsi="{r["drsi"] or 0}" data-ath="{r["ath_pct"]}" '
        f'data-blast="{blast_data}" data-seg="{seg}">'
        f'<td>{idx}</td>'
        f'<td><b style="color:#e6edf3">{sym}</b> {seg_badge}<br>'
        f'<span style="color:#8b949e;font-size:11px">{name[:28]}</span></td>'
        f'<td style="text-align:right">₹{r["close"]:,.2f}</td>'
        f'<td style="text-align:right;color:{ath_col}">{ath_str}</td>'
        f'<td style="text-align:right;color:{_rsi_color(r["drsi"])}">{r["drsi"] or "—"}</td>'
        f'<td style="text-align:right;color:{_rsi_color(r["wrsi"])}">{r["wrsi"] or "—"}</td>'
        f'<td style="text-align:right;color:{_rsi_color(r["mrsi"])}">{r["mrsi"] or "—"}</td>'
        f'<td style="text-align:right;color:{macd_col}">{macd_str}</td>'
        # Darvas
        f'<td style="text-align:center">{_darvas_badge(d_dv.get("status","N/A"))}</td>'
        f'<td style="text-align:center">{_darvas_badge(w_dv.get("status","N/A"))}</td>'
        f'<td style="text-align:center">{_darvas_badge(m_dv.get("status","N/A"))}</td>'
        # ── Daily Blast column ──
        f'<td style="text-align:center" data-blast="{blast_data}">{_blast_badge(is_blast, blast_reasons)}</td>'
        # Fib
        f'<td style="text-align:right;color:#8b949e">₹{fib["f0618"]:,.0f}</td>'
        f'<td style="text-align:right;color:#26d07c">₹{fib["f1618"]:,.0f}</td>'
        f'<td style="text-align:right;color:#f0b429">₹{fib["f2618"]:,.0f}</td>'
        f'<td style="text-align:right;color:#ff6b6b">₹{fib["f4236"]:,.0f}</td>'
        # Signal / Score
        f'<td style="text-align:left">{_signal_badge(r["signal"])}</td>'
        f'<td style="text-align:right;color:#00d4ff;font-weight:700">{r["score"]}</td>'
        # Backtest
        f'<td style="text-align:right;color:#8b949e">{bt_trades}</td>'
        f'<td style="text-align:right;color:#8b949e">{bt_wr}</td>'
        f'<td style="text-align:right;color:{bt_avg_col}">{bt_avg}</td>'
        f'<td style="text-align:right;color:#26d07c">{bt_best}</td>'
        # Chart + Detail
        f'<td>{chart_link}</td>'
        f'<td><button onclick="toggleDetail(\'{detail_id}\')" '
        f'style="background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:6px;'
        f'padding:3px 8px;cursor:pointer;font-size:11px">▼</button></td>'
        f'</tr>'
        + detail_html
    )
    return row_html

# ── HTML template ─────────────────────────────────────────────────────────────
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multibagger Report — {run_ts}</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--border:#21262d;--text:#e6edf3;
       --sub:#8b949e;--cyan:#00d4ff;--green:#26d07c;--gold:#f0b429;
       --red:#ff6b6b;--purple:#c084fc;--blue:#60a5fa;--blast:#ff8c00}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}}
.header{{background:#010409;border-bottom:2px solid var(--border);padding:18px 24px 14px}}
.header h1{{font-size:20px;font-weight:700;color:var(--cyan);letter-spacing:1px}}
.subtitle{{color:var(--sub);font-size:11px;margin-top:4px}}
.nav-links{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}
.nav-link{{display:inline-flex;align-items:center;gap:5px;background:var(--card);
           border:1px solid var(--border);color:var(--text);border-radius:20px;
           padding:4px 14px;font-size:11.5px;font-weight:600;text-decoration:none;transition:all .15s}}
.nav-link:hover{{border-color:var(--cyan);color:var(--cyan)}}
.nav-link.active{{background:var(--cyan);color:#000;border-color:var(--cyan)}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;padding:14px 24px}}
.stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 16px;min-width:105px}}
.stat .val{{font-size:22px;font-weight:700}}
.stat .lbl{{font-size:10px;color:var(--sub);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}}
.info-box{{margin:0 24px 14px;border-radius:10px;padding:12px 16px}}
.info-box.strategy{{background:#0a1628;border:1px solid #1e3a5f}}
.info-box.darvas{{background:#060d1a;border:1px solid #1a3055}}
.info-box.blast-info{{background:#0d0600;border:1px solid #ff8c0033}}
.info-box h3{{font-size:12px;margin-bottom:6px}}
.info-box ul{{color:var(--sub);font-size:11px;line-height:1.75;padding-left:16px}}
.info-box li span{{color:var(--text)}}
.filter-bar{{padding:9px 24px;background:#010409;border-bottom:1px solid var(--border);
             position:sticky;top:0;z-index:1000;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.filter-bar input{{flex:1;min-width:160px;max-width:220px;background:var(--card);
                   border:1px solid var(--border);color:var(--text);border-radius:20px;
                   padding:5px 14px;font-size:12px;outline:none}}
.filter-bar input:focus{{border-color:var(--cyan)}}
.fbtn{{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;
        padding:4px 12px;cursor:pointer;font-size:11.5px;transition:all .15s;font-weight:600}}
.fbtn:hover,.fbtn.active{{background:var(--cyan);color:#000;border-color:var(--cyan)}}
.fbtn.blast-btn{{border-color:#ff8c0066;color:#ff8c00}}
.fbtn.blast-btn.active{{background:#ff8c00;color:#000;border-color:#ff8c00}}
.fbtn.sme-btn{{border-color:#f0b42966;color:#f0b429}}
.fbtn.sme-btn.active{{background:#f0b429;color:#000;border-color:#f0b429}}
table{{width:100%;border-collapse:collapse}}
th{{background:#010409;padding:8px 9px;text-align:right;font-size:10.5px;color:var(--sub);
     text-transform:uppercase;letter-spacing:.4px;cursor:pointer;white-space:nowrap;
     border-bottom:1px solid var(--border)}}
th:hover{{color:var(--cyan)}}
th.asc::after{{content:" ▲"}}th.desc::after{{content:" ▼"}}
th:first-child,th:nth-child(2),th:last-child,th:nth-last-child(2){{text-align:left}}
td{{padding:8px 9px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr:hover td{{background:#161b2266}}
.blast-row td{{background:#1a090022!important}}
.blast-row:hover td{{background:#1a090044!important}}
.footer{{text-align:center;padding:20px;color:var(--sub);font-size:11px;
          border-top:1px solid var(--border);margin-top:16px}}
</style>
</head>
<body>
<div class="header">
  <h1>🏆 Multibagger Report — NSE Cash + SME Scanner v3.0</h1>
  <div class="subtitle">
    ATH + MTF RSI + MACD(34,1000,20) + Fibonacci + Darvas Box + 📦 S/R + Trend Channel + 💥 Daily Blast
    &nbsp;|&nbsp; {run_ts} IST &nbsp;|&nbsp; {n_cash} Cash · {n_sme} SME stocks
  </div>
  <div class="nav-links">
    <a class="nav-link" href="/">📊 Full RSI Report</a>
    <a class="nav-link" href="/ath">🏆 ATH Breakout</a>
    <a class="nav-link active" href="/multibagger">💎 Multibagger</a>
    <a class="nav-link" href="/rocket">🚀 Rocket Scanner</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="val" style="color:var(--cyan)">{n_total}</div><div class="lbl">Analysed</div></div>
  <div class="stat"><div class="val" style="color:var(--blast)">{n_blast}</div><div class="lbl">💥 Daily Blast</div></div>
  <div class="stat"><div class="val" style="color:var(--green)">{n_ath}</div><div class="lbl">ATH Breakouts</div></div>
  <div class="stat"><div class="val" style="color:var(--red)">{n_mrsi}</div><div class="lbl">M-RSI &gt; 70</div></div>
  <div class="stat"><div class="val" style="color:var(--blue)">{n_darvas}</div><div class="lbl">Darvas Breakout</div></div>
  <div class="stat"><div class="val" style="color:var(--gold)">{n_strong}</div><div class="lbl">Strong Signals</div></div>
  <div class="stat"><div class="val" style="color:var(--purple)">{avg_score:.1f}</div><div class="lbl">Avg Score</div></div>
</div>

<div class="info-box blast-info">
  <h3 style="color:var(--blast)">💥 Daily Blast — What It Means</h3>
  <ul>
    <li><span>Trigger:</span> Daily close broke above a key Weekly or Monthly Resistance level, OR above the Trend Channel upper band</li>
    <li><span>Significance:</span> Strong breakout with institutional interest — price has escaped a major supply zone</li>
    <li><span>Filter:</span> Click "💥 Only Blast" button below to show only breakout stocks · Rows highlighted in orange</li>
    <li><span>Confidence:</span> Higher when BOTH weekly + monthly S/R are broken simultaneously</li>
  </ul>
</div>

<div class="info-box darvas">
  <h3 style="color:var(--blue)">📦 Darvas Box + S/R + Trend Channel</h3>
  <ul>
    <li><span>Support / Resistance:</span> Pivot swing highs (resistance) and lows (support) on weekly &amp; monthly charts — dashed lines on chart</li>
    <li><span>Trend Channel:</span> Linear regression ±2σ band on weekly (52 bars) and monthly (24 bars) — shaded area on chart</li>
    <li><span>Darvas Box:</span> High confirmed when not exceeded for 3 consecutive bars · ▲OUT=breakout, ▬IN=inside box, ▼DN=breakdown</li>
  </ul>
</div>

<div class="filter-bar">
  <input type="text" id="searchInput" placeholder="🔍 Search ticker…" oninput="applyFilters()">
  <button class="fbtn blast-btn" id="blastBtn" onclick="toggleBlast()">💥 Only Blast</button>
  <button class="fbtn sme-btn"   id="smeBtn"   onclick="toggleSeg('SME')">SME Only</button>
  <button class="fbtn"           id="cashBtn"  onclick="toggleSeg('CASH')">Cash Only</button>
  <button class="fbtn" onclick="clearFilters()">✕ Clear</button>
  <button class="fbtn" onclick="sortByField('score')">Score ↓</button>
  <button class="fbtn" onclick="sortByField('mrsi')">M-RSI ↓</button>
  <button class="fbtn" onclick="sortByField('ath')">ATH% ↓</button>
  <button class="fbtn" onclick="sortByField('drsi')">D-RSI ↓</button>
  <span id="countInfo" style="color:var(--sub);font-size:11px;margin-left:4px"></span>
</div>

<table id="mainTable">
<thead>
<tr>
  <th onclick="colSort(0)">#</th>
  <th onclick="colSort(1)" style="text-align:left">Ticker / Name</th>
  <th onclick="colSort(2)">Close</th>
  <th onclick="colSort(3)">ATH%</th>
  <th onclick="colSort(4)">D-RSI</th>
  <th onclick="colSort(5)">W-RSI</th>
  <th onclick="colSort(6)">M-RSI</th>
  <th onclick="colSort(7)">MACD</th>
  <th onclick="colSort(8)" style="color:#60a5fa">D-Box</th>
  <th onclick="colSort(9)" style="color:#60a5fa">W-Box</th>
  <th onclick="colSort(10)" style="color:#60a5fa">M-Box</th>
  <th onclick="colSort(11)" style="color:var(--blast)">💥 Blast</th>
  <th onclick="colSort(12)">Fib 0.618x</th>
  <th onclick="colSort(13)">Fib 1.618x</th>
  <th onclick="colSort(14)">Fib 2.618x</th>
  <th onclick="colSort(15)">Fib 4.236x</th>
  <th onclick="colSort(16)" style="text-align:left">Signal</th>
  <th onclick="colSort(17)">Score</th>
  <th onclick="colSort(18)">Trades</th>
  <th onclick="colSort(19)">Win%</th>
  <th onclick="colSort(20)">Avg Ret</th>
  <th onclick="colSort(21)">Best</th>
  <th style="text-align:left">Chart</th>
  <th style="text-align:left">▼</th>
</tr>
</thead>
<tbody id="tableBody">
{rows_html}
</tbody>
</table>

<div class="footer">
  Multibagger + S/R + Trend Channel + Darvas + Daily Blast Report v3.0
  &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp; <b>Not financial advice.</b> Educational use only.
</div>

<script>
let blastOnly=false, segFilter='ALL';

function getRows(){{return[...document.querySelectorAll('#tableBody tr[data-name]')];}}

function applyFilters(){{
  const q=document.getElementById('searchInput').value.toLowerCase();
  const rows=getRows(); let shown=0;
  rows.forEach(r=>{{
    const name=r.dataset.name||'';
    const sym=r.querySelector('b')?.textContent.toLowerCase()||'';
    const isBlast=r.dataset.blast==='1';
    const seg=r.dataset.seg||'CASH';
    const matchQ=!q||(name.includes(q)||sym.includes(q));
    const matchBlast=!blastOnly||isBlast;
    const matchSeg=segFilter==='ALL'||seg===segFilter;
    const show=matchQ&&matchBlast&&matchSeg;
    r.style.display=show?'':'none';
    // hide detail row too
    const detBtn=r.querySelector('button[onclick^="toggleDetail"]');
    if(detBtn){{
      const detId=detBtn.getAttribute('onclick').match(/'(d\\d+)'/)?.[1];
      if(detId)document.getElementById(detId).style.display='none';
    }}
    if(show)shown++;
    if(show&&isBlast)r.classList.add('blast-row');
    else r.classList.remove('blast-row');
  }});
  document.getElementById('countInfo').textContent=`Showing ${{shown}} of {n_total}`;
}}

function toggleBlast(){{
  blastOnly=!blastOnly;
  document.getElementById('blastBtn').classList.toggle('active',blastOnly);
  applyFilters();
}}

function toggleSeg(s){{
  segFilter=segFilter===s?'ALL':s;
  document.getElementById('smeBtn').classList.toggle('active',segFilter==='SME');
  document.getElementById('cashBtn').classList.toggle('active',segFilter==='CASH');
  applyFilters();
}}

function clearFilters(){{
  blastOnly=false; segFilter='ALL';
  document.getElementById('searchInput').value='';
  document.getElementById('blastBtn').classList.remove('active');
  document.getElementById('smeBtn').classList.remove('active');
  document.getElementById('cashBtn').classList.remove('active');
  applyFilters();
}}

function sortByField(field){{
  const map={{score:'score',mrsi:'mrsi',wrsi:'wrsi',drsi:'drsi',ath:'ath'}};
  const rows=getRows();
  rows.sort((a,b)=>parseFloat(b.dataset[map[field]]||0)-parseFloat(a.dataset[map[field]]||0));
  const tbody=document.getElementById('tableBody');
  rows.forEach(r=>{{
    tbody.appendChild(r);
    const detBtn=r.querySelector('button[onclick^="toggleDetail"]');
    if(detBtn){{
      const detId=detBtn.getAttribute('onclick').match(/'(d\\d+)'/)?.[1];
      if(detId)tbody.appendChild(document.getElementById(detId));
    }}
  }});
}}

let lastCol=-1,lastDir=1;
function colSort(col){{
  const tbody=document.getElementById('tableBody');
  const rows=getRows();
  const dir=lastCol===col?-lastDir:1;
  lastCol=col; lastDir=dir;
  rows.sort((a,b)=>{{
    const av=(a.cells[col]?.textContent||'').replace(/[₹,%+▲▼▬~ 💥]/g,'').trim();
    const bv=(b.cells[col]?.textContent||'').replace(/[₹,%+▲▼▬~ 💥]/g,'').trim();
    const an=parseFloat(av),bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn))return dir*(an-bn);
    return dir*av.localeCompare(bv);
  }});
  rows.forEach(r=>{{
    tbody.appendChild(r);
    const detBtn=r.querySelector('button[onclick^="toggleDetail"]');
    if(detBtn){{
      const detId=detBtn.getAttribute('onclick').match(/'(d\\d+)'/)?.[1];
      if(detId){{const det=document.getElementById(detId);if(det)tbody.appendChild(det);}}
    }}
  }});
  document.querySelectorAll('th').forEach((h,i)=>{{
    h.className=i===col?(dir===1?'asc':'desc'):'';
  }});
}}

function toggleDetail(id){{
  const el=document.getElementById(id);
  el.style.display=el.style.display==='none'?'table-row':'none';
}}

document.addEventListener('DOMContentLoaded',()=>{{
  applyFilters();
  // highlight blast rows on load
  getRows().forEach(r=>{{if(r.dataset.blast==='1')r.classList.add('blast-row');}});
}});
</script>
</body>
</html>'''

# ── GitHub push ───────────────────────────────────────────────────────────────
def push_to_github():
    token = os.environ.get("GITHUB_TOKEN","")
    if not token:
        print("  GITHUB_TOKEN not set — skipping push")
        return
    try:
        env = {**os.environ, "GIT_AUTHOR_NAME":"NSE Bot","GIT_AUTHOR_EMAIL":"bot@noreply",
               "GIT_COMMITTER_NAME":"NSE Bot","GIT_COMMITTER_EMAIL":"bot@noreply"}
        run_ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
        subprocess.run(["git","add", OUTPUT_HTML, "multibagger_report.py"], check=True, env=env)
        subprocess.run(["git","commit","-m",f"multibagger: S/R + Darvas + Blast report {run_ts}"],
                       check=True, env=env)
        remote = subprocess.run(["git","remote","get-url","origin"],
                                capture_output=True, text=True).stdout.strip()
        if remote.startswith("https://") and "@" not in remote:
            remote = remote.replace("https://", f"https://x-access-token:{token}@")
        subprocess.run(["git","push", remote, "HEAD"], check=True, env=env)
        print(f"  ✅ Pushed to GitHub")
    except subprocess.CalledProcessError as e:
        print(f"  GitHub push failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 68)
    print("  Multibagger v3.0 — NSE Cash + SME + S/R + Darvas + Daily Blast")
    print("=" * 68)

    tickers = load_all_tickers()
    cache   = load_cache()
    results, errors = [], 0

    n_cash_tickers = sum(1 for t in tickers if t[2] == "CASH")
    n_sme_tickers  = sum(1 for t in tickers if t[2] == "SME")
    print(f"\n  Scanning {len(tickers)} stocks ({n_cash_tickers} Cash + {n_sme_tickers} SME) …\n")

    def process(t):
        return analyse(t[0], t[1], t[2], cache)

    batches = [tickers[i:i+BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    done    = 0

    for batch in batches:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(process, t): t for t in batch}
            for fut in as_completed(futures):
                r = fut.result()
                done += 1
                if r:
                    results.append(r)
                    blast_tag = " 💥BLAST" if r["is_blast"] else ""
                    print(f"  [{done:4d}/{len(tickers)}] {r['symbol']:12s} "
                          f"₹{r['close']:>9,.2f}  score={r['score']:3d}  "
                          f"{r['signal']:<20s}  D={r['darvas']['Daily']['status']:<9s}{blast_tag}")
                else:
                    errors += 1
        save_cache(cache)
        if batch != batches[-1]:
            time.sleep(BATCH_PAUSE)

    # Sort: blast first, then by score
    results.sort(key=lambda x: (0 if x["is_blast"] else 1, -x["score"]))

    # Stats
    n_total  = len(results)
    n_blast  = sum(1 for r in results if r["is_blast"])
    n_ath    = sum(1 for r in results if r["is_ath"])
    n_mrsi   = sum(1 for r in results if r["mrsi"] and r["mrsi"] > 70)
    n_darvas = sum(1 for r in results if any(
        r["darvas"][tf]["status"] == "BREAKOUT" for tf in ["Daily","Weekly","Monthly"]))
    n_strong = sum(1 for r in results if "BUY" in r["signal"] or "BLAST" in r["signal"])
    avg_score = sum(r["score"] for r in results) / n_total if n_total else 0
    n_cash   = sum(1 for r in results if r["segment"] == "CASH")
    n_sme    = sum(1 for r in results if r["segment"] == "SME")
    run_ts   = datetime.now(IST).strftime("%Y-%m-%d %H:%M")

    rows_html = "".join(build_row(i+1, r) for i, r in enumerate(results))

    html = HTML_TEMPLATE.format(
        run_ts=run_ts, n_total=n_total, n_blast=n_blast, n_ath=n_ath,
        n_mrsi=n_mrsi, n_darvas=n_darvas, n_strong=n_strong,
        avg_score=avg_score, n_cash=n_cash, n_sme=n_sme,
        rows_html=rows_html,
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n{'='*68}")
    print(f"  ✅ {OUTPUT_HTML} — {n_total} stocks | 💥 Blast: {n_blast} | ATH: {n_ath} | Score avg: {avg_score:.1f}")
    print(f"{'='*68}\n")

    # Push to GitHub
    push_to_github()


if __name__ == "__main__":
    main()
