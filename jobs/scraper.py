"""
JobSpy wrapper — scrapes Indeed and Google Jobs.

JobSpy is synchronous (pandas-based), so we run it in a thread executor
to avoid blocking the async event loop.

Multiple locations: if user.filters["locations"] is a list, one scrape is run
per location and results are merged. Deduplication happens downstream in filters.py.

Role variants: if role_variants is passed (AI-expanded title list), each variant
is searched separately and results are merged. Semantic dedup collapses cross-board
duplicates before any AI calls.

Secondary sources:
  Adzuna API (free, set ADZUNA_APP_ID + ADZUNA_APP_KEY in .env) runs in parallel
  and its results are merged before filtering. Adds Canada/global coverage + more
  job descriptions.

hours_old is derived from user.notify_freq so we never show stale duplicates:
  twice_daily → 12 h
  daily       → 24 h  (default)
  realtime    → 6 h
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# LinkedIn removed: jobspy can no longer fetch descriptions — all results
# return empty description and get dropped by the no-description filter.
# Glassdoor removed: consistently returns API errors.
_DEFAULT_SITES = ["indeed", "google"]


def _hours_for_freq(notify_freq: str | None) -> int:
    """Return look-back window in hours based on the user's notify frequency."""
    return {"twice_daily": 12, "realtime": 6}.get(notify_freq or "daily", 24)


async def scrape_for_user(user, role_variants: list[str] | None = None) -> list[dict]:
    """
    Scrape jobs for a user based on their stored filters.

    role_variants: AI-expanded title list (e.g. ["Software Engineer", "SWE", "Backend Dev"]).
                   Falls back to user.filters["role"] if not provided.
    Returns merged job dicts from JobSpy + Adzuna (if configured).
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

    # ── Run JobSpy + Adzuna concurrently ─────────────────────────────────────
    from jobs.adzuna_scraper import scrape_adzuna

    loop = asyncio.get_event_loop()
    adzuna_country  = (country[:2].lower() if len(country) >= 2 else "ca")
    first_location  = locations[0] if locations else ""
    jobspy_future = loop.run_in_executor(None, _sync_scrape)
    adzuna_coro   = scrape_adzuna(
        search_terms=search_terms,
        location=first_location,
        country=adzuna_country,
        hours_old=hours_old,
        results_per_term=50,
    )

    results = await asyncio.gather(jobspy_future, adzuna_coro, return_exceptions=True)
    df_result, adzuna_jobs = results[0], results[1]

    if isinstance(df_result, Exception):
        logger.error("[scraper] jobspy raised: %s", df_result)
        df_result = None
    if isinstance(adzuna_jobs, Exception):
        logger.error("[scraper] adzuna raised: %s", adzuna_jobs)
        adzuna_jobs = []

    jobspy_records: list[dict] = []
    if df_result is not None:
        try:
            if not df_result.empty:
                jobspy_records = df_result.fillna("").to_dict("records")
        except Exception:
            pass

    all_records = jobspy_records + list(adzuna_jobs or [])
    logger.info(
        "[scraper] total %d raw jobs (jobspy=%d  adzuna=%d) — %d term(s) × %d location(s)",
        len(all_records), len(jobspy_records), len(adzuna_jobs or []),
        len(search_terms), len(search_locations),
    )
    return all_records
