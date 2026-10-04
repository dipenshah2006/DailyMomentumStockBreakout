---
name: Dashboard indicator shape
description: Preserve pandas series shape for short-history dashboard calculations.
---

Indicator helper results should remain pandas Series/DataFrames, including when the input has one observation. Avoid `squeeze()` unless a scalar is explicitly required; squeezing a one-item Series turns it into a NumPy scalar and breaks rolling and cross calculations.

**Why:** Synthetic index joins can produce exactly one usable daily close, which caused the dashboard to fail in downstream indicator calculations.

**How to apply:** Before changing RSI, MACD, or SMA helpers, trace downstream `.rolling()`, `.ewm()`, `.dropna()`, `.iloc`, and cross-detection consumers. Convert to a scalar only at presentation boundaries.