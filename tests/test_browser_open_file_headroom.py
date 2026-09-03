"""RLIMIT_NOFILE preflight for the protected browser runtime.

A headless Chromium tree run ``--disable-dev-shm-usage`` (Playwright's default)
backs every shared-memory region with a file descriptor. The 1024 soft
``RLIMIT_NOFILE`` a container gets by default is exhausted by heavier career
pages: Chromium hits ``EMFILE`` and self-aborts, which Playwright surfaces as
``Page crashed``. ``validate_browser_runtime_boundary`` refuses to launch below
``MINIMUM_BROWSER_OPEN_FILE_LIMIT`` so a mis-sized deployment fails at startup
instead of as intermittent, load-dependent crashes.
"""
from __future__ import annotations

import inspect
import unittest
from pathlib import Path

try:  # POSIX only; the boundary itself already requires Linux.
    import resource
except ImportError:  # pragma: no cover - Windows dev boxes
    resource = None

from backend import outbound_security as ob


class BoundaryWiresThePreflightTests(unittest.TestCase):
    """Portable check (no POSIX rlimit needed): the boundary validator calls the
    preflight, and before the Playwright / Chromium metadata checks so a bad
    file-descriptor limit is reported first."""

    def test_validate_boundary_calls_preflight_before_runtime_metadata(self) -> None:
        src = inspect.getsource(ob.validate_browser_runtime_boundary)
        self.assertIn("_require_browser_open_file_headroom()", src)
        self.assertLess(
            src.index("_require_browser_open_file_headroom()"),
            src.index('importlib.metadata.version("playwright")'),
        )

    def test_preflight_is_documented_in_the_page_crash_runbook(self) -> None:
        doc = (Path(__file__).resolve().parents[1] / "docs" / "BROWSER_PAGE_CRASH.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("RLIMIT_NOFILE", doc)
        self.assertIn("65535", doc)


@unittest.skipIf(resource is None, "resource module (POSIX RLIMIT_NOFILE) unavailable")
class BrowserOpenFileHeadroomTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = resource.getrlimit(resource.RLIMIT_NOFILE)

    def tearDown(self) -> None:
        resource.setrlimit(resource.RLIMIT_NOFILE, self._saved)

    def test_soft_limit_below_floor_and_unraisable_fails_closed(self) -> None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        with self.assertRaises(ob.BrowserEgressConfigurationError):
            ob._require_browser_open_file_headroom()

    def test_low_soft_limit_under_high_hard_ceiling_is_lifted_not_rejected(self) -> None:
        _soft, hard = self._saved
        if hard != resource.RLIM_INFINITY and hard < ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT:
            self.skipTest("hard ceiling below the browser floor; cannot lift")
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
        ob._require_browser_open_file_headroom()  # must not raise
        new_soft, _new_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.assertTrue(
            new_soft == resource.RLIM_INFINITY
            or new_soft >= ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT
        )

    def test_soft_limit_at_or_above_floor_passes(self) -> None:
        _soft, hard = self._saved
        target = ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT
        if hard != resource.RLIM_INFINITY:
            target = min(target, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(target, ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT), hard))
        ob._require_browser_open_file_headroom()  # must not raise

    def test_floor_is_well_above_the_container_default(self) -> None:
        # The Docker default soft limit is 1024; the floor must be meaningfully
        # higher or the check does nothing.
        self.assertGreaterEqual(ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT, 4096)


if __name__ == "__main__":
    unittest.main()
