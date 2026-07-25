@AGENTS.md

<!-- fork 專屬規則(upstream AGENTS.md 不動) -->

## ⚠️ 本 fork 紅線

- **別下 `--deep`**:會叫付費模型,本帳號實測 429(quota 沒開)。日常一律 `--emit brief`(cron 已固定);`--deep-research` 另有 API key 硬 gate 擋著,`--deep` 沒有 code 防呆——這行文件就是防線。
  - 本機另有 env 層封印(2026-07-25):`~/.config/last30days/.env` 已設 `LAST30DAYS_RERANK_MODEL=gemini-3.1-flash-lite`(override 優先於 deep→PRO 預設,providers.py `_resolve_model_pins`),就算誤下 `--deep` 也不會碰到付費 pro。此檔在 repo 外,重灌機器要重設。
