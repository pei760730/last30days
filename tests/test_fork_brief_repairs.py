"""Offline regressions for #60's reviewed trust and information-loss defects."""

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib import cluster, render, schema
from _fork_brief_scenarios import Spec, build_report
from test_fork_brief_to_message import btm


def raw(report):
    return json.dumps(schema.to_dict(report))


def report(*specs):
    return build_report("Palantir", specs)


def test_real_upstream_thin_evidence_is_never_hidden():
    evidence = report(Spec("Palantir renewal remains uncertain", sources=("reddit", "hackernews"), score=30))
    evidence.clusters[0].uncertainty = cluster._cluster_uncertainty(evidence.ranked_candidates)
    assert evidence.clusters[0].uncertainty == "thin-evidence"
    assert "[thin evidence]" in render.render_brief(evidence)
    for text in (raw(evidence), render.render_brief(evidence)):
        message = btm.shape(text, "Palantir").message
        assert "證據薄弱" in message
        assert "多來源交叉" not in message


def test_contradictory_similar_titles_are_not_corroboration():
    evidence = report(
        Spec("Palantir wins $10B Army contract", uncertainty="single-source"),
        Spec("Palantir loses $10B Army contract", sources=("hackernews",), uncertainty="single-source"),
    )
    shaped = btm.shape(raw(evidence), "Palantir")
    assert shaped.items == 1 and shaped.near_duplicates == 1
    assert "另見 Hacker News" in shaped.message
    assert "單一來源,待證" in shaped.message
    assert "多來源交叉" not in shaped.message
    assert "標題相近、已併入" in shaped.message
    # Even multiple platforms in a cluster cannot override upstream uncertainty.
    evidence.clusters[0].sources.append("hackernews")
    assert "單一來源,待證" in btm.shape(raw(evidence), "Palantir").message


@pytest.mark.parametrize("damage", ["provider_runtime", "representative", "source_items", "root", "clusters", "topic", "null"])
def test_report_shape_errors_are_explicit_and_leave_seen_untouched(damage, tmp_path, capsys):
    payload = schema.to_dict(report(Spec("Palantir opens a Tokyo office")))
    if damage == "provider_runtime":
        del payload[damage]
    elif damage == "representative":
        payload["clusters"][0]["representative_ids"] = ["not-a-member"]
    elif damage == "source_items":
        payload["ranked_candidates"][0]["source_items"] = ["broken"]
    elif damage == "topic":
        payload["topic"] = 42
    elif damage == "null":
        payload = None
    elif damage == "clusters":
        del payload[damage]
    else:
        payload = []
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    state = tmp_path / "seen.json"
    original = b'{"2020-01-01": [["keep", "bytes"]]}\n'
    state.write_bytes(original)
    assert btm.main(["brief_to_message.py", str(path), "Palantir", "--seen", str(state)]) == 0
    output = capsys.readouterr()
    assert "Report 結構解析失敗" in output.out
    assert "::error" in output.err and "解析" in output.err
    assert "來源可能全部無回應" not in output.out
    assert "乾涸" not in output.err
    assert state.read_bytes() == original


