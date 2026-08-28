from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document

from backend.resume_files import _join_bounded_text, _validate_docx_archive, build_resume_profile


def make_docx(text: str = "Cloud security and PowerShell automation") -> bytes:
    document = Document()
    document.add_paragraph(text)
    contents = BytesIO()
    document.save(contents)
    return contents.getvalue()


class _FakeArchive:
    def __init__(self, entries: list[SimpleNamespace], corrupt_name: str | None = None) -> None:
        self._entries = entries
        self._corrupt_name = corrupt_name

    def __enter__(self) -> "_FakeArchive":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def infolist(self) -> list[SimpleNamespace]:
        return self._entries

    def testzip(self) -> str | None:
        return self._corrupt_name


class ResumeUploadSecurityTests(unittest.TestCase):
    def test_accepts_pdf_with_readable_text(self) -> None:
        fake_reader = SimpleNamespace(
            is_encrypted=False,
            pages=[SimpleNamespace(extract_text=lambda: "Azure security engineering")],
        )
        with patch("backend.resume_files.PdfReader", return_value=fake_reader):
            profile = build_resume_profile("resume.pdf", b"%PDF-synthetic")
        self.assertEqual(profile["fileName"], "resume.pdf")
        self.assertEqual(profile["extractedText"], "Azure security engineering")

    def test_rejects_upload_over_configured_limit_without_large_allocation(self) -> None:
        with patch("backend.resume_files.MAX_RESUME_UPLOAD_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "upload limit"):
                build_resume_profile("resume.pdf", b"%PDF-1234")

    def test_rejects_extension_signature_mismatches(self) -> None:
        with self.assertRaisesRegex(ValueError, "do not match the PDF extension"):
            build_resume_profile("resume.pdf", b"PK\x03\x04not-a-pdf")
        with self.assertRaisesRegex(ValueError, "do not match the DOCX extension"):
            build_resume_profile("resume.docx", b"%PDF-not-a-docx")

    def test_rejects_malformed_docx_and_pdf_with_safe_messages(self) -> None:
        with self.assertRaisesRegex(ValueError, "DOCX file is malformed"):
            build_resume_profile("resume.docx", b"PK\x03\x04not-a-zip")

        with patch("backend.resume_files.PdfReader", side_effect=RuntimeError("sensitive parser detail")):
            with self.assertRaises(ValueError) as raised:
                build_resume_profile("resume.pdf", b"%PDF-not-a-real-pdf")
        self.assertEqual(str(raised.exception), "The resume file is malformed or could not be read.")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_normalizes_docx_archive_parser_failures(self) -> None:
        for exception_type in (RuntimeError, NotImplementedError, EOFError):
            with self.subTest(exception_type=exception_type.__name__):
                secret_error = exception_type("sensitive archive parser detail")
                with patch("backend.resume_files.zipfile.ZipFile", side_effect=secret_error):
                    with self.assertRaises(ValueError) as raised:
                        _validate_docx_archive(Path("synthetic.docx"))
                self.assertEqual(str(raised.exception), "The DOCX file is malformed or could not be read.")
                self.assertNotIn("sensitive", str(raised.exception))

    def test_rejects_pdf_page_count_over_limit(self) -> None:
        fake_reader = SimpleNamespace(
            is_encrypted=False,
            pages=[SimpleNamespace(extract_text=lambda: "one"), SimpleNamespace(extract_text=lambda: "two")],
        )
        with patch("backend.resume_files.MAX_RESUME_PDF_PAGES", 1), patch(
            "backend.resume_files.PdfReader", return_value=fake_reader
        ):
            with self.assertRaisesRegex(ValueError, "at most 1 pages"):
                build_resume_profile("resume.pdf", b"%PDF-synthetic")

    def test_rejects_docx_archive_file_and_expanded_size_limits(self) -> None:
        contents = make_docx()
        with patch("backend.resume_files.MAX_RESUME_DOCX_FILES", 1):
            with self.assertRaisesRegex(ValueError, "too many files"):
                build_resume_profile("resume.docx", contents)
        with patch("backend.resume_files.MAX_RESUME_DOCX_UNCOMPRESSED_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "expands beyond"):
                build_resume_profile("resume.docx", contents)

    def test_rejects_unsafe_docx_compression_ratio_without_large_allocation(self) -> None:
        entries = [
            SimpleNamespace(filename="[Content_Types].xml", file_size=1, compress_size=1),
            SimpleNamespace(
                filename="word/document.xml",
                file_size=(1024 * 1024) + 1,
                compress_size=1,
            ),
        ]
        with patch("backend.resume_files.zipfile.ZipFile", return_value=_FakeArchive(entries)):
            with self.assertRaisesRegex(ValueError, "unsafe compression ratio"):
                _validate_docx_archive(Path("synthetic.docx"))

    def test_temporary_upload_is_cleaned_after_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            profile = build_resume_profile("../private/resume.docx", make_docx(), temporary_root=temporary_root)
            self.assertEqual(profile["fileName"], "resume.docx")
            self.assertEqual(list(temporary_root.iterdir()), [])

            with patch("backend.resume_files.PdfReader", side_effect=RuntimeError("parser failure")):
                with self.assertRaisesRegex(ValueError, "malformed or could not be read"):
                    build_resume_profile(
                        "resume.pdf",
                        b"%PDF-synthetic",
                        temporary_root=temporary_root,
                    )
            self.assertEqual(list(temporary_root.iterdir()), [])

    def test_rejects_extracted_text_over_limit_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            with patch("backend.resume_files.MAX_RESUME_EXTRACTED_TEXT_CHARS", 4):
                with self.assertRaisesRegex(ValueError, "too much text"):
                    build_resume_profile("resume.docx", make_docx("five!"), temporary_root=temporary_root)
            self.assertEqual(list(temporary_root.iterdir()), [])

    def test_extracted_text_limit_stops_incremental_consumption(self) -> None:
        consumed: list[str] = []

        def fragments():
            for value in ("four", "more", "must-not-be-read"):
                consumed.append(value)
                yield value

        with patch("backend.resume_files.MAX_RESUME_EXTRACTED_TEXT_CHARS", 8):
            with self.assertRaisesRegex(ValueError, "too much text"):
                _join_bounded_text(fragments())
        self.assertEqual(consumed, ["four", "more"])


if __name__ == "__main__":
    unittest.main()
