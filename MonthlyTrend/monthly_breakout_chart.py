"""
monthly_breakout_chart.py
=========================
Per-symbol TradingView Lightweight Charts v5 chart for the monthly
MACD(12,26,9) + CCI(20) breakout screener.

Layout (3 panes, white background, light grid):
  Pane 0 : Monthly HA candles + Fib extension levels as horizontal price
            lines + stop-loss line (trend beginning) + regression channel
  Pane 1 : Monthly CCI(20) + SMA20 + overbought/sold bands (+100/0/-100)
  Pane 2 : Monthly MACD(12,26,9) line / signal / histogram

Design:
  - White background, dark text (professional print-ready style)
  - Horizontal price lines: light grey, thin, dashed
  - Fib extensions labeled clearly (1.272x, 1.618x, 2.618x)
  - Stop-loss level: red dashed price line
  - Trend state badge shown in the legend area
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pandas as pd

import indicators as ind

LWC_CDN = "https://unpkg.com/lightweight-charts@5/dist/lightweight-charts.standalone.production.js"


def _fmt(idx) -> str:
    return pd.Timestamp(idx).strftime("%Y-%m-%d")


def _line(series: pd.Series) -> list:
    s = series.dropna()
    return [{"time": _fmt(t), "value": round(float(v), 4)} for t, v in s.items()]


def _candle(o, h, l, c) -> list:
    out = []
    for t in o.index:
        ov, hv, lv, cv = o[t], h[t], l[t], c[t]
        if any(pd.isna(x) for x in [ov, hv, lv, cv]):
            continue
        out.append({"time": _fmt(t),
                    "open": round(float(ov), 4), "high": round(float(hv), 4),
                    "low":  round(float(lv), 4), "close": round(float(cv), 4)})
    return out


def _volume(df: pd.DataFrame) -> list:
    out = []
    for t, row in df.iterrows():
        if pd.isna(row["Volume"]):
            continue
        up = row["Close"] >= row["Open"]
        out.append({"time": _fmt(t), "value": float(row["Volume"]),
                    "color": "rgba(38,166,154,0.5)" if up else "rgba(239,83,80,0.5)"})
    return out


def _quarterly(daily: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to quarterly (calendar quarter-end)."""
    agg = {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
    return daily.resample("QE").agg(agg).dropna(subset=["Close"])


def build_monthly_chart(symbol: str, mo: pd.DataFrame, extras: dict, out_path: Path,
                        daily: pd.DataFrame = None):
    """
    symbol  : NSE ticker
    mo      : monthly OHLCV + indicator DataFrame (from monthly_signals.py)
    extras  : dict returned by build_monthly_signal_table()
    out_path: where to write the HTML file
    """
    # Quarterly data
    qr = _quarterly(daily) if daily is not None and len(daily) > 0 else pd.DataFrame()
    qr_ha = ind.heikin_ashi(qr) if len(qr) > 0 else pd.DataFrame()

    mo_ha = ind.heikin_ashi(mo)

    # Regression channel on monthly close (12-bar lookback)
    m_trend, m_upper, m_lower = ind.regression_channel(mo["Close"], lookback=12)

    # RSI price targets (monthly)
    mo_rsi_targets = ind.rsi_price_targets(mo, length=14)
    mo_rsi_now     = ind.rsi(mo["Close"], 14)
    rsi_current    = float(mo_rsi_now.iloc[-1]) if len(mo_rsi_now) and pd.notna(mo_rsi_now.iloc[-1]) else None

    data = {
        "symbol":       symbol,
        "trend_state":  extras.get("trend_state", ""),
        "stop_loss":    extras.get("stop_loss"),
        "fib_levels":   extras.get("fib_levels", {}),
        "fib_extensions": extras.get("fib_extensions", {}),
        "buy_signal":   extras.get("buy_signal", False),
        "near_buy":     extras.get("near_buy", False),

        # Pane 0
        "ha":           _candle(mo_ha["HA_Open"], mo_ha["HA_High"],
                                mo_ha["HA_Low"],  mo_ha["HA_Close"]),
        "regular":      _candle(mo["Open"], mo["High"], mo["Low"], mo["Close"]),
        "volume":       _volume(mo),
        "ch_trend":     _line(m_trend),
        "ch_upper":     _line(m_upper),
        "ch_lower":     _line(m_lower),

        # Pane 1 - CCI
        "cci20":        _line(mo["CCI20"]),
        "cci20_sma20":  _line(mo["CCI20_SMA20"]),

        # Pane 2 - MACD
        "macd":         _line(mo["MACD"]),
        "macd_signal":  _line(mo["MACD_Signal"]),
        "macd_hist":    [{"time": r["time"], "value": r["value"],
                          "color": "rgba(38,166,154,0.75)" if r["value"] >= 0
                                   else "rgba(239,83,80,0.75)"}
                         for r in _line(mo["MACD_Hist"])],

        # RSI price target table
        "rsi_current":  round(rsi_current, 2) if rsi_current else None,
        "rsi_targets":  [{"rsi": t, "price": round(p, 2) if p else None}
                         for t, p in mo_rsi_targets.items()],

        "n_bars": len(mo),
        # Quarterly data
        "qr":    _candle(qr["Open"], qr["High"], qr["Low"], qr["Close"]) if len(qr) > 0 else [],
        "qr_ha": _candle(qr_ha["HA_Open"], qr_ha["HA_High"], qr_ha["HA_Low"], qr_ha["HA_Close"]) if len(qr_ha) > 0 else [],
        "n_qr":  len(qr),
    }

    html = _TEMPLATE.replace("__SYMBOL__", symbol)\
                    .replace("__DATA_JSON__", json.dumps(data))\
                    .replace("__LWC_CDN__", LWC_CDN)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


# ── HTML template ─────────────────────────────────────────────────────────────
_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>__SYMBOL__ — Monthly Breakout</title>
<script src="__LWC_CDN__"></script>
<style>
  /* ── White background theme ── */
  html, body {
    margin: 0; padding: 0;
    background: #ffffff;
    color: #131722;
    font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
    height: 100%; overflow-x: hidden;
  }
  #toolbar {
    display: flex; gap: 6px; align-items: center;
    padding: 7px 12px; flex-wrap: wrap;
    background: #f8f9fa;
    border-bottom: 1px solid #e0e0e0;
  }
  .tvbtn {
    background: #ffffff; color: #131722;
    border: 1px solid #c8cad0;
    border-radius: 4px; padding: 4px 11px;
    cursor: pointer; font-size: 0.81rem;
    transition: background 0.15s;
  }
  .tvbtn:hover  { background: #e8eaf6; }
  .tvbtn.active { background: #1a73e8; border-color: #1a73e8; color: #fff; }

  /* Trend-state badge */
  #trendBadge {
    padding: 3px 10px; border-radius: 12px;
    font-size: 0.78rem; font-weight: 600;
    margin-left: 8px;
  }
  .badge-none     { background:#f5f5f5; color:#666; }
  .badge-begin    { background:#e3f2fd; color:#1565c0; border:1px solid #90caf9; }
  .badge-medium   { background:#e8f5e9; color:#2e7d32; border:1px solid #a5d6a7; }
  .badge-strong   { background:#fff3e0; color:#e65100; border:1px solid #ffcc80; }

  #chartContainer { position:relative; width:100%; height:calc(100vh - 46px); min-height:580px; }

  /* ── Legend ── */
  #legend {
    position: absolute; top: 8px; left: 10px; z-index: 5;
    font-size: 0.77rem; background: rgba(255,255,255,0.92);
    border: 1px solid #e0e0e0;
    padding: 6px 10px; border-radius: 5px;
    pointer-events: none; line-height: 1.6;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  }
  #legend .sym  { font-weight: 700; color: #131722; }
  #legend .up   { color: #26a69a; } 
  #legend .down { color: #ef5350; }

  /* ── Fib / Stop table ── */
  #fibTable {
    position: absolute; top: 8px; right: 10px; z-index: 6; display: none;
    background: rgba(255,255,255,0.96); border: 1px solid #e0e0e0;
    border-radius: 5px; padding: 8px 12px;
    font-size: 0.75rem; min-width: 200px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
  }
  #fibTable table { border-collapse: collapse; width: 100%; }
  #fibTable td    { padding: 2px 5px; white-space: nowrap; }
  #fibTable .hdr  { font-weight: 600; color: #131722; border-bottom: 1px solid #e0e0e0; padding-bottom: 3px; }
  #fibTable .ext  { color: #7b1fa2; }
  #fibTable .ret  { color: #1565c0; }
  #fibTable .stp  { color: #c62828; font-weight: 600; }
  #fibTable .prc  { text-align: right; font-weight: 500; }
  #fibTable .sub  { color: #888; font-size: 0.7rem; }

  /* ── separator ── */
  .sep { width:1px; background:#e0e0e0; align-self:stretch; margin:0 3px; }
</style>
</head>
<body>

<div id="toolbar">
  <span style="font-weight:700;color:#131722;font-size:0.9rem;">__SYMBOL__</span>
  <span style="font-size:0.78rem;color:#555;">Monthly Breakout</span>
  <span id="trendBadge" class="badge-none">–</span>
  <div class="sep"></div>
  <button class="tvbtn" id="scrollLeftBtn">&#9664;</button>
  <button class="tvbtn" id="zoomOutBtn">&minus;</button>
  <button class="tvbtn" id="zoomInBtn">&plus;</button>
  <button class="tvbtn" id="scrollRightBtn">&#9654;</button>
  <div class="sep"></div>
  <button class="tvbtn" id="haToggle" title="Toggle HA/Regular candles">HA Candles</button>
  <button class="tvbtn" id="tfToggle" title="Switch timeframe">Monthly</button>
  <button class="tvbtn" id="scaleToggle">Log Scale</button>
  <button class="tvbtn" id="fitBtn">Fit All</button>
  <button class="tvbtn" id="fsBtn">Fullscreen</button>
  <div class="sep"></div>
  <button class="tvbtn" id="fibBtn">Fib Levels</button>
  <button class="tvbtn" id="stopBtn">Stop Loss Line</button>
</div>

<div id="chartContainer">
  <div id="legend"><span class="sym">__SYMBOL__</span></div>
  <div id="fibTable"></div>
</div>

<script>
const DATA = __DATA_JSON__;

// ── Trend badge ────────────────────────────────────────────────────────────
(function() {
  var badge = document.getElementById('trendBadge');
  var state = DATA.trend_state || '';
  var cls   = 'badge-none', label = state || 'No Trend';
  if (state === 'Trend Beginning') { cls = 'badge-begin';  }
  else if (state === 'Medium Bullish') { cls = 'badge-medium'; }
  else if (state === 'Strong Bullish') { cls = 'badge-strong'; }
  badge.className = 'tvbtn ' + cls;
  badge.textContent = label;
})();

// ── Chart (white background) ────────────────────────────────────────────────
var container = document.getElementById('chartContainer');
var chart = LightweightCharts.createChart(container, {
  layout: {
    background: { type: 'solid', color: '#ffffff' },
    textColor: '#131722',
    panes: { separatorColor: '#e0e0e0' }
  },
  grid: {
    vertLines: { color: '#f0f0f0', style: LightweightCharts.LineStyle.Dashed },
    horzLines: { color: '#f0f0f0', style: LightweightCharts.LineStyle.Dashed }
  },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#e0e0e0' },
  timeScale: { borderColor: '#e0e0e0', timeVisible: false },
  handleScale: { axisPressedMouseMove: { time: true, price: true }, mouseWheel: true, pinch: true },
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  autoSize: true,
});

// ── Pane 0 : Price (HA candles) ────────────────────────────────────────────
var haSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: '#26a69a', downColor: '#ef5350',
  borderUpColor: '#26a69a', borderDownColor: '#ef5350',
  wickUpColor: '#26a69a', wickDownColor: '#ef5350',
}, 0);

var regularSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: 'rgba(38,166,154,0.35)', downColor: 'rgba(239,83,80,0.35)',
  borderUpColor: 'rgba(38,166,154,0.35)', borderDownColor: 'rgba(239,83,80,0.35)',
  wickUpColor: 'rgba(38,166,154,0.35)', wickDownColor: 'rgba(239,83,80,0.35)',
  visible: false,
}, 0);

