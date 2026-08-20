# AstroPaperDigest

[Chinese Docs](README_zh.md)

Automatically fetches, filters, and ranks daily astrophysics papers based on your research interests using LLM-powered relevance scoring. Comes with a native desktop window for browsing, date navigation, and personalized settings.

## Features

- **LLM ranking**: DeepSeek (or any OpenAI-compatible API) scores paper relevance 1-10
- **Desktop UI**: Native macOS window (WebKit) with date navigation, tiered browsing, and real-time filtering
- **Setup wizard**: First-launch wizard for API key and research interests
- **Dual output**: BibTeX entries + Markdown digest, organized by date
- **macOS app**: Double-click to run, opens a native desktop window

## Quick Start

### Option A: dmg installer (no Python required)

1. Download **`AstroPaperDigest-<version>.dmg`** from GitHub Releases
2. Double-click it and drag **AstroPaperDigest.app** into **Applications**
3. Launch it — a native desktop window with the setup wizard opens

> First launch of a downloaded app may ask for confirmation once (right-click → **Open**, or **System Settings → Privacy & Security → Open Anyway**). Only needed once.

### Option B: source / Install.command (requires Python 3.9+)

> **Note:** Do not run from `~/Downloads/` — macOS blocks downloaded files. Move the project to a permanent location first (e.g., `~/Projects/`).

1. Double-click **`Install.command`** — sets up Python environment and builds the app
2. Double-click **`AstroPaperDigest.app`** — a native desktop window opens automatically

## Auto Update

- **When checked**: once in the background at app startup (silent on network failure), plus a manual "Check for Updates" button on the **Settings (⛭) → General → Update** page.
- **Notification**: a blue banner appears at the top of the Digest page when a new version is found; the Settings page shows current/latest version and release notes in the Update panel.
- **Install flow** (semi-automatic): click "Download Update" on the Settings page → SHA-256 verification after download → click "Install & Restart" → old code is backed up, sources replaced, the .app is rebuilt, and the app relaunches.
- **Update source**: GitHub Releases (public repo). Check endpoint: `https://api.github.com/repos/jiangrz77/AstroPaperDigest/releases/latest`.
- **Version**: single source of truth in `version.txt` (read by `build_app.sh` when building the .app).
- **Preserved files**: updates never touch `.env`, `config.yaml`, `preferences.json`, `feedback.json`, `data/`, `output/`, `.venv`; old code is backed up to `backups/`.

## Releasing a New Version (maintainers)

1. Bump `version.txt` (e.g. `1.0.3`), commit and push:
   ```bash
   git add . && git commit -m "v1.0.3" && git push origin main
   git tag v1.0.3 && git push origin v1.0.3
   ```
2. Run `./release.sh` — builds the self-contained app, then generates three artifacts under `dist/` (with SHA-256): `AstroPaperDigest-v1.0.3.source.zip` (source channel), `AstroPaperDigest-v1.0.3.app.zip` (update package), `AstroPaperDigest-1.0.3.dmg` (installer), plus `version.json`.
3. On GitHub: **Releases → Draft a new release** → pick tag `v1.0.3` → write release notes → upload all three artifacts → **Publish release** (do NOT mark it Pre-release). Alternatively push the tag and let the GitHub Actions workflow build and attach everything automatically.
4. dmg users install the new version from the dmg; existing app users see the banner and update in one click after launching.

> If the repo is private: GitHub Releases cannot be accessed anonymously. Upload the `version.json` + `*.app.zip` from `release.sh` to any static host and point `update.github_repo` in `config.yaml` at it (or switch to a self-hosted static JSON update source).

## Requirements

- macOS (Apple Silicon or Intel)
- dmg installer: **no Python needed** (self-contained)
- Install.command channel: Python 3.9+ (pre-installed on most Macs via Xcode Command Line Tools)
- DeepSeek API key (or any OpenAI-compatible provider)

## Acknowledgment

Thank you to arXiv for use of its open access interoperability.

## License

MIT