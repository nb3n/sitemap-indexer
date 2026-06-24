# Google Sitemap Indexer

Submits all URLs from a live XML sitemap to the [Google Indexing API](https://developers.google.com/search/apis/indexing-api/v3/quickstart).

Supports standard sitemaps, sitemap index files (recursive), and gzip-compressed sitemaps. Handles retries, rate limiting, resume across interrupted runs, and detailed logging automatically.

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
| `--sitemap` | Yes | | Live URL of your sitemap or sitemap index |
| `--key` | Yes | | Path to your service account JSON key file |
| `--delay` | No | `1.0` | Seconds between API requests (must be > 0) |
| `--retries` | No | `3` | Max retries on rate-limit or server errors (must be >= 1) |
| `--backoff` | No | `5.0` | Base backoff seconds, multiplied by attempt number (must be > 0) |
| `--log-file` | No | `logs/indexing.log` | Path to write the full DEBUG log (parent dirs created automatically) |
| `--dry-run` | No | off | Parse and list URLs without submitting anything |
| `--filter` | No | | Regex; only matching URLs are submitted |
| `--resume-file` | No | | File to record and skip already-submitted URLs across runs |

### Examples

```bash
# Basic usage
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json

# Preview URLs without submitting
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json \
  --dry-run

# Submit only blog posts
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json \
  --filter '/blog/'

# Resume an interrupted run
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json \
  --resume-file submitted_urls.txt

# Write log to a subdirectory (created automatically)
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json \
  --log-file logs/run_2026.log

# Slower submission with more retries
python sitemap_indexer.py \
  --sitemap https://example.com/sitemap.xml \
  --key service_account.json \
  --delay 2.0 \
  --retries 5 \
  --backoff 10.0
```

---

## Output

The script logs to both the terminal (INFO and above) and the log file (full DEBUG).

```
2026-01-15 10:32:01 [INFO] Sitemap : https://example.com/sitemap.xml
2026-01-15 10:32:01 [INFO] Key file: service_account.json
2026-01-15 10:32:01 [INFO] Sitemap Indexer started.
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
2026-01-15 10:33:24 [INFO] Full log written to logs/indexing.log
```

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All URLs submitted successfully, or dry run completed |
| 1 | Sitemap could not be fetched, or one or more submissions failed |

This makes the script safe to use in CI/CD pipelines: a non-zero exit code signals that action is needed.

---

## Quota

The Google Indexing API allows **200 requests per day** by default per Search Console property. The script warns automatically if a run is about to meet or exceed this limit.

You can request a quota increase in Google Cloud Console if needed. The default `--delay 1.0` keeps submissions safe and predictable.

---

## Error handling

| HTTP status | Behaviour |
|---|---|
| 400 | Fails immediately. The URL is malformed or not owned by the Search Console property. |
| 403 | Fails immediately. The service account is not an Owner in Search Console. |
| 404 | Fails immediately. The URL is not publicly accessible. |
| 429 | Retries with backoff up to `--retries` times, then fails. |
| 5xx | Retries with backoff up to `--retries` times, then fails. |

Failed URLs are listed individually in the summary and log file. A single failure does not stop remaining submissions.

---

## Resume support

Use `--resume-file` to make long runs safe to interrupt. Every accepted URL is appended to the file immediately. On the next run with the same file, those URLs are skipped automatically.

```bash
# First run (interrupted at URL 80/200)
python sitemap_indexer.py --sitemap ... --key ... --resume-file submitted_urls.txt

# Second run picks up from where the first stopped
python sitemap_indexer.py --sitemap ... --key ... --resume-file submitted_urls.txt
```


---

## IndexNow Indexer

Fetches all URLs from a live XML sitemap and submits them to the **IndexNow API**, which notifies Bing, Yandex, and all other participating search engines simultaneously in a single batch request.

**Key differences from Google (`sitemap_indexer.py`)**

| | Google | Bing / IndexNow |
|---|---|---|
| Auth | Service account JSON key + OAuth | Plain API key string |
| Submission | One URL per API call (200/day limit) | Up to 10,000 URLs per request |
| Engines notified | Google only | Bing, Yandex, and all IndexNow participants |
| Removal API | Yes (`URL_DELETED`) | No. Return 404 or 410 from the page. |

### Prerequisites

- A Bing Webmaster Tools account at [webmaster.bing.com](https://www.bing.com/webmasters)
- Your site verified in Bing Webmaster Tools
- An IndexNow API key (generate one at [bing.com/indexnow/getstarted](https://www.bing.com/indexnow/getstarted))
- A key file hosted at `https://yourdomain.com/<your-key>.txt` containing only your key as plain text

The key file is how IndexNow verifies you own the domain. Without it, submissions will be rejected with HTTP 403.

### Setup

**1. Generate your API key**

Go to [bing.com/indexnow/getstarted](https://www.bing.com/indexnow/getstarted) and click Generate. Copy the key shown.

**2. Host your key file**

Create a plain text file named `<your-key>.txt`. The file must contain only your API key and nothing else.

Upload it to the root of your website so it is publicly accessible at:

```
https://yourdomain.com/<your-key>.txt
```

**3. Install dependencies**

No new dependencies required. `bing_indexer.py` uses only `requests`, which is already in `requirements.txt`.

### Usage

```bash
python bing_indexer.py --sitemap https://example.com/sitemap.xml --key YOUR_API_KEY --host example.com
```

### All options

| Flag | Required | Default | Description |
|---|---|---|---|
| `--sitemap` | Yes | | Live URL of your sitemap or sitemap index |
| `--key` | Yes | | Your IndexNow API key (plain string, not a file path) |
| `--host` | Yes | | Your domain without protocol (e.g. `example.com`) |
| `--key-location` | No | | Full URL to your key file, if not hosted at the domain root |
| `--retries` | No | `3` | Max retries on transient errors (must be >= 1) |
| `--backoff` | No | `5.0` | Base backoff seconds, multiplied by attempt number (must be > 0) |
| `--batch-delay` | No | `1.0` | Seconds between batch requests (only relevant for > 10,000 URLs) |
| `--log-file` | No | `logs/bing.log` | Path to write the full DEBUG log (parent dirs created automatically) |
| `--dry-run` | No | off | Parse and list URLs without submitting anything |
| `--filter` | No | | Regex; only matching URLs are submitted |

### Examples

```bash
# Basic usage
python bing_indexer.py \
  --sitemap https://yourdomain.com/sitemap.xml \
  --key YOUR_API_KEY \
  --host yourdomain.com

# Preview URLs without submitting
python bing_indexer.py \
  --sitemap https://yourdomain.com/sitemap.xml \
  --key YOUR_API_KEY \
  --host yourdomain.com \
  --dry-run

# Submit only blog posts
python bing_indexer.py \
  --sitemap https://yourdomain.com/sitemap.xml \
  --key YOUR_API_KEY \
  --host yourdomain.com \
  --filter '/blog/'

# Key file hosted at a non-root location
python bing_indexer.py \
  --sitemap https://yourdomain.com/sitemap.xml \
  --key YOUR_API_KEY \
  --host yourdomain.com \
  --key-location https://yourdomain.com/keys/mykey.txt
```

### Output

All URLs are sent in a single batch (or multiple batches for > 10,000 URLs). The log reflects this.

```
2026-01-15 10:32:01 [INFO] Sitemap : https://yourdomain.com/sitemap.xml
2026-01-15 10:32:01 [INFO] Host    : yourdomain.com
2026-01-15 10:32:01 [INFO] API key : YOUR_API_KEY
2026-01-15 10:32:01 [INFO] Bing Indexer started.
2026-01-15 10:32:02 [INFO] Found 44 URLs in: https://yourdomain.com/sitemap.xml
2026-01-15 10:32:02 [INFO] Submitting 44 URLs in 1 batch.
2026-01-15 10:32:02 [INFO] Batch [1/1]: submitting 44 URLs.
2026-01-15 10:32:03 [INFO] Batch [1/1]: accepted.
2026-01-15 10:32:03 [INFO] --- Summary ---
2026-01-15 10:32:03 [INFO] Total URLs  : 44
2026-01-15 10:32:03 [INFO] Submitted   : 44
2026-01-15 10:32:03 [INFO] Failed      : 0
2026-01-15 10:32:03 [INFO] Batches OK  : 1
2026-01-15 10:32:03 [INFO] Batches fail: 0
2026-01-15 10:32:03 [INFO] Full log written to logs/bing.log
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All batches submitted successfully, or dry run completed |
| 1 | Sitemap could not be fetched, or one or more batches failed |

### Error handling

| HTTP status | Behaviour |
|---|---|
| 200 / 202 | Accepted. 202 means key validation is still pending. |
| 400 | Fails immediately. URLs are malformed or incorrectly formatted. |
| 403 | Fails immediately. API key not found or key file not accessible. |
| 422 | Fails immediately. URLs do not belong to the declared host, or key schema mismatch. |
| 429 | Retries with backoff up to `--retries` times, then fails. |
| 5xx | Retries with backoff up to `--retries` times, then fails. |

### Removing URLs from Bing

IndexNow has no deletion API. To remove a URL from Bing:

- Return HTTP **410 Gone** (preferred) or **404 Not Found** from the page
- Add `Disallow: /` to `robots.txt` for domains that should never be indexed (sandbox, CDN)
- Optionally use the URL removal tool in [Bing Webmaster Tools](https://www.bing.com/webmasters) for faster manual removal

---

## deindex.py

Removes URLs from Google's index by submitting `URL_DELETED` notifications via the Google Indexing API v3.

Use this to clean up sandbox URLs accidentally indexed, deleted pages, duplicate www subdomain URLs, CDN domains, or malformed query string URLs that should not appear in search results.

### Usage

```bash
# Remove specific URLs directly
python deindex.py --key service_account.json https://sandbox.example.com/page

# Remove all URLs listed in a file
python deindex.py --key service_account.json --url-file bad_urls.txt

# Combine both
python deindex.py --key service_account.json --url-file bad_urls.txt https://extra.example.com/page

# Preview what would be removed without touching the API
python deindex.py --key service_account.json --url-file bad_urls.txt --dry-run
```

### All options

| Flag | Required | Default | Description |
|---|---|---|---|
| `URL` (positional) | No | | One or more URLs to remove, passed directly as arguments |
| `--key` | Yes | | Path to your service account JSON key file |
| `--url-file` | No | | Path to a plain text file with one URL per line (# lines are comments) |
| `--delay` | No | `1.0` | Seconds between API requests (must be > 0) |
| `--retries` | No | `3` | Max retries on transient errors (must be >= 1) |
| `--backoff` | No | `5.0` | Base backoff seconds, multiplied by attempt number (must be > 0) |
| `--log-file` | No | `logs/deindex.log` | Path to write the full DEBUG log (parent dirs created automatically) |
| `--dry-run` | No | off | List URLs that would be removed without calling the API |

### URL file format

Plain text, one URL per line. Lines starting with `#` are comments and are ignored.

```
# Sandbox URLs
https://sandbox.example.com/en
https://sandbox.example.com/about

# CDN domain - serves assets only
https://cdn.example.com

# www duplicates
https://www.example.com/contact
https://www.example.com/services
```

### Output

```
2026-01-15 10:32:01 [INFO] Key file : service_account.json
2026-01-15 10:32:01 [INFO] URLs to remove: 15
2026-01-15 10:32:01 [INFO] Authenticated with Google Indexing API.
2026-01-15 10:32:01 [INFO] [1/15] Removing: https://sandbox.example.com/en
2026-01-15 10:32:02 [INFO] Accepted for removal.
...
2026-01-15 10:32:18 [INFO] --- Summary ---
2026-01-15 10:32:18 [INFO] Total   : 15
2026-01-15 10:32:18 [INFO] Removed : 15
2026-01-15 10:32:18 [INFO] Failed  : 0
2026-01-15 10:32:18 [INFO] Full log written to logs/deindex.log
```

### How long removal takes

Removal typically takes 3 to 7 days. The URL disappears from search results once Google recrawls and confirms the page is gone. If the page still returns HTTP 200, Google may ignore the removal and reindex it.

For permanent removal, combine the API call with two server-side changes:

- Return HTTP 410 Gone (preferred) or 404 Not Found from the page
- Add `Disallow: /` to `robots.txt` on the relevant domain (for subdomains like sandbox or CDN)

A 410 is processed faster than a 404 because it signals the deletion is intentional and permanent.

### Error handling

| HTTP status | Behaviour |
|---|---|
| 400 | Fails immediately. The URL is malformed or not owned by the Search Console property. |
| 403 | Fails immediately. The service account is not an Owner in Search Console. |
| 404 | Fails immediately. The URL was not indexed or is already removed. |
| 429 | Retries with backoff up to `--retries` times, then fails. |
| 5xx | Retries with backoff up to `--retries` times, then fails. |

### Quota

`deindex.py` and `sitemap_indexer.py` share the same 200 requests/day quota. If you are running both on the same day, plan accordingly.

## Security

Never commit your service account JSON key to version control. The `.gitignore` in this repo excludes all `*.json` files for this reason. Store the key file outside the repo, or use an environment variable or secrets manager in production.

## License

[MIT](LICENSE)
