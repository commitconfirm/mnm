"""Smoke tests for controller/app/static/js/table-export.js.

Runs the JS helpers under Node with a minimal CommonJS fixture and
asserts the CSV + JSON outputs for a 3-column, 2-row fixture that
exercises RFC 4180 edge cases (embedded comma, embedded double-quote).

Skipped if ``node`` is not on PATH — keeps the suite green on
environments without Node installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS_FILE = ROOT / "controller" / "app" / "static" / "js" / "table-export.js"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _have_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_table_export_csv_and_json_shape():
    """rowsToCSV + rowsToJSON over a 3x2 fixture with RFC 4180 edge
    cases. Runs the fixture JS file under Node."""
    script = FIXTURES / "table_export_smoke.js"
    result = subprocess.run(
        ["node", str(script), str(JS_FILE)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f"node script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    out = json.loads(result.stdout)
    assert "Name,IP,Notes" in out["csv"]
    assert out["json"][0] == {
        "Name": "alpha", "IP": "192.0.2.1", "Notes": "lab, main",
    }


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_table_export_empty_rows(tmp_path):
    """rowsToCSV on an empty rows array still emits the header line."""
    script = tmp_path / "run.js"
    script.write_text(
        "global.window = undefined;\n"
        "const exp = require(process.argv[2]);\n"
        "const csv = exp.rowsToCSV(['a', 'b'], []);\n"
        "if (csv !== 'a,b') {"
        " console.error('expected header-only, got', JSON.stringify(csv));"
        " process.exit(1); }\n"
        "process.stdout.write('ok');\n"
    )
    result = subprocess.run(
        ["node", str(script), str(JS_FILE)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert result.stdout == "ok"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_table_export_iso_date_format(tmp_path):
    """isoDate() returns YYYY-MM-DD (UTC)."""
    script = tmp_path / "run.js"
    script.write_text(
        "global.window = undefined;\n"
        "const exp = require(process.argv[2]);\n"
        "const d = exp.isoDate();\n"
        "if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(d)) {"
        " console.error('bad date', d); process.exit(1); }\n"
        "process.stdout.write(d);\n"
    )
    result = subprocess.run(
        ["node", str(script), str(JS_FILE)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert len(result.stdout) == 10
