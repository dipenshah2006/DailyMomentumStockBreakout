#!/usr/bin/env python3
"""
fo_scanner_report.py
====================
F&O-only multi-indicator breakout scanner for NSE.

Indicators
----------
- Trend Channel (linear regression ±2σ)
- Support / Resistance (pivot clustering)
- Darvas Box breakout
- Bollinger Band (20,2) upper breakout
- Donchian Channel (20) breakout
- RSI-7, RSI-34, RSI-200
  • RSI-7 × RSI-34 crossover            → BUY
  • RSI-7 × RSI-34  + RSI-200 > 50      → STRONG BUY
  • RSI-7 crosses above 70               → MOMENTUM
- MACD (34, 200, 9) bullish
- Chande Kroll Stop (ATR-10, mult-1, stop-9)
- Volume Oscillator (EMA-5 − EMA-10) zero-cross up
- Fibonacci Extension  on 15-min (bullish targets)
- Fibonacci Retracement on 15-min (bearish targets)

Output
------
  fo_report.html              — main report
  charts/fo/<SYM>.png         — daily chart per stock (top 60)
  charts/fo/<SYM>_15m.png     — 15-min Fib chart per stock (top 60)
"""

import os, sys, csv, io, time, pickle, logging, warnings, traceback
import concurrent.futures
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import requests

warnings.filterwarnings("ignore")

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
LOCAL_FO_CSV   = "india/NSE/nse_fo_list.csv"
CACHE_FILE     = "stock_data_cache.pkl"      # shared daily cache (v2 format)
CACHE_15M_FILE = "fo_15m_cache.pkl"          # 15-min data cache
REPORT_FILE    = "fo_report.html"
CHARTS_DIR     = "charts/fo"
DATA_PERIOD    = "max"
PERIOD_15M     = "60d"                       # yf limit for 15-min interval
TOP_N_CHARTS   = 60                          # generate charts for top-N stocks
MAX_WORKERS    = 12                          # parallel analysis workers
CACHE_15M_TTL  = 4 * 3600                   # 4-hour TTL for 15-min cache

os.makedirs(CHARTS_DIR, exist_ok=True)

# ── Global state ──────────────────────────────────────────────────────────────
_CACHE:     dict = {}
_CACHE_15M: dict = {}
_FO_SET:    set  = set()
_CACHE_DIRTY: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# 1. F&O UNIVERSE  (logic copied from rsi_mtf_report_nse.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_fo_list():
    """Load NSE F&O stock universe into _FO_SET."""
    global _FO_SET

    # ── Local CSV first ────────────────────────────────────────────────────
    if os.path.exists(LOCAL_FO_CSV):
        try:
            with open(LOCAL_FO_CSV, encoding="utf-8", errors="replace") as fh:
                raw = fh.read().lstrip("\ufeff")
            reader = csv.DictReader(io.StringIO(raw))
            loaded: set[str] = set()
            for row in reader:
                clean = {k.strip().upper(): (v.strip() if v else "")
                         for k, v in row.items() if k}
                sym = clean.get("SYMBOL", "")
                if sym:
                    loaded.add(sym.upper())
            _FO_SET = loaded
            log.info(f"  ✅ F&O list: {len(_FO_SET)} symbols ← '{LOCAL_FO_CSV}'")
            return
        except Exception as e:
            log.warning(f"  [!] Error reading '{LOCAL_FO_CSV}': {e}")

    # ── NSE archives (works from GitHub Actions) ───────────────────────────
    _INDEX_SYMS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                   "NIFTYNXT50", "SENSEX", "BANKEX"}
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                           "Referer": "https://www.nseindia.com/"})
        r = s.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
                  timeout=20, verify=False)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        symbols: set[str] = set()
        rows_raw = []
        for row in reader:
            clean = {k.strip(): (v.strip() if v else "") for k, v in row.items()}
            sym = clean.get("SYMBOL", "").upper()
            if sym and sym not in _INDEX_SYMS:
                symbols.add(sym)
                rows_raw.append({"SYMBOL": sym,
                                 "UNDERLYING": clean.get("UNDERLYING", "")})
        if symbols:
            _FO_SET = symbols
            log.info(f"  ✅ F&O list: {len(_FO_SET)} symbols from NSE archives")
            try:
                os.makedirs(os.path.dirname(LOCAL_FO_CSV), exist_ok=True)
                pd.DataFrame(rows_raw).drop_duplicates("SYMBOL")\
                  .to_csv(LOCAL_FO_CSV, index=False)
            except Exception:
                pass
            return
    except Exception as e:
        log.warning(f"  [!] NSE archives failed: {e}")

    # ── NSE API (may be geo-blocked) ──────────────────────────────────────
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0",
                           "Accept": "application/json, */*",
                           "Referer": "https://www.nseindia.com/"})
        s.get("https://www.nseindia.com/", timeout=10)
        r = s.get("https://www.nseindia.com/api/foSecList", timeout=15)
        r.raise_for_status()
        data = r.json()
        rows = data.get("data", data) if isinstance(data, dict) else data
        symbols = {str(row.get("SYMBOL", "")).strip().upper()
                   for row in rows if row.get("SYMBOL")}
        symbols.discard("")
        if symbols:
            _FO_SET = symbols
            log.info(f"  ✅ F&O list: {len(_FO_SET)} symbols from NSE API")
            return
    except Exception as e:
        log.warning(f"  [!] NSE API failed: {e}")

    log.error("  ❌ Could not load F&O list from any source.")
    _FO_SET = set()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DAILY CACHE ENGINE  (shared stock_data_cache.pkl, v2 format)
# ═══════════════════════════════════════════════════════════════════════════════

def _last_trading_day_str() -> str:
    d = date.today()
    if d.weekday() == 5:
        d -= timedelta(days=1)
    elif d.weekday() == 6:
        d -= timedelta(days=2)
    return d.isoformat()


