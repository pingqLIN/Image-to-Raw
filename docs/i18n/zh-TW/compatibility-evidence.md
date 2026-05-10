# Compatibility Evidence 流程

本文件是 compatibility evidence 的 Traditional Chinese source manuscript，已更新至 Phase 6 RAW processor evidence policy。

## 目的

`image2dng` 產生的是 synthetic DNG。Phase 6 的目標不是保證所有 RAW processor 都完整支援，而是建立可重跑、可追蹤、可外部審查的 evidence matrix，明確區分：

- `image2dng validate` structural validation；
- optional RAW processor command/export evidence；
- Adobe DNG SDK manual-only validation；
- 工具不存在時的 `skipped` 狀態。

## 產生命令

```powershell
uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence
```

輸出：

- `inputs/*.tif`：deterministic 16-bit fixture inputs。
- `raw/*.dng`：LinearRaw、CFA、sensor-effect DNG fixtures。
- `validation/*.json`：每個 DNG 的 validation report。
- `processor-output/*`：RAW processor export/open smoke 輸出，僅在工具存在且成功時產生。
- `compatibility-report.json`：machine-readable evidence report。
- `compatibility-summary.md`：人工可讀 evidence matrix。

`demo-output/` 是本機輸出，不應提交 binary fixtures。

## Fixture Set

第一版固定產生：

- `srgb-gradient-linearraw`
- `linear-rec709-gradient-linearraw`
- `acescg-gradient-linearraw`
- `xyz-gradient-linearraw`
- `prophoto-rgb-chart-linearraw`
- `linear-rec709-cfa-rggb`
- `linear-rec709-cfa-bggr`
- `linear-rec709-cfa-grbg`
- `linear-rec709-cfa-gbrg`
- `linear-rec709-cfa-rggb-noisy`
- `linear-rec709-linearraw-noisy`

## Optional Tool Policy

- Structural validation 是必要 gate。
- `exiftool`、`dcraw`、`darktable-cli`、`rawtherapee-cli` 是 optional RAW processor tools。
- optional tool 不存在時記錄 `skipped: not found`，不造成失敗。
- optional tool 若實際執行但 parse/open/export 失敗，或成功 exit 但沒有產出預期 artifact，report 會標示 failure，script exit code 也會是 non-zero。
- 本流程只輸出 dry-run install hints，不會自動安裝任何 RAW processor。
- Adobe DNG SDK 目前維持 `manual-only`，不作為 CI gate。

## Report Schema

`compatibility-report.json` 使用：

```text
image2dng.compatibility_evidence.v2
```

主要欄位：

- `environment`：平台、Python、image2dng 版本。
- `tools`：工具 availability、version command、version、timeout、dry-run install hint。
- `install_policy`：不自動安裝、missing tool policy、available tool failure policy。
- `fixtures`：每個 fixture 的 input、DNG、validation JSON、validation status、processor result records。
- `matrix`：fixture/tool/result/evidence/notes/exit code/duration/output artifacts evidence matrix。
- `ok` / `errors`：整體 gate 結果。
