#!/usr/bin/env python3
"""Classify a Semgrep CE run for .github/workflows/security.yml.

The SAST job is advisory: findings are reported but do not fail the job.
A scan that did not complete (non-zero Semgrep exit, missing or unreadable
JSON report, fatal errors, or zero scanned files) is not a pass and fails the
job, so a configuration or tool error can never look like "no findings".

Usage:
  python3 .github/scripts/semgrep_summary.py EXIT_CODE REPORT_JSON
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CLEAN = "clean"
FINDINGS = "findings"
FAILED = "failed"


def classify(exit_code: int, report: dict | None) -> tuple[str, list[str]]:
    """Return (status, summary lines) for one Semgrep run."""
    if exit_code != 0:
        return FAILED, [f"Semgrep exited {exit_code}; the scan did not complete."]
    if not isinstance(report, dict):
        return FAILED, ["Semgrep JSON report is missing or unreadable; the scan did not complete."]

    errors = report.get("errors") or []
    fatal = [e for e in errors if e.get("level") != "warn"]
    if fatal:
        return FAILED, [f"Semgrep reported {len(fatal)} fatal error(s); the scan did not complete."]

    scanned = (report.get("paths") or {}).get("scanned") or []
    if not scanned:
        return FAILED, ["Semgrep scanned 0 files; no source was covered."]

    results = report.get("results") or []
    lines = [f"Files scanned: {len(scanned)}."]
    if errors:
        lines.append(f"Non-fatal warnings (partially analyzed files): {len(errors)}.")
    if not results:
        return CLEAN, ["Scan completed with no findings.", *lines]

    lines.insert(0, f"Scan completed with {len(results)} finding(s) (advisory, not blocking).")
    for result in results:
        start = result.get("start") or {}
        lines.append(f"- {result.get('path')}:{start.get('line')} {result.get('check_id')}")
    return FINDINGS, lines


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    status, lines = classify(int(argv[1]), _load(Path(argv[2])))

    titles = {
        CLEAN: "Semgrep SAST: completed, no findings",
        FINDINGS: "Semgrep SAST: completed with findings (advisory)",
        FAILED: "Semgrep SAST: NOT COMPLETED (execution error, not a pass)",
    }
    summary = "\n".join([f"## {titles[status]}", "", *lines, ""])
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary)

    if status == FAILED:
        print(f"::error title=Semgrep SAST not completed::{lines[0]}")
        return 1
    if status == FINDINGS:
        print(f"::warning title=Semgrep SAST findings::{lines[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
