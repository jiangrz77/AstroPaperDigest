# ArXivDailyDigest

[中文文档](README_zh.md)

Automatically fetches, filters, and ranks daily arXiv papers based on your research interests using LLM-powered relevance scoring. Comes with a Web UI for browsing, date navigation, and personalized settings.

## Features

- **Web UI**: Flask-powered local web interface with paper browsing, date navigation, and real-time filtering
- **Setup wizard**: First-launch wizard guides you through LLM API, research interests, and email configuration
- **Smart filtering**: Two-stage filter using arXiv categories + keyword matching, with cross-list and replacement exclusion
- **LLM ranking**: Uses DeepSeek (or any OpenAI-compatible API) to score paper relevance 1-10
- **Date navigation**: Browse historical digests by date, auto-detects arXiv update days (weekdays) vs non-update days (weekends/holidays)
- **Dual output**: BibTeX entries + Markdown digest, organized by date
- **Feedback system**: Mark papers as "Overrated" or "Underrated" to calibrate future recommendations
- **Email notifications**: Optional daily digest via SMTP (self-send-self-receive mode)
- **macOS app**: Double-click to run, auto-opens browser

## Quick Start

### Option 1: macOS App (Recommended)

> **Note:** Do not run from `~/Downloads/` — macOS blocks downloaded files. Move the project to a permanent location first (e.g., `~/Projects/` or `~/Applications/`).

The `.app` bundle is not included in the repo. Build it first:

```bash
./build_app.sh   # Creates .venv, installs deps, builds .app
```

Then double-click `ArXivDailyDigest.app` — your browser opens automatically:
- First run: Shows the setup wizard
- Subsequent runs: Displays the latest paper digest directly

> **macOS security prompt?** On first launch, macOS may block the app. Go to **System Settings → Privacy & Security**, then click **"Open Anyway"**. This only happens once.

### Option 2: Command Line

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up environment
cp .env.example .env
# Edit .env and add your API key

# 3. Configure your research interests
# Edit config.yaml: categories, keywords, etc.

# 4. Run (CLI mode)
python main.py

# 5. Or launch the Web UI
python src/gui.py
```

## Web UI Features

After launching the Web UI (`python src/gui.py` or double-click .app), open `http://127.0.0.1:5123`:

- **Date navigation**: Top date picker + left/right arrows to switch dates
- **Tiered browsing**: Papers grouped by "Highly Relevant" / "Possibly Relevant" / "Marginally Relevant"
- **Real-time filtering**: Toggle "Cross-listed" and "Replacements" checkboxes to filter instantly
- **Feedback buttons**: Mark each paper as "Overrated" or "Underrated"
- **Settings page**: Click the gear icon in the toolbar to modify configuration
- **Re-run**: Click the ↻ button to re-fetch and re-rank papers for the current date

## Configuration

### Setup Wizard (Web UI)

Shown automatically on first launch; accessible anytime via the gear icon in the toolbar:

1. **LLM API**: Choose provider (DeepSeek/OpenAI/Custom), enter API Key and model name
2. **Research interests**: Quick mode (select categories + keywords) or upload a .bib file
3. **Email notifications**: Configure SMTP server, port, sender (optional)

### Config File (config.yaml)

| Section | Description |
|---------|-------------|
| `arxiv_categories` | arXiv categories to monitor (e.g., `astro-ph.GA`) |
| `keywords` | Keywords for initial filtering |
| `bib_file` | Path to your BibTeX collection (used to build interest profile) |
| `llm` | LLM provider settings (base_url, model, api_key_env) |
| `output` | Output directories for BibTeX and digests |
| `email` | SMTP notification settings |
| `filter` | Score threshold, max candidates, fetch window |

### Environment Variables (.env)

```bash
DEEPSEEK_API_KEY="sk-..."        # LLM API key (required)
EMAIL_APP_PASSWORD="..."          # Email app password (optional)
EMAIL_SENDER="you@example.com"    # Sender email
EMAIL_RECIPIENT="you@example.com" # Recipient email (same as sender)
SMTP_SERVER="smtp.gmail.com"      # SMTP server
SMTP_PORT="587"                   # SMTP port
```

## CLI Options

```bash
python main.py [options]

--config PATH        Config file path (default: config.yaml)
--days N             Fetch papers from last N days (default: 3)
--threshold N        Minimum score for BibTeX output (default: 7)
--target-date DATE   Fetch for a specific date (format: YYYY-MM-DD)
--no-cross           Exclude cross-listed papers
--no-replacements    Exclude replacement (updated) papers
--no-email           Skip email notification
--dry-run            Skip LLM ranking (for testing)
--update-profile     Print interest profile summary and exit
```

## Output Structure

```
output/
  bibtex/
    recommendations_2026-07-29.bib
    recommendations_2026-07-30.bib
  digests/
    digest_2026-07-29.md
    digest_2026-07-30.md
```

## arXiv Update Schedule

arXiv publishes new papers on weekdays (20:00 ET = 08:00 Beijing time next day):

| Submission Deadline (ET) | Publication (ET) | Beijing Time |
|--------------------------|------------------|--------------|
| Mon 14:00 - Tue 14:00 | Tue 20:00 | Wed 08:00 |
| Tue 14:00 - Wed 14:00 | Wed 20:00 | Thu 08:00 |
| Wed 14:00 - Thu 14:00 | Thu 20:00 | Fri 08:00 |
| Thu 14:00 - Fri 14:00 | Sun 20:00 | Mon 08:00 |
| Fri 14:00 - Mon 14:00 | Mon 20:00 | Tue 08:00 |

No publications on weekends or holidays. The system auto-detects non-update days and shows a notice.

## Email Setup

> **Note:** The email notification feature is still under development. You can skip this section — no email configuration is needed.

Email uses a "self-send-self-receive" mode: sender and recipient are the same mailbox, so you receive your own daily digest.

**Common SMTP settings:**
| Provider | Server | Port | SSL |
|----------|--------|------|-----|
| Gmail | smtp.gmail.com | 587 | false |
| CSTNet | cstnet.mail.cn | 587 | false |
| QQ Mail | smtp.qq.com | 465 | true |
| 163 Mail | smtp.163.com | 465 | true |

## Project Structure

```
├── main.py              # CLI entry point
├── build_app.sh         # macOS .app build script
├── config.yaml          # Configuration file
├── .env                 # Environment variables (API key, etc.)
├── .env.example         # Environment variable template
├── requirements.txt     # Python dependencies
├── src/
│   ├── gui.py           # Web UI (Flask)
│   ├── fetch_arxiv.py   # arXiv paper fetching
│   ├── filter.py        # Keyword filtering
│   ├── ranker.py        # LLM ranking
│   ├── profile.py       # Interest profile building
│   ├── output.py        # Output generation
│   ├── notifier.py      # Email notifications
│   └── digest_parser.py # Digest parsing
├── data/                # Data files (.bib collections)
├── output/              # Output directory
│   ├── bibtex/          # BibTeX files
│   └── digests/         # Markdown digests
├── assets/              # App icons
└── ArXivDailyDigest.app/ # macOS app bundle (generated by build_app.sh)
```

## Requirements

- Python 3.9+
- DeepSeek API key (or any OpenAI-compatible provider)
- Optional: Email app password for notifications

## License

MIT
