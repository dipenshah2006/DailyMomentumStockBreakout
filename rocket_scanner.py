"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  ROCKET SCANNER v2.0 — NSE Explosive Breakout Finder                        ║
║  Targets stocks with 30-80% return potential in 5-10 trading days           ║
║                                                                              ║
║  Indicators (Daily · Weekly · Monthly):                                      ║
║   • CCI(200) & CCI(1000)  — long-base breakout detection                    ║
║   • MACD(34,200,9) & MACD(34,1000,9) — slow-cycle bullish crossovers        ║
║   • MFI(14)               — volume-weighted momentum (institutional flow)    ║
║   • Volume Suite (8 methods):                                                ║
║       vs SMA(20) ratio  · vs EMA(20) ratio  · vs SMA(50) ratio              ║
║       Z-Score (unusual) · All-Time High Vol · Dry-up→Surge (VCP)            ║
║       OBV Breakout      · Volume Trend (5D>10D>20D rising)                   ║
║   • Chande Kroll Stop     — ATR-based dynamic stop (price above = trend on) ║
║   • Donchian Channel      — 52-week & 13-week price breakouts                ║
║   • Bollinger Bands       — squeeze → expansion breakout                     ║
║   • ADX + DI              — trend strength & direction confirmation          ║
║                                                                              ║
║  Scoring: 0–30+ pts  |  ≥20 🚀 ROCKET  ≥14 ⚡ LAUNCH  ≥9 👀 WATCH          ║
╚══════════════════════════════════════════════════════════════════════════════╝

