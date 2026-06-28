"""
NSE WEEKLY MARKET DIGEST
========================
Runs every Saturday at 5 AM IST after all daily reports are generated.
Produces weekly_digest.html — a combined email highlighting:
  - Top stocks at / near All-Time Highs
  - Top RSI Momentum picks (Strong Buy)
  - Top Volume Breakouts
  - Market pulse summary (breadth across F&O universe)

No talib · Pure pandas/numpy
Output: weekly_digest.html
"""

import os
import math
import warnings
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
LOCAL_NSE_CSV  = "india/NSE/NSECash/EQUITY_L.csv"
LOCAL_FO_CSV   = "india/NSE/nse_fo_list.csv"
OUTPUT_FILE    = "weekly_digest.html"
MAX_STOCKS     = 400       # scan top 400 (F&O first)
MAX_WORKERS    = 14
MIN_PRICE      = 20
MIN_AVG_VOL    = 50_000
TOP_N          = 12        # picks per section
PAGES_BASE     = "https://dipenshah2006.github.io/DailyMomentumStockBreakout"


# ── Helpers ───────────────────────────────────────────────────────────────────
def calc_rsi(series: pd.Series, period: int = 14) -> float:
    s = series.dropna()
    if len(s) < period + 1:
        return float("nan")
    delta = s.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return float((100 - 100 / (1 + rs)).iloc[-1])


def week_range() -> str:
    today = date.today()
    mon   = today - timedelta(days=today.weekday())
    fri   = mon + timedelta(days=4)
    return f"{mon.strftime('%d %b')} – {fri.strftime('%d %b %Y')}"


# ── Universe ──────────────────────────────────────────────────────────────────
def load_universe() -> list[dict]:
    fo_set = set()
    if os.path.exists(LOCAL_FO_CSV):
        try:
            df  = pd.read_csv(LOCAL_FO_CSV)
            col = next((c for c in df.columns if "SYMBOL" in c.upper()), df.columns[0])
            fo_set = set(df[col].str.strip().str.upper())
        except Exception:
            pass

    stocks = []
    if os.path.exists(LOCAL_NSE_CSV):
        try:
            df       = pd.read_csv(LOCAL_NSE_CSV)
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

    stocks.sort(key=lambda x: (0 if x["fo"] else 1, x["sym"]))
    return stocks[:MAX_STOCKS]


# ── Per-stock analysis ────────────────────────────────────────────────────────
def analyse(sym: str, name: str, fo: bool) -> dict | None:
    yf_sym = sym + ".NS"
    try:
        df = yf.download(
            yf_sym, period="1y", interval="1d",
            auto_adjust=True, progress=False
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open","High","Low","Close","Volume"]].dropna()

        if df.empty or len(df) < 30:
            return None

        close     = df["Close"]
        vol       = df["Volume"]
        last      = float(close.iloc[-1])
        avg_vol   = float(vol.tail(20).mean())

        if last < MIN_PRICE or avg_vol < MIN_AVG_VOL:
            return None

        # ATH (full 1-year window)
        ath_price = float(df["High"].max())
        ath_pct   = round((last / ath_price - 1) * 100, 1)
        is_ath    = last >= ath_price * 0.99

        # 52-week high/low
        high52    = float(df["High"].tail(252).max())
        low52     = float(df["Low"].tail(252).min())
        dist52h   = round((last / high52 - 1) * 100, 1)
        rise_52w  = round((last / low52 - 1) * 100, 1) if low52 > 0 else None

        # RSI daily
        rsi_d = calc_rsi(close, 14)

        # Weekly RSI
        dfw   = df.resample("W").agg({"Close": "last"}).dropna()
        rsi_w = calc_rsi(dfw["Close"], 14) if len(dfw) >= 20 else float("nan")

        # EMAs
        ema20  = float(close.ewm(span=20,  adjust=False).mean().iloc[-1])
        ema50  = float(close.ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1]) if len(df) >= 50 else ema50

        uptrend = last > ema20 > ema50 > ema200

        # Volume ratio (latest day vs 20-day avg)
        vol_ratio = round(float(vol.iloc[-1]) / avg_vol, 1) if avg_vol > 0 else None

        # Weekly return
        week_ret = round((last / float(close.iloc[-6]) - 1) * 100, 1) if len(close) >= 6 else None

        return {
            "sym":       sym,
            "name":      name,
            "fo":        fo,
            "last":      round(last, 2),
            "ath_pct":   ath_pct,
            "is_ath":    is_ath,
            "ath_price": round(ath_price, 2),
            "dist52h":   dist52h,
            "rise_52w":  rise_52w,
            "rsi_d":     round(rsi_d, 1) if not math.isnan(rsi_d) else None,
            "rsi_w":     round(rsi_w, 1) if not math.isnan(rsi_w) else None,
            "uptrend":   uptrend,
            "vol_ratio": vol_ratio,
            "week_ret":  week_ret,
        }
    except Exception:
        return None


