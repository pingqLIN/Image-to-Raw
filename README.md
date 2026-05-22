# Image-to-DNG RAW Generator

[繁體中文](README.zh-tw.md)

![Status](https://img.shields.io/badge/status-prototype-orange)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-proprietary%20prototype-lightgrey)

[Features](#features) · [Installation](#install-for-development) · [Compatibility](#generate-compatibility-evidence) · [Validation](#validate-a-dng) · [Testing](#tests) · [License](#prototype-license-status)

> Convert scene-linear or 16-bit RGB images into truthful synthetic DNG/RAW research artifacts.

`image2dng` is a prototype CLI and Python library for turning 16-bit TIFF/PNG or scene-linear RGB images into truthful synthetic DNG files. The MVP writes uncompressed 16-bit `LinearRaw` raw image data, uses a default DNG layout with an IFD0 JPEG preview, embeds AI provenance in a custom XMP namespace, and avoids MakerNote spoofing.

The project direction is expanding from one-shot image-to-raw conversion into **RAW-native AI image generation**: the primary generated artifact should be a synthetic RAW/DNG file, while JPEG/PNG outputs are previews or delivery renders derived from the RAW buffer. The repository now includes a minimal node-style pipeline for Prompt/Scene/Virtual Camera/Sensor/DNG/JPEG/Validation experiments.

The project intentionally does **not** try to impersonate a real camera RAW file. Generated DNGs use `UniqueCameraModel = "Synthetic Camera v1"` and XMP metadata marks camera parameters as simulated.

This product includes DNG technology under license by Adobe.

---

## Features

| Capability | Status | Notes |
| --- | --- | --- |
| Synthetic LinearRaw DNG | MVP | 16-bit uncompressed DNG with explicit synthetic provenance |
| Simulated CFA mode | Available | Explicit opt-in Bayer mosaic for workflow and compatibility research |
| Embedded DNG preview | Experimental | Default DNG layout writes IFD0 JPEG preview plus raw SubIFD |
| RAW-native node batch | Available | Generates DNG, sidecar JPEG preview, validation JSON, and graph manifests |
| Semantic scene sidecar v1 | Available | Validates and preserves external scene semantics; optional semantic reaction chain helpers |
| Compatibility evidence | Available | Structural validation plus optional ExifTool/Darktable/RawTherapee smoke evidence |
| Review bundle | Available | Local-only package with contact sheets, representative DNGs, validation JSON, and manifests |

---

## Development status

Current status: active proof of concept. Behavior, metadata fields, DNG tag layout, and compatibility expectations may change while the design is being validated.

## Documentation

Key explanatory documents:

- Design overview: [docs/design.md](docs/design.md) / [docs/i18n/zh-TW/design-overview.md](docs/i18n/zh-TW/design-overview.md)
- Compatibility evidence: [docs/compatibility.md](docs/compatibility.md) / [docs/i18n/zh-TW/compatibility-evidence.md](docs/i18n/zh-TW/compatibility-evidence.md)
- Demo workflow: [docs/demo.md](docs/demo.md) / [docs/i18n/zh-TW/demo-visualization-workflow.md](docs/i18n/zh-TW/demo-visualization-workflow.md)
- Current public status: [docs/Current Status and Development Suggestions_ChatGPT.md](docs/Current%20Status%20and%20Development%20Suggestions_ChatGPT.md) / [docs/i18n/zh-TW/current-public-status.md](docs/i18n/zh-TW/current-public-status.md)

## Install for development

```powershell
uv sync --extra dev
```

## Generate a RAW-native node batch

```powershell
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
```

If the generated DNG files need to be passed into Adobe DNG Converter, write the
single raw IFD layout:

```powershell
uv run python scripts/generate_raw_native_batch.py `
  --output-dir demo-output/raw-native-node-batch-adobe `
  --dng-layout single-raw-ifd
```

The batch emits:

- scene-linear TIFF intermediates;
- `LinearRaw` synthetic DNG files;
- simulated RGGB CFA synthetic DNG files;
- DNG files with an embedded IFD0 JPEG preview and raw data in a Raw SubIFD;
- sidecar JPEG previews rendered from generated DNG raw buffers;
- validation JSON for each DNG;
- `manifests/raw-native-node-batch.json` as the node graph manifest;
- `manifests/sample-index.json` as the sample index.

The current decision is to build the minimal core pipeline inside this repository first. ComfyUI / Stable Diffusion integration has been split into a sibling bridge project, while this core repository keeps the generic external scene-linear manifest and RAW/DNG semantics.

External renderers, AI generators, and simulators can now enter through the scene-linear producer boundary:

```powershell
uv run python scripts/generate_raw_native_batch.py `
  --output-dir demo-output/external-scene-linear-batch `
  --scene-linear path\to\scene-linear.tif
```

Use a manifest when each image needs producer, prompt, lighting, or semantic sidecar metadata:

```json
{
  "schema": "image2dng.external_scene_linear_sources.v1",
  "scenes": [
    {
      "slug": "renderer-frame-001",
      "path": "renderer-frame-001.tif",
      "input_space": "linear-rec709",
      "producer": "external renderer",
      "prompt": "studio material test",
      "description": "scene-linear output from an upstream generator",
      "lighting": "virtual D65 studio",
      "semantic_manifest": "renderer-frame-001.semantic.json"
    }
  ]
}
```

## ComfyUI / Stable Diffusion Bridge

The ComfyUI / Stable Diffusion importer now lives in an external sibling bridge project; this core repository does not vendor or install that bridge:

`image-to-raw-comfyui-sd-bridge`

The old `scripts/import_comfyui_output.py` and `image2dng.comfyui_importer` interfaces moved to that bridge project. The new CLI is provided by the bridge project, not by this repository's console scripts:

```powershell
uv run image2dng-comfyui-import `
  path\to\ComfyUI_00002_.png `
  --output-dir demo-output/comfyui-import `
  --run-pipeline
```

This core repository accepts `image2dng.external_scene_linear_sources.v1` manifests from the bridge or any other external producer, then owns the scene-linear input, semantic sidecar, DNG writer, validation, and RAW-native batch steps. ComfyUI workflow metadata still enters manifests through `producer_metadata` / `producer_metadata_manifest`, but it is no longer a built-in core package API.

When `semantic_manifest` uses `image2dng.semantic_scene.v1`, it is validated before DNG generation. The sidecar and resolvable local assets are copied and recorded in the batch manifest / sample index. By default this remains preservation + validation; when the manifest explicitly sets `apply_semantic_reaction: true`, the deterministic `semantic-reaction-chain-v1` helpers currently apply `region-exposure-mask-v1` followed by `highlight-clipping-policy-v1` to affect 16-bit scene-linear RGB values. This chain is not a full physical sensor model. See [docs/i18n/en/semantic-scene-sidecar-contract.md](docs/i18n/en/semantic-scene-sidecar-contract.md) for the detailed format.

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

For Adobe DNG Converter regression, use the dedicated local script:

```powershell
uv run python scripts/verify_adobe_dng_converter.py --dry-run
uv run python scripts/verify_adobe_dng_converter.py --output-dir demo-output/adobe-dng-converter-verification
```

This keeps validation layers separate: strict `image2dng` contract validation for source DNGs, external processor smoke checks for optional tools, and relaxed Adobe-converted artifact inspection for files rewritten by Adobe DNG Converter.

## Generate a demo review bundle

```powershell
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
```

This flow reruns the visual demo, RAW-native node batch, development baseline, and compatibility evidence, then collects the externally reviewable contact sheets, representative DNG files, validation JSON, reports, and manifests under `demo-output/review-bundle/`. The human entry point is `index.md`; the machine-readable manifest is `review-bundle-report.json`. `demo-output/` remains local output and binary samples should not be committed.

## Generate a RAW processor setup audit

```powershell
uv run python scripts/audit_raw_processor_setup.py --output-dir demo-output/raw-processor-setup-audit
```

This dry-run audit detects current availability and package-manager search evidence for `dcraw`, `darktable-cli`, and `rawtherapee-cli`, then writes `setup-audit-report.json`, `setup-runbook.md`, and `external-review-prompt.md`. On Windows, Darktable and RawTherapee are discovered from `PATH` first and then from their standard install paths: `C:\Program Files\darktable\bin\darktable-cli.exe` and `C:\Program Files\RawTherapee\5.12\rawtherapee-cli.exe`. Version hints are recorded only when the package-manager output exactly matches the package identity. It does not install or update any tool; installing one RAW processor requires explicit user approval, and post-install evidence should rerun the setup audit, compatibility evidence, and review bundle.

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

The default DNG layout is `preview-subifd`: IFD0 is a JPEG-compressed RGB preview, and the raw image data is stored in a Raw SubIFD. To write the older single raw IFD layout, pass:

```powershell
uv run image2dng input.tif output.dng --dng-layout single-raw-ifd
```

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

The validator checks required DNG tags, XMP parseability, black/white level sanity, image geometry, synthetic provenance, and absence of MakerNote. The current tag contract is documented in [docs/i18n/en/dng-tag-contract.md](docs/i18n/en/dng-tag-contract.md). If `exiftool`, `dcraw`, `darktable-cli`, or `rawtherapee-cli` are available through `PATH` or an adapter-supported common install path, it also attempts smoke tests.

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
    dng_layout="preview-subifd",
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
uv run python scripts/audit_raw_processor_setup.py --output-dir demo-output/raw-processor-setup-audit
```

See [docs/demo.md](docs/demo.md) for the architecture demo, sample set, and application scenarios.

## Tests

```powershell
uv run pytest
uv run ruff check
```

## 🤖 AI-Assisted Development

This project was developed with AI assistance.

| Model | Role |
| --- | --- |
| OpenAI Codex CLI | Primary implementation, compatibility evidence workflow, documentation review |

> ⚠️ **Disclaimer:** While the author has made every effort to review and validate
> the AI-generated code, no guarantee can be made regarding its correctness, security,
> or fitness for any particular purpose. Use at your own risk.

## Prototype License Status

The current license status remains `Proprietary prototype`, matching `pyproject.toml`. The public documentation describes the current proof-of-concept capabilities and validation workflow; unless a separate license file is added, do not assume this repository is released under MIT or another open-source license.

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
- Default `preview-subifd` DNG layout with an IFD0 JPEG preview and Raw SubIFD.
- Built-in minimal RAW-native node pipeline that emits DNG, sidecar JPEG preview, validation JSON, and graph manifest artifacts.
- `image2dng.semantic_scene.v1` sidecar validation / preservation plus the opt-in `semantic-reaction-chain-v1` helpers.
- Optional deterministic synthetic sensor effects for demos and compatibility testing.
- RGB input normalization and simple virtual camera transform.
- XMP custom namespace: `https://example.org/ns/xmp/ai/1.0/`.
- Synthetic provenance always written.

Known limitations:

- Sensor effects are simple synthetic controls, not a physical camera model.
- Embedded preview is currently an IFD layout experiment, not a full Adobe compatibility claim.
- The pipeline does not emit EXIF IFD, semantic mask IFD, depth IFD, or `DNGPrivateData` payloads.
- Compatibility is validated structurally and with optional local smoke tools; Adobe DNG SDK remains manual-only.

See [docs/design.md](docs/design.md) for the design notes.
See [docs/i18n/en/dng-tag-contract.md](docs/i18n/en/dng-tag-contract.md) for the current DNG tag contract.
See [docs/i18n/en/raw-native-node-pipeline.md](docs/i18n/en/raw-native-node-pipeline.md) for the RAW-native node pipeline direction.