RUN:    python rocket_scanner.py
OUTPUT: rocket_scan_YYYYMMDD_HHMM.html
"""

import os, sys, json, time, logging, pickle, warnings
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

LOCAL_NSE_CSV   = "india/NSE/NSECash/EQUITY_L.csv"
LOCAL_SME_CSV   = "india/NSE/NSESME/MW-SME-05-May-2026.csv"
LOCAL_FO_CSV    = "india/NSE/nse_fo_list.csv"
DATA_PERIOD     = "max"          # need max history for CCI(1000) / MACD(34,1000,9)
MIN_BARS_D      = 60             # minimum daily bars to process a stock
BATCH_SIZE      = 20             # yfinance batch size
BATCH_PAUSE     = 1.2            # seconds between batches
CACHE_FILE      = "rocket_cache.pkl"
USE_CACHE       = True
FRESH_BARS      = 3              # crossover/breakout "fresh" if within N bars

# Scoring thresholds
SCORE_ROCKET    = 20
SCORE_LAUNCH    = 14
SCORE_WATCH     = 9

# Indicator parameters
CCI_S, CCI_L    = 200, 1000     # short & long CCI periods
MACD_F          = 34            # MACD fast (common to all custom MACDs)
MACD_S1, MACD_S2 = 200, 1000   # MACD slow periods
MACD_SIG        = 9             # MACD signal
MFI_P           = 14
ATR_P           = 14
ADX_P           = 14
BB_P, BB_STD    = 20, 2.0
DONCH_LONG      = 252           # ~1 year for 52W high
DONCH_MED       = 65            # ~13 weeks
CKS_ATR_P       = 10            # Chande Kroll Stop ATR period
CKS_FACTOR      = 1.5           # ATR multiplier
CKS_Q           = 9             # CKS highest period


# ═══════════════════════════════════════════════════════════════════════════════
#  F&O LIST LOADER
# ═══════════════════════════════════════════════════════════════════════════════

import csv as _csv, io as _io, requests as _req

_FO_SET: set[str] = set()

def _load_fo_list() -> None:
    """Load NSE F&O eligible symbols into _FO_SET. Tries local CSV → NSE archives → NSE API."""
    global _FO_SET
    import os

    # ── 1. Local CSV ──────────────────────────────────────────────────────────
    if os.path.exists(LOCAL_FO_CSV):
        try:
            with open(LOCAL_FO_CSV, encoding="utf-8", errors="replace") as f:
                raw = f.read().lstrip("\ufeff")
            syms = set()
            for row in _csv.DictReader(_io.StringIO(raw)):
                clean = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
                s = clean.get("SYMBOL", "")
                if s:
                    syms.add(s.upper())
            _FO_SET = syms
            log.info(f"  ✅ F&O list loaded: {len(_FO_SET)} symbols ← '{LOCAL_FO_CSV}'")
            return
        except Exception as e:
            log.warning(f"  [!] Error reading F&O CSV: {e}")

    # ── 2. NSE Archives (works from GitHub Actions) ───────────────────────────
    _IDX = {"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50","SENSEX","BANKEX"}
    try:
        import warnings; warnings.filterwarnings("ignore")
        import urllib3; urllib3.disable_warnings()
        s = _req.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
        r = s.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", timeout=20, verify=False)
        r.raise_for_status()
        rows_raw, syms = [], set()
        for row in _csv.DictReader(_io.StringIO(r.text)):
            clean = {k.strip(): (v.strip() if v else "") for k, v in row.items()}
            sym = clean.get("SYMBOL", "").upper()
            if sym and sym not in _IDX:
                syms.add(sym)
                rows_raw.append({"SYMBOL": sym, "UNDERLYING": clean.get("UNDERLYING", "")})
        if not syms:
            raise ValueError("Empty fo_mktlots response")
        _FO_SET = syms
        log.info(f"  ✅ F&O list fetched: {len(_FO_SET)} symbols from NSE archives")
        try:
            os.makedirs(os.path.dirname(LOCAL_FO_CSV), exist_ok=True)
            import pandas as _pd
            _pd.DataFrame(rows_raw).drop_duplicates("SYMBOL").to_csv(LOCAL_FO_CSV, index=False)
        except Exception:
            pass
        return
    except Exception as e:
        log.warning(f"  [!] NSE archives failed ({e}) — trying NSE API...")

    # ── 3. NSE API fallback ───────────────────────────────────────────────────
    try:
        s = _req.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
        s.get("https://www.nseindia.com/", timeout=10)
        r = s.get("https://www.nseindia.com/api/foSecList", timeout=15)
        r.raise_for_status()
        data = r.json()
        rows = data.get("data", data) if isinstance(data, dict) else data
        _FO_SET = {str(row.get("SYMBOL","")).strip().upper() for row in rows if row.get("SYMBOL")} - {""}
        log.info(f"  ✅ F&O list fetched: {len(_FO_SET)} symbols from NSE API")
    except Exception as e:
        log.warning(f"  [!] F&O list unavailable ({e}) — F&O tags will be hidden")
        _FO_SET = set()


# ═══════════════════════════════════════════════════════════════════════════════
#  STOCK UNIVERSE LOADER
# ═══════════════════════════════════════════════════════════════════════════════

def load_universe() -> list[dict]:
    stocks = []

    # NSE EQ
    if os.path.exists(LOCAL_NSE_CSV):
        try:
            df = pd.read_csv(LOCAL_NSE_CSV)
            sym_col  = next((c for c in df.columns if "SYMBOL" in c.upper()), None)
            name_col = next((c for c in df.columns if "NAME" in c.upper() or "COMPANY" in c.upper()), None)
            ser_col  = next((c for c in df.columns if "SERIES" in c.upper()), None)
            if sym_col:
                for _, row in df.iterrows():
                    if ser_col and str(row.get(ser_col, "")).strip() not in ("EQ", ""):
                        continue
                    sym = str(row[sym_col]).strip()
                    nm  = str(row[name_col]).strip() if name_col else sym
                    if sym:
                        stocks.append({"ticker": sym, "company": nm, "sme": False})
            log.info(f"NSE EQ: {len(stocks)} stocks from CSV")
        except Exception as e:
            log.warning(f"Could not load NSE CSV: {e}")

    # NSE SME
    sme_count = 0
    if os.path.exists(LOCAL_SME_CSV):
        try:
            df = pd.read_csv(LOCAL_SME_CSV, skiprows=3)
            sym_col = next((c for c in df.columns if "SYMBOL" in c.upper()), None)
            if sym_col:
                existing = {s["ticker"] for s in stocks}
                for _, row in df.iterrows():
                    sym = str(row[sym_col]).strip()
                    if sym and sym not in existing:
                        stocks.append({"ticker": sym, "company": sym, "sme": True})
                        sme_count += 1
            log.info(f"NSE SME: {sme_count} stocks added")
        except Exception as e:
            log.warning(f"Could not load SME CSV: {e}")

    if not stocks:
        # fallback: Nifty50
        fallback = [
            "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR",
            "ITC","SBIN","BAJFINANCE","BHARTIARTL","KOTAKBANK","LT",
        ]
        stocks = [{"ticker": t, "company": t, "sme": False} for t in fallback]
        log.warning("Using fallback Nifty50 ticker list")

    return stocks


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA DOWNLOAD (with cache)
# ═══════════════════════════════════════════════════════════════════════════════

def load_cache() -> dict:
    if USE_CACHE and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(cache, f)
    except Exception as e:
        log.warning(f"Cache save failed: {e}")


def download_batch(tickers: list[str]) -> dict[str, pd.DataFrame]:
    suffixed = [f"{t}.NS" for t in tickers]
    result = {}
    try:
        raw = yf.download(
            suffixed, period=DATA_PERIOD, interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True
        )
        for t, ts in zip(tickers, suffixed):
            try:
                if len(tickers) == 1:
                    df = raw.copy()
                else:
                    df = raw[ts].copy() if ts in raw.columns.get_level_values(0) else pd.DataFrame()
                df.dropna(subset=["Close"], inplace=True)
                if len(df) >= MIN_BARS_D:
                    result[t] = df
            except Exception:
                pass
    except Exception as e:
        log.debug(f"Batch download error: {e}")
    return result


def fetch_all_data(universe: list[dict]) -> dict[str, pd.DataFrame]:
    cache = load_cache()
    today = datetime.now().strftime("%Y%m%d")
    tickers = [s["ticker"] for s in universe]

    # Only re-download if stale
    stale = [t for t in tickers if t not in cache or cache[t].get("date") != today]
    log.info(f"Downloading {len(stale)} stocks (cache has {len(tickers)-len(stale)} fresh)")

    batches = [stale[i:i+BATCH_SIZE] for i in range(0, len(stale), BATCH_SIZE)]
    for i, batch in enumerate(batches):
        data = download_batch(batch)
        for t, df in data.items():
            cache[t] = {"date": today, "df": df}
        log.info(f"  Batch {i+1}/{len(batches)} — {len(data)}/{len(batch)} downloaded")
        if i < len(batches) - 1:
            time.sleep(BATCH_PAUSE)

    save_cache(cache)
    return {t: cache[t]["df"] for t in tickers if t in cache and "df" in cache[t]}


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATOR LIBRARY
# ═══════════════════════════════════════════════════════════════════════════════

def cci(high, low, close, period: int) -> pd.Series:
    tp = (high + low + close) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


def custom_macd(close, fast: int, slow: int, signal: int = 9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_f - ema_s
    sig_line  = macd_line.ewm(span=signal, adjust=False).mean()
    hist      = macd_line - sig_line
    return macd_line, sig_line, hist


def mfi(high, low, close, volume, period: int = 14) -> pd.Series:
    tp   = (high + low + close) / 3
    rmf  = tp * volume
    pos  = rmf.where(tp > tp.shift(1), 0.0)
    neg  = rmf.where(tp < tp.shift(1), 0.0)
    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum()
    mfr  = pos_sum / neg_sum.replace(0, np.nan)
    return 100 - (100 / (1 + mfr))


def atr(high, low, close, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def chande_kroll_stop(high, low, close, atr_p=10, factor=1.5, q=9):
    """Returns (stop_long, stop_short) — price > stop_long = BUY signal."""
    a = atr(high, low, close, atr_p)
    first_high_stop = high.rolling(atr_p).max() - factor * a
    first_low_stop  = low.rolling(atr_p).min()  + factor * a
    stop_short = first_high_stop.rolling(q).max()   # long entry when close > stop_short
    stop_long  = first_low_stop.rolling(q).min()    # short entry when close < stop_long
    return stop_short, stop_long


def bollinger(close, period=20, std=2.0):
    sma    = close.rolling(period).mean()
    sigma  = close.rolling(period).std()
    upper  = sma + std * sigma
    lower  = sma - std * sigma
    width  = (upper - lower) / sma.replace(0, np.nan)  # normalised band width
    pct_b  = (close - lower) / (upper - lower).replace(0, np.nan)
    return upper, lower, sma, width, pct_b


def adx_di(high, low, close, period=14):
    up   = high.diff()
    down = -low.diff()
    pos_dm = up.where((up > down) & (up > 0), 0.0)
    neg_dm = down.where((down > up) & (down > 0), 0.0)
    atr14  = atr(high, low, close, period)
    pos_di = 100 * pos_dm.ewm(alpha=1/period, adjust=False).mean() / atr14
    neg_di = 100 * neg_dm.ewm(alpha=1/period, adjust=False).mean() / atr14
    dx     = 100 * (pos_di - neg_di).abs() / (pos_di + neg_di).replace(0, np.nan)
    adx_v  = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx_v, pos_di, neg_di


def target_analysis(close: pd.Series, high: pd.Series, low: pd.Series,
                    volume: pd.Series, cks_stop: float | None = None) -> dict:
    """
    ┌──────────────────────────────────────────────────────────────────────────┐
    │  TARGET PRICE & STOP LOSS — 5 methods for daily momentum + volume plays  │
    │  Goal: ride the daily trend with big price moves, stay with momentum     │
    └──────────────────────────────────────────────────────────────────────────┘

    METHOD COMPARISON (use the one that fits your setup)
    ─────────────────────────────────────────────────────────────────────────
    M1  ATR Multiples        ⭐ BEST for daily momentum (default)
        Targets = cur + N × ATR(14). Adapts to current stock volatility.
        Bigger ATR = stock moving more = bigger targets are realistic.
        T1=1.5×, T2=2.5×, T3=4.0×, T4=6.0× | SL = 1.0×ATR below entry.
        Why best: ATR embeds both price range AND recent momentum energy.
        Use always; override only when vol or Z-score signals are extreme.

    M2  Z-Score Move         ⭐ BEST when today's return is already large
        Uses 60-bar distribution of daily returns to calibrate targets.
        ret_std = rolling std of daily pct_change(1) over 60 bars.
        T = cur × (1 + N × ret_std). N = 1.5, 2.5, 4.0 standard deviations.
        today_z = (today_return − mean_ret60) / ret_std.
        Best when: today_z ≥ 1.5 — stock already in an unusual up-move.
        Advantage: statistically calibrated for THIS stock's typical move size.

    M3  Avg Daily Range      Good for quick daily swing estimates
        ADR(20) = mean of (high − low) over 20 daily bars.
        T1=1×ADR, T2=2×ADR, T3=3×ADR above current price.
        Best when: comparing target vs typical single-day range.
        Limitation: range-based, not direction-aware — use as sanity check.

    M4  Fibonacci Extension  Good for chart-based base breakouts
        20-bar base: base_low → base_high. Projects 127.2%, 161.8%, 200%, 261.8%.
        Strong horizontal levels used by many traders → self-fulfilling.
        Best when: stock has a clean consolidation box before breakout.
        Limitation: base_high must be below current price to project forward.

    M5  Volume-Surge Proj    ⭐ BEST when volume is extreme (Z ≥ 3)
        vol_z = (today_vol − mean_vol50) / std_vol50.
        surge_mult = clamp(vol_z, 1.0, 5.0).
        T = cur + N × ADR × surge_mult. N = 1.0, 2.0, 3.5.
        Best when: ATH volume / 5× surge — institutional conviction move.
        Logic: higher volume = higher conviction = can sustain larger move.

    STOP LOSS METHODS
    ─────────────────────────────────────────────────────────────────────────
    SL-ATR1  ⭐ BEST for momentum — cur − 1.0×ATR. Tight. Broken = failed.
    SL-ATR2  Standard vol stop — cur − 2.0×ATR. More room; good for swing.
    SL-CKS   Chande Kroll Stop line — structural trend inception level.
    SL-Base  20-bar low × 0.98 — structural base; wider safety stop.

    RECOMMENDATION LOGIC (auto-selected)
    ─────────────────────────────────────────────────────────────────────────
    vol_z ≥ 3  →  M5 Vol-Surge  + SL-ATR1   (explosive conviction breakout)
    today_z ≥ 2 → M2 Z-Score   + SL-ATR1   (statistically rare move today)
    default     → M1 ATR Multi + SL-ATR1   (universal momentum play)
    """
    n = len(close)
    if n < 21:
        return {}

    cur = float(close.iloc[-1])
    if cur <= 0:
        return {}

    def pct(price: float) -> float:
        return round((price / cur - 1) * 100, 1)

    def rr_ratio(target: float, sl: float) -> float | None:
        risk = cur - sl
        if risk <= 0 or target <= cur:
            return None
        return round((target - cur) / risk, 1)

    # ── ATR(14) & ADR(20) ─────────────────────────────────────────────────
    atr14_s  = atr(high, low, close, 14).iloc[-1]
    atr14    = float(atr14_s) if not np.isnan(atr14_s) else cur * 0.02
    atr_pct  = round(atr14 / cur * 100, 2)
    dr       = (high - low)
    adr20_s  = dr.rolling(20).mean().iloc[-1]
    adr20    = float(adr20_s) if not np.isnan(adr20_s) else atr14
    adr_pct  = round(adr20 / cur * 100, 2)

    # ── M1: ATR Multiples ─────────────────────────────────────────────────
    sl_m1  = round(cur - 1.0 * atr14, 2)
    m1_t   = [round(cur + m * atr14, 2) for m in (1.5, 2.5, 4.0, 6.0)]
    m1 = {
        "tag": "M1", "label": "ATR Multiples",
        "t1": m1_t[0], "t1_mult": "1.5×", "t1_pct": pct(m1_t[0]), "t1_rr": rr_ratio(m1_t[0], sl_m1),
        "t2": m1_t[1], "t2_mult": "2.5×", "t2_pct": pct(m1_t[1]), "t2_rr": rr_ratio(m1_t[1], sl_m1),
        "t3": m1_t[2], "t3_mult": "4.0×", "t3_pct": pct(m1_t[2]), "t3_rr": rr_ratio(m1_t[2], sl_m1),
        "t4": m1_t[3], "t4_mult": "6.0×", "t4_pct": pct(m1_t[3]),
        "sl": sl_m1, "sl_pct": pct(sl_m1),
        "notes": f"ATR(14)=₹{atr14:.2f} ({atr_pct}% of price)",
    }

    # ── M2: Z-Score Expected Move ─────────────────────────────────────────
    m2: dict = {}
    rets = close.pct_change(1).dropna()
    if len(rets) >= 21:
        window  = rets.iloc[-60:] if len(rets) >= 60 else rets
        r_mean  = float(window.mean())
        r_std   = float(window.std())
        today_r = float(rets.iloc[-1])
        today_z = round((today_r - r_mean) / r_std, 2) if r_std > 0 else 0.0
        sl_m2   = round(cur * (1 - 1.0 * r_std), 2)
        m2_t    = [round(cur * (1 + m * r_std), 2) for m in (1.5, 2.5, 4.0)]
        m2 = {
            "tag": "M2", "label": "Z-Score Move",
            "ret_std_pct": round(r_std * 100, 2), "today_z": today_z,
            "daily_1s": round(cur * r_std, 2),
            "t1": m2_t[0], "t1_mult": "1.5σ", "t1_pct": pct(m2_t[0]), "t1_rr": rr_ratio(m2_t[0], sl_m2),
            "t2": m2_t[1], "t2_mult": "2.5σ", "t2_pct": pct(m2_t[1]), "t2_rr": rr_ratio(m2_t[1], sl_m2),
            "t3": m2_t[2], "t3_mult": "4.0σ", "t3_pct": pct(m2_t[2]),
            "sl": sl_m2, "sl_pct": pct(sl_m2),
            "notes": f"1σ=₹{round(cur*r_std,2)} ({round(r_std*100,2)}%) · today={today_z}σ",
        }

    # ── M3: Avg Daily Range ───────────────────────────────────────────────
    sl_m3 = round(cur - 0.75 * adr20, 2)
    m3_t  = [round(cur + m * adr20, 2) for m in (1.0, 2.0, 3.0)]
    m3 = {
        "tag": "M3", "label": "Avg Daily Range",
        "adr": round(adr20, 2), "adr_pct": adr_pct,
        "t1": m3_t[0], "t1_mult": "1×ADR", "t1_pct": pct(m3_t[0]), "t1_rr": rr_ratio(m3_t[0], sl_m3),
        "t2": m3_t[1], "t2_mult": "2×ADR", "t2_pct": pct(m3_t[1]), "t2_rr": rr_ratio(m3_t[1], sl_m3),
        "t3": m3_t[2], "t3_mult": "3×ADR", "t3_pct": pct(m3_t[2]),
        "sl": sl_m3, "sl_pct": pct(sl_m3),
        "notes": f"ADR(20)=₹{adr20:.2f} ({adr_pct}% of price)",
    }

    # ── M4: Fibonacci Extension (20-bar base) ─────────────────────────────
    m4: dict = {}
    lb4 = min(20, n - 1)
    b_lo = float(low.iloc[-lb4:].min())
    b_hi = float(high.iloc[-lb4:].max())
    rng4 = b_hi - b_lo
    if rng4 > 0:
        sl_m4 = round(b_lo * 0.98, 2)
        m4_t  = [round(b_hi + m * rng4, 2) for m in (0.272, 0.618, 1.000, 1.618)]
        m4 = {
            "tag": "M4", "label": "Fibonacci Ext",
            "base_low": round(b_lo, 2), "base_high": round(b_hi, 2),
            "base_rng_pct": round(rng4 / b_lo * 100, 1),
            "t1": m4_t[0], "t1_mult": "127.2%", "t1_pct": pct(m4_t[0]), "t1_rr": rr_ratio(m4_t[0], sl_m4),
            "t2": m4_t[1], "t2_mult": "161.8%", "t2_pct": pct(m4_t[1]), "t2_rr": rr_ratio(m4_t[1], sl_m4),
            "t3": m4_t[2], "t3_mult": "200.0%", "t3_pct": pct(m4_t[2]),
            "t4": m4_t[3], "t4_mult": "261.8%", "t4_pct": pct(m4_t[3]),
            "sl": sl_m4, "sl_pct": pct(sl_m4),
            "notes": f"20-bar ₹{b_lo:.0f}–₹{b_hi:.0f} ({round(rng4/b_lo*100,1)}% wide)",
        }

    # ── M5: Volume-Surge Projection ───────────────────────────────────────
    m5: dict = {}
    if len(volume) >= 21:
        v50m = volume.rolling(50).mean().iloc[-1]
        v50s = volume.rolling(50).std().iloc[-1]
        vz   = float((volume.iloc[-1] - v50m) / v50s
                     if not np.isnan(v50s) and float(v50s) > 0 else 0)
        surge = float(np.clip(vz, 1.0, 5.0))
        sl_m5 = round(cur - 1.0 * atr14, 2)
        m5_t  = [round(cur + m * adr20 * surge, 2) for m in (1.0, 2.0, 3.5)]
        m5 = {
            "tag": "M5", "label": "Vol-Surge Proj",
            "vol_z": round(vz, 2), "surge_mult": round(surge, 2),
            "t1": m5_t[0], "t1_mult": f"1×ADR×{surge:.1f}", "t1_pct": pct(m5_t[0]), "t1_rr": rr_ratio(m5_t[0], sl_m5),
            "t2": m5_t[1], "t2_mult": f"2×ADR×{surge:.1f}", "t2_pct": pct(m5_t[1]), "t2_rr": rr_ratio(m5_t[1], sl_m5),
            "t3": m5_t[2], "t3_mult": f"3.5×ADR×{surge:.1f}", "t3_pct": pct(m5_t[2]),
            "sl": sl_m5, "sl_pct": pct(sl_m5),
            "notes": f"vol Z={round(vz,2)}σ · surge={surge:.1f}× · ADR=₹{adr20:.2f}",
        }

    # ── Stop Loss Summary ─────────────────────────────────────────────────
    sl_atr1  = round(cur - 1.0 * atr14, 2)
    sl_atr2  = round(cur - 2.0 * atr14, 2)
    cks_sl   = round(float(cks_stop), 2) if cks_stop is not None else None
    base_sl  = round(float(low.iloc[-20:].min()) * 0.98, 2) if n >= 20 else None
    stops = {
        "sl_atr1":    sl_atr1,  "sl_atr1_pct":  pct(sl_atr1),
        "sl_atr2":    sl_atr2,  "sl_atr2_pct":  pct(sl_atr2),
        "sl_cks":     cks_sl,   "sl_cks_pct":   pct(cks_sl)  if cks_sl  else None,
        "sl_base":    base_sl,  "sl_base_pct":  pct(base_sl) if base_sl else None,
        "primary":    sl_atr1,  "primary_pct":  pct(sl_atr1),
    }

    # ── Auto-recommendation ───────────────────────────────────────────────
    vz_rec = m5.get("vol_z", 0) if m5 else 0
    tz_rec = m2.get("today_z", 0) if m2 else 0
    if vz_rec >= 3 and m5:
        rec_tag, rec_label = "M5", "🔥 Vol-Surge"
        rec_why = f"Extreme volume (Z={vz_rec}σ) — use surge-scaled targets"
        rec = m5
    elif abs(float(tz_rec)) >= 2 and m2:
        rec_tag, rec_label = "M2", "⚡ Z-Score"
        rec_why = f"Today's move is already {tz_rec}σ — statistically significant"
        rec = m2
    else:
        rec_tag, rec_label = "M1", "✅ ATR Multi"
        rec_why = "Standard momentum: ATR adapts to this stock's volatility"
        rec = m1

    return {
        "m1": m1, "m2": m2, "m3": m3, "m4": m4, "m5": m5,
        "stops":      stops,
        "rec_tag":    rec_tag,
        "rec_label":  rec_label,
        "rec_why":    rec_why,
        "rec":        rec,
        "atr14":      round(atr14, 2),
        "atr_pct":    atr_pct,
        "adr20":      round(adr20, 2),
        "adr_pct":    adr_pct,
    }


def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("W-FRI").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).dropna(subset=["Close"])


def resample_to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("ME").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).dropna(subset=["Close"])


def volume_analysis(v: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series) -> dict:
    """
    8-method volume breakout suite. Returns dict with individual signals,
    a composite vol_pts (capped at 10), and a formatted vol_signal string.

    Methods:
      1. vs SMA(20)          — standard volume/SMA20 ratio
      2. vs EMA(20)          — EMA-smoothed baseline (faster reaction)
      3. vs SMA(50)          — longer baseline, separates daily noise
      4. Z-Score             — (vol − μ50) / σ50, flags statistically unusual volume
      5. All-Time High Vol   — is today's volume the largest ever / near ATH?
      6. Dry-up → Surge      — VCP-style: quiet accumulation then explosion
      7. OBV Breakout        — On-Balance Volume making new 20-bar high
      8. Volume Trend        — 5D avg > 10D avg > 20D avg (rising accumulation)
    """
    n = len(v)
    if n < 22:
        return {"vol_pts": 0, "vol_signal": "—", "vol_detail": "—",
                "vol_sma20": None, "vol_ema20": None, "vol_sma50": None,
                "vol_zscore": None, "vol_ath_pct": None, "vol_dry_surge": None,
                "vol_obv_break": False, "vol_trend_up": 0}

    cur = float(v.iloc[-1]) if not np.isnan(v.iloc[-1]) else 0.0
    pts = 0
    details = []

    # ── 1. vs SMA(20) ─────────────────────────────────────────────────────
    sma20 = float(v.rolling(20).mean().iloc[-1])
    r_sma20 = cur / sma20 if sma20 > 0 else 0.0
    if r_sma20 >= 5:   pts += 3; details.append(f"🔥SMA20:{r_sma20:.1f}x")
    elif r_sma20 >= 3: pts += 2; details.append(f"⚡SMA20:{r_sma20:.1f}x")
    elif r_sma20 >= 2: pts += 1; details.append(f"✅SMA20:{r_sma20:.1f}x")
    else:              details.append(f"SMA20:{r_sma20:.1f}x")

    # ── 2. vs EMA(20) ─────────────────────────────────────────────────────
    ema20 = float(v.ewm(span=20, adjust=False).mean().iloc[-1])
    r_ema20 = cur / ema20 if ema20 > 0 else 0.0
    if r_ema20 >= 4:     pts += 2; details.append(f"🔥EMA20:{r_ema20:.1f}x")
    elif r_ema20 >= 2.5: pts += 1; details.append(f"✅EMA20:{r_ema20:.1f}x")
    else:                details.append(f"EMA20:{r_ema20:.1f}x")

    # ── 3. vs SMA(50) ─────────────────────────────────────────────────────
    r_sma50 = None
    if n >= 52:
        sma50 = float(v.rolling(50).mean().iloc[-1])
        r_sma50 = cur / sma50 if sma50 > 0 else 0.0
        if r_sma50 >= 6:   pts += 2; details.append(f"🔥SMA50:{r_sma50:.1f}x")
        elif r_sma50 >= 3: pts += 1; details.append(f"✅SMA50:{r_sma50:.1f}x")
        else:              details.append(f"SMA50:{r_sma50:.1f}x")

    # ── 4. Z-Score (Unusual Volume) ────────────────────────────────────────
    zscore = None
    if n >= 52:
        mean50 = float(v.rolling(50).mean().iloc[-1])
        std50  = float(v.rolling(50).std().iloc[-1])
        zscore = (cur - mean50) / std50 if std50 > 0 else 0.0
        if zscore >= 4:   pts += 3; details.append(f"🔥Z:{zscore:.1f}σ")
        elif zscore >= 3: pts += 2; details.append(f"⚡Z:{zscore:.1f}σ")
        elif zscore >= 2: pts += 1; details.append(f"✅Z:{zscore:.1f}σ")
        else:             details.append(f"Z:{zscore:.1f}σ")

    # ── 5. All-Time High Volume ────────────────────────────────────────────
    ath_vol_pct = None
    if n >= 30:
        ath_vol = float(v.max())
        ath_vol_pct = (cur / ath_vol - 1) * 100 if ath_vol > 0 else -100.0
        if ath_vol_pct >= 0:      pts += 3; details.append("🔥ATH VOL")
        elif ath_vol_pct >= -10:  pts += 2; details.append(f"⚡ATH-{abs(ath_vol_pct):.0f}%")
        elif ath_vol_pct >= -25:  pts += 1; details.append(f"✅ATH-{abs(ath_vol_pct):.0f}%")
        else:                     details.append(f"ATH-{abs(ath_vol_pct):.0f}%")

    # ── 6. Volume Dry-up → Surge (VCP) ────────────────────────────────────
    dry_surge = None
    if n >= 25:
        avg5_prev  = float(v.iloc[-6:-1].mean())       # 5-bar avg BEFORE today
        sma20_ago  = float(v.rolling(20).mean().iloc[-6])  # 20D avg 5 bars ago
        dryup_ratio = avg5_prev / sma20_ago if sma20_ago > 0 else 1.0
        surge_ratio = cur / avg5_prev if avg5_prev > 0 else 0.0
        dry_surge   = round(surge_ratio, 1)
        if dryup_ratio < 0.7 and surge_ratio >= 4:
            pts += 3; details.append(f"🔥DRY→{surge_ratio:.0f}x")
        elif dryup_ratio < 0.85 and surge_ratio >= 2.5:
            pts += 2; details.append(f"⚡DRY→{surge_ratio:.0f}x")
        elif surge_ratio >= 2.0:
            pts += 1; details.append(f"✅SURGE:{surge_ratio:.0f}x")
        else:
            details.append(f"DRY:{dryup_ratio:.1f}")

    # ── 7. OBV Breakout / Trend ────────────────────────────────────────────
    obv_break = False
    if n >= 25:
        dir_sign = np.sign(c.diff().fillna(0))
        obv = (v * dir_sign).cumsum()
        obv_max20_prev = float(obv.rolling(20).max().iloc[-2])
        obv_cur        = float(obv.iloc[-1])
        obv_break = obv_cur > obv_max20_prev
        if obv_break:
            pts += 2; details.append("🔥OBV HIGH")
        else:
            # OBV rising trend (5+ of last 7 bars up)
            obv_rising = int((obv.diff().iloc[-7:] > 0).sum())
            if obv_rising >= 6:
                pts += 1; details.append(f"✅OBV↑({obv_rising}/7)")
            elif obv_rising >= 5:
                details.append(f"OBV↑({obv_rising}/7)")

    # ── 8. Volume Trend (5D > 10D > 20D) ──────────────────────────────────
    trend_up = 0
    if n >= 22:
        avg5d  = float(v.iloc[-5:].mean())
        avg10d = float(v.iloc[-10:].mean())
        avg20d = float(v.rolling(20).mean().iloc[-1])
        if avg5d > avg10d and avg10d > avg20d:
            pts += 2; trend_up = 3; details.append("🔥VOL↑↑↑")
        elif avg5d > avg10d:
            pts += 1; trend_up = 2; details.append("✅VOL↑↑")
        elif avg5d > avg20d:
            trend_up = 1; details.append("VOL↑")
        else:
            details.append("VOL→")

    # Cap at 10 to keep scoring balanced
    pts = min(pts, 10)

    if pts >= 7:   vol_sig = f"🔥 {pts}pts"
    elif pts >= 5: vol_sig = f"⚡ {pts}pts"
    elif pts >= 3: vol_sig = f"✅ {pts}pts"
    elif pts >= 1: vol_sig = f"→ {pts}pts"
    else:          vol_sig = "— 0pts"

    return {
        "vol_pts":       pts,
        "vol_signal":    vol_sig,
        "vol_detail":    " | ".join(details[:6]),   # up to 6 detail tokens
        "vol_sma20":     round(r_sma20, 1),
        "vol_ema20":     round(r_ema20, 1),
        "vol_sma50":     round(r_sma50, 1) if r_sma50 is not None else None,
        "vol_zscore":    round(zscore, 1) if zscore is not None else None,
        "vol_ath_pct":   round(ath_vol_pct, 1) if ath_vol_pct is not None else None,
        "vol_dry_surge": dry_surge,
        "vol_obv_break": obv_break,
        "vol_trend_up":  trend_up,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def score_frame(df: pd.DataFrame, label: str) -> dict:
    """
    Compute all indicators and score a single timeframe dataframe.

    Periods auto-scale to the timeframe so every indicator fires
    meaningfully on Daily, Weekly, AND Monthly data:

      Timeframe │ CCI-S │ CCI-L  │ MACD slow1 │ MACD slow2 │ Donchian long/med
      ──────────┼───────┼────────┼────────────┼────────────┼──────────────────
      Daily     │  200  │  1000  │    200     │    1000    │  252 / 65
      Weekly    │   50  │   200  │     50     │     200    │   52 / 26
      Monthly   │   20  │    50  │     26     │      50    │   12 /  6

    Equivalent time spans are roughly equal across all timeframes.
    """
    sig = {"tf": label}
    if len(df) < 15:
        return sig

    h, l, c, v = df["High"], df["Low"], df["Close"], df["Volume"]

    # ── Timeframe-adaptive periods ─────────────────────────────────────────
    if label == "W":
        tf_cci_s,  tf_cci_l  = 50,  200
        tf_macd_s1, tf_macd_s2 = 50, 200
        tf_macd_f  = MACD_F          # fast stays 34
        tf_dc_long, tf_dc_med = 52, 26
        lbl_cci_s,  lbl_cci_l  = "CCI50",  "CCI200"
        lbl_mS,     lbl_mL     = "34,50",  "34,200"
        lbl_dc_long = "52W"; lbl_dc_med = "6M"
    elif label == "M":
        tf_cci_s,  tf_cci_l  = 20,  50
        tf_macd_s1, tf_macd_s2 = 26, 50
        tf_macd_f  = 12              # classic fast for monthly
        tf_dc_long, tf_dc_med = 12, 6
        lbl_cci_s,  lbl_cci_l  = "CCI20",  "CCI50"
        lbl_mS,     lbl_mL     = "12,26",  "34,50"
        lbl_dc_long = "12M"; lbl_dc_med = "6M"
    else:  # Daily
        tf_cci_s,  tf_cci_l  = CCI_S,  CCI_L
        tf_macd_s1, tf_macd_s2 = MACD_S1, MACD_S2
        tf_macd_f  = MACD_F
        tf_dc_long, tf_dc_med = DONCH_LONG, DONCH_MED
        lbl_cci_s,  lbl_cci_l  = "CCI200", "CCI1000"
        lbl_mS,     lbl_mL     = "34,200", "34,1000"
        lbl_dc_long = "52W"; lbl_dc_med = "13W"

    # ── CCI (both short & long) ────────────────────────────────────────────
    for period, key, plbl in [
        (tf_cci_s, "cci200",  lbl_cci_s),
        (tf_cci_l, "cci1000", lbl_cci_l),
    ]:
        if len(df) < period + 5:
            sig[f"{key}_val"] = None
            sig[f"{key}_pts"] = 0
            sig[f"{key}_signal"] = "—"
            continue
        cc  = cci(h, l, c, period)
        val = cc.iloc[-1]
        prev_vals   = cc.iloc[-FRESH_BARS-1:-1]
        fresh_cross = (any(prev_vals.dropna() < 100)
                       and val is not None and not np.isnan(val) and val > 100)
        if fresh_cross:
            sig[f"{key}_pts"] = 3
            sig[f"{key}_signal"] = f"🔥 {plbl} CROSS"
        elif val is not None and not np.isnan(val) and val > 100:
            sig[f"{key}_pts"] = 1
            sig[f"{key}_signal"] = f"✅ {plbl} >100"
        elif val is not None and not np.isnan(val) and val > 0:
            sig[f"{key}_pts"] = 0
            sig[f"{key}_signal"] = f"→ {plbl} {val:.0f}"
        else:
            sig[f"{key}_pts"] = 0
            sig[f"{key}_signal"] = (f"↓ {plbl} {val:.0f}"
                                    if val is not None and not np.isnan(val) else "—")
        sig[f"{key}_val"] = (round(float(val), 1)
                             if val is not None and not np.isnan(val) else None)

    # ── Custom MACD (slow & ultra-slow) ───────────────────────────────────
    for slow, key, plbl in [
        (tf_macd_s1, "macd200",  lbl_mS),
        (tf_macd_s2, "macd1000", lbl_mL),
    ]:
        if len(df) < slow + MACD_SIG + 5:
            sig[f"{key}_pts"] = 0
            sig[f"{key}_signal"] = "—"
            sig[f"{key}_val"] = None
            continue
        _, _, hist = custom_macd(c, tf_macd_f, slow, MACD_SIG)
        cur       = hist.iloc[-1]
        prev_hist = hist.iloc[-FRESH_BARS-1:-1]
        fresh_cross = (any(prev_hist.dropna() <= 0)
                       and cur is not None and not np.isnan(cur) and cur > 0)
        if fresh_cross:
            sig[f"{key}_pts"] = 3
            sig[f"{key}_signal"] = f"🔥 {plbl} CROSS"
        elif cur is not None and not np.isnan(cur) and cur > 0:
            sig[f"{key}_pts"] = 1
            sig[f"{key}_signal"] = f"✅ {plbl} BULL"
        else:
            sig[f"{key}_pts"] = 0
            sig[f"{key}_signal"] = (f"↓ {plbl} BEAR"
                                    if cur is not None and not np.isnan(cur) else "—")
        sig[f"{key}_val"] = (round(float(cur), 4)
                             if cur is not None and not np.isnan(cur) else None)

    # ── MFI(14) ───────────────────────────────────────────────────────────
    # 14 bars = 14D / 14W(~3M) / 14M(~1yr) — all meaningful
    if len(df) >= MFI_P + 2:
        mfi_v = mfi(h, l, c, v, MFI_P).iloc[-1]
        sig["mfi_val"] = (round(float(mfi_v), 1)
                          if mfi_v is not None and not np.isnan(mfi_v) else None)
        if mfi_v is not None and not np.isnan(mfi_v):
            if mfi_v > 80:
                sig["mfi_pts"] = 2; sig["mfi_signal"] = f"🔥 MFI={mfi_v:.0f}"
            elif mfi_v > 70:
                sig["mfi_pts"] = 1; sig["mfi_signal"] = f"✅ MFI={mfi_v:.0f}"
            elif mfi_v > 60:
                sig["mfi_pts"] = 0; sig["mfi_signal"] = f"→ MFI={mfi_v:.0f}"
            else:
                sig["mfi_pts"] = 0; sig["mfi_signal"] = f"↓ MFI={mfi_v:.0f}"
        else:
            sig["mfi_pts"] = 0; sig["mfi_signal"] = "—"
    else:
        sig["mfi_pts"] = 0; sig["mfi_signal"] = "—"; sig["mfi_val"] = None

    # ── Volume (8-method suite) ────────────────────────────────────────────
    # volume_analysis uses rolling bar counts → adapts naturally to D/W/M
    va = volume_analysis(v, h, l, c)
    sig["vol_pts"]       = va["vol_pts"]
    sig["vol_signal"]    = va["vol_signal"]
    sig["vol_detail"]    = va["vol_detail"]
    sig["vol_sma20"]     = va["vol_sma20"]
    sig["vol_ema20"]     = va["vol_ema20"]
    sig["vol_sma50"]     = va["vol_sma50"]
    sig["vol_zscore"]    = va["vol_zscore"]
    sig["vol_ath_pct"]   = va["vol_ath_pct"]
    sig["vol_dry_surge"] = va["vol_dry_surge"]
    sig["vol_obv_break"] = va["vol_obv_break"]
    sig["vol_trend_up"]  = va["vol_trend_up"]

    # ── Chande Kroll Stop ─────────────────────────────────────────────────
    # ATR(10) + rolling(9) — works equally well on D/W/M bars
    if len(df) >= CKS_ATR_P + CKS_Q + 5:
        cks_long, _ = chande_kroll_stop(h, l, c, CKS_ATR_P, CKS_FACTOR, CKS_Q)
        prev_close = c.iloc[-2];  prev_cks = cks_long.iloc[-2]
        cur_close  = c.iloc[-1];  cur_cks  = cks_long.iloc[-1]
        if np.isnan(cur_cks):
            sig["cks_pts"] = 0; sig["cks_signal"] = "—"; sig["cks_val"] = None
        else:
            fresh_cks = (not np.isnan(prev_cks)
                         and prev_close < prev_cks and cur_close > cur_cks)
            above_cks = cur_close > cur_cks
            if fresh_cks:
                sig["cks_pts"] = 3; sig["cks_signal"] = "🔥 CKS CROSS"
            elif above_cks:
                sig["cks_pts"] = 2; sig["cks_signal"] = "✅ CKS ABOVE"
            else:
                sig["cks_pts"] = 0; sig["cks_signal"] = "↓ CKS BELOW"
            sig["cks_val"] = round(float(cur_cks), 2)
    else:
        sig["cks_pts"] = 0; sig["cks_signal"] = "—"; sig["cks_val"] = None

    # ── Donchian Channel breakout ─────────────────────────────────────────
    # Uses timeframe-scaled long/med periods set above
    donch_pts = 0; donch_sig = ""
    if len(df) >= tf_dc_long + 1:
        dc_h = h.rolling(tf_dc_long).max().iloc[-1]
        if c.iloc[-1] >= dc_h:
            donch_pts = 3; donch_sig = f"🔥 {lbl_dc_long} HIGH"
    if donch_pts == 0 and len(df) >= tf_dc_med + 1:
        dc_m = h.rolling(tf_dc_med).max().iloc[-1]
        if c.iloc[-1] >= dc_m:
            donch_pts = 2; donch_sig = f"⚡ {lbl_dc_med} HIGH"
    if donch_pts == 0 and len(df) >= 11:
        dc_10 = h.rolling(min(10, len(df)-1)).max().iloc[-1]
        if c.iloc[-1] >= dc_10:
            donch_pts = 1; donch_sig = "✅ 10-bar HIGH"
    sig["donch_pts"]    = donch_pts
    sig["donch_signal"] = donch_sig if donch_sig else "—"

    # ── Bollinger Bands(20) ────────────────────────────────────────────────
    # 20 bars = 20D / 20W(~5M) / 20M(~1.7yr) — reasonable for all frames
    if len(df) >= BB_P + 2:
        upper, lower, _, width, pct_b = bollinger(c, BB_P, BB_STD)
        w_cur  = width.iloc[-1]
        w_prev = width.iloc[-6:-1].mean()
        expanding   = (not np.isnan(w_prev)) and w_cur > w_prev
        pb          = pct_b.iloc[-1]
        above_upper = (not np.isnan(pb)) and pb > 1.0
        near_upper  = (not np.isnan(pb)) and pb > 0.9
        if above_upper and expanding:
            sig["bb_pts"] = 2; sig["bb_signal"] = f"🔥 BB BREAK {pb*100:.0f}%"
        elif near_upper:
            sig["bb_pts"] = 1; sig["bb_signal"] = f"✅ BB NEAR {pb*100:.0f}%"
        else:
            sig["bb_pts"] = 0
            sig["bb_signal"] = f"BB={pb*100:.0f}%" if not np.isnan(pb) else "—"
        sig["bb_pct_b"] = round(float(pb) * 100, 1) if not np.isnan(pb) else None
    else:
        sig["bb_pts"] = 0; sig["bb_signal"] = "—"; sig["bb_pct_b"] = None

    # ── ADX + DI(14) ──────────────────────────────────────────────────────
    # 14 bars = 14D / 14W / 14M — standard period, works for all
    if len(df) >= ADX_P * 3:
        adx_v, pos_di, neg_di = adx_di(h, l, c, ADX_P)
        adx_cur = adx_v.iloc[-1]; pdi = pos_di.iloc[-1]; ndi = neg_di.iloc[-1]
        bullish_di = (not np.isnan(pdi)) and (not np.isnan(ndi)) and pdi > ndi
        if not np.isnan(adx_cur):
            if adx_cur >= 35 and bullish_di:
                sig["adx_pts"] = 3; sig["adx_signal"] = f"🔥 ADX={adx_cur:.0f}"
            elif adx_cur >= 25 and bullish_di:
                sig["adx_pts"] = 2; sig["adx_signal"] = f"⚡ ADX={adx_cur:.0f}"
            elif adx_cur >= 20 and bullish_di:
                sig["adx_pts"] = 1; sig["adx_signal"] = f"✅ ADX={adx_cur:.0f}"
            else:
                sig["adx_pts"] = 0; sig["adx_signal"] = f"ADX={adx_cur:.0f}"
        else:
            sig["adx_pts"] = 0; sig["adx_signal"] = "—"
        sig["adx_val"]  = round(float(adx_cur), 1) if not np.isnan(adx_cur) else None
        sig["pdi_val"]  = round(float(pdi), 1)     if not np.isnan(pdi)     else None
        sig["ndi_val"]  = round(float(ndi), 1)     if not np.isnan(ndi)     else None
    else:
        sig["adx_pts"] = 0; sig["adx_signal"] = "—"
        sig["adx_val"] = sig["pdi_val"] = sig["ndi_val"] = None

    # Frame total
    all_pts = [sig.get(k, 0) for k in [
        "cci200_pts","cci1000_pts","macd200_pts","macd1000_pts",
        "mfi_pts","vol_pts","cks_pts","donch_pts","bb_pts","adx_pts"
    ]]
    sig["frame_score"] = sum(all_pts)
    return sig


def analyse_stock(ticker: str, company: str, df_d: pd.DataFrame) -> dict | None:
    try:
        if len(df_d) < MIN_BARS_D:
            return None

        close = df_d["Close"].iloc[-1]
        if close <= 0 or close != close:
            return None

        # Resample
        df_w = resample_to_weekly(df_d)
        df_m = resample_to_monthly(df_d)

        d = score_frame(df_d, "D")
        # Weekly: need ≥55 bars for CCI(50); monthly: ≥25 for CCI(20)
        w = score_frame(df_w, "W") if len(df_w) >= 55 else {}
        m = score_frame(df_m, "M") if len(df_m) >= 25 else {}

        d_score = d.get("frame_score", 0)
        w_score = w.get("frame_score", 0)
        m_score = m.get("frame_score", 0)

        # Weekly/monthly weighted less (fewer bars = fewer signal opportunities)
        total = d_score + round(w_score * 0.7) + round(m_score * 0.4)

        signal = ("🚀 ROCKET" if total >= SCORE_ROCKET
                  else "⚡ LAUNCH" if total >= SCORE_LAUNCH
                  else "👀 WATCH"  if total >= SCORE_WATCH
                  else "—")

        # 52-week ATH calc
        high_52w = df_d["High"].rolling(252).max().iloc[-1]
        ath_pct  = round((close / high_52w - 1) * 100, 1) if high_52w > 0 else None

        # 5-method target + stop loss analysis (daily bars)
        ta = target_analysis(
            df_d["Close"], df_d["High"], df_d["Low"], df_d["Volume"],
            cks_stop=d.get("cks_val")
        )

        return {
            "ticker":   ticker,
            "company":  company[:30],
            "close":    round(float(close), 2),
            "ath_pct":  ath_pct,
            "score":    total,
            "score_d":  d_score,
            "score_w":  w_score,
            "score_m":  m_score,
            "signal":   signal,
            "is_fo":    ticker in _FO_SET,
            "daily":    d,
            "weekly":   w,
            "monthly":  m,
            "ta":       ta,
        }
    except Exception as e:
        log.debug(f"{ticker} error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML REPORT
# ═══════════════════════════════════════════════════════════════════════════════

SIG_COLORS = {
    "🚀 ROCKET": ("#dcfce7", "#166534"),
    "⚡ LAUNCH":  ("#fef9c3", "#854d0e"),
    "👀 WATCH":   ("#dbeafe", "#1e40af"),
    "—":          ("#f3f4f6", "#6b7280"),
}

FRAME_COLS = [
    ("cci200",  "CCI Short\nD:200 W:50 M:20"),
    ("cci1000", "CCI Long\nD:1000 W:200 M:50"),
    ("macd200",  "MACD Slow\nD:34,200 W:34,50 M:12,26"),
    ("macd1000", "MACD Ultra\nD:34,1000 W:34,200 M:34,50"),
    ("mfi",     "MFI(14)\nD/W/M"),
    ("vol",     "📊 Volume\n8 methods · D/W/M"),
    ("cks",     "Chande Kroll\nATR D/W/M"),
    ("donch",   "Donchian\nD:52W/13W W:52/26bar M:12/6mo"),
    ("bb",      "Bollinger(20)\nD/W/M"),
    ("adx",     "ADX+DI(14)\nD/W/M"),
]

def cell_color(signal: str) -> tuple[str, str]:
    if "CROSS" in signal or "ROCKET" in signal or "52W" in signal or "BREAKOUT" in signal:
        return ("#dcfce7", "#166534")
    if "BULL" in signal or "ABOVE" in signal or "13W" in signal or "NEAR" in signal or ">70" in signal or ">80" in signal:
        return ("#f0fdf4", "#15803d")
    if "ADX=" in signal and ("🔥" in signal or "⚡" in signal):
        return ("#fef9c3", "#854d0e")
    if "x" in signal and ("🔥" in signal or "⚡" in signal):
        return ("#fff7ed", "#c2410c")
    if "↓" in signal or "BEAR" in signal or "BELOW" in signal:
        return ("#fef2f2", "#b91c1c")
    return ("#f9fafb", "#6b7280")


def sig_cell(signal: str) -> str:
    bg, fg = cell_color(signal)
    return (f'<td style="padding:5px 7px;font-size:11px;white-space:nowrap;'
            f'background:{bg};color:{fg};font-weight:600;">{signal}</td>')


def build_html(results: list[dict], run_ts: str) -> str:
    rockets = [r for r in results if r["score"] >= SCORE_ROCKET]
    launches = [r for r in results if SCORE_LAUNCH <= r["score"] < SCORE_ROCKET]
    watches  = [r for r in results if SCORE_WATCH  <= r["score"] < SCORE_LAUNCH]

    def section(title: str, items: list[dict], accent: str) -> str:
        if not items:
            return f'<div style="color:#6b7280;font-style:italic;margin-bottom:24px;">No {title} stocks today.</div>'
        rows = ""
        for r in items:
            bg_card, fg_card = SIG_COLORS.get(r["signal"], ("#f3f4f6","#374151"))
            fo_badge = ('<span style="background:#7c3aed;color:#fff;font-size:9px;font-weight:700;'
                        'padding:1px 5px;border-radius:3px;margin-left:5px;vertical-align:middle;'
                        'letter-spacing:0.3px;">F&amp;O</span>' if r.get("is_fo") else "")
            rows += f"""
