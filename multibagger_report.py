#!/usr/bin/env python3
import os
import io
import sys
import base64
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec

warnings.filterwarnings('ignore')

# ── Constants ─────────────────────────────────────────────────────────────────
REPORT_HTML   = "multibagger_report.html"
LOOKBACK_YEARS = 10
IST_OFFSET     = "+05:30"

WATCHLIST = {
    "NCC":              "NCC.NS",
    "Mankind Pharma":   "MANKIND.NS",
    "Dixon Technologies":"DIXON.NS",
    "Polycab India":    "POLYCAB.NS",
    "Astral":           "ASTRAL.NS",
    "Tube Investments": "TIINDIA.NS",
}

# Strategy thresholds
M_RSI_STRONG  = 70     # Monthly RSI above this = strong trend
W_RSI_BULL    = 55     # Weekly RSI above this = bullish
D_RSI_ENTRY   = 60     # Daily RSI above this = fresh entry zone
VOL_SURGE     = 1.5    # Volume ratio above this = volume breakout
ADX_TREND     = 25     # ADX above this = trending market

# MACD configs
MACD_FAST, MACD_SLOW, MACD_SIG      = 12, 26, 9       # Standard
MACD_SF, MACD_SS, MACD_SSIG         = 34, 1000, 20    # Ultra-slow (mega-trend)

MPLSTYLE = {
    'axes.facecolor':   '#0d1117',
    'figure.facecolor': '#0d1117',
    'axes.edgecolor':   '#30363d',
    'axes.labelcolor':  '#8b949e',
    'xtick.color':      '#8b949e',
    'ytick.color':      '#8b949e',
    'grid.color':       '#21262d',
    'text.color':       '#e6edf3',
    'grid.linestyle':   '--',
    'grid.linewidth':   0.5,
}

# ── Helper: RSI ───────────────────────────────────────────────────────────────
def rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

# ── Helper: ATR ───────────────────────────────────────────────────────────────
def atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(com=period-1, min_periods=period).mean()

# ── Helper: ADX ───────────────────────────────────────────────────────────────
def adx(high, low, close, period=14):
    tr  = atr(high, low, close, period)
    up  = high.diff().clip(lower=0)
    dn  = (-low.diff()).clip(lower=0)
    dm_pos = np.where((up > dn) & (up > 0), up, 0.0)
    dm_neg = np.where((dn > up) & (dn > 0), dn, 0.0)
    dm_pos = pd.Series(dm_pos, index=high.index).ewm(com=period-1, min_periods=period).mean()
    dm_neg = pd.Series(dm_neg, index=high.index).ewm(com=period-1, min_periods=period).mean()
    di_pos = 100 * dm_pos / tr.replace(0, np.nan)
    di_neg = 100 * dm_neg / tr.replace(0, np.nan)
    dx     = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).replace(0, np.nan)
    return dx.ewm(com=period-1, min_periods=period).mean()

# ── Helper: Stochastic ────────────────────────────────────────────────────────
def stochastic(high, low, close, k=14, d=3):
    lo_k  = low.rolling(k).min()
    hi_k  = high.rolling(k).max()
    stoch_k = 100 * (close - lo_k) / (hi_k - lo_k).replace(0, np.nan)
    stoch_d = stoch_k.rolling(d).mean()
    return stoch_k, stoch_d

# ── Helper: CCI ───────────────────────────────────────────────────────────────
def cci(high, low, close, period=20):
    tp  = (high + low + close) / 3
    ma  = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - ma) / (0.015 * mad.replace(0, np.nan))

# ── Helper: OBV ───────────────────────────────────────────────────────────────
def obv(close, volume):
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()

# ── Helper: Bollinger Bands ───────────────────────────────────────────────────
def bollinger(close, period=20, std_dev=2):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid - std_dev*std, mid, mid + std_dev*std

# ── Helper: MFI ───────────────────────────────────────────────────────────────
def mfi(high, low, close, volume, period=14):
    tp  = (high + low + close) / 3
    mf  = tp * volume
    pos = mf.where(tp > tp.shift(), 0)
    neg = mf.where(tp < tp.shift(), 0)
    mfr = pos.rolling(period).sum() / neg.rolling(period).sum().replace(0, np.nan)
    return 100 - (100 / (1 + mfr))

# ── Helper: Williams %R ───────────────────────────────────────────────────────
def willr(high, low, close, period=14):
    hi = high.rolling(period).max()
    lo = low.rolling(period).min()
    return -100 * (hi - close) / (hi - lo).replace(0, np.nan)

# ── Helper: MACD ──────────────────────────────────────────────────────────────
def macd(close, fast=12, slow=26, signal=9):
    ema_f  = close.ewm(span=fast,   adjust=False).mean()
    ema_s  = close.ewm(span=slow,   adjust=False).mean()
    line   = ema_f - ema_s
    sig    = line.ewm(span=signal,  adjust=False).mean()
    return line, sig, line - sig

# ── Helper: Parabolic SAR (simplified) ───────────────────────────────────────
def psar(high, low, iaf=0.02, maxaf=0.2):
    length = len(high)
    dates  = high.index
    sar    = pd.Series(np.nan, index=dates)
    bull   = True
    af     = iaf
    ep     = low.iloc[0]
    hp     = high.iloc[0]
    lp     = low.iloc[0]
    sar.iloc[0] = lp
    for i in range(1, length):
        if bull:
            sar.iloc[i] = sar.iloc[i-1] + af * (hp - sar.iloc[i-1])
            sar.iloc[i] = min(sar.iloc[i], low.iloc[i-1], low.iloc[max(0,i-2)])
            if low.iloc[i] < sar.iloc[i]:
                bull = False; af = iaf; ep = low.iloc[i]; sar.iloc[i] = hp
            else:
                if high.iloc[i] > hp:
                    hp = high.iloc[i]; af = min(af+iaf, maxaf)
        else:
            sar.iloc[i] = sar.iloc[i-1] + af * (ep - sar.iloc[i-1])
            sar.iloc[i] = max(sar.iloc[i], high.iloc[i-1], high.iloc[max(0,i-2)])
            if high.iloc[i] > sar.iloc[i]:
                bull = True; af = iaf; ep = high.iloc[i]; sar.iloc[i] = lp; hp = high.iloc[i]
            else:
                if low.iloc[i] < ep:
                    ep = low.iloc[i]; af = min(af+iaf, maxaf)
            lp = low.iloc[i] if not bull else lp
    return sar

