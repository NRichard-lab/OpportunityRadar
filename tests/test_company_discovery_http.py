from __future__ import annotations

import sys
import types
import unittest
from io import BytesIO
from unittest.mock import patch

import requests

from job_board_discovery import (
    JobBoardCandidate,
    discover_job_board_for_row,
    verify_search_candidate,
)
from job_platforms import detect_job_platform
from search_tools import (
    SearchCandidate,
    WebsiteEvaluation,
    choose_official_website_details,
    evaluate_official_website_details,
    request_with_limited_retries,
    search_web,
    validate_and_canonicalize_url,
)
from website_tools import can_fetch, find_careers_page


def response(
    url: str,
    *,
    status: int = 200,
    body: str = "<html><body>Example</body></html>",
    content_type: str = "text/html; charset=utf-8",
) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.headers["content-type"] = content_type
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    result.raw = BytesIO(result._content)
    return result


class QueueSession:
    def __init__(self, outcomes: list[requests.Response | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> requests.Response:
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class DiscoveryHTTPTests(unittest.TestCase):
    def test_transient_timeout_is_retried_with_a_bounded_attempt_count(self) -> None:
        session = QueueSession([requests.Timeout("slow"), response("https://example.com/")])

        with patch("search_tools.time.sleep") as mocked_sleep:
            result = request_with_limited_retries(
                session,  # type: ignore[arg-type]
                "https://example.com/",
                max_attempts=20,
                backoff_seconds=0.01,
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(session.calls), 2)
        mocked_sleep.assert_called_once()

    def test_non_transient_http_failure_is_not_retried(self) -> None:
        session = QueueSession([response("https://example.com/missing", status=404)])

        result = request_with_limited_retries(
            session,  # type: ignore[arg-type]
            "https://example.com/missing",
            max_attempts=3,
            backoff_seconds=0,
        )

        self.assertEqual(result.status_code, 404)
        self.assertEqual(len(session.calls), 1)

    def test_server_error_is_retried_and_attempts_are_clamped(self) -> None:
        recovered = QueueSession(
            [response("https://example.com/", status=503), response("https://example.com/")]
        )
        with patch("search_tools.time.sleep"):
            result = request_with_limited_retries(
                recovered,  # type: ignore[arg-type]
                "https://example.com/",
                max_attempts=3,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(recovered.calls), 2)

        exhausted = QueueSession([requests.Timeout("slow")] * 4)
        with patch("search_tools.time.sleep"):
            with self.assertRaises(requests.Timeout):
                request_with_limited_retries(
                    exhausted,  # type: ignore[arg-type]
                    "https://example.com/",
                    max_attempts=20,
                )
        self.assertEqual(len(exhausted.calls), 3)

    def test_url_validation_returns_canonical_redirect_and_rejects_aggregators(self) -> None:
        session = QueueSession([response("https://www.example.com/careers/#openings")])

        final_url, _ = validate_and_canonicalize_url(
            "HTTP://Example.COM:80/start#old",
            session,  # type: ignore[arg-type]
        )

        self.assertEqual(final_url, "https://www.example.com/careers/")
        self.assertEqual(session.calls[0][0], "http://example.com/start")

        with self.assertRaisesRegex(ValueError, "disallowed"):
            validate_and_canonicalize_url(
                "https://www.indeed.com/cmp/example",
                QueueSession([]),  # type: ignore[arg-type]
            )

        redirected = QueueSession([response("https://jobs.indeed.com/viewjob?id=1")])
        with self.assertRaisesRegex(ValueError, "redirected"):
            validate_and_canonicalize_url(
                "https://example.com/jobs",
                redirected,  # type: ignore[arg-type]
            )

    def test_robots_rules_are_cached_per_session_and_origin(self) -> None:
        robots = response(
            "https://example.com/robots.txt",
            body="User-agent: *\nDisallow: /private\n",
            content_type="text/plain",
        )
        session = QueueSession([robots])

        self.assertTrue(can_fetch("https://example.com/public", session))  # type: ignore[arg-type]
        self.assertFalse(can_fetch("https://example.com/private/record", session))  # type: ignore[arg-type]
        self.assertEqual([call[0] for call in session.calls], ["https://example.com/robots.txt"])

    def test_careers_discovery_reuses_verified_initial_html(self) -> None:
        initial_html = '<html><body><a href="/about">About</a><a href="/careers">Careers</a></body></html>'

        with patch("website_tools.fetch_html") as mocked_fetch:
            careers_url, platform, notes = find_careers_page(
                "https://example.com/",
                QueueSession([]),  # type: ignore[arg-type]
                initial_html=initial_html,
                initial_final_url="https://www.example.com/",
            )

        mocked_fetch.assert_not_called()
        self.assertEqual(careers_url, "https://www.example.com/careers")
        self.assertEqual(platform, "")
        self.assertEqual(notes, [])

    def test_careers_discovery_rejects_unrelated_partner_ats_link(self) -> None:
        initial_html = (
            '<html><body><a href="https://boards.greenhouse.io/partner/jobs/123">'
            "Technology partner</a></body></html>"
        )

        careers_url, platform, notes = find_careers_page(
            "https://example.com/",
            QueueSession([]),  # type: ignore[arg-type]
            initial_html=initial_html,
            initial_final_url="https://example.com/",
        )

        self.assertEqual(careers_url, "")
        self.assertEqual(platform, "")
        self.assertIn("no careers link found", notes[-1])

    def test_careers_discovery_accepts_ats_link_with_careers_anchor(self) -> None:
        initial_html = (
            '<html><body><a href="https://boards.greenhouse.io/acme/jobs">'
            "View careers</a></body></html>"
        )

        careers_url, platform, notes = find_careers_page(
            "https://example.com/",
            QueueSession([]),  # type: ignore[arg-type]
            initial_html=initial_html,
        )

        self.assertEqual(careers_url, "https://boards.greenhouse.io/acme/jobs")
        self.assertEqual(platform, "Greenhouse")
        self.assertEqual(notes, [])

    def test_job_platform_detection_only_matches_hostnames(self) -> None:
        self.assertEqual(detect_job_platform("https://tenant.myworkdayjobs.com/jobs"), "Workday")
        self.assertEqual(detect_job_platform("https://example.com/path/myworkdayjobs.com/jobs"), "")
        self.assertEqual(detect_job_platform("https://evilmyworkdayjobs.com/jobs"), "")
        self.assertEqual(detect_job_platform("https://jobs.ukg.com/openings"), "UKG")

    def test_invalid_known_website_falls_back_to_limited_search(self) -> None:
        invalid = WebsiteEvaluation(
            url="https://invalid.example/",
            final_url="https://invalid.example/",
            rejected=True,
            rejection_reason="could not verify homepage",
            notes=["could not verify homepage"],
        )
        verified = WebsiteEvaluation(
            url="https://acmebank.example/",
            final_url="https://acmebank.example/",
            confidence="High",
            score=70,
            verified=True,
            notes=["company identity verified"],
        )
        candidate = SearchCandidate(
            url="https://acmebank.example/",
            title="Acme Bank",
            snippet="Official site",
            rank=1,
        )

        with (
            patch("search_tools.evaluate_official_website_details", side_effect=[invalid, verified]),
            patch("search_tools.search_web", return_value=[candidate]) as mocked_search,
        ):
            result = choose_official_website_details(
                "Acme Bank",
                "https://invalid.example/",
                QueueSession([]),  # type: ignore[arg-type]
            )

        self.assertTrue(result.verified)
        self.assertEqual(result.final_url, "https://acmebank.example/")
        self.assertIn("known website failed verification", result.notes[0])
        self.assertEqual(mocked_search.call_args.kwargs["max_queries"], 2)
        self.assertEqual(mocked_search.call_args.kwargs["max_total_results"], 6)

    def test_official_verification_keeps_html_for_metadata_reuse(self) -> None:
        homepage = response(
            "https://www.acmebank.example/",
            body=(
                "<html><head><title>Acme Bank</title></head><body>"
                "<h1>Acme Bank</h1><p>Banking, checking, savings, loans, and mortgages "
                "for Acme customers.</p><a>Locations</a><a>Careers</a>"
                "<p>Member FDIC. Contact our banking team for financial services.</p>"
                "</body></html>"
            ),
        )
        session = QueueSession([homepage])

        with patch.object(homepage, "close", wraps=homepage.close) as mocked_close:
            result = evaluate_official_website_details(
                "Acme Bank",
                "https://www.acmebank.example/",
                session,  # type: ignore[arg-type]
            )

        self.assertTrue(result.verified)
        self.assertIn("Member FDIC", result.html)
        self.assertNotIn("Member FDIC", repr(result))
        mocked_close.assert_called_once_with()

    def test_official_search_clamps_query_and_result_limits(self) -> None:
        calls: list[tuple[str, int]] = []

        class FakeDDGS:
            def __init__(self, *, timeout: float) -> None:
                self.timeout = timeout

            def __enter__(self) -> "FakeDDGS":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def text(self, phrase: str, *, max_results: int) -> list[dict[str, str]]:
                calls.append((phrase, max_results))
                index = len(calls)
                return [{"href": f"https://candidate{index}.example/", "title": "Acme Bank"}]

        fake_module = types.SimpleNamespace(DDGS=FakeDDGS)
        with patch.dict(sys.modules, {"ddgs": fake_module}):
            candidates = search_web(
                "Acme Bank",
                max_results=100,
                max_queries=100,
                max_total_results=100,
            )

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(limit == 4 for _, limit in calls))
        self.assertEqual(len(candidates), 3)

    def test_job_board_discovery_reuses_session_and_can_skip_search(self) -> None:
        reused_session = QueueSession([])
        row = {"Company Name": "Acme Bank", "Official Website": "https://acme.example/"}

        with (
            patch("job_board_discovery.make_session") as mocked_make_session,
            patch("job_board_discovery.static_scan", return_value=[]) as mocked_static,
            patch("job_board_discovery.same_domain_careers_expansion", return_value=[]) as mocked_expand,
            patch("job_board_discovery.verified_search_fallback") as mocked_search,
        ):
            result = discover_job_board_for_row(
                row,
                session=reused_session,  # type: ignore[arg-type]
                allow_search_fallback=False,
            )

        mocked_make_session.assert_not_called()
        self.assertIs(mocked_static.call_args.args[2], reused_session)
        self.assertIs(mocked_expand.call_args.args[2], reused_session)
        mocked_search.assert_not_called()
        self.assertEqual(result.status, "Needs Review")

    def test_search_candidate_must_survive_final_url_validation(self) -> None:
        candidate = JobBoardCandidate(
            url="https://jobs.acme.example/careers",
            text="Acme careers",
            platform="",
            score=70,
        )

        with patch(
            "job_board_discovery.fetch_html",
            return_value=("https://www.indeed.com/cmp/acme/jobs", "<html>Acme jobs</html>"),
        ):
            verified = verify_search_candidate(
                candidate,
                "Acme Bank",
                "https://acme.example/",
                QueueSession([]),  # type: ignore[arg-type]
            )

        self.assertFalse(verified)
        self.assertTrue(candidate.rejected)
        self.assertIn("final URL rejected", candidate.rejection_reason)

    def test_search_candidate_network_failure_is_isolated_as_rejection(self) -> None:
        candidate = JobBoardCandidate(url="https://jobs.acme.example/", score=70)

        with patch("job_board_discovery.fetch_html", side_effect=requests.Timeout("slow")):
            verified = verify_search_candidate(
                candidate,
                "Acme Bank",
                "https://acme.example/",
                QueueSession([]),  # type: ignore[arg-type]
            )

        self.assertFalse(verified)
        self.assertTrue(candidate.rejected)
        self.assertIn("could not verify", candidate.rejection_reason)


if __name__ == "__main__":
    unittest.main()