def run_scan(universe: list[dict]) -> list[dict]:
    results, done = [], 0
    total = len(universe)
    print(f"Scanning {total} stocks for weekly digest…", flush=True)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(analyse, s["sym"], s["name"], s["fo"]): s for s in universe}
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass
            if done % 50 == 0 or done == total:
                print(f"  [{done}/{total}] {len(results)} with data", flush=True)
    return results


# ── HTML helpers ──────────────────────────────────────────────────────────────
def _inr(v):
    if v is None: return "—"
    if v >= 1000: return f"₹{v:,.0f}"
    return f"₹{v:.2f}"

def _pct(v, show_plus=True):
    if v is None: return "—"
    s = f"+{v:.1f}%" if v >= 0 and show_plus else f"{v:.1f}%"
    return s

def _rsi_clr(v):
    if v is None: return "#8b949e"
    if v >= 70:   return "#ff6b6b"
    if v >= 55:   return "#26d07c"
    if v >= 45:   return "#f0b429"
    return "#8b949e"

def _pct_clr(v):
    if v is None: return "#8b949e"
    return "#26d07c" if v >= 0 else "#ff6b6b"

def _week_badge(v):
    if v is None: return "—"
    clr = "#26d07c" if v >= 0 else "#ff6b6b"
    bg  = "#0d2615" if v >= 0 else "#2a0000"
    sym = "▲" if v >= 0 else "▼"
    return (f'<span style="background:{bg};color:{clr};border:1px solid {clr}44;'
            f'border-radius:8px;padding:1px 8px;font-size:11px;font-weight:700">'
            f'{sym} {abs(v):.1f}%</span>')

def stock_row(r: dict, rank: int, show_ath=False) -> str:
    fo_tag = ('<span style="color:#00d4ff;font-size:9px;font-weight:700;'
              'border:1px solid #00d4ff44;border-radius:5px;padding:0 4px">F&O</span> '
              if r["fo"] else "")
    ath_tag = ""
    if show_ath:
        if r["is_ath"]:
            ath_tag = ('<span style="background:#002d1a;color:#00ff88;border:1px solid #00ff8844;'
                       'border-radius:8px;padding:1px 7px;font-size:10px;font-weight:700">🏆 ATH</span>')
        else:
            clr = "#f0b429" if r["ath_pct"] >= -5 else "#fb923c" if r["ath_pct"] >= -10 else "#ff6b6b"
            ath_tag = (f'<span style="color:{clr};font-size:11px;font-weight:700">'
                       f'{r["ath_pct"]:+.1f}%</span>')

    return f'''<tr>
  <td style="color:#8b949e;text-align:center;font-size:11px">{rank}</td>
  <td>
    {fo_tag}<b style="color:#e6edf3">{r['sym']}</b>&nbsp;
    {'<span style="font-size:9px;color:#8b949e">'+r['name']+'</span>' if r['name'] != r['sym'] else ''}
  </td>
  <td style="text-align:right;font-weight:700">{_inr(r['last'])}</td>
  <td style="text-align:center">{_week_badge(r['week_ret'])}</td>
  <td style="text-align:right;color:{_rsi_clr(r['rsi_d'])};font-weight:700">{r['rsi_d'] if r['rsi_d'] else "—"}</td>
  <td style="text-align:right;color:{_rsi_clr(r['rsi_w'])}">{r['rsi_w'] if r['rsi_w'] else "—"}</td>
  <td style="text-align:center">{ath_tag if show_ath else _inr(r['ath_price'])}</td>
</tr>'''


def section_table(title: str, emoji: str, rows_html: str, col7_label: str, href: str, color: str) -> str:
    return f'''
<div style="margin-bottom:32px">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
    <h2 style="font-size:16px;font-weight:700;color:{color};margin:0">{emoji} {title}</h2>
    <a href="{href}" style="color:{color};font-size:11px;text-decoration:none;
       border:1px solid {color}44;border-radius:12px;padding:3px 12px">View Full Report →</a>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:12px">
  <thead>
  <tr style="background:#010409">
    <th style="color:#8b949e;padding:6px 8px;text-align:center;font-size:10px;border-bottom:1px solid #21262d">#</th>
    <th style="color:#8b949e;padding:6px 8px;text-align:left;font-size:10px;border-bottom:1px solid #21262d">Symbol</th>
    <th style="color:#8b949e;padding:6px 8px;text-align:right;font-size:10px;border-bottom:1px solid #21262d">Price</th>
    <th style="color:#8b949e;padding:6px 8px;text-align:center;font-size:10px;border-bottom:1px solid #21262d">Week</th>
    <th style="color:#8b949e;padding:6px 8px;text-align:right;font-size:10px;border-bottom:1px solid #21262d">RSI D</th>
    <th style="color:#8b949e;padding:6px 8px;text-align:right;font-size:10px;border-bottom:1px solid #21262d">RSI W</th>
    <th style="color:#8b949e;padding:6px 8px;text-align:center;font-size:10px;border-bottom:1px solid #21262d">{col7_label}</th>
  </tr>
  </thead>
  <tbody>
  {rows_html}
  </tbody>
  </table>
</div>'''


