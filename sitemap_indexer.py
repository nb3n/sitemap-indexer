"""
Fetches all URLs from a live XML sitemap (including sitemap index files)
and submits each one to the Google Indexing API v3.

Handles:
  - Standard sitemaps and recursive sitemap index files
  - Gzip-compressed sitemaps (.xml.gz)
  - Automatic retries with configurable backoff on transient errors
  - Per-request delay to stay within the 200 req/day default quota
  - Dry-run mode to preview URLs without submitting
  - URL pattern filtering to target specific sections of a site
  - Resume support to skip already-submitted URLs across interrupted runs
  - Dual output: console (INFO) and file (DEBUG)

Prerequisites:
  1. Google Cloud project with the Indexing API enabled
  2. Service Account with a downloaded JSON key file
  3. Service account email added as an Owner in Google Search Console

Usage:
  python sitemap_indexer.py --sitemap https://example.com/sitemap.xml --key service_account.json
"""

from __future__ import annotations

import argparse
import gzip
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GZIP_MAGIC = b"\x1f\x8b"
_DAILY_QUOTA = 200


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(log_file: str) -> logging.Logger:
    """
    Create and return a logger that writes INFO to stdout and DEBUG to a file.

    Creates any missing parent directories for the log file. Guards against
    duplicate handlers so the function is safe to call more than once within
    the same process (e.g. in tests).

    Args:
        log_file: File path to write the full DEBUG log.

    Returns:
        Configured Logger instance named 'sitemap_indexer'.

    Raises:
        OSError: If the log file directory cannot be created.
    """
    logger = logging.getLogger("sitemap_indexer")

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_path = Path(log_file)
    if log_path.parent != Path("."):
        log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


# ---------------------------------------------------------------------------
# Sitemap parser
# ---------------------------------------------------------------------------

