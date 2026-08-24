import os, re, json, sys, time, warnings, pickle, logging
import plotly.io as pio
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

# ── PNG export capability check ──
try:
    import kaleido  # noqa: F401
    _HAS_KALEIDO = True
    logger.info("kaleido detected — charts will be saved as PNG images.")
except ImportError:
    _HAS_KALEIDO = False
    logger.info("kaleido not found — charts will be saved as standalone HTML files (install kaleido for PNG).")

# ══════════════════════════════════════════════════════════
# PROFESSIONAL CHART COLOR PALETTE
# High-contrast, harmonious, readable on dark + light bg
# ══════════════════════════════════════════════════════════
C = {
    # Candles / price line
    "bull":       "#00C853",     # vivid green  — bull candles
    "bear":       "#FF1744",     # vivid red    — bear candles
    "line":       "#2196F3",     # clean blue   — line price chart
    "sma34":      "#FF8F00",     # amber        — SMA34 on price
    # RSI lines (purple→blue→red gradient = fast→medium→slow)
    "rsi_d":      "#7B1FA2",     # rich purple  — daily RSI (fastest)
    "rsi_w":      "#1565C0",     # deep blue    — weekly RSI
    "rsi_m":      "#B71C1C",     # deep red     — monthly RSI (slowest)
    "rsi_s14":    "#FFD600",     # gold         — RSI SMA14
    "rsi_s34":    "#FF6D00",     # deep orange  — RSI SMA34
    # Signal markers (size & shape encode strength)
    "sig_sm_b":   "#FFD600",     # gold ▲       — D>SMA34 bull (small)
    "sig_sm_r":   "#FF6D00",     # orange ▼     — D<SMA34 bear (small)
    "sig_md_b":   "#00E676",     # bright green — D>Weekly bull (medium)
    "sig_md_r":   "#FF5252",     # bright red   — D<Weekly bear (medium)
    "sig_lg_b":   "#69F0AE",     # light green  — D>Monthly bull (large)
    "sig_lg_r":   "#EF9A9A",     # light red    — D<Monthly bear (large)
    # Volume Oscillator
    "vo_p":       "#00ACC1",     # cyan         — positive vol osc bars
    "vo_n":       "#FF7043",     # deep orange  — negative vol osc bars
    "vo_sma":     "#455A64",     # blue-grey    — vol osc SMA9 line
    # MACD
    "macd_l":     "#1E88E5",     # blue         — MACD line
    "macd_s":     "#FB8C00",     # orange       — signal line
    "macd_p":     "#26A69A",     # teal         — positive histogram
    "macd_n":     "#EF5350",     # red          — negative histogram
    # RSI reference levels
    "ref_ob":     "rgba(239,83,80,0.50)",    # 70 overbought
    "ref_mid":    "rgba(100,100,100,0.32)",  # 50 midline
    "ref_os":     "rgba(38,166,154,0.50)",   # 30 oversold
    # Backgrounds
    "bg_plot":    "#F8F9FF",     # near-white blue tint plot bg
    "bg_paper":   "#FFFFFF",     # white paper bg
    "grid":       "#E8EAF6",     # soft indigo grid lines
}

# ══════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════

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
    """Extract a plain Python scalar from a pandas Series/DataFrame or numpy scalar."""
    if isinstance(val, pd.DataFrame):
        val = val.iloc[-1, -1] if not val.empty else None
    if isinstance(val, pd.Series):
        val = val.iloc[-1] if not val.empty else None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return val

def _safe_sum(series):
    """Return int count of non-null values — always a Python int, never a Series."""
    try:
        result = series.notna().sum()
        if isinstance(result, (pd.Series, pd.DataFrame)):
            result = result.iloc[0] if not result.empty else 0
        return int(result)
    except Exception:
        return 0

def _safe_bool(val):
    """Convert scalar/Series/DataFrame to Python bool safely."""
    if isinstance(val, (pd.Series, pd.DataFrame)):
        try:
            val = val.iloc[-1] if not val.empty else False
        except Exception:
            return False
    try:
        return bool(val)
    except Exception:
        return False

def _safe_rsi_gt50(val):
    """Return True only when val is a numeric scalar > 50, never raises."""
    try:
        return float(_scalar(val)) > 50
    except (TypeError, ValueError):
        return False

def _get_series(df, col):
    """
    Safely extract a named column as a clean 1-D float64 pandas Series.
    Handles duplicate column names and duplicate index entries.
    Returns zeros Series if column is absent.
    """
    if col not in df.columns:
        return pd.Series(0.0, index=df.index, dtype=float)
    raw = df[col]
    if isinstance(raw, pd.DataFrame):
        raw = raw.iloc[:, -1]
    s = raw.copy()
    try:
        s = s.astype(float)
    except Exception:
        pass
    if s.index.duplicated().any():
        s = s[~s.index.duplicated(keep="last")]
    return s

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
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep="last")]
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep="last")]
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
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep="last")]
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep="last")]
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
    for col, fill_val in [('Open', df['Close']), ('High', df['Close']),
                          ('Low', df['Close']), ('Volume', 0)]:
        if col not in df.columns:
            df[col] = fill_val
    try:
        return df.resample(rule).agg({
            'Open':   lambda x: x.iloc[0]  if len(x) > 0 else np.nan,
            'High':   'max',
            'Low':    'min',
            'Close':  lambda x: x.iloc[-1] if len(x) > 0 else np.nan,
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

    res["daily_rsi"] = rsi(close, 14).iloc[-1] if len(close) >= 14 else None
    rsi_d = rsi(close, 14)
    sma_rsi_d = sma(rsi_d, 14)
    res["daily_rsi_cross"] = cross_status(rsi_d, sma_rsi_d) if len(sma_rsi_d.dropna()) >= 2 else "sideways"

    macd1, sig1, _ = macd(close, 34, 200, 9)
    res["daily_macd_34_200_9_cross"] = cross_status(macd1, sig1) if len(macd1.dropna()) >= 2 else "sideways"

    macd2, sig2, _ = macd(close, 34, 1000, 9)
    res["daily_macd_34_1000_9_cross"] = cross_status(macd2, sig2) if len(macd2.dropna()) >= 2 else "sideways"

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

    try:
        monthly = resample_ohlc(daily_df, "ME")
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
    """Create an equal-weighted average price series from constituents."""
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
        "Open": avg_close, "High": avg_close,
        "Low": avg_close, "Close": avg_close, "Volume": 0
    }, index=avg_close.index)
    return df

def load_master_equity(path):
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    logger.info(f"EQUITY_L.csv columns (first 5): {df.columns[:5].tolist()}")
    sym_col = None
    for col in df.columns:
        if col.lower() == "symbol":
            sym_col = col
            break
    if sym_col is None:
        sym_col = df.columns[0]
        logger.warning(f"No column named 'symbol' found, using first column: {sym_col}")
    if sym_col != "Symbol":
        df.rename(columns={sym_col: "Symbol"}, inplace=True)
    return df["Symbol"].dropna().astype(str).unique().tolist()

# ══════════════════════════════════════════════════════════
# CHART FILE SAVE UTILITY
# ══════════════════════════════════════════════════════════

def _safe_filename(s):
    """Make a string safe for use as a filesystem filename."""
    return re.sub(r'[^\w\-]', '_', str(s)).strip('_')

def _save_chart(fig, out_dir, filename_no_ext, img_width=1800, img_scale=1.5):
    """
    Save a Plotly figure to out_dir.
    Tries PNG (requires kaleido), falls back to standalone HTML with CDN Plotly.
    Returns the relative path string for use in <img src="..."> or <iframe src="...">.
    """
    os.makedirs(out_dir, exist_ok=True)
    dir_name = os.path.basename(out_dir)

    if _HAS_KALEIDO:
        try:
            fname = filename_no_ext + ".png"
            path = os.path.join(out_dir, fname)
            fig.write_image(path, width=img_width, scale=img_scale)
            return (dir_name + "/" + fname).replace("\\", "/")
        except Exception as e:
            logger.warning(f"PNG export failed for {filename_no_ext}: {e}. Falling back to HTML.")

    fname = filename_no_ext + ".html"
    path = os.path.join(out_dir, fname)
    pio.write_html(
        fig, file=path, include_plotlyjs='cdn', full_html=True,
        config={'responsive': True, 'scrollZoom': True, 'displaylogo': False,
                'modeBarButtonsToRemove': ['autoScale2d']}
    )
    return (dir_name + "/" + fname).replace("\\", "/")

# ══════════════════════════════════════════════════════════
# MODULE-LEVEL CHART PANEL HELPERS
# (previously defined as closures inside process_index)
# ══════════════════════════════════════════════════════════

def _vol_oscillator(df_tf, fast=5, slow=20, sig=9):
    """Volume Oscillator = EMA(vol,fast) − EMA(vol,slow); Signal = SMA(VO,sig)."""
    if "Volume" not in df_tf.columns:
        return None, None, None
    vol = _get_series(df_tf, "Volume").replace(0, np.nan).dropna()
    if len(vol) < slow:
        return None, None, None
    ema_fast = vol.ewm(span=fast, adjust=False).mean()
    ema_slow = vol.ewm(span=slow, adjust=False).mean()
    vo = ema_fast - ema_slow
    vo_sma = vo.rolling(sig).mean()
    return vo, vo_sma, vol.reindex(df_tf.index)

def _rsi_crossover_markers(series_a, series_b):
    """Return (bull_x, bull_y, bear_x, bear_y) where series_a crosses series_b."""
    bull_x, bull_y, bear_x, bear_y = [], [], [], []
    common = series_a.index.intersection(series_b.index)
    if len(common) < 2:
        return bull_x, bull_y, bear_x, bear_y
    a = series_a.reindex(common).values
    b = series_b.reindex(common).values
    idx = common
    for k in range(1, len(a)):
        if any(np.isnan(v) for v in [a[k], b[k], a[k-1], b[k-1]]):
            continue
        if a[k-1] <= b[k-1] and a[k] > b[k]:
            bull_x.append(idx[k]); bull_y.append(float(a[k]))
        elif a[k-1] >= b[k-1] and a[k] < b[k]:
            bear_x.append(idx[k]); bear_y.append(float(a[k]))
    return bull_x, bull_y, bear_x, bear_y

def _add_price_panel(fig, df_tf, row, col, use_candle=True, sl=True):
    """Add price + SMA34 traces to a subplot."""
    close = df_tf["Close"]
    has_ohlc = all(c in df_tf.columns for c in ["Open", "High", "Low"])
    if use_candle and has_ohlc:
        fig.add_trace(go.Candlestick(
            x=df_tf.index,
            open=df_tf["Open"], high=df_tf["High"],
            low=df_tf["Low"],   close=close,
            name="Price",
            increasing_line_color=C["bull"],
            decreasing_line_color=C["bear"],
            increasing_fillcolor=C["bull"],
            decreasing_fillcolor=C["bear"],
            showlegend=sl), row=row, col=col)
    else:
        fig.add_trace(go.Scatter(
            x=df_tf.index, y=close, name="Price",
            line=dict(color=C["line"], width=1.8),
            showlegend=sl), row=row, col=col)

    sma34_p = sma(close, 34)
    fig.add_trace(go.Scatter(
        x=df_tf.index, y=sma34_p, name="SMA34",
        line=dict(color=C["sma34"], width=1.4, dash="dash"),
        showlegend=sl), row=row, col=col)

