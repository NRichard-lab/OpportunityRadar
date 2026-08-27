from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from threading import Event
from typing import Any, Callable, Iterable

from backend.match_constants import MATCH_ALGORITHM_VERSION, job_match_fingerprint
from backend.repository import OpportunityRepository, match_label
from backend.utility_tasks import UtilityCancelled


ProgressCallback = Callable[..., None]
TRACKED_KEYWORDS = (
    "active directory", "automation", "azure", "banking", "cloud", "compliance",
    "credit union", "cybersecurity", "database", "excel", "financial services",
    "fiserv", "help desk", "infrastructure", "jack henry", "kubernetes", "leadership",
    "linux", "microsoft 365", "networking", "operations", "powershell", "project management",
    "risk", "security", "sql", "symitar", "terraform", "vendor management",
    "virtualization", "windows server",
)
STOP_WORDS = {
    "about", "ability", "company", "experience", "including", "position", "preferred",
    "required", "requirements", "responsibilities", "skills", "strong", "their", "there",
    "these", "those", "through", "using", "with", "years", "your",
}


@dataclass(frozen=True)
class MatchContext:
    resume: dict[str, Any]
    version: str
    normalized_text: str


class ResumeMatchService:
    def __init__(self, repository: OpportunityRepository) -> None:
        self.repository = repository

    def match_job(self, job_id: str) -> dict[str, Any]:
        context = self._context()
        if context is None:
            raise ValueError("Upload an active resume before matching this job.")
        job = self.repository.get_job(job_id)
        self._match_and_persist(context, job)
        return self.repository.get_job(job_id)

    def match_jobs_if_needed(self, job_ids: Iterable[str]) -> dict[str, Any]:
        context = self._context()
        if context is None:
            return {"matched": 0, "failed": 0, "skipped": len(set(job_ids)), "reason": "No active resume."}
        matched = failed = skipped = 0
        for job_id in dict.fromkeys(job_ids):
            try:
                job = self.repository.get_job(job_id)
                if job.get("matchStatus") == "Matched":
                    skipped += 1
                    continue
                self._match_and_persist(context, job)
                matched += 1
            except Exception as exc:
                failed += 1
                self._record_failure(context, job_id, exc)
                logging.exception("Resume matching failed for job %s; the saved job was retained.", job_id)
        return {"matched": matched, "failed": failed, "skipped": skipped}

    def rematch_all(self, progress: ProgressCallback, cancelled: Event) -> dict[str, Any]:
        context = self._context()
        if context is None:
            raise ValueError("Upload an active resume before rematching jobs.")
        jobs = [
            job for job in self.repository.list_jobs()
            if str(job.get("status") or "").casefold() == "open"
            and job.get("id") and job.get("title") and job.get("sourceUrl")
        ]
        matched = failed = 0
        for index, job in enumerate(jobs, start=1):
            if cancelled.is_set():
                raise UtilityCancelled("Cancelled by user.")
            try:
                self._match_and_persist(context, job)
                matched += 1
            except Exception as exc:
                failed += 1
                self._record_failure(context, job["id"], exc)
                logging.exception("Resume matching failed for job %s.", job["id"])
            progress(
                index,
                len(jobs),
                f"{job.get('companyName', '')} - {job.get('title', '')}",
                {
                    "jobsProcessed": index,
                    "jobsMatched": matched,
                    "jobsFailed": failed,
                    "jobsRemaining": len(jobs) - index,
                },
            )
        return {"jobsProcessed": len(jobs), "jobsMatched": matched, "jobsFailed": failed}

    def _context(self) -> MatchContext | None:
        resume = self.repository.get_resume()
        if resume is None:
            return None
        legacy_notes = str(resume.get("notes") or "").casefold()
        legacy_experience = str(resume.get("yearsExperienceSummary") or "").casefold()
        if "parsing is stubbed" in legacy_notes or legacy_experience.startswith("todo: parse"):
            raise ValueError("Re-upload the active resume so Opportunity Radar can read its PDF or DOCX contents.")
        text = str(resume.get("extractedText") or resume.get("rawText") or "").strip()
        if not text:
            raise ValueError("The active resume does not contain readable text.")
        return MatchContext(resume, str(resume.get("version") or resume.get("id") or ""), normalize(text))

    def _match_and_persist(self, context: MatchContext, job: dict[str, Any]) -> dict[str, Any]:
        details = calculate_match(context.normalized_text, job)
        return self.repository.upsert_resume_fit_result(
            job_id=job["id"], resume_version=context.version,
            job_fingerprint=job_match_fingerprint(job), status="Matched",
            score=details["score"], details=details,
        )

    def _record_failure(self, context: MatchContext, job_id: str, exc: Exception) -> None:
        try:
            job = self.repository.get_job(job_id)
            self.repository.upsert_resume_fit_result(
                job_id=job_id, resume_version=context.version,
                job_fingerprint=job_match_fingerprint(job), status="Failed",
                score=None, details={}, error=str(exc),
            )
        except Exception:
            logging.exception("Could not persist resume-match failure for job %s.", job_id)


def calculate_match(normalized_resume: str, job: dict[str, Any]) -> dict[str, Any]:
    normalized_job = normalize(f"{job.get('title', '')} {job.get('description', '')}")
    job_keywords = extract_keywords(normalized_job)
    matched_keywords = [keyword for keyword in job_keywords if keyword in normalized_resume]
    missing_keywords = [keyword for keyword in job_keywords if keyword not in normalized_resume][:12]
    title_terms = meaningful_words(normalize(str(job.get("title") or "")))
    matched_title_terms = [term for term in title_terms if term in normalized_resume]
    keyword_score = (len(matched_keywords) / len(job_keywords) * 60) if job_keywords else 0
    title_score = (len(matched_title_terms) / len(title_terms) * 25) if title_terms else 0
    experience_score = 10 if re.search(r"\b(?:[3-9]|1\d)\+?\s+years?\b", normalized_resume) else 0
    location_score = 5 if str(job.get("workType") or "").casefold() == "remote" or "remote" in normalized_resume else 2
    score = min(100, round(keyword_score + title_score + experience_score + location_score))
    return {
        "score": score,
        "label": match_label(score),
        "matchedKeywords": matched_keywords,
        "missingKeywords": missing_keywords,
        "experienceAlignment": "Experience duration is present in the resume." if experience_score else "No explicit experience duration was detected.",
        "titleAlignment": f"{len(matched_title_terms)} title term(s) overlap." if matched_title_terms else "No direct title-term overlap was detected.",
        "summary": f"{len(matched_keywords)} of {len(job_keywords)} relevant job terms were found in the active resume.",
        "algorithmVersion": MATCH_ALGORITHM_VERSION,
    }


def extract_keywords(text: str) -> list[str]:
    tracked = [keyword for keyword in TRACKED_KEYWORDS if keyword in text]
    dynamic = meaningful_words(text)[:30]
    return list(dict.fromkeys([*tracked, *dynamic]))


def meaningful_words(text: str) -> list[str]:
    return list(dict.fromkeys(
        word for word in text.split() if len(word) > 4 and word not in STOP_WORDS
    ))


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+.#\s-]", " ", value.casefold())).strip()
