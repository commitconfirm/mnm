"""Migration round-trip test.

Verifies that ``models.py`` and the on-disk
``0001_initial.py`` agree — running ``makemigrations
--check --dry-run`` should produce no diff.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase


class MigrationRoundTripTests(TestCase):
    def test_makemigrations_produces_no_diff(self):
        """If this test fails, run ``nautobot-server makemigrations
        mnm_plugin`` locally, review the diff, and commit."""
        out = StringIO()
        try:
            call_command(
                "makemigrations",
                "mnm_plugin",
                check=True,
                dry_run=True,
                stdout=out,
                verbosity=0,
            )
        except SystemExit as e:
            # Django exits with code 1 when there are unmade
            # migrations.
            self.fail(
                "makemigrations --check --dry-run reports "
                f"unmade migrations: {out.getvalue()!r} "
                f"(exit={e.code})"
            )