// Quarterly HA series (hidden initially)
var qrHaSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: '#1976d2', downColor: '#e53935',
  borderUpColor: '#1976d2', borderDownColor: '#e53935',
  wickUpColor: '#1976d2', wickDownColor: '#e53935',
  visible: false,
}, 0);
var qrSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: 'rgba(25,118,210,0.35)', downColor: 'rgba(229,57,53,0.35)',
  borderUpColor: 'rgba(25,118,210,0.35)', borderDownColor: 'rgba(229,57,53,0.35)',
  wickUpColor: 'rgba(25,118,210,0.35)', wickDownColor: 'rgba(229,57,53,0.35)',
  visible: false,
}, 0);

var volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
  priceFormat: { type: 'volume' }, priceScaleId: 'vol',
}, 0);
volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
haSeries.priceScale().applyOptions({ scaleMargins: { top: 0.04, bottom: 0.22 } });

// Regression channel (light amber lines, thin)
var chUpper = chart.addSeries(LightweightCharts.LineSeries, {
  color: 'rgba(180,120,0,0.45)', lineWidth: 1,
  lineStyle: LightweightCharts.LineStyle.Dashed,
  priceLineVisible: false, lastValueVisible: false,
}, 0);
var chLower = chart.addSeries(LightweightCharts.LineSeries, {
  color: 'rgba(180,120,0,0.45)', lineWidth: 1,
  lineStyle: LightweightCharts.LineStyle.Dashed,
  priceLineVisible: false, lastValueVisible: false,
}, 0);
var chTrend = chart.addSeries(LightweightCharts.LineSeries, {
  color: 'rgba(180,120,0,0.65)', lineWidth: 1,
  lineStyle: LightweightCharts.LineStyle.Dotted,
  priceLineVisible: false, lastValueVisible: false,
  title: 'Monthly Trend',
}, 0);

