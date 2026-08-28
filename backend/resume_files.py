from __future__ import annotations

import tempfile
import zipfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from docx import Document
from pypdf import PdfReader

from backend.resume_matching import extract_keywords
from config import (
    MAX_RESUME_DOCX_FILES,
    MAX_RESUME_DOCX_UNCOMPRESSED_BYTES,
    MAX_RESUME_EXTRACTED_TEXT_CHARS,
    MAX_RESUME_PDF_PAGES,
    MAX_RESUME_UPLOAD_BYTES,
)


def build_resume_profile(filename: str, contents: bytes, *, temporary_root: Path | None = None) -> dict:
    suffix = Path(filename).suffix.casefold()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("Resume upload supports PDF and DOCX files.")
    if not contents:
        raise ValueError("The selected resume is empty.")
    if len(contents) > MAX_RESUME_UPLOAD_BYTES:
        raise ValueError("The resume exceeds the 10 MiB upload limit.")
    _validate_signature(suffix, contents)

    root = Path(temporary_root) if temporary_root is not None else None
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="opportunity-radar-resume-", dir=root) as temporary:
            upload_path = Path(temporary) / f"upload{suffix}"
            upload_path.write_bytes(contents)
            text = _extract_resume_text(upload_path, suffix)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The resume file is malformed or could not be read.") from exc
    text = text.strip()
    if not text:
        raise ValueError("No readable text was found in this resume.")
    if len(text) > MAX_RESUME_EXTRACTED_TEXT_CHARS:
        raise ValueError("The resume contains too much text to process safely.")
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


def _validate_signature(suffix: str, contents: bytes) -> None:
    if suffix == ".pdf" and not contents.startswith(b"%PDF-"):
        raise ValueError("The file contents do not match the PDF extension.")
    if suffix == ".docx" and not contents.startswith(b"PK\x03\x04"):
        raise ValueError("The file contents do not match the DOCX extension.")


def _extract_resume_text(path: Path, suffix: str) -> str:
    if suffix == ".pdf":
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Password-protected PDF resumes are not supported.")
        if len(reader.pages) > MAX_RESUME_PDF_PAGES:
            raise ValueError(f"PDF resumes may contain at most {MAX_RESUME_PDF_PAGES} pages.")
        return _join_bounded_text(page.extract_text() or "" for page in reader.pages)

    _validate_docx_archive(path)
    document = Document(path)
    return _join_bounded_text(paragraph.text for paragraph in document.paragraphs)


def _join_bounded_text(fragments: Iterable[str]) -> str:
    parts: list[str] = []
    character_count = 0
    for fragment in fragments:
        value = str(fragment or "")
        character_count += len(value) + (1 if parts else 0)
        if character_count > MAX_RESUME_EXTRACTED_TEXT_CHARS:
            raise ValueError("The resume contains too much text to process safely.")
        parts.append(value)
    return "\n".join(parts)


def _validate_docx_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ValueError("The DOCX file is missing required document content.")
            if len(entries) > MAX_RESUME_DOCX_FILES:
                raise ValueError("The DOCX archive contains too many files.")
            expanded_size = sum(max(0, entry.file_size) for entry in entries)
            if expanded_size > MAX_RESUME_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("The DOCX archive expands beyond the safe processing limit.")
            for entry in entries:
                if entry.file_size > 1024 * 1024 and entry.file_size > max(1, entry.compress_size) * 200:
                    raise ValueError("The DOCX archive has an unsafe compression ratio.")
            if archive.testzip() is not None:
                raise ValueError("The DOCX archive is corrupted.")
    except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
        raise ValueError("The DOCX file is malformed or could not be read.") from exc