<tr>
  <td style="padding:6px 10px;font-weight:700;font-size:13px;border-bottom:1px solid #e5e7eb;white-space:nowrap;">
    {r['ticker']}{fo_badge}</td>
  <td style="padding:6px 10px;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{r['company']}</td>
  <td style="padding:6px 10px;text-align:center;border-bottom:1px solid #e5e7eb;">
    <span style="background:{bg_card};color:{fg_card};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;">{r['signal']}</span></td>
  <td style="padding:6px 10px;text-align:center;font-weight:700;font-size:15px;border-bottom:1px solid #e5e7eb;color:{accent};">{r['score']}</td>
  <td style="padding:6px 10px;text-align:center;font-size:11px;border-bottom:1px solid #e5e7eb;color:#1d4ed8;">{r['score_d']}</td>
  <td style="padding:6px 10px;text-align:center;font-size:11px;border-bottom:1px solid #e5e7eb;color:#7c3aed;">{r['score_w']}</td>
  <td style="padding:6px 10px;text-align:center;font-size:11px;border-bottom:1px solid #e5e7eb;color:#0891b2;">{r['score_m']}</td>
  <td style="padding:6px 10px;text-align:right;font-weight:600;border-bottom:1px solid #e5e7eb;">₹{r['close']:,.2f}</td>
  <td style="padding:6px 10px;text-align:center;font-size:11px;border-bottom:1px solid #e5e7eb;color:{'#b91c1c' if r.get('ath_pct') and r['ath_pct'] < -20 else '#15803d'};">{r['ath_pct']}%</td>
