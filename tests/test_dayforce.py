import json
import unittest
from unittest.mock import Mock, patch

from backend.job_board_discovery import discover_job_board
from backend.job_collection import _dayforce_detail_candidate
from job_platforms import detect_job_platform


DAYFORCE_URL = "https://jobs.dayforcehcm.com/golden1/CANDIDATEPORTAL"


class DayforceDiscoveryTest(unittest.TestCase):
    def test_outbound_warning_destination_is_followed_and_classified(self):
        warning_page = f"""
        <html><body>
          <a href="#external-warning" data-external-url="{DAYFORCE_URL}">Search Jobs</a>
          <div id="external-warning"><p>You are about to leave this website.</p>
            <a href="{DAYFORCE_URL}">Proceed</a></div>
        </body></html>
        """
        with patch("backend.job_board_discovery.fetch_page") as fetch:
            fetch.side_effect = [
                ("https://www.golden1.com/discoverg1/careers", warning_page),
                (DAYFORCE_URL, "<html><title>Job Board | Dayforce Jobs</title></html>"),
            ]
            result = discover_job_board(
                "https://www.golden1.com",
                "https://www.golden1.com/discoverg1/careers",
            )
        self.assertEqual(result.job_board_url, DAYFORCE_URL)
        self.assertEqual(result.job_board_type, "Dayforce")
        self.assertEqual(result.classification_confidence, "High")
        self.assertEqual(fetch.call_args_list[-1].args[1], DAYFORCE_URL)
        self.assertEqual(detect_job_platform(DAYFORCE_URL), "Dayforce")

    def test_public_detail_state_maps_to_candidate_fields(self):
        posting = {
            "jobPostingId": 52843,
            "jobReqId": 8349,
            "jobTitle": "Member Service Specialist II (Part-Time)",
            "postingStartTimestampUTC": "2026-08-21T10:00:00+00:00",
            "isoCurrencyRegion": "USD",
            "postingLocations": [{"formattedAddress": "Lincoln, CA 95648, USA"}],
            "jobPostingAttributes": [
                {"name": "PayType", "value": "Hourly"},
                {"name": "HiringMinRate", "value": 22},
                {"name": "HiringMaxRate", "value": 24},
            ],
            "jobPostingContent": {
                "jobDescriptionHeader": "",
                "jobDescription": "<p>Department: Lincoln Branch Job Code: 2201 Pay Range: $22.00 - $24.00 Hourly. Medical and dental insurance.</p>",
                "jobDescriptionFooter": "",
            },
        }
        payload = {"props": {"pageProps": {"dehydratedState": {"queries": [
            {"state": {"data": posting}}
        ]}}}}
        response = Mock()
        response.url = "https://jobs.dayforcehcm.com/en-US/golden1/CANDIDATEPORTAL/jobs/52843"
        response.text = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response

        candidate = _dayforce_detail_candidate(session, response.url)
        self.assertEqual(candidate.title, "Member Service Specialist II (Part-Time)")
        self.assertEqual(candidate.external_job_id, "8349")
        self.assertEqual(candidate.department, "Lincoln Branch")
        self.assertEqual(candidate.employment_type.lower(), "part-time")
        self.assertEqual(candidate.pay_min, 22)
        self.assertEqual(candidate.pay_max, 24)
        self.assertEqual(candidate.pay_period, "hourly")
        self.assertIn("Medical and dental insurance", candidate.description)


if __name__ == "__main__":
    unittest.main()
