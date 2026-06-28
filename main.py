import os
import glob
import subprocess
import sys
import threading
import time
import logging
from datetime import datetime

import pytz
from flask import Flask, send_file, abort, Response, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

SCRIPT = 'rsi_mtf_report_nse.py'
IST = pytz.timezone('Asia/Kolkata')

job_lock = threading.Lock()
job_running = False
job_started_at = None


def latest_report():
    files = glob.glob('rsi_mtf_report_NSE*.html') + glob.glob('rsi_mtf_report_*.html')
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def run_nse_report():
    global job_running, job_started_at
    with job_lock:
        if job_running:
            log.info('NSE report already running, skipping.')
            return
        job_running = True
        job_started_at = datetime.now(IST)
    try:
        now = job_started_at.strftime('%Y-%m-%d %H:%M IST')
        log.info(f'Starting NSE RSI MTF report at {now}')
        result = subprocess.run(
            [sys.executable, SCRIPT],
            capture_output=True, text=True, timeout=7200
        )
        if result.returncode == 0:
            log.info('NSE report completed successfully.')
        else:
            log.error(f'NSE report failed:\n{result.stderr[-1000:]}')
    except subprocess.TimeoutExpired:
        log.error('NSE report timed out after 2 hours.')
    except Exception as e:
        log.error(f'NSE report error: {e}')
    finally:
        with job_lock:
            job_running = False


