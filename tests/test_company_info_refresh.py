from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from unittest.mock import Mock, patch

import requests

from backend.company_information import (
    CompanyInformationDiscovery,
    discover_company_information,
    extract_official_company_metadata,
    missing_company_information_fields,
    validate_related_company_url,
)
from backend.repository import OpportunityRepository
from backend.utility_runs import UtilityRunManager
from backend.utility_tasks import UtilityCancelled, refresh_missing_company_information
from search_tools import WebsiteEvaluation


DESCRIPTION = (
    "Acme Bank provides trusted community banking and financial services "
    "to customers throughout Colorado."
)


def complete_company(
    *,
    company_id: str = "company-acme",
    name: str = "Acme Bank",
) -> dict[str, Any]:
    return {
        "id": company_id,
        "name": name,
        "officialWebsite": "https://www.acmebank.com/",
        "careersPageUrl": "https://www.acmebank.com/careers",
        "jobBoardUrl": "https://acme.wd5.myworkdayjobs.com/Acme",
        "jobPlatform": "Workday",
        "city": "Denver",
        "state": "CO",
        "companyDescription": DESCRIPTION,
        "industry": "Banking",
        "websiteVerified": True,
    }


def verified_website(
    url: str = "https://www.acmebank.com/",
    *,
    html: str = "",
) -> WebsiteEvaluation:
    evaluation = WebsiteEvaluation(
        url=url,
        final_url=url,
        confidence="High",
        verified=True,
        notes=["Confirmed the official company website."],
        discovery_method="Known Website",
        candidate_urls=[url],
    )
    # WebsiteEvaluation intentionally models URL verification only; discovery
    # accepts an optional cached homepage body when one is available.
    evaluation.html = html  # type: ignore[attr-defined]
    return evaluation


class FakeResponse:
    def __init__(self, url: str, text: str = "", status_code: int = 200) -> None:
        self.url = url
        self.text = text
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        self.closed = True


