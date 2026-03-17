"""
HireLoop bot entry point.

Run with:
    python bot/main.py
    (from project root with .venv active)
"""

import logging
import os
import sys
import warnings

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# Suppress PTB per_message warning
warnings.filterwarnings("ignore", message=".*per_message.*", category=UserWarning)

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, TypeHandler, filters
from telegram.ext import ApplicationHandlerStop

from ai.factory import AIFactory
from ai.service import HireLoopAI
from bot.onboarding import build_onboarding_handler
from bot.handlers.add_skills import build_add_skills_handler
from bot.handlers.settings import get_settings_handlers
from bot.handlers.skill_verify import (
    build_skill_verify_handler,
    get_job_card_handlers,
    get_jobs_command_handler,
    cmd_pending_jobs,
)
from bot.keyboards import MAIN_KEYBOARD
from db.models import Base
from db.session import engine
from jobs.scheduler import build_scheduler, run_scrape_cycle, _get_user_profile, _build_card_text
from jobs.parser import fetch_jd_from_url

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.DEBUG,
)
# Silence noisy third-party loggers — we only want HireLoop internals at DEBUG
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("groq").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("JobSpy").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def cmd_fetch_now(update: Update, context) -> None:
    """Trigger an immediate scrape for the calling user."""
    tg_id = str(update.effective_user.id)
    msg = await update.message.reply_text("Searching for jobs now... this may take a minute.")
    ai = context.application.bot_data["ai"]
    try:
        notified = await run_scrape_cycle(context.bot, ai, telegram_id=tg_id)
        if notified:
            await msg.edit_text(f"Done! Found {notified} new job{'s' if notified != 1 else ''}.")
        else:
            await msg.edit_text("Done! No new jobs right now. Try again later or adjust your filters with 🎛️ Edit Filters.")
    except Exception as e:
        logger.error("fetchnow error for user=%s: %s", tg_id, e, exc_info=True)
        await msg.edit_text("Something went wrong. Check the logs.")


async def _handle_url_paste(update: Update, context, url: str) -> None:
    """Parse a job from a pasted URL and send a job card."""
    import uuid
    from datetime import datetime
    from sqlalchemy import select
    from db.models import Job, User
    from db.session import AsyncSessionLocal
    from jobs.filters import url_hash
    from bot.keyboards import job_card_keyboard

    tg_id = str(update.effective_user.id)
    ai = context.application.bot_data["ai"]
    msg = await update.message.reply_text("Fetching job details from that link...")

    try:
        jd_text = await fetch_jd_from_url(url)
    except Exception as e:
        logger.error("URL fetch failed for %s: %s", url, e)
        await msg.edit_text("Couldn't fetch that link. Make sure it's a public job posting URL.")
        return

    await msg.edit_text("Got it! Analyzing fit...")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = result.scalar_one_or_none()

    if not user:
        await msg.edit_text("Finish onboarding first with /start.")
        return

    async with AsyncSessionLocal() as session:
        profile = await _get_user_profile(user.id, session)

    try:
        parsed = await ai.parse_job(jd_text)
        fit    = await ai.analyze_fit(parsed, profile)
    except Exception as e:
        logger.error("AI error for URL job: %s", e)
        await msg.edit_text("AI analysis failed. Try again in a moment.")
        return

    job_url = url
    job = Job(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title=parsed.get("title") or "Unknown",
        company=parsed.get("company") or "Unknown",
        url=job_url,
        url_hash=url_hash(job_url),
        raw_jd=jd_text[:10_000],
        parsed={**parsed, "_fit": fit},
        fit_score=fit.get("fit_score", 0),
        cover_letter_required=bool(parsed.get("requires_cover_letter")),
        status="pending",
        created_at=datetime.utcnow(),
    )
    async with AsyncSessionLocal() as session:
        async with session.begin():
            session.add(job)

    card_text = _build_card_text(job, parsed, fit)
    await msg.delete()
    await update.message.reply_text(
        card_text,
        parse_mode="Markdown",
        reply_markup=job_card_keyboard(job.id, job_url),
    )


