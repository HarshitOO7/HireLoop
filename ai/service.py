import json
import re
from ai.base import AIProvider
from ai import cache


def _parse_json(raw: str) -> dict | list:
    """Strip markdown fences and parse JSON. Models sometimes wrap despite instructions."""
    text = raw.strip()
    # Remove ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text.strip())
    return json.loads(text.strip())

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
        if cached := cache.get("parse_job", raw_jd_text):
            return cached
        prompt = _PARSE_JOB_PROMPT.format(jd_text=raw_jd_text)
        raw = await self._fast.complete(prompt, system=_PARSE_JOB_SYSTEM)
        result = _parse_json(raw)
        cache.put("parse_job", result, raw_jd_text)
        return result

    async def parse_resume(self, resume_text: str) -> dict:
        if cached := cache.get("parse_resume", resume_text):
            return cached
        prompt = _PARSE_RESUME_PROMPT.format(resume_text=resume_text)
        raw = await self._fast.complete(prompt, system=_PARSE_RESUME_SYSTEM)
        result = _parse_json(raw)
        cache.put("parse_resume", result, resume_text)
        return result

    async def analyze_fit(self, job: dict, user_profile: dict) -> dict:
        if cached := cache.get("analyze_fit", job, user_profile):
            return cached
        prompt = _FIT_PROMPT.format(
            jd_text=json.dumps(job, indent=2),
            skill_graph_json=json.dumps(user_profile.get("skills", []), indent=2),
            variant_tags=", ".join(user_profile.get("variant_tags", ["general"])),
        )
        raw = await self._fast.complete(prompt, system=_FIT_SYSTEM)
        result = _parse_json(raw)
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
        prompt = _TAILOR_PROMPT.format(
            variant_tag=fit.get("best_resume_variant", "general"),
            base_resume_text=base_resume,
            jd_text=json.dumps(job, indent=2),
            verified_skills_json=json.dumps(verified_skills, indent=2),
            user_evidence_text=user_evidence or "None provided.",
            requires_cl=str(job.get("requires_cover_letter", False)),
        )
        return await self._quality.complete(prompt, system=_TAILOR_SYSTEM)

    async def edit_resume(self, current_resume: str, instruction: str) -> str:
        """Apply a targeted edit to an already-generated resume.

        Only changes what the instruction specifies — the rest of the resume
        is preserved verbatim. Use this instead of tailor_resume() when the
        user asks to change one line, reword a bullet, adjust a section, etc.
        """
        prompt = (
            "You are editing a resume. Apply ONLY the change described below.\n"
            "Do NOT rewrite, restructure, or touch anything else.\n"
            "Return the complete resume with only that specific change made.\n\n"
            f"## Current Resume\n{current_resume}\n\n"
            f"## Instruction\n{instruction}\n\n"
            "Return the full resume with only this edit applied."
        )
        return await self._quality.complete(prompt, system="You are a precise resume editor.")

    async def write_cover_letter(
        self, job: dict, user_profile: dict, fit: dict
    ) -> str:
        prompt = _COVER_LETTER_PROMPT.format(
            jd_text=json.dumps(job, indent=2),
            user_profile_json=json.dumps(user_profile, indent=2),
            fit_json=json.dumps(fit, indent=2),
        )
        return await self._quality.complete(prompt, system=_COVER_LETTER_SYSTEM)

    async def answer_screening_questions(
        self, questions: list[str], job: dict, user_profile: dict
    ) -> list[dict]:
        prompt = _SCREENING_PROMPT.format(
            questions="\n".join(f"- {q}" for q in questions),
            jd_text=json.dumps(job, indent=2),
            user_profile_json=json.dumps(user_profile, indent=2),
        )
        raw = await self._quality.complete(prompt, system=_SCREENING_SYSTEM)
        return _parse_json(raw)
