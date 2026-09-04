"""Fork-local guard for CI settings this fork fixed and upstream does not have.

The fork syncs from upstream by squash-merging a big diff. Every fork-local edit
to an upstream-owned file is therefore one careless resolution away from being
silently reverted — and each of the settings below was added to fix something
that had already gone wrong once:

* `timeout-minutes` — GitHub's default is 360. One hung job burns six hours of
  Actions quota before it is killed (#28).
* the OSV caller's `actions: read` — the reusable workflow declares it; granting
  less turns the weekly scan into a `startup_failure` that nobody sees (#12).
* dependabot's three ecosystems — an earlier revision of that file replaced the
  whole thing with only the github-actions block, which would have quietly
  retired uv and gomod updates (#21).

These are cheap invariants. They exist so a bad sync fails loudly in CI instead
of costing quota or going dark for weeks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert files, f"no workflows found under {WORKFLOW_DIR}"
    return files


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda p: p.name)
def test_every_job_bounds_its_runtime(workflow: Path) -> None:
    """No job may ride GitHub's 360-minute default."""
    jobs = (_load(workflow) or {}).get("jobs") or {}
    missing = []
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        # A job that calls a reusable workflow cannot set timeout-minutes —
        # GitHub rejects the key there. The callee owns its own bound.
        if "uses" in job:
            continue
        if "timeout-minutes" not in job:
            missing.append(name)
    assert not missing, (
        f"{workflow.name}: jobs {missing} have no timeout-minutes, so they inherit "
        "GitHub's 360-minute default — one hang burns six hours of quota."
    )


def test_osv_caller_keeps_actions_read() -> None:
    """Dropping this permission makes the weekly scan fail at startup, silently."""
    job = _load(WORKFLOW_DIR / "osv-scanner.yml")["jobs"]["scan-scheduled"]
    permissions = job.get("permissions") or {}
    assert permissions.get("actions") == "read", (
        "osv-scanner.yml scan-scheduled needs `actions: read` — the reusable "
        "workflow declares it, and granting less causes startup_failure (#12)."
    )


def test_dependabot_keeps_all_three_ecosystems_on_the_biweekly_cron() -> None:
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    entries = {
        (update["package-ecosystem"], update.get("directory")): update
        for update in config["updates"]
    }
    expected = {("github-actions", "/"), ("uv", "/"), ("gomod", "/mcp")}
    assert expected <= set(entries), (
        f"dependabot.yml lost ecosystems: {sorted(expected - set(entries))}. "
        "Dropping one retires those updates without any visible failure (#21)."
    )
    for key in expected:
        schedule = entries[key]["schedule"]
        assert schedule.get("cronjob") == "0 9 1,15 * *", (
            f"{key} left the fork's 1st-and-15th schedule: {schedule}"
        )


def test_daily_brief_still_runs_at_0700_taipei() -> None:
    """The fork's only product. 23:00 UTC is 07:00 Asia/Taipei."""
    workflow = _load(WORKFLOW_DIR / "daily-brief.yml")
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = workflow.get("on") or workflow.get(True)
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert crons == ["0 23 * * *"], f"daily-brief schedule changed: {crons}"


def test_seen_state_is_saved_only_after_a_successful_push() -> None:
    """The dedupe memory must not advance when the Telegram push failed.

    Recording a storyline as "sent" on a run whose push failed means tomorrow
    suppresses it — the owner never sees that content at all. So the save step
    has to sit AFTER the push and carry no `if:` (bare `success()`); an
    `always()` here would quietly trade a retry for permanent loss.
    """
    steps = _load(WORKFLOW_DIR / "daily-brief.yml")["jobs"]["brief"]["steps"]
    names = [step.get("name", "") for step in steps]
    push = names.index("Push brief to Telegram (kai-notify, fail-soft)")
    save = names.index("Save seen storylines")

    assert save > push, "the seen-state save must run after the push, not before"
    assert "if" not in steps[save], (
        "the save step must keep the default success() condition — an always() "
        "would record storylines as sent on a run that never delivered them."
    )
