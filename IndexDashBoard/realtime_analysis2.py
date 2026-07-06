import os, re, json, sys, time, warnings, pickle, logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import yfinance as yf
from plotly.subplots import make_subplots
import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger()
yf_logger = logging.getLogger("yfinance")
yf_logger.setLevel(logging.WARNING)

# ---------- Helpers ----------
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
    matching_files = [f for f in all_files if f.lower().startswith("ind") and f.lower().endswith(".csv")]
    logger.info(f"Matching files: {len(matching_files)}")
    for fname in matching_files:
        idx_name = parse_index_name(fname)
        full_path = os.path.join(abs_dir, fname)
        try:
            df = pd.read_csv(full_path)
            df.columns = [c.strip() for c in df.columns]
            if "Symbol" not in df.columns:
                logger.warning(f"File {fname} missing 'Symbol' column, skipping")
                continue
            indices[idx_name] = df
        except Exception as e:
            logger.warning(f"Error reading {fname}: {e}")
    logger.info(f"Loaded {len(indices)} index constituent files.")
    return indices

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return (100 - (100 / (1 + rs))).squeeze()

def macd(series, fast, slow, signal):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd_line = (exp1 - exp2).squeeze()
    signal_line = macd_line.ewm(span=signal, adjust=False).mean().squeeze()
    histogram = (macd_line - signal_line).squeeze()
    return macd_line, signal_line, histogram

def sma(series, period):
    return series.rolling(window=period, min_periods=period).mean().squeeze()

def _scalar(val):
    if isinstance(val, pd.Series):
        return val.iloc[0]
    return val

def cross_status(series1, series2):
    if len(series1) < 2:
        return "sideways"
    prev = _scalar(series1.iloc[-2]) - _scalar(series2.iloc[-2])
    curr = _scalar(series1.iloc[-1]) - _scalar(series2.iloc[-1])
    if prev <= 0 and curr > 0:
        return "bullish"
    elif prev >= 0 and curr < 0:
        return "bearish"
    else:
        return "sideways"

def ensure_close(df):
    if df.empty:
        return None
    df = df.copy()
    if 'Close' not in df.columns:
        if 'close' in df.columns:
            df.rename(columns={'close': 'Close'}, inplace=True)
        elif 'Adj Close' in df.columns:
            df['Close'] = df['Adj Close']
        else:
            numeric_cols = df.select_dtypes(include=[np.number]).columns
            if len(numeric_cols) > 0:
                df['Close'] = df[numeric_cols[0]]
            else:
                return None
    return df

def resample_ohlc(df, rule):
    """Resample to weekly/monthly etc. using dict aggregation – works with all pandas versions."""
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    # Ensure a 'Close' column exists
    if 'Close' not in df.columns:
        if 'close' in df.columns:
            df.rename(columns={'close': 'Close'}, inplace=True)
        elif 'Adj Close' in df.columns:
            df['Close'] = df['Adj Close']
        else:
            numeric_cols = df.select_dtypes(include=[np.number]).columns
            if len(numeric_cols) > 0:
                df['Close'] = df[numeric_cols[0]]
            else:
                return pd.DataFrame()
    # Fill missing OHLCV columns with Close or 0
    for col, fill_val in [('Open', df['Close']), ('High', df['Close']),
                          ('Low', df['Close']), ('Volume', 0)]:
        if col not in df.columns:
            df[col] = fill_val
    try:
        return df.resample(rule).agg({
            'Open': lambda x: x.iloc[0] if len(x) > 0 else np.nan,
            'High': 'max',
            'Low': 'min',
            'Close': lambda x: x.iloc[-1] if len(x) > 0 else np.nan,
            'Volume': 'sum'
        }).dropna()
    except Exception as e:
        logger.warning(f"Resample failed for rule {rule}: {e}")
        return pd.DataFrame()

def get_cached_or_fresh(ticker, interval, period, cache_dir, cache_duration_min):
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
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            rename_map = {
                'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close',
                'volume': 'Volume', 'adj close': 'Adj Close'
            }
            df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
            os.makedirs(cache_dir, exist_ok=True)
            with open(fname, "wb") as f:
                pickle.dump(df, f)
        return df
    except Exception:
        return pd.DataFrame()

