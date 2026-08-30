"""
monthly_macd_cci_breakout_screener.py
======================================
NSE Monthly Breakout Screener

Signal:
  BUY = Monthly MACD(12,26,9) bullish cross  AND  Monthly CCI(20) bullish cross SMA(20)

Trend classification from monthly CCI(20):
  ● Trend Beginning  — CCI just crossed above SMA20, CCI in -100 to +150
  ● Medium Bullish   — CCI > 0, above SMA20, MACD bullish
  ● Strong Bullish   — CCI > 200, above SMA20, MACD bullish

Report columns include:
  - Trend Start date, Trend Months (how long in trend)
  - Gain % since trend start
  - Stop Loss (trend-beginning bar low) + Risk % to stop
  - Fib 127.2% / 161.8% / 261.8% price targets + % upside to each
  - Reward:Risk ratio (upside / risk)

Usage:
    python monthly_macd_cci_breakout_screener.py --universe Equity_L.csv
    python monthly_macd_cci_breakout_screener.py --universe Equity_L.csv --limit 50
"""
from __future__ import annotations
import argparse
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

import data_fetch
import indicators as ind
import monthly_signals as msig
from monthly_breakout_chart import build_monthly_chart
import pattern_engine as pe
from rsi_reversal_chart import build_rsi_reversal_chart
import stage_analysis as sta

BASE_DIR    = Path(__file__).resolve().parent
CACHE_DIR   = BASE_DIR / "cache_parquet"
CHARTS_DIR  = BASE_DIR / "monthly_charts"
OUTPUT_HTML = BASE_DIR / "monthly_breakout_report.html"

MIN_BARS = 20


def load_universe(csv_path: Path) -> list[str]:
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]
    return df["SYMBOL"].dropna().astype(str).str.strip().tolist()


def process_symbol(symbol: str) -> dict | None:
    try:
        daily = data_fetch.get_history(symbol, CACHE_DIR)
        if daily is None or len(daily) < MIN_BARS:
            return None

        mo, extras = msig.build_monthly_signal_table(daily)
        if len(mo) < 15 or not extras:
            return None

        chart_path = CHARTS_DIR / f"{symbol}.html"
        build_monthly_chart(symbol, mo, extras, chart_path, daily=daily)

        snap = msig.latest_monthly_snapshot(symbol, daily, mo, extras)
        snap = msig.enrich_snapshot_with_stage_fib(snap, daily, mo)
        snap["Chart"] = f"monthly_charts/{symbol}.html"

        # Pattern detection across all timeframes
        wk   = ind.weekly(daily)
        pats = pe.scan_all_patterns(daily, wk, mo)
        psumm = pe.patterns_summary(pats)
        snap["Pattern_Count"]   = psumm["count"]
        snap["Pattern_Top"]     = psumm["strongest"]
        snap["Pattern_Bullish"] = psumm["bullish"]
        snap["Pattern_Bearish"] = psumm["bearish"]
        snap["Patterns_JSON"]   = psumm["all"]   # for chart overlay

        # RSI Reversal chart (only if monthly RSI > 75)
        mo_rsi = ind.rsi(mo["Close"], 14)
        rsi_now = float(mo_rsi.iloc[-1]) if len(mo_rsi) and pd.notna(mo_rsi.iloc[-1]) else 0
        snap["Monthly_RSI14"] = round(rsi_now, 2) if rsi_now else None
        if rsi_now and rsi_now >= 70:   # generate for >= 70 so chart always available
            rr_path = CHARTS_DIR / f"{symbol}_rsi_reversal.html"
            try:
                build_rsi_reversal_chart(symbol, mo, mo_rsi, rr_path)
                snap["RSI_Reversal_Chart"] = f"monthly_charts/{symbol}_rsi_reversal.html"
            except Exception:
                snap["RSI_Reversal_Chart"] = None
        else:
            snap["RSI_Reversal_Chart"] = None

        return snap

    except Exception as e:
        print(f"[WARN] {symbol}: {e}")
        traceback.print_exc()
        return None


# ─── HTML Report ──────────────────────────────────────────────────────────────

TREND_ORDER = {
    msig.STRONG_BULLISH:  0,
    msig.MEDIUM_BULLISH:  1,
    msig.TREND_BEGINNING: 2,
    msig.NO_TREND:        3,
}

FILTER_DEFS = [
    ("stage_2a",        "🟢 Stage 2A"),
    ("stage_2b",        "🟩 Stage 2B"),
    ("stage_1b",        "🔵 Stage 1B"),
    ("Super_Bullish",   "⭐ Super Bullish"),
    ("Rocket_Buy",      "🚀 Rocket Buy"),
    ("Buy_Signal",      "🎯 Both Crossed (this bar)"),
    ("Near_Buy",        "📈 Near Buy (last 3 bars)"),
    ("MACD_Bull_Cross", "MACD Bull Cross"),
    ("MACD_Zero_Cross", "MACD Zero Line Cross"),
    ("CCI34_Bull_Cross","CCI34 Bull Cross SMA14"),
    ("CCI20_Bull_Cross","CCI20 Bull Cross"),
    ("trend_strong",    "🟧 Strong Bullish"),
    ("trend_medium",    "🟩 Medium Bullish"),
    ("trend_begin",     "🟦 Trend Beginning"),
]

