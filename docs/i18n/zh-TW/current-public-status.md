# Image-to-DNG RAW Generator 目前公開狀態

English public baseline: [docs/current-public-status.md](../../current-public-status.md)

本文件記錄目前可對外說明的工程基線，不包含內部規劃筆記、時程或未公開的優先順序。

## 目前能力

- 將 16-bit TIFF/PNG 或 scene-linear RGB 輸入轉成誠實標示來源的 synthetic DNG。
- 輸出未壓縮 16-bit 三通道 `LinearRaw` DNG。
- 輸出明確 opt-in 的單通道 simulated CFA DNG，支援 `rggb`、`bggr`、`grbg`、`gbrg`。
- 提供 deterministic synthetic sensor effects，供 demo 與 compatibility testing 使用。
- 可顯式覆寫 DNG `ColorMatrix1` 與 `AsShotNeutral` metadata，用於 virtual camera profile 實驗。
- 驗證並保存 `image2dng.semantic_scene.v1` semantic sidecars，並可在明確 opt-in 時執行已實作的 deterministic semantic reactions。
- 在 XMP 中嵌入 AI provenance、raw mode、simulated camera parameters 與已啟用的 sensor-effect 設定。
- 以 structural checks、mode-aware tag checks、XMP checks 與 optional local smoke tools 驗證產出的 DNG。
- 同時提供 CLI 與 public Python `convert()` API。

## 技術定位

本專案刻意不偽裝成真實相機 RAW 檔。產出的 DNG 會明確標示為 synthetic output，並避免 MakerNote spoofing。

`LinearRaw` 仍是 compatibility-first 的主要輸出路徑，因為它簡單、可檢查，也避免對 sensor capture 做出不實宣稱。simulated CFA 路徑則是明確 opt-in，定位在 workflow research、compatibility testing 與可控 demo。sensor effects 只是簡化的 synthetic control，不是完整物理相機模型。

## 對外介面

主要入口：

- CLI：`uv run image2dng input.tif output.dng`
- Validator：`uv run image2dng validate output.dng --json`
- Semantic sidecar validator：`uv run image2dng validate-semantic scene.semantic.json --json`
- Semantic reaction registry：`uv run image2dng semantic-reactions --json`
- Python API：`from image2dng import convert`
- Demo generator：`uv run python scripts/generate_demo_samples.py --output-dir demo-output`

參考文件：

- [README.md](../../../README.md)
- [docs/design.md](../../design.md)
- [docs/compatibility.md](../../compatibility.md)
- [docs/demo.md](../../demo.md)

## 驗證基線

目前高訊號的檢查包括：

- `uv run pytest`
- `uv run ruff check`
- `uv build`
- wheel install smoke for `image2dng --help`, `image2dng validate --help`, `image2dng validate-semantic --help`, and `image2dng semantic-reactions --help`
- 對 LinearRaw、CFA 與 CFA with sensor effects 進行 demo sample generation 與 validation
