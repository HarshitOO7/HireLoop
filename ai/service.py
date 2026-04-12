import json
import logging
import re
import time
from ai.base import AIProvider
from ai import cache

logger = logging.getLogger(__name__)


def _parse_json(raw: str) -> dict | list:
    """Strip markdown fences and parse JSON. Falls back to brace/bracket extraction."""
    text = raw.strip()
    # Remove ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text.strip())
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract the first JSON object or array from the response
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            end   = text.rfind(end_char)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
        logger.error("[_parse_json] could not parse JSON from response (first 200 chars): %r", text[:200])
        raise


def _jc(obj) -> str:
    """Compact JSON — no indentation, no wasted tokens."""
    return json.dumps(obj, separators=(",", ":"))


def _slim_job(job: dict, keys: tuple) -> str:
    """Extract only the needed keys from a parsed job dict and return compact JSON."""
    return _jc({k: job[k] for k in keys if k in job})


def _norm(text: str) -> str:
    """Collapse all whitespace for stable cache keys."""
    return " ".join(text.split())


# ── Prompts ───────────────────────────────────────────────────────────────────

_FIT_SYSTEM = """You are a job fit analyzer. Be conservative and honest. Never inflate scores.

SCORING RULES:
- A skill only counts as matched if the candidate's name for it closely matches the JD's name
- Partial knowledge does not count as matched — when in doubt, put it in missing_required
- Never return fit_score > 85 unless all required skills are matched
- gap_summary must be honest — if fit_score < 60, say so plainly

Return ONLY valid JSON. No preamble, no markdown fences, no explanation."""

_FIT_PROMPT = """Analyze the fit between this job and candidate profile.

<job>
{jd_text}
</job>

<candidate_skills>
{skill_graph_json}
</candidate_skills>

<resume_variants>
{variant_tags}
</resume_variants>

Return this exact JSON structure:
{{
  "fit_score": <integer 0-100>,
  "matched_skills": [<string>],
  "missing_required": [{{"skill": <string>, "importance": <"required"|"preferred"|"nice">}}],
  "requires_cover_letter": <true|false>,
  "best_resume_variant": <one of the resume_variants>,
  "gap_summary": <2-sentence honest assessment>,
  "action": <"apply"|"consider"|"skip">
}}

Return ONLY the JSON object. No text before or after the closing brace."""

_PARSE_JOB_SYSTEM = """Extract structured information from job descriptions.

EXTRACTION RULES:
- Only extract information explicitly stated in the job description
- If a field is not mentioned, return null — do NOT infer or guess
- "hybrid" or "flexible" does not mean remote=true — only return true if explicitly stated

Return ONLY valid JSON. No preamble, no markdown fences."""

_PARSE_JOB_PROMPT = """Parse this job description into structured data.

<job_description>
{jd_text}
</job_description>

Return this exact JSON structure:
{{
  "title": <string>,
  "company": <string>,
  "location": <string|null>,
  "salary_range": <string|null>,
  "remote": <true|false|null>,
  "required_skills": [<string>],
  "preferred_skills": [<string>],
  "years_experience": <integer|null>,
  "seniority": <"junior"|"mid"|"senior"|"lead"|"staff"|null>,
  "requires_cover_letter": <true|false>,
  "cover_letter_keywords": [<string>],
  "recruiter_name": <string|null>,
  "recruiter_contact": <string|null>
}}

Return ONLY the JSON object. No text before or after the closing brace."""

_PARSE_RESUME_SYSTEM = """Extract skills and experience from resumes.
Return ONLY valid JSON. No preamble, no markdown fences."""

