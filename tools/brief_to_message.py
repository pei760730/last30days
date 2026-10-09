#!/usr/bin/env python3
"""把 last30days 的研究結果整理成一封 Telegram 早報。

fork 專屬(上游沒有這支)。`.github/workflows/daily-brief.yml` 每題呼叫一次。

輸入兩種都收:
- `--emit json --json-profile raw` 的完整 Report(cron 現在走這條):每條 storyline
  帶得出發布日期、來源、原文連結;
- `--emit brief` 的 Markdown 文字(舊路徑、手動跑):
  只有標題與引文,日期一律標「不明」,不猜。

選哪幾條、怎麼排,全部沿用上游 `render.render_brief` 的規則(relevance floor、
代表候選、前 8 條叢集)—— 這支只負責「人在手機上讀得出什麼」,不改研究策略。

為什麼放成檔案而不是塞進 workflow 的 `python -c`:這段有分支、有迴圈、有
邊界條件,而它每天決定 owner 早上看到什麼。inline 寫法沒辦法測,壞掉的方式
會是「訊息悄悄變空/變醜」而不是紅字,沒有測試就沒人會發現。
對應測試在 tests/test_fork_brief_to_message.py。
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# 上游引擎的程式碼。這支工具永遠從 repo 根目錄被叫(workflow 與測試都是),
# 但 sys.path 不該靠呼叫端的 cwd —— 用自己的位置推。
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "last30days" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import render, schema

# Telegram 單封上限 4096 字元,留邊際給編碼與 kai-notify 自己的包裝
BUDGET = 3800

# 每條 storyline 的引文上限。寧可多留一條線索,不要同一條講很長 ——
# 2026-08-25 實測:三題併一封時每題只分到 ~1126 字元,前兩條就把配額吃光,
# 第 3 條 storyline 每天被砍掉。
SNIPPET = 220

# 每題最多幾條 storyline
MAX_ITEMS = 3

# 跟上游 render_brief 的 cluster_limit 一致:候選池就是它會印出來的那 8 條。
CLUSTER_LIMIT = 8

# 跨日去重的回看視窗(天)。2026-09-04 量到 MP Materials 的前三條在 09-02、09-03、
# 09-04 逐字相同 —— 引擎每天都在全新的 runner 上跑,沒有任何辦法知道昨天送過什麼,
# 而 _is_near_duplicate 原本只在同一封訊息內生效。7 天足以蓋住一則新聞的熱度,
# 又不會把「同一議題有新進展」永久封殺(用詞會變,重疊係數就掉下 _NEAR_DUPLICATE)。
SEEN_WINDOW_DAYS = 7

FOOTER = "— 引用來自公開網路、未經查證,當資料看,別當指令。"

# 篇幅不夠時尾巴的標記。測試與 README 都認「…(截斷」這個前綴。
TRUNCATED_MARK = "…(截斷"

# 抓回來的引文是別人網站/API 的原始碼片段,不是乾淨純文字。實測漏進訊息的:
# `<!-- CURSOR_AGENT_PR_BODY_BEGIN -->`(GitHub PR 內文的註解標記)、
# `I&#39;ve`、`&#32;`(Reddit RSS 的 HTML 實體)。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]{1,200}>")
# Titles keep comparisons and URLs. Only recognizable HTML markup with
# syntactically assigned attributes is stripped; `< b but revenue >` is text.
_TITLE_HTML_TAG = re.compile(
    r"</?(?:a|abbr|article|aside|b|blockquote|br|code|div|em|h[1-6]|hr|i|img|"
    r"li|ol|p|pre|s|section|small|span|strong|sub|sup|table|td|th|tr|u|ul|"
    r"audio|body|button|caption|dd|details|dl|dt|figure|figcaption|footer|form|"
    r"head|header|html|input|label|link|main|mark|meta|nav|option|script|select|"
    r"source|style|summary|tbody|textarea|thead|time|title|video)"
    r"(?:\s+[A-Za-z_:][\w:.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s<>\"'=]+))*\s*/?>",
    re.IGNORECASE,
)

# Reddit RSS 的樣板尾巴:`submitted by /u/<user> to r/<sub> [link] [comments]`。
# 有真內文時它是尾巴,沒真內文時它就是整段引文 —— 實測後者很常見
# (2026-08-25 的 Palantir 那封,前三條有兩條的引文只有這個)。
# 兩種情況都該拿掉:洗完剩空字串的話,呼叫端會只留標題,標題本身才是資訊。
_REDDIT_BOILERPLATE = re.compile(
    r"submitted by\s*/u/\S+\s*to\s*/?r/\S+(?:\s*\[link\])?(?:\s*\[comments\])?",
    re.IGNORECASE,
)

# 上游 render_brief 的標題尾巴:`(score 67, Reddit, Hacker News) [single source]`。
_HEADING_SUFFIX = re.compile(
    r"\s*\(score\s+(?P<score>\d+)(?:,\s*(?P<sources>[^)]*))?\)"
    r"(?:\s+\[(?P<qualifier>thin evidence|single source)\])?\s*$"
)

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_BARE_URL = re.compile(r"https?://\S+")


# 近重複判定用。這些詞在標題裡到處都是,留著只會讓不相關的兩則看起來像。
_STOPWORDS = frozenset(
    """the a an of on in for to and or with is are was were be been at by as from
    that this it its his her their they he she we you i not no more than after
    over under about into out up down new just says said will would can could""".split()  # noqa: SIM905 — 區塊字串比 40 元素的 list literal 好讀好改
)

# 相似度門檻。用「重疊係數」(共同詞 / 較短那則的詞數),不是 Jaccard ——
# 兩則長度差很多時 Jaccard 會被長的那則稀釋掉。
#
# 2026-08-26 拿真實標題量測校準:
#   0.56  同一場財報的兩則("blowout quarter ... commercial revenue soaring 150%"
#         vs "blowout Q2 earnings ... commercial revenue soaring nearly 150%") → 該合併
#   0.50  Michael Burry 開空單 vs Burry 預測 PLTR 跌破 $1 → 同一人不同主張,該保留
# 安全區間只有這一線,所以取 0.55。tests/test_fork_brief_to_message.py 把這兩個
# 真實案例都釘住了,調門檻會直接紅。
#
# 這招只抓得到「近乎逐字重述」。同一事件但用詞完全不同(例如
# "Palantir Shares Jump on 'Otherworldly' Sales" vs "Palantir soars 12% on
# blowout quarter")量出來是 0.00 —— 純詞彙比對做不到,不要假裝它做得到。
_NEAR_DUPLICATE = 0.55

_UNCERTAINTY_LABEL = {
    "single-source": "單一來源,待證",
    "thin-evidence": "證據薄弱",
}


def _title_tokens(title: str, topic: str) -> set[str]:
    """標題的內容詞。去掉 `### N.` 前綴、`(score N, 來源)` 後綴、停用詞。

    主題本身的詞也要拿掉:每一則標題都含主題(「Palantir」),留著等於給
    所有配對灌一個固定的假相似度。

    JSON 路徑丟進來的是乾淨的叢集標題,文字路徑丟進來的是整行 heading;
    兩邊洗完要得到同一組詞 —— 跨日 seen cache 的指紋才接得上。
    """
    text = re.sub(r"^###\s*\d+\.\s*", "", title.strip())
    text = _HEADING_SUFFIX.sub("", text).strip()
    topic_words = set(re.findall(r"[a-z0-9]+", topic.lower()))
    return {
        w
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 1 and w not in _STOPWORDS and w not in topic_words
    }


def _is_near_duplicate(tokens: set[str], seen: list[set[str]]) -> int | None:
    """跟已收錄的哪一則近乎重述?回傳它的索引,沒有就 None。"""
    for index, other in enumerate(seen):
        if not tokens or not other:
            continue
        if len(tokens & other) / min(len(tokens), len(other)) >= _NEAR_DUPLICATE:
            return index
    return None


def _norm(text: str) -> str:
    """比對用的正規化:去標點、收空白、轉小寫。"""
    return re.sub(r"\W+", " ", text.lower()).strip()


def _echoes_title(title: str, quote: str) -> bool:
    """引文只是把標題再講一次(HN 那類條目的 snippet 就是標題本身)。

    同一句印兩次是純浪費 —— Telegram 上它佔掉的是下一條線索的位置。
    """
    normalized = _norm(quote)
    return bool(normalized) and normalized in _norm(title)


def _clean(text: str) -> str:
    """把引文洗成人看的純文字。"""
    text = _HTML_COMMENT.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    # 兩次 unescape:Reddit 那條路徑實測有雙重轉義(&amp;#39; → &#39; → ')
    text = html.unescape(html.unescape(text))
    # 樣板要在 unescape 之後才剝:原文長成 `submitted by &#32; /u/x &#32; to ...`
    text = _REDDIT_BOILERPLATE.sub(" ", text)
    # 引文裡的裸網址:Reddit 轉貼文常以一整條連結開頭,佔掉 100 字卻沒資訊 ——
    # 原文連結另外有 🔗 那行。順手把分隔用的 `----` 也拿掉。
    text = _BARE_URL.sub(" ", text)
    text = re.sub(r"(?:^|\s)-{3,}(?=\s|$)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title(text: str) -> str:
    """保留正文、比較式與網址,只去已辨識的 HTML 標記與註解。"""
    text = _TITLE_HTML_TAG.sub(" ", _HTML_COMMENT.sub(" ", text))
    text = html.unescape(html.unescape(text))
    return re.sub(r"\s+", " ", text).strip() or "(無標題)"


def _fingerprint_title(text: str) -> str:
    """Keep #60's existing cache identity separate from corrected display text.

    _clean is deliberately unchanged: old files retain their exact fingerprints
    and the same 0.55 comparisons without a cache migration or version bump.
    """
    return _clean(text) or re.sub(r"\s+", " ", html.unescape(text)).strip() or "(無標題)"


def _source_label(source: str) -> str:
    return render.SOURCE_LABELS.get(source, source.replace("_", " ").title())


# ── 資料模型:一條 storyline 在訊息裡需要的全部欄位 ────────────────────


@dataclass(frozen=True)
class Story:
    """一條 storyline。文字路徑拿不到的欄位留空,渲染時誠實說「不明」。"""

    title: str
    quote: str = ""
    sources: tuple[str, ...] = ()  # 人看的標籤,如 ("Reddit", "Hacker News")
    url: str = ""
    published: str | None = None  # YYYY-MM-DD;None = 來源沒給日期
    date_confidence: str | None = None  # high / med / low
    uncertainty: str | None = None  # single-source / thin-evidence
    engagement: str = ""  # 已排好版的互動數,如 "120 讚 · 48 留言"
    also: tuple[str, ...] = ()  # 被併進來的近重複條目帶來的其他來源
    fingerprint_title: str | None = None  # pre-repair cache identity, not display text


@dataclass(frozen=True)
class Head:
    topic: str = ""
    range_from: str = ""
    range_to: str = ""
    sources: tuple[str, ...] = ()  # 有回應的來源(標籤)
    input_state: str = "valid"  # valid / incomplete / invalid-report
    input_error: str = ""


@dataclass(frozen=True)
class Shaped:
    """一封訊息,加上「它為什麼長這樣」的實際數字。"""

    message: str
    items: int
    candidates: int
    near_duplicates: int
    repeats: int
    shipped: tuple[frozenset[str], ...]
    dropped_for_budget: int = 0
    input_state: str = "valid"
    input_error: str = ""
    responsive_sources: bool = False


# ── 輸入解析:Report(JSON)或 brief 文字 → Head + Story 清單 ──────────


def _date_parts(value: str | None) -> str | None:
    if not value:
        return None
    match = _ISO_DATE.match(value.strip())
    return match.group(0) if match else None


def _format_count(value: float) -> str:
    value = int(value)
    if value >= 10_000:
        return f"{value / 1000:.0f}k"
    if value >= 1_000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return str(value)


def _engagement_text(item: schema.SourceItem | None) -> str:
    """互動數:這是「真人在看」的證據,也是這條值得追的理由之一。

    各來源的欄位名不一樣,只認得出來的才印;認不出來就不印,不猜。
    """
    if not item or not item.engagement:
        return ""
    eng = item.engagement
    likes_key = {"reddit": "score", "hackernews": "points"}.get(item.source)
    parts: list[str] = []
    for key, label in (
        (likes_key, "讚"),
        ("likes", "讚"),
        ("like_count", "讚"),
        ("upvotes", "讚"),
        ("num_comments", "留言"),
        ("comments", "留言"),
        ("comment_count", "留言"),
    ):
        if not key:
            continue
        value = eng.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
            continue
        if any(p.endswith(label) for p in parts):
            continue
        parts.append(f"{_format_count(value)} {label}")
    return " · ".join(parts)


def _story_from_cluster(
    cluster: schema.Cluster,
    candidate_by_id: dict[str, schema.Candidate],
) -> Story:
    reps = [
        candidate_by_id[cid]
        for cid in render._qualifying_representative_ids(cluster, candidate_by_id, limit=2)
        if cid in candidate_by_id
    ]
    representative = reps[0] if reps else None
    primary = schema.candidate_primary_item(representative) if representative else None
    # One provenance for date, URL, engagement and quote. Missing fields stay
    # missing instead of borrowing a newer date or a different author's quote.
    if primary:
        quote = _clean(primary.snippet or primary.body)
    else:
        quote = _clean(representative.snippet) if representative else ""
    url = primary.url if primary else representative.url if representative else ""
    url = url if url.startswith(("http://", "https://")) else ""
    published = _date_parts(primary.published_at) if primary else None
    confidence = primary.date_confidence if primary else None
    return Story(
        title=_title(cluster.title),
        quote=quote,
        sources=tuple(dict.fromkeys(_source_label(s) for s in cluster.sources)),
        url=url,
        published=published,
        date_confidence=confidence,
        uncertainty=cluster.uncertainty,
        engagement=_engagement_text(primary),
        fingerprint_title=_fingerprint_title(cluster.title),
    )


def stories_from_report(report: schema.Report) -> tuple[Head, list[Story]]:
    """跟上游 render_brief 看到同一組 storyline:同樣的 relevance floor、同樣的前 8 條。"""
    evidence = schema.without_sources(report, {"corpus"})
    candidate_by_id = {c.candidate_id: c for c in evidence.ranked_candidates}
    visible = render._clusters_clearing_relevance_floor(
        evidence, evidence.clusters[:CLUSTER_LIMIT]
    )
    active = [s for s, items in sorted(report.items_by_source.items()) if items]
    head = Head(
        topic=report.topic,
        range_from=report.range_from,
        range_to=report.range_to,
        sources=tuple(_source_label(s) for s in active),
    )
    return head, [_story_from_cluster(cluster, candidate_by_id) for cluster in visible]


def _head_from_text(raw: str) -> Head:
    head = Head()
    for line in raw.splitlines():
        if line.startswith("## Ranked Storylines"):
            break
        text = line.strip()
        if text.startswith("# Production Brief:"):
            head = replace(head, topic=text.partition(":")[2].strip())
        elif text.startswith("- Date range:"):
            match = re.match(r"- Date range:\s*(\S+)\s+to\s+(\S+)", text)
            if match:
                head = replace(head, range_from=match.group(1), range_to=match.group(2))
        elif text.startswith("- Sources:"):
            match = re.search(r"\((.*)\)\s*$", text)
            if match:
                head = replace(
                    head,
                    sources=tuple(s.strip() for s in match.group(1).split(",") if s.strip()),
                )
    return head


def stories_from_text(raw: str) -> tuple[Head, list[Story]]:
    """`--emit brief` 的 Markdown。沒有日期、沒有連結 —— 渲染時會照實說。"""
    stories: list[Story] = []
    for block in re.split(r"(?=^### )", raw, flags=re.MULTILINE):
        if not block.startswith("### "):
            continue
        lines = block.splitlines()
        heading = re.sub(r"^###\s*\d+\.\s*", "", lines[0].strip())
        suffix = _HEADING_SUFFIX.search(heading)
        sources: tuple[str, ...] = ()
        uncertainty: str | None = None
        if suffix:
            heading = heading[: suffix.start()].strip()
            sources = tuple(s.strip() for s in (suffix.group("sources") or "").split(",") if s.strip())
            if suffix.group("qualifier"):
                uncertainty = suffix.group("qualifier").replace(" ", "-")
        body: list[str] = []
        for line in lines[1:]:
            text = line.strip()
            # 碰到下一個大段落(Audience Questions / Source Clusters)就停,
            # 否則最後一條 storyline 會把整份報告的尾巴都吸進來
            if text.startswith("## "):
                break
            # `_Why:` 是 reranker 的自我說明,佔位置又不帶新資訊
            if not text or text.startswith("_Why:"):
                continue
            body.append(text.lstrip("-").strip())
        stories.append(
            Story(
                title=_title(heading),
                quote=_clean(" ".join(body)),
                sources=sources,
                uncertainty=uncertainty,
                fingerprint_title=_fingerprint_title(heading),
            )
        )
    return _head_from_text(raw), stories


def parse_input(raw: str) -> tuple[Head, list[Story]]:
    """Separate a valid empty report from missing input and schema failures."""
    stripped = raw.lstrip()
    if not stripped:
        return Head(input_state="incomplete", input_error="empty input"), []
    if not stripped.startswith("# Production Brief:"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return Head(input_state="incomplete", input_error=f"JSON line {exc.lineno}, column {exc.colno}: {exc.msg}"), []
        try:
            if not isinstance(payload, dict):
                raise ValueError("Report root must be an object")
            for key in ("topic", "range_from", "range_to", "generated_at"):
                if not isinstance(payload.get(key), str):
                    raise ValueError(f"Report {key} must be str")
            for key, expected in (("ranked_candidates", list), ("clusters", list), ("items_by_source", dict)):
                if not isinstance(payload.get(key), expected):
                    raise ValueError(f"Report {key} must be {expected.__name__}")
            return stories_from_report(schema.report_from_dict(payload))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # Never echo the payload; diagnostic detail is annotation-escaped.
            return Head(input_state="invalid-report", input_error=f"{type(exc).__name__}: {exc}"), []
    return stories_from_text(raw)


# ── 選條:近重複併入前一則、近 7 天送過的跳過 ───────────────────────────


def _select(
    stories: list[Story],
    topic: str,
    prior: tuple[frozenset[str], ...],
) -> tuple[list[Story], int, int, int, list[set[str]]]:
    """前 MAX_ITEMS 條,近重複併進前一則(只帶來源,不佔位置)。

    brief 一律產 8 條候選而我們只取 3,所以跳過還有得補。2026-08-26 的
    Palantir 早報三個位置全是同一場財報,等於 owner 只拿到一條資訊。

    ``prior`` 是最近幾天已經送出去的標題指紋;命中的條目會被跳過,並單獨計數 ——
    「今天沒有新東西」跟「今天只抓到一條」要能分辨,兩者的處置完全不同。

    回傳 (條目, 候選總數, 近重複數, 跨日重複數, 今天送出的指紋)。
    """
    items: list[Story] = []
    seen_tokens: list[set[str]] = []
    candidates = near_duplicates = repeats = 0
    prior_tokens = [set(entry) for entry in prior]
    for story in stories:
        candidates += 1
        tokens = _title_tokens(story.fingerprint_title if story.fingerprint_title is not None else story.title, topic)
        twin = _is_near_duplicate(tokens, seen_tokens)
        if twin is not None:
            # Only title similarity: additional platform names are not corroboration.
            near_duplicates += 1
            extra = tuple(
                s for s in story.sources if s not in items[twin].sources and s not in items[twin].also
            )
            if extra:
                items[twin] = replace(items[twin], also=items[twin].also + extra)
            continue
        if len(items) >= MAX_ITEMS:
            # 取滿了,剩下的只數不做 —— 候選數要算完整,才講得出「8 條選 3 條」
            continue
        if _is_near_duplicate(tokens, prior_tokens) is not None:
            # 近 SEEN_WINDOW_DAYS 天送過同一則。不加進 seen_tokens:它沒佔今天的位置。
            repeats += 1
            continue
        seen_tokens.append(tokens)
        items.append(story)
    return items, candidates, near_duplicates, repeats, seen_tokens


# ── 渲染 ─────────────────────────────────────────────────────────────────


def _short_date(date: str, range_to: str) -> str:
    # 同一年只印月日(手機上省位置);跨年才印全。
    if range_to[:4] == date[:4]:
        return date[5:]
    return date


def _date_text(story: Story, range_to: str) -> str:
    if not story.published:
        return "發布日期不明"
    shown = _short_date(story.published, range_to)
    if story.date_confidence and story.date_confidence != "high":
        return f"發布約 {shown}"
    return f"發布 {shown}"


def _meta_line(story: Story, range_to: str) -> str:
    sources = " + ".join(story.sources) if story.sources else "來源不明"
    parts = [_date_text(story, range_to), sources]
    if story.also:
        parts.append("另見 " + "、".join(story.also))
    if story.engagement:
        parts.append(story.engagement)
    if story.uncertainty in _UNCERTAINTY_LABEL:
        parts.append(_UNCERTAINTY_LABEL[story.uncertainty])
    return "📅 " + " · ".join(parts)


def _render_story(index: int, story: Story, range_to: str, *, with_quote: bool = True) -> str:
    lines = [f"{index}. {story.title}", _meta_line(story, range_to)]
    quote = story.quote
    # 判斷要在截斷之前做:截過的引文是前綴,比對不出它本來就是標題
    if with_quote and quote and not _echoes_title(story.title, quote):
        if len(quote) > SNIPPET:
            quote = quote[:SNIPPET].rstrip() + "…"
        lines.append(f"「{quote}」")
    if story.url:
        lines.append(f"🔗 {story.url}")
    return "\n".join(lines)


def _run_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _render_head(head: Head, topic: str, now: datetime.datetime | None = None) -> str:
    name = topic or head.topic or "這題"
    instant = now if now is not None else _run_now()
    if instant.tzinfo is None:
        raise ValueError("run time must be timezone-aware")
    # Current Taipei civil time is UTC+08:00, including year boundaries.
    # An explicit timezone avoids requiring an external tzdata install on Windows.
    taipei = datetime.timezone(datetime.timedelta(hours=8), "Asia/Taipei")
    day = instant.astimezone(taipei).date().isoformat()
    first = f"📰 {name} · {day} 早報"
    window = f"觀察窗 {head.range_from} → {head.range_to} (UTC)" if head.range_from and head.range_to else ""
    sources = "有回應的來源:" + ("、".join(head.sources) if head.sources else "無")
    second = " · ".join(p for p in (window, sources) if p)
    return f"{first}\n{second}"


def _shortfall_note(
    shown: int,
    candidates: int,
    near_duplicates: int,
    repeats: int,
    dropped: int,
) -> str:
    # 少於 MAX_ITEMS 時要講出為什麼。訊息本身看不出「今天只有兩條」是
    # 因為來源沒東西,還是被去重吃掉 —— 而那是「今天新聞少」跟「抓取
    # 半殘」的差別。不講數字,連續幾天只有一條也不會有人察覺。
    reasons = [f"候選 {candidates} 條"]
    if near_duplicates:
        reasons.append(f"{near_duplicates} 條標題相近、已併入")
    if repeats:
        reasons.append(f"{repeats} 條近 {SEEN_WINDOW_DAYS} 天送過")
    if dropped:
        reasons.append(f"{dropped} 條因篇幅略過")
    return f"ℹ️ 今天只有 {shown}/{MAX_ITEMS} 條新的({','.join(reasons)})。"


def _empty_note(topic: str, head: Head, candidates: int, repeats: int, dropped: int = 0) -> str:
    # 這題沒東西時要講清楚是「這題沒抓到」,不是系統掛了 ——
    # owner 靠這句分辨「來源沒回應」跟「早報壞掉」,沉默兩者長得一樣
    label = f"「{topic or head.topic}」" if (topic or head.topic) else "這題"
    if head.input_state == "invalid-report":
        return f"⚠️ {label}早報 Report 結構解析失敗，請查看執行紀錄。"
    if head.input_state == "incomplete":
        return f"⚠️ {label}早報輸入空白或殘缺，無法判斷來源結果。"
    if dropped:
        return f"⚠️ {label}有資料，但篇幅放不下。"
    if repeats:
        # 抓到了,只是全都送過。講成「來源沒回應」是錯的診斷,而錯的診斷
        # 會讓人去查一個沒有壞掉的東西。
        return (
            f"📭 {label}今天沒有新東西"
            f"(候選 {candidates} 條,{repeats} 條近 {SEEN_WINDOW_DAYS} 天都送過)。"
        )
    if head.sources:
        return f"📭 {label}有資料，但沒有符合選條條件的內容。"
    return f"📭 {label}觀察窗內沒有內容。"


def shape_stories(
    head: Head,
    stories: list[Story],
    topic: str = "",
    prior: tuple[frozenset[str], ...] = (),
    *,
    now: datetime.datetime | None = None,
) -> Shaped:
    """Head + Story 清單 → 一封訊息。訊息永遠非空、永遠在預算內、尾巴永遠是 FOOTER。

    篇幅不夠時從最後一條開始退讓:先拿掉它的引文,還不夠就整條略過(連
    指紋一起,明天還有機會送)。永遠不切半條 —— 半截標題、斷掉的連結比沒有更糟。
    """
    items, candidates, near_duplicates, repeats, shipped = _select(stories, topic, prior)
    limit = BUDGET - len(FOOTER) - 2
    header = _render_head(head, topic, now)
    with_quote = [True] * len(items)
    dropped = 0

    while True:
        blocks = [
            _render_story(i + 1, story, head.range_to, with_quote=with_quote[i])
            for i, story in enumerate(items)
        ]
        tail: list[str] = []
        if items and (len(items) < MAX_ITEMS or dropped):
            tail.append(_shortfall_note(len(items), candidates, near_duplicates, repeats, dropped))
        elif not items:
            tail.append(_empty_note(topic, head, candidates, repeats, dropped))
        if dropped:
            tail.append(f"{TRUNCATED_MARK}:篇幅已滿,略過 {dropped} 條)")
        body = "\n\n".join([header, *blocks, *tail])
        if len(body) <= limit:
            break
        if items and with_quote[-1] and items[-1].quote:
            with_quote[-1] = False
            continue
        if items:
            items.pop()
            with_quote.pop()
            shipped.pop()
            dropped += 1
            continue
        # 連標頭加一句話都放不下(不該發生,但預算是可調的)—— 硬切,保住頁尾
        marker = TRUNCATED_MARK + ")"
        body = body[: limit - len(marker)].rstrip() + marker
        break

    return Shaped(
        message=body + "\n\n" + FOOTER,
        items=len(items),
        candidates=candidates,
        near_duplicates=near_duplicates,
        repeats=repeats,
        shipped=tuple(frozenset(t) for t in shipped),
        dropped_for_budget=dropped,
        input_state=head.input_state,
        input_error=head.input_error,
        responsive_sources=bool(head.sources),
    )


def shape_report(
    report: schema.Report,
    topic: str = "",
    prior: tuple[frozenset[str], ...] = (),
    *,
    now: datetime.datetime | None = None,
) -> Shaped:
    head, stories = stories_from_report(report)
    return shape_stories(head, stories, topic, prior, now=now)


def shape(
    raw: str,
    topic: str = "",
    prior: tuple[frozenset[str], ...] = (),
    *,
    now: datetime.datetime | None = None,
) -> Shaped:
    """原始輸入(JSON 或 brief 文字)→ 一封訊息,附上決定它長相的數字。"""
    head, stories = parse_input(raw)
    return shape_stories(head, stories, topic, prior, now=now)


def build_message(raw: str, topic: str = "") -> str:
    """只要訊息字串的呼叫端與既有測試走這個。"""
    return shape(raw, topic).message


# ── 跨日 seen cache(契約沿用 #48 / #58:只記真的送出去的完整標題) ─────


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _cutoff(window: int) -> str:
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=window)).isoformat()


def load_seen(path: str | None, window: int = SEEN_WINDOW_DAYS) -> tuple[frozenset[str], ...]:
    """讀出回看視窗內送過的標題指紋。

    壞掉的狀態檔一律當成空的:這是個「加分」機制,不該讓一個壞掉的 JSON
    害整題早報變紅。最壞情況是退回沒有跨日去重的行為,也就是今天的行為。
    """
    if not path:
        return ()
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return ()
    if not isinstance(state, dict):
        return ()
    cutoff = _cutoff(window)
    out: list[frozenset[str]] = []
    for day, entries in state.items():
        if not isinstance(day, str) or day < cutoff or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, list) and all(isinstance(w, str) for w in entry):
                out.append(frozenset(entry))
    return tuple(out)


def save_seen(
    path: str | None,
    shipped: tuple[frozenset[str], ...],
    window: int = SEEN_WINDOW_DAYS,
) -> None:
    """把今天送出的指紋併進狀態檔,同時把視窗外的丟掉。"""
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    cutoff = _cutoff(window)
    state = {
        day: entries
        for day, entries in state.items()
        if isinstance(day, str) and day >= cutoff and isinstance(entries, list)
    }
    if shipped:
        today = _today()
        state.setdefault(today, [])
        state[today].extend(sorted(entry) for entry in shipped)

    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True)
    except OSError as exc:
        # 寫不進去就只是明天少一天的記憶,不值得讓這題變紅。
        print(f"::warning title=daily-brief 狀態沒寫成::{exc}", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=Path(argv[0]).name if argv else "brief_to_message.py")
    parser.add_argument("path", nargs="?", default="report.json")
    parser.add_argument("topic", nargs="?", default="")
    parser.add_argument(
        "--seen",
        default=None,
        help="狀態檔:記住最近幾天送過哪些標題,用來跨日去重(可有可無)",
    )
    parser.add_argument("--window", type=int, default=SEEN_WINDOW_DAYS)
    args = parser.parse_args(argv[1:])
    path, topic = args.path, args.topic
    try:
        # errors="replace":內容是抓回來的網路文字,萬一夾到一個非 UTF-8
        # byte,硬解會拋例外 → 這題變紅 → owner 收到一封假 FAILED。
        # 一個替代字元的雜訊,換一整題的早報。
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read().replace("\r", "")
    except OSError:
        raw = ""

    shaped = shape(raw, topic, load_seen(args.seen, args.window))
    if shaped.input_state == "valid":
        save_seen(args.seen, shaped.shipped, args.window)

    # Telegram 那封會被滑掉,run 上的 annotation 不會。GitHub 會從 stderr 解析
    # 這種 workflow command,所以這裡不需要動 workflow —— 而且 stdout 是訊息
    # 本體,不能混東西進去。
    label = topic or "(未指定主題)"
    detail = f"「{label}」{shaped.input_error}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if shaped.input_state == "invalid-report":
        print(f"::error title=daily-brief Report 結構解析失敗::{detail}", file=sys.stderr)
    elif shaped.input_state == "incomplete":
        print(f"::warning title=daily-brief 輸入空白或殘缺::{detail}", file=sys.stderr)
    elif shaped.items == 0 and shaped.dropped_for_budget:
        print(
            f"::warning title=daily-brief 截斷::「{label}」候選 {shaped.candidates} 條,"
            "沒有完整條目能放進字數上限",
            file=sys.stderr,
        )
    elif shaped.items == 0 and shaped.repeats:
        print(
            f"::notice title=daily-brief 沒有新東西::「{label}」候選 {shaped.candidates} 條,"
            f"全部在近 {args.window} 天送過",
            file=sys.stderr,
        )
    elif shaped.items == 0:
        diagnosis = "有資料，但沒有符合選條條件的內容" if shaped.responsive_sources else "觀察窗內沒有內容"
        print(f"::warning title=daily-brief 空結果::「{label}」{diagnosis}", file=sys.stderr)
    elif shaped.items < MAX_ITEMS:
        print(
            f"::warning title=daily-brief 缺條目::「{label}」只取到 "
            f"{shaped.items}/{MAX_ITEMS} 條(候選 {shaped.candidates} 條、"
            f"近重複 {shaped.near_duplicates} 條、跨日重複 {shaped.repeats} 條、"
            f"篇幅略過 {shaped.dropped_for_budget} 條)",
            file=sys.stderr,
        )

    # 寫 bytes 而不是 text:後者要看執行環境的 stdout 編碼,
    # 替代字元 U+FFFD 在非 UTF-8 環境會再炸一次
    sys.stdout.buffer.write((shaped.message + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
