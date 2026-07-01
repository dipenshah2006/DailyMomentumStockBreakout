import os
import glob
import re
import hashlib
import subprocess
import sys
import threading
import time
import logging
from datetime import datetime
from functools import wraps

import pytz
from flask import Flask, send_file, abort, Response, jsonify, request, session, make_response
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'nse-dashboard-secret')

SCRIPT = 'rsi_mtf_report_nse.py'
IST = pytz.timezone('Asia/Kolkata')

job_lock = threading.Lock()
job_running = False
job_started_at = None

email_lock = threading.Lock()
email_running = False
email_started_at = None
email_last_result = None

RECIPIENTS_FILE = 'email_recipients.txt'
EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')


def _read_recipients():
    if not os.path.exists(RECIPIENTS_FILE):
        return []
    with open(RECIPIENTS_FILE, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()
    return [ln.strip().lower() for ln in lines
            if ln.strip() and not ln.strip().startswith('#')]


def _append_recipient(email):
    with open(RECIPIENTS_FILE, 'a', encoding='utf-8') as f:
        f.write(email + '\n')


def _remove_recipient(email):
    if not os.path.exists(RECIPIENTS_FILE):
        return
    with open(RECIPIENTS_FILE, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()
    kept = [ln for ln in lines if ln.strip().lower() != email.lower()]
    with open(RECIPIENTS_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(kept) + ('\n' if kept else ''))


def _push_to_github(commit_msg):
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        return False, 'saved locally (set GITHUB_TOKEN to also push to GitHub)'
    try:
        env = {**os.environ,
               'GIT_AUTHOR_NAME': 'NSE Bot', 'GIT_AUTHOR_EMAIL': 'bot@noreply',
               'GIT_COMMITTER_NAME': 'NSE Bot', 'GIT_COMMITTER_EMAIL': 'bot@noreply'}
        subprocess.run(['git', 'add', RECIPIENTS_FILE], check=True, env=env)
        subprocess.run(['git', 'commit', '-m', commit_msg], check=True, env=env)
        remote = subprocess.run(['git', 'remote', 'get-url', 'origin'],
                                capture_output=True, text=True).stdout.strip()
        if remote.startswith('https://') and '@' not in remote:
            remote = remote.replace('https://', f'https://x-access-token:{token}@')
        subprocess.run(['git', 'push', remote, 'HEAD'], check=True, env=env)
        return True, 'saved and pushed to GitHub'
    except subprocess.CalledProcessError as e:
        log.error(f'GitHub push failed: {e}')
        return False, 'saved locally (GitHub push failed — check GITHUB_TOKEN)'


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


def run_email_send(dry_run=False):
    global email_running, email_started_at, email_last_result
    with email_lock:
        if email_running:
            log.info('Email send already running, skipping.')
            return
        email_running = True
        email_started_at = datetime.now(IST)
        email_last_result = None
    try:
        mode = 'TEST (sender only)' if dry_run else 'ALL subscribers'
        log.info(f'Starting manual email send — {mode}…')
        env = dict(os.environ)
        if dry_run:
            gmail_user = os.environ.get('GMAIL_USERNAME', '')
            env['EMAIL_RECIPIENTS'] = gmail_user
        result = subprocess.run(
            [sys.executable, 'send_report_email.py'],
            capture_output=True, text=True, timeout=300, env=env
        )
        if result.returncode == 0:
            msg = 'Test email sent to your Gmail.' if dry_run else 'Emails sent to all subscribers.'
            email_last_result = {'ok': True, 'msg': msg}
            log.info('Email send completed successfully.')
        else:
            err = (result.stderr or result.stdout or 'Unknown error')[-300:]
            email_last_result = {'ok': False, 'msg': f'Send failed: {err}'}
            log.error(f'Email send failed:\n{err}')
    except subprocess.TimeoutExpired:
        email_last_result = {'ok': False, 'msg': 'Timed out after 5 minutes.'}
        log.error('Email send timed out.')
    except Exception as e:
        email_last_result = {'ok': False, 'msg': str(e)}
        log.error(f'Email send error: {e}')
    finally:
        with email_lock:
            email_running = False


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
  .badge { display: inline-block; background: #38bdf8; color: #0f172a; border-radius: 20px;
           font-size: 0.72rem; font-weight: 700; padding: 1px 7px; margin-left: 4px; vertical-align: middle; }
  .nav { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; margin-top: 20px; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                background: #fbbf24; animation: pulse 1.2s infinite; margin-right: 6px; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  #msg { margin-top: 12px; font-size: 0.85rem; color: #64748b; min-height: 20px; }
  .sub-box { background: #1e293b; border: 1px solid #334155; border-radius: 14px;
             padding: 18px 28px; margin-top: 20px; max-width: 520px; width: 100%; text-align: center; }
  .sub-box .label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
  .sub-row { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
  .sub-row input[type=email] { flex: 1; min-width: 200px; background: #0f172a; border: 1px solid #475569;
    border-radius: 24px; padding: 9px 18px; color: #e2e8f0; font-size: 0.9rem; outline: none; }
  .sub-row input[type=email]:focus { border-color: #38bdf8; }
  .sub-row input[type=email]::placeholder { color: #475569; }
  #sub-msg { margin-top: 10px; font-size: 0.82rem; min-height: 18px; color: #64748b; }
  #sub-msg.ok  { color: #4ade80; }
  #sub-msg.err { color: #f87171; }
  .unsub-link { margin-top: 8px; font-size: 0.78rem; color: #475569; }
  .unsub-link a { color: #64748b; text-decoration: underline; cursor: pointer; }
  .unsub-link a:hover { color: #94a3b8; }
"""

_SUBSCRIBE_WIDGET = """
  <div class="sub-box">
    <div class="label">📬 Get daily reports in your inbox</div>
    <div class="sub-row">
      <input type="email" id="subEmail" placeholder="you@example.com" />
      <button class="btn" style="margin-top:0" onclick="doSubscribe()">Subscribe</button>
    </div>
    <div id="sub-msg"></div>
    <div class="unsub-link"><a onclick="doUnsubscribe()">Unsubscribe</a></div>
  </div>
<script>
async function doSubscribe() {
  const email = document.getElementById('subEmail').value.trim();
  const msg = document.getElementById('sub-msg');
  if (!email) { msg.className='err'; msg.textContent='Please enter your email.'; return; }
  msg.className=''; msg.textContent='Saving…';
  try {
    const r = await fetch('/subscribe', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
    const d = await r.json();
    msg.className = d.ok ? 'ok' : 'err';
    msg.textContent = d.message;
  } catch(e) { msg.className='err'; msg.textContent='Error — try again.'; }
}
async function doUnsubscribe() {
  const email = document.getElementById('subEmail').value.trim();
  const msg = document.getElementById('sub-msg');
  if (!email) { msg.className='err'; msg.textContent='Enter your email to unsubscribe.'; return; }
  msg.className=''; msg.textContent='Removing…';
  try {
    const r = await fetch('/unsubscribe', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
    const d = await r.json();
    msg.className = d.ok ? 'ok' : 'err';
    msg.textContent = d.message;
  } catch(e) { msg.className='err'; msg.textContent='Error — try again.'; }
}
</script>
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
    <a class="btn outline" href="/subscribers">⚙️ Admin <span class="badge" id="adminBadge"></span></a>
  </div>

  {_SUBSCRIBE_WIDGET}

<script>
fetch('/sub-count').then(r=>r.json()).then(d=>{{if(d.count>0)document.getElementById('adminBadge').textContent=d.count;}});
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
    <a class="btn outline" href="/subscribers">⚙️ Admin <span class="badge" id="adminBadge"></span></a>
  </div>

  {_SUBSCRIBE_WIDGET}

<script>
fetch('/sub-count').then(r=>r.json()).then(d=>{{if(d.count>0)document.getElementById('adminBadge').textContent=d.count;}});
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


@app.route('/sub-count')
def sub_count():
    return jsonify({'count': len(_read_recipients())})


@app.route('/send-email', methods=['POST'])
def send_email_now():
    if email_running:
        return jsonify({'started': False, 'message': 'Email send already in progress.'})
    gmail_user = os.environ.get('GMAIL_USERNAME', '')
    gmail_pass = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_pass:
        return jsonify({'started': False, 'message': 'GMAIL_USERNAME or GMAIL_APP_PASSWORD not set.'})
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get('dry_run', False))
    t = threading.Thread(target=run_email_send, kwargs={'dry_run': dry_run}, daemon=True)
    t.start()
    mode = 'test (sender only)' if dry_run else 'all subscribers'
    log.info(f'Email send triggered manually — {mode}.')
    return jsonify({'started': True, 'message': f'Email send started ({mode}).'})


@app.route('/email-status')
def email_status():
    elapsed = None
    if email_running and email_started_at:
        elapsed = int((datetime.now(IST) - email_started_at).total_seconds())
    return jsonify({
        'running': email_running,
        'elapsed_seconds': elapsed,
        'last_result': email_last_result,
    })


@app.route('/subscribe', methods=['POST'])
def subscribe():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or not EMAIL_RE.match(email):
        return jsonify({'ok': False, 'message': 'Please enter a valid email address.'})
    existing = _read_recipients()
    if email in existing:
        return jsonify({'ok': True, 'message': '✅ Already subscribed — you\'re on the list!'})
    _append_recipient(email)
    pushed, detail = _push_to_github(f'subscribe: add {email}')
    log.info(f'New subscriber: {email} — {detail}')
    return jsonify({'ok': True, 'message': f'✅ Subscribed! Daily reports will arrive in your inbox ({detail}).'})


@app.route('/unsubscribe', methods=['POST'])
def unsubscribe():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or not EMAIL_RE.match(email):
        return jsonify({'ok': False, 'message': 'Please enter a valid email address.'})
    existing = _read_recipients()
    if email not in existing:
        return jsonify({'ok': False, 'message': 'That email isn\'t on the list.'})
    _remove_recipient(email)
    pushed, detail = _push_to_github(f'unsubscribe: remove {email}')
    log.info(f'Unsubscribed: {email} — {detail}')
    return jsonify({'ok': True, 'message': f'✅ Unsubscribed — you\'ve been removed from the list.'})


@app.route('/charts/<path:filename>')
def charts(filename):
    path = os.path.join('charts', filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


def _check_admin():
    pw = os.environ.get('ADMIN_PASSWORD', '')
    return session.get('admin_ok') and session.get('admin_hash') == hashlib.sha256(pw.encode()).hexdigest()


_ADMIN_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 32px 16px; }
  h1 { color: #38bdf8; font-size: 1.6rem; margin-bottom: 6px; }
  .sub-count { color: #64748b; font-size: 0.9rem; margin-bottom: 24px; }
  .back { color: #64748b; font-size: 0.85rem; text-decoration: none; display: inline-block; margin-bottom: 20px; }
  .back:hover { color: #38bdf8; }
  table { width: 100%; max-width: 680px; border-collapse: collapse; margin-top: 8px; }
  th { text-align: left; font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 1px;
       padding: 8px 12px; border-bottom: 1px solid #1e293b; }
  td { padding: 10px 12px; border-bottom: 1px solid #1e293b; font-size: 0.9rem; color: #cbd5e1; }
  tr:hover td { background: #1e293b; }
  .del { background: none; border: 1px solid #475569; color: #94a3b8; border-radius: 20px;
         padding: 4px 14px; font-size: 0.8rem; cursor: pointer; }
  .del:hover { border-color: #f87171; color: #f87171; }
  .empty { color: #475569; margin-top: 24px; }
  .login-wrap { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 80vh; gap: 12px; }
  .login-card { background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 32px 40px; width: 100%; max-width: 360px; text-align: center; }
  .login-card h2 { color: #38bdf8; margin-bottom: 20px; font-size: 1.2rem; }
  .login-card input { width: 100%; background: #0f172a; border: 1px solid #475569; border-radius: 8px;
    padding: 10px 14px; color: #e2e8f0; font-size: 0.95rem; margin-bottom: 14px; outline: none; }
  .login-card input:focus { border-color: #38bdf8; }
  .login-card button { width: 100%; background: #38bdf8; color: #0f172a; border: none; border-radius: 24px;
    padding: 10px; font-size: 0.95rem; font-weight: 700; cursor: pointer; }
  .login-card button:hover { background: #7dd3fc; }
  .err-msg { color: #f87171; font-size: 0.85rem; margin-top: -8px; margin-bottom: 8px; }
  .flash { color: #4ade80; font-size: 0.85rem; margin-bottom: 16px; }
  .err-inline { color: #f87171; font-size: 0.85rem; margin-bottom: 16px; }
  .add-row { display: flex; gap: 8px; align-items: center; margin-bottom: 24px; flex-wrap: wrap; }
  .add-row input[type=email] { flex: 1; min-width: 220px; background: #0f172a; border: 1px solid #475569;
    border-radius: 24px; padding: 9px 18px; color: #e2e8f0; font-size: 0.9rem; outline: none; }
  .add-row input[type=email]:focus { border-color: #38bdf8; }
  .add-row input[type=email]::placeholder { color: #475569; }
  .add-btn { background: #38bdf8; color: #0f172a; border: none; border-radius: 24px;
             padding: 9px 22px; font-size: 0.9rem; font-weight: 700; cursor: pointer; white-space: nowrap; }
  .add-btn:hover { background: #7dd3fc; }
  .toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .export-btn { background: transparent; border: 1px solid #475569; color: #94a3b8; border-radius: 24px;
                padding: 7px 18px; font-size: 0.85rem; cursor: pointer; text-decoration: none; display: inline-block; }
  .export-btn:hover { border-color: #38bdf8; color: #38bdf8; }
  .bulk-section { background: #1e293b; border: 1px solid #334155; border-radius: 12px;
                  padding: 16px 20px; margin-bottom: 20px; }
  .bulk-section summary { color: #94a3b8; font-size: 0.88rem; cursor: pointer; user-select: none; list-style: none; }
  .bulk-section summary::before { content: '▶ '; font-size: 0.75rem; }
  details[open] summary::before { content: '▼ '; }
  .bulk-section summary:hover { color: #38bdf8; }
  .bulk-section textarea { width: 100%; margin-top: 12px; background: #0f172a; border: 1px solid #475569;
    border-radius: 8px; padding: 10px 14px; color: #e2e8f0; font-size: 0.85rem; font-family: monospace;
    resize: vertical; min-height: 100px; outline: none; }
  .bulk-section textarea:focus { border-color: #38bdf8; }
  .bulk-hint { font-size: 0.78rem; color: #475569; margin-top: 6px; margin-bottom: 10px; }
  .search-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .search-row input { flex: 1; max-width: 340px; background: #0f172a; border: 1px solid #475569;
    border-radius: 24px; padding: 8px 16px; color: #e2e8f0; font-size: 0.88rem; outline: none; }
  .search-row input:focus { border-color: #38bdf8; }
  .search-row input::placeholder { color: #475569; }
  .no-match { display: none; color: #475569; font-size: 0.88rem; padding: 12px 0; }
  .run-section { background: #1e293b; border: 1px solid #334155; border-radius: 12px;
                 padding: 16px 20px; margin-bottom: 20px; display: flex; align-items: center;
                 justify-content: space-between; flex-wrap: wrap; gap: 12px; }
  .run-section .run-info { font-size: 0.85rem; color: #64748b; }
  .run-section .run-info strong { color: #94a3b8; }
  .run-btn { background: #38bdf8; color: #0f172a; border: none; border-radius: 24px;
             padding: 9px 22px; font-size: 0.9rem; font-weight: 700; cursor: pointer; white-space: nowrap; }
  .run-btn:hover { background: #7dd3fc; }
  .run-btn:disabled { background: #334155; color: #64748b; cursor: not-allowed; }
  .run-status { font-size: 0.82rem; color: #64748b; margin-top: 4px; min-height: 18px; }
  .run-status.running { color: #fbbf24; }
  .run-status.done { color: #4ade80; }
"""


@app.route('/subscribers/export')
def admin_export():
    if not _check_admin():
        return abort(403)
    recipients = _read_recipients()
    now = datetime.now(IST).strftime('%Y-%m-%d')
    csv_content = 'email\n' + '\n'.join(recipients)
    resp = make_response(csv_content)
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = f'attachment; filename="subscribers_{now}.csv"'
    return resp


@app.route('/subscribers', methods=['GET', 'POST'])
def admin_subscribers():
    pw = os.environ.get('ADMIN_PASSWORD', '')
    error = ''
    flash = ''

    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'login':
            entered = request.form.get('password', '')
            if entered == pw:
                session['admin_ok'] = True
                session['admin_hash'] = hashlib.sha256(pw.encode()).hexdigest()
            else:
                error = 'Incorrect password.'
        elif action == 'logout':
            session.clear()
        elif action == 'add' and _check_admin():
            email = request.form.get('email', '').strip().lower()
            if not email or not EMAIL_RE.match(email):
                error = 'Please enter a valid email address.'
            elif email in _read_recipients():
                error = f'{email} is already on the list.'
            else:
                _append_recipient(email)
                _push_to_github(f'admin: add {email}')
                flash = f'Added {email}'
        elif action == 'bulk_add' and _check_admin():
            raw = request.form.get('bulk_emails', '')
            candidates = [e.strip().lower().strip(',') for e in re.split(r'[\n,;]+', raw) if e.strip()]
            existing = set(_read_recipients())
            added, skipped_dup, skipped_bad = [], [], []
            for e in candidates:
                if not EMAIL_RE.match(e):
                    skipped_bad.append(e)
                elif e in existing:
                    skipped_dup.append(e)
                else:
                    _append_recipient(e)
                    existing.add(e)
                    added.append(e)
            if added:
                _push_to_github(f'admin: bulk add {len(added)} emails')
            parts = []
            if added:
                parts.append(f'Added {len(added)}')
            if skipped_dup:
                parts.append(f'{len(skipped_dup)} already on list')
            if skipped_bad:
                parts.append(f'{len(skipped_bad)} invalid')
            flash = ' · '.join(parts) if parts else 'Nothing to add.'
        elif action == 'delete' and _check_admin():
            email = request.form.get('email', '').strip().lower()
            if email:
                _remove_recipient(email)
                _push_to_github(f'admin: remove {email}')
                flash = f'Removed {email}'

    if not _check_admin():
        html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Login</title><style>{_ADMIN_CSS}</style></head><body>
<div class="login-wrap">
  <div class="login-card">
    <h2>🔒 Admin Login</h2>
    {'<p class="err-msg">' + error + '</p>' if error else ''}
    <form method="POST">
      <input type="hidden" name="action" value="login">
      <input type="password" name="password" placeholder="Password" autofocus>
      <button type="submit">Log in</button>
    </form>
  </div>
</div>
</body></html>"""
        return Response(html, mimetype='text/html')

    recipients = _read_recipients()
    rows = ''.join(
        f'<tr><td>{i+1}</td><td>{r}</td><td>'
        f'<form method="POST" onsubmit="return confirm(\'Remove {r}?\');">'
        f'<input type="hidden" name="action" value="delete">'
        f'<input type="hidden" name="email" value="{r}">'
        f'<button class="del" type="submit">Remove</button></form></td></tr>'
        for i, r in enumerate(recipients)
    )
    count_label = f"{len(recipients)} address{'es' if len(recipients) != 1 else ''} on the mailing list"
    flash_html  = f'<p class="flash">✅ {flash}</p>' if flash else ''
    error_html  = f'<p class="err-inline">⚠️ {error}</p>' if error else ''
    if recipients:
        table_html = (
            '<div class="search-row">'
            '<input id="srch" type="search" placeholder="🔍  Search subscribers…" oninput="filterTable(this.value)">'
            '</div>'
            '<p class="no-match" id="noMatch">No matching subscribers.</p>'
            '<table><thead><tr><th>#</th><th>Email</th><th></th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<script>'
            'function filterTable(q){'
            'const rows=document.querySelectorAll("tbody tr");let vis=0;'
            'rows.forEach(r=>{'
            'const show=r.cells[1].textContent.toLowerCase().includes(q.toLowerCase());'
            'r.style.display=show?"":"none";if(show)vis++;});'
            'document.getElementById("noMatch").style.display=vis===0&&q?"block":"none";}'
            '</script>'
        )
    else:
        table_html = '<p class="empty">No subscribers yet.</p>'
    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subscribers — Admin</title><style>{_ADMIN_CSS}</style></head><body>
<a class="back" href="/">← Back to dashboard</a>
<h1>📋 Subscribers</h1>
<p class="sub-count">{count_label}</p>
{flash_html}
{error_html}
<div class="run-section">
  <div>
    <div class="run-info">📊 <strong>NSE Report</strong> — triggers the full scan across all stocks</div>
    <div class="run-status" id="runStatus"></div>
  </div>
  <button class="run-btn" id="runBtn" onclick="adminRun()">▶ Run Report Now</button>
</div>
<div class="run-section">
  <div>
    <div class="run-info">📧 <strong>Send Report Email</strong> — delivers the latest report to all {len(recipients)} subscribers</div>
    <label style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:0.83rem;color:#94a3b8;cursor:pointer;">
      <input type="checkbox" id="dryRunChk" style="accent-color:#38bdf8;width:15px;height:15px;">
      Test mode — send only to me (preview before full send)
    </label>
    <div class="run-status" id="emailStatus"></div>
  </div>
  <button class="run-btn" id="emailBtn" onclick="adminSendEmail()">✉️ Send Email Now</button>
</div>
<div class="toolbar">
  <form method="POST" class="add-row" style="margin-bottom:0">
    <input type="hidden" name="action" value="add">
    <input type="email" name="email" placeholder="Add email address…" autocomplete="off">
    <button class="add-btn" type="submit">+ Add</button>
  </form>
  <a class="export-btn" href="/subscribers/export">⬇ Export CSV</a>
</div>
<details class="bulk-section">
  <summary>Bulk import emails</summary>
  <form method="POST">
    <input type="hidden" name="action" value="bulk_add">
    <textarea name="bulk_emails" placeholder="Paste emails here — one per line, or comma/semicolon separated:&#10;alice@example.com&#10;bob@example.com, carol@example.com"></textarea>
    <p class="bulk-hint">Duplicates and invalid addresses are automatically skipped.</p>
    <button class="add-btn" type="submit">Import</button>
  </form>
</details>
{table_html}
<form method="POST" style="margin-top:32px">
  <input type="hidden" name="action" value="logout">
  <button class="del" type="submit">Log out</button>
</form>
<script>
async function adminSendEmail() {{
  const btn = document.getElementById('emailBtn');
  const st  = document.getElementById('emailStatus');
  const dryRun = document.getElementById('dryRunChk').checked;
  btn.disabled = true; btn.textContent = dryRun ? '⏳ Sending test…' : '⏳ Sending…';
  st.className = 'run-status running'; st.textContent = '';
  try {{
    const r = await fetch('/send-email', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{dry_run:dryRun}})}});
    const d = await r.json();
    if (d.started) {{
      st.textContent = '⏳ Sending to all subscribers…';
      pollEmail();
    }} else {{
      st.className = 'run-status'; st.textContent = d.message;
      btn.disabled = false; btn.textContent = '✉️ Send Email Now';
    }}
  }} catch(e) {{
    st.className = 'run-status'; st.textContent = 'Error — check server logs.';
    btn.disabled = false; btn.textContent = '✉️ Send Email Now';
  }}
}}
async function pollEmail() {{
  try {{
    const r = await fetch('/email-status');
    const d = await r.json();
    if (!d.running) {{
      const btn = document.getElementById('emailBtn');
      const st  = document.getElementById('emailStatus');
      btn.disabled = false; btn.textContent = '✉️ Send Email Now';
      if (d.last_result) {{
        st.className = 'run-status ' + (d.last_result.ok ? 'done' : '');
        st.textContent = (d.last_result.ok ? '✅ ' : '⚠️ ') + d.last_result.msg;
      }}
      return;
    }}
  }} catch(e) {{}}
  setTimeout(pollEmail, 4000);
}}
(async () => {{
  try {{
    const r = await fetch('/email-status');
    const d = await r.json();
    if (d.running) {{
      const btn = document.getElementById('emailBtn');
      const st  = document.getElementById('emailStatus');
      btn.disabled = true; btn.textContent = '⏳ Sending…';
      st.className = 'run-status running'; st.textContent = '⏳ Send in progress…';
      pollEmail();
    }} else if (d.last_result) {{
      const st = document.getElementById('emailStatus');
      st.className = 'run-status ' + (d.last_result.ok ? 'done' : '');
      st.textContent = (d.last_result.ok ? '✅ ' : '⚠️ ') + d.last_result.msg;
    }}
  }} catch(e) {{}}
}})();
async function adminRun() {{
  const btn = document.getElementById('runBtn');
  const st  = document.getElementById('runStatus');
  btn.disabled = true; btn.textContent = '⏳ Starting…';
  try {{
    const r = await fetch('/run', {{method:'POST'}});
    const d = await r.json();
    if (d.started) {{
      st.className = 'run-status running';
      st.textContent = '⏳ Report is generating (15–60 min)…';
      btn.textContent = '⏳ Running…';
      pollRun();
    }} else {{
      st.className = 'run-status running';
      st.textContent = d.message || 'Already running…';
      btn.textContent = '⏳ Running…';
      pollRun();
    }}
  }} catch(e) {{
    st.className = 'run-status'; st.textContent = 'Error — check server logs.';
    btn.disabled = false; btn.textContent = '▶ Run Report Now';
  }}
}}
async function pollRun() {{
  try {{
    const r = await fetch('/status');
    const d = await r.json();
    if (!d.running) {{
      const btn = document.getElementById('runBtn');
      const st  = document.getElementById('runStatus');
      btn.disabled = false; btn.textContent = '▶ Run Report Now';
      st.className = 'run-status done';
      st.textContent = d.latest_report ? '✅ Report ready — ' + (d.report_generated_at || '') : '✅ Done.';
      return;
    }}
  }} catch(e) {{}}
  setTimeout(pollRun, 8000);
}}
(async () => {{
  try {{
    const r = await fetch('/status');
    const d = await r.json();
    if (d.running) {{
      const btn = document.getElementById('runBtn');
      const st  = document.getElementById('runStatus');
      btn.disabled = true; btn.textContent = '⏳ Running…';
      st.className = 'run-status running';
      st.textContent = '⏳ Report is generating…';
      pollRun();
    }}
  }} catch(e) {{}}
}})();
</script>
</body></html>"""
    return Response(html, mimetype='text/html')


def _keep_alive():
    """Ping ourselves every 10 minutes so Replit never sleeps during market days."""
    import urllib.request
    try:
        host = os.environ.get('REPLIT_DEV_DOMAIN') or '127.0.0.1:5000'
        url = f'http://127.0.0.1:5000/status'
        urllib.request.urlopen(url, timeout=10)
        log.debug('Keep-alive ping sent.')
    except Exception:
        pass  # silent — server may not be ready yet


if __name__ == '__main__':
    scheduler = BackgroundScheduler(timezone=IST)
    # Mon–Sat at 5:00 AM IST (NSE is closed on Sunday)
    scheduler.add_job(run_nse_report, 'cron', day_of_week='mon-sat', hour=5, minute=0)
    # Ping every 10 min to keep the process alive so the cron always fires
    scheduler.add_job(_keep_alive, 'interval', minutes=10)
    scheduler.start()
    log.info('Scheduler started — NSE report will run Mon–Sat at 5:00 AM IST.')

    if not latest_report():
        log.info('No existing report found — generating first report now...')
        t = threading.Thread(target=run_nse_report, daemon=True)
        t.start()

    app.run(host='0.0.0.0', port=5000, debug=False)