def _load_cache_v2() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "rb") as fh:
            raw = pickle.load(fh)
        # Migrate old v1 (plain dict of DataFrames)
        if isinstance(raw, dict) and raw.get("__version__") != 2:
            migrated: dict = {"__version__": 2}
            for k, v in raw.items():
                if k.startswith("__"):
                    continue
                if isinstance(v, pd.DataFrame) and not v.empty:
                    last = v.index[-1].date().isoformat() if len(v) else ""
                    migrated[k] = {"df": v, "last_date": last,
                                   "marketcap": None, "mcap_ts": 0.0}
            log.info(f"  🔄 Migrated {len(migrated)-1} entries from cache v1 → v2")
            return migrated
        # Repair mixed-state v2
        for k, v in list(raw.items()):
            if not k.startswith("__") and isinstance(v, pd.DataFrame):
                last = v.index[-1].date().isoformat() if len(v) else ""
                raw[k] = {"df": v, "last_date": last,
                           "marketcap": None, "mcap_ts": 0.0}
        return raw
    except Exception as e:
        log.warning(f"  [!] Daily cache load error: {e} — starting fresh")
        return {}


def _save_cache_v2():
    global _CACHE_DIRTY
    try:
        with open(CACHE_FILE, "wb") as fh:
            pickle.dump(_CACHE, fh, protocol=pickle.HIGHEST_PROTOCOL)
        _CACHE_DIRTY = False
    except Exception as e:
        log.warning(f"  [!] Cache save error: {e}")


def _get_daily_df(ticker: str) -> pd.DataFrame | None:
    entry = _CACHE.get(ticker)
    if isinstance(entry, dict):
        df = entry.get("df")
        return df if (df is not None and not df.empty) else None
    if isinstance(entry, pd.DataFrame):
        return entry if not entry.empty else None
    return None


def _is_cached_fresh(ticker: str) -> bool:
    entry = _CACHE.get(ticker)
    if not isinstance(entry, dict):
        return False
    return entry.get("last_date", "") >= _last_trading_day_str()


