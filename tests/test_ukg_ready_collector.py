from __future__ import annotations

import unittest
from unittest.mock import Mock

from collectors.base import pick_collector
from collectors.ukg_ready_collector import UKGReadyCollector, build_ukg_ready_urls


BOARD_URL = "https://secure7.saashr.com/ta/6202174.careers?CareersSearch=&lang=en-US"


class UKGReadyCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-verity",
            "Company Name": "Verity Credit Union",
            "Job Board URL": BOARD_URL,
            "Job Platform": "",
        }

    def test_collects_public_requisitions_with_stable_destinations(self) -> None:
        payload = {
            "job_requisitions": [
                {
                    "id": 621242952,
                    "job_title": "Benefits & HRIS Generalist",
                    "location": {"city": "Seattle", "state": "WA", "country": "USA"},
                    "base_pay_from": 82553.0,
                    "base_pay_to": 123829.0,
                    "base_pay_frequency": "YEAR",
                    "employee_type": {"name": "FT Exempt"},
                    "job_description": "Hybrid current opening.",
                    "is_remote_job": False,
                },
                {
                    "id": 621238086,
                    "job_title": "Contact Center Agent I",
                    "location": {"city": "Seattle", "state": "WA", "country": "USA"},
                    "base_pay_from": 21.30,
                    "base_pay_to": 31.06,
                    "base_pay_frequency": "HOUR",
                    "job_description": "Current opening.",
                    "is_remote_job": False,
                },
            ],
            "_paging": {"offset": 1, "size": 100, "total": 2},
        }
        collector = UKGReadyCollector(delay_seconds=0)
        collector.fetch_page = Mock(return_value=payload)

        jobs = collector.collect(self.company)

        self.assertEqual([job.title for job in jobs], ["Benefits & HRIS Generalist", "Contact Center Agent I"])
        self.assertEqual((jobs[0].workType, jobs[0].payPeriod), ("Hybrid", "year"))
        self.assertIn("ShowJob=621242952", jobs[0].sourceUrl)
        self.assertTrue(all(job.rawData["structuredSource"] for job in jobs))
        self.assertEqual(type(pick_collector(self.company)).__name__, "UKGReadyCollector")

    def test_normalizes_only_saashr_tenant_boards(self) -> None:
        board, endpoint, tenant = build_ukg_ready_urls(BOARD_URL)
        self.assertEqual(tenant, "6202174")
        self.assertIn("/ta/6202174.careers", board)
        self.assertIn("/%7C6202174/job-requisitions", endpoint)
        with self.assertRaisesRegex(ValueError, "saashr.com"):
            build_ukg_ready_urls("https://example.com/ta/6202174.careers")


if __name__ == "__main__":
    unittest.main()