TOOLTIPS = {
    "Symbol":        "NSE ticker",
    "Close":         "Latest daily closing price (₹)",
    "Trend_State":   "CCI-based classification: Trend Beginning / Medium Bullish / Strong Bullish",
    "Trend_Start":   "Date of most recent MACD or CCI bullish cross — when current trend began",
    "Trend_Months":  "Months elapsed since trend start",
    "Gain_Pct":      "% price gain from trend-start close to today's close",
    "Stop_Loss":     "Low of the trend-start bar — use as hard stop loss (₹)",
    "Risk_Pct":      "% current price is above stop loss. Smaller = tighter stop.",
    "Fib_127":       "127.2% Fibonacci extension target (₹)",
    "Upside_127_Pct":"% upside from current close to 127.2% Fib target",
    "Fib_162":       "161.8% Fibonacci extension target — primary target (₹)",
    "Upside_162_Pct":"% upside from current close to 161.8% Fib target",
    "Fib_262":       "261.8% Fibonacci extension — stretch target (₹)",
    "Upside_262_Pct":"% upside to 261.8% Fib target",
    "RR_127":        "Reward:Risk — upside to 127.2% Fib ÷ risk to stop loss",
    "RR_162":        "Reward:Risk — upside to 161.8% Fib ÷ risk to stop loss",
    "Monthly_CCI20": "CCI(20) on monthly bars. >100=bullish, >200=strong",
    "Monthly_MACD_Hist": "MACD(12,26,9) histogram on monthly bars",
    "Monthly_RSI14": "RSI(14) on monthly bars",
    "Super_Bullish": "⭐ SUPER BULLISH: CCI(200) > 100 AND crossed above SMA(20). Long-period momentum fully confirmed — strongest monthly trend signal.",
    "Rocket_Buy":    "🚀 HIGH CONVICTION: CCI(34) bull-crosses SMA(14) with CCI ≥ -10 AND MACD(12,26,9) crossed above zero line (or near zero within 3 bars). Strong trend-launch signal.",
    "Buy_Signal":    "MACD(12,26,9) AND CCI(20) both crossed bullish — same bar",
    "Near_Buy":      "Either cross within last 3 monthly bars, both currently bullish",
    "MACD_Zero_Cross": "MACD(12,26,9) line crossed above the zero line this bar",
    "CCI34_Bull_Cross": "CCI(34) crossed above its SMA(14) this bar",
    "Monthly_CCI34": "CCI(34) on monthly bars — used in Rocket Buy signal",
    "Monthly_CCI200": "CCI(200) on monthly bars. >100 = Super Bullish zone",
    "W52_High":      "52-week high price (₹)",
    "W52_High_Pct":  "% vs 52-week high. Negative = below high, 0 = at high",
    "W52_Low":       "52-week low price (₹)",
    "W52_Low_Pct":   "% above 52-week low. Higher = further recovered from low",
    "Pattern_Count":  "Total chart patterns detected across daily, weekly, monthly timeframes",
    "Pattern_Top":    "Strongest pattern detected — format: Name (D/W/M) [Strong/Medium/Weak]",
    "Pattern_Bullish":"Number of bullish patterns detected",
    "Pattern_Bearish":"Number of bearish patterns detected",
    "Stage":          "Weinstein Stage: 1A/1B=Basing, 2A/2B/2C=Advancing, 3A/3B=Topping, 4A/4B=Declining. Click Stage header to sort.",
    "Fib_Ret_382":    "38.2% Fibonacci retracement from cycle P1→P2 (auto-detected)",
    "Fib_Ret_618":    "61.8% Golden ratio retracement — major support/resistance",
    "Fib_Ext_100":    "100% Fibonacci extension from P3 (1:1 move target)",
    "Fib_Ext_100_Pct":"% upside to 100% Fib extension target",
    "Fib_Ext_162":    "161.8% Fib extension — major cycle reversal target",
    "Fib_Ext_162_Pct":"% upside to 161.8% Fib extension",
    "Fib_Next_TZ":    "Next Fibonacci time zone date (projected from cycle low)",
    "Fib_Next_TZ_Mo": "Months until next Fib time zone",
    "MA30W":          "30-week SMA — the core Weinstein indicator",
    "Pct_Above_MA30": "% price is above/below 30-week MA",
    "Weekly_RSI":     "RSI(14) on weekly bars",
    "RSI_Reversal_Chart":"Monthly RSI reversal analysis — price levels where RSI would drop to 70/60/50, scenario projections, historical episodes",
}

COLUMNS = [
    "Symbol", "Close",
    "Stage",
    "Super_Bullish", "Rocket_Buy",
    "Monthly_CCI200",
    "W52_High", "W52_High_Pct",
    "W52_Low",  "W52_Low_Pct",
    "Trend_State", "Trend_Start", "Trend_Months", "Gain_Pct",
    "Stop_Loss", "Risk_Pct",
    "Fib_Ret_382", "Fib_Ret_618",
    "Fib_Ext_100", "Fib_Ext_100_Pct",
    "Fib_Ext_162", "Fib_Ext_162_Pct",
    "Fib_Next_TZ", "Fib_Next_TZ_Mo",
    "MA30W", "Pct_Above_MA30", "Weekly_RSI",
    "Monthly_CCI34", "Monthly_CCI20", "Monthly_MACD_Hist", "Monthly_RSI14",
    "Buy_Signal", "Near_Buy",
    "Pattern_Count", "Pattern_Top", "Pattern_Bullish", "Pattern_Bearish",
    "RSI_Reversal_Chart",
    "Chart",
]

COL_LABELS = {
    "Stage":          "Stage",
    "Super_Bullish":  "⭐ Super Bullish",
    "Rocket_Buy":     "🚀 Rocket Buy",
    "Monthly_CCI200": "CCI200",
    "Gain_Pct":       "Gain%",
    "Risk_Pct":       "Risk%",
    "Upside_127_Pct": "↑127%",
    "Upside_162_Pct": "↑162%",
    "Upside_262_Pct": "↑262%",
    "Monthly_CCI34":  "CCI34",
    "Monthly_CCI20":  "CCI20",
    "Monthly_MACD_Hist": "MACD Hist",
    "Monthly_RSI14":  "RSI14",
    "Trend_Start":    "Trend Since",
    "Trend_Months":   "Months",
    "Stop_Loss":      "Stop ₹",
    "Fib_127":        "T1 ₹(127%)",
    "Fib_162":        "T2 ₹(162%)",
    "Fib_262":        "T3 ₹(262%)",
    "W52_High":       "52W High ₹",
    "W52_High_Pct":   "vs 52W High",
    "W52_Low":        "52W Low ₹",
    "W52_Low_Pct":    "vs 52W Low",
    "Pattern_Count":  "Patterns #",
    "Pattern_Top":    "Top Pattern",
    "Pattern_Bullish":"🟢 Bull Pat.",
    "Pattern_Bearish":"🔴 Bear Pat.",
    "RSI_Reversal_Chart":"RSI Reversal",
    "Fib_Ret_382":    "Fib 38.2%",
    "Fib_Ret_618":    "Fib 61.8%",
    "Fib_Ext_100":    "Ext 100% ₹",
    "Fib_Ext_100_Pct":"Ext 100% Δ",
    "Fib_Ext_162":    "Ext 161.8% ₹",
    "Fib_Ext_162_Pct":"Ext 161.8% Δ",
    "Fib_Next_TZ":    "Next TZ",
    "Fib_Next_TZ_Mo": "TZ In (Mo)",
    "MA30W":          "MA30W ₹",
    "Pct_Above_MA30": "vs MA30W",
    "Weekly_RSI":     "Wk RSI",
}


