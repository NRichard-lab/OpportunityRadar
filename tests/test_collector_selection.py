from __future__ import annotations

import unittest

from collectors.base import pick_collector


class CollectorSelectionTests(unittest.TestCase):
    def test_detected_board_vendor_overrides_stale_supported_platform(self) -> None:
        cases = [
            (
                "https://www.paycomonline.net/v4/ats/web.php/portal/abc/career-page",
                "Workday",
                "PaycomCollector",
            ),
            (
                "https://recruiting.ultipro.com/TENANT/JobBoard/123/",
                "Dayforce",
                "UKGCollector",
            ),
            (
                "https://example.wd5.myworkdayjobs.com/examplecareers",
                "ADP Workforce Now",
                "WorkdayCollector",
            ),
            (
                "https://tenant.csod.com/ux/ats/careersite/4/home?c=tenant",
                "Paycom",
                "CSODCollector",
            ),
            (
                "https://tenant.hrmdirect.com/employment/job-openings.php?search=true",
                "Workday",
                "ClearCompanyCollector",
            ),
            (
                "https://secure7.saashr.com/ta/6202174.careers?lang=en-US",
                "UKG",
                "UKGReadyCollector",
            ),
        ]
        for board_url, stale_platform, expected in cases:
            with self.subTest(board_url=board_url, stale_platform=stale_platform):
                collector = pick_collector(
                    {"Job Board URL": board_url, "Job Platform": stale_platform},
                    delay_seconds=0,
                )
                self.assertEqual(type(collector).__name__, expected)

    def test_stored_platform_remains_fallback_for_an_official_embedding_page(self) -> None:
        collector = pick_collector(
            {
                "Job Board URL": "https://www.example-bank.test/careers",
                "Job Platform": "ADP Workforce Now",
            },
            delay_seconds=0,
        )
        self.assertEqual(type(collector).__name__, "ADPCollector")


if __name__ == "__main__":
    unittest.main()
