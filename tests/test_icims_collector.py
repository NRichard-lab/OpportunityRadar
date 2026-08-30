from __future__ import annotations

import unittest
from unittest.mock import Mock

from collectors.icims_collector import ICIMSCollector, normalize_icims_board_url


BOARD_URL = "https://careers-bank.icims.com/jobs/search?ss=1&searchRelation=keyword_all"


def listing(job_id: int, requisition: str, title: str, *, next_url: str = "") -> str:
    next_link = f'<link rel="next" href="{next_url}">' if next_url else ""
    return f"""
    <html><head>{next_link}</head><body>
      <ul class="iCIMS_JobsTable">
        <li class="iCIMS_JobCardItem">
          <div class="header left"><span class="field-label">Location : Location</span><span>US-WA-Seattle</span></div>
          <div class="title"><a href="https://careers-bank.icims.com/jobs/{job_id}/{requisition}/job?in_iframe=1"><h3>{title}</h3></a></div>
          <div class="description">Current opening for {title}.</div>
          <div class="iCIMS_JobHeaderTag"><dt>Category</dt><dd>Banking</dd></div>
          <div class="iCIMS_JobHeaderTag"><dt>ID</dt><dd>{requisition}</dd></div>
          <div class="iCIMS_JobHeaderTag"><dt>Min</dt><dd>USD $20.00/Hr.</dd></div>
          <div class="iCIMS_JobHeaderTag"><dt>Max</dt><dd>USD $25.00/Hr.</dd></div>
        </li>
      </ul>
    </body></html>
    """


class FakeResponse:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text


class ICIMSCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-bank",
            "Company Name": "Example Bank",
            "Job Board URL": BOARD_URL,
            "Job Platform": "ICIMS",
        }

    def test_collects_and_paginates_public_iframe_cards(self) -> None:
        page_two = "https://careers-bank.icims.com/jobs/search?pr=1&searchRelation=keyword_all&in_iframe=1"
        collector = ICIMSCollector(delay_seconds=0)
        collector.get = Mock(
            side_effect=[
                FakeResponse(normalize_icims_board_url(BOARD_URL), listing(101, "2026-101", "Personal Banker", next_url=page_two)),
                FakeResponse(page_two, listing(102, "2026-102", "CSS")),
            ]
        )

        jobs = collector.collect(self.company)

        self.assertEqual([job.title for job in jobs], ["Personal Banker", "CSS"])
        self.assertEqual(len({job.sourceUrl for job in jobs}), 2)
        self.assertEqual((jobs[0].location, jobs[0].payMin, jobs[0].payMax), ("Seattle, WA, US", 20.0, 25.0))
        self.assertTrue(all(job.rawData["structuredSource"] for job in jobs))
        self.assertEqual((collector.candidate_count, collector.saved_count, collector.rejected_count), (2, 2, 0))

    def test_rejects_non_icims_hosts(self) -> None:
        with self.assertRaisesRegex(ValueError, "icims.com"):
            normalize_icims_board_url("https://example.com/jobs/search")


if __name__ == "__main__":
    unittest.main()
