"""
Scraper debug script — run directly, no bot needed.

Usage:
    python scripts/debug_scraper.py
    python scripts/debug_scraper.py --role "Backend Developer" --location "Toronto"
    python scripts/debug_scraper.py --role "AI Engineer" --sites indeed linkedin
    python scripts/debug_scraper.py --terms 3 --results 5   # quick test, 3 terms, 5 results each

Output:
    - Live to console (all levels)
    - Full report written to  scripts/scrape_debug_<timestamp>.txt
    - Raw job dump written to scripts/scrape_raw_<timestamp>.json

Edit the CONFIG block below to change defaults without touching CLI args.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── allow imports from project root ──────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

# ── output paths ─────────────────────────────────────────────────────────────
SCRIPTS_DIR  = Path(__file__).resolve().parent
TIMESTAMP    = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE     = SCRIPTS_DIR / f"scrape_debug_{TIMESTAMP}.txt"
RAW_DUMP     = SCRIPTS_DIR / f"scrape_raw_{TIMESTAMP}.json"

# ── logging: console + file simultaneously ───────────────────────────────────
fmt = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(fmt)
file_handler.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(fmt)
console_handler.setLevel(logging.DEBUG)

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Silence noise
for noisy in ("urllib3", "httpx", "httpcore", "JobSpy:Glassdoor"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("debug_scraper")

# ── CONFIG — edit these defaults ─────────────────────────────────────────────
DEFAULT_ROLE        = "Software Developer"   # override with --role for any other field
DEFAULT_LOCATION    = ""          # blank = anywhere
DEFAULT_COUNTRY     = "Canada"
DEFAULT_REMOTE      = "any"       # any | remote | onsite
DEFAULT_SITES       = ["indeed", "linkedin", "glassdoor", "google"]
DEFAULT_RESULTS     = 50          # results per search term — high enough to not miss 24h jobs
DEFAULT_HOURS_OLD   = 24          # 24 h default; use --hours to override
MAX_TERMS           = 8           # limit role variants to this many


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args():
    p = argparse.ArgumentParser(description="Debug HireLoop job scraper")
    p.add_argument("--role",     default=DEFAULT_ROLE,     help="Comma-separated role titles")
    p.add_argument("--location", default=DEFAULT_LOCATION, help="City/region or blank for any")
    p.add_argument("--country",  default=DEFAULT_COUNTRY,  help="Country for Indeed (e.g. Canada, USA)")
    p.add_argument("--remote",   default=DEFAULT_REMOTE,   choices=["any","remote","onsite"])
    p.add_argument("--sites",    default=DEFAULT_SITES,    nargs="+",
                   choices=["indeed","linkedin","glassdoor","google"])
    p.add_argument("--results",  default=DEFAULT_RESULTS,  type=int, help="Results per search term")
    p.add_argument("--hours",    default=DEFAULT_HOURS_OLD,type=int, help="Hours old cutoff")
    p.add_argument("--terms",    default=MAX_TERMS,        type=int, help="Max role variants to test")
    p.add_argument("--years",      default=None,         help="Years of exp filter e.g. '< 4', '2-5', '3+', 'any'")
    p.add_argument("--no-expand",  action="store_true", help="Skip AI role expansion, use --role as-is")
    p.add_argument("--no-jobspy", action="store_true",  help="Skip JobSpy (dry run / filter test only)")
    return p.parse_args()


# ── role expansion (optional — needs ANTHROPIC/GROQ key) ─────────────────────
async def _expand_roles(role: str, limit: int) -> list[str]:
    try:
        from ai.factory import AIFactory
        from ai.service import HireLoopAI
        fast = AIFactory.create_fast()
        ai   = HireLoopAI(fast_provider=fast, quality_provider=fast)
        variants = await ai.expand_role_titles(role)
        return variants[:limit]
    except Exception as e:
        logger.warning("Role expansion failed (%s) — using raw titles", e)
        return [t.strip() for t in role.split(",")][:limit]


# ── scrape one term ───────────────────────────────────────────────────────────
def _scrape_term(term: str, args) -> list[dict]:
    """Synchronous — called from thread executor."""
    from jobspy import scrape_jobs
    kwargs = dict(
        site_name     = args.sites,
        search_term   = term,
        is_remote     = (args.remote == "remote"),
        results_wanted= args.results,
        hours_old     = args.hours,
        fetch_description=True,
    )
    if args.location:
        kwargs["location"] = args.location
    if args.country:
        kwargs["country_indeed"] = args.country

    logger.info("  scraping: %r  location=%r  sites=%s", term, args.location or "any", args.sites)
    t0 = time.monotonic()
    try:
        df = scrape_jobs(**kwargs)
    except Exception as e:
        logger.error("  scrape_jobs() raised: %s", e, exc_info=True)
        return []
    elapsed = time.monotonic() - t0

    if df is None or df.empty:
        logger.warning("  → 0 results (%.2fs)", elapsed)
        return []

    records = df.fillna("").to_dict("records")
    logger.info("  → %d results (%.2fs)", len(records), elapsed)
    return records


# ── analyse results ───────────────────────────────────────────────────────────
def _analyse(all_jobs: list[dict]):
    """Print a breakdown of what we got."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("ANALYSIS")
    logger.info("=" * 60)

    by_site: dict[str, int] = {}
    no_desc = has_desc = has_salary = no_url = 0

    for j in all_jobs:
        site = str(j.get("site") or "unknown")
        by_site[site] = by_site.get(site, 0) + 1

        desc = (j.get("description") or "").strip()
        if desc:
            has_desc += 1
        else:
            no_desc += 1

        if j.get("job_url") or j.get("url"):
            pass
        else:
            no_url += 1

        sal = j.get("max_amount") or j.get("min_amount")
        if sal:
            has_salary += 1

    logger.info("Total raw jobs:     %d", len(all_jobs))
    logger.info("  Has description:  %d  (%.0f%%)", has_desc,
                100 * has_desc / len(all_jobs) if all_jobs else 0)
    logger.info("  No description:   %d  ← THESE ARE DROPPED BY FILTER", no_desc)
    logger.info("  No URL:           %d", no_url)
    logger.info("  Has salary info:  %d", has_salary)
    logger.info("")
    logger.info("By site:")
    for site, count in sorted(by_site.items(), key=lambda x: -x[1]):
        logger.info("  %-20s %d", site, count)

    # Dedup check
    urls = [j.get("job_url") or j.get("url") or "" for j in all_jobs]
    dupes = len(urls) - len(set(urls))
    logger.info("")
    logger.info("Duplicate URLs:     %d", dupes)

    # ALL jobs — full table (title, company, location, salary, site, url, has_desc)
    logger.info("")
    logger.info("ALL JOBS (%d total):", len(all_jobs))
    logger.info("-" * 60)
    for i, j in enumerate(all_jobs, 1):
        title   = (j.get("title") or "?")[:55]
        company = (j.get("company") or "?")[:30]
        site    = str(j.get("site") or "?")[:10]
        url     = j.get("job_url") or j.get("url") or "NO-URL"
        salary  = j.get("max_amount") or j.get("min_amount") or ""
        loc     = (j.get("location") or "")[:25]
        has_d   = "✓ desc" if (j.get("description") or "").strip() else "✗ NO DESC"
        sal_str = f"  ${salary}" if salary else ""
        logger.info(
            "%3d. [%-10s] %-55s | %-30s | %-25s%s  %s",
            i, site, title, company, loc, sal_str, has_d,
        )
        logger.info("     %s", url)
    logger.info("-" * 60)


