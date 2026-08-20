#!/usr/bin/env python3
"""Update checker and installer for AstroPaperDigest.

Checks GitHub Releases (or a static JSON manifest) for a newer version,
downloads and verifies the source zip, then applies it with a restart.

All standard-library only - no new dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from src import paths as _paths
_PROJECT_DIR = _paths.data_dir()
VERSION_FILE = _PROJECT_DIR / "version.txt"
UPDATES_DIR = _PROJECT_DIR / "output" / "updates"
BACKUPS_DIR = _PROJECT_DIR / "backups"
PENDING_FILE = _PROJECT_DIR / "pending_update.json"

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_UA = "AstroPaperDigest-Updater/1.0"
NETWORK_TIMEOUT = 6
DOWNLOAD_TIMEOUT = 60

# Top-level entries that belong to the user and must NEVER be replaced.
KEEP_TOP = {
    ".env",
    "config.yaml",
    "preferences.json",
    "feedback.json",
    "data",
    "output",
    ".venv",
    "venv",
    ".git",
    "backups",
    ".DS_Store",
    "pending_update.json",
}


class UpdateCheckError(Exception):
    """Raised when the update check fails (network, 404, rate limit...)."""


class UpdateApplyError(Exception):
    """Raised when applying an update fails."""


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def get_current_version() -> str:
    """Read the single source of truth (version.txt)."""
    try:
        v = VERSION_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    return "0.0.0"


def parse_version(v: str):
    """Return a comparable tuple (major, minor, patch) from 'v1.2.3' etc."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def _http_json(url: str, timeout: int = NETWORK_TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": GITHUB_UA,  # GitHub API requires a User-Agent
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _update_package_name(version: str) -> str:
    """Name of this channel's update package (source zip vs .app bundle zip)."""
    suffix = ".app" if getattr(sys, "frozen", False) else ".source"
    return f"AstroPaperDigest-v{version}{suffix}.zip"


def normalize_release(data: dict) -> dict:
    """Map a GitHub release object to our normalized shape."""
    version = (data.get("tag_name") or "").lstrip("v")
    assets = data.get("assets") or []
    zip_assets = [
        a for a in assets
        if (a.get("name") or "").lower().endswith(".zip")
    ]
    exact = [
        a for a in zip_assets
        if a.get("name") == _update_package_name(version)
    ]
    chosen = (exact or zip_assets or [None])[0]
    download_url = ""
    if chosen:
        download_url = chosen.get("browser_download_url") or ""
    if not download_url:
        download_url = data.get("zipball_url") or ""
    body = data.get("body") or ""
    sha256 = None
    m = re.search(r"(?i)sha-?256[^0-9a-f]{0,8}([0-9a-f]{64})", body)
    if m:
        sha256 = m.group(1).lower()
    return {
        "version": version,
        "tag": data.get("tag_name") or f"v{version}",
        "name": data.get("name") or data.get("tag_name") or f"v{version}",
        "notes": body.strip(),
        "published_at": data.get("published_at"),
        "download_url": download_url,
        "sha256": sha256,
        "prerelease": bool(data.get("prerelease")),
    }


def check_github_release(repo: str, timeout: int = NETWORK_TIMEOUT) -> dict:
    """Fetch the latest GitHub release. Raises UpdateCheckError on failure."""
    url = GITHUB_API.format(repo=repo)
    data = _http_json(url, timeout)
    return normalize_release(data)