# ── HTML builder ──────────────────────────────────────────────────────────────
def build_html(results: list[dict]) -> str:
    run_ts   = datetime.now().strftime("%d %b %Y  %H:%M IST")
    wk_range = week_range()
    n_total  = len(results)

    # Market breadth
    n_up    = sum(1 for r in results if r["uptrend"])
    n_ath   = sum(1 for r in results if r["is_ath"])
    n_near  = sum(1 for r in results if r["ath_pct"] >= -5 and not r["is_ath"])
    n_sb    = sum(1 for r in results if r["rsi_d"] and r["rsi_d"] >= 60 and r["uptrend"])
    avg_rsi = round(sum(r["rsi_d"] for r in results if r["rsi_d"]) /
                    max(1, sum(1 for r in results if r["rsi_d"])), 1)
    breadth_pct = round(n_up / max(1, n_total) * 100)

    # 1. ATH section — AT ATH first, then closest
    ath_picks = sorted(
        [r for r in results if r["ath_pct"] >= -10],
        key=lambda x: (-1 if x["is_ath"] else 0, x["ath_pct"]),
        reverse=True
    )[:TOP_N]

    # 2. RSI Momentum — RSI 55-70 (sweet spot) + uptrend
    rsi_picks = sorted(
        [r for r in results if r["rsi_d"] and 55 <= r["rsi_d"] <= 72 and r["uptrend"]],
        key=lambda x: x["rsi_d"],
        reverse=True
    )[:TOP_N]

    # 3. Weekly top gainers
    gainer_picks = sorted(
        [r for r in results if r["week_ret"] is not None and r["week_ret"] > 0 and r["uptrend"]],
        key=lambda x: x["week_ret"],
        reverse=True
    )[:TOP_N]

    # 4. Volume surge picks
    vol_picks = sorted(
        [r for r in results if r["vol_ratio"] and r["vol_ratio"] >= 1.5 and r["uptrend"]],
        key=lambda x: x["vol_ratio"],
        reverse=True
    )[:TOP_N]

    ath_rows     = "\n".join(stock_row(r, i+1, show_ath=True) for i, r in enumerate(ath_picks))
    rsi_rows     = "\n".join(stock_row(r, i+1, show_ath=False) for i, r in enumerate(rsi_picks))
    gainer_rows  = "\n".join(stock_row(r, i+1, show_ath=False) for i, r in enumerate(gainer_picks))
    vol_rows     = "\n".join(stock_row(r, i+1, show_ath=False) for i, r in enumerate(vol_picks))

    td_style = "padding:9px 8px;border-bottom:1px solid #21262d;vertical-align:middle"
    # inject td style into rows
    for var in ("ath_rows", "rsi_rows", "gainer_rows", "vol_rows"):
        exec(f'{var} = {var}.replace("<td", "<td style=\\"{td_style}\\"", )', globals())

    ath_section    = section_table("ATH Breakout — Top Picks",     "🏆", ath_rows,    "vs ATH",    f"{PAGES_BASE}/ath.html",        "#00ff88")
    rsi_section    = section_table("RSI Momentum — Strong Setups", "📈", rsi_rows,    "ATH ₹",     f"{PAGES_BASE}/",                "#00d4ff")
    gainer_section = section_table("Weekly Top Gainers",           "🚀", gainer_rows, "ATH ₹",     f"{PAGES_BASE}/rocket.html",     "#f0b429")
    vol_section    = section_table("Volume Surge Picks",           "💥", vol_rows,    "Vol Ratio",  f"{PAGES_BASE}/multibagger.html","#fb923c")

    # Pulse bar
    bar_w = min(100, breadth_pct)
    bar_clr = "#26d07c" if breadth_pct >= 60 else "#f0b429" if breadth_pct >= 40 else "#ff6b6b"
    mood = "🟢 Bullish" if breadth_pct >= 60 else "🟡 Neutral" if breadth_pct >= 40 else "🔴 Bearish"

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Weekly Digest — {wk_range}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;font-size:13px}}
a{{color:inherit;text-decoration:none}}
</style>
</head>
<body>