def _add_rsi_panel(fig, df_tf, row, col, is_daily=False,
                   weekly_rsi_ref=None, monthly_rsi_ref=None, sl=True):
    """
    Add RSI panel to a subplot.
    On daily charts: overlays weekly (blue) + monthly (red) RSI and crossover signal markers.
    Returns (weekly_aligned, monthly_aligned) for further use.
    """
    close = df_tf["Close"]
    d_rsi   = rsi(close, 14)
    sma34_r = sma(d_rsi, 34)
    lg  = f"rsi{row}{col}"

    fig.add_trace(go.Scatter(
        x=df_tf.index, y=d_rsi, name="RSI(14)",
        line=dict(color=C["rsi_d"], width=2.2),
        legendgroup=lg, showlegend=sl), row=row, col=col)

    fig.add_trace(go.Scatter(
        x=df_tf.index, y=sma34_r, name="SMA34",
        line=dict(color=C["rsi_s34"], width=1.4, dash="dash"),
        legendgroup=lg, showlegend=sl), row=row, col=col)

    weekly_aligned  = None
    monthly_aligned = None

    if is_daily and weekly_rsi_ref is not None:
        weekly_aligned = weekly_rsi_ref.reindex(df_tf.index, method="ffill")
        fig.add_trace(go.Scatter(
            x=df_tf.index, y=weekly_aligned, name="W RSI",
            line=dict(color=C["rsi_w"], width=1.9),
            legendgroup=lg, showlegend=sl), row=row, col=col)

    if is_daily and monthly_rsi_ref is not None:
        monthly_aligned = monthly_rsi_ref.reindex(df_tf.index, method="ffill")
        fig.add_trace(go.Scatter(
            x=df_tf.index, y=monthly_aligned, name="M RSI",
            line=dict(color=C["rsi_m"], width=1.9, dash="dot"),
            legendgroup=lg, showlegend=sl), row=row, col=col)

    # Reference lines
    fig.add_hline(y=70, line_dash="dot", line_color=C["ref_ob"],  row=row, col=col)
    fig.add_hline(y=50, line_dash="dot", line_color=C["ref_mid"], row=row, col=col)
    fig.add_hline(y=30, line_dash="dot", line_color=C["ref_os"],  row=row, col=col)

    # ── Crossover signal markers ──
    lgs = f"sig{row}{col}"

    # D RSI vs SMA34 — gold triangles (small)
    bx, by, rx, ry = _rsi_crossover_markers(d_rsi, sma34_r)
    if bx:
        fig.add_trace(go.Scatter(x=bx, y=by, mode="markers", name="↑SMA34",
            marker=dict(symbol="triangle-up", color=C["sig_sm_b"],
                        size=10, line=dict(width=1.2, color="#7B5800")),
            legendgroup=lgs, showlegend=sl), row=row, col=col)
    if rx:
        fig.add_trace(go.Scatter(x=rx, y=ry, mode="markers", name="↓SMA34",
            marker=dict(symbol="triangle-down", color=C["sig_sm_r"],
                        size=10, line=dict(width=1.2, color="#7F3500")),
            legendgroup=lgs, showlegend=sl), row=row, col=col)

    if is_daily and weekly_aligned is not None:
        # D vs W RSI — bright circles (medium strength)
        bx2, by2, rx2, ry2 = _rsi_crossover_markers(d_rsi, weekly_aligned)
        if bx2:
            fig.add_trace(go.Scatter(x=bx2, y=by2, mode="markers", name="D>W ▲",
                marker=dict(symbol="circle", color=C["sig_md_b"],
                            size=12, line=dict(width=1.5, color="#004D20")),
                legendgroup=lgs, showlegend=sl), row=row, col=col)
        if rx2:
            fig.add_trace(go.Scatter(x=rx2, y=ry2, mode="markers", name="D<W ▼",
                marker=dict(symbol="circle", color=C["sig_md_r"],
                            size=12, line=dict(width=1.5, color="#7F0000")),
                legendgroup=lgs, showlegend=sl), row=row, col=col)

    if is_daily and monthly_aligned is not None:
        # D vs M RSI — diamonds (strongest signal)
        bx3, by3, rx3, ry3 = _rsi_crossover_markers(d_rsi, monthly_aligned)
        if bx3:
            fig.add_trace(go.Scatter(x=bx3, y=by3, mode="markers", name="D>M ▲▲",
                marker=dict(symbol="diamond", color=C["sig_lg_b"],
                            size=14, line=dict(width=2, color="#004D20")),
                legendgroup=lgs, showlegend=sl), row=row, col=col)
        if rx3:
            fig.add_trace(go.Scatter(x=rx3, y=ry3, mode="markers", name="D<M ▼▼",
                marker=dict(symbol="diamond", color=C["sig_lg_r"],
                            size=14, line=dict(width=2, color="#7F0000")),
                legendgroup=lgs, showlegend=sl), row=row, col=col)

    return weekly_aligned, monthly_aligned

def _add_volosc_panel(fig, df_tf, row, col, sl=True):
    """Add Volume Oscillator panel traces."""
    vo, vo_sma_line, _ = _vol_oscillator(df_tf)
    if vo is not None:
        vo_colors = [C["vo_p"] if v >= 0 else C["vo_n"] for v in vo.fillna(0)]
        fig.add_trace(go.Bar(x=df_tf.index, y=vo, name="Vol Osc",
                             marker_color=vo_colors, opacity=0.80,
                             showlegend=sl), row=row, col=col)
        fig.add_trace(go.Scatter(x=df_tf.index, y=vo_sma_line, name="VO SMA9",
                                 line=dict(color=C["vo_sma"], width=1.3),
                                 showlegend=sl), row=row, col=col)
        fig.add_hline(y=0, line_dash="solid",
                      line_color="rgba(100,100,100,0.3)", row=row, col=col)

def _add_macd_panel(fig, df_tf, row, col, macd_params=(34, 200, 9), sl=True):
    """Add MACD panel traces."""
    close = df_tf["Close"]
    fast, slow_p, sig_p = macd_params
    m_line, m_sig, m_hist = macd(close, fast, slow_p, sig_p)
    hist_cols = [C["macd_p"] if v >= 0 else C["macd_n"] for v in m_hist.fillna(0)]
    fig.add_trace(go.Bar(x=df_tf.index, y=m_hist, name="MACD Hist",
                         marker_color=hist_cols, opacity=0.70,
                         showlegend=sl), row=row, col=col)
    fig.add_trace(go.Scatter(x=df_tf.index, y=m_line,
                             name=f"MACD({fast},{slow_p})",
                             line=dict(color=C["macd_l"], width=1.7),
                             showlegend=sl), row=row, col=col)
    fig.add_trace(go.Scatter(x=df_tf.index, y=m_sig, name="Signal",
                             line=dict(color=C["macd_s"], width=1.3),
                             showlegend=sl), row=row, col=col)
    fig.add_hline(y=0, line_dash="dot",
                  line_color="rgba(100,100,100,0.35)", row=row, col=col)

    # Trend annotation (only on individual charts = row 4, col 1)
    if row == 4 and col == 1:
        try:
            trend = cross_status(m_line, m_sig)
            tcol  = "#00897B" if trend == "bullish" else ("#E53935" if trend == "bearish" else "#FB8C00")
            fig.add_annotation(
                text=f"Trend: {trend.upper()}", xref="paper", yref="paper",
                x=0.01, y=0.01, showarrow=False,
                font=dict(size=12, color=tcol, family="Arial Black"),
                bgcolor="white", bordercolor=tcol, borderwidth=1.5,
                borderpad=4, opacity=0.92)
        except Exception:
            pass

def _apply_chart_style(fig, title_text, height=880):
    """Apply consistent professional styling to any chart figure."""
    fig.update_layout(
        title=dict(text=title_text, font=dict(size=16, color="#1A237E"),
                   x=0.5, xanchor="center"),
        template="plotly_white",
        height=height,
        plot_bgcolor=C["bg_plot"],
        paper_bgcolor=C["bg_paper"],
        legend=dict(
            orientation="h", y=1.03, x=0,
            font=dict(size=10, color="#37474F"),
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#CFD8DC", borderwidth=1,
            tracegroupgap=3
        ),
        margin=dict(t=90, b=35, l=65, r=25),
    )
    fig.update_xaxes(rangeslider_visible=False,
                     showgrid=True, gridcolor=C["grid"], gridwidth=1,
                     zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor=C["grid"], gridwidth=1,
                     zeroline=False)

# ══════════════════════════════════════════════════════════
# INDIVIDUAL TIMEFRAME CHART (4-panel: Price/RSI/VolOsc/MACD)
# ══════════════════════════════════════════════════════════

def _build_individual_chart_fig(df_tf, label, tf_label, use_candle=True,
                                  weekly_rsi_ref=None, monthly_rsi_ref=None,
                                  macd_params=(34, 200, 9)):
    """
    Build a clean 4-row chart for a single timeframe.
    Row 1: Price + SMA34
    Row 2: RSI(14) with SMA34 overlay; on Daily — weekly+monthly RSI overlaid + crossover signals
    Row 3: Volume Oscillator (EMA5−EMA20) + SMA9
    Row 4: MACD
    """
    is_daily = (tf_label == "Daily")
    rsi_title = (
        "RSI(14)  ·  Purple=Daily · Blue=Weekly · Red=Monthly  |  Gold▲▼=SMA34 · Green/Red●=D>W · Diamond=D>M"
        if is_daily else f"RSI(14)  ·  SMA34(orange dashed)  |  Gold▲=Bullish · Orange▼=Bearish"
    )

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=[0.38, 0.26, 0.16, 0.20],
        subplot_titles=(
            f"Price  +  SMA34(amber)",
            rsi_title,
            "Volume Oscillator  (EMA5 − EMA20)  ·  Cyan=Bull · Orange=Bear",
            f"MACD({macd_params[0]},{macd_params[1]},{macd_params[2]})  ·  Blue=MACD · Orange=Signal · Teal/Red=Hist"
        )
    )

    _add_price_panel(fig, df_tf, 1, 1, use_candle=use_candle)
    _add_rsi_panel(fig, df_tf, 2, 1, is_daily=is_daily,
                   weekly_rsi_ref=weekly_rsi_ref, monthly_rsi_ref=monthly_rsi_ref)
    _add_volosc_panel(fig, df_tf, 3, 1)
    _add_macd_panel(fig, df_tf, 4, 1, macd_params=macd_params)

    _apply_chart_style(fig, f"<b>{label}</b>  —  {tf_label} Chart", height=880)
    return fig

# ══════════════════════════════════════════════════════════
# COMBINED DAILY · WEEKLY · MONTHLY CHART  (4 × 3 grid)
# ══════════════════════════════════════════════════════════

def _build_combined_chart_fig(df_daily, df_weekly, df_monthly, label,
                               use_candle=True,
                               weekly_rsi_ref=None, monthly_rsi_ref=None):
    """
    Build a single 4×3 combined chart showing all three timeframes equally split:
      Col 1 = Daily  |  Col 2 = Weekly  |  Col 3 = Monthly
      Row 1 = Price   |  Row 2 = RSI    |  Row 3 = Vol Osc  |  Row 4 = MACD
    Rows within each column share the same x-axis (temporal alignment).
    """
    fig = make_subplots(
        rows=4, cols=3,
        shared_xaxes="columns",   # rows 1-4 in each column share x
        shared_yaxes=False,
        vertical_spacing=0.035,
        horizontal_spacing=0.045,
        row_heights=[0.36, 0.27, 0.15, 0.22],
        subplot_titles=(
            "◀ DAILY — Price + SMA34",      "◀ WEEKLY — Price + SMA34",      "◀ MONTHLY — Price + SMA34",
            "DAILY — RSI  (all timeframes)", "WEEKLY — RSI + SMA34",          "MONTHLY — RSI + SMA34",
            "DAILY — Vol Oscillator",        "WEEKLY — Vol Oscillator",        "MONTHLY — Vol Oscillator",
            "DAILY — MACD(34,200,9)",        "WEEKLY — MACD(34,200,9)",        "MONTHLY — MACD(12,26,9)"
        )
    )

    # Timeframe configs: (dataframe, column_index, is_daily, macd_params, weekly_ref, monthly_ref)
    tfs = [
        (df_daily,   1, True,  (34, 200, 9), weekly_rsi_ref, monthly_rsi_ref),
        (df_weekly,  2, False, (34, 200, 9), None,            None),
        (df_monthly, 3, False, (12, 26,  9), None,            None),
    ]

    for df_tf, col_n, is_d, mp, wref, mref in tfs:
        if df_tf is None or df_tf.empty:
            continue
        # show legend only on first column to avoid duplicates
        sl = (col_n == 1)
        _add_price_panel(fig, df_tf, 1, col_n, use_candle=use_candle, sl=sl)
        _add_rsi_panel(fig, df_tf, 2, col_n, is_daily=is_d,
                       weekly_rsi_ref=wref, monthly_rsi_ref=mref, sl=sl)
        _add_volosc_panel(fig, df_tf, 3, col_n, sl=sl)
        _add_macd_panel(fig, df_tf, 4, col_n, macd_params=mp, sl=sl)

    fig.update_layout(
        title=dict(
            text=f"<b>{label}</b>  —  Daily · Weekly · Monthly  Combined Analysis",
            font=dict(size=17, color="#1A237E"),
            x=0.5, xanchor="center"
        ),
        template="plotly_white",
        height=1050,
        plot_bgcolor=C["bg_plot"],
        paper_bgcolor=C["bg_paper"],
        legend=dict(
            orientation="h", y=1.025, x=0,
            font=dict(size=9, color="#37474F"),
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#CFD8DC", borderwidth=1,
            tracegroupgap=3
        ),
        margin=dict(t=100, b=35, l=65, r=25),
    )
    fig.update_xaxes(rangeslider_visible=False,
                     showgrid=True, gridcolor=C["grid"], gridwidth=1)
    fig.update_yaxes(showgrid=True, gridcolor=C["grid"], gridwidth=1)
    return fig

