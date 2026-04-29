"""
╔══════════════════════════════════════════════════════════════════════╗
║       NSE MOMENTUM BREAKOUT SCANNER — Full Universe Edition          ║
║       Source : nsearchives.nseindia.com/content/equities/EQUITY_L.csv
║       Strategy: MACD Multi-TF + CCI(200) + RSI + DMI + Volume       ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALL (one-time):
    pip install yfinance pandas numpy openpyxl requests

RUN:
    python momentum_breakout_scanner.py

UNIVERSE:
    • Auto-downloads ALL NSE EQ-series cash stocks live from NSE.
    • Falls back to a built-in ~200-stock list if NSE is unreachable.
    • Optionally merge your own tickers: create  my_stocks.txt
      (one NSE symbol per line, no .NS, lines starting with # are ignored).

OUTPUTS:
    • Terminal — tiered results printed with progress bar
    • Excel    — breakout_scan_YYYYMMDD_HHMM.xlsx  (4 sheets)
    • CSV      — fallback if openpyxl not installed

BEST TIME TO RUN:
    After 3:35 PM IST on any trading day (closed candles, final volume).

CONFIG — edit the block below to tune thresholds:
"""

# ─────────────────────────────────────────────────────────────────
# USER CONFIGURATION
# ─────────────────────────────────────────────────────────────────

NSE_CSV_URL     = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
CUSTOM_FILE     = "my_stocks.txt"   # optional: your own tickers, one per line

# Which NSE series to include
# "EQ"  = regular cash equities (recommended)
# "BE"  = trade-to-trade / surveillance stocks
# "BZ"  = Z-category (high risk)
SERIES_FILTER   = ["EQ"]

DATA_PERIOD     = "2y"    # yfinance history: "1y" / "2y" / "5y"
MIN_CANDLES     = 220     # skip stocks with fewer than this many trading days

BATCH_SIZE      = 25      # pause after every N stocks (avoids rate-limiting)
BATCH_PAUSE     = 1.0     # pause duration in seconds

# Scoring tiers (out of 26)
TIER1_SCORE     = 18      # Ultra momentum  → buy breakout
TIER2_SCORE     = 14      # High momentum   → buy dip / retest
TIER3_SCORE     = 10      # Watchlist       → wait for more alignment

# ─────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────

import csv
import io
import os
import sys
import time
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")


# ─────────────────────────────────────────────────────────────────
# STEP 1 — FETCH NSE UNIVERSE FROM LIVE CSV
# ─────────────────────────────────────────────────────────────────