_PARSE_RESUME_PROMPT = """Parse this resume and extract all skills with confidence levels.

<resume>
{resume_text}
</resume>

Return this exact JSON structure:
{{
  "name": <string>,
  "skills": [
    {{
      "skill_name": <string>,
      "confidence": <"high"|"medium"|"low">,
      "evidence": <brief note from resume>
    }}
  ],
  "work_history": [
    {{
      "company": <string>,
      "role": <string>,
      "duration_months": <integer|null>,
      "last_used_year": <integer|null>
    }}
  ],
  "variant_tags": [<string>]
}}

Return ONLY the JSON object. No text before or after the closing brace."""

_TAILOR_SYSTEM = """You are an expert resume writer. Tailor resumes truthfully using only verified evidence.

EDUCATION — NON-NEGOTIABLE RULE (read before anything else):
The EDUCATION section must be copied verbatim from the <resume> block. Do not paraphrase, normalize,
or simplify any credential. The exact string from the resume is the only correct output.
  WRONG: "Postgraduate Degree, Information Technology"   ← fabricated
  RIGHT: "Postgraduate Diploma, Computer Information Technology"  ← exact copy from resume
A wrong credential type on a resume is fraud. Copy. Do not rewrite.

HARD RULES:
- Only include skills where status starts with "verified_" in the skill graph
- Never invent accomplishments — use STAR format for all bullets
- Mirror JD keywords for ATS optimization (do not keyword-stuff)
- Max 1 page under 5yr experience, 2 pages for 5yr+ — but NEVER drop a work experience entry to fit; condense bullets instead
- If "ADDITIONAL WORK HISTORY FROM SKILL GRAPH" is present in the context, include every company listed there as a WORK EXPERIENCE entry — the candidate verified this experience via the skill verification flow, it must appear even if absent from the uploaded resume
- If cover letter required: append after ---COVER LETTER--- separator
- DO NOT output the candidate's name or contact line — start your output directly from the first ## section (e.g. ## SUMMARY)
- Contact line rule: NO city, NO country, NO postal code — phone, email, and links only
- GitHub link: only include if the candidate's resume already contains one AND the target role is technical (software/engineering/data/IT). Omit entirely for non-technical roles.
- Links: write bare URLs (e.g. linkedin.com/in/username) — the renderer handles making them clickable

EXPERIENCE FILTERING:
- Omit work experience entries older than 10 years unless they directly use a required skill for the target role
- Keep at most 4 Work Experience entries; if more exist, drop the oldest/least relevant first
- For retained older roles, condense to 1–2 bullets — never omit an entry that is within 10 years
- Education, certifications, licences, and volunteer work are exempt from the 10-year rule — always include them

ANTI-HALLUCINATION RULES:
- Every bullet must be traceable to either the base resume or the evidence notes
- Never add a metric (%, $, number) that does not appear in the source material
- Never add a technology, tool, or framework not present in verified_skills or the base resume
- If you cannot write a strong bullet without inventing details, write a weaker honest bullet
- "Led", "Architected", "Designed" — only use these if the source material uses them or clearly implies them

For ADDITIONAL WORK HISTORY FROM SKILL GRAPH entries:
- You have limited info — write 1-2 bullets max per company, strictly from what is provided
- Do not expand or embellish the user's description
- If duration is present, include it as the date range

OUTPUT FORMAT (follow this section structure exactly):
## SUMMARY
## WORK EXPERIENCE
**Role** | Date
*Company*
- bullet
## SKILLS
**Category:** skills
## EDUCATION
**[credential copied EXACTLY from resume — no rewording]** | Year
*Institution*
## PROJECTS (tech roles only)
**Name** | Year
- bullet"""

_TAILOR_PROMPT = """Tailor this resume for the job below.

<resume variant="{variant_tag}">
{base_resume_text}
</resume>

<job>
{jd_text}
</job>

<verified_skills>
{verified_skills_json}
</verified_skills>

<evidence_notes>
{user_evidence_text}
</evidence_notes>

<special_instructions>
{special_instructions_text}
</special_instructions>

Cover letter required: {requires_cl}

Output the full tailored resume in Markdown starting from the first ## section.
{cover_letter_instruction}"""

