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

Per stock output:
  - Fibonacci extension targets (127.2%, 161.8%, 261.8%) from monthly swing
  - Stop-loss: Low of the monthly bar when bullish cross occurred
  - Per-symbol TradingView chart (white background, light lines)

Usage:
    python monthly_macd_cci_breakout_screener.py --universe Equity_L.csv --limit 0
    python monthly_macd_cci_breakout_screener.py --universe Equity_L.csv --limit 50  # test run
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

BASE_DIR    = Path(__file__).resolve().parent
CACHE_DIR   = BASE_DIR / "cache_parquet"
CHARTS_DIR  = BASE_DIR / "monthly_charts"
OUTPUT_HTML = BASE_DIR / "monthly_breakout_report.html"

MIN_BARS = 30   # minimum daily bars to even try (monthly resampling needs history)


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
        if len(mo) < 15:
            return None

        # Build chart
        chart_path = CHARTS_DIR / f"{symbol}.html"
        build_monthly_chart(symbol, mo, extras, chart_path)

        snap = msig.latest_monthly_snapshot(symbol, daily, mo, extras)
        snap["Chart"] = f"monthly_charts/{symbol}.html"
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
    ("Buy_Signal",      "🎯 Both Crossed Bullish (this bar)"),
    ("Near_Buy",        "📈 Near Buy (cross in last 3 bars)"),
    ("MACD_Bull_Cross", "MACD Bull Cross (this bar)"),
    ("CCI20_Bull_Cross","CCI20 Bull Cross (this bar)"),
    ("trend_begin",     "🟦 Trend Beginning"),
    ("trend_medium",    "🟩 Medium Bullish"),
    ("trend_strong",    "🟧 Strong Bullish"),
]

TOOLTIPS = {
    "Symbol":             "NSE ticker",
    "Close":              "Latest daily closing price",
    "Monthly_RSI14":      "RSI(14) on monthly bars",
    "Monthly_CCI20":      "CCI(20) on monthly bars",
    "Monthly_MACD_Hist":  "MACD(12,26,9) histogram on monthly bars",
    "Trend_State":        "CCI-based trend classification: Trend Beginning / Medium Bullish / Strong Bullish",
    "Buy_Signal":         "Monthly MACD(12,26,9) bullish cross AND CCI(20) bullish cross SMA20 — same bar",
    "Near_Buy":           "Either MACD or CCI bullish cross within last 3 monthly bars, and both currently bullish",
    "Stop_Loss":          "Low of the bar where the most recent bullish cross occurred — use as hard stop",
    "Fib_1.272":          "127.2% Fibonacci extension target",
    "Fib_1.618":          "161.8% Fibonacci extension target (primary)",
    "Fib_2.618":          "261.8% Fibonacci extension target (stretch)",
}


def _trend_badge(state: str) -> str:
    colors = {
        msig.TREND_BEGINNING: ("#1565c0", "#e3f2fd"),
        msig.MEDIUM_BULLISH:  ("#2e7d32", "#e8f5e9"),
        msig.STRONG_BULLISH:  ("#e65100", "#fff3e0"),
        msig.NO_TREND:        ("#666",    "#f5f5f5"),
    }
    col, bg = colors.get(state, ("#666", "#f5f5f5"))
    return (f'<span style="background:{bg};color:{col};padding:2px 8px;'
            f'border-radius:10px;font-size:0.78rem;font-weight:600;">{state}</span>')


