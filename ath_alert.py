"""
NSE ATH ALERT — Real-time Watchlist Monitor
============================================
Runs hourly during market hours (9:15 AM – 3:30 PM IST, Mon–Fri).
Checks every stock in watchlist.txt against its historical ATH.
If today's intraday high > previous ATH → fires an email alert.

Deduplication: writes ath_alert_fired_YYYYMMDD.txt so the same stock
only triggers ONE alert per calendar day, no matter how many hourly
runs catch it.

Output: ath_alert.html (emailed if any new ATHs found)
"""

import os
import warnings
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
WATCHLIST_FILE   = "watchlist.txt"
FIRED_FILE       = f"ath_alert_fired_{date.today().strftime('%Y%m%d')}.txt"
OUTPUT_FILE      = "ath_alert.html"
MAX_WORKERS      = 10
PAGES_BASE       = "https://dipenshah2006.github.io/DailyMomentumStockBreakout"
ATH_BUFFER       = 0.001   # 0.1% — counts as ATH if within this margin above old ATH


# ── Watchlist loader ──────────────────────────────────────────────────────────
def load_watchlist() -> list[str]:
    if not os.path.exists(WATCHLIST_FILE):
        print(f"⚠️  {WATCHLIST_FILE} not found — using Nifty50 fallback")
        return ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","KOTAKBANK",
                "SBIN","AXISBANK","LT","WIPRO","HCLTECH","BAJFINANCE",
                "TITAN","ASIANPAINT","MARUTI","ULTRACEMCO"]
    syms = []
    with open(WATCHLIST_FILE, "r") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                syms.append(s.upper())
    return syms


def load_fired_today() -> set[str]:
    if not os.path.exists(FIRED_FILE):
        return set()
    with open(FIRED_FILE, "r") as f:
        return {ln.strip().upper() for ln in f if ln.strip()}


def mark_fired(sym: str):
    with open(FIRED_FILE, "a") as f:
        f.write(sym.upper() + "\n")


# ── Per-stock ATH checker ─────────────────────────────────────────────────────
def check_ath(sym: str) -> dict | None:
    """Returns alert dict if stock hit a new ATH today, else None."""
    yf_sym = sym + ".NS"
    try:
        # Historical daily data — last 10 years for true ATH
        df_d = yf.download(
            yf_sym, period="10y", interval="1d",
            auto_adjust=True, progress=False
        )
        if isinstance(df_d.columns, pd.MultiIndex):
            df_d.columns = df_d.columns.get_level_values(0)
        df_d = df_d[["Open","High","Low","Close","Volume"]].dropna()

        if df_d.empty or len(df_d) < 20:
            return None

        today_str = date.today().strftime("%Y-%m-%d")

        # Previous ATH = max of High column EXCLUDING today
        df_hist = df_d[df_d.index.strftime("%Y-%m-%d") < today_str]
        if df_hist.empty:
            return None

        prev_ath       = float(df_hist["High"].max())
        prev_ath_date  = df_hist["High"].idxmax().strftime("%d %b %Y")
        last_close     = float(df_d["Close"].iloc[-1])

        # Today's intraday high from 5-min bars
        df5 = yf.download(
            yf_sym, period="1d", interval="5m",
            auto_adjust=True, progress=False
        )
        if isinstance(df5.columns, pd.MultiIndex):
            df5.columns = df5.columns.get_level_values(0)
        df5 = df5[["Open","High","Low","Close","Volume"]].dropna()

        if df5.empty:
            # Fall back to today's daily bar high
            today_rows = df_d[df_d.index.strftime("%Y-%m-%d") == today_str]
            today_high = float(today_rows["High"].max()) if not today_rows.empty else last_close
        else:
            today_high = float(df5["High"].max())

        # Check if new ATH
        if today_high <= prev_ath * (1 + ATH_BUFFER):
            return None

        # Bonus info
        above_pct   = round((today_high / prev_ath - 1) * 100, 2)
        current_px  = float(df5["Close"].iloc[-1]) if not df5.empty else last_close
        vol_today   = int(df5["Volume"].sum()) if not df5.empty else 0
        avg_vol_20d = float(df_d["Volume"].tail(20).mean())
        vol_ratio   = round(vol_today / avg_vol_20d, 1) if avg_vol_20d > 0 else None

        # RSI
        close = df_d["Close"]
        delta = close.diff().dropna()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])

        return {
            "sym":           sym,
            "yf_sym":        yf_sym,
            "current_px":    round(current_px, 2),
            "today_high":    round(today_high, 2),
            "prev_ath":      round(prev_ath, 2),
            "prev_ath_date": prev_ath_date,
            "above_pct":     above_pct,
            "rsi":           round(rsi, 1),
            "vol_ratio":     vol_ratio,
        }
    except Exception as e:
        return None


