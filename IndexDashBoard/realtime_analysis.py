import os, re, json, sys, time, warnings, pickle, logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import yfinance as yf
from plotly.subplots import make_subplots
import plotly.graph_objects as go
import plotly.io as pio

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger()
# Silence noisy yfinance messages
yf_logger = logging.getLogger("yfinance")
yf_logger.setLevel(logging.WARNING)

# ---------- Helper functions ----------
def parse_index_name(filename):
    name = os.path.splitext(filename)[0]
    name = re.sub(r"^Ind_?", "", name, flags=re.IGNORECASE)
    name = re.sub(r"_?[Ll]ist$", "", name)
    return name.lower()

def load_constituents(indices_dir):
    indices = {}
    abs_dir = os.path.abspath(indices_dir)
    logger.info(f"Looking for index files in: {abs_dir}")
    if not os.path.exists(abs_dir):
        logger.error(f"Directory does NOT exist: {abs_dir}")
        return indices
    all_files = os.listdir(abs_dir)
    # We only pick files that start with "Ind" (case-insensitive) and end with .csv
    matching_files = [f for f in all_files if f.lower().startswith("ind") and f.lower().endswith(".csv")]
    logger.info(f"Matching files: {matching_files}")
    for fname in matching_files:
        idx_name = parse_index_name(fname)
        full_path = os.path.join(abs_dir, fname)
        df = pd.read_csv(full_path)
        df.columns = [c.strip() for c in df.columns]
        if "Symbol" not in df.columns:
            logger.warning(f"File {fname} missing 'Symbol' column, skipping")
            continue
        indices[idx_name] = df
    logger.info(f"Loaded {len(indices)} index constituent files.")
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
    return df.resample(rule).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum"
    }).dropna()

def get_cached_or_fresh(ticker, interval, period, cache_dir, cache_duration_min):
    """Download data with caching. Return empty DataFrame on failure."""
    fname = os.path.join(cache_dir, f"{ticker}_{interval}_{period}.pkl")
    if os.path.exists(fname):
        age = time.time() - os.path.getmtime(fname)
        if age < cache_duration_min * 60:
            try:
                with open(fname, "rb") as f:
                    df = pickle.load(f)
                if not df.empty:
                    return df
            except:
                pass
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        if not df.empty:
            os.makedirs(cache_dir, exist_ok=True)
            with open(fname, "wb") as f:
                pickle.dump(df, f)
        return df
    except Exception:
        # Most failures are missing tickers / no intraday data – ignore
        return pd.DataFrame()

