import unittest
from unittest.mock import patch

from collectors.paylocity_collector import find_paylocity_listing_scope
from collectors.base import BaseCollector
from job_board_discovery import static_scan
from job_validation import is_valid_job_title


class FakeResponse:
    status_code = 404
    text = ""


class EmbeddedResponse:
    url = "https://example.com/careers"
    text = '<iframe src="https://recruitingbypaycor.com/career/CareerHome.action?clientId=abc123abc123abc123"></iframe>'


class FakeSession:
    def get(self, *_args, **_kwargs):
        return FakeResponse()


class FakeScope:
    def __init__(self, url: str, frames=None):
        self.url = url
        self.frames = frames or []


class EmbeddedJobBoardTests(unittest.TestCase):
    def test_static_scan_finds_embedded_provider(self):
        html = '<iframe src="https://recruiting.paylocity.com/recruiting/jobs/All/company-id/Example"></iframe>'
        with patch("job_board_discovery.fetch_html", return_value=("https://example.com/careers", html)):
            candidates = static_scan(
                "https://example.com/careers", "Example Credit Union", FakeSession(), "Static Link"
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].platform, "Paylocity")
        self.assertTrue(candidates[0].url.startswith("https://recruiting.paylocity.com/"))
        self.assertFalse(candidates[0].rejected)

    def test_paylocity_collector_uses_embedded_listing_frame(self):
        frame = FakeScope("https://recruiting.paylocity.com/recruiting/jobs/All/company-id/Example")
        page = FakeScope("https://example.com/careers", [FakeScope("about:blank"), frame])

        self.assertIs(find_paylocity_listing_scope(page), frame)

    def test_single_word_accountant_is_a_valid_job_title(self):
        self.assertTrue(is_valid_job_title("Accountant"))

    def test_collector_resolves_embedded_provider_before_browser_navigation(self):
        collector = BaseCollector(delay_seconds=0)
        with patch.object(collector, "get", return_value=EmbeddedResponse()):
            resolved = collector.resolve_embedded_job_board_url("https://example.com/careers", "Paycor")

        self.assertIn("recruitingbypaycor.com/career/CareerHome.action", resolved)


if __name__ == "__main__":
    unittest.main()
