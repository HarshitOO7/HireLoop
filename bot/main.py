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
from telegram.ext import Application, MessageHandler, TypeHandler, filters
from telegram.ext import ApplicationHandlerStop

from ai.factory import AIFactory
from ai.service import HireLoopAI
from bot.onboarding import build_onboarding_handler
from bot.handlers.add_skills import build_add_skills_handler
from bot.handlers.settings import get_settings_handlers
from bot.keyboards import MAIN_KEYBOARD
from db.models import Base
from db.session import engine

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
logger = logging.getLogger(__name__)


async def handle_keyboard_buttons(update, context):
    """Route persistent reply keyboard button taps to the right handler."""
    from bot.handlers.settings import cmd_skills, cmd_settings, cmd_filters, cmd_pause
    text = update.message.text

    routes = {
        "📊 My Skills":    cmd_skills,
        "⚙️ Settings":     cmd_settings,
        "🎛️ Edit Filters": cmd_filters,
        "⏸ Pause Agent":  cmd_pause,
    }
    handler = routes.get(text)
    if handler:
        await handler(update, context)
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

    app = Application.builder().token(token).build()
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

    # Register handlers — order matters (ConversationHandler first)
    app.add_handler(build_onboarding_handler())
    app.add_handler(build_add_skills_handler())

    for h in get_settings_handlers():
        app.add_handler(h)

    # Catch-all for persistent keyboard taps
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard_buttons))

    logger.info("HireLoop bot starting — press Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
