import json
import logging
import re
import time
from ai.base import AIProvider
from ai import cache

logger = logging.getLogger(__name__)


def _parse_json(raw: str) -> dict | list:
    """Strip markdown fences and parse JSON. Models sometimes wrap despite instructions."""
    text = raw.strip()
    # Remove ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text.strip())
    return json.loads(text.strip())


def _jc(obj) -> str:
    """Compact JSON — no indentation, no wasted tokens."""
    return json.dumps(obj, separators=(",", ":"))


def _slim_job(job: dict, keys: tuple) -> str:
    """Extract only the needed keys from a parsed job dict and return compact JSON."""
    return _jc({k: job[k] for k in keys if k in job})

_FIT_SYSTEM = """You are a job fit analyzer. Be conservative and honest. Never inflate scores.
Return ONLY valid JSON. No preamble, no markdown fences, no explanation."""

_FIT_PROMPT = """Analyze the fit between this job and candidate profile.

## Job Description
{jd_text}

## Candidate Skill Graph
{skill_graph_json}

## Resume Variants Available
{variant_tags}

Return this exact JSON structure:
{{
  "fit_score": <0-100 integer>,
  "matched_skills": ["skill_name"],
  "missing_required": [{{"skill": "...", "importance": "required|preferred|nice"}}],
  "requires_cover_letter": <true|false>,
  "best_resume_variant": "backend|lead|fullstack|general",
  "gap_summary": "2 sentence honest assessment",
  "action": "apply|consider|skip",
  "verify_questions": ["Have you used X in production? Where?"],
  "recruiter_info_found": "name/contact from JD, or null"
}}"""

_PARSE_JOB_SYSTEM = """Extract structured information from job descriptions.
Return ONLY valid JSON. No preamble, no markdown fences."""

_PARSE_JOB_PROMPT = """Parse this job description into structured data.

{jd_text}

Return this exact JSON structure:
{{
  "title": "...",
  "company": "...",
  "location": "...",
  "salary_range": "... or null",
  "remote": <true|false|null>,
  "required_skills": ["skill"],
  "preferred_skills": ["skill"],
  "years_experience": <integer or null>,
  "seniority": "junior|mid|senior|lead|staff|null",
  "requires_cover_letter": <true|false>,
  "cover_letter_keywords": ["..."],
  "recruiter_name": "... or null",
  "recruiter_contact": "... or null"
}}"""

_PARSE_RESUME_SYSTEM = """Extract skills and experience from resumes.
Return ONLY valid JSON. No preamble, no markdown fences."""

_PARSE_RESUME_PROMPT = """Parse this resume and extract all skills with confidence levels.

{resume_text}

Return this exact JSON structure:
{{
  "name": "...",
  "skills": [
    {{
      "skill_name": "...",
      "confidence": "high|medium|low",
      "evidence": "brief note from resume"
    }}
  ],
  "work_history": [
    {{
      "company": "...",
      "role": "...",
      "duration_months": <integer>,
      "last_used_year": <integer>
    }}
  ],
  "variant_tags": ["backend", "fullstack", "lead"]
}}"""

_TAILOR_SYSTEM = """You are an expert resume writer. Tailor resumes truthfully using only verified evidence.

HARD RULES:
- Only include skills where status starts with "verified_" in the skill graph
- Never invent accomplishments — use STAR format for all bullets
- Mirror JD keywords for ATS optimization (do not keyword-stuff)
- Max 1 page under 5yr experience, 2 pages for 5yr+
- If cover letter required: append after ---COVER LETTER--- separator"""

_TAILOR_PROMPT = """Tailor this resume for the job.

## Base Resume ({variant_tag})
{base_resume_text}

## Job Description
{jd_text}

## Verified Skill Graph (only use these)
{verified_skills_json}

## User Evidence Notes
{user_evidence_text}

## Cover Letter Required
{requires_cl}

Output the full tailored resume in Markdown.
If cover letter required → append after ---COVER LETTER--- separator.
Append ---CHANGES--- with 5 specific edits made and why."""

_COVER_LETTER_SYSTEM = "You are an expert at writing compelling, honest cover letters."

_COVER_LETTER_PROMPT = """Write a cover letter for this job application.

## Job
{jd_text}

## Candidate Profile
{user_profile_json}

## Fit Analysis
{fit_json}

Write a professional, personalized cover letter. 3 paragraphs max. No fluff."""

_SCREENING_SYSTEM = "Answer screening questions honestly based on the candidate's verified experience."

_SCREENING_PROMPT = """Answer these screening questions for a job application.

## Questions
{questions}

## Job
{jd_text}

## Candidate Profile
{user_profile_json}

Return a JSON array: [{{"question": "...", "answer": "..."}}]"""


