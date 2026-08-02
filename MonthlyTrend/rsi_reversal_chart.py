"""
rsi_reversal_chart.py
=====================
Builds a standalone HTML chart for the RSI Reversal analysis.

Layout:
  Pane 0 : Monthly candlesticks + RSI-level price lines (70/60/50 as
            horizontal support lines with labels)
  Pane 1 : Monthly RSI(14) + overbought zones (75/80/85 bands) +
            scenario projection lines (next 6 bars)
  Right panel (floating) :
            - Current RSI + risk badge
            - Next-bar price targets table (what price → RSI 80/75/70/60/50)
            - Scenario table (months to reach 70/60/50 at -2%/-4%/-7%/flat)
            - Historical episodes summary
"""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd
import indicators as ind
from rsi_reversal import analyze_rsi_reversal

LWC = "https://unpkg.com/lightweight-charts@5/dist/lightweight-charts.standalone.production.js"


def _fmt(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _candle(df: pd.DataFrame) -> list:
    out = []
    for t, row in df.iterrows():
        if any(pd.isna(row[c]) for c in ["Open", "High", "Low", "Close"]):
            continue
        out.append({"time": _fmt(t),
                    "open": round(float(row["Open"]), 2),
                    "high": round(float(row["High"]), 2),
                    "low":  round(float(row["Low"]),  2),
                    "close":round(float(row["Close"]),2)})
    return out


def _line(series: pd.Series, decimals: int = 2) -> list:
    return [{"time": _fmt(t), "value": round(float(v), decimals)}
            for t, v in series.dropna().items()]


def _project_rsi_line(last_date, path: list, monthly_mo: pd.DataFrame) -> list:
    """Convert scenario path [(price, rsi), ...] to RSI time-series for chart."""
    out = []
    date = pd.Timestamp(last_date)
    for _, rsi in path:
        date = date + pd.DateOffset(months=1)
        out.append({"time": date.strftime("%Y-%m-%d"), "value": round(rsi, 2)})
    return out


def build_rsi_reversal_chart(symbol: str, mo: pd.DataFrame,
                              rsi_series: pd.Series, out_path: Path):
    """Build the RSI reversal HTML chart."""
    analysis = analyze_rsi_reversal(symbol, mo, rsi_series)
    if not analysis:
        return

    # Historical RSI series for chart
    rsi_data = _line(rsi_series)

    # RSI SMA(14) for reference
    rsi_sma = ind.sma(rsi_series, 14)
    rsi_sma_data = _line(rsi_sma)

    # Scenario RSI projection lines (from last bar)
    last_date = mo.index[-1]
    proj_lines = {}
    for sc, path in analysis["scenario_paths"].items():
        proj_lines[sc] = _project_rsi_line(last_date, path[:6], mo)

    # Price data
    candles = _candle(mo)

    payload = {
        "symbol":       symbol,
        "candles":      candles,
        "rsi":          rsi_data,
        "rsi_sma":      rsi_sma_data,
        "proj_flat":    proj_lines.get("flat", []),
        "proj_mild":    proj_lines.get("mild_decline", []),
        "proj_mod":     proj_lines.get("mod_decline", []),
        "proj_sharp":   proj_lines.get("sharp_decline", []),
        "analysis":     analysis,
        "n_bars":       len(mo),
    }

    html = _TEMPLATE\
        .replace("__SYMBOL__", symbol)\
        .replace("__DATA__", json.dumps(payload))\
        .replace("__LWC__", LWC)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>__SYMBOL__ — Monthly RSI Reversal Analysis</title>
<script src="__LWC__"></script>
<style>
*,*::before,*::after{box-sizing:border-box;}
html,body{margin:0;padding:0;background:#fff;color:#131722;
  font-family:-apple-system,'Segoe UI',Roboto,sans-serif;height:100%;overflow:hidden;}

/* ── Layout ── */
#wrap{display:flex;height:100vh;width:100vw;}
#chartWrap{flex:1;min-width:0;display:flex;flex-direction:column;}
#toolbar{display:flex;gap:6px;align-items:center;padding:6px 10px;
  background:#f8f9fa;border-bottom:1px solid #e0e0e0;flex-wrap:wrap;}
#chartArea{flex:1;position:relative;}
#sidePanel{width:320px;min-width:280px;overflow-y:auto;
  border-left:1px solid #e0e0e0;padding:14px;background:#fafbfc;}

/* ── Toolbar ── */
.tvbtn{background:#fff;color:#131722;border:1px solid #c8cad0;
  border-radius:4px;padding:3px 10px;cursor:pointer;font-size:0.79rem;}
.tvbtn:hover{background:#e8eaf6;}
.tvbtn.active{background:#1a73e8;border-color:#1a73e8;color:#fff;}
#symLabel{font-weight:700;font-size:0.92rem;}

/* ── Side panel ── */
.sp-section{margin-bottom:16px;}
.sp-title{font-size:0.72rem;font-weight:700;color:#888;text-transform:uppercase;
  letter-spacing:0.5px;border-bottom:1px solid #e8e8e8;padding-bottom:4px;margin-bottom:8px;}
.rsi-badge{display:inline-block;padding:4px 14px;border-radius:20px;
  font-size:1.1rem;font-weight:800;margin-bottom:6px;}
.ob-high  {background:#ffebee;color:#c62828;}
.ob-med   {background:#fff3e0;color:#e65100;}
.ob-low   {background:#e8f5e9;color:#2e7d32;}
.mom-row{font-size:0.78rem;color:#555;margin-bottom:4px;}
.mom-val{font-weight:700;}

/* Target table */
.tbl{width:100%;border-collapse:collapse;font-size:0.79rem;}
.tbl th{background:#f5f7fa;padding:4px 7px;text-align:left;
  font-weight:600;border-bottom:2px solid #e0e0e0;color:#444;font-size:0.74rem;}
.tbl td{padding:4px 7px;border-bottom:1px solid #f0f0f0;}
.tbl tr:hover td{background:#f8f9ff;}
.up  {color:#2e7d32;font-weight:600;}
.dn  {color:#c62828;font-weight:600;}
.neu {color:#555;}
.rsi-lvl{font-weight:700;color:#1565c0;}
.cur-row td{background:#fffde7;font-weight:700;}

/* Scenario table */
.sc-tbl{width:100%;border-collapse:collapse;font-size:0.76rem;}
.sc-tbl th{background:#f5f7fa;padding:4px 5px;text-align:center;
  font-weight:600;border-bottom:2px solid #e0e0e0;font-size:0.72rem;}
.sc-tbl td{padding:3px 5px;text-align:center;border-bottom:1px solid #f0f0f0;}
.sc-nm{text-align:left!important;font-weight:600;color:#444;}
.m-val{color:#6a1b9a;font-weight:600;}
.na{color:#aaa;}

/* History */
.ep-card{background:#fff;border:1px solid #e8e8e8;border-radius:6px;
  padding:7px 9px;margin-bottom:6px;font-size:0.76rem;}
.ep-head{font-weight:700;color:#131722;margin-bottom:3px;}
.ep-row{color:#555;display:flex;justify-content:space-between;margin-bottom:1px;}
.ep-dn{color:#c62828;font-weight:600;}
.ep-up{color:#2e7d32;font-weight:600;}

/* Legend */
#legend{position:absolute;top:6px;left:8px;z-index:5;
  font-size:0.74rem;background:rgba(255,255,255,0.92);
  border:1px solid #e0e0e0;padding:5px 9px;border-radius:5px;
  pointer-events:none;line-height:1.6;box-shadow:0 1px 4px rgba(0,0,0,0.07);}
.lu{color:#26a69a;} .ld{color:#ef5350;}
</style>
</head>
<body>
<div id="wrap">

<!-- Chart side -->
<div id="chartWrap">
  <div id="toolbar">
    <span id="symLabel">__SYMBOL__</span>
    <span style="font-size:0.78rem;color:#666;">Monthly RSI Reversal</span>
    <button class="tvbtn active" id="btnBoth">Price + RSI</button>
    <button class="tvbtn" id="btnPrice">Price Only</button>
    <button class="tvbtn" id="btnRSI">RSI Only</button>
    <button class="tvbtn" id="btnFit">Fit All</button>
    <button class="tvbtn" id="btnLog">Log Scale</button>
    <button class="tvbtn" id="btnFS">⛶ Full</button>
    <button class="tvbtn" id="btnPanel" style="margin-left:auto;">◀ Panel</button>
  </div>
  <div id="chartArea">
    <div id="legend"><span style="font-weight:700;">__SYMBOL__</span></div>
  </div>
</div>

<!-- Side panel -->
<div id="sidePanel">
  <div class="sp-section" id="secCurrent"></div>
  <div class="sp-section" id="secTargets"></div>
  <div class="sp-section" id="secScenarios"></div>
  <div class="sp-section" id="secHistory"></div>
</div>

</div><!-- #wrap -->

<script>
const D = __DATA__;
const A = D.analysis;

// ── Charts ────────────────────────────────────────────────────────────────────
var chartArea = document.getElementById('chartArea');
var chart = LightweightCharts.createChart(chartArea, {
  layout:{background:{type:'solid',color:'#ffffff'},textColor:'#131722'},
  grid:{vertLines:{color:'#f0f0f0',style:LightweightCharts.LineStyle.Dashed},
        horzLines:{color:'#f0f0f0',style:LightweightCharts.LineStyle.Dashed}},
  crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
  rightPriceScale:{borderColor:'#e0e0e0'},
  timeScale:{borderColor:'#e0e0e0',timeVisible:false},
  handleScale:{mouseWheel:true,pinch:true},
  handleScroll:{mouseWheel:true,pressedMouseMove:true},
  autoSize:true,
});

// Pane 0 — Price
var priceSeries = chart.addSeries(LightweightCharts.CandlestickSeries,{
  upColor:'#26a69a',downColor:'#ef5350',
  borderUpColor:'#26a69a',borderDownColor:'#ef5350',
  wickUpColor:'#26a69a',wickDownColor:'#ef5350',
},0);
priceSeries.setData(D.candles);
priceSeries.priceScale().applyOptions({scaleMargins:{top:0.04,bottom:0.06}});

// RSI price-level horizontal lines on price pane
var RSI_LINE_COLORS = {
  70:'rgba(38,166,154,0.65)',60:'rgba(255,152,0,0.65)',50:'rgba(239,83,80,0.65)'
};
var nb = A.next_bar_targets || {};
[70,60,50].forEach(function(lvl){
  var info = nb[lvl];
  if(!info||!info.price) return;
  priceSeries.createPriceLine({
    price:info.price,
    color:RSI_LINE_COLORS[lvl]||'#888',
    lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,
    axisLabelVisible:true,
    title:'RSI'+lvl+' → ₹'+info.price+' ('+info.pct_chg+'%)',
  });
});

// Stop loss — price at RSI 80 (higher level = still overbought)
var nb80 = nb[80];
if(nb80&&nb80.price){
  priceSeries.createPriceLine({
    price:nb80.price,color:'rgba(106,27,154,0.55)',lineWidth:1,
    lineStyle:LightweightCharts.LineStyle.Dotted,
    axisLabelVisible:true,title:'RSI80 → ₹'+nb80.price,
  });
}

// Pane 1 — RSI
var rsiSeries = chart.addSeries(LightweightCharts.LineSeries,{
  color:'#1a73e8',lineWidth:2,title:'RSI(14)',
  priceLineVisible:false,
},1);
var rsiSmaSeries = chart.addSeries(LightweightCharts.LineSeries,{
  color:'rgba(230,81,0,0.7)',lineWidth:1,
  lineStyle:LightweightCharts.LineStyle.Dashed,title:'SMA(14)',
  priceLineVisible:false,lastValueVisible:true,
},1);
rsiSeries.setData(D.rsi);
rsiSmaSeries.setData(D.rsi_sma);

// RSI overbought zones (price lines on RSI pane)
var RSI_ZONES = [{v:85,c:'rgba(198,40,40,0.45)',t:'Extreme OB 85'},
                  {v:80,c:'rgba(230,81,0,0.45)', t:'OB 80'},
                  {v:75,c:'rgba(245,127,23,0.45)',t:'OB 75'},
                  {v:70,c:'rgba(38,166,154,0.45)',t:'OS 70'},
                  {v:60,c:'rgba(255,152,0,0.40)', t:'60'},
                  {v:50,c:'rgba(180,180,180,0.40)',t:'Mid 50'}];
RSI_ZONES.forEach(function(z){
  rsiSeries.createPriceLine({price:z.v,color:z.c,lineWidth:1,
    lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:z.t});
});

// Scenario projection lines on RSI pane
var PROJ_COLORS = {
  flat:'rgba(100,100,100,0.65)',
  mild:'rgba(255,152,0,0.75)',
  mod:'rgba(239,83,80,0.75)',
  sharp:'rgba(198,40,40,0.85)',
};
var PROJ_LABELS = {flat:'Flat',mild:'-2%/mo',mod:'-4%/mo',sharp:'-7%/mo'};
[['flat',D.proj_flat],['mild',D.proj_mild],['mod',D.proj_mod],['sharp',D.proj_sharp]]
  .forEach(function(pair){
    var key=pair[0],data=pair[1];
    if(!data||!data.length) return;
    var s = chart.addSeries(LightweightCharts.LineSeries,{
      color:PROJ_COLORS[key],lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dashed,
      title:PROJ_LABELS[key],priceLineVisible:false,lastValueVisible:true,
    },1);
    s.setData(data);
  });

// Pane heights
function resizePanes(){
  var total = chartArea.clientHeight;
  var panes = chart.panes();
  if(panes[0]) panes[0].setHeight(Math.round(total*0.58));
  if(panes[1]) panes[1].setHeight(Math.round(total*0.42));
}
resizePanes();

// Initial view
var n = D.n_bars;
chart.timeScale().setVisibleLogicalRange({from:Math.max(0,n-36),to:n+7});

// ── Crosshair legend ──────────────────────────────────────────────────────────
var legend = document.getElementById('legend');
chart.subscribeCrosshairMove(function(param){
  if(!param||!param.time){
    legend.innerHTML='<span style="font-weight:700;">__SYMBOL__</span>';return;
  }
  var c=param.seriesData.get(priceSeries);
  var r=param.seriesData.get(rsiSeries);
  var html='<span style="font-weight:700;">__SYMBOL__</span> '+param.time;
  if(c){
    var cls=c.close>=c.open?'lu':'ld';
    html+=' <span class="'+cls+'">O'+c.open+' H'+c.high+' L'+c.low+' C'+c.close+'</span>';
  }
  if(r) html+=' <b>RSI '+r.value+'</b>';
  legend.innerHTML=html;
});

// ── Toolbar ───────────────────────────────────────────────────────────────────
var showPrice=true,showRSI=true;
function setPanes(){
  var ps=chart.panes();
  if(ps[0]) ps[0].setHeight(showPrice&&showRSI?Math.round(chartArea.clientHeight*0.58):
                             showPrice?chartArea.clientHeight:1);
  if(ps[1]) ps[1].setHeight(showRSI&&showPrice?Math.round(chartArea.clientHeight*0.42):
                             showRSI?chartArea.clientHeight:1);
}
document.getElementById('btnBoth').onclick=function(){showPrice=true;showRSI=true;setPanes();this.classList.add('active');document.getElementById('btnPrice').classList.remove('active');document.getElementById('btnRSI').classList.remove('active');};
document.getElementById('btnPrice').onclick=function(){showPrice=true;showRSI=false;setPanes();this.classList.add('active');document.getElementById('btnBoth').classList.remove('active');document.getElementById('btnRSI').classList.remove('active');};
document.getElementById('btnRSI').onclick=function(){showPrice=false;showRSI=true;setPanes();this.classList.add('active');document.getElementById('btnBoth').classList.remove('active');document.getElementById('btnPrice').classList.remove('active');};
document.getElementById('btnFit').onclick=function(){chart.timeScale().fitContent();};
var isLog=false;
document.getElementById('btnLog').onclick=function(){isLog=!isLog;priceSeries.priceScale().applyOptions({mode:isLog?LightweightCharts.PriceScaleMode.Logarithmic:LightweightCharts.PriceScaleMode.Normal});this.classList.toggle('active',isLog);};
document.getElementById('btnFS').onclick=function(){if(!document.fullscreenElement)document.getElementById('wrap').requestFullscreen();else document.exitFullscreen();};
var panelOpen=true;
document.getElementById('btnPanel').onclick=function(){
  panelOpen=!panelOpen;
  document.getElementById('sidePanel').style.display=panelOpen?'':'none';
  this.textContent=panelOpen?'◀ Panel':'▶ Panel';
  chart.timeScale().fitContent();
};

window.addEventListener('resize',function(){resizePanes();});

// ── Side panel rendering ──────────────────────────────────────────────────────
function fmt(v,d){return v!=null?Number(v).toFixed(d!=null?d:2):'–';}
function pctStr(v){if(v==null)return'–';var s=v>=0?'+':'';return s+v+'%';}

// Section 1: Current RSI status
(function(){
  var obCls={'High':'ob-high','Medium':'ob-med','Low':'ob-low'}[A.overbought_risk]||'ob-low';
  var momDir = A.rsi_momentum>0?'▲':'▼';
  var momCls = A.rsi_momentum>0?'up':'dn';
  var accDir = A.rsi_acceleration>0?'↑ accelerating':'↓ decelerating';
  var html='<div class="sp-title">Current RSI Status</div>';
  html+='<div><span class="rsi-badge '+obCls+'">RSI '+A.current_rsi+'</span></div>';
  html+='<div class="mom-row">Price: <span class="mom-val">₹'+A.current_price+'</span></div>';
  html+='<div class="mom-row">Monthly change: <span class="mom-val '+momCls+'">'+momDir+' '+Math.abs(A.rsi_momentum)+'</span></div>';
  html+='<div class="mom-row">Momentum: <span class="mom-val">'+accDir+'</span></div>';
  html+='<div class="mom-row">Overbought risk: <span class="mom-val '+obCls+'">'+A.overbought_risk+'</span></div>';
  document.getElementById('secCurrent').innerHTML=html;
})();

// Section 2: Next-bar price targets
(function(){
  var nb=A.next_bar_targets||{};
  var html='<div class="sp-title">Price Needed for RSI to Hit Level <small style="color:#aaa;">(next month close)</small></div>';
  html+='<table class="tbl"><thead><tr><th>RSI Level</th><th>Price ₹</th><th>Move %</th><th>Direction</th></tr></thead><tbody>';
  [85,80,A.current_rsi,75,70,65,60,50,40].forEach(function(lvl){
    if(lvl===A.current_rsi){
      html+='<tr class="cur-row"><td colspan="4" style="text-align:center;">▶ Current RSI: <b>'+A.current_rsi+'</b> @ ₹'+A.current_price+'</td></tr>';
      return;
    }
    var info=nb[lvl];
    if(!info) return;
    var cls=info.direction==='up'?'up':'dn';
    var arrow=info.direction==='up'?'▲ Rise':'▼ Fall';
    html+='<tr><td class="rsi-lvl">'+lvl+'</td><td>₹'+info.price+'</td><td class="'+cls+'">'+pctStr(info.pct_chg)+'</td><td class="'+cls+'">'+arrow+'</td></tr>';
  });
  html+='</tbody></table>';
  html+='<div style="font-size:0.71rem;color:#aaa;margin-top:5px;">Based on current RMA state. Actual RSI depends on future closes.</div>';
  document.getElementById('secTargets').innerHTML=html;
})();

// Section 3: Scenario projection
(function(){
  var sm=A.scenario_months||{};
  var SC=[['flat','Flat (0%)'],['mild_decline','-2%/month'],['mod_decline','-4%/month'],['sharp_decline','-7%/month']];
  var html='<div class="sp-title">Months Until RSI Drops To… <small style="color:#aaa;">(price scenarios)</small></div>';
  html+='<table class="sc-tbl"><thead><tr><th class="sc-nm">Scenario</th><th>RSI 70</th><th>RSI 60</th><th>RSI 50</th></tr></thead><tbody>';
  SC.forEach(function(sc){
    var key=sc[0],label=sc[1];
    var data=sm[key]||{};
    function cell(v){return v!=null?'<td class="m-val">'+v+'mo</td>':'<td class="na">>24mo</td>';}
    html+='<tr><td class="sc-nm">'+label+'</td>'+cell(data[70])+cell(data[60])+cell(data[50])+'</tr>';
  });
  html+='</tbody></table>';
  // Projected prices at 6 months for each scenario
  var paths=A.scenario_paths||{};
  html+='<div style="font-size:0.72rem;font-weight:700;color:#888;margin:8px 0 4px;">Price & RSI at Month 6</div>';
  html+='<table class="sc-tbl"><thead><tr><th class="sc-nm">Scenario</th><th>Price ₹</th><th>RSI</th></tr></thead><tbody>';
  SC.forEach(function(sc){
    var key=sc[0],label=sc[1];
    var path=paths[key]||[];
    var entry=path[5]||path[path.length-1];
    if(!entry){html+='<tr><td class="sc-nm">'+label+'</td><td class="na">–</td><td class="na">–</td></tr>';return;}
    var rsiCls=entry[1]>=75?'dn':entry[1]>=60?'neu':'up';
    html+='<tr><td class="sc-nm">'+label+'</td><td>₹'+entry[0]+'</td><td class="'+rsiCls+'">'+entry[1]+'</td></tr>';
  });
  html+='</tbody></table>';
  document.getElementById('secScenarios').innerHTML=html;
})();

// Section 4: Historical episodes
(function(){
  var eps=A.historical_episodes||[];
  var html='<div class="sp-title">Past RSI &gt; 75 Episodes ('+eps.length+')</div>';
  if(!eps.length){html+='<div style="font-size:0.77rem;color:#aaa;">No past episodes found.</div>';}
  else{
    // Summary averages
    html+='<div style="font-size:0.77rem;margin-bottom:8px;padding:6px 8px;background:#f5f7fa;border-radius:6px;">';
    html+='Avg peak RSI: <b>'+(A.avg_peak_rsi||'–')+'</b> &nbsp;|&nbsp; Avg duration: <b>'+(A.avg_duration_months||'–')+'mo</b><br>';
    html+='Avg price Δ to RSI70: <b class="dn">'+(A.avg_drawdown_to_70!=null?A.avg_drawdown_to_70+'%':'–')+'</b> &nbsp;';
    html+='to RSI60: <b class="dn">'+(A.avg_drawdown_to_60!=null?A.avg_drawdown_to_60+'%':'–')+'</b> &nbsp;';
    html+='to RSI50: <b class="dn">'+(A.avg_drawdown_to_50!=null?A.avg_drawdown_to_50+'%':'–')+'</b></div>';
    // Individual episodes (last 5)
    eps.slice(-5).reverse().forEach(function(ep){
      html+='<div class="ep-card">';
      html+='<div class="ep-head">'+ep.start+' → '+ep.end+' &nbsp; Peak RSI: <span style="color:#e65100;">'+ep.peak_rsi+'</span></div>';
      html+='<div class="ep-row"><span>Duration</span><span>'+ep.duration_m+' months</span></div>';
      html+='<div class="ep-row"><span>Price @ Peak</span><span>₹'+ep.peak_price+'</span></div>';
      html+='<div class="ep-row"><span>After exit: chg to RSI70</span><span class="'+(ep.price_chg_to_70_pct!=null&&ep.price_chg_to_70_pct<0?'ep-dn':'ep-up')+'">'+(ep.price_chg_to_70_pct!=null?ep.price_chg_to_70_pct+'%':'still above')+'</span></div>';
      html+='<div class="ep-row"><span>After exit: chg to RSI60</span><span class="'+(ep.price_chg_to_60_pct!=null&&ep.price_chg_to_60_pct<0?'ep-dn':'ep-up')+'">'+(ep.price_chg_to_60_pct!=null?ep.price_chg_to_60_pct+'%':'–')+'</span></div>';
      html+='</div>';
    });
  }
  document.getElementById('secHistory').innerHTML=html;
})();
</script>
</body>
</html>
"""
