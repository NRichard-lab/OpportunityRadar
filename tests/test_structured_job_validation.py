from __future__ import annotations

import unittest

from job_validation import is_valid_structured_job_title


class StructuredJobTitleValidationTests(unittest.TestCase):
    def test_structured_ats_titles_do_not_require_role_keywords_or_four_characters(self) -> None:
        self.assertTrue(is_valid_structured_job_title("CFO"))
        self.assertTrue(is_valid_structured_job_title("Collector"))
        self.assertTrue(is_valid_structured_job_title("Paralegal"))
        self.assertTrue(is_valid_structured_job_title("Director, Retirement Benefits - REMOTE"))

    def test_structured_ats_titles_still_reject_non_jobs(self) -> None:
        self.assertFalse(is_valid_structured_job_title("General Employment Application"))
        self.assertFalse(is_valid_structured_job_title("Join Our Talent Community"))
        self.assertFalse(is_valid_structured_job_title("Apply Now"))
        self.assertFalse(is_valid_structured_job_title("2 days ago"))


if __name__ == "__main__":
    unittest.main()
