"""
Removes URLs from Google's index by submitting URL_DELETED notifications
via the Google Indexing API v3.

Use this to clean up:
  - Sandbox or staging URLs accidentally indexed
  - Deleted pages returning 404 or 410
  - Duplicate URLs from www subdomains, old subdomains, or malformed query strings
  - CDN or asset domains that should never appear in search results
  - Any URL that should not appear in Google Search results

How to provide URLs:
  Pass one or more URLs directly as command-line arguments, point to a plain
  text file with one URL per line, or combine both in a single run.
  Lines in the file starting with # are treated as comments and ignored.

Handles:
  - Automatic retries with configurable backoff on transient errors
  - Per-request delay to stay within the 200 req/day default quota
  - Dry-run mode to preview removals without touching the API
  - Graceful Ctrl+C handling with a summary of what was completed
  - Dual output: console (INFO) and file (DEBUG)

Prerequisites:
  1. Google Cloud project with the Indexing API enabled
  2. Service Account with a downloaded JSON key file
  3. Service account email added as an Owner in Google Search Console

Note:
  URL_DELETED tells Google to drop the URL from its index. Removal typically
  takes 3 to 7 days. If the page still returns HTTP 200, Google may recrawl
  and reindex it. For permanent removal, ensure the page returns 404 or 410
  and add Disallow rules to robots.txt on the relevant domain.

Usage:
  # Remove specific URLs directly
  python deindex.py --key service_account.json https://old.example.com/page

  # Remove all URLs listed in a file
  python deindex.py --key service_account.json --url-file bad_urls.txt

  # Combine both
  python deindex.py --key service_account.json --url-file bad_urls.txt https://extra.example.com/page

  # Preview what would be removed without touching the API
  python deindex.py --key service_account.json --url-file bad_urls.txt --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DAILY_QUOTA = 200


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(log_file: str) -> logging.Logger:
    """
    Create and return a logger writing INFO to stdout and DEBUG to a file.

    Creates missing parent directories for the log file. Safe to call more
    than once in the same process.

    Args:
        log_file: Path to write the full DEBUG log.

    Returns:
        Configured Logger named 'deindex'.
    """
    logger = logging.getLogger("deindex")

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
# Removal result
# ---------------------------------------------------------------------------

class RemovalResult:
    """
    Record of a batch URL removal outcome.

    Attributes:
        total: Number of URLs attempted.
        succeeded: URLs successfully deindexed.
        failed: (url, error_message) pairs for each failure.
    """

    def __init__(
        self,
        total: int,
        succeeded: list[str],
        failed: list[tuple[str, str]],
    ) -> None:
        """
        Args:
            total: Number of URLs that were attempted.
            succeeded: URLs accepted for removal by the API.
            failed: (url, error_message) pairs for each rejection.
        """
        self.total = total
        self.succeeded = succeeded
        self.failed = failed

    @property
    def success_count(self) -> int:
        """Number of successfully removed URLs."""
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        """Number of URLs that could not be removed."""
        return len(self.failed)

    def __repr__(self) -> str:
        """Return a concise string representation for debugging."""
        return (
            f"RemovalResult("
            f"total={self.total}, "
            f"success={self.success_count}, "
            f"failed={self.failure_count})"
        )


# ---------------------------------------------------------------------------
# Google Indexing API client
# ---------------------------------------------------------------------------

class DeindexClient:
    """
    Submits URL_DELETED notifications to the Google Indexing API v3.

    Args:
        service_account_file: Path to the Google service account JSON key.
        logger: Logger instance to use for output.
        request_delay: Seconds between consecutive API calls.
        max_retries: Maximum retries on transient errors.
        retry_backoff: Base backoff seconds, multiplied by attempt number.

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
        """Initialise the client and authenticate with the Google Indexing API."""
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

    def remove(self, url: str) -> dict:
        """
        Submit a single URL_DELETED notification for `url`.

        Retries on HTTP 429 and 5xx. Fails immediately on 400, 403, 404.

        Args:
            url: The page URL to deindex.

        Returns:
            Raw API response dict on success.

        Raises:
            RuntimeError: On permanent errors or exhausted retries.
        """
        body = {"url": url, "type": "URL_DELETED"}

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
                        f"Bad request for {url}. The URL may be malformed or "
                        "not owned by this Search Console property."
                    ) from exc

                if status == 403:
                    raise RuntimeError(
                        f"Permission denied for {url}. Ensure the service "
                        "account email is an Owner in Google Search Console."
                    ) from exc

                if status == 404:
                    raise RuntimeError(
                        f"URL not recognised by the Indexing API: {url}. "
                        "It may not have been indexed or is already removed."
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
                    f"Unrecoverable HTTP {status} for {url}: {exc}"
                ) from exc

            except Exception as exc:
                raise RuntimeError(
                    f"Unexpected error removing {url}: {exc}"
                ) from exc

        raise RuntimeError(
            f"Removal failed for {url} after {self._max_retries} retries."
        )

    def remove_all(self, urls: list[str]) -> RemovalResult:
        """
        Submit URL_DELETED for all URLs sequentially.

        Args:
            urls: Ordered list of URLs to deindex.

        Returns:
            RemovalResult with counts and URL lists.
        """
        total = len(urls)

        if total >= _DAILY_QUOTA:
            self._log.warning(
                "This run will submit %d removals, which meets or exceeds "
                "the default daily quota of %d. Consider splitting into "
                "batches or requesting a quota increase.",
                total, _DAILY_QUOTA,
            )

        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []

        try:
            for index, url in enumerate(urls, start=1):
                self._log.info("[%d/%d] Removing: %s", index, total, url)
                try:
                    self.remove(url)
                    self._log.info("Accepted for removal.")
                    succeeded.append(url)
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

        return RemovalResult(
            total=total,
            succeeded=succeeded,
            failed=failed,
        )


