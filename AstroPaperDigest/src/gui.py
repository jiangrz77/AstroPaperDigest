#!/usr/bin/env python3
"""AstroPaperDigest - native desktop window backed by a local Flask server.

Flow: .app launches this -> pywebview opens the status page in a desktop
window -> pipeline runs in the background -> page auto-updates when done.
"""

import argparse
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread, Timer

from flask import Flask, abort, jsonify, redirect, render_template_string, request
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

import webview

# Ensure working directory is the data dir (repo root in source mode,
# ~/Library/Application Support/AstroPaperDigest in the frozen .app).
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import paths as _paths
_PROJECT_DIR = _paths.data_dir()
os.chdir(_PROJECT_DIR)
sys.path.insert(0, str(_PROJECT_DIR))

from src.digest_parser import parse_digest, get_latest_digest_path, get_digest_path_for_date, get_available_dates
from src import updater
from src.preference_learning import (
    load_learned_profile,
    rebuild_learned_profile,
    reset_learned_profile,
)
from src.progress import parse as parse_progress

FEEDBACK_FILE = os.path.join(_PROJECT_DIR, "feedback.json")
PREFERENCES_FILE = os.path.join(_PROJECT_DIR, "preferences.json")

app = Flask(__name__)

# Global state
_current_digest = None
_pipeline_status = "idle"  # idle | running | done | error
_pipeline_message = ""
_pipeline_process = None
_pipeline_started_at = None
_pipeline_progress = {"stage": "", "done": 0, "total": 0, "message": ""}
_pipeline_log = deque(maxlen=60)
_pipeline_lock = Lock()
_desktop_window = None
_server = None
_run_lock_fh = None

# Single-instance + run-info location.  fcntl.flock releases automatically on
# process exit, so a crashed app never leaves a "live" lock behind.
_APD_APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "AstroPaperDigest"
_APD_LOCK_PATH = _APD_APP_SUPPORT_DIR / "apd.lock"
_APD_RUN_INFO_PATH = _APD_APP_SUPPORT_DIR / "apd-run.json"

# --- Update check state ---
_update_state = {
    "status": "idle",  # idle|checking|available|up_to_date|downloading|ready|installing|error
    "current": updater.get_current_version(),
    "latest": "",
    "tag": "",
    "notes": "",
    "published_at": "",
    "download_url": "",
    "sha256": "",
    "progress": 0,
    "error": "",
    "checked_at": "",
}
_update_lock = Lock()

_UPDATE_BANNER_SCRIPT = """
<div id="apd-update-banner" style="display:none;background:#2563eb;color:#fff;padding:10px 20px;font-size:13px;align-items:center;justify-content:center;gap:14px;flex-wrap:wrap">
  <span id="apd-update-text"></span>
  <a id="apd-update-view" href="/settings#update" style="color:#fff;font-weight:600;text-decoration:underline">View</a>
  <button id="apd-update-now" style="background:#fff;color:#2563eb;border:none;border-radius:6px;padding:5px 14px;font-weight:600;cursor:pointer">Update Now</button>
  <button id="apd-update-later" style="background:transparent;color:#fff;border:1px solid rgba(255,255,255,.5);border-radius:6px;padding:5px 14px;cursor:pointer">Remind Me Later</button>
</div>
<script>
(function () {
  var STORAGE_KEY = "apdUpdateDismissed";
  function showBanner(s) {
    var bar = document.getElementById("apd-update-banner");
    if (!bar) return;
    document.getElementById("apd-update-text").textContent =
      "New version v" + s.latest + " available (current v" + s.current + ")";
    bar.style.display = "flex";
    document.getElementById("apd-update-now").addEventListener("click", function () {
      fetch("/update/download", {method: "POST", cache: "no-store"}).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) {
          window.location.href = "/settings#update";
        } else if (d.error) {
          alert(d.error);
        }
      }).catch(function () {});
    });
    document.getElementById("apd-update-later").addEventListener("click", function () {
      try { sessionStorage.setItem(STORAGE_KEY, s.latest); } catch (_) {}
      bar.style.display = "none";
    });
  }
  fetch("/update/status").then(function (r) { return r.json(); }).then(function (s) {
    if (!s || s.status !== "available") return;
    try {
      if (sessionStorage.getItem(STORAGE_KEY) === s.latest) return;
    } catch (_) {}
    showBanner(s);
  }).catch(function () {});
})();
</script>
"""