"""
            # ── Stop Loss column (all 4 methods) ─────────────────────────
            ta = r.get("ta", {})
            stops = ta.get("stops", {})
            if stops:
                psl    = stops.get("primary", 0)
                pslp   = stops.get("primary_pct", 0)
                atr1   = stops.get("sl_atr1");   atr1p  = stops.get("sl_atr1_pct")
                atr2   = stops.get("sl_atr2");   atr2p  = stops.get("sl_atr2_pct")
                cks_s  = stops.get("sl_cks");    cks_p  = stops.get("sl_cks_pct")
                base_s = stops.get("sl_base");   base_p = stops.get("sl_base_pct")
                def sl_row(lbl: str, val, vp, rec: bool = False) -> str:
                    if val is None:
                        return ""
                    clr = "#b91c1c" if rec else "#9ca3af"
                    fw  = "font-weight:700;" if rec else ""
                    star = " ⭐" if rec else ""
                    return (f'<div style="font-size:9px;color:{clr};{fw}margin-bottom:1px;">'
                            f'{lbl}{star}: ₹{val:,.2f} ({vp}%)</div>')
                rows += (
                    f'<td style="padding:4px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top;min-width:130px;">'
                    f'<div style="font-size:11px;font-weight:700;color:#b91c1c;">₹{psl:,.2f}</div>'
                    f'<div style="font-size:10px;color:#b91c1c;margin-bottom:3px;">{pslp}% · ATR1× stop</div>'
                    + sl_row("ATR 1×", atr1, atr1p, True)
                    + sl_row("ATR 2×", atr2, atr2p)
                    + sl_row("CKS",    cks_s, cks_p)
                    + sl_row("Base",   base_s, base_p)
                    + f'<div style="font-size:9px;color:#6b7280;margin-top:2px;">ATR=₹{ta.get("atr14",0):.1f} ({ta.get("atr_pct",0)}%)</div>'
                    + f'</td>'
                )
            else:
                rows += '<td style="border-bottom:1px solid #e5e7eb;color:#9ca3af;text-align:center;font-size:10px;">—</td>'

            # ── Targets column (recommended method prominent + others) ────
            if ta:
                rec    = ta.get("rec", {})
                rl     = ta.get("rec_label", "")
                rwhy   = ta.get("rec_why", "")
                psl_v  = stops.get("primary", 0) if stops else 0

                def trow(m: dict, is_rec: bool) -> str:
                    if not m:
                        return ""
                    tag   = m.get("tag", "")
                    lbl   = m.get("label", "")
                    t1p   = m.get("t1_pct", "")
                    t2p   = m.get("t2_pct", "")
                    notes = m.get("notes", "")
                    t1v   = m.get("t1", 0)
                    t2v   = m.get("t2", 0)
                    rr1   = m.get("t1_rr")
                    rr2   = m.get("t2_rr")
                    rr1s  = f" R/R {rr1}:1" if rr1 else ""
                    rr2s  = f" R/R {rr2}:1" if rr2 else ""
                    if is_rec:
                        return (
                            f'<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:4px;'
                            f'padding:3px 5px;margin-bottom:3px;">'
                            f'<div style="font-size:10px;font-weight:700;color:#166534;">{rl} ({tag} {lbl})</div>'
                            f'<div style="font-size:10px;color:#166534;">T1 ₹{t1v:,.0f} +{t1p}%{rr1s}</div>'
                            f'<div style="font-size:10px;color:#15803d;">T2 ₹{t2v:,.0f} +{t2p}%{rr2s}</div>'
                            f'<div style="font-size:9px;color:#6b7280;margin-top:1px;">{notes}</div>'
                            f'<div style="font-size:8px;color:#6b7280;font-style:italic;">{rwhy}</div>'
                            f'</div>'
                        )
                    else:
                        return (
                            f'<div style="font-size:9px;color:#374151;margin-bottom:1px;">'
                            f'<span style="color:#6b7280;">{tag}</span> '
                            f'T1 +{t1p}% T2 +{t2p}% '
                            f'<span style="color:#9ca3af;">({lbl})</span></div>'
                        )

                m1 = ta.get("m1", {}); m2 = ta.get("m2", {})
                m3 = ta.get("m3", {}); m4 = ta.get("m4", {}); m5 = ta.get("m5", {})
                rec_tag = ta.get("rec_tag", "M1")
                rows += (
                    f'<td style="padding:4px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top;min-width:200px;">'
                    + trow(m1, rec_tag == "M1")
                    + trow(m2, rec_tag == "M2")
                    + trow(m5, rec_tag == "M5")
                    + trow(m3, False)
                    + trow(m4, False)
                    + f'</td>'
                )
            else:
                rows += '<td style="border-bottom:1px solid #e5e7eb;color:#9ca3af;text-align:center;font-size:10px;">—</td>'

            for key, _ in FRAME_COLS:
                d_sig = r["daily"].get(f"{key}_signal", "—")
                w_sig = r["weekly"].get(f"{key}_signal", "—")
                m_sig = r["monthly"].get(f"{key}_signal", "—")
                if key == "vol":
                    # Volume column: show score + full 8-method detail on daily
                    d_detail = r["daily"].get("vol_detail", "")
                    rows += (
                        f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;vertical-align:top;min-width:160px;">'
                        f'<div style="font-size:10px;color:#374151;margin-bottom:2px;font-weight:700;">D: {d_sig}</div>'
                        f'<div style="font-size:9px;color:#374151;opacity:0.8;line-height:1.5;">{d_detail}</div>'
                        f'<div style="font-size:9px;color:#7c3aed;margin-top:3px;">W: {w_sig}</div>'
                        f'<div style="font-size:9px;color:#0891b2;">M: {m_sig}</div>'
                        f'</td>'
                    )
                else:
                    rows += (f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;vertical-align:top;">'
                             f'<div style="font-size:10px;color:#374151;margin-bottom:2px;font-weight:600;">D: {d_sig}</div>'
                             f'<div style="font-size:9px;color:#7c3aed;">W: {w_sig}</div>'
                             f'<div style="font-size:9px;color:#0891b2;">M: {m_sig}</div>'
                             f'</td>')
            rows += "</tr>\n"

        header_style = (
            f"background:{accent};color:#fff;padding:7px 10px;font-size:11px;"
            "font-weight:700;text-align:center;white-space:pre-line;"
        )
        headers = "".join(
            f'<th style="{header_style}">{lbl}</th>' for _, lbl in FRAME_COLS
        )
        return f"""
