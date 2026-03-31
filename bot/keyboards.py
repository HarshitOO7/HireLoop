from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup


# ── Onboarding ─────────────────────────────────────────────────────────────

def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Let's go ✅", callback_data="onboard_start"),
    ]])


def returning_user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎛️ Update filters",   callback_data="returning_filters")],
        [InlineKeyboardButton("➕ Add more skills",   callback_data="returning_addskills")],
        [InlineKeyboardButton("📎 Re-upload resume",  callback_data="returning_resume")],
        [InlineKeyboardButton("❌ Nothing, cancel",   callback_data="returning_cancel")],
    ])


def done_uploading_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Done uploading ✅", callback_data="done_uploading"),
    ]])


def skill_confirm_keyboard(idx: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("✅ Confirm", callback_data=f"skill_confirm_{idx}"),
        InlineKeyboardButton("✏️ Add context", callback_data=f"skill_context_{idx}"),
        InlineKeyboardButton("❌ Remove", callback_data=f"skill_remove_{idx}"),
    ]]
    if idx > 0:
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"skill_back_{idx}")])
    return InlineKeyboardMarkup(rows)


def remote_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Remote only", callback_data="remote_yes"),
        InlineKeyboardButton("Hybrid ok", callback_data="remote_hybrid"),
        InlineKeyboardButton("Any", callback_data="remote_any"),
    ]])


def skip_keyboard(step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Skip", callback_data=f"skip_{step}"),
    ]])


def location_keyboard(count: int = 0) -> InlineKeyboardMarkup:
    if count == 0:
        label = "⏭ Skip (no location)"
    else:
        label = f"Done ✅ ({count} location{'s' if count > 1 else ''} added)"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="done_locations")]])


_ALL_SITES = ["indeed", "linkedin", "glassdoor"]
_SITE_LABELS = {
    "indeed":    "Indeed ⚡",
    "linkedin":  "LinkedIn ⚡",
    "glassdoor": "Glassdoor 🐢 slow",
}


def sites_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    """Toggle buttons for each job board. ✓ = currently selected (shown in label only)."""
    rows = []
    for site in _ALL_SITES:
        label = _SITE_LABELS[site] + (" ✓" if site in selected else "")
        rows.append([InlineKeyboardButton(label, callback_data=f"toggle_site_{site}")])
    rows.append([InlineKeyboardButton("Done ✅", callback_data="sites_done")])
    return InlineKeyboardMarkup(rows)


def years_exp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Any (no filter)", callback_data="yrs_any")],
    ])


def frequency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📬 Daily digest", callback_data="freq_daily"),
            InlineKeyboardButton("⚡ Real-time", callback_data="freq_realtime"),
        ],
        [InlineKeyboardButton("2x per day", callback_data="freq_twice")],
    ])


def fit_score_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("50%", callback_data="fit_50"),
        InlineKeyboardButton("60%", callback_data="fit_60"),
        InlineKeyboardButton("70%", callback_data="fit_70"),
        InlineKeyboardButton("80%", callback_data="fit_80"),
    ]])


def add_skills_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📎 Upload resume",       callback_data="addskills_upload")],
        [InlineKeyboardButton("✍️ Add skill manually",  callback_data="addskills_manual")],
        [InlineKeyboardButton("❌ Cancel",               callback_data="addskills_cancel")],
    ])


def add_skill_done_uploading_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Done uploading ✅", callback_data="add_done_uploading"),
    ]])


def add_skill_confirm_keyboard(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes",          callback_data=f"add_confirm_{idx}"),
        InlineKeyboardButton("✏️ Add context",  callback_data=f"add_ctx_{idx}"),
        InlineKeyboardButton("❌ Skip",          callback_data=f"add_remove_{idx}"),
    ]])


# ── Persistent main keyboard (always visible) ──────────────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    ["📎 Add Resume",  "🎛️ Edit Filters"],
    ["📊 My Skills",   "📋 Pending Jobs"],
    ["🔍 Fetch Jobs",  "⚙️ Settings"],
    ["⏸ Pause Agent"],
], resize_keyboard=True)


# ── Job card keyboard ──────────────────────────────────────────────────────

def job_card_keyboard(job_id: str, job_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ I know these",     callback_data=f"job_skills_{job_id}"),
            InlineKeyboardButton("⏭ Skip",              callback_data=f"job_skip_{job_id}"),
        ],
        [
            InlineKeyboardButton("➡️ Generate Anyway", callback_data=f"job_generate_{job_id}"),
        ],
        [
            InlineKeyboardButton("📄 Full JD",          callback_data=f"job_fulljd_{job_id}"),
            InlineKeyboardButton("🔗 Open Link",        url=job_url),
        ],
    ])


def resume_delivery_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Word (.docx)", callback_data=f"deliver_docx_{job_id}"),
            InlineKeyboardButton("📋 PDF",          callback_data=f"deliver_pdf_{job_id}"),
        ],
        [
            InlineKeyboardButton("📦 Both",         callback_data=f"deliver_both_{job_id}"),
            InlineKeyboardButton("⏭ Skip",          callback_data=f"skip_job_{job_id}"),
        ],
    ])


def job_approval_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{job_id}"),
            InlineKeyboardButton("✏️ Edit Resume", callback_data=f"edit_{job_id}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip_job_{job_id}"),
        ],
        [InlineKeyboardButton("📝 Add Cover Letter", callback_data=f"addcl_{job_id}")],
    ])
