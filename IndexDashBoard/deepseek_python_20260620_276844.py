import os, re, json, time, warnings, pickle
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import yfinance as yf
from plotly.subplots import make_subplots
import plotly.graph_objects as go
import plotly.io as pio

warnings.filterwarnings("ignore")

# ---------- Configuration ----------
BASE_DIR = r"C:\python\cursorYfinance\newMomentum\30april20262pm\india\NSE"
MASTER_FILE = os.path.join(BASE_DIR, "NSECash", "EQUITY_L.csv")
INDICES_DIR = os.path.join(BASE_DIR, "NseIndice")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Index name -> Yahoo Finance ticker (add your custom mappings here)
INDEX_TICKER_MAP = {
    "nifty_50": "^NSEI",
    "nifty_bank": "^NSEBANK",
    "nifty_privatebank": "NIFTY_PVT_BANK.NS",
    "nifty_psu_bank": "NIFTY_PSU_BANK.NS",
    "nifty_auto": "NIFTY_AUTO.NS",
    "nifty_financial_services": "NIFTY_FIN_SERVICE.NS",
    "nifty_fmcg": "NIFTY_FMCG.NS",
    "nifty_it": "NIFTY_IT.NS",
    "nifty_media": "NIFTY_MEDIA.NS",
    "nifty_metal": "NIFTY_METAL.NS",
    "nifty_pharma": "NIFTY_PHARMA.NS",
    "nifty_realty": "NIFTY_REALTY.NS",
    "nifty_energy": "NIFTY_ENERGY.NS",
    "nifty_infra": "NIFTY_INFRA.NS",
    "nifty_commodities": "NIFTY_COMMODITIES.NS",
    "nifty_consumption": "NIFTY_CONSUMPTION.NS",
    "nifty_midsmall_financial_services": "NIFTY_MIDSMALL_FIN_SERVICE.NS",
}

# ---------- Helper Functions ----------
def parse_index_name(filename):
    """Strip 'Ind_' / 'Ind' prefix and '_list' / 'List' suffix, keep underscores."""
    name = os.path.splitext(filename)[0]
    name = re.sub(r'^Ind_?', '', name, flags=re.IGNORECASE)
    name = re.sub(r'_?[Ll]ist$', '', name)
    return name.lower()

def load_constituents():
    """Read all index files, return {index_name: DataFrame with columns}."""
    indices = {}
    for fname in os.listdir(INDICES_DIR):
        if fname.startswith("Ind") and fname.endswith((".csv", ".CSV")):
            idx_name = parse_index_name(fname)
            df = pd.read_csv(os.path.join(INDICES_DIR, fname))
            df.columns = [c.strip() for c in df.columns]
            # Ensure required columns exist
            if "Symbol" not in df.columns:
                print(f"WARNING: {fname} missing 'Symbol' column, skipping")
                continue
            indices[idx_name] = df
    return indices

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def macd(series, fast, slow, signal):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def sma(series, period):
    return series.rolling(window=period, min_periods=period).mean()

def cross_status(series1, series2):
    """Return last crossover status: bullish, bearish, sideways."""
    if len(series1) < 2:
        return "sideways"
    prev = series1.iloc[-2] - series2.iloc[-2]
    curr = series1.iloc[-1] - series2.iloc[-1]
    if prev <= 0 and curr > 0:
        return "bullish"
    elif prev >= 0 and curr < 0:
        return "bearish"
    else:
        return "sideways"

def resample_ohlc(df, rule):
    """Resample OHLC dataframe to given rule (e.g. 'W', 'M', '4H')."""
    resampled = df.resample(rule).agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna()
    return resampled