# ── after-filter analysis ─────────────────────────────────────────────────────
def _run_filters(all_jobs: list[dict], args, terms: list[str]) -> list[dict]:
    from jobs.filters import apply_filters
    user_filters = {
        "role":         args.role,
        "location":     args.location,
        "remote":       args.remote,
        "min_salary":   0,
        "blacklist":    [],
        "years_of_exp": args.years,
    }
    if args.years:
        logger.info("  years_of_exp filter: %r", args.years)
    filtered = apply_filters(all_jobs, user_filters, seen_hashes=set(), seen_keys=set(),
                             search_terms=terms)
    logger.info("")
    logger.info("AFTER FILTERS: %d / %d jobs remain", len(filtered), len(all_jobs))
    if filtered:
        logger.info("JOBS THAT PASSED FILTERS:")
        logger.info("-" * 60)
        for i, j in enumerate(filtered, 1):
            title   = (j.get("title") or "?")[:55]
            company = (j.get("company") or "?")[:30]
            site    = str(j.get("site") or "?")[:10]
            url     = j.get("job_url") or j.get("url") or "NO-URL"
            salary  = j.get("max_amount") or j.get("min_amount") or ""
            loc     = (j.get("location") or "")[:25]
            sal_str = f"  ${salary}" if salary else ""
            logger.info("%3d. [%-10s] %-55s | %-30s | %-25s%s", i, site, title, company, loc, sal_str)
            logger.info("     %s", url)
        logger.info("-" * 60)
    return filtered


# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    args = _parse_args()

    logger.info("=" * 60)
    logger.info("HireLoop Scraper Debug")
    logger.info("=" * 60)
    logger.info("Role:      %s", args.role)
    logger.info("Location:  %s", args.location or "any")
    logger.info("Country:   %s", args.country)
    logger.info("Remote:    %s", args.remote)
    logger.info("Sites:     %s", args.sites)
    logger.info("Results:   %d per term", args.results)
    logger.info("Hours old: %d", args.hours)
    logger.info("Years exp: %s", args.years or "any (no filter)")
    logger.info("Log file:  %s", LOG_FILE)
    logger.info("Raw dump:  %s", RAW_DUMP)
    logger.info("=" * 60)

    # Step 1: get search terms
    if args.no_expand:
        terms = [t.strip() for t in args.role.split(",")][:args.terms]
        logger.info("Using raw role titles: %s", terms)
    else:
        logger.info("Expanding role titles via AI...")
        terms = await _expand_roles(args.role, args.terms)
        logger.info("Expanded to %d terms: %s", len(terms), terms)

    # Step 2: scrape each term
    logger.info("")
    logger.info("SCRAPING %d terms...", len(terms))
    logger.info("-" * 60)

    import asyncio
    loop = asyncio.get_event_loop()
    all_jobs: list[dict] = []

    if args.no_jobspy:
        logger.info("(JobSpy skipped — --no-jobspy flag set)")
    else:
        for i, term in enumerate(terms, 1):
            logger.info("[%d/%d] term=%r", i, len(terms), term)
            t0 = time.monotonic()
            jobs = await loop.run_in_executor(None, _scrape_term, term, args)
            logger.info("  done in %.2fs — got %d jobs", time.monotonic() - t0, len(jobs))
            all_jobs.extend(jobs)

    logger.info("")
    logger.info("TOTAL RAW: %d jobs from %d terms", len(all_jobs), len(terms))

    # Step 3: analyse raw results
    if all_jobs:
        _analyse(all_jobs)
    else:
        logger.warning("NO JOBS RETURNED — check your role/location/sites config above")
        logger.warning("Possible causes:")
        logger.warning("  1. jobspy not installed: pip install python-jobspy")
        logger.warning("  2. Sites are rate-limiting your IP (try again in 10 min)")
        logger.warning("  3. Search term too specific or unusual")
        logger.warning("  4. All results older than %d hours", args.hours)

    # Step 4: apply filters
    if all_jobs:
        _run_filters(all_jobs, args, terms)

    # Step 5: dump raw JSON
    try:
        # Stringify any non-serialisable values
        clean = []
        for j in all_jobs:
            clean.append({
                k: (str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v)
                for k, v in j.items()
            })
        with open(RAW_DUMP, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, ensure_ascii=False)
        logger.info("")
        logger.info("Raw JSON written → %s", RAW_DUMP)
    except Exception as e:
        logger.warning("Could not write JSON dump: %s", e)

    logger.info("")
    logger.info("=" * 60)
    logger.info("DONE — full log at %s", LOG_FILE)
    logger.info("=" * 60)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