def compute_ticker_indicators(ticker, daily_df, i15_df, i1h_df):
    res = {}
    if daily_df.empty:
        return res
    close = daily_df["Close"]

    # Daily RSI
    res["daily_rsi"] = rsi(close, 14).iloc[-1] if len(close) >= 14 else None
    rsi_d = rsi(close, 14)
    sma_rsi_d = sma(rsi_d, 14)
    res["daily_rsi_cross"] = cross_status(rsi_d, sma_rsi_d) if len(sma_rsi_d.dropna()) >= 2 else "sideways"

    # MACD daily (34,200,9)
    macd1, sig1, _ = macd(close, 34, 200, 9)
    res["daily_macd_34_200_9_cross"] = cross_status(macd1, sig1) if len(macd1.dropna()) >= 2 else "sideways"

    # MACD daily (34,1000,9)
    macd2, sig2, _ = macd(close, 34, 1000, 9)
    res["daily_macd_34_1000_9_cross"] = cross_status(macd2, sig2) if len(macd2.dropna()) >= 2 else "sideways"

    # Weekly
    if len(daily_df) >= 5:
        weekly = resample_ohlc(daily_df, "W")
        wclose = weekly["Close"]
        res["weekly_rsi"] = rsi(wclose, 14).iloc[-1] if len(wclose) >= 14 else None
        rsi_w = rsi(wclose, 14)
        sma_rsi_w = sma(rsi_w, 14)
        res["weekly_rsi_cross"] = cross_status(rsi_w, sma_rsi_w) if len(sma_rsi_w.dropna()) >= 2 else "sideways"
        mw, sw, _ = macd(wclose, 34, 200, 9)
        res["weekly_macd_cross"] = cross_status(mw, sw) if len(mw.dropna()) >= 2 else "sideways"
    else:
        res["weekly_rsi"] = None
        res["weekly_rsi_cross"] = "sideways"
        res["weekly_macd_cross"] = "sideways"

    # Monthly
    if len(daily_df) >= 21:
        monthly = resample_ohlc(daily_df, "M")
        mclose = monthly["Close"]
        res["monthly_rsi"] = rsi(mclose, 14).iloc[-1] if len(mclose) >= 14 else None
        rsi_m = rsi(mclose, 14)
        sma_rsi_m = sma(rsi_m, 14)
        res["monthly_rsi_cross"] = cross_status(rsi_m, sma_rsi_m) if len(sma_rsi_m.dropna()) >= 2 else "sideways"
        mm, sm, _ = macd(mclose, 12, 26, 9)
        res["monthly_macd_cross"] = cross_status(mm, sm) if len(mm.dropna()) >= 2 else "sideways"
    else:
        res["monthly_rsi"] = None
        res["monthly_rsi_cross"] = "sideways"
        res["monthly_macd_cross"] = "sideways"

    # Intraday RSI
    for tf, df in [("15min", i15_df), ("1h", i1h_df)]:
        if not df.empty and len(df) >= 14:
            res[f"{tf}_rsi"] = rsi(df["Close"], 14).iloc[-1]
        else:
            res[f"{tf}_rsi"] = None

    # 4h from 1h
    if not i1h_df.empty and len(i1h_df) >= 4:
        try:
            i4h = resample_ohlc(i1h_df, "4H")
            res["4h_rsi"] = rsi(i4h["Close"], 14).iloc[-1] if len(i4h) >= 14 else None
        except:
            res["4h_rsi"] = None
    else:
        res["4h_rsi"] = None

    # Performance
    if len(close) >= 2:
        res["daily_perf"] = (close.iloc[-1] / close.iloc[-2] - 1) * 100
    else:
        res["daily_perf"] = None
    if len(close) >= 6:
        res["weekly_perf"] = (close.iloc[-1] / close.iloc[-6] - 1) * 100
    else:
        res["weekly_perf"] = None
    if len(close) >= 22:
        res["monthly_perf"] = (close.iloc[-1] / close.iloc[-22] - 1) * 100
    else:
        res["monthly_perf"] = None

    res["last_price"] = close.iloc[-1]
    return res

# ---------- HTML generation ----------
def generate_html(index_summary, constituents_detail, charts_json, refresh_seconds):
    summary_json = json.dumps(index_summary, default=str)
    detail_json = json.dumps(constituents_detail, default=str)
    charts_json_str = json.dumps(charts_json)

    meta_refresh = ""
    if refresh_seconds:
        meta_refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    {meta_refresh}
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Index Momentum Analysis (Auto‑Refresh)</title>
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
    return html