def compute_ticker_indicators(ticker, daily_df, i15_df, i1h_df):
    res = {}
    daily_df = ensure_close(daily_df)
    if daily_df is None or daily_df.empty:
        return res
    close = daily_df["Close"]

    # Daily RSI
    res["daily_rsi"] = rsi(close, 14).iloc[-1] if len(close) >= 14 else None
    rsi_d = rsi(close, 14)
    sma_rsi_d = sma(rsi_d, 14)
    res["daily_rsi_cross"] = cross_status(rsi_d, sma_rsi_d) if len(sma_rsi_d.dropna()) >= 2 else "sideways"

    macd1, sig1, _ = macd(close, 34, 200, 9)
    res["daily_macd_34_200_9_cross"] = cross_status(macd1, sig1) if len(macd1.dropna()) >= 2 else "sideways"

    macd2, sig2, _ = macd(close, 34, 1000, 9)
    res["daily_macd_34_1000_9_cross"] = cross_status(macd2, sig2) if len(macd2.dropna()) >= 2 else "sideways"

    # Weekly (safe)
    try:
        weekly = resample_ohlc(daily_df, "W")
        if not weekly.empty and len(weekly) >= 14:
            wclose = weekly["Close"]
            res["weekly_rsi"] = rsi(wclose, 14).iloc[-1]
            rsi_w = rsi(wclose, 14)
            sma_rsi_w = sma(rsi_w, 14)
            res["weekly_rsi_cross"] = cross_status(rsi_w, sma_rsi_w) if len(sma_rsi_w.dropna()) >= 2 else "sideways"
            mw, sw, _ = macd(wclose, 34, 200, 9)
            res["weekly_macd_cross"] = cross_status(mw, sw) if len(mw.dropna()) >= 2 else "sideways"
        else:
            raise ValueError("Not enough data")
    except Exception:
        res["weekly_rsi"] = None
        res["weekly_rsi_cross"] = "sideways"
        res["weekly_macd_cross"] = "sideways"

    # Monthly (safe)
    try:
        monthly = resample_ohlc(daily_df, "M")
        if not monthly.empty and len(monthly) >= 14:
            mclose = monthly["Close"]
            res["monthly_rsi"] = rsi(mclose, 14).iloc[-1]
            rsi_m = rsi(mclose, 14)
            sma_rsi_m = sma(rsi_m, 14)
            res["monthly_rsi_cross"] = cross_status(rsi_m, sma_rsi_m) if len(sma_rsi_m.dropna()) >= 2 else "sideways"
            mm, sm, _ = macd(mclose, 12, 26, 9)
            res["monthly_macd_cross"] = cross_status(mm, sm) if len(mm.dropna()) >= 2 else "sideways"
        else:
            raise ValueError("Not enough data")
    except Exception:
        res["monthly_rsi"] = None
        res["monthly_rsi_cross"] = "sideways"
        res["monthly_macd_cross"] = "sideways"

    # Intraday RSI (only if data provided)
    for tf, df in [("15min", i15_df), ("1h", i1h_df)]:
        if not df.empty and len(df) >= 14:
            try:
                df_c = ensure_close(df)
                if df_c is not None:
                    res[f"{tf}_rsi"] = rsi(df_c["Close"], 14).iloc[-1]
                else:
                    res[f"{tf}_rsi"] = None
            except:
                res[f"{tf}_rsi"] = None
        else:
            res[f"{tf}_rsi"] = None

    # 4h from 1h
    if not i1h_df.empty and len(i1h_df) >= 4:
        try:
            i4h = resample_ohlc(i1h_df, "4H")
            if not i4h.empty and len(i4h) >= 14:
                res["4h_rsi"] = rsi(i4h["Close"], 14).iloc[-1]
            else:
                res["4h_rsi"] = None
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

def build_synthetic_index(constituent_symbols, daily_data):
    """Create an equal‑weighted average price series from constituents."""
    close_series = {}
    for sym in constituent_symbols:
        tick = f"{sym}.NS"
        if tick in daily_data and not daily_data[tick].empty:
            df = ensure_close(daily_data[tick])
            if df is not None:
                close_series[tick] = df["Close"]
    if not close_series:
        return pd.DataFrame()
    combined = pd.concat(close_series, axis=1, join="inner")
    avg_close = combined.mean(axis=1)
    df = pd.DataFrame({
        "Open": avg_close,
        "High": avg_close,
        "Low": avg_close,
        "Close": avg_close,
        "Volume": 0
    }, index=avg_close.index)
    return df