_COMMON_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         min-height: 100vh; padding: 24px; }
  h1  { color: #38bdf8; font-size: 1.7rem; margin-bottom: 10px; text-align: center; }
  p   { color: #94a3b8; font-size: 0.95rem; max-width: 500px; text-align: center; line-height: 1.65; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 14px;
          padding: 20px 32px; margin-top: 24px; text-align: center; max-width: 520px; width: 100%; }
  .card .label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
  .card strong { color: #38bdf8; }
  .spinner { width: 44px; height: 44px; border: 4px solid #1e293b; border-top-color: #38bdf8;
             border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 28px; }
  .spinner.amber { border-top-color: #fbbf24; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .btn { display: inline-flex; align-items: center; gap: 8px; margin-top: 18px;
         background: #38bdf8; color: #0f172a; border: none; border-radius: 24px;
         padding: 10px 26px; font-size: 0.95rem; font-weight: 700; cursor: pointer;
         transition: background .15s, transform .1s; text-decoration: none; }
  .btn:hover  { background: #7dd3fc; }
  .btn:active { transform: scale(.97); }
  .btn:disabled { background: #334155; color: #64748b; cursor: not-allowed; }
  .btn.outline { background: transparent; border: 1px solid #475569; color: #94a3b8; }
  .btn.outline:hover { border-color: #38bdf8; color: #38bdf8; background: #001d2e; }
  .nav { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; margin-top: 20px; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                background: #fbbf24; animation: pulse 1.2s infinite; margin-right: 6px; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  #msg { margin-top: 12px; font-size: 0.85rem; color: #64748b; min-height: 20px; }
"""

WAITING_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Free NSE stock analysis tool with multi-timeframe RSI/SMA crossover reports, Phase detection, and Nifty50 ranking.">
<title>Indian Stock Market Toolkit</title>
<style>{_COMMON_CSS}</style>
</head>
<body>
  <div class="spinner"></div>
  <h1>📈 Indian Stock Market Toolkit</h1>
  <p>No report has been generated yet. The report runs automatically every day at
     <strong>5:00 AM IST</strong>, or you can trigger it manually below.</p>

  <div class="card">
    <div class="label">Manual trigger</div>
    <button class="btn" id="runBtn" onclick="triggerRun()">▶ Run Report Now</button>
    <div id="msg"></div>
  </div>

  <div class="nav">
    <a class="btn outline" href="/ath">🏆 ATH Breakout</a>
    <a class="btn outline" href="/rocket">🚀 Rocket Scanner</a>
  </div>

<script>
async function triggerRun() {{
  const btn = document.getElementById('runBtn');
  const msg = document.getElementById('msg');
  btn.disabled = true;
  btn.textContent = '⏳ Starting…';
  try {{
    const r = await fetch('/run', {{method:'POST'}});
    const d = await r.json();
    if (d.started) {{
      msg.innerHTML = '<span class="status-dot"></span>Report is generating — this page will reload when ready.';
      btn.textContent = '⏳ Running…';
      pollStatus();
    }} else {{
      msg.textContent = d.message || 'Already running…';
      pollStatus();
    }}
  }} catch(e) {{
    msg.textContent = 'Error — check server logs.';
    btn.disabled = false;
    btn.textContent = '▶ Run Report Now';
  }}
}}

async function pollStatus() {{
  const r = await fetch('/status');
  const d = await r.json();
  if (!d.running) {{
    location.reload();
  }} else {{
    setTimeout(pollStatus, 5000);
  }}
}}
</script>
</body>
</html>"""

RUNNING_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Indian Stock Market Toolkit — Generating…</title>
<style>{_COMMON_CSS}</style>
</head>
<body>
  <div class="spinner amber"></div>
  <h1>⏳ Generating Report…</h1>
  <p>The NSE RSI MTF report is running now. This typically takes <strong>15–60 minutes</strong>.<br>
     This page polls every 10 seconds and will reload automatically when done.</p>

  <div class="card">
    <div class="label">Status</div>
    <span class="status-dot"></span><strong id="elapsed">Calculating…</strong>
    <div id="msg" style="margin-top:8px;color:#64748b;font-size:0.85rem">Fetching market data and computing RSI…</div>
  </div>

  <div class="nav">
    <a class="btn outline" href="/ath">🏆 ATH Breakout</a>
    <a class="btn outline" href="/rocket">🚀 Rocket Scanner</a>
  </div>

<script>
const startTime = Date.now();
function updateElapsed() {{
  const s = Math.floor((Date.now() - startTime) / 1000);
  const m = Math.floor(s / 60), sec = s % 60;
  document.getElementById('elapsed').textContent =
    (m > 0 ? m + 'm ' : '') + sec + 's elapsed';
}}
setInterval(updateElapsed, 1000);

async function poll() {{
  try {{
    const r = await fetch('/status');
    const d = await r.json();
    if (!d.running) {{ location.reload(); return; }}
  }} catch(e) {{}}
  setTimeout(poll, 10000);
}}
setTimeout(poll, 10000);
</script>
</body>
</html>"""


META_DESC = '<meta name="description" content="Daily NSE stock analysis with multi-timeframe RSI/SMA crossover reports, Phase detection, and Nifty50 ranking for Indian equity markets.">'


@app.route('/')
def index():
    if job_running:
        return Response(RUNNING_HTML, mimetype='text/html')
    report = latest_report()
    if report:
        with open(report, 'r', encoding='utf-8', errors='replace') as f:
            html = f.read()
        if META_DESC not in html:
            html = html.replace('<head>', '<head>\n' + META_DESC, 1)
        return Response(html, mimetype='text/html')
    return Response(WAITING_HTML, mimetype='text/html')


@app.route('/run', methods=['POST'])
def run():
    global job_running
    if job_running:
        return jsonify({'started': False, 'message': 'Report is already running.'})
    t = threading.Thread(target=run_nse_report, daemon=True)
    t.start()
    log.info('Report triggered manually via /run endpoint.')
    return jsonify({'started': True, 'message': 'Report generation started.'})


@app.route('/status')
def status():
    elapsed = None
    if job_running and job_started_at:
        elapsed = int((datetime.now(IST) - job_started_at).total_seconds())
    report = latest_report()
    report_time = None
    if report:
        report_time = datetime.fromtimestamp(os.path.getmtime(report), IST).strftime('%Y-%m-%d %H:%M IST')
    return jsonify({
        'running': job_running,
        'elapsed_seconds': elapsed,
        'latest_report': report,
        'report_generated_at': report_time,
    })


@app.route('/multibagger')
def multibagger():
    path = 'multibagger_report.html'
    if not os.path.exists(path):
        return Response(
            '<html><body style="font-family:Segoe UI,sans-serif;padding:40px;background:#0f172a;color:#e2e8f0;">'
            '<h2 style="color:#38bdf8;">💎 Multibagger Report</h2>'
            '<p style="color:#94a3b8;">No multibagger report found yet. Run <code>python multibagger_report.py</code> to generate it.</p>'
            '<a href="/" style="color:#38bdf8;">← Back to Full Report</a>'
            '</body></html>',
            mimetype='text/html'
        )
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return Response(f.read(), mimetype='text/html')


@app.route('/ath')
def ath():
    path = 'ath_report_NSE.html'
    if not os.path.exists(path):
        return Response(
            '<html><body style="font-family:Segoe UI,sans-serif;padding:40px;background:#0f172a;color:#e2e8f0;">'
            '<h2 style="color:#38bdf8;">🏆 ATH Breakout Report</h2>'
            '<p style="color:#94a3b8;">No ATH report yet — will be generated during the next daily run.</p>'
            '<a href="/" style="color:#38bdf8;">← Back to Full Report</a>'
            '</body></html>',
            mimetype='text/html'
        )
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return Response(f.read(), mimetype='text/html')


@app.route('/rocket')
def rocket():
    path = 'rocket_scan_latest.html'
    if not os.path.exists(path):
        return Response(
            '<html><body style="font-family:Segoe UI,sans-serif;padding:40px;background:#0f172a;color:#e2e8f0;">'
            '<h2 style="color:#38bdf8;">🚀 Rocket Scanner</h2>'
            '<p style="color:#94a3b8;">No rocket scan yet. Run <code>python rocket_scanner.py</code> to generate.</p>'
            '<a href="/" style="color:#38bdf8;">← Back to Full Report</a>'
            '</body></html>',
            mimetype='text/html'
        )
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return Response(f.read(), mimetype='text/html')


@app.route('/intraday')
def intraday():
    path = 'intraday_report_NSE.html'
    if not os.path.exists(path):
        return Response(
            '<html><body style="font-family:Segoe UI,sans-serif;padding:40px;background:#0f172a;color:#e2e8f0;">'
            '<h2 style="color:#38bdf8;">⚡ Intraday Breakout Scanner</h2>'
            '<p style="color:#94a3b8;">No intraday report yet. Run <code>python intraday_report.py</code> to generate.</p>'
            '<a href="/" style="color:#38bdf8;">← Back to Full Report</a>'
            '</body></html>',
            mimetype='text/html'
        )
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return Response(f.read(), mimetype='text/html')


@app.route('/charts/<path:filename>')
def charts(filename):
    path = os.path.join('charts', filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


if __name__ == '__main__':
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(run_nse_report, 'cron', hour=5, minute=0)
    scheduler.start()
    log.info('Scheduler started — NSE report will run daily at 5:00 AM IST.')

    if not latest_report():
        log.info('No existing report found — generating first report now...')
        t = threading.Thread(target=run_nse_report, daemon=True)
        t.start()

    app.run(host='0.0.0.0', port=5000, debug=False)
