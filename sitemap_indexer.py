"""
sitemap_indexer.py

Fetches all URLs from a live XML sitemap (including sitemap index files)
and submits each one to the Google Indexing API v3.

Handles:
  - Standard sitemaps and recursive sitemap index files
  - Automatic retries with configurable backoff on transient errors
  - Per-request delay to stay within the 200 req/day default quota
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
import logging
import sys
import time
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(log_file: str) -> logging.Logger:
    """
    Create and return a logger that writes INFO to stdout and DEBUG to a file.

    Guards against duplicate handlers so the function is safe to call more
    than once within the same process (e.g. in tests).
    """
    logger = logging.getLogger("sitemap_indexer")

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)

    return logger


# ---------------------------------------------------------------------------
# Sitemap Parser
# ---------------------------------------------------------------------------

class SitemapParser:
    """
    Fetches and parses XML sitemaps from live URLs.

    Handles both standard sitemaps (<urlset>) and sitemap index files
    (<sitemapindex>) recursively. Skips already-visited URLs to prevent
    infinite loops caused by circular references in sitemap indexes.

    Args:
        logger: Logger instance to use for output.
        request_timeout: Seconds before an HTTP request times out.
    """

    SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def __init__(self, logger: logging.Logger, request_timeout: int = 30) -> None:
        self._log = logger
        self._timeout = request_timeout

    def extract_urls(self, sitemap_url: str) -> list[str]:
        """
        Return a deduplicated, ordered list of all page URLs found in the
        sitemap at `sitemap_url`, recursing into child sitemaps as needed.

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
        Fetch `url` over HTTP and return the parsed XML root element.

        Raises:
            RuntimeError: On connection errors, HTTP errors, or invalid XML.
        """
        try:
            response = requests.get(
                url,
                timeout=self._timeout,
                headers={"User-Agent": "SitemapIndexer/2.0"},
            )
            response.raise_for_status()
            return ET.fromstring(response.content)
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
        locs = root.findall("sm:sitemap/sm:loc", self.SITEMAP_NS) or root.findall(".//loc")
        self._log.info("Sitemap index found with %d child sitemaps.", len(locs))

        urls: list[str] = []
        for loc in locs:
            child_url = (loc.text or "").strip()
            if child_url:
                urls.extend(self._collect(child_url, visited))
        return urls

    def _process_sitemap(self, root: ET.Element, source_url: str) -> list[str]:
        locs = root.findall("sm:url/sm:loc", self.SITEMAP_NS) or root.findall(".//loc")
        self._log.info("Found %d URLs in: %s", len(locs), source_url)

        urls: list[str] = []
        for loc in locs:
            page_url = (loc.text or "").strip()
            if page_url:
                urls.append(page_url)
        return urls


# ---------------------------------------------------------------------------
# Submission result
# ---------------------------------------------------------------------------

class SubmissionResult:
    """
    Holds the outcome of a batch URL submission.

    Attributes:
        total: Number of URLs attempted.
        succeeded: URLs accepted by the API.
        failed: (url, error_message) pairs for each failure.
    """

    def __init__(self) -> None:
        self.total: int = 0
        self.succeeded: list[str] = []
        self.failed: list[tuple[str, str]] = []

    @property
    def success_count(self) -> int:
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        return len(self.failed)


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

    SCOPES = ["https://www.googleapis.com/auth/indexing"]

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

        Raises:
            RuntimeError: If the key file is not found or credentials are invalid.
        """
        try:
            credentials = service_account.Credentials.from_service_account_file(
                service_account_file, scopes=self.SCOPES
            )
            service = build("indexing", "v3", credentials=credentials, cache_discovery=False)
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
        Raises immediately on 400, 403, and 404 since those indicate permanent
        problems that retrying will not resolve.

        Args:
            url: The page URL to submit.

        Returns:
            The raw API response dict on success.

        Raises:
            RuntimeError: On permanent errors or after exhausting all retries.
        """
        body = {"url": url, "type": "URL_UPDATED"}

        for attempt in range(1, self._max_retries + 1):
            try:
                return self._service.urlNotifications().publish(body=body).execute()

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
                raise RuntimeError(f"Unexpected error submitting {url}: {exc}") from exc

        # Unreachable: the loop always returns or raises, but satisfies type checkers.
        raise RuntimeError(f"Submission failed for {url} after {self._max_retries} retries.")

    def submit_all(self, urls: list[str]) -> SubmissionResult:
        """
        Submit all URLs sequentially, pausing `request_delay` seconds between calls.

        Args:
            urls: Ordered list of page URLs to submit.

        Returns:
            A SubmissionResult with success and failure details.
        """
        result = SubmissionResult()
        result.total = len(urls)

        for index, url in enumerate(urls, start=1):
            self._log.info("[%d/%d] Submitting: %s", index, result.total, url)
            try:
                response = self.submit(url)
                notify_time = (
                    response
                    .get("urlNotificationMetadata", {})
                    .get("latestUpdate", {})
                    .get("notifyTime", "N/A")
                )
                self._log.info("Accepted. Notify time: %s", notify_time)
                result.succeeded.append(url)
            except RuntimeError as exc:
                self._log.error("Failed: %s", exc)
                result.failed.append((url, str(exc)))

            if index < result.total:
                time.sleep(self._request_delay)

        return result


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
        log_file: Path to write the full DEBUG log. Default is indexing_log.txt.

    Raises:
        ValueError: If `sitemap_url` does not start with http:// or https://.
    """

    def __init__(
        self,
        sitemap_url: str,
        service_account_file: str,
        request_delay: float = 1.0,
        max_retries: int = 3,
        retry_backoff: float = 5.0,
        log_file: str = "indexing_log.txt",
    ) -> None:
        self._validate_url(sitemap_url)
        self._sitemap_url = sitemap_url
        self._log_file = log_file
        self._log = configure_logging(log_file)
        self._parser = SitemapParser(logger=self._log)
        self._client = IndexingApiClient(
            service_account_file=service_account_file,
            logger=self._log,
            request_delay=request_delay,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Sitemap URL must start with http:// or https://. Got: {url!r}"
            )

    def run(self) -> None:
        """
        Parse the sitemap, submit all discovered URLs, and log a summary.

        Exits the process with code 1 if the sitemap cannot be fetched,
        or code 0 if there are no URLs to submit.
        """
        self._log.info("Sitemap Indexer started.")
        self._log.info("Sitemap : %s", self._sitemap_url)

        try:
            urls = self._parser.extract_urls(self._sitemap_url)
        except RuntimeError as exc:
            self._log.error("Sitemap parsing failed: %s", exc)
            sys.exit(1)

        if not urls:
            self._log.warning("No URLs found in sitemap. Nothing to submit.")
            sys.exit(0)

        self._log.info("Total unique URLs to submit: %d", len(urls))

        result = self._client.submit_all(urls)
        self._log_summary(result)

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
# Argument validation helpers
# ---------------------------------------------------------------------------

def _positive_float(value: str) -> float:
    """Argparse type that rejects zero and negative floats."""
    f = float(value)
    if f <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number, got {value!r}")
    return f


def _positive_int(value: str) -> int:
    """Argparse type that rejects zero and negative integers."""
    i = int(value)
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
        default="indexing_log.txt",
        metavar="PATH",
        help="Path to write the full DEBUG log. Default: indexing_log.txt.",
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
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    indexer.run()


if __name__ == "__main__":
    main()