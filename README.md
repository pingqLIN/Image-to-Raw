# Image-to-DNG RAW Generator

`image2dng` is a prototype CLI for generating **truthful synthetic camera-negative DNG files** from 16-bit TIFF/PNG, display-referred RGB, scene-linear RGB, or model-estimated linear camera signals.

The production path writes uncompressed 16-bit `LinearRaw` DNG. The file is intended to behave like a high-dynamic-range, high-information RAW editing negative, not like an original sensor dump from a real camera. Generated DNGs use `UniqueCameraModel = "Synthetic Camera v1"`, embed AI/synthetic provenance in a custom XMP namespace, and avoid MakerNote spoofing.

## Product definition

`image2dng` generates a **synthetic camera-negative**:

- It stores a camera-native, linear, high-bit-depth signal suitable for RAW-style editing.
- It preserves editability through DNG metadata such as black level, white level, white balance, color matrix, simulated exposure/ISO hints, and provenance.
- It does not claim that the output is a captured Bayer sensor measurement.
- It does not impersonate a real camera, lens, serial number, MakerNote, or original capture pipeline.

The project treats `scene-linear` as a **working signal representation**, not as directly measurable ground truth for arbitrary real-world scenes. Real-world scene radiance usually has no complete pixelwise reference sample available. Therefore, project validation is based on reference hierarchy and behavioral tests rather than a single universal ground-truth image.

## Validation philosophy

Since arbitrary real-world `scene-linear` ground truth is unavailable, validation is defined in layers:

1. **DNG structural correctness**: required tags, IFD layout, offsets, image geometry, black/white level sanity, XMP parseability, and absence of MakerNote spoofing.
2. **Signal behavior**: monotonicity, clipping policy, highlight headroom, black offset, channel balance, white-balance behavior, and predictable exposure edits.
3. **Round-trip rendering**: generated DNG is developed through a fixed reference pipeline and compared against the intended display rendering or source image where appropriate.
4. **Anchor references**: synthetic renderer outputs, calibrated color charts, HDR brackets, or paired RAW/RGB datasets may be used as calibration anchors, but these are reference cases, not a claim that every output has real-scene ground truth.
5. **Compatibility**: Camera Raw / Lightroom, darktable, RawTherapee, `exiftool`, `dcraw`, and eventually the Adobe DNG SDK are used as decoder-side oracles.

## Install for development

```powershell
uv sync --extra dev
```

## Generate a DNG

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

Supported input spaces:

- `srgb`: display-referred sRGB; the CLI applies the inverse sRGB OETF.
- `linear-rec709`: scene-linear Rec.709/sRGB primaries.
- `acescg`: scene-linear ACEScg/AP1, converted through XYZ into the virtual camera space.
- `xyz`: scene-linear CIE XYZ, converted into the virtual camera space.
- `prophoto-rgb`: encoded ProPhoto RGB / ROMM-style 1.8 transfer, adapted from D50 to D65.

For true scene-linear input spaces (`linear-rec709`, `acescg`, `xyz`), opt-in exposure placement can preserve highlight headroom already present in the source:

```powershell
uv run image2dng scene-linear.tif output.dng `
  --input-space acescg `
  --highlight-headroom-ev 2 `
  --exposure-bias-ev 0