_COVER_LETTER_SYSTEM = "You are an expert at writing compelling, honest cover letters."

_COVER_LETTER_PROMPT = """Write a cover letter for this job application.

<job>
{jd_text}
</job>

<candidate>
{user_profile_json}
</candidate>

<fit>
{fit_json}
</fit>

Write a professional cover letter. Exactly 3 paragraphs:
1. Opening: why this specific role and company
2. Body: 2-3 most relevant experiences/skills matched to the role requirements
3. Close: enthusiasm + call to action

250-350 words. No fluff. No "I am writing to apply for..." opener."""

_EXPAND_ROLES_SYSTEM = """You are a job search expert. Return ONLY a valid JSON array of strings. No preamble, no markdown."""

_EXPAND_ROLES_PROMPT = """The user is targeting these job titles:
{role_titles}

Return exactly 3 job search terms for use as job board keywords. Rules:
- Each term must cover DIFFERENT search space — no synonyms, no near-duplicates
- If the input already contains a broad term (e.g. "Software Engineer"), do NOT add narrower variants of it (e.g. "Backend Engineer") — they are already covered
- Prefer broader/more general titles over specific ones so one search catches more listings
- Keep each title SHORT (2–4 words max)
- No seniority prefixes (no Senior/Junior/Lead) — those are covered by the user's years filter
- No descriptions, no explanations

Return a JSON array of exactly 3 strings:
["Title 1", "Title 2", "Title 3"]"""

_PARSE_AND_FIT_SYSTEM = """You are a job parser and fit analyzer. Be conservative and honest. Never inflate scores.

EXTRACTION RULES:
- Only extract information explicitly stated in the job description
- If a field is not mentioned, return null — do NOT infer or guess
- "hybrid" or "flexible" does not mean remote=true — only return true if explicitly stated

SCORING RULES:
- A skill only counts as matched if the candidate's name for it closely matches the JD's name
- Partial knowledge does not count as matched — when in doubt, put it in missing_required
- Never return fit_score > 85 unless all required skills are matched
- gap_summary must be honest — if fit_score < 60, say so plainly

Return ONLY valid JSON. No preamble, no markdown fences, no explanation."""

_PARSE_AND_FIT_PROMPT = """Parse this job description and analyze fit with the candidate in one pass.

<job_description>
{jd_text}
</job_description>

<candidate_skills>
{skill_graph_json}
</candidate_skills>

<resume_variants>
{variant_tags}
</resume_variants>

Return this exact JSON structure:
{{
  "parsed": {{
    "title": <string>,
    "company": <string>,
    "location": <string|null>,
    "salary_range": <string|null>,
    "remote": <true|false|null>,
    "required_skills": [<string>],
    "preferred_skills": [<string>],
    "years_experience": <integer|null>,
    "seniority": <"junior"|"mid"|"senior"|"lead"|"staff"|null>,
    "requires_cover_letter": <true|false>,
    "cover_letter_keywords": [<string>],
    "recruiter_name": <string|null>,
    "recruiter_contact": <string|null>
  }},
  "fit": {{
    "fit_score": <integer 0-100>,
    "matched_skills": [<string>],
    "missing_required": [{{"skill": <string>, "importance": <"required"|"preferred"|"nice">}}],
    "requires_cover_letter": <true|false>,
    "best_resume_variant": <one of the resume_variants>,
    "gap_summary": <2-sentence honest assessment>,
    "action": <"apply"|"consider"|"skip">
  }}
}}

Return ONLY the JSON object. No text before or after the closing brace."""

_SCREENING_SYSTEM = "Answer screening questions honestly based on the candidate's verified experience."

_SCREENING_PROMPT = """Answer these screening questions for a job application.

<questions>
{questions}
</questions>

<job>
{jd_text}
</job>

<candidate>
{user_profile_json}
</candidate>

Return a JSON array: [{{"question": <string>, "answer": <string>}}]

Return ONLY the JSON array. No text before or after the closing bracket."""

