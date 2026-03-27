"""
JobSpy wrapper — scrapes Indeed, LinkedIn, and Glassdoor.

JobSpy is synchronous (pandas-based), so we run it in a thread executor
to avoid blocking the async event loop.

Multiple locations: if user.filters["locations"] is a list, one scrape is run
per location and results are merged. Deduplication happens downstream in filters.py.

Role variants: if role_variants is passed (AI-expanded title list), each variant
is searched separately and results are merged. Semantic dedup collapses cross-board
duplicates before any AI calls.

hours_old is derived from user.notify_freq so we never show stale duplicates:
  twice_daily → 12 h
  daily       → 24 h  (default)
  realtime    → 6 h
"""

import asyncio
import logging

from jobs.glassdoor_patch import apply_glassdoor_patch

logger = logging.getLogger(__name__)

# Apply Glassdoor curl_cffi patch once at import time.
# On Canadian IPs, glassdoor.com geo-redirects to glassdoor.ca where tls_client
# gets Cloudflare-blocked.  curl_cffi's Chrome impersonation passes the check.
apply_glassdoor_patch()

_DEFAULT_SITES = ["indeed", "linkedin", "glassdoor"]


def _hours_for_freq(notify_freq: str | None) -> int:
    """Return look-back window in hours based on the user's notify frequency."""
    return {"twice_daily": 12, "realtime": 6}.get(notify_freq or "daily", 24)


async def scrape_for_user(user, role_variants: list[str] | None = None) -> list[dict]:
    """
    Scrape jobs for a user based on their stored filters.

    role_variants: AI-expanded title list (e.g. ["Software Engineer", "SWE", "Backend Dev"]).
                   Falls back to user.filters["role"] if not provided.
    """
    f = user.filters or {}
    role      = (f.get("role") or "").strip()
    country   = (f.get("country") or "").strip()
    remote    = f.get("remote", "any")
    sites     = f.get("sites") or _DEFAULT_SITES
    hours_old = _hours_for_freq(getattr(user, "notify_freq", None))

    # Support both old single-string "location" and new list "locations"
    locations: list[str] = f.get("locations") or []
    if not locations and f.get("location"):
        locations = [(f.get("location") or "").strip()]

    search_terms = role_variants or ([role] if role else [])
    if not search_terms:
        logger.warning("[scraper] user %s has no role filter — skipping scrape", user.telegram_id)
        return []

    is_remote = remote == "remote"
    search_locations = locations or [""]  # [""] = no location filter

    logger.info(
        "[scraper] starting — user=%s  variants=%d %s  locations=%s  country=%r  "
        "remote=%s  sites=%s  hours_old=%d",
        user.telegram_id, len(search_terms), search_terms,
        locations or "any", country, remote, sites, hours_old,
    )

    # ── JobSpy (sync, runs in thread executor) ────────────────────────────────
    def _sync_scrape():
        import pandas as pd
        from jobspy import scrape_jobs

        all_dfs = []
        per_variant = 50  # fetch up to 50 per (term × location) — dedup handles overlap

        for term in search_terms:
            for loc in search_locations:
                kwargs = dict(
                    site_name=sites,
                    search_term=term,
                    is_remote=is_remote,
                    results_wanted=per_variant,
                    hours_old=hours_old,
                    fetch_description=True,
                    linkedin_fetch_description=True,
                )
                if loc:
                    kwargs["location"] = loc
                if country:
                    kwargs["country_indeed"] = country

                try:
                    df = scrape_jobs(**kwargs)
                    if df is not None and not df.empty:
                        all_dfs.append(df)
                        logger.debug("[scraper] jobspy term=%r location=%r → %d results",
                                     term, loc or "any", len(df))
                except Exception as e:
                    logger.error("[scraper] jobspy failed term=%r location=%r: %s",
                                 term, loc, e)

        if not all_dfs:
            return None
        return pd.concat(all_dfs, ignore_index=True)

    # ── Run JobSpy in thread executor ────────────────────────────────────────
    loop = asyncio.get_event_loop()
    df_result = await loop.run_in_executor(None, _sync_scrape)

    records: list[dict] = []
    if df_result is not None:
        try:
            if not df_result.empty:
                records = df_result.fillna("").to_dict("records")
        except Exception:
            pass

    logger.info(
        "[scraper] got %d raw jobs — %d term(s) × %d location(s)",
        len(records), len(search_terms), len(search_locations),
    )
    return records
