# ArXivDailyDigest

[中文文档](README_zh.md)

Automatically fetches, filters, and ranks daily arXiv papers based on your research interests using LLM-powered relevance scoring.

## Features

- **Smart filtering**: Two-stage filter using arXiv categories + keyword matching
- **LLM ranking**: Uses DeepSeek (or any OpenAI-compatible API) to score paper relevance 1-10
- **Dual output**: BibTeX entries + Markdown digest, organized by date
- **Email notifications**: Optional daily digest via SMTP
- **macOS app**: Double-click launcher for non-CLI users

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up your API key
cp .env.example .env
# Edit .env and add your DeepSeek API key

# 3. Configure your interests
# Edit config.yaml: categories, keywords, email settings

# 4. Add your paper collection (builds your interest profile)
cp ArxivDailyCollection.bib.example ArxivDailyCollection.bib
# Add your own BibTeX entries

# 5. Run
python main.py
```

## Configuration

Edit `config.yaml`:

| Section | Description |
|---------|-------------|
| `arxiv_categories` | arXiv categories to monitor (e.g., `astro-ph.GA`) |
| `keywords` | Keywords for initial filtering |
| `bib_file` | Path to your BibTeX collection |
| `llm` | LLM provider settings (DeepSeek default) |
| `output` | Output directories for BibTeX and digests |
| `email` | SMTP settings for notifications |
| `filter` | Threshold, max candidates, fetch window |

### Switching LLM Provider

The tool uses any OpenAI-compatible API. To switch from DeepSeek to OpenAI:

```yaml
llm:
  base_url: "https://api.openai.com/v1"
  api_key_env: "OPENAI_API_KEY"
  model: "gpt-4o-mini"
```

## CLI Options

```bash
python main.py [options]

--config PATH      Config file path (default: config.yaml)
--days N           Fetch papers from last N days (default: 1)
--threshold N      Minimum score for BibTeX output (default: 7)
--no-email         Skip email notification
--dry-run          Skip LLM ranking (for testing)
--update-profile   Print interest profile summary and exit
```

## Output Structure

```
bibtex/
  recommendations_2026-07-29.bib
  recommendations_2026-07-30.bib
digests/
  digest_2026-07-29.md
  digest_2026-07-30.md
```

## Email Setup

> **Note:** The email notification feature is still under development. You can skip this section — no email configuration is needed.

1. Enable email in `config.yaml`:
   ```yaml
   email:
     enabled: true
     smtp_server: "smtp.gmail.com"
     smtp_port: 587
     use_ssl: false
     sender: "you@example.com"
     recipient: "you@example.com"
   ```

2. Add your app password to `.env`:
   ```
   EMAIL_APP_PASSWORD="your-app-password"
   ```

**Common SMTP settings:**
| Provider | Server | Port | SSL |
|----------|--------|------|-----|
| Gmail | smtp.gmail.com | 587 | false |
| CSTNet | mail.cstnet.cn | 465 | true |
| QQ Mail | smtp.qq.com | 465 | true |
| 163 Mail | smtp.163.com | 465 | true |

## macOS App

Double-click `ArxivRecommend.app` to run the full pipeline. It will:
1. Load credentials from `.env`
2. Run the recommender
3. Open the latest digest
4. Show a notification with results

## Requirements

- Python 3.9+
- DeepSeek API key (or any OpenAI-compatible provider)
- Optional: Email app password for notifications

## License

MIT
