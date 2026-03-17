"""
6-state onboarding wizard triggered by /start.

States:
  WELCOME              → "Let's go" button
  UPLOAD_RESUME        → accept PDF/DOCX files, [Done uploading]
  CONFIRM_SKILLS       → iterate medium/low skills one-by-one
  CONFIRM_SKILL_CONTEXT→ sub-state: collect user context text for a skill
  SET_FILTERS_ROLE     → text: job role(s)
  SET_FILTERS_LOCATION → text or skip
  SET_FILTERS_REMOTE   → button choice
  SET_FILTERS_SALARY   → text (number) or skip
  SET_FILTERS_BLACKLIST→ text or skip
  SET_FREQUENCY        → button choice
  SET_FIT_SCORE        → button choice → save to DB → DONE
"""

import asyncio
import io
import logging
import re
import time
import uuid
from datetime import datetime

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.keyboards import (
    MAIN_KEYBOARD,
    _ALL_SITES,
    done_uploading_keyboard,
    fit_score_keyboard,
    frequency_keyboard,
    location_keyboard,
    remote_keyboard,
    returning_user_keyboard,
    sites_keyboard,
    skip_keyboard,
    skill_confirm_keyboard,
    welcome_keyboard,
)
from db.models import SkillEvidence, SkillNode, User
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ── State constants ─────────────────────────────────────────────────────────
(
    WELCOME,
    UPLOAD_RESUME,
    CONFIRM_SKILLS,
    CONFIRM_SKILL_CONTEXT,
    SET_FILTERS_ROLE,
    SET_FILTERS_LOCATION,
    SET_FILTERS_COUNTRY,
    SET_FILTERS_REMOTE,
    SET_FILTERS_SITES,
    SET_FILTERS_SALARY,
    SET_FILTERS_BLACKLIST,
    SET_FREQUENCY,
    SET_FIT_SCORE,
    RETURNING_USER,
) = range(14)


# ── Skill normalization ─────────────────────────────────────────────────────

# Suffixes that don't add meaning — "Drupal CMS" and "Drupal" are the same skill
_REDUNDANT_SUFFIXES = re.compile(
    r"\s+(cms|framework|db|database|server|sdk|api|platform|library|lang|language)$",
    re.IGNORECASE,
)
# ".js" / " js" tail — "Vue.js" → "vue", "Vue JS" → "vue"
_JS_SUFFIX = re.compile(r"[.\s]js$", re.IGNORECASE)


def _normalize_skill(name: str) -> str:
    """Return a canonical key for deduplication.

    Examples:
        "Drupal CMS"  → "drupal"
        "Vue.js"      → "vue"
        "Node JS"     → "node"
        "PostgreSQL"  → "postgresql"
    """
    n = name.strip().lower()
    n = _JS_SUFFIX.sub("", n)
    n = _REDUNDANT_SUFFIXES.sub("", n)
    return n.strip()


# ── File text extraction ────────────────────────────────────────────────────

def _extract_pdf(data: bytes) -> str:
    import PyPDF2
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


# ── DB helpers ──────────────────────────────────────────────────────────────

