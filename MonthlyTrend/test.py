#!/usr/bin/env python3
"""
NSE Momentum Screener using EQUITY_L.csv + yfinance

- Reads EQUITY_L.csv as stock universe
- Maps NSE symbols to Yahoo Finance tickers (SYMBOL.NS)
- Downloads price data with yfinance (with progress bar)
- Computes multi-window returns and simple momentum scores
- Tags each stock with a momentum_style label:
    "PPAP-style"       = strong multi-window uptrend
    "MTAR-style"       = strong recent momentum, moderate longer term
    "Bhagyanagar-style"= low-liquid / erratic or weak momentum
- Saves results to nse_momentum_output.csv
"""

import os
import sys
import time
import datetime as dt
from typing import List, Dict

import pandas as pd
import numpy as np
import yfinance as yf
from tqdm import tqdm  # progress bar [web:46]


# -----------------------------
# CONFIG
# -----------------------------

EQUITY_FILE = "EQUITY_L.csv"   # must be in current working directory
OUTPUT_FILE = "nse_momentum_output.csv"

# Lookback windows (trading days approx)
WINDOWS = {
    "ret_5d": 5,
    "ret_21d": 21,   # ~1 month
    "ret_63d": 63,   # ~3 months
    "ret_126d": 126  # ~6 months
}

# Minimum trading days required for reliable stats
MIN_VALID_DAYS = 80

# Basic liquidity filters
MIN_AVG_DAILY_VALUE = 2e7     # ~2 Cr per day (rough heuristic)
MIN_PRICE = 10.0


# -----------------------------
# UTILS
# -----------------------------

def load_equity_universe(path: str) -> pd.DataFrame:
    """
    Read EQUITY_L.csv and extract NSE equity series symbols.

    Expected columns (from NSE equity listing file):
    SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, ...
    """
    df = pd.read_csv(path)
    # Normalize column names
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]

    # Filter for equity series (usually 'EQ')
    if "SERIES" in df.columns:
        df = df[df["SERIES"].str.upper() == "EQ"]

    # Keep core columns
    keep_cols = [c for c in ["SYMBOL", "NAME_OF_COMPANY", "SERIES", "DATE_OF_LISTING"] if c in df.columns]
    df = df[keep_cols].drop_duplicates("SYMBOL")

    df["YF_TICKER"] = df["SYMBOL"].astype(str).str.strip() + ".NS"
    return df


def download_price_data(tickers: List[str],
                        start: dt.date,
                        end: dt.date) -> Dict[str, pd.DataFrame]:
    """
    Download adjusted OHLCV data for each ticker using yfinance.
    Returns dict: {ticker: DataFrame}
    """
    data = {}
    for t in tqdm(tickers, desc="Downloading price data"):
        try:
            df = yf.download(
                t,
                start=start,
                end=end,
                progress=False,
                auto_adjust=False,
            )
            if df.empty:
                continue
            df = df.reset_index()
            # Ensure standard columns
            df.rename(columns={
                "Date": "date",
                "Adj Close": "adj_close",
                "Close": "close",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Volume": "volume"
            }, inplace=True)
            data[t] = df
            time.sleep(0.02)  # small throttle to be nice to API
        except Exception as e:
            print(f"Error downloading {t}: {e}", file=sys.stderr)
            continue
    return data