def check_update(repo: str, current: str | None = None) -> dict:
    """Compare the latest release with the installed version.

    Returns a dict with: available/current/latest/tag/notes/download_url/
    sha256/published_at/error. Raises UpdateCheckError on network/API errors.
    """
    current = current or get_current_version()
    try:
        release = check_github_release(repo)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateCheckError(
                "No version information found: the repository does not exist, "
                "has no releases, or is private (private repositories cannot be "
                "checked anonymously; make it public or use a self-hosted update source)."
            ) from e
        if e.code == 403:
            raise UpdateCheckError("GitHub API access is limited (HTTP 403). Please try again later.") from e
        raise UpdateCheckError(f"Update check failed (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise UpdateCheckError(f"Network error, could not reach the update server: {reason}") from e
    except Exception as e:
        raise UpdateCheckError(f"Update check failed: {e}") from e

    if release.get("prerelease"):
        return {
            "available": False,
            "current": current,
            "latest": release["version"],
            "tag": release["tag"],
            "notes": release["notes"],
            "download_url": release["download_url"],
            "sha256": release["sha256"],
            "published_at": release["published_at"],
            "error": None,
        }
    return {
        "available": is_newer(release["version"], current),
        "current": current,
        "latest": release["version"],
        "tag": release["tag"],
        "notes": release["notes"],
        "download_url": release["download_url"],
        "sha256": release["sha256"],
        "published_at": release["published_at"],
        "error": None,
    }


# ---------------------------------------------------------------------------
# Download & verify
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path, progress_callback=None) -> Path:
    """Download url to dest (atomically via a .part file)."""
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": GITHUB_UA})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_callback:
                    progress_callback(done, total)
    tmp.replace(dest)
    return dest


def sha256_of(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path, expected: str) -> bool:
    if not expected:
        return True
    return sha256_of(path).lower() == str(expected).strip().lower()


# ---------------------------------------------------------------------------
# Applying (runs in a detached process)
# ---------------------------------------------------------------------------

def _backup_and_replace(new_root: Path, log) -> None:
    project = _PROJECT_DIR
    backup_dir = BACKUPS_DIR / f"backup_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for item in new_root.iterdir():
        name = item.name
        if name in KEEP_TOP:
            continue
        target = project / name
        if target.exists():
            if target.is_dir():
                shutil.copytree(target, backup_dir / name)
            else:
                shutil.copy2(target, backup_dir / name)
    log(f"Backed up to {backup_dir}")

    for item in new_root.iterdir():
        name = item.name
        if name in KEEP_TOP:
            continue
        target = project / name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def apply_update(version: str, zip_path: Path, log) -> dict:
    """Install a new version.

    Frozen (self-contained .app): the update package is a zip of the whole
    AstroPaperDigest.app bundle - replace the running bundle in place and
    relaunch.  Source mode keeps the historical behavior (replace source
    files, rebuild the .app).
    """
    if getattr(sys, "frozen", False):
        return _apply_frozen_bundle(version, zip_path, log)
    return _apply_source(version, zip_path, log)