def _batch_download(tickers: list[str], period: str = "max") -> dict[str, pd.DataFrame]:
    ns_tickers = [t + ".NS" for t in tickers]
    try:
        raw = yf.download(
            ns_tickers, period=period, interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        result: dict[str, pd.DataFrame] = {}
        for t, ns in zip(tickers, ns_tickers):
            try:
                df = raw[ns] if isinstance(raw.columns, pd.MultiIndex) else raw
                if isinstance(df.columns, pd.MultiIndex):
                    df = df.droplevel(1, axis=1)
                df = df.dropna(subset=["Close"])
                if not df.empty:
                    result[t] = df
            except Exception:
                pass
        return result
    except Exception as e:
        log.warning(f"  [!] Batch download error: {e}")
        return {}


def prefetch_daily(tickers: list[str]):
    """Download stale/missing tickers in batches of 50, reusing shared cache."""
    global _CACHE_DIRTY
    stale = [t for t in tickers if not _is_cached_fresh(t)]
    if not stale:
        log.info(f"  ✅ All {len(tickers)} F&O tickers already cached and fresh.")
        return

    log.info(f"  ⬇️  Downloading {len(stale)} stale/missing tickers in batches…")
    BATCH_SIZE = 50
    batches = [stale[i:i + BATCH_SIZE] for i in range(0, len(stale), BATCH_SIZE)]
    for idx, batch in enumerate(batches, 1):
        log.info(f"    Batch {idx}/{len(batches)} — {len(batch)} tickers")
        data = _batch_download(batch, DATA_PERIOD)
        for t, new_df in data.items():
            existing = _get_daily_df(t)
            if existing is not None and not existing.empty:
                new_rows = new_df[~new_df.index.isin(existing.index)]
                new_df = pd.concat([existing, new_rows]).sort_index()
            if isinstance(new_df.columns, pd.MultiIndex):
                try:
                    new_df = new_df.droplevel(1, axis=1)
                except Exception:
                    pass
            entry = _CACHE.get(t)
            if not isinstance(entry, dict):
                entry = {}
            entry["df"]        = new_df
            entry["last_date"] = new_df.index[-1].date().isoformat()
            _CACHE[t]   = entry
            _CACHE_DIRTY = True
        time.sleep(0.3)

    if _CACHE_DIRTY:
        _save_cache_v2()
        log.info("  💾 Daily cache updated and saved.")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 15-MIN DATA CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def _load_15m_cache():
    global _CACHE_15M
    if not os.path.exists(CACHE_15M_FILE):
        _CACHE_15M = {}
        return
    try:
        with open(CACHE_15M_FILE, "rb") as fh:
            _CACHE_15M = pickle.load(fh)
        log.info(f"  ✅ 15-min cache loaded: {len(_CACHE_15M)} entries")
    except Exception as e:
        log.warning(f"  [!] 15-min cache error: {e}")
        _CACHE_15M = {}


def _save_15m_cache():
    try:
        with open(CACHE_15M_FILE, "wb") as fh:
            pickle.dump(_CACHE_15M, fh, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        log.warning(f"  [!] 15-min cache save error: {e}")


def get_15m_df(ticker: str) -> pd.DataFrame | None:
    """Return 15-min DataFrame from cache or download fresh."""
    entry = _CACHE_15M.get(ticker)
    if entry:
        if time.time() - entry.get("ts", 0) < CACHE_15M_TTL:
            df = entry.get("df")
            if df is not None and not df.empty:
                return df

    try:
        df = yf.download(
            ticker + ".NS", period=PERIOD_15M, interval="15m",
            auto_adjust=True, progress=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        df = df.dropna(subset=["Close"])
        if df.empty:
            return None
        _CACHE_15M[ticker] = {"df": df, "ts": time.time()}
        return df
    except Exception as e:
        log.debug(f"    15m download failed for {ticker}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def rsi(series: pd.Series, period: int) -> pd.Series:
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_l  = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def macd_calc(series: pd.Series, fast: int = 34, slow: int = 200, sig: int = 9):
    ema_f  = series.ewm(span=fast, adjust=False).mean()
    ema_s  = series.ewm(span=slow, adjust=False).mean()
    line   = ema_f - ema_s
    signal = line.ewm(span=sig, adjust=False).mean()
    hist   = line - signal
    return line, signal, hist


def bollinger(series: pd.Series, period: int = 20, width: float = 2.0):
    mid   = series.rolling(period).mean()
    std   = series.rolling(period).std(ddof=0)
    upper = mid + width * std
    lower = mid - width * std
    return lower, mid, upper


def donchian(high: pd.Series, low: pd.Series, period: int = 20):
    upper = high.rolling(period).max().shift(1)   # shift-1 avoids lookahead
    lower = low.rolling(period).min().shift(1)
    mid   = (upper + lower) / 2
    return upper, mid, lower


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def chande_kroll_stop(high: pd.Series, low: pd.Series, close: pd.Series,
                      atr_p: int = 10, atr_m: float = 1.0, stop_p: int = 9):
    """
    Returns (stop_long, stop_short):
      stop_long  — dynamic bull support  (price above = bullish)
      stop_short — dynamic bear resist.  (price above = strongly bullish)
    """
    a    = _atr(high, low, close, atr_p)
    hh   = high.rolling(atr_p).max()
    ll   = low.rolling(atr_p).min()
    fhs  = hh - atr_m * a           # First High Stop
    fls  = ll + atr_m * a           # First Low Stop
    stop_short = fhs.rolling(stop_p).max()   # CK Short (resistance)
    stop_long  = fls.rolling(stop_p).min()   # CK Long  (support)
    return stop_long, stop_short


def volume_oscillator(vol: pd.Series, fast: int = 5, slow: int = 10) -> pd.Series:
    ema_f = vol.ewm(span=fast, adjust=False).mean()
    ema_s = vol.ewm(span=slow, adjust=False).mean()
    return (ema_f - ema_s) / ema_s.replace(0, 1e-9) * 100


def trend_channel(close: pd.Series, period: int = 50):
    """Linear regression channel on last `period` bars.
    Returns (regression, upper, lower, slope).
    """
    n = min(period, len(close))
    y = close.iloc[-n:].values.astype(float)
    x = np.arange(n, dtype=float)
    m, b  = np.polyfit(x, y, 1)
    regr  = pd.Series(m * x + b, index=close.index[-n:])
    resid = close.iloc[-n:] - regr
    s     = resid.std()
    return regr, regr + 2 * s, regr - 2 * s, float(m)


def find_sr_levels(high: pd.Series, low: pd.Series,
                   n: int = 10, tolerance: float = 0.015,
                   max_levels: int = 8) -> list[float]:
    """Pivot-based support/resistance via clustering."""
    pivots: list[float] = []
    for i in range(n, len(high) - n):
        if high.iloc[i] >= high.iloc[i - n:i + n + 1].max():
            pivots.append(float(high.iloc[i]))
        if low.iloc[i] <= low.iloc[i - n:i + n + 1].min():
            pivots.append(float(low.iloc[i]))
    if not pivots:
        return []
    pivots.sort()
    levels: list[float] = []
    cluster: list[float] = [pivots[0]]
    for p in pivots[1:]:
        if abs(p - cluster[-1]) / (cluster[-1] + 1e-9) < tolerance:
            cluster.append(p)
        else:
            levels.append(float(np.mean(cluster)))
            cluster = [p]
    levels.append(float(np.mean(cluster)))
    return sorted(levels)[-max_levels:]


def find_darvas_top(high: pd.Series, box_period: int = 15) -> float | None:
    """Return the top of the most recent confirmed Darvas Box."""
    boxes: list[float] = []
    n = len(high)
    for i in range(box_period, n - 3):
        box_top = float(high.iloc[i - box_period:i].max())
        if float(high.iloc[i]) > box_top:
            continue
        end = min(i + 4, n)
        if all(float(high.iloc[i + j]) <= box_top for j in range(1, end - i)):
            boxes.append(box_top)
    return boxes[-1] if boxes else None


def fibonacci_levels(swing_low: float, swing_high: float):
    """Retracement (bearish) and Extension (bullish) Fibonacci levels."""
    diff = swing_high - swing_low
    retrace = {
        "0%":    swing_high,
        "23.6%": swing_high - 0.236 * diff,
        "38.2%": swing_high - 0.382 * diff,
        "50%":   swing_high - 0.500 * diff,
        "61.8%": swing_high - 0.618 * diff,
        "78.6%": swing_high - 0.786 * diff,
        "100%":  swing_low,
    }
    extend = {
        "127.2%": swing_high + 0.272 * diff,
        "161.8%": swing_high + 0.618 * diff,
        "200%":   swing_high + 1.000 * diff,
        "261.8%": swing_high + 1.618 * diff,
    }
    return retrace, extend


# ═══════════════════════════════════════════════════════════════════════════════
# 5. SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def analyse_ticker(sym: str) -> dict | None:
    """Compute all indicators for one ticker. Returns result dict or None."""
    df = _get_daily_df(sym)
    if df is None or len(df) < 50:
        return None

    try:
        close  = df["Close"].astype(float)
        high   = df["High"].astype(float)
        low    = df["Low"].astype(float)
        volume = df["Volume"].astype(float)

        # ── RSI ─────────────────────────────────────────────────────────────
        r7   = rsi(close, 7)
        r34  = rsi(close, 34)
        r200 = rsi(close, 200)

        cur_r7,   prev_r7  = float(r7.iloc[-1]),   float(r7.iloc[-2])
        cur_r34,  prev_r34 = float(r34.iloc[-1]),  float(r34.iloc[-2])
        cur_r200           = float(r200.iloc[-1])

        rsi7_cross_rsi34 = (cur_r7  > cur_r34)  and (prev_r7  <= prev_r34)
        rsi7_strong_buy  = rsi7_cross_rsi34 and (cur_r200 > 50)
        rsi7_momentum    = (cur_r7  > 70) and (prev_r7 <= 70)

        # ── MACD(34,200,9) ──────────────────────────────────────────────────
        m_line, m_sig, m_hist = macd_calc(close, 34, 200, 9)
        macd_bull     = float(m_line.iloc[-1]) > float(m_sig.iloc[-1])
        macd_cross_up = macd_bull and (float(m_line.iloc[-2]) <= float(m_sig.iloc[-2]))

        # ── Bollinger Bands(20,2) ───────────────────────────────────────────
        bb_lo, bb_mid, bb_hi = bollinger(close, 20, 2.0)
        cur_close    = float(close.iloc[-1])
        cur_bb_hi    = float(bb_hi.iloc[-1])
        cur_bb_lo    = float(bb_lo.iloc[-1])
        bb_breakout  = cur_close > cur_bb_hi
        bb_range     = cur_bb_hi - cur_bb_lo
        bb_pct       = ((cur_close - cur_bb_lo) / (bb_range + 1e-9)) * 100

        # ── Donchian Channel(20) ────────────────────────────────────────────
        don_hi, don_mid, don_lo = donchian(high, low, 20)
        don_breakout = cur_close > float(don_hi.iloc[-1])

        # ── Chande Kroll Stop ───────────────────────────────────────────────
        ck_long, ck_short = chande_kroll_stop(high, low, close, 10, 1.0, 9)
        ck_bull = cur_close > float(ck_short.iloc[-1])

        # ── Volume Oscillator ───────────────────────────────────────────────
        vo       = volume_oscillator(volume, 5, 10)
        vo_val   = float(vo.iloc[-1])
        vo_prev  = float(vo.iloc[-2])
        vo_bull      = vo_val > 0
        vo_cross_up  = vo_bull and (vo_prev <= 0)

        # ── Trend Channel ───────────────────────────────────────────────────
        regr, ch_up, ch_lo, slope = trend_channel(close, 50)
        if   cur_close > float(ch_up.iloc[-1]):  channel_pos = "above"
        elif cur_close < float(ch_lo.iloc[-1]):  channel_pos = "below"
        else:                                    channel_pos = "within"

        # ── S/R ─────────────────────────────────────────────────────────────
        sr_levels = find_sr_levels(high, low, n=10)

        # ── Darvas Box ──────────────────────────────────────────────────────
        darvas_top   = find_darvas_top(high, 15)
        darvas_break = (darvas_top is not None) and (cur_close > darvas_top)

        # ── Score & Signals ─────────────────────────────────────────────────
        score   = 0
        signals: list[str] = []

        if rsi7_strong_buy:
            score += 30
            signals.append("🔥 STRONG BUY (RSI7 × RSI34 + RSI200>50)")
        elif rsi7_cross_rsi34:
            score += 20
            signals.append("✅ BUY (RSI7 cross RSI34)")

        if rsi7_momentum:
            score += 10
            signals.append("⚡ MOMENTUM (RSI7 > 70)")

        if macd_cross_up:
            score += 15
            signals.append("📈 MACD Cross Up (34,200,9)")
        elif macd_bull:
            score += 8
            signals.append("📈 MACD Bullish")

        if ck_bull:
            score += 10
            signals.append("🛡️ Chande Kroll Bullish")

        if vo_cross_up:
            score += 10
            signals.append("📊 Vol Osc Zero Cross ↑")
        elif vo_bull:
            score += 5
            signals.append("📊 Vol Osc Positive")

        if bb_breakout:
            score += 15
            signals.append("🚀 BB Upper Breakout")

        if don_breakout:
            score += 15
            signals.append("💎 Donchian 20 Breakout")

        if darvas_break:
            score += 20
            signals.append("🎯 Darvas Box Breakout")

        if slope > 0 and channel_pos == "above":
            score += 5
            signals.append("📐 Above Rising Channel")
        elif slope > 0:
            score += 2

        score = min(score, 100)

        if score >= 60:
            signal_tag = "STRONG BUY"
        elif score >= 40:
            signal_tag = "BUY"
        elif score >= 25:
            signal_tag = "BULLISH"
        elif score >= 10:
            signal_tag = "WATCH"
        else:
            signal_tag = "HOLD"

        return {
            # Identity
            "sym":   sym,
            "close": cur_close,
            "score": score,
            "signal": signal_tag,
            "signals": signals,
            # RSI
            "r7": cur_r7, "r34": cur_r34, "r200": cur_r200,
            "rsi7_cross_rsi34": rsi7_cross_rsi34,
            "rsi7_strong_buy":  rsi7_strong_buy,
            "rsi7_momentum":    rsi7_momentum,
            # MACD
            "macd_line":    float(m_line.iloc[-1]),
            "macd_sig":     float(m_sig.iloc[-1]),
            "macd_hist":    float(m_hist.iloc[-1]),
            "macd_bull":    macd_bull,
            "macd_cross_up": macd_cross_up,
            # BB
            "bb_hi": cur_bb_hi, "bb_pct": bb_pct, "bb_breakout": bb_breakout,
            # Donchian
            "don_hi": float(don_hi.iloc[-1]), "don_breakout": don_breakout,
            # CK
            "ck_long":  float(ck_long.iloc[-1]),
            "ck_short": float(ck_short.iloc[-1]),
            "ck_bull":  ck_bull,
            # Vol Osc
            "vo": vo_val, "vo_bull": vo_bull, "vo_cross_up": vo_cross_up,
            # Trend
            "slope": slope, "channel": channel_pos,
            # SR / Darvas
            "sr_levels":   sr_levels,
            "darvas_top":  darvas_top,
            "darvas_break": darvas_break,
            # Raw series for chart generation (stripped before HTML write)
            "_df":       df,
            "_r7":       r7,  "_r34": r34, "_r200": r200,
            "_macd_l":   m_line, "_macd_s": m_sig, "_macd_h": m_hist,
            "_bb_lo":    bb_lo,  "_bb_hi": bb_hi,
            "_don_hi":   don_hi, "_don_lo": don_lo,
            "_ck_long":  ck_long, "_ck_short": ck_short,
            "_vo":       vo,
            "_regr":     regr, "_ch_up": ch_up, "_ch_lo": ch_lo,
        }

    except Exception as e:
        log.debug(f"  [!] Analysis error for {sym}: {e}\n{traceback.format_exc()}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CHART GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

_DARK   = "#0f172a"
_CARD   = "#1e293b"
_GRID   = "#1e3a5f"
_TEXT   = "#e2e8f0"
_BULL   = "#22c55e"
_BEAR   = "#ef4444"
_BLUE   = "#38bdf8"
_AMBER  = "#fbbf24"
_PURPLE = "#a855f7"
_PINK   = "#ec4899"


def _style_ax(ax, title: str = "", ylabel: str = ""):
    ax.set_facecolor(_CARD)
    ax.tick_params(colors=_TEXT, labelsize=7)
    for sp in ax.spines.values():
        sp.set_color(_GRID)
    ax.grid(True, color=_GRID, linewidth=0.4, alpha=0.5)
    if title:
        ax.set_title(title, color=_TEXT, fontsize=9, pad=3)
    if ylabel:
        ax.set_ylabel(ylabel, color=_TEXT, fontsize=7)


def draw_daily_chart(res: dict, out_path: str):
    df    = res["_df"].copy()
    N     = min(200, len(df))
    plot  = df.iloc[-N:]
    xi    = range(N)

    fig = plt.figure(figsize=(14, 12), facecolor=_DARK)
    gs  = GridSpec(5, 1, figure=fig, hspace=0.05,
                   height_ratios=[4, 1.2, 1.2, 1, 0.9])

    ax1 = fig.add_subplot(gs[0])   # price / overlays
    ax2 = fig.add_subplot(gs[1], sharex=ax1)   # RSI
    ax3 = fig.add_subplot(gs[2], sharex=ax1)   # MACD
    ax4 = fig.add_subplot(gs[3], sharex=ax1)   # Vol Osc
    ax5 = fig.add_subplot(gs[4], sharex=ax1)   # Volume bars

    # ── Price / candlestick ────────────────────────────────────────────────
    _style_ax(ax1, ylabel="Price ₹")
    for i, (_, row) in enumerate(plot.iterrows()):
        op = float(row.get("Open", row["Close"]))
        hi, lo, cl = float(row["High"]), float(row["Low"]), float(row["Close"])
        c = _BULL if cl >= op else _BEAR
        ax1.plot([i, i], [lo, hi], color=c, linewidth=0.6)
        ax1.add_patch(plt.Rectangle(
            (i - 0.3, min(op, cl)), 0.6, abs(cl - op), color=c, alpha=0.85))

    def _tail(series, n):
        return series.iloc[-n:].values

    # Bollinger Bands
    ax1.fill_between(xi, _tail(res["_bb_lo"], N), _tail(res["_bb_hi"], N),
                     alpha=0.07, color=_BLUE)
    ax1.plot(xi, _tail(res["_bb_hi"], N), color=_BLUE, lw=0.8, ls="--", alpha=0.7, label="BB(20,2)")
    ax1.plot(xi, _tail(res["_bb_lo"], N), color=_BLUE, lw=0.8, ls="--", alpha=0.7)

    # Donchian Channel
    ax1.plot(xi, _tail(res["_don_hi"], N), color=_AMBER, lw=0.8, ls=":", alpha=0.7, label="Donchian(20)")
    ax1.plot(xi, _tail(res["_don_lo"], N), color=_AMBER, lw=0.8, ls=":", alpha=0.7)

    # Chande Kroll Stop
    ax1.plot(xi, _tail(res["_ck_long"],  N), color=_BULL, lw=1.0, ls="-.", alpha=0.75, label="CK Long")
    ax1.plot(xi, _tail(res["_ck_short"], N), color=_BEAR, lw=1.0, ls="-.", alpha=0.75, label="CK Short")

    # Trend Channel
    rlen = len(res["_regr"])
    xr   = list(range(N - rlen, N))
    ax1.fill_between(xr, res["_ch_lo"].values, res["_ch_up"].values,
                     alpha=0.05, color=_PURPLE)
    ax1.plot(xr, res["_regr"].values, color=_PURPLE, lw=0.9, label="Regression")
    ax1.plot(xr, res["_ch_up"].values, color=_PURPLE, lw=0.6, ls="--", alpha=0.5)
    ax1.plot(xr, res["_ch_lo"].values, color=_PURPLE, lw=0.6, ls="--", alpha=0.5)

    # S/R levels
    for lvl in res["sr_levels"]:
        ax1.axhline(lvl, color=_TEXT, lw=0.5, ls="--", alpha=0.3)

    # Darvas top
    if res["darvas_top"]:
        ax1.axhline(res["darvas_top"], color=_PINK, lw=1.0, ls="--", alpha=0.75, label="Darvas Top")

    ax1.legend(loc="upper left", fontsize=6.5, facecolor=_CARD,
               edgecolor=_GRID, labelcolor=_TEXT, ncol=4)
    ax1.set_title(
        f"{res['sym']}  ₹{res['close']:.2f}  |  Score {res['score']}/100  "
        f"|  {res['signal']}  |  {date.today().isoformat()}",
        color=_TEXT, fontsize=9, pad=4)

    # ── RSI panel ──────────────────────────────────────────────────────────
    _style_ax(ax2, ylabel="RSI")
    ax2.plot(xi, _tail(res["_r7"],   N), color=_BULL,  lw=0.9, label="RSI-7")
    ax2.plot(xi, _tail(res["_r34"],  N), color=_AMBER, lw=0.9, label="RSI-34")
    ax2.plot(xi, _tail(res["_r200"], N), color=_PINK,  lw=0.9, label="RSI-200")
    for lvl, c in [(70, _BEAR), (50, _TEXT), (30, _BULL)]:
        ax2.axhline(lvl, color=c, lw=0.6, ls="--", alpha=0.45)
    ax2.set_ylim(0, 100)
    ax2.legend(loc="upper left", fontsize=6, facecolor=_CARD,
               edgecolor=_GRID, labelcolor=_TEXT, ncol=3)

    # ── MACD panel ─────────────────────────────────────────────────────────
    _style_ax(ax3, ylabel="MACD(34,200,9)")
    ml = _tail(res["_macd_l"], N)
    ms = _tail(res["_macd_s"], N)
    mh = _tail(res["_macd_h"], N)
    ax3.plot(xi, ml, color=_BLUE,  lw=0.9, label="MACD")
    ax3.plot(xi, ms, color=_AMBER, lw=0.9, label="Signal")
    ax3.bar(xi, mh,
            color=[_BULL if v >= 0 else _BEAR for v in mh],
            alpha=0.6, width=0.8)
    ax3.axhline(0, color=_TEXT, lw=0.5, alpha=0.4)
    ax3.legend(loc="upper left", fontsize=6, facecolor=_CARD,
               edgecolor=_GRID, labelcolor=_TEXT, ncol=2)

    # ── Volume Oscillator panel ────────────────────────────────────────────
    _style_ax(ax4, ylabel="Vol Osc %")
    vo_vals = _tail(res["_vo"], N)
    ax4.bar(xi, vo_vals,
            color=[_BULL if v >= 0 else _BEAR for v in vo_vals],
            alpha=0.7, width=0.8)
    ax4.axhline(0, color=_TEXT, lw=0.6, alpha=0.5)

    # ── Volume bars ────────────────────────────────────────────────────────
    _style_ax(ax5, ylabel="Volume")
    ax5.bar(xi, plot["Volume"].astype(float).values, color=_BLUE, alpha=0.45, width=0.8)

    dates = [str(idx)[:10] for idx in plot.index]
    step  = max(1, N // 10)
    ax5.set_xticks(range(0, N, step))
    ax5.set_xticklabels([dates[i] for i in range(0, N, step)],
                        rotation=30, ha="right", fontsize=6, color=_TEXT)
    for ax in (ax1, ax2, ax3, ax4):
        plt.setp(ax.get_xticklabels(), visible=False)

    plt.tight_layout()
    plt.savefig(out_path, dpi=100, bbox_inches="tight", facecolor=_DARK)
    plt.close(fig)


def draw_15m_chart(sym: str, df15: pd.DataFrame, out_path: str):
    """15-min chart with Fibonacci Extension (bullish) & Retracement (bearish)."""
    if df15 is None or len(df15) < 20:
        return

    N    = min(100, len(df15))
    plot = df15.iloc[-N:]
    high_arr  = plot["High"].astype(float).values
    low_arr   = plot["Low"].astype(float).values
    xi        = range(N)

    # Swing high & low in visible window
    sh_idx = int(np.argmax(high_arr))
    sl_idx = int(np.argmin(low_arr))
    swing_high = high_arr[sh_idx]
    swing_low  = low_arr[sl_idx]

    retrace, extend = fibonacci_levels(swing_low, swing_high)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 9), facecolor=_DARK,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.04})
    _style_ax(ax1, ylabel="Price ₹")
    _style_ax(ax2, ylabel="Volume")

    # Candlestick
    for i, (_, row) in enumerate(plot.iterrows()):
        op = float(row.get("Open", row["Close"]))
        hi, lo, cl = float(row["High"]), float(row["Low"]), float(row["Close"])
        c = _BULL if cl >= op else _BEAR
        ax1.plot([i, i], [lo, hi], color=c, linewidth=0.6)
        ax1.add_patch(plt.Rectangle(
            (i - 0.3, min(op, cl)), 0.6, abs(cl - op), color=c, alpha=0.8))

    # Mark swing points
    ax1.scatter([sh_idx], [swing_high], color=_BEAR, s=65, zorder=5, marker="v",
                label=f"Swing H ₹{swing_high:.1f}")
    ax1.scatter([sl_idx], [swing_low],  color=_BULL, s=65, zorder=5, marker="^",
                label=f"Swing L ₹{swing_low:.1f}")

    # Retracement levels (bearish, warm colours)
    retrace_palette = ["#ef4444", "#f97316", "#fbbf24", "#a3e635", "#22c55e", "#14b8a6", "#38bdf8"]
    for (lbl, lvl), col in zip(retrace.items(), retrace_palette):
        ax1.axhline(lvl, color=col, lw=0.8, ls="--", alpha=0.8)
        ax1.text(N - 0.5, lvl, f"  {lbl} ₹{lvl:.1f}",
                 color=col, fontsize=6.5, va="center", ha="left")

    # Extension levels (bullish, cool colours)
    ext_palette = ["#818cf8", "#a855f7", "#ec4899", "#f43f5e"]
    for (lbl, lvl), col in zip(extend.items(), ext_palette):
        ax1.axhline(lvl, color=col, lw=0.9, ls=":", alpha=0.85)
        ax1.text(N - 0.5, lvl, f"  {lbl} ₹{lvl:.1f}",
                 color=col, fontsize=6.5, va="center", ha="left")

    ax1.legend(loc="upper left", fontsize=7, facecolor=_CARD,
               edgecolor=_GRID, labelcolor=_TEXT)
    ax1.set_title(
        f"{sym} — 15-min  |  Swing H ₹{swing_high:.1f}  Swing L ₹{swing_low:.1f}  "
        f"|  Fib Extension & Retracement",
        color=_TEXT, fontsize=9, pad=4)

    # Volume
    ax2.bar(xi, plot["Volume"].astype(float).values, color=_BLUE, alpha=0.5, width=0.8)
    dates = [str(idx)[:16] for idx in plot.index]
    step  = max(1, N // 8)
    ax2.set_xticks(range(0, N, step))
    ax2.set_xticklabels([dates[i] for i in range(0, N, step)],
                        rotation=30, ha="right", fontsize=6, color=_TEXT)
    plt.setp(ax1.get_xticklabels(), visible=False)

    plt.tight_layout()
    plt.savefig(out_path, dpi=90, bbox_inches="tight", facecolor=_DARK)
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. HTML REPORT
# ═══════════════════════════════════════════════════════════════════════════════

_SIG_STYLE = {
    "STRONG BUY": ("color:#22c55e;background:#052e16;border:1px solid #22c55e"),
    "BUY":        ("color:#38bdf8;background:#0c1a26;border:1px solid #38bdf8"),
    "BULLISH":    ("color:#fbbf24;background:#1c1500;border:1px solid #fbbf24"),
    "WATCH":      ("color:#94a3b8;background:#1e293b;border:1px solid #475569"),
    "HOLD":       ("color:#475569;background:#1e293b;border:1px solid #334155"),
}


def _sig_badge(signal: str) -> str:
    style = _SIG_STYLE.get(signal, _SIG_STYLE["HOLD"])
    return f'<span style="{style};padding:3px 10px;border-radius:20px;font-size:0.7rem;font-weight:700">{signal}</span>'


def _clr(val: float, lo: float = 50.0) -> str:
    return "#22c55e" if val >= lo else "#ef4444"


def build_html(results: list[dict], gen_time: str) -> str:
    n_sb  = sum(1 for r in results if r["signal"] == "STRONG BUY")
    n_buy = sum(1 for r in results if r["signal"] == "BUY")
    n_bl  = sum(1 for r in results if r["signal"] == "BULLISH")
    n_w   = sum(1 for r in results if r["signal"] == "WATCH")

    rows_html = ""
    for rank, r in enumerate(results, 1):
        chart_path   = os.path.join(CHARTS_DIR, f"{r['sym']}.png")
        chart15_path = os.path.join(CHARTS_DIR, f"{r['sym']}_15m.png")
        chart_btn    = (f'<a href="charts/fo/{r["sym"]}.png" target="_blank" class="cbtn">📊 Daily</a>'
                        if os.path.exists(chart_path) else "")
        chart15_btn  = (f'<a href="charts/fo/{r["sym"]}_15m.png" target="_blank" class="cbtn green">📐 15m Fib</a>'
                        if os.path.exists(chart15_path) else "")

        sigs = "<br>".join(r["signals"]) if r["signals"] else "—"
        darv = (f'₹{r["darvas_top"]:.2f} {"🎯" if r["darvas_break"] else ""}'
                if r["darvas_top"] else "—")
        bb   = f'{r["bb_pct"]:.0f}% {"🚀" if r["bb_breakout"] else ""}'
        don  = f'₹{r["don_hi"]:.2f} {"💎" if r["don_breakout"] else ""}'
        ck   = f'{"🟢" if r["ck_bull"] else "🔴"} SL ₹{r["ck_long"]:.2f}'
        vo   = f'{"🟢" if r["vo_bull"] else "🔴"} {r["vo"]:.1f}%'
        macd_icon = "⬆️" if r["macd_cross_up"] else ("🟢" if r["macd_bull"] else "🔴")
        macd = f'{macd_icon} {r["macd_hist"]:+.2f}'
        chan  = f'{r["channel"]} ({"↗" if r["slope"]>0 else "↘"})'

        rows_html += f"""
        <tr>
          <td style="text-align:center;color:#64748b;font-size:0.78rem">{rank}</td>
          <td><strong style="color:#38bdf8">{r['sym']}</strong><br>
              <small style="color:#94a3b8">₹{r['close']:.2f}</small></td>
          <td style="text-align:center">{_sig_badge(r['signal'])}<br>
              <small style="color:#64748b;font-size:0.7rem">{r['score']}/100</small></td>
          <td style="font-size:0.76rem;color:#a3e635;line-height:1.6">{sigs}</td>
          <td style="text-align:center;font-size:0.82rem;white-space:nowrap">
            <span style="color:{_clr(r['r7'])}">{r['r7']:.1f}</span> /
            <span style="color:{_clr(r['r34'])}">{r['r34']:.1f}</span> /
            <span style="color:{_clr(r['r200'])}">{r['r200']:.1f}</span>
          </td>
          <td style="font-size:0.82rem">{macd}</td>
          <td style="font-size:0.82rem">{ck}</td>
          <td style="font-size:0.82rem">{vo}</td>
          <td style="font-size:0.82rem">{bb}</td>
          <td style="font-size:0.82rem">{don}</td>
          <td style="font-size:0.82rem">{darv}</td>
          <td style="font-size:0.82rem;color:#94a3b8">{chan}</td>
          <td>{chart_btn} {chart15_btn}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="NSE F&amp;O Multi-Indicator Breakout Scanner — RSI, MACD, Darvas, BB, Donchian, Chande Kroll, Fibonacci">
<title>F&amp;O Scanner — {gen_time}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:24px 12px}}
h1{{color:#38bdf8;font-size:1.6rem;margin-bottom:4px;text-align:center}}
.sub{{color:#64748b;text-align:center;font-size:0.85rem;margin-bottom:22px}}
.summ{{display:flex;gap:14px;flex-wrap:wrap;justify-content:center;margin-bottom:24px}}
.sc{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:14px 24px;text-align:center;min-width:130px}}
.sc .n{{font-size:2rem;font-weight:700}}
.sc .l{{font-size:0.72rem;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px}}
.nav{{display:flex;gap:10px;justify-content:center;margin-bottom:20px;flex-wrap:wrap}}
.nav a{{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:20px;padding:6px 18px;text-decoration:none;font-size:0.83rem}}
.nav a:hover{{border-color:#38bdf8;color:#38bdf8}}
.wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:0.83rem}}
th{{background:#1e293b;color:#64748b;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.8px;padding:10px 8px;text-align:left;border-bottom:1px solid #334155;position:sticky;top:0;z-index:2}}
td{{padding:9px 8px;border-bottom:1px solid #1e293b;vertical-align:middle}}
tr:hover td{{background:#1e293b55}}
.cbtn{{display:inline-block;background:#0c1a26;color:#38bdf8;border:1px solid #38bdf8;border-radius:16px;padding:3px 10px;font-size:0.7rem;text-decoration:none;margin:2px}}
.cbtn.green{{background:#052e16;color:#22c55e;border-color:#22c55e}}
.cbtn:hover{{opacity:0.8}}
footer{{text-align:center;color:#334155;font-size:0.73rem;margin-top:40px;padding-top:20px;border-top:1px solid #1e293b}}
</style>
</head>
<body>
<h1>📊 F&amp;O Multi-Indicator Scanner</h1>
<div class="sub">NSE F&amp;O Universe · RSI · MACD(34,200,9) · Chande Kroll · Darvas · BB · Donchian · Fibonacci · Generated {gen_time}</div>

<div class="summ">
  <div class="sc"><div class="n" style="color:#22c55e">{n_sb}</div><div class="l">Strong Buy</div></div>
  <div class="sc"><div class="n" style="color:#38bdf8">{n_buy}</div><div class="l">Buy</div></div>
  <div class="sc"><div class="n" style="color:#fbbf24">{n_bl}</div><div class="l">Bullish</div></div>
  <div class="sc"><div class="n" style="color:#94a3b8">{n_w}</div><div class="l">Watch</div></div>
  <div class="sc"><div class="n" style="color:#475569">{len(results)}</div><div class="l">Scanned</div></div>
</div>

<div class="nav">
  <a href="/">📈 RSI Report</a>
  <a href="/multibagger">💎 Multibagger</a>
  <a href="/ath">🏆 ATH</a>
  <a href="/rocket">🚀 Rocket</a>
  <a href="/intraday">⚡ Intraday</a>
</div>

<div class="wrap">
<table>
  <thead><tr>
    <th>#</th><th>Symbol</th><th>Signal</th><th>Active Signals</th>
    <th>RSI 7/34/200</th><th>MACD(34,200,9)</th><th>CK Stop</th>
    <th>Vol Osc</th><th>BB%</th><th>Donchian Hi</th>
    <th>Darvas Top</th><th>Channel</th><th>Charts</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
</div>

<footer>
  F&amp;O Scanner &nbsp;|&nbsp; NSE India &nbsp;|&nbsp; Data via Yahoo Finance &nbsp;|&nbsp; {gen_time}<br>
  Not investment advice. For educational &amp; research purposes only.
</footer>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# 8. MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    try:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        gen_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    except ImportError:
        gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    log.info("=" * 65)
    log.info("F&O MULTI-INDICATOR SCANNER")
    log.info("=" * 65)

    # Step 1: F&O universe
    log.info("[1/7] Loading F&O universe…")
    _load_fo_list()
    if not _FO_SET:
        log.error("No F&O symbols found. Exiting.")
        sys.exit(1)
    fo_tickers = sorted(_FO_SET)
    log.info(f"      Universe: {len(fo_tickers)} F&O stocks")

    # Step 2: Daily cache
    log.info("[2/7] Loading shared daily cache…")
    global _CACHE
    _CACHE = _load_cache_v2()
    fresh  = sum(1 for t in fo_tickers if _is_cached_fresh(t))
    cached = sum(1 for t in fo_tickers if _get_daily_df(t) is not None)
    log.info(f"      {cached} cached / {fresh} fresh / {len(fo_tickers)} total")

    # Step 3: Download stale daily data
    log.info("[3/7] Fetching stale daily data…")
    prefetch_daily(fo_tickers)

    # Step 4: 15-min cache
    log.info("[4/7] Loading 15-min cache…")
    _load_15m_cache()

    # Step 5: Parallel analysis
    log.info("[5/7] Running indicator analysis (parallel)…")
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(analyse_ticker, t): t for t in fo_tickers}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            done += 1
            res = fut.result()
            if res:
                results.append(res)
            if done % 50 == 0:
                log.info(f"      {done}/{len(fo_tickers)} analysed — {len(results)} with data")

    results.sort(key=lambda r: r["score"], reverse=True)
    log.info(f"      ✅ {len(results)} stocks analysed | Top score: {results[0]['score'] if results else 0}")

    # Step 6: Generate charts for top-N
    log.info(f"[6/7] Generating charts for top {TOP_N_CHARTS}…")
    done_c = 0
    for res in results[:TOP_N_CHARTS]:
        sym = res["sym"]
        try:
            draw_daily_chart(res, os.path.join(CHARTS_DIR, f"{sym}.png"))
        except Exception as e:
            log.debug(f"      Daily chart failed {sym}: {e}")
        try:
            df15 = get_15m_df(sym)
            if df15 is not None and len(df15) >= 20:
                draw_15m_chart(sym, df15, os.path.join(CHARTS_DIR, f"{sym}_15m.png"))
        except Exception as e:
            log.debug(f"      15m chart failed {sym}: {e}")
        done_c += 1
        if done_c % 10 == 0:
            log.info(f"      {done_c}/{min(TOP_N_CHARTS, len(results))} charts done")

    _save_15m_cache()
    log.info("      💾 15-min cache saved.")

    # Step 7: Write HTML (strip raw series first)
    log.info("[7/7] Building HTML report…")
    for res in results:
        for key in [k for k in res if k.startswith("_")]:
            del res[key]

    html = build_html(results, gen_time)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    log.info("")
    log.info("=" * 65)
    n_sb  = sum(1 for r in results if r["signal"] == "STRONG BUY")
    n_buy = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"✅ F&O SCANNER COMPLETE")
    log.info(f"   Scanned  : {len(results)} stocks")
    log.info(f"   Strong Buy: {n_sb} | Buy: {n_buy}")
    log.info(f"   Report   : {REPORT_FILE}")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
