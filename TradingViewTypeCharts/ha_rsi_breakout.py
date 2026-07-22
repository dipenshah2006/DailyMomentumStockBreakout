"""
Monthly RSI/CCI Crossover + Fibonacci Extension Screener
=========================================================
Signal logic (Monthly timeframe):
  • RSI(14) crosses above SMA(14) of RSI  →  RSI bullish crossover
  • CCI(20) crosses above SMA(20) of CCI  →  CCI bullish crossover
  • Both must be active (independently tagged as fresh/ongoing)

Fibonacci Extension targets (from swing low before breakout):
  • Swing Low  = lowest low in lookback window before crossover bar
  • Swing High = highest high in lookback window before crossover bar
  • Breakout level = close at crossover bar
  • Fibo extension targets: 0.618, 1.0, 1.272, 1.618, 2.0, 2.618
    Target = Breakout + (High - Low) * fib_ratio

Output: monthly_rsi_cci_fibo.html
"""

import os, sys, math, warnings, json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
pd.options.mode.chained_assignment = None

# ── Config ────────────────────────────────────────────────────────────────────
EQUITY_CSV  = r"C:\python\cursorYfinance\newMomentum\30april20262pm\india\NSE\NSECash\EQUITY_L.csv"
OUT_HTML    = r"C:\python\cursorYfinance\newMomentum\30april20262pm\monthly_rsi_cci_fibo.html"

RSI_LEN     = 14
RSI_SMA     = 14
CCI_LEN     = 20
CCI_SMA     = 20
SWING_BARS  = 20        # bars to look back for swing high/low
FRESH_BARS  = 1         # months ≤ this = "fresh"
BATCH       = 50
PERIOD      = "15y"     # yfinance monthly lookback
INTERVAL    = "1mo"

FIBO_RATIOS = [0.618, 1.0, 1.272, 1.618, 2.0, 2.618]
FIBO_LABELS = ["0.618", "1.000", "1.272", "1.618", "2.000", "2.618"]

# ── Indicators ────────────────────────────────────────────────────────────────

def calc_rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder-smoothed RSI (standard)."""
    delta = close.diff()
    up  = delta.clip(lower=0)
    dn  = (-delta).clip(lower=0)
    # Wilder = EWM with com = n-1
    avg_up = up.ewm(com=n - 1, min_periods=n).mean()
    avg_dn = dn.ewm(com=n - 1, min_periods=n).mean()
    rs  = avg_up / avg_dn.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_cci(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 20) -> pd.Series:
    """Classic CCI = (TP - SMA(TP,n)) / (0.015 * MeanAbsDev)."""
    tp  = (high + low + close) / 3
    sma = tp.rolling(n).mean()
    mad = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    cci = (tp - sma) / (0.015 * mad.replace(0, np.nan))
    return cci


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


# ── Crossover detection ───────────────────────────────────────────────────────

def crossover(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on bars where fast crosses above slow (was below, now above)."""
    above = fast > slow
    was_below = ~(fast.shift(1) > slow.shift(1))
    return above & was_below