# ══════════════════════════════════════════════════════════
# TECHNICAL INDICATOR HELPERS
# (df["col"] if "col" in df.columns else df["Close"] — NO df.get())
# ══════════════════════════════════════════════════════════

def calc_atr(df, period=14):
    """Average True Range."""
    high       = df["High"]  if "High"  in df.columns else df["Close"]
    low        = df["Low"]   if "Low"   in df.columns else df["Close"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

def calc_keltner(df, ema_period=20, atr_mult=2.0, atr_period=14):
    """Keltner Channel: mid=EMA(close,20), upper/lower=mid ± 2×ATR(14)."""
    mid   = df["Close"].ewm(span=ema_period, adjust=False).mean()
    atr_v = calc_atr(df, atr_period)
    upper = mid + atr_mult * atr_v
    lower = mid - atr_mult * atr_v
    return upper, mid, lower

def calc_obv(df):
    """On-Balance Volume."""
    vol   = _get_series(df, "Volume").fillna(0)
    delta = df["Close"].diff()
    sign  = np.where(delta > 0, 1, np.where(delta < 0, -1, 0))
    return (sign * vol).cumsum()

def calc_pvt(df):
    """Price-Volume Trend."""
    vol = _get_series(df, "Volume").fillna(0)
    pct = df["Close"].pct_change().fillna(0)
    return (pct * vol).cumsum()

def calc_cmf(df, period=20):
    """Chaikin Money Flow."""
    high  = df["High"]  if "High"  in df.columns else df["Close"]
    low   = df["Low"]   if "Low"   in df.columns else df["Close"]
    vol   = _get_series(df, "Volume").fillna(0)
    denom = (high - low).replace(0, np.nan)
    mfm   = ((df["Close"] - low) - (high - df["Close"])) / denom
    mfm   = mfm.fillna(0)
    return (mfm * vol).rolling(period).sum() / vol.rolling(period).sum().replace(0, np.nan)

def calc_chandkroll(df, atr_period=10, atr_mult=2.0, period=9):
    """Chande Kroll Stop — returns (stop_short, stop_long)."""
    atr_v   = calc_atr(df, atr_period)
    high_p  = (df["High"] if "High" in df.columns else df["Close"]).rolling(period).max()
    low_p   = (df["Low"]  if "Low"  in df.columns else df["Close"]).rolling(period).min()
    first_hi = high_p - atr_mult * atr_v
    first_lo = low_p  + atr_mult * atr_v
    stop_short = first_hi.rolling(period).max()
    stop_long  = first_lo.rolling(period).min()
    return stop_short, stop_long

def calc_vwap_bands(df):
    """Simple rolling VWAP + 1 SD bands (daily proxy)."""
    vol    = _get_series(df, "Volume").replace(0, np.nan).fillna(1)
    high   = df["High"]  if "High"  in df.columns else df["Close"]
    low    = df["Low"]   if "Low"   in df.columns else df["Close"]
    tp     = (high + low + df["Close"]) / 3
    cum_tpv = (tp * vol).rolling(20).sum()
    cum_v   = vol.rolling(20).sum()
    vwap    = cum_tpv / cum_v
    dev     = (tp - vwap).abs().rolling(20).mean()
    return vwap, vwap + dev, vwap - dev

def calc_volume_profile(df, bins=20):
    """Volume Profile — returns dict with poc, vah, val."""
    vol   = _get_series(df, "Volume").fillna(0)
    price = df["Close"]
    if int(vol.sum()) == 0 or len(df) < 10:
        return {"poc": None, "vah": None, "val": None}
    lo, hi = price.min(), price.max()
    if hi == lo:
        v = float(price.iloc[-1])
        return {"poc": v, "vah": v, "val": v}
    edges  = np.linspace(lo, hi, bins + 1)
    bucket = np.digitize(price.values, edges, right=True).clip(0, bins - 1)
    vol_by_bin = np.zeros(bins)
    for i, b in enumerate(bucket):
        vol_by_bin[b] += float(vol.iloc[i])
    poc_idx = int(np.argmax(vol_by_bin))
    poc     = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)
    total   = vol_by_bin.sum(); target = total * 0.70
    va_vol  = vol_by_bin[poc_idx]
    lo_idx, hi_idx = poc_idx, poc_idx
    while va_vol < target and (lo_idx > 0 or hi_idx < bins - 1):
        lo_add = vol_by_bin[lo_idx - 1] if lo_idx > 0       else 0
        hi_add = vol_by_bin[hi_idx + 1] if hi_idx < bins - 1 else 0
        if hi_add >= lo_add and hi_idx < bins - 1:
            hi_idx += 1; va_vol += hi_add
        elif lo_idx > 0:
            lo_idx -= 1; va_vol += lo_add
        else:
            hi_idx += 1; va_vol += hi_add
    vah = float((edges[hi_idx] + edges[hi_idx + 1]) / 2)
    val = float((edges[lo_idx] + edges[lo_idx + 1]) / 2)
    return {"poc": poc, "vah": vah, "val": val}

def calc_mean_reversion(df, period=20):
    """Mean-reversion Z-score: (close − SMA) / rolling_std."""
    mu  = df["Close"].rolling(period).mean()
    sig = df["Close"].rolling(period).std()
    return (df["Close"] - mu) / sig.replace(0, np.nan)

def calc_rsi14_sma14_cross(close):
    """RSI(14) vs SMA(14-of-RSI) cross — returns (cross_status_str, rsi_series, sma_series)."""
    r   = rsi(close, 14)
    s14 = sma(r, 14)
    return cross_status(r, s14), r, s14

def calc_multiyear_high(df, years=3):
    """Check if latest monthly close is at a multi-year high. Returns (bool, pct_from_ath, ath)."""
    try:
        mdf = resample_ohlc(df, "ME")
        if mdf.empty or len(mdf) < 3:
            return False, None, None
        ath    = float(mdf["Close"].max())
        last_m = float(mdf["Close"].iloc[-1])
        cutoff   = mdf.index[-1] - pd.DateOffset(years=years)
        window_m = mdf[mdf.index >= cutoff]["Close"]
        period_high = float(window_m.max()) if not window_m.empty else ath
        is_new_high  = last_m >= period_high * 0.999
        pct_from_ath = round((last_m - ath) / ath * 100, 2)
        return is_new_high, pct_from_ath, round(ath, 2)
    except Exception:
        return False, None, None

# ══════════════════════════════════════════════════════════
# STOCK SCREENER DATA BUILDER
# ══════════════════════════════════════════════════════════

def compute_stock_screener_row(sym, tick, daily_data):
    """Compute all screener fields + score for a single stock. Returns dict or None."""
    df = daily_data.get(tick)
    if df is None or df.empty:
        return None
    df = ensure_close(df)
    if df is None or df.empty or len(df) < 34:
        return None

    close = df["Close"]
    score = 0
    signals = []

    # ── RSI values ──
    d_rsi = rsi(close, 14)
    w_rsi_series = None
    m_rsi_series = None
    try:
        wdf = resample_ohlc(df, "W")
        if not wdf.empty and len(wdf) >= 14:
            w_rsi_series = rsi(wdf["Close"], 14)
    except Exception:
        pass
    try:
        mdf = resample_ohlc(df, "ME")
        if not mdf.empty and len(mdf) >= 14:
            m_rsi_series = rsi(mdf["Close"], 14)
    except Exception:
        pass

    d_rsi_last  = _scalar(d_rsi.iloc[-1])
    w_rsi_last  = _scalar(w_rsi_series.iloc[-1]) if w_rsi_series is not None and len(w_rsi_series) else None
    m_rsi_last  = _scalar(m_rsi_series.iloc[-1]) if m_rsi_series is not None and len(m_rsi_series) else None

    if d_rsi_last and d_rsi_last > 50: score += 0.5; signals.append("D RSI>50")
    if w_rsi_last and w_rsi_last > 50: score += 0.5; signals.append("W RSI>50")
    if m_rsi_last and m_rsi_last > 50: score += 0.5; signals.append("M RSI>50")

    # ── Daily RSI vs SMA(34) cross ──
    sma34_rsi = sma(d_rsi, 34)
    rsi_sma34_cross = cross_status(d_rsi, sma34_rsi)
    if rsi_sma34_cross == "bullish":  score += 1.5; signals.append("RSI>SMA34 ▲")
    elif rsi_sma34_cross == "bearish": score -= 1.5; signals.append("RSI<SMA34 ▼")

    # ── Daily RSI vs Weekly RSI cross ──
    dw_cross = "–"
    if w_rsi_series is not None:
        w_aligned = w_rsi_series.reindex(d_rsi.index, method="ffill")
        dw_cross  = cross_status(d_rsi, w_aligned)
        if dw_cross == "bullish":  score += 2;   signals.append("D>W RSI ▲")
        elif dw_cross == "bearish": score -= 2;   signals.append("D<W RSI ▼")

    # ── Daily RSI vs Monthly RSI cross ──
    dm_cross = "–"
    if m_rsi_series is not None:
        m_aligned = m_rsi_series.reindex(d_rsi.index, method="ffill")
        dm_cross  = cross_status(d_rsi, m_aligned)
        if dm_cross == "bullish":  score += 2.5; signals.append("D>M RSI ▲▲")
        elif dm_cross == "bearish": score -= 2.5; signals.append("D<M RSI ▼▼")

    # ── MACD(34,200,9) cross ──
    macd_line, macd_sig, _ = macd(close, 34, 200, 9)
    macd_cross = cross_status(macd_line, macd_sig)
    if macd_cross == "bullish":  score += 1.5; signals.append("MACD ▲")
    elif macd_cross == "bearish": score -= 1.5; signals.append("MACD ▼")

    # ── Volume Oscillator ──
    vo_cross  = "–"
    lth_vol   = False
    has_vol   = False
    last_price = _scalar(close.iloc[-1])
    if "Volume" in df.columns:
        vol = _get_series(df, "Volume").replace(0, np.nan)
        if _safe_sum(vol) >= 20:
            has_vol   = True
            ema_fast  = vol.ewm(span=5,  adjust=False).mean()
            ema_slow  = vol.ewm(span=20, adjust=False).mean()
            vo        = ema_fast - ema_slow
            vo_sma_line = sma(vo.fillna(0), 9)
            vo_cross  = cross_status(vo, vo_sma_line)
            if vo_cross == "bullish":  score += 1;   signals.append("Vol Osc ▲")
            elif vo_cross == "bearish": score -= 0.5

            last_vol = _scalar(vol.iloc[-1]); max_vol = _scalar(vol.max())
            if last_vol is not None and max_vol and max_vol > 0:
                lth_vol = (last_vol >= max_vol * 0.95)
                if lth_vol: score += 2; signals.append("LTH Vol 🔥")

    # ── Price vs SMA(34) ──
    sma34_price   = sma(close, 34)
    price_above34 = bool(_scalar(close.iloc[-1]) > _scalar(sma34_price.iloc[-1])) \
                    if not _safe_bool(sma34_price.isna().all()) else False
    if price_above34: score += 0.5; signals.append("P>SMA34")

    last_vol_val = _scalar(_get_series(df, "Volume").iloc[-1]) if "Volume" in df.columns else None

    # ── RSI(14) vs SMA(14) ──
    rsi14_sma14_cross, _, _ = calc_rsi14_sma14_cross(close)
    if rsi14_sma14_cross == "bullish":  score += 1.5; signals.append("RSI14>SMA14 ▲")
    elif rsi14_sma14_cross == "bearish": score -= 1.0; signals.append("RSI14<SMA14 ▼")

    # ── Multi-year high ──
    is_myh, pct_from_ath, ath_price = calc_multiyear_high(df, years=3)
    if is_myh: score += 3.0; signals.append("3Y High 🏆")
    ath_str = f"{pct_from_ath:+.1f}%" if pct_from_ath is not None else "–"

    # ── ATR ──
    atr_series = calc_atr(df, 14)
    atr_val  = _scalar(atr_series.iloc[-1]) if not atr_series.empty else None
    atr_pct  = round(atr_val / last_price * 100, 2) if (atr_val and last_price) else None

    # ── Keltner Channel ──
    kc_upper, kc_mid, kc_lower = calc_keltner(df)
    kc_bull   = bool(_scalar(close.iloc[-1]) > _scalar(kc_upper.iloc[-1])) \
                if not _safe_bool(kc_upper.isna().all()) else False
    kc_bear   = bool(_scalar(close.iloc[-1]) < _scalar(kc_lower.iloc[-1])) \
                if not _safe_bool(kc_lower.isna().all()) else False
    kc_status = "above" if kc_bull else ("below" if kc_bear else "inside")
    if kc_bull:  score += 1.5; signals.append("KC Breakout ↑")
    elif kc_bear: score -= 1.0; signals.append("KC Breakdown ↓")

    # ── OBV trend ──
    obv_series = calc_obv(df)
    obv_sma20  = sma(obv_series, 20)
    obv_bull   = bool(_scalar(obv_series.iloc[-1]) > _scalar(obv_sma20.iloc[-1])) \
                 if not _safe_bool(obv_sma20.isna().all()) else False
    if obv_bull:  score += 1.0; signals.append("OBV▲")
    else:          score -= 0.5

    # ── PVT trend ──
    pvt_series = calc_pvt(df)
    pvt_sma20  = sma(pvt_series, 20)
    pvt_bull   = bool(_scalar(pvt_series.iloc[-1]) > _scalar(pvt_sma20.iloc[-1])) \
                 if not _safe_bool(pvt_sma20.isna().all()) else False
    if pvt_bull: score += 1.0; signals.append("PVT▲")

    # ── CMF ──
    cmf_series = calc_cmf(df, 20)
    cmf_val    = _scalar(cmf_series.iloc[-1]) if not cmf_series.empty else None
    if cmf_val is not None:
        if cmf_val > 0.05:   score += 1.0; signals.append(f"CMF+{cmf_val:.2f}")
        elif cmf_val < -0.05: score -= 1.0; signals.append(f"CMF{cmf_val:.2f}")

    # ── Chande Kroll Stop ──
    ck_short, ck_long = calc_chandkroll(df)
    ck_s = _scalar(ck_short.iloc[-1]) if not _safe_bool(ck_short.isna().all()) else None
    ck_bull_flag = bool(last_price > ck_s) if (ck_s and last_price) else None
    ck_trend = "bullish" if ck_bull_flag else ("bearish" if ck_bull_flag is False else "–")
    if ck_bull_flag is True:   score += 1.0; signals.append("CK Bull")
    elif ck_bull_flag is False: score -= 1.0; signals.append("CK Bear")

    # ── Volume Profile ──
    vp    = calc_volume_profile(df, bins=20)
    vp_pos = "–"
    if vp["poc"] and last_price:
        if last_price > vp["vah"]:    vp_pos = "above VAH"; score += 1.0; signals.append("Above VAH")
        elif last_price < vp["val"]:  vp_pos = "below VAL"; score -= 0.5
        else:                         vp_pos = "inside VA"

    # ── Mean Reversion Z-score ──
    zsc  = calc_mean_reversion(df, 20)
    zval = _scalar(zsc.iloc[-1]) if not zsc.empty else None
    if zval is not None:
        if zval > 2.0:    score -= 0.5; signals.append("MR Overbought")
        elif zval < -2.0:  score += 1.0; signals.append("MR Oversold")

    return {
        "Symbol":      sym,
        "Price":       last_price,
        "ATH":         ath_price,
        "ATH%":        ath_str,
        "3Y High":     "🏆" if is_myh else "–",
        "D RSI":       d_rsi_last,
        "W RSI":       w_rsi_last,
        "M RSI":       m_rsi_last,
        "RSI14/SMA14": rsi14_sma14_cross,
        "RSI/SMA34":   rsi_sma34_cross,
        "D>W RSI":     dw_cross,
        "D>M RSI":     dm_cross,
        "MACD":        macd_cross,
        "Vol Osc":     vo_cross if has_vol else "–",
        "LTH Vol":     "🔥 YES" if lth_vol else "no",
        "P>SMA34":     "✓" if price_above34 else "–",
        "KC":          kc_status,
        "OBV":         "▲" if obv_bull else "▼",
        "PVT":         "▲" if pvt_bull else "▼",
        "CMF":         round(cmf_val, 3) if cmf_val is not None else None,
        "CK Stop":     ck_trend,
        "VP Pos":      vp_pos,
        "ATR%":        atr_pct,
        "MR Z":        round(zval, 2) if zval is not None else None,
        "Volume":      last_vol_val,
        "Score":       round(score, 1),
        "Signals":     " | ".join(signals) if signals else "–"
    }

