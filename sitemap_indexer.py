import argparse
import logging
import sys
import time
from xml.etree import ElementTree as ET

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("sitemap_indexer")
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
    Handles both standard sitemaps and sitemap index files recursively.
    """

    NAMESPACE = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def __init__(self, logger: logging.Logger, request_timeout: int = 30):
        self._log = logger
        self._timeout = request_timeout
        self._visited: set[str] = set()

    def extract_urls(self, sitemap_url: str) -> list[str]:
        """
        Returns a deduplicated, ordered list of all page URLs found
        in the sitemap (and any child sitemaps in a sitemap index).
        """
        self._visited.clear()
        raw = self._collect(sitemap_url)
        return list(dict.fromkeys(raw))

    def _collect(self, url: str) -> list[str]:
        if url in self._visited:
            self._log.warning("Skipping already-visited sitemap: %s", url)
            return []

        self._visited.add(url)
        self._log.info("Fetching sitemap: %s", url)

        root = self._fetch_xml(url)

        if self._is_index(root):
            return self._process_index(root)
        return self._process_sitemap(root, url)

    def _fetch_xml(self, url: str) -> ET.Element:
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
                f"HTTP {exc.response.status_code} when fetching: {url}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc
        except ET.ParseError as exc:
            raise RuntimeError(f"Invalid XML returned from: {url}: {exc}") from exc

    @staticmethod
    def _is_index(root: ET.Element) -> bool:
        return "sitemapindex" in root.tag

    def _process_index(self, root: ET.Element) -> list[str]:
        locs = root.findall("sm:sitemap/sm:loc", self.NAMESPACE)
        if not locs:
            locs = root.findall(".//loc")

        self._log.info("Sitemap index found with %d child sitemaps.", len(locs))

        urls: list[str] = []
        for loc in locs:
            child_url = (loc.text or "").strip()
            if child_url:
                urls.extend(self._collect(child_url))
        return urls

    def _process_sitemap(self, root: ET.Element, source_url: str) -> list[str]:
        locs = root.findall("sm:url/sm:loc", self.NAMESPACE)
        if not locs:
            locs = root.findall(".//loc")

        self._log.info("Found %d URLs in: %s", len(locs), source_url)

        urls: list[str] = []
        for loc in locs:
            page_url = (loc.text or "").strip()
            if page_url:
                urls.append(page_url)
        return urls


# ---------------------------------------------------------------------------
# Google Indexing API Client
# ---------------------------------------------------------------------------

class IndexingApiClient:
    """
    Wraps the Google Indexing API v3.
    Handles authentication, submission, retries, and quota throttling.
    """

    SCOPES = ["https://www.googleapis.com/auth/indexing"]

    def __init__(
        self,
        service_account_file: str,
        logger: logging.Logger,
        request_delay: float = 1.0,
        max_retries: int = 3,
        retry_backoff: float = 5.0,
    ):
        self._log = logger
        self._request_delay = request_delay
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._service = self._authenticate(service_account_file)

    def _authenticate(self, service_account_file: str):
        try:
            credentials = service_account.Credentials.from_service_account_file(
                service_account_file, scopes=self.SCOPES
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
        Submit a single URL for indexing with automatic retry on transient errors.
        Raises RuntimeError on permanent failures or exhausted retries.
        """
        body = {"url": url, "type": "URL_UPDATED"}

        for attempt in range(1, self._max_retries + 1):
            try:
                response = (
                    self._service.urlNotifications().publish(body=body).execute()
                )
                return response
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

                if status == 429 or (500 <= status <= 504):
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

        raise RuntimeError(
            f"Submission failed for {url} after {self._max_retries} retries."
        )

    def submit_all(self, urls: list[str]) -> dict:
        """
        Submit all URLs sequentially, respecting the configured inter-request delay.
        Returns a result dict with success/failure counts and detail lists.
        """
        total = len(urls)
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []

        for index, url in enumerate(urls, start=1):
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
            except RuntimeError as exc:
                self._log.error("Failed: %s", exc)
                failed.append((url, str(exc)))

            if index < total:
                time.sleep(self._request_delay)

        return {
            "total": total,
            "success_count": len(succeeded),
            "failure_count": len(failed),
            "succeeded": succeeded,
            "failed": failed,
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SitemapIndexer:
    """
    Coordinates sitemap parsing and URL submission end to end.
    """

    def __init__(
        self,
        sitemap_url: str,
        service_account_file: str,
        request_delay: float = 1.0,
        max_retries: int = 3,
        retry_backoff: float = 5.0,
        log_file: str = "indexing_log.txt",
    ):
        self._sitemap_url = sitemap_url
        self._service_account_file = service_account_file
        self._log = configure_logging(log_file)
        self._parser = SitemapParser(logger=self._log)
        self._client = IndexingApiClient(
            service_account_file=service_account_file,
            logger=self._log,
            request_delay=request_delay,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )

    def run(self) -> None:
        self._log.info("Sitemap Indexer started.")
        self._log.info("Sitemap : %s", self._sitemap_url)
        self._log.info("Key file: %s", self._service_account_file)

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
        self._print_summary(result)

    def _print_summary(self, result: dict) -> None:
        self._log.info("--- Summary ---")
        self._log.info("Total   : %d", result["total"])
        self._log.info("Success : %d", result["success_count"])
        self._log.info("Failed  : %d", result["failure_count"])

        if result["failed"]:
            self._log.info("Failed URLs:")
            for url, reason in result["failed"]:
                self._log.info("  %s  |  %s", url, reason)

        self._log.info("Full log written to indexing_log.txt")


# ---------------------------------------------------------------------------
# CLI Entry Point
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
        help="Live URL of the XML sitemap or sitemap index (e.g. https://example.com/sitemap.xml).",
    )
    parser.add_argument(
        "--key",
        required=True,
        help="Path to the Google service account JSON key file. Never commit this file to version control.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Delay between API requests in seconds. Default: 1.0. "
             "The Indexing API allows 200 requests/day by default.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="Maximum retry attempts on rate-limit or server errors. Default: 3.",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Base backoff in seconds, multiplied by attempt number on each retry. Default: 5.0.",
    )
    parser.add_argument(
        "--log-file",
        default="indexing_log.txt",
        metavar="PATH",
        help="Path to write the full log output. Default: indexing_log.txt.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    indexer = SitemapIndexer(
        sitemap_url=args.sitemap,
        service_account_file=args.key,
        request_delay=args.delay,
        max_retries=args.retries,
        retry_backoff=args.backoff,
        log_file=args.log_file,
    )
    indexer.run()


if __name__ == "__main__":
    main()
    