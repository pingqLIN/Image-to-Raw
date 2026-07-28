# Compatibility Evidence 流程

English public baseline: [docs/compatibility.md](../../compatibility.md)

本文件是 compatibility evidence 的 Traditional Chinese source manuscript，已更新至 Phase 6 RAW processor evidence policy。

## 目的

`image2dng` 產生的是 synthetic DNG。Phase 6 的目標不是保證所有 RAW processor 都完整支援，而是建立可重跑、可追蹤、可外部審查的 evidence matrix，明確區分：

- `image2dng validate` structural validation；
- optional RAW processor command/export evidence；
- Adobe DNG SDK local-only/manual-resource validation；
- 工具不存在時的 `skipped` 狀態。

目前 required DNG tags 與 mode-specific contract 見 [DNG tag contract](dng-tag-contract.md)。Compatibility evidence 應依照這份 contract 解讀：缺少 optional tool 是環境狀態；缺少必要 tag 則是 structural failure。

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
- `compatibility-summary.md`：人工可讀 evidence matrix 與 fixture integrity 表，列出每個 fixture 的 DNG / validation JSON byte count 與 SHA-256。

`demo-output/` 是本機輸出，不應提交 binary fixtures。

## RAW processor setup audit

```powershell
uv run python scripts/audit_raw_processor_setup.py --output-dir demo-output/raw-processor-setup-audit
```

這個流程是 dry-run setup audit，供人或外部代理審查：

- 偵測 `dcraw`、`darktable-cli`、`rawtherapee-cli` 目前是否可用。
- Windows 上的 Darktable 與 RawTherapee 會先查 PATH，再查標準安裝位置 `C:\Program Files\darktable\bin\darktable-cli.exe` 與 `C:\Program Files\RawTherapee\5.12\rawtherapee-cli.exe`，report 會記錄 discovery source。
- 在本機有 winget、Scoop、Chocolatey 時，記錄 package-manager search evidence。
- 只有 package-manager output 含有精確 package identity match 時，才記錄 version hint。
- 產出 `setup-audit-report.json`、`setup-runbook.md`、`external-review-prompt.md`。
- 不會安裝、不會升級任何 RAW processor。

若使用者後續批准安裝其中一個工具，再重跑 setup audit、compatibility evidence 與 review bundle，讓安裝前決策與安裝後 evidence 清楚分開。

## Adobe DNG SDK local-only/manual-resource validation

Adobe DNG SDK 目前維持 local-only/manual-resource 模式，不作為 CI gate，也不由本專案腳本自動下載、安裝、解壓或更新。

若本機尚未準備 SDK 或 validator，建議人工準備流程：

1. 從 Adobe DNG 官方頁面確認目前 SDK 與 specification 版本。
2. 由使用者明確批准後，在本機隔離位置準備 SDK 或 validator build。
3. 對 `demo-output/review-bundle-*/artifacts/representative-dng/` 中的代表性 DNG 執行 SDK validation。
4. 將命令、SDK 版本、fixture、exit code、stdout/stderr 摘要與環境寫入 local-only notes。
5. 驗證後重跑：

```powershell
uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
```

若本機已經有可執行的 `dng_validate.exe`，可使用本 repo 的 local-only runner 產生可重跑 report：

```powershell
uv run python scripts/run_adobe_dng_sdk_validation.py `
  --validator Adobe/dng_sdk_1_7_1/dng_sdk/targets/win/release64_x64/dng_validate.exe `
  --fixture-dir demo-output/review-bundle-phase6/artifacts/representative-dng `
  --output-dir demo-output/adobe-dng-sdk-validation
```

若要同時收斂 Adobe resource audit、project DNG fixture generation、Adobe DNG Converter regression 與 SDK validation，可使用：

```powershell
uv run python scripts/verify_adobe_validation_stack.py --dry-run-converter
uv run python scripts/verify_adobe_validation_stack.py --output-dir demo-output/adobe-validation-stack
```

`--dry-run-converter` 只證明 fixture 與報告路徑，不代表完整 Adobe readiness；正式 evidence 應在使用者已準備 converter 與 validator 後重跑一次。

限制：

- SDK validation path 必須 local-configurable，不應 hard-code 私人機器路徑。
- `compatibility-report.json` 中的 Adobe DNG SDK entry 仍維持 `manual-only`；可重現的本機 SDK evidence 應由 dedicated local scripts 產出，不混入一般 optional RAW processor matrix。
- 若需要產生本機 SDK evidence，可使用 `scripts/run_adobe_dng_sdk_validation.py` 或外層 `scripts/verify_adobe_validation_stack.py`，輸出仍應留在 ignored `demo-output/` 或 local-only notes。
- 若 SDK 或 Adobe tool 回報 DNG 結構問題，該結果應升級成下一輪 Phase 6 blocking compatibility finding。

## Adobe DNG Converter regression

Adobe DNG Converter 可用獨立腳本做本機回歸，不混入一般 optional RAW processor matrix：

```powershell
uv run python scripts/verify_adobe_dng_converter.py --dry-run
uv run python scripts/verify_adobe_dng_converter.py --output-dir demo-output/adobe-dng-converter-verification
```

此腳本會產生 deterministic `single-raw-ifd` fixture，先以 `image2dng` contract validator 檢查本專案輸出的來源 DNG，再尋找 Adobe DNG Converter 並執行轉換。轉換後 artifact 以 Adobe-converted inspection 檢查，不要求完全符合本專案 writer contract。

Validation 分層如下：

- `image2dng` contract validation：針對本專案直接輸出的 DNG，嚴格檢查必要 DNG tag、XMP provenance、raw buffer 幾何與 byte count。
- external processor smoke validation：針對 optional RAW tools，確認工具是否接受 DNG 並輸出 artifact。
- Adobe-converted artifact inspection：針對 Adobe DNG Converter 改寫後的 DNG，只檢查可解析 DNG 結構、raw IFD、camera identity 與基本幾何；允許 Adobe 重新壓縮或重排 metadata。

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
- Adobe DNG SDK 維持 local-only/manual-resource 模式，不作為 CI gate；`compatibility-report.json` 的 matrix entry 仍維持 `manual-only`，直到有可重現的公開 gate 政策。

## Report Schema

`compatibility-report.json` 使用：

```text
image2dng.compatibility_evidence.v2
```

主要欄位：

- `environment`：平台、Python、image2dng 版本。
- `tools`：工具 availability、version command、version、timeout、dry-run install hint。
- `install_policy`：不自動安裝、missing tool policy、available tool failure policy。
- `fixtures`：每個 fixture 的 input、DNG、DNG byte count、DNG SHA-256、DNG layout、raw IFD location、IFD0 preview flag、validation JSON、validation JSON byte count、validation JSON SHA-256、validation status、processor result records。
- `matrix`：fixture/tool/result/evidence/notes/exit code/duration/output artifacts/missing output artifacts evidence matrix。
- `compatibility-summary.md`：除了 tool evidence matrix，也呈現 fixture DNG 與 validation JSON 的 byte count / SHA-256，方便人工審查時不用先打開 JSON report 才能核對 artifact integrity。
- `ok` / `errors`：整體 gate 結果。