# ── Data Fetching ─────────────────────────────────────────────────────────────
def fetch_data(ticker, years=LOOKBACK_YEARS):
    import yfinance as yf
    end   = datetime.today()
    start = end - timedelta(days=years*365 + 30)
    try:
        df = yf.download(ticker, start=start.strftime('%Y-%m-%d'),
                         end=end.strftime('%Y-%m-%d'),
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 100:
            return None
        df.index = pd.to_datetime(df.index)
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open','High','Low','Close','Volume']].copy()
        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"  ⚠️  {ticker}: {e}")
        return None

def resample_weekly(df):
    return df.resample('W').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()

def resample_monthly(df):
    return df.resample('ME').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()

# ── Indicator Engine ──────────────────────────────────────────────────────────
def add_indicators(df):
    h, l, c, v = df['High'], df['Low'], df['Close'], df['Volume']

    # EMAs / SMAs
    for p in [9, 21, 50, 200]:
        df[f'EMA{p}'] = c.ewm(span=p, adjust=False).mean()
    for p in [20, 50, 200]:
        df[f'SMA{p}'] = c.rolling(p).mean()

    # Momentum
    df['RSI']      = rsi(c, 14)
    df['RSI9']     = rsi(c, 9)
    df['MOM10']    = c - c.shift(10)
    df['ROC20']    = ((c - c.shift(20)) / c.shift(20)) * 100
    df['CCI']      = cci(h, l, c, 20)
    df['WILLR']    = willr(h, l, c, 14)
    df['MFI']      = mfi(h, l, c, v, 14)

    # MACD standard
    df['MACD'], df['MACD_sig'], df['MACD_hist'] = macd(c, MACD_FAST, MACD_SLOW, MACD_SIG)

    # Ultra-slow MACD (34, 1000, 20) — catches mega-trends
    df['MACD_US'], df['MACD_US_sig'], df['MACD_US_hist'] = macd(c, MACD_SF, MACD_SS, MACD_SSIG)

    # Trend strength
    df['ADX']      = adx(h, l, c, 14)
    df['ATR']      = atr(h, l, c, 14)

    # Oscillators
    df['STOCH_K'], df['STOCH_D'] = stochastic(h, l, c)

    # Bands
    df['BB_lo'], df['BB_mid'], df['BB_hi'] = bollinger(c, 20, 2)

    # Volume
    df['OBV']       = obv(c, v)
    df['VOL_MA20']  = v.rolling(20).mean()
    df['VOL_RATIO'] = v / df['VOL_MA20'].replace(0, np.nan)

    # ATH tracking
    df['ATH']       = c.expanding().max()
    prev_ath        = c.shift(1).expanding().max()
    df['ATH_PCT']   = ((c / prev_ath.replace(0, np.nan)) - 1) * 100
    df['ATH_BREAK'] = c > prev_ath

    # 52-week high/low
    df['HIGH52W']   = h.rolling(252).max()
    df['LOW52W']    = l.rolling(252).min()

    # SAR
    try:
        df['SAR'] = psar(h, l)
    except Exception:
        df['SAR'] = np.nan

    return df

# ── Signal Generation ─────────────────────────────────────────────────────────
def generate_signals(df_d, df_w, df_m):
    """Generate MTF buy/sell signals on daily data, confirmed by weekly/monthly."""
    sig = pd.Series('HOLD', index=df_d.index)

    # Align weekly/monthly RSI onto daily index
    w_rsi_daily = df_w['RSI'].reindex(df_d.index, method='ffill')
    m_rsi_daily = df_m['RSI'].reindex(df_d.index, method='ffill')
    w_ema50     = df_w['EMA50'].reindex(df_d.index, method='ffill') if 'EMA50' in df_w else None

    c = df_d['Close']
    r = df_d['RSI']
    v = df_d['VOL_RATIO']
    macd_us = df_d['MACD_US']
    macd_std = df_d['MACD']
    ath_brk = df_d['ATH_BREAK']

    # ── Signal 1: ATH Breakout + Monthly RSI > M_RSI_STRONG ─────────────────
    s1 = ath_brk & (m_rsi_daily > M_RSI_STRONG)
    sig[s1] = 'STRONG BUY'

    # ── Signal 2: Ultra-slow MACD crosses above zero (mega-trend shift) ──────
    us_cross_up = (macd_us > 0) & (macd_us.shift(1) <= 0)
    s2 = us_cross_up & (m_rsi_daily > 60) & (w_rsi_daily > W_RSI_BULL)
    sig[s2 & (sig != 'STRONG BUY')] = 'MACD MEGA BUY'

    # ── Signal 3: Fresh momentum entry ───────────────────────────────────────
    rsi_cross_60 = (r > D_RSI_ENTRY) & (r.shift(1) <= D_RSI_ENTRY)
    s3 = rsi_cross_60 & (w_rsi_daily > W_RSI_BULL) & (m_rsi_daily > 55) & (v > VOL_SURGE)
    sig[s3 & (sig == 'HOLD')] = 'BUY'

    # ── Signal 4: Volume breakout + price above all MAs ──────────────────────
    above_emas = (c > df_d['EMA21']) & (c > df_d['EMA50'])
    vol_break  = v > 2.5
    s4 = vol_break & above_emas & (r > 55) & (m_rsi_daily > 55)
    sig[s4 & (sig == 'HOLD')] = 'VOL BUY'

    # ── Signal 5: Monthly RSI fresh cross above 70 ───────────────────────────
    m_rsi_cross70 = (m_rsi_daily > 70) & (m_rsi_daily.shift(1) <= 70)
    s5 = m_rsi_cross70 & (c > df_d['EMA50'])
    sig[s5 & (sig == 'HOLD')] = 'M-RSI BUY'

    # ── Sell signals ──────────────────────────────────────────────────────────
    rsi_cross_50 = (r < 50) & (r.shift(1) >= 50)
    below_sma20  = (c < df_d['SMA20']) & (c.shift(1) >= df_d['SMA20'].shift(1))
    sell_cond    = rsi_cross_50 & below_sma20
    sig[sell_cond & ~sig.isin(['STRONG BUY','MACD MEGA BUY','BUY','VOL BUY','M-RSI BUY'])] = 'SELL'
    # Override: always mark SELL even if was BUY (sell overrides buy/hold but not strong buy)
    rsi_cross_50_strict = (r < 45) & (r.shift(1) >= 45)
    sig[rsi_cross_50_strict & sig.isin(['BUY','VOL BUY','HOLD'])] = 'SELL'

    return sig

