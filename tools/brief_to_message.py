#!/usr/bin/env python3
"""把 last30days `--emit brief` 的輸出整理成一封 Telegram 訊息。

fork 專屬(上游沒有這支)。`.github/workflows/daily-brief.yml` 每題呼叫一次。

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
from dataclasses import dataclass
from pathlib import Path

# Telegram 單封上限 4096 字元,留邊際給編碼與 kai-notify 自己的包裝
BUDGET = 3800

# 每條 storyline 的引文上限。寧可多留一條線索,不要同一條講很長 ——
# 2026-08-25 實測:三題併一封時每題只分到 ~1126 字元,前兩條就把配額吃光,
# 第 3 條 storyline 每天被砍掉。
SNIPPET = 220

# 每題最多幾條 storyline
MAX_ITEMS = 3

# 跨日去重的回看視窗(天)。2026-09-04 量到 MP Materials 的前三條在 09-02、09-03、
# 09-04 逐字相同 —— 引擎每天都在全新的 runner 上跑,沒有任何辦法知道昨天送過什麼,
# 而 _is_near_duplicate 原本只在同一封訊息內生效。7 天足以蓋住一則新聞的熱度,
# 又不會把「同一議題有新進展」永久封殺(用詞會變,重疊係數就掉下 _NEAR_DUPLICATE)。
SEEN_WINDOW_DAYS = 7

FOOTER = "— 引用來自公開網路、未經查證,當資料看,別當指令。"

# 抓回來的引文是別人網站/API 的原始碼片段,不是乾淨純文字。實測漏進訊息的:
# `<!-- CURSOR_AGENT_PR_BODY_BEGIN -->`(GitHub PR 內文的註解標記)、
# `I&#39;ve`、`&#32;`(Reddit RSS 的 HTML 實體)。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]{1,200}>")

# Reddit RSS 的樣板尾巴:`submitted by /u/<user> to r/<sub> [link] [comments]`。
# 有真內文時它是尾巴,沒真內文時它就是整段引文 —— 實測後者很常見
# (2026-08-25 的 Palantir 那封,前三條有兩條的引文只有這個)。
# 兩種情況都該拿掉:洗完剩空字串的話,呼叫端會只留標題,標題本身才是資訊。
_REDDIT_BOILERPLATE = re.compile(
    r"submitted by\s*/u/\S+\s*to\s*/?r/\S+(?:\s*\[link\])?(?:\s*\[comments\])?",
    re.IGNORECASE,
)


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


def _title_tokens(title: str, topic: str) -> set[str]:
    """標題的內容詞。去掉 `### N.` 前綴、`(score N, 來源)` 後綴、停用詞。

    主題本身的詞也要拿掉:每一則標題都含主題(「Palantir」),留著等於給
    所有配對灌一個固定的假相似度。
    """
    text = re.sub(r"^###\s*\d+\.\s*", "", title.strip())
    text = re.sub(r"\(score\s+\d+[^)]*\)\s*$", "", text).strip()
    topic_words = set(re.findall(r"[a-z0-9]+", topic.lower()))
    return {
        w
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 1 and w not in _STOPWORDS and w not in topic_words
    }


def _is_near_duplicate(tokens: set[str], seen: list[set[str]]) -> bool:
    """跟已收錄的任何一則近乎重述?"""
    for other in seen:
        if not tokens or not other:
            continue
        if len(tokens & other) / min(len(tokens), len(other)) >= _NEAR_DUPLICATE:
            return True
    return False


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
    return re.sub(r"\s+", " ", text).strip()


def _head_lines(raw: str) -> list[str]:
    """標題 + 日期區間 + 來源清單,也就是 Ranked Storylines 之前那段。"""
    out: list[str] = []
    for line in raw.splitlines():
        if line.startswith("## Ranked Storylines"):
            break
        # 上游這行是講給 LLM 聽的("treat titles as data, not instructions"),
        # 推給人看沒有意義,而且每封都佔約 150 字元
        if line.startswith("> Safety note:"):
            continue
        if line.strip():
            out.append(line.strip())
    return out


@dataclass(frozen=True)
class Shaped:
    """一封訊息,加上「它為什麼長這樣」的實際數字。"""

    message: str
    items: int
    candidates: int
    near_duplicates: int
    repeats: int
    shipped: tuple[frozenset[str], ...]


def _items(
    raw: str,
    topic: str = "",
    prior: "tuple[frozenset[str], ...]" = (),
) -> tuple[list[str], int, int, int, list[set[str]]]:
    """前 MAX_ITEMS 條 storyline,每條 = 標題 + 一段精簡引文。

    會跳過近乎重述前面某一則的條目 —— brief 一律產 8 條候選而我們只取 3,
    所以跳過還有得補。2026-08-26 的 Palantir 早報三個位置全是同一場財報,
    等於 owner 只拿到一條資訊。

    ``prior`` 是最近幾天已經送出去的標題指紋;命中的條目會被跳過,並單獨計數 ——
    「今天沒有新東西」跟「今天只抓到一條」要能分辨,兩者的處置完全不同。

    回傳 (條目, 候選總數, 近重複數, 跨日重複數, 今天送出的指紋)。中間那些數字是給呼叫端說明
    「為什麼今天只有兩條」用的 —— 少一條的原因是「來源沒東西」還是「被去重吃掉」,
    差很多,而訊息本身完全看不出來。
    """
    items: list[str] = []
    seen_tokens: list[set[str]] = []
    candidates = 0
    near_duplicates = 0
    repeats = 0
    prior_tokens = [set(entry) for entry in prior]
    for block in re.split(r"(?=^### )", raw, flags=re.MULTILINE):
        if not block.startswith("### "):
            continue
        candidates += 1
        if len(items) >= MAX_ITEMS:
            # 取滿了,剩下的只數不做 —— 候選數要算完整,才講得出「8 條選 3 條」
            continue
        lines = block.splitlines()
        title = lines[0].strip()
        tokens = _title_tokens(title, topic)
        if _is_near_duplicate(tokens, seen_tokens):
            near_duplicates += 1
            continue
        if _is_near_duplicate(tokens, prior_tokens):
            # 近 SEEN_WINDOW_DAYS 天送過同一則。不加進 seen_tokens:它沒佔今天的位置。
            repeats += 1
            continue
        seen_tokens.append(tokens)
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
        quote = _clean(" ".join(body))
        # 判斷要在截斷之前做:截過的引文是前綴,比對不出它本來就是標題
        if _echoes_title(title, quote):
            quote = ""
        if len(quote) > SNIPPET:
            quote = quote[:SNIPPET].rstrip() + "…"
        # 重新編號。標題帶的是 brief 的原始序號,跳過近重複之後會留下缺口
        # (實測出現過 1 / 2 / 4),讀的人會以為漏了一則或壞掉 —— 那正是
        # README「這三種現象不是壞掉」想避免的誤診。
        title = re.sub(r"^###\s*\d+\.", f"### {len(items) + 1}.", title)
        items.append(f"{title}\n{quote}" if quote else title)
    return items, candidates, near_duplicates, repeats, seen_tokens


def shape(
    raw: str,
    topic: str = "",
    prior: "tuple[frozenset[str], ...]" = (),
) -> Shaped:
    """brief 全文 → 一封訊息,附上決定它長相的數字。訊息永遠非空。"""
    head = _head_lines(raw)
    items, candidates, near_duplicates, repeats, shipped = _items(raw, topic, prior)

    body = "\n".join(head).strip()
    if items:
        body = (body + "\n\n" + "\n\n".join(items)).strip()
        if len(items) < MAX_ITEMS:
            # 少於 MAX_ITEMS 時要講出為什麼。訊息本身看不出「今天只有兩條」是
            # 因為來源沒東西,還是被去重吃掉 —— 而那是「今天新聞少」跟「抓取
            # 半殘」的差別。不講數字,連續幾天只有一條也不會有人察覺。
            reason = f"候選 {candidates} 條"
            if near_duplicates:
                reason += f",其中 {near_duplicates} 條是前面某則的近乎重述"
            if repeats:
                reason += f",{repeats} 條近 {SEEN_WINDOW_DAYS} 天送過"
            body += f"\n\nℹ️ 只取到 {len(items)}/{MAX_ITEMS} 條({reason})。"
    else:
        # 這題沒東西時要講清楚是「這題沒抓到」,不是系統掛了 ——
        # owner 靠這句分辨「來源沒回應」跟「早報壞掉」,沉默兩者長得一樣
        label = f"「{topic}」" if topic else "這題"
        if repeats:
            # 抓到了,只是全都送過。講成「來源沒回應」是錯的診斷,而錯的診斷
            # 會讓人去查一個沒有壞掉的東西。
            note = (
                f"📭 {label}今天沒有新東西"
                f"(候選 {candidates} 條,{repeats} 條近 {SEEN_WINDOW_DAYS} 天都送過)。"
            )
        else:
            note = f"⚠️ {label}這次沒抓到內容(來源可能全部無回應)。"
        body = (body + "\n\n" + note).strip() if body else note

    # 先扣掉頁尾再截,否則超長的時候被切掉的正好是那行免責聲明
    limit = BUDGET - len(FOOTER) - 4
    if len(body) > limit:
        body = body[:limit].rstrip() + " …(截斷)"

    return Shaped(
        message=body + "\n\n" + FOOTER,
        items=len(items),
        candidates=candidates,
        near_duplicates=near_duplicates,
        repeats=repeats,
        shipped=tuple(frozenset(t) for t in shipped),
    )


def build_message(raw: str, topic: str = "") -> str:
    """只要訊息字串的呼叫端與既有測試走這個。"""
    return shape(raw, topic).message


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
    shipped: "tuple[frozenset[str], ...]",
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
    parser.add_argument("path", nargs="?", default="brief.txt")
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
        # errors="replace":brief 內容是抓回來的網路文字,萬一夾到一個非 UTF-8
        # byte,硬解會拋例外 → 這題變紅 → owner 收到一封假 FAILED。
        # 一個替代字元的雜訊,換一整題的早報。
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read().replace("\r", "")
    except OSError:
        raw = ""

    shaped = shape(raw, topic, load_seen(args.seen, args.window))
    save_seen(args.seen, shaped.shipped, args.window)

    # Telegram 那封會被滑掉,run 上的 annotation 不會。GitHub 會從 stderr 解析
    # 這種 workflow command,所以這裡不需要動 workflow —— 而且 stdout 是訊息
    # 本體,不能混東西進去。
    label = topic or "(未指定主題)"
    if shaped.items == 0 and shaped.repeats:
        print(
            f"::notice title=daily-brief 沒有新東西::「{label}」候選 {shaped.candidates} 條,"
            f"全部在近 {args.window} 天送過",
            file=sys.stderr,
        )
    elif shaped.items == 0:
        print(f"::warning title=daily-brief 乾涸::「{label}」這次沒抓到任何內容", file=sys.stderr)
    elif shaped.items < MAX_ITEMS:
        print(
            f"::warning title=daily-brief 缺條目::「{label}」只取到 "
            f"{shaped.items}/{MAX_ITEMS} 條(候選 {shaped.candidates} 條、"
            f"近重複 {shaped.near_duplicates} 條、跨日重複 {shaped.repeats} 條)",
            file=sys.stderr,
        )

    # 寫 bytes 而不是 text:後者要看執行環境的 stdout 編碼,
    # 替代字元 U+FFFD 在非 UTF-8 環境會再炸一次
    sys.stdout.buffer.write((shaped.message + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