class HireLoopAI:
    """All 6 HireLoop AI tasks. Uses tiered providers:
    - fast_provider: parse_job, parse_resume, analyze_fit (high volume)
    - quality_provider: tailor_resume, write_cover_letter, answer_screening_questions
    """

    def __init__(self, fast_provider: AIProvider, quality_provider: AIProvider):
        self._fast = fast_provider
        self._quality = quality_provider

    async def parse_job(self, raw_jd_text: str) -> dict:
        logger.info("[parse_job] START — jd text %d chars", len(raw_jd_text))
        t0 = time.monotonic()
        if cached := cache.get("parse_job", raw_jd_text):
            logger.info("[parse_job] CACHE HIT (%.2fs)", time.monotonic() - t0)
            return cached
        prompt = _PARSE_JOB_PROMPT.format(jd_text=raw_jd_text)
        logger.info("[parse_job] sending to %s — prompt %d chars", self._fast.provider_name, len(prompt))
        t_ai = time.monotonic()
        raw = await self._fast.complete(prompt, system=_PARSE_JOB_SYSTEM)
        logger.info("[parse_job] AI responded in %.2fs — raw %d chars", time.monotonic() - t_ai, len(raw))
        result = _parse_json(raw)
        logger.info("[parse_job] DONE %.2fs — title=%r company=%r fit=%s skills_req=%d",
                    time.monotonic() - t0,
                    result.get("title"), result.get("company"),
                    result.get("fit_score"), len(result.get("required_skills", [])))
        cache.put("parse_job", result, raw_jd_text)
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
        raw = await self._fast.complete(prompt, system=_PARSE_RESUME_SYSTEM)
        logger.info("[parse_resume] AI responded in %.2fs — raw response length: %d chars", time.monotonic() - t_ai, len(raw))

        logger.info("[parse_resume] parsing JSON response...")
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
        slim_skills = [
            {"skill": s["skill_name"], "status": s.get("status", ""), "conf": s.get("confidence", "")}
            for s in user_profile.get("skills", [])
        ]
        prompt = _FIT_PROMPT.format(
            jd_text=_slim_job(job, ("title", "required_skills", "preferred_skills",
                                    "seniority", "years_experience", "cover_letter_required")),
            skill_graph_json=_jc(slim_skills),
            variant_tags=", ".join(user_profile.get("variant_tags", ["general"])),
        )
        logger.info("[analyze_fit] sending to %s — prompt %d chars", self._fast.provider_name, len(prompt))
        t_ai = time.monotonic()
        raw = await self._fast.complete(prompt, system=_FIT_SYSTEM)
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
    ) -> str:
        variant = fit.get("best_resume_variant", "general")
        logger.info("[tailor_resume] START — job=%r  variant=%s  verified_skills=%d  base_resume=%d chars",
                    job.get("title", "?"), variant, len(verified_skills), len(base_resume))
        t0 = time.monotonic()
        prompt = _TAILOR_PROMPT.format(
            variant_tag=variant,
            base_resume_text=base_resume,
            jd_text=_slim_job(job, ("title", "company", "required_skills", "preferred_skills",
                                    "seniority", "years_experience", "cover_letter_keywords")),
            verified_skills_json=_jc(verified_skills),
            user_evidence_text=user_evidence or "None provided.",
            requires_cl=str(job.get("requires_cover_letter", False)),
        )
        logger.info("[tailor_resume] sending to %s (quality) — prompt %d chars",
                    self._quality.provider_name, len(prompt))
        result = await self._quality.complete(prompt, system=_TAILOR_SYSTEM)
        logger.info("[tailor_resume] DONE %.2fs — output %d chars", time.monotonic() - t0, len(result))
        return result

    async def edit_resume(self, current_resume: str, instruction: str) -> str:
        """Apply a targeted edit to an already-generated resume.

        Only changes what the instruction specifies — the rest of the resume
        is preserved verbatim. Use this instead of tailor_resume() when the
        user asks to change one line, reword a bullet, adjust a section, etc.
        """
        logger.info("[edit_resume] START — resume %d chars  instruction=%r",
                    len(current_resume), instruction[:120])
        t0 = time.monotonic()
        prompt = (
            "You are editing a resume. Apply ONLY the change described below.\n"
            "Do NOT rewrite, restructure, or touch anything else.\n"
            "Return the complete resume with only that specific change made.\n\n"
            f"## Current Resume\n{current_resume}\n\n"
            f"## Instruction\n{instruction}\n\n"
            "Return the full resume with only this edit applied."
        )
        logger.info("[edit_resume] sending to %s (quality) — prompt %d chars",
                    self._quality.provider_name, len(prompt))
        result = await self._quality.complete(prompt, system="You are a precise resume editor.")
        logger.info("[edit_resume] DONE %.2fs — output %d chars", time.monotonic() - t0, len(result))
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
            "work_history": user_profile.get("work_history", []),
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
        result = await self._quality.complete(prompt, system=_COVER_LETTER_SYSTEM)
        logger.info("[write_cover_letter] DONE %.2fs — output %d chars", time.monotonic() - t0, len(result))
        return result

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
        raw = await self._quality.complete(prompt, system=_SCREENING_SYSTEM)
        logger.info("[answer_screening] AI responded in %.2fs — raw %d chars", time.monotonic() - t_ai, len(raw))
        result = _parse_json(raw)
        logger.info("[answer_screening] DONE %.2fs — answered %d questions", time.monotonic() - t0, len(result))
        return result
