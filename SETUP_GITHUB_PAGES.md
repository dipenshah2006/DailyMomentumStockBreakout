# One-Time GitHub Pages Setup

The workflow is ready. You just need to enable GitHub Pages once.

## Steps (do this once)

### 1. Enable GitHub Pages
1. Go to your repo → **Settings** → **Pages** (left sidebar)
2. Under **Build and deployment**, set **Source** → `GitHub Actions`
3. Click **Save**

### 2. Enable write permissions for Actions
1. Go to **Settings** → **Actions** → **General**
2. Scroll to **Workflow permissions**
3. Select **Read and write permissions**
4. Click **Save**

### 3. Run the workflow
1. Go to **Actions** tab
2. Click **Generate NSE RSI Report** on the left
3. Click **Run workflow** → **Run workflow**
4. Wait ~5–90 minutes (depends on how many stocks are downloaded)

### 4. View your report
Once the green ✅ appears, your report is live at:
```
https://dipenshah2006.github.io/DailyMomentumStockBreakout/
```

---

## What was fixed in the workflow

| Problem | Fix |
|---------|-----|
| `pip install -r requirements.txt` failing (no file) | Inline pip install + added `requirements.txt` |
| `deploy-pages` failing silently (no Pages environment) | Added `environment: github-pages` block |
| Report file not found (`rsi_mtf_report_*.html`) | Added fallback to accept `rsi_mtf_report_NSE.html` (the actual output name) |
| Node.js 20 deprecation warning | Uses `actions/checkout@v4`, `actions/setup-python@v5` (Node 20 compatible, upgrade when v5+ drops) |
| No artifact on failure (can't debug) | `upload-artifact` now runs `if: always()` |
| Charts not cached between runs | Added `actions/cache` for both `stock_data_cache.pkl` and `charts/` |