<div style="margin-bottom:32px;">
  <h2 style="font-size:18px;color:{accent};margin:0 0 10px;">{title}
    <span style="font-size:13px;font-weight:400;color:#6b7280;">({len(items)} stocks)</span>
  </h2>
  <div style="overflow-x:auto;">
  <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px;box-shadow:0 1px 4px rgba(0,0,0,0.08);border-radius:8px;overflow:hidden;">
    <thead>
      <tr>
        <th style="{header_style}">Ticker</th>
        <th style="{header_style}">Company</th>
        <th style="{header_style}">Signal</th>
        <th style="{header_style}">Score\nTotal</th>
        <th style="{header_style}">D\nScore</th>
        <th style="{header_style}">W\nScore</th>
        <th style="{header_style}">M\nScore</th>
        <th style="{header_style}">Close</th>
        <th style="{header_style}">52W\nATH%</th>
        <th style="{header_style}">🛡 Stop Loss\nATR1× · ATR2× · CKS · Base</th>
        <th style="{header_style}">📐 Upside Targets\nM1 ATR · M2 Z-Score · M3 ADR · M4 Fib · M5 Vol-Surge</th>
        {headers}
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
</div>"""

    rocket_section  = section("🚀 ROCKET",  rockets,  "#166534")
    launch_section  = section("⚡ LAUNCH",   launches, "#b45309")
    watch_section   = section("👀 WATCH",    watches,  "#1d4ed8")

    legend = """
