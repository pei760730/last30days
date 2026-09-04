@AGENTS.md

<!-- fork 專屬規則(upstream AGENTS.md 不動) -->

## ⚠️ 本 fork 紅線

- **別下 `--deep`**:會叫付費模型,本帳號實測 429(quota 沒開)。日常一律 `--emit brief`(cron 已固定);`--deep-research` 另有 API key 硬 gate 擋著,`--deep` 沒有 code 防呆——這行文件就是防線。
  - 本機另有 env 層封印(2026-07-25):`~/.config/last30days/.env` 已設 `LAST30DAYS_RERANK_MODEL=gemini-3.1-flash-lite`(override 優先於 deep→PRO 預設,providers.py `_resolve_model_pins`),就算誤下 `--deep` 也不會碰到付費 pro。此檔在 repo 外,重灌機器要重設。

## 同步上游的規約

上游是 `mvanhorn/last30days-skill`,更新很快(2026-09-04 時我們在 v3.21.1、上游 v3.23.0)。

**用真 merge,不要 squash。** 上一次同步(#35)是 squash 進來的,單親 commit,血緣沒接上,
後果是 `git rev-list --left-right --count upstream/main...main` 永遠虛胖:2026-09-04 那個
「落後 230 顆」拆開是 **216 顆早就進來的舊帳 + 14 顆真的新的**。血緣斷了,每次同步都得從零
re-diff,沒有 merge base 可用。

```bash
git remote add upstream https://github.com/mvanhorn/last30days-skill.git   # 只需一次
git fetch upstream
git merge upstream/main        # 真 merge:留下血緣,下次才有 merge base
```

**同步後一定要跑 `pytest tests/test_fork_ci_invariants.py`。** 這個 fork 對上游檔案的補丁
散在各處(workflow 的 `timeout-minutes`、OSV caller 的 `actions: read`、dependabot 的三個
生態系與雙週排程),每一條都是「被蓋掉之後不會有人發現」的類型——不是紅燈,是安靜地多燒六小時
額度、或讓每週掃描從此不再跑。那支測試就是把這些補丁變成會叫的守門,別把它跳過。

新增 fork 專屬的東西時,**優先開新檔案而不是改上游檔案**——新檔案永遠不會在同步時衝突。
