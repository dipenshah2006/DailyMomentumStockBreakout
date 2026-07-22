"""
NSE INTRADAY BREAKOUT REPORT
============================
Batch script — runs once at market open, outputs intraday_report_NSE.html
No Flask · No talib · Pure pandas/numpy

Strategies scanned:
  1. PDH Breakout   — price crossed above previous-day high with volume surge
  2. VWAP Breakout  — price crossed above intraday VWAP
  3. Opening Range  — price broke above 15-min opening range high
  4. MTF Momentum   — Daily RSI 55-70 + intraday 5M trend up

Run: python intraday_report.py
Output: intraday_report_NSE.html
"""

import os
import sys
import warnings
import math
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
LOCAL_NSE_CSV   = "india/NSE/NSECash/EQUITY_L.csv"
LOCAL_FO_CSV    = "india/NSE/nse_fo_list.csv"
OUTPUT_FILE     = "intraday_report_NSE.html"
MAX_STOCKS      = 500        # scan top 500 stocks (F&O first, then large-cap)
MAX_WORKERS     = 12
MIN_PRICE       = 20
MIN_DAILY_VOL   = 50_000    # skip illiquid stocks
VOL_SURGE_RATIO = 1.5       # intraday volume > 1.5x expected → surge


# ── Helpers ───────────────────────────────────────────────────────────────────
def calc_rsi(series: pd.Series, period: int = 14) -> float:
    s = series.dropna()
    if len(s) < period + 1:
        return float("nan")
    delta = s.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def calc_vwap(df5: pd.DataFrame) -> float:
    """Intraday VWAP from 5-minute bars (today only)."""
    tp = (df5["High"] + df5["Low"] + df5["Close"]) / 3
    vol = df5["Volume"].replace(0, float("nan"))
    vwap = (tp * vol).cumsum() / vol.cumsum()
    return float(vwap.iloc[-1])


def calc_opening_range(df5: pd.DataFrame, minutes: int = 15) -> tuple[float, float]:
    """High/Low of the first `minutes` of today's session (≈ first 3 5-min bars)."""
    n = max(1, minutes // 5)
    subset = df5.head(n)
    return float(subset["High"].max()), float(subset["Low"].min())


def pct(a, b):
    if b and b != 0:
        return round((a / b - 1) * 100, 2)
    return None


# ── Universe loader ───────────────────────────────────────────────────────────
def load_universe() -> list[dict]:
    fo_set = set()
    if os.path.exists(LOCAL_FO_CSV):
        try:
            df = pd.read_csv(LOCAL_FO_CSV)
            col = next((c for c in df.columns if "SYMBOL" in c.upper()), df.columns[0])
            fo_set = set(df[col].str.strip().str.upper())
        except Exception:
            pass

    stocks = []
    if os.path.exists(LOCAL_NSE_CSV):
        try:
            df = pd.read_csv(LOCAL_NSE_CSV)
            sym_col  = next((c for c in df.columns if "SYMBOL" in c.upper()), None)
            name_col = next((c for c in df.columns if "NAME"   in c.upper()), None)
            ser_col  = next((c for c in df.columns if "SERIES" in c.upper()), None)
            for _, row in df.iterrows():
                if ser_col and str(row.get(ser_col, "")).strip() != "EQ":
                    continue
                sym = str(row.get(sym_col, "")).strip()
                nm  = str(row.get(name_col, sym)).strip()[:35]
                if sym:
                    stocks.append({"sym": sym, "name": nm, "fo": sym in fo_set})
        except Exception:
            pass

    if not stocks:
        fallback = ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","KOTAKBANK",
                    "SBIN","AXISBANK","LT","WIPRO","HCLTECH","BAJFINANCE",
                    "TITAN","ASIANPAINT","MARUTI","ULTRACEMCO","NTPC",
                    "POWERGRID","ONGC","TATAMOTORS","M&M","SUNPHARMA"]
        stocks = [{"sym": s, "name": s, "fo": True} for s in fallback]

    # F&O stocks first, then rest
    stocks.sort(key=lambda x: (0 if x["fo"] else 1, x["sym"]))
    return stocks[:MAX_STOCKS]