def get_cached_data(ticker, interval, period):
    """Cache yfinance downloads to speed up repeated runs."""
    fname = os.path.join(CACHE_DIR, f"{ticker}_{interval}_{period}.pkl")
    if os.path.exists(fname):
        with open(fname, 'rb') as f:
            df = pickle.load(f)
        if not df.empty:
            return df
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        if not df.empty:
            with open(fname, 'wb') as f:
                pickle.dump(df, f)
        return df
    except Exception as e:
        print(f"Error downloading {ticker} ({interval}): {e}")
        return pd.DataFrame()

# ---------- Main Analysis ----------
def analyze_all():
    # 1. Load constituent data
    print("Loading index constituents...")
    index_constituents = load_constituents()
    all_symbols = set()
    for df in index_constituents.values():
        all_symbols.update(df['Symbol'].dropna().unique())
    print(f"Total indices: {len(index_constituents)}, unique stocks: {len(all_symbols)}")

    # 2. Map index names to yahoo tickers
    index_tickers = {}
    for iname in index_constituents:
        ticker = INDEX_TICKER_MAP.get(iname)
        if ticker is None:
            print(f"WARNING: No ticker mapping for {iname}, skipping index.")
            continue
        index_tickers[iname] = ticker
    # Remove indices with no mapping
    index_constituents = {k: v for k, v in index_constituents.items() if k in index_tickers}

    # 3. Download data
    print("Downloading price data (this may take a while)...")
    # Daily data for all stocks + indices (5y)
    all_tickers = [f"{sym}.NS" for sym in all_symbols] + list(index_tickers.values())
    daily_data = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_cached_data, t, "1d", "5y"): t for t in all_tickers}
        for future in as_completed(futures):
            t = futures[future]
            df = future.result()
            if not df.empty:
                daily_data[t] = df
    print(f"Downloaded daily data for {len(daily_data)} tickers")

    # Intraday data for stocks + indices (60d, 15min & 1h)
    intraday_15m = {}
    intraday_1h = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures_15 = {executor.submit(get_cached_data, t, "15m", "60d"): t for t in all_tickers}
        futures_1h = {executor.submit(get_cached_data, t, "1h", "60d"): t for t in all_tickers}
        for future in as_completed(list(futures_15.keys()) + list(futures_1h.keys())):
            t = future.result()
            df = future.result()
            if future in futures_15:
                intraday_15m[t] = df
            else:
                intraday_1h[t] = df
    print("Intraday data downloaded.")

    # 4. Compute all indicators per stock/index
    def compute_ticker_indicators(ticker, daily_df, i15_df, i1h_df):
        res = {}
        if daily_df.empty:
            return res
        close = daily_df['Close']
        # Daily
        res['daily_rsi'] = rsi(close, 14).iloc[-1] if len(close)>=14 else None
        rsi_daily_series = rsi(close, 14)
        sma_rsi_daily = sma(rsi_daily_series, 14)
        if len(sma_rsi_daily.dropna()) >= 2:
            res['daily_rsi_cross'] = cross_status(rsi_daily_series, sma_rsi_daily)
        else:
            res['daily_rsi_cross'] = 'sideways'

        # MACD daily (34,200,9)
        macd1, sig1, hist1 = macd(close, 34, 200, 9)
        if len(macd1.dropna()) >= 2:
            res['daily_macd_34_200_9_cross'] = cross_status(macd1, sig1)
        else:
            res['daily_macd_34_200_9_cross'] = 'sideways'
        # MACD daily (34,1000,9)
        macd2, sig2, hist2 = macd(close, 34, 1000, 9)
        if len(macd2.dropna()) >= 2:
            res['daily_macd_34_1000_9_cross'] = cross_status(macd2, sig2)
        else:
            res['daily_macd_34_1000_9_cross'] = 'sideways'

        # Weekly resample
        if len(daily_df) >= 5:
            weekly = resample_ohlc(daily_df, 'W')
            weekly_close = weekly['Close']
            res['weekly_rsi'] = rsi(weekly_close, 14).iloc[-1] if len(weekly_close)>=14 else None
            rsi_w_series = rsi(weekly_close, 14)
            sma_rsi_w = sma(rsi_w_series, 14)
            if len(sma_rsi_w.dropna()) >= 2:
                res['weekly_rsi_cross'] = cross_status(rsi_w_series, sma_rsi_w)
            else:
                res['weekly_rsi_cross'] = 'sideways'
            macd_w, sig_w, hist_w = macd(weekly_close, 34, 200, 9)
            if len(macd_w.dropna()) >= 2:
                res['weekly_macd_cross'] = cross_status(macd_w, sig_w)
            else:
                res['weekly_macd_cross'] = 'sideways'
        else:
            res['weekly_rsi'] = None; res['weekly_rsi_cross'] = 'sideways'; res['weekly_macd_cross'] = 'sideways'

        # Monthly resample
        if len(daily_df) >= 21:
            monthly = resample_ohlc(daily_df, 'M')
            monthly_close = monthly['Close']
            res['monthly_rsi'] = rsi(monthly_close, 14).iloc[-1] if len(monthly_close)>=14 else None
            rsi_m_series = rsi(monthly_close, 14)
            sma_rsi_m = sma(rsi_m_series, 14)
            if len(sma_rsi_m.dropna()) >= 2:
                res['monthly_rsi_cross'] = cross_status(rsi_m_series, sma_rsi_m)
            else:
                res['monthly_rsi_cross'] = 'sideways'
            macd_m, sig_m, hist_m = macd(monthly_close, 12, 26, 9)
            if len(macd_m.dropna()) >= 2:
                res['monthly_macd_cross'] = cross_status(macd_m, sig_m)
            else:
                res['monthly_macd_cross'] = 'sideways'
        else:
            res['monthly_rsi'] = None; res['monthly_rsi_cross'] = 'sideways'; res['monthly_macd_cross'] = 'sideways'

        # Intraday RSI
        for tf, df in [('15min', i15_df), ('1h', i1h_df)]:
            if not df.empty and len(df) >= 14:
                res[f'{tf}_rsi'] = rsi(df['Close'], 14).iloc[-1]
            else:
                res[f'{tf}_rsi'] = None
        # 4h: resample 1h
        if not i1h_df.empty and len(i1h_df) >= 4:
            try:
                i4h = resample_ohlc(i1h_df, '4H')
                if len(i4h) >= 14:
                    res['4h_rsi'] = rsi(i4h['Close'], 14).iloc[-1]
                else:
                    res['4h_rsi'] = None
            except:
                res['4h_rsi'] = None
        else:
            res['4h_rsi'] = None

        # Performance
        if len(close) >= 2:
            res['daily_perf'] = (close.iloc[-1] / close.iloc[-2] - 1) * 100
        else:
            res['daily_perf'] = None
        if len(close) >= 5:
            res['weekly_perf'] = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close)>=6 else None
        else:
            res['weekly_perf'] = None
        if len(close) >= 21:
            res['monthly_perf'] = (close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close)>=22 else None
        else:
            res['monthly_perf'] = None

        res['last_price'] = close.iloc[-1]
        return res

    # Compute for all tickers
    ticker_indicators = {}
    for t in daily_data:
        ticker_indicators[t] = compute_ticker_indicators(
            t, daily_data[t],
            intraday_15m.get(t, pd.DataFrame()),
            intraday_1h.get(t, pd.DataFrame())
        )

    # 5. Build summary per index and constituents table
    index_summary = []
    constituents_detail = {}  # {index_name: list of constituent records}
    for iname, idx_df in index_constituents.items():
        idx_ticker = index_tickers[iname]
        idx_ind = ticker_indicators.get(idx_ticker, {})
        # Build index summary record
        summary = {"Index": iname.replace('_',' ').title(),
                   "Ticker": idx_ticker}
        summary.update({k: idx_ind.get(k) for k in ['daily_rsi','weekly_rsi','monthly_rsi',
                        'daily_rsi_cross','weekly_rsi_cross','monthly_rsi_cross',
                        'daily_macd_34_200_9_cross','daily_macd_34_1000_9_cross',
                        'weekly_macd_cross','monthly_macd_cross',
                        '15min_rsi','1h_rsi','4h_rsi',
                        'daily_perf','weekly_perf','monthly_perf','last_price']})
        # Prediction score
        score = 0
        for key in ['daily_rsi_cross','weekly_rsi_cross','monthly_rsi_cross',
                    'daily_macd_34_200_9_cross','daily_macd_34_1000_9_cross',
                    'weekly_macd_cross','monthly_macd_cross']:
            val = idx_ind.get(key)
            if val == 'bullish': score += 1
            elif val == 'bearish': score -= 1
        # Add RSI > 50 bonus
        for k in ['daily_rsi','weekly_rsi','monthly_rsi']:
            if idx_ind.get(k) and idx_ind[k] > 50: score += 0.5
        summary['prediction_score'] = score
        index_summary.append(summary)

        # Build constituent details
        const_list = []
        for _, row in idx_df.iterrows():
            sym = row['Symbol']
            tick = f"{sym}.NS"
            ind = ticker_indicators.get(tick, {})
            rec = {
                'Symbol': sym,
                'Company': row.get('Company name', ''),
                'Industry': row.get('Industry', ''),
                'Series': row.get('Series', ''),
                'Last Price': ind.get('last_price'),
                'Daily RSI': ind.get('daily_rsi'),
                'Weekly RSI': ind.get('weekly_rsi'),
                'Monthly RSI': ind.get('monthly_rsi'),
                'D RSI Cross': ind.get('daily_rsi_cross'),
                'W RSI Cross': ind.get('weekly_rsi_cross'),
                'M RSI Cross': ind.get('monthly_rsi_cross'),
                'MACD D(34,200)': ind.get('daily_macd_34_200_9_cross'),
                'MACD D(34,1000)': ind.get('daily_macd_34_1000_9_cross'),
                'MACD W': ind.get('weekly_macd_cross'),
                'MACD M': ind.get('monthly_macd_cross'),
                '15m RSI': ind.get('15min_rsi'),
                '1h RSI': ind.get('1h_rsi'),
                '4h RSI': ind.get('4h_rsi'),
            }
            const_list.append(rec)
        constituents_detail[iname] = const_list

    # 6. Build plotly charts for each index
    charts_json = {}
    for iname, idx_ticker in index_tickers.items():
        df = daily_data.get(idx_ticker)
        if df is None or df.empty:
            continue
        close = df['Close']
        # Create subplot: price, RSI, MACD
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                            vertical_spacing=0.02,
                            row_heights=[0.5, 0.25, 0.25])
        # Candlestick
        fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'],
                                     low=df['Low'], close=df['Close'], name='Price'),
                      row=1, col=1)
        # RSI
        rsi_vals = rsi(close, 14)
        fig.add_trace(go.Scatter(x=df.index, y=rsi_vals, name='RSI(14)', line=dict(color='purple')),
                      row=2, col=1)
        fig.add_hline(y=70, line_dash="dot", row=2, col=1)
        fig.add_hline(y=30, line_dash="dot", row=2, col=1)
        # MACD (34,200,9) for daily
        macd1, sig1, hist1 = macd(close, 34, 200, 9)
        fig.add_trace(go.Scatter(x=df.index, y=macd1, name='MACD', line=dict(color='blue')), row=3, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=sig1, name='Signal', line=dict(color='red')), row=3, col=1)
        fig.add_trace(go.Bar(x=df.index, y=hist1, name='Hist', marker_color='gray'), row=3, col=1)
        fig.update_layout(title=f"{iname.replace('_',' ').title()} - Daily Chart",
                          template='plotly_white', height=600, xaxis_rangeslider_visible=False)
        charts_json[iname] = json.dumps(fig, cls=pio.utils.PlotlyJSONEncoder)

    # 7. Generate HTML
    # Convert summary and details to JSON for embedding
    summary_json = json.dumps(index_summary, default=str)
    detail_json = json.dumps(constituents_detail, default=str)
    charts_json_str = json.dumps(charts_json)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Index Momentum Analysis</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.datatables.net/1.13.4/css/dataTables.bootstrap5.min.css" rel="stylesheet">
    <script src="https://code.jquery.com/jquery-3.6.4.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/jquery.dataTables.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/dataTables.bootstrap5.min.js"></script>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body {{ background: #f8f9fa; }}
        .card {{ margin-bottom: 20px; }}
        .bullish {{ color: green; font-weight: bold; }}
        .bearish {{ color: red; font-weight: bold; }}
        .sideways {{ color: orange; }}
        .table-responsive {{ max-height: 600px; overflow-y: auto; }}
    </style>
</head>
<body>
<div class="container-fluid mt-4">
    <h2 class="text-center mb-4">NSE Indices Multi‑Timeframe Analysis</h2>
    <ul class="nav nav-tabs" id="mainTabs" role="tablist">
        <li class="nav-item"><a class="nav-link active" id="summary-tab" data-bs-toggle="tab" href="#summary" role="tab">Index Summary</a></li>
        <li class="nav-item"><a class="nav-link" id="detail-tab" data-bs-toggle="tab" href="#detail" role="tab">Constituent Details</a></li>
    </ul>
    <div class="tab-content mt-3">
        <div class="tab-pane fade show active" id="summary" role="tabpanel">
            <div class="card">
                <div class="card-body">
                    <table id="summaryTable" class="table table-striped table-bordered" style="width:100%">
                        <thead><tr>
                            <th>Index</th><th>Price</th><th>D%</th><th>W%</th><th>M%</th>
                            <th>D RSI</th><th>W RSI</th><th>M RSI</th>
                            <th>RSI D Cross</th><th>RSI W Cross</th><th>RSI M Cross</th>
                            <th>MACD D(34,200)</th><th>MACD D(34,1000)</th><th>MACD W</th><th>MACD M</th>
                            <th>15m RSI</th><th>1h RSI</th><th>4h RSI</th><th>Score</th>
                        </tr></thead>
                    </table>
                </div>
            </div>
        </div>
        <div class="tab-pane fade" id="detail" role="tabpanel">
            <div class="accordion" id="indexAccordion">
                <!-- dynamically filled -->
            </div>
        </div>
    </div>
</div>

<script>
    const summaryData = {summary_json};
    const detailData = {detail_json};
    const chartsData = {charts_json_str};

    // Summary table
    $(document).ready(function() {{
        const cols = ['Index','last_price','daily_perf','weekly_perf','monthly_perf',
                      'daily_rsi','weekly_rsi','monthly_rsi',
                      'daily_rsi_cross','weekly_rsi_cross','monthly_rsi_cross',
                      'daily_macd_34_200_9_cross','daily_macd_34_1000_9_cross',
                      'weekly_macd_cross','monthly_macd_cross',
                      '15min_rsi','1h_rsi','4h_rsi','prediction_score'];
        const table = $('#summaryTable').DataTable({{
            data: summaryData,
            columns: cols.map(c => {{ data: c, title: c.replace(/_/g,' ') }}),
            columnDefs: [
                {{ targets: [2,3,4], render: v => v ? v.toFixed(2)+'%' : '' }},
                {{ targets: [5,6,7,15,16,17], render: v => v ? v.toFixed(1) : '' }},
                {{ targets: [8,9,10,11,12,13,14], render: function(data, type) {{
                    if (type === 'display') return '<span class="'+data+'">'+data+'</span>';
                    return data;
                }}}},
                {{ targets: 18, render: v => v.toFixed(1) }}
            ],
            order: [[18, 'desc']]
        }});

        // Accordion for each index
        let accordion = '';
        Object.keys(detailData).forEach((iname, i) => {{
            const title = iname.replace(/_/g, ' ');
            const constituents = detailData[iname];
            const tableId = 'table_'+i;
            const chartDiv = 'chart_'+i;
            accordion += `
            <div class="accordion-item">
                <h2 class="accordion-header" id="heading${{i}}">
                    <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#collapse${{i}}">
                        ${{title}} (${{constituents.length}} stocks)
                    </button>
                </h2>
                <div id="collapse${{i}}" class="accordion-collapse collapse" data-bs-parent="#indexAccordion">
                    <div class="accordion-body">
                        <div id="${{chartDiv}}" style="width:100%; height:600px;"></div>
                        <div class="table-responsive mt-3">
                            <table id="${{tableId}}" class="table table-striped table-bordered" style="width:100%">
                                <thead><tr>
                                    <th>Symbol</th><th>Company</th><th>Industry</th><th>Series</th>
                                    <th>Price</th><th>D RSI</th><th>W RSI</th><th>M RSI</th>
                                    <th>D Cross</th><th>W Cross</th><th>M Cross</th>
                                    <th>MACD D1</th><th>MACD D2</th><th>MACD W</th><th>MACD M</th>
                                    <th>15m RSI</th><th>1h RSI</th><th>4h RSI</th>
                                </tr></thead>
                            </table>
                        </div>
                    </div>
                </div>
            </div>`;
        }});
        $('#indexAccordion').html(accordion);

        // Initialize constituent tables and charts when tab shown
        $('#detail-tab').on('shown.bs.tab', function() {{
            Object.keys(detailData).forEach((iname, i) => {{
                const tableId = '#table_'+i;
                if (!$.fn.DataTable.isDataTable(tableId)) {{
                    $(tableId).DataTable({{
                        data: detailData[iname],
                        columns: [
                            {{data:'Symbol'}}, {{data:'Company'}}, {{data:'Industry'}}, {{data:'Series'}},
                            {{data:'Last Price'}},
                            {{data:'Daily RSI', render: v=>v?v.toFixed(1):''}},
                            {{data:'Weekly RSI', render: v=>v?v.toFixed(1):''}},
                            {{data:'Monthly RSI', render: v=>v?v.toFixed(1):''}},
                            {{data:'D RSI Cross', render: d => '<span class="'+d+'">'+d+'</span>'}},
                            {{data:'W RSI Cross', render: d => '<span class="'+d+'">'+d+'</span>'}},
                            {{data:'M RSI Cross', render: d => '<span class="'+d+'">'+d+'</span>'}},
                            {{data:'MACD D(34,200)', render: d => '<span class="'+d+'">'+d+'</span>'}},
                            {{data:'MACD D(34,1000)', render: d => '<span class="'+d+'">'+d+'</span>'}},
                            {{data:'MACD W', render: d => '<span class="'+d+'">'+d+'</span>'}},
                            {{data:'MACD M', render: d => '<span class="'+d+'">'+d+'</span>'}},
                            {{data:'15m RSI', render: v=>v?v.toFixed(1):''}},
                            {{data:'1h RSI', render: v=>v?v.toFixed(1):''}},
                            {{data:'4h RSI', render: v=>v?v.toFixed(1):''}}
                        ]
                    }});
                }}
                const chartDiv = 'chart_'+i;
                if (chartsData[iname] && document.getElementById(chartDiv).innerHTML === '') {{
                    Plotly.newPlot(chartDiv, JSON.parse(chartsData[iname]).data, JSON.parse(chartsData[iname]).layout, {{responsive: true}});
                }}
            }});
        }});
    }});
</script>
</body>
</html>"""

    with open("index_analysis.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Analysis complete. Open index_analysis.html in your browser.")

if __name__ == "__main__":
    analyze_all()