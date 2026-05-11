# Compatibility Evidence

This project validates generated DNG files structurally and records RAW processor compatibility evidence when optional local tools are available on `PATH`.

Missing optional tools are recorded as `skipped`, not as failures. Available tools that fail to run or fail to emit their expected export artifact are recorded as `failed`. CI must not require locally installed RAW processors unless a reproducible install path is added later. The evidence generator reports install hints as dry-run guidance only and never installs tools.

This product includes DNG technology under license by Adobe.

Traditional Chinese source manuscript: [docs/i18n/zh-TW/compatibility-evidence.md](i18n/zh-TW/compatibility-evidence.md).

## Generate Compatibility Evidence

```powershell
uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence
```

The evidence generator emits deterministic local fixtures and two reports:

- `compatibility-report.json`: machine-readable report using `image2dng.compatibility_evidence.v2`.
- `compatibility-summary.md`: human-readable evidence matrix.

The generated fixtures live under `demo-output/compatibility-evidence/` and should not be committed as binary artifacts.

## Audit RAW Processor Setup

```powershell
uv run python scripts/audit_raw_processor_setup.py --output-dir demo-output/raw-processor-setup-audit
```

The setup audit is a dry-run package for humans or external reviewers. It detects whether `dcraw`, `darktable-cli`, and `rawtherapee-cli` are currently available, records package-manager search evidence from local managers such as winget, Scoop, and Chocolatey when available, and writes:

- `setup-audit-report.json`: machine-readable tool state, package search evidence, install policy, and expected matrix changes.
- `setup-runbook.md`: human-readable post-approval install and rerun guide.
- `external-review-prompt.md`: prompt for external review of recommended smoke targets.

The setup audit never installs or upgrades RAW processor tools. Package search version hints are recorded only when the local package-manager output contains an exact package identity match. On Windows, Darktable is resolved from `PATH` first and then from the standard install path `C:\Program Files\darktable\bin\darktable-cli.exe`; the audit report records the discovery source so reviewers can tell whether a temporary `PATH` override was needed. If a tool is approved and installed later, rerun the setup audit, compatibility evidence, and review bundle generators so the pre-install decision and post-install evidence are clearly separated.

## Validator Contract

Default output is human-readable. `image2dng validate --json` emits a structured report with:

- `ok`: overall structural success.
- `checks`: individual checks with `passed`, `failed`, `skipped`, or `warning`.
- `errors`: validation failures.
- `warnings`: non-fatal validation concerns.
- `smoke_tests`: compatibility smoke results by tool for the `image2dng validate` CLI.

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
| `srgb-gradient` | `exiftool` | `13.57` | `exiftool fixture.dng` | `passed` | JSON report | Windows, Python 3.12 | Parsed metadata |
| `srgb-gradient` | `darktable-cli` | `not available` | `darktable-cli fixture.dng out.tif` | `skipped` | JSON report | Windows, Python 3.12 | skipped: not found |
| `srgb-gradient` | `adobe-dng-sdk` | manual-only | manual SDK validation | `manual-only` | JSON report | local workstation | Not a CI gate |

Recommended fixtures:

- `srgb-gradient`
- `linear-rec709-gradient`
- `acescg-gradient`
- `xyz-gradient`
- `prophoto-rgb-chart`
- `linear-rec709-cfa-rggb`
- `linear-rec709-cfa-rggb-noisy`

Each fixture should use a small deterministic RGB gradient with channel ramps, near-black patches, and near-white patches so channel order, clipping, black level, white level, and transfer assumptions remain visible.

CFA fixtures should additionally record the selected CFA pattern and confirm that the raw buffer is single-channel with `CFARepeatPatternDim = 2,2` and a four-entry `CFAPattern`.

Sensor-effect fixtures should record the enabled effect parameters and deterministic seed so generated outputs can be reproduced.

## Automated Evidence Report

`compatibility-report.json` records:

- environment: platform, Python version, and `image2dng` version;
- tool inventory: availability, executable path, version command, version, timeout policy, and dry-run install hint;
- install policy: no automatic installation, missing tool policy, and available tool failure policy;
- fixtures: input path, DNG path, validation JSON path, structural validation status, and processor result records;
- matrix: fixture, tool, command, result, evidence path, environment, notes, exit code, duration, and output artifacts.

Adobe DNG SDK remains `manual-only` until a reproducible local SDK validation path exists.
