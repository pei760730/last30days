"""fork 專屬測試:明確指定的來源白名單必須贏過「自動加 jobs」的啟發式。

上游沒有這支。檔名帶 `fork_` 前綴,跟上游測試一眼分得開。

背景:daily-brief 靠 research-topics.txt 的每題 `--search` 擋掉會污染該題的
來源。2026-08-25 實測 `Palantir PLTR --search reddit,hackernews,polymarket`
仍然回 308 筆職缺 —— pipeline 收斂完 available 之後,又因為「這題看起來像
公司」把 jobs 無條件加回來。職缺條目共用同一段公司樣板文案,會把真訊號擠出
前三名。修在 pipeline.py,這裡把行為鎖住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "last30days" / "scripts"))

from lib import pipeline  # noqa: E402

CONFIG = {"LAST30DAYS_REASONING_PROVIDER": "gemini"}

# 公司形狀的主題(<=4 字、無問號、無通用詞)才會觸發自動加 jobs
COMPANY_TOPIC = "Palantir PLTR"


def _run(**kwargs):
    return pipeline.run(topic=COMPANY_TOPIC, config=CONFIG, depth="quick", mock=True, **kwargs)


def test_company_topic_auto_adds_jobs_when_no_sources_pinned():
    """沒指定來源時,原本的啟發式行為要保留 —— 這不是要關掉的功能。"""
    assert pipeline._company_topic_likely(COMPANY_TOPIC)
    report = _run()
    assert "jobs" in report.items_by_source


def test_explicit_search_excludes_jobs_on_company_topic():
    """有指定白名單時,白名單說了算。這就是原本壞掉的那條。"""
    report = _run(requested_sources=["reddit"])
    assert "jobs" not in report.items_by_source


def test_hiring_signals_mode_still_forces_jobs():
    """--hiring-signals 是使用者明講要看徵才訊號,不受白名單收斂影響。"""
    report = _run(requested_sources=["reddit"], hiring_signals_mode=True)
    assert "jobs" in report.items_by_source


@pytest.mark.parametrize("topic", ["how to value rare earth miners", "What is Palantir?"])
def test_non_company_topics_never_auto_add_jobs(topic):
    """啟發式本來就不該對問句/通用詞開火,順帶確認沒被我改壞。"""
    assert not pipeline._company_topic_likely(topic)