_PATCH_SYSTEM = """You are a precise resume editor. Apply exactly what the user requests.

OUTPUT FORMAT:
- Wrap every changed section in <section name="SECTION NAME">...</section> using the exact heading name from the resume
- Omit sections that did not change
- For section-level reordering: also include <reorder>SEC1, SEC2, ...</reorder> listing ALL sections in new order
- Both <reorder> and <section> tags can appear together in the same response — they are each applied

GLOBAL CHANGES — CRITICAL:
- Any change to a date, title, skill, company name, or fact must be applied in EVERY section where it appears
- Output a <section> tag for each section that was touched — never leave stale data elsewhere

SORTING WITHIN A SECTION:
- To sort entries inside a section (e.g. jobs or projects by descending date), rewrite the full section
  content in the new order and output it as a <section> tag
- This applies to any section: WORK EXPERIENCE, PROJECTS, EDUCATION, or any other

ORDER-ONLY REQUESTS:
- If the user ONLY asks to change section order (no content edits needed), output ONLY the <reorder> tag
- Do NOT output any <section> tags for order-only requests — that would rewrite content unnecessarily

CONTENT RULES:
- Preserve all formatting, bullets, and wording exactly for anything not being changed
- No invented content — only use what is already in the resume, in <evidence_notes>, or explicitly stated by the user
- If the user provides new information directly in their request, you may use it verbatim

NEW SECTIONS:
- If the user asks to add a section (e.g. "add certifications: AWS SAA 2024"), create it with only their stated content
- Choose a sensible section name matching standard resume conventions

CANNOT APPLY:
- Only output <section name="CANNOT_APPLY">reason</section> if the request is genuinely impossible
  (e.g. information does not exist anywhere and the user did not provide it)
- Never use CANNOT_APPLY just because information is missing from one section — check all sections and evidence"""

_PATCH_PROMPT = """<current_resume>
{current_resume}
</current_resume>
{evidence_block}
<edit_request>
{user_request}
</edit_request>

Apply the edit. Output <section name="..."> tags for changed sections, and a <reorder> tag if section order changed. Both may appear together."""


# ── Service ───────────────────────────────────────────────────────────────────

