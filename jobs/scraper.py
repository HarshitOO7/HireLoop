"""
JobSpy wrapper — scrapes Indeed, LinkedIn, and Glassdoor.

Each (term × location × site) triple runs in its own thread executor slot so
all boards are scraped in parallel instead of sequentially within a single call.

Caps to prevent runaway fan-out:
  MAX_VARIANTS  = 3  — max role variants used (excess trimmed, first N kept)
  MAX_LOCATIONS = 2  — max locations used

So the ceiling is 3 × 2 × 3 = 18 parallel tasks.

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
MAX_VARIANTS   = 3
MAX_LOCATIONS  = 2


def _hours_for_freq(notify_freq: str | None) -> int:
    """Return look-back window in hours based on the user's notify frequency."""
    return {"twice_daily": 12, "realtime": 6}.get(notify_freq or "daily", 24)


def _dedup_variants(terms: list[str], cap: int) -> list[str]:
    """
    Remove duplicates and subsumed variants, then cap to `cap` entries.

    Steps:
      1. Exact dedup (case-insensitive), preserving first occurrence.
      2. Subsumption: if term A's words are a strict subset of term B's words,
         drop B (A is broader and already covers B's search space).
         e.g. "Software Engineer" subsumes "Software Engineer II" or "Backend Software Engineer".
      3. Cap at `cap`, keeping the shortest (broadest) terms first.
    """
    # Step 1: exact dedup
    seen_lower: set[str] = set()
    unique: list[str] = []
    for t in terms:
        key = t.strip().lower()
        if key and key not in seen_lower:
            seen_lower.add(key)
            unique.append(t.strip())

    # Step 2: drop terms whose word-set is a strict superset of another term
    def words(t: str) -> frozenset[str]:
        return frozenset(t.lower().split())

    kept: list[str] = []
    for candidate in unique:
        cw = words(candidate)
        # Skip if any already-kept term's words are a strict subset of this one
        # (meaning the kept term is broader and covers this candidate)
        if any(words(other) < cw for other in kept):
            continue
        # Also remove already-kept terms that this candidate subsumes
        kept = [other for other in kept if not (cw < words(other))]
        kept.append(candidate)

    # Step 3: sort by word count (shorter = broader) then cap
    kept.sort(key=lambda t: len(t.split()))
    result = kept[:cap]

    if len(terms) != len(result):
        logger.info("[scraper] variants dedup: %d → %d (cap=%d)  kept=%s",
                    len(terms), len(result), cap, result)
    return result


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

    raw_terms = role_variants or ([role] if role else [])
    if not raw_terms:
        logger.warning("[scraper] user %s has no role filter — skipping scrape", user.telegram_id)
        return []

    # ── Dedup then cap ────────────────────────────────────────────────────────
    search_terms = _dedup_variants(raw_terms, MAX_VARIANTS)

    raw_locs = locations[:MAX_LOCATIONS] if locations else []
    if len(locations) > MAX_LOCATIONS:
        logger.info("[scraper] trimmed locations %d → %d (cap=%d)",
                    len(locations), MAX_LOCATIONS, MAX_LOCATIONS)
    search_locations = raw_locs or [""]  # [""] = no location filter

    is_remote = remote == "remote"

    total_tasks = len(search_terms) * len(search_locations) * len(sites)
    logger.info(
        "[scraper] starting — user=%s  variants=%d %s  locations=%s  country=%r  "
        "remote=%s  sites=%s  hours_old=%d  tasks=%d",
        user.telegram_id, len(search_terms), search_terms,
        raw_locs or "any", country, remote, sites, hours_old, total_tasks,
    )

    # ── One sync call per (term × location × site) — full parallelism ────────
    per_task = 25  # results per individual (term × loc × site) call

    def _scrape_one(term: str, loc: str, site: str):
        from jobspy import scrape_jobs

        kwargs = dict(
            site_name=[site],
            search_term=term,
            is_remote=is_remote,
            results_wanted=per_task,
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
                logger.debug("[scraper] %s term=%r loc=%r → %d results",
                             site, term, loc or "any", len(df))
                return df
        except Exception as e:
            logger.error("[scraper] %s failed term=%r loc=%r: %s", site, term, loc, e)
        return None

    # ── Fan out all (term × location × site) combos in parallel ──────────────
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, _scrape_one, term, loc, site)
        for term in search_terms
        for loc in search_locations
        for site in sites
    ]
    dfs = await asyncio.gather(*tasks)

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
        "[scraper] got %d raw jobs — %d variant(s) × %d location(s) × %d site(s) = %d tasks",
        len(records), len(search_terms), len(search_locations), len(sites), total_tasks,
    )
    return records
