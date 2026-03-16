"""
JobSpy wrapper — scrapes Indeed, LinkedIn, Glassdoor, Google Jobs.

JobSpy is synchronous (pandas-based), so we run it in a thread executor
to avoid blocking the async event loop.

Multiple locations: if user.filters["locations"] is a list, one scrape is run
per location and results are merged. Deduplication happens downstream in filters.py.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Default: all supported sites. Users can toggle during onboarding.
# Note: Glassdoor and Google are slower than Indeed/LinkedIn.
_DEFAULT_SITES = ["indeed", "linkedin", "glassdoor", "google"]


async def scrape_for_user(user) -> list[dict]:
    """
    Scrape jobs for a user based on their stored filters.
    All scrape parameters come from user.filters — nothing hardcoded.
    Returns a list of raw job dicts (from jobspy DataFrame).
    """
    f = user.filters or {}
    role    = (f.get("role") or "").strip()
    country = (f.get("country") or "").strip()
    remote  = f.get("remote", "any")
    sites   = f.get("sites") or _DEFAULT_SITES

    # Support both old single-string "location" and new list "locations"
    locations: list[str] = f.get("locations") or []
    if not locations and f.get("location"):
        locations = [(f.get("location") or "").strip()]

    if not role:
        logger.warning("[scraper] user %s has no role filter — skipping scrape", user.telegram_id)
        return []

    is_remote = remote == "remote"
    search_locations = locations or [""]  # [""] = no location filter

    logger.info(
        "[scraper] starting — user=%s  role=%r  locations=%s  country=%r  remote=%s  sites=%s",
        user.telegram_id, role, locations or "any", country, remote, sites,
    )

    def _sync_scrape():
        import pandas as pd
        from jobspy import scrape_jobs

        all_dfs = []
        for loc in search_locations:
            kwargs = dict(
                site_name=sites,
                search_term=role,
                is_remote=is_remote,
                results_wanted=25,
                hours_old=24,
            )
            if loc:
                kwargs["location"] = loc
            if country:
                kwargs["country_indeed"] = country

            try:
                df = scrape_jobs(**kwargs)
                if df is not None and not df.empty:
                    all_dfs.append(df)
                    logger.debug("[scraper] location=%r → %d results", loc or "any", len(df))
            except Exception as e:
                logger.error("[scraper] scrape failed for location=%r: %s", loc, e)

        if not all_dfs:
            return None
        return pd.concat(all_dfs, ignore_index=True)

    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(None, _sync_scrape)
    except Exception as e:
        logger.error("[scraper] scrape failed: %s", e, exc_info=True)
        return []

    if df is None or df.empty:
        logger.info("[scraper] no results returned")
        return []

    records = df.fillna("").to_dict("records")
    logger.info("[scraper] got %d raw jobs (across %d location(s))", len(records), len(search_locations))
    return records
