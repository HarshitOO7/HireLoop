"""
JobSpy wrapper — scrapes Indeed, LinkedIn, and Glassdoor.

JobSpy is synchronous (pandas-based), so each (term × location) combo runs in its
own thread executor slot.  All combos are gathered in parallel so 8 variants finish
in ~the time of 1 instead of sequentially.

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

    # ── One sync function per (term × location) combo ────────────────────────
    per_variant = 50  # fetch up to 50 per combo — dedup handles overlap

    def _scrape_one(term: str, loc: str):
        import pandas as pd
        from jobspy import scrape_jobs

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
        elif country:
            # No specific location set — use country as location so Glassdoor
            # resolves the correct country-level location ID instead of
            # defaulting to US nationwide (hardcoded ID 11047).
            kwargs["location"] = country
        if country:
            kwargs["country_indeed"] = country

        try:
            df = scrape_jobs(**kwargs)
            if df is not None and not df.empty:
                logger.debug("[scraper] jobspy term=%r location=%r → %d results",
                             term, loc or "any", len(df))
                return df
        except Exception as e:
            logger.error("[scraper] jobspy failed term=%r location=%r: %s", term, loc, e)
        return None

    # ── Fan out all combos in parallel (each in its own thread) ──────────────
    loop = asyncio.get_event_loop()
    combos = [(term, loc) for term in search_terms for loc in search_locations]
    tasks  = [loop.run_in_executor(None, _scrape_one, term, loc) for term, loc in combos]
    dfs    = await asyncio.gather(*tasks)

    import pandas as pd
    all_dfs = [df for df in dfs if df is not None]

    records: list[dict] = []
    if all_dfs:
        try:
            merged = pd.concat(all_dfs, ignore_index=True)
            if not merged.empty:
                records = merged.fillna("").to_dict("records")
        except Exception:
            pass

    logger.info(
        "[scraper] got %d raw jobs — %d term(s) × %d location(s) (parallel)",
        len(records), len(search_terms), len(search_locations),
    )
    return records
