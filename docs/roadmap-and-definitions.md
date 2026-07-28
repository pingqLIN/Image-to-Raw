# Image-to-RAW Roadmap and Definitions

## Decision

The project should define `LinearRaw` as the production path and `CFA` as an optional experimental path.

This decision follows from the product goal: generate a high-dynamic-range, high-information, RAW-editable synthetic camera-negative DNG. A CFA mosaic is useful for sensor-pipeline simulation, demosaic testing, and ISP research, but it is not required to preserve high-information linear editing data.

## Core definition

### Synthetic camera-negative

A **synthetic camera-negative** is a DNG file that contains a camera-native, linear, high-bit-depth signal plus enough DNG metadata to behave like a RAW editing negative.

It may include:

- `PhotometricInterpretation = LinearRaw`
- 16-bit or higher internal precision before quantization
- explicit black/white level
- color matrix and illuminant metadata
- as-shot neutral / white balance
- simulated exposure and ISO hints
- AI provenance and source/model hashes
- optional depth, masks, confidence maps, and private payloads

It must not claim:

- that it is an original captured RAW file
- that it is a real sensor dump
- that it was captured by a named real camera model unless the source actually was such a RAW
- that the scene-linear signal is universal real-world ground truth

## Scene-linear policy

### Problem

For arbitrary real-world scenes, there is no complete pixelwise `scene-linear` ground-truth sample available to compare against. Camera RAW is camera- and sensor-specific; RGB images are usually already tone-mapped, color-rendered, compressed, or edited. Therefore, validation cannot be based on one universal ground-truth scene-linear image.

### Project policy

The project treats `scene-linear` as a **working representation** and `camera-native LinearRaw` as a **synthetic or estimated signal representation**.

The correct validation question is not:

> Did this exactly reconstruct the unknowable physical scene radiance?

The correct validation questions are:

> Is the DNG structurally valid?
> Is the signal linear, editable, and self-consistent?
> Does it preserve intended highlight/headroom behavior?
> Does it round-trip through RAW software predictably?
> Are all assumptions and provenance explicit?

## Reference hierarchy

Validation should use a hierarchy rather than a single ground-truth target.

| Reference class | Example | What it validates | Limit |
|---|---|---|---|
| Structural reference | DNG SDK / TIFF parser / ExifTool | IFD layout, tags, offsets, XMP | Does not validate visual quality |
| Synthetic ground truth | EXR / ACES render from a renderer | Known linear source behavior | Synthetic, not real-world capture |
| Calibration target | ColorChecker, gray ramp, HDR ramp | Color, monotonicity, white balance, clipping | Limited scene variety |
| Paired RAW/RGB | Camera RAW + developed RGB pair | Inverse-ISP calibration and regression | Camera-specific, not universal |
| Fixed render round-trip | DNG -> reference development -> display RGB | Editability and output stability | Depends on chosen development pipeline |
| Compatibility oracle | Camera Raw, darktable, RawTherapee | Real-world software behavior | Not a mathematical proof |

## Output modes

### Production: `linearraw`

Purpose:

- high-dynamic synthetic camera-negative DNG
- RAW-style editing
- stable interchange
- explicit provenance

Default claims:

- synthetic / estimated camera-native linear signal
- simulated camera parameters
- high-information editing negative

Default non-claims:

- not real camera RAW
- not true captured Bayer data
- not forensic evidence of real-world capture
- not exact recovery of unknowable scene radiance

### Experimental: `cfa`

Purpose:

- demosaic testing
- RAW denoise testing
- ISP research
- Bayer/CFA sensor-pipeline simulation
- camera artifact simulation

Default claims:

- simulated CFA mosaic from the project signal model

Default non-claims:

- not required for high-information synthetic RAW editing
- not a real sensor dump unless the source was an actual RAW and the pipeline preserves that fact

## Roadmap

### Phase 0 — Definitions, claims, and provenance

Deliverables:

- Update README with project definition.
- Add `docs/roadmap-and-definitions.md`.
- Rename product language from "real-world captured RAW" to "synthetic camera-negative" or "estimated camera-native LinearRaw".
- Add a documented reference hierarchy.
- Add XMP fields for `provenanceType`, `reconstructionType`, `cameraParametersAreSimulated`, source hash, model hash, adapter hash, and workflow digest.

Exit criteria:

- No project documentation implies that generated output is original camera RAW.
- `linearraw` is documented as default production mode.
- `cfa` is documented as optional experimental mode.

### Phase 1 — Production LinearRaw DNG

Deliverables:

- Keep 16-bit uncompressed `LinearRaw` DNG as the default.
- Always write explicit `BlackLevel`, `WhiteLevel`, `ColorMatrix1`, `CalibrationIlluminant1`, `AsShotNeutral`, `RawDataUniqueID`, `UniqueCameraModel`, and synthetic XMP.
- Add preview IFD and EXIF IFD where useful.
- Add tests for deterministic gradients, color ramps, saturation ramps, gray ramps, and white-balance behavior.

Exit criteria:

- Generated DNGs open in target RAW software.
- Exposure and white-balance controls behave predictably.
- Metadata and provenance survive basic readback.

### Phase 2 — Validation suite

Deliverables:

- Add reference-render comparison.
- Add DNG SDK or equivalent parser round-trip validation.
- Add compatibility smoke tests.
- Add metrics for open success, render stability, black/white sanity, metadata retention, provenance completeness, and mask/depth integrity.

Exit criteria:

- Each release includes validation report artifacts.
- Failure in required tags, provenance, or black/white sanity blocks release.

### Phase 3 — Neural inverse-ISP plugin

Deliverables:

- Add `NeuralRawEstimator` protocol.
- Support optional SpiralDiff-like / CamLoRA-like adapters through plugin integration.
- Store model and adapter hashes.
- Store uncertainty summary or uncertainty map digest.
- Keep the DNG writer independent from model code.

Exit criteria:

- The same DNG packaging path works with both deterministic and neural estimated signals.
- Model-derived output is always labeled `estimated_raw` and `synthetic`.

### Phase 4 — Semantic and private payloads

Deliverables:

- Add semantic mask IFD support.
- Add depth IFD support.
- Add structured `DNGPrivateData` support.
- Add digest/index schema for large payloads.

Exit criteria:

- Main Raw IFD remains readable when optional semantic/private data is ignored.
- Payload digest readback passes.

### Phase 5 — Optional CFA research mode

Deliverables:

- Add `--mode cfa` behind an experimental flag.
- Implement CFA tags and Bayer pattern handling.
- Add optional sensor artifact simulation: shot/read noise, row/column noise, PRNU, hot pixels, masked pixels.
- Add demosaic/ISP benchmark tests.

Exit criteria:

- CFA files open in target software without crash.
- Demosaic behavior is plausible for research use.
- Documentation clearly states that CFA mode is simulated and experimental.
