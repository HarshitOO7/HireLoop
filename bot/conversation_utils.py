"""Shared helpers for robust ConversationHandler behavior.

Every multi-step input flow in the bot should use these so the experience is
consistent and hard to get stuck in:

- `TEXT_INPUT`     — text-input filter that ignores commands and main-menu
                     buttons, so tapping a button mid-flow is never mistaken
                     for the user's answer.
- `escape_fallbacks()` — fallbacks every input ConversationHandler should add:
                     `/cancel` to bail, and an interrupt handler so tapping any
                     main-menu button cleanly exits (and runs simple actions).
- `on_error`       — global error handler: any unhandled exception replies
                     gracefully instead of dumping "No error handlers registered".
"""
import logging
import re

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.keyboards import MAIN_KEYBOARD

logger = logging.getLogger(__name__)

# Main reply-keyboard button labels — keep in sync with MAIN_KEYBOARD in keyboards.py.
# Simple buttons run a one-shot action; conv-entry buttons open another flow.
SIMPLE_BUTTONS = {
    "📊 My Skills", "⚙️ Settings", "🎛️ Edit Filters",
    "⏸ Pause Agent", "📋 Pending Jobs", "🔍 Fetch Jobs",
}
CONV_ENTRY_BUTTONS = {"📎 Add Resume", "📁 My Apps", "💾 Saved Jobs"}
MAIN_BUTTONS = SIMPLE_BUTTONS | CONV_ENTRY_BUTTONS

# Matches any main-menu button label exactly.
INTERRUPT_REGEX = "^(" + "|".join(re.escape(b) for b in MAIN_BUTTONS) + ")$"

# Free-text input: plain text that is NOT a command and NOT a main-menu button.
TEXT_INPUT = filters.TEXT & ~filters.COMMAND & ~filters.Regex(INTERRUPT_REGEX)


async def universal_cancel(update: Update, context) -> int:
    """Generic /cancel — end the current flow and show the menu."""
    if update.message:
        await update.message.reply_text("✖️ Cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


async def interrupt_handler(update: Update, context) -> int:
    """A main-menu button was tapped during an input flow.

    Exit the current flow. Simple actions run immediately; sub-menus (which open
    their own flow) just prompt a re-tap so we never stack two conversations.
    """
    text = (update.message.text or "").strip() if update.message else ""

    if text in SIMPLE_BUTTONS:
        # Lazy imports: main imports the handler modules, so importing at module
        # load time would create a circular import.
        from bot.handlers.settings import cmd_skills, cmd_settings, cmd_filters, cmd_pause
        from bot.handlers.skill_verify import cmd_pending_jobs
        from bot.main import cmd_fetch_now
        routes = {
            "📊 My Skills":    cmd_skills,
            "⚙️ Settings":     cmd_settings,
            "🎛️ Edit Filters": cmd_filters,
            "⏸ Pause Agent":  cmd_pause,
            "📋 Pending Jobs": cmd_pending_jobs,
            "🔍 Fetch Jobs":   cmd_fetch_now,
        }
        try:
            await routes[text](update, context)
        except Exception:
            logger.exception("interrupt re-dispatch failed for %s", text)
            await update.message.reply_text("✖️ Exited.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text(
            f"✖️ Exited. Tap “{text}” again to open it.", reply_markup=MAIN_KEYBOARD
        )
    return ConversationHandler.END


def escape_fallbacks(cancel=universal_cancel) -> list:
    """Fallbacks every input ConversationHandler should include.

    Pass a flow-specific `cancel` to preserve custom cleanup; otherwise the
    generic cancel is used.
    """
    return [
        CommandHandler("cancel", cancel),
        MessageHandler(filters.Regex(INTERRUPT_REGEX), interrupt_handler),
    ]


async def on_error(update: object, context) -> None:
    """Global error handler — last line of defense for unhandled exceptions."""
    logger.error("Unhandled exception while processing update", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on my side. Please try again, or /cancel to start over."
            )
    except Exception:
        pass
