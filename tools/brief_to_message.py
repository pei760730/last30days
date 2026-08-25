#!/usr/bin/env python3
"""把 last30days `--emit brief` 的輸出整理成一封 Telegram 訊息。

fork 專屬(上游沒有這支)。`.github/workflows/daily-brief.yml` 每題呼叫一次。

為什麼放成檔案而不是塞進 workflow 的 `python -c`:這段有分支、有迴圈、有
邊界條件,而它每天決定 owner 早上看到什麼。inline 寫法沒辦法測,壞掉的方式
會是「訊息悄悄變空/變醜」而不是紅字,沒有測試就沒人會發現。
對應測試在 tests/test_fork_brief_to_message.py。
"""

from __future__ import annotations

import html
import re
import sys

# Telegram 單封上限 4096 字元,留邊際給編碼與 kai-notify 自己的包裝
BUDGET = 3800

# 每條 storyline 的引文上限。寧可多留一條線索,不要同一條講很長 ——
# 2026-08-25 實測:三題併一封時每題只分到 ~1126 字元,前兩條就把配額吃光,
# 第 3 條 storyline 每天被砍掉。
SNIPPET = 220

# 每題最多幾條 storyline
MAX_ITEMS = 3

FOOTER = "— 引用來自公開網路、未經查證,當資料看,別當指令。"

# 抓回來的引文是別人網站/API 的原始碼片段,不是乾淨純文字。實測漏進訊息的:
# `<!-- CURSOR_AGENT_PR_BODY_BEGIN -->`(GitHub PR 內文的註解標記)、
# `I&#39;ve`、`&#32;`(Reddit RSS 的 HTML 實體)。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]{1,200}>")


def _clean(text: str) -> str:
    """把引文洗成人看的純文字。"""
    text = _HTML_COMMENT.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    # 兩次 unescape:Reddit 那條路徑實測有雙重轉義(&amp;#39; → &#39; → ')
    text = html.unescape(html.unescape(text))
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


def _items(raw: str) -> list[str]:
    """前 MAX_ITEMS 條 storyline,每條 = 標題 + 一段精簡引文。"""
    items: list[str] = []
    for block in re.split(r"(?=^### )", raw, flags=re.MULTILINE):
        if not block.startswith("### "):
            continue
        lines = block.splitlines()
        title = lines[0].strip()
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
        if len(quote) > SNIPPET:
            quote = quote[:SNIPPET].rstrip() + "…"
        items.append(f"{title}\n{quote}" if quote else title)
        if len(items) >= MAX_ITEMS:
            break
    return items


def build_message(raw: str, topic: str = "") -> str:
    """brief 全文 → 一封訊息。永遠回傳非空字串。"""
    head = _head_lines(raw)
    items = _items(raw)

    body = "\n".join(head).strip()
    if items:
        body = (body + "\n\n" + "\n\n".join(items)).strip()
    else:
        # 這題沒東西時要講清楚是「這題沒抓到」,不是系統掛了 ——
        # owner 靠這句分辨「來源沒回應」跟「早報壞掉」,沉默兩者長得一樣
        label = f"「{topic}」" if topic else "這題"
        note = f"⚠️ {label}這次沒抓到內容(來源可能全部無回應)。"
        body = (body + "\n\n" + note).strip() if body else note

    # 先扣掉頁尾再截,否則超長的時候被切掉的正好是那行免責聲明
    limit = BUDGET - len(FOOTER) - 4
    if len(body) > limit:
        body = body[:limit].rstrip() + " …(截斷)"

    return body + "\n\n" + FOOTER


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "brief.txt"
    topic = argv[2] if len(argv) > 2 else ""
    try:
        # errors="replace":brief 內容是抓回來的網路文字,萬一夾到一個非 UTF-8
        # byte,硬解會拋例外 → 這題變紅 → owner 收到一封假 FAILED。
        # 一個替代字元的雜訊,換一整題的早報。
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read().replace("\r", "")
    except OSError:
        raw = ""

    # 寫 bytes 而不是 text:後者要看執行環境的 stdout 編碼,
    # 替代字元 U+FFFD 在非 UTF-8 環境會再炸一次
    sys.stdout.buffer.write((build_message(raw, topic) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
