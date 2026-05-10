# Image-to-DNG RAW Generator

`image2dng` is a prototype CLI and Python library for turning 16-bit TIFF/PNG or scene-linear RGB images into truthful synthetic DNG files. The MVP writes uncompressed 16-bit `LinearRaw` DNG, embeds AI provenance in a custom XMP namespace, and avoids MakerNote spoofing.

The project direction is expanding from one-shot image-to-raw conversion into **RAW-native AI image generation**: the primary generated artifact should be a synthetic RAW/DNG file, while JPEG/PNG outputs are previews or delivery renders derived from the RAW buffer. The repository now includes a minimal node-style pipeline for Prompt/Scene/Virtual Camera/Sensor/DNG/JPEG/Validation experiments.

This project intentionally does **not** try to impersonate a real camera RAW file. Generated DNGs use `UniqueCameraModel = "Synthetic Camera v1"` and XMP metadata marks camera parameters as simulated.

This product includes DNG technology under license by Adobe.

## Development status

This project is currently an active proof of concept. Behavior, metadata fields, DNG tag layout, and compatibility expectations may change while the design is being validated.

## Install for development

```powershell
uv sync --extra dev
```

## Generate a RAW-native node batch

```powershell
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
```

The batch emits:

- scene-linear TIFF intermediates;
- `LinearRaw` synthetic DNG files;
- simulated RGGB CFA synthetic DNG files;
- JPEG previews rendered from generated DNG buffers;
- validation JSON for each DNG;
- `manifests/raw-native-node-batch.json` as the node graph manifest;
- `manifests/sample-index.json` as the sample index.

The current decision is to build the minimal core pipeline inside this repository first. ComfyUI remains a strong candidate for a later visual orchestration layer, workflow UI, or custom-node integration, but it is not the first required dependency for the core RAW/DNG semantics.

## Run the development baseline verification

```powershell
uv run python scripts/verify_development_baseline.py --output-dir demo-output/development-baseline
```

This verification flow runs `pytest`, `ruff check`, RAW-native batch generation, and checks the manifest, sample index, DNG validation JSON, and JPEG previews. It writes `demo-output/development-baseline/verification-report.json`. `demo-output/` is local output and binary samples should not be committed.

## Generate compatibility evidence

```powershell
uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence
```

This flow emits deterministic DNG fixtures, validation JSON, `compatibility-report.json`, and `compatibility-summary.md`. Phase 6 reports use `image2dng.compatibility_evidence.v2` and record RAW processor commands, exit codes, stdout/stderr tails, output artifacts, and dry-run install hints. Missing optional RAW tools are recorded as `skipped` instead of failures; installed tools that fail to run or fail to emit their export artifact are recorded as `failed`; Adobe DNG SDK remains manual-only for now.

## Generate a demo review bundle

```powershell
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
```

This flow reruns the visual demo, RAW-native node batch, development baseline, and compatibility evidence, then collects the externally reviewable contact sheets, representative DNG files, validation JSON, reports, and manifests under `demo-output/review-bundle/`. The human entry point is `index.md`; the machine-readable manifest is `review-bundle-report.json`. `demo-output/` remains local output and binary samples should not be committed.

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

By default, the CLI refuses to replace an existing output file. Pass `--overwrite` only when replacing the output is intentional.

Supported input spaces:

- `srgb`: display-referred sRGB; the CLI applies the inverse sRGB OETF.
- `linear-rec709`: scene-linear Rec.709/sRGB primaries.
- `acescg`: scene-linear ACEScg/AP1, converted through XYZ into the virtual camera space.
- `xyz`: scene-linear CIE XYZ, converted into the virtual camera space.
- `prophoto-rgb`: encoded ProPhoto RGB / ROMM-style 1.8 transfer, adapted from D50 to D65.

Supported output modes:

- `linearraw`: default three-channel 16-bit LinearRaw DNG.
- `cfa`: explicit simulated single-channel CFA mosaic DNG.

Example CFA output:

```powershell
uv run image2dng input.tif output-cfa.dng `
  --mode cfa `
  --cfa-pattern rggb `
  --input-space linear-rec709
```

CFA mode is a simplified simulation intended for compatibility and workflow research. It does not claim to be a real sensor capture and does not add sensor noise by default.

## Validate a DNG

```powershell
uv run image2dng validate output.dng
uv run image2dng validate output.dng --json
```

The validator checks required DNG tags, XMP parseability, black/white level sanity, image geometry, synthetic provenance, and absence of MakerNote. If `exiftool`, `dcraw`, `darktable-cli`, or `rawtherapee-cli` are available on `PATH`, it also attempts smoke tests.

Validation exit codes:

- `0`: structural validation passed; optional smoke tools passed or were skipped.
- `1`: structural DNG validation failed.
- `2`: an optional smoke tool ran and reported an actual parse/open failure.
- `3`: CLI usage or configuration error.

See [docs/compatibility.md](docs/compatibility.md) for the compatibility evidence format.

## Use as a Python library

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

The public API raises `Image2DNGError` subclasses instead of exiting the process. CLI and library outputs are expected to be semantically equivalent under `image2dng validate`.

## Generate demo samples

```powershell
uv run python scripts/generate_demo_samples.py --output-dir demo-output
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
uv run python scripts/generate_visual_demo.py --output-dir demo-output/visual-demo
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
```

See [docs/demo.md](docs/demo.md) for the architecture demo, sample set, and application scenarios.

## Tests

```powershell
uv run pytest
```

## Project structure

- `src/image2dng/cli.py`: command-line entry points for generation and validation.
- `src/image2dng/image_processing.py`: input loading, color-space conversion, linearization, and quantization.
- `src/image2dng/models.py`: dataclasses for raw layout, camera profile, and AI metadata.
- `src/image2dng/dng_writer.py`: DNG/TIFF writing and DNG metadata tags.
- `src/image2dng/xmp.py`: synthetic AI provenance XMP packet generation.
- `src/image2dng/validate.py`: structural DNG validation and optional external smoke tests.

## Scope

Current MVP:

- 16-bit uncompressed LinearRaw DNG.
- Explicit simulated CFA mosaic mode.
- Built-in minimal RAW-native node pipeline that emits DNG, JPEG preview, validation JSON, and graph manifest artifacts.
- Optional deterministic synthetic sensor effects for demos and compatibility testing.
- RGB input normalization and simple virtual camera transform.
- XMP custom namespace: `https://example.org/ns/xmp/ai/1.0/`.
- Synthetic provenance always written.

Known limitations:

- Sensor effects are simple synthetic controls, not a physical camera model.
- No preview IFD, EXIF IFD, semantic mask IFD, depth IFD, or `DNGPrivateData` payload yet.
- Compatibility is validated structurally and with optional local smoke tools, not yet against the Adobe DNG SDK.

See [docs/design.md](docs/design.md) for the design notes.
See [docs/i18n/en/raw-native-node-pipeline.md](docs/i18n/en/raw-native-node-pipeline.md) for the RAW-native node pipeline direction.
