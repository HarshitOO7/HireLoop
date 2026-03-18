"""
Adzuna job scraper — free, official API, no scraping / no proxies needed.

Adzuna is a job search engine available in 20+ countries.
Free API tier: 1,000 requests/day (more than enough for personal use).

Setup:
  1. Register at https://developer.adzuna.com  (free, instant)
  2. Add to .env:
       ADZUNA_APP_ID=your_app_id
       ADZUNA_APP_KEY=your_app_key
  3. Leave blank to skip silently — zero impact on the main scraper.

Supported countries (use 2-letter codes):
  ca, us, gb, au, de, fr, in, it, nl, pl, ru, sg, za, at, be, br, mx, nz

Why Adzuna over JobSpy for this use case:
  ✅ Official API — no bot-detection, no CAPTCHAs
  ✅ Returns full job descriptions
  ✅ Great Canada coverage (indeed + local boards aggregated)
  ✅ Salary data on many listings
  ✅ Free (1,000 req/day)
  ✅ Pure HTTP — no headless browser required
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_ADZUNA_TIMEOUT = 20  # seconds


def _creds() -> tuple[str, str] | None:
    """Return (app_id, app_key) or None if not configured."""
    app_id  = os.getenv("ADZUNA_APP_ID", "").strip()
    app_key = os.getenv("ADZUNA_APP_KEY", "").strip()
    if app_id and app_key:
        return app_id, app_key
    return None


def _normalize(item: dict) -> dict | None:
    """Map Adzuna response item → standard HireLoop job dict."""
    title   = (item.get("title") or "").strip()
    company = (item.get("company") or {}).get("display_name") or ""
    desc    = (item.get("description") or "").strip()
    url     = (item.get("redirect_url") or "").strip()
    loc     = (item.get("location") or {}).get("display_name") or ""

    if not title or not url:
        return None

    return {
        "site":        "adzuna",
        "title":       title,
        "company":     company.strip(),
        "description": desc,
        "job_url":     url,
        "location":    loc,
        "min_amount":  item.get("salary_min"),
        "max_amount":  item.get("salary_max"),
    }


async def _fetch_page(
    client: httpx.AsyncClient,
    app_id: str,
    app_key: str,
    country: str,
    term: str,
    location: str,
    max_days_old: int,
    page: int,
    results_per_page: int,
) -> list[dict]:
    url = _BASE.format(country=country.lower(), page=page)
    params: dict = {
        "app_id":           app_id,
        "app_key":          app_key,
        "what":             term,
        "results_per_page": results_per_page,
        "max_days_old":     max_days_old,
        "content-type":     "application/json",
    }
    if location:
        params["where"] = location

    try:
        r = await client.get(url, params=params, timeout=_ADZUNA_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("results") or []
    except Exception as e:
        logger.error("[adzuna] fetch failed — term=%r page=%d: %s", term, page, e)
        return []


async def scrape_adzuna(
    search_terms: list[str],
    location: str = "",
    country: str = "ca",
    hours_old: int = 24,
    results_per_term: int = 10,
) -> list[dict]:
    """
    Fetch jobs from Adzuna for each search term.

    Args:
        search_terms:    Title variants to search.
        location:        City / region (blank = nationwide).
        country:         2-letter country code (default "ca" for Canada).
        hours_old:       Only return jobs posted within this many hours.
        results_per_term: How many results to fetch per term.

    Returns:
        List of normalized job dicts (same schema as JobSpy output).
        Returns [] silently if ADZUNA_APP_ID/KEY not configured.
    """
    creds = _creds()
    if not creds:
        return []

    app_id, app_key = creds
    max_days_old = max(1, round(hours_old / 24))  # Adzuna uses days, not hours

    logger.info(
        "[adzuna] starting — %d term(s)  location=%r  country=%r  max_days_old=%d",
        len(search_terms), location or "any", country, max_days_old,
    )

    async with httpx.AsyncClient() as client:
        tasks = [
            _fetch_page(
                client, app_id, app_key,
                country, term, location, max_days_old,
                page=1, results_per_page=min(results_per_term, 50),
            )
            for term in search_terms
        ]
        raw_pages = await asyncio.gather(*tasks, return_exceptions=True)

    all_jobs: list[dict] = []
    for i, items in enumerate(raw_pages):
        if isinstance(items, Exception):
            logger.error("[adzuna] task %d raised: %s", i, items)
            continue
        for item in items:
            job = _normalize(item)
            if job:
                all_jobs.append(job)

    logger.info("[adzuna] total normalized: %d jobs", len(all_jobs))
    return all_jobs