def find_monthly_crossover(df: pd.DataFrame, fresh_bars: int = FRESH_BARS):
    """
    Compute RSI/CCI crossovers on monthly df.
    Returns dict with signal info or None.

    Requirements for a valid signal:
      - RSI(14) crossed above SMA(14) of RSI at some recent bar AND is still above
      - CCI(20) crossed above SMA(20) of CCI at some recent bar AND is still above
      - Both conditions active simultaneously (overlap or same bar)
    """
    if len(df) < max(RSI_LEN, CCI_LEN) + RSI_SMA + 5:
        return None

    df = df.copy()
    df["rsi"]     = calc_rsi(df["Close"], RSI_LEN)
    df["rsi_sma"] = sma(df["rsi"], RSI_SMA)
    df["cci"]     = calc_cci(df["High"], df["Low"], df["Close"], CCI_LEN)
    df["cci_sma"] = sma(df["cci"], CCI_SMA)

    df["rsi_xo"]  = crossover(df["rsi"], df["rsi_sma"])
    df["cci_xo"]  = crossover(df["cci"], df["cci_sma"])

    # Current state: both must be above their SMA right now
    last = df.iloc[-1]
    if not (last["rsi"] > last["rsi_sma"] and last["cci"] > last["cci_sma"]):
        return None

    # Find most recent RSI crossover date
    rsi_xo_dates = df.index[df["rsi_xo"]]
    cci_xo_dates = df.index[df["cci_xo"]]
    if len(rsi_xo_dates) == 0 or len(cci_xo_dates) == 0:
        return None

    rsi_xo_date = rsi_xo_dates[-1]
    cci_xo_date = cci_xo_dates[-1]

    # The signal start = later of the two (both must have crossed)
    signal_date  = max(rsi_xo_date, cci_xo_date)
    signal_idx   = df.index.get_loc(signal_date)
    bars_ago     = len(df) - 1 - signal_idx
    is_fresh     = bars_ago <= fresh_bars

    # RSI + CCI values
    rsi_val      = round(float(last["rsi"]), 1)
    rsi_sma_val  = round(float(last["rsi_sma"]), 1)
    cci_val      = round(float(last["cci"]), 1)
    cci_sma_val  = round(float(last["cci_sma"]), 1)

    # Individual crossover freshness
    rsi_bars_ago = len(df) - 1 - df.index.get_loc(rsi_xo_date)
    cci_bars_ago = len(df) - 1 - df.index.get_loc(cci_xo_date)

    return {
        "signal_date"  : signal_date,
        "bars_ago"     : int(bars_ago),
        "is_fresh"     : is_fresh,
        "rsi"          : rsi_val,
        "rsi_sma"      : rsi_sma_val,
        "cci"          : cci_val,
        "cci_sma"      : cci_sma_val,
        "rsi_xo_date"  : rsi_xo_date,
        "cci_xo_date"  : cci_xo_date,
        "rsi_bars_ago" : int(rsi_bars_ago),
        "cci_bars_ago" : int(cci_bars_ago),
        "df"           : df,
        "signal_idx"   : int(signal_idx),
    }


# ── Fibonacci Extension targets ───────────────────────────────────────────────

def calc_fibo_targets(df: pd.DataFrame, signal_idx: int, swing_bars: int = SWING_BARS):
    """
    Fibonacci Extension from monthly swing structure.

    Method:
      Look back SWING_BARS monthly bars before the signal bar.
      Swing Low  = lowest Low in that window
      Swing High = highest High in that window
      Breakout   = Close at signal bar

    Extension targets above breakout:
      Target(n) = Breakout + (SwingHigh - SwingLow) * fib_ratio

    Also return retracement support levels (% pullbacks from current price).
    """
    # Swing window: bars before signal
    start = max(0, signal_idx - swing_bars)
    window = df.iloc[start:signal_idx + 1]

    if len(window) < 3:
        return None

    swing_low   = float(window["Low"].min())
    swing_high  = float(window["High"].max())
    swing_range = swing_high - swing_low

    if swing_range <= 0:
        return None

    breakout_price = float(df.iloc[signal_idx]["Close"])
    current_price  = float(df.iloc[-1]["Close"])

    targets = {}
    for ratio, label in zip(FIBO_RATIOS, FIBO_LABELS):
        t = breakout_price + swing_range * ratio
        pct_gain = (t / current_price - 1) * 100
        targets[f"F{label}"] = {
            "price"   : round(t, 2),
            "pct_gain": round(pct_gain, 1),
            "ratio"   : ratio,
            "hit"     : current_price >= t,
        }

    # Swing low date + high date
    swing_low_date  = df.iloc[start:signal_idx + 1]["Low"].idxmin()
    swing_high_date = df.iloc[start:signal_idx + 1]["High"].idxmax()

    return {
        "swing_low"       : round(swing_low, 2),
        "swing_high"      : round(swing_high, 2),
        "swing_range"     : round(swing_range, 2),
        "swing_low_date"  : swing_low_date,
        "swing_high_date" : swing_high_date,
        "breakout_price"  : round(breakout_price, 2),
        "current_price"   : round(current_price, 2),
        "targets"         : targets,
    }


# ── Data pipeline ─────────────────────────────────────────────────────────────

def load_tickers(csv_path: str):
    df   = pd.read_csv(csv_path)
    col  = [c for c in df.columns if c.strip().upper() == "SYMBOL"][0]
    return [s.strip() + ".NS" for s in df[col].dropna().unique()]


