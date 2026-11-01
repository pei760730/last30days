import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
# AGENTS.md is the canonical agent-guidance file; CLAUDE.md is a one-line
# pointer (`@AGENTS.md`) so anything Claude Code-shaped reads the same source.
AGENTS = ROOT / "AGENTS.md"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_security_workflow_exists() -> None:
    assert WORKFLOW.is_file()


def test_security_workflow_runs_dependency_audit_as_blocking_check() -> None:
    text = _workflow_text()
    dependency_audit_job = text.split("dependency-audit:", 1)[1].split("secret-scan:", 1)[0]

    assert "dependency-audit:" in text
    assert "uv audit --locked" in dependency_audit_job
    assert "continue-on-error: true" not in dependency_audit_job


def test_security_workflow_runs_secret_scan_for_pull_requests_and_main_pushes() -> None:
    text = _workflow_text()
    secret_scan_job = text.split("secret-scan:", 1)[1].split("sast-scan:", 1)[0]

    assert "secret-scan:" in text
    assert "pull_request:" in text
    assert "push:" in text
    assert "workflow_dispatch:" in text
    assert "branches:\n      - main" in text
    assert "trufflesecurity/trufflehog" in secret_scan_job
    assert "version: 3.95.5" in secret_scan_job
    assert "extra_args: --results=verified" in secret_scan_job
    assert "continue-on-error: true" not in secret_scan_job
    assert "if: github.event_name" not in secret_scan_job
    assert "path: ./" not in secret_scan_job


def test_security_workflow_runs_sast_without_unused_code_scanning_permission() -> None:
    text = _workflow_text()
    sast_job = text.split("sast-scan:", 1)[1]

    assert "semgrep/semgrep@sha256:" in sast_job
    assert "semgrep scan --config p/default --metrics off" in sast_job
    assert "SEMGREP_SEND_METRICS: off" in sast_job
    # Semgrep refuses `--config=auto` when metrics are off (exit 2), and
    # continue-on-error hid that the scan never ran.
    assert "semgrep scan --config=auto" not in sast_job
    assert "semgrep scan --config auto" not in sast_job
    assert "continue-on-error" not in sast_job
    assert 'python3 -I .github/scripts/semgrep_summary.py "$scan_exit"' in sast_job
    # Semgrep echoes repository file names; commands stay stopped around the
    # scan (stderr folded in, since the runner reads the streams separately)
    # and resume on a fresh line before the classifier's trusted annotations.
    before_scan, after_scan = sast_job.split("semgrep scan --config", 1)
    assert 'echo "::stop-commands::${stop_token}"' in before_scan
    assert "python3 -I -c 'import secrets; print(secrets.token_hex(16))'" in before_scan
    scan_line = after_scan.split("\n", 1)[0]
    assert scan_line.endswith("2>&1")
    resume, classifier = after_scan.split("python3 -I .github/scripts/semgrep_summary.py", 1)
    assert "printf '\\n::%s::\\n' \"$stop_token\"" in resume
    assert classifier.split("\n", 1)[0].endswith("2>&1")
    assert "contents: read" in sast_job
    assert "security-events: write" not in sast_job
    assert "--sarif" not in sast_job
    assert "upload-sarif" not in sast_job


def test_agent_guidance_mentions_secret_hygiene() -> None:
    text = AGENTS.read_text(encoding="utf-8")

    assert "Security hygiene" in text
    assert "Never commit real API keys" in text
    assert "skills/last30days/scripts/lib/env.py" in text
    assert "fixtures" in text


def _sast_run_block() -> str:
    step = _workflow_text().split("- name: Run Semgrep SAST scan", 1)[1]
    body = step.split("run: |\n", 1)[1]
    lines = []
    for line in body.split("\n"):
        if line.strip() and not line.startswith(" " * 10):
            break
        lines.append(line[10:])
    return "\n".join(lines).strip("\n") + "\n"


def _runner_commands(stdout: str) -> list[str]:
    """Commands the Actions runner would act on (V2 ``::`` and legacy ``##[``)."""
    commands, stop = [], None
    for raw in re.split(r"\r\n|\r|\n", stdout):
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


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("scan_exit", [0, 2])
def test_sast_run_block_keeps_scanner_output_out_of_workflow_commands(tmp_path, scan_exit) -> None:
    """Execute the real run block with a stub scanner that forges commands."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    report = {
        "results": [{"check_id": "r", "path": "bait\n::error title=FORGED::x", "start": {"line": 1}}],
        "errors": [],
        "paths": {"scanned": ["a.py"]},
    }
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    python = Path(sys.executable).as_posix()
    stub = bin_dir / "semgrep"
    stub.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do case "$arg" in --json-output=*) out="${arg#--json-output=}";; esac; done\n'
        f'cp "{(tmp_path / "report.json").as_posix()}" "$out"\n'
        "echo 'bait'\n"
        "echo '::error title=FORGED_STDOUT::x'\n"
        "echo '##[error title=FORGED_V1]x' >&2\n"
        "printf '::error title=FORGED_STDERR::unterminated' >&2\n"
        f"exit {scan_exit}\n",
        encoding="utf-8",
        newline="\n",
    )
    shim = bin_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{python}" "$@"\n', encoding="utf-8", newline="\n")
    for path in (stub, shim):
        path.chmod(0o755)
    script = tmp_path / "run.sh"
    script.write_text(_sast_run_block(), encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}",
        "RUNNER_TEMP": tmp_path.as_posix(),
        "PYTHONUTF8": "1",
    }
    env.pop("GITHUB_STEP_SUMMARY", None)
    proc = subprocess.run(
        ["sh", "-e", script.as_posix()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.stderr == ""  # everything untrusted went through the stopped stdout
    commands = _runner_commands(proc.stdout)
    if scan_exit == 0:
        assert proc.returncode == 0
        assert commands == [
            "::warning title=Semgrep SAST findings::Scan completed with 1 finding(s) (advisory, not blocking)."
        ]
    else:
        assert proc.returncode == 1
        assert commands == ["::error title=Semgrep SAST not completed::Semgrep exited 2; the scan did not complete."]
