"""
NSE Report bulk emailer
=======================
Reads email_recipients.txt (one address per line, # for comments),
then sends the HTML report either:
  - mode=bcc   : batches of BCC_BATCH_SIZE per send  (fewer SMTP calls, fast)
  - mode=individual : one SMTP call per recipient    (personalised, slower)

Environment variables (required):
  GMAIL_USERNAME      sender Gmail address
  GMAIL_APP_PASSWORD  Gmail App Password (not your login password)

Optional env vars:
  EMAIL_SUBJECT_FILE  path to subject line file   (default: email_subject.txt)
  EMAIL_BODY_FILE     path to HTML body file       (default: email_summary.html)
  EMAIL_SEND_MODE     bcc | individual             (default: bcc)
  EMAIL_BCC_BATCH     recipients per BCC batch     (default: 100)
  EMAIL_RECIPIENTS    comma-separated override list (skips email_recipients.txt)
  EMAIL_DRY_RUN       1 = print addresses, don't send (default: 0)
"""

import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

RECIPIENTS_FILE   = "email_recipients.txt"
DEFAULT_SUBJECT   = "📈 NSE Daily Momentum Breakout Report"
DEFAULT_BODY_FILE = "email_summary.html"
DEFAULT_SUBJECT_FILE = "email_subject.txt"
SMTP_HOST         = "smtp.gmail.com"
SMTP_PORT         = 465


# ── Config ────────────────────────────────────────────────────────────────────

def cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


USERNAME    = cfg("GMAIL_USERNAME")
PASSWORD    = cfg("GMAIL_APP_PASSWORD")
SEND_MODE   = cfg("EMAIL_SEND_MODE", "bcc").lower()       # "bcc" or "individual"
BCC_BATCH   = int(cfg("EMAIL_BCC_BATCH", "100"))
DRY_RUN     = cfg("EMAIL_DRY_RUN", "0") == "1"
BODY_FILE   = cfg("EMAIL_BODY_FILE", DEFAULT_BODY_FILE)
SUBJECT_FILE = cfg("EMAIL_SUBJECT_FILE", DEFAULT_SUBJECT_FILE)
FAILURE_MODE = cfg("EMAIL_FAILURE_MODE", "0") == "1"      # set by workflow on failure


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_recipients() -> list[str]:
    override = cfg("EMAIL_RECIPIENTS")
    if override:
        addrs = [a.strip() for a in override.split(",") if a.strip()]
        print(f"📋 Using EMAIL_RECIPIENTS override: {len(addrs)} addresses")
        return addrs

    path = Path(RECIPIENTS_FILE)
    if not path.exists():
        print(f"⚠️  {RECIPIENTS_FILE} not found — no recipients loaded.")
        return []

    addrs = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            addrs.append(line)

    print(f"📋 Loaded {len(addrs)} recipients from {RECIPIENTS_FILE}")
    return addrs


def load_subject() -> str:
    path = Path(SUBJECT_FILE)
    if path.exists():
        subject = path.read_text(encoding="utf-8").strip()
        if subject:
            return subject
    return DEFAULT_SUBJECT


def load_html_body() -> str:
    path = Path(BODY_FILE)
    if not path.exists():
        raise FileNotFoundError(f"Email body file not found: {BODY_FILE}")
    return path.read_text(encoding="utf-8")


def failure_body() -> str:
    run_id = cfg("GITHUB_RUN_ID")
    run_num = cfg("GITHUB_RUN_NUMBER", "?")
    repo    = cfg("GITHUB_REPOSITORY", "dipenshah2006/DailyMomentumStockBreakout")
    logs_url = f"https://github.com/{repo}/actions/runs/{run_id}" if run_id else \
               f"https://github.com/{repo}/actions"
    return f"""<html><body style="font-family:Arial,sans-serif;color:#374151;">
<p>Hi,</p>
<p>The NSE RSI Report workflow <strong>failed</strong> on Run #{run_num}.</p>
<p><a href="{logs_url}" style="color:#1d4ed8;">🔗 View Logs</a></p>
<p>Regards,<br>NSE Report Bot</p>
</body></html>"""


