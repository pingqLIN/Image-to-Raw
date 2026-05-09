# Compatibility Evidence

This project validates generated DNG files structurally and can run optional local smoke tools when they are available on `PATH`.

Missing optional tools are recorded as `skipped`, not as failures. CI must not require locally installed RAW processors unless a reproducible install path is added later.

This product includes DNG technology under license by Adobe.

## Validator Contract

Default output is human-readable. `image2dng validate --json` emits a structured report with:

- `ok`: overall structural success.
- `checks`: individual checks with `passed`, `failed`, `skipped`, or `warning`.
- `errors`: validation failures.
- `warnings`: non-fatal validation concerns.
- `smoke_tests`: compatibility smoke results by tool.

Exit codes:

- `0`: structural validation passed; optional smoke tools passed or were skipped.
- `1`: structural DNG validation failed.
- `2`: an optional smoke tool executed but reported a parse/open failure.
- `3`: CLI usage or configuration error.

## Evidence Matrix Schema

Use this table shape for reproducible compatibility notes:

| Fixture | Tool | Tool version | Command | Result | Evidence | Environment | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `srgb-gradient` | `image2dng validate` | `0.1.0` | `uv run image2dng validate fixture.dng --json` | `passed` | JSON report | Windows, Python 3.12 | Structural baseline |
| `srgb-gradient` | `exiftool` | `not installed` | optional smoke | `skipped` | `skipped: not found` | Windows, Python 3.12 | Optional local tool |
| `srgb-gradient` | Adobe DNG SDK | manual-only | manual SDK validation | `manual-only` | pending | local workstation | Not a CI gate |

Recommended fixtures:

- `srgb-gradient`
- `linear-rec709-gradient`
- `acescg-gradient`
- `xyz-gradient`
- `linear-rec709-cfa-rggb`
- `linear-rec709-cfa-rggb-noisy`

Each fixture should use a small deterministic RGB gradient with channel ramps, near-black patches, and near-white patches so channel order, clipping, black level, white level, and transfer assumptions remain visible.

CFA fixtures should additionally record the selected CFA pattern and confirm that the raw buffer is single-channel with `CFARepeatPatternDim = 2,2` and a four-entry `CFAPattern`.

Sensor-effect fixtures should record the enabled effect parameters and deterministic seed so generated outputs can be reproduced.
