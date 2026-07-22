"""
seed_shared_cache.py
────────────────────
Reads the RSI MTF shared cache (stock_data_cache.pkl, v2 format) once and
pre-populates the per-scanner caches so yfinance is never called twice for
the same stock on the same day.

Scanners seeded
───────────────
1. NSE Index Dashboard  (realtime_analysis4.py)
   Expects: india/NSE/cache/{TICKER.NS}_1d_max.pkl  → plain DataFrame

2. Rocket Scanner  (rocket_scanner.py)
   Expects: rocket_cache.pkl  → {SYMBOL: {"date": "YYYYMMDD", "df": DataFrame}}
   Note: keys are plain symbols WITHOUT .NS suffix

3. ATH Breakout  (ath_report.py)
   Reads DataFrame from RSI cache directly (patched in ath_report.py).
   No file seeding needed — shared in-memory dict handles it.

4. F&O Scanner  (fo_scanner_report.py)
   Already uses stock_data_cache.pkl natively — zero changes needed.

Usage
─────
Run before any scanner that can benefit:
    python seed_shared_cache.py
    python IndexDashBoard/realtime_analysis4.py index_dashboard_config.json
    python rocket_scanner.py
    python ath_report.py
"""

import os
import pickle
import time
from datetime import datetime

RSI_CACHE_FILE   = "stock_data_cache.pkl"
INDEX_CACHE_DIR  = os.path.join("india", "NSE", "cache")  # must match index_dashboard_config.json
ROCKET_CACHE     = "rocket_cache.pkl"
INDEX_INTERVAL   = "1d"
INDEX_PERIOD     = "max"
MAX_AGE_HOURS    = 23   # skip if RSI cache is stale — don't seed from yesterday's data


def _load_rsi_cache():
    if not os.path.exists(RSI_CACHE_FILE):
        print(f"[seed] RSI cache not found at '{RSI_CACHE_FILE}' — other scanners will download fresh.")
        return None

    age_h = (time.time() - os.path.getmtime(RSI_CACHE_FILE)) / 3600
    if age_h > MAX_AGE_HOURS:
        print(f"[seed] RSI cache is {age_h:.1f}h old (> {MAX_AGE_HOURS}h) — skipping seed to avoid stale data.")
        return None

    print(f"[seed] Loading RSI cache ({RSI_CACHE_FILE}, {age_h:.1f}h old)…")
    try:
        with open(RSI_CACHE_FILE, "rb") as fh:
            raw = pickle.load(fh)
        if not isinstance(raw, dict):
            print("[seed] Unexpected RSI cache format — skipping.")
            return None
        n = sum(1 for k in raw if not k.startswith("__"))
        print(f"[seed] RSI cache loaded: {n} tickers.")
        return raw
    except Exception as e:
        print(f"[seed] Failed to load RSI cache: {e}")
        return None


def _extract_df(entry):
    """Extract a DataFrame from an RSI v2 entry dict or a raw v1 DataFrame."""
    if isinstance(entry, dict):
        return entry.get("df")
    if hasattr(entry, "empty"):        # raw DataFrame (v1 format)
        return entry
    return None


def seed_index_dashboard(raw: dict) -> int:
    """Write per-ticker .pkl files for realtime_analysis4.py's get_cached_or_fresh().

    RSI cache keys are plain symbols (e.g. 'RELIANCE').
    realtime_analysis4.py builds tickers as f'{sym}.NS' and looks for
    'RELIANCE.NS_1d_max.pkl', so we must append '.NS' when naming the file.
    """
    os.makedirs(INDEX_CACHE_DIR, exist_ok=True)
    written = skipped = 0
    rsi_mtime = os.path.getmtime(RSI_CACHE_FILE)

    for symbol, entry in raw.items():
        if symbol.startswith("__"):
            continue
        df = _extract_df(entry)
        if df is None or (hasattr(df, "empty") and df.empty):
            skipped += 1
            continue

        # realtime_analysis4.py requests cache files with the yfinance ticker name,
        # which for NSE equities is always SYMBOL.NS  (e.g. RELIANCE.NS_1d_max.pkl).
        yf_ticker = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
        out_path = os.path.join(INDEX_CACHE_DIR, f"{yf_ticker}_{INDEX_INTERVAL}_{INDEX_PERIOD}.pkl")

        # Don't overwrite a file that is newer than the RSI cache itself
        if os.path.exists(out_path) and os.path.getmtime(out_path) >= rsi_mtime:
            skipped += 1
            continue

        try:
            with open(out_path, "wb") as f:
                pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)
            written += 1
        except Exception as e:
            print(f"[seed][index] ⚠️  {out_path}: {e}")

    return written


def seed_rocket(raw: dict) -> int:
    """Write rocket_cache.pkl so rocket_scanner.py skips re-downloading today's data.

    RSI cache keys are plain symbols (e.g. 'RELIANCE') — same format that
    rocket_scanner.py uses for its own cache — so no suffix stripping is needed.
    """
    today_str = datetime.now().strftime("%Y%m%d")

    # Load existing rocket cache so we only update stale/missing entries
    existing = {}
    if os.path.exists(ROCKET_CACHE):
        try:
            with open(ROCKET_CACHE, "rb") as f:
                existing = pickle.load(f)
        except Exception:
            existing = {}

    added = skipped = 0
    for symbol, entry in raw.items():
        if symbol.startswith("__"):
            continue
        # RSI cache keys are already plain symbols (no .NS).
        # Skip index tickers that do start with ^ (e.g. ^NSEI) — Rocket has no use for them.
        if symbol.startswith("^"):
            skipped += 1
            continue

        # Already fresh in rocket cache — skip
        if existing.get(symbol, {}).get("date") == today_str:
            skipped += 1
            continue

        df = _extract_df(entry)
        if df is None or (hasattr(df, "empty") and df.empty):
            skipped += 1
            continue

        existing[symbol] = {"date": today_str, "df": df}
        added += 1

    try:
        with open(ROCKET_CACHE, "wb") as f:
            pickle.dump(existing, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        print(f"[seed][rocket] ⚠️  Could not write {ROCKET_CACHE}: {e}")
        return 0

    return added


def main():
    raw = _load_rsi_cache()
    if raw is None:
        return

    # 1. Index Dashboard
    idx_written = seed_index_dashboard(raw)
    print(f"[seed] Index Dashboard  : {idx_written} ticker files → '{INDEX_CACHE_DIR}/'")

    # 2. Rocket Scanner
    rkt_added = seed_rocket(raw)
    print(f"[seed] Rocket Scanner   : {rkt_added} entries → '{ROCKET_CACHE}'")

    print("[seed] Done. ATH uses RSI cache directly (patched in ath_report.py). F&O shares stock_data_cache.pkl natively.")


if __name__ == "__main__":
    main()
