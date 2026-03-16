"""
APScheduler-based job scrape loop — embedded in the bot process.

Schedule:
  - 08:00 daily  → morning scrape (all users)
  - 18:00 daily  → evening scrape (all users)

Each cycle:
  scrape → filter → parse_job → analyze_fit → send job card to Telegram → save to DB
"""

import logging
import uuid
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from bot.keyboards import job_card_keyboard
from db.models import Job, SkillNode, User
from db.session import AsyncSessionLocal
from jobs.filters import apply_filters, url_hash
from jobs.scraper import scrape_for_user

logger = logging.getLogger(__name__)


# ── DB helpers ───────────────────────────────────────────────────────────────

async def _get_seen_hashes(user_id: str, session) -> set[str]:
    result = await session.execute(select(Job.url_hash).where(Job.user_id == user_id))
    return {row[0] for row in result if row[0]}


async def _get_user_profile(user_id: str, session) -> dict:
    result = await session.execute(select(SkillNode).where(SkillNode.user_id == user_id))
    skills = [
        {"skill_name": n.skill_name, "status": n.status, "confidence": n.confidence}
        for n in result.scalars()
    ]
    return {"skills": skills, "variant_tags": ["general"]}


# ── Job card formatting ──────────────────────────────────────────────────────

def _build_card_text(job: Job, parsed: dict, fit: dict) -> str:
    score  = fit.get("fit_score", 0)
    action = fit.get("action", "consider")
    label  = {"apply": "Strong match", "consider": "Worth considering", "skip": "Weak match"}.get(action, "")

    matched = fit.get("matched_skills", [])
    gaps    = fit.get("missing_required", [])

    matched_str = ", ".join(matched[:5]) or "—"
    gap_str     = ", ".join(g["skill"] for g in gaps[:4]) if gaps else "None"

    salary   = parsed.get("salary_range") or "—"
    location = parsed.get("location") or "—"

    return (
        f"🏢 *{parsed.get('title') or job.title}*\n"
        f"{job.company} · {location} · {salary}\n\n"
        f"Fit Score: *{score}%* · {label}\n\n"
        f"✅ Matched: {matched_str}\n"
        f"❓ Gaps: {gap_str}"
    )


# ── Per-user scrape + notify ─────────────────────────────────────────────────

async def _process_user(user: User, bot, ai) -> None:
    logger.info("[scheduler] processing user=%s", user.telegram_id)

    raw_jobs = await scrape_for_user(user)
    if not raw_jobs:
        return

    async with AsyncSessionLocal() as session:
        async with session.begin():
            seen    = await _get_seen_hashes(user.id, session)
            profile = await _get_user_profile(user.id, session)

    filtered = apply_filters(raw_jobs, user.filters or {}, seen)
    if not filtered:
        logger.info("[scheduler] no new jobs after filtering for user=%s", user.telegram_id)
        return

    notified = 0

    async with AsyncSessionLocal() as session:
        async with session.begin():
            for raw in filtered[:10]:  # cap at 10 notifications per cycle
                try:
                    desc   = raw.get("description") or ""
                    parsed = await ai.parse_job(desc)
                    fit    = await ai.analyze_fit(parsed, profile)
                    score  = fit.get("fit_score", 0)

                    if score < user.min_fit_score:
                        logger.debug(
                            "[scheduler] score %d < threshold %d — skip", score, user.min_fit_score
                        )
                        continue

                    job_url = raw.get("job_url") or raw.get("url") or ""
                    job = Job(
                        id=str(uuid.uuid4()),
                        user_id=user.id,
                        title=parsed.get("title") or raw.get("title") or "Unknown",
                        company=raw.get("company") or parsed.get("company") or "Unknown",
                        url=job_url,
                        url_hash=raw.get("url_hash") or url_hash(job_url),
                        raw_jd=desc[:10_000],
                        parsed={**parsed, "_fit": fit},
                        fit_score=score,
                        cover_letter_required=bool(parsed.get("requires_cover_letter")),
                        status="pending",
                        created_at=datetime.utcnow(),
                    )
                    session.add(job)
                    await session.flush()

                    card_text = _build_card_text(job, parsed, fit)
                    fallback_url = job_url or f"https://www.google.com/search?q={job.title.replace(' ', '+')}"

                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=card_text,
                        parse_mode="Markdown",
                        reply_markup=job_card_keyboard(job.id, fallback_url),
                    )
                    notified += 1

                except Exception as e:
                    logger.error("[scheduler] error on job for user=%s: %s", user.telegram_id, e, exc_info=True)
                    continue

    logger.info("[scheduler] done — user=%s  notified=%d", user.telegram_id, notified)


# ── Main cycle ───────────────────────────────────────────────────────────────

async def run_scrape_cycle(bot, ai) -> None:
    """Entry point called by APScheduler. Processes all onboarded users."""
    logger.info("[scheduler] ── scrape cycle starting ──")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.onboarded == True))
        users  = result.scalars().all()

    logger.info("[scheduler] %d onboarded user(s) to process", len(users))

    for user in users:
        try:
            await _process_user(user, bot, ai)
        except Exception as e:
            logger.error("[scheduler] unhandled error for user=%s: %s", user.telegram_id, e, exc_info=True)

    logger.info("[scheduler] ── scrape cycle complete ──")


# ── Scheduler factory ────────────────────────────────────────────────────────

def build_scheduler(bot, ai) -> AsyncIOScheduler:
    """
    Build the APScheduler instance with two daily jobs.
    Call scheduler.start() to activate, scheduler.shutdown() to stop.
    """
    scheduler = AsyncIOScheduler()

    common = dict(args=[bot, ai], replace_existing=True)

    scheduler.add_job(
        run_scrape_cycle, "cron", hour=8,  minute=0,
        id="scrape_morning", name="Morning scrape (08:00)", **common,
    )
    scheduler.add_job(
        run_scrape_cycle, "cron", hour=18, minute=0,
        id="scrape_evening", name="Evening scrape (18:00)", **common,
    )

    logger.info("[scheduler] built — jobs scheduled at 08:00 and 18:00 daily")
    return scheduler
