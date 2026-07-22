"""
rsi_cci_macd_screener.py
=========================
NSE momentum screener combining:

  BUY  : RSI(14) bull-crosses SMA14(RSI)  AND  CCI(200) bull-crosses SMA20(CCI)
         AND both are positive (RSI>50, CCI>0)  AND  MACD(34,200,9) bullish cross
  SELL : MACD(34,200,9) bearish cross  AND  CCI(200) bearish-crosses SMA20(CCI)

Extra filters (independent boolean columns, usable for report filtering):
  - CCI(200) above 100
  - CCI(200) bullish AND increasing
  - CCI(200) strong momentum + strong volume
  - Heikin-Ashi bullish trend (last 3 HA candles bullish)
  - Strong RSI Buy: daily, weekly & monthly RSI(14) all rising simultaneously

Also produces, per stock:
  - RSI-based projected price targets (ported from the supplied Pine Script)
  - RSI trend-line/channel (daily) via linear regression
  - Price support / resistance levels (swing pivots, daily)
  - Weekly & monthly trend channels overlaid on the daily chart
  - Interactive TradingView-style Plotly chart with daily/weekly/monthly RSI

Usage:
    python rsi_cci_macd_screener.py --universe Equity_L.csv --limit 0

Run with --limit N while testing (N=0 means the full universe).
"""
from __future__ import annotations
import argparse
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

import data_fetch
import signals as sig
import indicators as ind
from chart import build_chart

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache_parquet"
CHARTS_DIR = BASE_DIR / "charts"
OUTPUT_HTML = BASE_DIR / "rsi_cci_macd_report.html"

MIN_BARS_REQUIRED = 30  # safety floor only (avoids crashes in rolling/regression calcs);
                         # indicators needing more history (CCI200, MACD 200) will just show as blank/NaN


def load_universe(csv_path: Path) -> list[str]:
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]
    symbols = df["SYMBOL"].dropna().astype(str).str.strip().tolist()
    return symbols


def process_symbol(symbol: str) -> dict | None:
    try:
        daily = data_fetch.get_history(symbol, CACHE_DIR)
        if daily is None:
            print(f"[SKIP] {symbol}: no data returned from yfinance")
            return None
        if len(daily) < MIN_BARS_REQUIRED:
            print(f"[SKIP] {symbol}: only {len(daily)} bars (<{MIN_BARS_REQUIRED} required)")
            return None

        df, wk, mo, wk_rsi, mo_rsi = sig.build_signal_table(daily)
        targets = ind.rsi_price_targets(df, length=14)
        supports, resistances = ind.support_resistance(df, order=5, lookback=250, n_levels=2)

        chart_path = CHARTS_DIR / f"{symbol}.html"
        build_chart(symbol, df, wk, mo, wk_rsi, mo_rsi, supports, resistances, chart_path)

        snap = sig.latest_snapshot(symbol, df, wk_rsi, mo_rsi, targets, supports, resistances)
        snap["Chart"] = f"charts/{symbol}.html"
        return snap
    except Exception as e:
        print(f"[WARN] {symbol}: {e}")
        traceback.print_exc()
        return None


# ----------------------------------------------------------------------
# HTML report
# ----------------------------------------------------------------------
FILTER_DEFS = [
    ("Buy_Signal", "Buy Signal"),
    ("Sell_Signal", "Sell Signal"),
    ("CCI200_Above_100", "CCI(200) > 100"),
    ("CCI200_Bullish_Increasing", "CCI(200) Bullish & Increasing"),
    ("CCI200_Strong_Mom_Volume", "CCI(200) Strong Momentum + Volume"),
    ("HA_Bullish_Trend", "Heikin-Ashi Bullish Trend"),
    ("Strong_RSI_Buy", "Strong RSI Buy (D+W+M rising)"),
]

TOOLTIPS = {
    "Symbol": "NSE ticker symbol",
    "Close": "Latest daily closing price",
    "RSI14": "Daily RSI(14). Buy leg requires RSI14 crossing above its SMA(14) and RSI14 &gt; 50.",
    "Weekly_RSI14": "RSI(14) computed on weekly-resampled closes",
    "Monthly_RSI14": "RSI(14) computed on monthly-resampled closes",
    "CCI200": "Commodity Channel Index(200). Buy leg requires CCI200 crossing above its SMA(20) and CCI200 &gt; 0.",
    "MACD_Hist": "MACD(34,200,9) histogram (MACD line minus signal line)",
    "Buy_Signal": "RSI14 bull-cross SMA14 + CCI200 bull-cross SMA20 (both positive) + MACD(34,200,9) bullish cross",
    "Sell_Signal": "MACD(34,200,9) bearish cross + CCI200 bearish-cross SMA20",
    "Strong_RSI_Buy": "Daily, Weekly and Monthly RSI(14) are all rising on the same day",
}


