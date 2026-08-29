import unittest
from unittest.mock import MagicMock, Mock, patch

from browser_tools import BrowserCandidate, _discover_with_launched_browser, discover_job_board_with_browser
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

    def test_browser_discovery_always_closes_browser_when_context_creation_crashes(self):
        manager = MagicMock()
        manager.__enter__.return_value = Mock()
        browser = Mock()
        browser.new_context.side_effect = RuntimeError("page crashed")

        with (
            patch("playwright.sync_api.sync_playwright", return_value=manager),
            patch("browser_tools.launch_playwright_chromium", return_value=browser),
        ):
            with self.assertRaisesRegex(RuntimeError, "page crashed"):
                discover_job_board_with_browser("https://example.com/careers", "Example Bank")

        browser.close.assert_called_once_with()

    def test_browser_discovery_rejects_final_redirect_to_indeed_company_profile(self):
        browser = Mock()
        context = Mock()
        browser.new_context.return_value = context
        initial_page = Mock()
        candidate_page = Mock()
        final_page = Mock()
        final_page.url = "https://www.indeed.com/cmp/Acnb-Bank-1"
        context.new_page.side_effect = [initial_page, candidate_page]
        context.pages = [initial_page, candidate_page, final_page]
        popup = MagicMock()
        popup.__enter__.return_value = Mock(value=final_page)
        candidate_page.expect_popup.return_value = popup
        candidate_page.locator.return_value.nth.return_value = Mock()
        candidate = BrowserCandidate(
            index=0,
            text="View Open Positions",
            href="https://bank.example/jobs",
            tag="a",
            score=80,
        )

        with (
            patch("browser_tools.install_playwright_url_guard"),
            patch("browser_tools.safe_page_goto"),
            patch("browser_tools.find_platform_iframe", return_value=""),
            patch("browser_tools.choose_candidates", return_value=[candidate]),
        ):
            result = _discover_with_launched_browser(browser, "https://bank.example/careers", TimeoutError)

        self.assertEqual(result["status"], "Needs Review")
        self.assertIsNone(result["final_url"])
        self.assertIn("Rejected https://www.indeed.com/cmp/Acnb-Bank-1", str(result["notes"]))


if __name__ == "__main__":
    unittest.main()