def build_html_report(rows: list[dict]):
    # Add virtual filter columns
    for r in rows:
        r["trend_begin"]  = r.get("Trend_State") == msig.TREND_BEGINNING
        r["trend_medium"] = r.get("Trend_State") == msig.MEDIUM_BULLISH
        r["trend_strong"] = r.get("Trend_State") == msig.STRONG_BULLISH

    # Default sort: trend order, then by CCI20 descending
    rows.sort(key=lambda r: (
        TREND_ORDER.get(r.get("Trend_State", msig.NO_TREND), 9),
        -(r.get("Monthly_CCI20") or -9999)
    ))

    data_json      = json.dumps(rows, default=str)
    filter_buttons = "\n".join(
        f'<button class="filter-btn" data-key="{key}">{label}</button>'
        for key, label in FILTER_DEFS
    )
    tooltips_json  = json.dumps(TOOLTIPS)

    total   = len(rows)
    buy_cnt = sum(1 for r in rows if r.get("Buy_Signal"))
    near_cnt= sum(1 for r in rows if r.get("Near_Buy"))
    begin_cnt=sum(1 for r in rows if r.get("Trend_State") == msig.TREND_BEGINNING)
    med_cnt = sum(1 for r in rows if r.get("Trend_State") == msig.MEDIUM_BULLISH)
    str_cnt = sum(1 for r in rows if r.get("Trend_State") == msig.STRONG_BULLISH)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>NSE Monthly MACD + CCI Breakout Screener</title>