# ---------- Master Stock List Loader (FIXED) ----------
def load_master_equity(path):
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    logger.info(f"EQUITY_L.csv columns (first 5): {df.columns[:5].tolist()}")
    # Find the symbol column case‑insensitively
    sym_col = None
    for col in df.columns:
        if col.lower() == "symbol":
            sym_col = col
            break
    if sym_col is None:
        # Fallback: assume first column
        sym_col = df.columns[0]
        logger.warning(f"No column named 'symbol' found, using first column: {sym_col}")
    # Rename to standard 'Symbol'
    if sym_col != "Symbol":
        df.rename(columns={sym_col: "Symbol"}, inplace=True)
    return df["Symbol"].dropna().astype(str).unique().tolist()

# ---------- HTML generation (with fallback) ----------
def generate_html(index_summary, constituents_detail, charts_json, refresh_seconds=None):
    summary_json = json.dumps(index_summary, default=str)
    detail_json = json.dumps(constituents_detail, default=str)
    charts_json_str = json.dumps(charts_json)

    meta_refresh = ""
    if refresh_seconds:
        meta_refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">'

    if not index_summary:
        return "<html><body><h2>No index data available.</h2></body></html>"

    # Build a plain HTML fallback table for the summary
    plain_table = "<table border='1'><tr><th>Index</th><th>Type</th><th>Price</th><th>D%</th><th>D RSI</th><th>W RSI</th><th>M RSI</th><th>Score</th></tr>"
    for item in index_summary:
        plain_table += f"<tr><td>{item.get('Index','')}</td><td>{item.get('type','')}</td><td>{item.get('last_price')}</td><td>{item.get('daily_perf')}</td><td>{item.get('daily_rsi')}</td><td>{item.get('weekly_rsi')}</td><td>{item.get('monthly_rsi')}</td><td>{item.get('prediction_score')}</td></tr>"
    plain_table += "</table>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    {meta_refresh}
    <title>Index Momentum Analysis{" (Live)" if refresh_seconds else ""} – {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.datatables.net/1.13.4/css/dataTables.bootstrap5.min.css" rel="stylesheet">
    <script src="https://code.jquery.com/jquery-3.6.4.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/jquery.dataTables.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/dataTables.bootstrap5.min.js"></script>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body {{ background: #f8f9fa; padding: 20px; }}
        .bullish {{ color: green; font-weight: bold; }}
        .bearish {{ color: red; font-weight: bold; }}
        .sideways {{ color: orange; }}
        .synthetic {{ color: #6c757d; font-style: italic; }}
        #fallbackTable {{ display: block; }}
        #dtContainer {{ display: none; }}
    </style>
</head>
<body>
    <h2>NSE Indices Multi‑Timeframe Analysis</h2>
    <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Indices: {len(index_summary)}</p>

    <!-- Fallback plain HTML table (shown if DataTables fails) -->
    <div id="fallbackTable">
        <h3>Index Summary (fallback)</h3>
        {plain_table}
    </div>

    <!-- DataTables container (hidden until successfully initialised) -->
    <div id="dtContainer">
        <ul class="nav nav-tabs" id="mainTabs" role="tablist">
            <li class="nav-item"><a class="nav-link active" id="summary-tab" data-bs-toggle="tab" href="#summary" role="tab">Index Summary</a></li>
            <li class="nav-item"><a class="nav-link" id="detail-tab" data-bs-toggle="tab" href="#detail" role="tab">Constituent Details</a></li>
        </ul>
        <div class="tab-content mt-3">
            <div class="tab-pane fade show active" id="summary" role="tabpanel">
                <table id="summaryTable" class="table table-striped table-bordered" style="width:100%">
                    <thead><tr>
                        <th>Index</th><th>Type</th><th>Price</th><th>D%</th><th>W%</th><th>M%</th>
                        <th>D RSI</th><th>W RSI</th><th>M RSI</th>
                        <th>RSI D Cross</th><th>RSI W Cross</th><th>RSI M Cross</th>
                        <th>MACD D(34,200)</th><th>MACD D(34,1000)</th><th>MACD W</th><th>MACD M</th>
                        <th>15m RSI</th><th>1h RSI</th><th>4h RSI</th><th>Score</th>
                    </tr></thead>
                </table>
            </div>
            <div class="tab-pane fade" id="detail" role="tabpanel">
                <div class="accordion" id="indexAccordion"></div>
            </div>
        </div>
    </div>

    <script>
        const summaryData = {summary_json};
        const detailData = {detail_json};
        const chartsData = {charts_json_str};

        console.log("Summary items:", summaryData.length);
        console.log("Detail indices:", Object.keys(detailData).length);

        try {{
            $(document).ready(function() {{
                if (summaryData.length === 0) {{
                    document.getElementById('fallbackTable').style.display = 'block';
                    return;
                }}

                // Initialise DataTable
                const table = $('#summaryTable').DataTable({{
                    data: summaryData,
                    columns: [
                        {{ data: 'Index' }}, {{ data: 'type' }}, {{ data: 'last_price' }},
                        {{ data: 'daily_perf', render: v => v ? v.toFixed(2)+'%' : '' }},
                        {{ data: 'weekly_perf', render: v => v ? v.toFixed(2)+'%' : '' }},
                        {{ data: 'monthly_perf', render: v => v ? v.toFixed(2)+'%' : '' }},
                        {{ data: 'daily_rsi', render: v => v ? v.toFixed(1) : '' }},
                        {{ data: 'weekly_rsi', render: v => v ? v.toFixed(1) : '' }},
                        {{ data: 'monthly_rsi', render: v => v ? v.toFixed(1) : '' }},
                        {{ data: 'daily_rsi_cross', render: d => '<span class="'+d+'">'+d+'</span>' }},
                        {{ data: 'weekly_rsi_cross', render: d => '<span class="'+d+'">'+d+'</span>' }},
                        {{ data: 'monthly_rsi_cross', render: d => '<span class="'+d+'">'+d+'</span>' }},
                        {{ data: 'daily_macd_34_200_9_cross', render: d => '<span class="'+d+'">'+d+'</span>' }},
                        {{ data: 'daily_macd_34_1000_9_cross', render: d => '<span class="'+d+'">'+d+'</span>' }},
                        {{ data: 'weekly_macd_cross', render: d => '<span class="'+d+'">'+d+'</span>' }},
                        {{ data: 'monthly_macd_cross', render: d => '<span class="'+d+'">'+d+'</span>' }},
                        {{ data: '15min_rsi', render: v => v ? v.toFixed(1) : '' }},
                        {{ data: '1h_rsi', render: v => v ? v.toFixed(1) : '' }},
                        {{ data: '4h_rsi', render: v => v ? v.toFixed(1) : '' }},
                        {{ data: 'prediction_score', render: v => v.toFixed(1) }}
                    ],
                    order: [[18, 'desc']],
                    pageLength: 25
                }});

                // Hide fallback, show DataTables container
                document.getElementById('fallbackTable').style.display = 'none';
                document.getElementById('dtContainer').style.display = 'block';

                // Build accordion for constituent details
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

                // Lazy initialise constituent tables and charts
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
        }} catch(e) {{
            console.error("DataTables initialisation failed:", e);
            document.getElementById('fallbackTable').style.display = 'block';
            document.getElementById('dtContainer').style.display = 'none';
        }}
    </script>
</body>
</html>"""
    return html

# ---------- Analysis cycle ----------
def run_analysis_cycle(config, live=False):
    base_dir = config.get("base_dir", os.path.dirname(os.path.abspath(__file__)))
    indices_dir = os.path.join(base_dir, config.get("indices_dir", "NseIndice"))
    output_html = config.get("output_html", "index_analysis.html")
    cache_dir = os.path.join(base_dir, config.get("cache_dir", "cache"))
    refresh_min = config.get("refresh_interval_minutes", 15)
    cache_dur_min = config.get("cache_duration_minutes", 5) if live else 24*60
    ticker_map = config.get("ticker_mapping", {})

    daily_period = config.get("daily_period", "max")
    intraday_period = config.get("intraday_period", "60d")

    # 1. Load master equity list (if configured)
    master_equity_path = config.get("master_equity_path", os.path.join(base_dir, "NSECash", "EQUITY_L.csv"))
    all_stocks = load_master_equity(master_equity_path) if os.path.exists(master_equity_path) else []
    logger.info(f"Loaded {len(all_stocks)} stocks from master equity list.")

    # 2. Load index constituents from CSV files
    index_constituents = load_constituents(indices_dir)
    all_symbols = set(all_stocks)
    for df in index_constituents.values():
        all_symbols.update(df["Symbol"].dropna().unique())
    logger.info(f"Indices: {len(index_constituents)}, total unique symbols: {len(all_symbols)}")

    # 3. Separate mapped and unmapped indices
    mapped_indices = {}
    unmapped_indices = {}
    for iname, df in index_constituents.items():
        ticker = ticker_map.get(iname)
        if ticker:
            mapped_indices[iname] = ticker
        else:
            unmapped_indices[iname] = df

    all_index_tickers = list(mapped_indices.values())
    all_stock_tickers = [f"{sym}.NS" for sym in all_symbols]
    all_tickers = all_stock_tickers + all_index_tickers
    logger.info(f"Total tickers to download: {len(all_tickers)}")

    # ----- Daily data -----
    logger.info(f"Downloading daily data (period = {daily_period})...")
    daily_data = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_cached_or_fresh, t, "1d", daily_period, cache_dir, 24*60): t
            for t in all_tickers
        }
        for future in as_completed(futures):
            t = futures[future]
            df = future.result()
            if not df.empty:
                daily_data[t] = df
    logger.info(f"Downloaded daily data for {len(daily_data)} tickers.")

    # ----- Intraday data (only if live) -----
    intraday_15m = {}
    intraday_1h = {}
    if live:
        logger.info(f"Downloading intraday data (period = {intraday_period})...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            fut_15 = {
                executor.submit(get_cached_or_fresh, t, "15m", intraday_period, cache_dir, cache_dur_min): t
                for t in all_tickers
            }
            fut_1h = {
                executor.submit(get_cached_or_fresh, t, "1h", intraday_period, cache_dir, cache_dur_min): t
                for t in all_tickers
            }
            for future in as_completed(list(fut_15.keys()) + list(fut_1h.keys())):
                if future in fut_15:
                    t = fut_15[future]
                    df = future.result()
                    intraday_15m[t] = df
                else:
                    t = fut_1h[future]
                    df = future.result()
                    intraday_1h[t] = df

    # ----- Compute indicators for every ticker -----
    ticker_indicators = {}
    for t in all_tickers:
        if t in daily_data:
            ticker_indicators[t] = compute_ticker_indicators(
                t,
                daily_data[t],
                intraday_15m.get(t, pd.DataFrame()),
                intraday_1h.get(t, pd.DataFrame()),
            )

    # ----- Build synthetic indices for unmapped ones -----
    synthetic_indicators = {}
    synthetic_dfs = {}
    for iname, df in unmapped_indices.items():
        symbols = df["Symbol"].dropna().unique().tolist()
        syn_df = build_synthetic_index(symbols, daily_data)
        if syn_df.empty:
            logger.warning(f"Synthetic index for {iname} could not be built (no data).")
            continue
        synthetic_dfs[iname] = syn_df
        ind = compute_ticker_indicators(iname, syn_df, pd.DataFrame(), pd.DataFrame())
        synthetic_indicators[iname] = ind
    logger.info(f"Built {len(synthetic_indicators)} synthetic indices.")

    # ----- Build summary & charts -----
    index_summary = []
    constituents_detail = {}
    charts_json = {}

    def process_index(iname, idx_ticker, indicators, use_type):
        summary = {
            "Index": iname.replace("_", " ").title(),
            "type": use_type,
            "Ticker": idx_ticker if use_type == "actual" else "Synthetic"
        }
        for key in [
            "daily_rsi","weekly_rsi","monthly_rsi",
            "daily_rsi_cross","weekly_rsi_cross","monthly_rsi_cross",
            "daily_macd_34_200_9_cross","daily_macd_34_1000_9_cross",
            "weekly_macd_cross","monthly_macd_cross",
            "15min_rsi","1h_rsi","4h_rsi",
            "daily_perf","weekly_perf","monthly_perf","last_price"
        ]:
            summary[key] = indicators.get(key)

        score = 0
        for cross_key in [
            "daily_rsi_cross","weekly_rsi_cross","monthly_rsi_cross",
            "daily_macd_34_200_9_cross","daily_macd_34_1000_9_cross",
            "weekly_macd_cross","monthly_macd_cross"
        ]:
            val = indicators.get(cross_key)
            if val == "bullish": score += 1
            elif val == "bearish": score -= 1
        for rsi_key in ["daily_rsi","weekly_rsi","monthly_rsi"]:
            if indicators.get(rsi_key) is not None and indicators[rsi_key] > 50:
                score += 0.5
        summary["prediction_score"] = score
        index_summary.append(summary)

        # Constituent details
        const_list = []
        df = index_constituents.get(iname)
        if df is not None:
            for _, row in df.iterrows():
                sym = row["Symbol"]
                tick = f"{sym}.NS"
                ind = ticker_indicators.get(tick, {})
                const_list.append({
                    "Symbol": sym,
                    "Company": row.get("Company name",""),
                    "Industry": row.get("Industry",""),
                    "Series": row.get("Series",""),
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
                    "4h RSI": ind.get("4h_rsi")
                })
        constituents_detail[iname] = const_list

        # Chart
        if use_type == "actual":
            df_chart = daily_data.get(idx_ticker)
        else:
            df_chart = synthetic_dfs.get(iname)
        if df_chart is not None and not df_chart.empty:
            try:
                close = df_chart["Close"]
                fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                                    vertical_spacing=0.02, row_heights=[0.5,0.25,0.25])
                fig.add_trace(go.Candlestick(x=df_chart.index, open=df_chart["Open"],
                                             high=df_chart["High"], low=df_chart["Low"],
                                             close=df_chart["Close"], name="Price"),
                              row=1, col=1)
                rsi_vals = rsi(close, 14)
                fig.add_trace(go.Scatter(x=df_chart.index, y=rsi_vals, name="RSI(14)",
                                         line=dict(color="purple")), row=2, col=1)
                fig.add_hline(y=70, line_dash="dot", row=2, col=1)
                fig.add_hline(y=30, line_dash="dot", row=2, col=1)
                macd1, sig1, hist1 = macd(close, 34, 200, 9)
                fig.add_trace(go.Scatter(x=df_chart.index, y=macd1, name="MACD",
                                         line=dict(color="blue")), row=3, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=sig1, name="Signal",
                                         line=dict(color="red")), row=3, col=1)
                fig.add_trace(go.Bar(x=df_chart.index, y=hist1, name="Hist",
                                     marker_color="gray"), row=3, col=1)
                fig.update_layout(title=f"{iname.replace('_',' ').title()} - {use_type.title()} Index Chart",
                                  template="plotly_white", height=600,
                                  xaxis_rangeslider_visible=False)
                charts_json[iname] = json.dumps(fig, cls=PlotlyJSONEncoder)
            except Exception as e:
                logger.warning(f"Chart creation failed for {iname}: {e}")

    for iname, ticker in mapped_indices.items():
        ind = ticker_indicators.get(ticker, {})
        process_index(iname, ticker, ind, "actual")

    for iname in unmapped_indices:
        ind = synthetic_indicators.get(iname, {})
        process_index(iname, None, ind, "synthetic")

    logger.info(f"Prepared summary for {len(index_summary)} indices.")

    # 6. Write HTML
    refresh_seconds = refresh_min * 60 + 10 if live else None
    html_content = generate_html(index_summary, constituents_detail, charts_json, refresh_seconds)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"HTML written to {output_html}.")

# ---------- Main ----------
def main():
    live_mode = "-live" in sys.argv

    if len(sys.argv) > 1 and sys.argv[1] not in ("-live",):
        config_file = sys.argv[1]
    else:
        config_file = "config.json"

    if not os.path.exists(config_file):
        logger.error(f"Configuration file {config_file} not found.")
        sys.exit(1)

    with open(config_file, "r") as f:
        config = json.load(f)

    if live_mode:
        logger.info("*** LIVE MODE *** Auto‑refresh every {} minutes.".format(
            config.get("refresh_interval_minutes", 15)))
        while True:
            start = time.time()
            try:
                run_analysis_cycle(config, live=True)
            except Exception as e:
                logger.exception(f"Analysis failed: {e}")
            elapsed = time.time() - start
            sleep_time = max(0, config.get("refresh_interval_minutes", 15) * 60 - elapsed)
            logger.info(f"Sleeping {sleep_time/60:.1f} minutes...")
            time.sleep(sleep_time)
    else:
        logger.info("*** DEFAULT MODE *** Running single daily analysis...")
        run_analysis_cycle(config, live=False)
        logger.info("Done. Open index_analysis.html in your browser.")

if __name__ == "__main__":
    main()