# ── Per-stock analyser ────────────────────────────────────────────────────────
def analyse(sym: str, name: str, fo: bool) -> dict | None:
    yf_sym = sym + ".NS"
    try:
        today = date.today()

        # Daily data: last 30 days for RSI + PDH
        df_d = yf.download(
            yf_sym, period="30d", interval="1d",
            auto_adjust=True, progress=False
        )
        if isinstance(df_d.columns, pd.MultiIndex):
            df_d.columns = df_d.columns.get_level_values(0)
        df_d = df_d[["Open","High","Low","Close","Volume"]].dropna()

        if df_d.empty or len(df_d) < 5:
            return None

        last_close  = float(df_d["Close"].iloc[-1])
        avg_vol_20d = float(df_d["Volume"].tail(20).mean())

        if last_close < MIN_PRICE or avg_vol_20d < MIN_DAILY_VOL:
            return None

        # Previous day high/low/close
        if len(df_d) >= 2:
            pdh = float(df_d["High"].iloc[-2])
            pdl = float(df_d["Low"].iloc[-2])
            pdc = float(df_d["Close"].iloc[-2])
        else:
            pdh = pdl = pdc = last_close

        rsi_d = calc_rsi(df_d["Close"], 14)

        # Daily EMAs
        ema20  = float(df_d["Close"].ewm(span=20,  adjust=False).mean().iloc[-1])
        ema50  = float(df_d["Close"].ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200 = float(df_d["Close"].ewm(span=200, adjust=False).mean().iloc[-1]) if len(df_d) >= 50 else ema50

        # Intraday 5-min data
        df5 = yf.download(
            yf_sym, period="1d", interval="5m",
            auto_adjust=True, progress=False
        )
        if isinstance(df5.columns, pd.MultiIndex):
            df5.columns = df5.columns.get_level_values(0)
        df5 = df5[["Open","High","Low","Close","Volume"]].dropna()

        if df5.empty or len(df5) < 3:
            # No intraday data yet (pre-market) — use daily only
            vwap = None
            or_high = or_low = None
            intraday_vol = 0
            last_price = last_close
            price_vs_vwap = None
            price_vs_orh  = None
        else:
            vwap       = calc_vwap(df5)
            or_high, or_low = calc_opening_range(df5, 15)
            last_price = float(df5["Close"].iloc[-1])
            intraday_vol = int(df5["Volume"].sum())
            price_vs_vwap = pct(last_price, vwap)
            price_vs_orh  = pct(last_price, or_high)

        # Expected intraday volume by now (linear extrapolation over 375-min session)
        now_min = datetime.now().hour * 60 + datetime.now().minute
        market_start_min = 9 * 60 + 15   # 9:15 AM
        market_end_min   = 15 * 60 + 30  # 3:30 PM
        elapsed = max(1, now_min - market_start_min)
        session_fraction = min(1.0, elapsed / (market_end_min - market_start_min))
        expected_vol = avg_vol_20d * session_fraction
        vol_ratio = round(intraday_vol / expected_vol, 1) if expected_vol > 0 else None

        # Signals
        price_vs_pdh = pct(last_price, pdh)
        pdh_breakout = last_price > pdh * 1.002

        vwap_breakout = (price_vs_vwap is not None and price_vs_vwap > 0)
        orh_breakout  = (price_vs_orh  is not None and price_vs_orh  > 0)

        daily_trend_up = (last_close > ema20 > ema50) and (not math.isnan(rsi_d)) and rsi_d >= 55
        momentum_setup = daily_trend_up and vwap_breakout

        # Score (0-100)
        score = 0
        if pdh_breakout:                         score += 35
        if vwap_breakout:                         score += 20
        if orh_breakout:                          score += 15
        if daily_trend_up:                        score += 15
        if vol_ratio and vol_ratio >= VOL_SURGE_RATIO: score += 15

        if score == 0:
            return None

        return {
            "sym":            sym,
            "name":           name,
            "fo":             fo,
            "last_price":     round(last_price, 2),
            "pdh":            round(pdh, 2),
            "pdl":            round(pdl, 2),
            "price_vs_pdh":   price_vs_pdh,
            "pdh_breakout":   pdh_breakout,
            "vwap":           round(vwap, 2) if vwap else None,
            "price_vs_vwap":  price_vs_vwap,
            "vwap_breakout":  vwap_breakout,
            "or_high":        round(or_high, 2) if or_high else None,
            "price_vs_orh":   price_vs_orh,
            "orh_breakout":   orh_breakout,
            "rsi_d":          round(rsi_d, 1) if not math.isnan(rsi_d) else None,
            "ema20":          round(ema20, 2),
            "ema50":          round(ema50, 2),
            "daily_trend_up": daily_trend_up,
            "vol_ratio":      vol_ratio,
            "score":          score,
        }
    except Exception:
        return None


# ── Batch runner ──────────────────────────────────────────────────────────────
def run_scan(universe: list[dict]) -> list[dict]:
    results = []
    total   = len(universe)
    done    = 0
    print(f"Scanning {total} stocks for intraday breakouts...", flush=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {
            ex.submit(analyse, s["sym"], s["name"], s["fo"]): s
            for s in universe
        }
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass
            if done % 50 == 0 or done == total:
                print(f"  [{done}/{total}] {len(results)} candidates", flush=True)

    results.sort(key=lambda x: -x["score"])
    return results


# ── HTML builder ──────────────────────────────────────────────────────────────
def signal_badge(label: str, active: bool, color: str = "#26d07c") -> str:
    if not active:
        return f'<span style="color:#21262d;font-size:10px;font-weight:700">{label}</span>'
    return (f'<span style="background:{color}22;color:{color};border:1px solid {color}55;'
            f'border-radius:8px;padding:1px 7px;font-size:10px;font-weight:700">{label}</span>')


def rsi_clr(v):
    if v is None: return "#8b949e"
    if v >= 70:   return "#ff6b6b"
    if v >= 55:   return "#26d07c"
    if v >= 45:   return "#f0b429"
    return "#8b949e"


def pct_clr(v, neutral_zero=True):
    if v is None: return "#8b949e"
    if v > 0:     return "#26d07c"
    if v < 0:     return "#ff6b6b"
    return "#f0b429"


def score_bar(s: int) -> str:
    w = min(100, s)
    clr = "#26d07c" if s >= 70 else "#f0b429" if s >= 40 else "#fb923c"
    return (f'<div style="display:flex;align-items:center;gap:5px">'
            f'<div style="width:60px;height:6px;background:#21262d;border-radius:3px">'
            f'<div style="width:{w}%;height:100%;background:{clr};border-radius:3px"></div></div>'
            f'<span style="color:{clr};font-weight:700;font-size:11px">{s}</span></div>')


def build_html(results: list[dict]) -> str:
    run_ts   = datetime.now().strftime("%d %b %Y  %H:%M IST")
    n_total  = len(results)
    n_pdh    = sum(1 for r in results if r["pdh_breakout"])
    n_vwap   = sum(1 for r in results if r["vwap_breakout"])
    n_orh    = sum(1 for r in results if r["orh_breakout"])
    n_strong = sum(1 for r in results if r["score"] >= 70)

    rows = []
    for i, r in enumerate(results, 1):
        pvp = r["price_vs_pdh"]
        pvp_str = f"{pvp:+.2f}%" if pvp is not None else "—"

        pvv = r["price_vs_vwap"]
        pvv_str = f"{pvv:+.2f}%" if pvv is not None else "—"

        vr = r["vol_ratio"]
        vr_str = f"{vr:.1f}x" if vr is not None else "—"
        vr_clr = "#26d07c" if (vr and vr >= 2) else "#f0b429" if (vr and vr >= VOL_SURGE_RATIO) else "#8b949e"

        fo_badge = ('<span style="background:#002d6622;color:#00d4ff;border:1px solid #00d4ff44;'
                    'border-radius:6px;padding:0 5px;font-size:9px;font-weight:700">F&O</span> '
                    if r["fo"] else "")

        rows.append(f'''<tr>
  <td style="color:#8b949e;text-align:center;font-size:11px">{i}</td>
  <td>
    {fo_badge}<b style="color:#e6edf3">{r['sym']}</b><br>
    <span style="color:#8b949e;font-size:10px">{r['name']}</span>
  </td>
  <td style="text-align:right;font-weight:700">₹{r['last_price']:,.2f}</td>
  <td style="text-align:center">
    {signal_badge("PDH ✓", r["pdh_breakout"], "#00ff88")}
    {signal_badge("VWAP ✓", r["vwap_breakout"], "#00d4ff")}
    {signal_badge("ORH ✓", r["orh_breakout"], "#f0b429")}
  </td>
  <td style="text-align:right;color:{pct_clr(pvp)};font-weight:700">{pvp_str}</td>
  <td style="text-align:right;color:#8b949e;font-size:11px">₹{r['pdh']:,.2f}</td>
  <td style="text-align:right;color:{pct_clr(pvv)}">{pvv_str}</td>
  <td style="text-align:right;color:#8b949e;font-size:11px">{f"₹{r['vwap']:,.2f}" if r['vwap'] else "—"}</td>
  <td style="text-align:right;color:{rsi_clr(r['rsi_d'])};font-weight:700">{r['rsi_d'] if r['rsi_d'] else "—"}</td>
  <td style="text-align:right;color:{vr_clr}">{vr_str}</td>
  <td>{score_bar(r['score'])}</td>
</tr>''')

    rows_html = "\n".join(rows)
    no_data_msg = "" if results else '<tr><td colspan="11" style="text-align:center;padding:40px;color:#8b949e">No breakout candidates found yet — market may still be in opening range</td></tr>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Intraday Breakout — {run_ts}</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--border:#21262d;--text:#e6edf3;--sub:#8b949e;
      --cyan:#00d4ff;--green:#26d07c;--gold:#f0b429;--red:#ff6b6b}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}}
.header{{background:#010409;border-bottom:2px solid var(--border);padding:18px 24px 14px}}
.header h1{{font-size:21px;font-weight:700;color:var(--cyan);letter-spacing:1px}}
.subtitle{{color:var(--sub);font-size:11.5px;margin-top:4px}}
.nav-links{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}
.nav-link{{display:inline-flex;align-items:center;gap:5px;background:var(--card);
           border:1px solid var(--border);color:var(--text);border-radius:20px;
           padding:4px 14px;font-size:11.5px;font-weight:600;text-decoration:none;transition:all .15s}}
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
.filter-btn:hover,.filter-btn.active{{border-color:var(--cyan);color:var(--cyan)}}
.tbl-wrap{{overflow-x:auto;padding:0 24px 40px}}
table{{width:100%;border-collapse:collapse;margin-top:12px}}
th{{background:#010409;color:var(--sub);font-size:11px;text-transform:uppercase;
    letter-spacing:.5px;padding:8px 10px;border-bottom:2px solid var(--border);
    text-align:right;cursor:pointer;white-space:nowrap;user-select:none}}
th:first-child,th:nth-child(2),th:nth-child(4){{text-align:left}}
th:hover{{color:var(--text)}}
td{{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr:hover td{{background:#161b22}}
.disclaimer{{padding:12px 24px;color:var(--sub);font-size:10.5px;border-top:1px solid var(--border);
             line-height:1.6;background:#010409}}
</style>
</head>
<body>

<div class="header">
  <h1>⚡ NSE Intraday Breakout Scanner</h1>
  <div class="subtitle">Generated: {run_ts} &nbsp;·&nbsp; {n_total} candidates from top {MAX_STOCKS} NSE stocks &nbsp;·&nbsp; PDH · VWAP · Opening Range breakouts</div>
  <div class="nav-links">
    <a class="nav-link active" href="intraday.html">⚡ Intraday</a>
    <a class="nav-link" href="index.html">📈 RSI MTF</a>
    <a class="nav-link" href="ath.html">🏆 ATH Breakout</a>
    <a class="nav-link" href="multibagger.html">💎 Multibagger</a>
    <a class="nav-link" href="rocket.html">🚀 Rocket</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="val" style="color:var(--green)">{n_total}</div><div class="lbl">Total Candidates</div></div>
  <div class="stat"><div class="val" style="color:#00ff88">{n_pdh}</div><div class="lbl">PDH Breakout</div></div>
  <div class="stat"><div class="val" style="color:var(--cyan)">{n_vwap}</div><div class="lbl">VWAP Breakout</div></div>
  <div class="stat"><div class="val" style="color:var(--gold)">{n_orh}</div><div class="lbl">OR High Break</div></div>
  <div class="stat"><div class="val" style="color:var(--red)">{n_strong}</div><div class="lbl">Score ≥ 70</div></div>
</div>

<div class="filter-bar">
  <input type="text" id="search" placeholder="Search symbol or company…" oninput="applyFilter()">
  <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
  <button class="filter-btn" onclick="setFilter('pdh',this)">PDH Only</button>
  <button class="filter-btn" onclick="setFilter('vwap',this)">VWAP Only</button>
  <button class="filter-btn" onclick="setFilter('strong',this)">Score ≥ 70</button>
  <button class="filter-btn" onclick="setFilter('fo',this)">F&O Only</button>
</div>

<div class="tbl-wrap">
<table id="mainTable">
<thead>
<tr>
  <th onclick="thSort(0)">#</th>
  <th onclick="thSort(1)" style="text-align:left">Ticker / Company</th>
  <th onclick="thSort(2)">Price</th>
  <th style="text-align:left">Signals</th>
  <th onclick="thSort(4)" title="% above/below Previous Day High">% vs PDH</th>
  <th onclick="thSort(5)">PDH ₹</th>
  <th onclick="thSort(6)" title="% above/below intraday VWAP">% vs VWAP</th>
  <th onclick="thSort(7)">VWAP ₹</th>
  <th onclick="thSort(8)">RSI (D)</th>
  <th onclick="thSort(9)">Vol Ratio</th>
  <th onclick="thSort(10)">Score</th>
</tr>
</thead>
<tbody id="tableBody">
{rows_html}{no_data_msg}
</tbody>
</table>
</div>

<div class="disclaimer">
  ⚠️ For educational and research purposes only. Not SEBI-registered investment advice.
  Intraday signals are based on data at scan time — prices change continuously.
  PDH = Previous Day High &nbsp;·&nbsp; VWAP = Volume Weighted Average Price &nbsp;·&nbsp; ORH = Opening Range High (first 15 min)
</div>

<script>
let activeFilter = 'all';
const rows = Array.from(document.querySelectorAll('#tableBody tr'));

function setFilter(f, btn) {{
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  applyFilter();
}}

function applyFilter() {{
  const q = document.getElementById('search').value.toLowerCase();
  rows.forEach(tr => {{
    const txt   = tr.textContent.toLowerCase();
    const cells = tr.querySelectorAll('td');
    const signals = cells[3] ? cells[3].textContent : '';
    let show = txt.includes(q);
    if (show && activeFilter === 'pdh')    show = signals.includes('PDH ✓');
    if (show && activeFilter === 'vwap')   show = signals.includes('VWAP ✓');
    if (show && activeFilter === 'strong') show = parseInt(cells[10]?.textContent) >= 70;
    if (show && activeFilter === 'fo')     show = tr.textContent.includes('F&O');
    tr.style.display = show ? '' : 'none';
  }});
}}

function thSort(col) {{
  const tbody = document.getElementById('tableBody');
  const allRows = Array.from(tbody.querySelectorAll('tr'));
  let asc = tbody.dataset.sortCol == col && tbody.dataset.sortDir == 'asc' ? false : true;
  tbody.dataset.sortCol = col;
  tbody.dataset.sortDir = asc ? 'asc' : 'desc';
  allRows.sort((a, b) => {{
    const av = a.querySelectorAll('td')[col]?.textContent.replace(/[₹,+%x]/g,'').trim() || '';
    const bv = b.querySelectorAll('td')[col]?.textContent.replace(/[₹,+%x]/g,'').trim() || '';
    const an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  allRows.forEach(r => tbody.appendChild(r));
}}
</script>
</body>
</html>'''


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"=== NSE Intraday Breakout Report  {datetime.now().strftime('%d %b %Y %H:%M')} ===")
    universe = load_universe()
    print(f"Universe: {len(universe)} stocks")

    results = run_scan(universe)
    print(f"\nBreakout candidates: {len(results)}")

    html = build_html(results)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"Written: {OUTPUT_FILE}  ({size_kb:.1f} KB)")
    print("Done.")
