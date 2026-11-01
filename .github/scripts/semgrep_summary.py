#!/usr/bin/env python3
"""Classify a Semgrep CE run for .github/workflows/security.yml.

The SAST job is advisory: findings are reported but do not fail the job.
A scan that did not complete (non-zero Semgrep exit, missing or unreadable
JSON report, fatal errors, or zero scanned files) is not a pass and fails the
job, so a configuration or tool error can never look like "no findings".

Finding paths and rule ids come from repository content and the public
registry, so they are untrusted: they are escaped onto one line and shown as
Markdown code, and the listing is printed with workflow commands stopped.
Only the fixed, count-based annotations below are emitted as commands.

Usage:
  python3 .github/scripts/semgrep_summary.py EXIT_CODE REPORT_JSON
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import unicodedata
from pathlib import Path

CLEAN = "clean"
FINDINGS = "findings"
FAILED = "failed"

# Escaped so untrusted text stays on one visible line: controls and line
# breaks the runner or Markdown split on (Cc, Zl, Zp), invisible format
# characters such as bidi overrides and zero-width spaces (Cf), and lone
# surrogates from non-UTF-8 file names that cannot be written out (Cs).
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _escape(text: str) -> str:
    return "".join(
        (f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
        if unicodedata.category(ch) in _ESCAPED_CATEGORIES
        else ch
        for ch in text
    )


def _code(value: object) -> str:
    """Render untrusted text as a single-line Markdown code span."""
    text = _escape(str(value))
    if not text.strip(" "):
        # CommonMark never strips an all-space span; an empty one is not a span.
        return f"`{text or ' '}`"
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # One space is stripped from each side when both ends are spaces, and a
    # backtick next to the fence would merge with it, so pad in either case.
    pad = " " if text[0] in "` " or text[-1] in "` " else ""
    return f"{fence}{pad}{text}{pad}{fence}"


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
        lines.append(f"- {_code(result.get('path'))}:{start.get('line')} {_code(result.get('check_id'))}")
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
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}")
    print(summary)
    print(f"::{token}::")
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