# ══════════════════════════════════════════════════════════
# STOCK FULL 8-PANEL DAILY CHART
# ══════════════════════════════════════════════════════════

def _build_stock_full_chart_fig(sym, df):
    """Build a rich 8-panel full daily chart for a stock. Returns figure or None."""
    close   = df["Close"]
    has_hlv = all(c in df.columns for c in ["High", "Low", "Volume"])
    vol     = _get_series(df, "Volume").replace(0, np.nan)

    # Pre-compute series
    d_rsi       = rsi(close, 14)
    sma14_rsi   = sma(d_rsi, 14)
    sma34_rsi   = sma(d_rsi, 34)
    sma34_p     = sma(close, 34)
    w_rsi_al = m_rsi_al = None
    try:
        wdf = resample_ohlc(df, "W")
        if not wdf.empty and len(wdf) >= 14:
            w_rsi_al = rsi(wdf["Close"], 14).reindex(df.index, method="ffill")
    except Exception: pass
    try:
        mdf = resample_ohlc(df, "ME")
        if not mdf.empty and len(mdf) >= 14:
            m_rsi_al = rsi(mdf["Close"], 14).reindex(df.index, method="ffill")
    except Exception: pass

    obv_s    = calc_obv(df);           pvt_s    = calc_pvt(df)
    cmf_s    = calc_cmf(df, 20);       atr_s    = calc_atr(df, 14)
    kc_up, kc_mid, kc_lo = calc_keltner(df)
    ck_short, ck_long    = calc_chandkroll(df)
    zsc                  = calc_mean_reversion(df, 20)
    vwap_v, vwap_up, vwap_dn = calc_vwap_bands(df)
    vp                   = calc_volume_profile(df, bins=30)

    vol_ma20 = vol.rolling(20).mean()
    ema5_v   = vol.ewm(span=5,  adjust=False).mean()
    ema20_v  = vol.ewm(span=20, adjust=False).mean()
    vo_s     = ema5_v - ema20_v
    max_vol  = vol.max()

    def _mkr(sa, sb):
        bx, by, rx, ry = [], [], [], []
        ci = sa.index.intersection(sb.index)
        if len(ci) < 2: return bx, by, rx, ry
        a = sa.reindex(ci).values; b = sb.reindex(ci).values
        for k in range(1, len(a)):
            if any(np.isnan(v) for v in [a[k],b[k],a[k-1],b[k-1]]): continue
            if a[k-1]<=b[k-1] and a[k]>b[k]: bx.append(ci[k]); by.append(float(a[k]))
            elif a[k-1]>=b[k-1] and a[k]<b[k]: rx.append(ci[k]); ry.append(float(a[k]))
        return bx, by, rx, ry

    is_myh, pct_ath, ath_price = calc_multiyear_high(df, years=3)

    fig = make_subplots(
        rows=8, cols=1, shared_xaxes=True,
        vertical_spacing=0.012,
        row_heights=[0.22, 0.13, 0.10, 0.10, 0.10, 0.12, 0.12, 0.11],
        subplot_titles=(
            f"{sym}  |  Price · SMA34(amber) · Keltner(purple) · VWAP(teal) · CK Stop",
            "RSI(14) Purple · SMA14(gold) · SMA34(orange) · Weekly(blue) · Monthly(red)  — Cross Signals",
            "Volume  ·  MA20(navy) · Vol Osc(purple, scaled)  ·  🔥=LTH Volume",
            "OBV (normalised, blue)  |  PVT (normalised, teal)",
            "Chaikin Money Flow CMF(20)  ·  Green>0.05 · Red<-0.05",
            "ATR%(14, brown)  |  Mean-Reversion Z-score×0.5 (pink)",
            "MACD(34,200,9)  ·  Blue=Line · Orange=Signal · Teal/Red=Histogram",
            "Volume Profile  (POC=orange · VAH/VAL=teal/red · VA=teal bars)"
        )
    )

    # ═══ ROW 1: Price ═══
    if has_hlv:
        fig.add_trace(go.Candlestick(
            x=df.index, open=df["Open"], high=df["High"],
            low=df["Low"], close=close, name="Price",
            increasing_line_color=C["bull"], decreasing_line_color=C["bear"],
            increasing_fillcolor=C["bull"], decreasing_fillcolor=C["bear"]),
            row=1, col=1)
    else:
        fig.add_trace(go.Scatter(x=df.index, y=close, name="Price",
                                 line=dict(color=C["line"], width=1.8)), row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=sma34_p, name="SMA34",
                             line=dict(color=C["sma34"], width=1.4, dash="dash")), row=1, col=1)
    # Keltner
    fig.add_trace(go.Scatter(x=df.index, y=kc_up, name="KC Upper",
                             line=dict(color="#9C27B0", width=0.9, dash="dot")), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=kc_mid, name="KC Mid",
                             line=dict(color="#9C27B0", width=0.9)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=kc_lo, name="KC Lower",
                             line=dict(color="#9C27B0", width=0.9, dash="dot"),
                             fill="tonexty", fillcolor="rgba(156,39,176,0.05)"), row=1, col=1)
    # VWAP
    fig.add_trace(go.Scatter(x=df.index, y=vwap_v, name="VWAP",
                             line=dict(color="#00838F", width=1.2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=vwap_up, name="VWAP+σ",
                             line=dict(color="#00838F", width=0.7, dash="dot")), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=vwap_dn, name="VWAP−σ",
                             line=dict(color="#00838F", width=0.7, dash="dot"),
                             fill="tonexty", fillcolor="rgba(0,131,143,0.04)"), row=1, col=1)
    # CK Stop
    fig.add_trace(go.Scatter(x=df.index, y=ck_short, name="CK Short",
                             line=dict(color="#E53935", width=1.1, dash="longdash")), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=ck_long, name="CK Long",
                             line=dict(color="#43A047", width=1.1, dash="longdash")), row=1, col=1)
    if is_myh:
        fig.add_annotation(text="🏆 3-Year High!", xref="paper", yref="paper",
                           x=0.99, y=0.99, xanchor="right", showarrow=False,
                           font=dict(size=13, color="#FF6F00", family="Arial Black"),
                           bgcolor="rgba(255,111,0,0.10)", bordercolor="#FF6F00",
                           borderwidth=1, borderpad=3)
    if ath_price:
        fig.add_annotation(
            text=f"ATH: ₹{ath_price:,.1f}  ({pct_ath:+.1f}% from ATH)",
            xref="paper", yref="paper", x=0.01, y=0.97, xanchor="left", showarrow=False,
            font=dict(size=10, color="#B71C1C"),
            bgcolor="rgba(183,28,28,0.06)", bordercolor="#B71C1C",
            borderwidth=1, borderpad=3)

    # ═══ ROW 2: RSI multi-TF ═══
    fig.add_trace(go.Scatter(x=df.index, y=d_rsi, name="Daily RSI(14)",
                             line=dict(color=C["rsi_d"], width=2.3)), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sma14_rsi, name="RSI SMA(14)",
                             line=dict(color=C["rsi_s14"], width=1.6, dash="dash")), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sma34_rsi, name="RSI SMA(34)",
                             line=dict(color=C["rsi_s34"], width=1.3, dash="dot")), row=2, col=1)
    if w_rsi_al is not None:
        fig.add_trace(go.Scatter(x=df.index, y=w_rsi_al, name="Weekly RSI",
                                 line=dict(color=C["rsi_w"], width=2.0)), row=2, col=1)
    if m_rsi_al is not None:
        fig.add_trace(go.Scatter(x=df.index, y=m_rsi_al, name="Monthly RSI",
                                 line=dict(color=C["rsi_m"], width=2.0, dash="dot")), row=2, col=1)
    for y, cl in [(70, C["ref_ob"]), (50, C["ref_mid"]), (30, C["ref_os"])]:
        fig.add_hline(y=y, line_dash="dot", line_color=cl, row=2, col=1)
    # Crossover markers
    bx,by,rx,ry = _mkr(d_rsi, sma14_rsi)
    if bx: fig.add_trace(go.Scatter(x=bx,y=by,mode="markers",name="RSI>SMA14▲",
        marker=dict(symbol="triangle-up",color=C["sig_sm_b"],size=10,
                    line=dict(width=1.2,color="#7B5800"))),row=2,col=1)
    if rx: fig.add_trace(go.Scatter(x=rx,y=ry,mode="markers",name="RSI<SMA14▼",
        marker=dict(symbol="triangle-down",color=C["sig_sm_r"],size=10,
                    line=dict(width=1.2,color="#7F3500"))),row=2,col=1)
    if w_rsi_al is not None:
        bx,by,rx,ry = _mkr(d_rsi, w_rsi_al)
        if bx: fig.add_trace(go.Scatter(x=bx,y=by,mode="markers",name="D>W RSI▲",
            marker=dict(symbol="circle",color=C["sig_md_b"],size=12,
                        line=dict(width=1.5,color="#004D20"))),row=2,col=1)
        if rx: fig.add_trace(go.Scatter(x=rx,y=ry,mode="markers",name="D<W RSI▼",
            marker=dict(symbol="circle",color=C["sig_md_r"],size=12,
                        line=dict(width=1.5,color="#7F0000"))),row=2,col=1)
    if m_rsi_al is not None:
        bx,by,rx,ry = _mkr(d_rsi, m_rsi_al)
        if bx: fig.add_trace(go.Scatter(x=bx,y=by,mode="markers",name="D>M RSI▲▲",
            marker=dict(symbol="diamond",color=C["sig_lg_b"],size=14,
                        line=dict(width=2,color="#004D20"))),row=2,col=1)
        if rx: fig.add_trace(go.Scatter(x=rx,y=ry,mode="markers",name="D<M RSI▼▼",
            marker=dict(symbol="diamond",color=C["sig_lg_r"],size=14,
                        line=dict(width=2,color="#7F0000"))),row=2,col=1)

    # ═══ ROW 3: Volume ═══
    if _safe_sum(vol) >= 10:
        bar_cols = []
        for i in range(len(df)):
            if i == 0: bar_cols.append("#90CAF9"); continue
            bar_cols.append(C["bull"] if float(close.iloc[i]) >= float(close.iloc[i-1]) else C["bear"])
        fig.add_trace(go.Bar(x=df.index, y=vol, name="Volume",
                             marker_color=bar_cols, opacity=0.50), row=3, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=vol_ma20, name="Vol MA20",
                                 line=dict(color="navy", width=1.3)), row=3, col=1)
        vo_scaled = vo_s / (vo_s.abs().max() or 1) * (vol.max() or 1) * 0.28
        fig.add_trace(go.Scatter(x=df.index, y=vo_scaled, name="Vol Osc (scaled)",
                                 line=dict(color="#7B1FA2", width=1.0, dash="dot")), row=3, col=1)
        lth_x = [df.index[k] for k in range(len(vol))
                 if pd.notna(vol.iloc[k]) and vol.iloc[k] >= max_vol * 0.95]
        if lth_x:
            fig.add_trace(go.Scatter(
                x=lth_x, y=[float(vol.loc[x]) for x in lth_x],
                mode="markers", name="LTH Vol 🔥",
                marker=dict(symbol="star", color="#FF6F00", size=13,
                            line=dict(width=1, color="#7F3500"))), row=3, col=1)

    # ═══ ROW 4: OBV + PVT (normalised) ═══
    obv_sma = sma(obv_s, 20); pvt_sma = sma(pvt_s, 20)
    def _norm(s, ref): return (s - ref.min()) / ((ref.max() - ref.min()) or 1)
    fig.add_trace(go.Scatter(x=df.index, y=_norm(obv_s,  obv_s), name="OBV",
                             line=dict(color=C["rsi_w"], width=1.5)), row=4, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=_norm(obv_sma, obv_s), name="OBV SMA20",
                             line=dict(color=C["rsi_w"], width=1.0, dash="dash")), row=4, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=_norm(pvt_s,  pvt_s), name="PVT",
                             line=dict(color="#00838F", width=1.5)), row=4, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=_norm(pvt_sma, pvt_s), name="PVT SMA20",
                             line=dict(color="#00838F", width=1.0, dash="dash")), row=4, col=1)
    fig.add_hline(y=0.5, line_dash="dot", line_color="rgba(100,100,100,0.3)", row=4, col=1)

    # ═══ ROW 5: CMF ═══
    fig.add_trace(go.Bar(x=df.index, y=cmf_s,
                         marker_color=["#26A69A" if v >= 0 else "#EF5350" for v in cmf_s.fillna(0)],
                         name="CMF(20)", opacity=0.78), row=5, col=1)
    fig.add_hline(y=0,     line_dash="solid", line_color="rgba(100,100,100,0.3)", row=5, col=1)
    fig.add_hline(y=0.05,  line_dash="dot",   line_color="rgba(38,166,154,0.5)",  row=5, col=1)
    fig.add_hline(y=-0.05, line_dash="dot",   line_color="rgba(239,83,80,0.5)",   row=5, col=1)

    # ═══ ROW 6: ATR% + Z-score ═══
    atr_pct_s = atr_s / close * 100
    fig.add_trace(go.Scatter(x=df.index, y=atr_pct_s, name="ATR%(14)",
                             line=dict(color="#795548", width=1.3)), row=6, col=1)
    z_scaled = zsc * 0.5
    fig.add_trace(go.Scatter(x=df.index, y=z_scaled, name="MR Z ×0.5",
                             line=dict(color="#E91E63", width=1.3, dash="dash")), row=6, col=1)
    for y, cl in [(1.0,"rgba(239,83,80,0.4)"),(-1.0,"rgba(38,166,154,0.4)")]:
        fig.add_hline(y=y*0.5, line_dash="dot", line_color=cl, row=6, col=1)

    # ═══ ROW 7: MACD ═══
    m_line, m_sig, m_hist = macd(close, 34, 200, 9)
    hist_cols = [C["macd_p"] if v >= 0 else C["macd_n"] for v in m_hist.fillna(0)]
    fig.add_trace(go.Bar(x=df.index, y=m_hist, name="MACD Hist",
                          marker_color=hist_cols, opacity=0.68), row=7, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=m_line, name="MACD(34,200)",
                             line=dict(color=C["macd_l"], width=1.6)), row=7, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=m_sig, name="Signal",
                             line=dict(color=C["macd_s"], width=1.2)), row=7, col=1)
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(100,100,100,0.35)", row=7, col=1)
    try:
        trend = cross_status(m_line, m_sig)
        tcol  = "#00897B" if trend=="bullish" else ("#E53935" if trend=="bearish" else "#FB8C00")
        fig.add_annotation(text=f"MACD: {trend.upper()}", xref="paper", yref="paper",
                           x=0.01, y=0.01, showarrow=False,
                           font=dict(size=11, color=tcol, family="Arial Black"),
                           bgcolor="white", bordercolor=tcol, borderwidth=1.5,
                           borderpad=3, opacity=0.92)
    except Exception: pass

    # ═══ ROW 8: Volume Profile ═══
    if vp["poc"] is not None:
        lo_p, hi_p = float(close.min()), float(close.max())
        bins = 30
        edges = np.linspace(lo_p, hi_p, bins + 1)
        vol_f  = _get_series(df, "Volume").fillna(0)
        vol_by_bin = np.zeros(bins)
        bucket = np.digitize(close.values, edges, right=True).clip(0, bins-1)
        for i, b in enumerate(bucket):
            vol_by_bin[b] += float(vol_f.iloc[i])
        mid_prices = [(edges[k]+edges[k+1])/2 for k in range(bins)]
        poc_p = vp["poc"]; vah_p = vp["vah"]; val_p = vp["val"]
        bar_c = []
        for mp in mid_prices:
            if abs(mp - poc_p) < (hi_p-lo_p)/bins*1.5: bar_c.append("#FF6F00")
            elif val_p <= mp <= vah_p:                   bar_c.append("#26A69A")
            else:                                        bar_c.append("#90CAF9")
        fig.add_trace(go.Scatter(x=df.index, y=[poc_p]*len(df), name=f"POC ₹{poc_p:,.1f}",
                                 line=dict(color="#FF6F00", width=2, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=[vah_p]*len(df), name=f"VAH ₹{vah_p:,.1f}",
                                 line=dict(color="#26A69A", width=1.2, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=[val_p]*len(df), name=f"VAL ₹{val_p:,.1f}",
                                 line=dict(color="#EF5350", width=1.2, dash="dash")), row=1, col=1)
        fig.add_trace(go.Bar(x=mid_prices, y=vol_by_bin.tolist(),
                             name="VP Bins", marker_color=bar_c, opacity=0.72,
                             orientation="v"), row=8, col=1)
        fig.update_xaxes(title_text="Price (₹)", row=8, col=1)
        fig.update_yaxes(title_text="Volume",    row=8, col=1)

    fig.update_layout(
        title=dict(text=f"<b>{sym}</b> — Full Technical Analysis Dashboard",
                   font=dict(size=16, color="#1A237E"), x=0.5, xanchor="center"),
        template="plotly_white",
        height=1650,
        plot_bgcolor=C["bg_plot"], paper_bgcolor=C["bg_paper"],
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.01, x=0, font=dict(size=8, color="#37474F"),
                    tracegroupgap=2, bgcolor="rgba(255,255,255,0.85)",
                    bordercolor="#CFD8DC", borderwidth=1),
        margin=dict(t=90, b=40, l=65, r=25)
    )
    fig.update_xaxes(rangeslider_visible=False,
                     showgrid=True, gridcolor=C["grid"], gridwidth=1)
    fig.update_yaxes(showgrid=True, gridcolor=C["grid"], gridwidth=1)
    return fig

def build_stock_chart(sym, tick, daily_data, out_dir):
    """
    Build and save:
      1. Combined D/W/M chart (4×3 grid, equally split)
      2. Full daily 8-panel chart
    Returns dict {"combined": rel_path, "full": rel_path} — missing keys if failed.
    """
    df = daily_data.get(tick)
    if df is None or df.empty:
        return {}
    df = ensure_close(df)
    if df is None or df.empty or len(df) < 34:
        return {}

    results = {}
    safe_sym = _safe_filename(sym)

    # ── Combined D/W/M chart ──
    try:
        df_weekly  = resample_ohlc(df, "W")
        df_monthly = resample_ohlc(df, "ME")
        w_rsi = rsi(df_weekly["Close"],  14) if not df_weekly.empty  and len(df_weekly)  >= 14 else None
        m_rsi = rsi(df_monthly["Close"], 14) if not df_monthly.empty and len(df_monthly) >= 14 else None

        fig_comb = _build_combined_chart_fig(
            df,
            df_weekly  if not df_weekly.empty  else None,
            df_monthly if not df_monthly.empty else None,
            sym, use_candle=True,
            weekly_rsi_ref=w_rsi, monthly_rsi_ref=m_rsi)
        results["combined"] = _save_chart(
            fig_comb, out_dir, f"stock_{safe_sym}_combined",
            img_width=2700, img_scale=1.2)
    except Exception as e:
        logger.warning(f"Stock combined chart failed for {sym}: {e}")

    # ── Full 8-panel daily chart ──
    try:
        fig_full = _build_stock_full_chart_fig(sym, df)
        if fig_full:
            results["full"] = _save_chart(
                fig_full, out_dir, f"stock_{safe_sym}_full",
                img_width=1800, img_scale=1.0)
    except Exception as e:
        logger.warning(f"Stock full chart failed for {sym}: {e}")

    return results

# ══════════════════════════════════════════════════════════
# HTML GENERATION (lightweight — charts are external files)
# ══════════════════════════════════════════════════════════

def generate_html(index_summary, constituents_detail, charts_paths,
                  stock_screener_rows=None, stock_chart_paths=None,
                  refresh_seconds=None):
    """
    Generate dashboard HTML.
    charts_paths:     {iname: {"combined": path, "daily": path, "weekly": path, "monthly": path}}
    stock_chart_paths:{sym:   {"combined": path, "full":  path}}
    Charts are referenced by file path — NOT embedded as JSON.
    """
    summary_json  = json.dumps(index_summary,            default=str)
    detail_json   = json.dumps(constituents_detail,      default=str)
    charts_js     = json.dumps(charts_paths)
    screener_json = json.dumps(stock_screener_rows or [], default=str)
    stk_charts_js = json.dumps(stock_chart_paths  or {})

    meta_refresh = ""
    if refresh_seconds:
        meta_refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">'

    if not index_summary:
        return "<html><body><h2>No index data available.</h2></body></html>"

    plain_table = ("<table border='1'><tr><th>Index</th><th>Type</th><th>Price</th>"
                   "<th>D%</th><th>D RSI</th><th>W RSI</th><th>M RSI</th><th>Score</th></tr>")
    for item in index_summary:
        plain_table += (f"<tr><td>{item.get('Index','')}</td><td>{item.get('type','')}</td>"
                        f"<td>{item.get('last_price')}</td><td>{item.get('daily_perf')}</td>"
                        f"<td>{item.get('daily_rsi')}</td><td>{item.get('weekly_rsi')}</td>"
                        f"<td>{item.get('monthly_rsi')}</td><td>{item.get('prediction_score')}</td></tr>")
    plain_table += "</table>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    {meta_refresh}
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NSE Index Analysis{"  (Live)" if refresh_seconds else ""} – {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.datatables.net/1.13.4/css/dataTables.bootstrap5.min.css" rel="stylesheet">
    <script src="https://code.jquery.com/jquery-3.6.4.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/jquery.dataTables.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.4/js/dataTables.bootstrap5.min.js"></script>
    <style>
        body {{ background: #F3F4F8; padding: 20px; font-family: 'Segoe UI', sans-serif; }}
        h2   {{ color: #1A237E; font-weight: 700; }}
        .bullish  {{ color: #00897B; font-weight: 700; }}
        .bearish  {{ color: #E53935; font-weight: 700; }}
        .sideways {{ color: #FB8C00; }}
        .synthetic{{ color: #6c757d; font-style: italic; }}
        #fallbackTable {{ display: block; }}
        #dtContainer   {{ display: none; }}
        /* Chart containers */
        .chart-wrap  {{ width:100%; background:#fff; border-radius:6px;
                        box-shadow:0 1px 4px rgba(0,0,0,.10); overflow:hidden; }}
        .chart-frame {{ width:100%; height:880px; border:none; display:block; }}
        .chart-img   {{ width:100%; height:auto; cursor:zoom-in; display:block; }}
        .chart-ph    {{ padding:30px; color:#999; text-align:center; font-style:italic; }}
        /* Accordion polish */
        .accordion-button {{ font-weight: 600; color: #1A237E; }}
        .nav-pills .nav-link.active {{ background-color: #1A237E; }}
        .nav-pills .nav-link        {{ color: #1A237E; border: 1px solid #1A237E;
                                       margin-right: 4px; border-radius: 4px; }}
    </style>
</head>
<body>
    <h2 class="mb-1">📊 NSE Indices — Multi-Timeframe Technical Analysis</h2>
    <p class="text-muted small mb-3">
        Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp;
        Indices: {len(index_summary)} &nbsp;|&nbsp;
        {"🟢 Live Mode" if refresh_seconds else "🔵 Static Mode"}
    </p>

    <!-- Fallback plain table (shown if JS fails) -->
    <div id="fallbackTable">
        <h5>Index Summary (plain fallback)</h5>
        {plain_table}
    </div>

    <!-- Main DataTables container -->
    <div id="dtContainer">
        <ul class="nav nav-tabs mb-3" id="mainTabs" role="tablist">
            <li class="nav-item"><a class="nav-link active" id="summary-tab"  data-bs-toggle="tab" href="#summary"  role="tab">📋 Index Summary</a></li>
            <li class="nav-item"><a class="nav-link"        id="detail-tab"   data-bs-toggle="tab" href="#detail"   role="tab">📂 Constituent Details</a></li>
            <li class="nav-item"><a class="nav-link"        id="screener-tab" data-bs-toggle="tab" href="#screener" role="tab">🔍 Stock Screener</a></li>
        </ul>

        <div class="tab-content">
            <!-- ═══ TAB 1: Index Summary ═══ -->
            <div class="tab-pane fade show active" id="summary" role="tabpanel">
                <table id="summaryTable" class="table table-striped table-bordered" style="width:100%">
                    <thead><tr>
                        <th>Index</th><th>Type</th><th>Price</th>
                        <th>D%</th><th>W%</th><th>M%</th>
                        <th>D RSI</th><th>W RSI</th><th>M RSI</th>
                        <th>RSI D×</th><th>RSI W×</th><th>RSI M×</th>
                        <th>MACD D(34,200)</th><th>MACD D(34,1000)</th><th>MACD W</th><th>MACD M</th>
                        <th>15m RSI</th><th>1h RSI</th><th>4h RSI</th><th>Score</th>
                    </tr></thead>
                </table>
            </div>

            <!-- ═══ TAB 2: Constituent Details ═══ -->
            <div class="tab-pane fade" id="detail" role="tabpanel">
                <div class="accordion" id="indexAccordion"><!-- filled dynamically --></div>
            </div>

            <!-- ═══ TAB 3: Stock Screener ═══ -->
            <div class="tab-pane fade" id="screener" role="tabpanel">
                <div class="d-flex align-items-center gap-3 mb-2 flex-wrap">
                    <span class="fw-bold">Score filter:</span>
                    <select id="scoreFilter" class="form-select form-select-sm" style="width:170px">
                        <option value="">All stocks</option>
                        <option value="5">Score ≥ 5  (Strong buy)</option>
                        <option value="3">Score ≥ 3  (Buy)</option>
                        <option value="0">Score ≥ 0  (Neutral+)</option>
                        <option value="-99">All (incl. bearish)</option>
                    </select>
                    <span id="screenerCount" class="text-muted small"></span>
                    <small class="text-muted ms-auto">🔗 Symbol = click to open Combined D·W·M chart &nbsp; | &nbsp; 📊 = Full 8-panel chart</small>
                </div>
                <table id="screenerTable" class="table table-striped table-bordered table-sm" style="width:100%">
                    <thead><tr>
                        <th>Symbol</th><th>Price</th><th>ATH</th><th>ATH%</th><th>3Y High</th>
                        <th>D RSI</th><th>W RSI</th><th>M RSI</th>
                        <th>RSI14/SMA14</th><th>RSI/SMA34</th><th>D&gt;W RSI</th><th>D&gt;M RSI</th>
                        <th>MACD</th><th>Vol Osc</th><th>LTH Vol</th><th>P&gt;SMA34</th>
                        <th>KC</th><th>OBV</th><th>PVT</th><th>CMF</th>
                        <th>CK Stop</th><th>VP Pos</th><th>ATR%</th><th>MR Z</th>
                        <th>Score</th><th>Signals</th>
                    </tr></thead>
                </table>
            </div>
        </div>
    </div>

    <script>
        const summaryData   = {summary_json};
        const detailData    = {detail_json};
        const chartsData    = {charts_js};
        const screenerData  = {screener_json};
        const stkChartsData = {stk_charts_js};

        // ── Chart render helper ──
        // src can end in .html (iframe) or .png/.jpg (img tag)
        function renderChart(divEl, pathsObj, tf) {{
            if (!divEl || divEl.dataset.chartLoaded === '1') return;
            const src = pathsObj && pathsObj[tf];
            if (!src) {{
                divEl.innerHTML = '<p class="chart-ph">No ' + tf + ' chart available.</p>';
                divEl.dataset.chartLoaded = '1';
                return;
            }}
            const isHtml = src.toLowerCase().endsWith('.html');
            divEl.innerHTML = isHtml
                ? '<iframe src="' + src + '" class="chart-frame" loading="lazy"></iframe>'
                : '<a href="' + src + '" target="_blank" title="Click to open full size">'
                  + '<img src="' + src + '" class="chart-img" loading="lazy" /></a>';
            divEl.dataset.chartLoaded = '1';
        }}

        try {{
            $(document).ready(function() {{
                if (summaryData.length === 0) {{
                    document.getElementById('fallbackTable').style.display = 'block';
                    return;
                }}

                // ── Summary DataTable ──
                $('#summaryTable').DataTable({{
                    data: summaryData,
                    columns: [
                        {{ data:'Index' }}, {{ data:'type' }}, {{ data:'last_price' }},
                        {{ data:'daily_perf',   render: v => v ? v.toFixed(2)+'%' : '' }},
                        {{ data:'weekly_perf',  render: v => v ? v.toFixed(2)+'%' : '' }},
                        {{ data:'monthly_perf', render: v => v ? v.toFixed(2)+'%' : '' }},
                        {{ data:'daily_rsi',    render: v => v ? v.toFixed(1) : '' }},
                        {{ data:'weekly_rsi',   render: v => v ? v.toFixed(1) : '' }},
                        {{ data:'monthly_rsi',  render: v => v ? v.toFixed(1) : '' }},
                        {{ data:'daily_rsi_cross',            render: d => `<span class="${{d}}">${{d}}</span>` }},
                        {{ data:'weekly_rsi_cross',           render: d => `<span class="${{d}}">${{d}}</span>` }},
                        {{ data:'monthly_rsi_cross',          render: d => `<span class="${{d}}">${{d}}</span>` }},
                        {{ data:'daily_macd_34_200_9_cross',  render: d => `<span class="${{d}}">${{d}}</span>` }},
                        {{ data:'daily_macd_34_1000_9_cross', render: d => `<span class="${{d}}">${{d}}</span>` }},
                        {{ data:'weekly_macd_cross',          render: d => `<span class="${{d}}">${{d}}</span>` }},
                        {{ data:'monthly_macd_cross',         render: d => `<span class="${{d}}">${{d}}</span>` }},
                        {{ data:'15min_rsi', render: v => v ? v.toFixed(1) : '' }},
                        {{ data:'1h_rsi',    render: v => v ? v.toFixed(1) : '' }},
                        {{ data:'4h_rsi',    render: v => v ? v.toFixed(1) : '' }},
                        {{ data:'prediction_score', render: v => v.toFixed(1),
                           createdCell: function(td, val) {{
                               if      (val >= 7)  $(td).css({{'color':'#004D40','font-weight':'900','background':'#E0F2F1'}});
                               else if (val >= 4)  $(td).css({{'color':'#00897B','font-weight':'700'}});
                               else if (val >= 2)  $(td).css({{'color':'green',  'font-weight':'600'}});
                               else if (val <= -2) $(td).css({{'color':'#E53935','font-weight':'600'}});
                           }}
                        }}
                    ],
                    order: [[19, 'desc']],
                    pageLength: 25
                }});

                document.getElementById('fallbackTable').style.display = 'none';
                document.getElementById('dtContainer').style.display   = 'block';

                // ── Screener DataTable ──
                function crossCell(d) {{
                    if (!d || d === '–') return '<span class="text-muted">–</span>';
                    const cls  = d === 'bullish' ? 'bullish' : (d === 'bearish' ? 'bearish' : 'sideways');
                    const icon = d === 'bullish' ? '▲' : (d === 'bearish' ? '▼' : '↔');
                    return `<span class="${{cls}}">${{icon}} ${{d}}</span>`;
                }}
                const screenerDT = $('#screenerTable').DataTable({{
                    data: screenerData,
                    columns: [
                        {{ data:'Symbol', render: (d) => {{
                            const paths = stkChartsData[d];
                            if (!paths) return `<span class="fw-bold text-muted">${{d}}</span>`;
                            const combPath = paths['combined'] || paths['full'] || '';
                            const fullPath = paths['full'] || '';
                            let html = combPath
                                ? `<a href="${{combPath}}" target="_blank" class="fw-bold" title="Combined D·W·M chart">${{d}}</a>`
                                : `<span class="fw-bold">${{d}}</span>`;
                            if (fullPath && fullPath !== combPath)
                                html += ` <a href="${{fullPath}}" target="_blank" class="text-muted small ms-1" title="Full 8-panel">📊</a>`;
                            return html;
                        }} }},
                        {{ data:'Price',   render: v => v != null ? v.toFixed(2) : '' }},
                        {{ data:'ATH',     render: v => v != null ? v.toFixed(2) : '–' }},
                        {{ data:'ATH%',    render: (d) => {{
                            if (!d || d === '–') return '–';
                            const num = parseFloat(d);
                            const col = num >= 0 ? 'color:#00897B;font-weight:bold'
                                      : (num < -20 ? 'color:#B71C1C;font-weight:bold' : 'color:#e53935');
                            return `<span style="${{col}}">${{d}}</span>`;
                        }} }},
                        {{ data:'3Y High', render: d => d === '🏆'
                            ? '<span style="color:#FF6F00;font-size:1.15em;font-weight:bold">🏆 YES</span>'
                            : '<span class="text-muted">–</span>' }},
                        {{ data:'D RSI',   render: v => v != null ? v.toFixed(1) : '' }},
                        {{ data:'W RSI',   render: v => v != null ? v.toFixed(1) : '' }},
                        {{ data:'M RSI',   render: v => v != null ? v.toFixed(1) : '' }},
                        {{ data:'RSI14/SMA14', render: d => crossCell(d) }},
                        {{ data:'RSI/SMA34',   render: d => crossCell(d) }},
                        {{ data:'D>W RSI',     render: d => crossCell(d) }},
                        {{ data:'D>M RSI',     render: d => crossCell(d) }},
                        {{ data:'MACD',        render: d => crossCell(d) }},
                        {{ data:'Vol Osc',     render: d => crossCell(d) }},
                        {{ data:'LTH Vol',     render: d => d && d.includes('YES')
                            ? '<span style="color:#FF6F00;font-weight:bold">🔥 YES</span>'
                            : '<span class="text-muted">–</span>' }},
                        {{ data:'P>SMA34',     render: d => d === '✓'
                            ? '<span class="bullish">✓</span>'
                            : '<span class="text-muted">–</span>' }},
                        {{ data:'KC',  render: d => d === 'above'
                            ? '<span class="bullish">↑ above</span>'
                            : (d === 'below' ? '<span class="bearish">↓ below</span>'
                                             : '<span class="sideways">inside</span>') }},
                        {{ data:'OBV', render: d => d === '▲'
                            ? '<span class="bullish">▲</span>'
                            : '<span class="bearish">▼</span>' }},
                        {{ data:'PVT', render: d => d === '▲'
                            ? '<span class="bullish">▲</span>'
                            : '<span class="bearish">▼</span>' }},
                        {{ data:'CMF', render: v => {{
                            if (v == null) return '–';
                            const col = v > 0.05 ? '#00897B' : (v < -0.05 ? '#E53935' : '#78909C');
                            return `<span style="color:${{col}}">${{v.toFixed(3)}}</span>`;
                        }} }},
                        {{ data:'CK Stop',  render: d => crossCell(d) }},
                        {{ data:'VP Pos',   render: d => d === 'above VAH'
                            ? '<span class="bullish">↑ above VAH</span>'
                            : (d === 'below VAL' ? '<span class="bearish">↓ below VAL</span>'
                                                  : '<span class="sideways">inside VA</span>') }},
                        {{ data:'ATR%',  render: v => v != null ? v.toFixed(2)+'%' : '–' }},
                        {{ data:'MR Z',  render: v => {{
                            if (v == null) return '–';
                            const col = v > 2 ? '#B71C1C' : (v < -2 ? '#1B5E20' : '#78909C');
                            return `<span style="color:${{col}}">${{v.toFixed(2)}}</span>`;
                        }} }},
                        {{ data:'Score',
                           render: v => v != null ? v.toFixed(1) : '0',
                           createdCell: function(td, val) {{
                               if      (val >= 8)  $(td).css({{'color':'#004D40','font-weight':'900','font-size':'1.05em','background':'#E0F2F1'}});
                               else if (val >= 5)  $(td).css({{'color':'#00897B','font-weight':'700'}});
                               else if (val >= 3)  $(td).css({{'color':'green',  'font-weight':'600'}});
                               else if (val <= -3) $(td).css({{'color':'#E53935','font-weight':'600'}});
                               else if (val < 0)   $(td).css({{'color':'#e53935'}});
                           }}
                        }},
                        {{ data:'Signals', render: d => `<small class="text-muted">${{d||''}}</small>` }}
                    ],
                    order: [[24, 'desc']],
                    pageLength: 50,
                    lengthMenu: [25, 50, 100, 200],
                    scrollX: true
                }});

                $('#screenerCount').text(screenerData.length + ' stocks loaded');

                // Score filter
                $('#scoreFilter').on('change', function() {{
                    $.fn.dataTable.ext.search = $.fn.dataTable.ext.search.filter(fn => fn._sf !== true);
                    const val = $(this).val();
                    if (val !== '') {{
                        const minScore = parseFloat(val);
                        const fn = function(settings, data, idx) {{
                            if (settings.nTable.id !== 'screenerTable') return true;
                            return (parseFloat(screenerDT.row(idx).data()['Score']) || 0) >= minScore;
                        }};
                        fn._sf = true;
                        $.fn.dataTable.ext.search.push(fn);
                    }}
                    screenerDT.draw();
                    $('#screenerCount').text(
                        screenerDT.rows({{search:'applied'}}).count() + ' / ' + screenerData.length + ' stocks');
                }});

                // ── Accordion build ──
                let accordion = '';
                Object.keys(detailData).forEach((iname, i) => {{
                    const title = iname.replace(/_/g, ' ');
                    const n     = detailData[iname].length;
                    accordion += `
                    <div class="accordion-item">
                        <h2 class="accordion-header" id="hd${{i}}">
                            <button class="accordion-button collapsed" type="button"
                                    data-bs-toggle="collapse" data-bs-target="#col${{i}}">
                                ${{title}} &nbsp;<span class="badge bg-secondary ms-1">${{n}} stocks</span>
                            </button>
                        </h2>
                        <div id="col${{i}}" class="accordion-collapse collapse" data-iname="${{iname}}">
                            <div class="accordion-body">
                                <!-- Chart tabs: Combined | Daily | Weekly | Monthly -->
                                <ul class="nav nav-pills mb-2" id="ct${{i}}">
                                    <li class="nav-item"><button class="nav-link active" data-tf="combined" data-idx="${{i}}" type="button">🗂 Combined D·W·M</button></li>
                                    <li class="nav-item"><button class="nav-link"        data-tf="daily"    data-idx="${{i}}" type="button">Daily</button></li>
                                    <li class="nav-item"><button class="nav-link"        data-tf="weekly"   data-idx="${{i}}" type="button">Weekly</button></li>
                                    <li class="nav-item"><button class="nav-link"        data-tf="monthly"  data-idx="${{i}}" type="button">Monthly</button></li>
                                </ul>
                                <div class="chart-wrap">
                                    <div id="ch${{i}}_combined" data-chart-loaded="0" style="display:block;"></div>
                                    <div id="ch${{i}}_daily"    data-chart-loaded="0" style="display:none;"></div>
                                    <div id="ch${{i}}_weekly"   data-chart-loaded="0" style="display:none;"></div>
                                    <div id="ch${{i}}_monthly"  data-chart-loaded="0" style="display:none;"></div>
                                </div>
                                <!-- Constituent table -->
                                <div class="table-responsive mt-3">
                                    <table id="tbl${{i}}" class="table table-striped table-bordered table-sm" style="width:100%">
                                        <thead><tr>
                                            <th>Symbol</th><th>Company</th><th>Industry</th><th>Series</th>
                                            <th>Price</th><th>D RSI</th><th>W RSI</th><th>M RSI</th>
                                            <th>D Cross</th><th>W Cross</th><th>M Cross</th>
                                            <th>MACD D1</th><th>MACD D2</th><th>MACD W</th><th>MACD M</th>
                                            <th>15m RSI</th><th>1h RSI</th><th>4h RSI</th><th>Score</th>
                                        </tr></thead>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>`;
                }});
                $('#indexAccordion').html(accordion);

                // Tab switch handler
                $('#indexAccordion').on('click', '[data-tf]', function() {{
                    const tf  = $(this).data('tf');
                    const idx = $(this).data('idx');
                    const iname = $('#col' + idx).data('iname');
                    $(this).closest('ul').find('.nav-link').removeClass('active');
                    $(this).addClass('active');
                    ['combined','daily','weekly','monthly'].forEach(t => {{
                        const d = document.getElementById('ch' + idx + '_' + t);
                        if (d) d.style.display = (t === tf) ? 'block' : 'none';
                    }});
                    const divEl = document.getElementById('ch' + idx + '_' + tf);
                    renderChart(divEl, chartsData[iname], tf);
                }});

                // Lazy init on accordion open
                $('#indexAccordion').on('shown.bs.collapse', '.collapse', function() {{
                    const iname = $(this).data('iname');
                    const colId = $(this).attr('id');          // e.g. "col3"
                    const i     = colId.replace('col', '');
                    const tblId = '#tbl' + i;

                    if (!$.fn.DataTable.isDataTable(tblId)) {{
                        $(tblId).DataTable({{
                            data: detailData[iname],
                            columns: [
                                {{data:'Symbol'}}, {{data:'Company'}}, {{data:'Industry'}}, {{data:'Series'}},
                                {{data:'Last Price', render: v => v != null ? (typeof v==='number' ? v.toFixed(2) : v) : ''}},
                                {{data:'Daily RSI',   render: v => v != null ? v.toFixed(1) : ''}},
                                {{data:'Weekly RSI',  render: v => v != null ? v.toFixed(1) : ''}},
                                {{data:'Monthly RSI', render: v => v != null ? v.toFixed(1) : ''}},
                                {{data:'D RSI Cross',     render: d => d ? `<span class="${{d}}">${{d}}</span>` : ''}},
                                {{data:'W RSI Cross',     render: d => d ? `<span class="${{d}}">${{d}}</span>` : ''}},
                                {{data:'M RSI Cross',     render: d => d ? `<span class="${{d}}">${{d}}</span>` : ''}},
                                {{data:'MACD D(34,200)',  render: d => d ? `<span class="${{d}}">${{d}}</span>` : ''}},
                                {{data:'MACD D(34,1000)', render: d => d ? `<span class="${{d}}">${{d}}</span>` : ''}},
                                {{data:'MACD W',          render: d => d ? `<span class="${{d}}">${{d}}</span>` : ''}},
                                {{data:'MACD M',          render: d => d ? `<span class="${{d}}">${{d}}</span>` : ''}},
                                {{data:'15m RSI', render: v => v != null ? v.toFixed(1) : ''}},
                                {{data:'1h RSI',  render: v => v != null ? v.toFixed(1) : ''}},
                                {{data:'4h RSI',  render: v => v != null ? v.toFixed(1) : ''}},
                                {{data:'Score', render: v => v != null ? v.toFixed(1) : '0.0',
                                  createdCell: function(td, val) {{
                                      if      (val >= 3)  $(td).css({{'color':'#00897B','font-weight':'700'}});
                                      else if (val <= -3) $(td).css({{'color':'#E53935','font-weight':'700'}});
                                  }}
                                }}
                            ],
                            order: [[18, 'desc']],
                            pageLength: 25
                        }});
                    }}

                    // Auto-render combined chart on first open
                    const cmbDiv = document.getElementById('ch' + i + '_combined');
                    renderChart(cmbDiv, chartsData[iname], 'combined');
                }});

            }});
        }} catch(e) {{
            console.error("Initialisation error:", e);
            document.getElementById('fallbackTable').style.display = 'block';
            document.getElementById('dtContainer').style.display   = 'none';
        }}
    </script>
</body>
</html>"""
    return html

# ══════════════════════════════════════════════════════════
# MAIN ANALYSIS CYCLE
# ══════════════════════════════════════════════════════════

def run_analysis_cycle(config, live=False):
    base_dir        = config.get("base_dir", os.path.dirname(os.path.abspath(__file__)))
    indices_dir     = os.path.join(base_dir, config.get("indices_dir", "NseIndice"))
    output_html     = config.get("output_html", "index_analysis.html")
    cache_dir       = os.path.join(base_dir, config.get("cache_dir", "cache"))
    refresh_min     = config.get("refresh_interval_minutes", 15)
    cache_dur_min   = config.get("cache_duration_minutes", 5) if live else 24*60
    ticker_map      = config.get("ticker_mapping", {})
    daily_period    = config.get("daily_period", "max")
    intraday_period = config.get("intraday_period", "60d")

    # Charts output folder (sibling to the HTML file)
    charts_dir_name = config.get("charts_output_dir", "charts")
    html_dir  = os.path.dirname(os.path.abspath(output_html)) if os.path.dirname(output_html) else os.getcwd()
    charts_dir = os.path.join(html_dir, charts_dir_name)
    os.makedirs(charts_dir, exist_ok=True)
    logger.info(f"Charts output directory: {charts_dir}")

    # 1. Load master equity list — support both key names in config
    master_file = config.get("master_file") or config.get("master_equity_path")
    if master_file:
        master_equity_path = os.path.join(base_dir, master_file)
    else:
        master_equity_path = os.path.join(base_dir, "NSECash", "EQUITY_L.csv")
    all_stocks = load_master_equity(master_equity_path) if os.path.exists(master_equity_path) else []
    logger.info(f"Loaded {len(all_stocks)} stocks from master equity list.")

    # 2. Load index constituents
    index_constituents = load_constituents(indices_dir)
    all_symbols = set(all_stocks)
    for df in index_constituents.values():
        all_symbols.update(df["Symbol"].dropna().unique())
    logger.info(f"Indices: {len(index_constituents)}, total unique symbols: {len(all_symbols)}")

    mapped_indices   = {}
    unmapped_indices = {}
    for iname, df in index_constituents.items():
        ticker = ticker_map.get(iname)
        if ticker:
            mapped_indices[iname]   = ticker
        else:
            unmapped_indices[iname] = df

    all_index_tickers = list(mapped_indices.values())
    all_stock_tickers = [f"{sym}.NS" for sym in all_symbols]
    all_tickers = all_stock_tickers + all_index_tickers
    logger.info(f"Total tickers to download: {len(all_tickers)}")

    # 3. Daily data
    logger.info(f"Downloading daily data (period={daily_period})...")
    daily_data = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_cached_or_fresh, t, "1d", daily_period, cache_dir, 24*60): t
            for t in all_tickers
        }
        for future in as_completed(futures):
            t = futures[future]
            try:
                df = future.result()
                if df is not None and not df.empty:
                    daily_data[t] = df
            except Exception as e:
                # A single malformed/delisted Yahoo response must not abort
                # the complete NSE dashboard run.
                logger.warning(f"Daily data failed for {t}: {e}")
    logger.info(f"Downloaded daily data for {len(daily_data)} tickers.")

    # 4. Intraday (live mode only)
    intraday_15m = {}
    intraday_1h  = {}
    if live:
        logger.info(f"Downloading intraday data (period={intraday_period})...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            fut_15 = {executor.submit(get_cached_or_fresh, t, "15m", intraday_period, cache_dir, cache_dur_min): t for t in all_tickers}
            fut_1h = {executor.submit(get_cached_or_fresh, t, "1h",  intraday_period, cache_dir, cache_dur_min): t for t in all_tickers}
            for future in as_completed(list(fut_15.keys()) + list(fut_1h.keys())):
                if future in fut_15:
                    intraday_15m[fut_15[future]] = future.result()
                else:
                    intraday_1h[fut_1h[future]]  = future.result()

    # 5. Compute indicators
    ticker_indicators = {}
    for t in all_tickers:
        if t in daily_data:
            try:
                ticker_indicators[t] = compute_ticker_indicators(
                    t, daily_data[t],
                    intraday_15m.get(t, pd.DataFrame()),
                    intraday_1h.get(t,  pd.DataFrame()))
            except Exception as e:
                logger.warning(f"Indicators failed for {t}: {e}")

    # 6. Synthetic indices
    synthetic_indicators = {}
    synthetic_dfs = {}
    for iname, df in unmapped_indices.items():
        symbols = df["Symbol"].dropna().unique().tolist()
        syn_df  = build_synthetic_index(symbols, daily_data)
        if syn_df.empty:
            logger.warning(f"Synthetic index for {iname} could not be built (no data).")
            continue
        synthetic_dfs[iname]        = syn_df
        synthetic_indicators[iname] = compute_ticker_indicators(iname, syn_df, pd.DataFrame(), pd.DataFrame())
    logger.info(f"Built {len(synthetic_indicators)} synthetic indices.")

    # 7. Build summary + constituent details + save charts
    index_summary       = []
    constituents_detail = {}
    charts_paths        = {}   # {iname: {tf: rel_path}}

    def process_index(iname, idx_ticker, indicators, use_type):
        # Summary row
        summary = {
            "Index":  iname.replace("_", " ").title(),
            "type":   use_type,
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
            summary[key] = _scalar(indicators.get(key))

        score = 0
        for ck in ["daily_rsi_cross","weekly_rsi_cross","monthly_rsi_cross",
                   "daily_macd_34_200_9_cross","daily_macd_34_1000_9_cross",
                   "weekly_macd_cross","monthly_macd_cross"]:
            v = indicators.get(ck)
            if v == "bullish": score += 1
            elif v == "bearish": score -= 1
        for rk in ["daily_rsi","weekly_rsi","monthly_rsi"]:
            if _safe_rsi_gt50(indicators.get(rk)): score += 0.5
        summary["prediction_score"] = score
        index_summary.append(summary)

        # Constituent list
        const_list = []
        df_const = index_constituents.get(iname)
        if df_const is not None:
            for _, row in df_const.iterrows():
                sym  = row["Symbol"]
                tick = f"{sym}.NS"
                ind  = ticker_indicators.get(tick, {})
                cs   = 0
                for ck in ["daily_rsi_cross","weekly_rsi_cross","monthly_rsi_cross",
                           "daily_macd_34_200_9_cross","daily_macd_34_1000_9_cross",
                           "weekly_macd_cross","monthly_macd_cross"]:
                    v = ind.get(ck)
                    if v == "bullish": cs += 1
                    elif v == "bearish": cs -= 1
                for rk in ["daily_rsi","weekly_rsi","monthly_rsi"]:
                    if _safe_rsi_gt50(ind.get(rk)): cs += 0.5
                const_list.append({
                    "Symbol":          sym,
                    "Company":         row.get("Company name",""),
                    "Industry":        row.get("Industry",""),
                    "Series":          row.get("Series",""),
                    "Last Price":      _scalar(ind.get("last_price")),
                    "Daily RSI":       _scalar(ind.get("daily_rsi")),
                    "Weekly RSI":      _scalar(ind.get("weekly_rsi")),
                    "Monthly RSI":     _scalar(ind.get("monthly_rsi")),
                    "D RSI Cross":     ind.get("daily_rsi_cross"),
                    "W RSI Cross":     ind.get("weekly_rsi_cross"),
                    "M RSI Cross":     ind.get("monthly_rsi_cross"),
                    "MACD D(34,200)":  ind.get("daily_macd_34_200_9_cross"),
                    "MACD D(34,1000)": ind.get("daily_macd_34_1000_9_cross"),
                    "MACD W":          ind.get("weekly_macd_cross"),
                    "MACD M":          ind.get("monthly_macd_cross"),
                    "15m RSI":         _scalar(ind.get("15min_rsi")),
                    "1h RSI":          _scalar(ind.get("1h_rsi")),
                    "4h RSI":          _scalar(ind.get("4h_rsi")),
                    "Score":           cs
                })
        constituents_detail[iname] = const_list

        # Build and save charts
        df_daily = (daily_data.get(idx_ticker) if use_type == "actual"
                    else synthetic_dfs.get(iname))
        if df_daily is None or df_daily.empty:
            return
        try:
            df_daily = ensure_close(df_daily)
            if df_daily is None or df_daily.empty:
                return

            label     = iname.replace('_', ' ').title()
            safe_name = _safe_filename(iname)
            tf_paths  = {}

            df_weekly  = resample_ohlc(df_daily, "W")
            df_monthly = resample_ohlc(df_daily, "ME")

            w_rsi_ref = m_rsi_ref = None
            try:
                if not df_weekly.empty and len(df_weekly) >= 14:
                    w_rsi_ref = rsi(df_weekly["Close"], 14)
            except Exception: pass
            try:
                if not df_monthly.empty and len(df_monthly) >= 14:
                    m_rsi_ref = rsi(df_monthly["Close"], 14)
            except Exception: pass

            uc = (use_type == "actual")

            # ── Combined D·W·M chart ──
            try:
                fig_comb = _build_combined_chart_fig(
                    df_daily,
                    df_weekly  if not df_weekly.empty  else None,
                    df_monthly if not df_monthly.empty else None,
                    label, use_candle=uc,
                    weekly_rsi_ref=w_rsi_ref, monthly_rsi_ref=m_rsi_ref)
                tf_paths["combined"] = _save_chart(
                    fig_comb, charts_dir, f"index_{safe_name}_combined",
                    img_width=2700, img_scale=1.2)
                logger.info(f"  Combined chart saved: {tf_paths['combined']}")
            except Exception as e:
                logger.warning(f"Combined chart failed for {iname}: {e}")

            # ── Daily individual chart ──
            try:
                fig_d = _build_individual_chart_fig(
                    df_daily, label, "Daily", use_candle=uc,
                    weekly_rsi_ref=w_rsi_ref, monthly_rsi_ref=m_rsi_ref,
                    macd_params=(34, 200, 9))
                tf_paths["daily"] = _save_chart(
                    fig_d, charts_dir, f"index_{safe_name}_daily",
                    img_width=1800, img_scale=1.5)
            except Exception as e:
                logger.warning(f"Daily chart failed for {iname}: {e}")

            # ── Weekly individual chart ──
            if not df_weekly.empty and len(df_weekly) >= 20:
                try:
                    fig_w = _build_individual_chart_fig(
                        df_weekly, label, "Weekly", use_candle=uc,
                        macd_params=(34, 200, 9))
                    tf_paths["weekly"] = _save_chart(
                        fig_w, charts_dir, f"index_{safe_name}_weekly",
                        img_width=1800, img_scale=1.5)
                except Exception as e:
                    logger.warning(f"Weekly chart failed for {iname}: {e}")

            # ── Monthly individual chart ──
            if not df_monthly.empty and len(df_monthly) >= 14:
                try:
                    fig_m = _build_individual_chart_fig(
                        df_monthly, label, "Monthly", use_candle=uc,
                        macd_params=(12, 26, 9))
                    tf_paths["monthly"] = _save_chart(
                        fig_m, charts_dir, f"index_{safe_name}_monthly",
                        img_width=1800, img_scale=1.5)
                except Exception as e:
                    logger.warning(f"Monthly chart failed for {iname}: {e}")

            charts_paths[iname] = tf_paths

        except Exception as e:
            logger.warning(f"Chart pipeline failed for {iname}: {e}")

    for iname, ticker in mapped_indices.items():
        process_index(iname, ticker, ticker_indicators.get(ticker, {}), "actual")
    for iname in unmapped_indices:
        process_index(iname, None, synthetic_indicators.get(iname, {}), "synthetic")

    logger.info(f"Summary ready for {len(index_summary)} indices.")

    # 8. Stock screener
    logger.info("Building stock screener rows...")
    stock_screener_rows = []
    for sym in all_stocks:
        tick = f"{sym}.NS"
        try:
            row = compute_stock_screener_row(sym, tick, daily_data)
            if row is not None:
                stock_screener_rows.append(row)
        except Exception as e:
            logger.warning(f"Screener row failed for {sym}: {e}")
    stock_screener_rows.sort(key=lambda r: r.get("Score", 0), reverse=True)
    logger.info(f"Stock screener: {len(stock_screener_rows)} stocks scored.")

    # 9. Stock charts (top 200 by score)
    logger.info("Building & saving stock charts (top 200 by score)...")
    stock_chart_paths = {}
    for row in stock_screener_rows[:200]:
        sym   = row["Symbol"]
        tick  = f"{sym}.NS"
        try:
            paths = build_stock_chart(sym, tick, daily_data, charts_dir)
            if paths:
                stock_chart_paths[sym] = paths
        except Exception as e:
            logger.warning(f"Stock chart failed for {sym}: {e}")
    logger.info(f"Saved charts for {len(stock_chart_paths)} stocks → {charts_dir}")

    # 10. Write HTML dashboard
    refresh_seconds = refresh_min * 60 + 10 if live else None
    html_content = generate_html(
        index_summary, constituents_detail, charts_paths,
        stock_screener_rows, stock_chart_paths, refresh_seconds)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"Dashboard HTML written to: {output_html}")
    logger.info(f"Open '{output_html}' in your browser (charts/ folder must stay alongside it).")

# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════

def main():
    live_mode = "-live" in sys.argv

    if len(sys.argv) > 1 and sys.argv[1] not in ("-live",):
        config_file = sys.argv[1]
    else:
        config_file = "config.json"

    if not os.path.exists(config_file):
        logger.error(f"Configuration file '{config_file}' not found.")
        sys.exit(1)

    with open(config_file, "r") as f:
        config = json.load(f)

    if live_mode:
        logger.info("*** LIVE MODE *** Auto-refresh every {} minutes.".format(
            config.get("refresh_interval_minutes", 15)))
        while True:
            start = time.time()
            try:
                run_analysis_cycle(config, live=True)
            except Exception as e:
                logger.exception(f"Analysis cycle failed: {e}")
            elapsed    = time.time() - start
            sleep_time = max(0, config.get("refresh_interval_minutes", 15) * 60 - elapsed)
            logger.info(f"Sleeping {sleep_time/60:.1f} minutes until next refresh...")
            time.sleep(sleep_time)
    else:
        logger.info("*** DEFAULT MODE *** Running single analysis...")
        run_analysis_cycle(config, live=False)
        logger.info("Done. Open index_analysis.html in your browser.")

if __name__ == "__main__":
    main()