def _load_config_and_env():
    """Return (config dict, .env dict) from config.yaml and .env."""
    import yaml
    cfg = {}
    config_path = os.path.join(_PROJECT_DIR, "config.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    env_vars = {}
    env_path = os.path.join(_PROJECT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return cfg, env_vars


def _write_env(env_values: dict) -> None:
    """Write .env, escaping values and locking down permissions."""
    env_path = os.path.join(_PROJECT_DIR, ".env")
    with open(env_path, "w", encoding="utf-8") as f:
        for key, value in env_values.items():
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            f.write(f'{key}="{escaped}"\n')
    os.chmod(env_path, 0o600)


def _write_config(config: dict) -> None:
    """Write config.yaml, preserving key order and unicode."""
    import yaml
    config_path = os.path.join(_PROJECT_DIR, "config.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _apply_llm(config: dict, env_values: dict, provider: str, api_key: str,
               model: str, base_url: str) -> str:
    """Update LLM config + .env values; returns the API key env var name."""
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

    env_values[api_key_env] = api_key
    llm_cfg = config.setdefault("llm", {})
    llm_cfg["base_url"] = base_url
    llm_cfg["api_key_env"] = api_key_env
    llm_cfg["model"] = model
    return api_key_env


def _apply_interests(config: dict, request) -> None:
    """Update research-interest settings (quick keywords or bib profile)."""
    profile_mode = request.form.get("profile_mode", "quick")
    if profile_mode == "quick":
        categories = request.form.getlist("categories")
        keywords_raw = request.form.get("keywords", "").strip()
        if categories:
            config["arxiv_categories"] = categories
        if keywords_raw:
            config["keywords"] = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    else:
        bib_file = request.files.get("bib_file")
        bib_path = request.form.get("bib_path", "").strip()
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


def _apply_email(config: dict, env_values: dict, enable_email: bool,
                 email_sender: str, email_recipient: str, smtp_server: str,
                 smtp_protocol: str, smtp_port_value: str,
                 email_password: str) -> None:
    """Update email config + credentials; never wipe stored values when off."""
    smtp_port = 465 if smtp_protocol == "ssl" else 587
    if enable_email:
        try:
            smtp_port = int(smtp_port_value) if smtp_port_value else smtp_port
        except ValueError:
            abort(400, "SMTP port must be an integer.")
        if not 1 <= smtp_port <= 65535:
            abort(400, "SMTP port must be between 1 and 65535.")

    if enable_email:
        env_values["EMAIL_APP_PASSWORD"] = email_password
        env_values["EMAIL_SENDER"] = email_sender
        env_values["EMAIL_RECIPIENT"] = email_recipient or email_sender
        env_values["SMTP_SERVER"] = smtp_server
        env_values["SMTP_PORT"] = str(smtp_port)

    email_cfg = config.setdefault("email", {})
    if enable_email and email_sender and smtp_server:
        email_cfg["enabled"] = True
        email_cfg["sender"] = email_sender
        email_cfg["recipient"] = email_recipient or email_sender
        email_cfg["smtp_server"] = smtp_server
        email_cfg["use_ssl"] = (smtp_protocol == "ssl")
        email_cfg["smtp_port"] = smtp_port
        email_cfg["password_env"] = "EMAIL_APP_PASSWORD"
    else:
        email_cfg["enabled"] = False


def _setup_context() -> dict:
    """Build the shared template context for /setup and /settings."""
    cfg, env_vars = _load_config_and_env()
    email_cfg = cfg.get("email", {})
    llm_cfg = cfg.get("llm", {})
    api_key_env = llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    provider = {"DEEPSEEK_API_KEY": "deepseek", "OPENAI_API_KEY": "openai"}.get(api_key_env, "custom")
    return {
        "cur_provider": provider,
        "cur_model": llm_cfg.get("model", "deepseek-v4-flash"),
        "cur_base_url": llm_cfg.get("base_url", ""),
        "cur_api_key": env_vars.get(
            "DEEPSEEK_API_KEY",
            env_vars.get("OPENAI_API_KEY", env_vars.get("CUSTOM_API_KEY", "")),
        ),
        "cur_categories": cfg.get("arxiv_categories", []),
        "cur_keywords": ", ".join(cfg.get("keywords", [])),
        "cur_bib_file": cfg.get("bib_file", ""),
        "cur_email_sender": email_cfg.get("sender", ""),
        "cur_email_recipient": email_cfg.get("recipient", ""),
        "cur_smtp_server": email_cfg.get("smtp_server", ""),
        "cur_smtp_port": str(email_cfg.get("smtp_port", "465")),
        "cur_use_ssl": email_cfg.get("use_ssl", True),
        "cur_email_enabled": email_cfg.get("enabled", False),
        "cur_email_password": env_vars.get("EMAIL_APP_PASSWORD", ""),
    }


def _update_config() -> dict:
    """Return the 'update' section of config.yaml (defaults GitHub repo)."""
    try:
        import yaml
        with open(os.path.join(_PROJECT_DIR, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("update", {}) or {}
    except Exception:
        return {}


def _start_update_check():
    """Check GitHub Releases for a newer version and update _update_state."""
    repo = _update_config().get("github_repo", "jiangrz77/AstroPaperDigest")
    with _update_lock:
        _update_state["status"] = "checking"
        _update_state["error"] = ""
    try:
        result = updater.check_update(repo)
        with _update_lock:
            _update_state.update({
                "status": "available" if result["available"] else "up_to_date",
                "latest": result["latest"],
                "tag": result["tag"],
                "notes": result["notes"],
                "published_at": result["published_at"],
                "download_url": result["download_url"],
                "sha256": result["sha256"],
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "error": "",
            })
    except updater.UpdateCheckError as e:
        with _update_lock:
            _update_state.update({"status": "error", "error": str(e)})
    except Exception as e:
        with _update_lock:
            _update_state.update({"status": "error", "error": f"Update check failed: {e}"})


def _download_update(url: str, version: str, expected_sha: str):
    """Download the release zip in the background with progress."""
    dest = updater.UPDATES_DIR / f"AstroPaperDigest-v{version}.zip"

    def progress(done, total):
        pct = int(done * 100 / total) if total else 0
        with _update_lock:
            _update_state["progress"] = pct

    try:
        path = updater.download_file(url, dest, progress)
        if not updater.verify_sha256(path, expected_sha):
            with _update_lock:
                _update_state.update({
                    "status": "error",
                    "error": "Package verification failed (SHA-256 mismatch). Installation stopped.",
                })
            return
        with _update_lock:
            _update_state.update({"status": "ready", "progress": 100})
    except Exception as e:
        with _update_lock:
            _update_state.update({"status": "error", "error": f"Download failed: {e}"})


def _stop_pipeline_process():
    """Terminate the background CLI pipeline if it is still running."""
    global _pipeline_process
    process = _pipeline_process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    _pipeline_process = None


def _cleanup_run_files():
    """Release the single-instance lock and remove the run-info file."""
    global _run_lock_fh
    try:
        if _APD_RUN_INFO_PATH.exists():
            _APD_RUN_INFO_PATH.unlink()
    except OSError:
        pass
    if _run_lock_fh is not None:
        try:
            fcntl.flock(_run_lock_fh.fileno(), fcntl.LOCK_UN)
            _run_lock_fh.close()
        except OSError:
            pass
        _run_lock_fh = None


def _shutdown_server():
    """Stop HTTP server + pipeline and release the port.  Idempotent."""
    global _server
    _stop_pipeline_process()
    if _server is not None:
        try:
            _server.shutdown()
        except Exception:
            pass
        try:
            _server.server_close()
        except Exception:
            pass
        _server = None
    _cleanup_run_files()


def _terminate_server():
    """Shut the app down from a request thread (update/restart path)."""
    window = _desktop_window
    if window is not None:
        try:
            window.destroy()
        except Exception:
            pass
    _shutdown_server()
    # Safety net in case the pywebview GUI loop did not unwind promptly.
    Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


def _ensure_run_dir():
    try:
        _APD_APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _read_run_info():
    try:
        with open(_APD_RUN_INFO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_run_info(port: int):
    _ensure_run_dir()
    payload = {
        "pid": os.getpid(),
        "port": port,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp_path = _APD_RUN_INFO_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, _APD_RUN_INFO_PATH)


def _acquire_single_instance_lock() -> bool:
    """Try to become the single running instance.  Returns False if one exists."""
    global _run_lock_fh
    _ensure_run_dir()
    fh = open(_APD_LOCK_PATH, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _run_lock_fh = fh
    return True


def _focus_existing_instance() -> bool:
    """Ask the already-running instance to bring its window to the front.

    The lock guarantees another instance is alive, but the first launch may
    still be writing its run-info file.  Retry briefly before giving up.
    """
    import urllib.request
    for _ in range(15):
        info = _read_run_info()
        port = info.get("port") if info else None
        if port:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/focus", timeout=2).read()
                return True
            except Exception:
                pass
        time.sleep(0.2)
    return False


def _handle_termination_signal(signum, frame):
    """Release the port and run files when macOS/launcher sends SIGTERM/SIGINT."""
    try:
        _shutdown_server()
    except Exception:
        pass
    sys.exit(0)


@app.route("/focus", methods=["GET", "POST"])
def focus_desktop_window():
    """Bring the native pywebview window back to the front."""
    window = _desktop_window
    if window is not None:
        try:
            window.restore()
        except Exception:
            pass
        try:
            window.show()
        except Exception:
            pass
    return "", 204


def _needs_setup():
    """Check if first-time setup is needed (no .env file)."""
    return not os.path.exists(os.path.join(_PROJECT_DIR, ".env"))


@app.before_request
def check_setup():
    """Redirect to setup page if first-time user."""
    if (
        _needs_setup()
        and request.path != "/setup"
        and not request.path.startswith(("/static", "/focus"))
    ):
        return redirect("/setup")


@app.after_request
def inject_update_banner(response):
    """Attach the update banner to every rendered page."""
    if response.mimetype == "text/html":
        content = response.get_data(as_text=True)
        if "</body>" in content:
            inject = f"{_UPDATE_BANNER_SCRIPT}\n</body>"
            response.set_data(content.replace("</body>", inject, 1))
    return response


def load_preferences() -> dict:
    """Load user preferences from file."""
    defaults = {
        "include_cross": True,
        "include_replacements": True,
        "last_viewed_date": "",
        "auto_check_updates": True,
        "dismissed_update_version": "",
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


_STAGE_LABELS = {
    "profile": "Profile",
    "fetch": "Fetching arXiv papers",
    "filter": "Filtering papers",
    "rank": "AI ranking",
    "output": "Generating output",
    "done": "Done",
    "error": "Error",
}


def _pipeline_progress_message(line: str) -> str:
    """Convert CLI output into a concise browser status message."""
    line = line.strip()
    if not line:
        return ""
    if line.startswith("["):
        return line
    if line.startswith(("Fetched ", "Category filter:", "Keyword filter:")):
        return line
    if line.startswith("Sending email"):
        return "Sending email notification..."
    if line.startswith("Respecting arXiv"):
        return "Waiting for arXiv rate-limit interval…"
    if line.startswith("Proxy connection failed"):
        return "Proxy connection failed; retrying without proxy…"
    if line.startswith("Waiting "):
        return f"{line.lower()}…"
    if line.startswith(("Submission window", "arXiv API", "Error fetching")):
        return line
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
            event = parse_progress(output_line)
            if event:
                _pipeline_progress.update({
                    "stage": event.get("stage") or _pipeline_progress.get("stage", ""),
                    "done": event.get("done", 0) or 0,
                    "total": event.get("total", 0) or 0,
                    "message": event.get("message") or "",
                })
                if event.get("message"):
                    _pipeline_message = event["message"]
            else:
                _pipeline_log.append(output_line.rstrip("\n"))
                progress = _pipeline_progress_message(output_line)
                if progress:
                    _pipeline_message = progress

        return _pipeline_process.wait(), "".join(output_lines)
    finally:
        _pipeline_process = None


def run_pipeline(include_cross: bool = True, include_replacements: bool = True, target_date: str = ""):
    """Run the recommendation pipeline in a background thread."""
    global _current_digest, _pipeline_status, _pipeline_message
    global _pipeline_started_at, _pipeline_progress
    _pipeline_status = "running"
    _pipeline_started_at = time.time()
    _pipeline_log.clear()
    _pipeline_progress = {
        "stage": "profile",
        "done": 0,
        "total": 0,
        "message": "[1/5] Starting pipeline…",
    }
    _pipeline_message = "[1/5] Starting pipeline..."

    try:
        # Build command with preferences
        if getattr(sys, "frozen", False):
            cmd = [str(Path(sys._MEIPASS) / "apd-cli")]
        else:
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
            _pipeline_progress.update({"stage": "done", "done": 1, "total": 1})
            # Set contextual message based on result
            if "NOT_YET_AVAILABLE" in stdout:
                _pipeline_message = "Today's batch is not published yet (expected after ~10:00); no digest generated."
            elif "NO_ANNOUNCEMENT" in stdout:
                _pipeline_message = "No arXiv announcement on this day (weekend/holiday deferral); an empty digest was generated."
            elif "DEFERRED_OR_LAGGING" in stdout:
                _pipeline_message = "This day's batch may be deferred (holiday) or the listing lags; an empty digest was generated."
            elif (
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
            _pipeline_progress.update({"stage": "error"})
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
        _pipeline_progress.update({"stage": "error"})
        _pipeline_message = "Pipeline timed out (>15 minutes)."
    except Exception as e:
        _pipeline_status = "error"
        _pipeline_progress.update({"stage": "error"})
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

_CALENDAR_SNIPPET = r"""<style>
.btn-settings{display:inline-flex;align-items:center;justify-content:center;height:34px;width:34px;padding:0;box-sizing:border-box;background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;cursor:pointer;font-size:16px;line-height:1;transition:background .15s}
.btn-settings:hover{background:rgba(255,255,255,.3)}
.apd-cal{position:fixed;z-index:1000;background:#fff;color:#333;border-radius:12px;box-shadow:0 10px 34px rgba(0,0,0,.28),0 2px 8px rgba(0,0,0,.14);padding:14px;width:288px;font-size:13px;user-select:none}
.apd-cal[hidden]{display:none}
.cal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.cal-title{font-weight:700;font-size:14px;color:#1a2332}
.cal-nav{background:#f0f2f5;border:none;border-radius:6px;width:28px;height:28px;font-size:16px;cursor:pointer;color:#555;line-height:1}
.cal-nav:hover{background:#dfe6e9}
.cal-nav:disabled{opacity:.3;cursor:default}
.cal-week,.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;text-align:center}
.cal-week{color:#999;font-size:11px;margin-bottom:4px;font-weight:600}
.cal-week span{padding:4px 0}
.cal-day{border:none;background:transparent;border-radius:8px;padding:7px 0;font-size:13px;cursor:pointer;color:#333;position:relative}
.cal-day:hover{background:#eef2ff}
.cal-day:disabled{color:#ccc;cursor:default}
.cal-day:disabled:hover{background:transparent}
.cal-day.today{outline:1px solid #2563eb;outline-offset:-1px}
.cal-day.selected{background:#2563eb;color:#fff;font-weight:700}
.cal-day.selected:hover{background:#1d4ed8}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex-shrink:0}
.cal-day .dot{position:absolute;left:50%;bottom:3px;transform:translateX(-50%);width:5px;height:5px}
.cal-day.selected .dot{box-shadow:0 0 0 1.5px #fff}
.dot-green{background:#27ae60}
.dot-orange{background:#f39c12}
.dot-gray{background:#cbd5e1}
.cal-legend{display:flex;gap:12px;margin-top:10px;padding-top:10px;border-top:1px solid #eef0f3;font-size:11px;color:#777;flex-wrap:wrap}
.cal-legend span{display:inline-flex;align-items:center;gap:5px}
.cal-hint{font-size:11px;color:#aaa;margin-top:8px;text-align:center}
</style>
<script>
(function () {
  var STATUS = window.APD_DIGEST_STATUS || {};
  var todayStr = '';
  var selectedStr = '';
  var viewY = 0, viewM = 0; // viewM is 0-based
  var pop = null;
  var label = document.getElementById('date-label');
  var picker = document.getElementById('date-picker');
  if (!label || !picker) return;

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function iso(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }

  todayStr = picker.max || (function () {
    var n = new Date();
    return iso(n.getFullYear(), n.getMonth(), n.getDate());
  })();
  selectedStr = /^\d{4}-\d{2}-\d{2}$/.test(picker.value) ? picker.value : todayStr;

  var parts = selectedStr.split('-');
  viewY = parseInt(parts[0], 10);
  viewM = parseInt(parts[1], 10) - 1;


  function build() {
    pop = document.createElement('div');
    pop.className = 'apd-cal';
    pop.id = 'apd-cal';
    pop.hidden = true;
    pop.innerHTML =
      '<div class="cal-head">' +
        '<button type="button" class="cal-nav" id="cal-prev" title="Previous month">‹</button>' +
        '<span class="cal-title" id="cal-title"></span>' +
        '<button type="button" class="cal-nav" id="cal-next" title="Next month">›</button>' +
      '</div>' +
      '<div class="cal-week"><span>Mo</span><span>Tu</span><span>We</span><span>Th</span><span>Fr</span><span>Sa</span><span>Su</span></div>' +
      '<div class="cal-grid" id="cal-grid"></div>' +
      '<div class="cal-legend">' +
        '<span><i class="dot dot-green"></i>Has content</span>' +
        '<span><i class="dot dot-orange"></i>Some unscored</span>' +
        '<span><i class="dot dot-gray"></i>Empty digest</span>' +
      '</div>' +
      '<div class="cal-hint">Click a date to open it · click outside or press Esc to close</div>';
    document.body.appendChild(pop);
    pop.addEventListener('click', function (e) { e.stopPropagation(); });
    document.getElementById('cal-prev').addEventListener('click', function () {
      viewM--;
      if (viewM < 0) { viewM = 11; viewY--; }
      render();
    });
    document.getElementById('cal-next').addEventListener('click', function () {
      viewM++;
      if (viewM > 11) { viewM = 0; viewY++; }
      render();
    });
    document.getElementById('cal-grid').addEventListener('click', function (e) {
      var target = e.target;
      var btn = target && target.closest ? target.closest('.cal-day') : null;
      if (!btn || btn.disabled) return;
      var d = btn.getAttribute('data-date');
      if (d && window.navigateToDate) window.navigateToDate(d);
      close();
    });
  }

  function render() {
    document.getElementById('cal-title').textContent =
      new Date(viewY, viewM, 1).toLocaleDateString('en-US', {month: 'long', year: 'numeric'});
    var tp = todayStr.split('-');
    var ty = parseInt(tp[0], 10), tm = parseInt(tp[1], 10) - 1;
    document.getElementById('cal-next').disabled = (viewY > ty) || (viewY === ty && viewM >= tm);

    var first = new Date(viewY, viewM, 1);
    var offset = (first.getDay() + 6) % 7; // Monday-first week
    var daysInMonth = new Date(viewY, viewM + 1, 0).getDate();
    var html = '';
    for (var i = 0; i < offset; i++) html += '<span></span>';
    for (var d = 1; d <= daysInMonth; d++) {
      var dateStr = iso(viewY, viewM, d);
      var cls = 'cal-day';
      if (dateStr === todayStr) cls += ' today';
      if (dateStr === selectedStr) cls += ' selected';
      var disabled = dateStr > todayStr ? ' disabled' : '';
      var dot = STATUS[dateStr] ? '<i class="dot dot-' + STATUS[dateStr] + '"></i>' : '';
      html += '<button type="button" class="' + cls + '" data-date="' + dateStr + '"' + disabled + '>' + d + dot + '</button>';
    }
    document.getElementById('cal-grid').innerHTML = html;
  }

  function position() {
    var r = label.getBoundingClientRect();
    var left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8));
    var top = r.bottom + 8;
    if (top + pop.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, r.top - pop.offsetHeight - 8);
    }
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  }

  function open() {
    pop.hidden = false; // unhide first so offsetWidth/Height are measurable
    render();
    position();
  }
  function close() { pop.hidden = true; }

  // Override the native date-picker trigger used by the page templates.
  window.openDatePicker = function () {
    if (pop.hidden) { open(); } else { close(); }
  };

  document.addEventListener('mousedown', function (e) {
    if (pop.hidden) return;
    if (!pop.contains(e.target) && e.target !== label && !label.contains(e.target)) close();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !pop.hidden) { e.preventDefault(); close(); }
  });
  window.addEventListener('resize', close);
  window.addEventListener('scroll', function () { if (!pop.hidden) position(); }, true);

  build();
})();
</script>
<script>
// macOS convention: Cmd+, (Ctrl+, on other platforms) opens Settings.
document.addEventListener('keydown', function (e) {
  if ((e.metaKey || e.ctrlKey) && e.key === ',') {
    e.preventDefault();
    if (window.location.pathname !== '/settings') window.location.href = '/settings';
  }
});
</script>
"""


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
    <input type="text" id="model" name="model" value="{{ cur_model or 'deepseek-v4-flash' }}">
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
    <label style="display:flex;align-items:center;gap:8px;font-weight:600;color:#999;cursor:not-allowed">
      <input type="checkbox" id="enable_email" name="enable_email" onchange="toggleEmail()" {% if cur_email_enabled %}checked{% endif %} disabled>
      Enable email notification
    </label>
    <p class="hint">Daily email reminders are still under development. Stay tuned.</p>
    <div id="email-fields" {% if not cur_email_enabled %}style="display:none"{% endif %}>
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
      <input type="text" id="smtp_port" name="smtp_port" placeholder="465" value="{{ cur_smtp_port or '465' }}">
      <label for="email_password">Email Password / App Password</label>
      <input type="password" id="email_password" name="email_password" placeholder="App password" value="{{ cur_email_password or '' }}">
    </div>
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
  if (p === 'deepseek') { model.value = 'deepseek-v4-flash'; urlGroup.style.display = 'none'; }
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
function toggleEmail() {
  const on = document.getElementById('enable_email').checked;
  document.getElementById('email-fields').style.display = on ? '' : 'none';
}
</script>
</body>
</html>"""

SETTINGS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstroPaperDigest - Settings</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333}
.header{background:#1a2332;color:#fff;padding:16px 32px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.header h1{font-size:20px;margin:0}
.back-link{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:10px 12px;border-radius:8px;background:#f8fafc;border:1px solid #d1d5db;color:#1a2332;text-decoration:none;font-size:14px;font-weight:600;transition:background .15s,border-color .15s}
.back-link:hover{background:#eef2f7;border-color:#9ca3af;color:#1a2332}
.layout{display:flex;align-items:flex-start;max-width:1100px;margin:0 auto;padding:24px 16px;gap:24px}
.sidebar{width:220px;flex-shrink:0;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);padding:10px;position:sticky;top:24px}
.sidebar-back{margin-top:12px;padding-top:14px;border-top:1px solid #e5e7eb}
.nav-item{display:flex;align-items:center;gap:10px;width:100%;padding:10px 14px;border:none;border-radius:8px;background:transparent;font-size:14px;color:#444;cursor:pointer;text-align:left}
.nav-item:hover{background:#f0f2f5}
.nav-item.active{background:#2563eb;color:#fff;font-weight:600}
.nav-icon{width:20px;height:20px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0}
.nav-icon svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.content{flex:1;min-width:0}
.panel{display:none}
.panel.active{display:block}
.card{background:#fff;border-radius:10px;padding:24px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card h2{font-size:16px;margin-bottom:4px;color:#2c3e50}
.card .sub{font-size:13px;color:#888;margin-bottom:16px}
label{display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:6px;margin-top:14px}
input[type="text"],input[type="password"],select,textarea{width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;margin-bottom:4px}
input:focus,select:focus,textarea:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 2px rgba(37,99,235,.15)}
textarea{height:90px;resize:vertical}
.checkbox-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}
.checkbox-grid label{display:flex;align-items:center;gap:6px;font-weight:400;font-size:13px;margin:0}
.checkbox-grid input{width:auto}
.toggle-group{display:flex;gap:12px;margin-bottom:12px}
.toggle-group label{display:flex;align-items:center;gap:6px;font-weight:500;margin:0;cursor:pointer}
.toggle-group input{width:auto}
.hint{font-size:12px;color:#999;margin-top:4px}
.btn{padding:9px 18px;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
.btn-primary{background:#2563eb;color:#fff}
.btn-primary:hover{background:#1d4ed8}
.btn-success{background:#16a34a;color:#fff}
.btn-success:hover{background:#15803d}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.check-line{display:flex;align-items:center;gap:8px;margin:10px 0;cursor:pointer}
.check-line input{width:auto}
.version-badge{display:inline-block;background:#eef2ff;color:#2563eb;border:1px solid #c7d2fe;border-radius:12px;padding:2px 10px;font-size:12px;font-weight:600}
.update-status{font-size:13px;color:#666;margin-top:10px;min-height:18px}
.update-notes{display:none;margin-top:10px;padding:10px 12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;font-size:13px;color:#444;white-space:pre-wrap;max-height:160px;overflow-y:auto}
.update-progress{display:none;margin-top:10px;height:8px;background:#e2e8f0;border-radius:4px;overflow:hidden}
.update-progress-bar{height:100%;width:0;background:#2563eb;transition:width .3s}
.notice{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:8px;padding:12px 14px;font-size:13px}
.lp-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
.lp-table th,.lp-table td{padding:8px 6px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:middle}
.lp-table input{width:82px;padding:6px 8px;margin:0;font-size:13px}
.lp-badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px;font-weight:600}
.lp-auto{background:#eef2ff;color:#2563eb}
.lp-manual{background:#fef3c7;color:#b45309}
.lp-ignored{background:#f1f5f9;color:#64748b}
.lp-source{color:#999;font-size:11px;display:block;margin-top:2px}
.lp-empty{color:#999;font-size:13px;margin:8px 0}
.lp-cal{font-size:13px;margin:4px 0 10px}
.lp-btn{padding:5px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;cursor:pointer;background:#fff;margin-right:4px}
.lp-btn:hover{border-color:#999}
.lp-btn.danger{color:#dc2626;border-color:#fecaca}
.lp-btn.danger:hover{background:#fef2f2}
</style>
</head>
<body>
<div class="header">
  <h1>AstroPaperDigest - Settings</h1>
</div>
<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-nav">
      <button class="nav-item active" data-section="general" onclick="activate('general')"><span class="nav-icon"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></span>General</button>
      <button class="nav-item" data-section="llm" onclick="activate('llm')"><span class="nav-icon"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/></svg></span>LLM &amp; API</button>
      <button class="nav-item" data-section="interests" onclick="activate('interests')"><span class="nav-icon"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg></span>Research Interests</button>
      <button class="nav-item" data-section="learned" onclick="activate('learned')"><span class="nav-icon"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg></span>Learned Preferences</button>
      <button class="nav-item" data-section="email" onclick="activate('email')"><span class="nav-icon"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg></span>Email Notification</button>
    </div>
    <div class="sidebar-back">
      <a class="back-link" href="/">← Back to Digest</a>
    </div>
  </nav>
  <main class="content">

    <section class="panel active" id="panel-general">
      <div class="card">
        <h2>General</h2>
        <p class="sub">Digest preferences and application updates.</p>
        <label class="check-line" style="margin-top:0">
          <input type="checkbox" id="pref-cross" {% if prefs.include_cross %}checked{% endif %}>
          Include cross-listed papers
        </label>
        <label class="check-line">
          <input type="checkbox" id="pref-repl" {% if prefs.include_replacements %}checked{% endif %}>
          Include replacement (updated) papers
        </label>
        <div class="row">
          <button class="btn btn-primary" type="button" id="btn-save-general" onclick="saveGeneral()">Save Changes</button>
        </div>
      </div>

      <div class="card" id="update">
        <h2>Update</h2>
        <p class="sub">Current version: <span class="version-badge">v{{ current_version }}</span></p>
        <p class="update-status" id="update-status">—</p>
        <div class="update-notes" id="update-notes"></div>
        <div class="update-progress" id="update-progress"><div class="update-progress-bar" id="update-progress-bar"></div></div>
        <div class="row">
          <button class="btn btn-primary" type="button" id="btn-check-update">Check for Updates</button>
          <button class="btn btn-primary" type="button" id="btn-download-update" style="display:none">Download Update</button>
          <button class="btn btn-success" type="button" id="btn-apply-update" style="display:none">Install &amp; Restart</button>
        </div>
        <label class="check-line" style="margin-top:16px">
          <input type="checkbox" id="auto-check-updates" {% if prefs.auto_check_updates %}checked{% endif %}>
          Check for updates on startup
        </label>
      </div>

      <div class="card">
        <h2>About</h2>
        <p class="sub">AstroPaperDigest v{{ current_version }} — LLM-scored daily arXiv digests for your research interests.</p>
        <a href="https://github.com/jiangrz77/AstroPaperDigest" target="_blank" style="font-size:13px;color:#2563eb">GitHub Repository ↗</a>
      </div>
    </section>

    <section class="panel" id="panel-llm">
      <div class="card">
        <h2>LLM &amp; API</h2>
        <p class="sub">Configure the LLM provider used to score paper relevance.</p>
        <form method="POST" action="/settings/save?section=llm">
          <label for="provider">Provider</label>
          <select id="provider" name="provider" onchange="updateProvider()">
            <option value="deepseek" {% if cur_provider == 'deepseek' %}selected{% endif %}>DeepSeek</option>
            <option value="openai" {% if cur_provider == 'openai' %}selected{% endif %}>OpenAI</option>
            <option value="custom" {% if cur_provider == 'custom' %}selected{% endif %}>Custom (OpenAI-compatible)</option>
          </select>
          <label for="api_key">API Key</label>
          <input type="password" id="api_key" name="api_key" placeholder="sk-..." value="{{ cur_api_key or '' }}">
          <label for="model">Model</label>
          <input type="text" id="model" name="model" value="{{ cur_model or 'deepseek-v4-flash' }}">
          <div id="baseurl-group" style="display:none">
            <label for="base_url">Base URL</label>
            <input type="text" id="base_url" name="base_url" placeholder="https://api.example.com/v1" value="{{ cur_base_url or '' }}">
          </div>
          <div class="row">
            <button class="btn btn-primary" type="submit">Save Changes</button>
          </div>
        </form>
      </div>
    </section>

    <section class="panel" id="panel-interests">
      <div class="card">
        <h2>Research Interests</h2>
        <p class="sub">Control which arXiv papers are fetched and how your profile is built.</p>
        <form method="POST" action="/settings/save?section=interests" enctype="multipart/form-data">
          <div class="toggle-group">
            <label><input type="radio" name="profile_mode" value="quick" {% if not cur_bib_file %}checked{% endif %} onchange="toggleProfileMode()"> Quick Start</label>
            <label><input type="radio" name="profile_mode" value="bib" {% if cur_bib_file %}checked{% endif %} onchange="toggleProfileMode()"> Use Bib File</label>
          </div>
          <div id="quick-mode" {% if cur_bib_file %}style="display:none"{% endif %}>
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
          <div id="bib-mode" {% if not cur_bib_file %}style="display:none"{% endif %}>
            <label>Upload your .bib file</label>
            <input type="file" name="bib_file" accept=".bib" style="width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px">
            <label for="bib_path" style="margin-top:10px">Or enter a file path</label>
            <input type="text" id="bib_path" name="bib_path" placeholder="/path/to/your/collection.bib" value="{{ cur_bib_file or '' }}">
            <p class="hint">Your research profile is extracted automatically from the bibliography.</p>
          </div>
          <div class="row">
            <button class="btn btn-primary" type="submit">Save Changes</button>
          </div>
        </form>
      </div>
    </section>

    <section class="panel" id="panel-email">
      <div class="card">
        <h2>Email Notification</h2>
        <p class="sub">Daily email reminders for new papers.</p>
        <div class="notice">Email notification is under development and currently disabled. Stay tuned.</div>
        <label class="check-line" style="margin-top:16px;color:#999;cursor:not-allowed">
          <input type="checkbox" disabled> Enable email notification
        </label>
      </div>
    </section>

    <section class="panel" id="panel-learned">
      <div class="card">
        <h2>Learned Preferences</h2>
        <p class="sub">Learned from your Overrated / Underrated feedback; you can also adjust manually. Weight &gt; 1 = more relevant, &lt; 1 = less relevant, 1 = no effect.</p>
        <p class="hint">Auto = learned from your feedback; Manual = your own value (takes priority over auto). Ignore = stop this item from affecting scores; Restore auto = drop the manual value and return to the learned result.</p>
        <div id="learned-content">Loading…</div>
        <div class="row">
          <button class="btn btn-primary" type="button" id="btn-reset-learned" onclick="resetLearned()">Reset All</button>
        </div>
      </div>
    </section>

  </main>
</div>
<script>
function updateProvider() {
  const p = document.getElementById('provider').value;
  const model = document.getElementById('model');
  const urlGroup = document.getElementById('baseurl-group');
  const baseUrl = document.getElementById('base_url');
  if (p === 'deepseek') { model.value = 'deepseek-v4-flash'; urlGroup.style.display = 'none'; }
  else if (p === 'openai') { model.value = 'gpt-4o-mini'; urlGroup.style.display = 'none'; }
  else { model.value = ''; urlGroup.style.display = 'block'; baseUrl.focus(); }
}
function toggleProfileMode() {
  const mode = document.querySelector('input[name="profile_mode"]:checked').value;
  document.getElementById('quick-mode').style.display = mode === 'quick' ? '' : 'none';
  document.getElementById('bib-mode').style.display = mode === 'bib' ? '' : 'none';
}
(function () {
  const provider = document.getElementById('provider');
  if (provider && provider.value === 'custom') {
    document.getElementById('baseurl-group').style.display = 'block';
  }
})();
</script>
<script>
(function () {
  const SECTIONS = ["general", "llm", "interests", "learned", "email"];
  function activate(name) {
    const requested = name;
    if (name === "update") name = "general";
    if (SECTIONS.indexOf(name) < 0) name = "general";
    const panels = document.querySelectorAll(".panel");
    for (let i = 0; i < panels.length; i++) panels[i].classList.remove("active");
    const panel = document.getElementById("panel-" + name);
    if (panel) panel.classList.add("active");
    const items = document.querySelectorAll(".nav-item");
    for (let j = 0; j < items.length; j++) {
      items[j].classList.toggle("active", items[j].getAttribute("data-section") === name);
    }
    if (history.replaceState) history.replaceState(null, "", "#" + name);
    if (name === "general" && requested === "update") {
      const upd = document.getElementById("update");
      if (upd) setTimeout(function () { upd.scrollIntoView({behavior: "smooth", block: "start"}); }, 60);
    }
  }
  window.activate = activate;
  window.addEventListener("hashchange", function () { activate(location.hash.slice(1)); });
  activate(location.hash.slice(1) || "general");
})();
</script>
<script>
(function () {
  function saveGeneral() {
    const cross = document.getElementById("pref-cross").checked;
    const repl = document.getElementById("pref-repl").checked;
    const auto = document.getElementById("auto-check-updates").checked;
    fetch("/preferences", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({include_cross: cross, include_replacements: repl, auto_check_updates: auto})
    }).then(function (r) { return r.json(); }).then(function (d) {
      const btn = document.getElementById("btn-save-general");
      if (btn) {
        const old = btn.textContent;
        btn.textContent = d.ok ? "Saved" : "Failed";
        setTimeout(function () { btn.textContent = old; }, 1500);
      }
    }).catch(function () {});
  }
  window.saveGeneral = saveGeneral;
})();
</script>
<script>
(function () {
  const content = document.getElementById('learned-content');
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function rowHtml(kind, term, meta) {
    const w = (meta && meta.weight != null) ? meta.weight : 1.0;
    const manual = meta && meta.origin === 'manual';
    const source = meta && meta.source ? meta.source : '';
    const badge = manual
      ? '<span class="lp-badge lp-manual">manual</span>'
      : '<span class="lp-badge lp-auto">auto</span>';
    const srcHtml = source ? '<span class="lp-source">Source: ' + esc(source) + '</span>' : '';
    const secondary = manual
      ? '<button class="lp-btn" data-op="revert" data-kind="' + esc(kind) + '" data-term="' + esc(term) + '">Restore auto</button>'
      : '<button class="lp-btn danger" data-op="ignore" data-kind="' + esc(kind) + '" data-term="' + esc(term) + '">Ignore</button>';
    return '<tr>' +
      '<td>' + esc(term) + badge + srcHtml + '</td>' +
      '<td><input type="number" step="0.05" min="0.5" max="2" value="' + w + '" data-kind="' + esc(kind) + '" data-term="' + esc(term) + '"></td>' +
      '<td><button class="lp-btn" data-op="set" data-kind="' + esc(kind) + '" data-term="' + esc(term) + '">Save</button>' + secondary + '</td>' +
      '</tr>';
  }
  function ignoredRowHtml(kind, term) {
    return '<tr><td>' + esc(term) + ' <span class="lp-badge lp-ignored">ignored</span><span class="lp-source">no longer affects scoring</span></td>' +
      '<td><button class="lp-btn" data-op="revert" data-kind="' + esc(kind) + '" data-term="' + esc(term) + '">Restore</button></td></tr>';
  }
  function render(profile) {
    if (!profile) { content.textContent = 'No learned preferences yet.'; return; }
    const kw = profile.keyword_weights || {};
    const cw = profile.category_weights || {};
    const cal = profile.global_calibration || 0;
    const manual = profile.manual || {};
    const mkw = manual.keyword_weights || {};
    const mcat = manual.category_weights || {};

    let html = '<div class="lp-cal">Global calibration: <strong>' + (cal > 0 ? '+' : '') + cal.toFixed(2) + '</strong> points (positive = shift scores up, negative = down)</div>';

    const kwKeys = Object.keys(kw);
    if (kwKeys.length) {
      html += '<h3 style="font-size:14px;margin:12px 0 4px">Topic keyword weights</h3>';
      html += '<table class="lp-table"><thead><tr><th>Topic</th><th>Weight</th><th>Actions</th></tr></thead><tbody>';
      kwKeys.sort().forEach(function (t) { html += rowHtml('keyword_weights', t, kw[t]); });
      html += '</tbody></table>';
    }
    const cwKeys = Object.keys(cw);
    if (cwKeys.length) {
      html += '<h3 style="font-size:14px;margin:12px 0 4px">arXiv category weights</h3>';
      html += '<table class="lp-table"><thead><tr><th>Category</th><th>Weight</th><th>Actions</th></tr></thead><tbody>';
      cwKeys.sort().forEach(function (c) { html += rowHtml('category_weights', c, cw[c]); });
      html += '</tbody></table>';
    }
    if (!kwKeys.length && !cwKeys.length) {
      html += '<p class="lp-empty">No learned weights yet. Mark papers Overrated / Underrated in a digest to build them.</p>';
    }

    const ignoredKw = Object.keys(mkw).filter(function (t) { return mkw[t] === null; });
    const ignoredCat = Object.keys(mcat).filter(function (c) { return mcat[c] === null; });
    if (ignoredKw.length || ignoredCat.length) {
      html += '<h3 style="font-size:14px;margin:16px 0 4px">Ignored (no longer affects scoring)</h3>';
      html += '<table class="lp-table"><thead><tr><th>Item</th><th>Actions</th></tr></thead><tbody>';
      ignoredKw.sort().forEach(function (t) { html += ignoredRowHtml('keyword_weights', t); });
      ignoredCat.sort().forEach(function (c) { html += ignoredRowHtml('category_weights', c); });
      html += '</tbody></table>';
    }
    content.innerHTML = html;
  }
  function load() {
    fetch('/learned-profile').then(function (r) { return r.json(); }).then(render).catch(function () {
      content.textContent = 'Failed to load.';
    });
  }
  function post(kind, term, op, weight) {
    fetch('/learned-profile', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: kind, term: term, op: op, weight: weight})
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) render(d.profile);
    }).catch(function () {});
  }
  content.addEventListener('click', function (ev) {
    const btn = ev.target.closest('button');
    if (!btn) return;
    const kind = btn.getAttribute('data-kind');
    const term = btn.getAttribute('data-term');
    const op = btn.getAttribute('data-op');
    if (!kind || !term || !op) return;
    if (op === 'set') {
      const input = content.querySelector('input[data-kind="' + CSS.escape(kind) + '"][data-term="' + CSS.escape(term) + '"]');
      if (!input) return;
      const v = parseFloat(input.value);
      if (!isFinite(v)) return;
      post(kind, term, 'set', v);
    } else {
      post(kind, term, op);
    }
  });
  window.resetLearned = function () {
    if (!confirm('Reset all learned preferences? This clears your feedback history, manual settings and ignored items.')) return;
    fetch('/learned-profile/reset', {method: 'POST'}).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) render(d.profile);
    });
  };
  load();
})();
</script>
<script>
(function () {
  const statusEl = document.getElementById("update-status");
  const notesEl = document.getElementById("update-notes");
  const progressWrap = document.getElementById("update-progress");
  const progressBar = document.getElementById("update-progress-bar");
  const btnCheck = document.getElementById("btn-check-update");
  const btnDownload = document.getElementById("btn-download-update");
  const btnApply = document.getElementById("btn-apply-update");

  function render(s) {
    if (!s) return;
    if (s.status === "checking") {
      statusEl.textContent = "Checking for updates…";
      btnCheck.disabled = true;
    } else if (s.status === "available") {
      statusEl.textContent = "New version v" + s.latest + " available (current v" + s.current + ")";
      notesEl.style.display = "block";
      notesEl.textContent = s.notes || "(No release notes)";
      btnDownload.style.display = "inline-block";
      btnApply.style.display = "none";
    } else if (s.status === "up_to_date") {
      statusEl.textContent = "You are up to date (v" + s.current + ")";
      notesEl.style.display = "none";
    } else if (s.status === "downloading") {
      statusEl.textContent = "Downloading v" + s.latest + "… " + s.progress + "%";
      progressWrap.style.display = "block";
      progressBar.style.width = s.progress + "%";
    } else if (s.status === "ready") {
      statusEl.textContent = "Download complete and verified. Click Install & Restart to install v" + s.latest + ".";
      progressWrap.style.display = "none";
      btnDownload.style.display = "none";
      btnApply.style.display = "inline-block";
    } else if (s.status === "installing") {
      statusEl.textContent = "Installing — the app will restart…";
      btnCheck.disabled = true;
      btnApply.disabled = true;
    } else if (s.status === "error") {
      statusEl.textContent = s.error || "Update check failed";
      btnCheck.disabled = false;
    } else {
      statusEl.textContent = "—";
    }
  }

  function poll() {
    fetch("/update/status").then(function (r) { return r.json(); }).then(function (s) {
      render(s);
      if (s && ["checking", "downloading", "installing"].indexOf(s.status) >= 0) {
        setTimeout(poll, 1000);
      }
    }).catch(function () {});
  }

  btnCheck.addEventListener("click", function () {
    statusEl.textContent = "Checking for updates…";
    btnCheck.disabled = true;
    fetch("/update/check", {method: "POST", cache: "no-store"})
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s && s.error) { statusEl.textContent = s.error; btnCheck.disabled = false; return; }
        render(s);
        btnCheck.disabled = false;
      })
      .catch(function () { statusEl.textContent = "Check failed. Please try again later."; btnCheck.disabled = false; });
  });

  btnDownload.addEventListener("click", function () {
    fetch("/update/download", {method: "POST", cache: "no-store"})
      .then(function (r) { return r.json(); })
      .then(function (d) { if (!d.ok && d.error) alert(d.error); poll(); })
      .catch(function () {});
  });

  btnApply.addEventListener("click", function () {
    if (!confirm("Install the new version and restart the app? Your current code will be backed up automatically.")) return;
    btnApply.disabled = true;
    fetch("/update/apply", {method: "POST", cache: "no-store"})
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok && d.error) { alert(d.error); btnApply.disabled = false; return; }
        statusEl.textContent = "Installing — the app will restart…";
        poll();
      })
      .catch(function () {});
  });

  poll();
})();
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
.toolbar{background:#fff;padding:10px 32px;border-bottom:1px solid #e0e0e0;display:flex;gap:10px;align-items:center;flex-wrap:wrap;row-gap:8px;overflow-x:visible}
.toolbar button{height:36px;padding:0 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;display:inline-flex;align-items:center;justify-content:center;line-height:1;box-sizing:border-box}
.btn-refresh{background:#3498db;color:#fff}.btn-refresh:hover{background:#2980b9}
.btn-nav{background:#ecf0f1;color:#555}.btn-nav:hover{background:#dfe6e9}
.date-display{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 12px;box-sizing:border-box;font-size:16px;font-weight:600;color:#fff;cursor:default;border-radius:6px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);line-height:1}
.date-arrow{display:inline-flex;align-items:center;justify-content:center;height:34px;width:34px;padding:0;box-sizing:border-box;background:rgba(255,255,255,.12);color:#fff;border:none;border-radius:6px;cursor:default;font-size:15px;line-height:1;opacity:.5}
.btn-today{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 12px;box-sizing:border-box;background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;line-height:1}
.btn-today:hover{background:rgba(255,255,255,.3)}
.loading-area{text-align:center;padding:70px 20px;color:#999}
.stage-label{font-size:19px;font-weight:700;color:#2c3e50;margin-bottom:12px}
.progress-wrap{width:100%;max-width:560px;height:14px;margin:0 auto 10px;background:#e2e8f0;border-radius:999px;overflow:hidden;position:relative}
.progress-bar{height:100%;width:0;background:linear-gradient(90deg,#2563eb,#38bdf8);border-radius:999px;transition:width .4s ease}
.progress-bar.indeterminate{width:35%;animation:indet 1.2s ease-in-out infinite}
@keyframes indet{0%{transform:translateX(-120%)}100%{transform:translateX(400%)}}
.progress-bar.error{width:100%;background:linear-gradient(90deg,#dc2626,#f87171)}
.progress-meta{display:flex;justify-content:center;gap:20px;font-size:13px;color:#888;margin-bottom:6px}
.loading-area .msg{color:#555;font-size:15px;margin-bottom:8px}
.loading-area .error{color:#e74c3c;font-size:13px;max-width:600px;margin:16px auto 0;text-align:left;background:rgba(231,76,60,.08);padding:16px;border-radius:8px;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto}
.log-toggle{margin-top:20px;font-size:13px;color:#2563eb;cursor:pointer;user-select:none;display:none}
.log-box{display:none;max-width:640px;margin:10px auto 0;text-align:left;background:#0f172a;color:#cbd5e1;border-radius:8px;padding:12px 14px;font-size:12px;line-height:1.5;max-height:220px;overflow-y:auto}
.log-box div{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,Consolas,monospace}
.log-line-error{color:#fca5a5;font-weight:600}
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
      <span style="width:1px;height:22px;background:rgba(255,255,255,.25);margin:0 6px"></span>
      <button class="btn-settings" onclick="location.href='/settings'" title="Settings (⌘,)" aria-label="Settings">&#x2699;</button>
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
  <div class="stage-label" id="stage-label">Starting…</div>
  <div class="progress-wrap"><div class="progress-bar indeterminate" id="progress-bar"></div></div>
  <div class="progress-meta">
    <span id="pct">In progress…</span>
    <span id="elapsed">Elapsed 0s</span>
  </div>
  <p class="msg" id="msg">Starting pipeline...</p>
  <div class="error" id="error" style="display:none"></div>
  <div class="log-toggle" id="log-toggle" onclick="toggleLog()">▸ View run log</div>
  <div class="log-box" id="log-box"><div id="log-content"></div></div>
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
let startTs = null;
let elapsedTimer = null;

function fmtTime(sec) {
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + 'm ' + s + 's';
}

function renderStatus(d) {
  const bar = document.getElementById('progress-bar');
  const pctEl = document.getElementById('pct');
  const stageEl = document.getElementById('stage-label');
  const msgEl = document.getElementById('msg');
  const errEl = document.getElementById('error');
  const retryEl = document.getElementById('retry');
  const backEl = document.getElementById('back');
  const logToggle = document.getElementById('log-toggle');
  const logBox = document.getElementById('log-box');
  const logContent = document.getElementById('log-content');

  stageEl.textContent = d.stage_label || 'Starting…';
  msgEl.textContent = d.message || '';
  bar.classList.remove('indeterminate', 'error');
  bar.style.width = '';

  if (d.status === 'error') {
    bar.classList.add('error');
    pctEl.textContent = 'Failed';
    errEl.style.display = 'block';
    errEl.textContent = d.message || 'Pipeline failed.';
    retryEl.style.display = 'inline-block';
    backEl.style.display = 'inline-block';
  } else if (d.status === 'done') {
    bar.style.width = '100%';
    pctEl.textContent = '100%';
  } else {
    if (d.total > 0) {
      const pct = Math.round(100 * d.done / d.total);
      bar.style.width = pct + '%';
      pctEl.textContent = pct + '%';
    } else {
      bar.classList.add('indeterminate');
      pctEl.textContent = 'In progress…';
    }
  }

  if (d.log && d.log.length) {
    logToggle.style.display = 'block';
    logContent.innerHTML = d.log.map(function (line) {
      const esc = String(line).replace(/[&<>"']/g, function (c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
      });
      const isErr = /error|traceback|failed|exception/i.test(line);
      return '<div class="' + (isErr ? 'log-line-error' : '') + '">' + esc + '</div>';
    }).join('');
    logBox.scrollTop = logBox.scrollHeight;
    if (d.status === 'error') {
      logBox.style.display = 'block';
      logToggle.textContent = '▾ Hide run log';
    }
  }

  if (d.status === 'running') {
    if (startTs === null) startTs = Date.now() - (d.elapsed || 0) * 1000;
    if (!elapsedTimer) {
      elapsedTimer = setInterval(function () {
        document.getElementById('elapsed').textContent = 'Elapsed ' + fmtTime(Math.floor((Date.now() - startTs) / 1000));
      }, 1000);
    }
  } else if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function toggleLog() {
  const box = document.getElementById('log-box');
  const toggle = document.getElementById('log-toggle');
  const open = box.style.display === 'block';
  box.style.display = open ? 'none' : 'block';
  toggle.textContent = open ? '▸ View run log' : '▾ Hide run log';
  if (!open) box.scrollTop = box.scrollHeight;
}

function poll() {
  fetch('/status').then(r=>r.json()).then(d=>{
    renderStatus(d);
    if (d.status === 'done') {
      setTimeout(function () { window.location.href = '/digest'; }, 900);
    } else if (d.status === 'error') {
      // stay on this page; Retry / Back to Digest buttons are shown
    } else {
      setTimeout(poll, 1500);
    }
  }).catch(()=>setTimeout(poll, 3000));
}
poll();
</script>
<script>
window.APD_DIGEST_STATUS = {{ digest_status_map() | tojson }};
</script>
{{ calendar_snippet | safe }}
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
.toolbar{background:#fff;padding:10px 32px;border-bottom:1px solid #e0e0e0;display:flex;gap:10px;align-items:center;flex-wrap:wrap;row-gap:8px;overflow-x:visible}
.toolbar button{height:36px;padding:0 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;display:inline-flex;align-items:center;justify-content:center;line-height:1;box-sizing:border-box}
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
.adj-badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:700;flex-shrink:0}
.adj-pos{background:#e8f5e9;color:#2e7d32}
.adj-neg{background:#fdecea;color:#c62828}
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
.checkbox-group label{display:inline-flex;align-items:center;gap:5px;height:36px;cursor:pointer;color:#555}
.checkbox-group input[type="checkbox"]{cursor:pointer;width:16px;height:16px}
.date-display{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 12px;box-sizing:border-box;font-size:16px;font-weight:600;color:#fff;cursor:pointer;border-radius:6px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);line-height:1;user-select:none}
.date-display:hover{background:rgba(255,255,255,.2)}
.date-arrow{display:inline-flex;align-items:center;justify-content:center;height:34px;width:34px;padding:0;box-sizing:border-box;background:rgba(255,255,255,.12);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:15px;line-height:1}
.date-arrow:hover{background:rgba(255,255,255,.25)}
.btn-today{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 12px;box-sizing:border-box;background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;line-height:1}
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
      <span style="width:1px;height:22px;background:rgba(255,255,255,.25);margin:0 6px"></span>
      <button class="btn-settings" onclick="location.href='/settings'" title="Settings (⌘,)" aria-label="Settings">&#x2699;</button>
    </div>
  </div>
</div>
<div class="toolbar">
  <button class="btn-refresh" onclick="rerunWithPrefs()" title="Re-run pipeline" style="font-size:18px">&#x21bb;</button>
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
      {% if paper.scoring_failed %}
      <span class="score-badge score-low" style="background:#dc2626">No score</span>
      {% else %}
      <span class="score-badge {% if paper.score >= 7 %}score-high{% elif paper.score >= 5 %}score-mid{% else %}score-low{% endif %}">{{ paper.score }}/10</span>
      {% if paper.score_adjustment %}<span class="adj-badge {% if paper.score_adjustment > 0 %}adj-pos{% else %}adj-neg{% endif %}" title="Preference adjustment (relative to LLM raw score)">{{ '%+.1f' | format(paper.score_adjustment) }}</span>{% endif %}
      {% endif %}
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
const DIGEST_DATE = {{ digest.date | tojson }};
function giveFeedback(btn, action) {
  const id = btn.dataset.id, title = btn.dataset.title, score = btn.dataset.score;
  const wasActive = btn.classList.contains('active');
  fetch('/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({paper_id:id, title:title, action: wasActive ? 'cancel' : action, original_score:parseInt(score), date: DIGEST_DATE})
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
  if (!confirm('Re-run the pipeline for this date? This will regenerate the digest and may consume LLM API credits.')) return;
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

function countVisibleBetween(tier, nextTier) {
  let count = 0;
  let el = tier.nextElementSibling;
  while (el && el !== nextTier) {
    if (el.classList.contains('card') && el.style.display !== 'none') count++;
    el = el.nextElementSibling;
  }
  return count;
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
    const visibleCount = countVisibleBetween(tier, nextTier);
    const countEl = tier.querySelector('.tier-count');
    if (countEl) countEl.textContent = visibleCount;
    // Also update toolbar button count
    const btnCount = document.querySelector(`.btn-nav[data-tier-idx="${idx}"] .btn-tier-count`);
    if (btnCount) btnCount.textContent = visibleCount;
  });
  
  // Update total + highly-relevant stat from the visible tier counts, so the
  // header numbers stay consistent with the filtered cards.
  const statsEl = document.querySelector('.stats');
  if (statsEl) {
    const firstTier = tierHeaders[0];
    const visibleHighly = firstTier ? countVisibleBetween(firstTier, tierHeaders[1] || null) : 0;
    statsEl.innerHTML = `Total: ${totalVisible} papers &nbsp;|&nbsp; Highly relevant: ${visibleHighly}`;
  }
}
</script>
<script>
// Apply initial filter on page load
filterCards();
</script>
<script>
window.APD_DIGEST_STATUS = {{ digest_status_map() | tojson }};
</script>
{{ calendar_snippet | safe }}
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
.toolbar{background:#fff;padding:10px 32px;border-bottom:1px solid #e0e0e0;display:flex;gap:10px;align-items:center;flex-wrap:wrap;row-gap:8px;overflow-x:visible}
.toolbar button{height:36px;padding:0 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;display:inline-flex;align-items:center;justify-content:center;line-height:1;box-sizing:border-box}
.btn-refresh{background:#3498db;color:#fff}.btn-refresh:hover{background:#2980b9}
.btn-nav{background:#ecf0f1;color:#555}.btn-nav:hover{background:#dfe6e9}
.no-data{text-align:center;padding:80px 20px;color:#999}
.no-data h2{font-size:20px;color:#666;margin-bottom:12px}
.no-data p{font-size:14px;margin-bottom:24px}
.no-data a{color:#3498db;text-decoration:none}
.date-display{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 12px;box-sizing:border-box;font-size:16px;font-weight:600;color:#fff;cursor:pointer;border-radius:6px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);line-height:1;user-select:none}
.date-display:hover{background:rgba(255,255,255,.2)}
.date-arrow{display:inline-flex;align-items:center;justify-content:center;height:34px;width:34px;padding:0;box-sizing:border-box;background:rgba(255,255,255,.12);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:15px;line-height:1}
.date-arrow:hover{background:rgba(255,255,255,.25)}
.btn-today{display:inline-flex;align-items:center;justify-content:center;height:34px;padding:0 12px;box-sizing:border-box;background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;line-height:1}
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
      <span style="width:1px;height:22px;background:rgba(255,255,255,.25);margin:0 6px"></span>
      <button class="btn-settings" onclick="location.href='/settings'" title="Settings (⌘,)" aria-label="Settings">&#x2699;</button>
    </div>
  </div>
</div>
<div class="toolbar">
  <button class="btn-refresh" onclick="if(!confirm('Run the pipeline for this date? This may consume LLM API credits.'))return;location.href='/run?date={{ selected_date }}'" title="Re-run pipeline">&#x21bb;</button>
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
<script>
window.APD_DIGEST_STATUS = {{ digest_status_map() | tojson }};
</script>
{{ calendar_snippet | safe }}
</body>
</html>"""


# --- Routes ---

# Arxiv 2026 holidays (no announcements on these dates)
_ARXIV_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2026-12-29", "2026-12-31",
}

def _digest_today_str() -> str:
    """Today's date in the configured digest timezone (config.yaml)."""
    try:
        import yaml
        from zoneinfo import ZoneInfo
        with open(os.path.join(_PROJECT_DIR, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        tz_name = (cfg.get("timezone") or {}).get("digest", "Asia/Shanghai")
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        return date.today().isoformat()


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


def _digest_status_map() -> dict:
    """Map date -> 'green' | 'orange' | 'gray' for dates that have a digest file.

    green  = digest has content (all papers scored)
    orange = digest has content but some papers have no score
    gray   = digest exists but has no content (empty digest)

    Uses the **Content:** tag written by the pipeline when present (fast,
    header-only classification), and falls back to a quick scan for older
    digest files that predate the tag.  Never runs the pipeline.
    """
    result = {}
    for date_str in get_available_dates():
        path = get_digest_path_for_date(date_str)
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        tag = re.search(r"\*\*Content:\*\* (\w+)", content)
        if tag:
            result[date_str] = {"empty": "gray", "partial": "orange", "full": "green"}.get(tag.group(1), "green")
            continue
        # Fallback for older digest files without the tag
        if re.search(r"\*\*Status:\*\* \w+", content):
            result[date_str] = "gray"
        elif re.search(r"\*\*Total papers reviewed:\*\* 0\b", content):
            result[date_str] = "gray"
        elif re.search(r"\*\*Score:\*\* No score", content):
            result[date_str] = "orange"
        else:
            result[date_str] = "green"
    return result


# Shared UI assets for the calendar popover and settings entry, exposed to
# every template via Jinja globals so no route needs to pass them explicitly.
app.jinja_env.globals["calendar_snippet"] = _CALENDAR_SNIPPET
app.jinja_env.globals["digest_status_map"] = _digest_status_map


def _render_digest(digest=None):
    """Render digest template with computed variables."""
    d = digest or _current_digest
    today_str = _digest_today_str()
    # Old digest files may not carry a parseable date; fall back to today so
    # date navigation and the update-day check never see an empty value.
    if not d.get("date"):
        d["date"] = today_str
    # Handle empty digests (0 papers)
    if d["total_papers"] == 0:
        status = d.get("status", "no_papers")
        is_today = (d.get("date", "") == today_str)
        if status == "no_announcement":
            msg = "No arXiv announcement on this day (no weekend announcements / holiday deferral)"
        elif status == "deferred_or_lagging":
            msg = "This day's batch may be deferred (holiday) or the listing lags; no content yet"
        elif is_today and status == "no_papers":
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
    today_str = _digest_today_str()
    return render_template_string(DIGEST_TEMPLATE, digest=d, prefs=prefs, today_str=today_str)


@app.route("/setup", methods=["GET"])
def setup_page():
    """Show the first-run setup wizard (no update module)."""
    return render_template_string(SETUP_TEMPLATE, **_setup_context())


@app.route("/settings", methods=["GET"])
def settings_page():
    """Show the two-column settings page with per-section panels."""
    context = _setup_context()
    context["prefs"] = load_preferences()
    context["current_version"] = updater.get_current_version()
    return render_template_string(SETTINGS_TEMPLATE, **context)


@app.route("/setup", methods=["POST"])
def setup_submit():
    """Process the first-run wizard form and write configuration."""
    config, env_values = _load_config_and_env()
    api_key = request.form.get("api_key", "").strip()
    api_key_env = _apply_llm(
        config, env_values,
        provider=request.form.get("provider", "deepseek"),
        api_key=api_key,
        model=request.form.get("model", "").strip(),
        base_url=request.form.get("base_url", "").strip(),
    )
    _apply_interests(config, request)
    _apply_email(
        config, env_values,
        enable_email=request.form.get("enable_email") == "on",
        email_sender=request.form.get("email_sender", "").strip(),
        email_recipient=request.form.get("email_recipient", "").strip(),
        smtp_server=request.form.get("smtp_server", "").strip(),
        smtp_protocol=request.form.get("smtp_protocol", "ssl"),
        smtp_port_value=request.form.get("smtp_port", "").strip(),
        email_password=request.form.get("email_password", "").strip(),
    )
    _write_env(env_values)
    _write_config(config)
    os.environ[api_key_env] = api_key
    return redirect("/")


@app.route("/settings/save", methods=["POST"])
def settings_save():
    """Save one settings section (llm | interests) and return to its panel."""
    section = request.args.get("section", "")
    config, env_values = _load_config_and_env()

    if section == "llm":
        api_key = request.form.get("api_key", "").strip()
        api_key_env = _apply_llm(
            config, env_values,
            provider=request.form.get("provider", "deepseek"),
            api_key=api_key,
            model=request.form.get("model", "").strip(),
            base_url=request.form.get("base_url", "").strip(),
        )
        _write_env(env_values)
        _write_config(config)
        os.environ[api_key_env] = api_key
        return redirect("/settings#llm")

    if section == "interests":
        _apply_interests(config, request)
        _write_config(config)
        return redirect("/settings#interests")

    abort(400, "Unknown settings section.")
@app.route("/")
def index():
    """Landing page: show last viewed or latest digest."""
    if _pipeline_status == "running":
        prefs = load_preferences()
        display_date = prefs.get("last_viewed_date") or _digest_today_str()
        return render_template_string(STATUS_PAGE, display_date=display_date, today_str=_digest_today_str())
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
    today_str = _digest_today_str()
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
    today_str = _digest_today_str()
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
    elapsed = 0
    if _pipeline_started_at is not None:
        elapsed = int(time.time() - _pipeline_started_at)
    stage = _pipeline_progress.get("stage", "")
    return jsonify({
        "app": "AstroPaperDigest",
        "status": _pipeline_status,
        "message": _pipeline_message,
        "stage": stage,
        "stage_label": _STAGE_LABELS.get(stage, ""),
        "done": _pipeline_progress.get("done", 0),
        "total": _pipeline_progress.get("total", 0),
        "elapsed": elapsed,
        "log": list(_pipeline_log)[-15:],
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
    return render_template_string(STATUS_PAGE, display_date=target_date or _digest_today_str(), today_str=_digest_today_str())


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
    if isinstance(data.get("auto_check_updates"), bool):
        prefs["auto_check_updates"] = data["auto_check_updates"]
    if data.get("dismissed_update_version"):
        prefs["dismissed_update_version"] = str(data["dismissed_update_version"])
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

    # Which digest does this feedback refer to? The page sends its rendered date.
    date_str = ""
    raw_date = data.get("date", "")
    if isinstance(raw_date, str) and raw_date:
        try:
            date.fromisoformat(raw_date)
            date_str = raw_date
        except ValueError:
            date_str = ""
    if not date_str:
        date_str = (_current_digest.get("date") or _digest_today_str()) if _current_digest else _digest_today_str()

    # Enrich the feedback with categories + abstract snippet so the learned
    # profile can extract meaningful topic signals later.
    categories = []
    abstract_snippet = ""
    digest_path = get_digest_path_for_date(date_str)
    if digest_path:
        d = parse_digest(digest_path)
        for tier in d.get("tiers", []):
            for p in tier.get("papers", []):
                if p.get("paper_id") == paper_id:
                    cats = p.get("categories", "")
                    if isinstance(cats, str):
                        categories = [c.strip() for c in cats.split(",") if c.strip()]
                    elif isinstance(cats, list):
                        categories = [str(c).strip() for c in cats if str(c).strip()]
                    abstract_snippet = (p.get("abstract") or "")[:500]
                    break
            if categories or abstract_snippet:
                break

    feedback = load_feedback()
    feedback = [fb for fb in feedback if fb.get("paper_id") != paper_id]

    if action != "cancel":
        feedback.append({
            "paper_id": paper_id,
            "title": data.get("title", ""),
            "action": action,
            "original_score": data.get("original_score", 0),
            "categories": categories,
            "abstract_snippet": abstract_snippet,
            "date": date_str,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        })

    save_feedback(feedback)

    # Rebuild the learned profile so the next ranking uses the new feedback.
    try:
        cfg, _ = _load_config_and_env()
        rebuild_learned_profile(config_keywords=cfg.get("keywords", []))
    except Exception:
        pass  # learned profile is best-effort; never break feedback recording

    return jsonify({"ok": True})


@app.route("/learned-profile", methods=["GET"])
def learned_profile_get():
    """Return the learned preference profile (derive one if missing)."""
    profile = load_learned_profile()
    if profile is None:
        cfg, _ = _load_config_and_env()
        profile = rebuild_learned_profile(config_keywords=cfg.get("keywords", []))
    return jsonify(profile)


@app.route("/learned-profile", methods=["POST"])
def learned_profile_update():
    """Edit one learned-preference entry.

    Body: {kind: "keyword_weights"|"category_weights", term: str,
           op: "set"|"ignore"|"revert", weight: float (required for set)}
    - set:    apply a manual weight (overrides the auto-learned value)
    - ignore: hide/suppress the entry so it no longer affects scoring
    - revert: remove the manual override / ignore so the entry goes back to auto
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a JSON object.")

    kind = data.get("kind", "")
    term = str(data.get("term", "")).strip()
    op = data.get("op", "")
    if kind not in ("keyword_weights", "category_weights"):
        abort(400, "kind must be keyword_weights or category_weights.")
    if not term:
        abort(400, "term is required.")
    if op not in ("set", "ignore", "revert"):
        abort(400, "op must be set, ignore or revert.")

    if kind == "keyword_weights":
        term = term.lower()

    profile = load_learned_profile()
    manual = (profile or {}).get("manual", {}) or {}
    m = dict(manual.get(kind, {}) or {})

    if op == "revert":
        m.pop(term, None)
    elif op == "ignore":
        m[term] = None
    else:  # set
        try:
            w = float(data.get("weight"))
        except (TypeError, ValueError):
            abort(400, "weight must be a number for op=set.")
        if not math.isfinite(w):
            abort(400, "weight must be a finite number.")
        m[term] = w

    manual[kind] = m
    cfg, _ = _load_config_and_env()
    profile = rebuild_learned_profile(config_keywords=cfg.get("keywords", []),
                                      manual=manual)
    return jsonify({"ok": True, "profile": profile})


@app.route("/learned-profile/reset", methods=["POST"])
def learned_profile_reset():
    """Clear feedback history, manual overrides and the learned profile."""
    reset_learned_profile()
    cfg, _ = _load_config_and_env()
    profile = rebuild_learned_profile(config_keywords=cfg.get("keywords", []))
    return jsonify({"ok": True, "profile": profile})


@app.route("/update/status")
def update_status():
    """JSON status of the update checker (polled by banner/settings JS)."""
    with _update_lock:
        return jsonify(dict(_update_state))


@app.route("/update/check", methods=["POST"])
def update_check_now():
    """Manually check for updates (from the settings page)."""
    with _update_lock:
        busy = _update_state.get("status") in ("checking", "downloading", "installing")
    if busy:
        return jsonify({"ok": False, "error": "Another update operation is already in progress."}), 409
    _start_update_check()
    with _update_lock:
        return jsonify(dict(_update_state))


@app.route("/update/download", methods=["POST"])
def update_download():
    """Start downloading the latest release zip (background)."""
    with _update_lock:
        if _update_state.get("status") != "available":
            return jsonify({"ok": False, "error": "No update available to download."}), 400
        url = _update_state.get("download_url") or ""
        version = _update_state.get("latest") or ""
        expected_sha = _update_state.get("sha256") or ""
    if not url:
        return jsonify({"ok": False, "error": "Missing download URL."}), 400
    with _update_lock:
        _update_state.update({"status": "downloading", "progress": 0})
    Thread(target=_download_update, args=(url, version, expected_sha), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/update/apply", methods=["POST"])
def update_apply():
    """Confirm install: write marker, spawn detached updater, restart."""
    if _pipeline_status == "running":
        return jsonify({"ok": False, "error": "The paper pipeline is running. Please wait for it to finish before updating."}), 409
    with _update_lock:
        if _update_state.get("status") != "ready":
            return jsonify({"ok": False, "error": "There is no downloaded and verified update."}), 400
        version = _update_state.get("latest") or ""
        zip_path = updater.UPDATES_DIR / f"AstroPaperDigest-v{version}.zip"
        if not zip_path.exists():
            return jsonify({"ok": False, "error": "Update package file is missing."}), 400
        _update_state["status"] = "installing"

    marker = {
        "version": version,
        "zip_path": str(zip_path),
        "server_pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    marker_path = _PROJECT_DIR / "pending_update.json"
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=2, ensure_ascii=False)

    log_path = updater.UPDATES_DIR / "apply.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")
    if getattr(sys, "frozen", False):
        updater_cmd = [str(Path(sys._MEIPASS) / "apd-cli"), "--apply", str(marker_path)]
    else:
        updater_cmd = [sys.executable, "-u", "src/updater.py", "--apply", str(marker_path)]
    subprocess.Popen(
        updater_cmd,
        cwd=str(_PROJECT_DIR),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Give the HTTP response time to flush, then shut the server down.
    Timer(1.5, _terminate_server).start()
    return jsonify({"ok": True, "message": "Installation started. The app will restart shortly."})


def _create_server(port: int):
    """Create a loopback HTTP server.  port=0 asks macOS for a free port."""
    return make_server("127.0.0.1", port or 0, app, threaded=True)


def _start_serving(server):
    """Serve Flask from a daemon thread so pywebview can own the main thread."""
    global _server
    _server = server
    thread = Thread(target=server.serve_forever, name="apd-http", daemon=True)
    thread.start()
    return thread


def _run_desktop(server):
    """Open the native pywebview window and block until the user closes it."""
    global _desktop_window
    _write_run_info(server.server_port)
    url = f"http://127.0.0.1:{server.server_port}"
    window = webview.create_window(
        "AstroPaperDigest",
        url,
        width=1200,
        height=820,
        min_size=(960, 640),
    )
    _desktop_window = window
    try:
        webview.start()
    finally:
        _desktop_window = None
        _shutdown_server()


def main():
    global _current_digest, _pipeline_message, _pipeline_status

    parser = argparse.ArgumentParser(description="AstroPaperDigest desktop app")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Optional fixed loopback port (default: 0 = random free port)",
    )
    parser.add_argument("--no-window", action="store_true", help="Serve only; don't open the desktop window")
    parser.add_argument("--no-run", action="store_true", help="Don't auto-start pipeline")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_termination_signal)
    signal.signal(signal.SIGINT, _handle_termination_signal)

    # Single instance: focus the existing window and exit instead of starting
    # a second server on a new random port.
    if not _acquire_single_instance_lock():
        if _focus_existing_instance():
            print("AstroPaperDigest is already running; focusing its window.")
        else:
            print("AstroPaperDigest appears to be running, but its window could not be focused.")
        return

    # Load existing digest if available
    digest_path = get_latest_digest_path()
    if digest_path and os.path.exists(digest_path):
        _current_digest = parse_digest(digest_path)

    # Only auto-run pipeline if today's digest doesn't exist
    today_str = _digest_today_str()
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

    # Background update check on startup (silent on failure)
    if load_preferences().get("auto_check_updates", True):
        Thread(target=_start_update_check, daemon=True).start()

    # Bind to 127.0.0.1 only; port 0 lets the OS choose a free port.
    server = _create_server(args.port)
    http_thread = _start_serving(server)

    if args.no_window:
        _write_run_info(server.server_port)
        print(f"Server: http://127.0.0.1:{server.server_port}")
        try:
            http_thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown_server()
        return

    print(f"Server: http://127.0.0.1:{server.server_port}")
    _run_desktop(server)


if __name__ == "__main__":
    main()