class SitemapParser:
    """
    Fetches and parses XML sitemaps from live URLs.

    Handles both standard sitemaps (<urlset>) and sitemap index files
    (<sitemapindex>) recursively. Transparently decompresses gzip responses
    so plain and .xml.gz sitemaps both work without extra configuration.

    Circular references in sitemap indexes are detected and skipped to
    prevent infinite recursion.

    Args:
        logger: Logger instance to use for output.
        request_timeout: Seconds before an HTTP request times out.
    """

    _SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def __init__(self, logger: logging.Logger, request_timeout: float = 30.0) -> None:
        self._log = logger
        self._timeout = request_timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "SitemapIndexer/3.0"})

    def extract_urls(self, sitemap_url: str) -> list[str]:
        """
        Return a deduplicated, ordered list of all page URLs found in the
        sitemap at `sitemap_url`, recursing into child sitemaps as needed.

        Args:
            sitemap_url: Live URL of the sitemap or sitemap index.

        Returns:
            Deduplicated list of page URLs preserving discovery order.

        Raises:
            RuntimeError: If the sitemap cannot be fetched or parsed.
        """
        visited: set[str] = set()
        raw = self._collect(sitemap_url, visited)
        return list(dict.fromkeys(raw))

    def _collect(self, url: str, visited: set[str]) -> list[str]:
        if url in visited:
            self._log.warning("Skipping already-visited sitemap: %s", url)
            return []

        visited.add(url)
        self._log.info("Fetching sitemap: %s", url)
        root = self._fetch_xml(url)

        if "sitemapindex" in root.tag:
            return self._process_index(root, visited)

        return self._process_sitemap(root, url)

    def _fetch_xml(self, url: str) -> ET.Element:
        """
        Fetch `url` and return the parsed XML root element.

        Transparently decompresses gzip responses. Raises RuntimeError for
        all network, HTTP, and XML parsing failures.

        Args:
            url: URL to fetch.

        Returns:
            Root element of the parsed XML document.

        Raises:
            RuntimeError: On connection errors, HTTP errors, or invalid XML.
        """
        try:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()
            content = _decompress_if_gzip(response.content, url)
            return ET.fromstring(content)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"Request timed out fetching: {url}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(f"Could not connect to: {url}") from exc
        except requests.exceptions.HTTPError as exc:
            raise RuntimeError(
                f"HTTP {exc.response.status_code} fetching: {url}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc
        except ET.ParseError as exc:
            raise RuntimeError(f"Invalid XML returned from {url}: {exc}") from exc

    def _process_index(self, root: ET.Element, visited: set[str]) -> list[str]:
        locs = (
            root.findall("sm:sitemap/sm:loc", self._SITEMAP_NS)
            or root.findall(".//loc")
        )
        self._log.info("Sitemap index found with %d child sitemaps.", len(locs))

        urls: list[str] = []
        for loc in locs:
            child_url = (loc.text or "").strip()
            if child_url:
                urls.extend(self._collect(child_url, visited))
        return urls

    def _process_sitemap(self, root: ET.Element, source_url: str) -> list[str]:
        locs = (
            root.findall("sm:url/sm:loc", self._SITEMAP_NS)
            or root.findall(".//loc")
        )
        self._log.info("Found %d URLs in: %s", len(locs), source_url)

        return [
            page_url
            for loc in locs
            if (page_url := (loc.text or "").strip())
        ]


def _decompress_if_gzip(content: bytes, url: str) -> bytes:
    """
    Return decompressed bytes if `content` is gzip-encoded, or return it
    unchanged if it is plain XML.

    Args:
        content: Raw response bytes.
        url: Source URL, used only in the error message on failed decompression.

    Returns:
        Decompressed or original bytes.

    Raises:
        RuntimeError: If content looks like gzip but cannot be decompressed.
    """
    if not content.startswith(_GZIP_MAGIC):
        return content
    try:
        return gzip.decompress(content)
    except OSError as exc:
        raise RuntimeError(
            f"Failed to decompress gzip content from {url}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Submission result
# ---------------------------------------------------------------------------

class SubmissionResult:
    """
    Immutable-by-convention record of a batch URL submission outcome.

    Attributes:
        total: Number of URLs in the original list (including any skipped by resume).
        succeeded: URLs accepted by the API, in submission order.
        failed: (url, error_message) pairs for each rejection, in order.
    """

    def __init__(
        self,
        total: int,
        succeeded: list[str],
        failed: list[tuple[str, str]],
    ) -> None:
        self.total = total
        self.succeeded = succeeded
        self.failed = failed

    @property
    def success_count(self) -> int:
        """Number of successfully submitted URLs."""
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        """Number of URLs that could not be submitted."""
        return len(self.failed)

    def __repr__(self) -> str:
        return (
            f"SubmissionResult("
            f"total={self.total}, "
            f"success={self.success_count}, "
            f"failed={self.failure_count})"
        )


# ---------------------------------------------------------------------------
# Google Indexing API client
# ---------------------------------------------------------------------------

class IndexingApiClient:
    """
    Wraps the Google Indexing API v3.

    Handles OAuth2 authentication via a service account key file,
    URL submission, and retry logic for transient errors.

    Args:
        service_account_file: Path to the Google service account JSON key.
        logger: Logger instance to use for output.
        request_delay: Seconds to wait between consecutive API calls.
        max_retries: Maximum retry attempts on rate-limit or server errors.
        retry_backoff: Base backoff in seconds, multiplied by attempt number.

    Raises:
        RuntimeError: If the key file is missing or authentication fails.
    """

    _SCOPES = ["https://www.googleapis.com/auth/indexing"]

    def __init__(
        self,
        service_account_file: str,
        logger: logging.Logger,
        request_delay: float = 1.0,
        max_retries: int = 3,
        retry_backoff: float = 5.0,
    ) -> None:
        self._log = logger
        self._request_delay = request_delay
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._service = self._authenticate(service_account_file)

    def _authenticate(self, service_account_file: str):
        """
        Build and return an authenticated Google API service object.

        Args:
            service_account_file: Path to the service account JSON key.

        Raises:
            RuntimeError: If the key file is not found or credentials are invalid.
        """
        try:
            credentials = service_account.Credentials.from_service_account_file(
                service_account_file, scopes=self._SCOPES
            )
            service = build(
                "indexing", "v3", credentials=credentials, cache_discovery=False
            )
            self._log.info("Authenticated with Google Indexing API.")
            return service
        except FileNotFoundError:
            raise RuntimeError(
                f"Service account key file not found: {service_account_file}\n"
                "Download it from Google Cloud Console > IAM > Service Accounts."
            )
        except Exception as exc:
            raise RuntimeError(f"Authentication failed: {exc}") from exc

    def submit(self, url: str) -> dict:
        """
        Submit a single URL for indexing notification.

        Retries automatically on HTTP 429 (rate limit) and 5xx (server) errors.
        Fails immediately on 400, 403, and 404 because retrying will not help.

        Args:
            url: The page URL to submit.

        Returns:
            Raw API response dict on success.

        Raises:
            RuntimeError: On permanent errors or after exhausting all retries.
        """
        body = {"url": url, "type": "URL_UPDATED"}

        for attempt in range(1, self._max_retries + 1):
            try:
                return (
                    self._service.urlNotifications()
                    .publish(body=body)
                    .execute(num_retries=0)
                )

            except HttpError as exc:
                status = exc.resp.status

                if status == 400:
                    raise RuntimeError(
                        f"Bad request for {url}. The URL may be malformed or not "
                        "owned by this Search Console property."
                    ) from exc

                if status == 403:
                    raise RuntimeError(
                        f"Permission denied for {url}. Ensure the service account "
                        "email is added as an Owner in Google Search Console."
                    ) from exc

                if status == 404:
                    raise RuntimeError(
                        f"URL not found by the Indexing API: {url}. "
                        "The URL must be publicly accessible."
                    ) from exc

                if status == 429 or 500 <= status <= 504:
                    if attempt == self._max_retries:
                        raise RuntimeError(
                            f"HTTP {status} for {url}. "
                            f"Gave up after {self._max_retries} retries."
                        ) from exc
                    wait = self._retry_backoff * attempt
                    self._log.warning(
                        "HTTP %d for %s. Retry %d/%d in %.0fs.",
                        status, url, attempt, self._max_retries, wait,
                    )
                    time.sleep(wait)
                    continue

                raise RuntimeError(
                    f"Unrecoverable HTTP {status} submitting {url}: {exc}"
                ) from exc

            except Exception as exc:
                raise RuntimeError(
                    f"Unexpected error submitting {url}: {exc}"
                ) from exc

        # Unreachable: every code path above returns or raises.
        raise RuntimeError(
            f"Submission failed for {url} after {self._max_retries} retries."
        )

    def submit_all(
        self,
        urls: list[str],
        resume_set: set[str] | None = None,
        resume_file: str | None = None,
    ) -> SubmissionResult:
        """
        Submit all URLs sequentially, pausing `request_delay` seconds between calls.

        URLs present in `resume_set` are skipped and counted as already succeeded
        so the summary reflects the full picture across runs. Each newly accepted
        URL is appended to `resume_file` immediately so a crash mid-run can be
        resumed without resubmitting completed URLs.

        Emits a quota warning if the number of new submissions in this run meets
        or exceeds the default daily limit of 200 requests.

        Args:
            urls: Ordered list of page URLs to submit.
            resume_set: URLs already submitted in a previous run; skipped here.
            resume_file: Path to append each newly accepted URL to.

        Returns:
            SubmissionResult with counts and URL lists for this run.
        """
        resume_set = resume_set or set()
        skipped = [u for u in urls if u in resume_set]
        pending = [u for u in urls if u not in resume_set]

        if skipped:
            self._log.info(
                "Skipping %d already-submitted URLs (resume).", len(skipped)
            )

        if len(pending) >= _DAILY_QUOTA:
            self._log.warning(
                "This run will submit %d URLs, which meets or exceeds the default "
                "daily quota of %d. Consider splitting into batches or requesting "
                "a quota increase in Google Cloud Console.",
                len(pending),
                _DAILY_QUOTA,
            )

        succeeded: list[str] = list(skipped)
        failed: list[tuple[str, str]] = []
        total = len(pending)

        try:
            for index, url in enumerate(pending, start=1):
                self._log.info("[%d/%d] Submitting: %s", index, total, url)
                try:
                    response = self.submit(url)
                    notify_time = (
                        response
                        .get("urlNotificationMetadata", {})
                        .get("latestUpdate", {})
                        .get("notifyTime", "N/A")
                    )
                    self._log.info("Accepted. Notify time: %s", notify_time)
                    succeeded.append(url)
                    if resume_file:
                        with open(resume_file, "a", encoding="utf-8") as fh:
                            fh.write(url + "\n")
                except RuntimeError as exc:
                    self._log.error("Failed: %s", exc)
                    failed.append((url, str(exc)))

                if index < total:
                    time.sleep(self._request_delay)

        except KeyboardInterrupt:
            remaining = total - index
            self._log.warning(
                "Interrupted after %d/%d URLs. %d not attempted.",
                index, total, remaining,
            )
            if resume_file:
                self._log.info(
                    "Resume file '%s' is up to date. Re-run with the same "
                    "--resume-file flag to continue from where this stopped.",
                    resume_file,
                )
            else:
                self._log.info(
                    "Tip: use --resume-file to avoid resubmitting completed "
                    "URLs on the next run."
                )

        return SubmissionResult(
            total=len(urls),
            succeeded=succeeded,
            failed=failed,
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SitemapIndexer:
    """
    Coordinates sitemap parsing and URL submission end to end.

    Args:
        sitemap_url: Live URL of the XML sitemap or sitemap index to process.
        service_account_file: Path to the Google service account JSON key.
        request_delay: Seconds between API requests. Default is 1.0.
        max_retries: Maximum retries on transient failures. Default is 3.
        retry_backoff: Base backoff seconds per retry attempt. Default is 5.0.
        log_file: Path to write the full DEBUG log. Default is logs/indexing.log.
        dry_run: If True, parse the sitemap and log URLs without submitting.
        url_pattern: Optional regex; only matching URLs are submitted.
        resume_file: Path to a file of previously submitted URLs to skip.

    Raises:
        ValueError: If `sitemap_url` is not a valid http or https URL.
    """

    def __init__(
        self,
        sitemap_url: str,
        service_account_file: str,
        request_delay: float = 1.0,
        max_retries: int = 3,
        retry_backoff: float = 5.0,
        log_file: str = "logs/indexing.log",
        dry_run: bool = False,
        url_pattern: str | None = None,
        resume_file: str | None = None,
    ) -> None:
        _validate_sitemap_url(sitemap_url)

        self._sitemap_url = sitemap_url
        self._log_file = log_file
        self._dry_run = dry_run
        self._url_pattern = re.compile(url_pattern) if url_pattern else None
        self._resume_file = resume_file

        self._log = configure_logging(log_file)
        self._log.info("Sitemap : %s", self._sitemap_url)
        self._log.info("Key file: %s", service_account_file)

        self._parser = SitemapParser(logger=self._log)
        self._client: IndexingApiClient | None = None

        if not dry_run:
            self._client = IndexingApiClient(
                service_account_file=service_account_file,
                logger=self._log,
                request_delay=request_delay,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )

    def run(self) -> int:
        """
        Parse the sitemap, submit all discovered URLs, and log a summary.

        Returns:
            0 on full success or dry run.
            1 if the sitemap could not be fetched or any submissions failed.
        """
        self._log.info(
            "Sitemap Indexer started.%s", " [DRY RUN]" if self._dry_run else ""
        )

        try:
            urls = self._parser.extract_urls(self._sitemap_url)
        except RuntimeError as exc:
            self._log.error("Sitemap parsing failed: %s", exc)
            return 1

        if not urls:
            self._log.warning("No URLs found in sitemap. Nothing to submit.")
            return 0

        if self._url_pattern is not None:
            before = len(urls)
            urls = [u for u in urls if self._url_pattern.search(u)]
            self._log.info(
                "Pattern '%s' matched %d of %d URLs.",
                self._url_pattern.pattern, len(urls), before,
            )

        if not urls:
            self._log.warning(
                "No URLs matched the filter pattern. Nothing to submit."
            )
            return 0

        if self._dry_run:
            self._log.info("Dry run: %d URLs would be submitted:", len(urls))
            for url in urls:
                self._log.info("  %s", url)
            return 0

        resume_set = self._load_resume_set()
        self._log.info("Total unique URLs to submit: %d", len(urls))

        result = self._client.submit_all(
            urls,
            resume_set=resume_set,
            resume_file=self._resume_file,
        )
        self._log_summary(result)

        attempted = result.success_count + result.failure_count
        interrupted = attempted < result.total
        return 1 if (interrupted or result.failure_count) else 0

    def _load_resume_set(self) -> set[str]:
        """
        Return the set of URLs recorded in the resume file, or an empty set
        if no resume file was specified or the file does not yet exist.
        """
        if not self._resume_file:
            return set()
        try:
            with open(self._resume_file, encoding="utf-8") as fh:
                urls = {line.strip() for line in fh if line.strip()}
            self._log.info(
                "Loaded %d URLs from resume file: %s",
                len(urls), self._resume_file,
            )
            return urls
        except FileNotFoundError:
            return set()

    def _log_summary(self, result: SubmissionResult) -> None:
        self._log.info("--- Summary ---")
        self._log.info("Total   : %d", result.total)
        self._log.info("Success : %d", result.success_count)
        self._log.info("Failed  : %d", result.failure_count)

        if result.failed:
            self._log.info("Failed URLs:")
            for url, reason in result.failed:
                self._log.info("  %s  |  %s", url, reason)

        self._log.info("Full log written to %s", self._log_file)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_sitemap_url(url: str) -> None:
    """
    Raise ValueError if `url` is not a valid http or https URL with a host.

    Args:
        url: The URL string to validate.

    Raises:
        ValueError: If the scheme is not http/https or the host is missing.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Sitemap URL must start with http:// or https://. Got: {url!r}"
        )
    if not parsed.netloc:
        raise ValueError(f"Sitemap URL has no host: {url!r}")


def _positive_float(value: str) -> float:
    """
    Argparse type converter that accepts only positive (> 0) floats.

    Raises:
        argparse.ArgumentTypeError: If the value is not a valid positive float.
    """
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}")
    if f <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value!r}")
    return f


def _positive_int(value: str) -> int:
    """
    Argparse type converter that accepts only integers >= 1.

    Raises:
        argparse.ArgumentTypeError: If the value is not a valid integer >= 1.
    """
    try:
        i = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if i < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value!r}")
    return i


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Submit all URLs from a live XML sitemap to the Google Indexing API.\n\n"
            "Before running:\n"
            "  1. Enable the Indexing API in Google Cloud Console.\n"
            "  2. Create a Service Account and download the JSON key.\n"
            "  3. Add the service account email as an Owner in Google Search Console."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sitemap",
        required=True,
        metavar="URL",
        help=(
            "Live URL of the XML sitemap or sitemap index. "
            "Example: https://example.com/sitemap.xml"
        ),
    )
    parser.add_argument(
        "--key",
        required=True,
        metavar="PATH",
        help=(
            "Path to the Google service account JSON key file. "
            "Never commit this file to version control."
        ),
    )
    parser.add_argument(
        "--delay",
        type=_positive_float,
        default=1.0,
        metavar="SECONDS",
        help=(
            "Delay between API requests in seconds. Default: 1.0. "
            "The Indexing API allows 200 requests/day by default."
        ),
    )
    parser.add_argument(
        "--retries",
        type=_positive_int,
        default=3,
        metavar="N",
        help="Maximum retry attempts on rate-limit or server errors. Default: 3.",
    )
    parser.add_argument(
        "--backoff",
        type=_positive_float,
        default=5.0,
        metavar="SECONDS",
        help=(
            "Base backoff in seconds, multiplied by the attempt number on each retry. "
            "Default: 5.0."
        ),
    )
    parser.add_argument(
        "--log-file",
        default="logs/indexing.log",
        metavar="PATH",
        help="Path to write the full DEBUG log. Default: logs/indexing.log.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse the sitemap and log all discovered URLs without submitting "
            "anything to the Indexing API. Useful for verifying sitemap content "
            "and filter patterns before a live run."
        ),
    )
    parser.add_argument(
        "--filter",
        dest="url_pattern",
        metavar="REGEX",
        help=(
            "Only submit URLs matching this regular expression. "
            "Example: --filter '/blog/' submits only URLs that contain /blog/."
        ),
    )
    parser.add_argument(
        "--resume-file",
        metavar="PATH",
        help=(
            "Path to a plain-text file used for resume support. "
            "Successfully submitted URLs are appended here after each acceptance. "
            "On the next run, URLs already in this file are skipped automatically. "
            "Useful for resuming large or interrupted runs without resubmitting."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        indexer = SitemapIndexer(
            sitemap_url=args.sitemap,
            service_account_file=args.key,
            request_delay=args.delay,
            max_retries=args.retries,
            retry_backoff=args.backoff,
            log_file=args.log_file,
            dry_run=args.dry_run,
            url_pattern=args.url_pattern,
            resume_file=args.resume_file,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    sys.exit(indexer.run())


if __name__ == "__main__":
    main()