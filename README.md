<p align="center">
  <h1 align="center">HireLoop</h1>
  <p align="center">
    <b>Your autonomous job hunting agent — runs in Telegram, thinks with AI, never applies without your approval.</b>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white" />
    <img src="https://img.shields.io/badge/telegram-bot-2CA5E0?style=flat-square&logo=telegram&logoColor=white" />
    <img src="https://img.shields.io/badge/AI-multi--provider-8A2BE2?style=flat-square" />
    <img src="https://img.shields.io/badge/database-SQLite%20%7C%20PostgreSQL-003B57?style=flat-square&logo=sqlite&logoColor=white" />
    <img src="https://img.shields.io/badge/phase-1%20week%203%20done-green?style=flat-square" />
    <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" />
  </p>
</p>

---

## What Is HireLoop?

HireLoop is a **human-in-the-loop autonomous job hunting agent** that runs entirely through Telegram.

It scrapes jobs, scores your fit against your verified skill graph, generates a tailored resume for each job, and asks for your approval before doing anything. You stay in control — the bot does the grunt work.

```
Scrape jobs  →  Score fit  →  Verify skills  →  Generate resume  →  You approve  →  Log application
     ↑                              ↓
     └─────────── APScheduler runs this loop on your schedule ──────────────────┘
```

**Core principles:**
- No skill is ever claimed without your explicit confirmation
- No resume is sent without your approval
- No application fires without per-job sign-off
- Your entire interface is Telegram — no web app, no desktop client

---

## Features

| Feature | Status |
|---|---|
| Multi-provider AI (Groq, Anthropic, OpenAI, Gemini, Ollama) | ✅ Done |
| Tiered AI routing (fast for bulk, quality for resumes) | ✅ Done |
| In-memory AI response cache (SHA-256 keyed) | ✅ Done |
| 6-step Telegram onboarding wizard | ✅ Done |
| Resume parsing (PDF + DOCX, parallel with asyncio.gather) | ✅ Done |
| Skill graph with confidence levels + evidence | ✅ Done |
| Skill deduplication ("Drupal" = "Drupal CMS") | ✅ Done |
| Add/update skills post-onboarding (no wipe) | ✅ Done |
| Interactive skill verify flow (confirm / add context / remove) | ✅ Done |
| HTML skill graph report (sent as file, opens in browser) | ✅ Done |
| Settings, filters, pause/resume, /menu keyboard refresh | ✅ Done |
| Job scraping (JobSpy — Indeed, LinkedIn, Glassdoor, Google) | 🧪 Testing |
| AI role title expansion (cached 24h, 6–8 variants) | ✅ Done |
| URL-hash dedup + semantic dedup (title + company) | ✅ Done |
| URL-paste job ingestion (Jina Reader) | ✅ Done |
| Fit scoring + job notification cards | ✅ Done |
| Skill verification dialog (gap skills → evidence → DB) | ✅ Done |
| APScheduler — daily scrape at 08:00 + 18:00 | ✅ Done |
| /fetchnow — instant on-demand scrape | ✅ Done |
| Auto-purge stale jobs after 10 days (keeps applied/approved) | ✅ Done |
| Resume stored as Markdown in DB (render on-demand) | 🔜 Week 4 |
| Resume tailoring — resume/generator.py | 🔜 Week 4 |
| Word (.docx) export via python-docx (primary, ATS-controlled) | 🔜 Week 4 |
| PDF export on-request via reportlab (resume/pdf_export.py ✅) | 🔜 Week 4 |
| ATS-safe output (single-col, no tables/boxes, standard headings) | 🔜 Week 4 |
| Smart resume section order (inferred from profile, zero AI tokens) | 🔜 Week 4 |
| Job scraper summary card before first job card | 🔜 Week 4 |
| Job approval screen — [📄 Word] [📋 PDF] [Both] [Skip] | 🔜 Week 4 |
| Cover letter generation (on request only) | 🔜 Week 4 |
| Application logging | 🔜 Week 4 |
| Recruiter finder | 🔜 Phase 2 |
| Embedding-based fit scoring (RAG / semantic similarity) | 🔜 Phase 3 |
| Auto-apply (Playwright) | 🔜 Phase 3 |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                              │
│  bot/main.py · onboarding.py · handlers/ · keyboards.py         │
│                                                                  │
│  ConversationHandlers:                                           │
│    /start     → onboarding wizard (12 states)                    │
│    /addskills → add skills without wiping (6 states)             │
│  CommandHandlers:                                                 │
│    /skills /settings /filters /pause /help /menu /fetchnow       │
│    /deleteskill                                                   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      AI Service Layer                            │
│  ai/service.py — HireLoopAI                                      │
│                                                                  │
│   FAST provider (Groq / llama-3.3-70b)                          │
│     parse_resume()  parse_job()  analyze_fit()                   │
│     expand_role_titles()  ← cached 24h, 6–8 variants            │
│                                                                  │
│   QUALITY provider (Anthropic / claude-sonnet-4-6)              │
│     tailor_resume()  write_cover_letter()                        │
│     answer_screening_questions()  edit_resume()                  │
│                                                                  │
│   ai/cache.py — SHA-256 in-memory cache (no repeat AI calls)    │
│   ai/factory.py — reads .env, builds provider instances          │
│   ai/providers/ — Groq · Anthropic · OpenAI · Gemini · Ollama   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Database (SQLite → Postgres)               │
│  db/models.py — SQLAlchemy async                                 │
│  User · SkillNode · SkillEvidence · Job · Application            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### Prerequisites
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- At least one AI provider API key (Groq is free and fast)