def build_html_report(rows: list[dict]):
    data_json = json.dumps(rows, default=str)
    filter_buttons = "\n".join(
        f'<button class="btn btn-sm btn-outline-info filter-btn" data-key="{key}">{label}</button>'
        for key, label in FILTER_DEFS
    )

    html = f"""<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
<meta charset="UTF-8">
<title>NSE RSI / CCI / MACD Momentum Screener</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body {{ padding: 20px; }}
.filter-btn.active {{ background:#0dcaf0; color:#000; }}
th, td {{ font-size: 0.85rem; white-space: nowrap; }}
.buy {{ color:#00e676; font-weight:600; }}
.sell {{ color:#ff1744; font-weight:600; }}
</style>
</head>
<body>
<h3>NSE RSI(14) / CCI(200) / MACD(34,200,9) Momentum Screener</h3>
<p class="text-secondary">Generated report - click a column header to sort, use buttons below to filter.</p>
<div class="mb-3 d-flex flex-wrap gap-2">
{filter_buttons}
<button class="btn btn-sm btn-outline-secondary" id="clearFilters">Clear Filters</button>
</div>
<div class="table-responsive">
<table class="table table-dark table-striped table-hover" id="stockTable">
<thead>
<tr id="headerRow"></tr>
</thead>
<tbody id="tableBody"></tbody>
</table>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
const DATA = {data_json};
const TOOLTIPS = {json.dumps(TOOLTIPS)};
const COLUMNS = ["Symbol","Date","Close","RSI14","Weekly_RSI14","Monthly_RSI14",
                  "CCI200","MACD_Hist","Buy_Signal","Sell_Signal","Strong_RSI_Buy","Chart"];

let activeFilters = new Set();
let sortKey = "Symbol", sortAsc = true;

function renderHeader() {{
  const row = document.getElementById("headerRow");
  row.innerHTML = "";
  COLUMNS.forEach(col => {{
    const th = document.createElement("th");
    th.style.cursor = "pointer";
    th.innerHTML = col.replace(/_/g," ") + (sortKey===col ? (sortAsc? " ▲":" ▼") : "");
    if (TOOLTIPS[col]) th.title = TOOLTIPS[col];
    th.onclick = () => {{
      if (sortKey === col) sortAsc = !sortAsc; else {{ sortKey = col; sortAsc = true; }}
      renderAll();
    }};
    row.appendChild(th);
  }});
}}

function passesFilters(row) {{
  for (const key of activeFilters) {{
    if (!row[key]) return false;
  }}
  return true;
}}

function renderBody() {{
  let rows = DATA.filter(passesFilters);
  rows.sort((a,b) => {{
    let va = a[sortKey], vb = b[sortKey];
    if (va === null || va === undefined) va = -Infinity;
    if (vb === null || vb === undefined) vb = -Infinity;
    if (typeof va === "string") return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortAsc ? va - vb : vb - va;
  }});
  const tbody = document.getElementById("tableBody");
  tbody.innerHTML = "";
  rows.forEach(r => {{
    const tr = document.createElement("tr");
    tr.innerHTML = COLUMNS.map(col => {{
      let v = r[col];
      if (col === "Chart") return `<td><a href="${{v}}" target="_blank" class="btn btn-sm btn-outline-primary">Chart</a></td>`;
      if (col === "Buy_Signal") return `<td class="${{v?'buy':''}}">${{v?'BUY':''}}</td>`;
      if (col === "Sell_Signal") return `<td class="${{v?'sell':''}}">${{v?'SELL':''}}</td>`;
      if (col === "Strong_RSI_Buy") return `<td>${{v?'⭐':''}}</td>`;
      if (v === null || v === undefined) return "<td>-</td>";
      return `<td>${{v}}</td>`;
    }}).join("");
    tbody.appendChild(tr);
  }});
}}

function renderAll() {{ renderHeader(); renderBody(); }}

document.querySelectorAll(".filter-btn").forEach(btn => {{
  btn.addEventListener("click", () => {{
    const key = btn.dataset.key;
    if (activeFilters.has(key)) {{ activeFilters.delete(key); btn.classList.remove("active"); }}
    else {{ activeFilters.add(key); btn.classList.add("active"); }}
    renderBody();
  }});
}});
document.getElementById("clearFilters").addEventListener("click", () => {{
  activeFilters.clear();
  document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
  renderBody();
}});

renderAll();
</script>
</body>
</html>
"""
    OUTPUT_HTML.write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=str(BASE_DIR / "Equity_L.csv"))
    parser.add_argument("--limit", type=int, default=0, help="0 = full universe")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    symbols = load_universe(Path(args.universe))
    if args.limit:
        symbols = symbols[: args.limit]

    print(f"Universe size: {len(symbols)}")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_symbol, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            res = fut.result()
            if res:
                results.append(res)
            if i % 25 == 0 or i == len(symbols):
                print(f"  processed {i}/{len(symbols)} ({len(results)} usable)")

    build_html_report(results)
    print(f"\nReport written to: {OUTPUT_HTML}")
    print(f"Per-stock charts written to: {CHARTS_DIR}")


if __name__ == "__main__":
    main()