# ── Backtesting ───────────────────────────────────────────────────────────────
def backtest(df_d, signals):
    trades   = []
    position = None

    for date, sig in signals.items():
        price = df_d.loc[date, 'Close']
        if sig in ('STRONG BUY', 'MACD MEGA BUY', 'BUY', 'VOL BUY', 'M-RSI BUY') and position is None:
            position = {'entry_date': date, 'entry_price': price, 'signal': sig}
        elif sig == 'SELL' and position is not None:
            ret = (price - position['entry_price']) / position['entry_price'] * 100
            trades.append({**position, 'exit_date': date, 'exit_price': price, 'return_pct': ret,
                           'days_held': (date - position['entry_date']).days})
            position = None

    if position:
        last_price = df_d['Close'].iloc[-1]
        last_date  = df_d.index[-1]
        ret = (last_price - position['entry_price']) / position['entry_price'] * 100
        trades.append({**position, 'exit_date': last_date, 'exit_price': last_price,
                       'return_pct': ret, 'days_held': (last_date - position['entry_date']).days,
                       'open': True})

    return trades

# ── Fibonacci Extensions ──────────────────────────────────────────────────────
def fibonacci_targets(df_d, lookback=252):
    """Compute Fibonacci extension targets from recent swing low to current price."""
    recent = df_d.tail(lookback)
    swing_low  = recent['Low'].min()
    swing_high = recent['High'].max()
    current    = df_d['Close'].iloc[-1]

    # Extension from swing low, using swing range as base
    rng = swing_high - swing_low
    base = current  # project from current price upward

    targets = {
        'swing_low':  round(swing_low, 2),
        'swing_high': round(swing_high, 2),
        'current':    round(current, 2),
        'fib_0618':   round(base + 0.618 * rng, 2),
        'fib_1000':   round(base + 1.000 * rng, 2),
        'fib_1618':   round(base + 1.618 * rng, 2),
        'fib_2618':   round(base + 2.618 * rng, 2),
        'fib_4236':   round(base + 4.236 * rng, 2),
    }
    return targets

# ── Score ─────────────────────────────────────────────────────────────────────
def compute_score(df_d, df_w, df_m, signals):
    score = 0
    c = df_d['Close'].iloc[-1]

    # Monthly RSI
    m_rsi = df_m['RSI'].iloc[-1] if len(df_m) > 0 else 50
    if m_rsi > 70:   score += 25
    elif m_rsi > 60: score += 15
    elif m_rsi > 50: score += 5

    # ATH Breakout
    if df_d['ATH_PCT'].iloc[-1] >= 0:
        score += 25

    # Ultra-slow MACD
    us = df_d['MACD_US'].iloc[-1]
    us_prev = df_d['MACD_US'].iloc[-5] if len(df_d) > 5 else us
    if us >= 0:               score += 15
    elif us > -0.02 * c:     score += 8
    if us > us_prev:          score += 5  # rising

    # Weekly RSI
    w_rsi = df_w['RSI'].iloc[-1] if len(df_w) > 0 else 50
    if w_rsi > 60: score += 10
    elif w_rsi > 50: score += 5

    # Daily RSI
    d_rsi = df_d['RSI'].iloc[-1]
    if 60 <= d_rsi <= 75: score += 8
    elif d_rsi > 75:      score += 4  # might be overbought

    # Volume
    vol_r = df_d['VOL_RATIO'].iloc[-1]
    if vol_r > 2.5: score += 7
    elif vol_r > 1.5: score += 4

    # ADX
    adx_v = df_d['ADX'].iloc[-1]
    if adx_v > 30: score += 5

    # Recent signal
    recent_sigs = signals.tail(20)
    if 'STRONG BUY' in recent_sigs.values: score += 10
    elif 'MACD MEGA BUY' in recent_sigs.values: score += 8

    return min(score, 100)

