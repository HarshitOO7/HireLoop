"""
Patches JobSpy's Glassdoor scraper to use curl_cffi instead of tls_client.

Why:  From Canadian IPs, glassdoor.com geo-redirects to glassdoor.ca, and
      Cloudflare blocks tls_client sessions with 403.  curl_cffi's Chrome
      impersonation passes the check.  The session wrapper makes curl_cffi
      look like tls_client so no other Glassdoor code changes are needed.

Also: Glassdoor's /graph API sometimes returns partial errors for SEO-only
      fields (jobsPageSeoData → 503) while job listings are fully populated.
      The original code raises ValueError on ANY "errors" key, dropping real
      results.  The patched version only errors if job data itself is absent.

Usage:
    from jobs.glassdoor_patch import apply_glassdoor_patch
    apply_glassdoor_patch()   # call once before scrape_jobs()
"""

import re
import logging

logger = logging.getLogger(__name__)
_patched = False


class _CurlCffiSessionWrapper:
    """Thin wrapper so curl_cffi sessions match the tls_client interface used by JobSpy."""

    def __init__(self, curl_session):
        self._s = curl_session

    # JobSpy accesses session.headers as a mutable dict
    @property
    def headers(self):
        return self._s.headers

    def get(self, url, **kwargs):
        kwargs.pop("timeout_seconds", None)  # not a curl_cffi kwarg
        return self._s.get(url, **kwargs)

    def post(self, url, timeout_seconds=None, **kwargs):
        if timeout_seconds is not None:
            kwargs.setdefault("timeout", timeout_seconds)
        return self._s.post(url, **kwargs)


def _make_curl_session(base_url: str) -> "_CurlCffiSessionWrapper":
    from curl_cffi.requests import Session as CurlSession
    s = CurlSession(impersonate="chrome124")
    s.headers.update({
        "accept-language": "en-CA,en;q=0.9",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return _CurlCffiSessionWrapper(s)


def _patched_get_csrf_token(self):
    """Fetch a fresh CSRF token using curl_cffi so Cloudflare is bypassed."""
    base = getattr(self, "base_url", "https://www.glassdoor.com")
    try:
        # Homepage is the most reliable page on both .com and .ca
        resp = self.session.get(base)
        tokens = re.findall(r'"token":\s*"([^"]+)"', resp.text)
        # Real CSRF tokens contain colons; Cloudflare challenge tokens are hex only
        token = next((t for t in tokens if ":" in t), None)
        if token:
            logger.debug("[glassdoor_patch] fresh CSRF token acquired")
            return token
        logger.warning("[glassdoor_patch] no CSRF token found on %s — will use fallback", base)
        return None
    except Exception as e:
        logger.warning("[glassdoor_patch] token fetch error: %s", e)
        return None


def apply_glassdoor_patch() -> bool:
    """
    Monkey-patch the Glassdoor scraper class to use curl_cffi.
    Returns True if the patch was applied (or was already applied), False on error.
    """
    global _patched
    if _patched:
        return True

    try:
        from curl_cffi.requests import Session  # noqa — ensure installed
    except ImportError:
        logger.error(
            "[glassdoor_patch] curl_cffi not installed — Glassdoor will likely fail. "
            "Run: pip install curl-cffi"
        )
        return False

    try:
        import jobspy.glassdoor as _gd_module
        from jobspy.glassdoor import Glassdoor

        # ── 1. Replace create_session in the glassdoor module namespace ───────
        # The original scrape() calls `create_session(...)` to build self.session.
        # By replacing it here (in the module where it was imported), we ensure
        # the Glassdoor class always gets a curl_cffi session without touching any
        # other JobSpy scraper.
        def _glassdoor_create_session(proxies=None, ca_cert=None, has_retry=False, **kwargs):
            base = "https://www.glassdoor.com/"  # updated to .ca in _get_csrf_token
            return _make_curl_session(base)

        _gd_module.create_session = _glassdoor_create_session

        # ── 2. Patch _get_csrf_token() to use a valid page ────────────────────
        Glassdoor._get_csrf_token = _patched_get_csrf_token

        # ── 3. Patch _fetch_jobs_page() to tolerate partial SEO errors ────────
        def _patched_fetch_jobs_page(self, scraper_input, location_id, location_type, page_num, cursor):
            from jobspy.exception import GlassdoorException
            import requests  # only for exception type check

            jobs = []
            try:
                payload = self._add_payload(location_id, location_type, page_num, cursor)
                response = self.session.post(
                    f"{self.base_url}/graph",
                    timeout_seconds=20,
                    data=payload,
                )
                if response.status_code != 200:
                    raise GlassdoorException(f"bad response status code: {response.status_code}")

                res_json = response.json()[0]

                if "errors" in res_json:
                    # Only fail if job listings are also absent (SEO errors are non-fatal)
                    job_data = (
                        res_json.get("data", {})
                        .get("jobListings", {})
                        .get("jobListings")
                    )
                    if not job_data:
                        raise ValueError("Error encountered in API response (no job data)")
                    logger.debug(
                        "[glassdoor_patch] partial API errors but %d jobs present — continuing",
                        len(job_data),
                    )

            except Exception as e:
                logger.error("Glassdoor: %s", str(e))
                return jobs, None

            from concurrent.futures import ThreadPoolExecutor, as_completed
            from jobspy.glassdoor.util import get_cursor_for_page

            jobs_data = res_json["data"]["jobListings"]["jobListings"]
            cursor_out = None
            pagination = res_json["data"]["jobListings"].get("paginationCursors", [])
            for pc in pagination:
                if pc.get("pageNumber") == page_num + 1:
                    cursor_out = pc.get("cursor")
                    break

            with ThreadPoolExecutor(max_workers=len(jobs_data) or 1) as executor:
                future_to_job = {executor.submit(self._process_job, j): j for j in jobs_data}
                for future in as_completed(future_to_job):
                    try:
                        job_post = future.result()
                        if job_post:
                            jobs.append(job_post)
                    except Exception as exc:
                        logger.debug("[glassdoor_patch] _process_job error: %s", exc)

            return jobs, cursor_out

        Glassdoor._fetch_jobs_page = _patched_fetch_jobs_page

        _patched = True
        logger.info("[glassdoor_patch] Glassdoor patched (curl_cffi + partial-error tolerance)")
        return True

    except Exception as e:
        logger.error("[glassdoor_patch] failed to apply patch: %s", e)
        return False
