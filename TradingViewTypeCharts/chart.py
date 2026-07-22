"""
chart.py
Builds a per-symbol chart using TradingView's own open-source charting
engine - Lightweight Charts v5 (https://tradingview.github.io/lightweight-charts/)
- instead of a general-purpose plotting library. This is the same rendering
engine TradingView's own site uses, so pan/zoom/crosshair/price-scale
behaviour matches TradingView natively rather than being approximated.

Layout (3 native panes, one chart instance, synced crosshair/time-scale):
  Pane 0 : Heikin-Ashi candles (Daily/Weekly/Monthly switch) + regular-candle
           overlay + trend channel (Daily/Weekly/Monthly, switches with the
           timeframe buttons) + Buy/Sell/Strong-RSI markers + Support/
           Resistance native price-lines + volume histogram (own price scale)
  Pane 1 : Daily / Weekly / Monthly RSI(14) + RSI trend channel + 70/30 lines
  Pane 2 : MACD(34,200,9) line/signal/histogram

Native TradingView behaviours used as-is (no custom re-implementation):
  - price-scale auto-fits to visible candles (built-in autoScale)
  - drag on the price axis to zoom price scale vertically
  - mouse-wheel zoom, click-drag pan, kinetic scroll
  - price lines (Support/Resistance) with on-axis labels
  - series markers (Buy/Sell/Strong RSI)
  - crosshair with a live OHLC/indicator legend box
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pandas as pd

import indicators as ind

LWC_CDN = "https://unpkg.com/lightweight-charts@5/dist/lightweight-charts.standalone.production.js"


def _fmt_time(idx) -> str:
    return pd.Timestamp(idx).strftime("%Y-%m-%d")


def _line_records(series: pd.Series) -> list:
    s = series.dropna()
    return [{"time": _fmt_time(t), "value": round(float(v), 4)} for t, v in s.items()]


def _candle_records(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series) -> list:
    out = []
    for t in o.index:
        ov, hv, lv, cv = o[t], h[t], l[t], c[t]
        if pd.isna(ov) or pd.isna(hv) or pd.isna(lv) or pd.isna(cv):
            continue
        out.append({"time": _fmt_time(t), "open": round(float(ov), 4),
                     "high": round(float(hv), 4), "low": round(float(lv), 4),
                     "close": round(float(cv), 4)})
    return out


def _volume_records(df: pd.DataFrame) -> list:
    out = []
    for t, row in df.iterrows():
        if pd.isna(row["Volume"]):
            continue
        up = row["Close"] >= row["Open"]
        out.append({"time": _fmt_time(t), "value": float(row["Volume"]),
                     "color": "rgba(38,166,154,0.6)" if up else "rgba(239,83,80,0.6)"})
    return out


def _marker_records(dates, position: str, color: str, shape: str, text: str) -> list:
    return [{"time": _fmt_time(t), "position": position, "color": color,
              "shape": shape, "text": text} for t in dates]


def _channel_records(series: pd.Series, lookback: int):
    trend, upper, lower = ind.regression_channel(series, lookback=lookback)
    return _line_records(trend), _line_records(upper), _line_records(lower)


def build_chart(symbol: str, df: pd.DataFrame, wk: pd.DataFrame, mo: pd.DataFrame,
                 wk_rsi: pd.Series, mo_rsi: pd.Series, supports: list, resistances: list,
                 out_path: Path):
    wk_ha = ind.heikin_ashi(wk) if len(wk) else pd.DataFrame()
    mo_ha = ind.heikin_ashi(mo) if len(mo) else pd.DataFrame()

    d_trend, d_upper, d_lower = _channel_records(df["Close"], lookback=60)
    w_trend, w_upper, w_lower = _channel_records(wk["Close"], lookback=13) if len(wk) else ([], [], [])
    m_trend, m_upper, m_lower = _channel_records(mo["Close"], lookback=12) if len(mo) else ([], [], [])

    # RSI -> price projection table (per timeframe, mirrors the Pine Script table)
    def _rsi_table(tf_df: pd.DataFrame):
        if len(tf_df) < 16:
            return None
        rsi_min, rsi_max = ind.rsi_range_for_bar(tf_df, length=14)
        targets = ind.rsi_price_targets(tf_df, length=14)
        rsi_now = ind.rsi(tf_df["Close"], 14)
        rsi_current = float(rsi_now.iloc[-1]) if len(rsi_now) and pd.notna(rsi_now.iloc[-1]) else None
        return {
            "rsi_current": round(rsi_current, 2) if rsi_current is not None else None,
            "rsi_min": round(rsi_min, 2) if rsi_min is not None else None,
            "rsi_max": round(rsi_max, 2) if rsi_max is not None else None,
            "targets": [{"rsi": t, "price": (round(p, 2) if p is not None else None)}
                        for t, p in targets.items()],
        }

    rsi_table_daily = _rsi_table(df)
    rsi_table_weekly = _rsi_table(wk) if len(wk) else None
    rsi_table_monthly = _rsi_table(mo) if len(mo) else None

    # Fibonacci retracement/extension levels (per timeframe)
    fib_daily = ind.fibonacci_levels(df, lookback=60)
    fib_weekly = ind.fibonacci_levels(wk, lookback=26) if len(wk) else {}
    fib_monthly = ind.fibonacci_levels(mo, lookback=24) if len(mo) else {}

    data = {
        "daily": {
            "ha": _candle_records(df["HA_Open"], df["HA_High"], df["HA_Low"], df["HA_Close"]),
            "regular": _candle_records(df["Open"], df["High"], df["Low"], df["Close"]),
            "volume": _volume_records(df),
            "channel_trend": d_trend, "channel_upper": d_upper, "channel_lower": d_lower,
            "rsi_table": rsi_table_daily,
            "fib": fib_daily,
        },
        "weekly": {
            "ha": _candle_records(wk_ha["HA_Open"], wk_ha["HA_High"], wk_ha["HA_Low"], wk_ha["HA_Close"]) if len(wk_ha) else [],
            "volume": _volume_records(wk) if len(wk) else [],
            "channel_trend": w_trend, "channel_upper": w_upper, "channel_lower": w_lower,
            "rsi_table": rsi_table_weekly,
            "fib": fib_weekly,
        },
        "monthly": {
            "ha": _candle_records(mo_ha["HA_Open"], mo_ha["HA_High"], mo_ha["HA_Low"], mo_ha["HA_Close"]) if len(mo_ha) else [],
            "volume": _volume_records(mo) if len(mo) else [],
            "channel_trend": m_trend, "channel_upper": m_upper, "channel_lower": m_lower,
            "rsi_table": rsi_table_monthly,
            "fib": fib_monthly,
        },
        "rsi_daily": _line_records(df["RSI14"]),
        "rsi_daily_sma14": _line_records(df["RSI14_SMA14"]),
        "rsi_daily_sma34": _line_records(df["RSI14_SMA34"]),
        "rsi_weekly": _line_records(df["Weekly_RSI14"]),
        "rsi_weekly_sma": _line_records(df["Weekly_RSI14_SMA14"]),
        "rsi_monthly": _line_records(df["Monthly_RSI14"]),
        "rsi_monthly_sma": _line_records(df["Monthly_RSI14_SMA14"]),
        "macd": _line_records(df["MACD"]),
        "macd_signal": _line_records(df["MACD_Signal"]),
        "macd_hist": [{"time": r["time"], "value": r["value"],
                       "color": "rgba(38,166,154,0.7)" if r["value"] >= 0 else "rgba(239,83,80,0.7)"}
                      for r in _line_records(df["MACD_Hist"])],
        "buys": _marker_records(df[df["Buy_Signal"]].index, "belowBar", "#00e676", "arrowUp", "BUY"),
        "sells": _marker_records(df[df["Sell_Signal"]].index, "aboveBar", "#ff1744", "arrowDown", "SELL"),
        "strong": _marker_records(df[df["Strong_RSI_Buy"]].index, "belowBar", "#ffd600", "circle", "★"),
        "supports": supports,
        "resistances": resistances,
        "symbol": symbol,
        "n_daily_bars": len(df),
    }

    html = _HTML_TEMPLATE.replace("__SYMBOL__", symbol).replace("__DATA_JSON__", json.dumps(data)).replace("__LWC_CDN__", LWC_CDN)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>__SYMBOL__ - Chart</title>
<script src="__LWC_CDN__"></script>
<style>
  html, body { margin:0; padding:0; background:#0e1116; color:#d1d4dc; font-family: -apple-system, Segoe UI, Roboto, sans-serif;
               height:100%; overflow-x:hidden; }
  #toolbar { display:flex; gap:6px; align-items:center; padding:8px 10px; flex-wrap:wrap; background:#131722; border-bottom:1px solid #2a2e39; }
  button.tvbtn { background:#1e222d; color:#d1d4dc; border:1px solid #2a2e39; border-radius:4px; padding:5px 12px; cursor:pointer; font-size:0.82rem; }
  button.tvbtn:hover { background:#2a2e39; }
  button.tvbtn.active { background:#2962ff; border-color:#2962ff; color:#fff; }
  #chartContainer { position:relative; width:100%; height:calc(100vh - 46px); min-height:600px; }
  #legend { position:absolute; top:8px; left:10px; z-index:5; font-size:0.78rem; background:rgba(19,23,34,0.85);
            padding:6px 10px; border-radius:4px; pointer-events:none; line-height:1.5; }
  #legend .sym { font-weight:600; color:#fff; }
  .up { color:#26a69a; } .down { color:#ef5350; }
  #rsiTable { position:absolute; top:8px; right:10px; z-index:6; display:none; background:rgba(19,23,34,0.92);
              border:1px solid #2a2e39; border-radius:5px; padding:8px 10px; font-size:0.76rem; min-width:190px; }
  #rsiTable table { border-collapse:collapse; width:100%; }
  #rsiTable td { padding:2px 6px; white-space:nowrap; }
  #rsiTable .hdr { font-weight:600; color:#fff; border-bottom:1px solid #2a2e39; padding-bottom:4px; }
  #rsiTable .tgt { color:#00e5ff; }
  #rsiTable .prc { color:#00e676; text-align:right; }
</style>
</head>
<body>
<div id="toolbar">
  <span style="font-weight:600;color:#fff;margin-right:8px;">__SYMBOL__</span>
  <button class="tvbtn tf-btn active" data-tf="daily">Daily</button>
  <button class="tvbtn tf-btn" data-tf="weekly">Weekly</button>
  <button class="tvbtn tf-btn" data-tf="monthly">Monthly</button>
  <span style="width:1px;background:#2a2e39;align-self:stretch;margin:0 4px;"></span>
  <button class="tvbtn" id="scrollLeftBtn" title="Scroll left">&#9664;</button>
  <button class="tvbtn" id="zoomOutBtn" title="Zoom out">&minus;</button>
  <button class="tvbtn" id="zoomInBtn" title="Zoom in">&plus;</button>
  <button class="tvbtn" id="scrollRightBtn" title="Scroll right">&#9654;</button>
  <span style="width:1px;background:#2a2e39;align-self:stretch;margin:0 4px;"></span>
  <button class="tvbtn" id="scaleToggle">Log Scale</button>
  <button class="tvbtn" id="regularToggle">Show Regular Candles</button>
  <span style="width:1px;background:#2a2e39;align-self:stretch;margin:0 4px;"></span>
  <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px;"><input type="checkbox" id="chkWeekly" checked> <span style="color:#ff9800;">Weekly RSI</span></label>
  <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px;"><input type="checkbox" id="chkMonthly" checked> <span style="color:#e040fb;">Monthly RSI</span></label>
  <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px;"><input type="checkbox" id="chkSma14" checked> SMA14</label>
  <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px;"><input type="checkbox" id="chkSma34" checked> SMA34</label>
  <button class="tvbtn" id="fitBtn">Fit All</button>
  <button class="tvbtn" id="fsBtn">Fullscreen</button>
  <span style="width:1px;background:#2a2e39;align-self:stretch;margin:0 4px;"></span>
  <button class="tvbtn" id="rsiTableBtn">RSI Price Table</button>
  <button class="tvbtn" id="fibBtn">Fib Levels</button>
</div>
<div id="chartContainer">
  <div id="legend"></div>
  <div id="rsiTable"></div>
</div>

<script>
const DATA = __DATA_JSON__;

const container = document.getElementById('chartContainer');
const chart = LightweightCharts.createChart(container, {
  layout: { background: { type: 'solid', color: '#0e1116' }, textColor: '#d1d4dc', panes: { separatorColor: '#2a2e39' } },
  grid: { vertLines: { color: '#1b1f2b' }, horzLines: { color: '#1b1f2b' } },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#2a2e39' },
  timeScale: { borderColor: '#2a2e39', timeVisible: false },
  handleScale: { axisPressedMouseMove: { time: true, price: true }, mouseWheel: true, pinch: true },
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  autoSize: true,
});

// ---------- Pane 0: Price ----------
const haSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
  wickUpColor: '#26a69a', wickDownColor: '#ef5350',
}, 0);

const regularSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: 'rgba(128,203,196,0.35)', downColor: 'rgba(239,154,154,0.35)', borderVisible: false,
  wickUpColor: 'rgba(128,203,196,0.35)', wickDownColor: 'rgba(239,154,154,0.35)', visible: false,
}, 0);

const volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
  priceFormat: { type: 'volume' }, priceScaleId: 'vol',
}, 0);
volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
haSeries.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } });

const chUpper = chart.addSeries(LightweightCharts.LineSeries, { color: '#ffb300', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false }, 0);
const chLower = chart.addSeries(LightweightCharts.LineSeries, { color: '#ffb300', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false }, 0);
const chTrend = chart.addSeries(LightweightCharts.LineSeries, { color: '#ffb300', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dotted, priceLineVisible: false, lastValueVisible: false, title: 'Trend Channel' }, 0);

// Support / Resistance as native price lines
const srLines = [];
(DATA.supports || []).forEach(function(p) {
  srLines.push(haSeries.createPriceLine({ price: p, color: '#66bb6a', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.LargeDashed, axisLabelVisible: true, title: 'Support ' + p }));
});
(DATA.resistances || []).forEach(function(p) {
  srLines.push(haSeries.createPriceLine({ price: p, color: '#ff7043', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.LargeDashed, axisLabelVisible: true, title: 'Resistance ' + p }));
});

// ---------- Pane 1: RSI (TradingView-style) ----------
// Daily RSI as a Baseline series: shaded green above 50 (bullish momentum),
// shaded red below 50 (bearish momentum) - matches TradingView's own RSI
// indicator far better than a plain line.
const rsiDaily = chart.addSeries(LightweightCharts.BaselineSeries, {
  baseValue: { type: 'price', price: 50 },
  topLineColor: '#26a69a', topFillColor1: 'rgba(38,166,154,0.28)', topFillColor2: 'rgba(38,166,154,0.05)',
  bottomLineColor: '#ef5350', bottomFillColor1: 'rgba(239,83,80,0.05)', bottomFillColor2: 'rgba(239,83,80,0.28)',
  lineWidth: 2, title: 'RSI Daily',
  autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
}, 1);
const rsiDailySma14 = chart.addSeries(LightweightCharts.LineSeries, { color: '#fdd835', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, title: 'RSI Daily SMA14' }, 1);
const rsiDailySma34 = chart.addSeries(LightweightCharts.LineSeries, { color: '#ffffff', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, priceLineVisible: false, lastValueVisible: false, title: 'RSI Daily SMA34' }, 1);
const rsiWeekly = chart.addSeries(LightweightCharts.LineSeries, { color: 'rgba(255,152,0,0.8)', lineWidth: 1, title: 'RSI Weekly' }, 1);
const rsiWeeklySma = chart.addSeries(LightweightCharts.LineSeries, { color: 'rgba(255,152,0,0.5)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, title: 'RSI Weekly SMA14' }, 1);
const rsiMonthly = chart.addSeries(LightweightCharts.LineSeries, { color: 'rgba(224,64,251,0.8)', lineWidth: 1, title: 'RSI Monthly' }, 1);
const rsiMonthlySma = chart.addSeries(LightweightCharts.LineSeries, { color: 'rgba(224,64,251,0.5)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, title: 'RSI Monthly SMA14' }, 1);

// Reference bands: standard 70/30 overbought/oversold, plus 60/40 (mid-trend
// confirmation zone) and a 50 midline - all with visible axis labels.
rsiDaily.createPriceLine({ price: 70, color: '#ef5350', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'Overbought 70' });
rsiDaily.createPriceLine({ price: 60, color: '#9e9e9e', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: '60' });
rsiDaily.createPriceLine({ price: 50, color: '#757575', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: '50' });
rsiDaily.createPriceLine({ price: 40, color: '#9e9e9e', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: '40' });
rsiDaily.createPriceLine({ price: 30, color: '#26a69a', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'Oversold 30' });

// ---------- Pane 2: MACD ----------
const macdHist = chart.addSeries(LightweightCharts.HistogramSeries, { title: 'MACD Hist' }, 2);
const macdLine = chart.addSeries(LightweightCharts.LineSeries, { color: '#42a5f5', lineWidth: 1, title: 'MACD' }, 2);
const macdSignal = chart.addSeries(LightweightCharts.LineSeries, { color: '#ff7043', lineWidth: 1, title: 'Signal' }, 2);

// Pane sizing - proportional to the ACTUAL container height (which now fills
// the real browser viewport) rather than fixed pixels, so RSI/MACD always
// get proper visible space without needing manual drag-resize or scrolling.
function resizePanes() {
  const total = container.clientHeight;
  const panes = chart.panes();
  if (panes[0]) panes[0].setHeight(Math.round(total * 0.48));
  if (panes[1]) panes[1].setHeight(Math.round(total * 0.30));
  if (panes[2]) panes[2].setHeight(Math.round(total * 0.22));
}
resizePanes();

// ---------- Static (always-visible) series data ----------
rsiDaily.setData(DATA.rsi_daily);
rsiDailySma14.setData(DATA.rsi_daily_sma14);
rsiDailySma34.setData(DATA.rsi_daily_sma34);
rsiWeekly.setData(DATA.rsi_weekly);
rsiWeeklySma.setData(DATA.rsi_weekly_sma);
rsiMonthly.setData(DATA.rsi_monthly);
rsiMonthlySma.setData(DATA.rsi_monthly_sma);
macdHist.setData(DATA.macd_hist);
macdLine.setData(DATA.macd);
macdSignal.setData(DATA.macd_signal);
regularSeries.setData(DATA.daily.regular);

// ---------- Markers (attached to the HA candle series) ----------
let allMarkers = [].concat(DATA.buys, DATA.sells, DATA.strong)
  .sort(function(a, b) { return a.time.localeCompare(b.time); });
const markersPrimitive = LightweightCharts.createSeriesMarkers(haSeries, allMarkers);

// ---------- RSI Price-Projection Table (per timeframe) ----------
const rsiTableEl = document.getElementById('rsiTable');
let rsiTableVisible = false;
function renderRsiTable() {
  if (!rsiTableVisible) return;
  const t = DATA[currentTf].rsi_table;
  if (!t) { rsiTableEl.innerHTML = '<div class="hdr">No RSI table (insufficient history)</div>'; return; }
  let rows = '<tr class="hdr"><td colspan="2">' + currentTf.charAt(0).toUpperCase() + currentTf.slice(1) + ' RSI(14)</td></tr>';
  rows += '<tr><td>Current</td><td class="prc">' + (t.rsi_current ?? '-') + '</td></tr>';
  rows += '<tr><td>Max (if High)</td><td class="prc">' + (t.rsi_max ?? '-') + '</td></tr>';
  rows += '<tr><td>Min (if Low)</td><td class="prc">' + (t.rsi_min ?? '-') + '</td></tr>';
  rows += '<tr class="hdr"><td>RSI Target</td><td>Projected Price</td></tr>';
  (t.targets || []).forEach(function(row) {
    rows += '<tr><td class="tgt">' + row.rsi + '</td><td class="prc">' + (row.price ?? 'N/A') + '</td></tr>';
  });
  rsiTableEl.innerHTML = '<table>' + rows + '</table>';
}
document.getElementById('rsiTableBtn').addEventListener('click', function() {
  rsiTableVisible = !rsiTableVisible;
  rsiTableEl.style.display = rsiTableVisible ? 'block' : 'none';
  this.classList.toggle('active', rsiTableVisible);
  renderRsiTable();
});

// ---------- Fibonacci retracement / extension levels (per timeframe) ----------
let fibVisible = false;
let fibLines = [];
const FIB_COLORS = { 0: '#787b86', 0.236: '#f23645', 0.382: '#ff9800', 0.5: '#fdd835',
                      0.618: '#4caf50', 0.786: '#26c6da', 1: '#787b86', 1.272: '#ab47bc',
                      1.618: '#e040fb', 2.618: '#ff5252' };
function clearFibLines() {
  fibLines.forEach(function(l) { haSeries.removePriceLine(l); });
  fibLines = [];
}
function renderFibLines() {
  clearFibLines();
  if (!fibVisible) return;
  const fib = DATA[currentTf].fib || {};
  Object.keys(fib).forEach(function(ratio) {
    const price = fib[ratio];
    const pct = (parseFloat(ratio) * 100).toFixed(1);
    fibLines.push(haSeries.createPriceLine({
      price: price, color: FIB_COLORS[ratio] || '#9e9e9e', lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true,
      title: 'Fib ' + pct + '% ' + price,
    }));
  });
}
document.getElementById('fibBtn').addEventListener('click', function() {
  fibVisible = !fibVisible;
  this.classList.toggle('active', fibVisible);
  renderFibLines();
});

// ---------- Timeframe switching ----------
let currentTf = 'daily';
function loadTimeframe(tf) {
  currentTf = tf;
  const tfData = DATA[tf];
  haSeries.setData(tfData.ha);
  volumeSeries.setData(tfData.volume);
  chTrend.setData(tfData.channel_trend);
  chUpper.setData(tfData.channel_upper);
  chLower.setData(tfData.channel_lower);
  regularSeries.setData(tf === 'daily' ? DATA.daily.regular : []);
  markersPrimitive.setMarkers(tf === 'daily' ? allMarkers : []);
  document.querySelectorAll('.tf-btn').forEach(function(b) { b.classList.toggle('active', b.dataset.tf === tf); });
  if (tf === 'daily') {
    const n = DATA.n_daily_bars;
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 65), to: n + 2 });
  } else {
    chart.timeScale().fitContent();
  }
  renderRsiTable();
  renderFibLines();
}
loadTimeframe('daily');

document.querySelectorAll('.tf-btn').forEach(function(btn) {
  btn.addEventListener('click', function() { loadTimeframe(btn.dataset.tf); });
});

// ---------- Log / Linear price scale ----------
let isLog = false;
document.getElementById('scaleToggle').addEventListener('click', function() {
  isLog = !isLog;
  haSeries.priceScale().applyOptions({ mode: isLog ? LightweightCharts.PriceScaleMode.Logarithmic : LightweightCharts.PriceScaleMode.Normal });
  this.classList.toggle('active', isLog);
});

// ---------- Regular candle overlay toggle ----------
let showRegular = false;
document.getElementById('regularToggle').addEventListener('click', function() {
  showRegular = !showRegular;
  regularSeries.applyOptions({ visible: showRegular });
  this.classList.toggle('active', showRegular);
});

// ---------- RSI declutter toggles ----------
document.getElementById('chkWeekly').addEventListener('change', function(e) {
  rsiWeekly.applyOptions({ visible: e.target.checked });
  rsiWeeklySma.applyOptions({ visible: e.target.checked && document.getElementById('chkSma14').checked });
});
document.getElementById('chkMonthly').addEventListener('change', function(e) {
  rsiMonthly.applyOptions({ visible: e.target.checked });
  rsiMonthlySma.applyOptions({ visible: e.target.checked && document.getElementById('chkSma14').checked });
});
document.getElementById('chkSma14').addEventListener('change', function(e) {
  rsiDailySma14.applyOptions({ visible: e.target.checked });
  rsiWeeklySma.applyOptions({ visible: e.target.checked && document.getElementById('chkWeekly').checked });
  rsiMonthlySma.applyOptions({ visible: e.target.checked && document.getElementById('chkMonthly').checked });
});
document.getElementById('chkSma34').addEventListener('change', function(e) {
  rsiDailySma34.applyOptions({ visible: e.target.checked });
});

// ---------- Zoom in/out and horizontal scroll (+/-, </>) ----------
function zoomBy(factor) {
  const range = chart.timeScale().getVisibleLogicalRange();
  if (!range) return;
  const center = (range.from + range.to) / 2;
  const halfWidth = Math.max(2, (range.to - range.from) / 2 * factor);
  chart.timeScale().setVisibleLogicalRange({ from: center - halfWidth, to: center + halfWidth });
}
function scrollBy(barsDelta) {
  const range = chart.timeScale().getVisibleLogicalRange();
  if (!range) return;
  chart.timeScale().setVisibleLogicalRange({ from: range.from + barsDelta, to: range.to + barsDelta });
}
document.getElementById('zoomInBtn').addEventListener('click', function() { zoomBy(0.8); });
document.getElementById('zoomOutBtn').addEventListener('click', function() { zoomBy(1.25); });
document.getElementById('scrollLeftBtn').addEventListener('click', function() {
  const range = chart.timeScale().getVisibleLogicalRange();
  const step = range ? Math.max(2, Math.round((range.to - range.from) * 0.2)) : 10;
  scrollBy(-step);
});
document.getElementById('scrollRightBtn').addEventListener('click', function() {
  const range = chart.timeScale().getVisibleLogicalRange();
  const step = range ? Math.max(2, Math.round((range.to - range.from) * 0.2)) : 10;
  scrollBy(step);
});

// ---------- Fit all / Fullscreen ----------
document.getElementById('fitBtn').addEventListener('click', function() { chart.timeScale().fitContent(); });
document.getElementById('fsBtn').addEventListener('click', function() {
  if (!document.fullscreenElement) { container.requestFullscreen(); } else { document.exitFullscreen(); }
});

// ---------- Crosshair legend ----------
const legend = document.getElementById('legend');
function fmt(v) { return (v === undefined || v === null || isNaN(v)) ? '-' : Number(v).toFixed(2); }
chart.subscribeCrosshairMove(function(param) {
  if (!param || !param.time) { legend.innerHTML = '<span class="sym">__SYMBOL__</span>'; return; }
  const ha = param.seriesData.get(haSeries);
  const rD = param.seriesData.get(rsiDaily);
  const mL = param.seriesData.get(macdLine);
  let html = '<span class="sym">__SYMBOL__</span> &nbsp; ' + param.time;
  if (ha) {
    const cls = ha.close >= ha.open ? 'up' : 'down';
    html += ' &nbsp; <span class="' + cls + '">O ' + fmt(ha.open) + ' H ' + fmt(ha.high) + ' L ' + fmt(ha.low) + ' C ' + fmt(ha.close) + '</span>';
  }
  if (rD) html += ' &nbsp; RSI ' + fmt(rD.value);
  if (mL) html += ' &nbsp; MACD ' + fmt(mL.value);
  legend.innerHTML = html;
});

window.addEventListener('resize', function() { resizePanes(); chart.timeScale().fitContent(); });
</script>
</body>
</html>
"""