# ── Chart Generation ──────────────────────────────────────────────────────────
def generate_chart(name, df_d, df_w, df_m, signals, fibs, trades):
    with plt.rc_context(MPLSTYLE):
        fig = plt.figure(figsize=(16, 18), facecolor='#0d1117')
        gs  = GridSpec(5, 1, figure=fig, hspace=0.06,
                       height_ratios=[3.5, 1, 1, 1, 1])

        ax_price = fig.add_subplot(gs[0])
        ax_vol   = fig.add_subplot(gs[1], sharex=ax_price)
        ax_rsi   = fig.add_subplot(gs[2], sharex=ax_price)
        ax_macd  = fig.add_subplot(gs[3], sharex=ax_price)
        ax_us    = fig.add_subplot(gs[4], sharex=ax_price)

        # Use last 3 years for chart clarity
        df = df_d.tail(252*3).copy()
        sig_plot = signals.reindex(df.index)
        x = df.index

        # ── Panel 1: Price + EMAs + Signals + Fib ────────────────────────────
        ax_price.plot(x, df['Close'],  color='#e6edf3', lw=1.4, label='Close', zorder=3)
        ax_price.plot(x, df['EMA21'],  color='#f0b429', lw=0.9, alpha=0.8, label='EMA21')
        ax_price.plot(x, df['EMA50'],  color='#58a6ff', lw=0.9, alpha=0.8, label='EMA50')
        ax_price.plot(x, df['EMA200'], color='#ff6b6b', lw=0.9, alpha=0.8, label='EMA200')
        ax_price.fill_between(x, df['BB_lo'], df['BB_hi'],
                              color='#58a6ff', alpha=0.05, label='BB')
        ax_price.plot(x, df['BB_hi'], color='#58a6ff', lw=0.4, ls='--', alpha=0.4)
        ax_price.plot(x, df['BB_lo'], color='#58a6ff', lw=0.4, ls='--', alpha=0.4)

        # SAR dots
        if 'SAR' in df.columns:
            ax_price.scatter(x, df['SAR'], s=1.5, color='#9e6eff', alpha=0.5, zorder=2)

        # Buy/Sell markers
        buys  = sig_plot[sig_plot.isin(['STRONG BUY','MACD MEGA BUY','BUY','VOL BUY','M-RSI BUY'])]
        sells = sig_plot[sig_plot == 'SELL']
        SIG_COLORS = {
            'STRONG BUY': '#00ff88',
            'MACD MEGA BUY': '#00d4ff',
            'BUY': '#26d07c',
            'VOL BUY': '#fbbf24',
            'M-RSI BUY': '#c084fc',
            'SELL': '#ff6b6b',
        }
        for sig_val, color in SIG_COLORS.items():
            mask = sig_plot == sig_val
            if mask.any():
                prices = df.loc[mask, 'Close']
                offset = -0.04 if sig_val == 'SELL' else 0.04
                marker = 'v' if sig_val == 'SELL' else '^'
                ax_price.scatter(prices.index, prices * (1 + offset), s=60,
                                 color=color, marker=marker, zorder=5,
                                 label=sig_val, edgecolors='white', linewidths=0.3)

        # Fibonacci levels
        curr = df['Close'].iloc[-1]
        fib_colors = ['#fbbf24','#f97316','#ef4444','#dc2626']
        fib_labels = ['Fib 0.618x','Fib 1.618x','Fib 2.618x','Fib 4.236x']
        for fk, fc, fl in zip(['fib_0618','fib_1618','fib_2618','fib_4236'],
                               fib_colors, fib_labels):
            fv = fibs[fk]
            if fv > df['Low'].min():
                ax_price.axhline(fv, color=fc, lw=0.7, ls=':', alpha=0.8)
                ax_price.annotate(f'{fl} ₹{fv:,.0f}',
                                  xy=(x[-1], fv), xytext=(8, 0),
                                  textcoords='offset points',
                                  fontsize=6.5, color=fc, va='center')
        ax_price.axhline(fibs['swing_low'], color='#8b949e', lw=0.5, ls='--', alpha=0.5)

        ax_price.set_title(f'{name} — Multibagger Analysis', fontsize=13,
                           color='#e6edf3', fontweight='bold', pad=8)
        ax_price.legend(fontsize=7, loc='upper left', ncol=4,
                        facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3')
        ax_price.grid(True, alpha=0.3)
        ax_price.yaxis.tick_right()
        ax_price.set_ylabel('Price (₹)', color='#8b949e', fontsize=8)

        # ATH line
        ax_price.axhline(df['ATH'].iloc[-1], color='#00ff88', lw=0.6, ls='-.', alpha=0.6)
        ax_price.annotate(f'ATH ₹{df["ATH"].iloc[-1]:,.0f}',
                          xy=(x[-1], df['ATH'].iloc[-1]), xytext=(-60, 3),
                          textcoords='offset points', fontsize=6.5, color='#00ff88')

        # ── Panel 2: Volume ───────────────────────────────────────────────────
        vol_colors = np.where(df['Close'] >= df['Open'], '#26d07c', '#ff6b6b')
        vol_break_mask = df['VOL_RATIO'] > 2.5
        vol_colors_arr = vol_colors.copy()
        vol_colors_arr = np.where(vol_break_mask, '#fbbf24', vol_colors_arr)
        ax_vol.bar(x, df['Volume'] / 1e6, color=vol_colors_arr, alpha=0.8, width=1.2)
        ax_vol.plot(x, df['VOL_MA20'] / 1e6, color='#9e6eff', lw=0.9, label='Vol MA20')
        ax_vol.set_ylabel('Vol (M)', color='#8b949e', fontsize=7)
        ax_vol.yaxis.tick_right()
        ax_vol.legend(fontsize=6.5, loc='upper left', facecolor='#161b22',
                      edgecolor='#30363d', labelcolor='#e6edf3')
        ax_vol.grid(True, alpha=0.3)

        # ── Panel 3: RSI ──────────────────────────────────────────────────────
        ax_rsi.plot(x, df['RSI'], color='#c084fc', lw=1.1, label='RSI(14)')
        ax_rsi.axhline(70, color='#ff6b6b', lw=0.7, ls='--', alpha=0.6)
        ax_rsi.axhline(60, color='#26d07c', lw=0.5, ls=':', alpha=0.5)
        ax_rsi.axhline(50, color='#8b949e', lw=0.5, ls='-', alpha=0.4)
        ax_rsi.axhline(30, color='#fbbf24', lw=0.7, ls='--', alpha=0.6)
        ax_rsi.fill_between(x, df['RSI'], 70, where=df['RSI']>=70,
                            color='#ff6b6b', alpha=0.15)
        ax_rsi.fill_between(x, df['RSI'], 30, where=df['RSI']<=30,
                            color='#26d07c', alpha=0.15)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel('RSI', color='#8b949e', fontsize=7)
        ax_rsi.yaxis.tick_right()
        ax_rsi.legend(fontsize=6.5, loc='upper left', facecolor='#161b22',
                      edgecolor='#30363d', labelcolor='#e6edf3')
        ax_rsi.grid(True, alpha=0.3)

        # ── Panel 4: Standard MACD ────────────────────────────────────────────
        ax_macd.plot(x, df['MACD'],     color='#58a6ff', lw=1.0, label='MACD(12,26)')
        ax_macd.plot(x, df['MACD_sig'], color='#f0b429', lw=0.9, label='Signal(9)', ls='--')
        hist_colors = np.where(df['MACD_hist'] >= 0, '#26d07c', '#ff6b6b')
        ax_macd.bar(x, df['MACD_hist'], color=hist_colors, alpha=0.6, width=1.2)
        ax_macd.axhline(0, color='#8b949e', lw=0.5, alpha=0.5)
        ax_macd.set_ylabel('MACD', color='#8b949e', fontsize=7)
        ax_macd.yaxis.tick_right()
        ax_macd.legend(fontsize=6.5, loc='upper left', facecolor='#161b22',
                       edgecolor='#30363d', labelcolor='#e6edf3')
        ax_macd.grid(True, alpha=0.3)

        # ── Panel 5: Ultra-slow MACD (34,1000,20) ────────────────────────────
        ax_us.plot(x, df['MACD_US'],     color='#00d4ff', lw=1.2, label='MACD(34,1000,20)')
        ax_us.plot(x, df['MACD_US_sig'], color='#ff9800', lw=0.9, label='Signal(20)', ls='--')
        ush_colors = np.where(df['MACD_US_hist'] >= 0, '#26d07c', '#ff6b6b')
        ax_us.bar(x, df['MACD_US_hist'], color=ush_colors, alpha=0.5, width=1.2)
        ax_us.axhline(0, color='#00ff88', lw=0.8, alpha=0.7, ls='-')
        ax_us.set_ylabel('MACD\nUltra-Slow', color='#8b949e', fontsize=7)
        ax_us.yaxis.tick_right()
        ax_us.legend(fontsize=6.5, loc='upper left', facecolor='#161b22',
                     edgecolor='#30363d', labelcolor='#e6edf3')
        ax_us.grid(True, alpha=0.3)
        ax_us.set_xlabel('Date', color='#8b949e', fontsize=8)

        plt.setp(ax_price.get_xticklabels(), visible=False)
        plt.setp(ax_vol.get_xticklabels(),   visible=False)
        plt.setp(ax_rsi.get_xticklabels(),   visible=False)
        plt.setp(ax_macd.get_xticklabels(),  visible=False)

        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=110, bbox_inches='tight',
                    facecolor='#0d1117', edgecolor='none')
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

