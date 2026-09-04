"""URL variants that used to fall through to the generic page collector.

An ADP Workforce Now tenant is named by its ``cid``/``ccId`` pair, and a UKG
Recruiting tenant by its ``/<tenant>/JobBoard/<uuid>`` path. Both were recognized
only in their single canonical shape, so an alias link, a career-center link, or
a stored job-detail link -- all of which name the same tenant -- were handed to
the generic collector, which cannot read either single-page board at all.
"""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from collectors.adp_collector import ADPCollector, build_adp_api_request, build_destination_url
from collectors.base import pick_collector
from collectors.ukg_collector import UKGCollector, build_ukg_urls


ADP_TENANT_QUERY = "cid=1b568ba3-8b35-4407-aa58-c406cde8fb17&ccId=19000101_000001&lang=en_US"
ADP_CANONICAL = (
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    f"?{ADP_TENANT_QUERY}&type=MP"
)
UKG_BOARD = (
    "https://recruiting.ultipro.com/EXA1000EXCU/JobBoard/"
    "a1ad5f09-7f9c-420c-9e77-4ace84ced6e0"
)


def selected(board_url: str, platform: str = ""):
    return pick_collector({"Job Board URL": board_url, "Job Platform": platform}, delay_seconds=0)


class ADPWorkforceNowVariantTests(unittest.TestCase):
    VARIANTS = (
        ADP_CANONICAL,
        f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?{ADP_TENANT_QUERY}",
        f"https://workforcenow.adp.com/?{ADP_TENANT_QUERY}",
        f"https://workforcenow.adp.com/mascsr/default/?{ADP_TENANT_QUERY}",
        f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?{ADP_TENANT_QUERY}&jobId=583650",
    )

    def test_every_tenant_bearing_variant_selects_the_adp_collector(self) -> None:
        for url in self.VARIANTS:
            with self.subTest(url=url):
                self.assertIsInstance(selected(url), ADPCollector)

    def test_every_variant_resolves_to_the_same_tenant_api_request(self) -> None:
        expected = build_adp_api_request(ADP_CANONICAL)[1]
        for url in self.VARIANTS:
            with self.subTest(url=url):
                api_url, params = build_adp_api_request(url)
                self.assertEqual(urlsplit(api_url).hostname, "workforcenow.adp.com")
                self.assertTrue(
                    urlsplit(api_url).path.endswith(
                        "/careercenter/public/events/staffing/v1/job-requisitions"
                    )
                )
                self.assertEqual(params["cid"], expected["cid"])
                self.assertEqual(params["ccId"], expected["ccId"])

    def test_a_variant_still_links_each_job_to_the_recruitment_board(self) -> None:
        for url in self.VARIANTS:
            with self.subTest(url=url):
                destination = build_destination_url(url, "583650")
                parsed = urlsplit(destination)
                self.assertEqual(parsed.hostname, "workforcenow.adp.com")
                self.assertTrue(parsed.path.endswith("/mdf/recruitment/recruitment.html"))
                self.assertEqual(parse_qs(parsed.query)["jobId"], ["583650"])

    def test_a_workforce_now_url_without_a_tenant_is_not_routed_to_adp(self) -> None:
        # No cid/ccId means no tenant, so there is nothing for the API to read.
        self.assertNotIsInstance(selected("https://workforcenow.adp.com/mascsr/default/"), ADPCollector)

    def test_a_non_adp_host_is_still_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "workforcenow.adp.com"):
            build_adp_api_request(f"https://attacker.invalid/mascsr/default/?{ADP_TENANT_QUERY}")

    def test_a_url_missing_tenant_parameters_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "cid or ccId"):
            build_adp_api_request(
                "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            )


class UKGRecruitingVariantTests(unittest.TestCase):
    VARIANTS = (
        UKG_BOARD,
        f"{UKG_BOARD}/",
        f"{UKG_BOARD}/?q=&o=postedDateDesc",
        f"{UKG_BOARD}/OpportunityDetail?opportunityId=0f2a3b4c-5d6e-7f80-9012-3456789abcde",
        f"{UKG_BOARD}/JobBoardView/LoadSearchResults",
        UKG_BOARD.replace("recruiting.ultipro.com", "recruiting2.ultipro.com"),
    )

    def test_every_variant_selects_the_ukg_collector(self) -> None:
        for url in self.VARIANTS:
            with self.subTest(url=url):
                self.assertIsInstance(selected(url), UKGCollector)

    def test_every_variant_normalizes_back_to_the_tenant_board_root(self) -> None:
        for url in self.VARIANTS:
            with self.subTest(url=url):
                board_url, search_url = build_ukg_urls(url)
                self.assertEqual(
                    urlsplit(board_url).path,
                    "/EXA1000EXCU/JobBoard/a1ad5f09-7f9c-420c-9e77-4ace84ced6e0",
                )
                self.assertTrue(search_url.endswith("/JobBoardView/LoadSearchResults"))
                self.assertEqual(urlsplit(board_url).hostname, urlsplit(url).hostname)

    def test_a_foreign_host_is_still_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            build_ukg_urls(
                "https://attacker.invalid/EXA1000EXCU/JobBoard/"
                "a1ad5f09-7f9c-420c-9e77-4ace84ced6e0"
            )

    def test_a_path_without_a_board_uuid_is_still_refused(self) -> None:
        for path in ["/not-a-board", "/EXA1000EXCU/JobBoard/", "/EXA1000EXCU/JobBoard/not-a-uuid"]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "JobBoard UUID"):
                    build_ukg_urls(f"https://recruiting.ultipro.com{path}")


if __name__ == "__main__":
    unittest.main()
