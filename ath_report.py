#!/usr/bin/env python3
"""
ATH Breakout Report — NSE All-Time High Scanner
Scans all NSE stocks and shows which are at or near their All-Time High
Outputs: ath_report_NSE.html
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

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_HTML     = "ath_report_NSE.html"
LOCAL_NSE_CSV   = "india/NSE/NSECash/EQUITY_L.csv"
DATA_YEARS      = 10          # years of history (for true ATH)
BATCH_SIZE      = 20          # stocks per yfinance batch
BATCH_PAUSE     = 1.0         # seconds between batches
MAX_WORKERS     = 4
CACHE_FILE      = "ath_cache.pkl"
USE_CACHE       = True
CACHE_MAX_AGE_H = 6           # hours before cache expires

IST_OFFSET = "+05:30"

# ── Stock List Loader ─────────────────────────────────────────────────────────
def load_nse_tickers():
    tickers = []
    if os.path.exists(LOCAL_NSE_CSV):
        try:
            with open(LOCAL_NSE_CSV, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = (row.get("SYMBOL") or row.get("Symbol") or "").strip()
                    series = (row.get("SERIES") or row.get("Series") or "EQ").strip()
                    name = (row.get("NAME OF COMPANY") or row.get("Company Name") or sym).strip()
                    if sym and series == "EQ":
                        tickers.append((sym, name))
            print(f"  Loaded {len(tickers)} NSE EQ stocks from CSV")
            return tickers
        except Exception as e:
            print(f"  Warning: CSV error: {e}")

    # Fallback: Nifty 50 stocks
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
        ("SBILIFE","SBI Life"),("ICICIPRULI","ICICI Prudential Life"),
        ("M&M","Mahindra & Mahindra"),("TATASTEEL","Tata Steel"),("UPL","UPL"),
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

# ── RSI calculation ───────────────────────────────────────────────────────────
def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).iloc[-1]

# ── Fetch & analyse one stock ─────────────────────────────────────────────────
def analyse(symbol, name, cache):
    yf_sym = symbol + ".NS"
    if yf_sym in cache:
        return cache[yf_sym]

    try:
        end   = datetime.today()
        start = end - timedelta(days=DATA_YEARS * 365 + 60)
        df = yf.download(
            yf_sym,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False, auto_adjust=True
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 20:
            return None

        df = df[["Open","High","Low","Close","Volume"]].dropna()

        close = df["Close"]
        vol   = df["Volume"]

        # ATH from High series (true extreme)
        ath_price  = float(df["High"].max())
        ath_idx    = df["High"].idxmax()
        ath_date   = ath_idx.strftime("%d %b %Y")
        last_close = float(close.iloc[-1])
        last_date  = df.index[-1]
        first_date = df.index[0]

        is_ath  = last_close >= ath_price * 0.99
        ath_pct = round((last_close / ath_price - 1) * 100, 1)

        # Time string
        if is_ath:
            days = (ath_idx.date() - first_date.date()).days
        else:
            days = (last_date.date() - ath_idx.date()).days

        def _ts(d):
            d = max(0, d)
            y, r = divmod(d, 365)
            m = r // 30
            if y and m: return f"{y}y {m}m"
            if y: return f"{y}y"
            if m: return f"{m}m"
            return f"{d}d"

        ath_time = _ts(days) + (" to reach" if is_ath else " ago")

        # RSI (daily)
        rsi_d = round(float(calc_rsi(close, 14)), 1) if len(close) >= 20 else None

        # Weekly RSI
        df_w = df.resample("W").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        rsi_w = round(float(calc_rsi(df_w["Close"], 14)), 1) if len(df_w) >= 20 else None

        # Monthly RSI
        df_m = df.resample("ME").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        rsi_m = round(float(calc_rsi(df_m["Close"], 14)), 1) if len(df_m) >= 14 else None

        # Volume ratio (today vs 20-day avg)
        vol_avg = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else None
        vol_today = float(vol.iloc[-1])
        vol_ratio = round(vol_today / vol_avg, 1) if vol_avg and vol_avg > 0 else None

        # 52-week high
        high52 = float(df["High"].tail(252).max()) if len(df) >= 252 else float(df["High"].max())
        dist52 = round((last_close / high52 - 1) * 100, 1)

        # EMA trend (simple phase)
        ema20  = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50  = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        if last_close > ema20 > ema50 > ema200:
            phase = "UPTREND"
        elif last_close < ema20 < ema50:
            phase = "BEARISH"
        else:
            phase = "SIDEWAYS"

        # Market cap (approx from shares outstanding — not available in yfinance basic)
        # Use a simple price * volume proxy or leave None
        mcap = None

        result = {
            "symbol":    symbol,
            "name":      name[:35],
            "yf_sym":    yf_sym,
            "close":     round(last_close, 2),
            "ath_price": round(ath_price, 2),
            "ath_pct":   ath_pct,
            "is_ath":    is_ath,
            "ath_date":  ath_date,
            "ath_time":  ath_time,
            "rsi_d":     rsi_d,
            "rsi_w":     rsi_w,
            "rsi_m":     rsi_m,
            "vol_ratio": vol_ratio,
            "dist52":    dist52,
            "high52":    round(high52, 2),
            "phase":     phase,
            "bars":      len(df),
        }
        cache[yf_sym] = result
        return result
    except Exception as e:
        return None

# ── Batch downloader ──────────────────────────────────────────────────────────
def fetch_all(tickers):
    cache = load_cache()
    results = []
    total = len(tickers)
    done  = 0

    # Split into batches
    batches = [tickers[i:i+BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]

    for bi, batch in enumerate(batches, 1):
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(analyse, sym, nm, cache): (sym, nm) for sym, nm in batch}
            for fut in as_completed(futs):
                sym, nm = futs[fut]
                done += 1
                try:
                    r = fut.result()
                    if r:
                        results.append(r)
                except Exception:
                    pass
                if done % 50 == 0 or done == total:
                    pct = done / total * 100
                    print(f"  [{done}/{total}] {pct:.0f}%  — {len(results)} with data", flush=True)

        if bi < len(batches):
            time.sleep(BATCH_PAUSE)

    save_cache(cache)
    return results

# ── ATH category ─────────────────────────────────────────────────────────────
def ath_category(r):
    if r["is_ath"]:    return "at"
    p = r["ath_pct"]
    if p >= -5:        return "w5"
    if p >= -10:       return "w10"
    if p >= -20:       return "w20"
    return "far"

def ath_sort_key(r):
    if r["is_ath"]:    return 0
    return -r["ath_pct"]   # e.g. -5 → 5 (closer = smaller number)

# ── HTML builder ─────────────────────────────────────────────────────────────
def rsi_color(v):
    if v is None: return "#8b949e"
    if v >= 70: return "#ff6b6b"
    if v >= 60: return "#26d07c"
    if v >= 50: return "#f0b429"
    return "#8b949e"

def phase_badge(phase):
    MAP = {
        "UPTREND":  ("📈", "#26d07c", "#0d2615"),
        "SIDEWAYS": ("➡️",  "#f0b429", "#2a1e00"),
        "BEARISH":  ("📉", "#ff6b6b", "#2a0000"),
    }
    em, fg, bg = MAP.get(phase, ("⏸", "#8b949e", "#1a1a1a"))
    return (f'<span style="background:{bg};color:{fg};border:1px solid {fg}55;'
            f'border-radius:10px;padding:2px 8px;font-size:10px;font-weight:700">'
            f'{em} {phase}</span>')

def ath_badge(r):
    if r["is_ath"]:
        return '<span style="background:#002d1a;color:#00ff88;border:1px solid #00ff8855;border-radius:10px;padding:2px 8px;font-size:10px;font-weight:700">🏆 AT ATH</span>'
    p = r["ath_pct"]
    if p >= -5:  clr, bg = "#f0b429", "#2a1e00"
    elif p >= -10: clr, bg = "#fb923c", "#2a1000"
    elif p >= -20: clr, bg = "#f97316", "#2a0800"
    else:          clr, bg = "#ff6b6b", "#2a0000"
    return (f'<span style="background:{bg};color:{clr};border:1px solid {clr}55;'
            f'border-radius:10px;padding:2px 8px;font-size:10px;font-weight:700">'
            f'{p:+.1f}% ATH</span>')

def fmt_inr(v):
    if v is None: return "—"
    if v >= 1000: return f"₹{v:,.0f}"
    return f"₹{v:.2f}"

def build_html(results, run_ts):
    # Sort: AT ATH first, then closest to ATH
    results.sort(key=ath_sort_key)

    n_total = len(results)
    n_at    = sum(1 for r in results if r["is_ath"])
    n_w5    = sum(1 for r in results if ath_category(r) == "w5")
    n_w10   = sum(1 for r in results if ath_category(r) == "w10")
    n_w20   = sum(1 for r in results if ath_category(r) == "w20")
    n_far   = sum(1 for r in results if ath_category(r) == "far")
    n_up    = sum(1 for r in results if r["phase"] == "UPTREND")

    rows = []
    for i, r in enumerate(results, 1):
        rd = r["rsi_d"]
        rw = r["rsi_w"]
        rm = r["rsi_m"]
        vr = r["vol_ratio"]
        vr_str  = f"{vr:.1f}x" if vr else "—"
        vr_clr  = "#26d07c" if (vr and vr >= 2) else "#f0b429" if (vr and vr >= 1) else "#8b949e"
        d52_clr = "#26d07c" if r["dist52"] >= -2 else "#f0b429" if r["dist52"] >= -10 else "#8b949e"
        cat     = ath_category(r)

        rows.append(f'''<tr data-cat="{cat}" data-phase="{r['phase']}" data-ath="{r['ath_pct']}">
  <td style="color:#8b949e;text-align:center">{i}</td>
  <td>
    <b style="color:#e6edf3">{r['symbol']}</b><br>
    <span style="color:#8b949e;font-size:10px">{r['name']}</span>
  </td>
  <td style="text-align:right">{fmt_inr(r['close'])}</td>
  <td style="text-align:center">{ath_badge(r)}</td>
  <td style="text-align:right;color:#8b949e;font-size:11px">{fmt_inr(r['ath_price'])}<br><span style="font-size:10px">{r['ath_date']}</span></td>
  <td style="text-align:center;color:#8b949e;font-size:11px">{r['ath_time']}</td>
  <td style="text-align:right;color:{d52_clr}">{r['dist52']:+.1f}%</td>
  <td style="text-align:right;color:{rsi_color(rd)};font-weight:700">{rd if rd else "—"}</td>
  <td style="text-align:right;color:{rsi_color(rw)}">{rw if rw else "—"}</td>
  <td style="text-align:right;color:{rsi_color(rm)}">{rm if rm else "—"}</td>
  <td style="text-align:right;color:{vr_clr}">{vr_str}</td>
  <td style="text-align:center">{phase_badge(r['phase'])}</td>
</tr>''')

    rows_html = "\n".join(rows)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ATH Breakout Report — {run_ts}</title>
<style>
:root {{
  --bg:#0d1117; --card:#161b22; --border:#21262d; --text:#e6edf3;
  --sub:#8b949e; --cyan:#00d4ff; --green:#26d07c; --gold:#f0b429;
  --red:#ff6b6b;
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
.stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 18px;min-width:90px}}
.stat .val{{font-size:22px;font-weight:700}}
.stat .lbl{{font-size:10px;color:var(--sub);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}}
.filter-bar{{padding:10px 24px;background:#010409;border-bottom:1px solid var(--border);
             position:sticky;top:0;z-index:1000;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.filter-bar input{{flex:1;min-width:180px;max-width:280px;background:var(--card);border:1px solid var(--border);
                   color:var(--text);border-radius:20px;padding:5px 14px;font-size:12px;outline:none}}
.filter-bar input:focus{{border-color:var(--cyan)}}
.filter-btn{{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;
             padding:4px 12px;cursor:pointer;font-size:11.5px;transition:all .15s;font-weight:600}}
.filter-btn:hover,.filter-btn.active{{background:var(--cyan);color:#000;border-color:var(--cyan)}}
select.filter-btn{{cursor:pointer;padding:4px 10px}}
table{{width:100%;border-collapse:collapse}}
th{{background:#010409;padding:9px 10px;text-align:right;font-size:11px;color:var(--sub);
    text-transform:uppercase;letter-spacing:.5px;cursor:pointer;white-space:nowrap;
    border-bottom:1px solid var(--border)}}
th:hover{{color:var(--cyan)}}
th.asc::after{{content:" ▲"}}th.desc::after{{content:" ▼"}}
th:first-child,th:nth-child(2){{text-align:left}}
td{{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr:hover td{{background:#161b2288}}
#countInfo{{color:var(--sub);font-size:11px;margin-left:4px}}
.footer{{text-align:center;padding:24px;color:var(--sub);font-size:11px;
         border-top:1px solid var(--border);margin-top:20px}}
</style>
</head>
<body>

<div class="header">
  <h1>🏆 ATH Breakout Report — NSE All-Time High Scanner</h1>
  <div class="subtitle">
    Stocks at or near their All-Time High &nbsp;|&nbsp; {run_ts} IST
    &nbsp;|&nbsp; Sorted by proximity to ATH
  </div>
  <div class="nav-links">
    <a class="nav-link" href="/">📊 Full RSI Report</a>
    <a class="nav-link active" href="/ath">🏆 ATH Breakout</a>
    <a class="nav-link" href="/multibagger">💎 Multibagger</a>
    <a class="nav-link" href="/rocket">🚀 Rocket Scanner</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="val" style="color:var(--cyan)">{n_total}</div><div class="lbl">Total Scanned</div></div>
  <div class="stat"><div class="val" style="color:#00ff88">{n_at}</div><div class="lbl">🏆 At ATH</div></div>
  <div class="stat"><div class="val" style="color:var(--gold)">{n_w5}</div><div class="lbl">Within 5%</div></div>
  <div class="stat"><div class="val" style="color:var(--gold)">{n_w10}</div><div class="lbl">Within 10%</div></div>
  <div class="stat"><div class="val" style="color:#fb923c">{n_w20}</div><div class="lbl">Within 20%</div></div>
  <div class="stat"><div class="val" style="color:var(--red)">{n_far}</div><div class="lbl">&gt;20% Away</div></div>
  <div class="stat"><div class="val" style="color:var(--green)">{n_up}</div><div class="lbl">📈 Uptrend</div></div>
</div>

<div class="filter-bar">
  <input type="text" id="searchInput" placeholder="🔍 Search ticker or company…" oninput="filterTable()">
  <button class="filter-btn active" onclick="filterCat('all',this)">All</button>
  <button class="filter-btn" onclick="filterCat('at',this)">🏆 At ATH <b>({n_at})</b></button>
  <button class="filter-btn" onclick="filterCat('w5',this)">✅ &lt;5% <b>({n_w5})</b></button>
  <button class="filter-btn" onclick="filterCat('w10',this)">🟡 &lt;10% <b>({n_w10})</b></button>
  <button class="filter-btn" onclick="filterCat('w20',this)">🟠 &lt;20% <b>({n_w20})</b></button>
  <button class="filter-btn" onclick="filterCat('far',this)">📉 &gt;20% <b>({n_far})</b></button>
  <select class="filter-btn" id="phaseSelect" onchange="filterTable()">
    <option value="all">All Phases</option>
    <option value="UPTREND">📈 Uptrend</option>
    <option value="SIDEWAYS">➡️ Sideways</option>
    <option value="BEARISH">📉 Bearish</option>
  </select>
  <span id="countInfo"></span>
</div>

<table id="mainTable">
<thead>
<tr>
  <th onclick="thSort(0)">#</th>
  <th onclick="thSort(1)" style="text-align:left">Ticker / Company</th>
  <th onclick="thSort(2)">Price</th>
  <th onclick="thSort(3)" style="text-align:center">ATH Distance</th>
  <th onclick="thSort(4)">ATH Price</th>
  <th onclick="thSort(5)">ATH Time</th>
  <th onclick="thSort(6)">52W Dist</th>
  <th onclick="thSort(7)">RSI D</th>
  <th onclick="thSort(8)">RSI W</th>
  <th onclick="thSort(9)">RSI M</th>
  <th onclick="thSort(10)">Vol Ratio</th>
  <th onclick="thSort(11)" style="text-align:center">Phase</th>
</tr>
</thead>
<tbody id="tableBody">
{rows_html}
</tbody>
</table>

<div class="footer">
  ATH Breakout Report &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp;
  <b>Not financial advice.</b> For educational purposes only.<br>
  ATH = All-Time High using {DATA_YEARS}-year price history &nbsp;|&nbsp;
  "At ATH" = within 1% of all-time high
</div>

<script>
let activeCat = 'all';

function filterCat(cat, btn) {{
  activeCat = cat;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  filterTable();
}}

function filterTable() {{
  const q = document.getElementById('searchInput').value.toLowerCase();
  const phase = document.getElementById('phaseSelect').value;
  const rows = document.querySelectorAll('#tableBody tr');
  let shown = 0;
  rows.forEach(r => {{
    const cat   = r.dataset.cat   || '';
    const ph    = r.dataset.phase || '';
    const text  = r.textContent.toLowerCase();
    const catOk   = activeCat === 'all' || cat === activeCat;
    const phaseOk = phase === 'all' || ph === phase;
    const searchOk = !q || text.includes(q);
    const visible = catOk && phaseOk && searchOk;
    r.style.display = visible ? '' : 'none';
    if (visible) shown++;
  }});
  document.getElementById('countInfo').textContent = `Showing ${{shown}} of {n_total} stocks`;
}}

let lastCol = -1, lastDir = 1;
function thSort(col) {{
  const tbody = document.getElementById('tableBody');
  const rows  = [...tbody.querySelectorAll('tr')];
  const dir   = lastCol === col ? -lastDir : 1;
  lastCol = col; lastDir = dir;
  rows.sort((a, b) => {{
    const av = a.cells[col]?.textContent.replace(/[₹,%+▲▼x ]/g,'').trim() || '';
    const bv = b.cells[col]?.textContent.replace(/[₹,%+▲▼x ]/g,'').trim() || '';
    const an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return dir * (an - bn);
    return dir * av.localeCompare(bv);
  }});
  rows.forEach(r => tbody.appendChild(r));
  document.querySelectorAll('th').forEach((h, i) => {{
    h.className = i === col ? (dir === 1 ? 'asc' : 'desc') : '';
  }});
}}

document.addEventListener('DOMContentLoaded', () => {{
  document.getElementById('countInfo').textContent = `Showing {n_total} of {n_total} stocks`;
}});
</script>
</body>
</html>'''


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now()
    run_ts = now.strftime("%d %b %Y %H:%M") + " " + IST_OFFSET

    print("=" * 60)
    print(f"  ATH BREAKOUT REPORT — {run_ts}")
    print("=" * 60)

    tickers = load_nse_tickers()
    print(f"\n  Fetching data for {len(tickers)} stocks…")
    start = time.time()

    results = fetch_all(tickers)

    elapsed = time.time() - start
    print(f"\n  Done: {len(results)} stocks with data in {elapsed:.0f}s")
    print(f"  At ATH: {sum(1 for r in results if r['is_ath'])}")
    print(f"  Within 5%: {sum(1 for r in results if ath_category(r)=='w5')}")
    print(f"  Within 10%: {sum(1 for r in results if ath_category(r)=='w10')}")

    print("\n  Building HTML…")
    html = build_html(results, run_ts)

    tmp = OUTPUT_HTML + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, OUTPUT_HTML)

    size_kb = os.path.getsize(OUTPUT_HTML) / 1024
    print(f"  ✅ Saved: {OUTPUT_HTML} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