# ── Monthly RSI Chart ─────────────────────────────────────────────────────────
def generate_monthly_chart(name, df_m):
    with plt.rc_context(MPLSTYLE):
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 6), sharex=True,
                                       facecolor='#0d1117', gridspec_kw={'height_ratios':[2,1]})
        x = df_m.index
        ax1.plot(x, df_m['Close'], color='#e6edf3', lw=1.5)
        ax1.fill_between(x, df_m['Close'], df_m['Close'].min()*0.9,
                         color='#58a6ff', alpha=0.07)
        ax1.set_title(f'{name} — Monthly Price & RSI', fontsize=11,
                      color='#e6edf3', fontweight='bold')
        ax1.grid(True, alpha=0.3)
        ax1.yaxis.tick_right()
        ax1.set_ylabel('Price (₹)', color='#8b949e', fontsize=8)

        ax2.plot(x, df_m['RSI'], color='#c084fc', lw=1.3, label='Monthly RSI(14)')
        ax2.axhline(70, color='#ff6b6b', lw=1.0, ls='--', alpha=0.8, label='RSI 70')
        ax2.axhline(50, color='#8b949e', lw=0.5, ls='-',  alpha=0.4)
        ax2.fill_between(x, df_m['RSI'], 70, where=df_m['RSI']>=70,
                         color='#ff6b6b', alpha=0.25, label='Above 70')
        ax2.set_ylim(0, 100)
        ax2.set_ylabel('RSI', color='#8b949e', fontsize=8)
        ax2.yaxis.tick_right()
        ax2.legend(fontsize=7, facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3')
        ax2.grid(True, alpha=0.3)

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0d1117')
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

# ── Weekly RSI Chart ──────────────────────────────────────────────────────────
def generate_weekly_chart(name, df_w):
    with plt.rc_context(MPLSTYLE):
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 5), sharex=True,
                                       facecolor='#0d1117', gridspec_kw={'height_ratios':[2,1]})
        x = df_w.tail(156).index   # last 3 years weekly
        dw = df_w.tail(156)
        ax1.plot(x, dw['Close'], color='#e6edf3', lw=1.3)
        ax1.plot(x, dw['EMA21'], color='#f0b429', lw=0.9, alpha=0.8)
        ax1.plot(x, dw['EMA50'], color='#58a6ff', lw=0.9, alpha=0.8)
        ax1.set_title(f'{name} — Weekly Price & RSI', fontsize=11,
                      color='#e6edf3', fontweight='bold')
        ax1.grid(True, alpha=0.3)
        ax1.yaxis.tick_right()
        ax2.plot(x, dw['RSI'], color='#f0b429', lw=1.3, label='Weekly RSI(14)')
        ax2.axhline(70, color='#ff6b6b', lw=1.0, ls='--', alpha=0.8)
        ax2.axhline(55, color='#26d07c', lw=0.6, ls=':', alpha=0.6, label='RSI 55')
        ax2.fill_between(x, dw['RSI'], 70, where=dw['RSI']>=70,
                         color='#ff6b6b', alpha=0.2)
        ax2.set_ylim(0, 100)
        ax2.yaxis.tick_right()
        ax2.legend(fontsize=7, facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3')
        ax2.grid(True, alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0d1117')
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

# ── HTML Helpers ──────────────────────────────────────────────────────────────
SIG_BADGE = {
    'STRONG BUY':   ('🚀', '#00ff88', '#002d1a'),
    'MACD MEGA BUY':('🌊', '#00d4ff', '#001a26'),
    'BUY':          ('✅', '#26d07c', '#0d2615'),
    'VOL BUY':      ('🔥', '#fbbf24', '#2a1e00'),
    'M-RSI BUY':    ('💜', '#c084fc', '#1e0030'),
    'SELL':         ('❌', '#ff6b6b', '#2a0000'),
    'HOLD':         ('⏸', '#8b949e', '#1a1a1a'),
}

def sig_badge(sig):
    em, fg, bg = SIG_BADGE.get(sig, ('⏸','#8b949e','#1a1a1a'))
    return (f'<span style="background:{bg};color:{fg};border:1px solid {fg}44;'
            f'border-radius:12px;padding:2px 10px;font-size:11px;font-weight:700;'
            f'white-space:nowrap">{em} {sig}</span>')

def ret_badge(pct):
    c = '#26d07c' if pct >= 0 else '#ff6b6b'
    return f'<span style="color:{c};font-weight:700">{pct:+.1f}%</span>'

def score_bar(score):
    c = '#26d07c' if score >= 70 else '#fbbf24' if score >= 45 else '#ff6b6b'
    return (f'<div style="display:flex;align-items:center;gap:6px">'
            f'<div style="flex:1;height:6px;background:#21262d;border-radius:3px">'
            f'<div style="width:{score}%;height:100%;background:{c};border-radius:3px"></div></div>'
            f'<span style="color:{c};font-weight:700;font-size:11px">{score}</span></div>')

def _trade_table(trades):
    if not trades:
        return '<p style="color:#8b949e;font-size:12px;margin-top:12px">No completed backtest trades found.</p>'
    rows = []
    for t in trades[-10:]:  # last 10 trades
        rc = '#26d07c' if t['return_pct'] > 0 else '#ff6b6b'
        open_badge = ' <span style="color:#fbbf24;font-size:10px">(OPEN)</span>' if t.get('open') else ''
        rows.append(f'''<tr>
          <td>{t["entry_date"].strftime("%d %b %Y")}</td>
          <td>{sig_badge(t["signal"])}</td>
          <td style="text-align:right">₹{t["entry_price"]:,.1f}</td>
          <td>{t["exit_date"].strftime("%d %b %Y")}{open_badge}</td>
          <td style="text-align:right">₹{t["exit_price"]:,.1f}</td>
          <td style="text-align:right;color:{rc};font-weight:700">{t["return_pct"]:+.1f}%</td>
          <td style="text-align:right;color:#8b949e">{t["days_held"]}d</td>
        </tr>''')
    return f'''<h4 style="color:#8b949e;font-size:12px;margin-bottom:8px">📋 Backtest Trades (last 10)</h4>
      <table style="width:100%;border-collapse:collapse;font-size:11.5px">
        <tr style="color:#8b949e;font-size:10px">
          <th style="text-align:left;padding:4px 8px">Entry Date</th>
          <th style="text-align:left;padding:4px 8px">Signal</th>
          <th style="text-align:right;padding:4px 8px">Entry ₹</th>
          <th style="text-align:left;padding:4px 8px">Exit Date</th>
          <th style="text-align:right;padding:4px 8px">Exit ₹</th>
          <th style="text-align:right;padding:4px 8px">Return</th>
          <th style="text-align:right;padding:4px 8px">Held</th>
        </tr>
        {"".join(rows)}
      </table>'''

# ── Main HTML Generator ───────────────────────────────────────────────────────
def build_html(results, run_ts):
    n_total       = len(results)
    n_ath         = sum(1 for r in results if r['ath_pct'] >= 0)
    n_mrsi70      = sum(1 for r in results if r['m_rsi'] > 70)
    n_strong      = sum(1 for r in results if r['signal'] in ('STRONG BUY','MACD MEGA BUY'))
    avg_score     = round(np.mean([r['score'] for r in results]), 1) if results else 0

    rows = []
    for i, r in enumerate(results, 1):
        fibs = r['fibs']
        trades = r['trades']
        n_trades  = len(trades)
        wins      = [t for t in trades if t['return_pct'] > 0]
        win_rate  = round(len(wins)/n_trades*100) if n_trades else 0
        avg_ret   = round(np.mean([t['return_pct'] for t in trades]), 1) if trades else 0
        best_ret  = round(max([t['return_pct'] for t in trades], default=0), 1)
        latest_sig = r['signal']

        fib_cells = "".join([
            f'<td style="text-align:right;color:#fbbf24;font-size:11px">₹{fibs["fib_0618"]:,.0f}</td>',
            f'<td style="text-align:right;color:#f97316;font-size:11px">₹{fibs["fib_1618"]:,.0f}</td>',
            f'<td style="text-align:right;color:#ef4444;font-size:11px">₹{fibs["fib_2618"]:,.0f}</td>',
            f'<td style="text-align:right;color:#dc2626;font-size:11px">₹{fibs["fib_4236"]:,.0f}</td>',
        ])

        ath_color = '#26d07c' if r['ath_pct'] >= 0 else ('#fbbf24' if r['ath_pct'] > -5 else '#ff6b6b')
        ath_str   = f'+{r["ath_pct"]:.1f}%' if r['ath_pct'] >= 0 else f'{r["ath_pct"]:.1f}%'

        rows.append(f'''<tr data-score="{r['score']}" data-mrsi="{r['m_rsi']:.1f}"
            data-wrsi="{r['w_rsi']:.1f}" data-drsi="{r['d_rsi']:.1f}"
            data-ath="{r['ath_pct']:.1f}" data-name="{r['name']}">
          <td style="text-align:center;color:#8b949e">{i}</td>
          <td><b style="color:#e6edf3">{r['ticker']}</b><br>
              <span style="color:#8b949e;font-size:10px">{r['name']}</span></td>
          <td style="text-align:right">₹{r['price']:,.1f}</td>
          <td style="text-align:right;color:{ath_color};font-weight:700">{ath_str}</td>
          <td style="text-align:right;color:{"#ff6b6b" if r["m_rsi"]>70 else "#fbbf24" if r["m_rsi"]>55 else "#8b949e"};font-weight:700">{r['m_rsi']:.1f}</td>
          <td style="text-align:right;color:{"#26d07c" if r["w_rsi"]>55 else "#8b949e"}">{r['w_rsi']:.1f}</td>
          <td style="text-align:right;color:{"#26d07c" if r["d_rsi"]>60 else "#8b949e"}">{r['d_rsi']:.1f}</td>
          <td style="text-align:right;color:{"#00d4ff" if r["us_macd"]>=0 else "#ff6b6b"};font-size:11px">{"▲ +" if r["us_macd"]>=0 else "▼ "}{abs(r["us_macd"]):.4f}</td>
          {fib_cells}
          <td>{sig_badge(latest_sig)}</td>
          <td style="text-align:right">{score_bar(r['score'])}</td>
          <td style="text-align:right;color:#8b949e">{n_trades}</td>
          <td style="text-align:right">{ret_badge(win_rate)} WR</td>
          <td style="text-align:right">{ret_badge(avg_ret)}</td>
          <td style="text-align:right">{ret_badge(best_ret)}</td>
          <td><button onclick="toggleDetail('d{i}')"
              style="background:#21262d;border:1px solid #30363d;color:#8b949e;
                     border-radius:8px;padding:3px 10px;cursor:pointer;font-size:11px">
              📊 Charts</button></td>
        </tr>
        <tr id="d{i}" style="display:none">
          <td colspan="16" style="padding:0">
            <div style="background:#0d1117;border-top:1px solid #21262d;padding:20px">
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
                <img src="data:image/png;base64,{r['chart_m']}" style="width:100%;border-radius:8px">
                <img src="data:image/png;base64,{r['chart_w']}" style="width:100%;border-radius:8px">
              </div>
              <img src="data:image/png;base64,{r['chart_d']}" style="width:100%;border-radius:8px;margin-bottom:16px">
              <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:10px">
                <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px">
                  <div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px">Swing Low</div>
                  <div style="color:#e6edf3;font-size:16px;font-weight:700">₹{fibs["swing_low"]:,.1f}</div>
                </div>
                <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px">
                  <div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px">Swing High</div>
                  <div style="color:#e6edf3;font-size:16px;font-weight:700">₹{fibs["swing_high"]:,.1f}</div>
                </div>
                <div style="background:#002d1a;border:1px solid #26d07c44;border-radius:8px;padding:12px">
                  <div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px">Fib 1.618x Target</div>
                  <div style="color:#26d07c;font-size:16px;font-weight:700">₹{fibs["fib_1618"]:,.1f}</div>
                </div>
                <div style="background:#2a1200;border:1px solid #dc262644;border-radius:8px;padding:12px">
                  <div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px">Fib 4.236x Target</div>
                  <div style="color:#dc2626;font-size:16px;font-weight:700">₹{fibs["fib_4236"]:,.1f}</div>
                </div>
              </div>
              {_trade_table(trades)}
            </div>
          </td>
        </tr>''')

    rows_html = "\n".join(rows)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multibagger Report — {run_ts}</title>
<style>
:root {{
  --bg:#0d1117; --card:#161b22; --border:#21262d; --text:#e6edf3;
  --sub:#8b949e; --cyan:#00d4ff; --green:#26d07c; --gold:#f0b429;
  --red:#ff6b6b; --purple:#c084fc;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}}
.header{{background:#010409;border-bottom:2px solid var(--border);padding:18px 24px 14px}}
.header h1{{font-size:21px;font-weight:700;color:var(--cyan);letter-spacing:1px}}
.subtitle{{color:var(--sub);font-size:11.5px;margin-top:4px}}
.nav-links{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}
.nav-link{{display:inline-flex;align-items:center;gap:5px;background:var(--card);border:1px solid var(--border);
           color:var(--text);border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;
           text-decoration:none;transition:all .15s}}
.nav-link:hover{{border-color:var(--cyan);color:var(--cyan)}}
.nav-link.active{{background:var(--cyan);color:#000;border-color:var(--cyan)}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;padding:16px 24px}}
.stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 18px;min-width:110px}}
.stat .val{{font-size:24px;font-weight:700}}
.stat .lbl{{font-size:10px;color:var(--sub);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}}
.strategy-box{{margin:0 24px 16px;background:#0a1628;border:1px solid #1e3a5f;border-radius:10px;padding:14px 18px}}
.strategy-box h3{{color:var(--cyan);font-size:13px;margin-bottom:8px}}
.strategy-box ul{{color:var(--sub);font-size:11.5px;line-height:1.8;padding-left:18px}}
.strategy-box li span{{color:var(--text)}}
.filter-bar{{padding:10px 24px;background:#010409;border-bottom:1px solid var(--border);
             position:sticky;top:0;z-index:1000;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.filter-bar input{{flex:1;min-width:180px;max-width:260px;background:var(--card);border:1px solid var(--border);
                   color:var(--text);border-radius:20px;padding:5px 14px;font-size:12px;outline:none}}
.filter-bar input:focus{{border-color:var(--cyan)}}
.sort-btn{{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;
           padding:4px 12px;cursor:pointer;font-size:11.5px;transition:all .15s;font-weight:600}}
.sort-btn:hover,.sort-btn.active{{background:var(--cyan);color:#000;border-color:var(--cyan)}}
table{{width:100%;border-collapse:collapse}}
th{{background:#010409;padding:9px 10px;text-align:right;font-size:11px;color:var(--sub);
    text-transform:uppercase;letter-spacing:.5px;cursor:pointer;white-space:nowrap;border-bottom:1px solid var(--border)}}
th:hover{{color:var(--cyan)}}
th.asc::after{{content:" ▲"}}th.desc::after{{content:" ▼"}}
th:first-child,th:nth-child(2),th:last-child{{text-align:left}}
td{{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr:hover td{{background:#161b2288}}
.footer{{text-align:center;padding:24px;color:var(--sub);font-size:11px;border-top:1px solid var(--border);margin-top:20px}}
</style>
</head>
<body>
<div class="header">
  <h1>🏆 Multibagger Report — NSE Strong Momentum Scanner</h1>
  <div class="subtitle">
    Strategy: ATH Breakout + Monthly RSI&gt;70 + Ultra-Slow MACD(34,1000,20) + Fibonacci Extensions
    &nbsp;|&nbsp; {run_ts} IST &nbsp;|&nbsp; Multi-Timeframe: Daily · Weekly · Monthly
  </div>
  <div class="nav-links">
    <a class="nav-link" href="/">📊 Full RSI Report</a>
    <a class="nav-link" href="/ath">🏆 ATH Breakout</a>
    <a class="nav-link active" href="/multibagger">💎 Multibagger</a>
    <a class="nav-link" href="/rocket">🚀 Rocket Scanner</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="val" style="color:var(--cyan)">{n_total}</div><div class="lbl">Stocks Analysed</div></div>
  <div class="stat"><div class="val" style="color:var(--green)">{n_ath}</div><div class="lbl">ATH Breakouts</div></div>
  <div class="stat"><div class="val" style="color:var(--red)">{n_mrsi70}</div><div class="lbl">M-RSI &gt; 70</div></div>
  <div class="stat"><div class="val" style="color:var(--gold)">{n_strong}</div><div class="lbl">Strong Signals</div></div>
  <div class="stat"><div class="val" style="color:var(--purple)">{avg_score}</div><div class="lbl">Avg Score</div></div>
</div>

<div class="strategy-box">
  <h3>📌 Multibagger Strategy — How to Ride 5x–10x Trends</h3>
  <ul>
    <li><span>🚀 STRONG BUY</span> — ATH Breakout (new all-time high) AND Monthly RSI&gt;70: Highest conviction signal for mega-trends</li>
    <li><span>🌊 MACD MEGA BUY</span> — Ultra-slow MACD(34,1000,20) crosses above zero + M-RSI&gt;60: Structural trend confirmation</li>
    <li><span>✅ BUY</span> — Daily RSI crosses 60 + Weekly RSI&gt;55 + Volume surge: Fresh momentum entry in established uptrend</li>
    <li><span>🔥 VOL BUY</span> — Volume &gt;2.5x average + price above all EMAs: Institutional accumulation signal</li>
    <li><span>💜 M-RSI BUY</span> — Monthly RSI freshly crosses 70 + price above 50 EMA: Monthly momentum ignition</li>
    <li><span>Fib Targets</span> — 0.618x (conservative), 1.618x (standard), 2.618x (aggressive), 4.236x (moonshot 5x–10x)</li>
  </ul>
</div>

<div class="filter-bar">
  <input type="text" id="searchInput" placeholder="🔍 Search ticker or company…" oninput="filterTable()">
  <button class="sort-btn" onclick="sortBy('score')">Sort: Score</button>
  <button class="sort-btn" onclick="sortBy('mrsi')">Sort: M-RSI</button>
  <button class="sort-btn" onclick="sortBy('ath')">Sort: ATH%</button>
  <button class="sort-btn" onclick="sortBy('wrsi')">Sort: W-RSI</button>
  <button class="sort-btn" onclick="sortBy('drsi')">Sort: D-RSI</button>
  <span id="countInfo" style="color:var(--sub);font-size:11px;margin-left:4px"></span>
</div>

<table id="mainTable">
<thead>
<tr>
  <th onclick="thSort(0)">#</th>
  <th onclick="thSort(1)" style="text-align:left">Ticker / Company</th>
  <th onclick="thSort(2)">Price</th>
  <th onclick="thSort(3)">ATH%</th>
  <th onclick="thSort(4)">M-RSI</th>
  <th onclick="thSort(5)">W-RSI</th>
  <th onclick="thSort(6)">D-RSI</th>
  <th onclick="thSort(7)">MACD Ultra-Slow</th>
  <th onclick="thSort(8)">Fib 0.618x</th>
  <th onclick="thSort(9)">Fib 1.618x</th>
  <th onclick="thSort(10)">Fib 2.618x</th>
  <th onclick="thSort(11)">Fib 4.236x</th>
  <th onclick="thSort(12)" style="text-align:left">Signal</th>
  <th onclick="thSort(13)">Score</th>
  <th onclick="thSort(14)">Trades</th>
  <th onclick="thSort(15)">Win Rate</th>
  <th onclick="thSort(16)">Avg Ret</th>
  <th onclick="thSort(17)">Best Ret</th>
  <th style="text-align:left">Charts</th>
</tr>
</thead>
<tbody id="tableBody">
{rows_html}
</tbody>
</table>

<div class="footer">
  Multibagger Report v1.0 &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp;
  <b>Not financial advice.</b> For educational purposes only.<br>
  Strategy: ATH Breakout + M-RSI&gt;70 + Ultra-Slow MACD(34,1000,20) + Fibonacci Extensions
</div>

<script>
function toggleDetail(id){{
  const el=document.getElementById(id);
  el.style.display=el.style.display==='none'?'table-row':'none';
}}
function filterTable(){{
  const q=document.getElementById('searchInput').value.toLowerCase();
  const rows=document.querySelectorAll('#tableBody tr[data-name]');
  let shown=0;
  rows.forEach(r=>{{
    const match=r.dataset.name.toLowerCase().includes(q)||
                r.querySelector('b').textContent.toLowerCase().includes(q);
    r.style.display=match?'':'none';
    const nextId=r.querySelector('button')?.getAttribute('onclick')?.match(/'(d\\d+)'/)?.[1];
    if(nextId){{document.getElementById(nextId).style.display='none';}}
    if(match)shown++;
  }});
  document.getElementById('countInfo').textContent=`Showing ${{shown}} of {n_total}`;
}}
function sortBy(field){{
  const rows=[...document.querySelectorAll('#tableBody tr[data-name]')];
  const map={{score:'score',mrsi:'mrsi',wrsi:'wrsi',drsi:'drsi',ath:'ath'}};
  rows.sort((a,b)=>parseFloat(b.dataset[map[field]]||0)-parseFloat(a.dataset[map[field]]||0));
  const tbody=document.getElementById('tableBody');
  rows.forEach(r=>{{tbody.appendChild(r);}});
  document.querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
}}
let lastThSort=-1, lastThDir=1;
function thSort(col){{
  const tbody=document.getElementById('tableBody');
  const rows=[...tbody.querySelectorAll('tr[data-name]')];
  const dir=lastThSort===col?-lastThDir:1;
  lastThSort=col; lastThDir=dir;
  rows.sort((a,b)=>{{
    const av=a.cells[col]?.textContent.replace(/[₹,%+▲▼ ]/g,'').trim()||'';
    const bv=b.cells[col]?.textContent.replace(/[₹,%+▲▼ ]/g,'').trim()||'';
    const an=parseFloat(av), bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn)) return dir*(an-bn);
    return dir*av.localeCompare(bv);
  }});
  rows.forEach(r=>tbody.appendChild(r));
  document.querySelectorAll('th').forEach((h,i)=>{{
    h.className=i===col?(dir===1?'asc':'desc'):'';
  }});
}}
document.addEventListener('DOMContentLoaded',()=>{{
  document.getElementById('countInfo').textContent=`Showing {n_total} stocks`;
}});
</script>
</body>
</html>'''


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    run_ts = datetime.now(ist).strftime('%Y-%m-%d %H:%M')

    print(f"🚀 Multibagger Report — {run_ts} IST")
    print(f"   Watchlist: {list(WATCHLIST.keys())}")
    print()

    results = []
    for name, ticker in WATCHLIST.items():
        print(f"  📥 Fetching {name} ({ticker})…")
        df_d = fetch_data(ticker)
        if df_d is None:
            print(f"  ⚠️  Skipping {name} — no data")
            continue

        df_d = add_indicators(df_d)
        df_w = add_indicators(resample_weekly(df_d.copy()))
        df_m = add_indicators(resample_monthly(df_d.copy()))

        signals = generate_signals(df_d, df_w, df_m)
        trades  = backtest(df_d, signals)
        fibs    = fibonacci_targets(df_d)
        score   = compute_score(df_d, df_w, df_m, signals)

        latest_sig = signals.iloc[-1]
        price      = round(df_d['Close'].iloc[-1], 2)
        ath_pct    = round(df_d['ATH_PCT'].iloc[-1], 1)
        m_rsi      = round(df_m['RSI'].iloc[-1], 1) if len(df_m) > 0 else 0
        w_rsi      = round(df_w['RSI'].iloc[-1], 1) if len(df_w) > 0 else 0
        d_rsi      = round(df_d['RSI'].iloc[-1], 1)
        us_macd    = round(df_d['MACD_US'].iloc[-1], 4)

        print(f"  ✅ {name}: Price=₹{price:,.1f} | M-RSI={m_rsi} | Signal={latest_sig} | Score={score}")

        print(f"     Generating charts…")
        chart_d = generate_chart(name, df_d, df_w, df_m, signals, fibs, trades)
        chart_m = generate_monthly_chart(name, df_m)
        chart_w = generate_weekly_chart(name, df_w)

        results.append({
            'name': name, 'ticker': ticker.replace('.NS',''),
            'price': price, 'ath_pct': ath_pct,
            'm_rsi': m_rsi, 'w_rsi': w_rsi, 'd_rsi': d_rsi,
            'us_macd': us_macd, 'score': score, 'signal': latest_sig,
            'fibs': fibs, 'trades': trades,
            'chart_d': chart_d, 'chart_w': chart_w, 'chart_m': chart_m,
        })

    # Sort by score desc
    results.sort(key=lambda r: r['score'], reverse=True)

    # Build HTML
    print(f"\nBuilding HTML report…")
    html = build_html(results, run_ts)

    tmp = REPORT_HTML + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(html)
    os.replace(tmp, REPORT_HTML)

    size_mb = os.path.getsize(REPORT_HTML) / 1024 / 1024
    print(f"✅ Report saved: {REPORT_HTML} ({size_mb:.1f} MB)")
    return REPORT_HTML


if __name__ == '__main__':
    main()
