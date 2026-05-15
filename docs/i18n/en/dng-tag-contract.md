# DNG Tag Contract

This document describes the DNG tag contract currently emitted by `image2dng`. It records implemented behavior covered by validator checks and tests, not a future roadmap.

The project generates truthful synthetic DNG files and does not impersonate a real camera RAW file. All outputs must preserve synthetic provenance and must not write MakerNote metadata.

## DNG Layout

The default layout is `preview-subifd`:

- IFD0 is a JPEG-compressed RGB preview with `NewSubFileType = 1`.
- IFD0 references the main raw image IFD through `SubIFDs`.
- The Raw SubIFD keeps the primary raw data and full raw tag contract with `NewSubFileType = 0`.

For compatibility or regression testing, `single-raw-ifd` remains available and writes the older single raw IFD layout. The validator locates the raw page by `NewSubFileType = 0` instead of assuming `pages[0]` is always the raw image.

## Shared Required Raw Tags

Both LinearRaw and simulated CFA outputs must include:

| Tag | Expected value / policy |
| --- | --- |
| `NewSubFileType` | `0`, the main raw image |
| `DNGVersion` | `1.4.0.0` |
| `DNGBackwardVersion` | `1.1.0.0` |
| `Make` | `image2dng` |
| `Model` | `Synthetic Camera v1` |
| `UniqueCameraModel` | `Synthetic Camera v1` |
| `Orientation` | `1` |
| `ImageWidth` / `ImageLength` | Derived from input dimensions |
| `BitsPerSample` | `16`; three channels for LinearRaw, one channel for CFA |
| `Compression` | `1`, uncompressed |
| `BlackLevelRepeatDim` | LinearRaw: `1,1`; CFA: `2,2` |
| `BlackLevel` | LinearRaw: 3 values; CFA: 4 values |
| `WhiteLevel` | LinearRaw: 3 values; CFA: 1 value |
| `DefaultScale` | `1/1, 1/1` |
| `ActiveArea` | `0,0,height,width` |
| `DefaultCropOrigin` | `0,0` |
| `DefaultCropSize` | `width,height` |
| `ColorMatrix1` | Virtual camera XYZ-to-native matrix |
| `AsShotNeutral` | White-balance neutral normalized to green |
| `CalibrationIlluminant1` | `21` (`D65`) |
| `RawDataUniqueID` | 16-byte deterministic ID derived from the raw image buffer |
| `Software` | `image2dng <version>` |
| `XMP` | Synthetic AI provenance packet |

IFD0 preview pages also write the basic identity/provenance tags: `DNGVersion`, `DNGBackwardVersion`, `Make`, `Model`, `UniqueCameraModel`, `Orientation`, `Software`, and `XMP`. The preview is an 8-bit RGB JPEG-compressed image, not raw data.

## LinearRaw Mode

`linearraw` mode writes a three-channel main image:

| Tag | Expected value |
| --- | --- |
| `PhotometricInterpretation` | `34892` (`LinearRaw`) |
| `SamplesPerPixel` | `3` |
| `BitsPerSample` | `16,16,16` |
| `PlanarConfiguration` | chunky/contiguous |

## Simulated CFA Mode

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

## Provenance and Safety Contract

- XMP must include `xmpAI:provenanceType="synthetic"`.
- XMP must include `xmpAI:cameraParametersAreSimulated="True"`.
- `xmpAI:rawMode` records `linearraw` or `cfa`.
- `xmpAI:cfaPattern` is written only for CFA outputs.
- `xmpAI:highlightHeadroomEV` and `xmpAI:exposureBiasEV` are written only when opt-in exposure placement is enabled. They record simulated scene-linear white placement and do not claim recovered dynamic range.
- Plaintext prompts are not written by default; only `prompt_hash` is written unless the user explicitly opts in.
- MakerNote must be absent.
- Real camera/lens impersonation is out of scope.

## Validation Status

`image2dng validate` checks embedded preview layout when present, required raw tags, geometry, black/white levels, mode-specific CFA tags, XMP provenance, and MakerNote absence.

Optional smoke tools are compatibility evidence, not mandatory gates:

- available tools that parse/export successfully are recorded as `passed`;
- missing tools are recorded as `skipped`;
- Adobe DNG SDK remains `manual-only` until a reproducible local validation path is added.
