# Image-to-DNG RAW Generator

[English](README.md)

![Status](https://img.shields.io/badge/status-prototype-orange)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-proprietary%20prototype-lightgrey)

[功能](#功能摘要) · [安裝](#開發安裝) · [相容性證據](#產生相容性證據矩陣) · [驗證](#驗證-dng) · [測試](#測試) · [License](#prototype-license-status)

> 將 scene-linear 或 16-bit RGB 影像轉成誠實標示來源的 synthetic DNG/RAW research artifact。

`image2dng` 是一個原型 CLI 與 Python library，可將 16-bit TIFF/PNG 或 scene-linear RGB 影像轉成誠實標示來源的 synthetic DNG。現階段 MVP 會寫出未壓縮 16-bit `LinearRaw` raw image data，預設 DNG layout 會加入 IFD0 JPEG preview，在自訂 XMP namespace 中嵌入 AI provenance，並避免 MakerNote spoofing。

專案方向正在從單次「image-to-raw 轉換」擴展成 **RAW-native AI image generation**：生成流程的主要輸出應該是 synthetic RAW/DNG，JPEG/PNG 則是由 RAW buffer render 出來的預覽或交付副產品。repo 內已提供最小節點式 pipeline，用來驗證 Prompt/Scene/Virtual Camera/Sensor/DNG/JPEG/Validation 這條流程。

本專案不嘗試偽裝成真實相機 RAW 檔。產生的 DNG 使用 `UniqueCameraModel = "Synthetic Camera v1"`，且 XMP metadata 會標示相機參數為 simulated。

This product includes DNG technology under license by Adobe.

---

## 功能摘要

| Capability | Status | Notes |
| --- | --- | --- |
| Synthetic LinearRaw DNG | MVP | 16-bit uncompressed DNG with explicit synthetic provenance |
| Simulated CFA mode | Available | Explicit opt-in Bayer mosaic for workflow and compatibility research |
| Embedded DNG preview | Experimental | Default DNG layout writes IFD0 JPEG preview plus raw SubIFD |
| RAW-native node batch | Available | Generates DNG, sidecar JPEG preview, validation JSON, and graph manifests |
| Semantic scene sidecar v1 | Available | Validates and preserves external scene semantics; optional region-exposure reaction prototype |
| Compatibility evidence | Available | Structural validation plus optional ExifTool/Darktable/RawTherapee smoke evidence |
| Review bundle | Available | Local-only package with contact sheets, representative DNGs, validation JSON, and manifests |

---

## 開發安裝

```powershell
uv sync --extra dev
```

## 產生節點式 RAW-native 批次

```powershell
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
```

若目標是把輸出的 DNG 再交給 Adobe DNG Converter，請改用單一 raw IFD layout：

```powershell
uv run python scripts/generate_raw_native_batch.py `
  --output-dir demo-output/raw-native-node-batch-adobe `
  --dng-layout single-raw-ifd
```

這個批次流程會產生：

- scene-linear TIFF 中間檔；
- `LinearRaw` synthetic DNG；
- simulated RGGB CFA synthetic DNG；
- DNG 內嵌 IFD0 JPEG preview，並保留 raw data 於 Raw SubIFD；
- 由 DNG raw buffer tone-map 出來的 sidecar JPEG 預覽；
- 每個 DNG 的 validation JSON；
- `manifests/raw-native-node-batch.json` 節點流程 manifest；
- `manifests/sample-index.json` 樣片索引。

目前決策是先自建 repo 內的最小核心 pipeline。ComfyUI / Stable Diffusion 整合已分割到 sibling bridge 專案，核心 repo 只保留 generic external scene-linear manifest 與 RAW/DNG 語意。

外部 renderer、AI generator 或 simulator 已可透過 scene-linear producer boundary 交付輸入：

```powershell
uv run python scripts/generate_raw_native_batch.py `
  --output-dir demo-output/external-scene-linear-batch `
  --scene-linear path\to\scene-linear.tif
```

若需要每張圖的 producer、prompt、lighting 或 semantic sidecar metadata，使用 manifest：

```json
{
  "schema": "image2dng.external_scene_linear_sources.v1",
  "scenes": [
    {
      "slug": "renderer-frame-001",
      "path": "renderer-frame-001.tif",
      "input_space": "linear-rec709",
      "producer": "external renderer",
      "prompt": "studio material test",
      "description": "scene-linear output from an upstream generator",
      "lighting": "virtual D65 studio",
      "semantic_manifest": "renderer-frame-001.semantic.json"
    }
  ]
}
```

## ComfyUI / Stable Diffusion bridge

ComfyUI / Stable Diffusion 專屬 importer 已分割到 sibling project：

[image-to-raw-comfyui-sd-bridge](../image-to-raw-comfyui-sd-bridge/README.zh-tw.md)

原本位於本 repo 的 `scripts/import_comfyui_output.py` 與 `image2dng.comfyui_importer` 已搬到該 bridge 專案。新的 CLI 是：

```powershell
uv run image2dng-comfyui-import `
  path\to\ComfyUI_00002_.png `
  --output-dir demo-output/comfyui-import `
  --run-pipeline
```

本核心 repo 只接收 bridge 或其他外部 producer 交付的 `image2dng.external_scene_linear_sources.v1` manifest，並負責 scene-linear input、semantic sidecar、DNG writer、validation 與 RAW-native batch。ComfyUI workflow metadata 仍以 `producer_metadata` / `producer_metadata_manifest` 進入 manifest，但不再是核心 package 的內建 API。

`semantic_manifest` 若使用 `image2dng.semantic_scene.v1`，會在 DNG 產生前被驗證，sidecar 與可解析的 local assets 會被複製並寫入 batch manifest / sample index。預設仍只做 preservation + validation；若 manifest 明確設定 `apply_semantic_reaction: true`，可啟用 deterministic `region-exposure-mask-v1` prototype，使用 region mask 與 `exposure_bias_ev` 影響 16-bit scene-linear RGB values。此 prototype 不是完整物理 sensor model。詳細格式見 [docs/i18n/zh-TW/semantic-scene-sidecar-contract.md](docs/i18n/zh-TW/semantic-scene-sidecar-contract.md)。

## 執行開發基線驗證

```powershell
uv run python scripts/verify_development_baseline.py --output-dir demo-output/development-baseline
```

這個驗證流程會執行 `pytest`、`ruff check`、RAW-native batch generation，並檢查 manifest、sample index、DNG validation JSON 與 JPEG preview。結果會寫入 `demo-output/development-baseline/verification-report.json`。`demo-output/` 是本機輸出資料夾，不應提交 binary 樣片。

## 產生相容性證據矩陣

```powershell
uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence
```

這個流程會產生 deterministic DNG fixtures、validation JSON、`compatibility-report.json` 與 `compatibility-summary.md`。Phase 6 report schema 為 `image2dng.compatibility_evidence.v2`，會記錄 RAW processor command、exit code、stdout/stderr tail、輸出 artifact 與 dry-run install hints。缺少 optional RAW tools 時會記錄 `skipped`，不會造成失敗；已安裝工具若執行或輸出失敗會記錄 `failed`；Adobe DNG SDK 目前維持 manual-only。

Adobe DNG Converter regression 使用獨立本機腳本：

```powershell
uv run python scripts/verify_adobe_dng_converter.py --dry-run
uv run python scripts/verify_adobe_dng_converter.py --output-dir demo-output/adobe-dng-converter-verification
```

此流程會分開三層驗證：本專案輸出來源 DNG 使用嚴格 `image2dng` contract validation；optional tools 使用 external processor smoke checks；Adobe DNG Converter 改寫後的檔案使用較寬鬆的 Adobe-converted artifact inspection。

## 產生 demo review bundle

```powershell
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
```

這個流程會重跑 visual demo、RAW-native node batch、development baseline 與 compatibility evidence，並把外部審查需要的 contact sheets、代表性 DNG、validation JSON、reports 與 manifests 收斂到 `demo-output/review-bundle/`。人讀入口是 `index.md`，機器可讀 manifest 是 `review-bundle-report.json`。`demo-output/` 仍是本機輸出資料夾，不應提交 binary 樣片。

## 產生 RAW processor setup audit

```powershell
uv run python scripts/audit_raw_processor_setup.py --output-dir demo-output/raw-processor-setup-audit
```

這個 dry-run audit 會偵測 `dcraw`、`darktable-cli`、`rawtherapee-cli` 的目前可用狀態與 package-manager search evidence，產出 `setup-audit-report.json`、`setup-runbook.md` 與 `external-review-prompt.md`。Windows 上的 Darktable 會先查 PATH，再查標準安裝位置 `C:\Program Files\darktable\bin\darktable-cli.exe`。version hint 只會在 package-manager output 精確命中 package identity 時記錄。它不會安裝或更新任何工具；安裝其中一個 RAW processor 必須等使用者明確批准，安裝後需重跑 setup audit、compatibility evidence 與 review bundle。

## 產生 DNG

```powershell
uv run image2dng input.tif output.dng `
  --input-space srgb `
  --mode linearraw `
  --iso 100 `
  --white-balance 6500 `
  --prompt-hash sha256:... `
  --scene-description "synthetic test scene" `
  --model-name "Example Model" `
  --model-version "1.0"
```

CLI 預設不會覆寫既有輸出檔。只有在確定要替換輸出時才傳入 `--overwrite`。

預設 DNG layout 為 `preview-subifd`：IFD0 是 JPEG-compressed RGB preview，raw image data 寫在 Raw SubIFD。需要回到舊版單一 raw IFD layout 時，可傳入：

```powershell
uv run image2dng input.tif output.dng --dng-layout single-raw-ifd
```

支援的輸入色彩空間：

- `srgb`：display-referred sRGB；CLI 會套用 inverse sRGB OETF。
- `linear-rec709`：scene-linear Rec.709/sRGB primaries。
- `acescg`：scene-linear ACEScg/AP1，經 XYZ 轉換到 virtual camera space。
- `xyz`：scene-linear CIE XYZ，轉換到 virtual camera space。
- `prophoto-rgb`：encoded ProPhoto RGB / ROMM-style 1.8 transfer，並從 D50 adapted 到 D65。

支援的輸出模式：

- `linearraw`：預設三通道 16-bit LinearRaw DNG。
- `cfa`：明確 opt-in 的 simulated single-channel CFA mosaic DNG。

CFA 輸出範例：

```powershell
uv run image2dng input.tif output-cfa.dng `
  --mode cfa `
  --cfa-pattern rggb `
  --input-space linear-rec709
```

CFA mode 是為相容性與工作流研究設計的簡化模擬。它不宣稱是真實 sensor capture，且預設不加入 sensor noise。

## 驗證 DNG

```powershell
uv run image2dng validate output.dng
uv run image2dng validate output.dng --json
```

validator 會檢查必要 DNG tags、XMP 可解析性、black/white level、影像幾何、synthetic provenance，以及 MakerNote absence。現行 tag contract 見 [docs/i18n/zh-TW/dng-tag-contract.md](docs/i18n/zh-TW/dng-tag-contract.md)。若 `exiftool`、`dcraw`、`darktable-cli` 或 `rawtherapee-cli` 可由 PATH 或 adapter 支援的 common install path 找到，也會執行 optional smoke tests。

驗證 exit codes：

- `0`：結構驗證通過；optional smoke tools 通過或被跳過。
- `1`：DNG 結構驗證失敗。
- `2`：optional smoke tool 有執行，但回報實際 parse/open failure。
- `3`：CLI 使用方式或設定錯誤。

## Python library

```python
from pathlib import Path

from image2dng import convert

result = convert(
    input_path=Path("input.tif"),
    output_path=Path("output.dng"),
    input_space="srgb",
    mode="linearraw",
    cfa_pattern="rggb",
    iso=100,
    white_balance_kelvin=6500,
    shot_noise=0.0,
    read_noise=0.0,
    row_noise=0.0,
    sensor_effect_seed=None,
    prompt_hash="sha256:...",
    scene_description="synthetic test scene",
    dng_layout="preview-subifd",
    overwrite=False,
)
```

public API 會丟出 `Image2DNGError` 子類別，而不是結束整個 process。CLI 與 library API 產出應在 `image2dng validate` 下語意等價。

## 產生示範樣張

```powershell
uv run python scripts/generate_demo_samples.py --output-dir demo-output
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
uv run python scripts/generate_visual_demo.py --output-dir demo-output/visual-demo
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
uv run python scripts/audit_raw_processor_setup.py --output-dir demo-output/raw-processor-setup-audit
```

架構 demo、sample set 與應用情境見 [docs/demo.md](docs/demo.md)。

## 測試

```powershell
uv run pytest
uv run ruff check
```

## 🤖 AI-Assisted Development

本專案部分開發流程使用 AI 協助。

| Model | Role |
| --- | --- |
| OpenAI Codex CLI | Primary implementation, compatibility evidence workflow, documentation review |

> ⚠️ **Disclaimer:** AI 輔助產出的程式碼與文件已經過作者與本機測試流程檢查，但仍不保證其正確性、安全性或適用於特定用途。請自行評估後使用。

## Prototype License Status

目前授權狀態維持 `Proprietary prototype`，與 `pyproject.toml` 一致。本 repository 的公開文件僅描述目前 proof-of-concept 能力與驗證方式；除非另有明確授權文件，不應假設本專案以 MIT 或其他開源授權釋出。

## 範圍

目前 MVP：

- 16-bit uncompressed LinearRaw DNG。
- 明確 opt-in 的 simulated CFA mosaic mode。
- 預設 `preview-subifd` DNG layout：IFD0 JPEG preview 加上 Raw SubIFD。
- 內建最小 RAW-native node pipeline，可產生 DNG、sidecar JPEG preview、validation JSON 與 graph manifest。
- `image2dng.semantic_scene.v1` sidecar validation / preservation，以及 opt-in `region-exposure-mask-v1` reaction prototype。
- 可選 deterministic synthetic sensor effects，供 demo 與 compatibility testing 使用。
- RGB input normalization 與 simple virtual camera transform。
- XMP custom namespace：`https://example.org/ns/xmp/ai/1.0/`。
- 永遠寫入 synthetic provenance。

已知限制：

- Sensor effects 是簡化 synthetic controls，不是物理相機模型。
- Embedded preview 目前是 IFD layout experiment，不代表完整 Adobe 相容承諾。
- 尚未支援 EXIF IFD、semantic mask IFD、depth IFD，或 `DNGPrivateData` payload。
- 相容性目前以結構驗證與 optional local smoke tools 為主，尚未納入 Adobe DNG SDK 自動驗證。

設計說明見 [docs/design.md](docs/design.md)。
目前 DNG tag contract 見 [docs/i18n/zh-TW/dng-tag-contract.md](docs/i18n/zh-TW/dng-tag-contract.md)。
節點式 RAW-native 方向見 [docs/i18n/zh-TW/raw-native-node-pipeline.md](docs/i18n/zh-TW/raw-native-node-pipeline.md)。