<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;
            font-size:12px;color:#374151;line-height:1.9;margin-bottom:24px;">
  <strong style="font-size:13px;">📖 How to read this report — all indicators run on Daily · Weekly · Monthly</strong><br>
  <strong>Score</strong> = weighted sum: Daily (1×) + Weekly (0.7×) + Monthly (0.4×) — each timeframe scored independently<br>
  <strong>🔥 CROSS</strong> = fresh crossover/breakout within last 3 bars (highest priority) &nbsp;|&nbsp;
  <strong>⚡</strong> = strong signal &nbsp;|&nbsp; <strong>✅</strong> = confirmed signal &nbsp;|&nbsp; <strong>↓</strong> = negative/bearish<br>

  <strong style="color:#166534;">Periods auto-scale per timeframe so every indicator fires meaningfully:</strong><br>
  <table style="font-size:11px;border-collapse:collapse;margin:4px 0 8px;">
    <tr style="background:#e5e7eb;">
      <th style="padding:3px 10px;text-align:left;">Indicator</th>
      <th style="padding:3px 10px;">Daily</th>
      <th style="padding:3px 10px;">Weekly</th>
      <th style="padding:3px 10px;">Monthly</th>
      <th style="padding:3px 10px;text-align:left;">Why</th>
    </tr>
    <tr><td style="padding:2px 10px;"><strong>CCI Short</strong></td><td style="padding:2px 10px;text-align:center;">200</td><td style="padding:2px 10px;text-align:center;">50</td><td style="padding:2px 10px;text-align:center;">20</td><td style="padding:2px 10px;color:#6b7280;">All ≈ 1–2 year lookback. Cross above +100 = trend breakout</td></tr>
    <tr style="background:#f9fafb;"><td style="padding:2px 10px;"><strong>CCI Long</strong></td><td style="padding:2px 10px;text-align:center;">1000</td><td style="padding:2px 10px;text-align:center;">200</td><td style="padding:2px 10px;text-align:center;">50</td><td style="padding:2px 10px;color:#6b7280;">All ≈ 4 year lookback. Major multi-year base breakout</td></tr>
    <tr><td style="padding:2px 10px;"><strong>MACD Slow</strong></td><td style="padding:2px 10px;text-align:center;">34,200,9</td><td style="padding:2px 10px;text-align:center;">34,50,9</td><td style="padding:2px 10px;text-align:center;">12,26,9</td><td style="padding:2px 10px;color:#6b7280;">Slow EMA flip = major trend change</td></tr>
    <tr style="background:#f9fafb;"><td style="padding:2px 10px;"><strong>MACD Ultra</strong></td><td style="padding:2px 10px;text-align:center;">34,1000,9</td><td style="padding:2px 10px;text-align:center;">34,200,9</td><td style="padding:2px 10px;text-align:center;">34,50,9</td><td style="padding:2px 10px;color:#6b7280;">Ultra-slow flip = decade-level trend change</td></tr>
    <tr><td style="padding:2px 10px;"><strong>MFI</strong></td><td style="padding:2px 10px;text-align:center;">14</td><td style="padding:2px 10px;text-align:center;">14</td><td style="padding:2px 10px;text-align:center;">14</td><td style="padding:2px 10px;color:#6b7280;">&gt;70 = institutional flow; &gt;80 = strong buying</td></tr>
    <tr style="background:#f9fafb;"><td style="padding:2px 10px;"><strong>Donchian</strong></td><td style="padding:2px 10px;text-align:center;">252/65 bars</td><td style="padding:2px 10px;text-align:center;">52/26 bars</td><td style="padding:2px 10px;text-align:center;">12/6 bars</td><td style="padding:2px 10px;color:#6b7280;">All ≈ 52-week / 26-week price highs</td></tr>
    <tr><td style="padding:2px 10px;"><strong>Bollinger</strong></td><td style="padding:2px 10px;text-align:center;">20</td><td style="padding:2px 10px;text-align:center;">20</td><td style="padding:2px 10px;text-align:center;">20</td><td style="padding:2px 10px;color:#6b7280;">Squeeze→expansion; price above upper band</td></tr>
    <tr style="background:#f9fafb;"><td style="padding:2px 10px;"><strong>ADX+DI</strong></td><td style="padding:2px 10px;text-align:center;">14</td><td style="padding:2px 10px;text-align:center;">14</td><td style="padding:2px 10px;text-align:center;">14</td><td style="padding:2px 10px;color:#6b7280;">&gt;25 = trend; &gt;35 = strong trend; +DI &gt; -DI = bullish</td></tr>
    <tr><td style="padding:2px 10px;"><strong>Chande Kroll</strong></td><td style="padding:2px 10px;text-align:center;">ATR(10)+Q(9)</td><td style="padding:2px 10px;text-align:center;">same</td><td style="padding:2px 10px;text-align:center;">same</td><td style="padding:2px 10px;color:#6b7280;">Price crossing above CKS stop = trend inception</td></tr>
  </table>

  <strong style="font-size:13px;color:#b91c1c;">🛡 Stop Loss — 4 methods (all shown, ⭐ = recommended for momentum)</strong><br>
  <table style="font-size:11px;border-collapse:collapse;margin:4px 0 10px;width:100%;">
    <tr style="background:#fee2e2;">
      <th style="padding:3px 10px;text-align:left;">Method</th>
      <th style="padding:3px 10px;">Formula</th>
      <th style="padding:3px 10px;text-align:left;">Best for</th>
      <th style="padding:3px 10px;text-align:left;">Note</th>
    </tr>
    <tr style="background:#fef2f2;"><td style="padding:2px 10px;font-weight:700;">⭐ ATR 1×</td><td style="padding:2px 10px;text-align:center;">cur − 1.0 × ATR(14)</td><td style="padding:2px 10px;">Momentum (default)</td><td style="padding:2px 10px;color:#6b7280;">Tight. Broken = momentum failed. Exit immediately.</td></tr>
    <tr><td style="padding:2px 10px;font-weight:600;">ATR 2×</td><td style="padding:2px 10px;text-align:center;">cur − 2.0 × ATR(14)</td><td style="padding:2px 10px;">Swing trade</td><td style="padding:2px 10px;color:#6b7280;">Wider cushion; tolerates intraday shakeouts.</td></tr>
    <tr style="background:#fef2f2;"><td style="padding:2px 10px;">CKS</td><td style="padding:2px 10px;text-align:center;">Chande Kroll Stop line</td><td style="padding:2px 10px;">Trend inception</td><td style="padding:2px 10px;color:#6b7280;">Level price just crossed above. Re-crossing down = trend inception failed.</td></tr>
    <tr><td style="padding:2px 10px;">Base</td><td style="padding:2px 10px;text-align:center;">20-bar low × 0.98</td><td style="padding:2px 10px;">Wide structural</td><td style="padding:2px 10px;color:#6b7280;">Base support with 2% buffer. Use when staying in a multi-day swing.</td></tr>
  </table>

  <strong style="font-size:13px;color:#0891b2;">📐 Upside Targets — 5 methods (goal: ride daily trend with big move + volume)</strong><br>
  <table style="font-size:11px;border-collapse:collapse;margin:4px 0 10px;width:100%;">
    <tr style="background:#dbeafe;">
      <th style="padding:3px 10px;text-align:left;">Method</th>
      <th style="padding:3px 10px;">Formula</th>
      <th style="padding:3px 10px;">T1 / T2 / T3</th>
      <th style="padding:3px 10px;text-align:left;">Best when</th>
      <th style="padding:3px 10px;text-align:left;">Advantage / Limitation</th>
    </tr>
    <tr style="background:#eff6ff;">
      <td style="padding:2px 10px;font-weight:700;">⭐ M1 ATR Multi</td>
      <td style="padding:2px 10px;text-align:center;">cur + N × ATR(14)</td>
      <td style="padding:2px 10px;text-align:center;">1.5× / 2.5× / 4.0× / 6.0×</td>
      <td style="padding:2px 10px;"><strong>Always — use as default</strong></td>
      <td style="padding:2px 10px;color:#6b7280;">ATR already encodes volatility + recent momentum energy. Bigger ATR = bigger realistic targets. Universal.</td>
    </tr>
    <tr>
      <td style="padding:2px 10px;font-weight:700;">⭐ M2 Z-Score</td>
      <td style="padding:2px 10px;text-align:center;">cur × (1 + N × ret_std60)</td>
      <td style="padding:2px 10px;text-align:center;">1.5σ / 2.5σ / 4.0σ</td>
      <td style="padding:2px 10px;">today_z ≥ 1.5σ (unusual move)</td>
      <td style="padding:2px 10px;color:#6b7280;">Calibrated to THIS stock's 60-bar return distribution. today_z shows if today is already a σ-event. Best for catching acceleration.</td>
    </tr>
    <tr style="background:#eff6ff;">
      <td style="padding:2px 10px;">M3 Avg Daily Range</td>
      <td style="padding:2px 10px;text-align:center;">cur + N × ADR(20)</td>
      <td style="padding:2px 10px;text-align:center;">1× / 2× / 3×</td>
      <td style="padding:2px 10px;">Sanity check vs typical range</td>
      <td style="padding:2px 10px;color:#6b7280;">Intuitive: "T1 = one typical day's move." Limitation: range-based, not direction-aware. Use to validate M1/M2.</td>
    </tr>
    <tr>
      <td style="padding:2px 10px;">M4 Fibonacci</td>
      <td style="padding:2px 10px;text-align:center;">base_high + ratio × range</td>
      <td style="padding:2px 10px;text-align:center;">127% / 162% / 200% / 262%</td>
      <td style="padding:2px 10px;">Clean base/consolidation box</td>
      <td style="padding:2px 10px;color:#6b7280;">Self-fulfilling resistance (many traders use these levels). Best for chart-based entries with a clear 20-bar base.</td>
    </tr>
    <tr style="background:#eff6ff;">
      <td style="padding:2px 10px;font-weight:700;">⭐ M5 Vol-Surge</td>
      <td style="padding:2px 10px;text-align:center;">cur + N × ADR × surge_mult</td>
      <td style="padding:2px 10px;text-align:center;">1× / 2× / 3.5× (× surge)</td>
      <td style="padding:2px 10px;">vol Z-score ≥ 3 (extreme)</td>
      <td style="padding:2px 10px;color:#6b7280;">Higher volume conviction = can sustain larger move. surge_mult = clamp(vol_z, 1, 5). At Z=4, targets are 4× larger than ADR alone.</td>
    </tr>
  </table>
  <em style="color:#6b7280;font-size:11px;">
    Auto-recommendation: vol Z ≥ 3 → M5 · today_z ≥ 2 → M2 · default → M1 &nbsp;|&nbsp;
    Highlighted box in Targets column = auto-recommended method for this stock today &nbsp;|&nbsp;
    R/R = reward-to-risk ratio (e.g. 3.2:1 = ₹3.20 potential per ₹1 risked vs ATR1× stop)
  </em><br>

  <strong style="color:#b45309;">📊 Volume Suite (8 methods, max 10 pts) — computed on each timeframe:</strong><br>
  &nbsp;&nbsp;<strong>vs SMA20</strong> ≥2x✅ ≥3x⚡ ≥5x🔥 &nbsp;·&nbsp;
  <strong>vs EMA20</strong> (faster baseline) &nbsp;·&nbsp;
  <strong>vs SMA50</strong> (longer baseline, filters noise) &nbsp;·&nbsp;
  <strong>Z-Score</strong> σ≥2✅ σ≥3⚡ σ≥4🔥 (statistically unusual)<br>
  &nbsp;&nbsp;<strong>ATH Vol</strong> all-time high or within 10%/25% 🔥 (ultimate conviction) &nbsp;·&nbsp;
  <strong>DRY→Surge</strong> 5-bar quiet then explosion (VCP accumulation breakout) &nbsp;·&nbsp;
  <strong>OBV</strong> new 20-bar high (smart money confirmed) &nbsp;·&nbsp;
  <strong>Trend</strong> 5D&gt;10D&gt;20D average (rising accumulation)<br>
  <em style="color:#6b7280;font-size:11px;">Volume detail row (below D score) shows all 8 sub-signals for that timeframe's bars.
  W and M volume uses weekly/monthly bars — a 5x weekly surge = 5x vs 20-week average, which is extremely rare.</em>