def download_batch(tickers, interval, period):
    try:
        raw = yf.download(
            tickers,
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as e:
        print(f"  [warn] {e}")
        return {}

    result = {}
    for t in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            else:
                if t not in raw.columns.get_level_values(0):
                    continue
                df = raw[t].copy()

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(-1)
            df = df.loc[:, ~df.columns.duplicated()]

            needed = ["Open", "High", "Low", "Close"]
            if not all(c in df.columns for c in needed):
                continue
            df = df[needed].dropna(how="all")
            min_bars = max(RSI_LEN + RSI_SMA, CCI_LEN + CCI_SMA) + SWING_BARS + 10
            if len(df) < min_bars:
                continue
            result[t] = df
        except Exception:
            pass
    return result


# ── Screener ──────────────────────────────────────────────────────────────────

def screen(tickers):
    results = []
    total   = len(tickers)
    batches = math.ceil(total / BATCH)

    print(f"\nMonthly RSI/CCI Crossover + Fibo Screener")
    print(f"Tickers: {total}  |  Batch size: {BATCH}")
    print("=" * 60)

    for b_idx, batch_start in enumerate(range(0, total, BATCH)):
        batch = tickers[batch_start: batch_start + BATCH]
        print(f"  Batch {b_idx+1}/{batches}  ({len(batch)} tickers)", end="\r")

        data = download_batch(batch, INTERVAL, PERIOD)

        for ticker, df in data.items():
            try:
                sig = find_monthly_crossover(df)
                if sig is None:
                    continue

                fibo = calc_fibo_targets(sig["df"], sig["signal_idx"])
                if fibo is None:
                    continue

                symbol = ticker.replace(".NS", "")

                # Flatten fibo targets for storage
                fibo_flat = {}
                next_target = None
                for label, info in fibo["targets"].items():
                    fibo_flat[f"{label}_price"]    = info["price"]
                    fibo_flat[f"{label}_pct"]      = info["pct_gain"]
                    fibo_flat[f"{label}_hit"]      = info["hit"]
                    # First un-hit target = nearest
                    if next_target is None and not info["hit"]:
                        next_target = {"label": label, "price": info["price"], "pct": info["pct_gain"]}

                row = {
                    "Symbol"          : symbol,
                    "Breakout Date"   : pd.Timestamp(sig["signal_date"]).strftime("%Y-%m-%d"),
                    "Months Ago"      : sig["bars_ago"],
                    "is_fresh"        : bool(sig["is_fresh"]),
                    "RSI"             : sig["rsi"],
                    "RSI SMA"         : sig["rsi_sma"],
                    "CCI"             : round(sig["cci"], 1),
                    "CCI SMA"         : round(sig["cci_sma"], 1),
                    "RSI XO Date"     : pd.Timestamp(sig["rsi_xo_date"]).strftime("%Y-%m-%d"),
                    "CCI XO Date"     : pd.Timestamp(sig["cci_xo_date"]).strftime("%Y-%m-%d"),
                    "RSI Months Ago"  : sig["rsi_bars_ago"],
                    "CCI Months Ago"  : sig["cci_bars_ago"],
                    "Current Price"   : fibo["current_price"],
                    "Breakout Price"  : fibo["breakout_price"],
                    "Swing Low"       : fibo["swing_low"],
                    "Swing High"      : fibo["swing_high"],
                    "Swing Range"     : fibo["swing_range"],
                    "Swing Low Date"  : pd.Timestamp(fibo["swing_low_date"]).strftime("%Y-%m-%d"),
                    "Swing High Date" : pd.Timestamp(fibo["swing_high_date"]).strftime("%Y-%m-%d"),
                    "Next Target Lbl" : next_target["label"] if next_target else "—",
                    "Next Target"     : next_target["price"] if next_target else None,
                    "Next Target Pct" : next_target["pct"]   if next_target else None,
                    **fibo_flat,
                }
                results.append(row)

            except Exception as e:
                pass

    print(f"\nDone. Signals found: {len(results)}")
    return pd.DataFrame(results)


# ── HTML Report ───────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monthly RSI/CCI + Fibo Targets</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.2/css/bootstrap.min.css">
<style>
:root{--bg:#0a0a12;--card:#12122a;--card2:#1a1a2e;--border:#2d2d4e;--purple:#7c4dff;--lpurple:#bb86fc;--green:#00e676;--red:#ef5350;--amber:#ffb300;--teal:#80cbc4;--muted:#546e7a;--sub:#90a4ae;}
body{background:var(--bg);color:#e0e0e0;font-family:'Segoe UI',sans-serif;font-size:.875rem;}
.navbar-brand{color:var(--purple)!important;font-weight:700;font-size:1.25rem;}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 20px;text-align:center;}
.stat-num{font-size:1.8rem;font-weight:700;color:var(--lpurple);}
.stat-lbl{font-size:.72rem;color:var(--sub);}
.tab-btn{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:8px;padding:5px 16px;cursor:pointer;transition:all .15s;}
.tab-btn.active{background:var(--purple);color:#fff;border-color:var(--purple);}
.filter-bar{background:var(--card);padding:8px 14px;border-radius:10px;border:1px solid var(--border);}
input[type=text],select{background:#0d0d1a;border:1px solid var(--border);color:#e0e0e0;border-radius:6px;padding:4px 10px;font-size:.8rem;}
input[type=text]:focus,select:focus{outline:none;border-color:var(--purple);}
#tableWrap{max-height:72vh;overflow-y:auto;}
table{font-size:.8rem;}
thead th{background:#1a1a3e;color:var(--lpurple);cursor:pointer;user-select:none;white-space:nowrap;position:sticky;top:0;z-index:2;padding:8px 6px;}
thead th:hover{background:#252560;}
tbody tr{background:var(--card);border-bottom:1px solid #1a1a38;}
tbody tr:hover{background:#1e1e4e!important;}
td{padding:6px 6px;vertical-align:middle;}
.badge-fresh{background:var(--green);color:#000;font-weight:700;padding:2px 8px;border-radius:6px;font-size:.72rem;}
.badge-old{background:#263238;color:var(--sub);padding:2px 8px;border-radius:6px;font-size:.72rem;}
.fibo-row{display:flex;gap:4px;flex-wrap:wrap;}
.fibo-chip{padding:2px 7px;border-radius:5px;font-size:.7rem;font-weight:600;white-space:nowrap;}
.fibo-hit{background:#1b3a2a;color:var(--green);border:1px solid #2e7d52;}
.fibo-next{background:#2a1a4a;color:#ce93d8;border:1px solid var(--purple);}
.fibo-future{background:#1a1a2e;color:var(--sub);border:1px solid var(--border);}
.xo-rsi{color:#42a5f5;}
.xo-cci{color:#ffb300;}
.xo-both{color:var(--green);}
a.tv{color:var(--purple);text-decoration:none;font-size:1rem;}
a.tv:hover{color:var(--lpurple);}
.sort-ico{font-size:.6rem;margin-left:3px;opacity:.4;}
.sort-ico.on{opacity:1;color:var(--green);}
.detail-toggle{cursor:pointer;color:var(--sub);font-size:.85rem;transition:.1s;}
.detail-toggle:hover{color:var(--lpurple);}
.detail-row td{background:#0d0d22;padding:10px 16px;}
.progress-thin{height:5px;border-radius:3px;background:#1a1a3e;}
.progress-fill{height:5px;border-radius:3px;}
</style>
</head>
<body>
<nav class="navbar navbar-dark px-4 py-2" style="background:#070710;border-bottom:1px solid #1a1a3e;">
  <span class="navbar-brand">📈 Monthly RSI/CCI × Fibonacci Screener</span>
  <span style="color:var(--muted);font-size:.75rem;">Generated: __GENERATED__</span>
</nav>

<div class="container-fluid px-3 py-3">

  <!-- Stats -->
  <div class="row g-2 mb-3">
    <div class="col-6 col-md-2"><div class="stat-box"><div class="stat-num" id="sTotal">-</div><div class="stat-lbl">Total Signals</div></div></div>
    <div class="col-6 col-md-2"><div class="stat-box"><div class="stat-num" id="sFresh" style="color:var(--green)">-</div><div class="stat-lbl">🟢 Fresh (≤1 mo)</div></div></div>
    <div class="col-6 col-md-2"><div class="stat-box"><div class="stat-num" id="sRsiCci" style="color:#ce93d8">-</div><div class="stat-lbl">RSI+CCI Both Fresh</div></div></div>
    <div class="col-6 col-md-2"><div class="stat-box"><div class="stat-num" id="sNearTgt" style="color:var(--amber)">-</div><div class="stat-lbl">Near F0.618 Target</div></div></div>
    <div class="col-6 col-md-2"><div class="stat-box"><div class="stat-num" id="sAbove100" style="color:var(--red)">-</div><div class="stat-lbl">Above Breakout</div></div></div>
    <div class="col-6 col-md-2"><div class="stat-box"><div class="stat-num" id="sHit2" style="color:var(--teal)">-</div><div class="stat-lbl">Hit 1.618+ Target</div></div></div>
  </div>

  <!-- Filters -->
  <div class="d-flex gap-2 mb-3 flex-wrap align-items-center">
    <div class="d-flex gap-2">
      <button class="tab-btn active" onclick="setFresh('all',this)">All</button>
      <button class="tab-btn" onclick="setFresh('fresh',this)">🟢 Fresh</button>
      <button class="tab-btn" onclick="setFresh('old',this)">📅 Old</button>
    </div>
    <div class="filter-bar d-flex gap-2 align-items-center flex-wrap">
      <input type="text" id="symQ" placeholder="Symbol…" oninput="applyF()" style="width:130px">
      <select id="xoFilter" onchange="applyF()">
        <option value="all">All signals</option>
        <option value="both">RSI+CCI same month</option>
        <option value="rsi_new">RSI fresh (≤1mo)</option>
        <option value="cci_new">CCI fresh (≤1mo)</option>
      </select>
      <select id="targetFilter" onchange="applyF()">
        <option value="all">All targets</option>
        <option value="none_hit">No target hit yet</option>
        <option value="hit618">Hit 0.618</option>
        <option value="hit100">Hit 1.000</option>
        <option value="hit1618">Hit 1.618</option>
      </select>
      <span style="color:var(--muted);font-size:.75rem;" id="cntLbl"></span>
    </div>
  </div>

  <!-- Table -->
  <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;">
    <div id="tableWrap">
      <table class="table table-borderless mb-0" id="mainTbl">
        <thead>
          <tr>
            <th style="width:28px"></th>
            <th onclick="sortBy(0)" class="ps-2">Symbol<span class="sort-ico" id="si0">▲▼</span></th>
            <th onclick="sortBy(1)">Status<span class="sort-ico" id="si1">▲▼</span></th>
            <th onclick="sortBy(2)" title="Months since signal">Mo.<span class="sort-ico" id="si2">▲▼</span></th>
            <th onclick="sortBy(3)" title="Signal start date">Date<span class="sort-ico" id="si3">▲▼</span></th>
            <th onclick="sortBy(4)" title="RSI(14) current value">RSI<span class="sort-ico" id="si4">▲▼</span></th>
            <th onclick="sortBy(5)" title="CCI(20) current value">CCI<span class="sort-ico" id="si5">▲▼</span></th>
            <th onclick="sortBy(6)" title="Current price">Price ₹<span class="sort-ico" id="si6">▲▼</span></th>
            <th onclick="sortBy(7)" title="Price at RSI+CCI crossover">Brkout ₹<span class="sort-ico" id="si7">▲▼</span></th>
            <th onclick="sortBy(8)" title="Nearest un-hit Fibonacci target">Next Target<span class="sort-ico" id="si8">▲▼</span></th>
            <th title="Fibonacci extension targets — green=hit, purple=next, grey=future">Fibo Targets</th>
            <th>Chart</th>
          </tr>
        </thead>
        <tbody id="tBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const RAW = __JSON__;
let freshF='all', xoF='all', tgtF='all', sortCol=2, sortAsc=true;

function fiboChips(r) {
  const labels = ['F0.618','F1.000','F1.272','F1.618','F2.000','F2.618'];
  const next   = r['Next Target Lbl'];
  return labels.map(l => {
    const p = r[l+'_price'], pct = r[l+'_pct'], hit = r[l+'_hit'];
    const cls = hit ? 'fibo-hit' : (l===next ? 'fibo-next' : 'fibo-future');
    const icon = hit ? '✓' : (l===next ? '▶' : '');
    return `<span class="fibo-chip ${cls}" title="${l} = ₹${p ? p.toFixed(0) : '—'} (${pct != null ? (pct>0?'+':'')+pct+'%' : ''})">${icon}${l.replace('F','')} ₹${p ? Math.round(p).toLocaleString('en-IN') : '—'}</span>`;
  }).join('');
}

function detailRow(r, i) {
  const pct_from_bo = r['Current Price'] && r['Breakout Price']
    ? ((r['Current Price']/r['Breakout Price']-1)*100).toFixed(1) : '—';
  return `<tr class="detail-row" id="dr${i}" style="display:none">
    <td colspan="12">
      <div class="row g-3">
        <div class="col-md-4">
          <div style="color:var(--sub);font-size:.72rem;margin-bottom:6px;">📐 SWING STRUCTURE</div>
          <table style="width:100%;font-size:.78rem;">
            <tr><td style="color:var(--sub)">Swing Low</td><td style="color:#42a5f5">₹${r['Swing Low']?.toLocaleString('en-IN')} <span style="color:var(--muted);font-size:.7rem">(${r['Swing Low Date']})</span></td></tr>
            <tr><td style="color:var(--sub)">Swing High</td><td style="color:var(--amber)">₹${r['Swing High']?.toLocaleString('en-IN')} <span style="color:var(--muted);font-size:.7rem">(${r['Swing High Date']})</span></td></tr>
            <tr><td style="color:var(--sub)">Range</td><td style="color:#e0e0e0">₹${r['Swing Range']?.toLocaleString('en-IN')}</td></tr>
            <tr><td style="color:var(--sub)">Breakout</td><td style="color:var(--green)">₹${r['Breakout Price']?.toLocaleString('en-IN')}</td></tr>
            <tr><td style="color:var(--sub)">Current</td><td style="color:#fff">₹${r['Current Price']?.toLocaleString('en-IN')} <span style="color:${pct_from_bo>=0?'var(--green)':'var(--red)'}">(${pct_from_bo>=0?'+':''}${pct_from_bo}% from BO)</span></td></tr>
          </table>
        </div>
        <div class="col-md-4">
          <div style="color:var(--sub);font-size:.72rem;margin-bottom:6px;">📊 INDICATORS</div>
          <table style="width:100%;font-size:.78rem;">
            <tr><td style="color:var(--sub)">RSI(14)</td><td style="color:#42a5f5">${r.RSI} <span style="color:var(--muted)">vs SMA ${r['RSI SMA']}</span></td></tr>
            <tr><td style="color:var(--sub)">RSI XO Date</td><td style="color:#42a5f5">${r['RSI XO Date']} <span style="color:var(--muted)">(${r['RSI Months Ago']} mo ago)</span></td></tr>
            <tr><td style="color:var(--sub)">CCI(20)</td><td style="color:var(--amber)">${r.CCI} <span style="color:var(--muted)">vs SMA ${r['CCI SMA']}</span></td></tr>
            <tr><td style="color:var(--sub)">CCI XO Date</td><td style="color:var(--amber)">${r['CCI XO Date']} <span style="color:var(--muted)">(${r['CCI Months Ago']} mo ago)</span></td></tr>
          </table>
        </div>
        <div class="col-md-4">
          <div style="color:var(--sub);font-size:.72rem;margin-bottom:6px;">🎯 FIBO EXTENSION TARGETS</div>
          <div style="display:flex;flex-direction:column;gap:5px;">
            ${['F0.618','F1.000','F1.272','F1.618','F2.000','F2.618'].map(l => {
              const p = r[l+'_price'], pct = r[l+'_pct'], hit = r[l+'_hit'];
              const next = r['Next Target Lbl'] === l;
              const pctDone = r['Breakout Price'] && p ? Math.min(100, Math.max(0, (r['Current Price']-r['Breakout Price'])/(p-r['Breakout Price'])*100)) : 0;
              return `<div>
                <div style="display:flex;justify-content:space-between;font-size:.74rem;margin-bottom:2px;">
                  <span style="color:${hit?'var(--green)':next?'#ce93d8':'var(--sub)'}">${l.replace('F','')} ${hit?'✓':next?'▶':''}</span>
                  <span style="color:#e0e0e0">₹${p ? Math.round(p).toLocaleString('en-IN') : '—'}</span>
                  <span style="color:${pct>=0?'var(--green)':'var(--red)'}">${pct!=null?(pct>0?'+':'')+pct+'%':'—'}</span>
                </div>
                <div class="progress-thin"><div class="progress-fill" style="width:${pctDone}%;background:${hit?'var(--green)':next?'#9c27b0':'var(--border)'}"></div></div>
              </div>`;
            }).join('')}
          </div>
        </div>
      </div>
    </td>
  </tr>`;
}

function xoLabel(r) {
  const rb = r['RSI Months Ago'] <= 1;
  const cb = r['CCI Months Ago'] <= 1;
  if (rb && cb) return '<span class="xo-both">◉ RSI+CCI</span>';
  if (rb)       return '<span class="xo-rsi">◉ RSI</span> <span class="xo-cci" style="opacity:.5">◎ CCI</span>';
  if (cb)       return '<span class="xo-rsi" style="opacity:.5">◎ RSI</span> <span class="xo-cci">◉ CCI</span>';
  return        '<span style="color:var(--sub)">◎ RSI ◎ CCI</span>';
}

function render(data) {
  const tb = document.getElementById('tBody');
  document.getElementById('cntLbl').textContent = data.length+' rows';
  if (!data.length) { tb.innerHTML='<tr><td colspan="12" class="text-center py-5" style="color:var(--muted)">No results</td></tr>'; return; }
  tb.innerHTML = data.map((r,i) => {
    const fresh = r.is_fresh;
    const badge = fresh
      ? '<span class="badge-fresh">🟢 FRESH</span>'
      : `<span class="badge-old">📅 ${r['Breakout Date'].slice(0,7)}</span>`;
    const rsiColor = r.RSI >= 60 ? '#ef5350' : r.RSI >= 50 ? 'var(--amber)' : '#66bb6a';
    const cciColor = r.CCI >= 100 ? '#ef5350' : r.CCI >= 0 ? 'var(--green)' : 'var(--red)';
    const nxtLbl  = r['Next Target Lbl'] || '—';
    const nxtPrc  = r['Next Target'] ? '₹'+Math.round(r['Next Target']).toLocaleString('en-IN') : '—';
    const nxtPct  = r['Next Target Pct'] != null ? (r['Next Target Pct']>0?'+':'')+r['Next Target Pct']+'%' : '';
    return `<tr>
      <td class="ps-2"><span class="detail-toggle" onclick="toggleDetail(${i})" id="dt${i}">▶</span></td>
      <td><a href="https://www.tradingview.com/chart/?symbol=NSE:${r.Symbol}" target="_blank" style="color:#e0e0e0;text-decoration:none;font-weight:600">${r.Symbol}</a></td>
      <td>${badge}</td>
      <td style="color:var(--sub)">${r['Months Ago']}</td>
      <td style="color:var(--sub);font-size:.75rem">${r['Breakout Date'].slice(0,7)}</td>
      <td style="color:${rsiColor};font-weight:600">${r.RSI}</td>
      <td style="color:${cciColor};font-weight:600">${r.CCI}</td>
      <td style="color:#fff;font-weight:600">₹${r['Current Price']?.toLocaleString('en-IN')}</td>
      <td style="color:var(--green)">₹${r['Breakout Price']?.toLocaleString('en-IN')}</td>
      <td><span style="color:#ce93d8;font-weight:600">${nxtLbl.replace('F','')}</span> <span style="color:#e0e0e0">${nxtPrc}</span> <span style="color:var(--green);font-size:.72rem">${nxtPct}</span></td>
      <td><div class="fibo-row">${fiboChips(r)}</div></td>
      <td><a class="tv" href="https://www.tradingview.com/chart/?symbol=NSE:${r.Symbol}" target="_blank">📈</a></td>
    </tr>
    ${detailRow(r,i)}`;
  }).join('');
}

function toggleDetail(i) {
  const dr = document.getElementById('dr'+i);
  const dt = document.getElementById('dt'+i);
  const open = dr.style.display !== 'none';
  dr.style.display = open ? 'none' : 'table-row';
  dt.textContent = open ? '▶' : '▼';
}

function updateStats(data) {
  document.getElementById('sTotal').textContent   = data.length;
  document.getElementById('sFresh').textContent   = data.filter(r=>r.is_fresh).length;
  document.getElementById('sRsiCci').textContent  = data.filter(r=>r['RSI Months Ago']<=1&&r['CCI Months Ago']<=1).length;
  document.getElementById('sNearTgt').textContent = data.filter(r=>r['Next Target Lbl']==='F0.618').length;
  document.getElementById('sAbove100').textContent= data.filter(r=>r['Current Price']>r['Breakout Price']).length;
  document.getElementById('sHit2').textContent    = data.filter(r=>r['F1.618_hit']).length;
}

function filtered() {
  const sym = document.getElementById('symQ').value.trim().toUpperCase();
  return RAW.filter(r => {
    if (freshF==='fresh' && !r.is_fresh)  return false;
    if (freshF==='old'   &&  r.is_fresh)  return false;
    if (sym && !r.Symbol.includes(sym))   return false;
    if (xoF==='both'    && !(r['RSI Months Ago']<=1&&r['CCI Months Ago']<=1)) return false;
    if (xoF==='rsi_new' && r['RSI Months Ago']>1)  return false;
    if (xoF==='cci_new' && r['CCI Months Ago']>1)  return false;
    if (tgtF==='none_hit' && r['F0.618_hit'])  return false;
    if (tgtF==='hit618'   && !r['F0.618_hit']) return false;
    if (tgtF==='hit100'   && !r['F1.000_hit']) return false;
    if (tgtF==='hit1618'  && !r['F1.618_hit']) return false;
    return true;
  });
}

const SORT_KEYS = ['Symbol','is_fresh','Months Ago','Breakout Date','RSI','CCI','Current Price','Breakout Price','Next Target'];
function applyF() {
  let data = filtered();
  data = [...data].sort((a,b)=>{
    let va=a[SORT_KEYS[sortCol]], vb=b[SORT_KEYS[sortCol]];
    if(va==null) va = sortAsc?Infinity:-Infinity;
    if(vb==null) vb = sortAsc?Infinity:-Infinity;
    if(typeof va==='string') va=va.toLowerCase(), vb=vb.toLowerCase();
    return sortAsc ? (va<vb?-1:va>vb?1:0) : (va>vb?-1:va<vb?1:0);
  });
  updateStats(data);
  render(data);
}

function setFresh(v,el) {
  freshF=v;
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  applyF();
}

function sortBy(col) {
  if(sortCol===col) sortAsc=!sortAsc; else {sortCol=col;sortAsc=true;}
  document.querySelectorAll('[id^=si]').forEach(e=>{e.textContent='▲▼';e.classList.remove('on');});
  const ic=document.getElementById('si'+col);
  ic.textContent=sortAsc?'▲':'▼'; ic.classList.add('on');
  applyF();
}

applyF();
</script>
</body>
</html>
"""


def build_html(df: pd.DataFrame, out_path: str):
    df = df.copy()
    # Ensure bool serializable
    for col in [c for c in df.columns if "_hit" in c or c == "is_fresh"]:
        df[col] = df[col].astype(bool)

    df = df.sort_values(["is_fresh", "Months Ago"], ascending=[False, True])
    records = json.loads(df.to_json(orient="records", date_format="iso", default_handler=str))

    # Clean NaN/None
    for r in records:
        for k, v in r.items():
            if v != v or v is None:  # NaN check
                r[k] = None

    html = HTML.replace("__GENERATED__", datetime.now().strftime("%d %b %Y %H:%M"))
    html = html.replace("__JSON__", json.dumps(records, default=str))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅  Saved → {out_path}")


def main():
    print(f"Loading: {EQUITY_CSV}")
    tickers = load_tickers(EQUITY_CSV)
    print(f"Tickers: {len(tickers)}")

    df = screen(tickers)
    if df.empty:
        print("No signals found.")
        return

    print(f"\nSummary:")
    print(f"  Total signals  : {len(df)}")
    print(f"  Fresh (≤1 mo)  : {df['is_fresh'].sum()}")
    print(f"  RSI+CCI fresh  : {((df['RSI Months Ago']<=1) & (df['CCI Months Ago']<=1)).sum()}")
    build_html(df, OUT_HTML)


if __name__ == "__main__":
    main()
