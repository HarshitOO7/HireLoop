"""
JobSpy wrapper — scrapes Indeed, LinkedIn, and Glassdoor.

Each (term × location × site) triple is a scrape task, run in a thread
executor. Concurrency is capped PER SITE (not globally) so a large title
fan-out doesn't fire a burst of near-simultaneous requests at any one board —
that burst pattern is what gets an IP flagged as a bot. Indeed in particular
blocks aggressively on concurrent-request bursts, so it gets the lowest cap
plus a small stagger delay between launches; LinkedIn tolerates more.

Caps to prevent runaway fan-out:
  MAX_VARIANTS  = 20 — max role variants used (excess trimmed, first N kept)
  MAX_LOCATIONS = 2  — max locations used
  SITE_CONCURRENCY — max in-flight requests, per site (see dict below)
  SITE_STAGGER_S   — delay between launching requests, per site

So the ceiling is 20 × 2 = 40 tasks per site, throttled per SITE_CONCURRENCY.
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
MAX_VARIANTS   = 20
MAX_LOCATIONS  = 2

# Indeed blocks hard on concurrent-request bursts from one IP (confirmed: went
# from healthy to 0-results-silently-every-scrape the run right after variant
# fan-out rose from 3 to 20, i.e. concurrent Indeed requests per scrape rose
# from ~9 to ~20+). Keep it low and paced. LinkedIn has shown no such
# sensitivity even at full 20-variant fan-out, so it keeps more headroom.
SITE_CONCURRENCY: dict[str, int] = {"indeed": 2, "linkedin": 8, "glassdoor": 3}
SITE_STAGGER_S:   dict[str, float] = {"indeed": 1.5, "linkedin": 0.0, "glassdoor": 0.5}
_DEFAULT_CONCURRENCY = 4
_DEFAULT_STAGGER_S   = 0.5


def _hours_for_freq(notify_freq: str | None) -> int:
    """Return look-back window in hours based on the user's notify frequency."""
    return {"twice_daily": 12}.get(notify_freq or "daily", 24)


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


async def scrape_for_user(
    user, role_variants: list[str] | None = None
) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """
    Scrape jobs for a user based on their stored filters.

    role_variants: AI-expanded title list (e.g. ["Software Engineer", "SWE", "Backend Dev"]).
                   Falls back to user.filters["role"] if not provided.

    Returns (records, per_site) where per_site is
    {site: {"results": int, "errors": int}} for health tracking.
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
        return [], {s: {"results": 0, "errors": 0} for s in sites}

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
                return (site, len(df), False, df)
            return (site, 0, False, None)
        except Exception as e:
            logger.error("[scraper] %s failed term=%r loc=%r: %s", site, term, loc, e)
            return (site, 0, True, None)

    # ── Fan out all (term × location × site) combos, throttled PER SITE ──────
    # A shared/global concurrency cap still lets many requests to the SAME site
    # land at once whenever combos for that site cluster together in the
    # batch — that concurrent-burst pattern is what got Indeed to start
    # silently blocking this IP. Each site instead gets its own semaphore
    # (and Indeed additionally gets a launch stagger) so no single board ever
    # sees more than its configured number of requests in flight together.
    loop = asyncio.get_event_loop()
    combos = [
        (term, loc, site)
        for term in search_terms
        for loc in search_locations
        for site in sites
    ]
    semaphores = {
        site: asyncio.Semaphore(SITE_CONCURRENCY.get(site, _DEFAULT_CONCURRENCY))
        for site in sites
    }
    logger.info("[scraper] running %d tasks — per-site concurrency: %s",
                len(combos), {s: SITE_CONCURRENCY.get(s, _DEFAULT_CONCURRENCY) for s in sites})

    async def _run_one(term: str, loc: str, site: str):
        async with semaphores[site]:
            stagger = SITE_STAGGER_S.get(site, _DEFAULT_STAGGER_S)
            if stagger:
                await asyncio.sleep(stagger)
            return await loop.run_in_executor(None, _scrape_one, term, loc, site)

    results = await asyncio.gather(*[_run_one(term, loc, site) for term, loc, site in combos])

    # Aggregate per-site results/errors for health tracking, and collect the dfs.
    per_site: dict[str, dict[str, int]] = {s: {"results": 0, "errors": 0} for s in sites}
    all_dfs = []
    for site, count, errored, df in results:
        per_site[site]["results"] += count
        if errored:
            per_site[site]["errors"] += 1
        if df is not None:
            all_dfs.append(df)

    import pandas as pd
    records: list[dict] = []
    if all_dfs:
        try:
            merged = pd.concat(all_dfs, ignore_index=True)
            if not merged.empty:
                records = merged.fillna("").to_dict("records")
        except Exception:
            pass

    logger.info(
        "[scraper] per-site: %s",
        "  ".join(f"{s}={per_site[s]['results']}(err={per_site[s]['errors']})" for s in sites),
    )
    logger.info(
        "[scraper] got %d raw jobs — %d variant(s) × %d location(s) × %d site(s) = %d tasks",
        len(records), len(search_terms), len(search_locations), len(sites), total_tasks,
    )
    return records, per_site
