from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from fastapi import HTTPException

import server
from backend.repository import DuplicateCompanyError


class CompanyApiConflictTests(unittest.TestCase):
    def test_company_request_exposes_company_description(self) -> None:
        request = server.CompanyRequest(
            name="Example Company",
            companyDescription="An API-visible company description.",
        )
        self.assertEqual(
            request.model_dump()["companyDescription"],
            "An API-visible company description.",
        )

    def test_create_returns_conflict_for_normalized_duplicate(self) -> None:
        service = Mock()
        service.add_company.side_effect = DuplicateCompanyError(
            " EXAMPLE ", "company-existing", "Example",
        )
        with (
            patch.object(server, "company_service", return_value=service),
            patch.object(server, "api_mutation", return_value=nullcontext()),
            self.assertRaises(HTTPException) as error,
        ):
            server.create_company_endpoint(server.CompanyRequest(name=" EXAMPLE "))
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("already exists", str(error.exception.detail))

    def test_update_returns_conflict_for_normalized_duplicate(self) -> None:
        service = Mock()
        service.edit_company.side_effect = DuplicateCompanyError(
            " EXAMPLE ", "company-existing", "Example",
        )
        with (
            patch.object(server, "company_service", return_value=service),
            patch.object(server, "api_mutation", return_value=nullcontext()),
            self.assertRaises(HTTPException) as error,
        ):
            server.update_company_endpoint(
                "company-other", server.CompanyRequest(name=" EXAMPLE "),
            )
        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
