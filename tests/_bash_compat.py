"""Shared bash discovery for every test that shells out to bash.

Why this exists (2026-09-01): four test files each did their own
`shutil.which("bash")` with a naive `is None` skip. On the owner's Windows
machine that resolves to the WSL launcher stub (System32\\bash.exe), which
answers a trivial `bash -c "echo ok"` (a distro is installed) but eats the
backslashes in a native script path at test time ("C:Users..." -> exit 127),
and does so flakily. #38 fixed one file; the other three kept the bug because
the logic was hand-copied, not shared. One implementation, imported by all,
is the fix for that too.

The probe exercises the capability the tests actually need: running a script
FILE addressed by a native path, with UTF-8 decoding pinned (the console is
cp950 on this machine; text=True alone corrupts multibyte output).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

NO_USABLE_BASH = "no usable bash on this machine — bash-path tests run in CI"
BASH_PROBE_TIMEOUT_SECONDS = 3


def bash_candidates() -> list[str]:
    """Possible bash executables, WITHOUT starting subprocesses (collection-safe)."""
    windows_git_bash = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Git"
        / "bin"
        / "bash.exe"
        if os.name == "nt"
        else None
    )
    seen: list[str] = []
    for candidate in (
        "/opt/homebrew/bin/bash",
        "/usr/local/bin/bash",
        windows_git_bash,
        shutil.which("bash"),
    ):
        if not candidate:
            continue
        path = str(Path(candidate).resolve())
        if path not in seen and Path(path).is_file():
            seen.append(path)
    return seen


def bash_is_usable(bash_path: str) -> bool:
    """True only if this bash can run a script file given by native path."""
    try:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "probe.sh"
            script.write_text("echo LAST30DAYS_BASH_PROBE_OK\n", encoding="utf-8")
            result = subprocess.run(
                [bash_path, str(script)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=BASH_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "LAST30DAYS_BASH_PROBE_OK" in (
        result.stdout or ""
    )


@lru_cache(maxsize=1)
def usable_bash_binaries() -> tuple[str, ...]:
    """All usable bashes, probed once per session (lazy — never at import)."""
    return tuple(c for c in bash_candidates() if bash_is_usable(c))


def first_usable_bash() -> str | None:
    binaries = usable_bash_binaries()
    return binaries[0] if binaries else None


# ── PATH the hook tests need ────────────────────────────────────────────────
# check-config.sh guards its last-run block with `command -v python3`. On this
# owner's Windows machine that resolves to the App Execution Alias stub at
# %LOCALAPPDATA%\Microsoft\WindowsApps\python3 — a launcher that prints
# nothing and exits 0 when Python is not installed from the Store. The guard
# passes, the interpreter does nothing, LAST_RUN_LINE comes out empty, and the
# hook's "Last run:" line silently disappears. Verified directly:
#
#     $ python3 -c "print(42)"      # via that stub
#     $                             # no output, rc=0
#
# So tests that shell out to the hook must put a python3 that actually runs on
# PATH. A shim script works everywhere and needs no privileges — unlike a
# symlink, which raises WinError 1314 without Developer Mode.


@lru_cache(maxsize=1)
def python3_shim_dir() -> str:
    """A directory whose ``python3`` really is this interpreter.

    Created once per session in a temp dir that outlives the caller (pytest's
    tmp_path is per-test; this is shared), and left for the OS to reap.
    """
    directory = Path(tempfile.mkdtemp(prefix="last30days-python3-shim-"))
    shim = directory / "python3"
    # Forward slashes: Git Bash cannot exec a backslash path.
    shim.write_text(
        '#!/bin/sh\nexec "{}" "$@"\n'.format(sys.executable.replace("\\", "/")),
        encoding="utf-8",
        newline="\n",
    )
    shim.chmod(0o755)
    return str(directory)


def _dir_provides(directory: str, tool: str) -> bool:
    """Would a shell find ``tool`` in this one directory?

    Not ``shutil.which``: on Windows it only accepts names matching PATHEXT, so
    it cannot see an extension-less executable that bash's ``command -v`` finds
    perfectly well — and ``command -v`` is what the hook actually uses.
    """
    base = Path(directory)
    return any((base / (tool + ext)).exists() for ext in ("", ".exe", ".cmd", ".bat", ".py"))


def path_without(tool: str, *, base: str | None = None) -> str:
    """The given PATH minus every directory that provides ``tool``."""
    entries = [e for e in (base if base is not None else os.environ.get("PATH", "")).split(os.pathsep) if e]
    return os.pathsep.join(e for e in entries if not _dir_provides(e, tool))


def hook_path(*prefix: str, base: str | None = None) -> str:
    """PATH for running check-config.sh: prefixes first, then a real python3."""
    entries = [*prefix, python3_shim_dir()]
    tail = base if base is not None else os.environ.get("PATH", "")
    if tail:
        entries.append(tail)
    return os.pathsep.join(entries)
