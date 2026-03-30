"""
APScheduler-based job scrape loop — embedded in the bot process.

Schedule:
  - 08:00 daily  → morning scrape (all users)
  - 18:00 daily  → evening scrape (all users)

Each cycle:
  scrape → filter → parse_job → analyze_fit → send first job card to Telegram → save to DB
  After user acts on a card, send_next_pending_card() delivers the next one.
"""

import asyncio
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

def _md(text) -> str:
    """Escape Telegram MarkdownV1 special chars in user-controlled strings."""
    return (str(text) if text else "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def _build_card_text(job: Job, parsed: dict, fit: dict) -> str:
    score  = fit.get("fit_score", 0)
    action = fit.get("action", "consider")
    label  = {"apply": "Strong match", "consider": "Worth considering", "skip": "Weak match"}.get(action, "")

    matched = fit.get("matched_skills", [])
    gaps    = fit.get("missing_required", [])

    matched_str = ", ".join(_md(s) for s in matched[:5]) or "—"
    gap_str     = ", ".join(_md(g.get("skill", "?")) for g in gaps[:4]) if gaps else "None"

    salary   = _md(parsed.get("salary_range") or "—")
    location = _md(parsed.get("location") or "—")

    return (
        f"🏢 *{_md(parsed.get('title') or job.title)}*\n"
        f"{_md(job.company)} · {location} · {salary}\n\n"
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


async def _process_user(user: User, bot, ai, is_manual: bool = False) -> int:
    logger.info("[scheduler] processing user=%s", user.telegram_id)

    role_variants = await _get_role_variants(user, ai)
    raw_jobs = await scrape_for_user(user, role_variants=role_variants or None)

    if not raw_jobs:
        if is_manual:
            await bot.send_message(
                chat_id=user.telegram_id,
                text="Done searching — no listings found. Try adjusting your search terms with 🎛️ Edit Filters.",
            )
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
        if is_manual:
            await bot.send_message(
                chat_id=user.telegram_id,
                text="Done — no new listings found (all already seen). I'll check again next cycle.",
            )
        logger.info("[scheduler] no new jobs after filtering for user=%s", user.telegram_id)
        return 0

    top = filtered[:10]

    await bot.send_message(
        chat_id=user.telegram_id,
        text=f"🔍 Analyzing your top {len(top)} for fit...",
    )

    # Build skill keyword sets for fast pre-filter
    skill_keywords = {
        s["skill_name"].lower()
        for s in profile.get("skills", [])
        if s.get("status", "").startswith("verified_")
    }
    skill_tokens: set[str] = set()
    for sk in skill_keywords:
        skill_tokens.update(w for w in sk.split() if len(w) >= 2)

    # Parallel AI analysis — max 3 concurrent to stay within rate limits
    sem = asyncio.Semaphore(3)

    async def _analyze_one(raw) -> tuple:
        desc    = raw.get("description") or ""
        job_url = raw.get("job_url") or raw.get("url") or ""
        h       = raw.get("url_hash") or url_hash(job_url)

        if skill_keywords or skill_tokens:
            desc_lower = desc.lower()
            has_overlap = (
                any(kw in desc_lower for kw in skill_keywords)
                or any(tok in desc_lower for tok in skill_tokens)
            )
            if not has_overlap:
                logger.debug("[scheduler] keyword pre-filter: no skill overlap — skip (saves 2 AI calls)")
                return (raw, None, None, h, "no_overlap")

        async with sem:
            try:
                parsed = await ai.parse_job(desc)
                fit    = await ai.analyze_fit(parsed, profile)
                return (raw, parsed, fit, h, "ok")
            except Exception as e:
                logger.error("[scheduler] AI error on job: %s", e)
                return (raw, None, None, h, "error")

    results = await asyncio.gather(*[_analyze_one(raw) for raw in top])

    # Save results to DB, collect qualifying jobs
    qualifying: list[Job] = []

    for raw, parsed, fit, h, analysis_status in results:
        job_url = raw.get("job_url") or raw.get("url") or ""
        desc    = raw.get("description") or ""
        try:
            if analysis_status == "no_overlap":
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        session.add(Job(
                            id=str(uuid.uuid4()), user_id=user.id,
                            title=raw.get("title") or "Unknown",
                            company=raw.get("company") or "Unknown",
                            url=job_url, url_hash=h,
                            status="rejected", created_at=datetime.utcnow(),
                        ))
                continue

            if analysis_status == "error" or not parsed or not fit:
                continue

            score      = fit.get("fit_score", 0)
            job_status = "pending" if score >= user.min_fit_score else "rejected"
            job = Job(
                id=str(uuid.uuid4()),
                user_id=user.id,
                title=raw.get("title") or parsed.get("title") or "Unknown",
                company=raw.get("company") or parsed.get("company") or "Unknown",
                url=job_url,
                url_hash=h,
                raw_jd=desc[:10_000],
                parsed={**parsed, "_fit": fit},
                fit_score=score,
                cover_letter_required=bool(parsed.get("requires_cover_letter")),
                status=job_status,
                created_at=datetime.utcnow(),
            )
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    session.add(job)

            if score >= user.min_fit_score:
                qualifying.append(job)
            else:
                logger.debug("[scheduler] score %d < threshold %d — saved as rejected", score, user.min_fit_score)

        except Exception as e:
            logger.error("[scheduler] error saving job for user=%s: %s", user.telegram_id, e, exc_info=True)
            continue

    if not qualifying:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=f"No new jobs above your {user.min_fit_score}% threshold this time. Try 🎛️ Edit Filters to lower it.",
        )
        logger.info("[scheduler] no qualifying jobs for user=%s", user.telegram_id)
        return 0

    # Sort best-fit first
    qualifying.sort(key=lambda j: j.fit_score or 0, reverse=True)
    count = len(qualifying)

    await bot.send_message(
        chat_id=user.telegram_id,
        text=(
            f"✅ *{count} job{'s' if count != 1 else ''}* match your {user.min_fit_score}%+ criteria!\n\n"
            f"Act on each card and the next one follows automatically 👇"
        ),
        parse_mode="Markdown",
    )

    # Send ONLY the first card — the rest stay as "pending" in DB.
    # After the user acts on this card, send_next_pending_card() delivers the next one.
    first     = qualifying[0]
    first_fit = (first.parsed or {}).get("_fit", {})
    card_text = _build_card_text(first, first.parsed or {}, first_fit)
    fallback  = first.url or f"https://www.google.com/search?q={first.title.replace(' ', '+')}"

    await bot.send_message(
        chat_id=user.telegram_id,
        text=card_text,
        parse_mode="Markdown",
        reply_markup=job_card_keyboard(first.id, fallback),
    )

    logger.info("[scheduler] done — user=%s  queued %d jobs, sent first card", user.telegram_id, count)
    return count


async def send_next_pending_card(telegram_id: str, bot) -> bool:
    """
    Find the next pending job for this user and send a card.
    Called after the user acts on a job card (skip or resume delivered).
    Returns True if a card was sent, False if the queue is empty.
    """
    async with AsyncSessionLocal() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == str(telegram_id))
        )
        user = user_result.scalar_one_or_none()
        if not user:
            return False

        job_result = await session.execute(
            select(Job)
            .where(Job.user_id == user.id, Job.status == "pending")
            .order_by(Job.fit_score.desc(), Job.created_at.asc())
            .limit(1)
        )
        job = job_result.scalar_one_or_none()

    if not job:
        await bot.send_message(
            chat_id=telegram_id,
            text="✅ All caught up — no more pending jobs!\n\nI'll notify you at the next scheduled search, or tap 🔍 Fetch Jobs anytime.",
        )
        return False

    fit       = (job.parsed or {}).get("_fit", {})
    card_text = _build_card_text(job, job.parsed or {}, fit)
    fallback  = job.url or f"https://www.google.com/search?q={job.title.replace(' ', '+')}"

    await bot.send_message(
        chat_id=telegram_id,
        text=card_text,
        parse_mode="Markdown",
        reply_markup=job_card_keyboard(job.id, fallback),
    )
    return True


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
    Entry point called by APScheduler or manual fetch.
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

    is_manual = telegram_id is not None
    total = 0
    for user in users:
        if (user.filters or {}).get("paused"):
            logger.info("[scheduler] skipping paused user=%s", user.telegram_id)
            continue
        try:
            n = await _process_user(user, bot, ai, is_manual=is_manual)
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
