import json
import tempfile
import unittest
from pathlib import Path

import backend.database as database
from backend.job_collection import Candidate, _extract_compensation_and_benefits, _save_job


BECU_DESCRIPTION = """PAY RANGE The Target Pay Range for this position is $171,700.00-$209,800.00 annually.
The full Pay Range is $133,100.00-$248,300.00 annually. In addition to your salary,
compensation incentives are available. Incentives are performance based. BENEFITS
401(k) Company Match (up to 3%) 4% annual contribution to your 401(k) by BECU
Medical, Dental and Vision PTO Program + Exchange Program Tuition Reimbursement Program
BECU Cares volunteer time off + donation match IMPACT YOU'LL MAKE:"""

WECU_BENEFITS_DESCRIPTION = """This is an incentive-based position with a competitive commission program featuring a draw,
an organizational bonus, and an outstanding benefits package. RESPONSIBILITIES: Serve members. COMPENSATION:
This commission-based position offers 75 basis points for originated residential loans as well as a guaranteed monthly draw.
WECU provides a comprehensive benefits package that includes robust medical, dental, and vision benefits with low employee
premiums, 401(k) retirement plan with an 8% annual contribution from WECU, bonus plan, two or more weeks of vacation,
up to 11 paid holidays, paid life and disability insurance, annual wellness benefit, loan discounts, professional development,
and much more. ABOUT WECU:"""


class BecuCompensationPathTest(unittest.TestCase):
    def test_full_description_benefits_are_not_lost_after_a_prose_heading(self):
        candidate = Candidate(
            title="Real Estate Loan Officer",
            detail_url="https://workforcenow.adp.com/example",
            description=WECU_BENEFITS_DESCRIPTION,
        )
        _extract_compensation_and_benefits(candidate)
        self.assertTrue(candidate.has_health_insurance)
        self.assertTrue(candidate.has_dental_insurance)
        self.assertTrue(candidate.has_vision_insurance)
        self.assertTrue(candidate.has_retirement)
        self.assertTrue(candidate.has_pto)
        self.assertEqual(candidate.retirement_contribution_percent, 8)
        self.assertIn("8% annual contribution", candidate.retirement_details)
        self.assertIn("medical, dental, and vision", candidate.benefits_source_text.lower())

    def test_parser_database_and_api_return_structured_values(self):
        original_path = database.DATABASE_PATH
        try:
            database.DATABASE_PATH = Path(tempfile.gettempdir()) / "becu_compensation_path_test.db"
            database.DATABASE_PATH.unlink(missing_ok=True)
            database.initialize_database()
            candidate = Candidate(
                title="Staff Software Developer - AI Innovation Team",
                detail_url="https://becu.wd1.myworkdayjobs.com/External/job/test_R-12991",
                location="Remote-WA",
                description=BECU_DESCRIPTION,
            )
            _extract_compensation_and_benefits(candidate)
            with database.get_connection() as conn:
                _save_job(conn, 1, "Workday", candidate)

            from fastapi.testclient import TestClient
            from backend.main import app

            with TestClient(app) as client:
                jobs = client.get("/api/jobs").json()
            job = next(item for item in jobs if item["title"] == candidate.title)
            self.assertEqual(job["target_pay_min"], 171700)
            self.assertEqual(job["target_pay_max"], 209800)
            self.assertEqual(job["full_pay_min"], 133100)
            self.assertEqual(job["full_pay_max"], 248300)
            self.assertEqual(job["retirement_match_percent"], 3)
            self.assertEqual(job["retirement_contribution_percent"], 4)
            self.assertTrue(job["has_health_insurance"])
            self.assertTrue(job["has_tuition_reimbursement"])
            self.assertTrue(job["compensation_source_text"])
            self.assertTrue(job["benefits_source_text"])
            self.assertNotIn("Not listed by employer", json.dumps(job))
        finally:
            database.DATABASE_PATH.unlink(missing_ok=True)
            database.DATABASE_PATH = original_path


if __name__ == "__main__":
    unittest.main()
