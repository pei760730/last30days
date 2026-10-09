"""fork 專屬:daily-brief 訊息成型層的離線情境資料。

⚠️ 全部是 **合成資料(SYNTHETIC)**:事件、標題、價格、日期、互動數都是編的,
只用來測排版與邊界;不是任何一天的真實情報,也不要拿來當今日的早報內容。
網址一律 `example.invalid`(RFC 2606 保留網域),永遠連不到真站。

每個情境用 ``Spec``(一條 storyline)組出一份上游 ``schema.Report``,跟
``--emit json --json-profile raw`` 給 formatter 的是同一個型別 —— 測的是真實入口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lib import schema

SYNTHETIC_NOTICE = (
    "SYNTHETIC TEST DATA — events, titles, prices, dates and engagement are invented; "
    "not real intelligence for any day."
)

RANGE_FROM = "2026-09-08"
RANGE_TO = "2026-10-08"


@dataclass(frozen=True)
class Spec:
    """一條 storyline 的原料。``sources`` 的第一個是代表候選(帶引文與互動數)。"""

    title: str
    snippet: str = ""
    sources: tuple[str, ...] = ("reddit",)
    published: str | None = "2026-10-05"
    date_confidence: str = "high"
    engagement: dict[str, int | float] | None = None
    score: float = 50.0
    uncertainty: str | None = None
    url: str | None = None  # None = 自動產 example.invalid;"" = 來源沒給連結


@dataclass(frozen=True)
class Scenario:
    name: str
    topic: str
    specs: tuple[Spec, ...]
    note: str = ""
    prior_titles: tuple[str, ...] = ()  # 「近 7 天送過」的標題(會轉成指紋)
    range_from: str = RANGE_FROM
    range_to: str = RANGE_TO
    group: str = ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "story"


def build_report(
    topic: str,
    specs: tuple[Spec, ...],
    range_from: str = RANGE_FROM,
    range_to: str = RANGE_TO,
) -> schema.Report:
    clusters: list[schema.Cluster] = []
    candidates: list[schema.Candidate] = []
    items_by_source: dict[str, list[schema.SourceItem]] = {}
    for index, spec in enumerate(specs, start=1):
        slug = f"{index}-{_slug(spec.title)}"
        cluster_ids: list[str] = []
        for position, source in enumerate(spec.sources):
            candidate_id = f"{slug}-{source}"
            url = f"https://example.invalid/{source}/{slug}" if spec.url is None else spec.url
            item = schema.SourceItem(
                item_id=candidate_id,
                source=source,
                title=spec.title,
                body=spec.snippet if position == 0 else "",
                url=url,
                container="synthetic" if source == "reddit" else None,
                published_at=spec.published,
                date_confidence=spec.date_confidence,  # type: ignore[arg-type]
                engagement=dict(spec.engagement or {}) if position == 0 else {},
                snippet=spec.snippet if position == 0 else "",
            )
            items_by_source.setdefault(source, []).append(item)
            candidates.append(
                schema.Candidate(
                    candidate_id=candidate_id,
                    item_id=candidate_id,
                    source=source,
                    title=spec.title,
                    url=url,
                    snippet=spec.snippet if position == 0 else "",
                    subquery_labels=[],
                    native_ranks={},
                    local_relevance=1.0,
                    freshness=100,
                    engagement=None,
                    source_quality=1.0,
                    rrf_score=1.0,
                    sources=[source],
                    source_items=[item],
                    final_score=spec.score,
                    cluster_id=f"cluster-{index}",
                    metadata={"range_from": range_from, "range_to": range_to, "synthetic": True},
                )
            )
            cluster_ids.append(candidate_id)
        clusters.append(
            schema.Cluster(
                cluster_id=f"cluster-{index}",
                title=spec.title,
                candidate_ids=cluster_ids,
                representative_ids=cluster_ids[:1],
                sources=list(spec.sources),
                score=spec.score,
                uncertainty=spec.uncertainty,  # type: ignore[arg-type]
            )
        )
    return schema.Report(
        topic=topic,
        range_from=range_from,
        range_to=range_to,
        generated_at=f"{range_to}T23:00:00Z",
        provider_runtime=schema.ProviderRuntime("local", "synthetic", "synthetic"),
        query_plan=schema.QueryPlan("news", "strict_recent", "story", topic, [], {}),
        clusters=clusters,
        ranked_candidates=candidates,
        items_by_source=items_by_source,
        errors_by_source={},
        artifacts={"synthetic_notice": SYNTHETIC_NOTICE},
    )


def report_for(scenario: Scenario) -> schema.Report:
    return build_report(scenario.topic, scenario.specs, scenario.range_from, scenario.range_to)


# ── 12 組基線情境(實作時就拿這些看修前/修後) ───────────────────────────

_FASHION = "men's fashion trends"
_PLTR = "Palantir PLTR"
_MP = "MP Materials rare earth"

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "full_fashion",
        _FASHION,
        (
            Spec(
                "Loafers are everywhere this fall and sneakers are quietly losing the streetwear crowd",
                "Walked through three menswear shops this weekend and every mannequin was in loafers. Sneaker wall was half the size it was in spring.",
                ("reddit", "tiktok"),
                "2026-10-06",
                engagement={"score": 412, "num_comments": 97},
                score=66,
            ),
            Spec(
                "Barn jackets replace the puffer as the default autumn layer in London and Tokyo street shots",
                "Counted barn jackets in every third street style photo from Tokyo this week. The puffer is gone until December.",
                ("reddit",),
                "2026-10-04",
                engagement={"score": 230, "num_comments": 41},
                score=58,
            ),
            Spec(
                "Wide-leg trousers hit the office: readers report dress codes loosening on pleats and volume",
                "Three readers wrote in saying their offices stopped flagging wide-leg trousers this quarter.",
                ("reddit",),
                "2026-10-02",
                engagement={"score": 120, "num_comments": 22},
                score=51,
            ),
            Spec(
                "Vintage military surplus thread: field jackets under $60 and where to find them",
                "",
                ("reddit",),
                "2026-09-29",
                engagement={"score": 88, "num_comments": 30},
                score=44,
            ),
        ),
        note="三題都有內容(1/3):四條候選、兩個來源、互動數齊全",
        group="full",
    ),
    Scenario(
        "full_pltr",
        _PLTR,
        (
            Spec(
                "Palantir signs a synthetic five-year logistics contract with a European rail operator",
                "The operator said the platform will schedule freight across four countries starting next quarter. (synthetic event)",
                ("hackernews", "reddit"),
                "2026-10-07",
                engagement={"points": 310, "num_comments": 140},
                score=70,
            ),
            Spec(
                "Short seller publishes a synthetic report claiming PLTR commercial growth is front-loaded",
                "The report argues most of the new commercial revenue came from two renewals pulled forward. (synthetic event)",
                ("reddit",),
                "2026-10-05",
                engagement={"score": 540, "num_comments": 260},
                score=64,
            ),
            Spec(
                "Polymarket traders price a 61% chance PLTR closes above a synthetic $200 line by year end",
                "",
                ("polymarket",),
                "2026-10-06",
                score=55,
            ),
            Spec(
                "Weekly PLTR discussion thread: earnings date, options flow, and the usual arguments",
                "Same thread as every week. Earnings date confirmed for early November. (synthetic)",
                ("reddit",),
                "2026-10-06",
                engagement={"score": 60, "num_comments": 400},
                score=40,
            ),
        ),
        note="三題都有內容(2/3):三個來源、含預測市場那種沒互動數的來源",
        group="full",
    ),
    Scenario(
        "full_mp",
        _MP,
        (
            Spec(
                "MP Materials ships the first synthetic batch of finished magnets from its Texas plant",
                "The company said the first commercial magnet shipment left the plant on Monday. (synthetic event)",
                ("reddit", "hackernews"),
                "2026-10-07",
                engagement={"score": 300, "num_comments": 85},
                score=72,
            ),
            Spec(
                "China tightens synthetic export licences on heavy rare earths, MP named as main US beneficiary",
                "Analysts on the thread argue the licence change matters more for magnets than for oxide prices. (synthetic)",
                ("reddit",),
                "2026-10-03",
                engagement={"score": 210, "num_comments": 64},
                score=61,
            ),
            Spec(
                "Rare earth price tracker thread (synthetic): NdPr up 4% this week, Dy flat",
                "NdPr oxide quoted higher for the third week in a row on the tracker. (synthetic prices)",
                ("reddit",),
                "2026-10-06",
                engagement={"score": 45, "num_comments": 12},
                score=48,
            ),
            Spec(
                "Hancock Prospecting takes a synthetic paper loss on its rare-earth stakes",
                "",
                ("reddit",),
                "2026-09-30",
                score=40,
            ),
        ),
        note="三題都有內容(3/3)",
        group="full",
    ),
    Scenario(
        "sparse",
        _MP,
        (
            Spec(
                "Only one thread this week: MP Materials magnet plant tour photos (synthetic)",
                "",
                ("reddit",),
                "2026-10-01",
                engagement={"score": 9},
                score=35,
            ),
        ),
        note="內容偏少:只有一條、沒有引文",
    ),
    Scenario(
        "same_event_two_sources",
        _PLTR,
        (
            Spec(
                "Palantir soars 12% on blowout quarter, with US commercial revenue soaring ~150%",
                "US commercial revenue grew about 150% year over year on the synthetic print.",
                ("hackernews",),
                "2026-10-06",
                engagement={"points": 220, "num_comments": 90},
                score=69,
            ),
            Spec(
                "JUST IN: Palantir soars on blowout Q2 earnings, US commercial revenue soaring nearly 150%",
                "Stock up double digits after hours. (synthetic)",
                ("reddit",),
                "2026-10-06",
                engagement={"score": 800, "num_comments": 300},
                score=62,
            ),
            Spec(
                "Britain would be bonkers to ditch Palantir, says a synthetic op-ed",
                "",
                ("hackernews",),
                "2026-10-04",
                score=42,
            ),
        ),
        note="同事件不同來源:前兩條是同一場財報,應併成一條並標出另一個來源",
    ),
    Scenario(
        "distinct_events",
        _PLTR,
        (
            Spec(
                "Michael Burry's fund opened a new short position in Palantir, synthetic 13F shows",
                "The filing lists put options on PLTR. (synthetic)",
                ("reddit",),
                "2026-10-05",
                engagement={"score": 150, "num_comments": 70},
                score=60,
            ),
            Spec(
                "Michael Burry says Palantir, $PLTR, will be at under $1 over the long run.",
                "",
                ("reddit",),
                "2026-10-03",
                engagement={"score": 90, "num_comments": 120},
                score=46,
            ),
            Spec(
                "Palantir Q3 synthetic earnings: revenue beat, guidance raised again",
                "Revenue and guidance both above the street. (synthetic)",
                ("hackernews",),
                "2026-10-07",
                engagement={"points": 180, "num_comments": 60},
                score=58,
            ),
        ),
        note="真正不同事件:同一個人兩種主張 + 一場財報,三條都要留",
    ),
    Scenario(
        "long_titles",
        _MP,
        tuple(
            Spec(
                f"Synthetic long headline {n}: " + " ".join(f"word{n}{i}" for i in range(45)),
                f"Synthetic body {n}. " + "lorem ipsum " * 30,
                ("reddit",),
                f"2026-10-0{n}",
                engagement={"score": 10 * n},
                score=60 - n,
            )
            for n in (1, 2, 3)
        ),
        note="長標題:每條 300 字上下,三條都要完整、不切半",
    ),
    Scenario(
        "partial_dates",
        _FASHION,
        (
            Spec(
                "Synthetic: corduroy is back on every rack this October",
                "Every rack. (synthetic)",
                ("reddit",),
                None,
                engagement={"score": 50},
                score=55,
            ),
            Spec(
                "Synthetic: the return of the chore coat, by way of Paris",
                "Three brands dropped chore coats the same week. (synthetic)",
                ("reddit",),
                "2026-10-02",
                "low",
                engagement={"score": 40},
                score=50,
            ),
            Spec(
                "Synthetic: knitted ties spotted on three runways",
                "",
                ("reddit",),
                "2026-10-06T14:00:00Z",
                "high",
                score=45,
            ),
        ),
        note="日期不完整:一條沒日期(要寫不明)、一條低信心(要寫約)、一條完整",
    ),
    Scenario(
        "single_source_only",
        _PLTR,
        (
            Spec(
                "Synthetic: Palantir hires a new head of federal sales",
                "",
                ("hackernews",),
                "2026-10-07",
                engagement={"points": 12},
                score=50,
                uncertainty="single-source",
            ),
            Spec(
                "Synthetic: a county in Ohio drops its Palantir contract",
                "",
                ("hackernews",),
                "2026-10-05",
                engagement={"points": 40, "num_comments": 15},
                score=47,
                uncertainty="single-source",
            ),
            Spec(
                "Synthetic: Palantir Foundry outage affected three customers for two hours",
                "",
                ("hackernews",),
                "2026-10-04",
                score=44,
                uncertainty="single-source",
            ),
        ),
        note="只剩單一來源:每條都要帶「單一來源,待證」",
    ),
    Scenario(
        "multi_day_updates",
        _MP,
        (
            Spec(
                "MP Materials ships the first synthetic batch of finished magnets from its Texas plant",
                "Same story as yesterday. (synthetic)",
                ("reddit",),
                "2026-10-07",
                score=70,
            ),
            Spec(
                "Day two (synthetic): GM confirms it received the first MP magnets and starts line trials",
                "GM said line trials begin next week. (synthetic)",
                ("reddit", "hackernews"),
                "2026-10-08",
                engagement={"score": 260, "num_comments": 70},
                score=68,
            ),
            Spec(
                "Synthetic: DoD price floor for NdPr kicks in, first quarterly true-up published",
                "",
                ("reddit",),
                "2026-10-08",
                score=52,
            ),
        ),
        note="多日連續更新:昨天送過的原事件跳過,今天的新進展與新事件要留",
        prior_titles=(
            "MP Materials ships the first synthetic batch of finished magnets from its Texas plant",
        ),
    ),
    Scenario(
        "mixed_cjk",
        _FASHION,
        (
            Spec(
                "東京男裝週(合成):AURALEE 與 CIOTA 的素材主義回潮,寬版西裝褲成主流",
                "這週東京街拍裡每三張就有一張寬版褲。(合成資料)",
                ("reddit",),
                "2026-10-06",
                engagement={"score": 77, "num_comments": 14},
                score=60,
            ),
            Spec(
                "Synthetic: Seoul buyers say 'quiet luxury' is over, texture is the new signal",
                "質感取代 logo,買手這樣說。(合成)",
                ("reddit",),
                "2026-10-05",
                score=55,
            ),
            Spec(
                "合成:台北秋季穿搭討論串 —— 巴爾幹夾克、樂福鞋與 wide-leg 的搭配",
                "",
                ("reddit",),
                "2026-10-03",
                score=48,
            ),
        ),
        note="中英混合:CJK 標題不可以被當成近重複、也不可以被切壞",
    ),
    Scenario(
        "over_budget",
        _MP,
        tuple(
            Spec(
                # 填充要是「很多個不同的詞」:三條共用的詞只有前幾個,否則重疊係數
                # 會把三條判成同一件事、併成一條,測的就不是預算了
                f"Synthetic oversized headline {n} " + " ".join(f"w{n}x{i}" for i in range(170)),
                f"Synthetic body {n}. " + "y" * 400,
                ("reddit",),
                f"2026-10-0{n}",
                score=60 - n,
                url="https://example.invalid/reddit/" + "z" * 120,
            )
            for n in (1, 2, 3)
        ),
        note="超出字數上限:整條退讓,不切半句,被略過的明天還能送",
    ),
)


# ── 6 組保留情境:實作完成後才加,沒拿來調整實作 ────────────────────────

HOLDOUTS: tuple[Scenario, ...] = (
    Scenario(
        "thin_evidence",
        _PLTR,
        (
            Spec(
                "Synthetic: low-score posts claim Palantir lost a UK NHS renewal",
                "Low-score posts, unverified. (synthetic)",
                ("reddit", "hackernews"),
                "2026-10-06",
                engagement={"score": 3},
                score=30,
                uncertainty="thin-evidence",
            ),
            Spec(
                "Synthetic: Palantir and a Gulf sovereign fund announce a data-centre partnership",
                "",
                ("hackernews", "reddit"),
                "2026-10-07",
                engagement={"points": 90},
                score=58,
            ),
        ),
        note="兩個來源且低分的證據薄弱限定要保留;平台名稱不表示佐證",
    ),
    Scenario(
        "html_entities",
        _FASHION,
        (
            Spec(
                "Synthetic: why I&#39;ve stopped buying &quot;raw denim&quot;",
                "submitted by &#32; /u/synthetic &#32; to r/malefashionadvice [link] [comments] <b>It&amp;#39;s</b> just not worth the fade chase anymore. <!-- tracking -->",
                ("reddit",),
                "2026-10-05",
                engagement={"score": 15},
                score=50,
            ),
        ),
        note="HTML 實體與 Reddit 樣板尾巴要洗乾淨",
    ),
    Scenario(
        "quote_echoes_title",
        _PLTR,
        (
            Spec(
                "Synthetic: Palantir opens a Tokyo office",
                "Synthetic: Palantir opens a Tokyo office",
                ("hackernews",),
                "2026-10-04",
                engagement={"points": 55},
                score=50,
            ),
        ),
        note="引文只是標題再講一次,不印引文",
    ),
    Scenario(
        "cross_year",
        _MP,
        (
            Spec(
                "Synthetic: year-end magnet shipment tally published",
                "",
                ("reddit",),
                "2025-12-30",
                score=55,
            ),
            Spec(
                "Synthetic: January price reset for NdPr announced",
                "",
                ("reddit",),
                "2026-01-05",
                score=50,
            ),
        ),
        note="跨年視窗:去年的日期要印全年份,今年的可以只印月日",
        range_from="2025-12-10",
        range_to="2026-01-09",
    ),
    Scenario(
        "url_missing",
        _FASHION,
        (
            Spec(
                "Synthetic: a source that gave us no link at all",
                "Some text. (synthetic)",
                ("reddit",),
                "2026-10-05",
                score=50,
                url="",
            ),
        ),
        note="沒有連結就沒有 🔗 行,不印空連結",
    ),
    Scenario(
        "three_way_duplicate",
        _PLTR,
        (
            Spec(
                "Palantir soars 12% on blowout quarter, with US commercial revenue soaring ~150%",
                "Synthetic print. (synthetic)",
                ("hackernews",),
                "2026-10-06",
                score=69,
            ),
            Spec(
                "Palantir soars on blowout quarter: US commercial revenue soaring ~150%",
                "",
                ("reddit",),
                "2026-10-06",
                score=62,
            ),
            Spec(
                "Palantir soars 12% on blowout quarter as commercial revenue soars 150%",
                "",
                ("x",),
                "2026-10-06",
                score=60,
            ),
            Spec(
                "Synthetic: Palantir names a new CFO",
                "",
                ("hackernews",),
                "2026-10-03",
                score=45,
            ),
        ),
        note="三個來源講同一件事:併成一條、另見兩個來源,第二條才是真正的另一件事",
    ),
)


def by_name(name: str) -> Scenario:
    for scenario in SCENARIOS + HOLDOUTS:
        if scenario.name == name:
            return scenario
    raise KeyError(name)