@pytest.mark.parametrize("text", ["", '{"ranked_candidates": ['])
def test_empty_or_incomplete_input_is_not_a_valid_empty_report(text, tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(text, encoding="utf-8")
    state = tmp_path / "seen.json"
    btm.main(["brief_to_message.py", str(path), "Palantir", "--seen", str(state)])
    output = capsys.readouterr()
    assert "輸入空白或殘缺" in output.out
    assert "::warning" in output.err and "輸入" in output.err
    assert "來源可能全部無回應" not in output.out
    assert not state.exists()


def test_valid_empty_report_has_a_distinct_diagnostic(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(raw(report()), encoding="utf-8")
    btm.main(["brief_to_message.py", str(path), "Palantir"])
    output = capsys.readouterr()
    assert "觀察窗內沒有內容" in output.out
    assert "輸入空白或殘缺" not in output.out
    assert "解析失敗" not in output.out
    assert "::error" not in output.err


def test_relevance_floor_excludes_entity_miss_and_zero_clusters():
    evidence = report(
        Spec("Palantir irrelevant celebrity gossip"),
        Spec("Palantir zero score residue", score=0),
        Spec("Palantir expands Tokyo research laboratory"),
    )
    evidence.ranked_candidates[0].explanation = "entity-miss: unrelated entity"
    head, stories = btm.stories_from_report(evidence)
    assert [s.title for s in stories] == ["Palantir expands Tokyo research laboratory"]
    text_headings = [line for line in render.render_brief(evidence).splitlines() if line.startswith("### ")]
    assert len(text_headings) == 1 and stories[0].title in text_headings[0]
    shaped = btm.shape(raw(evidence), "Palantir")
    assert shaped.candidates == shaped.items == 1
    assert "gossip" not in shaped.message and "residue" not in shaped.message


@pytest.mark.parametrize("instant,day", [
    ("2026-10-08T23:00:00+00:00", "2026-10-09"),
    ("2026-10-09T15:59:59+00:00", "2026-10-09"),
    ("2026-10-09T16:00:00+00:00", "2026-10-10"),
    ("2026-12-31T23:00:00+00:00", "2027-01-01"),
])
def test_run_date_uses_taipei_not_range_or_generation_date(instant, day):
    evidence = report(Spec("Palantir opens Tokyo office", published="2026-10-08"))
    now = dt.datetime.fromisoformat(instant)
    for value in (raw(evidence), render.render_brief(evidence)):
        shaped = btm.shape(value, "Palantir", now=now)
        assert shaped.message.startswith(f"📰 Palantir · {day} 早報")
        assert "觀察窗 2026-09-08 → 2026-10-08 (UTC)" in shaped.message
    shaped = btm.shape_report(evidence, now=now)
    assert "發布 10-08" in shaped.message  # Date-only source field stays date-only.


@pytest.mark.parametrize("title,expected", [
    ("Palantir P/E < 100 but revenue > $1B", "Palantir P/E < 100 but revenue > $1B"),
    ("Palantir P/E &lt; 100 but revenue &gt; $1B &amp; growth", "Palantir P/E < 100 but revenue > $1B & growth"),
    ("Palantir <b>revenue</b> <!-- hidden --> exceeds $1B", "Palantir revenue exceeds $1B"),
    ('Read <a href="https://example.invalid">Palantir</a> at https://example.invalid/report', "Read Palantir at https://example.invalid/report"),
    ("Palantir x < y but z > 3; p < b but revenue > 4", "Palantir x < y but z > 3; p < b but revenue > 4"),
    ("Palantir submitted by analysts to r/stocks [link]", "Palantir submitted by analysts to r/stocks [link]"),
])
def test_title_cleaning_preserves_comparisons_text_and_urls(title, expected):
    evidence = report(Spec(title))
    for value in (raw(evidence), render.render_brief(evidence)):
        assert f"1. {expected}\n" in btm.shape(value, "Palantir").message


@pytest.mark.parametrize("has_prior", [False, True])
def test_all_candidates_omitted_for_budget_are_not_missing_sources(has_prior, tmp_path, capsys):
    specs = [Spec("b" * 4000)]
    prior = ()
    if has_prior:
        specs.insert(0, Spec("Palantir yesterday railway agreement"))
        prior = btm.shape(raw(report(specs[0])), "Palantir").shipped
    evidence = report(*specs)
    shaped = btm.shape(raw(evidence), "Palantir", prior)
    assert shaped.items == 0 and shaped.dropped_for_budget == 1
    assert "有資料，但篇幅放不下" in shaped.message
    assert "來源可能全部無回應" not in shaped.message
    assert "今天沒有新東西" not in shaped.message
    assert shaped.shipped == ()
    path = tmp_path / "report.json"
    path.write_text(raw(evidence), encoding="utf-8")
    state = tmp_path / "seen.json"
    btm.save_seen(str(state), prior)
    btm.main(["brief_to_message.py", str(path), "Palantir", "--seen", str(state)])
    output = capsys.readouterr()
    assert "截斷" in output.err
    assert btm.load_seen(str(state)) == prior


def test_date_link_and_quote_share_first_qualifying_representative():
    evidence = report(Spec("Palantir quarterly results", sources=("reddit", "hackernews")))
    first, second = evidence.ranked_candidates
    evidence.clusters[0].representative_ids = [first.candidate_id, second.candidate_id]
    first.snippet = first.source_items[0].snippet = "Alice reports a cautious forecast."
    first.source_items[0].published_at = "2026-10-01"
    first.source_items[0].date_confidence = "low"
    second.snippet = second.source_items[0].snippet = "Bob reports record sales."
    second.source_items[0].published_at = "2026-10-07"
    # A later secondary item inside the first candidate is not its primary evidence.
    first.source_items.append(replace(second.source_items[0], published_at="2026-10-08"))
    shaped = btm.shape(raw(evidence), "Palantir")
    assert "發布約 10-01" in shaped.message
    assert f"🔗 {first.source_items[0].url}" in shaped.message
    assert "Alice reports" in shaped.message and "Bob reports" not in shaped.message
    # Keep upstream fallback selection; missing primary fields are not borrowed.
    first.explanation = "entity-miss"
    evidence.clusters[0].representative_ids = [first.candidate_id]
    assert "Bob reports" in btm.shape(raw(evidence), "Palantir").message
    second.source_items[0].url = second.url = ""
    second.source_items[0].published_at = None
    shaped = btm.shape(raw(evidence), "Palantir")
    assert "發布日期不明" in shaped.message and "🔗" not in shaped.message


def test_nothing_solid_with_responsive_platform_is_not_source_failure(tmp_path, capsys):
    evidence = report(Spec("Palantir retrieval residue", score=0))
    assert "Nothing solid" in render.render_brief(evidence)
    for value in (raw(evidence), render.render_brief(evidence)):
        message = btm.shape(value, "Palantir").message
        assert "有回應的來源:Reddit" in message
        assert "有資料，但沒有符合選條條件的內容" in message
        assert "來源可能全部無回應" not in message
    path = tmp_path / "report.json"
    path.write_text(raw(evidence), encoding="utf-8")
    btm.main(["brief_to_message.py", str(path), "Palantir"])
    output = capsys.readouterr()
    assert "沒有符合選條條件" in output.err
    assert "沒抓到" not in output.err


_LEGACY_CACHE = json.loads((Path(__file__).parent / "fixtures/fork_brief_cache_v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("example", _LEGACY_CACHE["examples"], ids=lambda e: e["title"])
def test_corrected_display_keeps_exact_old_cache_identity(example, tmp_path):
    evidence = report(Spec(example["title"]))
    shaped = btm.shape(raw(evidence), "Palantir")
    expected = tuple(frozenset(entry) for entry in example["fingerprints"]["60"])
    assert shaped.shipped == expected
    state = tmp_path / "seen.json"
    for legacy in ("main", "58", "60"):
        state.write_text(json.dumps({btm._today(): example["fingerprints"][legacy]}), encoding="utf-8")
        repeated = btm.shape(raw(evidence), "Palantir", btm.load_seen(str(state)))
        assert repeated.items == 0 and repeated.repeats == 1
    state.unlink()
    btm.save_seen(str(state), shaped.shipped)
    assert json.loads(state.read_text(encoding="utf-8")) == {btm._today(): example["fingerprints"]["60"]}
    assert btm.shape(render.render_brief(evidence), "Palantir", btm.load_seen(str(state))).items == 0