### 1. Clone & install

```bash
git clone https://github.com/yourname/hireloop.git
cd hireloop
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` — minimum required:

```env
TELEGRAM_BOT_TOKEN=your_token_from_botfather
AI_FAST_PROVIDER=groq
AI_FAST_API_KEY=your_groq_key
AI_QUALITY_PROVIDER=anthropic
AI_QUALITY_API_KEY=your_anthropic_key
DATABASE_URL=sqlite:///hireloop.db
```

### 3. Run

```bash
python bot/main.py
```

The bot will create the database schema on first run. Open Telegram, find your bot, and send `/start`.

---

## Configuration Reference

```env
# ── AI — Fast provider (high volume: parse jobs, score fit) ──────────────────
AI_FAST_PROVIDER=groq                      # groq | anthropic | openai | gemini | ollama
AI_FAST_API_KEY=your_key
AI_FAST_MODEL=                             # blank = use provider default (see table below)

# ── AI — Quality provider (high stakes: resume, cover letter) ────────────────
AI_QUALITY_PROVIDER=anthropic
AI_QUALITY_API_KEY=your_key
AI_QUALITY_MODEL=                          # blank = use provider default

# ── AI global settings ───────────────────────────────────────────────────────
AI_MAX_TOKENS=4096
AI_TEMPERATURE=0.3

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=from_botfather
ALLOWED_TELEGRAM_IDS=                      # comma-separated IDs/usernames, blank = open

# ── Database ─────────────────────────────────────────────────────────────────
DATABASE_URL=sqlite:///hireloop.db         # swap to postgresql+asyncpg://... for prod

# ── Optional ─────────────────────────────────────────────────────────────────
SERPAPI_KEY=                               # enables Google Jobs via SerpAPI
OLLAMA_HOST=http://localhost:11434         # local Ollama instance
```

### Provider Defaults

| Provider | Default Model | Speed | Cost |
|---|---|---|---|
| `groq` | `llama-3.3-70b-versatile` | Very fast | Free tier available |
| `anthropic` | `claude-sonnet-4-6` | Fast | ~$3/M tokens |
| `openai` | `gpt-4o` | Fast | ~$5/M tokens |
| `gemini` | `gemini-2.0-flash` | Fast | Free tier available |
| `ollama` | `llama3.2` | Depends on hardware | Free (local) |

---

## AI Task → Provider Routing

```
parse_resume()               →  FAST    (one-time extraction, not quality-critical)
parse_job()                  →  FAST    (runs on every scraped job, 50+/day)
expand_role_titles()         →  FAST    (runs once per user, cached 24h in user.filters)
                                        ↑ "AI Engineer" → ["ML Engineer", "LLM Developer", ...]
analyze_fit()                →  FAST    (runs per job above salary threshold)
                                        ↑ cached — same resume+job = no repeat call

tailor_resume()              →  QUALITY (goes on your actual resume — must be best)
write_cover_letter()         →  QUALITY (represents you to recruiters)
answer_screening_questions() →  QUALITY (high-stakes interview gating)
```

