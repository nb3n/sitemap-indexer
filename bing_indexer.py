"""
Fetches all URLs from a live XML sitemap and submits them to the IndexNow
API, which notifies Bing, Yandex, and all other participating search engines
simultaneously in a single request.

Handles:
  - Standard sitemaps and recursive sitemap index files
  - Gzip-compressed sitemaps (.xml.gz)
  - Automatic batching (up to 10,000 URLs per request)
  - Automatic retries with configurable backoff on transient errors
  - Dry-run mode to preview URLs without submitting
  - URL pattern filtering to target specific sections of a site
  - Dual output: console (INFO) and file (DEBUG)

How IndexNow differs from Google Indexing API:
  - No service account or OAuth required. Authentication is a plain API key.
  - One POST request submits up to 10,000 URLs. No per-URL rate limiting.
  - Submitting to api.indexnow.org notifies ALL participating engines at once
    (Bing, Yandex, and others). You do not need to run separate scripts.
  - There is no removal API. Deleted pages must return 404 or 410 for engines
    to drop them on their next crawl.

Prerequisites:
  1. A Bing Webmaster Tools account at https://www.bing.com/webmasters
  2. Your site verified in Bing Webmaster Tools
  3. An IndexNow API key (generate one at https://www.bing.com/indexnow/getstarted)
  4. A key file hosted at https://yourdomain.com/<your-key>.txt containing
     only your key as the file content

Usage:
  python bing_indexer.py --sitemap https://example.com/sitemap.xml --key YOUR_API_KEY --host example.com
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GZIP_MAGIC = b"\x1f\x8b"
_INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
_MAX_BATCH_SIZE = 10_000
_REQUEST_TIMEOUT = 30.0


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
        Configured Logger instance named 'bing_indexer'.

    Raises:
        OSError: If the log file directory cannot be created.
    """
    logger = logging.getLogger("bing_indexer")

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
# Sitemap parser (shared logic, identical to sitemap_indexer.py)
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
        """Initialise the parser and create a persistent HTTP session."""
        self._log = logger
        self._timeout = request_timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "BingIndexer/1.0"})

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
        """Recursively collect URLs from `url`, tracking visited sitemaps."""
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
        """Extract and recurse into child sitemaps listed in a sitemap index."""
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
        """Extract page URLs from a standard sitemap."""
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
    Record of a batch URL submission outcome.

    Attributes:
        total: Total number of URLs discovered and eligible for submission.
        batches_sent: Number of batch requests sent to the IndexNow API.
        batches_failed: Number of batch requests that could not be submitted.
        failed_batches: List of (batch_urls, error_message) for each failure.
    """

    def __init__(self) -> None:
        """Initialise an empty result record."""
        self.total: int = 0
        self.batches_sent: int = 0
        self.batches_failed: int = 0
        self.failed_batches: list[tuple[list[str], str]] = []

    @property
    def urls_submitted(self) -> int:
        """Number of URLs included in successful batch submissions."""
        failed_url_count = sum(len(b) for b, _ in self.failed_batches)
        return self.total - failed_url_count

    @property
    def urls_failed(self) -> int:
        """Number of URLs included in failed batch submissions."""
        return sum(len(b) for b, _ in self.failed_batches)

    def __repr__(self) -> str:
        """Return a concise string representation for debugging."""
        return (
            f"SubmissionResult("
            f"total={self.total}, "
            f"batches_sent={self.batches_sent}, "
            f"batches_failed={self.batches_failed})"
        )


# ---------------------------------------------------------------------------
# IndexNow API client
# ---------------------------------------------------------------------------

class IndexNowClient:
    """
    Submits URLs to the IndexNow API in batches of up to 10,000.

    A single submission notifies all participating search engines simultaneously
    (Bing, Yandex, and others). No per-URL rate limiting applies.

    Args:
        api_key: Your IndexNow API key.
        host: The domain of your site (e.g. 'example.com'), without protocol.
        logger: Logger instance to use for output.
        key_location: Optional full URL to your hosted key file. Required only
            if the key file is not at the root of your domain.
        max_retries: Maximum retry attempts on transient errors.
        retry_backoff: Base backoff seconds, multiplied by attempt number.
        batch_delay: Seconds to wait between batch requests when sending
            more than one batch. Default is 1.0.
    """

    def __init__(
        self,
        api_key: str,
        host: str,
        logger: logging.Logger,
        key_location: str | None = None,
        max_retries: int = 3,
        retry_backoff: float = 5.0,
        batch_delay: float = 1.0,
    ) -> None:
        """Initialise the client with credentials and a persistent HTTP session."""
        self._api_key = api_key
        self._host = host
        self._key_location = key_location
        self._log = logger
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._batch_delay = batch_delay
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "BingIndexer/1.0",
        })

    def submit_batch(self, urls: list[str]) -> None:
        """
        Submit a single batch of URLs to the IndexNow API.

        The batch must contain no more than 10,000 URLs and all URLs must
        belong to the same host configured on this client instance.

        Retries automatically on HTTP 429 and 5xx responses. Fails
        immediately on 400, 403, and 422.

        Args:
            urls: List of page URLs to submit. Maximum 10,000 per call.

        Raises:
            RuntimeError: On permanent API errors or exhausted retries.
            ValueError: If `urls` is empty or exceeds the maximum batch size.
        """
        if not urls:
            raise ValueError("Batch must contain at least one URL.")
        if len(urls) > _MAX_BATCH_SIZE:
            raise ValueError(
                f"Batch size {len(urls)} exceeds the maximum of {_MAX_BATCH_SIZE}."
            )

        payload: dict = {
            "host": self._host,
            "key": self._api_key,
            "urlList": urls,
        }
        if self._key_location:
            payload["keyLocation"] = self._key_location

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._session.post(
                    _INDEXNOW_ENDPOINT,
                    json=payload,
                    timeout=_REQUEST_TIMEOUT,
                )

                if response.status_code in (200, 202):
                    return

                if response.status_code == 400:
                    raise RuntimeError(
                        f"Bad request (HTTP 400). Check that all URLs are "
                        f"properly formatted and belong to '{self._host}'."
                    )

                if response.status_code == 403:
                    raise RuntimeError(
                        f"Forbidden (HTTP 403). The API key '{self._api_key}' "
                        f"was not found or is invalid. Ensure your key file is "
                        f"hosted at https://{self._host}/{self._api_key}.txt "
                        f"and contains only the key."
                    )

                if response.status_code == 422:
                    raise RuntimeError(
                        f"Unprocessable (HTTP 422). One or more URLs do not "
                        f"belong to '{self._host}', or the key does not match "
                        f"the IndexNow schema."
                    )

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self._max_retries:
                        raise RuntimeError(
                            f"HTTP {response.status_code} from IndexNow API. "
                            f"Gave up after {self._max_retries} retries."
                        )
                    wait = self._retry_backoff * attempt
                    self._log.warning(
                        "HTTP %d from IndexNow API. Retry %d/%d in %.0fs.",
                        response.status_code, attempt, self._max_retries, wait,
                    )
                    time.sleep(wait)
                    continue

                raise RuntimeError(
                    f"Unexpected HTTP {response.status_code} from IndexNow API."
                )

            except requests.exceptions.Timeout as exc:
                if attempt == self._max_retries:
                    raise RuntimeError(
                        "Request to IndexNow API timed out after "
                        f"{self._max_retries} attempts."
                    ) from exc
                wait = self._retry_backoff * attempt
                self._log.warning(
                    "Request timed out. Retry %d/%d in %.0fs.",
                    attempt, self._max_retries, wait,
                )
                time.sleep(wait)

            except requests.exceptions.RequestException as exc:
                raise RuntimeError(
                    f"Network error contacting IndexNow API: {exc}"
                ) from exc

        raise RuntimeError(
            f"Submission failed after {self._max_retries} retries."
        )

    def submit_all(self, urls: list[str]) -> SubmissionResult:
        """
        Submit all URLs in batches of up to 10,000.

        Each batch is sent as a single POST request. If the URL list is
        10,000 or fewer, only one request is made. Larger lists are split
        automatically and a delay of `batch_delay` seconds is applied between
        consecutive batch requests.

        Args:
            urls: Full list of page URLs to submit.

        Returns:
            SubmissionResult with batch counts and any failure details.
        """
        result = SubmissionResult()
        result.total = len(urls)

        batches = [
            urls[i: i + _MAX_BATCH_SIZE]
            for i in range(0, len(urls), _MAX_BATCH_SIZE)
        ]
        total_batches = len(batches)

        self._log.info(
            "Submitting %d URLs in %d batch%s.",
            len(urls), total_batches, "es" if total_batches != 1 else "",
        )

        try:
            for index, batch in enumerate(batches, start=1):
                self._log.info(
                    "Batch [%d/%d]: submitting %d URLs.",
                    index, total_batches, len(batch),
                )
                try:
                    self.submit_batch(batch)
                    self._log.info("Batch [%d/%d]: accepted.", index, total_batches)
                    result.batches_sent += 1
                except RuntimeError as exc:
                    self._log.error(
                        "Batch [%d/%d] failed: %s", index, total_batches, exc
                    )
                    result.batches_failed += 1
                    result.failed_batches.append((batch, str(exc)))

                if index < total_batches:
                    time.sleep(self._batch_delay)

        except KeyboardInterrupt:
            self._log.warning(
                "Interrupted after %d/%d batches.", index, total_batches
            )

        return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class BingIndexer:
    """
    Coordinates sitemap parsing and IndexNow URL submission end to end.

    Args:
        sitemap_url: Live URL of the XML sitemap or sitemap index to process.
        api_key: Your IndexNow API key.
        host: Your site's domain without protocol (e.g. 'example.com').
        key_location: Optional URL to your hosted key file if not at root.
        max_retries: Maximum retries on transient failures. Default is 3.
        retry_backoff: Base backoff seconds per retry attempt. Default is 5.0.
        batch_delay: Seconds between batch requests. Default is 1.0.
        log_file: Path to write the full DEBUG log. Default is logs/bing.log.
        dry_run: If True, parse the sitemap and log URLs without submitting.
        url_pattern: Optional regex; only matching URLs are submitted.

    Raises:
        ValueError: If `sitemap_url` is not a valid http or https URL,
                    or if `host` is empty.
    """

    def __init__(
        self,
        sitemap_url: str,
        api_key: str,
        host: str,
        key_location: str | None = None,
        max_retries: int = 3,
        retry_backoff: float = 5.0,
        batch_delay: float = 1.0,
        log_file: str = "logs/bing.log",
        dry_run: bool = False,
        url_pattern: str | None = None,
    ) -> None:
        """Initialise, validate inputs, configure logging, and authenticate."""
        _validate_sitemap_url(sitemap_url)
        if not host.strip():
            raise ValueError("--host must not be empty.")

        self._sitemap_url = sitemap_url
        self._log_file = log_file
        self._dry_run = dry_run
        self._url_pattern = re.compile(url_pattern) if url_pattern else None

        self._log = configure_logging(log_file)
        self._log.info("Sitemap : %s", self._sitemap_url)
        self._log.info("Host    : %s", host)
        self._log.info("API key : %s", api_key)

        self._parser = SitemapParser(logger=self._log)
        self._client = IndexNowClient(
            api_key=api_key,
            host=host,
            logger=self._log,
            key_location=key_location,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            batch_delay=batch_delay,
        )

    def run(self) -> int:
        """
        Parse the sitemap, submit all discovered URLs, and log a summary.

        Returns:
            0 on full success or dry run.
            1 if the sitemap could not be fetched or any batch failed.
        """
        self._log.info(
            "Bing Indexer started.%s", " [DRY RUN]" if self._dry_run else ""
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

        result = self._client.submit_all(urls)
        self._log_summary(result)

        return 1 if result.batches_failed else 0

    def _log_summary(self, result: SubmissionResult) -> None:
        """Log a human-readable summary of the submission run."""
        self._log.info("--- Summary ---")
        self._log.info("Total URLs  : %d", result.total)
        self._log.info("Submitted   : %d", result.urls_submitted)
        self._log.info("Failed      : %d", result.urls_failed)
        self._log.info("Batches OK  : %d", result.batches_sent)
        self._log.info("Batches fail: %d", result.batches_failed)

        if result.failed_batches:
            self._log.info("Failed batches:")
            for batch_urls, reason in result.failed_batches:
                self._log.info(
                    "  %d URLs  |  %s", len(batch_urls), reason
                )
                for url in batch_urls[:5]:
                    self._log.info("    %s", url)
                if len(batch_urls) > 5:
                    self._log.info("    ... and %d more", len(batch_urls) - 5)

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
    """Parse and return command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Submit all URLs from a live XML sitemap to the IndexNow API.\n\n"
            "IndexNow notifies Bing, Yandex, and all other participating search\n"
            "engines simultaneously in a single batch request.\n\n"
            "Before running:\n"
            "  1. Create an account at https://www.bing.com/webmasters\n"
            "  2. Verify your site in Bing Webmaster Tools\n"
            "  3. Generate an API key at https://www.bing.com/indexnow/getstarted\n"
            "  4. Host your key file at https://yourdomain.com/<your-key>.txt\n"
            "     The file must contain only your API key as plain text."
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
        metavar="KEY",
        help=(
            "Your IndexNow API key. This is a plain string, not a file path. "
            "Generate one at https://www.bing.com/indexnow/getstarted"
        ),
    )
    parser.add_argument(
        "--host",
        required=True,
        metavar="DOMAIN",
        help=(
            "Your site's domain without protocol or trailing slash. "
            "Example: example.com or www.example.com"
        ),
    )
    parser.add_argument(
        "--key-location",
        metavar="URL",
        help=(
            "Full URL to your hosted key file, required only if the file is "
            "not at the root of your domain. "
            "Example: https://example.com/keys/mykey.txt. "
            "If omitted, the key file is assumed to be at "
            "https://<host>/<key>.txt"
        ),
    )
    parser.add_argument(
        "--retries",
        type=_positive_int,
        default=3,
        metavar="N",
        help="Maximum retry attempts on transient errors. Default: 3.",
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
        "--batch-delay",
        type=_positive_float,
        default=1.0,
        metavar="SECONDS",
        help=(
            "Seconds to wait between batch requests when sending more than one batch. "
            "Only relevant for sitemaps with more than 10,000 URLs. Default: 1.0."
        ),
    )
    parser.add_argument(
        "--log-file",
        default="logs/bing.log",
        metavar="PATH",
        help="Path to write the full DEBUG log. Default: logs/bing.log.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse the sitemap and log all discovered URLs without submitting "
            "anything to the IndexNow API. Useful for verifying sitemap content "
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
    return parser.parse_args()


def main() -> None:
    """Entry point: validate inputs and run the Bing indexer."""
    args = parse_args()
    try:
        indexer = BingIndexer(
            sitemap_url=args.sitemap,
            api_key=args.key,
            host=args.host,
            key_location=args.key_location,
            max_retries=args.retries,
            retry_backoff=args.backoff,
            batch_delay=args.batch_delay,
            log_file=args.log_file,
            dry_run=args.dry_run,
            url_pattern=args.url_pattern,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    sys.exit(indexer.run())


if __name__ == "__main__":
    main()
    