def fetch_nse_universe():
    """
    Downloads the NSE equity master file from nsearchives.nseindia.com.

    NSE requires:
      1. A valid browser User-Agent header.
      2. A session cookie obtained by first visiting nseindia.com.
      3. The Referer header set to nseindia.com when fetching the CSV.

    Returns a list of ticker symbols filtered by SERIES_FILTER.
    Falls back to the built-in list on any network/auth error.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
    })

    try:
        # Step A: prime session — NSE sets required cookies here
        print("  Connecting to NSE and acquiring session cookie ...", end="", flush=True)
        r = session.get("https://www.nseindia.com/", timeout=15)
        r.raise_for_status()
        time.sleep(2)   # brief pause so NSE does not rate-limit us

        # Step B: download the CSV with session cookie + Referer
        session.headers["Referer"] = "https://www.nseindia.com/"
        resp = session.get(NSE_CSV_URL, timeout=20)
        resp.raise_for_status()
        print(" ✓")

        # Step C: parse CSV  (header: SYMBOL, NAME OF COMPANY, SERIES, ...)
        reader  = csv.DictReader(io.StringIO(resp.text))
        tickers = []
        total_rows = 0
        for row in reader:
            total_rows += 1
            # NSE CSV has a leading space before SERIES column name
            series = row.get(" SERIES", row.get("SERIES", "")).strip()
            symbol = row.get("SYMBOL", "").strip()
            if symbol and series in SERIES_FILTER:
                tickers.append(symbol)

        series_str = ", ".join(SERIES_FILTER)
        print(f"  NSE universe: {total_rows} listed stocks → "
              f"{len(tickers)} in series [{series_str}]")
        return tickers

    except requests.exceptions.ConnectionError:
        print("\n  [!] No internet / NSE unreachable — using built-in list.")
    except requests.exceptions.HTTPError as e:
        print(f"\n  [!] NSE returned HTTP {e.response.status_code} — using built-in list.")
    except Exception as e:
        print(f"\n  [!] NSE fetch error: {e} — using built-in list.")

    return []


# ─────────────────────────────────────────────────────────────────
# FALLBACK BUILT-IN TICKER LIST
# Used only when NSE CSV is unreachable.
# ─────────────────────────────────────────────────────────────────

BUILTIN = [
    # Index heavyweights
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BAJFINANCE","BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI",
    "SUNPHARMA","TITAN","WIPRO","ULTRACEMCO","NTPC","POWERGRID","ONGC","JSWSTEEL",
    "TATASTEEL","COALINDIA","TECHM","HCLTECH","DRREDDY","CIPLA","DIVISLAB",
    # Adani group
    "ADANIENT","ADANIGREEN","ADANIPORTS","ADANIPOWER","ADANITRANS","ADANIENSOL","ATGL","AWL",
    # Energy & power
    "GAIL","IOC","BPCL","HINDPETRO","TATAPOWER","TORNTPOWER","CESC","JSWENERGY",
    "NHPC","SJVN","IREDA","PFC","RECLTD","GREENPWR",
    # Metals
    "HINDALCO","VEDL","HINDZINC","NATIONALUM","NMDC","APLAPOLLO","JINDALSTEL","JSL","SAIL",
    # Banking & finance
    "BANKBARODA","PNB","CANBK","UNIONBANK","IDFCFIRSTB","FEDERALBNK","RBLBANK",
    "BANDHANBNK","INDUSINDBK","MUTHOOTFIN","BAJAJFINSV","CHOLAFIN","SBICARD",
    "HDFCLIFE","ICICIGI","SBILIFE","LICI","HDFCAMC","NIPPONLIFE","ABSLAMC",
    # Auto
    "TATAMOTORS","M&M","BAJAJ-AUTO","HEROMOTOCO","EICHERMOT","MOTHERSON",
    "BOSCHLTD","MRF","APOLLOTYRE","TIINDIA",
    # Infra & capital goods
    "SIEMENS","ABB","BHEL","HAVELLS","VOLTAS","POLYCAB","RVNL","IRFC","IRCON",
    "COCHINSHIP","BEL","HAL","BEML","GRSE","DATAPATTNS","KEC","KALPATPOWR",
    # Pharma & healthcare
    "AUROPHARMA","LUPIN","TORNTPHARM","ALKEM","IPCALAB","GLENMARK","NATCOPHARM",
    "LALPATHLAB","METROPOLIS","APOLLOHOSP","FORTIS","MAXHEALTH",
    # Consumer
    "TATACONSUM","NESTLEIND","BRITANNIA","DABUR","GODREJCP","EMAMILTD","COLPAL",
    "DMART","TRENT","PAGEIND",
    # Chemicals
    "DEEPAKNTR","PIIND","ATUL","CLEAN","SRF","TATACHEM","VINATI","NOCIL","NAVINFLUOR",
    # IT / Tech
    "PERSISTENT","COFORGE","LTIM","MPHASIS","NAUKRI","INDIAMART","AFFLE",
    "DIXON","AMBER","TATAELXSI",
    # Real estate
    "DLF","GODREJPROP","PHOENIXLTD","PRESTIGE","BRIGADE","SOBHA","OBEROIRLTY",
    # Defence / railways
    "HAL","BEL","BEML","GRSE","COCHINSHIP","RVNL","IRFC","IRCON","RAILTEL","TITAGARH",
    # Others
    "ZOMATO","ETERNAL","IRCTC","PIDILITIND","BERGEPAINT","ASTRAL","KALYANKJIL",
    "SENCO","JUBLFOOD","MCDOWELL-N","RADICO","ARE&M","EXIDEIND","CUMMINSIND",
    "ANGELONE","BSE","CDSL","CAMS","EDELWEISS","MOFSL",
    "GPIL","GRAPHITE","GRINDWELL","ELGIEQUIP","APARINDS",
]


# ─────────────────────────────────────────────────────────────────
# STEP 2 — TECHNICAL INDICATOR LIBRARY
# ─────────────────────────────────────────────────────────────────

def rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(com=period - 1, min_periods=period).mean()
    al    = loss.ewm(com=period - 1, min_periods=period).mean()
    return 100 - (100 / (1 + ag / (al + 1e-10)))


def macd(close, fast, slow, sig=9):
    line   = close.ewm(span=fast, adjust=False).mean() - \
             close.ewm(span=slow, adjust=False).mean()
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal


def cci(high, low, close, period):
    tp  = (high + low + close) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma) / (0.015 * mad + 1e-10)


def dmi(high, low, close, period=14):
    up  = high.diff()
    dn  = -low.diff()
    pdm = up.where((up > 0) & (up > dn), 0.0)
    ndm = dn.where((dn > 0) & (dn > up), 0.0)
    tr  = pd.concat([
              high - low,
              (high - close.shift()).abs(),
              (low  - close.shift()).abs()
          ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / period, adjust=False).mean() / (atr + 1e-10)
    ndi = 100 * ndm.ewm(alpha=1 / period, adjust=False).mean() / (atr + 1e-10)
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-10)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return pdi, ndi, adx


def mfi(high, low, close, volume, period=14):
    tp  = (high + low + close) / 3
    mf  = tp * volume
    pmf = mf.where(tp > tp.shift(1), 0.0)
    nmf = mf.where(tp < tp.shift(1), 0.0)
    r   = pmf.rolling(period).sum() / (nmf.rolling(period).sum() + 1e-10)
    return 100 - (100 / (1 + r))


def cmf(high, low, close, volume, period=20):
    mfm = ((close - low) - (high - close)) / (high - low + 1e-10)
    return (mfm * volume).rolling(period).sum() / (volume.rolling(period).sum() + 1e-10)


def stoch_rsi(rsi_series, period=14, k=3, d=3):
    lo    = rsi_series.rolling(period).min()
    hi    = rsi_series.rolling(period).max()
    stoch = (rsi_series - lo) / (hi - lo + 1e-10) * 100
    k_    = stoch.rolling(k).mean()
    return k_, k_.rolling(d).mean()


# ─────────────────────────────────────────────────────────────────
# STEP 3 — PER-STOCK SCAN
# ─────────────────────────────────────────────────────────────────

def scan_stock(ticker):
    """
    Downloads 2-year daily OHLCV data for ticker.NS,
    computes all indicators, runs the 26-point scoring engine,
    and returns a result dict (or None if data insufficient).
    """
    try:
        df = yf.download(
            ticker + ".NS",
            period=DATA_PERIOD,
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if df.empty or len(df) < MIN_CANDLES:
            return None

        c = df["Close"].squeeze()
        h = df["High"].squeeze()
        l = df["Low"].squeeze()
        v = df["Volume"].squeeze()

        # ── Indicators ──────────────────────────────────────────
        rsi14      = rsi(c, 14)
        rsi_sma    = rsi14.rolling(14).mean()

        m1226, _, h1226 = macd(c, 12,  26, 9)   # short-term entry
        m1200, _, _     = macd(c, 12, 200, 9)   # medium-term trend
        m3420, _, _     = macd(c, 34, 200, 9)   # medium-long trend
        m8920, _, _     = macd(c, 89, 200, 9)   # long-term trail / exit

        cci20   = cci(h, l, c, 20)
        cci200  = cci(h, l, c, 200)

        di_plus, di_minus, adx14 = dmi(h, l, c, 14)

        mfi14   = mfi(h, l, c, v, 14)
        cmf20   = cmf(h, l, c, v, 20)
        sk, _   = stoch_rsi(rsi14, 14, 3, 3)

        vol_avg20  = v.rolling(20).mean()
        high_52w   = c.rolling(252).max()
        from_52w   = (c / high_52w - 1) * 100

        # ── Extract latest floats safely ────────────────────────
        def f(s, i=-1):
            val = s.iloc[i]
            return float(val) if not (isinstance(val, float) and np.isnan(val)) else 0.0

        r_rsi      = f(rsi14)
        r_rsisma   = f(rsi_sma)
        r_m1226    = f(m1226)
        r_h1226    = f(h1226)
        r_m1200    = f(m1200)
        r_m3420    = f(m3420)
        r_m8920    = f(m8920)
        r_cci20    = f(cci20)
        r_cci200   = f(cci200)
        r_cci200p  = f(cci200, -2)     # yesterday's CCI(200) — for crossover detection
        r_pdi      = f(di_plus)
        r_ndi      = f(di_minus)
        r_adx      = f(adx14)
        r_mfi      = f(mfi14)
        r_cmf      = f(cmf20)
        r_stochk   = f(sk)
        r_volrat   = f(v) / (f(vol_avg20) + 1)
        r_52wdist  = f(from_52w)
        r_close    = f(c)
        r_52whi    = f(high_52w)

        # ── 26-point Scoring Engine ─────────────────────────────
        score   = 0
        signals = []

        # 1. RSI Momentum  (max 5 pts)
        if   r_rsi > 70: score += 4; signals.append("RSI>70 🔥")
        elif r_rsi > 60: score += 3; signals.append("RSI>60 ✅")
        elif r_rsi > 55: score += 2; signals.append("RSI>55 ✅")
        if r_rsi > r_rsisma:
            score += 1; signals.append("RSI↑SMA")

        # 2. MACD Alignment  (max 7 pts)
        if r_m1226 > 0:
            score += 2; signals.append("MACD(12,26)>0 ✅")
        if r_h1226 > 0 and r_m1226 > 0:
            score += 1; signals.append("MACD-Hist+")
        if r_m1200 > 0:
            score += 2; signals.append("MACD(12,200)>0 ✅")
        if r_m3420 > 0:
            score += 1; signals.append("MACD(34,200)>0")
        if r_m8920 > 0:
            score += 1; signals.append("MACD(89,200)>0")   # trail signal

        # 3. CCI Signals  (max 6 pts) — MOST IMPORTANT
        if r_cci200 > 100:
            score += 3; signals.append("CCI(200)>100 🔥")
        elif r_cci200 > 0:
            score += 1; signals.append("CCI(200)>0")
        if r_cci200p < 100 <= r_cci200:
            score += 2; signals.append("CCI(200) CROSS↑100 🚀")   # fresh crossover
        if r_cci20 > 100:
            score += 1; signals.append("CCI(20)>100")

        # 4. DMI / ADX Trend  (max 3 pts)
        if r_pdi > r_ndi:
            score += 1; signals.append("DI+>DI−")
        if   r_adx > 30: score += 2; signals.append("ADX>30 💪")
        elif r_adx > 25: score += 1; signals.append("ADX>25")

        # 5. Money Flow  (max 3 pts)
        if   r_mfi > 70: score += 2; signals.append("MFI>70 💰")
        elif r_mfi > 60: score += 1; signals.append("MFI>60")
        if r_cmf > 0.05:
            score += 1; signals.append("CMF>0.05")

        # 6. Volume Confirmation  (max 3 pts)
        if   r_volrat > 2.5: score += 3; signals.append(f"Vol {r_volrat:.1f}x 🔥")
        elif r_volrat > 1.5: score += 2; signals.append(f"Vol {r_volrat:.1f}x ✅")
        elif r_volrat > 1.2: score += 1; signals.append(f"Vol {r_volrat:.1f}x")

        # 7. 52-Week High Proximity  (max 3 pts)
        if   r_52wdist >= -2:  score += 3; signals.append("AT 52W HIGH 🏆")
        elif r_52wdist >= -5:  score += 2; signals.append("Near 52W High ✅")
        elif r_52wdist >= -10: score += 1; signals.append("<10% from 52W")

        # 8. StochRSI  (max 1 pt)
        if r_stochk > 80:
            score += 1; signals.append("StochRSI>80 ↑")

        return {
            "Ticker":        ticker,
            "Close":         round(r_close,   2),
            "52W_High":      round(r_52whi,   2),
            "From_52W_%":    round(r_52wdist, 1),
            "Score":         score,
            "Max":           26,
            "RSI_14":        round(r_rsi,     1),
            "RSI_SMA14":     round(r_rsisma,  1),
            "MACD_12_26":    round(r_m1226,   3),
            "MACD_12_200":   round(r_m1200,   3),
            "MACD_89_200":   round(r_m8920,   3),
            "CCI_20":        round(r_cci20,   1),
            "CCI_200":       round(r_cci200,  1),
            "DI_Plus":       round(r_pdi,     1),
            "DI_Minus":      round(r_ndi,     1),
            "ADX":           round(r_adx,     1),
            "MFI_14":        round(r_mfi,     1),
            "CMF_20":        round(r_cmf,     3),
            "StochRSI_K":    round(r_stochk,  1),
            "Vol_Ratio_20D": round(r_volrat,  2),
            "Signals":       " | ".join(signals),
        }

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    os.system("cls" if os.name == "nt" else "clear")

    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║   🚀  NSE MOMENTUM BREAKOUT SCANNER — Full Universe Edition      ║")
    print("║       MACD Multi-TF | CCI(200) | RSI | DMI | Volume Spike        ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print(f"   Run at : {datetime.now().strftime('%d %b %Y  %H:%M:%S IST')}\n")

    # ────────────────────────────────────────────────────────────
    # STEP 1 — Build universe
    # ────────────────────────────────────────────────────────────
    print("▶ STEP 1/3  Build stock universe")
    print("  ─────────────────────────────────────────────────────")
    tickers = fetch_nse_universe()

    if not tickers:
        tickers = list(dict.fromkeys(BUILTIN))
        print(f"  Built-in fallback list loaded: {len(tickers)} stocks")

    # Merge user's custom file
    if os.path.exists(CUSTOM_FILE):
        with open(CUSTOM_FILE) as fh:
            custom = [
                ln.strip().upper()
                for ln in fh
                if ln.strip() and not ln.startswith("#")
            ]
        before   = len(tickers)
        tickers  = list(dict.fromkeys(tickers + custom))
        added    = len(tickers) - before
        print(f"  Merged '{CUSTOM_FILE}': +{added} unique tickers  →  total {len(tickers)}")
    else:
        print(f"  Tip: create '{CUSTOM_FILE}' to add your own tickers.")

    total = len(tickers)
    print(f"\n  Universe ready: {total} stocks\n")

    # ────────────────────────────────────────────────────────────
    # STEP 2 — Scan all stocks
    # ────────────────────────────────────────────────────────────
    print("▶ STEP 2/3  Downloading data & computing indicators")
    print("  ─────────────────────────────────────────────────────")
    print(f"  Each stock: 2yr daily OHLCV → RSI, 4×MACD, 2×CCI, DMI, MFI, CMF, StochRSI, Vol\n")

    results = []
    errors  = 0
    t0      = time.time()

    for i, ticker in enumerate(tickers, 1):
        pct  = i / total * 100
        fill = int(pct / 2)
        bar  = "█" * fill + "░" * (50 - fill)
        sys.stdout.write(
            f"\r  [{bar}] {pct:5.1f}%  {i:>4}/{total}  {ticker:<14}  "
            f"hits={len(results)}  err={errors}"
        )
        sys.stdout.flush()

        res = scan_stock(ticker)
        if res:
            results.append(res)
        else:
            errors += 1

        if i % BATCH_SIZE == 0:
            time.sleep(BATCH_PAUSE)

    elapsed = time.time() - t0
    sys.stdout.write("\n")
    print(f"\n  ✓ Done — {len(results)} scanned  |  {errors} failed/insufficient data"
          f"  |  {elapsed:.0f}s total\n")

    if not results:
        print("  ❌ No results. Check internet connection and try again.")
        sys.exit(1)

    df = pd.DataFrame(results).sort_values("Score", ascending=False)

    # ────────────────────────────────────────────────────────────
    # STEP 3 — Print tiered results
    # ────────────────────────────────────────────────────────────
    print("▶ STEP 3/3  Results")
    print("  ─────────────────────────────────────────────────────\n")

    DISPLAY_COLS = [
        "Ticker", "Close", "From_52W_%", "Score",
        "RSI_14", "MACD_12_26", "MACD_12_200",
        "CCI_200", "ADX", "MFI_14", "Vol_Ratio_20D",
    ]

    SEP  = "═" * 110
    sep2 = "─" * 110

    tier1 = df[df["Score"] >= TIER1_SCORE]
    tier2 = df[(df["Score"] >= TIER2_SCORE) & (df["Score"] < TIER1_SCORE)]
    tier3 = df[(df["Score"] >= TIER3_SCORE) & (df["Score"] < TIER2_SCORE)]

    def print_tier(title, emoji, hint, data, show_sigs):
        print(SEP)
        print(f"  {emoji}  {title}  [ {len(data)} stocks found ]")
        print(f"     {hint}")
        print(SEP)
        if data.empty:
            print("  (none today — market may be consolidating or in drawdown)\n")
            return
        print(data[DISPLAY_COLS].to_string(index=False))
        if show_sigs and not data.empty:
            print(sep2)
            for _, row in data.iterrows():
                print(f"  {row['Ticker']:<15}  {row['Signals']}")
        print()

    print_tier(
        f"TIER 1 — ULTRA MOMENTUM  (Score ≥ {TIER1_SCORE} / 26)",
        "🔥",
        "All indicators aligned → BUY breakout, tight SL below prior swing low",
        tier1,
        show_sigs=True,
    )
    print_tier(
        f"TIER 2 — HIGH MOMENTUM  (Score {TIER2_SCORE}–{TIER1_SCORE - 1} / 26)",
        "⚡",
        "Strong setup, 1–2 criteria still building → BUY dip or breakout retest",
        tier2,
        show_sigs=True,
    )
    print_tier(
        f"TIER 3 — WATCHLIST  (Score {TIER3_SCORE}–{TIER2_SCORE - 1} / 26)",
        "👀",
        "Momentum building → Add to watchlist, wait for more indicators to fire",
        tier3.head(40),
        show_sigs=False,
    )

    # ────────────────────────────────────────────────────────────
    # Save Excel + CSV
    # ────────────────────────────────────────────────────────────
    ts        = datetime.now().strftime("%Y%m%d_%H%M")
    xlsx_path = f"breakout_scan_{ts}.xlsx"
    csv_path  = f"breakout_scan_{ts}.csv"

    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            tier1.to_excel(writer, sheet_name=f"TIER1_Score{TIER1_SCORE}+",             index=False)
            tier2.to_excel(writer, sheet_name=f"TIER2_Score{TIER2_SCORE}-{TIER1_SCORE-1}", index=False)
            tier3.to_excel(writer, sheet_name=f"TIER3_Score{TIER3_SCORE}-{TIER2_SCORE-1}", index=False)
            df.to_excel(   writer, sheet_name="All_Results",                              index=False)
        print(f"  💾 Excel saved : {xlsx_path}  (4 sheets: Tier1 / Tier2 / Tier3 / All)")
    except ImportError:
        df.to_csv(csv_path, index=False)
        print(f"  💾 CSV saved   : {csv_path}  (install openpyxl for Excel output)")

    # ────────────────────────────────────────────────────────────
    # Strategy summary reminder
    # ────────────────────────────────────────────────────────────
    print()
    print(SEP)
    print("  📌  STRATEGY REFERENCE")
    print(sep2)
    print("  ENTRY  : CCI(200) crosses above 100")
    print("           + MACD(12,200) > 0          ← institutional trend confirmed")
    print("           + RSI(14) > 55              ← real momentum, not a bounce")
    print("           + Volume spike > 1.5×       ← institutional participation")
    print("           → This is the ADANI ENERGY pattern → 30–60% in 1–2 weeks")
    print()
    print("  TRAIL  : Hold position as long as MACD(89,200) stays above zero (daily)")
    print("  EXIT   : MACD(89,200) crosses BELOW zero → long-term trend has ended")
    print()
    print("  SCORE  : ≥ 18 → Buy breakout  |  14–17 → Buy dip/retest  |  10–13 → Watch")
    print(SEP)
    print()


if __name__ == "__main__":
    main()