def compute_returns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Given price dataframe with 'date' and 'adj_close', compute log returns
    and cumulative returns for defined WINDOWS.
    """
    df = df.sort_values("date").copy()
    df["ret_daily"] = np.log(df["adj_close"]).diff()

    # cumulative log returns over windows
    for col, win in WINDOWS.items():
        df[col] = df["ret_daily"].rolling(win).sum()

    return df


def summarize_ticker(ticker: str, price_df: pd.DataFrame) -> Dict:
    """
    Build summary stats for one ticker:
    - latest returns at each window
    - average daily traded value (close * volume)
    - volatility, momentum_score, style tag
    """

    # Make sure we have the columns we need
    if "adj_close" not in price_df.columns or "close" not in price_df.columns or "volume" not in price_df.columns:
        return {
            "YF_TICKER": ticker,
            "num_days": len(price_df),
            "latest_price": np.nan,
            "avg_daily_value": np.nan,
            "ret_5d": np.nan,
            "ret_21d": np.nan,
            "ret_63d": np.nan,
            "ret_126d": np.nan,
            "vol_21d": np.nan,
            "momentum_score": np.nan,
            "momentum_style": "Bhagyanagar-style"
        }

    # Fraction of missing adj_close values (scalar, avoids ambiguous Series) [web:38][web:41]
    na_fraction = price_df["adj_close"].isna().sum() / max(len(price_df), 1)

    if price_df.empty or na_fraction > 0.5:
        return {
            "YF_TICKER": ticker,
            "num_days": len(price_df),
            "latest_price": np.nan,
            "avg_daily_value": np.nan,
            "ret_5d": np.nan,
            "ret_21d": np.nan,
            "ret_63d": np.nan,
            "ret_126d": np.nan,
            "vol_21d": np.nan,
            "momentum_score": np.nan,
            "momentum_style": "Bhagyanagar-style"
        }

    price_df = compute_returns(price_df)

    latest = price_df.iloc[-1]
    num_days = len(price_df)

    # Liquidity: average traded value over last ~3 months
    price_df["value_traded"] = price_df["close"] * price_df["volume"]
    avg_value = price_df["value_traded"].tail(63).mean()

    # Volatility: std of daily returns over ~1 month
    vol_21d = price_df["ret_daily"].tail(WINDOWS["ret_21d"]).std()

    # Momentum score: weighted combo of multi-window returns
    score = (
        0.15 * latest.get("ret_5d", 0.0) +
        0.25 * latest.get("ret_21d", 0.0) +
        0.3  * latest.get("ret_63d", 0.0) +
        0.3  * latest.get("ret_126d", 0.0)
    )

    style = classify_momentum_style(latest, avg_value, num_days)

    return {
        "YF_TICKER": ticker,
        "num_days": num_days,
        "latest_price": float(latest["close"]),
        "avg_daily_value": float(avg_value) if not np.isnan(avg_value) else np.nan,
        "ret_5d": float(latest.get("ret_5d", np.nan)),
        "ret_21d": float(latest.get("ret_21d", np.nan)),
        "ret_63d": float(latest.get("ret_63d", np.nan)),
        "ret_126d": float(latest.get("ret_126d", np.nan)),
        "vol_21d": float(vol_21d) if not np.isnan(vol_21d) else np.nan,
        "momentum_score": float(score),
        "momentum_style": style
    }


def classify_momentum_style(latest: pd.Series,
                            avg_value: float,
                            num_days: int) -> str:
    """
    Classify each stock's momentum into three simple buckets:

    PPAP-style:
        - strong trend in all windows
        - 63d and 126d log returns > ~25-35% (converted to %)
        - decent liquidity and data history

    MTAR-style:
        - strong near-term momentum (5d or 21d) while
          medium/long-term trend positive but milder

    Bhagyanagar-style:
        - everything else (illiquid, weak trend, erratic)
    """
    if num_days < MIN_VALID_DAYS or avg_value is None or np.isnan(avg_value):
        return "Bhagyanagar-style"

    r5 = float(latest.get("ret_5d", 0.0))
    r21 = float(latest.get("ret_21d", 0.0))
    r63 = float(latest.get("ret_63d", 0.0))
    r126 = float(latest.get("ret_126d", 0.0))

    # Convert log returns to percent for intuitive thresholds
    pct5   = np.expm1(r5) * 100.0
    pct21  = np.expm1(r21) * 100.0
    pct63  = np.expm1(r63) * 100.0
    pct126 = np.expm1(r126) * 100.0

    # PPAP-style: multi-window strong trend
    if (pct63 > 25 and pct126 > 35 and
        pct21 > 10 and pct5 > 3 and
        avg_value > MIN_AVG_DAILY_VALUE):
        return "PPAP-style"

    # MTAR-style: sharp recent move with decent backdrop
    if ((pct5 > 5 or pct21 > 12) and
        pct63 > 5 and pct126 > 0 and
        avg_value > MIN_AVG_DAILY_VALUE / 2):
        return "MTAR-style"

    # Otherwise treat as low-quality / non-momentum
    return "Bhagyanagar-style"


def main():
    # Check EQUITY_L.csv exists
    if not os.path.exists(EQUITY_FILE):
        print(f"Missing {EQUITY_FILE} in current directory", file=sys.stderr)
        sys.exit(1)

    print("Loading NSE equity universe from EQUITY_L.csv...")
    universe = load_equity_universe(EQUITY_FILE)

    print(f"Universe size (EQ series): {len(universe)} symbols")

    # Build Yahoo tickers
    tickers = universe["YF_TICKER"].tolist()

    # Date range for momentum (about 1 year back)
    today = dt.date.today()
    start = today - dt.timedelta(days=365)
    end = today

    print(f"Downloading price data from {start} to {end}...")
    price_data = download_price_data(tickers, start=start, end=end)

    if not price_data:
        print("No price data downloaded. Check network / yfinance.", file=sys.stderr)
        sys.exit(1)

    print(f"Downloaded data for {len(price_data)} tickers")

    summaries = []
    # Progress bar for summarization as well
    for t in tqdm(list(price_data.keys()), desc="Summarizing tickers"):
        df_price = price_data[t]
        summary = summarize_ticker(t, df_price)
        summaries.append(summary)

    result = pd.DataFrame(summaries)

    # Merge back with original universe info
    out = universe.merge(result, on="YF_TICKER", how="left")

    # Liquidity / price filters
    out = out[
        (out["latest_price"] >= MIN_PRICE) &
        (out["avg_daily_value"] >= MIN_AVG_DAILY_VALUE) &
        (out["num_days"] >= MIN_VALID_DAYS)
    ]

    # Sort by momentum_score descending
    out = out.sort_values("momentum_score", ascending=False)

    # Write to CSV
    out.to_csv(OUTPUT_FILE, index=False)
    print(f"Saved momentum screener output to {OUTPUT_FILE}")
    print("Top 10 rows:")
    print(out.head(10))


if __name__ == "__main__":
    main()