// ── Pane 1 : CCI(20) ──────────────────────────────────────────────────────
var cciSeries = chart.addSeries(LightweightCharts.BaselineSeries, {
  baseValue: { type: 'price', price: 0 },
  topLineColor: '#26a69a', topFillColor1: 'rgba(38,166,154,0.20)', topFillColor2: 'rgba(38,166,154,0.04)',
  bottomLineColor: '#ef5350', bottomFillColor1: 'rgba(239,83,80,0.04)', bottomFillColor2: 'rgba(239,83,80,0.20)',
  lineWidth: 2, title: 'CCI(20)',
}, 1);
var cciSma = chart.addSeries(LightweightCharts.LineSeries, {
  color: '#e65100', lineWidth: 1,
  lineStyle: LightweightCharts.LineStyle.Dashed,
  priceLineVisible: false, lastValueVisible: true,
  title: 'SMA(20)',
}, 1);

// CCI reference lines — LIGHT grey, thin
cciSeries.createPriceLine({ price:  200, color: 'rgba(150,100,200,0.50)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'Strong Bullish 200' });
cciSeries.createPriceLine({ price:  100, color: 'rgba(100,150,200,0.45)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'Bullish 100' });
cciSeries.createPriceLine({ price:    0, color: 'rgba(100,100,100,0.40)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid,  axisLabelVisible: true, title: 'Zero' });
cciSeries.createPriceLine({ price: -100, color: 'rgba(200,100,100,0.45)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'Bearish -100' });

// ── Pane 2 : MACD(12,26,9) ────────────────────────────────────────────────
var macdHist   = chart.addSeries(LightweightCharts.HistogramSeries, { title: 'MACD Hist', }, 2);
var macdLine   = chart.addSeries(LightweightCharts.LineSeries, {
  color: '#1a73e8', lineWidth: 1, title: 'MACD(12,26,9)',
}, 2);
var macdSignal = chart.addSeries(LightweightCharts.LineSeries, {
  color: '#e65100', lineWidth: 1,
  lineStyle: LightweightCharts.LineStyle.Dashed,
  title: 'Signal(9)',
}, 2);
// Zero line in MACD pane
macdLine.createPriceLine({ price: 0, color: 'rgba(130,130,130,0.4)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: false });

// ── Pane heights ──────────────────────────────────────────────────────────
function resizePanes() {
  var total = container.clientHeight;
  var panes = chart.panes();
  if (panes[0]) panes[0].setHeight(Math.round(total * 0.50));
  if (panes[1]) panes[1].setHeight(Math.round(total * 0.26));
  if (panes[2]) panes[2].setHeight(Math.round(total * 0.24));
}
resizePanes();

// ── Feed data ─────────────────────────────────────────────────────────────
haSeries.setData(DATA.ha);
regularSeries.setData(DATA.regular);
if (DATA.qr_ha && DATA.qr_ha.length) qrHaSeries.setData(DATA.qr_ha);
if (DATA.qr && DATA.qr.length) qrSeries.setData(DATA.qr);
volumeSeries.setData(DATA.volume);
chTrend.setData(DATA.ch_trend);
chUpper.setData(DATA.ch_upper);
chLower.setData(DATA.ch_lower);
cciSeries.setData(DATA.cci20);
cciSma.setData(DATA.cci20_sma20);
macdHist.setData(DATA.macd_hist);
macdLine.setData(DATA.macd);
macdSignal.setData(DATA.macd_signal);

// ── Fibonacci price lines (light, dashed) ─────────────────────────────────
var FIB_COLORS = {
  '0':     'rgba(170,170,170,0.50)',
  '0.236': 'rgba(200,80,80,0.50)',
  '0.382': 'rgba(200,130,30,0.50)',
  '0.5':   'rgba(160,130,0,0.50)',
  '0.618': 'rgba(30,140,80,0.50)',
  '0.786': 'rgba(30,140,140,0.50)',
  '1':     'rgba(80,80,180,0.55)',
  '1.272': 'rgba(120,40,180,0.60)',
  '1.618': 'rgba(160,0,200,0.65)',
  '2.618': 'rgba(200,0,80,0.65)',
};
var FIB_LABELS = {
  '0': '0%', '0.236': '23.6%', '0.382': '38.2%', '0.5': '50%',
  '0.618': '61.8%', '0.786': '78.6%', '1': '100%',
  '1.272': '127.2% Ext', '1.618': '161.8% Ext', '2.618': '261.8% Ext',
};

var fibLines = [];
var fibVisible = false;

function clearFibLines() {
  fibLines.forEach(function(l) { haSeries.removePriceLine(l); });
  fibLines = [];
}

function renderFibLines() {
  clearFibLines();
  if (!fibVisible) return;
  var fib = DATA.fib_levels || {};
  Object.keys(fib).forEach(function(ratio) {
    var price = fib[ratio];
    var label = (FIB_LABELS[ratio] || ratio) + '  ' + price;
    var col   = FIB_COLORS[ratio] || 'rgba(150,150,150,0.40)';
    var lw    = parseFloat(ratio) >= 1.0 ? 1 : 1;
    var ls    = parseFloat(ratio) >= 1.0
                  ? LightweightCharts.LineStyle.Dashed
                  : LightweightCharts.LineStyle.LargeDashed;
    fibLines.push(haSeries.createPriceLine({
      price: price, color: col, lineWidth: lw,
      lineStyle: ls, axisLabelVisible: true,
      title: label,
    }));
  });
}

document.getElementById('fibBtn').addEventListener('click', function() {
  fibVisible = !fibVisible;
  this.classList.toggle('active', fibVisible);
  renderFibLines();
  renderFibTable();
});

// ── Stop-loss price line ──────────────────────────────────────────────────
var stopLine = null;
var stopVisible = false;

function renderStopLine() {
  if (stopLine) { haSeries.removePriceLine(stopLine); stopLine = null; }
  if (!stopVisible || !DATA.stop_loss) return;
  stopLine = haSeries.createPriceLine({
    price: DATA.stop_loss,
    color: 'rgba(198,40,40,0.75)',
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: 'Stop Loss (Trend Begin) ' + DATA.stop_loss,
  });
}
document.getElementById('stopBtn').addEventListener('click', function() {
  stopVisible = !stopVisible;
  this.classList.toggle('active', stopVisible);
  renderStopLine();
  renderFibTable();
});

// ── Fib + Stop table (right-side panel) ──────────────────────────────────
function renderFibTable() {
  var el = document.getElementById('fibTable');
  if (!fibVisible && !stopVisible) { el.style.display = 'none'; return; }
  el.style.display = 'block';

  var rows = '<tr><td colspan="3" class="hdr">Monthly Fibonacci Levels</td></tr>';
  rows += '<tr><td class="sub">Ratio</td><td class="sub">Label</td><td class="sub prc">Price</td></tr>';

  var fib = DATA.fib_levels || {};
  Object.keys(fib).sort(function(a,b){return parseFloat(a)-parseFloat(b);}).forEach(function(ratio) {
    var cls = parseFloat(ratio) >= 1.0 ? 'ext' : 'ret';
    var label = FIB_LABELS[ratio] || ratio;
    rows += '<tr><td class="' + cls + '">' + ratio + '</td><td>' + label + '</td><td class="prc">' + fib[ratio] + '</td></tr>';
  });

  if (DATA.stop_loss) {
    rows += '<tr><td colspan="3" class="hdr" style="padding-top:6px;">Stop Loss</td></tr>';
    rows += '<tr><td class="stp" colspan="2">Trend Beginning Low</td><td class="prc stp">' + DATA.stop_loss + '</td></tr>';
  }

  el.innerHTML = '<table>' + rows + '</table>';
}

// ── Timeframe toggle (Monthly / Quarterly) ───────────────────────────────────
var currentTF = 'monthly';  // 'monthly' | 'quarterly'
document.getElementById('tfToggle').addEventListener('click', function() {
  currentTF = currentTF === 'monthly' ? 'quarterly' : 'monthly';
  this.textContent = currentTF === 'monthly' ? 'Monthly' : 'Quarterly';
  this.classList.toggle('active', currentTF === 'quarterly');
  _applyTFVisibility();
  chart.timeScale().fitContent();
});
function _applyTFVisibility() {
  var isQr = currentTF === 'quarterly';
  var showHA = !showRegular;
  haSeries.applyOptions({ visible: !isQr && showHA });
  regularSeries.applyOptions({ visible: !isQr && !showHA });
  qrHaSeries.applyOptions({ visible: isQr && showHA });
  qrSeries.applyOptions({ visible: isQr && !showHA });
  // RSI title
  var n = isQr ? DATA.n_qr : DATA.n_bars;
  chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - (isQr ? 20 : 36)), to: n + 1 });
}

// ── HA / Regular toggle ───────────────────────────────────────────────────────
var showRegular = false;
document.getElementById('haToggle').addEventListener('click', function() {
  showRegular = !showRegular;
  this.classList.toggle('active', showRegular);
  _applyTFVisibility();
});

// ── Toolbar controls ──────────────────────────────────────────────────────
function zoomBy(f) {
  var r = chart.timeScale().getVisibleLogicalRange();
  if (!r) return;
  var c = (r.from + r.to) / 2;
  var h = Math.max(2, (r.to - r.from) / 2 * f);
  chart.timeScale().setVisibleLogicalRange({ from: c - h, to: c + h });
}
function scrollBy(d) {
  var r = chart.timeScale().getVisibleLogicalRange();
  if (!r) return;
  chart.timeScale().setVisibleLogicalRange({ from: r.from + d, to: r.to + d });
}
document.getElementById('zoomInBtn').addEventListener('click',  function() { zoomBy(0.78); });
document.getElementById('zoomOutBtn').addEventListener('click', function() { zoomBy(1.28); });
document.getElementById('scrollLeftBtn').addEventListener('click', function() {
  var r = chart.timeScale().getVisibleLogicalRange();
  scrollBy(-(r ? Math.max(2, Math.round((r.to-r.from)*0.2)) : 5));
});
document.getElementById('scrollRightBtn').addEventListener('click', function() {
  var r = chart.timeScale().getVisibleLogicalRange();
  scrollBy(r ? Math.max(2, Math.round((r.to-r.from)*0.2)) : 5);
});
document.getElementById('fitBtn').addEventListener('click', function() { chart.timeScale().fitContent(); });
document.getElementById('fsBtn').addEventListener('click', function() {
  if (!document.fullscreenElement) { container.requestFullscreen(); }
  else { document.exitFullscreen(); }
});

var isLog = false;
document.getElementById('scaleToggle').addEventListener('click', function() {
  isLog = !isLog;
  haSeries.priceScale().applyOptions({
    mode: isLog ? LightweightCharts.PriceScaleMode.Logarithmic
                : LightweightCharts.PriceScaleMode.Normal
  });
  this.classList.toggle('active', isLog);
});

// ── Initial view (last 36 monthly bars) ──────────────────────────────────
var n = DATA.n_bars;
chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 36), to: n + 1 });

// ── Crosshair legend ─────────────────────────────────────────────────────
var legend = document.getElementById('legend');
function fmt(v) { return (v == null || isNaN(v)) ? '-' : Number(v).toFixed(2); }

chart.subscribeCrosshairMove(function(param) {
  if (!param || !param.time) {
    legend.innerHTML = '<span class="sym">__SYMBOL__</span>';
    return;
  }
  var ha  = param.seriesData.get(haSeries);
  var cci = param.seriesData.get(cciSeries);
  var ml  = param.seriesData.get(macdLine);
  var mh  = param.seriesData.get(macdHist);

  var html = '<span class="sym">__SYMBOL__</span>&nbsp; ' + param.time;
  if (ha) {
    var cls = ha.close >= ha.open ? 'up' : 'down';
    html += '&nbsp; <span class="' + cls + '">O ' + fmt(ha.open)
          + ' H ' + fmt(ha.high)
          + ' L ' + fmt(ha.low)
          + ' C ' + fmt(ha.close) + '</span>';
  }
  if (cci) html += '&nbsp; CCI ' + fmt(cci.value);
  if (ml)  html += '&nbsp; MACD ' + fmt(ml.value);
  if (mh)  html += '&nbsp; Hist ' + fmt(mh.value);
  legend.innerHTML = html;
});

window.addEventListener('resize', function() { resizePanes(); chart.timeScale().fitContent(); });
</script>
</body>
</html>
"""
