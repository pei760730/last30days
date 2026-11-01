import importlib.util
import json
import re
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
# The runner's legacy parser acts on `##[cmd]` anywhere in a line, so escaping
# alone cannot neutralise this one; only the stopped region does.
FORGED_LEGACY = {"check_id": "r", "path": "x##[error title=FORGED_V1]FORGED_V1", "start": {"line": 1}}
FORGED_HEADING = {
    "check_id": "rule\r\n\r\n## FORGED CLEAN PASS\n\nall good",
    "path": "x\n\n## FORGED CLEAN PASS\n\n[click](https://example.invalid) `tick`",
    "start": {"line": 2},
}
FORGED_BARE_CR = {"check_id": "r", "path": "x\r\r## FORGED CLEAN PASS\r", "start": {"line": 3}}
FORGED_LINK = {"check_id": "r", "path": "`[click](https://example.invalid) **bold**", "start": {"line": 4}}
FORGED_ALL = [FORGED_COMMAND, FORGED_LEGACY, FORGED_HEADING, FORGED_BARE_CR, FORGED_LINK]

TRUSTED_WARNING = "::warning title=Semgrep SAST findings::Scan completed with {n} finding(s) (advisory, not blocking)."


def _runner_lines(stdout: str) -> list[str]:
    return re.split(r"\r\n|\r|\n", stdout)


def _runner_commands(stdout: str) -> list[str]:
    """Lines the Actions runner would act on, honouring ::stop-commands::.

    Mirrors ActionCommand.TryParseV2 (trimmed line starting with ``::``) and
    the legacy TryParse fallback (``##[`` anywhere in the line).
    """
    commands, stop = [], None
    for raw in _runner_lines(stdout):
        line = raw.strip()
        if stop is not None:
            if line == f"::{stop}::":
                stop = None
            continue
        if line.startswith("::stop-commands::"):
            stop = line[len("::stop-commands::"):]
            continue
        if line.startswith("::") or "##[" in raw:
            commands.append(line)
    assert stop is None, "workflow commands were never resumed"
    return commands


def _stopped_region(stdout: str) -> tuple[str, list[str]]:
    lines = _runner_lines(stdout)
    starts = [i for i, line in enumerate(lines) if line.startswith("::stop-commands::")]
    assert len(starts) == 1
    token = lines[starts[0]][len("::stop-commands::"):]
    end = lines.index(f"::{token}::")
    return token, lines[starts[0] + 1 : end]


def _run_main(tmp_path, monkeypatch, capsys, results):
    summary = tmp_path / "summary.md"
    summary.unlink(missing_ok=True)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = tmp_path / "semgrep.json"
    report.write_text(json.dumps(_report(results=results)), encoding="utf-8")
    code = mod.main(["x", "0", str(report)])
    return code, capsys.readouterr().out, summary.read_text(encoding="utf-8")


def test_command_like_paths_cannot_forge_workflow_commands(tmp_path, monkeypatch, capsys):
    code, out, _ = _run_main(tmp_path, monkeypatch, capsys, FORGED_ALL)
    assert code == 0
    assert _runner_commands(out) == [TRUSTED_WARNING.format(n=len(FORGED_ALL))]
    # Every finding is listed inside the stopped region, escaped onto one line.
    _, region = _stopped_region(out)
    findings = [line for line in region if line.startswith("- ")]
    assert len(findings) == len(FORGED_ALL)
    assert r"bait\u000a::error title=FORGED_REVIEW::attacker.py" in findings[0]
    assert "x##[error title=FORGED_V1]FORGED_V1" in findings[1]


def test_stop_token_is_unpredictable(tmp_path, monkeypatch, capsys):
    tokens = {_stopped_region(_run_main(tmp_path, monkeypatch, capsys, [FINDING])[1])[0] for _ in range(3)}
    assert len(tokens) == 3
    assert all(re.fullmatch(r"[0-9a-f]{32}", token) for token in tokens)


def test_paths_cannot_inject_summary_headings_or_markdown(tmp_path, monkeypatch, capsys):
    _, _, text = _run_main(tmp_path, monkeypatch, capsys, FORGED_ALL)
    # No stray line breaks of any kind: Markdown sees exactly the lines we wrote.
    assert text.splitlines() == text.split("\n")[:-1]
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == ["## Semgrep SAST: completed with findings (advisory)"]
    findings = [line for line in text.splitlines() if line.startswith("- ")]
    assert findings[2].startswith(r"- `` x\u000a\u000a## FORGED CLEAN PASS")
    assert "`tick`" in findings[2]
    assert findings[3] == r"- `x\u000d\u000d## FORGED CLEAN PASS\u000d`:3 `r`"
    assert findings[4] == "- `` `[click](https://example.invalid) **bold** ``:4 `r`"


def test_undecodable_file_name_still_reports(tmp_path, monkeypatch, capsys):
    # json.loads cannot produce this, but a lone surrogate is what Python-side
    # tooling yields for a non-UTF-8 file name; it must not crash the summary.
    surrogate = {"check_id": "r", "path": "bad\udcffname.py", "start": {"line": 1}}
    code, out, text = _run_main(tmp_path, monkeypatch, capsys, [surrogate])
    assert code == 0
    assert r"`bad\udcffname.py`" in text
    assert _runner_commands(out) == [TRUSTED_WARNING.format(n=1)]


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
        ("`a", "`` `a ``"),
        ("a`", "`` a` ``"),
        (" a ", "`  a  `"),
        ("", "` `"),
        ("  ", "`  `"),
        ("a\rb", r"`a\u000db`"),
        ("line\u2028sep\x85x\x7f\u2029", r"`line\u2028sep\u0085x\u007f\u2029`"),
        ("\u202egnp.exe\u200b", r"`\u202egnp.exe\u200b`"),
        ("x\U000e0001y", r"`x\U000e0001y`"),
    ],
)
def test_code_span_escaping(value, expected):
    assert mod._code(value) == expected
