# AstroPaperDigest

[中文文档](README_zh.md)

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

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.9+ (pre-installed on most Macs via Xcode Command Line Tools)
- DeepSeek API key (or any OpenAI-compatible provider)

## Acknowledgment

Thank you to arXiv for use of its open access interoperability.

## License

MIT