</div>"""

    stat_row = f"""
<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:28px;">
  {"".join(
      f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px 20px;text-align:center;min-width:110px;">'
      f'<div style="font-size:22px;font-weight:700;color:{c};">{n}</div>'
      f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">{lbl}</div></div>'
      for n, lbl, c in [
          (len(results), "Scanned",   "#374151"),
          (len(rockets), "🚀 Rocket",  "#166534"),
          (len(launches),"⚡ Launch",  "#b45309"),
          (len(watches), "👀 Watch",   "#1d4ed8"),
      ]
  )}
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🚀 Rocket Scanner — {run_ts}</title>
<style>
  body{{margin:0;padding:20px;background:#f3f4f6;font-family:'Segoe UI',Arial,sans-serif;color:#111;}}
  h1{{font-size:24px;margin:0 0 4px;}}
  h2{{margin-top:0;}}
  table td, table th{{border:none;}}
</style>
</head>
<body>
<div style="max-width:1600px;margin:0 auto;">

  <div style="background:linear-gradient(135deg,#0f172a,#1e3a5f);color:#fff;
              padding:24px 32px;border-radius:12px;margin-bottom:24px;">
    <h1>🚀 Rocket Scanner — NSE Explosive Breakout Finder</h1>
    <p style="margin:4px 0 0;opacity:0.7;font-size:13px;">{run_ts} &nbsp;|&nbsp;
       CCI(200/1000) · MACD(34,200/1000,9) · MFI · 📊 Volume×8 [SMA20·EMA20·SMA50·Z-Score·ATH·DRY→Surge·OBV·Trend] ·
       Chande Kroll · Donchian · Bollinger · ADX &nbsp;·&nbsp; D/W/M</p>
  </div>

  {stat_row}
  {legend}
  {rocket_section}
  {launch_section}
  {watch_section}

  <div style="font-size:11px;color:#9ca3af;text-align:center;padding:16px 0;">
    Rocket Scanner &nbsp;|&nbsp; Not financial advice &nbsp;|&nbsp;
    Verify signals before trading. Past breakouts ≠ future performance.
  </div>
</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_file = f"rocket_scan_{ts_tag}.html"

    log.info("=" * 60)
    log.info("🚀 ROCKET SCANNER — NSE Explosive Breakout Finder")
    log.info("=" * 60)

    _load_fo_list()

    universe = load_universe()
    log.info(f"Universe: {len(universe)} stocks")

    all_data = fetch_all_data(universe)
    log.info(f"Data ready: {len(all_data)} stocks")

    results = []
    done, total = 0, len(all_data)
    ticker_map = {s["ticker"]: s["company"] for s in universe}

    for ticker, df_d in all_data.items():
        done += 1
        rec = analyse_stock(ticker, ticker_map.get(ticker, ticker), df_d)
        if rec and rec["score"] >= SCORE_WATCH:
            results.append(rec)
        if done % 200 == 0 or done == total:
            log.info(f"  Processed {done}/{total} — {len(results)} candidates so far")

    results.sort(key=lambda x: -x["score"])

    log.info(f"\n{'='*60}")
    log.info(f"🚀 ROCKETS  (≥{SCORE_ROCKET} pts): {sum(1 for r in results if r['score'] >= SCORE_ROCKET)}")
    log.info(f"⚡ LAUNCHES (≥{SCORE_LAUNCH} pts): {sum(1 for r in results if SCORE_LAUNCH <= r['score'] < SCORE_ROCKET)}")
    log.info(f"👀 WATCHES  (≥{SCORE_WATCH}  pts): {sum(1 for r in results if SCORE_WATCH  <= r['score'] < SCORE_LAUNCH)}")

    html = build_html(results, run_ts)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"\n✅ Report saved → {out_file}")

    # Keep a fixed-name copy for Flask dashboard
    with open("rocket_scan_latest.html", "w", encoding="utf-8") as f:
        f.write(html)

    if results:
        log.info("\n🏆 TOP 10 ROCKETS:")
        for r in results[:10]:
            log.info(f"  {r['ticker']:12s} {r['signal']:12s} score={r['score']:2d} "
                     f"(D:{r['score_d']} W:{r['score_w']} M:{r['score_m']}) "
                     f"₹{r['close']:,.0f} 52W:{r['ath_pct']}%")


if __name__ == "__main__":
    main()
