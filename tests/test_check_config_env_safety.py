"""Security tests for hooks/scripts/check-config.sh env parsing.

Covers the SessionStart hook's .env loader:

  - printf -v key injection (array-subscript command substitution)
  - LAST30DAYS_TRUST_PROJECT_CONFIG gate matching lib/env.py
  - Legitimate identifier keys still parse

The PoC key ``x[$(touch RCE-PROOF.txt)]=1`` executes under bash 4+/5 via
printf -v assignment semantics; bash 3.2 rejects it as an invalid identifier
and (with ``set -e``) aborts the hook. Either way, after the fix the hook must
exit 0 and must never create the proof file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "check-config.sh"
POC_LINE = "x[$(touch RCE-PROOF.txt)]=1\n"
NO_USABLE_BASH = "no usable bash on this machine — RCE-path tests run in CI"
# bash discovery is shared across every bash-shelling test file — see
# tests/_bash_compat.py for why a hand-copied which("bash") is not enough.
from _bash_compat import BASH_PROBE_TIMEOUT_SECONDS as _BASH_PROBE_TIMEOUT_SECONDS
from _bash_compat import usable_bash_binaries as _bash_binaries


def _bash_major(bash_path: str) -> int:
    try:
        result = subprocess.run(
            [bash_path, "-c", 'echo "${BASH_VERSINFO[0]}"'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            # text=True alone decodes with the console codepage (cp950 on this
            # owner's Windows); the hook emits UTF-8, so pin the decoding or a
            # single multibyte character turns stdout into a decode error.
            encoding="utf-8",
            errors="replace",
            timeout=_BASH_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        return int((result.stdout or "").strip() or "0")
    except ValueError:
        return 0


def _mode_bits_assertable() -> bool:
    """Match check_perms: Windows/MSYS synthesized modes are not meaningful."""
    if os.name == "nt":
        return False
    uname = ""
    try:
        uname = os.uname().sysname  # type: ignore[attr-defined]
    except AttributeError:
        return True
    return not uname.startswith(("MINGW", "MSYS", "CYGWIN"))


def _assert_mode(path: Path, expected: str) -> None:
    if _mode_bits_assertable():
        assert oct(path.stat().st_mode)[-3:] == expected


def _isolated_path(tmp_path: Path) -> str:
    """PATH with a stub ``security`` so macOS Keychain presence cannot leak into assertions."""
    bin_dir = tmp_path / "hook-bin"
    bin_dir.mkdir(exist_ok=True)
    security = bin_dir / "security"
    if not security.exists():
        security.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        security.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _run_hook(
    bash_path: str,
    cwd: Path,
    tmp_path: Path,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for k in (
        "LAST30DAYS_MEMORY_DIR",
        "SETUP_COMPLETE",
        "LAST30DAYS_CONFIG_DIR",
        "LAST30DAYS_TRUST_PROJECT_CONFIG",
        "OPENAI_API_KEY",
        "SCRAPECREATORS_API_KEY",
        "AUTH_TOKEN",
        "CT0",
        "XAI_API_KEY",
        "BSKY_HANDLE",
        "EXA_API_KEY",
    ):
        env.pop(k, None)
    env["PATH"] = _isolated_path(tmp_path)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [bash_path, str(HOOK)],
        capture_output=True,
        text=True,
        # Same cp950 trap as _bash_major: without an explicit encoding the
        # hook's UTF-8 output fails to decode on Windows and stdout reads None.
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(cwd),
        timeout=30,
        check=False,
    )


@pytest.fixture(scope="session")
def bash_binaries() -> tuple[str, ...]:
    """Probe bash candidates at test runtime, after collection has completed."""
    binaries = tuple(_bash_binaries())
    if not binaries:
        pytest.skip(NO_USABLE_BASH)
    return binaries


def _check_malicious_project_env_key_does_not_execute(bash_path: str, tmp_path: Path):
    """Reporter PoC: crafted key must not run, even under bash 4+/5."""
    project = tmp_path / "repo"
    env_file = project / ".claude" / "last30days.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"SETUP_COMPLETE=1\n{POC_LINE}", encoding="utf-8")
    proof = project / "RCE-PROOF.txt"

    result = _run_hook(
        bash_path,
        project,
        tmp_path,
        {
            "LAST30DAYS_CONFIG_DIR": str(tmp_path / "empty-config"),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert not proof.exists(), f"RCE proof file was created under {bash_path}"
    assert result.returncode == 0, (
        f"hook aborted under {bash_path}: stderr={result.stderr!r} stdout={result.stdout!r}"
    )


def _check_malicious_key_blocked_even_when_project_trusted(bash_path: str, tmp_path: Path):
    """Identifier gate must hold even after an explicit trust opt-in."""
    project = tmp_path / "repo"
    env_file = project / ".claude" / "last30days.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"SETUP_COMPLETE=1\n{POC_LINE}", encoding="utf-8")
    proof = project / "RCE-PROOF.txt"

    result = _run_hook(
        bash_path,
        project,
        tmp_path,
        {
            "LAST30DAYS_TRUST_PROJECT_CONFIG": "1",
            "LAST30DAYS_CONFIG_DIR": str(tmp_path / "empty-config"),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert not proof.exists(), f"RCE proof file was created under {bash_path} (trusted path)"
    assert result.returncode == 0, (
        f"hook aborted under {bash_path}: stderr={result.stderr!r} stdout={result.stdout!r}"
    )
    assert "Ready" in result.stdout


def _check_untrusted_project_env_is_ignored(bash_path: str, tmp_path: Path):
    """Without LAST30DAYS_TRUST_PROJECT_CONFIG, project file is not read or chmod'd."""
    project = tmp_path / "repo"
    env_file = project / ".claude" / "last30days.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "SETUP_COMPLETE=true\nSCRAPECREATORS_API_KEY=scrape-test-key-untrusted\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    result = _run_hook(
        bash_path,
        project,
        tmp_path,
        {
            "LAST30DAYS_CONFIG_DIR": str(tmp_path / "empty-config"),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert result.returncode == 0, result.stderr
    # check_perms only runs on the chosen config file; untrusted project stays 644.
    _assert_mode(env_file, "644")
    # Project ScrapeCreators key must not suppress the tip when the configured
    # banner path runs (isolated PATH stubs Keychain).
    if "sources active" in result.stdout:
        assert "Tip: Add ScrapeCreators" in result.stdout
    else:
        assert "Ready to use" in result.stdout


def _check_trusted_project_env_loads_normal_keys(bash_path: str, tmp_path: Path):
    project = tmp_path / "repo"
    env_file = project / ".claude" / "last30days.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "SETUP_COMPLETE=true\n"
        "SCRAPECREATORS_API_KEY=scrape-test-key\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    result = _run_hook(
        bash_path,
        project,
        tmp_path,
        {
            "LAST30DAYS_TRUST_PROJECT_CONFIG": "1",
            "LAST30DAYS_CONFIG_DIR": str(tmp_path / "empty-config"),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert re.search(r"Ready — \d+ sources active", result.stdout)
    assert "Tip: Add ScrapeCreators" not in result.stdout
    _assert_mode(env_file, "600")


def _check_global_trust_signal_unlocks_project_env(bash_path: str, tmp_path: Path):
    """Trust from ~/.config (via LAST30DAYS_CONFIG_DIR) unlocks project overlay."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "LAST30DAYS_TRUST_PROJECT_CONFIG=1\nSETUP_COMPLETE=true\n",
        encoding="utf-8",
    )

    project = tmp_path / "repo"
    env_file = project / ".claude" / "last30days.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("SCRAPECREATORS_API_KEY=from-project\n", encoding="utf-8")
    env_file.chmod(0o644)

    result = _run_hook(
        bash_path,
        project,
        tmp_path,
        {
            "LAST30DAYS_CONFIG_DIR": str(config_dir),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert re.search(r"Ready — \d+ sources active", result.stdout)
    assert "Tip: Add ScrapeCreators" not in result.stdout
    _assert_mode(env_file, "600")


def _check_process_deny_overrides_global_trust(bash_path: str, tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "LAST30DAYS_TRUST_PROJECT_CONFIG=1\nSETUP_COMPLETE=true\n",
        encoding="utf-8",
    )

    project = tmp_path / "repo"
    env_file = project / ".claude" / "last30days.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("SCRAPECREATORS_API_KEY=from-project\n", encoding="utf-8")
    env_file.chmod(0o644)

    result = _run_hook(
        bash_path,
        project,
        tmp_path,
        {
            "LAST30DAYS_TRUST_PROJECT_CONFIG": "0",
            "LAST30DAYS_CONFIG_DIR": str(config_dir),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert re.search(r"Ready — \d+ sources active", result.stdout)
    assert "Tip: Add ScrapeCreators" in result.stdout
    _assert_mode(env_file, "644")


def _check_trusted_project_env_discovered_from_nested_cwd(bash_path: str, tmp_path: Path):
    """Mirror lib/env.py: walk up from a subdirectory to the repo-root project env."""
    repo = tmp_path / "repo"
    nested = repo / "apps" / "web"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    env_file = repo / ".claude" / "last30days.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "SETUP_COMPLETE=true\nSCRAPECREATORS_API_KEY=from-repo-root\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    result = _run_hook(
        bash_path,
        nested,
        tmp_path,
        {
            "LAST30DAYS_TRUST_PROJECT_CONFIG": "1",
            "LAST30DAYS_CONFIG_DIR": str(tmp_path / "empty-config"),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert re.search(r"Ready — \d+ sources active", result.stdout)
    assert "Tip: Add ScrapeCreators" not in result.stdout
    _assert_mode(env_file, "600")


def _check_project_env_walk_stops_at_git_root(bash_path: str, tmp_path: Path):
    """An env above the git root must not be discovered (matches lib/env.py)."""
    outside = tmp_path / ".claude" / "last30days.env"
    outside.parent.mkdir(parents=True)
    outside.write_text(
        "SETUP_COMPLETE=true\nSCRAPECREATORS_API_KEY=outside-repo\n",
        encoding="utf-8",
    )
    outside.chmod(0o644)

    repo = tmp_path / "repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()

    result = _run_hook(
        bash_path,
        nested,
        tmp_path,
        {
            "LAST30DAYS_TRUST_PROJECT_CONFIG": "1",
            "LAST30DAYS_CONFIG_DIR": str(tmp_path / "empty-config"),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert result.returncode == 0, result.stderr
    _assert_mode(outside, "644")
    # Outside key must not suppress the ScrapeCreators tip / must not count as configured.
    if "sources active" in result.stdout:
        assert "Tip: Add ScrapeCreators" in result.stdout
    else:
        assert "Ready to use" in result.stdout


def _check_malicious_key_in_global_env_also_blocked(bash_path: str, tmp_path: Path):
    """Identifier gate applies to the global file too (defense in depth)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"SETUP_COMPLETE=1\n{POC_LINE}", encoding="utf-8")
    work = tmp_path / "workdir"
    work.mkdir()
    proof = work / "RCE-PROOF.txt"

    result = _run_hook(
        bash_path,
        work,
        tmp_path,
        {
            "LAST30DAYS_CONFIG_DIR": str(config_dir),
            "LAST30DAYS_MEMORY_DIR": str(tmp_path / "memory"),
        },
    )

    assert not proof.exists(), f"RCE proof created from global env under {bash_path}"
    assert result.returncode == 0, result.stderr
    assert "Ready" in result.stdout


def _check_each_bash(
    check: Callable[[str, Path], None],
    bash_binaries: tuple[str, ...],
    tmp_path: Path,
) -> None:
    for index, bash_path in enumerate(bash_binaries):
        bash_tmp_path = tmp_path if index == 0 else tmp_path / f"bash-{index}"
        bash_tmp_path.mkdir(exist_ok=True)
        check(bash_path, bash_tmp_path)


def test_malicious_project_env_key_does_not_execute(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(
        _check_malicious_project_env_key_does_not_execute, bash_binaries, tmp_path
    )


def test_malicious_key_blocked_even_when_project_trusted(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(
        _check_malicious_key_blocked_even_when_project_trusted, bash_binaries, tmp_path
    )


def test_untrusted_project_env_is_ignored(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(_check_untrusted_project_env_is_ignored, bash_binaries, tmp_path)


def test_trusted_project_env_loads_normal_keys(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(_check_trusted_project_env_loads_normal_keys, bash_binaries, tmp_path)


def test_global_trust_signal_unlocks_project_env(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(_check_global_trust_signal_unlocks_project_env, bash_binaries, tmp_path)


def test_process_deny_overrides_global_trust(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(_check_process_deny_overrides_global_trust, bash_binaries, tmp_path)


def test_trusted_project_env_discovered_from_nested_cwd(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(
        _check_trusted_project_env_discovered_from_nested_cwd, bash_binaries, tmp_path
    )


def test_project_env_walk_stops_at_git_root(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(_check_project_env_walk_stops_at_git_root, bash_binaries, tmp_path)


def test_malicious_key_in_global_env_also_blocked(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    _check_each_bash(
        _check_malicious_key_in_global_env_also_blocked, bash_binaries, tmp_path
    )


def test_rce_path_exercised_on_modern_bash(
    bash_binaries: tuple[str, ...], tmp_path: Path
):
    """Sanity: at least one bash>=4 is under test so the PoC path is real, not vacuous."""
    modern = [b for b in bash_binaries if _bash_major(b) >= 4]
    if not modern:
        pytest.skip("needs bash 4+ to exercise printf -v RCE")
    assert modern, "expected a bash 4+ binary from _bash_binaries()"
    _check_malicious_key_blocked_even_when_project_trusted(modern[0], tmp_path)