<!-- ═══ HEADER ═══ -->
<div style="background:linear-gradient(135deg,#010409 60%,#0d2615);
            border-bottom:2px solid #21262d;padding:24px 28px 20px">
  <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">
    NSE Weekly Market Digest
  </div>
  <h1 style="font-size:24px;font-weight:700;color:#00d4ff;letter-spacing:.5px">
    📊 Week of {wk_range}
  </h1>
  <div style="color:#8b949e;font-size:11.5px;margin-top:5px">
    Generated: {run_ts} &nbsp;·&nbsp; {n_total} stocks scanned (F&O-first universe)
  </div>

  <!-- nav pills -->
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
    <a href="{PAGES_BASE}/"             style="background:#161b22;border:1px solid #21262d;border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;color:#e6edf3">📈 RSI MTF</a>
    <a href="{PAGES_BASE}/ath.html"     style="background:#161b22;border:1px solid #21262d;border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;color:#e6edf3">🏆 ATH Breakout</a>
    <a href="{PAGES_BASE}/multibagger.html" style="background:#161b22;border:1px solid #21262d;border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;color:#e6edf3">💎 Multibagger</a>
    <a href="{PAGES_BASE}/rocket.html"  style="background:#161b22;border:1px solid #21262d;border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;color:#e6edf3">🚀 Rocket</a>
    <a href="{PAGES_BASE}/intraday.html" style="background:#161b22;border:1px solid #21262d;border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;color:#e6edf3">⚡ Intraday</a>
  </div>
</div>

<!-- ═══ MARKET PULSE ═══ -->
<div style="background:#161b22;border-bottom:1px solid #21262d;padding:16px 28px">
  <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px">
    Market Pulse — {mood}
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
    <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:10px 18px;min-width:90px">
      <div style="font-size:22px;font-weight:700;color:#26d07c">{n_up}</div>
      <div style="font-size:10px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.4px">Uptrend</div>
    </div>
    <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:10px 18px;min-width:90px">
      <div style="font-size:22px;font-weight:700;color:#00ff88">{n_ath}</div>
      <div style="font-size:10px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.4px">At ATH</div>
    </div>
    <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:10px 18px;min-width:90px">
      <div style="font-size:22px;font-weight:700;color:#f0b429">{n_near}</div>
      <div style="font-size:10px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.4px">Within 5% ATH</div>
    </div>
    <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:10px 18px;min-width:90px">
      <div style="font-size:22px;font-weight:700;color:#00d4ff">{n_sb}</div>
      <div style="font-size:10px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.4px">RSI &gt; 60</div>
    </div>
    <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:10px 18px;min-width:90px">
      <div style="font-size:22px;font-weight:700;color:#e6edf3">{avg_rsi}</div>
      <div style="font-size:10px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.4px">Avg RSI</div>
    </div>
    <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:10px 18px;min-width:90px">
      <div style="font-size:22px;font-weight:700;color:{bar_clr}">{breadth_pct}%</div>
      <div style="font-size:10px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.4px">Breadth</div>
    </div>
  </div>
  <!-- breadth bar -->
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:10px;color:#8b949e;width:70px">Uptrend</span>
    <div style="flex:1;height:8px;background:#21262d;border-radius:4px;max-width:400px">
      <div style="width:{bar_w}%;height:100%;background:{bar_clr};border-radius:4px;transition:width .3s"></div>
    </div>
    <span style="font-size:10px;color:{bar_clr};font-weight:700">{breadth_pct}% of {n_total}</span>
  </div>
</div>

<!-- ═══ SECTIONS ═══ -->
<div style="padding:24px 28px">
  {ath_section}
  {rsi_section}
  {gainer_section}
  {vol_section}
</div>

<!-- ═══ FOOTER ═══ -->
<div style="background:#010409;border-top:1px solid #21262d;padding:16px 28px;
            color:#8b949e;font-size:10.5px;line-height:1.7">
  <b style="color:#e6edf3">NSE Daily Momentum Breakout</b> &nbsp;·&nbsp;
  Auto-generated every Saturday 5:00 AM IST &nbsp;·&nbsp;
  Data source: Yahoo Finance (yfinance) — delayed, not real-time<br>
  ⚠️ For educational and research purposes only. Not SEBI-registered investment advice.
  Past performance is not a guarantee of future returns.<br>
  <a href="{PAGES_BASE}/" style="color:#00d4ff">View all live reports →</a>
</div>

</body>
</html>'''


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"=== NSE Weekly Digest  {datetime.now().strftime('%d %b %Y %H:%M')} ===")
    universe = load_universe()
    print(f"Universe: {len(universe)} stocks")

    results = run_scan(universe)
    print(f"\nStocks with data: {len(results)}")

    html = build_html(results)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Written: {OUTPUT_FILE}  ({os.path.getsize(OUTPUT_FILE)/1024:.1f} KB)")
    print("Done.")