The cache layer (`ai/cache.py`) stores results keyed by SHA-256 of the input. If you re-analyze the same resume or job posting, the AI is not called again.

---

## Skill Graph

The skill graph is the heart of HireLoop. Every skill has a **node** (name, status, confidence) and optional **evidence** (where you used it, for how long, in your own words).

### Skill Statuses

| Status | Meaning | Color in report |
|---|---|---|
| `verified_attested` | You confirmed it and gave context | Green |
| `verified_resume` | Extracted from your resume, auto-confirmed | Blue |
| `partial` | Mentioned but no evidence | Amber |
| `gap` | Required by a job, missing from your profile | Red |

### Confidence Levels

| Level | Source |
|---|---|
| `high` | AI found strong resume evidence → auto-confirmed |
| `medium` | AI found some signals → you verify one-by-one |
| `low` | Weak signal → you verify one-by-one |

### Evidence Travels Automatically

Once you confirm a skill with context, that evidence is reused forever:

```
You confirm: "Kafka at Acme Corp, 8 months, async order processing"

     ↓ saved as ↓

SkillNode(skill_name="Apache Kafka", status="verified_attested")
SkillEvidence(company="Acme Corp", duration_months=8,
              user_context="built async order processing pipeline")

     ↓ next Kafka job ↓

Resume bullet auto-generated:
"Designed Kafka-based event pipeline for order processing at Acme Corp (8 months)"
```

You never explain the same skill twice.

### Skill Deduplication

Synonymous skills are normalized before storage:

```
"Drupal CMS"  →  key: "drupal"   ┐
"Drupal"      →  key: "drupal"   ┘  merged, highest confidence kept

"Vue.js"      →  key: "vue"      ┐
"Vue JS"      →  key: "vue"      ┘  merged

"Node JS"     →  key: "node"
"React.js"    →  key: "react"
```

Stripped suffixes: `cms`, `framework`, `db`, `database`, `server`, `sdk`, `api`, `platform`, `library`, `.js`, ` js`

---

## Bot Commands