# ── HTML builder ──────────────────────────────────────────────────────────────
def build_html(alerts: list[dict], scan_time: str, total_checked: int) -> str:
    n = len(alerts)

    rows = []
    for i, a in enumerate(alerts, 1):
        vr     = a["vol_ratio"]
        vr_str = f"{vr:.1f}×" if vr else "—"
        vr_clr = "#26d07c" if (vr and vr >= 2) else "#f0b429" if (vr and vr >= 1) else "#8b949e"

        rsi    = a["rsi"]
        rsi_clr = "#ff6b6b" if rsi >= 70 else "#26d07c" if rsi >= 55 else "#f0b429"

        rows.append(f"""
<tr style="border-bottom:1px solid #21262d">
  <td style="padding:14px 10px;color:#8b949e;text-align:center">{i}</td>
  <td style="padding:14px 10px">
    <b style="font-size:15px;color:#00ff88">{a['sym']}</b>
  </td>
  <td style="padding:14px 10px;text-align:right;font-weight:700;font-size:15px;color:#e6edf3">
    ₹{a['current_px']:,.2f}
  </td>
  <td style="padding:14px 10px;text-align:right">
    <span style="background:#002d1a;color:#00ff88;border:1px solid #00ff8855;
                 border-radius:10px;padding:3px 10px;font-size:12px;font-weight:700">
      🏆 +{a['above_pct']:.2f}% above ATH
    </span>
  </td>
  <td style="padding:14px 10px;text-align:right;color:#8b949e;font-size:12px">
    ₹{a['prev_ath']:,.2f}<br>
    <span style="font-size:10px">{a['prev_ath_date']}</span>
  </td>
  <td style="padding:14px 10px;text-align:right">
    <span style="color:{rsi_clr};font-weight:700">{rsi:.1f}</span>
  </td>
  <td style="padding:14px 10px;text-align:right;color:{vr_clr}">{vr_str}</td>
</tr>""")

    rows_html = "\n".join(rows)
    if not rows_html:
        rows_html = '<tr><td colspan="7" style="padding:30px;text-align:center;color:#8b949e">No new ATH alerts</td></tr>'

    title_clr  = "#00ff88" if n > 0 else "#8b949e"
    title_text = f"🏆 {n} New ATH{'s' if n != 1 else ''} Detected!" if n > 0 else "No New ATHs"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE ATH Alert — {scan_time}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;font-size:13px}}
</style>
</head>
<body>

<!-- HEADER -->
<div style="background:linear-gradient(135deg,#010409,#002d1a);
            border-bottom:3px solid #00ff8855;padding:22px 28px 18px">
  <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">
    Real-Time ATH Alert · {scan_time}
  </div>
  <h1 style="font-size:26px;font-weight:700;color:{title_clr};letter-spacing:.5px">
    {title_text}
  </h1>
  <div style="color:#8b949e;font-size:11.5px;margin-top:5px">
    Scanned {total_checked} watchlist stocks &nbsp;·&nbsp;
    New ATH = today's intraday high &gt; all-time historical high
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
    <a href="{PAGES_BASE}/ath.html"
       style="background:#002d1a;border:1px solid #00ff8844;border-radius:20px;
              padding:4px 14px;font-size:11.5px;font-weight:600;color:#00ff88;
              text-decoration:none">🏆 Full ATH Report</a>
    <a href="{PAGES_BASE}/"
       style="background:#161b22;border:1px solid #21262d;border-radius:20px;
              padding:4px 14px;font-size:11.5px;font-weight:600;color:#e6edf3;
              text-decoration:none">📈 RSI Report</a>
    <a href="{PAGES_BASE}/intraday.html"
       style="background:#161b22;border:1px solid #21262d;border-radius:20px;
              padding:4px 14px;font-size:11.5px;font-weight:600;color:#e6edf3;
              text-decoration:none">⚡ Intraday</a>
  </div>
</div>

<!-- TABLE -->
<div style="padding:20px 28px 40px;overflow-x:auto">
  <table style="width:100%;border-collapse:collapse">
    <thead>
      <tr style="background:#010409;border-bottom:2px solid #21262d">
        <th style="padding:8px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:center">#</th>
        <th style="padding:8px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:left">Symbol</th>
        <th style="padding:8px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:right">Current Price</th>
        <th style="padding:8px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:right">ATH Break</th>
        <th style="padding:8px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:right">Prev ATH</th>
        <th style="padding:8px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:right">RSI</th>
        <th style="padding:8px 10px;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:right">Vol Ratio</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</div>

<!-- FOOTER -->
<div style="background:#010409;border-top:1px solid #21262d;padding:14px 28px;
            color:#8b949e;font-size:10.5px;line-height:1.7">
  ⚠️ For educational and research purposes only. Not SEBI-registered investment advice.<br>
  ATH comparison uses Yahoo Finance historical data (may have minor delays).<br>
  Edit <b>watchlist.txt</b> in the repository to add/remove stocks from this alert.
</div>

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    scan_time = datetime.now().strftime("%d %b %Y  %H:%M IST")
    print(f"=== NSE ATH Alert  {scan_time} ===")

    watchlist    = load_watchlist()
    fired_today  = load_fired_today()
    pending      = [s for s in watchlist if s not in fired_today]
    total_checked = len(pending)

    print(f"Watchlist: {len(watchlist)} stocks  |  Already alerted today: {len(fired_today)}  |  Checking: {total_checked}")

    alerts = []
    if pending:
        done = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(check_ath, sym): sym for sym in pending}
            for fut in as_completed(futs):
                sym = futs[fut]
                done += 1
                try:
                    r = fut.result()
                    if r:
                        alerts.append(r)
                        mark_fired(sym)
                        print(f"  🏆 NEW ATH: {sym}  +{r['above_pct']:.2f}% above prev ATH ₹{r['prev_ath']}")
                except Exception:
                    pass
                if done % 10 == 0 or done == total_checked:
                    print(f"  [{done}/{total_checked}] checked", flush=True)

    alerts.sort(key=lambda x: -x["above_pct"])

    html = build_html(alerts, scan_time, total_checked)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nResult: {len(alerts)} new ATH(s) detected")
    print(f"Written: {OUTPUT_FILE}  ({os.path.getsize(OUTPUT_FILE)/1024:.1f} KB)")

    # Signal to the workflow whether to send email
    with open("ath_alert_count.txt", "w") as f:
        f.write(str(len(alerts)))

    print("Done.")
