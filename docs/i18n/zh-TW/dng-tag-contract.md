# DNG Tag Contract

本文件是 `image2dng` 目前輸出的 DNG tag contract。它描述的是已實作並由 validator / tests 檢查的行為，不是未來 roadmap。

本專案產生 truthful synthetic DNG，不偽裝成真實相機 RAW。所有輸出都應保留 synthetic provenance，且不得寫入 MakerNote。

## DNG Layout

預設 layout 是 `preview-subifd`：

- IFD0 是 JPEG-compressed RGB preview，`NewSubFileType = 1`。
- IFD0 透過 `SubIFDs` 指向主 raw image IFD。
- Raw SubIFD 保留主要 raw data 與完整 raw tag contract，`NewSubFileType = 0`。

相容性或回歸測試需要時，仍可用 `single-raw-ifd` layout 寫出舊版單一 raw IFD。validator 會尋找 `NewSubFileType = 0` 的 raw page，而不是假設 `pages[0]` 一定是 raw image。

## 共同必要 Raw Tags

LinearRaw 與 simulated CFA 輸出都必須包含：

| Tag | 預期值 / 政策 |
| --- | --- |
| `NewSubFileType` | `0`，主 raw image |
| `DNGVersion` | `1.4.0.0` |
| `DNGBackwardVersion` | `1.1.0.0` |
| `Make` | `image2dng` |
| `Model` | `Synthetic Camera v1` |
| `UniqueCameraModel` | `Synthetic Camera v1` |
| `Orientation` | `1` |
| `ImageWidth` / `ImageLength` | 由輸入影像尺寸決定 |
| `BitsPerSample` | `16`；LinearRaw 為三通道，CFA 為單通道 |
| `Compression` | `1`，uncompressed |
| `BlackLevelRepeatDim` | LinearRaw: `1,1`；CFA: `2,2` |
| `BlackLevel` | LinearRaw: 3 個值；CFA: 4 個值 |
| `WhiteLevel` | LinearRaw: 3 個值；CFA: 1 個值 |
| `DefaultScale` | `1/1, 1/1` |
| `ActiveArea` | `0,0,height,width` |
| `DefaultCropOrigin` | `0,0` |
| `DefaultCropSize` | `width,height` |
| `ColorMatrix1` | virtual camera 的 XYZ-to-native matrix；必須是 9 個 rational values |
| `AsShotNeutral` | normalized to green 的 white-balance neutral；必須是 3 個正 rational values |
| `CalibrationIlluminant1` | `21` (`D65`) |
| `RawDataUniqueID` | 16-byte deterministic ID derived from the raw image buffer |
| `Software` | `image2dng <version>` |
| `XMP` | synthetic AI provenance packet |

IFD0 preview 也會寫入基本 identity/provenance tags：`DNGVersion`、`DNGBackwardVersion`、`Make`、`Model`、`UniqueCameraModel`、`Orientation`、`Software` 與 `XMP`。preview 本身是 8-bit RGB JPEG-compressed image，不是 raw data。

## LinearRaw Mode

`linearraw` mode 會寫入三通道主影像：

| Tag | 預期值 |
| --- | --- |
| `PhotometricInterpretation` | `34892` (`LinearRaw`) |
| `SamplesPerPixel` | `3` |
| `BitsPerSample` | `16,16,16` |
| `PlanarConfiguration` | chunky/contiguous |

## Simulated CFA Mode

`cfa` mode 會寫入單通道 simulated Bayer mosaic。這是一個明確 opt-in 的 workflow 與 compatibility research mode，不是真實 sensor capture 宣稱。

| Tag | 預期值 |
| --- | --- |
| `PhotometricInterpretation` | `32803` (`ColorFilterArray`) |
| `SamplesPerPixel` | `1` |
| `CFARepeatPatternDim` | `2,2` |
| `CFAPattern` | Four entries matching the selected Bayer pattern |
| `CFAPlaneColor` | `0,1,2` |
| `CFALayout` | `1` |

支援的 Bayer patterns：

- `rggb`
- `bggr`
- `grbg`
- `gbrg`

## Provenance 與 Safety Contract

- XMP 必須包含 `xmpAI:provenanceType="synthetic"`。
- XMP 必須包含 `xmpAI:cameraParametersAreSimulated="True"`。
- `xmpAI:rawMode` 記錄 `linearraw` 或 `cfa`。
- `xmpAI:cfaPattern` 只在 CFA 輸出中寫入。
- 預設不寫入 plaintext prompt；除非使用者明確 opt in，否則只寫入 `prompt_hash`。
- MakerNote 必須不存在。
- 偽裝成真實相機或鏡頭不在範圍內。

## Validation Status

`image2dng validate` 會檢查 embedded preview layout（若存在）、required raw tags、geometry、black/white levels、camera profile tags、mode-specific CFA tags、XMP provenance，以及 MakerNote absence。

Optional smoke tools 是 compatibility evidence，不是 mandatory gates：

- 可用且 parse/export 成功的 tools 會記錄為 `passed`。
- 缺少的 tools 會記錄為 `skipped`。
- Adobe DNG SDK matrix entries 維持 `manual-only`；local-only SDK reports 需要使用者準備 Adobe resources，且不是 mandatory gates。
