# Image-to-DNG RAW Generator

`image2dng` is a prototype CLI for turning 16-bit TIFF/PNG or scene-linear RGB images into truthful synthetic DNG files. The MVP writes uncompressed 16-bit `LinearRaw` DNG, embeds AI provenance in a custom XMP namespace, and avoids MakerNote spoofing.

This project intentionally does **not** try to impersonate a real camera RAW file. Generated DNGs use `UniqueCameraModel = "Synthetic Camera v1"` and XMP metadata marks camera parameters as simulated.

This product includes DNG technology under license by Adobe.

## Development status

This project is currently an active proof of concept. Behavior, metadata fields, DNG tag layout, and compatibility expectations may change while the design is being validated.

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

By default, the CLI refuses to replace an existing output file. Pass `--overwrite` only when replacing the output is intentional.

Supported input spaces:

- `srgb`: display-referred sRGB; the CLI applies the inverse sRGB OETF.
- `linear-rec709`: scene-linear Rec.709/sRGB primaries.
- `acescg`: scene-linear ACEScg/AP1, converted through XYZ into the virtual camera space.
- `xyz`: scene-linear CIE XYZ, converted into the virtual camera space.

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
    iso=100,
    white_balance_kelvin=6500,
    prompt_hash="sha256:...",
    scene_description="synthetic test scene",
    overwrite=False,
)
```

The public API raises `Image2DNGError` subclasses instead of exiting the process. CLI and library outputs are expected to be semantically equivalent under `image2dng validate`.

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
- RGB input normalization and simple virtual camera transform.
- XMP custom namespace: `https://example.org/ns/xmp/ai/1.0/`.
- Synthetic provenance always written.

Known limitations:

- No CFA Bayer mosaic yet.
- No shot/read noise model yet.
- No preview IFD, EXIF IFD, semantic mask IFD, depth IFD, or `DNGPrivateData` payload yet.
- Compatibility is validated structurally and with optional local smoke tools, not yet against the Adobe DNG SDK.

See [docs/design.md](docs/design.md) for the design notes.
