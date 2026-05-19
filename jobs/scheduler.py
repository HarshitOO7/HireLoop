"""
APScheduler-based job scrape loop — embedded in the bot process.

Schedule:
  - Hourly tick (Mon–Fri) → per-user timezone check → fires at 08:00 and 18:00 local time

Each cycle:
  scrape → filter → parse_job → analyze_fit → send first job card to Telegram → save to DB
  Background batch completes and sends a summary ("N more matched").
  After user acts on a card, send_next_pending_card() delivers the next one.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, func, select, update

from bot.keyboards import job_card_keyboard
from db.models import Job, SkillNode, User
from db.session import AsyncSessionLocal
from jobs.filters import apply_filters, semantic_key, url_hash
from jobs.scraper import scrape_for_user

logger = logging.getLogger(__name__)


# ── DB helpers ───────────────────────────────────────────────────────────────

async def _get_seen(user_id: str, session) -> tuple[set[str], set[tuple], dict[str, int]]:
    """Returns (url_hashes, semantic_keys, recent_company_counts) for pre-AI dedup."""
    result = await session.execute(
        select(Job.url_hash, Job.title, Job.company).where(Job.user_id == user_id)
    )
    rows = result.all()
    hashes = {r[0] for r in rows if r[0]}
    keys   = {((r[1] or "").lower().strip(), (r[2] or "").lower().strip()) for r in rows}

    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = await session.execute(
        select(Job.company, func.count()).select_from(Job)
        .where(Job.user_id == user_id, Job.created_at >= cutoff)
        .group_by(Job.company)
    )
    company_counts = {
        (c or "").lower().strip(): n
        for c, n in recent.all()
        if c
    }
    return hashes, keys, company_counts


async def _get_user_profile(user_id: str, session) -> dict:
    result = await session.execute(select(SkillNode).where(SkillNode.user_id == user_id))
    skills = [
        {"skill_name": n.skill_name, "status": n.status, "confidence": n.confidence}
        for n in result.scalars()
    ]
    return {"skills": skills, "variant_tags": ["general"]}


# ── Job card formatting ──────────────────────────────────────────────────────

def _esc(text) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return (str(text) if text else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_card_text(job: Job, parsed: dict, fit: dict) -> str:
    score  = fit.get("fit_score", 0)
    action = fit.get("action", "consider")
    label  = {"apply": "Strong match", "consider": "Worth considering", "skip": "Weak match"}.get(action, "")

    matched = fit.get("matched_skills", [])
    gaps    = fit.get("missing_required", [])

    matched_str = ", ".join(_esc(s) for s in matched[:5]) or "—"
    gap_str     = ", ".join(_esc(g.get("skill", "?")) for g in gaps[:4]) if gaps else "None"

    salary   = _esc(parsed.get("salary_range") or "—")
    location = _esc(parsed.get("location") or "—")

    text = (
        f"🏢 <b>{_esc(parsed.get('title') or job.title)}</b>\n"
        f"{_esc(job.company)} · {location} · {salary}\n\n"
        f"Fit Score: <b>{score}%</b> · {label}\n\n"
        f"✅ Matched: {matched_str}\n"
        f"❓ Gaps: {gap_str}"
    )
    gap_summary = fit.get("gap_summary", "")
    if gap_summary:
        text += f"\n📝 <i>{_esc(gap_summary)}</i>"
    return text


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


async def _save_job_result(
    raw, parsed, fit, h, analysis_status,
    user: User,
    qualifying: list | None,
) -> None:
    """Persist one analysis result. Appends to qualifying list if it passes threshold."""
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
            return

        if analysis_status == "error" or not parsed or not fit:
            return

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

        if qualifying is not None:
            if score >= user.min_fit_score:
                qualifying.append(job)
            else:
                logger.debug("[scheduler] score %d < threshold %d — rejected", score, user.min_fit_score)

    except Exception as e:
        logger.error("[scheduler] error saving job for user=%s: %s", user.telegram_id, e, exc_info=True)


async def _analyze_remaining(
    jobs: list,
    user: User,
    profile: dict,
    skill_keywords: set,
    skill_tokens: set,
    ai,
    bot=None,
) -> None:
    """Background task: analyze remaining batches, save to DB, then send a completion summary."""
    sem   = asyncio.Semaphore(2)   # reduced from 5 to avoid 429s
    BATCH = 5
    bg_qualifying: list[Job] = []

    async def _bg_one(raw) -> tuple:
        desc    = raw.get("description") or ""
        job_url = raw.get("job_url") or raw.get("url") or ""
        h       = raw.get("url_hash") or url_hash(job_url)
        if skill_keywords or skill_tokens:
            desc_lower = desc.lower()
            if not (any(kw in desc_lower for kw in skill_keywords)
                    or any(tok in desc_lower for tok in skill_tokens)):
                return (raw, None, None, h, "no_overlap")
        async with sem:
            try:
                parsed, fit = await ai.parse_and_analyze_fit(desc, profile)
                return (raw, parsed, fit, h, "ok")
            except Exception as e:
                logger.error("[scheduler] bg AI error: %s", e)
                return (raw, None, None, h, "error")

    for i in range(0, len(jobs), BATCH):
        batch   = jobs[i:i + BATCH]
        results = await asyncio.gather(*[_bg_one(raw) for raw in batch])
        for result in results:
            await _save_job_result(*result, user=user, qualifying=bg_qualifying)
        logger.info("[scheduler] background batch %d–%d / %d done",
                    i + 1, i + len(batch), len(jobs))
        if i + BATCH < len(jobs):
            await asyncio.sleep(2)   # breathing room between batches

    if bot:
        n_more = len(bg_qualifying)
        if n_more > 0:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"✅ Analysis done — <b>{n_more} more job{'s' if n_more != 1 else ''}</b> matched. "
                    f"Tap 📋 Pending Jobs to continue."
                ),
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=user.telegram_id,
                text="✅ Background analysis done — no additional matches found.",
            )


def _weekday_days_since(dt: datetime | None) -> int:
    """Count Mon–Fri days elapsed since dt. Returns 999 if dt is None."""
    if dt is None:
        return 999
    now = datetime.utcnow()
    if (now - dt).total_seconds() < 0:
        return 0
    if (now - dt).days > 10:
        return 10  # fast path — definitely > 3 weekdays
    count = 0
    current = dt.date()
    end = now.date()
    while current < end:
        if current.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        current += timedelta(days=1)
    return count


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
            seen_hashes, seen_keys, company_counts = await _get_seen(user.id, session)
            profile = await _get_user_profile(user.id, session)

    from jobs.scraper import _hours_for_freq
    hours_old = _hours_for_freq(getattr(user, "notify_freq", None))

    filtered = apply_filters(
        raw_jobs, user.filters or {}, seen_hashes, seen_keys,
        search_terms=role_variants or None,
        hours_old=hours_old,
        company_cooldown=company_counts,
    )

    if not filtered:
        if is_manual:
            await bot.send_message(
                chat_id=user.telegram_id,
                text="Done — no new listings found (all already seen). I'll check again next cycle.",
            )
        logger.info("[scheduler] no new jobs after filtering for user=%s", user.telegram_id)
        return 0

    n_total = len(filtered)
    await bot.send_message(
        chat_id=user.telegram_id,
        text=f"🔍 {n_total} listing{'s' if n_total != 1 else ''} found — analyzing fit in batches of 5...",
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

    async def _analyze_one(raw, sem) -> tuple:
        desc    = raw.get("description") or ""
        job_url = raw.get("job_url") or raw.get("url") or ""
        h       = raw.get("url_hash") or url_hash(job_url)
        if skill_keywords or skill_tokens:
            desc_lower = desc.lower()
            if not (any(kw in desc_lower for kw in skill_keywords)
                    or any(tok in desc_lower for tok in skill_tokens)):
                logger.debug("[scheduler] keyword pre-filter: no overlap — skip")
                return (raw, None, None, h, "no_overlap")
        async with sem:
            try:
                parsed, fit = await ai.parse_and_analyze_fit(desc, profile)
                return (raw, parsed, fit, h, "ok")
            except Exception as e:
                logger.error("[scheduler] AI error on job: %s", e)
                return (raw, None, None, h, "error")

    # ── Foreground: process batches of 5 until we have at least one match ────
    sem_fg     = asyncio.Semaphore(2)   # reduced to avoid 429s
    qualifying: list[Job] = []
    rest       = list(filtered)

    while rest and not qualifying:
        batch = rest[:5]
        rest  = rest[5:]
        logger.info("[scheduler] foreground batch: %d jobs (%d remaining)", len(batch), len(rest))
        results = await asyncio.gather(*[_analyze_one(raw, sem_fg) for raw in batch])
        for result in results:
            await _save_job_result(*result, user=user, qualifying=qualifying)

    if not qualifying:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=f"No new jobs above your {user.min_fit_score}% threshold this time. Try 🎛️ Edit Filters to lower it.",
        )
        logger.info("[scheduler] no qualifying jobs for user=%s", user.telegram_id)
        return 0

    # ── Fire background task for whatever is left ─────────────────────────────
    if rest:
        asyncio.create_task(
            _analyze_remaining(rest, user, profile, skill_keywords, skill_tokens, ai, bot=bot)
        )

    # ── Send first-batch summary + first card ─────────────────────────────────
    qualifying.sort(key=lambda j: j.fit_score or 0, reverse=True)
    count = len(qualifying)

    bg_note = f" — checking {len(rest)} more in the background" if rest else ""
    await bot.send_message(
        chat_id=user.telegram_id,
        text=(
            f"✅ <b>{count} match{'es' if count != 1 else ''}</b> so far{bg_note}!\n\n"
            f"Act on each card and the next one follows automatically 👇"
        ),
        parse_mode="HTML",
    )

    first     = qualifying[0]
    first_fit = (first.parsed or {}).get("_fit", {})
    card_text = _build_card_text(first, first.parsed or {}, first_fit)
    fallback  = first.url or f"https://www.google.com/search?q={first.title.replace(' ', '+')}"

    await bot.send_message(
        chat_id=user.telegram_id,
        text=card_text,
        parse_mode="HTML",
        reply_markup=job_card_keyboard(first.id, fallback),
    )

    logger.info("[scheduler] foreground done — user=%s  %d match(es), %d still in background",
                user.telegram_id, count, len(rest))
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
        parse_mode="HTML",
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
        if not is_manual:
            # Timezone gate: only process at 08:00 and 18:00 in the user's local time
            tz_name = getattr(user, "timezone", None) or "America/Vancouver"
            try:
                local_hour = datetime.now(ZoneInfo(tz_name)).hour
            except Exception:
                local_hour = datetime.utcnow().hour
            if local_hour not in (8, 18):
                continue

            # Inactivity gate: skip if user hasn't interacted for > 3 weekdays
            # Only applies once last_active is tracked (None = new column, skip check)
            if user.last_active is not None:
                weekday_inactive = _weekday_days_since(user.last_active)
                if weekday_inactive > 3:
                    last_warned_str = (user.filters or {}).get("inactivity_warned_at")
                    last_warned = None
                    if last_warned_str:
                        try:
                            last_warned = datetime.fromisoformat(last_warned_str)
                        except Exception:
                            pass
                    if last_warned and (datetime.utcnow() - last_warned).days < 5:
                        logger.info("[scheduler] skipping inactive user=%s (warned recently)", user.telegram_id)
                        continue
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text="You've been inactive for a few days — send any message or tap 🔍 Fetch Jobs to resume auto-scraping.",
                    )
                    new_filters = {**(user.filters or {}), "inactivity_warned_at": datetime.utcnow().isoformat()}
                    async with AsyncSessionLocal() as session:
                        async with session.begin():
                            await session.execute(
                                update(User).where(User.id == user.id).values(filters=new_filters)
                            )
                    logger.info("[scheduler] inactivity warning sent to user=%s", user.telegram_id)
                    continue

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
    Build the APScheduler instance with a single hourly tick (Mon–Fri).
    Per-user timezone checks inside run_scrape_cycle fire at 08:00 and 18:00 local time.
    Call scheduler.start() to activate, scheduler.shutdown() to stop.
    """
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_scrape_cycle, "cron", minute=0, day_of_week="mon-fri",
        id="scrape_hourly", name="Hourly scrape tick (Mon–Fri)",
        args=[bot, ai], replace_existing=True,
    )

    logger.info("[scheduler] built — hourly tick (Mon–Fri), fires at 08:00 and 18:00 per user timezone")
    return scheduler
