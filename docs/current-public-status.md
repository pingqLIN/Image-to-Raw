# Image-to-DNG RAW Generator - Current Public Status

Traditional Chinese source manuscript: [docs/i18n/zh-TW/current-public-status.md](i18n/zh-TW/current-public-status.md)

This document records the current engineering baseline for public exchange. It avoids internal planning notes, timelines, and unpublished priority lists.

## Current Capabilities

- Generate truthful synthetic DNG files from 16-bit TIFF/PNG or scene-linear RGB inputs.
- Write uncompressed 16-bit three-channel `LinearRaw` DNG output.
- Write explicit simulated single-channel CFA DNG output with `rggb`, `bggr`, `grbg`, or `gbrg` Bayer patterns.
- Apply optional deterministic synthetic sensor effects for demos and compatibility testing.
- Generate RAW-native node batches with DNGs, sidecar JPEG previews, validation JSON, graph manifests, and sample indexes.
- Validate and preserve `image2dng.semantic_scene.v1` semantic sidecars; explicit opt-in can apply deterministic semantic reaction helpers.
- Generate compatibility evidence matrices that record optional RAW processor state, commands, output artifacts, and failure evidence.
- Generate local-only review bundles with contact sheets, representative DNGs, validation JSON, manifests, and reproducibility commands.
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

- `uv run pytest`
- `uv run ruff check`
- `uv build`
- wheel install smoke for `image2dng --help` and `image2dng validate --help`
- demo sample generation and validation for LinearRaw, CFA, and CFA with sensor effects
- RAW-native node batch generation, sample index validation, and semantic sidecar preservation/reaction checks
- compatibility evidence generation, including missing optional tools and failed processor output artifact reporting
- review bundle generation, including bundle-relative artifact manifest and checksums