<style>
/* ── White, clean theme ── */
*, *::before, *::after {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 16px 20px;
  font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
  background: #f9fafb; color: #131722; font-size: 0.88rem;
}}
h2 {{ margin: 0 0 4px; font-size: 1.25rem; color: #131722; }}
.subtitle {{ color: #666; font-size: 0.8rem; margin-bottom: 14px; }}

/* ── Stats bar ── */
.stats {{
  display: flex; gap: 12px; flex-wrap: wrap;
  margin-bottom: 14px;
}}
.stat-card {{
  background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
  padding: 6px 16px; text-align: center;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}}
.stat-card .val {{ font-size: 1.4rem; font-weight: 700; color: #131722; }}
.stat-card .lbl {{ font-size: 0.72rem; color: #777; }}

/* ── Filter buttons ── */
.filters {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }}
.filter-btn {{
  background: #fff; color: #333; border: 1px solid #c8cad0;
  border-radius: 16px; padding: 4px 13px; cursor: pointer;
  font-size: 0.79rem; transition: all 0.15s;
}}
.filter-btn:hover  {{ background: #e8eaf6; }}
.filter-btn.active {{ background: #1a73e8; border-color: #1a73e8; color: #fff; }}
#clearBtn {{
  background: #fff; color: #888; border: 1px solid #e0e0e0;
  border-radius: 16px; padding: 4px 13px; cursor: pointer; font-size: 0.79rem;
}}
#clearBtn:hover {{ background: #fce4ec; color: #c62828; border-color: #ef9a9a; }}

/* ── Table ── */
.table-wrap {{
  background: #fff; border: 1px solid #e0e0e0; border-radius: 10px;
  overflow-x: auto;
  box-shadow: 0 2px 6px rgba(0,0,0,0.06);
}}
table {{ border-collapse: collapse; width: 100%; }}
thead th {{
  padding: 8px 10px; text-align: left;
  background: #f5f7fa; border-bottom: 2px solid #e0e0e0;
  cursor: pointer; user-select: none; white-space: nowrap;
  font-size: 0.8rem; color: #444; font-weight: 600;
}}
thead th:hover {{ background: #ebebeb; }}
tbody tr {{ border-bottom: 1px solid #f0f0f0; transition: background 0.1s; }}
tbody tr:hover {{ background: #f8f9ff; }}
tbody td {{ padding: 6px 10px; white-space: nowrap; font-size: 0.82rem; }}

/* ── Signal badges ── */
.buy-badge  {{ background:#e8f5e9; color:#2e7d32; padding:2px 8px; border-radius:10px; font-weight:700; font-size:0.78rem; }}
.near-badge {{ background:#e3f2fd; color:#1565c0; padding:2px 8px; border-radius:10px; font-weight:600; font-size:0.78rem; }}
.cross-dot  {{ color:#1a73e8; font-size:1rem; }}
.neg        {{ color:#ef5350; }}
.pos        {{ color:#26a69a; }}
.fib-ext    {{ color:#7b1fa2; font-weight:600; }}
.stop-val   {{ color:#c62828; font-weight:600; }}

a.chart-link {{
  background: #1a73e8; color: #fff; text-decoration: none;
  padding: 3px 10px; border-radius: 5px; font-size: 0.75rem;
  font-weight: 600;
}}
a.chart-link:hover {{ background: #1557b0; }}

#searchBox {{
  padding: 6px 12px; border: 1px solid #c8cad0; border-radius: 6px;
  font-size: 0.82rem; width: 180px; margin-left: auto;
}}
.toolbar-row {{ display:flex; align-items:center; gap:8px; margin-bottom:10px; }}
#resultCount {{ font-size:0.8rem; color:#888; }}
</style>
</head>
<body>

<h2>NSE Monthly MACD(12,26,9) + CCI(20) Breakout Screener</h2>
<div class="subtitle">
  Signal: Monthly MACD bullish cross &amp; CCI(20) bull-cross SMA20 &nbsp;|&nbsp;
  Fib extensions from monthly swing &nbsp;|&nbsp; Stop = trend-beginning bar low
</div>

<div class="stats">
  <div class="stat-card"><div class="val">{total}</div><div class="lbl">Total Stocks</div></div>
  <div class="stat-card"><div class="val" style="color:#2e7d32">{buy_cnt}</div><div class="lbl">Both Crossed (this bar)</div></div>
  <div class="stat-card"><div class="val" style="color:#1565c0">{near_cnt}</div><div class="lbl">Near Buy</div></div>
  <div class="stat-card"><div class="val" style="color:#1565c0">{begin_cnt}</div><div class="lbl">Trend Beginning</div></div>
  <div class="stat-card"><div class="val" style="color:#2e7d32">{med_cnt}</div><div class="lbl">Medium Bullish</div></div>
  <div class="stat-card"><div class="val" style="color:#e65100">{str_cnt}</div><div class="lbl">Strong Bullish</div></div>
</div>

<div class="filters">
{filter_buttons}
<button id="clearBtn">✕ Clear</button>
</div>

<div class="toolbar-row">
  <span id="resultCount"></span>
  <input type="text" id="searchBox" placeholder="Search symbol…">
</div>

<div class="table-wrap">
<table id="mainTable">
<thead>
<tr id="headerRow"></tr>
</thead>
<tbody id="tableBody"></tbody>
</table>
</div>

<script>
const DATA     = {data_json};
const TOOLTIPS = {tooltips_json};

// Columns to display
const COLUMNS = [
  "Symbol","Close",
  "Trend_State",
  "Monthly_CCI20","Monthly_CCI_SMA20","Monthly_MACD_Hist",
  "Monthly_RSI14",
  "Buy_Signal","Near_Buy",
  "Stop_Loss",
  "Fib_1.272","Fib_1.618","Fib_2.618",
  "Chart"
];

// Pre-compute fib extension columns
DATA.forEach(function(r) {{
  var fib = r.Fib_Extensions || {{}};
  r["Fib_1.272"] = fib["1.272"] || null;
  r["Fib_1.618"] = fib["1.618"] || null;
  r["Fib_2.618"] = fib["2.618"] || null;
}});

let activeFilters = new Set();
let sortKey = "Monthly_CCI20";
let sortAsc  = false;
let searchQ  = "";

function passes(r) {{
  if (searchQ && !r.Symbol.toUpperCase().includes(searchQ)) return false;
  for (var k of activeFilters) {{
    if (!r[k]) return false;
  }}
  return true;
}}

function renderHeader() {{
  var row = document.getElementById("headerRow");
  row.innerHTML = "";
  COLUMNS.forEach(function(col) {{
    var th = document.createElement("th");
    var lbl = col.replace(/_/g," ")
                 .replace("Monthly ","Mo.")
                 .replace("Fib 1.272","Fib 127%")
                 .replace("Fib 1.618","Fib 161.8%")
                 .replace("Fib 2.618","Fib 261.8%");
    th.innerHTML = lbl + (sortKey===col ? (sortAsc?" ▲":" ▼") : "");
    if (TOOLTIPS[col]) th.title = TOOLTIPS[col];
    if (col !== "Chart") th.onclick = function() {{
      if (sortKey===col) sortAsc=!sortAsc; else {{ sortKey=col; sortAsc=col==="Symbol"; }}
      renderAll();
    }};
    row.appendChild(th);
  }});
}}

const TREND_ORDER = {{"Strong Bullish":0,"Medium Bullish":1,"Trend Beginning":2,"No Trend":3}};

function renderBody() {{
  var rows = DATA.filter(passes);
  rows.sort(function(a,b) {{
    if (sortKey === "Trend_State") {{
      var oa = TREND_ORDER[a.Trend_State]??9, ob = TREND_ORDER[b.Trend_State]??9;
      return sortAsc ? oa-ob : ob-oa;
    }}
    var va = a[sortKey], vb = b[sortKey];
    if (va==null) va = sortAsc ? Infinity : -Infinity;
    if (vb==null) vb = sortAsc ? Infinity : -Infinity;
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
          return '<td><a class="chart-link" href="'+v+'" target="_blank">Chart</a></td>';
        case "Buy_Signal":
          return '<td>'+(v?'<span class="buy-badge">BUY</span>':'')+'</td>';
        case "Near_Buy":
          return '<td>'+(v?'<span class="near-badge">Near Buy</span>':'')+'</td>';
        case "Trend_State":
          var colors = {{
            "Strong Bullish":"#fff3e0;color:#e65100",
            "Medium Bullish":"#e8f5e9;color:#2e7d32",
            "Trend Beginning":"#e3f2fd;color:#1565c0",
          }};
          var cs = colors[v] || "#f5f5f5;color:#666";
          return '<td><span style="background:'+cs.split(';')[0]+';'+cs.split(';')[1]
               + ';padding:2px 8px;border-radius:10px;font-size:0.78rem;font-weight:600;">'+(v||'—')+'</span></td>';
        case "Stop_Loss":
          return '<td class="stop-val">'+(v!=null?v:'–')+'</td>';
        case "Fib_1.272":
        case "Fib_1.618":
        case "Fib_2.618":
          return '<td class="fib-ext">'+(v!=null?v:'–')+'</td>';
        case "Monthly_CCI20":
        case "Monthly_MACD_Hist": {{
          if (v==null) return '<td>–</td>';
          var cls = v>0?'pos':'neg';
          return '<td class="'+cls+'">'+v+'</td>';
        }}
        default:
          if (v==null||v===undefined) return '<td>–</td>';
          return '<td>'+v+'</td>';
      }}
    }}).join("");
    tbody.appendChild(tr);
  }});
}}

function renderAll() {{ renderHeader(); renderBody(); }}

// Filter buttons
document.querySelectorAll(".filter-btn").forEach(function(btn) {{
  btn.addEventListener("click", function() {{
    var k = btn.dataset.key;
    if (activeFilters.has(k)) {{ activeFilters.delete(k); btn.classList.remove("active"); }}
    else {{ activeFilters.add(k); btn.classList.add("active"); }}
    renderBody();
  }});
}});
document.getElementById("clearBtn").addEventListener("click", function() {{
  activeFilters.clear();
  document.querySelectorAll(".filter-btn").forEach(function(b) {{ b.classList.remove("active"); }});
  renderBody();
}});
document.getElementById("searchBox").addEventListener("input", function(e) {{
  searchQ = e.target.value.trim().toUpperCase();
  renderBody();
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
    parser.add_argument("--limit",   type=int, default=0,  help="0 = full universe")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    symbols = load_universe(Path(args.universe))
    if args.limit:
        symbols = symbols[:args.limit]

    print(f"Universe: {len(symbols)} symbols | workers: {args.workers}")
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
    print(f"\nCharts → {CHARTS_DIR}")
    print(f"Buy signals: {sum(1 for r in results if r.get('Buy_Signal'))}")
    print(f"Near buy:    {sum(1 for r in results if r.get('Near_Buy'))}")


if __name__ == "__main__":
    main()
