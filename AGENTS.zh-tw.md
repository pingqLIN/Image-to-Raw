# AGENTS.zh-tw.md

`image-to-raw` 專案層級 AI agent 指令的繁體中文原始母檔。

## 0. 適用範圍

- 本檔僅適用於 `Q:\Projects\image-to-raw`。
- 本檔補充全域與工作區層級的 `AGENTS.md`，不取代其安全與執行規則。
- Agent 指令檔不要在同一份文件中逐條中英雙寫；需要多語版本時，使用分檔維護。
- `AGENTS.zh-tw.md` 是本專案 repo-local agent 指令的繁體中文原始母檔。
- `AGENTS.md` 是英文 canonical baseline，也是多數 agent 預期會自動讀取的檔案。
- 更新本專案 agent 指令時，先修改 `AGENTS.zh-tw.md`，再同步更新英文 `AGENTS.md`。

## 1. 文件語言與 i18n

- 重要且面向人類讀者的專案文件，必須先以繁體中文撰寫原始母檔。
- 重要專案文件以 `zh-TW` 文件作為原始母稿。
- 英文文件仍可作為公開交換、GitHub 呈現、生態系整合與外部協作所需的 canonical baseline。
- 當繁體中文與英文版本同時存在時，英文版應由繁體中文母檔翻譯而來；除非使用者明確要求不同工作流。
- 不強制每一份文件都建立雙語成對檔案；只有在需要翻譯版或對外版本時才建立或維護成對檔案，尤其是已有繁體中文母檔時。
- 多語文件應使用符合 i18n 的檔案配置，不要在同一份文件中混雜多個完整語言版本。

建議配置：

```text
README.zh-tw.md            # 繁體中文原始母檔
README.md                  # 英文 canonical exchange/public baseline
docs/i18n/zh-TW/<slug>.md  # 繁體中文原始母檔
docs/i18n/en/<slug>.md     # 英文 canonical translation
docs/i18n/<locale>/<slug>.md
```

- repo root 的 `README.md` 可維持為 GitHub 與外部協作所需的英文 canonical exchange 版本，但內容應由 `README.zh-tw.md` 翻譯而來。
- 若某份英文文件後續升格為重要專案文件，後續大幅修改前應先建立繁體中文母檔。
- 若既有英文文件已被外部使用，先維持其穩定性，再補回繁體中文母檔。
- 若已有繁體中文母檔，且英文或對外版本有實際用途，則維持兩份檔案成對並有意識地同步更新。

## 2. AI 開發紀錄

- 主要用途為 AI 開發過程紀錄、review、確認、交接或討論的文件，預設以英文撰寫。
- 範例包含 AI 產生的 status report、implementation notes、review notes、handoff notes 與 planning discussion artifacts。
- 這類 AI 開發紀錄不需要成對建立繁體中文檔案，除非後續升格為面向使用者或定義專案方向的重要文件。
- 若 AI 開發紀錄後續成為面向使用者或定義專案方向的重要文件，應提升到上述 i18n 文件架構中管理。