class HireLoopAI:
    """All HireLoop AI tasks. Uses tiered providers:
    - fast_provider: parse_job, parse_resume, analyze_fit (high volume)
    - quality_provider: tailor_resume, write_cover_letter, answer_screening_questions
    """

    def __init__(self, fast_provider: AIProvider, quality_provider: AIProvider):
        self._fast = fast_provider
        self._quality = quality_provider

    async def parse_job(self, raw_jd_text: str) -> dict:
        logger.info("[parse_job] START — jd text %d chars", len(raw_jd_text))
        t0 = time.monotonic()
        norm = _norm(raw_jd_text)
        if cached := cache.get("parse_job", norm):
            logger.info("[parse_job] CACHE HIT (%.2fs)", time.monotonic() - t0)
            return cached
        prompt = _PARSE_JOB_PROMPT.format(jd_text=norm[:3000])
        logger.info("[parse_job] sending to %s — prompt %d chars", self._fast.provider_name, len(prompt))
        t_ai = time.monotonic()
        raw = await self._fast.complete_json(prompt, system=_PARSE_JOB_SYSTEM, max_tokens=512)
        logger.info("[parse_job] AI responded in %.2fs — raw %d chars", time.monotonic() - t_ai, len(raw))
        result = _parse_json(raw)
        logger.info("[parse_job] DONE %.2fs — title=%r company=%r skills_req=%d",
                    time.monotonic() - t0,
                    result.get("title"), result.get("company"),
                    len(result.get("required_skills", [])))
        cache.put("parse_job", result, norm)
        return result

    async def parse_resume(self, resume_text: str) -> dict:
        logger.info("[parse_resume] START — resume text length: %d chars", len(resume_text))
        t0 = time.monotonic()

        if cached := cache.get("parse_resume", resume_text):
            logger.info("[parse_resume] CACHE HIT — returning cached result (%.2fs)", time.monotonic() - t0)
            return cached

        logger.info("[parse_resume] cache miss — building prompt (provider: %s)", self._fast.provider_name)
        prompt = _PARSE_RESUME_PROMPT.format(resume_text=resume_text)
        logger.info("[parse_resume] prompt built — %d chars — sending to AI...", len(prompt))

        t_ai = time.monotonic()
        raw = await self._fast.complete_json(prompt, system=_PARSE_RESUME_SYSTEM, max_tokens=1000)
        logger.info("[parse_resume] AI responded in %.2fs — raw response length: %d chars", time.monotonic() - t_ai, len(raw))

        result = _parse_json(raw)
        skill_count = len(result.get("skills", []))
        logger.info("[parse_resume] DONE — extracted %d skills in %.2fs total", skill_count, time.monotonic() - t0)
        logger.debug("[parse_resume] skills: %s", [s["skill_name"] for s in result.get("skills", [])])

        cache.put("parse_resume", result, resume_text)
        return result

    async def analyze_fit(self, job: dict, user_profile: dict) -> dict:
        job_title = job.get("title", "?")
        skill_count = len(user_profile.get("skills", []))
        logger.info("[analyze_fit] START — job=%r  user_skills=%d", job_title, skill_count)
        t0 = time.monotonic()
        if cached := cache.get("analyze_fit", job, user_profile):
            logger.info("[analyze_fit] CACHE HIT (%.2fs)", time.monotonic() - t0)
            return cached
        slim_skills = [s["skill_name"] for s in user_profile.get("skills", [])]
        prompt = _FIT_PROMPT.format(
            jd_text=_slim_job(job, ("title", "required_skills", "preferred_skills",
                                    "seniority", "years_experience", "cover_letter_required")),
            skill_graph_json=_jc(slim_skills),
            variant_tags=", ".join(user_profile.get("variant_tags", ["general"])),
        )
        logger.info("[analyze_fit] sending to %s — prompt %d chars", self._fast.provider_name, len(prompt))
        t_ai = time.monotonic()
        raw = await self._fast.complete_json(prompt, system=_FIT_SYSTEM, max_tokens=600)
        logger.info("[analyze_fit] AI responded in %.2fs — raw %d chars", time.monotonic() - t_ai, len(raw))
        result = _parse_json(raw)
        logger.info("[analyze_fit] DONE %.2fs — fit_score=%s  action=%r  matched=%d  missing_required=%d",
                    time.monotonic() - t0,
                    result.get("fit_score"), result.get("action"),
                    len(result.get("matched_skills", [])),
                    len(result.get("missing_required", [])))
        cache.put("analyze_fit", result, job, user_profile)
        return result

    async def tailor_resume(
        self,
        job: dict,
        fit: dict,
        base_resume: str,
        verified_skills: list[dict],
        user_evidence: str = "",
        special_instructions: str = "",
    ) -> str:
        variant = fit.get("best_resume_variant", "general")
        requires_cl = bool(job.get("requires_cover_letter", False))
        cover_letter_instruction = (
            "Then append ---COVER LETTER--- followed by the cover letter."
            if requires_cl else
            "Do not include a cover letter."
        )
        logger.info("[tailor_resume] START — job=%r  variant=%s  verified_skills=%d  base_resume=%d chars",
                    job.get("title", "?"), variant, len(verified_skills), len(base_resume))
        t0 = time.monotonic()
        # Safety-truncate user-supplied strings — defence in depth against DB data
        # entered before limits were enforced or by a future admin/bulk import.
        _evidence = (user_evidence or "")[:2000]
        _instructions = (special_instructions or "")[:600]

        prompt = _TAILOR_PROMPT.format(
            variant_tag=variant,
            base_resume_text=base_resume,
            jd_text=_slim_job(job, ("title", "company", "required_skills", "preferred_skills",
                                    "seniority", "years_experience", "cover_letter_keywords")),
            verified_skills_json=_jc(verified_skills),
            user_evidence_text=_evidence or "None provided.",
            special_instructions_text=_instructions or "None.",
            requires_cl="yes" if requires_cl else "no",
            cover_letter_instruction=cover_letter_instruction,
        )
        logger.info("[tailor_resume] sending to %s (quality) — prompt %d chars",
                    self._quality.provider_name, len(prompt))
        result = await self._quality.complete(prompt, system=_TAILOR_SYSTEM, max_tokens=3500)
        logger.info("[tailor_resume] DONE %.2fs — output %d chars", time.monotonic() - t0, len(result))
        return result

    async def patch_resume(
        self,
        current_resume: str,
        user_request: str,
        evidence_notes: str = "",
    ) -> str:
        """Apply a targeted edit to an already-generated resume.

        Returns only the changed section(s) wrapped in <section name="..."> tags.
        Caller uses apply_patch() in resume/generator.py to splice back in.
        evidence_notes: optional context from skill evidence — pass this so the AI can
        add work experience entries or skills that are verified but absent from the current resume.
        """
        logger.info("[patch_resume] START — resume %d chars  request=%r  evidence=%d chars",
                    len(current_resume), user_request[:120], len(evidence_notes))
        t0 = time.monotonic()
        evidence_block = (
            f"<evidence_notes>\n{evidence_notes[:2000]}\n</evidence_notes>\n"
            if evidence_notes.strip() else ""
        )
        prompt = _PATCH_PROMPT.format(
            current_resume=current_resume,
            evidence_block=evidence_block,
            user_request=user_request[:600],   # safety cap — handler already enforces this
        )
        logger.info("[patch_resume] sending to %s (quality) — prompt %d chars",
                    self._quality.provider_name, len(prompt))
        result = await self._quality.complete(prompt, system=_PATCH_SYSTEM, max_tokens=1500)
        logger.info("[patch_resume] DONE %.2fs — output %d chars", time.monotonic() - t0, len(result))
        return result

    async def write_cover_letter(
        self, job: dict, user_profile: dict, fit: dict
    ) -> str:
        logger.info("[write_cover_letter] START — job=%r  fit_score=%s",
                    job.get("title", "?"), fit.get("fit_score", "?"))
        t0 = time.monotonic()
        slim_profile = {
            "name": user_profile.get("name", ""),
            "skills": [s["skill_name"] for s in user_profile.get("skills", [])
                       if s.get("status", "").startswith("verified_")],
            "role":   user_profile.get("role", ""),
        }
        prompt = _COVER_LETTER_PROMPT.format(
            jd_text=_slim_job(job, ("title", "company", "location", "required_skills",
                                    "preferred_skills", "cover_letter_keywords", "seniority")),
            user_profile_json=_jc(slim_profile),
            fit_json=_jc({"score": fit.get("fit_score"), "matched": fit.get("matched_skills"),
                          "gaps": fit.get("missing_required")}),
        )
        logger.info("[write_cover_letter] sending to %s (quality) — prompt %d chars",
                    self._quality.provider_name, len(prompt))
        result = await self._quality.complete(prompt, system=_COVER_LETTER_SYSTEM, max_tokens=800)
        logger.info("[write_cover_letter] DONE %.2fs — output %d chars", time.monotonic() - t0, len(result))
        return result

    async def expand_role_titles(self, role_titles: str) -> list[str]:
        """Expand comma-separated role titles into 3 job-board-friendly search variants."""
        logger.info("[expand_roles] START — input=%r", role_titles[:80])
        t0 = time.monotonic()
        if cached := cache.get("expand_roles", role_titles):
            logger.info("[expand_roles] CACHE HIT (%.2fs)", time.monotonic() - t0)
            return cached
        prompt = _EXPAND_ROLES_PROMPT.format(role_titles=role_titles)
        raw = await self._fast.complete_json(prompt, system=_EXPAND_ROLES_SYSTEM, max_tokens=150)
        result = _parse_json(raw)
        if not isinstance(result, list):
            result = [role_titles]
        logger.info("[expand_roles] DONE %.2fs — %d variants: %s",
                    time.monotonic() - t0, len(result), result)
        cache.put("expand_roles", result, role_titles)
        return result

    async def parse_and_analyze_fit(self, raw_jd_text: str, user_profile: dict) -> tuple[dict, dict]:
        """
        Parse a job description AND analyze fit in a single AI call.
        Returns (parsed_job, fit_result) — same shapes as parse_job() and analyze_fit().
        Use this in bulk-processing loops to halve the number of API calls.
        """
        logger.info("[parse_and_fit] START — jd %d chars  skills=%d",
                    len(raw_jd_text), len(user_profile.get("skills", [])))
        t0 = time.monotonic()
        norm = _norm(raw_jd_text)
        if cached := cache.get("parse_and_fit", norm, user_profile):
            logger.info("[parse_and_fit] CACHE HIT (%.2fs)", time.monotonic() - t0)
            return cached["parsed"], cached["fit"]
        slim_skills = [s["skill_name"] for s in user_profile.get("skills", [])]
        prompt = _PARSE_AND_FIT_PROMPT.format(
            jd_text=norm[:3000],
            skill_graph_json=_jc(slim_skills),
            variant_tags=", ".join(user_profile.get("variant_tags", ["general"])),
        )
        logger.info("[parse_and_fit] sending to %s — prompt %d chars", self._fast.provider_name, len(prompt))
        t_ai = time.monotonic()
        raw = await self._fast.complete_json(prompt, system=_PARSE_AND_FIT_SYSTEM, max_tokens=1200)
        logger.info("[parse_and_fit] AI responded in %.2fs — raw %d chars", time.monotonic() - t_ai, len(raw))
        result = _parse_json(raw)
        parsed = result.get("parsed") or {}
        fit    = result.get("fit") or {}
        logger.info("[parse_and_fit] DONE %.2fs — title=%r  fit_score=%s  action=%r",
                    time.monotonic() - t0, parsed.get("title"), fit.get("fit_score"), fit.get("action"))
        cache.put("parse_and_fit", {"parsed": parsed, "fit": fit}, norm, user_profile)
        return parsed, fit

    async def answer_screening_questions(
        self, questions: list[str], job: dict, user_profile: dict
    ) -> list[dict]:
        logger.info("[answer_screening] START — job=%r  questions=%d",
                    job.get("title", "?"), len(questions))
        t0 = time.monotonic()
        slim_profile = {
            "name": user_profile.get("name", ""),
            "skills": [s["skill_name"] for s in user_profile.get("skills", [])
                       if s.get("status", "").startswith("verified_")],
            "work_history": user_profile.get("work_history", []),
        }
        prompt = _SCREENING_PROMPT.format(
            questions="\n".join(f"- {q}" for q in questions),
            jd_text=_slim_job(job, ("title", "company", "required_skills", "preferred_skills")),
            user_profile_json=_jc(slim_profile),
        )
        logger.info("[answer_screening] sending to %s (quality) — prompt %d chars",
                    self._quality.provider_name, len(prompt))
        t_ai = time.monotonic()
        raw = await self._quality.complete_json(prompt, system=_SCREENING_SYSTEM, max_tokens=600)
        logger.info("[answer_screening] AI responded in %.2fs — raw %d chars", time.monotonic() - t_ai, len(raw))
        result = _parse_json(raw)
        logger.info("[answer_screening] DONE %.2fs — answered %d questions", time.monotonic() - t0, len(result))
        return result
