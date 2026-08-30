---
name: ASX screener CI validation
description: Root causes and verification expectations for ASX report failures in GitHub Actions.
---

The ASX screener must be smoke-tested through `analyze_stock()` for at least one real `.AX` symbol after structural edits; a missing helper declaration can be caught by the broad per-ticker error handling and otherwise appear only as an empty-report validation failure.

**Why:** The report process can exit successfully even when every ticker analysis fails, because per-stock exceptions are counted rather than re-raised. The workflow's non-empty-report check then fails later, obscuring the original Python error.

**How to apply:** When ASX Actions reports a successful screener step followed by validation failure, inspect per-ticker helper definitions first and test a real ASX symbol before changing the workflow validation.