async def _save_onboarding_to_db(telegram_id: str, name: str, confirmed_skills: list, filters: dict, notify_freq: str, min_fit_score: int):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            from sqlalchemy import select
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()

            if not user:
                user = User(
                    id=str(uuid.uuid4()),
                    telegram_id=telegram_id,
                    name=name,
                )
                session.add(user)
                await session.flush()

            user.filters = filters
            user.notify_freq = notify_freq
            user.min_fit_score = min_fit_score
            user.onboarded = True

            # Wipe old skill nodes for this user (re-onboarding)
            old_nodes = await session.execute(
                select(SkillNode).where(SkillNode.user_id == user.id)
            )
            for node in old_nodes.scalars():
                await session.delete(node)
            await session.flush()

            # Insert new skill nodes + evidence
            for skill in confirmed_skills:
                node = SkillNode(
                    user_id=user.id,
                    skill_name=skill["skill_name"],
                    status=skill["status"],
                    confidence=skill.get("confidence", "medium"),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                session.add(node)
                await session.flush()

                # Save evidence from: (a) user-typed context, or (b) AI-extracted resume evidence
                context_text = skill.get("user_context", "") or skill.get("evidence", "")
                if context_text:
                    evidence = SkillEvidence(
                        skill_node_id=node.id,
                        user_context=context_text,
                        source="telegram" if skill["status"] == "verified_attested" else "resume",
                    )
                    session.add(evidence)


# ── Skill flow helpers ──────────────────────────────────────────────────────

async def _show_next_pending_skill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show next unconfirmed skill, or advance to filters when done."""
    pending = context.user_data.get("pending_skills", [])
    idx = context.user_data.get("pending_idx", 0)

    msg = update.callback_query.message if update.callback_query else update.message

    if idx >= len(pending):
        count = len(context.user_data.get("confirmed_skills", []))
        await msg.reply_text(
            f"Skills locked in — {count} verified skill(s) in your graph.\n\n"
            "Now let's set your job filters."
        )
        return await _ask_role(msg)

    skill = pending[idx]
    conf_label = {"high": "high confidence", "medium": "medium confidence", "low": "low confidence"}.get(
        skill.get("confidence", "medium"), "confidence unknown"
    )
    await msg.reply_text(
        f"Skill {idx + 1} of {len(pending)}\n\n"
        f"*{skill['skill_name']}* ({conf_label})\n\n"
        "Have you used this in real work?",
        parse_mode="Markdown",
        reply_markup=skill_confirm_keyboard(idx),
    )
    return CONFIRM_SKILLS


async def _ask_role(msg) -> int:
    await msg.reply_text(
        "What job title(s) are you targeting?\n\n"
        "Type one or more *job titles* separated by commas — these become your search keywords, "
        "so keep them short and specific.\n\n"
        "✅ `Software Engineer, AI Engineer, Full Stack Developer`\n"
        "❌ `I am looking for software roles in AI...`",
        parse_mode="Markdown",
    )
    return SET_FILTERS_ROLE


# ── State: WELCOME ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_user = update.effective_user
    tg_id = str(tg_user.id)

    # Check if already onboarded — don't wipe their data
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, func
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if user and user.onboarded:
            skill_count_result = await session.execute(
                select(func.count()).select_from(SkillNode).where(SkillNode.user_id == user.id)
            )
            skill_count = skill_count_result.scalar()
            f = user.filters or {}
            await update.message.reply_text(
                f"Welcome back! Your profile is active.\n\n"
                f"Skills in graph: {skill_count}\n"
                f"Role: {f.get('role', 'not set')}\n"
                f"Min fit score: {user.min_fit_score}%\n\n"
                "What would you like to do?",
                reply_markup=returning_user_keyboard(),
            )
            return RETURNING_USER

    context.user_data.clear()
    await update.message.reply_text(
        f"Hey {tg_user.first_name}! Welcome to *HireLoop*.\n\n"
        "I'll find jobs, score your fit, and generate tailored resumes — "
        "you just approve and apply.\n\n"
        "Let's build your profile. It takes about 2 minutes.",
        parse_mode="Markdown",
        reply_markup=welcome_keyboard(),
    )
    return WELCOME


async def returning_user_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "returning_filters":
        await query.edit_message_text("Let's update your filters.")
        return await _ask_role(query.message)

    elif query.data == "returning_resume":
        context.user_data.clear()
        context.user_data["resume_texts"] = []
        await query.edit_message_text(
            "Send your updated resume — PDF or Word doc.\n"
            "Your existing skills will be replaced with the new ones.",
        )
        context.user_data["done_btn_msg_id"] = None
        return UPLOAD_RESUME

    elif query.data == "returning_addskills":
        await query.edit_message_text(
            "Tap \u2018\ud83d\udcce Add Resume\u2019 on the keyboard below, or use /addskills to add skills manually."
        )
        return ConversationHandler.END

    else:  # returning_cancel
        await query.edit_message_text("All good, nothing changed.")
        return ConversationHandler.END


async def welcome_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Send me your resume — PDF or Word doc.\n\n"
        "You can send up to 4 files (different versions are fine).",
        parse_mode="Markdown",
    )
    context.user_data["done_btn_msg_id"] = None
    return UPLOAD_RESUME


# ── State: UPLOAD_RESUME ────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    fname = (doc.file_name or "").lower()

    if not (fname.endswith(".pdf") or fname.endswith(".doc") or fname.endswith(".docx")):
        await update.message.reply_text("Please send a PDF or Word (.docx) file.")
        return UPLOAD_RESUME

    if len(context.user_data.get("resume_texts", [])) >= 4:
        await update.message.reply_text("4 resumes is the max. Tap Done to continue.")
        return UPLOAD_RESUME

    await update.message.reply_text("Reading your resume...")

    file = await doc.get_file()
    data = bytes(await file.download_as_bytearray())

    try:
        text = _extract_pdf(data) if fname.endswith(".pdf") else _extract_docx(data)
    except Exception as e:
        logger.error(f"File extraction failed: {e}")
        await update.message.reply_text("Couldn't read that file. Try a different format.")
        return UPLOAD_RESUME

    context.user_data.setdefault("resume_texts", []).append(text)
    count = len(context.user_data["resume_texts"])

    # Delete previous "Done uploading" button message so only one exists at a time
    prev_msg_id = context.user_data.get("done_btn_msg_id")
    if prev_msg_id:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=prev_msg_id
            )
        except Exception:
            pass

    sent = await update.message.reply_text(
        f"Got it! {count} resume{'s' if count > 1 else ''} uploaded.\n"
        "Send another or tap Done.",
        reply_markup=done_uploading_keyboard(),
    )
    context.user_data["done_btn_msg_id"] = sent.message_id
    return UPLOAD_RESUME


async def done_uploading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    resume_texts = context.user_data.get("resume_texts", [])
    if not resume_texts:
        await query.edit_message_text(
            "Please upload at least one resume first.",
            reply_markup=done_uploading_keyboard(),
        )
        return UPLOAD_RESUME

    n = len(resume_texts)
    ai = context.application.bot_data["ai"]
    t_total = time.monotonic()

    await query.edit_message_text(
        f"Got it! Parsing {'your resume' if n == 1 else f'all {n} resumes in parallel'}...\n"
        "This usually takes ~30 seconds — hang tight!"
    )

    logger.info("[done_uploading] starting resume analysis — %d file(s)", n)

    async def _parse_one(i: int, text: str) -> list:
        logger.info("[done_uploading] parsing resume %d/%d — %d chars", i, n, len(text))
        t_file = time.monotonic()
        try:
            parsed = await ai.parse_resume(text)
            raw_skills = parsed.get("skills", [])
            logger.info("[done_uploading] resume %d done in %.2fs — %d raw skills", i, time.monotonic() - t_file, len(raw_skills))
            return raw_skills
        except Exception as e:
            logger.error("[done_uploading] resume %d parse error (%.2fs): %s", i, time.monotonic() - t_file, e)
            return []

    results = await asyncio.gather(*[_parse_one(i, t) for i, t in enumerate(resume_texts, 1)])
    all_skills = [s for batch in results for s in batch]

    logger.info("[done_uploading] all files parsed — %d total raw skills before dedup", len(all_skills))

    await query.edit_message_text(
        f"Resume{'s' if n > 1 else ''} parsed! Found {len(all_skills)} skills.\n"
        "Building your skill graph..."
    )

    # Deduplicate — normalize name first so "Drupal" and "Drupal CMS" collapse to one entry.
    # Keep highest confidence per normalized key; preserve the original skill_name from the
    # higher-confidence entry so the user sees a clean name.
    conf_rank = {"high": 3, "medium": 2, "low": 1}
    skill_map: dict = {}
    for s in all_skills:
        key = _normalize_skill(s["skill_name"])
        existing_rank = conf_rank.get(skill_map[key]["confidence"], 0) if key in skill_map else -1
        new_rank = conf_rank.get(s["confidence"], 0)
        if key not in skill_map or new_rank > existing_rank:
            if key in skill_map:
                logger.debug("[done_uploading] dedup merge: '%s' supersedes '%s' (key=%s, %s > %s)",
                             s["skill_name"], skill_map[key]["skill_name"], key,
                             s["confidence"], skill_map[key]["confidence"])
            skill_map[key] = s
        else:
            logger.debug("[done_uploading] dedup drop: '%s' (key=%s) already covered by '%s'",
                         s["skill_name"], key, skill_map[key]["skill_name"])
    merged = list(skill_map.values())
    logger.info("[done_uploading] after dedup: %d unique skills (dropped %d duplicates) — total %.2fs",
                len(merged), len(all_skills) - len(merged), time.monotonic() - t_total)

    if not merged:
        await query.edit_message_text(
            "Couldn't extract skills from the resume. Let's skip to filters for now."
        )
        context.user_data["confirmed_skills"] = []
        context.user_data["pending_skills"] = []
        context.user_data["pending_idx"] = 0
        return await _ask_role(query.message)

    high = [s for s in merged if s["confidence"] == "high"]
    pending = [s for s in merged if s["confidence"] in ("medium", "low")]

    # Carry AI-extracted evidence as the initial user_context so it feeds SkillEvidence
    confirmed = [{**s, "status": "verified_resume", "user_context": s.get("evidence", "")} for s in high]
    context.user_data["confirmed_skills"] = confirmed
    context.user_data["pending_skills"] = pending
    context.user_data["pending_idx"] = 0

    if high:
        names = ", ".join(s["skill_name"] for s in high)
        await query.edit_message_text(
            f"Impressive! Found *{len(merged)}* skills. Auto-confirmed {len(high)} high-confidence:\n\n"
            f"{names}\n\n"
            f"Now let's verify the other {len(pending)}.",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            f"Found *{len(merged)}* skills to verify.",
            parse_mode="Markdown",
        )

    return await _show_next_pending_skill(update, context)


# ── State: CONFIRM_SKILLS ───────────────────────────────────────────────────

async def skill_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])
    skill = context.user_data["pending_skills"][idx]
    context.user_data["confirmed_skills"].append({**skill, "status": "verified_resume", "user_context": skill.get("evidence", "")})
    await query.edit_message_text(f"Confirmed: {skill['skill_name']} ✅")
    context.user_data["pending_idx"] = idx + 1
    return await _show_next_pending_skill(update, context)


async def skill_context_requested(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])
    skill = context.user_data["pending_skills"][idx]
    context.user_data["context_for_idx"] = idx
    await query.edit_message_text(
        f"Tell me about your *{skill['skill_name']}* experience:\n\n"
        "One sentence — company, what you built, how long.\n\n"
        "Example: _Built Kafka pipelines at Acme Corp for 8 months, async order processing_",
        parse_mode="Markdown",
    )
    return CONFIRM_SKILL_CONTEXT


async def handle_skill_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["context_for_idx"]
    skill = context.user_data["pending_skills"][idx]
    user_context = update.message.text.strip()
    context.user_data["confirmed_skills"].append({
        **skill, "status": "verified_attested", "user_context": user_context
    })
    await update.message.reply_text(f"Saved context for {skill['skill_name']} ✅")
    context.user_data["pending_idx"] = idx + 1
    return await _show_next_pending_skill(update, context)


async def skill_removed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])
    skill = context.user_data["pending_skills"][idx]
    await query.edit_message_text(f"Removed: {skill['skill_name']}")
    context.user_data["pending_idx"] = idx + 1
    return await _show_next_pending_skill(update, context)


async def skill_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])

    # Undo: remove previous skill from confirmed_skills if it was added there
    prev_skill = context.user_data["pending_skills"][idx - 1]
    context.user_data["confirmed_skills"] = [
        s for s in context.user_data.get("confirmed_skills", [])
        if s["skill_name"].lower() != prev_skill["skill_name"].lower()
    ]

    context.user_data["pending_idx"] = idx - 1
    await query.edit_message_text(f"Going back to {prev_skill['skill_name']}...")
    return await _show_next_pending_skill(update, context)


# ── State: SET_FILTERS ──────────────────────────────────────────────────────

async def set_filters_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault("filters", {})["role"] = update.message.text.strip()
    context.user_data["filters"]["locations"] = []  # start fresh list
    await update.message.reply_text("Remote preference?", reply_markup=remote_keyboard())
    return SET_FILTERS_REMOTE


async def set_filters_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a location — add it to the list and prompt for more."""
    loc = update.message.text.strip()
    locs: list = context.user_data.setdefault("filters", {}).setdefault("locations", [])
    locs.append(loc)
    count = len(locs)
    await update.message.reply_text(
        f"Added: *{loc}*\n\n"
        f"Locations so far: {', '.join(locs)}\n\n"
        "Send another location or tap Done.",
        parse_mode="Markdown",
        reply_markup=location_keyboard(count),
    )
    return SET_FILTERS_LOCATION


async def done_locations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User tapped Done (or skip with 0 locations) — proceed to job boards."""
    query = update.callback_query
    await query.answer()
    locs = context.user_data.get("filters", {}).get("locations", [])
    if locs:
        await query.edit_message_text(f"Locations: {', '.join(locs)}")
    else:
        await query.edit_message_text("Location: any")
    return await _ask_sites(query.message, context)


async def set_filters_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    country = update.message.text.strip()
    context.user_data["filters"]["country"] = country
    await update.message.reply_text(
        f"Which *city or region* in {country}?\n\n"
        "Type one and send — you can add multiple.\n"
        f"Examples: Toronto, ON · Vancouver · Calgary\n\n"
        "Want nationwide? Tap Skip.",
        parse_mode="Markdown",
        reply_markup=location_keyboard(0),
    )
    return SET_FILTERS_LOCATION


async def skip_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.setdefault("filters", {})["country"] = ""
    await query.edit_message_text("Country: any")
    await query.message.reply_text(
        "Which *city or region* are you targeting?\n\n"
        "Type one and send — you can add multiple.\n"
        "Examples: Toronto, ON · Vancouver · New York · London\n\n"
        "Want no city filter? Tap Skip.",
        parse_mode="Markdown",
        reply_markup=location_keyboard(0),
    )
    return SET_FILTERS_LOCATION


async def set_filters_remote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mapping = {"remote_yes": "remote", "remote_hybrid": "hybrid", "remote_any": "any"}
    context.user_data["filters"]["remote"] = mapping.get(query.data, "any")
    await query.edit_message_text(f"Remote: {context.user_data['filters']['remote']}")
    await query.message.reply_text(
        "Which country are you looking for jobs in?\n\n"
        "This sets the job board (e.g. Canada → indeed.ca, USA → indeed.com).\n\n"
        "Examples: Canada · USA · UK · Australia · India\n\n"
        "Tap Skip for global results.",
        reply_markup=skip_keyboard("country"),
    )
    return SET_FILTERS_COUNTRY


async def _ask_sites(message, context) -> int:
    # Initialise all sites as selected (default)
    context.user_data["filters"]["sites"] = list(_ALL_SITES)
    await message.reply_text(
        "Which job boards should I scrape?\n\n"
        "⚡ = fast   🐢 = slower but more listings\n"
        "All are selected by default. Tap to toggle off.",
        reply_markup=sites_keyboard(context.user_data["filters"]["sites"]),
    )
    return SET_FILTERS_SITES


async def toggle_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle a site on/off and refresh the keyboard."""
    query = update.callback_query
    await query.answer()
    site = query.data.replace("toggle_site_", "")
    selected: list = context.user_data.setdefault("filters", {}).setdefault("sites", list(_ALL_SITES))
    if site in selected:
        if len(selected) > 1:  # always keep at least one
            selected.remove(site)
        else:
            await query.answer("Keep at least one site selected.", show_alert=True)
            return SET_FILTERS_SITES
    else:
        selected.append(site)
    await query.edit_message_reply_markup(reply_markup=sites_keyboard(selected))
    return SET_FILTERS_SITES


async def sites_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    selected = context.user_data.get("filters", {}).get("sites", list(_ALL_SITES))
    await query.edit_message_text(f"Job boards: {', '.join(selected)}")
    await query.message.reply_text(
        "Minimum salary? (annual, in your currency)\n\nExample: 80000",
        reply_markup=skip_keyboard("salary"),
    )
    return SET_FILTERS_SALARY


async def set_filters_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", "").replace("$", "")
    try:
        context.user_data["filters"]["min_salary"] = int(float(text))
    except ValueError:
        await update.message.reply_text("Please enter a number, e.g. 80000")
        return SET_FILTERS_SALARY
    await update.message.reply_text(
        "Any companies or industries to blacklist?\n\nExample: Amazon, gambling, crypto",
        reply_markup=skip_keyboard("blacklist"),
    )
    return SET_FILTERS_BLACKLIST


async def skip_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.setdefault("filters", {})["min_salary"] = 0
    await query.edit_message_text("Min salary: none")
    await query.message.reply_text(
        "Any companies or industries to blacklist?\n\nExample: Amazon, gambling, crypto",
        reply_markup=skip_keyboard("blacklist"),
    )
    return SET_FILTERS_BLACKLIST


async def set_filters_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    blacklist = [x.strip() for x in raw.split(",") if x.strip()]
    context.user_data["filters"]["blacklist"] = blacklist
    await update.message.reply_text("How often should I notify you?", reply_markup=frequency_keyboard())
    return SET_FREQUENCY


async def skip_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.setdefault("filters", {})["blacklist"] = []
    await query.edit_message_text("Blacklist: none")
    await query.message.reply_text("How often should I notify you?", reply_markup=frequency_keyboard())
    return SET_FREQUENCY


# ── State: SET_FREQUENCY ────────────────────────────────────────────────────

async def set_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mapping = {"freq_daily": "daily", "freq_realtime": "realtime", "freq_twice": "twice_daily"}
    freq = mapping.get(query.data, "daily")
    context.user_data["notify_freq"] = freq
    await query.edit_message_text(f"Notification frequency: {freq}")
    await query.message.reply_text(
        "Minimum fit score to notify you? (Lower = more jobs, higher = better matches)",
        reply_markup=fit_score_keyboard(),
    )
    return SET_FIT_SCORE


# ── State: SET_FIT_SCORE → save to DB ──────────────────────────────────────

async def set_fit_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    score = int(query.data.split("_")[1])
    context.user_data["min_fit_score"] = score
    await query.edit_message_text(f"Min fit score: {score}%")

    await query.message.reply_text("Saving your profile...")

    user = update.effective_user
    try:
        await _save_onboarding_to_db(
            telegram_id=str(user.id),
            name=user.first_name or user.username or "User",
            confirmed_skills=context.user_data.get("confirmed_skills", []),
            filters=context.user_data.get("filters", {}),
            notify_freq=context.user_data.get("notify_freq", "daily"),
            min_fit_score=score,
        )
    except Exception as e:
        logger.error(f"DB save failed during onboarding: {e}")
        await query.message.reply_text("Something went wrong saving your profile. Try /start again.")
        return ConversationHandler.END

    skill_count = len(context.user_data.get("confirmed_skills", []))
    filters = context.user_data.get("filters", {})

    await query.message.reply_text(
        f"All set! Here's your profile summary:\n\n"
        f"Role: {filters.get('role', 'any')}\n"
        f"Location: {filters.get('location', 'any')}\n"
        f"Remote: {filters.get('remote', 'any')}\n"
        f"Min salary: {filters.get('min_salary', 0) or 'none'}\n"
        f"Skills in graph: {skill_count}\n"
        f"Min fit score: {score}%\n\n"
        "I'll start finding jobs for you. Use the keyboard below to manage your agent.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ── Cancellation ────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled. Run /start to begin again.")
    return ConversationHandler.END


# ── ConversationHandler builder ─────────────────────────────────────────────

def build_onboarding_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            RETURNING_USER: [
                CallbackQueryHandler(returning_user_choice, pattern=r"^returning_"),
            ],
            WELCOME: [
                CallbackQueryHandler(welcome_clicked, pattern="onboard_start"),
            ],
            UPLOAD_RESUME: [
                MessageHandler(filters.Document.ALL, handle_document),
                CallbackQueryHandler(done_uploading, pattern="done_uploading"),
            ],
            CONFIRM_SKILLS: [
                CallbackQueryHandler(skill_confirmed,         pattern=r"^skill_confirm_\d+$"),
                CallbackQueryHandler(skill_context_requested, pattern=r"^skill_context_\d+$"),
                CallbackQueryHandler(skill_removed,           pattern=r"^skill_remove_\d+$"),
                CallbackQueryHandler(skill_back,              pattern=r"^skill_back_\d+$"),
            ],
            CONFIRM_SKILL_CONTEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_skill_context),
            ],
            SET_FILTERS_ROLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_filters_role),
            ],
            SET_FILTERS_LOCATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_filters_location),
                CallbackQueryHandler(done_locations, pattern="done_locations"),
            ],
            SET_FILTERS_COUNTRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_filters_country),
                CallbackQueryHandler(skip_country, pattern="skip_country"),
            ],
            SET_FILTERS_REMOTE: [
                CallbackQueryHandler(set_filters_remote, pattern=r"^remote_"),
            ],
            SET_FILTERS_SITES: [
                CallbackQueryHandler(toggle_site,  pattern=r"^toggle_site_"),
                CallbackQueryHandler(sites_done,   pattern="^sites_done$"),
            ],
            SET_FILTERS_SALARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_filters_salary),
                CallbackQueryHandler(skip_salary, pattern="skip_salary"),
            ],
            SET_FILTERS_BLACKLIST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_filters_blacklist),
                CallbackQueryHandler(skip_blacklist, pattern="skip_blacklist"),
            ],
            SET_FREQUENCY: [
                CallbackQueryHandler(set_frequency, pattern=r"^freq_"),
            ],
            SET_FIT_SCORE: [
                CallbackQueryHandler(set_fit_score, pattern=r"^fit_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
