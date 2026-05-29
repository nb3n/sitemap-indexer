# Google Sitemap Indexer

Submits all URLs from a live XML sitemap to the [Google Indexing API](https://developers.google.com/search/apis/indexing-api/v3/quickstart).

Supports standard sitemaps and sitemap index files (recursive). Handles retries, rate limiting, and detailed logging automatically.

---

## Prerequisites

- Python 3.10 or higher
- A Google Cloud project with the **Indexing API** enabled
- A **Service Account** with a downloaded JSON key
- The service account email added as an **Owner** in [Google Search Console](https://search.google.com/search-console)

---

## Setup

**1. Clone the repository**

```bash
git clone https://github.com/nb3n/sitemap-indexer.git
cd sitemap-indexer
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Enable the Indexing API**

Go to [Google Cloud Console](https://console.cloud.google.com/) and enable the **Indexing API** for your project.

**4. Create a Service Account**

- In Google Cloud Console, go to IAM > Service Accounts
- Create a new service account
- Generate a JSON key and save it (e.g. `service_account.json`)

**5. Add the service account as a Search Console Owner**

- Open [Google Search Console](https://search.google.com/search-console)
- Go to Settings > Users and permissions
- Add the service account email as an **Owner**

---

## Usage

```bash
python sitemap_indexer.py --sitemap https://example.com/sitemap.xml --key service_account.json
```

### All options

| Flag | Required | Default | Description |
|---|---|---|---|
| `--sitemap` | Yes | `none` | Live URL of your sitemap or sitemap index |
| `--key` | Yes | `none` | Path to your service account JSON key file |
| `--delay` | No | `1.0` | Seconds between API requests |
| `--retries` | No | `3` | Max retries on rate-limit or server errors |
| `--backoff` | No | `5.0` | Base backoff seconds (multiplied by attempt number) |
| `--log-file` | No | `indexing_log.txt` | Path to write the full log |

### Examples

```bash
# Basic usage
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json

# Slower submission with more retries
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json \
  --delay 2.0 \
  --retries 5 \
  --backoff 10.0

# Custom log file
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json \
  --log-file logs/run_2026.txt
```

---

## Output

The script logs to both the terminal and the log file.

```
2026-01-15 10:32:01 [INFO] Sitemap Indexer started.
2026-01-15 10:32:01 [INFO] Sitemap : https://example.com/sitemap.xml
2026-01-15 10:32:01 [INFO] Key file: service_account.json
2026-01-15 10:32:02 [INFO] Fetching sitemap: https://example.com/sitemap.xml
2026-01-15 10:32:02 [INFO] Found 42 URLs in: https://example.com/sitemap.xml
2026-01-15 10:32:02 [INFO] Total unique URLs to submit: 42
2026-01-15 10:32:02 [INFO] [1/42] Submitting: https://example.com/
2026-01-15 10:32:03 [INFO] Accepted. Notify time: 2026-01-15T10:32:03.123Z
...
2026-01-15 10:33:24 [INFO] --- Summary ---
2026-01-15 10:33:24 [INFO] Total   : 42
2026-01-15 10:33:24 [INFO] Success : 41
2026-01-15 10:33:24 [INFO] Failed  : 1
```

---

## Quota

The Google Indexing API allows **200 requests per day** by default per Search Console property. You can request a quota increase in Google Cloud Console if needed.

The default `--delay 1.0` keeps submissions safe and predictable. Do not set it below `0.5` without a quota increase.

---

## Security

- **Never commit your service account JSON key to version control.** The `.gitignore` in this repo excludes all `*.json` files for this reason.
- Store the key file outside the repo or use an environment variable / secrets manager in production.

---

## License

[MIT](LICENSE)
