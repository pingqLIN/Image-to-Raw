# Image-to-DNG RAW Generator

`image2dng` 是一個原型 CLI 與 Python library，可將 16-bit TIFF/PNG 或 scene-linear RGB 影像轉成誠實標示來源的 synthetic DNG。現階段 MVP 會寫出未壓縮 16-bit `LinearRaw` DNG，在自訂 XMP namespace 中嵌入 AI provenance，並避免 MakerNote spoofing。

專案方向正在從單次「image-to-raw 轉換」擴展成 **RAW-native AI image generation**：生成流程的主要輸出應該是 synthetic RAW/DNG，JPEG/PNG 則是由 RAW buffer render 出來的預覽或交付副產品。repo 內已提供最小節點式 pipeline，用來驗證 Prompt/Scene/Virtual Camera/Sensor/DNG/JPEG/Validation 這條流程。

本專案不嘗試偽裝成真實相機 RAW 檔。產生的 DNG 使用 `UniqueCameraModel = "Synthetic Camera v1"`，且 XMP metadata 會標示相機參數為 simulated。

This product includes DNG technology under license by Adobe.

## 開發安裝

```powershell
uv sync --extra dev
```

## 產生節點式 RAW-native 批次

```powershell
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
```

這個批次流程會產生：

- scene-linear TIFF 中間檔；
- `LinearRaw` synthetic DNG；
- simulated RGGB CFA synthetic DNG；
- 由 DNG buffer tone-map 出來的 JPEG 預覽；
- 每個 DNG 的 validation JSON；
- `manifests/raw-native-node-batch.json` 節點流程 manifest；
- `manifests/sample-index.json` 樣片索引。

目前決策是先自建 repo 內的最小核心 pipeline。ComfyUI 適合作為後續視覺化編排、工作流 UI 或 custom node 整合層，但不先成為核心 RAW/DNG 語意的必要依賴。

## 執行開發基線驗證

```powershell
uv run python scripts/verify_development_baseline.py --output-dir demo-output/development-baseline
```

這個驗證流程會執行 `pytest`、`ruff check`、RAW-native batch generation，並檢查 manifest、sample index、DNG validation JSON 與 JPEG preview。結果會寫入 `demo-output/development-baseline/verification-report.json`。`demo-output/` 是本機輸出資料夾，不應提交 binary 樣片。

## 產生相容性證據矩陣

```powershell
uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence
```

這個流程會產生 deterministic DNG fixtures、validation JSON、`compatibility-report.json` 與 `compatibility-summary.md`。optional RAW tools 不存在時會記錄 `skipped`，不會造成失敗；Adobe DNG SDK 目前維持 manual-only。

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

validator 會檢查必要 DNG tags、XMP 可解析性、black/white level、影像幾何、synthetic provenance，以及 MakerNote absence。若 `exiftool`、`dcraw`、`darktable-cli` 或 `rawtherapee-cli` 存在於 `PATH`，也會執行 optional smoke tests。

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
    overwrite=False,
)
```

public API 會丟出 `Image2DNGError` 子類別，而不是結束整個 process。CLI 與 library API 產出應在 `image2dng validate` 下語意等價。

## 產生示範樣張

```powershell
uv run python scripts/generate_demo_samples.py --output-dir demo-output
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
uv run python scripts/generate_visual_demo.py --output-dir demo-output/visual-demo
```

架構 demo、sample set 與應用情境見 [docs/demo.md](docs/demo.md)。

## 測試

```powershell
uv run pytest
uv run ruff check
```

## 範圍

目前 MVP：

- 16-bit uncompressed LinearRaw DNG。
- 明確 opt-in 的 simulated CFA mosaic mode。
- 內建最小 RAW-native node pipeline，可產生 DNG、JPEG preview、validation JSON 與 graph manifest。
- 可選 deterministic synthetic sensor effects，供 demo 與 compatibility testing 使用。
- RGB input normalization 與 simple virtual camera transform。
- XMP custom namespace：`https://example.org/ns/xmp/ai/1.0/`。
- 永遠寫入 synthetic provenance。

已知限制：

- Sensor effects 是簡化 synthetic controls，不是物理相機模型。
- 尚未支援 preview IFD、EXIF IFD、semantic mask IFD、depth IFD，或 `DNGPrivateData` payload。
- 相容性目前以結構驗證與 optional local smoke tools 為主，尚未納入 Adobe DNG SDK 自動驗證。

設計說明見 [docs/design.md](docs/design.md)。
節點式 RAW-native 方向見 [docs/i18n/zh-TW/raw-native-node-pipeline.md](docs/i18n/zh-TW/raw-native-node-pipeline.md)。
