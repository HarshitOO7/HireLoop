"""
APScheduler-based job scrape loop — embedded in the bot process.

Schedule:
  - 08:00 daily  → morning scrape (all users)
  - 18:00 daily  → evening scrape (all users)

Each cycle:
  scrape → filter → parse_job → analyze_fit → send job card to Telegram → save to DB
"""

import logging
import time
import uuid
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, select, update

from bot.keyboards import job_card_keyboard
from db.models import Job, SkillNode, User
from db.session import AsyncSessionLocal
from jobs.filters import apply_filters, semantic_key, url_hash
from jobs.scraper import scrape_for_user

logger = logging.getLogger(__name__)


# ── DB helpers ───────────────────────────────────────────────────────────────

async def _get_seen(user_id: str, session) -> tuple[set[str], set[tuple]]:
    """One query — returns (url_hashes, semantic_keys) for fast pre-AI dedup."""
    result = await session.execute(
        select(Job.url_hash, Job.title, Job.company).where(Job.user_id == user_id)
    )
    rows = result.all()
    hashes = {r[0] for r in rows if r[0]}
    keys   = {((r[1] or "").lower().strip(), (r[2] or "").lower().strip()) for r in rows}
    return hashes, keys


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

_VARIANTS_TTL = 86_400  # refresh expanded titles once per day


async def _get_role_variants(user: User, ai) -> list[str]:
    """
    Return AI-expanded role title variants for this user.
    Cached in user.filters["role_variants"] for 24 h to avoid daily AI calls.
    """
    f = user.filters or {}
    role = (f.get("role") or "").strip()
    if not role:
        return []

    cached_at = f.get("role_variants_at", 0)
    cached    = f.get("role_variants") or []

    if cached and (time.time() - cached_at) < _VARIANTS_TTL:
        logger.info("[scheduler] role variants cache hit for user=%s — %d variants",
                    user.telegram_id, len(cached))
        return cached

    logger.info("[scheduler] expanding role titles for user=%s — role=%r", user.telegram_id, role)
    try:
        variants = await ai.expand_role_titles(role)
    except Exception as e:
        logger.error("[scheduler] role expansion failed: %s — falling back to raw role", e)
        return [role]

    new_filters = {**f, "role_variants": variants, "role_variants_at": time.time()}
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(User).where(User.id == user.id).values(filters=new_filters)
            )
    logger.info("[scheduler] cached %d role variants for user=%s: %s",
                len(variants), user.telegram_id, variants)
    return variants


async def _process_user(user: User, bot, ai) -> int:
    logger.info("[scheduler] processing user=%s", user.telegram_id)

    role_variants = await _get_role_variants(user, ai)
    raw_jobs = await scrape_for_user(user, role_variants=role_variants or None)
    if not raw_jobs:
        return 0

    async with AsyncSessionLocal() as session:
        async with session.begin():
            seen_hashes, seen_keys = await _get_seen(user.id, session)
            profile = await _get_user_profile(user.id, session)

    from jobs.scraper import _hours_for_freq
    hours_old = _hours_for_freq(getattr(user, "notify_freq", None))

    filtered = apply_filters(
        raw_jobs, user.filters or {}, seen_hashes, seen_keys,
        search_terms=role_variants or None,
        hours_old=hours_old,
    )
    if not filtered:
        logger.info("[scheduler] no new jobs after filtering for user=%s", user.telegram_id)
        return 0

    notified = 0

    # Build keyword set from user skills for fast pre-filter
    skill_keywords = {
        s["skill_name"].lower()
        for s in profile.get("skills", [])
        if s.get("status", "").startswith("verified_")
    }
    # Also add short words (2+ chars) from each skill name for partial matching
    skill_tokens: set[str] = set()
    for sk in skill_keywords:
        skill_tokens.update(w for w in sk.split() if len(w) >= 2)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            for raw in filtered[:10]:  # cap at 10 notifications per cycle
                try:
                    desc   = raw.get("description") or ""

                    # Fast keyword pre-filter: skip AI entirely if no skill overlap
                    if skill_keywords or skill_tokens:
                        desc_lower = desc.lower()
                        has_overlap = any(kw in desc_lower for kw in skill_keywords) or                                       any(tok in desc_lower for tok in skill_tokens)
                        if not has_overlap:
                            logger.debug("[scheduler] keyword pre-filter: no skill overlap — skip (saves 2 AI calls)")
                            continue

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
    return notified


# ── Cleanup ──────────────────────────────────────────────────────────────────

async def _purge_old_jobs(days: int = 10) -> int:
    """Delete jobs older than `days` days. Runs at the start of each scrape cycle."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                delete(Job).where(
                    Job.created_at < cutoff,
                    Job.status.in_(["pending", "skipped", "skill_verify"]),
                )
            )
    deleted = result.rowcount
    if deleted:
        logger.info("[scheduler] purged %d job(s) older than %d days", deleted, days)
    return deleted


# ── Main cycle ───────────────────────────────────────────────────────────────

async def run_scrape_cycle(bot, ai, telegram_id: str | None = None) -> int:
    """
    Entry point called by APScheduler or /fetchnow.
    Pass telegram_id to run for a single user only.
    Returns total jobs notified across all processed users.
    """
    logger.info("[scheduler] ── scrape cycle starting%s ──",
                f" (user={telegram_id})" if telegram_id else "")

    async with AsyncSessionLocal() as session:
        query = select(User).where(User.onboarded == True)
        if telegram_id:
            query = query.where(User.telegram_id == str(telegram_id))
        result = await session.execute(query)
        users  = result.scalars().all()

    logger.info("[scheduler] %d onboarded user(s) to process", len(users))

    if not telegram_id:  # only purge on full scheduled cycles, not manual fetches
        await _purge_old_jobs(days=10)

    total = 0
    for user in users:
        try:
            n = await _process_user(user, bot, ai)
            total += n or 0
        except Exception as e:
            logger.error("[scheduler] unhandled error for user=%s: %s", user.telegram_id, e, exc_info=True)

    logger.info("[scheduler] ── scrape cycle complete — total notified=%d ──", total)
    return total


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
