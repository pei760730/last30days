"""Tests for hooks/scripts/check-config.sh yt-dlp detection on the new-user path.

Covers issue #394 — new users with yt-dlp installed were never told YouTube was
available, because the capability-detection block ran AFTER the new-user early
exit. The SessionStart hook should detect yt-dlp on PATH and mention it in the
welcome message even when no config exists.

Cases:
  - new user + yt-dlp on PATH -> welcome says YouTube works out of the box
  - new user + no yt-dlp on PATH -> welcome unchanged (wizard can unlock YouTube)
  - existing user + yt-dlp -> numeric source count is higher than without
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from _bash_compat import NO_USABLE_BASH, first_usable_bash, hook_path, path_without

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "check-config.sh"


def _run_hook(env_overrides: dict[str, str], path_override: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for k in (
        "LAST30DAYS_MEMORY_DIR",
        "SETUP_COMPLETE",
        "LAST30DAYS_CONFIG_DIR",
        "OPENAI_API_KEY",
        "SCRAPECREATORS_API_KEY",
        "AUTH_TOKEN",
        "XAI_API_KEY",
        "CT0",
        "BSKY_HANDLE",
        "BSKY_APP_PASSWORD",
        "EXA_API_KEY",
    ):
        env.pop(k, None)
    env.update(env_overrides)
    if path_override is not None:
        env["PATH"] = path_override
    bash_path = first_usable_bash()
    if bash_path is None:
        pytest.skip(NO_USABLE_BASH)
    return subprocess.run(
        [bash_path, str(HOOK)],
        capture_output=True,
        text=True,
        # cp950 console would corrupt the hook's UTF-8 output without this.
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )


HOOK_TOOLS = ("cat", "id", "mkdir", "sed", "stat", "tr", "uname")


def _resolves(name: str, path: str) -> bool:
    """Does a shell find ``name`` on this PATH?

    The hook decides with ``command -v``, so the test must ask the same way.
    ``shutil.which`` is not equivalent on Windows: it only accepts names
    matching PATHEXT, so an extension-less fake yt-dlp — exactly what these
    tests plant — is invisible to it while bash resolves it fine.
    """
    bash_path = first_usable_bash()
    if bash_path is None:
        pytest.skip(NO_USABLE_BASH)
    env = os.environ.copy()
    env["PATH"] = path
    probe = subprocess.run(
        [bash_path, "-c", f"command -v {name}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )
    return probe.returncode == 0 and bool(probe.stdout.strip())


def _tool_path_without_ytdlp(tmp_path: Path) -> str:
    """A PATH that keeps the hook's dependencies but drops yt-dlp.

    Previously this built a sandbox directory of symlinks to the real tools.
    That cannot work on Windows twice over: ``os.symlink`` needs
    SeCreateSymbolicLinkPrivilege (WinError 1314 without Developer Mode), and
    copying is no substitute because the Git-for-Windows binaries load
    ``msys-2.0.dll`` from their own directory.

    Removing the directories that provide yt-dlp keeps every other tool exactly
    where it already works, on every platform. ``python3`` is re-added through
    the shared shim — see ``_bash_compat.python3_shim_dir``.
    """
    path = hook_path(base=path_without("yt-dlp"))
    if _resolves("yt-dlp", path):
        pytest.skip("yt-dlp still resolves after filtering it off PATH")
    for name in HOOK_TOOLS:
        if not _resolves(name, path):
            pytest.skip(f"{name} not on PATH")
    return path


def _write_fake_last_run(tmp_path: Path) -> str:
    """Write a minimal last-run.json and return the LAST30DAYS_CONFIG_DIR the hook
    should be pointed at.

    The hook reads ``$LAST30DAYS_CONFIG_DIR/last-run.json`` and runs a python3
    subshell on it. Without this file, the hook's last-run line stays empty
    and a pre-existing bug (#440) makes the script exit 1 even on success.
    We don't want our regression test to depend on that bug, so we always
    provide a well-formed last-run.json.
    """
    cfg_dir = tmp_path / "last30days_cfg"
    cfg_dir.mkdir()
    (cfg_dir / "last-run.json").write_text(
        json.dumps(
            {
                "topic": "test topic",
                "timestamp": "2026-06-01T00:00:00Z",
                "total": 0,
            }
        )
    )
    return str(cfg_dir)


def _parse_source_count(stdout: str) -> int:
    """Extract the source count from the 'Ready — N sources active.' line.

    The script emits the count in both the fully-configured and the
    'setup-done but missing ScrapeCreators' branches.
    """
    match = re.search(r"Ready\s+[—–-]\s+(\d+)\s+sources?\s+active", stdout)
    assert match, f"could not find source count in hook stdout: {stdout!r}"
    return int(match.group(1))


def test_new_user_with_ytdlp_says_youtube_works(tmp_path: Path):
    """A new user with yt-dlp on PATH should see YouTube flagged as already-working."""
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    (fake_bin / "yt-dlp").touch()
    (fake_bin / "yt-dlp").chmod(0o755)

    # PATH must contain bash (so the hook can run) AND the fake yt-dlp dir.
    # Putting fake_bin FIRST means any real yt-dlp elsewhere is shadowed.
    path = hook_path(str(fake_bin), base=path_without("yt-dlp"))
    assert _resolves("yt-dlp", path), (
        "test pre-condition: fake yt-dlp should resolve on the override PATH"
    )

    cfg_dir = _write_fake_last_run(tmp_path)
    result = _run_hook({"LAST30DAYS_CONFIG_DIR": cfg_dir}, path_override=path)

    assert result.returncode == 0, f"hook failed: stderr={result.stderr!r}"
    # The welcome message is now consistent: it explicitly says YouTube is
    # working via yt-dlp, AND drops "YouTube" from the wizard-unlock line so
    # the two don't contradict each other (see #394 follow-up).
    assert "Detected: yt-dlp" in result.stdout, (
        f"expected yt-dlp detection line, got: {result.stdout!r}"
    )
    assert "YouTube (yt-dlp detected) work out of the box" in result.stdout, (
        f"expected yt-dlp-aware YouTube line, got: {result.stdout!r}"
    )
    # The wizard line should NOT advertise YouTube as something the wizard unlocks,
    # because yt-dlp is already providing it.
    assert "wizard can unlock X/Twitter, YouTube, and more" not in result.stdout, (
        f"welcome should not claim wizard unlocks YouTube when yt-dlp is present: {result.stdout!r}"
    )


def test_new_user_without_ytdlp_unchanged_welcome(tmp_path: Path):
    """A new user without yt-dlp should see the original wizard-unlock copy."""
    path = _tool_path_without_ytdlp(tmp_path)
    assert not _resolves("yt-dlp", path), (
        "test pre-condition: yt-dlp should not resolve on the minimal PATH"
    )

    cfg_dir = _write_fake_last_run(tmp_path)
    result = _run_hook({"LAST30DAYS_CONFIG_DIR": cfg_dir}, path_override=path)

    assert result.returncode == 0, f"hook failed: stderr={result.stderr!r}"
    # No detection line, no yt-dlp-aware copy, original wizard line preserved.
    assert "Detected: yt-dlp" not in result.stdout
    assert "yt-dlp detected" not in result.stdout
    assert "wizard can unlock X/Twitter, YouTube, and more" in result.stdout, (
        f"expected unchanged wizard line, got: {result.stdout!r}"
    )


def test_setup_done_user_source_count_includes_ytdlp(tmp_path: Path):
    """Regression: the setup-done path must count YouTube when yt-dlp is on PATH.

    Runs the hook twice — once with yt-dlp and once without — and asserts the
    numeric source count is exactly 1 higher with yt-dlp present. This catches
    a real regression where HAS_YTDLP gets zeroed out before the counting block.
    """
    cfg_dir = _write_fake_last_run(tmp_path)
    base_env = {
        "SETUP_COMPLETE": "true",
        "SCRAPECREATORS_API_KEY": "sc_test",
        "LAST30DAYS_CONFIG_DIR": cfg_dir,
    }

    # 1) Run WITH yt-dlp
    fake_bin = tmp_path / "fake_bin_with"
    fake_bin.mkdir()
    (fake_bin / "yt-dlp").touch()
    (fake_bin / "yt-dlp").chmod(0o755)
    path_with = hook_path(str(fake_bin), base=path_without("yt-dlp"))
    assert _resolves("yt-dlp", path_with)

    with_yt = _run_hook(base_env, path_override=path_with)
    assert with_yt.returncode == 0, f"hook failed: stderr={with_yt.stderr!r}"
    count_with = _parse_source_count(with_yt.stdout)

    # 2) Run WITHOUT yt-dlp (minimal PATH)
    path_without_ytdlp = _tool_path_without_ytdlp(tmp_path)
    assert not _resolves("yt-dlp", path_without_ytdlp)

    without_yt = _run_hook(base_env, path_override=path_without_ytdlp)
    assert without_yt.returncode == 0, f"hook failed: stderr={without_yt.stderr!r}"
    count_without = _parse_source_count(without_yt.stdout)

    # YouTube adds exactly one source to the count.
    assert count_with == count_without + 1, (
        f"expected YouTube to add exactly 1 source; got "
        f"{count_with} (with yt-dlp) vs {count_without} (without). "
        f"Stdout with:    {with_yt.stdout!r}\n"
        f"Stdout without: {without_yt.stdout!r}"
    )


def test_keychain_credentials_avoid_new_user_welcome(tmp_path: Path):
    """macOS Keychain credentials should count as configured for the status hook.

    ``uname`` and ``security`` are shadowed with bash functions via ``BASH_ENV``
    rather than with fake binaries on PATH. Git Bash prepends its own
    ``/mingw64/bin:/usr/bin`` to whatever PATH it is handed, so ``/usr/bin/uname``
    wins on Windows no matter how the test orders the directories — verified with
    ``type -a uname``, which lists the real one first and the planted one second.
    A function is resolved before any file on every platform, so this runs the
    same way everywhere.
    """
    shadow = tmp_path / "shadow_darwin.sh"
    shadow.write_text(
        """uname() { echo Darwin; }
security() {
  service=""
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "-s" ]; then
      service="$2"
      shift 2
    else
      shift
    fi
  done
  case "$service" in
    last30days-XAI_API_KEY|last30days-SCRAPECREATORS_API_KEY) return 0 ;;
    *) return 44 ;;
  esac
}
""",
        encoding="utf-8",
        # LF: a CRLF shebang/body makes bash exec "/bin/sh\r".
        newline="\n",
    )

    cfg_dir = _write_fake_last_run(tmp_path)
    result = _run_hook(
        {
            "HOME": str(tmp_path),
            "LAST30DAYS_CONFIG_DIR": cfg_dir,
            # bash sources BASH_ENV for non-interactive shells, which is exactly
            # what `bash check-config.sh` is.
            "BASH_ENV": str(shadow).replace("\\", "/"),
        },
        path_override=_tool_path_without_ytdlp(tmp_path),
    )

    assert result.returncode == 0, f"hook failed: stderr={result.stderr!r}"
    assert "Ready to use. Run /last30days" not in result.stdout
    assert "Ready" in result.stdout
    assert "sources active" in result.stdout
    assert "Tip: Add ScrapeCreators" not in result.stdout
