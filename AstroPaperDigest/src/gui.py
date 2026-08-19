#!/usr/bin/env python3
"""AstroPaperDigest - Flask web interface with taste feedback.

Flow: .app launches this -> browser opens immediately with status page ->
pipeline runs in background -> page auto-updates when done.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import date
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread, Timer

from flask import Flask, abort, jsonify, redirect, render_template_string, request
from werkzeug.utils import secure_filename

# Ensure working directory is the project root (for .app launches)
_PROJECT_DIR = Path(__file__).resolve().parent.parent
os.chdir(_PROJECT_DIR)
sys.path.insert(0, str(_PROJECT_DIR))

from src.digest_parser import parse_digest, get_latest_digest_path, get_digest_path_for_date, get_available_dates

FEEDBACK_FILE = os.path.join(_PROJECT_DIR, "feedback.json")
PREFERENCES_FILE = os.path.join(_PROJECT_DIR, "preferences.json")

app = Flask(__name__)

# Global state
_current_digest = None
_pipeline_status = "idle"  # idle | running | done | error
_pipeline_message = ""
_pipeline_process = None
_pipeline_lock = Lock()
_browser_clients = {}
_browser_clients_lock = Lock()
_browser_check_timer = None

_BROWSER_CLIENT_TIMEOUT = 45.0
_BROWSER_CLOSE_GRACE = 2.0
_BROWSER_LIFECYCLE_SCRIPT = """
<script>
(() => {
  const storageKey = "astroPaperDigestClientId";
  let clientId;
  try {
    clientId = sessionStorage.getItem(storageKey);
    if (!clientId) {
      clientId = crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`;
      sessionStorage.setItem(storageKey, clientId);
    }
  } catch (_) {
    clientId = `${Date.now()}-${Math.random()}`;
  }

  const endpoint = (action) =>
    `/client/${action}?id=${encodeURIComponent(clientId)}`;
  const heartbeat = () => fetch(endpoint("heartbeat"), {
    method: "POST",
    cache: "no-store",
    keepalive: true
  }).catch(() => {});

  heartbeat();
  const heartbeatTimer = setInterval(heartbeat, 10000);
  addEventListener("pagehide", () => {
    clearInterval(heartbeatTimer);
    navigator.sendBeacon(endpoint("close"));
  }, { once: true });
})();
</script>
"""


def _terminate_server():
    """Terminate Flask so the launcher exits and releases its port."""
    process = _pipeline_process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    os.kill(os.getpid(), signal.SIGTERM)


def _schedule_browser_check_locked(delay: float):
    global _browser_check_timer
    if _browser_check_timer is not None:
        _browser_check_timer.cancel()
    _browser_check_timer = Timer(delay, _check_browser_clients)
    _browser_check_timer.daemon = True
    _browser_check_timer.start()


def _check_browser_clients():
    """Stop the server after all browser tabs close or disappear."""
    global _browser_check_timer
    with _browser_clients_lock:
        cutoff = time.monotonic() - _BROWSER_CLIENT_TIMEOUT
        stale_ids = [
            client_id
            for client_id, last_seen in _browser_clients.items()
            if last_seen < cutoff
        ]
        for client_id in stale_ids:
            _browser_clients.pop(client_id, None)

        if _browser_clients:
            _schedule_browser_check_locked(_BROWSER_CLIENT_TIMEOUT)
            return
        _browser_check_timer = None

    _terminate_server()


def _needs_setup():
    """Check if first-time setup is needed (no .env file)."""
    return not os.path.exists(os.path.join(_PROJECT_DIR, ".env"))


@app.before_request
def check_setup():
    """Redirect to setup page if first-time user."""
    if (
        _needs_setup()
        and request.path != "/setup"
        and not request.path.startswith(("/static", "/client/"))
    ):
        return redirect("/setup")


@app.after_request
def add_browser_lifecycle(response):
    """Attach lifecycle tracking to every rendered browser page."""
    if response.mimetype == "text/html":
        content = response.get_data(as_text=True)
        if "</body>" in content:
            response.set_data(
                content.replace(
                    "</body>",
                    f"{_BROWSER_LIFECYCLE_SCRIPT}</body>",
                    1,
                )
            )
    return response


@app.route("/client/heartbeat", methods=["POST"])
def browser_heartbeat():
    client_id = request.args.get("id", "")
    if not client_id:
        return "", 400

    with _browser_clients_lock:
        _browser_clients[client_id] = time.monotonic()
        _schedule_browser_check_locked(_BROWSER_CLIENT_TIMEOUT)
    return "", 204


@app.route("/client/close", methods=["POST"])
def browser_close():
    client_id = request.args.get("id", "")
    if client_id:
        with _browser_clients_lock:
            _browser_clients.pop(client_id, None)
            delay = (
                _BROWSER_CLIENT_TIMEOUT
                if _browser_clients
                else _BROWSER_CLOSE_GRACE
            )
            _schedule_browser_check_locked(delay)
    return "", 204


def load_preferences() -> dict:
    """Load user preferences from file."""
    defaults = {
        "include_cross": True,
        "include_replacements": True,
        "last_viewed_date": "",
    }
    if os.path.exists(PREFERENCES_FILE):
        try:
            with open(PREFERENCES_FILE, "r", encoding="utf-8") as f:
                prefs = json.load(f)
                defaults.update(prefs)
        except (json.JSONDecodeError, IOError):
            pass
    return defaults


def save_preferences(prefs: dict):
    """Save user preferences to file."""
    with open(PREFERENCES_FILE, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2, ensure_ascii=False)


def load_feedback() -> list:
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_feedback(feedback: list):
    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(feedback, f, indent=2, ensure_ascii=False)


def _pipeline_progress_message(line: str) -> str:
    """Convert CLI output into a concise browser status message."""
    line = line.strip()
    if not line:
        return ""
    if line.startswith("["):
        return line
    if line.startswith("Ranking batch"):
        return f"AI ranking: {line.lower()}"
    if line.startswith(("Fetched ", "Category filter:", "Keyword filter:")):
        return line
    if line.startswith("Sending email"):
        return "Sending email notification..."
    if "before retry" in line or "rate limit" in line.lower():
        return line
    return ""


def _stream_pipeline(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    """Run the CLI while streaming progress instead of buffering silently."""
    global _pipeline_message, _pipeline_process

    output_lines = []
    output_queue = Queue()
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    _pipeline_process = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=environment,
    )

    def read_output():
        assert _pipeline_process.stdout is not None
        for output_line in _pipeline_process.stdout:
            output_queue.put(output_line)
        output_queue.put(None)

    Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout

    try:
        while True:
            if time.monotonic() >= deadline:
                _pipeline_process.terminate()
                try:
                    _pipeline_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _pipeline_process.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)

            try:
                output_line = output_queue.get(timeout=0.5)
            except Empty:
                if _pipeline_process.poll() is not None:
                    break
                continue

            if output_line is None:
                break

            output_lines.append(output_line)
            progress = _pipeline_progress_message(output_line)
            if progress:
                _pipeline_message = progress

        return _pipeline_process.wait(), "".join(output_lines)
    finally:
        _pipeline_process = None


def run_pipeline(include_cross: bool = True, include_replacements: bool = True, target_date: str = ""):
    """Run the recommendation pipeline in a background thread."""
    global _current_digest, _pipeline_status, _pipeline_message
    _pipeline_status = "running"
    _pipeline_message = "[1/5] Starting pipeline..."

    try:
        # Build command with preferences
        cmd = [sys.executable, "-u", "main.py"]
        if not include_cross:
            cmd.append("--no-cross")
        if not include_replacements:
            cmd.append("--no-replacements")
        if target_date:
            cmd.extend(["--target-date", target_date])
        
        return_code, stdout = _stream_pipeline(cmd)
        if return_code == 0:
            # Locate the digest actually written by this run. Prefer the file
            # for the requested date, then the latest file, then the path the
            # CLI printed (covers custom digest_dir settings).
            digest_path = ""
            if target_date:
                digest_path = get_digest_path_for_date(target_date)
            if not digest_path:
                digest_path = get_latest_digest_path()
            if not digest_path:
                match = re.search(r"Digest:\s*(\S+)", stdout)
                if match:
                    digest_path = match.group(1).strip()
            try:
                if digest_path and os.path.exists(digest_path):
                    _current_digest = parse_digest(digest_path)
            except Exception:
                _current_digest = None
            _pipeline_status = "done"
            # Set contextual message based on result
            if (
                "No papers found" in stdout
                or "No new papers since last digest" in stdout
                or "No papers matched the filter criteria" in stdout
            ):
                if _current_digest and _current_digest["total_papers"] == 0:
                    status = _current_digest.get("status", "no_papers")
                    if status == "no_papers":
                        _pipeline_message = "No available papers (arxiv has not been updated yet)."
                    elif status == "no_new_papers":
                        _pipeline_message = "No new papers since last digest."
                    elif status == "no_matches":
                        _pipeline_message = "No papers matched your research keywords."
                    else:
                        _pipeline_message = "No new papers found."
                else:
                    _pipeline_message = "No new papers found."
            else:
                _pipeline_message = "Complete!"
        else:
            _pipeline_status = "error"
            # Show a cleaner error message
            if "HTTPError" in stdout and "429" in stdout:
                _pipeline_message = (
                    "arXiv API rate limit exceeded (HTTP 429).\n\n"
                    "To avoid adding more load, AstroPaperDigest stopped "
                    "without automatic retries.\n\n"
                    "Please wait at least five minutes before running again. "
                    "Repeatedly clicking Re-run will extend the problem."
                )
            else:
                if "ERROR:" in stdout:
                    # The CLI prints clean single-line errors; prefer those
                    # over a raw traceback in the browser status page.
                    error_lines = [
                        line for line in stdout.splitlines()
                        if line.startswith("ERROR:")
                    ]
                    _pipeline_message = "\n".join(error_lines) or (stdout or "Unknown error")[-800:]
                else:
                    _pipeline_message = (stdout or "Unknown error")[-800:]
    except subprocess.TimeoutExpired:
        _pipeline_status = "error"
        _pipeline_message = "Pipeline timed out (>15 minutes)."
    except Exception as e:
        _pipeline_status = "error"
        _pipeline_message = str(e)


def _start_pipeline(
    include_cross: bool = True,
    include_replacements: bool = True,
    target_date: str = "",
) -> bool:
    """Start at most one pipeline process."""
    global _pipeline_status, _pipeline_message

    with _pipeline_lock:
        if _pipeline_status == "running":
            return False
        _pipeline_status = "running"
        _pipeline_message = "[1/5] Starting pipeline..."
        Thread(
            target=run_pipeline,
            kwargs={
                "include_cross": include_cross,
                "include_replacements": include_replacements,
                "target_date": target_date,
            },
            daemon=True,
        ).start()
    return True


# --- HTML Templates ---

SETUP_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstroPaperDigest - Setup</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333}
.header{background:#1a2332;color:#fff;padding:24px 32px;text-align:center}
.header h1{font-size:22px;margin-bottom:4px}
.header p{color:#8899aa;font-size:14px}
.container{max-width:640px;margin:32px auto;padding:0 16px}
.step{background:#fff;border-radius:10px;padding:24px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.step h2{font-size:16px;margin-bottom:16px;color:#2c3e50}
.step-num{display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:50%;background:#2563eb;color:#fff;font-size:12px;font-weight:700;margin-right:8px}
label{display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:6px;margin-top:14px}
input[type="text"],input[type="password"],select,textarea{width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;margin-bottom:4px}
input:focus,select:focus,textarea:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 2px rgba(37,99,235,.15)}
textarea{height:80px;resize:vertical}
.checkbox-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}
.checkbox-grid label{display:flex;align-items:center;gap:6px;font-weight:400;font-size:13px;margin:0}
.checkbox-grid input{width:auto}
.toggle-group{display:flex;gap:12px;margin-bottom:12px}
.toggle-group label{display:flex;align-items:center;gap:6px;font-weight:500;margin:0;cursor:pointer}
.toggle-group input{width:auto}
.hint{font-size:12px;color:#999;margin-top:4px}
.btn-submit{display:block;width:100%;padding:14px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;margin-top:24px}
.btn-submit:hover{background:#1d4ed8}
</style>
</head>
<body>
<div class="header">
  <h1>AstroPaperDigest</h1>
  <p>Let's set up your personalized paper recommendation system</p>
</div>
<div class="container">
<form method="POST" action="/setup" enctype="multipart/form-data">
  <div class="step">
    <h2><span class="step-num">1</span>LLM API Configuration</h2>
    <label for="provider">Provider</label>
    <select id="provider" name="provider" onchange="updateProvider()">
      <option value="deepseek">DeepSeek</option>
      <option value="openai">OpenAI</option>
      <option value="custom">Custom (OpenAI-compatible)</option>
    </select>
    <label for="api_key">API Key</label>
    <input type="password" id="api_key" name="api_key" placeholder="sk-..." value="{{ cur_api_key or '' }}" required>
    <label for="model">Model</label>
    <input type="text" id="model" name="model" value="{{ cur_model or 'deepseek-chat' }}">
    <div id="baseurl-group" style="display:none">
      <label for="base_url">Base URL</label>
      <input type="text" id="base_url" name="base_url" placeholder="https://api.example.com/v1" value="{{ cur_base_url or '' }}">
    </div>
  </div>

  <div class="step">
    <h2><span class="step-num">2</span>Research Interests</h2>
    <div class="toggle-group">
      <label><input type="radio" name="profile_mode" value="quick" checked onchange="toggleProfileMode()"> Quick Start</label>
      <label><input type="radio" name="profile_mode" value="bib" onchange="toggleProfileMode()"> Use Bib File</label>
    </div>
    <div id="quick-mode">
      <label>Arxiv Categories</label>
      <div class="checkbox-grid">
        <label><input type="checkbox" name="categories" value="astro-ph.GA" {% if 'astro-ph.GA' in cur_categories %}checked{% endif %}> astro-ph.GA</label>
        <label><input type="checkbox" name="categories" value="astro-ph.SR" {% if 'astro-ph.SR' in cur_categories %}checked{% endif %}> astro-ph.SR</label>
        <label><input type="checkbox" name="categories" value="astro-ph.HE" {% if 'astro-ph.HE' in cur_categories %}checked{% endif %}> astro-ph.HE</label>
        <label><input type="checkbox" name="categories" value="astro-ph.CO" {% if 'astro-ph.CO' in cur_categories %}checked{% endif %}> astro-ph.CO</label>
        <label><input type="checkbox" name="categories" value="astro-ph.IM" {% if 'astro-ph.IM' in cur_categories %}checked{% endif %}> astro-ph.IM</label>
        <label><input type="checkbox" name="categories" value="astro-ph.EP" {% if 'astro-ph.EP' in cur_categories %}checked{% endif %}> astro-ph.EP</label>
      </div>
      <label for="keywords">Keywords (comma-separated)</label>
      <textarea id="keywords" name="keywords" placeholder="e.g. first stars, chemical evolution, supernova, stellar abundances">{{ cur_keywords or '' }}</textarea>
      <p class="hint">These help filter and rank papers relevant to your research.</p>
    </div>
    <div id="bib-mode" style="display:none">
      <label>Upload your .bib file</label>
      <input type="file" name="bib_file" accept=".bib" style="width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px">
      <label for="bib_path" style="margin-top:10px">Or enter a file path</label>
      <input type="text" id="bib_path" name="bib_path" placeholder="/path/to/your/collection.bib" value="{{ cur_bib_file or '' }}">
      <p class="hint">We'll extract your research profile automatically from your bibliography.</p>
    </div>
  </div>

  <div class="step">
    <h2><span class="step-num">3</span>Email Notification (Optional)</h2>
    <label for="email_sender">Sender Email</label>
    <input type="text" id="email_sender" name="email_sender" placeholder="you@example.com" value="{{ cur_email_sender or '' }}">
    <label for="email_recipient">Recipient Email</label>
    <input type="text" id="email_recipient" name="email_recipient" placeholder="you@example.com" value="{{ cur_email_recipient or '' }}">
    <label for="smtp_server">SMTP Server</label>
    <input type="text" id="smtp_server" name="smtp_server" placeholder="smtp.gmail.com" value="{{ cur_smtp_server or '' }}">
    <label for="smtp_protocol">Protocol</label>
    <select id="smtp_protocol" name="smtp_protocol" onchange="updatePort()">
      <option value="starttls" {% if not cur_use_ssl %}selected{% endif %}>STARTTLS (port 587)</option>
      <option value="ssl" {% if cur_use_ssl %}selected{% endif %}>SSL (port 465)</option>
    </select>
    <label for="smtp_port">Port (optional, auto-filled)</label>
    <input type="text" id="smtp_port" name="smtp_port" placeholder="587" value="{{ cur_smtp_port or '587' }}">
    <label for="email_password">Email Password / App Password</label>
    <input type="password" id="email_password" name="email_password" placeholder="App password" value="{{ cur_email_password or '' }}">
    <p class="hint">Leave all empty to skip email notifications.</p>
  </div>

  <div class="step">
    <h2><span class="step-num">4</span>Ready to Go</h2>
    <p style="font-size:14px;color:#666">Click below to save your configuration and start fetching papers.</p>
    <button type="submit" class="btn-submit">Start</button>
  </div>
</form>
</div>
<script>
function updateProvider() {
  const p = document.getElementById('provider').value;
  const model = document.getElementById('model');
  const urlGroup = document.getElementById('baseurl-group');
  const baseUrl = document.getElementById('base_url');
  if (p === 'deepseek') { model.value = 'deepseek-chat'; urlGroup.style.display = 'none'; }
  else if (p === 'openai') { model.value = 'gpt-4o-mini'; urlGroup.style.display = 'none'; }
  else { model.value = ''; urlGroup.style.display = 'block'; baseUrl.focus(); }
}
function toggleProfileMode() {
  const mode = document.querySelector('input[name="profile_mode"]:checked').value;
  document.getElementById('quick-mode').style.display = mode === 'quick' ? '' : 'none';
  document.getElementById('bib-mode').style.display = mode === 'bib' ? '' : 'none';
}
function updatePort() {
  const proto = document.getElementById('smtp_protocol').value;
  document.getElementById('smtp_port').value = proto === 'ssl' ? '465' : '587';
}
</script>
</body>
</html>"""

STATUS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstroPaperDigest</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333}
.header{background:#1a2332;color:#fff;padding:20px 32px}
.header h1{font-size:22px;margin-bottom:4px}
.header .stats{color:#8899aa;font-size:14px}
.sticky-wrapper{position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.toolbar{background:#fff;padding:10px 32px;border-bottom:1px solid #e0e0e0;display:flex;gap:10px;align-items:center;flex-wrap:nowrap;overflow-x:auto}
.toolbar button{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500}
.btn-refresh{background:#3498db;color:#fff}.btn-refresh:hover{background:#2980b9}
.btn-nav{background:#ecf0f1;color:#555}.btn-nav:hover{background:#dfe6e9}
.date-display{font-size:16px;font-weight:600;color:#fff;cursor:default;padding:4px 12px;border-radius:6px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2)}
.date-arrow{background:rgba(255,255,255,.12);color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:default;font-size:15px;line-height:1;opacity:.5}
.btn-today{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px;font-weight:600}
.btn-today:hover{background:rgba(255,255,255,.3)}
.loading-area{text-align:center;padding:80px 20px;color:#999}
.loading-area .spinner{width:40px;height:40px;border:3px solid #e0e0e0;border-top-color:#3498db;border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 20px}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-area .msg{color:#888;font-size:15px;margin-bottom:8px}
.loading-area .error{color:#e74c3c;font-size:13px;max-width:600px;margin:16px auto 0;text-align:left;background:rgba(231,76,60,.08);padding:16px;border-radius:8px;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto}
.btn{margin-top:20px;padding:10px 24px;background:#3498db;color:#fff;border:none;border-radius:6px;font-size:14px;cursor:pointer}
.btn:hover{background:#2980b9}
</style>
</head>
<body>
<div class="sticky-wrapper">
<div class="header">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
    <h1 style="margin:0;font-size:20px"><img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' width='20' height='20'%3E%3Cpath fill='%23fff' d='M19 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h13c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-1 14H7v-2h11v2zm0-4H7v-2h11v2zm0-4H7V6h11v2z'/%3E%3C/svg%3E" style="vertical-align:middle;margin-right:6px" width="20" height="20">AstroPaperDigest</h1>
    <div style="display:flex;align-items:center;gap:8px">
      <button class="date-arrow" onclick="shiftDate(-1)" style="cursor:pointer;opacity:1">&larr;</button>
      <span class="date-display" id="date-label" onclick="openDatePicker()" style="cursor:pointer">{{ display_date }}</span>
      <button class="date-arrow" id="arrow-right" onclick="shiftDate(1)" style="cursor:pointer;opacity:1">&rarr;</button>
      <button class="btn-today" onclick="goToToday()">Today</button>
      <input type="date" id="date-picker" value="{{ display_date }}" max="{{ today_str }}" onchange="navigateToDate(this.value)" style="position:absolute;opacity:0;pointer-events:none;width:0;height:0">
    </div>
  </div>
</div>
<div class="toolbar">
  <button class="btn-refresh" disabled style="opacity:.6" title="Running...">&#x21bb;</button>
  <button class="btn-nav" style="opacity:.5">Highly Relevant (...)</button>
  <button class="btn-nav" style="opacity:.5">Possibly Relevant (...)</button>
  <button class="btn-nav" style="opacity:.5">Marginal (...)</button>
</div>
</div>
<div class="loading-area">
  <div class="spinner" id="spinner"></div>
  <p class="msg" id="msg">Starting pipeline...</p>
  <div class="error" id="error" style="display:none"></div>
  <button class="btn" id="retry" style="display:none" onclick="location.href='/run?date={{ display_date }}'">Retry</button>
  <button class="btn" id="back" style="display:none;margin-left:10px;background:#7f8c8d" onclick="location.href='/digest/{{ display_date }}'">Back to Digest</button>
</div>
<script>
function navigateToDate(dateStr) {
  if (!dateStr || !/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return;
  fetch('/preferences', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({last_viewed_date: dateStr})});
  window.location.href = '/digest/' + dateStr;
}
function shiftDate(delta) {
  const picker = document.getElementById('date-picker');
  const today = '{{ today_str }}';
  if (!picker.value || !/^\d{4}-\d{2}-\d{2}$/.test(picker.value)) picker.value = today;
  if (delta > 0 && picker.value >= today) return;
  const parts = picker.value.split('-');
  const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
  if (isNaN(d.getTime())) { picker.value = today; return; }
  d.setDate(d.getDate() + delta);
  const y = d.getFullYear();
  const m = String(d.getMonth()+1).padStart(2,'0');
  const day = String(d.getDate()).padStart(2,'0');
  const newDate = y + '-' + m + '-' + day;
  picker.value = newDate;
  navigateToDate(newDate);
}
function updateArrows() {
  const picker = document.getElementById('date-picker');
  const today = '{{ today_str }}';
  const rightBtn = document.getElementById('arrow-right');
  if (rightBtn && picker) {
    const disabled = picker.value >= today;
    rightBtn.disabled = disabled;
    rightBtn.style.opacity = disabled ? '0.3' : '1';
    rightBtn.style.cursor = disabled ? 'default' : 'pointer';
  }
}
updateArrows();
function openDatePicker() {
  const picker = document.getElementById('date-picker');
  picker.style.pointerEvents = 'auto';
  picker.showPicker ? picker.showPicker() : picker.click();
  setTimeout(() => { picker.style.pointerEvents = 'none'; }, 500);
}
function goToToday() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth()+1).padStart(2,'0');
  const d = String(now.getDate()).padStart(2,'0');
  navigateToDate(y + '-' + m + '-' + d);
}
function poll() {
  fetch('/status').then(r=>r.json()).then(d=>{
    document.getElementById('msg').textContent = d.message;
    if(d.status === 'done') {
      window.location.href = '/digest';
    } else if(d.status === 'error') {
      document.getElementById('spinner').style.display = 'none';
      document.getElementById('error').style.display = 'block';
      document.getElementById('error').textContent = d.message;
      document.getElementById('retry').style.display = 'inline-block';
      document.getElementById('back').style.display = 'inline-block';
      document.getElementById('msg').textContent = 'Pipeline failed.';
    } else {
      setTimeout(poll, 2000);
    }
  }).catch(()=>setTimeout(poll, 3000));
}
poll();
</script>
</body>
</html>"""

DIGEST_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstroPaperDigest - {{ digest.date }}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333}
.header{background:#1a2332;color:#fff;padding:20px 32px}
.header h1{font-size:22px;margin-bottom:4px}
.header .stats{color:#8899aa;font-size:14px}
.sticky-wrapper{position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.toolbar{background:#fff;padding:10px 32px;border-bottom:1px solid #e0e0e0;display:flex;gap:10px;align-items:center;flex-wrap:nowrap;overflow-x:auto}
.toolbar button{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500}
.btn-refresh{background:#3498db;color:#fff}.btn-refresh:hover{background:#2980b9}
.btn-nav{background:#ecf0f1;color:#555}.btn-nav:hover{background:#dfe6e9}
.btn-nav-active{background:#2c3e50;color:#fff}
.container{max-width:900px;margin:0 auto;padding:24px 16px}
.tier-header{margin:28px 0 12px;padding-bottom:8px;border-bottom:2px solid #e0e0e0;scroll-margin-top:165px}
.tier-header h2{font-size:18px}
.tier-highly{color:#27ae60;border-color:#27ae60}
.tier-possibly{color:#f39c12;border-color:#f39c12}
.tier-marginal{color:#95a5a6;border-color:#95a5a6}
.card{background:#fff;border-radius:10px;padding:18px 20px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.08);transition:box-shadow .2s}
.card:hover{box-shadow:0 3px 12px rgba(0,0,0,.12)}
.card-title{font-size:15px;font-weight:600;color:#2c3e50;margin-bottom:6px;display:flex;align-items:flex-start;gap:10px}
.score-badge{display:inline-block;min-width:38px;text-align:center;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:700;color:#fff;flex-shrink:0}
.score-high{background:#27ae60}.score-mid{background:#f39c12}.score-low{background:#e74c3c}
.card-reason{font-size:13px;color:#666;margin-bottom:4px}
.card-meta{font-size:12px;color:#999;margin-bottom:8px}
.card-abstract{font-size:13px;color:#555;line-height:1.5;margin-bottom:10px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card-actions{display:flex;gap:8px;align-items:center}
.card-actions a{font-size:12px;color:#3498db;text-decoration:none}
.card-actions a:hover{text-decoration:underline}
.fb-btn{padding:4px 12px;border:1px solid #ddd;border-radius:14px;font-size:12px;cursor:pointer;background:#fff;transition:all .2s}
.fb-btn:hover{border-color:#999}
.fb-overrated{color:#e74c3c}.fb-overrated:hover,.fb-overrated.active{background:#e74c3c;color:#fff;border-color:#e74c3c}
.fb-underrated{color:#27ae60}.fb-underrated:hover,.fb-underrated.active{background:#27ae60;color:#fff;border-color:#27ae60}
.checkbox-group{display:flex;gap:15px;align-items:center;margin-left:auto;font-size:13px}
.checkbox-group label{display:flex;align-items:center;gap:5px;cursor:pointer;color:#555}
.checkbox-group input[type="checkbox"]{cursor:pointer;width:16px;height:16px}
.date-display{font-size:16px;font-weight:600;color:#fff;cursor:pointer;padding:4px 12px;border-radius:6px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);user-select:none}
.date-display:hover{background:rgba(255,255,255,.2)}
.date-arrow{background:rgba(255,255,255,.12);color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:15px;line-height:1}
.date-arrow:hover{background:rgba(255,255,255,.25)}
.btn-today{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px;font-weight:600}
.btn-today:hover{background:rgba(255,255,255,.3)}
.loading-overlay{text-align:center;padding:80px 20px;color:#999}
.loading-overlay .spinner{width:40px;height:40px;border:3px solid #e0e0e0;border-top-color:#3498db;border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 20px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="sticky-wrapper">
<div class="header">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
    <h1 style="margin:0;font-size:20px"><img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' width='20' height='20'%3E%3Cpath fill='%23fff' d='M19 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h13c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-1 14H7v-2h11v2zm0-4H7v-2h11v2zm0-4H7V6h11v2z'/%3E%3C/svg%3E" style="vertical-align:middle;margin-right:6px" width="20" height="20">AstroPaperDigest</h1>
    <div style="display:flex;align-items:center;gap:8px">
      <button class="date-arrow" onclick="shiftDate(-1)">&larr;</button>
      <span class="date-display" id="date-label" onclick="openDatePicker()">{{ digest.date }}</span>
      <button class="date-arrow" id="arrow-right" onclick="shiftDate(1)">&rarr;</button>
      <button class="btn-today" onclick="goToToday()">Today</button>
      <input type="date" id="date-picker" value="{{ digest.date }}" max="{{ today_str }}" onchange="navigateToDate(this.value)" style="position:absolute;opacity:0;pointer-events:none;width:0;height:0">
    </div>
  </div>
</div>
<div class="toolbar">
  <button class="btn-refresh" onclick="rerunWithPrefs()" title="Re-run pipeline" style="font-size:18px">&#x21bb;</button>
  <button class="btn-nav" onclick="location.href='/settings'" title="Settings" style="font-size:18px">&#x2699;</button>
  <span class="stats" style="font-size:13px;color:#666">Total: {{ digest.total_papers }} papers &nbsp;|&nbsp; Highly relevant: {{ digest.highly_relevant_count }}</span>
  {% for tier in digest.tiers %}
  <button class="btn-nav" data-tier-idx="{{ loop.index0 }}" onclick="document.getElementById('tier-{{ loop.index }}').scrollIntoView({behavior:'smooth'})">{{ tier.name }} (<span class="btn-tier-count">{{ tier.papers|length }}</span>)</button>
  {% endfor %}
  <div class="checkbox-group">
    <label><input type="checkbox" id="chk-cross" {% if prefs.include_cross %}checked{% endif %}> Cross-listed</label>
    <label><input type="checkbox" id="chk-repl" {% if prefs.include_replacements %}checked{% endif %}> Replacements</label>
  </div>
</div>
</div>
<div class="container">
{% for tier in digest.tiers %}
  <div class="tier-header {% if 'Highly' in tier.name %}tier-highly{% elif 'Possibly' in tier.name %}tier-possibly{% else %}tier-marginal{% endif %}" id="tier-{{ loop.index }}">
    <h2><span class="tier-name-text">{{ tier.name }}</span> (<span class="tier-count">{{ tier.papers|length }}</span>)</h2>
  </div>
  {% for paper in tier.papers %}
  <div class="card" id="card-{{ paper.paper_id | replace('.', '-') }}" data-paper-type="{{ paper.paper_type | default('new') }}">
    <div class="card-title">
      <span class="score-badge {% if paper.score >= 7 %}score-high{% elif paper.score >= 5 %}score-mid{% else %}score-low{% endif %}">{{ paper.score }}/10</span>
      <span>{{ paper.title }}</span>
    </div>
    {% if paper.reason %}<div class="card-reason">{{ paper.reason }}</div>{% endif %}
    <div class="card-meta">{{ paper.authors[:80] }}{% if paper.categories %} &nbsp;|&nbsp; {{ paper.categories }}{% endif %}</div>
    {% if paper.abstract %}<div class="card-abstract">{{ paper.abstract[:300] }}</div>{% endif %}
    <div class="card-actions">
      {% if paper.link %}<a href="{{ paper.link }}" target="_blank">arxiv:{{ paper.paper_id }}</a>{% endif %}
      <button class="fb-btn fb-overrated" data-id="{{ paper.paper_id }}" data-title="{{ paper.title[:80] }}" data-score="{{ paper.score }}" onclick="giveFeedback(this,'overrated')">Overrated</button>
      <button class="fb-btn fb-underrated" data-id="{{ paper.paper_id }}" data-title="{{ paper.title[:80] }}" data-score="{{ paper.score }}" onclick="giveFeedback(this,'underrated')">Underrated</button>
    </div>
  </div>
  {% endfor %}
{% endfor %}
</div>
<script>
function giveFeedback(btn, action) {
  const id = btn.dataset.id, title = btn.dataset.title, score = btn.dataset.score;
  const wasActive = btn.classList.contains('active');
  fetch('/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({paper_id:id, title:title, action: wasActive ? 'cancel' : action, original_score:parseInt(score)})
  }).then(r=>r.json()).then(d=>{
    if(d.ok){
      const parent = btn.parentElement;
      parent.querySelectorAll('.fb-btn').forEach(b=>b.classList.remove('active'));
      if(!wasActive) btn.classList.add('active');
    }
  });
}
fetch('/feedback').then(r=>r.json()).then(list=>{
  list.forEach(fb=>{
    const btns = document.querySelectorAll(`[data-id="${fb.paper_id}"]`);
    btns.forEach(b=>{
      if(b.classList.contains('fb-'+fb.action)) b.classList.add('active');
    });
  });
});

function rerunWithPrefs() {
  const includeCross = document.getElementById('chk-cross').checked;
  const includeRepl = document.getElementById('chk-repl').checked;
  const picker = document.getElementById('date-picker');
  const targetDate = (picker && /^\d{4}-\d{2}-\d{2}$/.test(picker.value)) ? picker.value : '';
  // Save preferences
  fetch('/preferences', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({include_cross: includeCross, include_replacements: includeRepl})
  }).then(() => {
    // Navigate to run page with target date
    window.location.href = '/run?date=' + targetDate;
  });
}

function navigateToDate(dateStr) {
  if (!dateStr || !/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return;
  // Save last viewed date
  fetch('/preferences', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({last_viewed_date: dateStr})
  });
  window.location.href = '/digest/' + dateStr;
}
function shiftDate(delta) {
  const picker = document.getElementById('date-picker');
  const today = '{{ today_str }}';
  if (!picker.value || !/^\d{4}-\d{2}-\d{2}$/.test(picker.value)) picker.value = today;
  if (delta > 0 && picker.value >= today) return;
  const parts = picker.value.split('-');
  const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
  if (isNaN(d.getTime())) { picker.value = today; return; }
  d.setDate(d.getDate() + delta);
  const y = d.getFullYear();
  const m = String(d.getMonth()+1).padStart(2,'0');
  const day = String(d.getDate()).padStart(2,'0');
  const newDate = y + '-' + m + '-' + day;
  picker.value = newDate;
  navigateToDate(newDate);
}
function updateArrows() {
  const picker = document.getElementById('date-picker');
  const today = '{{ today_str }}';
  const rightBtn = document.getElementById('arrow-right');
  if (rightBtn && picker) {
    const disabled = picker.value >= today;
    rightBtn.disabled = disabled;
    rightBtn.style.opacity = disabled ? '0.3' : '1';
    rightBtn.style.cursor = disabled ? 'default' : 'pointer';
  }
}
updateArrows();
function openDatePicker() {
  const picker = document.getElementById('date-picker');
  picker.style.pointerEvents = 'auto';
  picker.showPicker ? picker.showPicker() : picker.click();
  setTimeout(() => { picker.style.pointerEvents = 'none'; }, 500);
}
function goToToday() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth()+1).padStart(2,'0');
  const d = String(now.getDate()).padStart(2,'0');
  navigateToDate(y + '-' + m + '-' + d);
}

// Save preferences when checkboxes change
document.getElementById('chk-cross').addEventListener('change', onCheckboxChange);
document.getElementById('chk-repl').addEventListener('change', onCheckboxChange);

function onCheckboxChange() {
  const includeCross = document.getElementById('chk-cross').checked;
  const includeRepl = document.getElementById('chk-repl').checked;
  
  // Save preferences
  fetch('/preferences', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({include_cross: includeCross, include_replacements: includeRepl})
  });
  
  // Filter cards live
  filterCards();
}

function filterCards() {
  const includeCross = document.getElementById('chk-cross').checked;
  const includeRepl = document.getElementById('chk-repl').checked;
  
  const allCards = document.querySelectorAll('.card');
  let totalVisible = 0;
  
  allCards.forEach(card => {
    const type = card.dataset.paperType || 'new';
    let show = true;
    
    if (type === 'cross' && !includeCross) show = false;
    if (type === 'replacement' && !includeRepl) show = false;
    
    card.style.display = show ? '' : 'none';
    if (show) totalVisible++;
  });
  
  // Update tier counts by iterating siblings
  const tierHeaders = document.querySelectorAll('.tier-header');
  tierHeaders.forEach((tier, idx) => {
    const nextTier = tierHeaders[idx + 1];
    let visibleCount = 0;
    let el = tier.nextElementSibling;
    while (el && el !== nextTier) {
      if (el.classList.contains('card') && el.style.display !== 'none') {
        visibleCount++;
      }
      el = el.nextElementSibling;
    }
    const countEl = tier.querySelector('.tier-count');
    if (countEl) countEl.textContent = visibleCount;
    // Also update toolbar button count
    const btnCount = document.querySelector(`.btn-nav[data-tier-idx="${idx}"] .btn-tier-count`);
    if (btnCount) btnCount.textContent = visibleCount;
  });
  
  // Update total in header
  const statsEl = document.querySelector('.stats');
  if (statsEl) {
    const highlyRelevant = {{ digest.highly_relevant_count }};
    statsEl.innerHTML = `Total: ${totalVisible} papers &nbsp;|&nbsp; Highly relevant: ${highlyRelevant}`;
  }
}
</script>
<script>
// Apply initial filter on page load
filterCards();
</script>
</body>
</html>"""

NO_DIGEST_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstroPaperDigest - {{ selected_date }}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333}
.header{background:#1a2332;color:#fff;padding:20px 32px}
.header h1{font-size:22px;margin-bottom:4px}
.header .stats{color:#8899aa;font-size:14px}
.sticky-wrapper{position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.toolbar{background:#fff;padding:10px 32px;border-bottom:1px solid #e0e0e0;display:flex;gap:10px;align-items:center;flex-wrap:nowrap;overflow-x:auto}
.toolbar button{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500}
.btn-refresh{background:#3498db;color:#fff}.btn-refresh:hover{background:#2980b9}
.btn-nav{background:#ecf0f1;color:#555}.btn-nav:hover{background:#dfe6e9}
.no-data{text-align:center;padding:80px 20px;color:#999}
.no-data h2{font-size:20px;color:#666;margin-bottom:12px}
.no-data p{font-size:14px;margin-bottom:24px}
.no-data a{color:#3498db;text-decoration:none}
.date-display{font-size:16px;font-weight:600;color:#fff;cursor:pointer;padding:4px 12px;border-radius:6px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);user-select:none}
.date-display:hover{background:rgba(255,255,255,.2)}
.date-arrow{background:rgba(255,255,255,.12);color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:15px;line-height:1}
.date-arrow:hover{background:rgba(255,255,255,.25)}
.btn-today{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px;font-weight:600}
.btn-today:hover{background:rgba(255,255,255,.3)}
</style>
</head>
<body>
<div class="sticky-wrapper">
<div class="header">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
    <h1 style="margin:0;font-size:20px"><img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' width='20' height='20'%3E%3Cpath fill='%23fff' d='M19 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h13c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-1 14H7v-2h11v2zm0-4H7v-2h11v2zm0-4H7V6h11v2z'/%3E%3C/svg%3E" style="vertical-align:middle;margin-right:6px" width="20" height="20">AstroPaperDigest</h1>
    <div style="display:flex;align-items:center;gap:8px">
      <button class="date-arrow" onclick="shiftDate(-1)">&larr;</button>
      <span class="date-display" id="date-label" onclick="openDatePicker()">{{ selected_date }}</span>
      <button class="date-arrow" id="arrow-right" onclick="shiftDate(1)">&rarr;</button>
      <button class="btn-today" onclick="goToToday()">Today</button>
      <input type="date" id="date-picker" value="{{ selected_date }}" max="{{ today_str }}" onchange="navigateToDate(this.value)" style="position:absolute;opacity:0;pointer-events:none;width:0;height:0">
    </div>
  </div>
</div>
<div class="toolbar">
  <button class="btn-refresh" onclick="location.href='/run?date={{ selected_date }}'" title="Re-run pipeline">&#x21bb;</button>
  <span style="font-size:13px;color:#666">No data available</span>
</div>
</div>
<div class="no-data">
  <h2>{% if custom_message %}{{ custom_message }}{% elif is_update_day %}No digest for {{ selected_date }}{% else %}No arxiv update on {{ selected_date }}{% endif %}</h2>
  <p>{% if custom_message %}Use the date picker to browse other dates, or click &#x21bb; to re-run.{% elif is_update_day %}No papers have been fetched for this date yet.{% else %}Arxiv does not announce papers on weekends or holidays.{% endif %}</p>
  {% if is_update_day and not custom_message %}<button style="margin-top:28px;margin-bottom:24px;padding:14px 36px;font-size:16px;font-weight:600;color:#fff;background:#2563eb;border:none;border-radius:8px;cursor:pointer;box-shadow:0 2px 8px rgba(37,99,235,.3);transition:background .2s" onmouseover="this.style.background='#1d4ed8'" onmouseout="this.style.background='#2563eb'" onclick="location.href='/run?date={{ selected_date }}'">Generate Digest for {{ selected_date }}</button>{% endif %}
  {% if available_dates %}<p style="font-size:12px;color:#aaa">Available: {{ available_dates | join(', ') }}</p>{% endif %}
</div>
<script>
function navigateToDate(dateStr) {
  if (!dateStr || !/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return;
  fetch('/preferences', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({last_viewed_date: dateStr})});
  window.location.href = '/digest/' + dateStr;
}
function shiftDate(delta) {
  const picker = document.getElementById('date-picker');
  const today = '{{ today_str }}';
  if (!picker.value || !/^\d{4}-\d{2}-\d{2}$/.test(picker.value)) picker.value = today;
  if (delta > 0 && picker.value >= today) return;
  const parts = picker.value.split('-');
  const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
  if (isNaN(d.getTime())) { picker.value = today; return; }
  d.setDate(d.getDate() + delta);
  const y = d.getFullYear();
  const m = String(d.getMonth()+1).padStart(2,'0');
  const day = String(d.getDate()).padStart(2,'0');
  const newDate = y + '-' + m + '-' + day;
  picker.value = newDate;
  navigateToDate(newDate);
}
function updateArrows() {
  const picker = document.getElementById('date-picker');
  const today = '{{ today_str }}';
  const rightBtn = document.getElementById('arrow-right');
  if (rightBtn && picker) {
    const disabled = picker.value >= today;
    rightBtn.disabled = disabled;
    rightBtn.style.opacity = disabled ? '0.3' : '1';
    rightBtn.style.cursor = disabled ? 'default' : 'pointer';
  }
}
updateArrows();
function openDatePicker() {
  const picker = document.getElementById('date-picker');
  picker.style.pointerEvents = 'auto';
  picker.showPicker ? picker.showPicker() : picker.click();
  setTimeout(() => { picker.style.pointerEvents = 'none'; }, 500);
}
function goToToday() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth()+1).padStart(2,'0');
  const d = String(now.getDate()).padStart(2,'0');
  navigateToDate(y + '-' + m + '-' + d);
}
</script>
</body>
</html>"""


# --- Routes ---

# Arxiv 2026 holidays (no announcements on these dates)
_ARXIV_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2026-12-29", "2026-12-31",
}

def _is_arxiv_update_day(date_str: str) -> bool:
    """Check if date is a valid arxiv update day (weekday + not holiday)."""
    try:
        d = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return False
    if d.weekday() >= 5:  # Sat/Sun
        return False
    if date_str in _ARXIV_HOLIDAYS_2026:
        return False
    return True


def _render_digest(digest=None):
    """Render digest template with computed variables."""
    d = digest or _current_digest
    today_str = date.today().isoformat()
    # Old digest files may not carry a parseable date; fall back to today so
    # date navigation and the update-day check never see an empty value.
    if not d.get("date"):
        d["date"] = today_str
    # Handle empty digests (0 papers)
    if d["total_papers"] == 0:
        status = d.get("status", "no_papers")
        is_today = (d.get("date", "") == today_str)
        if is_today and status == "no_papers":
            msg = "No available papers (today's arxiv has not been updated yet)"
        elif is_today and status == "no_new_papers":
            msg = "No new papers since last digest"
        else:
            msg = "No papers available for this date"
        available = get_available_dates()
        return render_template_string(
            NO_DIGEST_TEMPLATE,
            selected_date=d.get("date", today_str),
            today_str=today_str,
            available_dates=available,
            custom_message=msg,
            is_update_day=_is_arxiv_update_day(d.get("date", today_str))
        )
    prefs = load_preferences()
    today_str = date.today().isoformat()
    return render_template_string(DIGEST_TEMPLATE, digest=d, prefs=prefs, today_str=today_str)


@app.route("/setup", methods=["GET"])
@app.route("/settings", methods=["GET"])
def setup_page():
    """Show the setup/settings page."""
    import yaml
    # Try to load current config for pre-filling
    cfg = {}
    config_path = os.path.join(_PROJECT_DIR, "config.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    # Read .env for API key
    env_vars = {}
    env_path = os.path.join(_PROJECT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip('"')
    email_cfg = cfg.get("email", {})
    llm_cfg = cfg.get("llm", {})
    return render_template_string(SETUP_TEMPLATE,
        cur_model=llm_cfg.get("model", "deepseek-chat"),
        cur_base_url=llm_cfg.get("base_url", ""),
        cur_api_key=env_vars.get("DEEPSEEK_API_KEY", env_vars.get("OPENAI_API_KEY", env_vars.get("CUSTOM_API_KEY", ""))),
        cur_categories=cfg.get("arxiv_categories", []),
        cur_keywords=", ".join(cfg.get("keywords", [])),
        cur_bib_file=cfg.get("bib_file", ""),
        cur_email_sender=email_cfg.get("sender", ""),
        cur_email_recipient=email_cfg.get("recipient", ""),
        cur_smtp_server=email_cfg.get("smtp_server", ""),
        cur_smtp_port=str(email_cfg.get("smtp_port", "587")),
        cur_use_ssl=email_cfg.get("use_ssl", False),
        cur_email_password=env_vars.get("EMAIL_APP_PASSWORD", ""),
    )


@app.route("/setup", methods=["POST"])
def setup_submit():
    """Process setup form and write configuration."""
    import yaml

    provider = request.form.get("provider", "deepseek")
    api_key = request.form.get("api_key", "").strip()
    model = request.form.get("model", "").strip()
    base_url = request.form.get("base_url", "").strip()
    profile_mode = request.form.get("profile_mode", "quick")
    categories = request.form.getlist("categories")
    keywords_raw = request.form.get("keywords", "").strip()
    bib_path = request.form.get("bib_path", "").strip()
    email_sender = request.form.get("email_sender", "").strip()
    email_recipient = request.form.get("email_recipient", "").strip()
    smtp_server = request.form.get("smtp_server", "").strip()
    smtp_protocol = request.form.get("smtp_protocol", "starttls")
    smtp_port_value = request.form.get("smtp_port", "").strip()
    email_password = request.form.get("email_password", "").strip()

    # Determine API key env var name and base URL
    if provider == "deepseek":
        api_key_env = "DEEPSEEK_API_KEY"
        base_url = "https://api.deepseek.com"
    elif provider == "openai":
        api_key_env = "OPENAI_API_KEY"
        base_url = "https://api.openai.com/v1"
    else:
        api_key_env = "CUSTOM_API_KEY"

    if provider == "custom" and not base_url:
        abort(400, "Base URL is required for a custom provider.")
    if not model:
        abort(400, "Model name is required.")

    try:
        smtp_port = int(smtp_port_value) if smtp_port_value else (
            465 if smtp_protocol == "ssl" else 587
        )
    except ValueError:
        abort(400, "SMTP port must be an integer.")
    if not 1 <= smtp_port <= 65535:
        abort(400, "SMTP port must be between 1 and 65535.")

    # Write .env file
    env_path = os.path.join(_PROJECT_DIR, ".env")
    env_values = {
        api_key_env: api_key,
        "EMAIL_APP_PASSWORD": email_password,
        "EMAIL_SENDER": email_sender,
        "EMAIL_RECIPIENT": email_recipient or email_sender,
        "SMTP_SERVER": smtp_server,
        "SMTP_PORT": str(smtp_port),
    }
    with open(env_path, "w", encoding="utf-8") as f:
        for key, value in env_values.items():
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            f.write(f'{key}="{escaped}"\n')
    os.chmod(env_path, 0o600)

    # Load existing config and update
    config_path = os.path.join(_PROJECT_DIR, "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Update LLM config
    config["llm"]["base_url"] = base_url
    config["llm"]["api_key_env"] = api_key_env
    config["llm"]["model"] = model

    # Update research interests
    if profile_mode == "quick":
        if categories:
            config["arxiv_categories"] = categories
        if keywords_raw:
            keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
            config["keywords"] = keywords
    else:
        # Handle bib file upload or path
        bib_file = request.files.get("bib_file")
        if bib_file and bib_file.filename:
            data_dir = os.path.join(_PROJECT_DIR, "data")
            os.makedirs(data_dir, exist_ok=True)
            filename = secure_filename(bib_file.filename)
            if not filename:
                abort(400, "Invalid BibTeX filename.")
            bib_file.save(os.path.join(data_dir, filename))
            config["bib_file"] = f"data/{filename}"
        elif bib_path:
            config["bib_file"] = bib_path

    # Update email config
    if email_sender and smtp_server:
        config["email"]["enabled"] = True
        config["email"]["sender"] = email_sender
        config["email"]["recipient"] = email_recipient or email_sender
        config["email"]["smtp_server"] = smtp_server
        config["email"]["use_ssl"] = (smtp_protocol == "ssl")
        config["email"]["smtp_port"] = smtp_port
        config["email"]["password_env"] = "EMAIL_APP_PASSWORD"
    else:
        # Leaving the email section empty should disable notifications,
        # not keep stale default credentials active.
        config["email"]["enabled"] = False

    # Write updated config
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Reload env for current process
    os.environ[api_key_env] = api_key

    return redirect("/")


@app.route("/")
def index():
    """Landing page: show last viewed or latest digest."""
    if _pipeline_status == "running":
        prefs = load_preferences()
        display_date = prefs.get("last_viewed_date") or date.today().isoformat()
        return render_template_string(STATUS_PAGE, display_date=display_date, today_str=date.today().isoformat())
    # Try last viewed date first
    prefs = load_preferences()
    last_date = prefs.get("last_viewed_date", "")
    if last_date:
        digest_path = get_digest_path_for_date(last_date)
        if digest_path:
            digest = parse_digest(digest_path)
            return _render_digest(digest)
    # Fall back to latest available digest
    if _current_digest:
        return _render_digest()
    # Try latest available
    available = get_available_dates()
    if available:
        digest_path = get_digest_path_for_date(available[0])
        if digest_path:
            digest = parse_digest(digest_path)
            return _render_digest(digest)
    # Nothing available
    today_str = date.today().isoformat()
    return render_template_string(
        NO_DIGEST_TEMPLATE,
        selected_date=today_str,
        today_str=today_str,
        available_dates=available,
        is_update_day=_is_arxiv_update_day(today_str)
    )


@app.route("/digest")
def digest_page():
    """Show the latest digest (fall back to the landing page if not ready)."""
    if _current_digest:
        return _render_digest()
    return redirect("/")


@app.route("/digest/<date_str>")
def digest_by_date(date_str):
    """Show digest for a specific date, or auto-run / show no-update."""
    try:
        date.fromisoformat(date_str)
    except ValueError:
        abort(404)

    digest_path = get_digest_path_for_date(date_str)
    if digest_path:
        digest = parse_digest(digest_path)
        return _render_digest(digest)
    # No digest for this date
    today_str = date.today().isoformat()
    available = get_available_dates()
    # Future dates: show no-update page, do NOT auto-run
    if date_str > today_str:
        return render_template_string(
            NO_DIGEST_TEMPLATE,
            selected_date=date_str,
            available_dates=available,
            today_str=today_str,
            is_update_day=_is_arxiv_update_day(date_str)
        )
    # Auto-run pipeline if navigating to today and no digest exists yet
    if date_str == today_str:
        prefs = load_preferences()
        _start_pipeline(
            include_cross=prefs.get("include_cross", True),
            include_replacements=prefs.get("include_replacements", True),
        )
        return render_template_string(STATUS_PAGE, display_date=date_str, today_str=today_str)
    return render_template_string(
        NO_DIGEST_TEMPLATE,
        selected_date=date_str,
        available_dates=available,
        today_str=today_str,
        is_update_day=_is_arxiv_update_day(date_str)
    )


@app.route("/status")
def status():
    """JSON status endpoint for polling."""
    return jsonify({
        "app": "AstroPaperDigest",
        "status": _pipeline_status,
        "message": _pipeline_message,
    })


@app.route("/run")
def run():
    """Trigger a pipeline run and show status page."""
    target_date = request.args.get("date", "")
    if target_date:
        try:
            date.fromisoformat(target_date)
        except ValueError:
            abort(400, "Date must use YYYY-MM-DD format.")
    prefs = load_preferences()
    _start_pipeline(
        include_cross=prefs.get("include_cross", True),
        include_replacements=prefs.get("include_replacements", True),
        target_date=target_date,
    )
    return render_template_string(STATUS_PAGE, display_date=target_date or date.today().isoformat(), today_str=date.today().isoformat())


@app.route("/preferences", methods=["GET"])
def get_preferences():
    return jsonify(load_preferences())


@app.route("/preferences", methods=["POST"])
def post_preferences():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a JSON object.")
    prefs = load_preferences()
    if isinstance(data.get("include_cross"), bool):
        prefs["include_cross"] = data["include_cross"]
    if isinstance(data.get("include_replacements"), bool):
        prefs["include_replacements"] = data["include_replacements"]
    if "last_viewed_date" in data:
        try:
            date.fromisoformat(data["last_viewed_date"])
        except (TypeError, ValueError):
            abort(400, "Date must use YYYY-MM-DD format.")
        prefs["last_viewed_date"] = data["last_viewed_date"]
    save_preferences(prefs)
    return jsonify({"ok": True, "preferences": prefs})


@app.route("/feedback", methods=["GET"])
def get_feedback():
    return jsonify(load_feedback())


@app.route("/feedback", methods=["POST"])
def post_feedback():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a JSON object.")
    paper_id = data.get("paper_id", "")
    action = data.get("action", "")
    if not isinstance(paper_id, str) or not paper_id:
        abort(400, "paper_id is required.")
    if action not in {"overrated", "underrated", "cancel"}:
        abort(400, "Invalid feedback action.")

    feedback = load_feedback()
    feedback = [fb for fb in feedback if fb.get("paper_id") != paper_id]

    if action != "cancel":
        feedback.append({
            "paper_id": paper_id,
            "title": data.get("title", ""),
            "action": action,
            "original_score": data.get("original_score", 0),
            "date": (_current_digest.get("date") or date.today().isoformat()) if _current_digest else date.today().isoformat(),
        })

    save_feedback(feedback)
    return jsonify({"ok": True})


def open_browser(port: int = 5123):
    """Open in Chrome on macOS (launches Chrome if not running)."""
    import platform
    url = f"http://127.0.0.1:{port}"
    if platform.system() == "Darwin":
        chrome = "/Applications/Google Chrome.app"
        if os.path.exists(chrome):
            subprocess.run(["open", "-a", "Google Chrome", url], capture_output=True)
        else:
            subprocess.run(["open", url], capture_output=True)
    else:
        webbrowser.open(url)


def main():
    global _current_digest, _pipeline_message, _pipeline_status

    parser = argparse.ArgumentParser(description="AstroPaperDigest Web Viewer")
    parser.add_argument("--port", type=int, default=5123, help="Port (default: 5123)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--no-run", action="store_true", help="Don't auto-start pipeline")
    args = parser.parse_args()

    # Load existing digest if available
    digest_path = get_latest_digest_path()
    if digest_path and os.path.exists(digest_path):
        _current_digest = parse_digest(digest_path)

    # Only auto-run pipeline if today's digest doesn't exist
    today_str = date.today().isoformat()
    today_digest = os.path.join(str(_PROJECT_DIR), "output", "digests", f"digest_{today_str}.md")
    has_today = os.path.exists(today_digest)

    if not args.no_run and not has_today:
        prefs = load_preferences()
        _start_pipeline(
            include_cross=prefs.get("include_cross", True),
            include_replacements=prefs.get("include_replacements", True),
        )
    else:
        _pipeline_status = "done"
        _pipeline_message = "Showing existing digest."

    # Open browser
    if not args.no_browser:
        Timer(1.0, open_browser, args=[args.port]).start()

    print(f"Server: http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