def make_message(sender: str, to_addr: str, bcc_list: list[str],
                 subject: str, html: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"NSE Report Bot <{sender}>"
    msg["To"]      = to_addr
    if bcc_list:
        msg["Bcc"] = ", ".join(bcc_list)
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ── Senders ───────────────────────────────────────────────────────────────────

def send_bcc_batches(smtp: smtplib.SMTP_SSL, sender: str, recipients: list[str],
                     subject: str, html: str) -> int:
    """Send one email per BCC_BATCH with recipients hidden from each other."""
    sent = 0
    batches = list(chunks(recipients, BCC_BATCH))
    print(f"📤 BCC mode — {len(recipients)} recipients in {len(batches)} batch(es) of ≤{BCC_BATCH}")

    for idx, batch in enumerate(batches, 1):
        if DRY_RUN:
            print(f"  [DRY RUN] Batch {idx}/{len(batches)}: {batch}")
            sent += len(batch)
            continue

        msg = make_message(sender, sender, batch, subject, html)
        all_recipients = [sender] + batch
        smtp.sendmail(sender, all_recipients, msg.as_string())
        sent += len(batch)
        print(f"  ✅ Batch {idx}/{len(batches)} sent — {len(batch)} recipients")

        if idx < len(batches):
            time.sleep(1)

    return sent


def send_individual(smtp: smtplib.SMTP_SSL, sender: str, recipients: list[str],
                    subject: str, html: str) -> int:
    """Send one email per recipient — each person sees only their own address."""
    sent = 0
    print(f"📤 Individual mode — sending {len(recipients)} emails one by one")

    for idx, addr in enumerate(recipients, 1):
        if DRY_RUN:
            print(f"  [DRY RUN] {idx}/{len(recipients)}: {addr}")
            sent += 1
            continue

        msg = make_message(sender, addr, [], subject, html)
        smtp.sendmail(sender, [addr], msg.as_string())
        sent += 1

        if idx % 20 == 0:
            print(f"  … {idx}/{len(recipients)} sent")

        time.sleep(0.3)

    return sent


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not USERNAME or not PASSWORD:
        print("❌ GMAIL_USERNAME and GMAIL_APP_PASSWORD must be set.")
        sys.exit(1)

    recipients = load_recipients()
    if not recipients:
        print("⚠️  No recipients — nothing to send.")
        sys.exit(0)

    subject = load_subject()
    if FAILURE_MODE:
        subject = f"❌ NSE RSI Report FAILED — {subject.lstrip('📈 ')}"
        html    = failure_body()
    else:
        html = load_html_body()

    print(f"📧 Subject : {subject}")
    print(f"📨 Mode    : {'DRY RUN — ' if DRY_RUN else ''}{SEND_MODE.upper()}")
    print(f"👥 Total   : {len(recipients)} recipients")

    if DRY_RUN:
        if SEND_MODE == "individual":
            send_individual(None, USERNAME, recipients, subject, html)
        else:
            send_bcc_batches(None, USERNAME, recipients, subject, html)
        print("✅ Dry run complete — no emails sent.")
        return

    print(f"🔐 Connecting to {SMTP_HOST}:{SMTP_PORT}…")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.login(USERNAME, PASSWORD)
            print("✅ Authenticated")

            if SEND_MODE == "individual":
                sent = send_individual(smtp, USERNAME, recipients, subject, html)
            else:
                sent = send_bcc_batches(smtp, USERNAME, recipients, subject, html)

        print(f"\n🎉 Done — {sent} recipients reached.")
    except smtplib.SMTPAuthenticationError:
        print("❌ SMTP authentication failed. Check GMAIL_USERNAME and GMAIL_APP_PASSWORD.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ SMTP error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
