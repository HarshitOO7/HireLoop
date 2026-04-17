"""
Standalone scrape script — full pipeline without the bot.

Scrapes jobs for the first onboarded user, applies all filters (including years_of_exp),
runs parse+fit AI, and saves qualifying jobs to DB.

Usage:
    python scripts/run_scrape.py
    python scripts/run_scrape.py --hours 48   # look back 48h instead of 24h

Run from project root.
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import time
import uuid
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)
for noisy in ("urllib3", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("run_scrape")

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()

    from ai.factory import AIFactory
    from ai.service import HireLoopAI
    from db.session import AsyncSessionLocal
    from db.models import User, Job, SkillNode, SkillEvidence
    from jobs.scraper import scrape_for_user
    from jobs.filters import apply_filters, url_hash
    from sqlalchemy import select, func

    fast    = AIFactory.create_fast()
    quality = AIFactory.create_quality()
    ai      = HireLoopAI(fast_provider=fast, quality_provider=quality)

    logger.info("Fast    : %s", fast.provider_name)
    logger.info("Quality : %s", quality.provider_name)

    async with AsyncSessionLocal() as s:
        result = await s.execute(select(User).where(User.onboarded == True))
        users = result.scalars().all()
        if not users:
            logger.error("No onboarded users found in DB.")
            return
        user = users[0]
        logger.info("User    : %s (%s)", user.name, user.id)
        f = user.filters or {}
        logger.info("User    : %s  min_fit=%d  years_of_exp=%r",
                    user.name, user.min_fit_score, f.get("years_of_exp"))

        # Already-seen hashes and keys to avoid re-inserting
        existing = (await s.execute(select(Job.url_hash, Job.title, Job.company)
                                    .where(Job.user_id == user.id))).all()
        seen_hashes = {row.url_hash for row in existing if row.url_hash}
        seen_keys   = {(row.title or "", row.company or "") for row in existing}

        # Build user profile for fit scoring
        node_rows = (await s.execute(
            select(SkillNode).where(SkillNode.user_id == user.id,
                                    SkillNode.status.like("verified_%"))
        )).scalars().all()
        ev_rows = (await s.execute(
            select(SkillEvidence).where(
                SkillEvidence.skill_node_id.in_([n.id for n in node_rows])
            )
        )).scalars().all()

    skills = [{"skill_name": n.skill_name, "confidence": n.confidence} for n in node_rows]
    profile = {
        "skills": skills,
        "variant_tags": f.get("role_variants", ["general"]),
    }

    # ── Expand roles ──────────────────────────────────────────────────────────
    role = f.get("role", "Software Developer")
    logger.info("Expanding role: %r", role)
    try:
        variants = await ai.expand_role_titles(role)
    except Exception as e:
        logger.warning("Role expansion failed (%s) — using raw role", e)
        variants = [r.strip() for r in role.split(",")]
    logger.info("Variants: %s", variants)

    # ── Scrape ────────────────────────────────────────────────────────────────
    logger.info("Scraping (hours_old=%d)...", args.hours)
    raw_jobs = await scrape_for_user(user, role_variants=variants)
    logger.info("Raw jobs: %d", len(raw_jobs))

    if not raw_jobs:
        logger.warning("No jobs returned — check your network/sites config.")
        return

    # ── Filter ────────────────────────────────────────────────────────────────
    filtered = apply_filters(
        raw_jobs, f,
        seen_hashes=seen_hashes,
        seen_keys=seen_keys,
        search_terms=variants,
        hours_old=args.hours,
    )
    logger.info("After filters: %d jobs", len(filtered))

    if not filtered:
        logger.info("Nothing new to process.")
        return

    # ── Parse + fit score ─────────────────────────────────────────────────────
    sem   = asyncio.Semaphore(3)
    saved = 0

    async def _process_one(raw: dict):
        nonlocal saved
        desc    = raw.get("description") or ""
        job_url = raw.get("job_url") or raw.get("url") or ""
        h       = raw.get("url_hash") or url_hash(job_url)
        title   = (raw.get("title") or "?")[:60]
        company = raw.get("company") or "?"

        async with sem:
            try:
                t0 = time.monotonic()
                parsed, fit = await ai.parse_and_analyze_fit(desc, profile)
                elapsed = time.monotonic() - t0
            except Exception as e:
                logger.error("AI error on %r @ %r: %s", title, company, e)
                return

        score  = fit.get("fit_score", 0)
        status = "pending" if score >= user.min_fit_score else "rejected"
        logger.info("  %s  fit=%d%%  [%s]  %s @ %s  (%.1fs)",
                    "✓" if status == "pending" else "✗", score, status, title, company, elapsed)

        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            title=raw.get("title") or parsed.get("title") or "Unknown",
            company=company,
            url=job_url,
            url_hash=h,
            raw_jd=desc[:10_000],
            parsed={**parsed, "_fit": fit},
            fit_score=score,
            cover_letter_required=bool(parsed.get("requires_cover_letter")),
            status=status,
            created_at=datetime.utcnow(),
        )
        async with AsyncSessionLocal() as s:
            async with s.begin():
                s.add(job)

        if status == "pending":
            saved += 1

    logger.info("Analyzing %d jobs...", len(filtered))
    await asyncio.gather(*[_process_one(raw) for raw in filtered])
    logger.info("Done — %d qualifying jobs saved (fit >= %d%%)", saved, user.min_fit_score)


if __name__ == "__main__":
    asyncio.run(main())