class FakeRepository:
    """Small in-memory repository for grouping/concurrency behavior tests."""

    def __init__(self, companies: list[dict[str, Any]]) -> None:
        self.companies = {
            str(company["id"]): dict(company)
            for company in companies
        }
        self.update_calls: list[str] = []
        self.update_payloads: dict[str, dict[str, Any]] = {}

    def list_companies(self) -> list[dict[str, Any]]:
        return [dict(company) for company in self.companies.values()]

    def get_company(self, company_id: str) -> dict[str, Any]:
        return dict(self.companies[company_id])

    def update_discovered_company_fields(
        self,
        company_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        self.update_calls.append(company_id)
        self.update_payloads[company_id] = dict(updates)
        current = self.companies[company_id]
        for key, value in updates.items():
            if key == "lastChecked":
                current[key] = value
            elif key == "websiteVerified" and value:
                current[key] = True
            elif value not in (None, "") and current.get(key) in (None, ""):
                current[key] = value
        return dict(current)


class CompanyInformationDiscoveryTests(unittest.TestCase):
    def test_missing_website_uses_confirmed_official_result(self) -> None:
        company = complete_company()
        company.update({"officialWebsite": "", "websiteVerified": False})
        session = Mock()

        with patch(
            "backend.company_information.choose_official_website_details",
            return_value=verified_website(),
        ) as choose_website:
            discovery = discover_company_information(
                company,
                requested_fields={"officialWebsite"},
                session=session,
            )

        self.assertTrue(discovery.attempted_network)
        self.assertEqual(discovery.updates["officialWebsite"], "https://www.acmebank.com/")
        self.assertTrue(discovery.updates["websiteVerified"])
        self.assertEqual(discovery.updates["searchStatus"], "Completed")
        self.assertEqual(discovery.found_fields, ["officialWebsite"])
        choose_website.assert_called_once()

    def test_missing_careers_url_uses_validated_official_link(self) -> None:
        company = complete_company()
        company["careersPageUrl"] = ""
        canonical_careers = "https://www.acmebank.com/about/careers"

        with (
            patch(
                "backend.company_information.choose_official_website_details",
                return_value=verified_website(),
            ),
            patch(
                "backend.company_information.find_careers_page",
                return_value=("http://acmebank.com/careers", "", ["Found a careers link."]),
            ) as find_careers,
            patch(
                "backend.company_information.validate_related_company_url",
                return_value=(canonical_careers, "<h1>Careers at Acme Bank</h1>", ""),
            ) as validate_url,
        ):
            discovery = discover_company_information(
                company,
                requested_fields={"careersPageUrl"},
                session=Mock(),
            )

        self.assertEqual(discovery.updates["careersPageUrl"], canonical_careers)
        self.assertIn("careersPageUrl", discovery.found_fields)
        self.assertEqual(discovery.updates["searchStatus"], "Completed")
        find_careers.assert_called_once()
        validate_url.assert_called_once()

    def test_supported_platform_is_inferred_without_network_lookup(self) -> None:
        company = complete_company()
        company["jobPlatform"] = "Unknown"

        with patch(
            "backend.company_information.choose_official_website_details"
        ) as choose_website:
            discovery = discover_company_information(
                company,
                requested_fields={"jobPlatform"},
                session=Mock(),
            )

        self.assertFalse(discovery.attempted_network)
        self.assertEqual(discovery.updates["jobPlatform"], "Workday")
        self.assertEqual(discovery.updates["replaceConfirmedFields"], ["jobPlatform"])
        self.assertEqual(discovery.updates["replacementSourceValues"], {"jobPlatform": "Unknown"})
        self.assertEqual(discovery.updates["searchStatus"], "Completed")
        self.assertTrue(discovery.updates["reconcileSearchStatus"])
        self.assertEqual(discovery.found_fields, ["jobPlatform"])
        choose_website.assert_not_called()

    def test_redirected_related_url_saves_final_canonical_url(self) -> None:
        session = Mock()
        session.get.return_value = FakeResponse(
            "https://www.acmebank.com/careers/openings#available-jobs",
            "<h1>Acme Bank careers</h1><p>Search jobs and open positions.</p>",
        )

        final_url, html, note = validate_related_company_url(
            "http://acmebank.com/jobs",
            "Acme Bank",
            "https://acmebank.com/",
            session,
            source_confirmed=False,
        )

        self.assertEqual(final_url, "https://www.acmebank.com/careers/openings")
        self.assertIn("Acme Bank careers", html)
        self.assertEqual(note, "")
        session.get.assert_called_once()

    def test_invalid_aggregator_and_unrelated_results_are_rejected(self) -> None:
        aggregator_session = Mock()
        aggregator = validate_related_company_url(
            "https://www.indeed.com/cmp/acme-bank/jobs",
            "Acme Bank",
            "https://acmebank.com/",
            aggregator_session,
            source_confirmed=True,
        )
        self.assertEqual(aggregator[:2], ("", ""))
        self.assertIn("aggregator", aggregator[2].lower())
        aggregator_session.get.assert_not_called()

        unrelated_session = Mock()
        unrelated_session.get.return_value = FakeResponse(
            "https://careers.unrelated.org/jobs",
            "<h1>Careers</h1><p>Search jobs and open positions.</p>",
        )
        unrelated = validate_related_company_url(
            "https://careers.unrelated.org/jobs",
            "Acme Bank",
            "https://acmebank.com/",
            unrelated_session,
            source_confirmed=False,
        )
        self.assertEqual(unrelated[:2], ("", ""))
        self.assertIn("could not be related", unrelated[2].lower())

    def test_invalid_saved_careers_url_is_replaced_by_rediscovery(self) -> None:
        company = complete_company()
        company["careersPageUrl"] = "https://www.indeed.com/cmp/acme-bank/jobs"
        canonical = "https://www.acmebank.com/careers"
        with (
            patch(
                "backend.company_information.choose_official_website_details",
                return_value=verified_website(),
            ),
            patch(
                "backend.company_information.find_careers_page",
                return_value=(canonical, "", []),
            ) as find_careers,
            patch(
                "backend.company_information.validate_related_company_url",
                return_value=(canonical, "<h1>Acme Bank Careers</h1>", ""),
            ),
        ):
            discovery = discover_company_information(
                company,
                requested_fields={"careersPageUrl"},
                session=Mock(),
            )

        find_careers.assert_called_once()
        self.assertEqual(discovery.updates["careersPageUrl"], canonical)
        self.assertIn("careersPageUrl", discovery.updates["replaceConfirmedFields"])

    def test_same_domain_non_careers_page_is_rejected(self) -> None:
        session = Mock()
        session.get.return_value = FakeResponse(
            "https://www.acmebank.com/privacy",
            "<html><title>Privacy Policy</title><h1>Privacy Policy</h1></html>",
        )

        final_url, html, note = validate_related_company_url(
            "https://www.acmebank.com/privacy",
            "Acme Bank",
            "https://www.acmebank.com/",
            session,
            source_confirmed=True,
        )

        self.assertEqual((final_url, html), ("", ""))
        self.assertIn("not confirmed as a careers", note)

    def test_partial_official_metadata_is_returned_without_inventing_fields(self) -> None:
        html = f"""
        <html><head><script type="application/ld+json">
        {json.dumps({
            "@type": "Organization",
            "name": "Acme Bank",
            "description": DESCRIPTION,
            "address": {"@type": "PostalAddress", "addressLocality": "Denver"},
        })}
        </script></head><body></body></html>
        """
        company = complete_company()
        company.update({
            "city": "",
            "state": "",
            "companyDescription": "",
            "industry": "",
        })

        with patch(
            "backend.company_information.choose_official_website_details",
            return_value=verified_website(html=html),
        ):
            discovery = discover_company_information(
                company,
                requested_fields={"city", "state", "companyDescription", "industry"},
                session=Mock(),
            )

        self.assertEqual(discovery.updates["city"], "Denver")
        self.assertEqual(discovery.updates["companyDescription"], DESCRIPTION)
        self.assertNotIn("state", discovery.updates)
        self.assertNotIn("industry", discovery.updates)
        self.assertEqual(discovery.updates["searchStatus"], "Partial")

    def test_metadata_parser_returns_only_confirmed_partial_fields(self) -> None:
        html = f"""
        <script type="application/ld+json">
        {json.dumps({
            "@type": "Organization",
            "name": "Acme Bank",
            "description": DESCRIPTION,
            "address": {"addressLocality": "Denver", "addressRegion": ""},
        })}
        </script>
        """

        metadata, notes, returned_html = extract_official_company_metadata(
            "https://acmebank.com/",
            "Acme Bank",
            Mock(),
            homepage_html=html,
        )

        self.assertEqual(
            metadata,
            {"city": "Denver", "companyDescription": DESCRIPTION},
        )
        self.assertTrue(notes)
        self.assertEqual(returned_html, html)

    def test_network_timeout_is_retried_then_reported_without_a_url(self) -> None:
        session = Mock()
        session.get.side_effect = requests.Timeout("request timed out")

        with patch("search_tools.time.sleep") as sleep:
            final_url, html, note = validate_related_company_url(
                "https://acmebank.com/careers",
                "Acme Bank",
                "https://acmebank.com/",
                session,
                source_confirmed=True,
            )

        self.assertEqual((final_url, html), ("", ""))
        self.assertIn("timed out", note.lower())
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once()

    def test_unverified_official_site_marks_the_company_partial(self) -> None:
        company = complete_company()
        company["state"] = ""
        unavailable = WebsiteEvaluation(
            url="",
            final_url="",
            confidence="Low",
            verified=False,
            notes=["The official site could not be verified."],
            discovery_method="Search",
            candidate_urls=[],
        )

        with patch(
            "backend.company_information.choose_official_website_details",
            return_value=unavailable,
        ):
            discovery = discover_company_information(
                company,
                requested_fields={"state"},
                session=Mock(),
            )

        self.assertEqual(discovery.updates["searchStatus"], "Partial")
        self.assertTrue(discovery.updates["reconcileSearchStatus"])


class BulkCompanyInformationRefreshTests(unittest.TestCase):
    def test_complete_company_is_skipped_without_discovery_or_save(self) -> None:
        repository = FakeRepository([complete_company()])
        progress = Mock()

        with patch("backend.utility_tasks._discover_company_refresh_group") as discover:
            summary = refresh_missing_company_information(repository, progress, Event())

        self.assertEqual(summary["totalCompaniesNeedingReview"], 0)
        self.assertEqual(summary["processedCount"], 0)
        self.assertEqual(summary["companyResults"], [])
        self.assertEqual(repository.update_calls, [])
        discover.assert_not_called()
        progress.assert_called_once()

    def test_valid_existing_values_are_protected_while_blanks_are_filled(self) -> None:
        original_description = (
            "Protected Bank is a long-established institution serving its local community."
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OpportunityRepository(
                Path(temp_dir) / "radar.db",
                initialize=True,
            )
            company = repository.create_company({
                "name": "Protected Bank",
                "companyWebsite": "https://protectedbank.com/",
                "careersPageUrl": "https://protectedbank.com/careers",
                "jobBoardUrl": "https://protected.wd5.myworkdayjobs.com/Protected",
                "industry": "Banking",
                "companyDescription": original_description,
                "city": "Denver",
                "state": "",
                "country": "United States",
                "notes": "",
            })
            discovery = CompanyInformationDiscovery(
                updates={
                    "city": "Boulder",
                    "state": "CO",
                    "industry": "Financial Services",
                    "companyDescription": "A shorter, lower-priority discovered description.",
                },
                found_fields=["city", "state", "industry", "companyDescription"],
            )

            with patch(
                "backend.utility_tasks._discover_company_refresh_group",
                return_value=discovery,
            ):
                summary = refresh_missing_company_information(
                    repository,
                    Mock(),
                    Event(),
                )

            after = repository.get_company(company["id"])
            self.assertEqual(after["city"], "Denver")
            self.assertEqual(after["state"], "CO")
            self.assertEqual(after["industry"], "Banking")
            self.assertEqual(after["companyDescription"], original_description)
            self.assertEqual(after["officialWebsite"], "https://protectedbank.com/")
            self.assertEqual(summary["updatedCount"], 1)

    def test_confirmed_metadata_replaces_clearly_invalid_placeholders(self) -> None:
        company = complete_company()
        company.update({
            "city": "-",
            "state": "N/A",
            "industry": "Unknown",
            "companyDescription": "TBD",
        })
        metadata = {
            "city": "Denver",
            "state": "CO",
            "industry": "Banking",
            "companyDescription": DESCRIPTION,
        }

        with (
            patch(
                "backend.company_information.choose_official_website_details",
                return_value=verified_website(),
            ),
            patch(
                "backend.company_information.extract_official_company_metadata",
                return_value=(metadata, [], "<html></html>"),
            ),
        ):
            discovery = discover_company_information(
                company,
                requested_fields=set(metadata),
                session=Mock(),
            )

        self.assertEqual({key: discovery.updates[key] for key in metadata}, metadata)
        self.assertCountEqual(discovery.updates["replaceConfirmedFields"], metadata)

    def test_completed_status_can_be_reconciled_to_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OpportunityRepository(
                Path(temp_dir) / "radar.db",
                initialize=True,
            )
            company = repository.create_company({
                "name": "Status Bank",
                "companyWebsite": "https://statusbank.example/",
                "careersPageUrl": "https://statusbank.example/careers",
                "jobBoardUrl": "https://statusbank.example/careers/jobs",
                "industry": "Banking",
                "companyDescription": DESCRIPTION,
                "city": "Denver",
                "state": "",
            })
            with repository.connection() as connection:
                connection.execute(
                    "UPDATE companies SET search_status = 'Completed' WHERE id = ?",
                    (company["id"],),
                )

            with patch(
                "backend.utility_tasks._discover_company_refresh_group",
                return_value=CompanyInformationDiscovery(
                    updates={"searchStatus": "Partial", "reconcileSearchStatus": True},
                ),
            ):
                refresh_missing_company_information(repository, Mock(), Event())

            self.assertEqual(repository.get_company(company["id"])["searchStatus"], "Partial")

    def test_duplicate_refresh_does_not_replace_another_records_valid_url(self) -> None:
        first = complete_company(company_id="duplicate-invalid", name="Acme Bank")
        second = complete_company(company_id="duplicate-valid", name="  acme bank  ")
        first["careersPageUrl"] = "https://www.indeed.com/cmp/acme-bank/jobs"
        second["state"] = ""
        repository = FakeRepository([first, second])
        replacement = "https://www.acmebank.com/careers/openings"
        discovery = CompanyInformationDiscovery(
            updates={
                "careersPageUrl": replacement,
                "state": "CO",
                "replaceConfirmedFields": ["careersPageUrl"],
                "replacementSourceValues": {"careersPageUrl": first["careersPageUrl"]},
            },
            found_fields=["careersPageUrl", "state"],
        )

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=discovery,
        ):
            refresh_missing_company_information(repository, Mock(), Event())

        self.assertIn(
            "careersPageUrl",
            repository.update_payloads[first["id"]]["replaceConfirmedFields"],
        )
        self.assertNotIn(
            "careersPageUrl",
            repository.update_payloads[second["id"]].get("replaceConfirmedFields", []),
        )
        self.assertEqual(repository.get_company(second["id"])["careersPageUrl"], second["careersPageUrl"])

    def test_duplicate_placeholder_is_repaired_from_confirmed_merged_value(self) -> None:
        invalid = complete_company(company_id="placeholder-invalid", name="Acme Bank")
        complete = complete_company(company_id="placeholder-complete", name=" acme bank ")
        invalid["state"] = "N/A"
        repository = FakeRepository([invalid, complete])
        discovery = CompanyInformationDiscovery(
            updates={"state": "CO", "searchStatus": "Completed", "reconcileSearchStatus": True},
            found_fields=["state"],
        )

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=discovery,
        ):
            refresh_missing_company_information(repository, Mock(), Event())

        self.assertIn(
            "state",
            repository.update_payloads[invalid["id"]]["replaceConfirmedFields"],
        )

    def test_duplicate_status_is_partial_when_its_placeholder_is_unresolved(self) -> None:
        invalid = complete_company(company_id="status-invalid", name="Acme Bank")
        complete = complete_company(company_id="status-complete", name=" acme bank ")
        invalid["state"] = "N/A"
        invalid["searchStatus"] = "Completed"
        repository = FakeRepository([invalid, complete])

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=CompanyInformationDiscovery(
                updates={"searchStatus": "Completed", "reconcileSearchStatus": True},
            ),
        ):
            refresh_missing_company_information(repository, Mock(), Event())

        self.assertEqual(repository.update_payloads[invalid["id"]]["searchStatus"], "Partial")

    def test_discovery_exception_marks_the_company_partial(self) -> None:
        company = complete_company(company_id="exception-status")
        company["state"] = ""
        company["searchStatus"] = "Completed"
        repository = FakeRepository([company])

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            side_effect=RuntimeError("provider unavailable"),
        ):
            summary = refresh_missing_company_information(repository, Mock(), Event())

        self.assertEqual(summary["failedCount"], 1)
        self.assertEqual(repository.update_payloads[company["id"]]["searchStatus"], "Partial")

    def test_one_company_failure_does_not_stop_remaining_companies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OpportunityRepository(
                Path(temp_dir) / "radar.db",
                initialize=True,
            )
            broken = repository.create_company({
                "name": "Broken Bank",
                "companyWebsite": "https://brokenbank.com/",
                "industry": "Banking",
                "companyDescription": DESCRIPTION,
                "city": "Denver",
                "state": "",
            })
            healthy = repository.create_company({
                "name": "Healthy Bank",
                "companyWebsite": "https://healthybank.com/",
                "industry": "Banking",
                "companyDescription": DESCRIPTION,
                "city": "Denver",
                "state": "",
            })

            def discover(group: dict[str, Any], _cancelled: Event) -> CompanyInformationDiscovery:
                if group["name"] == "Broken Bank":
                    raise RuntimeError("simulated provider outage")
                return CompanyInformationDiscovery(
                    updates={"state": "CO"},
                    found_fields=["state"],
                )

            with patch(
                "backend.utility_tasks._discover_company_refresh_group",
                side_effect=discover,
            ):
                summary = refresh_missing_company_information(
                    repository,
                    Mock(),
                    Event(),
                )

            results = {
                result["companyName"]: result
                for result in summary["companyResults"]
            }
            self.assertEqual(summary["processedCount"], 2)
            self.assertEqual(summary["failedCount"], 1)
            self.assertEqual(summary["updatedCount"], 1)
            self.assertEqual(results["Broken Bank"]["outcome"], "failed")
            self.assertEqual(results["Healthy Bank"]["outcome"], "updated")
            self.assertEqual(repository.get_company(broken["id"])["state"], "")
            self.assertEqual(repository.get_company(healthy["id"])["state"], "CO")

    def test_duplicate_records_share_one_lookup_and_are_each_saved_once(self) -> None:
        first = complete_company(company_id="duplicate-1", name="Acme Bank")
        second = complete_company(company_id="duplicate-2", name="  acme   bank  ")
        first["state"] = ""
        second["state"] = ""
        repository = FakeRepository([first, second])
        discovery = CompanyInformationDiscovery(
            updates={"state": "CO"},
            found_fields=["state"],
        )

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=discovery,
        ) as discover:
            summary = refresh_missing_company_information(repository, Mock(), Event())

        self.assertEqual(summary["totalCompaniesNeedingReview"], 1)
        self.assertEqual(summary["processedCount"], 1)
        self.assertEqual(summary["duplicateRecordsSkipped"], 1)
        self.assertEqual(discover.call_count, 1)
        group = discover.call_args.args[0]
        self.assertEqual(group["requestedFields"], {"state"})
        self.assertCountEqual(repository.update_calls, ["duplicate-1", "duplicate-2"])
        self.assertEqual(repository.get_company("duplicate-1")["state"], "CO")
        self.assertEqual(repository.get_company("duplicate-2")["state"], "CO")
        self.assertIn("One lookup safely covered 2 duplicate records", summary["companyResults"][0]["message"])

    def test_parallelism_is_bounded_and_uses_more_than_one_worker(self) -> None:
        companies = []
        for index in range(8):
            company = complete_company(
                company_id=f"company-{index}",
                name=f"Company {index} Bank",
            )
            company["state"] = ""
            companies.append(company)
        repository = FakeRepository(companies)
        release = Event()
        two_started = Event()
        lock = Lock()
        active = 0
        maximum_active = 0
        output: dict[str, Any] = {}

        def discover(_group: dict[str, Any], _cancelled: Event) -> CompanyInformationDiscovery:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active >= 2:
                    two_started.set()
            release.wait(timeout=2)
            with lock:
                active -= 1
            return CompanyInformationDiscovery(updates={"state": "CO"}, found_fields=["state"])

        def run_refresh() -> None:
            output.update(refresh_missing_company_information(repository, Mock(), Event()))

        with patch("backend.utility_tasks._discover_company_refresh_group", side_effect=discover):
            worker = Thread(target=run_refresh)
            worker.start()
            self.assertTrue(two_started.wait(timeout=2), "refresh did not use parallel workers")
            with lock:
                observed = maximum_active
            self.assertGreaterEqual(observed, 2)
            self.assertLessEqual(observed, 4)
            release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(output["processedCount"], 8)
        self.assertEqual(output["updatedCount"], 8)

    def test_live_progress_keeps_only_the_latest_company_result(self) -> None:
        companies = []
        for index in range(5):
            company = complete_company(
                company_id=f"progress-{index}",
                name=f"Progress Bank {index}",
            )
            company["state"] = ""
            companies.append(company)
        repository = FakeRepository(companies)
        progress = Mock()

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=CompanyInformationDiscovery(
                updates={"state": "CO"},
                found_fields=["state"],
            ),
        ):
            summary = refresh_missing_company_information(repository, progress, Event())

        live_details = [
            call.args[3]
            for call in progress.call_args_list
            if len(call.args) == 4 and call.args[3].get("processedCount", 0) > 0
        ]
        self.assertTrue(live_details)
        self.assertTrue(all(len(details["companyResults"]) == 1 for details in live_details))
        self.assertEqual(len(summary["companyResults"]), 5)

    def test_unresolved_requested_field_is_counted_as_no_information_found(self) -> None:
        company = complete_company()
        company["state"] = ""
        repository = FakeRepository([company])
        discovery = CompanyInformationDiscovery(
            updates={"officialWebsite": company["officialWebsite"], "websiteVerified": True},
            found_fields=["officialWebsite"],
        )

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=discovery,
        ):
            summary = refresh_missing_company_information(repository, Mock(), Event())

        self.assertEqual(summary["noInformationFoundCount"], 1)
        self.assertEqual(summary["unchangedCount"], 0)
        self.assertEqual(summary["companyResults"][0]["outcome"], "no_information_found")
        self.assertEqual(summary["companyResults"][0]["foundFields"], [])

    def test_same_name_with_conflicting_verified_domains_is_not_grouped(self) -> None:
        first = complete_company(company_id="first-bank-a", name="First Bank")
        second = complete_company(company_id="first-bank-b", name="First Bank")
        first.update({"officialWebsite": "https://firstbank-a.example/", "state": ""})
        second.update({"officialWebsite": "https://firstbank-b.example/", "state": ""})
        repository = FakeRepository([first, second])

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=CompanyInformationDiscovery(
                updates={"state": "CO"},
                found_fields=["state"],
            ),
        ) as discover:
            summary = refresh_missing_company_information(repository, Mock(), Event())

        self.assertEqual(summary["totalCompaniesNeedingReview"], 2)
        self.assertEqual(summary["duplicateRecordsSkipped"], 0)
        self.assertEqual(discover.call_count, 2)

    def test_same_name_with_conflicting_known_domain_is_not_grouped(self) -> None:
        first = complete_company(company_id="known-domain-a", name="First Bank")
        second = complete_company(company_id="known-domain-b", name="First Bank")
        first.update({"officialWebsite": "https://firstbank-a.example/", "state": ""})
        second.update({
            "officialWebsite": "",
            "knownWebsite": "https://firstbank-b.example/",
            "websiteVerified": False,
            "state": "",
        })
        repository = FakeRepository([first, second])

        with patch(
            "backend.utility_tasks._discover_company_refresh_group",
            return_value=CompanyInformationDiscovery(
                updates={"state": "CO"},
                found_fields=["state"],
            ),
        ) as discover:
            summary = refresh_missing_company_information(repository, Mock(), Event())

        self.assertEqual(summary["totalCompaniesNeedingReview"], 2)
        self.assertEqual(summary["duplicateRecordsSkipped"], 0)
        self.assertEqual(discover.call_count, 2)

    def test_cancellation_preserves_partial_company_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = UtilityRunManager(Path(temp_dir) / "radar.db")
            progress_written = Event()

            def worker(progress, cancelled):
                progress(1, 3, "First Bank", {
                    "totalCompaniesNeedingReview": 3,
                    "processedCount": 1,
                    "updatedCount": 1,
                    "noInformationFoundCount": 0,
                    "failedCount": 0,
                    "unchangedCount": 0,
                    "duplicateRecordsSkipped": 0,
                    "companyResults": [{
                        "companyId": "first",
                        "companyName": "First Bank",
                        "outcome": "updated",
                        "foundFields": ["Headquarters state"],
                        "updatedFields": ["Headquarters state"],
                        "message": "Updated Headquarters state.",
                    }],
                })
                progress_written.set()
                while not cancelled.wait(timeout=0.01):
                    pass
                raise UtilityCancelled("Cancelled by user.")

            started = manager.start(
                action="refresh-missing-company-information",
                task_name="Refresh Missing Company Info",
                progress_verb="Checking",
                progress_unit="companies",
                worker=worker,
                format_summary=lambda _summary: "Done.",
            )
            self.assertTrue(progress_written.wait(timeout=2))
            manager.cancel(started["id"])
            for _ in range(200):
                completed = manager.get(started["id"])
                if completed["status"] == "Cancelled":
                    break
                time.sleep(0.01)

            self.assertEqual(completed["status"], "Cancelled")
            self.assertEqual(completed["resultSummary"]["processedCount"], 1)
            self.assertEqual(completed["resultSummary"]["updatedCount"], 1)


if __name__ == "__main__":
    unittest.main()
