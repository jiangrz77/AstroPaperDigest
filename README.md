# AstroPaperDigest

[Chinese Docs](README_zh.md)

Automatically fetches, filters, and ranks daily astrophysics papers based on your research interests using LLM-powered relevance scoring. Comes with a local Web UI for browsing, date navigation, and personalized settings.

## Features

- **LLM ranking**: DeepSeek (or any OpenAI-compatible API) scores paper relevance 1-10
- **Web UI**: Local web interface with date navigation, tiered browsing, and real-time filtering
- **Setup wizard**: First-launch wizard for API key and research interests
- **Dual output**: BibTeX entries + Markdown digest, organized by date
- **macOS app**: Double-click to run, auto-opens browser

## Quick Start

> **Note:** Do not run from `~/Downloads/` — macOS blocks downloaded files. Move the project to a permanent location first (e.g., `~/Projects/`).

1. Double-click **`Install.command`** — sets up Python environment and builds the app
2. Double-click **`AstroPaperDigest.app`** — your browser opens automatically

> **macOS security prompt?** Go to **System Settings → Privacy & Security**, click **"Open Anyway"**. Only needed once.

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
2. Run `./release.sh` — generates `AstroPaperDigest-v1.0.3.zip` and `version.json` (with SHA-256).
3. On GitHub: **Releases → Draft a new release** → pick tag `v1.0.3` → write release notes → upload the zip → **Publish release** (do NOT mark it Pre-release).
4. Users see the banner and can update in one click after launching the app.

> If the repo is private: GitHub Releases cannot be accessed anonymously. Upload the `version.json` + zip from `release.sh` to any static host and point `update.github_repo` in `config.yaml` at it (or switch to a self-hosted static JSON update source).

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.9+ (pre-installed on most Macs via Xcode Command Line Tools)
- DeepSeek API key (or any OpenAI-compatible provider)

## Acknowledgment

Thank you to arXiv for use of its open access interoperability.

## License

MIT