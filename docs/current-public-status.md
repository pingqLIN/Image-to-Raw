# Image-to-DNG RAW Generator - Current Public Status

Traditional Chinese source manuscript: [docs/i18n/zh-TW/current-public-status.md](i18n/zh-TW/current-public-status.md)

This document records the current engineering baseline for public exchange. It avoids internal planning notes, timelines, and unpublished priority lists.

## Current Capabilities

- Generate truthful synthetic DNG files from 16-bit TIFF/PNG or scene-linear RGB inputs.
- Write uncompressed 16-bit three-channel `LinearRaw` DNG output.
- Write explicit simulated single-channel CFA DNG output with `rggb`, `bggr`, `grbg`, or `gbrg` Bayer patterns.
- Apply optional deterministic synthetic sensor effects for demos and compatibility testing.
- Override DNG `ColorMatrix1` and `AsShotNeutral` metadata explicitly for virtual camera profile experiments.
- Validate and preserve `image2dng.semantic_scene.v1` semantic sidecars, with explicit opt-in support for implemented deterministic semantic reactions.
- Generate local-only review bundles with contact sheets, representative DNGs, validation JSON, manifests, and reproducibility commands; if an upstream command fails, already generated source reports and manifests are preserved as diagnostic evidence.
- Track source report diagnostic preservation on command failure as part of the review-bundle contract.
- Embed AI provenance, selected raw mode, simulated camera parameters, and enabled sensor-effect settings in XMP.
- Validate generated DNG files with structural checks, mode-aware tag checks, XMP checks, and optional local smoke tools.
- Use either the CLI or the public Python `convert()` API.

## Technical Position

The project intentionally does not impersonate a real camera RAW file. Generated DNGs identify themselves as synthetic outputs and avoid MakerNote spoofing.

`LinearRaw` remains the compatibility-first output path because it is simple, inspectable, and avoids false claims about sensor capture. The simulated CFA path is explicit and intended for workflow research, compatibility testing, and controlled demos. Sensor effects and semantic reaction helpers are simple deterministic controls, not a physical camera model.

## Application Interfaces

Primary interfaces:

- CLI: `uv run image2dng input.tif output.dng`
- Validator: `uv run image2dng validate output.dng --json`
- Semantic sidecar validator: `uv run image2dng validate-semantic scene.semantic.json --json`
- Semantic reaction registry: `uv run image2dng semantic-reactions --json`
- Python API: `from image2dng import convert`
- Demo generator: `uv run python scripts/generate_demo_samples.py --output-dir demo-output`
- RAW-native batch: `uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch`
- Compatibility evidence: `uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence`
- Review bundle: `uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle`

See:

- [README.md](../README.md)
- [docs/design.md](design.md)
- [docs/compatibility.md](compatibility.md)
- [docs/demo.md](demo.md)

## Verification Baseline

The current high-signal checks are:

- `uv run python scripts/verify_development_baseline.py --output-dir demo-output/development-baseline`
- `uv run pytest`
- `uv run ruff check`
- `uv build`
- CLI help smoke for `image2dng --help`, `image2dng validate --help`, `image2dng validate-semantic --help`, and `image2dng semantic-reactions --help`
- local generation, manifest inspection, and validation for LinearRaw, CFA, CFA with sensor effects, and the RAW-native node batch