def _apply_source(version: str, zip_path: Path, log) -> dict:
    """Source channel: extract zip, backup old code, replace, rebuild .app."""
    log(f"Installing v{version} ...")
    if not zip_path.exists():
        raise UpdateApplyError(f"Update package not found: {zip_path}")

    with tempfile.TemporaryDirectory(prefix="apd-update-") as td:
        extract_root = Path(td)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_root)
        entries = [
            p for p in extract_root.iterdir()
            if p.name not in ("__MACOSX",)
        ]
        if len(entries) != 1 or not entries[0].is_dir():
            raise UpdateApplyError("Invalid update package: project root not found.")
        _backup_and_replace(entries[0], log)

    log(f"Code replaced with v{version}")

    # Rebuild the .app bundle from the new source.
    log("Rebuilding AstroPaperDigest.app ...")
    res = subprocess.run(
        ["bash", "build_app.sh"],
        cwd=str(_PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if res.returncode != 0:
        log("Rebuilding .app failed (you can run ./build_app.sh manually later)")
        tail = (res.stdout or "")[-1500:] + (res.stderr or "")[-1500:]
        if tail.strip():
            log("Output snippet: " + tail.strip()[-1500:])
    else:
        log("Rebuilt .app")

    # Relaunch the app (macOS).
    relaunched = False
    if sys.platform == "darwin":
        app_path = _PROJECT_DIR.parent / "AstroPaperDigest.app"
        if app_path.exists():
            time.sleep(3)  # let the old process exit and release its resources
            subprocess.Popen(["open", str(app_path)])
            log(f"Restarted {app_path.name}")
            relaunched = True
    if not relaunched:
        log("Please restart the app manually after installation.")
    return {"ok": True}


def _apply_frozen_bundle(version: str, zip_path: Path, log) -> dict:
    """Frozen channel: replace the running .app bundle with the update zip."""
    log(f"Installing v{version} (whole-bundle replace) ...")
    if not zip_path.exists():
        raise UpdateApplyError(f"Update package not found: {zip_path}")

    # The updater process itself runs from inside the bundle we replace:
    #   <App>.app/Contents/Frameworks/apd-cli
    exe = Path(sys.executable).resolve()
    app_bundle = exe.parent.parent.parent
    if app_bundle.suffix != ".app" or not (app_bundle / "Contents" / "Info.plist").exists():
        raise UpdateApplyError(f"Could not locate the app bundle (resolved exe: {exe})")

    # ditto (not zipfile) so symlinks inside the .app survive extraction.
    with tempfile.TemporaryDirectory(prefix="apd-update-") as td:
        root = Path(td)
        res = subprocess.run(
            ["ditto", "-x", "-k", str(zip_path), str(root)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise UpdateApplyError(f"Failed to extract update package: {(res.stderr or '')[-500:]}")
        entries = [p for p in root.iterdir() if p.name != "__MACOSX"]
        if len(entries) != 1 or not entries[0].is_dir():
            raise UpdateApplyError("Invalid update package: expected one .app folder.")
        new_app = entries[0]
        if new_app.suffix != ".app" or not (new_app / "Contents" / "Info.plist").exists():
            raise UpdateApplyError("Invalid update package: not a .app bundle.")
        if new_app.name != app_bundle.name:
            new_app = new_app.rename(root / app_bundle.name)

        backup = app_bundle.with_name(
            f"{app_bundle.name}.old-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        log(f"Replacing {app_bundle}")
        shutil.move(str(app_bundle), str(backup))
        try:
            shutil.move(str(new_app), str(app_bundle))
        except Exception:
            shutil.move(str(backup), str(app_bundle))  # roll back
            raise

    # Record the installed version in the data dir (the bundle carries none).
    try:
        (_PROJECT_DIR / "version.txt").write_text(f"{version}\n", encoding="utf-8")
    except OSError:
        pass

    # Relaunch the new bundle (skip with APD_UPDATE_NO_RELAUNCH=1 in tests).
    if os.environ.get("APD_UPDATE_NO_RELAUNCH") != "1":
        subprocess.Popen(["open", str(app_bundle)])
        log(f"Restarted {app_bundle.name}")

    # Best-effort, detached cleanup of the old bundle.
    subprocess.Popen(
        ["rm", "-rf", str(backup)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True}


def main_apply(marker_path: Path) -> int:
    log_path = UPDATES_DIR / "apply.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    time.sleep(3)  # give the Flask server time to shut down

    # Defensively stop the server if it is still alive.
    pid = marker.get("server_pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
            time.sleep(1)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass

    try:
        result = apply_update(
            marker.get("version", ""),
            Path(marker.get("zip_path", "")),
            log,
        )
        log("Update complete.")
        result["log"] = "See output/updates/apply.log"
        try:
            marker_path.unlink()
        except OSError:
            pass
        return 0
    except Exception as e:
        log(f"Update failed: {e}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AstroPaperDigest updater")
    parser.add_argument("--check", metavar="REPO", help="Check GitHub repo for updates")
    parser.add_argument("--apply", metavar="MARKER", help="Apply a pending update (marker JSON path)")
    args = parser.parse_args()

    if args.apply:
        return main_apply(Path(args.apply))

    if args.check:
        try:
            result = check_update(args.check)
        except UpdateCheckError as e:
            print(f"ERROR: {e}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