Register these with [@BotFather](https://t.me/BotFather) (`/setcommands`):

```
start       - Onboarding wizard (or re-run to update profile)
addskills   - Add new skills or upload an updated resume
skills      - View your skill graph (HTML report)
settings    - View all preferences
filters     - Edit job filters
pause       - Pause or resume job hunting
deleteskill - Remove a skill by name
fetchnow    - Trigger an immediate job scrape
menu        - Refresh the keyboard (useful after bot restarts)
cancel      - Cancel current operation
help        - Full command list
```

### Persistent Keyboard

Always visible at the bottom of the chat:

```
┌──────────────────┬──────────────────┐
│  📎 Add Resume   │  🎛️ Edit Filters  │
├──────────────────┼──────────────────┤
│  📊 My Skills    │  📋 Pending Jobs  │
├──────────────────┼──────────────────┤
│  🔍 Fetch Jobs   │  ⚙️ Settings      │
└──────────────────┴──────────────────┘
│        ⏸ Pause Agent                │
└──────────────────────────────────────┘
```

---

## Onboarding Flow (`/start`)

```
WELCOME
  └─ [Let's go ✅]

UPLOAD_RESUME
  └─ Send 1–4 PDF or DOCX resumes
  └─ [Done uploading ✅]
       │
       ├─ AI extracts all skills
       ├─ Deduplication (normalize → merge highest confidence)
       └─ High confidence → auto-confirmed
          Medium/Low → one-by-one verify:

CONFIRM_SKILLS (per skill)
  ├─ [✅ Confirm]       → status: verified_resume
  ├─ [✏️ Add context]   → ask for one sentence → status: verified_attested
  └─ [❌ Remove]        → skip this skill

SET_FILTERS
  └─ Role titles (comma-separated, e.g. "Software Engineer, AI Engineer")
  └─ Remote preference (remote / hybrid / on-site / any)
  └─ Country (sets regional job board)
  └─ City / Region (or skip for nationwide)
  └─ Job boards (Indeed, LinkedIn, Glassdoor, Google, ZipRecruiter)
  └─ Min salary
  └─ Blacklist companies

SET_FREQUENCY
  └─ [📬 Daily] [⚡ Real-time] [2x/day]
  └─ Min fit score: [50%] [60%] [70%] [80%]

DONE
  └─ Profile saved, keyboard activated
```

**Returning users** (`/start` when already onboarded) get a menu:
- `🎛️ Update filters` — re-run filter setup only
- `➕ Add more skills` — directs to `/addskills`
- `📎 Re-upload resume` — full re-onboarding (replaces skill graph)
- `❌ Nothing, cancel`

---

## Adding Skills Post-Onboarding (`/addskills`)

Triggered by `/addskills` or the `📎 Add Resume` keyboard button. **Never wipes existing skills.**

### Path A — Upload resume

```
Upload 1–4 resumes
  └─ AI extracts skills
  └─ Dedup against existing graph:
       Already have? → skipped (no duplicate nodes)
       New skill?    → high confidence = auto-merged
                       medium/low = ask to verify
  └─ Merge into graph (add new nodes, upgrade confidence/status if better)
```

### Path B — Add manually

```
Type skill name (e.g. "Kubernetes")
  └─ Add one-line context? (or type "skip")
  └─ Saved as verified_attested
  └─ Type another skill name, or /cancel to stop
```

### Merge rules

| Condition | Result |
|---|---|
| New skill | `SkillNode` + `SkillEvidence` inserted |
| Skill exists, new confidence is higher | Confidence upgraded |
| Skill exists, new status is higher (partial → verified) | Status upgraded |
| Skill exists, new evidence provided | Evidence appended (old preserved) |

---

## Skill Graph Report (`/skills`)

Sends a live HTML file that opens in any browser:

- Color-coded badge pills per status (green / blue / amber / red)
- Confidence indicator per skill (●●● / ●●○ / ●○○)
- Evidence snippet shown on each pill (first 120 chars)
- Header stats: Total / Verified / Have / Gaps
- Generated from the database — zero AI calls, instant

---

## Job Notification Card

```
🏢 Senior Backend Engineer
Acme Corp · Toronto, ON · $120k–$150k

Fit Score: 84% · Strong match

✅ Matched: Python, FastAPI, PostgreSQL, Redis
❓ Gaps:    Kafka (required), Temporal (preferred)

[✅ I know these]  [⏭ Skip]  [📄 Full JD]  [🔗 Open Link]
```

Tapping `📄 Full JD` sends the raw job description as a follow-up message (does not replace the card).

After confirming skills → fit score recalculated → resume generation begins.

---

## Resume Generation + Format Delivery

### Storage Strategy

The tailored resume is stored as **Markdown text** in the DB (`resume_markdown TEXT` per application). Binary files are rendered on-demand when you request them — keeping the DB small and the content LLM-friendly.

```
tailor_resume() → Markdown (stored in DB)
                       ↓
        [📄 Word Doc]   [📋 PDF]   [Both]
              ↓               ↓
      docx_export.py    pdf_export.py
    (python-docx)       (reportlab)
```

### Format Choice

| Format | Tool | Why |
|---|---|---|
| `.docx` (primary) | `python-docx` | Full style control — forces exact Heading/ListBullet styles ATS parsers recognize; no surprise tables |
| `.pdf` (on request) | `reportlab` | Fixed layout for email attachments |

### Smart Section Order

Section order is inferred from the user's parsed resume — no onboarding questions, zero AI tokens for 85% of cases.

**4 signals, all derived from existing data:**

| Signal | Source | Cost |
|---|---|---|
| `years_exp` | Sum of `duration_months` from `parse_resume()` | 0 tokens |
| `graduation_year` | Education section from `parse_resume()` | 0 tokens |
| `has_strong_projects` | Project count from `parse_resume()` | 0 tokens |
| `is_career_changer` | Domain match: past titles vs target role | 0 tokens |

**`is_career_changer` logic** — no API call, no user question:
```
is_career_changer = (
    domain_mismatch(past_titles, target_role)   ← keyword set comparison
    AND years_exp >= 2                           ← rules out freshers with part-time jobs
    AND any(duration_months >= 6 per role)       ← rules out short gigs
)
```

**Decision tree (`resume/section_order.py`):**
```
Fresher (years_exp < 2 or no full-time role):
  education → projects → skills → experience

Career changer (domain mismatch + years_exp ≥ 2 + full-time role):
  summary → skills → experience → education

Experienced (domain match + years_exp ≥ 2):
  summary → experience → skills → education [+ projects if strong]
```

Groq fallback (~70 tokens) only for true edge cases. Stored in `user.filters["resume_section_order"]`. Auto-updated on every new resume upload. Changeable anytime via `/settings → Resume preferences`.

### ATS Parsability Rules

All generated resumes enforce:
- **Single column** — no multi-column layouts
- **No tables for structure** — bullets only (tables confuse ATS parsers)
- **No text boxes, headers/footers** for important content
- **Standard section names**: `Work Experience`, `Education`, `Skills`, `Summary`
- **Standard fonts**: Arial / Calibri — no decorative fonts
- **Explicit heading styles** — `python-docx` `add_heading(level=1)` maps to `"Heading 1"` style, recognized by Workday, Greenhouse, Lever, LinkedIn, Indeed

`python-docx` used instead of pypandoc — pypandoc converts Markdown tables → Word tables (ATS killer). `python-docx` gives direct control over every element.

---

## Cover Letter Logic

A cover letter is generated **only when**:

1. The job description explicitly mentions one (`"cover letter"`, `"covering letter"`, `"letter of motivation"`, `"please include"`)
2. The user taps `[📝 Add Cover Letter]` on the approval screen

It is **never auto-generated** for every application.

---

## Project Structure

```
hireloop/
├── .env.example
├── requirements.txt
├── docker-compose.yml
│
├── ai/
│   ├── base.py                    # Abstract AIProvider class
│   ├── factory.py                 # AIFactory.create_fast() / create_quality()
│   ├── service.py                 # HireLoopAI — 6 task methods, tiered routing
│   ├── cache.py                   # SHA-256 in-memory response cache
│   └── providers/
│       ├── anthropic_provider.py
│       ├── openai_provider.py
│       ├── gemini_provider.py
│       ├── groq_provider.py
│       └── ollama_provider.py
│
├── db/
│   ├── models.py                  # User · SkillNode · SkillEvidence · Job · Application
│   ├── session.py                 # AsyncSessionLocal + engine
│   └── migrations/                # Alembic
│
├── bot/
│   ├── main.py                    # Entry point, handler registration
│   ├── onboarding.py              # /start ConversationHandler (12 states)
│   ├── keyboards.py               # All InlineKeyboardMarkup builders
│   └── handlers/
│       ├── add_skills.py          # /addskills ConversationHandler (6 states)
│       ├── settings.py            # /skills /settings /filters /pause /help /deleteskill
│       ├── skill_verify.py        # (Week 3) job skill confirmation
│       ├── job_approval.py        # (Week 4) resume approve/edit/skip
│       └── resume_upload.py       # (Week 4) resume generation
│
├── jobs/
│   ├── scraper.py                 # (Week 3) JobSpy wrapper
│   ├── parser.py                  # (Week 3) Jina Reader for pasted URLs
│   ├── filters.py                 # (Week 3) salary / blacklist / dedup filters
│   └── scheduler.py              # (Week 3) APScheduler AsyncIOScheduler
│
├── resume/
│   ├── generator.py               # (Week 4) tailor_resume() → store Markdown in DB
│   ├── section_order.py           # (Week 4) infer section order from profile, zero tokens
│   ├── docx_export.py             # (Week 4) Markdown → .docx via python-docx (ATS-safe)
│   ├── pdf_export.py              # (Week 4) Markdown → PDF via reportlab ✅ ready
│   ├── variants/                  # Your base resume markdown files
│   └── output/                    # Generated files (gitignored)
│
└── scripts/
    └── test_provider.py           # Smoke-test any AI provider from CLI
```

---

## Adding a New AI Provider

1. Create `ai/providers/myprovider.py`

```python
from ai.base import AIProvider

class MyProvider(AIProvider):
    @property
    def provider_name(self) -> str:
        return "myprovider"

    async def complete(self, prompt: str, system: str = "") -> str:
        # call your API here
        ...
```

2. Add it to `AIFactory._build()` in `ai/factory.py`:

```python
case "myprovider":
    from ai.providers.myprovider import MyProvider
    return MyProvider(api_key=api_key, model=model or "my-default-model")
```

3. Add default model to `_DEFAULT_MODELS` in `factory.py`:

```python
"myprovider": "my-default-model",
```

4. Set in `.env`:
```env
AI_FAST_PROVIDER=myprovider
AI_FAST_API_KEY=your_key
```

---

## Database Schema

```
users
  id (UUID PK) · telegram_id · name · filters (JSON) · notify_freq
  min_fit_score · daily_app_limit · onboarded · created_at

skill_nodes
  id · user_id (FK) · skill_name · status · confidence
  created_at · updated_at

skill_evidence
  id · skill_node_id (FK) · company · role_title · duration_months
  last_used_year · user_context · generated_bullet · source

jobs
  id (UUID PK) · user_id (FK) · title · company · url · url_hash
  raw_jd · parsed (JSON) · fit_score · cover_letter_required
  recruiter_name · recruiter_linkedin · status · created_at

applications
  id · job_id (FK) · resume_path · cover_letter_path · applied_at
  outcome · outcome_source · outcome_at
```

`url_hash` on `jobs` deduplicates scraped listings. `source` on `skill_evidence` is `resume | telegram | manual`.

---

## Roadmap

### Phase 1 — Foundation + Telegram (current)
- [x] AI provider abstraction + all 5 providers
- [x] Tiered routing (fast / quality)
- [x] In-memory cache
- [x] SQLAlchemy async schema
- [x] Telegram onboarding wizard (12 states)
- [x] Skill graph (nodes + evidence)
- [x] Skill deduplication + normalization
- [x] Post-onboarding skill management (/addskills)
- [x] HTML skill graph report (/skills)
- [~] Job scraper (JobSpy — Indeed, LinkedIn, Glassdoor, Google) — testing in progress
- [x] AI role title expansion (expand_role_titles, cached 24h)
- [x] URL-hash + semantic (title+company) dedup
- [x] URL-paste ingestion (Jina Reader)
- [x] Fit scoring notification cards
- [x] Skill verification dialog
- [x] APScheduler daily scrape loop (08:00 + 18:00)
- [x] /fetchnow — instant on-demand scrape
- [x] Auto-purge stale jobs after 10 days
- [x] PDF export (reportlab) — resume/pdf_export.py (render layer ready)
- [ ] Resume generator — resume/generator.py (AI tailoring → store Markdown in DB)
- [ ] Word (.docx) export — resume/docx_export.py via python-docx (ATS-safe)
- [ ] Smart section order — resume/section_order.py (pure Python decision tree, zero tokens)
- [ ] is_career_changer inference (domain match + years_exp + full-time role check)
- [ ] ATS-safe constraints: single-col, no tables/text-boxes, standard section names
- [ ] Job scraper summary card (X jobs found · Y above threshold · Z ready)
- [ ] Job approval screen — [📄 Word] [📋 PDF] [Both] [⏭ Skip]
- [ ] Cover letter generation (on request only)
- [ ] Application logging to DB

### Phase 2 — Multi-user + Intelligence
- [ ] Recruiter finder (3-tier: JD parse → LinkedIn → web search)
- [ ] Application rate limiter (daily cap, 30-day company cooldown)
- [ ] Outcome tracking (interview / rejected / ghosted / offer)
- [ ] Salary intel before fit scoring
- [ ] Postgres migration
- [ ] VPS deployment

### Phase 3 — Automation
- [ ] Auto-apply via Playwright (Workday / Greenhouse / Lever)
- [ ] ATS platform classifier
- [ ] Screenshot proof of submission
- [ ] Gmail integration for outcome loop
- [ ] Web dashboard (Next.js)
- [ ] Supabase multi-tenant auth
- [ ] Embedding-based fit scoring (sentence-transformers, cosine similarity) — low priority

---

## Running Tests

```bash
# Test any AI provider from CLI
python scripts/test_provider.py

# Syntax check all modules
python -m py_compile bot/main.py bot/onboarding.py \
  bot/handlers/add_skills.py bot/handlers/settings.py \
  ai/service.py ai/factory.py db/models.py
```

---

## Docker (coming Week 4)

```bash
docker-compose up -d
```

The compose file runs the bot process + volume-mounts the SQLite DB and resume output directory.

---

## Security Notes

- Never commit `.env` — it's in `.gitignore`
- Set `ALLOWED_TELEGRAM_IDS` to restrict access to your own Telegram user ID
- All user-supplied text is HTML-escaped before being sent back through Telegram (no Markdown injection)
- No external web server is exposed — the bot uses Telegram's long-polling

---

## License

MIT — do whatever you want, but don't blame us if your resume gets too good.