```

`--highlight-headroom-ev 2` maps scene value `4.0` to the RAW white point. It does not recover detail absent from display-referred images and is not dynamic-range recovery. Placement runs before sensor effects so existing HDR values are not clipped before placement.

## Output modes

### `linearraw` — default production path

Use this mode for high-dynamic-range, high-information synthetic RAW negatives.

Claims:

- The DNG contains a synthetic or estimated camera-native linear signal.
- The file is designed for RAW-style editing and interchange.
- Provenance and simulated camera parameters are explicit.

Non-claims:

- The DNG is not an original captured RAW file.
- The signal is not guaranteed to match an unknown real-world scene-linear sample.
- The file does not contain a real camera MakerNote or sensor serial identity.

### `cfa` — optional experimental research path

Use this mode only when the downstream task needs Bayer/CFA sampling itself, such as demosaic testing, RAW denoising, ISP research, sensor artifact simulation, or camera-pipeline benchmarking.

Claims:

- The DNG contains a simulated CFA mosaic derived from the project signal model.

Non-claims:

- The DNG is not a real sensor dump unless the source was a real camera RAW and the project is explicitly preserving it.
- CFA mode is not required for high-information synthetic RAW editing.

## Validate a DNG

```powershell
uv run image2dng validate output.dng
```

The validator checks required DNG tags, XMP parseability, black/white level sanity, image geometry, synthetic provenance, and absence of MakerNote. If `exiftool`, `dcraw`, `darktable-cli`, or `rawtherapee-cli` are available on `PATH`, it also attempts smoke tests.

## Tests

```powershell
uv run pytest
```

## Roadmap

### Phase 0 — Definition and provenance hardening

- Define `LinearRaw` as the production path for synthetic camera-negative DNG.
- Define `CFA` as an optional experimental sensor-simulation path.
- Add explicit XMP fields for `synthetic`, `estimated_raw`, simulated camera parameters, source hash, model hash, adapter hash, and workflow digest.
- Document that scene-linear signal is a working representation and not universal pixelwise ground truth.

### Phase 1 — LinearRaw production DNG

- Maintain 16-bit uncompressed `LinearRaw` DNG output.
- Write explicit `BlackLevel`, `WhiteLevel`, `ColorMatrix1`, `CalibrationIlluminant1`, `AsShotNeutral`, `RawDataUniqueID`, and `UniqueCameraModel`.
- Add preview IFD and EXIF IFD for compatibility and usability.
- Keep prompt plaintext opt-in; default to hashes and high-level provenance.

### Phase 2 — Validation and reference suite

- Add deterministic test vectors for gradients, color charts, saturation ramps, gray ramps, HDR ramps, and clipping boundaries.
- Add reference-render comparison through a fixed development transform.
- Add optional compatibility tests for Camera Raw / Lightroom, darktable, RawTherapee, `exiftool`, `dcraw`, and Adobe DNG SDK.
- Track metrics for open success, render stability, white-balance behavior, black/white sanity, metadata retention, and provenance completeness.

### Phase 3 — Neural inverse-ISP plugin layer

- Add a `NeuralRawEstimator` interface for optional RGB-to-linear-camera or RGB-to-RAW estimation.
- Integrate SpiralDiff-like / CamLoRA-like adapters as optional plugins, not as core DNG writer dependencies.
- Store model family, model version, checkpoint hash, adapter hash, training manifest hash, and uncertainty summary in XMP or private payload.
- Continue to label outputs as synthetic / estimated RAW.

### Phase 4 — Semantic and private payloads

- Add semantic mask IFD, depth IFD, and `DNGPrivateData` support.
- Store large confidence maps, segmentation maps, depth maps, workflow summaries, and hashes in structured payloads.
- Ensure the main Raw IFD remains readable when optional semantic/private data is ignored.

### Phase 5 — Experimental CFA mode

- Add CFA Bayer mosaic output only after the LinearRaw path, metadata, and validator are stable.
- Implement `CFAPattern`, `CFARepeatPatternDim`, `CFAPlaneColor`, active area, black/white level, optional masked pixels, and sensor artifact simulation.
- Use CFA mode for demosaic/ISP research, RAW denoise benchmarks, or sensor-pipeline simulation—not as the default route for high-information synthetic RAW editing.

## Scope

Current MVP:

- 16-bit uncompressed `LinearRaw` DNG.
- RGB input normalization and simple virtual camera transform.
- XMP custom namespace: `https://example.org/ns/xmp/ai/1.0/`.
- Synthetic provenance always written.
- No impersonation of real camera RAW, MakerNote, serial number, or original capture pipeline.

Known limitations:

- The current `scene-linear` signal is a constructed/estimated working representation, not a directly verifiable real-world ground-truth sample.
- No shot/read noise model yet.
- No preview IFD, EXIF IFD, semantic mask IFD, depth IFD, or `DNGPrivateData` payload yet.
- No optional CFA Bayer mosaic research mode yet.
- Compatibility is validated structurally and with optional local smoke tools, not yet against the Adobe DNG SDK.

See [docs/roadmap-and-definitions.md](docs/roadmap-and-definitions.md) for the current definitions and roadmap. See [docs/design.md](docs/design.md) for the broader design notes.