# ---------- Main analysis cycle ----------
def run_analysis_cycle(config):
    base_dir = config["base_dir"]
    indices_dir = os.path.join(base_dir, config["indices_dir"])
    output_html = config.get("output_html", "index_analysis.html")
    cache_dir = os.path.join(base_dir, config.get("cache_dir", "cache"))
    refresh_min = config.get("refresh_interval_minutes", 15)
    cache_dur_min = config.get("cache_duration_minutes", 5)
    ticker_map = config.get("ticker_mapping", {})

    # 1. Load constituents
    index_constituents = load_constituents(indices_dir)
    if not index_constituents:
        logger.error("No index constituents found. Exiting cycle.")
        return

    all_symbols = set()
    for df in index_constituents.values():
        all_symbols.update(df["Symbol"].dropna().unique())
    logger.info(f"Indices: {len(index_constituents)}, stocks: {len(all_symbols)}")

    # 2. Filter indices that have mapping
    valid_indices = {}
    for iname in index_constituents:
        ticker = ticker_map.get(iname)
        if ticker is None:
            logger.warning(f"No ticker mapping for {iname}, skipping")
            continue
        valid_indices[iname] = ticker
    index_constituents = {k: v for k, v in index_constituents.items() if k in valid_indices}

    # 3. Build ticker list (all stocks + index tickers)
    all_tickers = [f"{sym}.NS" for sym in all_symbols] + list(valid_indices.values())

    # ----- Daily data (cached for 24 hours) -----
    logger.info("Downloading daily data...")
    daily_data = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_cached_or_fresh, t, "1d", "5y", cache_dir, 24 * 60): t
            for t in all_tickers
        }
        for future in as_completed(futures):
            t = futures[future]            # <-- CORRECT way to get the ticker
            df = future.result()
            if not df.empty:
                daily_data[t] = df

    # ----- Intraday data (15min & 1h, short cache) -----
    logger.info("Downloading intraday (15m, 1h) data...")
    intraday_15m = {}
    intraday_1h = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        fut_15 = {
            executor.submit(get_cached_or_fresh, t, "15m", "60d", cache_dir, cache_dur_min): t
            for t in all_tickers
        }
        fut_1h = {
            executor.submit(get_cached_or_fresh, t, "1h", "60d", cache_dir, cache_dur_min): t
            for t in all_tickers
        }
        for future in as_completed(list(fut_15.keys()) + list(fut_1h.keys())):
            if future in fut_15:
                t = fut_15[future]          # <-- CORRECT
                df = future.result()
                intraday_15m[t] = df
            else:
                t = fut_1h[future]          # <-- CORRECT
                df = future.result()
                intraday_1h[t] = df

    # 4. Compute indicators for every ticker
    ticker_indicators = {}
    for t in daily_data:
        ticker_indicators[t] = compute_ticker_indicators(
            t,
            daily_data[t],
            intraday_15m.get(t, pd.DataFrame()),
            intraday_1h.get(t, pd.DataFrame()),
        )

    # 5. Build index summary & constituent details
    index_summary = []
    constituents_detail = {}
    charts_json = {}

    for iname, idx_ticker in valid_indices.items():
        idx_ind = ticker_indicators.get(idx_ticker, {})
        summary = {
            "Index": iname.replace("_", " ").title(),
            "Ticker": idx_ticker,
        }
        for key in [
            "daily_rsi",
            "weekly_rsi",
            "monthly_rsi",
            "daily_rsi_cross",
            "weekly_rsi_cross",
            "monthly_rsi_cross",
            "daily_macd_34_200_9_cross",
            "daily_macd_34_1000_9_cross",
            "weekly_macd_cross",
            "monthly_macd_cross",
            "15min_rsi",
            "1h_rsi",
            "4h_rsi",
            "daily_perf",
            "weekly_perf",
            "monthly_perf",
            "last_price",
        ]:
            summary[key] = idx_ind.get(key)

        # Prediction score
        score = 0
        for cross_key in [
            "daily_rsi_cross",
            "weekly_rsi_cross",
            "monthly_rsi_cross",
            "daily_macd_34_200_9_cross",
            "daily_macd_34_1000_9_cross",
            "weekly_macd_cross",
            "monthly_macd_cross",
        ]:
            val = idx_ind.get(cross_key)
            if val == "bullish":
                score += 1
            elif val == "bearish":
                score -= 1
        for rsi_key in ["daily_rsi", "weekly_rsi", "monthly_rsi"]:
            if idx_ind.get(rsi_key) is not None and idx_ind[rsi_key] > 50:
                score += 0.5
        summary["prediction_score"] = score
        index_summary.append(summary)

        # Constituents
        const_list = []
        for _, row in index_constituents[iname].iterrows():
            sym = row["Symbol"]
            tick = f"{sym}.NS"
            ind = ticker_indicators.get(tick, {})
            const_list.append(
                {
                    "Symbol": sym,
                    "Company": row.get("Company name", ""),
                    "Industry": row.get("Industry", ""),
                    "Series": row.get("Series", ""),
                    "Last Price": ind.get("last_price"),
                    "Daily RSI": ind.get("daily_rsi"),
                    "Weekly RSI": ind.get("weekly_rsi"),
                    "Monthly RSI": ind.get("monthly_rsi"),
                    "D RSI Cross": ind.get("daily_rsi_cross"),
                    "W RSI Cross": ind.get("weekly_rsi_cross"),
                    "M RSI Cross": ind.get("monthly_rsi_cross"),
                    "MACD D(34,200)": ind.get("daily_macd_34_200_9_cross"),
                    "MACD D(34,1000)": ind.get("daily_macd_34_1000_9_cross"),
                    "MACD W": ind.get("weekly_macd_cross"),
                    "MACD M": ind.get("monthly_macd_cross"),
                    "15m RSI": ind.get("15min_rsi"),
                    "1h RSI": ind.get("1h_rsi"),
                    "4h RSI": ind.get("4h_rsi"),
                }
            )
        constituents_detail[iname] = const_list

        # Chart for the index
        df = daily_data.get(idx_ticker)
        if df is not None and not df.empty:
            close = df["Close"]
            fig = make_subplots(
                rows=3,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.02,
                row_heights=[0.5, 0.25, 0.25],
            )
            fig.add_trace(
                go.Candlestick(
                    x=df.index,
                    open=df["Open"],
                    high=df["High"],
                    low=df["Low"],
                    close=df["Close"],
                    name="Price",
                ),
                row=1,
                col=1,
            )
            rsi_vals = rsi(close, 14)
            fig.add_trace(
                go.Scatter(x=df.index, y=rsi_vals, name="RSI(14)", line=dict(color="purple")),
                row=2,
                col=1,
            )
            fig.add_hline(y=70, line_dash="dot", row=2, col=1)
            fig.add_hline(y=30, line_dash="dot", row=2, col=1)
            macd1, sig1, hist1 = macd(close, 34, 200, 9)
            fig.add_trace(go.Scatter(x=df.index, y=macd1, name="MACD", line=dict(color="blue")), row=3, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=sig1, name="Signal", line=dict(color="red")), row=3, col=1)
            fig.add_trace(go.Bar(x=df.index, y=hist1, name="Hist", marker_color="gray"), row=3, col=1)
            fig.update_layout(
                title=f"{iname.replace('_',' ').title()} - Daily Chart",
                template="plotly_white",
                height=600,
                xaxis_rangeslider_visible=False,
            )
            charts_json[iname] = json.dumps(fig, cls=pio.utils.PlotlyJSONEncoder)

    # 6. Write HTML (with meta refresh)
    refresh_seconds = refresh_min * 60 + 10
    html_content = generate_html(index_summary, constituents_detail, charts_json, refresh_seconds)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"HTML written to {output_html}. Next refresh in {refresh_min} minutes.")

# ---------- Continuous loop ----------
def main():
    if len(sys.argv) > 1:
        config_file = sys.argv[1]
    else:
        config_file = "config.json"

    if not os.path.exists(config_file):
        logger.error(f"Configuration file {config_file} not found.")
        sys.exit(1)

    with open(config_file, "r") as f:
        config = json.load(f)

    logger.info(f"Starting auto‑refresh analysis every {config.get('refresh_interval_minutes', 15)} minutes.")
    while True:
        start = time.time()
        try:
            run_analysis_cycle(config)
        except Exception as e:
            logger.exception(f"Analysis failed: {e}")
        elapsed = time.time() - start
        sleep_time = max(0, config.get("refresh_interval_minutes", 15) * 60 - elapsed)
        logger.info(f"Sleeping {sleep_time/60:.1f} minutes...")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()