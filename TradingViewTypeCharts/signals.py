"""
signals.py
Builds the full indicator/signal table for one symbol's daily OHLCV data,
following the rules specified by the user:

BUY  = RSI(14) bull-crosses SMA14(RSI)  AND  CCI(200) bull-crosses SMA20(CCI)
       AND RSI14 > 50 AND CCI200 > 0 (both "positive")
       AND MACD(34,200,9) bullish cross

SELL = MACD(34,200,9) bearish cross  AND  CCI(200) bearish-crosses SMA20(CCI)

Extra filter columns:
  CCI200_Above_100
  CCI200_Bullish_Increasing   (CCI > its SMA20 AND CCI rising)
  CCI200_Strong_Mom_Volume    (CCI200 > 100 AND Volume > 1.5x its 20d avg)
  HA_Bullish_Trend            (last 3 Heikin-Ashi candles all bullish)
  Strong_RSI_Buy              (daily, weekly, monthly RSI all rising)
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import indicators as ind


def build_signal_table(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily.copy()

    # ---- Daily RSI / CCI / MACD ----
    df["RSI14"] = ind.rsi(df["Close"], 14)
    df["RSI14_SMA14"] = ind.sma(df["RSI14"], 14)
    df["RSI14_SMA34"] = ind.sma(df["RSI14"], 34)
    df["RSI14_SMA34"] = ind.sma(df["RSI14"], 34)
    df["CCI200"] = ind.cci(df, 200)
    df["CCI200_SMA20"] = ind.sma(df["CCI200"], 20)
    macd_line, macd_signal, macd_hist = ind.macd(df["Close"], 34, 200, 9)
    df["MACD"] = macd_line
    df["MACD_Signal"] = macd_signal
    df["MACD_Hist"] = macd_hist

    rsi_bull_cross = ind.crossed_above(df["RSI14"], df["RSI14_SMA14"])
    cci_bull_cross = ind.crossed_above(df["CCI200"], df["CCI200_SMA20"])
    macd_bull_cross = ind.crossed_above(df["MACD"], df["MACD_Signal"])
    cci_bear_cross = ind.crossed_below(df["CCI200"], df["CCI200_SMA20"])
    macd_bear_cross = ind.crossed_below(df["MACD"], df["MACD_Signal"])

    both_positive = (df["RSI14"] > 50) & (df["CCI200"] > 0)

    df["Buy_Signal"] = rsi_bull_cross & cci_bull_cross & both_positive & macd_bull_cross
    df["Sell_Signal"] = macd_bear_cross & cci_bear_cross

    # ---- Filters ----
    df["CCI200_Above_100"] = df["CCI200"] > 100
    df["CCI200_Bullish_Increasing"] = (df["CCI200"] > df["CCI200_SMA20"]) & \
                                       (df["CCI200"] > df["CCI200"].shift(1))
    vol_sma20 = ind.sma(df["Volume"], 20)
    df["CCI200_Strong_Mom_Volume"] = (df["CCI200"] > 100) & (df["Volume"] > 1.5 * vol_sma20)

    ha = ind.heikin_ashi(df)
    df["HA_Open"], df["HA_High"] = ha["HA_Open"], ha["HA_High"]
    df["HA_Low"], df["HA_Close"] = ha["HA_Low"], ha["HA_Close"]
    df["HA_Bullish"] = ha["HA_Bullish"]
    df["HA_Bullish_Trend"] = ha["HA_Bullish"].rolling(3).sum() == 3

    # ---- Multi-timeframe RSI (each with its own SMA(14) for crossover clarity) ----
    wk = ind.weekly(df)
    mo = ind.monthly(df)
    wk_rsi = ind.rsi(wk["Close"], 14)
    mo_rsi = ind.rsi(mo["Close"], 14)
    wk_rsi_sma = ind.sma(wk_rsi, 14)
    mo_rsi_sma = ind.sma(mo_rsi, 14)

    df["Weekly_RSI14"] = wk_rsi.reindex(df.index, method="ffill")
    df["Monthly_RSI14"] = mo_rsi.reindex(df.index, method="ffill")
    df["Weekly_RSI14_SMA14"] = wk_rsi_sma.reindex(df.index, method="ffill")
    df["Monthly_RSI14_SMA14"] = mo_rsi_sma.reindex(df.index, method="ffill")

    daily_rsi_rising = df["RSI14"] > df["RSI14"].shift(1)
    weekly_rsi_rising = wk_rsi.reindex(df.index, method="ffill") > \
        wk_rsi.shift(1).reindex(df.index, method="ffill")
    monthly_rsi_rising = mo_rsi.reindex(df.index, method="ffill") > \
        mo_rsi.shift(1).reindex(df.index, method="ffill")

    df["Strong_RSI_Buy"] = daily_rsi_rising & weekly_rsi_rising & monthly_rsi_rising

    # ---- RSI trend/channel (daily) ----
    rsi_trend, rsi_upper, rsi_lower = ind.regression_channel(df["RSI14"], lookback=60)
    df["RSI_Trend"], df["RSI_Channel_Upper"], df["RSI_Channel_Lower"] = rsi_trend, rsi_upper, rsi_lower

    return df, wk, mo, wk_rsi, mo_rsi


def latest_snapshot(symbol: str, df: pd.DataFrame, wk_rsi: pd.Series, mo_rsi: pd.Series,
                     targets: dict, supports: list, resistances: list) -> dict:
    last = df.iloc[-1]
    return {
        "Symbol": symbol,
        "Date": df.index[-1].strftime("%Y-%m-%d"),
        "Close": round(float(last["Close"]), 2),
        "RSI14": round(float(last["RSI14"]), 2) if pd.notna(last["RSI14"]) else None,
        "Weekly_RSI14": round(float(wk_rsi.iloc[-1]), 2) if len(wk_rsi) and pd.notna(wk_rsi.iloc[-1]) else None,
        "Monthly_RSI14": round(float(mo_rsi.iloc[-1]), 2) if len(mo_rsi) and pd.notna(mo_rsi.iloc[-1]) else None,
        "CCI200": round(float(last["CCI200"]), 2) if pd.notna(last["CCI200"]) else None,
        "MACD_Hist": round(float(last["MACD_Hist"]), 3) if pd.notna(last["MACD_Hist"]) else None,
        "Buy_Signal": bool(last["Buy_Signal"]),
        "Sell_Signal": bool(last["Sell_Signal"]),
        "CCI200_Above_100": bool(last["CCI200_Above_100"]),
        "CCI200_Bullish_Increasing": bool(last["CCI200_Bullish_Increasing"]),
        "CCI200_Strong_Mom_Volume": bool(last["CCI200_Strong_Mom_Volume"]),
        "HA_Bullish_Trend": bool(last["HA_Bullish_Trend"]),
        "Strong_RSI_Buy": bool(last["Strong_RSI_Buy"]),
        "Supports": supports,
        "Resistances": resistances,
        "RSI_Price_Targets": targets,
    }
