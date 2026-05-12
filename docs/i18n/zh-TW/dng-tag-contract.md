# DNG tag contract

本文件是 `image2dng` 目前輸出的 DNG tag contract。它描述的是已實作並由 validator / tests 檢查的行為，不是未來 roadmap。

本專案產生 truthful synthetic DNG，不偽裝成真實相機 RAW。所有輸出都應保留 synthetic provenance，且不得寫入 MakerNote。

## 共同必要 tags

LinearRaw 與 simulated CFA 輸出都必須包含：

| Tag | Expected value / policy |
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
| `BlackLevel` | LinearRaw: 3 values；CFA: 4 values |
| `WhiteLevel` | LinearRaw: 3 values；CFA: 1 value |
| `DefaultScale` | `1/1, 1/1` |
| `ActiveArea` | `0,0,height,width` |
| `DefaultCropOrigin` | `0,0` |
| `DefaultCropSize` | `width,height` |
| `ColorMatrix1` | virtual camera XYZ-to-native matrix |
| `AsShotNeutral` | white-balance neutral normalized to green |
| `CalibrationIlluminant1` | `21` (`D65`) |
| `RawDataUniqueID` | 16-byte deterministic ID derived from the raw image buffer |
| `Software` | `image2dng <version>` |
| `XMP` | synthetic AI provenance packet |

## LinearRaw mode

`linearraw` mode writes a three-channel main image:

| Tag | Expected value |
| --- | --- |
| `PhotometricInterpretation` | `34892` (`LinearRaw`) |
| `SamplesPerPixel` | `3` |
| `BitsPerSample` | `16,16,16` |
| `PlanarConfiguration` | chunky/contiguous |

## Simulated CFA mode

`cfa` mode writes a single-channel simulated Bayer mosaic. It is an explicit workflow and compatibility research mode, not a real sensor capture claim.

| Tag | Expected value |
| --- | --- |
| `PhotometricInterpretation` | `32803` (`ColorFilterArray`) |
| `SamplesPerPixel` | `1` |
| `CFARepeatPatternDim` | `2,2` |
| `CFAPattern` | Four entries matching the selected Bayer pattern |
| `CFAPlaneColor` | `0,1,2` |
| `CFALayout` | `1` |

Supported Bayer patterns:

- `rggb`
- `bggr`
- `grbg`
- `gbrg`

## Provenance and safety contract

- XMP must include `xmpAI:provenanceType="synthetic"`.
- XMP must include `xmpAI:cameraParametersAreSimulated="True"`.
- `xmpAI:rawMode` records `linearraw` or `cfa`.
- `xmpAI:cfaPattern` is written only for CFA outputs.
- Plaintext prompts are not written by default; only `prompt_hash` is written unless the user explicitly opts in.
- MakerNote must be absent.
- Real camera/lens impersonation is out of scope.

## Validation status

`image2dng validate` checks required tags, geometry, black/white levels, mode-specific CFA tags, XMP provenance, and MakerNote absence.

Optional smoke tools are compatibility evidence, not mandatory gates:

- available tools that parse/export successfully are recorded as `passed`;
- missing tools are recorded as `skipped`;
- Adobe DNG SDK remains `manual-only` until a reproducible local validation path is added.
