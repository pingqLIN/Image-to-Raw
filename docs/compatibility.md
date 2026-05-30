# Compatibility Evidence

Traditional Chinese source manuscript: [docs/i18n/zh-TW/compatibility-evidence.md](i18n/zh-TW/compatibility-evidence.md)

This project validates generated DNG files structurally and records RAW processor compatibility evidence when optional local tools are available on `PATH`.

The current DNG tag contract is documented in [docs/i18n/en/dng-tag-contract.md](i18n/en/dng-tag-contract.md). Compatibility evidence should be interpreted against that contract: missing optional tools are environment state, while missing required tags are structural failures.

Missing optional tools are recorded as `skipped`, not as failures. Available tools that fail to run or fail to emit their expected export artifact are recorded as `failed`. CI must not require locally installed RAW processors unless a reproducible install path is added later. The evidence generator reports install hints as dry-run guidance only and never installs tools.

This product includes DNG technology under license by Adobe.

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

The setup audit never installs or upgrades RAW processor tools. Package search version hints are recorded only when the local package-manager output contains an exact package identity match. On Windows, Darktable and RawTherapee are resolved from `PATH` first and then from their standard install paths: `C:\Program Files\darktable\bin\darktable-cli.exe` and `C:\Program Files\RawTherapee\5.12\rawtherapee-cli.exe`; the audit report records the discovery source so reviewers can tell whether a temporary `PATH` override was needed. If a tool is approved and installed later, rerun the setup audit, compatibility evidence, and review bundle generators so the pre-install decision and post-install evidence are clearly separated.

## Adobe DNG SDK Manual Validation

Adobe DNG SDK remains local-only/manual-resource evidence. It is not a CI gate, and project scripts must not download, install, extract, or update it automatically.

When the SDK or validator has not been prepared locally yet, use this manual preparation workflow:

1. Confirm the current SDK and specification versions from Adobe's DNG page.
2. After explicit user approval, prepare the SDK or validator build in an isolated local location.
3. Run SDK validation against representative DNG files under `demo-output/review-bundle-*/artifacts/representative-dng/`.
4. Record the command, SDK version, fixture, exit code, stdout/stderr summary, and environment in local-only notes.
5. Rerun:

```powershell
uv run python scripts/generate_compatibility_evidence.py --output-dir demo-output/compatibility-evidence
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
```

When a local `dng_validate.exe` is already available, the repo can write a repeatable local report:

```powershell
uv run python scripts/run_adobe_dng_sdk_validation.py `
  --validator Adobe/dng_sdk_1_7_1/dng_sdk/targets/win/release64_x64/dng_validate.exe `
  --fixture-dir demo-output/review-bundle-phase6/artifacts/representative-dng `
  --output-dir demo-output/adobe-dng-sdk-validation
```

To consolidate the Adobe resource audit, project DNG fixture generation, Adobe DNG Converter regression, and SDK validation into one local report, run:

```powershell
uv run python scripts/verify_adobe_validation_stack.py --dry-run-converter
uv run python scripts/verify_adobe_validation_stack.py --output-dir demo-output/adobe-validation-stack
```

`--dry-run-converter` proves fixture and reporting paths only; it does not prove full Adobe readiness. Final evidence should be rerun after the user has prepared the converter and validator.

Constraints:

- The SDK validation path must be locally configurable and must not hard-code a private machine path.
- Until a reproducible SDK path exists, Adobe DNG SDK entries in `compatibility-report.json` should remain `manual-only`.
- Any SDK or Adobe-tool structural failure should become a blocking compatibility finding for the next Phase 6 pass.

## Adobe DNG Converter Regression

Adobe DNG Converter has a separate local regression script instead of being mixed into the generic optional RAW processor matrix:

```powershell
uv run python scripts/verify_adobe_dng_converter.py --dry-run
uv run python scripts/verify_adobe_dng_converter.py --output-dir demo-output/adobe-dng-converter-verification
```

The script generates a deterministic `single-raw-ifd` fixture, validates the source DNG with the strict `image2dng` contract validator, discovers Adobe DNG Converter, runs the conversion, and checks that the converted output exists. The converted artifact is inspected with the Adobe-converted artifact layer rather than the strict writer contract.

Validation layers are intentionally separate:

- `image2dng` contract validation: strict checks for DNG tags, XMP provenance, raw geometry, and byte-count expectations on files directly written by this project.
- external processor smoke validation: optional tool acceptance and output-artifact checks.
- Adobe-converted artifact inspection: relaxed inspection for Adobe-rewritten DNG files, requiring parseable DNG structure, a raw IFD, camera identity, and basic geometry while allowing Adobe metadata/layout rewrites.

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

Adobe DNG SDK remains local-only/manual-resource evidence and the `compatibility-report.json` matrix entry remains `manual-only` until a reproducible public gate policy exists.
