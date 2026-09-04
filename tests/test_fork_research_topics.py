"""Fork-local guard: a typo in research-topics.txt must not silently kill a topic.

`daily-brief.yml` runs the CLI as `python "$CLI" "$t" --emit brief $flags || true`.
An unknown `--search` source makes `parse_search_flag` raise `SystemExit`, the
`|| true` swallows it, `brief.txt` comes out empty, and the topic ships as
"⚠️ 這次沒抓到內容(來源可能全部無回應)" — every morning, looking exactly like a
quiet upstream outage. Nothing else in CI reads this file, so a one-character
mistake could run for weeks.

These tests parse the file the same way the workflow does and validate it against
the CLI's own source table, so a bad line fails here instead of going dark.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOPICS_FILE = REPO_ROOT / "research-topics.txt"

sys.path.insert(0, str(REPO_ROOT / "skills/last30days/scripts"))

from lib import pipeline, registers  # noqa: E402

# daily-brief.yml only ever runs the first three (`head -3`).
LIVE_TOPICS = 3


def _topic_lines() -> list[str]:
    lines = []
    for raw in TOPICS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _split(line: str) -> tuple[str, list[str]]:
    """Same split the workflow does: first ' --' separates topic from flags."""
    marker = " --"
    if marker in line:
        head, _, tail = line.partition(marker)
        return head.strip(), ("--" + tail).split()
    return line.strip(), []


def test_file_has_at_least_one_topic() -> None:
    assert _topic_lines(), "research-topics.txt has no runnable topic lines"


@pytest.mark.parametrize("line", _topic_lines(), ids=lambda line: line[:40])
def test_topic_text_survives_the_flag_split(line: str) -> None:
    topic, _ = _split(line)
    assert topic, f"line reduces to an empty topic once flags are split off: {line!r}"


@pytest.mark.parametrize("line", _topic_lines(), ids=lambda line: line[:40])
def test_flags_alternate_flag_then_value(line: str) -> None:
    """Catch a stray ' --' in the topic text.

    The workflow splits on the *first* ' --', so anything after it is handed to
    the CLI as arguments. Tokens must therefore read as `--flag value --flag
    value`: a bare word may only appear right after a `--flag`.
    """
    _, flags = _split(line)
    if not flags:
        return
    assert flags[0].startswith("--"), f"{line!r}: flags do not start with a flag"
    for previous, token in zip(flags, flags[1:]):
        if token.startswith("--"):
            continue
        assert previous.startswith("--"), (
            f"{line!r}: {token!r} follows {previous!r}, which is not a flag — "
            "a stray ' --' in the topic text turns the rest of the line into "
            "CLI arguments."
        )


@pytest.mark.parametrize("line", _topic_lines(), ids=lambda line: line[:40])
def test_search_sources_are_real(line: str) -> None:
    """The failure this file exists for: an unknown source name."""
    _, flags = _split(line)
    for index, flag in enumerate(flags):
        if flag == "--search":
            value = flags[index + 1] if index + 1 < len(flags) else ""
        elif flag.startswith("--search="):
            value = flag.partition("=")[2]
        else:
            continue

        assert value, f"{line!r}: --search with no source list"
        for name in value.split(","):
            name = name.strip().lower()
            assert name, f"{line!r}: empty entry in --search (a stray comma?)"
            resolved = pipeline.SEARCH_ALIAS.get(name, name)
            assert resolved in pipeline.MOCK_AVAILABLE_SOURCES, (
                f"{line!r}: unknown --search source {name!r}. The CLI raises "
                "SystemExit on it, daily-brief.yml swallows that with `|| true`, "
                "and the topic ships empty every morning."
            )


@pytest.mark.parametrize("line", _topic_lines(), ids=lambda line: line[:40])
def test_register_presets_are_real(line: str) -> None:
    """An unknown --register dies the same silent death as an unknown source.

    argparse rejects it with exit 2, `|| true` swallows that, and the topic
    ships empty every morning looking like a source outage.
    """
    _, flags = _split(line)
    for index, flag in enumerate(flags):
        if flag == "--register":
            value = flags[index + 1] if index + 1 < len(flags) else ""
        elif flag.startswith("--register="):
            value = flag.partition("=")[2]
        else:
            continue
        assert value in registers.REGISTER_NAMES, (
            f"{line!r}: unknown --register {value!r}; "
            f"valid presets are {list(registers.REGISTER_NAMES)}"
        )


def test_the_topics_that_actually_run_are_all_scoped() -> None:
    """Every live topic needs an explicit source whitelist.

    Without one the pipeline auto-adds sources — GitHub and Jobs show up in the
    brief header — and both have polluted a topic here before: SEO-spam repos
    scored into the top three on the fashion topic (#33), and job postings are
    noise for a "what happened in the last 30 days" read.
    """
    unscoped = [
        line for line in _topic_lines()[:LIVE_TOPICS] if "--search" not in _split(line)[1]
    ]
    assert not unscoped, (
        f"topics running without a --search whitelist: {unscoped}. "
        "Add one, or move the topic below the first three if it is parked."
    )
