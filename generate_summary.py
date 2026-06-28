"""
Generate a daily HTML email summary of top NSE breakout stocks.
Reads rsi_mtf_report_NSE.html, extracts the STOCKS JSON array,
and writes email_summary.html for use by the GitHub Actions mailer.
Sector classification from india/NSE/NIFTY_Indices_Master.xlsx (All_Stocks sheet).
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

REPORT_FILE  = "rsi_mtf_report_NSE.html"
OUTPUT_FILE  = "email_summary.html"
XLSX_PATH    = "india/NSE/NIFTY_Indices_Master.xlsx"

STRONG_BUY_THRESHOLD  = 16
BUY_THRESHOLD         = 12
TOP_STRONG_BUY        = 15
TOP_BUY               = 10
MAX_SECTORS           = 14
MAX_TICKERS_PER_SECTOR = 8
OTHER_LABEL           = "Other / Smaller Cos."


# ── Data loading ─────────────────────────────────────────────────────────────

def load_stocks(report_path: str) -> list[dict]:
    with open(report_path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()
    m = re.search(r"const STOCKS\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        raise ValueError("Could not find STOCKS data array in the report HTML.")
    return json.loads(m.group(1))


def load_industry_map(xlsx_path: str) -> dict[str, str]:
    """Return {symbol: industry} from the All_Stocks sheet of NIFTY_Indices_Master.xlsx.
    Falls back to an empty dict if openpyxl/file is unavailable."""
    try:
        import openpyxl
    except ImportError:
        print("⚠️  openpyxl not installed — sector grouping will be skipped.")
        return {}
    if not os.path.exists(xlsx_path):
        print(f"⚠️  {xlsx_path} not found — sector grouping will be skipped.")
        return {}
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb["All_Stocks"]
        header_found = False
        sym_industry: dict[str, str] = {}
        for row in ws.iter_rows(min_row=1, values_only=True):
            if row[0] == "#":
                header_found = True
                continue
            if header_found and isinstance(row[0], int):
                sym      = str(row[1]).strip() if row[1] else ""
                industry = str(row[3]).strip() if row[3] else ""
                if sym and industry and sym not in sym_industry:
                    sym_industry[sym] = industry
        print(f"📋 Industry map loaded: {len(sym_industry)} symbols from {xlsx_path}")
        return sym_industry
    except Exception as e:
        print(f"⚠️  Could not load industry map: {e}")
        return {}


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_price(val) -> str:
    if val is None:
        return "—"
    try:
        return f"₹{float(val):,.2f}"
    except Exception:
        return str(val)


def fmt_pct(val, sign=True) -> str:
    if val is None:
        return "—"
    try:
        v = float(val)
        prefix = "+" if sign and v > 0 else ""
        return f"{prefix}{v:.1f}%"
    except Exception:
        return str(val)


def fmt_rsi(val) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):.1f}"
    except Exception:
        return str(val)


def signal_color(signal: str) -> str:
    s = signal.upper()
    if "STRONG BUY" in s:
        return "#0d6e1f"
    if "BUY" in s:
        return "#1a7a2e"
    if "WATCH" in s:
        return "#b45309"
    return "#7f1d1d"


def signal_bg(signal: str) -> str:
    s = signal.upper()
    if "STRONG BUY" in s:
        return "#dcfce7"
    if "BUY" in s:
        return "#d1fae5"
    if "WATCH" in s:
        return "#fef3c7"
    return "#fee2e2"


def phase_icon(phase: str) -> str:
    p = phase.upper()
    if "UPTREND" in p:
        return "📈"
    if "SIDEWAYS" in p:
        return "➡"
    return "📉"


def cap_badge(cap_cat: str) -> str:
    colors = {
        "Large Cap": ("#1e40af", "#dbeafe"),
        "Mid Cap":   ("#5b21b6", "#ede9fe"),
        "Small Cap": ("#92400e", "#fef3c7"),
        "Micro Cap": ("#991b1b", "#fee2e2"),
    }
    fg, bg = colors.get(cap_cat, ("#374151", "#f3f4f6"))
    return (
        f'<span style="display:inline-block;padding:1px 6px;border-radius:4px;'
        f'font-size:11px;font-weight:600;color:{fg};background:{bg};">'
        f"{cap_cat}</span>"
    )


def sector_icon(industry: str) -> str:
    s = industry.upper()
    if any(k in s for k in ("BANK", "FINANCIAL SERV", "NBFC", "INSURANCE")):
        return "🏦"
    if any(k in s for k in ("INFORMATION TECH", "SOFTWARE", " IT ")):
        return "💻"
    if any(k in s for k in ("PHARMA", "DRUG", "MEDICINE", "BIOTECH")):
        return "💊"
    if "HEALTHCARE" in s or "HEALTH CARE" in s:
        return "🏥"
    if any(k in s for k in ("AUTO", "VEHICLE", "MOTOR")):
        return "🚗"
    if any(k in s for k in ("CAPITAL GOODS",)):
        return "⚙️"
    if any(k in s for k in ("POWER", "SOLAR", "RENEW", "ENERGY")):
        return "⚡"
    if any(k in s for k in ("OIL", "GAS", "PETRO", "REFIN", "CONSUMABLE FUEL")):
        return "🛢️"
    if any(k in s for k in ("METAL", "STEEL", "IRON", "COPPER", "ALUMIN", "MINING")):
        return "⛏️"
    if any(k in s for k in ("REALTY", "REAL ESTATE")):
        return "🏠"
    if any(k in s for k in ("CONSTRUCT",)):
        return "🏗️"
    if any(k in s for k in ("FMCG", "FAST MOVING CONSUMER")):
        return "🛒"
    if "CONSUMER DURABLES" in s:
        return "📺"
    if any(k in s for k in ("CONSUMER SERVICES",)):
        return "☕"
    if any(k in s for k in ("SERVICES",)):
        return "🔧"
    if any(k in s for k in ("CHEMICAL", "FERTILISER", "AGROCH")):
        return "🧪"
    if any(k in s for k in ("TELECOM", "COMMUNICATION")):
        return "📡"
    if any(k in s for k in ("MEDIA", "ENTERTAINMENT", "PUBLICATION")):
        return "🎬"
    if any(k in s for k in ("AGRI", "FARM", "SEED", "SUGAR", "TEXTILE", "DIVERSIF")):
        return "🌾"
    return "📊"


def strength_bar(count: int, max_count: int) -> str:
    pct = int((count / max_count) * 100) if max_count else 0
    color = "#166534" if pct >= 60 else ("#1d4ed8" if pct >= 30 else "#6b7280")
    return (
        f'<div style="background:#e5e7eb;border-radius:4px;height:6px;'
        f'width:80px;display:inline-block;vertical-align:middle;">'
        f'<div style="background:{color};border-radius:4px;height:6px;width:{pct}%;"></div>'
        f'</div>'
    )


# ── Table builder ─────────────────────────────────────────────────────────────

def stock_row(s: dict, rank: int, bg: str) -> str:
    ticker       = s.get("ticker", "")
    company      = s.get("company", ticker)
    company_short = company[:28] + "…" if len(company) > 28 else company
    score        = s.get("score", 0)
    signal       = re.sub(r"[^\w\s]", "", s.get("signal", "")).strip()
    phase        = s.get("phase", "")
    close        = fmt_price(s.get("close"))
    dist52       = fmt_pct(s.get("dist52"), sign=False)
    cap_cat      = s.get("cap_cat", "Unknown")
    sector       = s.get("sector") or "—"
    rsi_d        = fmt_rsi(s.get("rsi_d"))
    rsi_w        = fmt_rsi(s.get("rsi_w"))
    rsi_m        = fmt_rsi(s.get("rsi_m"))
    rank_univ    = s.get("rank_univ_pos", "—")
    rank_of      = s.get("rank_univ_of", "—")
    fresh_d      = " 🔥" if s.get("fresh_d") else ""
    fresh_w      = " ⚡" if s.get("fresh_w") else ""
    sig_color    = signal_color(signal)
    sig_bg       = signal_bg(signal)
    cell         = "padding:8px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;"
    num_cell     = cell + "text-align:center;"
    return f"""
    <tr style="background:{bg};">
      <td style="{num_cell}color:#6b7280;">{rank}</td>
      <td style="{cell}">
        <strong style="font-size:14px;">{ticker}</strong>{fresh_d}{fresh_w}<br>
        <span style="color:#6b7280;font-size:11px;">{company_short}</span>
      </td>
      <td style="{num_cell}">
        <span style="font-size:15px;font-weight:700;color:#111;">{score}/21</span>
      </td>
      <td style="{cell}">
        <span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;
          font-weight:700;color:{sig_color};background:{sig_bg};">{signal}</span>
      </td>
      <td style="{num_cell}">{phase_icon(phase)}</td>
      <td style="{num_cell};font-weight:600;">{close}</td>
      <td style="{num_cell};color:{'#b91c1c' if '-' in dist52 else '#15803d'};">{dist52}</td>
      <td style="{cell}">{cap_badge(cap_cat)}</td>
      <td style="{num_cell};">{rsi_d} / {rsi_w} / {rsi_m}</td>
      <td style="{num_cell};color:#6b7280;font-size:12px;">{rank_univ}/{rank_of}</td>
      <td style="{cell};color:#6b7280;font-size:11px;">{sector[:22]}</td>
    </tr>"""


def build_table(stocks: list[dict], title: str, header_color: str) -> str:
    header_style = (
        f"background:{header_color};color:#fff;padding:8px 10px;"
        "font-size:12px;font-weight:600;text-align:center;"
        "border-bottom:2px solid rgba(0,0,0,0.15);"
    )
    rows_html = ""
    for i, s in enumerate(stocks):
        bg = "#ffffff" if i % 2 == 0 else "#f9fafb"
        rows_html += stock_row(s, i + 1, bg)

    return f"""
  <div style="margin-bottom:28px;">
    <h2 style="margin:0 0 10px;font-size:17px;color:#111;">{title}
      <span style="font-size:13px;font-weight:400;color:#6b7280;">({len(stocks)} stocks)</span>
    </h2>
    <table style="width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden;
                  box-shadow:0 1px 3px rgba(0,0,0,0.1);">
      <thead>
        <tr>
          <th style="{header_style}">#</th>
          <th style="{header_style};text-align:left;">Ticker</th>
          <th style="{header_style}">Score</th>
          <th style="{header_style}">Signal</th>
          <th style="{header_style}">Phase</th>
          <th style="{header_style}">Close</th>
          <th style="{header_style}">52W%</th>
          <th style="{header_style};text-align:left;">Cap</th>
          <th style="{header_style}">D/W/M RSI</th>
          <th style="{header_style}">Rank</th>
          <th style="{header_style};text-align:left;">Sector</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>"""


# ── Sector breakdown ──────────────────────────────────────────────────────────

def build_sector_breakdown(
    strong_buy: list[dict],
    buy_only: list[dict],
    industry_map: dict[str, str],
) -> str:
    """Sector cards using NIFTY industry classification from the XLSX master file."""

    # Group by industry
    sector_data: dict[str, dict] = defaultdict(lambda: {"sb": [], "buy": []})
    for s in strong_buy:
        ind = industry_map.get(s["ticker"], OTHER_LABEL)
        sector_data[ind]["sb"].append(s)
    for s in buy_only:
        ind = industry_map.get(s["ticker"], OTHER_LABEL)
        sector_data[ind]["buy"].append(s)

    # Sort: most Strong Buy first, then total, then name (put "Other" last)
    def sort_key(item):
        label, d = item
        return (label == OTHER_LABEL, -len(d["sb"]), -len(d["buy"]))

    sorted_sectors = sorted(sector_data.items(), key=sort_key)

    if not sorted_sectors:
        return ""

    # Re-include Other if it was cut; always append it at the end
    other = sector_data.get(OTHER_LABEL)
    has_other_in_list = any(label == OTHER_LABEL for label, _ in sorted_sectors)
    if other and not has_other_in_list:
        sorted_sectors.append((OTHER_LABEL, other))

    max_sb = max((len(d["sb"]) for _, d in sorted_sectors), default=1) or 1

    classified_sb  = sum(len(d["sb"])  for lbl, d in sorted_sectors if lbl != OTHER_LABEL)
    classified_buy = sum(len(d["buy"]) for lbl, d in sorted_sectors if lbl != OTHER_LABEL)

    cards_html = ""
    for industry, d in sorted_sectors:
        sb_list   = d["sb"]
        buy_list  = d["buy"]
        sb_count  = len(sb_list)
        buy_count = len(buy_list)
        avg_score = (
            sum(x.get("score", 0) for x in sb_list + buy_list) / (sb_count + buy_count)
            if (sb_count + buy_count) else 0
        )
        icon = sector_icon(industry)

        # Ticker chips — strong buy first, sorted by score within tier
        top_stocks = (
            sorted(sb_list,  key=lambda x: -x.get("score", 0)) +
            sorted(buy_list, key=lambda x: -x.get("score", 0))
        )[:MAX_TICKERS_PER_SECTOR]

        chips = ""
        for s in top_stocks:
            is_sb    = s in sb_list
            chip_bg  = "#dcfce7" if is_sb else "#dbeafe"
            chip_fg  = "#166534" if is_sb else "#1e40af"
            fresh    = "🔥" if s.get("fresh_d") else ("⚡" if s.get("fresh_w") else "")
            chips += (
                f'<span style="display:inline-block;margin:2px 3px 2px 0;padding:2px 7px;'
                f'border-radius:4px;font-size:11px;font-weight:600;color:{chip_fg};background:{chip_bg};">'
                f'{s["ticker"]}{fresh}</span>'
            )

        overflow = sb_count + buy_count - MAX_TICKERS_PER_SECTOR
        if overflow > 0:
            chips += (
                f'<span style="display:inline-block;margin:2px 3px;padding:2px 7px;'
                f'border-radius:4px;font-size:11px;color:#6b7280;background:#f3f4f6;">'
                f'+{overflow} more</span>'
            )

        # Border intensity: green if sb ≥ 5, blue if ≥ 2, grey otherwise
        if industry == OTHER_LABEL:
            border_color = "#9ca3af"
        elif sb_count >= 5:
            border_color = "#16a34a"
        elif sb_count >= 2:
            border_color = "#3b82f6"
        else:
            border_color = "#e5e7eb"

        label_display = industry if industry != OTHER_LABEL else "Other / Smaller Cos."

        cards_html += f"""
    <div style="background:#fff;border:1px solid {border_color};border-top:3px solid {border_color};
                border-radius:8px;padding:14px 16px;min-width:230px;flex:1 1 230px;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <span style="font-size:14px;font-weight:700;color:#111;">{icon} {label_display}</span>
        <span style="font-size:11px;color:#6b7280;">avg {avg_score:.1f}/21</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
        <span style="font-size:12px;font-weight:700;color:#166534;background:#dcfce7;
                     padding:2px 8px;border-radius:4px;">🚀 {sb_count} SB</span>
        <span style="font-size:12px;font-weight:600;color:#1d4ed8;background:#dbeafe;
                     padding:2px 8px;border-radius:4px;">✅ {buy_count} Buy</span>
        <span style="margin-left:4px;">{strength_bar(sb_count, max_sb)}</span>
      </div>
      <div style="line-height:1.9;">{chips}</div>
    </div>"""

    nifty_matched = classified_sb + classified_buy
    total_shown   = len(strong_buy) + len(buy_only)
    source_note   = (
        f"Sector via NIFTY Indices Master ({nifty_matched} of {total_shown} stocks classified)"
        if industry_map else "Sector classification unavailable"
    )

    return f"""
  <div style="margin-bottom:28px;">
    <h2 style="margin:0 0 6px;font-size:17px;color:#111;">🗂️ Sector Breakdown
      <span style="font-size:13px;font-weight:400;color:#6b7280;">
        — Strong Buy &amp; Buy grouped by NIFTY industry
      </span>
    </h2>
    <p style="margin:0 0 14px;font-size:12px;color:#6b7280;">
      {source_note} &nbsp;|&nbsp;
      🔥 fresh daily crossover &nbsp;⚡ fresh weekly &nbsp;|&nbsp;
      Green chip = Strong Buy &nbsp; Blue = Buy &nbsp;|&nbsp;
      Green border = hottest sector
    </p>
    <div style="display:flex;flex-wrap:wrap;gap:12px;">
      {cards_html}
    </div>
  </div>"""


# ── Email builder ─────────────────────────────────────────────────────────────

EXPLOSIVE_MIN_SCORE   = 5
EXPLOSIVE_MIN_RSI     = 40
TOP_EXPLOSIVE         = 10


def build_explosive_breakouts(stocks: list[dict]) -> str:
    """
    Build an orange-themed email section for MTAR/HFCL/Adani-type explosive breakouts.
    Filter: rsi_d > 40 AND explosive_score >= EXPLOSIVE_MIN_SCORE.
    Returns an HTML string (empty string if no candidates).
    """
    candidates = [
        s for s in stocks
        if s.get("rsi_d", 0) > EXPLOSIVE_MIN_RSI
        and s.get("explosive_score", 0) >= EXPLOSIVE_MIN_SCORE
    ]
    if not candidates:
        return ""

    candidates = sorted(candidates, key=lambda x: (
        x.get("explosive_score", 0),
        x.get("vol_ratio", 0),
    ), reverse=True)[:TOP_EXPLOSIVE]

    hdr_style = (
        "background:#92400e;color:#fff;padding:6px 10px;"
        "font-size:11px;font-weight:700;text-align:center;"
        "border-bottom:1px solid rgba(0,0,0,0.2);"
    )
    rows_html = ""
    for i, s in enumerate(candidates):
        bg = "#fff7ed" if i % 2 == 0 else "#fef3c7"
        ticker     = s.get("ticker", "")
        company    = s.get("company", "")[:20]
        close      = fmt_price(s.get("close"))
        escore     = s.get("explosive_score", 0)
        vol_ratio  = s.get("vol_ratio", 0)
        rsi_d      = s.get("rsi_d", 0)
        bb_pct     = s.get("bb_pct", 0)
        mfi        = s.get("mfi", 0)
        cci_200    = s.get("cci_200", 0)
        signals    = s.get("explosive_signals", [])

        # Fib extension targets (127.2% and 161.8%) from fib_levels
        fib_targets = ""
        fib_lvls = s.get("fib_levels", [])
        fib_type = s.get("fib_type", "")
        fib_base = s.get("fib_base", 0) or 0
        if fib_lvls and fib_type == "extension":
            ext_map = {r: p for r, p in fib_lvls}
            t1 = ext_map.get(1.272) or ext_map.get(1.27)
            t2 = ext_map.get(1.618)
            parts = []
            if t1: parts.append(f"<span style='color:#b45309;'>127%→₹{t1:.0f}</span>")
            if t2: parts.append(f"<span style='color:#92400e;font-weight:700;'>162%→₹{t2:.0f}</span>")
            if parts: fib_targets = " &nbsp; ".join(parts)
        if not fib_targets:
            fib_targets = "<span style='color:#9ca3af;font-size:10px;'>—</span>"

        # Score bar
        score_pct = int(escore / 12 * 100)
        score_color = "#dc2626" if escore >= 9 else "#ea580c" if escore >= 6 else "#f59e0b"
        score_bar = (
            f'<div style="display:inline-block;width:60px;height:8px;'
            f'background:#e5e7eb;border-radius:4px;vertical-align:middle;">'
            f'<div style="width:{score_pct}%;height:100%;background:{score_color};'
            f'border-radius:4px;"></div></div>'
        )

        # Signal chips
        sig_chips = " ".join(
            f'<span style="display:inline-block;padding:1px 5px;border-radius:3px;'
            f'font-size:10px;background:#fef3c7;color:#78350f;border:1px solid #fcd34d;">'
            f'{sig}</span>'
            for sig in signals[:4]
        )

        bb_color = "#dc2626" if bb_pct and bb_pct > 100 else "#374151"
        rows_html += f"""
    <tr style="background:{bg};">
      <td style="padding:6px 8px;font-weight:700;font-size:13px;color:#92400e;">{ticker}</td>
      <td style="padding:6px 8px;font-size:12px;color:#374151;">{company}</td>
      <td style="padding:6px 8px;text-align:right;font-weight:600;">₹{close}</td>
      <td style="padding:6px 8px;text-align:center;">{score_bar}
        <div style="font-size:11px;font-weight:700;color:{score_color};">{escore}/12</div></td>
      <td style="padding:6px 8px;text-align:center;font-weight:600;color:#ea580c;">{vol_ratio:.1f}x</td>
      <td style="padding:6px 8px;text-align:center;color:#1d4ed8;">{rsi_d:.0f}</td>
      <td style="padding:6px 8px;text-align:center;color:{bb_color};font-weight:600;">{bb_pct:.0f}%</td>
      <td style="padding:6px 8px;text-align:center;color:#7c3aed;">{mfi:.0f}</td>
      <td style="padding:6px 8px;text-align:center;color:{'#166534' if cci_200 > 0 else '#b91c1c'};">{cci_200:.0f}</td>
      <td style="padding:6px 8px;font-size:11px;">{fib_targets}</td>
      <td style="padding:6px 8px;font-size:10px;">{sig_chips}</td>
    </tr>"""

    return f"""
  <div style="margin-bottom:28px;">
    <h2 style="margin:0 0 10px;font-size:17px;color:#92400e;">
      💥 Explosive Daily Breakouts
      <span style="font-size:13px;font-weight:400;color:#6b7280;">
        ({len(candidates)} stocks · score ≥{EXPLOSIVE_MIN_SCORE}/12 · RSI&gt;{EXPLOSIVE_MIN_RSI})
      </span>
    </h2>
    <p style="margin:0 0 10px;font-size:12px;color:#6b7280;">
      MTAR / HFCL / Adani-type setups: volume surge + BB breakout + MACD acceleration + institutional MFI.
      BB% &gt; 100 = trading above upper Bollinger Band. Fib targets = 127.2% &amp; 161.8% extensions.
    </p>
    <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;
                  border:1px solid #fcd34d;border-radius:8px;overflow:hidden;">
      <thead>
        <tr>
          <th style="{hdr_style}">Ticker</th>
          <th style="{hdr_style}">Company</th>
          <th style="{hdr_style}">Close</th>
          <th style="{hdr_style}">Explosive Score</th>
          <th style="{hdr_style}">Vol Ratio</th>
          <th style="{hdr_style}">RSI-D</th>
          <th style="{hdr_style}">BB%</th>
          <th style="{hdr_style}">MFI</th>
          <th style="{hdr_style}">CCI(200)</th>
          <th style="{hdr_style}">Fib Targets</th>
          <th style="{hdr_style}">Signals</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>
  </div>"""


def build_email(
    stocks: list[dict],
    pages_url: str,
    run_number: str,
    industry_map: dict[str, str],
) -> str:
    today   = datetime.now().strftime("%d %b %Y")

    sorted_stocks = sorted(
        stocks,
        key=lambda x: (x.get("score", 0), -(x.get("rank_univ_pos") or 9999)),
        reverse=True,
    )

    strong_buy  = [s for s in sorted_stocks if s.get("score", 0) >= STRONG_BUY_THRESHOLD]
    buy_only    = [s for s in sorted_stocks if BUY_THRESHOLD <= s.get("score", 0) < STRONG_BUY_THRESHOLD]
    watch       = [s for s in sorted_stocks if 8 <= s.get("score", 0) < BUY_THRESHOLD]
    fresh_d_sb  = [s for s in strong_buy if s.get("fresh_d")]

    total         = len(stocks)
    uptrend_count = sum(1 for s in stocks if "UPTREND" in s.get("phase", "").upper())

    sb_table          = build_table(strong_buy, "🚀 Strong Buy Setups", "#166534")
    buy_table         = build_table(buy_only,  "✅ Buy Setups",        "#1d4ed8")
    sector_section    = build_sector_breakdown(strong_buy, buy_only, industry_map)
    explosive_section = build_explosive_breakouts(sorted_stocks)
    explosive_count   = sum(
        1 for s in stocks
        if s.get("rsi_d", 0) > EXPLOSIVE_MIN_RSI
        and s.get("explosive_score", 0) >= EXPLOSIVE_MIN_SCORE
    )

    # Fresh breakout callout
    fresh_items = ""
    for s in fresh_d_sb[:5]:
        fresh_items += (
            f'<li style="margin-bottom:6px;">'
            f'<strong>{s["ticker"]}</strong> — {s.get("company", "")} '
            f'<span style="color:#166534;font-weight:600;">Score {s.get("score")}/21</span> '
            f'@ {fmt_price(s.get("close"))}'
            f'</li>'
        )

    fresh_section = ""
    if fresh_items:
        fresh_section = f"""
    <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:14px 18px;
                margin-bottom:24px;border-radius:0 8px 8px 0;">
      <h3 style="margin:0 0 8px;font-size:15px;color:#166534;">🔥 Fresh Daily Breakouts (Strong Buy)</h3>
      <ul style="margin:0;padding-left:18px;color:#166534;font-size:13px;">
        {fresh_items}
      </ul>
    </div>"""

    pages_btn = ""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:980px;margin:24px auto;background:#fff;border-radius:12px;
              box-shadow:0 4px 12px rgba(0,0,0,0.08);overflow:hidden;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);
                padding:28px 32px;color:#fff;">
      <h1 style="margin:0 0 6px;font-size:22px;letter-spacing:0.5px;">
        📈 NSE Daily Momentum Breakout Report
      </h1>
      <p style="margin:0;opacity:0.75;font-size:14px;">{today} &nbsp;|&nbsp; Run #{run_number}</p>
    </div>

    <!-- Market Summary -->
    <div style="padding:20px 32px;background:#f8fafc;border-bottom:1px solid #e5e7eb;">
      <h2 style="margin:0 0 14px;font-size:15px;color:#374151;">Market Summary</h2>
      <div style="display:flex;gap:16px;flex-wrap:wrap;">
        {_stat_card("Stocks Scanned",    total,          "#374151")}
        {_stat_card("Uptrend",           uptrend_count,  "#15803d")}
        {_stat_card("Strong Buy (≥16)",  len(strong_buy),    "#166534")}
        {_stat_card("Buy (12–15)",        len(buy_only),      "#1d4ed8")}
        {_stat_card("Watch (8–11)",       len(watch),         "#b45309")}
        {_stat_card("Fresh Breakouts",   len(fresh_d_sb),    "#7c3aed")}
        {_stat_card("💥 Explosive",       explosive_count,    "#92400e")}
      </div>
    </div>

    <!-- Body -->
    <div style="padding:24px 32px;">
      {fresh_section}
      {sb_table}
      {buy_table}
      {explosive_section}
      {sector_section}

      <!-- Legend -->
      <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;
                  padding:14px 18px;margin-bottom:24px;font-size:12px;color:#6b7280;">
        <strong style="color:#374151;">Legend:</strong>
        &nbsp; 🔥 Fresh Daily Breakout &nbsp;|&nbsp; ⚡ Fresh Weekly Breakout
        &nbsp;|&nbsp; 52W% = Distance from 52-week high
        &nbsp;|&nbsp; D/W/M RSI = Daily / Weekly / Monthly RSI(14)
        &nbsp;|&nbsp; Rank = Universe rank (lower = stronger)
      </div>

    </div>

    <!-- Footer -->
    <div style="padding:16px 32px;background:#f8fafc;border-top:1px solid #e5e7eb;
                font-size:12px;color:#9ca3af;text-align:center;">
      NSE RSI Multi-Timeframe Breakout Scanner &nbsp;|&nbsp;
      Auto-generated by GitHub Actions &nbsp;|&nbsp; Not financial advice
    </div>
  </div>
</body>
</html>"""


