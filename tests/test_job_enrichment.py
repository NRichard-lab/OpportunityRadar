from __future__ import annotations

import unittest

from job_enrichment import classify_role_type, extract_json_ld_pay_info, extract_pay_info


class JobEnrichmentTests(unittest.TestCase):
    def test_pay_ranges_and_periods(self) -> None:
        fixtures = (
            ("Salary range $80,000-$105,000 annually", 80_000, 105_000, "annual", "USD"),
            ("Pay: $24.50 to $31.75 per hour", 24, 32, "hourly", "USD"),
            ("Compensation USD 90k–110k per year", 90_000, 110_000, "annual", "USD"),
            ("Hourly rate 28 to 34 USD", 28, 34, "hourly", "USD"),
            ("Salary: £55,000 - £65,000 annually", 55_000, 65_000, "annual", "GBP"),
        )
        for text, minimum, maximum, period, currency in fixtures:
            with self.subTest(text=text):
                result = extract_pay_info(text)
                self.assertEqual((result["payMin"], result["payMax"]), (minimum, maximum))
                self.assertEqual(result["payPeriod"], period)
                self.assertEqual(result["payCurrency"], currency)

    def test_pay_negative_fixtures(self) -> None:
        for text in (
            "We offer competitive pay and excellent benefits.",
            "Apply by 2026-09-15.",
            "Manage a portfolio of 401(k) accounts.",
            "Competitive pay; benefits include 401(k) matching.",
            "This role requires up to 10 years of experience.",
            "Competitive pay includes a 401k retirement plan.",
            "The team grew by 25 percent last year.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_pay_info(text)["payMin"])

    def test_json_ld_range_and_currency(self) -> None:
        result = extract_json_ld_pay_info({
            "currency": "CAD",
            "value": {"minValue": "70000", "maxValue": "90000", "unitText": "YEAR"},
        })
        self.assertEqual((result["payMin"], result["payMax"]), (70_000, 90_000))
        self.assertEqual(result["payPeriod"], "annual")
        self.assertEqual(result["payCurrency"], "CAD")

    def test_generic_hiring_language_is_not_management(self) -> None:
        result = classify_role_type(
            "Security Analyst",
            "We are hiring an analyst to investigate alerts and document findings.",
        )
        self.assertEqual(result["roleType"], "IC")

    def test_people_management_signals_remain_manager(self) -> None:
        fixtures = (
            ("Operations Director", "Owns service delivery."),
            ("Team Lead", "Has direct reports and conducts performance reviews."),
            ("Branch Operations", "Manages a team and coaches staff."),
        )
        for title, description in fixtures:
            with self.subTest(title=title):
                self.assertEqual(classify_role_type(title, description)["roleType"], "MGR")


if __name__ == "__main__":
    unittest.main()