# ---------------------------------------------------------------------------
# URL loading
# ---------------------------------------------------------------------------

def load_urls_from_file(path: str) -> list[str]:
    """
    Read one URL per line from a plain text file.

    Lines starting with # and blank lines are ignored.

    Args:
        path: Path to the URL list file.

    Returns:
        Ordered, deduplicated list of URLs.

    Raises:
        RuntimeError: If the file cannot be read.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        raise RuntimeError(f"URL file not found: {path}")
    except OSError as exc:
        raise RuntimeError(f"Could not read URL file {path}: {exc}") from exc

    urls = [
        line.strip()
        for line in lines
        if line.strip() and not line.startswith("#")
    ]
    return list(dict.fromkeys(urls))


def _validate_url(url: str) -> None:
    """
    Raise ValueError if `url` is not a valid http or https URL with a host.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL must start with http:// or https://. Got: {url!r}"
        )
    if not parsed.netloc:
        raise ValueError(f"URL has no host: {url!r}")


# ---------------------------------------------------------------------------
# Argument validation helpers
# ---------------------------------------------------------------------------

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
            "Remove URLs from Google's index via the Indexing API (URL_DELETED).\n\n"
            "Use this to clean up sandbox URLs, deleted pages, duplicate subdomains,\n"
            "or anything else that should not appear in Google Search results.\n\n"
            "URLs can be passed directly as arguments, loaded from a file, or both."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "urls",
        nargs="*",
        metavar="URL",
        help="One or more URLs to remove from Google's index.",
    )
    parser.add_argument(
        "--key",
        required=True,
        metavar="PATH",
        help="Path to the Google service account JSON key file.",
    )
    parser.add_argument(
        "--url-file",
        metavar="PATH",
        help=(
            "Path to a plain text file with one URL per line. "
            "Lines starting with # are treated as comments and ignored. "
            "Can be combined with inline URL arguments."
        ),
    )
    parser.add_argument(
        "--delay",
        type=_positive_float,
        default=1.0,
        metavar="SECONDS",
        help="Delay between API requests in seconds. Default: 1.0.",
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
        help="Base backoff seconds, multiplied by attempt number. Default: 5.0.",
    )
    parser.add_argument(
        "--log-file",
        default="logs/deindex.log",
        metavar="PATH",
        help="Path to write the full DEBUG log. Default: logs/deindex.log.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "List the URLs that would be removed without calling the API. "
            "Useful for reviewing the list before committing."
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Entry point: validate inputs, authenticate, and run removals."""
    args = parse_args()
    log = configure_logging(args.log_file)

    # Collect URLs from inline args and/or file
    urls: list[str] = list(args.urls)

    if args.url_file:
        try:
            file_urls = load_urls_from_file(args.url_file)
            log.info("Loaded %d URLs from file: %s", len(file_urls), args.url_file)
            urls.extend(file_urls)
        except RuntimeError as exc:
            log.error("%s", exc)
            sys.exit(1)

    # Deduplicate preserving order
    urls = list(dict.fromkeys(urls))

    if not urls:
        log.error(
            "No URLs provided. Pass URLs as arguments or use --url-file."
        )
        sys.exit(1)

    # Validate all URLs before touching the API
    invalid: list[str] = []
    for url in urls:
        try:
            _validate_url(url)
        except ValueError as exc:
            invalid.append(str(exc))

    if invalid:
        for msg in invalid:
            log.error("Invalid URL: %s", msg)
        sys.exit(1)

    log.info("Key file : %s", args.key)
    log.info("URLs to remove: %d", len(urls))

    if args.dry_run:
        log.info("Dry run: the following URLs would be removed:")
        for url in urls:
            log.info("  %s", url)
        sys.exit(0)

    try:
        client = DeindexClient(
            service_account_file=args.key,
            logger=log,
            request_delay=args.delay,
            max_retries=args.retries,
            retry_backoff=args.backoff,
        )
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)

    result = client.remove_all(urls)

    log.info("--- Summary ---")
    log.info("Total   : %d", result.total)
    log.info("Removed : %d", result.success_count)
    log.info("Failed  : %d", result.failure_count)

    if result.failed:
        log.info("Failed URLs:")
        for url, reason in result.failed:
            log.info("  %s  |  %s", url, reason)

    log.info("Full log written to %s", args.log_file)

    attempted = result.success_count + result.failure_count
    interrupted = attempted < result.total
    sys.exit(1 if (interrupted or result.failure_count) else 0)


if __name__ == "__main__":
    main()