async def handle_keyboard_buttons(update, context):
    """Route persistent reply keyboard button taps to the right handler."""
    from bot.handlers.settings import cmd_skills, cmd_settings, cmd_filters, cmd_pause
    text = update.message.text

    routes = {
        "📊 My Skills":    cmd_skills,
        "⚙️ Settings":     cmd_settings,
        "🎛️ Edit Filters": cmd_filters,
        "⏸ Pause Agent":  cmd_pause,
        "📋 Pending Jobs": cmd_pending_jobs,
        "🔍 Fetch Jobs":   cmd_fetch_now,
    }
    handler = routes.get(text)
    if handler:
        await handler(update, context)
    elif text.startswith("http://") or text.startswith("https://"):
        await _handle_url_paste(update, context, text)
    else:
        await update.message.reply_text(
            "Use the keyboard buttons or type a command.\n/help for the full list."
        )


def _build_allowlist(raw: str) -> tuple[set[int], set[str]] | None:
    """Parse ALLOWED_TELEGRAM_IDS from env.
    Accepts numeric IDs and @usernames. Returns None = open to everyone."""
    if not raw.strip():
        return None
    ids: set[int] = set()
    usernames: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
        elif part.startswith("@"):
            usernames.add(part[1:].lower())
        elif part:
            usernames.add(part.lower())
    if not ids and not usernames:
        return None
    return (ids, usernames)


async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def main():
    import asyncio
    asyncio.get_event_loop().run_until_complete(_init_db())

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    allowed = _build_allowlist(os.getenv("ALLOWED_TELEGRAM_IDS", ""))
    if allowed:
        ids, usernames = allowed
        logger.info(f"Access restricted to {len(ids)} ID(s) + {len(usernames)} username(s)")
    else:
        logger.warning("ALLOWED_TELEGRAM_IDS not set — bot is open to anyone")

    # Build tiered AI
    fast = AIFactory.create_fast()
    quality = AIFactory.create_quality()
    ai = HireLoopAI(fast_provider=fast, quality_provider=quality)
    logger.info(f"AI ready — fast: {fast.provider_name}, quality: {quality.provider_name}")

    async def _post_init(application):
        scheduler = build_scheduler(application.bot, ai)
        scheduler.start()
        application.bot_data["scheduler"] = scheduler
        logger.info("APScheduler started — scrapes at 08:00 and 18:00 daily")

    async def _post_stop(application):
        scheduler = application.bot_data.get("scheduler")
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("APScheduler stopped")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_stop(_post_stop)
        .build()
    )
    app.bot_data["ai"] = ai

    # Allowlist gate — runs before every handler (group -1)
    if allowed:
        allowed_ids, allowed_usernames = allowed

        async def auth_gate(update: Update, context) -> None:
            user = update.effective_user
            if not user:
                raise ApplicationHandlerStop
            uid = user.id
            uname = (user.username or "").lower()
            if uid not in allowed_ids and uname not in allowed_usernames:
                if update.message:
                    await update.message.reply_text("This bot is private.")
                elif update.callback_query:
                    await update.callback_query.answer("Not authorized.", show_alert=True)
                raise ApplicationHandlerStop

        app.add_handler(TypeHandler(Update, auth_gate), group=-1)

    # Register handlers — order matters (ConversationHandlers first)
    app.add_handler(build_onboarding_handler())
    app.add_handler(build_add_skills_handler())
    app.add_handler(build_skill_verify_handler())

    for h in get_settings_handlers():
        app.add_handler(h)

    app.add_handler(get_jobs_command_handler())
    app.add_handler(CommandHandler("fetchnow", cmd_fetch_now))

    for h in get_job_card_handlers():
        app.add_handler(h)

    # Catch-all for persistent keyboard taps
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard_buttons))

    logger.info("HireLoop bot starting — press Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
