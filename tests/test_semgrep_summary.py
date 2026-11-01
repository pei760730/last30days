import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "semgrep_summary.py"


def _load():
    spec = importlib.util.spec_from_file_location("semgrep_summary", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def _report(results=(), errors=(), scanned=("src/app.py",)):
    return {"results": list(results), "errors": list(errors), "paths": {"scanned": list(scanned)}}


FINDING = {"check_id": "python.lang.security.audit.eval-detected", "path": "src/app.py", "start": {"line": 3}}


def test_clean_scan_reports_no_findings():
    status, lines = mod.classify(0, _report())
    assert status == mod.CLEAN
    assert "Files scanned: 1." in lines


def test_findings_are_advisory():
    status, lines = mod.classify(0, _report(results=[FINDING]))
    assert status == mod.FINDINGS
    assert "advisory" in lines[0]
    assert "- `src/app.py`:3 `python.lang.security.audit.eval-detected`" in lines


@pytest.mark.parametrize("exit_code", [1, 2, 7, 13])
def test_nonzero_exit_is_failure_even_with_empty_report(exit_code):
    status, _ = mod.classify(exit_code, _report())
    assert status == mod.FAILED


def test_missing_report_is_failure_not_zero_findings():
    status, _ = mod.classify(0, None)
    assert status == mod.FAILED


def test_zero_scanned_files_is_failure():
    status, _ = mod.classify(0, _report(scanned=()))
    assert status == mod.FAILED


def test_fatal_error_entry_is_failure():
    status, _ = mod.classify(0, _report(errors=[{"level": "error", "type": "InvalidRuleSchemaError"}]))
    assert status == mod.FAILED


def test_warn_level_errors_are_reported_but_scan_counts():
    status, lines = mod.classify(0, _report(errors=[{"level": "warn", "type": "Timeout"}]))
    assert status == mod.CLEAN
    assert any("Non-fatal warnings" in line for line in lines)


def test_main_exit_codes_and_step_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = tmp_path / "semgrep.json"

    report.write_text(json.dumps(_report(results=[FINDING])), encoding="utf-8")
    assert mod.main(["x", "0", str(report)]) == 0
    assert "::warning title=Semgrep SAST findings::" in capsys.readouterr().out

    assert mod.main(["x", "2", str(tmp_path / "absent.json")]) == 1
    out = capsys.readouterr().out
    assert "NOT COMPLETED" in out
    assert "::error title=Semgrep SAST not completed::" in out
    assert "no findings" not in out.lower()

    text = summary.read_text(encoding="utf-8")
    assert "completed with findings (advisory)" in text
    assert "NOT COMPLETED (execution error, not a pass)" in text


# --- Untrusted finding text must stay data (paths come from repo content) ---

FORGED_COMMAND = {
    "check_id": "python.lang.security.audit.eval-detected",
    "path": "bait\n::error title=FORGED_REVIEW::attacker.py",
    "start": {"line": 1},
}
FORGED_HEADING = {
    "check_id": "rule\r\n\r\n## FORGED CLEAN PASS\n\nall good",
    "path": "x\n\n## FORGED CLEAN PASS\n\n[click](https://example.invalid) `tick`",
    "start": {"line": 2},
}


def _runner_commands(stdout: str) -> list[str]:
    """Lines the Actions runner would act on, honouring ::stop-commands::."""
    commands, stop = [], None
    for raw in stdout.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if stop is not None:
            if line == f"::{stop}::":
                stop = None
            continue
        if line.startswith("::stop-commands::"):
            stop = line[len("::stop-commands::"):]
            continue
        if line.startswith("::"):
            commands.append(line)
    assert stop is None, "workflow commands were never resumed"
    return commands


def _run_main(tmp_path, monkeypatch, capsys, results):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = tmp_path / "semgrep.json"
    report.write_text(json.dumps(_report(results=results)), encoding="utf-8")
    code = mod.main(["x", "0", str(report)])
    return code, capsys.readouterr().out, summary.read_text(encoding="utf-8")


def test_command_like_path_cannot_forge_a_workflow_command(tmp_path, monkeypatch, capsys):
    code, out, _ = _run_main(tmp_path, monkeypatch, capsys, [FORGED_COMMAND])
    assert code == 0
    assert _runner_commands(out) == [
        "::warning title=Semgrep SAST findings::Scan completed with 1 finding(s) (advisory, not blocking)."
    ]
    # Escaped onto the finding's own line, not dropped.
    assert r"bait\u000a::error title=FORGED_REVIEW::attacker.py" in out


def test_path_cannot_inject_summary_headings_or_markdown(tmp_path, monkeypatch, capsys):
    _, _, text = _run_main(tmp_path, monkeypatch, capsys, [FORGED_HEADING])
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == ["## Semgrep SAST: completed with findings (advisory)"]
    finding = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(finding) == 1
    # Each untrusted value is one code span, so links and backticks stay literal.
    assert finding[0].startswith(r"- `` x\u000a\u000a## FORGED CLEAN PASS")
    assert "`tick`" in finding[0]


def test_failed_scan_output_also_resumes_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    assert mod.main(["x", "2", str(tmp_path / "absent.json")]) == 1
    assert _runner_commands(capsys.readouterr().out) == [
        "::error title=Semgrep SAST not completed::Semgrep exited 2; the scan did not complete."
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("src/app.py", "`src/app.py`"),
        ("a`b", "``a`b``"),
        ("`edge`", "`` `edge` ``"),
        ("line\u2028sep\x85x\x7f", r"`line\u2028sep\u0085x\u007f`"),
    ],
)
def test_code_span_escaping(value, expected):
    assert mod._code(value) == expected
