import os
import glob
import subprocess
import sys
import threading
import time
import logging
from datetime import datetime

import pytz
from flask import Flask, send_file, abort, Response
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

SCRIPT = 'rsi_mtf_report_nse.py'
IST = pytz.timezone('Asia/Kolkata')

job_lock = threading.Lock()
job_running = False


def latest_report():
    files = glob.glob('rsi_mtf_report_NSE*.html') + glob.glob('rsi_mtf_report_*.html')
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def run_nse_report():
    global job_running
    with job_lock:
        if job_running:
            log.info('NSE report already running, skipping.')
            return
        job_running = True
    try:
        now = datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')
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


WAITING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Free NSE stock analysis tool with multi-timeframe RSI/SMA crossover reports, Phase detection, and Nifty50 ranking. Run live scans for Indian equity markets.">
<title>Indian Stock Market Toolkit</title>
<style>
  body { margin: 0; font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; }
  h1 { color: #38bdf8; font-size: 1.8rem; margin-bottom: 12px; }
  p  { color: #94a3b8; font-size: 1rem; max-width: 480px; text-align: center; line-height: 1.6; }
  .badge { margin-top: 28px; background: #1e293b; border: 1px solid #334155; border-radius: 10px;
           padding: 16px 28px; font-size: 0.9rem; color: #64748b; }
  .badge strong { color: #38bdf8; }
  .spinner { width: 40px; height: 40px; border: 4px solid #1e293b; border-top-color: #38bdf8;
             border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 28px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
<script>setTimeout(() => location.reload(), 60000);</script>
</head>
<body>
  <div class="spinner"></div>
  <h1>📈 Indian Stock Market Toolkit</h1>
  <p>The NSE RSI Multi-Timeframe report runs automatically every day at <strong>5:00 AM IST</strong>.<br>
     No report has been generated yet — check back after the first scheduled run.</p>
  <div class="badge">Next run: <strong>5:00 AM IST</strong> &nbsp;|&nbsp; This page refreshes automatically every minute.</div>
</body>
</html>"""

RUNNING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Free NSE stock analysis tool with multi-timeframe RSI/SMA crossover reports, Phase detection, and Nifty50 ranking. Run live scans for Indian equity markets.">
<title>Indian Stock Market Toolkit — Generating…</title>
<style>
  body { margin: 0; font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; }
  h1 { color: #38bdf8; font-size: 1.8rem; margin-bottom: 12px; }
  p  { color: #94a3b8; font-size: 1rem; max-width: 480px; text-align: center; line-height: 1.6; }
  .spinner { width: 40px; height: 40px; border: 4px solid #1e293b; border-top-color: #fbbf24;
             border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 28px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
<script>setTimeout(() => location.reload(), 30000);</script>
</head>
<body>
  <div class="spinner"></div>
  <h1>⏳ Generating Report…</h1>
  <p>The NSE RSI MTF report is currently being generated. This can take 15–60 minutes.<br>
     This page will refresh automatically every 30 seconds.</p>
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
