from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from docx import Document
from pypdf import PdfReader

from backend.resume_matching import extract_keywords


def build_resume_profile(filename: str, contents: bytes) -> dict:
    suffix = Path(filename).suffix.casefold()
    if suffix == ".pdf":
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(contents)).pages)
    elif suffix == ".docx":
        document = Document(io.BytesIO(contents))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    else:
        raise ValueError("Resume upload supports PDF and DOCX files.")
    text = text.strip()
    if not text:
        raise ValueError("No readable text was found in this resume.")
    version = f"resume-{uuid4()}"
    return {
        "id": version,
        "version": version,
        "fileName": Path(filename).name,
        "uploadedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "rawText": text,
        "extractedText": text,
        "skills": extract_keywords(text.casefold()),
        "titles": [],
        "yearsExperienceSummary": "",
        "notes": "Resume text extracted by Opportunity Radar.",
    }