def _stat_card(label: str, value, color: str) -> str:
    return (
        f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;'
        f'padding:12px 18px;min-width:110px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:700;color:{color};">{value}</div>'
        f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">{label}</div>'
        f'</div>'
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    pages_url  = os.environ.get("PAGES_URL", "")
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "—")

    if not os.path.exists(REPORT_FILE):
        print(f"❌ {REPORT_FILE} not found — skipping summary generation.")
        sys.exit(1)

    print(f"📖 Loading stocks from {REPORT_FILE}...")
    stocks = load_stocks(REPORT_FILE)
    print(f"✅ Loaded {len(stocks)} stocks")

    industry_map = load_industry_map(XLSX_PATH)

    print("📧 Building email summary with sector breakdown...")
    html = build_email(stocks, pages_url, run_number, industry_map)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    strong_buy_count = sum(1 for s in stocks if s.get("score", 0) >= STRONG_BUY_THRESHOLD)
    print(f"✅ Email summary written to {OUTPUT_FILE}")
    print(f"   Strong Buy: {strong_buy_count} | Total: {len(stocks)}")

    today   = datetime.now().strftime("%d %b %Y")
    subject = f"📈 NSE Breakout Report {today} — {strong_buy_count} Strong Buy setups"
    with open("email_subject.txt", "w") as f:
        f.write(subject)
    print(f"   Subject: {subject}")


if __name__ == "__main__":
    main()
