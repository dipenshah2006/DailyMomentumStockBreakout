---
name: MonthlyTrend CI
description: Daily execution and cache requirements for the MonthlyTrend breakout report.
---

MonthlyTrend's per-symbol parquet cache requires a parquet engine such as PyArrow; without it, the code catches cache read/write errors and silently falls back to repeated full downloads.

**Why:** The screener can still produce a report without a parquet engine, but daily runs become slow and more exposed to Yahoo rate limits.

**How to apply:** Keep PyArrow installed in both the scheduled workflow and any local environment used to run the MonthlyTrend main script.