def build_html_report(rows: list[dict]):
    for r in rows:
        r["super_bullish"] = r.get("Super_Bullish", False)
        r["stage_1a"] = r.get("Stage") == sta.STAGE_1A
        r["stage_1b"] = r.get("Stage") == sta.STAGE_1B
        r["stage_2a"] = r.get("Stage") == sta.STAGE_2A
        r["stage_2b"] = r.get("Stage") == sta.STAGE_2B
        r["stage_2c"] = r.get("Stage") == sta.STAGE_2C
        r["stage_3a"] = r.get("Stage") == sta.STAGE_3A
        r["stage_3b"] = r.get("Stage") == sta.STAGE_3B
        r["stage_4a"] = r.get("Stage") == sta.STAGE_4A
        r["stage_4b"] = r.get("Stage") == sta.STAGE_4B
        r["trend_begin"]  = r.get("Trend_State") == msig.TREND_BEGINNING
        r["trend_medium"] = r.get("Trend_State") == msig.MEDIUM_BULLISH
        r["trend_strong"] = r.get("Trend_State") == msig.STRONG_BULLISH

    rows.sort(key=lambda r: (
        sta.stage_sort_key(r.get("Stage", sta.STAGE_1A)),
        0 if r.get("Super_Bullish") else 1,
        0 if r.get("Rocket_Buy") else 1,
        TREND_ORDER.get(r.get("Trend_State", msig.NO_TREND), 9),
        -(r.get("Monthly_CCI200") or r.get("Monthly_CCI34") or -9999)
    ))

    # Strip large nested dicts not needed in HTML (keep Patterns_JSON for modal)
    report_rows = []
    for r in rows:
        row = {k: v for k, v in r.items() if k != "Fib_Data_JSON"}
        report_rows.append(row)
    data_json      = json.dumps(report_rows, default=str)
    tooltips_json  = json.dumps(TOOLTIPS)
    cols_json      = json.dumps(COLUMNS)
    labels_json    = json.dumps(COL_LABELS)
    filter_buttons = "\n".join(
        f'<button class="filter-btn" data-key="{key}">{label}</button>'
        for key, label in FILTER_DEFS
    )

    total      = len(rows)
    stage2a_cnt= sum(1 for r in rows if r.get("Stage") == sta.STAGE_2A)
    stage2b_cnt= sum(1 for r in rows if r.get("Stage") == sta.STAGE_2B)
    stage1b_cnt= sum(1 for r in rows if r.get("Stage") == sta.STAGE_1B)
    super_cnt  = sum(1 for r in rows if r.get("Super_Bullish"))
    rocket_cnt = sum(1 for r in rows if r.get("Rocket_Buy"))
    buy_cnt    = sum(1 for r in rows if r.get("Buy_Signal"))
    near_cnt   = sum(1 for r in rows if r.get("Near_Buy"))
    beg_cnt    = sum(1 for r in rows if r.get("Trend_State") == msig.TREND_BEGINNING)
    med_cnt    = sum(1 for r in rows if r.get("Trend_State") == msig.MEDIUM_BULLISH)
    str_cnt    = sum(1 for r in rows if r.get("Trend_State") == msig.STRONG_BULLISH)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>NSE Monthly MACD + CCI Breakout Screener</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 14px 18px;
  font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
  background: #f4f6f9; color: #131722; font-size: 0.87rem;
}}
h2 {{ margin: 0 0 2px; font-size: 1.2rem; }}
.subtitle {{ color: #666; font-size: 0.78rem; margin-bottom: 12px; }}

/* Stats */
.stats {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px; }}
.sc {{
  background:#fff; border:1px solid #e0e0e0; border-radius:8px;
  padding:5px 14px; text-align:center;
  box-shadow:0 1px 3px rgba(0,0,0,0.05);
}}
.sc .v {{ font-size:1.35rem; font-weight:700; }}
.sc .l {{ font-size:0.7rem; color:#777; }}

/* Filters */
/* ── Filter panel ── */
.filter-panel {{
  background:#fff; border:1px solid #e0e0e0; border-radius:10px;
  padding:10px 14px; margin-bottom:12px;
  box-shadow:0 1px 4px rgba(0,0,0,0.05);
}}
.filter-row {{
  display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-bottom:6px;
}}
.filter-row:last-child {{ margin-bottom:0; }}
.filter-row-label {{
  font-size:0.72rem; font-weight:700; color:#888; text-transform:uppercase;
  letter-spacing:0.4px; min-width:72px;
}}
.fbtn {{
  background:#f5f7fa; color:#444; border:1px solid #dde0e8;
  border-radius:14px; padding:3px 11px; cursor:pointer; font-size:0.77rem;
  transition:all 0.12s; white-space:nowrap;
}}
.fbtn:hover  {{ background:#e8eaf6; border-color:#b0b8d0; }}
.fbtn.active {{ background:#1a73e8; border-color:#1a73e8; color:#fff; font-weight:600; }}
.fbtn.active.str-strong {{ background:#2e7d32; border-color:#2e7d32; }}
.fbtn.active.str-medium {{ background:#e65100; border-color:#e65100; }}
.fbtn.active.str-weak   {{ background:#78909c; border-color:#78909c; }}
.fbtn.active.tf-daily   {{ background:#1565c0; border-color:#1565c0; }}
.fbtn.active.tf-weekly  {{ background:#6a1b9a; border-color:#6a1b9a; }}
.fbtn.active.tf-monthly {{ background:#00695c; border-color:#00695c; }}
.filter-sep {{ width:1px; background:#e0e0e0; align-self:stretch; margin:0 2px; }}
#clearAllBtn {{
  background:#fff; color:#aaa; border:1px solid #e8e8e8;
  border-radius:14px; padding:3px 11px; cursor:pointer; font-size:0.77rem;
  margin-left:auto;
}}
#clearAllBtn:hover {{ background:#fce4ec; color:#c62828; border-color:#ef9a9a; }}
.toolbar-row {{
  display:flex; align-items:center; gap:8px; margin-bottom:10px;
}}
#searchBox {{
  padding:5px 11px; border:1px solid #c8cad0; border-radius:6px;
  font-size:0.82rem; width:180px;
}}
#resultCount {{ font-size:0.78rem; color:#999; }}

/* Table */
.tw {{
  background:#fff; border:1px solid #e0e0e0; border-radius:10px;
  overflow-x:auto; box-shadow:0 2px 6px rgba(0,0,0,0.06);
}}
table {{ border-collapse:collapse; width:100%; }}
thead th {{
  padding:7px 9px; background:#f5f7fa;
  border-bottom:2px solid #e0e0e0;
  cursor:pointer; user-select:none; white-space:nowrap;
  font-size:0.78rem; color:#444; font-weight:600; text-align:left;
}}
thead th:hover {{ background:#eaecf2; }}
tbody tr {{ border-bottom:1px solid #f0f0f0; }}
tbody tr:hover {{ background:#f7f9ff; }}
tbody td {{ padding:5px 9px; white-space:nowrap; font-size:0.81rem; }}

/* Value coloring */
.pos   {{ color:#2e7d32; font-weight:600; }}
.neg   {{ color:#c62828; font-weight:600; }}
.fib   {{ color:#6a1b9a; font-weight:600; }}
.stop  {{ color:#c62828; font-weight:600; }}
.risk  {{ color:#e65100; }}
.rr    {{ color:#1565c0; font-weight:600; }}
.mths  {{ color:#37474f; }}

/* Badges */
/* Stage badges */
.stage-badge {{
  padding: 2px 8px; border-radius: 10px; font-size: 0.76rem; font-weight: 700;
  white-space: nowrap; display: inline-block;
}}
.s1a {{ background:#eceff1; color:#546e7a; }}
.s1b {{ background:#e3f2fd; color:#1565c0; border:1px solid #90caf9; }}
.s2a {{ background:#e8f5e9; color:#1b5e20; border:1px solid #a5d6a7; font-weight:800; }}
.s2b {{ background:#c8e6c9; color:#2e7d32; border:1px solid #81c784; }}
.s2c {{ background:#fff3e0; color:#e65100; border:1px solid #ffcc80; }}
.s3a {{ background:#fbe9e7; color:#bf360c; border:1px solid #ffab91; }}
.s3b {{ background:#ffebee; color:#b71c1c; border:1px solid #ef9a9a; }}
.s4a {{ background:#f3e5f5; color:#4a148c; border:1px solid #ce93d8; }}
.s4b {{ background:#ede7f6; color:#6a1b9a; border:1px solid #b39ddb; }}
.super-badge {{
  background: linear-gradient(135deg, #f57f17, #ff8f00);
  color: #fff; padding: 3px 10px; border-radius: 10px;
  font-weight: 800; font-size: 0.78rem; letter-spacing: 0.3px;
  box-shadow: 0 2px 6px rgba(245,127,23,0.40);
  display: inline-block;
}}
.rocket-badge {{
  background: linear-gradient(135deg, #6a1b9a, #1565c0);
  color: #fff; padding: 3px 10px; border-radius: 10px;
  font-weight: 800; font-size: 0.78rem; letter-spacing: 0.3px;
  box-shadow: 0 2px 6px rgba(106,27,154,0.35);
  display: inline-block;
}}
.buy-badge  {{ background:#e8f5e9; color:#2e7d32; padding:2px 7px; border-radius:9px; font-weight:700; font-size:0.76rem; }}
.near-badge {{ background:#e3f2fd; color:#1565c0; padding:2px 7px; border-radius:9px; font-size:0.76rem; }}
.trend-badge {{ padding:2px 8px; border-radius:10px; font-size:0.76rem; font-weight:600; }}
.tb-begin  {{ background:#e3f2fd; color:#1565c0; }}
.tb-medium {{ background:#e8f5e9; color:#2e7d32; }}
.tb-strong {{ background:#fff3e0; color:#e65100; }}
.tb-none   {{ background:#f5f5f5; color:#888; }}

a.cl {{
  background:#1a73e8; color:#fff; text-decoration:none;
  padding:2px 9px; border-radius:4px; font-size:0.74rem; font-weight:600;
}}
a.cl:hover {{ background:#1557b0; }}

/* Gain pill */
.gain {{ background:#f1f8e9; color:#33691e; padding:1px 6px; border-radius:8px; font-weight:600; }}

/* Pattern modal */
#patModal {{
  display:none; position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
  align-items:center; justify-content:center;
}}
#patModal.open {{ display:flex; }}
#patBox {{
  background:#fff; border-radius:12px; padding:20px 24px;
  max-width:680px; width:95vw; max-height:80vh; overflow-y:auto;
  box-shadow:0 8px 32px rgba(0,0,0,0.18);
}}
#patBox h3 {{ margin:0 0 12px; font-size:1rem; }}
.pat-card {{
  border:1px solid #e0e0e0; border-radius:8px; padding:9px 12px; margin-bottom:8px;
  display:grid; grid-template-columns:1fr auto; gap:4px;
}}
.pat-card .pname {{ font-weight:700; font-size:0.84rem; }}
.pat-card .pdesc {{ font-size:0.77rem; color:#555; grid-column:1/-1; margin-top:2px; }}
.pat-card .ptf   {{ font-size:0.74rem; color:#888; }}
.str-S {{ border-left:3px solid #2e7d32; }}
.str-M {{ border-left:3px solid #e65100; }}
.str-W {{ border-left:3px solid #aaa; }}
.dir-bull {{ background:#f1f8e9; }}
.dir-bear {{ background:#fff5f5; }}
.dir-neu  {{ background:#f5f5f5; }}
#patClose {{ float:right; cursor:pointer; font-size:1.1rem; color:#888; border:none; background:none; }}
</style>
</head>
<body>

<h2>NSE Monthly MACD(12,26,9) + CCI(20) Breakout Screener</h2>
<div class="subtitle">
  Signal: Monthly MACD &amp; CCI(20) bullish crossover &nbsp;|&nbsp;
  Stop = trend-beginning bar Low &nbsp;|&nbsp;
  Targets = Fibonacci extensions from monthly swing
</div>

<div class="stats">
  <div class="sc"><div class="v">{total}</div><div class="l">Total</div></div>
  <div class="sc" style="border-color:#1b5e20;background:linear-gradient(135deg,#e8f5e9,#fff)"><div class="v" style="color:#1b5e20">{stage2a_cnt}</div><div class="l">🟢 Stage 2A</div></div>
  <div class="sc" style="border-color:#2e7d32;background:linear-gradient(135deg,#c8e6c9,#fff)"><div class="v" style="color:#2e7d32">{stage2b_cnt}</div><div class="l">🟩 Stage 2B</div></div>
  <div class="sc" style="border-color:#1565c0;background:linear-gradient(135deg,#e3f2fd,#fff)"><div class="v" style="color:#1565c0">{stage1b_cnt}</div><div class="l">🔵 Stage 1B</div></div>
  <div class="sc" style="border-color:#f57f17;background:linear-gradient(135deg,#fffde7,#fff)"><div class="v" style="color:#f57f17">{super_cnt}</div><div class="l">⭐ Super Bullish</div></div>
  <div class="sc" style="border-color:#7b1fa2;background:linear-gradient(135deg,#f3e5f5,#fff)"><div class="v" style="color:#6a1b9a">{rocket_cnt}</div><div class="l">🚀 Rocket Buy</div></div>
  <div class="sc"><div class="v" style="color:#2e7d32">{buy_cnt}</div><div class="l">Both Crossed</div></div>
  <div class="sc"><div class="v" style="color:#1565c0">{near_cnt}</div><div class="l">Near Buy</div></div>
  <div class="sc"><div class="v" style="color:#1565c0">{beg_cnt}</div><div class="l">Trend Beginning</div></div>
  <div class="sc"><div class="v" style="color:#2e7d32">{med_cnt}</div><div class="l">Medium Bullish</div></div>
  <div class="sc"><div class="v" style="color:#e65100">{str_cnt}</div><div class="l">Strong Bullish</div></div>
</div>

<div class="filter-panel">

  <!-- Row 1: Signal filters -->
  <div class="filter-row">
    <span class="filter-row-label">Signal</span>
    <button class="fbtn sig-btn" data-key="Super_Bullish">⭐ Super Bullish</button>
    <button class="fbtn sig-btn" data-key="Rocket_Buy">🚀 Rocket Buy</button>
    <button class="fbtn sig-btn" data-key="Buy_Signal">🎯 Both Crossed</button>
    <button class="fbtn sig-btn" data-key="Near_Buy">📈 Near Buy</button>
    <button class="fbtn sig-btn" data-key="MACD_Bull_Cross">MACD Cross</button>
    <button class="fbtn sig-btn" data-key="MACD_Zero_Cross">MACD Zero</button>
    <button class="fbtn sig-btn" data-key="CCI34_Bull_Cross">CCI34 Cross</button>
    <button class="fbtn sig-btn" data-key="CCI20_Bull_Cross">CCI20 Cross</button>
    <div class="filter-sep"></div>
    <button class="fbtn sig-btn" data-key="trend_strong">🟧 Strong Bullish</button>
    <button class="fbtn sig-btn" data-key="trend_medium">🟩 Medium Bullish</button>
    <button class="fbtn sig-btn" data-key="trend_begin">🟦 Trend Beginning</button>
  </div>

  <!-- Row 1b: Stage filters -->
  <div class="filter-row">
    <span class="filter-row-label">Stage</span>
    <button class="fbtn sig-btn" data-key="stage_1a">⬜ 1A</button>
    <button class="fbtn sig-btn" data-key="stage_1b">🔵 1B</button>
    <div class="filter-sep"></div>
    <button class="fbtn sig-btn" data-key="stage_2a">🟢 2A — Best Buy</button>
    <button class="fbtn sig-btn" data-key="stage_2b">🟩 2B</button>
    <button class="fbtn sig-btn" data-key="stage_2c">🟡 2C ⚠️</button>
    <div class="filter-sep"></div>
    <button class="fbtn sig-btn" data-key="stage_3a">🟠 3A</button>
    <button class="fbtn sig-btn" data-key="stage_3b">🔴 3B</button>
    <div class="filter-sep"></div>
    <button class="fbtn sig-btn" data-key="stage_4a">⬛ 4A</button>
    <button class="fbtn sig-btn" data-key="stage_4b">🟣 4B</button>
  </div>

  <!-- Row 2: Pattern name filter -->
  <div class="filter-row">
    <span class="filter-row-label">Pattern</span>
    <div id="patNameFilters" style="display:flex;gap:5px;flex-wrap:wrap;"></div>
  </div>

  <!-- Row 3: Timeframe + Strength -->
  <div class="filter-row">
    <span class="filter-row-label">Timeframe</span>
    <button class="fbtn tf-btn tf-daily"   data-tf="daily">Daily</button>
    <button class="fbtn tf-btn tf-weekly"  data-tf="weekly">Weekly</button>
    <button class="fbtn tf-btn tf-monthly" data-tf="monthly">Monthly</button>
    <div class="filter-sep"></div>
    <span class="filter-row-label" style="min-width:56px;">Strength</span>
    <button class="fbtn str-btn str-strong" data-str="Strong">Strong</button>
    <button class="fbtn str-btn str-medium" data-str="Medium">Medium</button>
    <button class="fbtn str-btn str-weak"   data-str="Weak">Weak</button>
    <div class="filter-sep"></div>
    <span class="filter-row-label" style="min-width:52px;">Direction</span>
    <button class="fbtn dir-btn" data-dir="Bullish">🟢 Bullish</button>
    <button class="fbtn dir-btn" data-dir="Bearish">🔴 Bearish</button>
    <button class="fbtn" id="clearAllBtn">✕ Clear All</button>
  </div>

</div>

<div class="toolbar-row">
  <span id="resultCount"></span>
  <input type="text" id="searchBox" placeholder="Search symbol…" style="margin-left:auto;">
</div>

<!-- Pattern detail modal -->
<div id="patModal">
  <div id="patBox">
    <button id="patClose" onclick="document.getElementById('patModal').classList.remove('open')">✕</button>
    <h3 id="patTitle">Patterns</h3>
    <div id="patCards"></div>
  </div>
</div>

<div class="tw">
<table id="mainTable">
<thead><tr id="headerRow"></tr></thead>
<tbody id="tableBody"></tbody>
</table>
</div>

<script>
const DATA     = {data_json};
const TIPS     = {tooltips_json};
const COLUMNS  = {cols_json};
const LABELS   = {labels_json};

const TREND_ORDER = {{"Strong Bullish":0,"Medium Bullish":1,"Trend Beginning":2,"No Trend":3}};
const TREND_CLS   = {{"Strong Bullish":"tb-strong","Medium Bullish":"tb-medium","Trend Beginning":"tb-begin","No Trend":"tb-none"}};
const STAGE_META = {{
  "Stage 1A":{{icon:"⬜",label:"Stage 1A — Early Base",    cls:"s1a"}},
  "Stage 1B":{{icon:"🔵",label:"Stage 1B — Late Base",     cls:"s1b"}},
  "Stage 2A":{{icon:"🟢",label:"Stage 2A — Early Advance ✅",cls:"s2a"}},
  "Stage 2B":{{icon:"🟩",label:"Stage 2B — Mid Advance",   cls:"s2b"}},
  "Stage 2C":{{icon:"🟡",label:"Stage 2C — Late Advance ⚠️",cls:"s2c"}},
  "Stage 3A":{{icon:"🟠",label:"Stage 3A — Early Top",     cls:"s3a"}},
  "Stage 3B":{{icon:"🔴",label:"Stage 3B — Distribution",  cls:"s3b"}},
  "Stage 4A":{{icon:"⬛",label:"Stage 4A — Early Decline",  cls:"s4a"}},
  "Stage 4B":{{icon:"🟣",label:"Stage 4B — Capitulation",  cls:"s4b"}},
}};
const STAGE_SORT = {{"Stage 1A":1,"Stage 1B":2,"Stage 2A":3,"Stage 2B":4,"Stage 2C":5,"Stage 3A":6,"Stage 3B":7,"Stage 4A":8,"Stage 4B":9}};

// ── Filter state ─────────────────────────────────────────────────────────────
let activeSignals  = new Set();   // boolean row keys (Super_Bullish, etc.)
let activePatterns = new Set();   // pattern name strings
let activeTFs      = new Set();   // "daily" | "weekly" | "monthly"
let activeStrs     = new Set();   // "Strong" | "Medium" | "Weak"
let activeDirs     = new Set();   // "Bullish" | "Bearish"
let sortKey = "Monthly_CCI20";
let sortAsc  = false;
let searchQ  = "";

function rowHasPattern(r, name, tfs, strs, dirs) {{
  var pats = r.Patterns_JSON || [];
  return pats.some(function(p) {{
    var nameOk = !name   || p.name === name;
    var tfOk   = !tfs.size || tfs.has(p.timeframe);
    var strOk  = !strs.size || strs.has(p.strength);
    var dirOk  = !dirs.size || dirs.has(p.direction);
    return nameOk && tfOk && strOk && dirOk;
  }});
}}

function passes(r) {{
  // Search box
  if (searchQ && !r.Symbol.toUpperCase().includes(searchQ)) return false;
  // Signal filters (AND — must pass all active)
  for (var k of activeSignals) {{ if (!r[k]) return false; }}
  // Pattern filters — any active pattern/tf/str/dir filter requires a matching pattern
  var hasPatFilter = activePatterns.size || activeTFs.size || activeStrs.size || activeDirs.size;
  if (hasPatFilter) {{
    var pats = r.Patterns_JSON || [];
    if (!pats.length) return false;
    // If pattern names active: stock must have at least one matching pattern
    if (activePatterns.size) {{
      var nameMatch = false;
      for (var pn of activePatterns) {{
        if (rowHasPattern(r, pn, activeTFs, activeStrs, activeDirs)) {{ nameMatch=true; break; }}
      }}
      if (!nameMatch) return false;
    }} else {{
      // No name filter, but TF/Str/Dir filter active
      var anyMatch = pats.some(function(p) {{
        var tfOk  = !activeTFs.size  || activeTFs.has(p.timeframe);
        var strOk = !activeStrs.size || activeStrs.has(p.strength);
        var dirOk = !activeDirs.size || activeDirs.has(p.direction);
        return tfOk && strOk && dirOk;
      }});
      if (!anyMatch) return false;
    }}
  }}
  return true;
}}

function colLabel(col) {{
  return LABELS[col] || col.replace(/_/g," ");
}}

function renderHeader() {{
  var row = document.getElementById("headerRow");
  row.innerHTML = "";
  COLUMNS.forEach(function(col) {{
    var th = document.createElement("th");
    var arrow = sortKey===col
      ? '<span style="color:#1a73e8;font-size:0.85em;">' + (sortAsc?' ▲':' ▼') + '</span>'
      : '<span style="color:#ccc;font-size:0.8em;"> ⇅</span>';
    th.innerHTML = colLabel(col) + arrow;
    if (TIPS[col]) th.title = TIPS[col];
    if (col !== "Chart") th.onclick = function() {{
      if (sortKey===col) sortAsc=!sortAsc;
      else {{ sortKey=col; sortAsc=(col==="Symbol"||col==="Trend_Start"); }}
      renderAll();
    }};
    row.appendChild(th);
  }});
}}

function pct(v, cls) {{
  if (v==null) return '<td>–</td>';
  var sign = v>=0 ? '+' : '';
  return '<td class="'+cls+'">'+sign+v+'%</td>';
}}
function num(v, cls) {{
  if (v==null) return '<td>–</td>';
  return '<td class="'+(cls||'')+'">'+v+'</td>';
}}

function renderBody() {{
  var rows = DATA.filter(passes);
  rows.sort(function(a,b) {{
    if (sortKey==="Trend_State") {{
      var oa=TREND_ORDER[a.Trend_State]||9, ob=TREND_ORDER[b.Trend_State]||9;
      return sortAsc ? oa-ob : ob-oa;
    }}
    if (sortKey==="Stage") {{
      var sa=STAGE_SORT[a.Stage]||99, sb=STAGE_SORT[b.Stage]||99;
      return sortAsc ? sa-sb : sb-sa;
    }}
    var va=a[sortKey], vb=b[sortKey];
    if (va==null) va = sortAsc ? 1e15 : -1e15;
    if (vb==null) vb = sortAsc ? 1e15 : -1e15;
    if (typeof va==="string") return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortAsc ? va-vb : vb-va;
  }});

  document.getElementById("resultCount").textContent = rows.length + " stocks";
  var tbody = document.getElementById("tableBody");
  tbody.innerHTML = "";

  rows.forEach(function(r) {{
    var tr = document.createElement("tr");
    tr.innerHTML = COLUMNS.map(function(col) {{
      var v = r[col];
      switch(col) {{
        case "Chart":
          return '<td><a class="cl" href="'+v+'" target="_blank">Chart ↗</a></td>';

        case "Trend_State": {{
          var cls = TREND_CLS[v] || "tb-none";
          return '<td><span class="trend-badge '+cls+'">'+(v||'–')+'</span></td>';
        }}
        case "Gain_Pct": {{
          if (v==null) return '<td>–</td>';
          var sign = v>=0?'+':'';
          return '<td><span class="gain">'+sign+v+'%</span></td>';
        }}
        case "Risk_Pct":
          return pct(v, v!=null&&v<10 ? 'neg' : 'risk');

        case "Upside_127_Pct":
        case "Upside_162_Pct":
        case "Upside_262_Pct":
          return pct(v, v!=null&&v>0 ? 'pos' : 'neg');

        case "Fib_127":
        case "Fib_162":
        case "Fib_262":
          return num(v, 'fib');

        case "Stop_Loss":
          return num(v, 'stop');

        case "RR_127":
        case "RR_162":
          if (v==null) return '<td>–</td>';
          var cls2 = v>=2 ? 'rr pos' : v>=1 ? 'rr' : 'rr neg';
          return '<td class="'+cls2+'">'+v+'x</td>';

        case "Trend_Months":
          return num(v, 'mths');

        case "Monthly_CCI20":
        case "Monthly_MACD_Hist":
          if (v==null) return '<td>–</td>';
          return '<td class="'+(v>0?'pos':'neg')+'">'+v+'</td>';

        case "Super_Bullish":
          return '<td>'+(v?'<span class="super-badge">⭐ SUPER BULLISH</span>':'')+'</td>';

        case "Rocket_Buy":
          return '<td>'+(v?'<span class="rocket-badge">🚀 ROCKET BUY</span>':'')+'</td>';

        case "Monthly_CCI200": {{
          if (v==null) return '<td>–</td>';
          var cls6 = v > 100 ? 'fib' : v > 0 ? 'pos' : 'neg';
          return '<td class="'+cls6+'">'+v+'</td>';
        }}

        case "Monthly_CCI34": {{
          if (v==null) return '<td>–</td>';
          var cls5 = v >= 0 ? 'pos' : v >= -10 ? 'risk' : 'neg';
          return '<td class="'+cls5+'">'+v+'</td>';
        }}

        case "W52_High_Pct": {{
          if (v==null) return '<td>–</td>';
          // 0 = at high (green), negative = below high (amber→red)
          var sign = v>=0?'+':'';
          var cls3 = v >= -5 ? 'pos' : v >= -20 ? 'risk' : 'neg';
          return '<td class="'+cls3+'">'+sign+v+'%</td>';
        }}
        case "W52_Low_Pct": {{
          if (v==null) return '<td>–</td>';
          // positive = above low (good)
          var cls4 = v >= 50 ? 'pos' : v >= 20 ? 'risk' : 'neg';
          return '<td class="'+cls4+'">+'+v+'%</td>';
        }}
        case "W52_High":
        case "W52_Low":
          return num(v, '');

        case "Buy_Signal":
          return '<td>'+(v?'<span class="buy-badge">BUY</span>':'')+'</td>';
        case "Near_Buy":
          return '<td>'+(v?'<span class="near-badge">Near</span>':'')+'</td>';

        case "Stage": {{
          var stMeta = STAGE_META[v] || {{icon:"⬜",label:v||"–",cls:"s1a"}};
          if(!v) return '<td>–</td>';
          return '<td><span class="stage-badge '+stMeta.cls+'" title="'+stMeta.label+'">'+stMeta.icon+' '+v+'</span></td>';
        }}

        case "Fib_Ret_382":
        case "Fib_Ret_618":
        case "Fib_Ext_100":
        case "Fib_Ext_162":
        case "Fib_Ext_262":
          return num(v, 'fib');

        case "Fib_Ext_100_Pct":
        case "Fib_Ext_162_Pct":
          return pct(v, v!=null&&v>0?'pos':'neg');

        case "Fib_Next_TZ":
          return '<td style="font-size:0.75rem;color:#6a1b9a;font-weight:600;">'+(v||'–')+'</td>';

        case "Fib_Next_TZ_Mo":
          if(v==null) return '<td>–</td>';
          var tzCls = v<=3?'neg':v<=12?'risk':'mths';
          return '<td class="'+tzCls+'">'+v+'mo</td>';

        case "Pct_Above_MA30":
          if(v==null) return '<td>–</td>';
          return '<td class="'+(v>0?'pos':'neg')+'">'+v+'%</td>';

        case "MA30W":
          return num(v,'');

        case "RSI_Reversal_Chart":
          if(!v) return '<td style="color:#ccc;font-size:0.74rem;">–</td>';
          return '<td><a class="cl" href="'+v+'" target="_blank" style="background:#7b1fa2;">RSI ↗</a></td>';

        case "Pattern_Count":
          if (!v) return '<td>–</td>';
          return '<td style="font-weight:600;color:#1565c0;cursor:pointer;" onclick="showPatterns(this)" data-sym="'+r.Symbol+'">'+v+'</td>';
        case "Pattern_Top": {{
          if (!v) return '<td>–</td>';
          var str = v.includes('[Strong]') ? '#2e7d32' : v.includes('[Medium]') ? '#e65100' : '#888';
          return '<td style="color:'+str+';font-size:0.76rem;max-width:160px;overflow:hidden;text-overflow:ellipsis;" title="'+v+'">'+v+'</td>';
        }}
        case "Pattern_Bullish":
          return '<td class="pos">'+(v||0)+'</td>';
        case "Pattern_Bearish":
          return '<td class="neg">'+(v||0)+'</td>';

        default:
          if (v==null||v===undefined) return '<td>–</td>';
          return '<td>'+v+'</td>';
      }}
    }}).join("");
    tbody.appendChild(tr);
  }});
}}

function renderAll() {{ renderHeader(); renderBody(); }}

// ── Pattern name buttons (dynamic) ───────────────────────────────────────────
var ALL_PATTERN_NAMES = [
  "Ascending Triangle","Descending Triangle","Symmetrical Triangle",
  "Ascending Channel","Descending Channel","Horizontal Channel",
  "Rising Wedge","Falling Wedge",
  "Trendline Breakout","Trendline Breakdown",
  "Cup & Handle",
  "Bullish Pennant","Bearish Pennant",
  "Bull Flag","Bear Flag",
  "Head & Shoulders Top","Inverse Head & Shoulders",
  "Double Top","Double Bottom",
  "High Delivery","High Delivery (Gap-Up)",
  "52-Week High","52-Week Low",
  "All-Time High","All-Time Low"
];

// Only show pattern buttons that actually appear in the dataset
var foundPatterns = new Set();
DATA.forEach(function(r) {{
  (r.Patterns_JSON||[]).forEach(function(p) {{ foundPatterns.add(p.name); }});
}});

var patContainer = document.getElementById('patNameFilters');
ALL_PATTERN_NAMES.forEach(function(name) {{
  if (!foundPatterns.has(name)) return;
  var btn = document.createElement('button');
  btn.className = 'fbtn pat-name-btn';
  btn.textContent = name;
  btn.dataset.pat = name;
  btn.addEventListener('click', function() {{
    if (activePatterns.has(name)) {{ activePatterns.delete(name); btn.classList.remove('active'); }}
    else {{ activePatterns.add(name); btn.classList.add('active'); }}
    renderBody();
  }});
  patContainer.appendChild(btn);
}});
if (!patContainer.children.length) {{
  patContainer.innerHTML = '<span style="font-size:0.75rem;color:#aaa;">No patterns detected in universe</span>';
}}

// ── Signal buttons ─────────────────────────────────────────────────────────
document.querySelectorAll(".sig-btn").forEach(function(btn) {{
  btn.addEventListener("click", function() {{
    var k = btn.dataset.key;
    if (activeSignals.has(k)) {{ activeSignals.delete(k); btn.classList.remove("active"); }}
    else {{ activeSignals.add(k); btn.classList.add("active"); }}
    renderBody();
  }});
}});

// ── Timeframe buttons ──────────────────────────────────────────────────────
document.querySelectorAll(".tf-btn").forEach(function(btn) {{
  btn.addEventListener("click", function() {{
    var tf = btn.dataset.tf;
    if (activeTFs.has(tf)) {{ activeTFs.delete(tf); btn.classList.remove("active"); }}
    else {{ activeTFs.add(tf); btn.classList.add("active"); }}
    renderBody();
  }});
}});

// ── Strength buttons ───────────────────────────────────────────────────────
document.querySelectorAll(".str-btn").forEach(function(btn) {{
  btn.addEventListener("click", function() {{
    var s = btn.dataset.str;
    if (activeStrs.has(s)) {{ activeStrs.delete(s); btn.classList.remove("active"); }}
    else {{ activeStrs.add(s); btn.classList.add("active"); }}
    renderBody();
  }});
}});

// ── Direction buttons ──────────────────────────────────────────────────────
document.querySelectorAll(".dir-btn").forEach(function(btn) {{
  btn.addEventListener("click", function() {{
    var d = btn.dataset.dir;
    if (activeDirs.has(d)) {{ activeDirs.delete(d); btn.classList.remove("active"); }}
    else {{ activeDirs.add(d); btn.classList.add("active"); }}
    renderBody();
  }});
}});

// ── Clear All ──────────────────────────────────────────────────────────────
document.getElementById("clearAllBtn").addEventListener("click", function() {{
  activeSignals.clear(); activePatterns.clear();
  activeTFs.clear(); activeStrs.clear(); activeDirs.clear();
  document.querySelectorAll(".fbtn").forEach(function(b) {{ b.classList.remove("active"); }});
  renderBody();
}});

document.getElementById("searchBox").addEventListener("input", function(e) {{
  searchQ = e.target.value.trim().toUpperCase();
  renderBody();
}});

// Build pattern lookup: symbol -> patterns list
var PAT_LOOKUP = {{}};
DATA.forEach(function(r) {{
  if (r.Patterns_JSON && r.Patterns_JSON.length) PAT_LOOKUP[r.Symbol] = r.Patterns_JSON;
}});

function showPatterns(cell) {{
  var sym = cell.getAttribute('data-sym');
  var pats = PAT_LOOKUP[sym] || [];
  document.getElementById('patTitle').textContent = sym + ' — Chart Patterns (' + pats.length + ')';
  var TF_FULL = {{D:'Daily',W:'Weekly',M:'Monthly',daily:'Daily',weekly:'Weekly',monthly:'Monthly'}};
  var STR_CLS = {{Strong:'str-S',Medium:'str-M',Weak:'str-W'}};
  var DIR_CLS = {{Bullish:'dir-bull',Bearish:'dir-bear',Neutral:'dir-neu'}};
  var html = '';
  ['Strong','Medium','Weak'].forEach(function(str) {{
    var group = pats.filter(function(p) {{ return p.strength === str; }});
    if (!group.length) return;
    html += '<div style="font-size:0.74rem;font-weight:700;color:#888;margin:8px 0 4px;text-transform:uppercase;">'+str+'</div>';
    group.forEach(function(p) {{
      var dirCls = DIR_CLS[p.direction] || 'dir-neu';
      var strCls = STR_CLS[p.strength] || 'str-W';
      var tfLabel = TF_FULL[p.timeframe] || p.timeframe;
      var dirIcon = p.direction==='Bullish'?'🟢':p.direction==='Bearish'?'🔴':'⚪';
      var lvls = Object.entries(p.key_levels||{{}}).map(function(kv){{return kv[0]+': '+kv[1];}}).join(' | ');
      html += '<div class="pat-card '+strCls+' '+dirCls+'">';
      html += '<div class="pname">'+dirIcon+' '+p.name+'</div>';
      html += '<div class="ptf">'+tfLabel+' · '+p.direction+'</div>';
      if (p.description) html += '<div class="pdesc">'+p.description+'</div>';
      if (lvls) html += '<div class="pdesc" style="color:#1565c0;">'+lvls+'</div>';
      html += '</div>';
    }});
  }});
  if (!html) html = '<p style="color:#888">No patterns detected.</p>';
  document.getElementById('patCards').innerHTML = html;
  document.getElementById('patModal').classList.add('open');
}}
document.getElementById('patModal').addEventListener('click', function(e) {{
  if (e.target === this) this.classList.remove('open');
}});

renderAll();
</script>
</body>
</html>
"""
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Report → {OUTPUT_HTML}")


def main():
    parser = argparse.ArgumentParser(description="Monthly MACD+CCI Breakout Screener")
    parser.add_argument("--universe", default=str(BASE_DIR / "Equity_L.csv"))
    parser.add_argument("--limit",   type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    symbols = load_universe(Path(args.universe))
    if args.limit:
        symbols = symbols[:args.limit]

    print(f"Universe: {len(symbols)} | workers: {args.workers}")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_symbol, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res:
                results.append(res)
            if i % 50 == 0 or i == len(symbols):
                print(f"  {i}/{len(symbols)}  usable={len(results)}")

    build_html_report(results)
    print(f"\nCharts      → {CHARTS_DIR}")
    print(f"⭐ Super:    {sum(1 for r in results if r.get('Super_Bullish'))}")
    print(f"🚀 Rocket:  {sum(1 for r in results if r.get('Rocket_Buy'))}")
    print(f"Buy:        {sum(1 for r in results if r.get('Buy_Signal'))}")
    print(f"Near:       {sum(1 for r in results if r.get('Near_Buy'))}")
    print(f"Strong:     {sum(1 for r in results if r.get('Trend_State')==msig.STRONG_BULLISH)}")
    print(f"Medium:     {sum(1 for r in results if r.get('Trend_State')==msig.MEDIUM_BULLISH)}")
    print(f"Begin:      {sum(1 for r in results if r.get('Trend_State')==msig.TREND_BEGINNING)}")


if __name__ == "__main__":
    main()
