from __future__ import annotations

import argparse
import hashlib
import json
import platform
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from image2dng import __version__

REPORT_SCHEMA = "image2dng.adobe_dng_sdk_manual_validation_plan.v1"
REPORT_STATUSES = {"missing-sdk-validate-project", "prepared"}
POLICY_VALUES = {
    "archive_extraction": {"not-attempted"},
    "sdk_build": {"manual-only"},
    "sdk_execution": {"not-attempted"},
}

DEFAULT_FIXTURE_CANDIDATES = (
    Path("demo-output/review-bundle/artifacts/representative-dng"),
    Path("demo-output/review-bundle-phase6/artifacts/representative-dng"),
    Path("demo-output/adobe-dng-converter-verification/source-dng"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a local-only Adobe DNG SDK manual validation plan without "
            "extracting, building, installing, or executing SDK files."
        )
    )
    parser.add_argument(
        "--adobe-dir",
        type=Path,
        default=Path("Adobe"),
        help="local Adobe resource cache to inspect",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/adobe-dng-sdk-manual-validation"),
        help="local-only report directory; defaults under demo-output/",
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing the manual validation plan outside demo-output/",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        action="append",
        help="representative DNG fixture directory to include; repeat for multiple dirs",
    )
    args = parser.parse_args(argv)

    if not _output_dir_is_allowed(args.output_dir) and not args.allow_output_outside_demo_output:
        print(
            "Refusing to write Adobe DNG SDK manual validation plan outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override.",
            flush=True,
        )
        return 2

    fixture_dirs = tuple(args.fixture_dir) if args.fixture_dir else DEFAULT_FIXTURE_CANDIDATES
    report = build_report(args.adobe_dir, fixture_dirs=fixture_dirs)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "adobe-dng-sdk-manual-validation-plan.json"
    summary_path = args.output_dir / "adobe-dng-sdk-manual-validation-plan.md"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.write_text(_summary_markdown(report), encoding="utf-8")
    print(f"Wrote Adobe DNG SDK manual validation plan to {report_path}")
    print(f"Wrote Adobe DNG SDK manual validation summary to {summary_path}")
    return 0 if report["ok"] else 1


def build_report(adobe_dir: Path, *, fixture_dirs: tuple[Path, ...]) -> dict[str, Any]:
    sdk_archives = _sdk_archives(Path(adobe_dir))
    selected = next(
        (
            archive
            for archive in sdk_archives
            if archive["zip"]["findings"]["dng_validate_solution"]
            and archive["zip"]["findings"]["dng_validate_project"]
        ),
        None,
    )
    fixture_records = _fixture_records(fixture_dirs)
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "ok": selected is not None,
        "status": "prepared" if selected is not None else "missing-sdk-validate-project",
        "local_only": True,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "policy": {
            "archive_extraction": "not-attempted",
            "sdk_build": "manual-only",
            "sdk_execution": "not-attempted",
            "ci_gate": False,
        },
        "adobe_dir": _display_path(Path(adobe_dir)),
        "selected_sdk_archive": selected,
        "sdk_archives": sdk_archives,
        "representative_fixtures": fixture_records,
        "manual_steps": _manual_steps(selected),
    }


def _sdk_archives(adobe_dir: Path) -> list[dict[str, Any]]:
    if not adobe_dir.is_dir():
        return []
    records = []
    for path in sorted(adobe_dir.glob("dng_sdk*.zip"), key=lambda item: item.name.lower()):
        records.append(
            {
                "name": path.name,
                "path": _display_path(path),
                "size_bytes": path.stat().st_size,
                "zip": _zip_summary(path),
            }
        )
    return records


def _zip_summary(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as exc:
        return {
            "ok": False,
            "entry_count": 0,
            "sample_entries": [],
            "findings": {
                "dng_validate_solution": False,
                "dng_validate_project": False,
            },
            "error": f"bad zip file: {exc}",
        }

    normalized = [name.replace("\\", "/").lower() for name in names]
    return {
        "ok": True,
        "entry_count": len(names),
        "sample_entries": names[:40],
        "findings": {
            "dng_validate_solution": any(name.endswith("dng_validate.sln") for name in normalized),
            "dng_validate_project": any(
                name.endswith("dng_validate/dng_validate.vcxproj") for name in normalized
            ),
        },
    }


def _fixture_records(fixture_dirs: tuple[Path, ...]) -> list[dict[str, Any]]:
    records = []
    for directory in fixture_dirs:
        dngs = sorted(directory.glob("**/*.dng")) if directory.is_dir() else []
        records.append(
            {
                "directory": _display_path(directory),
                "exists": directory.is_dir(),
                "dng_count": len(dngs),
                "sample_dngs": [_dng_fixture_record(path) for path in dngs[:20]],
            }
        )
    return records


def _dng_fixture_record(path: Path) -> dict[str, Any]:
    return {
        "path": _display_path(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _manual_steps(selected: dict[str, Any] | None) -> list[dict[str, Any]]:
    sdk_archive = selected["path"] if selected is not None else "<missing dng_sdk*.zip>"
    return [
        {
            "step": "extract-sdk",
            "manual_only": True,
            "command_template": (
                "Expand-Archive -LiteralPath "
                f"'{sdk_archive}' -DestinationPath '<local ignored sdk workspace>'"
            ),
        },
        {
            "step": "build-dng-validate",
            "manual_only": True,
            "command_template": (
                "MSBuild '<local sdk workspace>\\dng_sdk\\projects\\win\\dng_validate.sln' "
                "/p:Configuration=Release /p:Platform=x64"
            ),
        },
        {
            "step": "run-dng-validate",
            "manual_only": True,
            "command_template": (
                "'<local sdk workspace>\\dng_validate.exe' "
                "'<representative image2dng fixture.dng>'"
            ),
        },
    ]


def _summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Adobe DNG SDK Manual Validation Plan",
        "",
        f"- Schema: `{_report_schema(report)}`",
        f"- OK: `{str(_report_ok(report)).lower()}`",
        f"- Status: `{_report_status(report)}`",
        f"- Local-only: `{str(_local_only(report)).lower()}`",
        "",
        "## Policy",
        "",
    ]
    for key, value in _policy(report).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Selected SDK Archive", ""])
    selected = _selected_sdk_archive(report)
    if selected is None:
        lines.append("- none")
    else:
        lines.append(f"- `{_selected_sdk_archive_path(selected)}`")
    lines.extend(["", "## Representative Fixtures", ""])
    for fixture in _representative_fixtures(report):
        lines.append(
            f"- `{_fixture_directory(fixture)}`: exists={_fixture_exists(fixture)}, "
            f"dng_count={_fixture_dng_count(fixture)}"
        )
        for sample in _fixture_sample_dngs(fixture):
            lines.append(
                f"  - `{_sample_path(sample)}`: {_sample_size_bytes(sample)} bytes, "
                f"`{_sample_sha256(sample)}`"
            )
    lines.extend(["", "## Manual Steps", ""])
    for item in _manual_step_records(report):
        lines.append(f"- `{item['step']}`: `{item['command_template']}`")
    lines.append("")
    return "\n".join(lines)


def _report_schema(report: dict[str, Any]) -> str:
    schema = report["schema"]
    if not isinstance(schema, str):
        raise TypeError("report schema must be a string")
    return schema


def _report_ok(report: dict[str, Any]) -> bool:
    ok = report["ok"]
    if not isinstance(ok, bool):
        raise TypeError("report ok must be a boolean")
    return ok


def _report_status(report: dict[str, Any]) -> str:
    status = report["status"]
    if not isinstance(status, str):
        raise TypeError("report status must be a string")
    if status not in REPORT_STATUSES:
        raise TypeError("report status must be missing-sdk-validate-project or prepared")
    return status


def _local_only(report: dict[str, Any]) -> bool:
    local_only = report["local_only"]
    if not isinstance(local_only, bool):
        raise TypeError("report local_only must be a boolean")
    return local_only


def _policy(report: dict[str, Any]) -> dict[str, Any]:
    policy = report["policy"]
    if not isinstance(policy, dict):
        raise TypeError("report policy must be an object")
    for key, allowed_values in POLICY_VALUES.items():
        value = policy[key]
        if not isinstance(value, str):
            raise TypeError(f"report policy {key} must be a string")
        if value not in allowed_values:
            allowed = " or ".join(sorted(allowed_values))
            raise TypeError(f"report policy {key} must be {allowed}")
    ci_gate = policy["ci_gate"]
    if not isinstance(ci_gate, bool):
        raise TypeError("report policy ci_gate must be a boolean")
    if ci_gate:
        raise TypeError("report policy ci_gate must be false")
    return policy


def _selected_sdk_archive(report: dict[str, Any]) -> dict[str, Any] | None:
    selected = report["selected_sdk_archive"]
    if selected is None or isinstance(selected, dict):
        return selected
    raise TypeError("report selected_sdk_archive must be an object or null")


def _selected_sdk_archive_path(selected: dict[str, Any]) -> str:
    path = selected["path"]
    if not isinstance(path, str):
        raise TypeError("selected SDK archive path must be a string")
    return path


def _representative_fixtures(report: dict[str, Any]) -> list[dict[str, Any]]:
    fixtures = report["representative_fixtures"]
    if not isinstance(fixtures, list) or not all(isinstance(item, dict) for item in fixtures):
        raise TypeError("report representative_fixtures must be an object list")
    return fixtures


def _manual_step_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    steps = report["manual_steps"]
    if not isinstance(steps, list) or not all(isinstance(item, dict) for item in steps):
        raise TypeError("report manual_steps must be an object list")
    return steps


def _fixture_directory(fixture: dict[str, Any]) -> str:
    directory = fixture["directory"]
    if not isinstance(directory, str):
        raise TypeError("fixture directory must be a string")
    return directory


def _fixture_exists(fixture: dict[str, Any]) -> bool:
    exists = fixture["exists"]
    if not isinstance(exists, bool):
        raise TypeError("fixture exists must be a boolean")
    return exists


def _fixture_dng_count(fixture: dict[str, Any]) -> int:
    dng_count = fixture["dng_count"]
    if not isinstance(dng_count, int) or isinstance(dng_count, bool):
        raise TypeError("fixture dng_count must be an integer")
    return dng_count


def _fixture_sample_dngs(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    samples = fixture["sample_dngs"]
    if not isinstance(samples, list) or not all(isinstance(item, dict) for item in samples):
        raise TypeError("fixture sample_dngs must be an object list")
    return samples


def _sample_path(sample: dict[str, Any]) -> str:
    path = sample["path"]
    if not isinstance(path, str):
        raise TypeError("sample path must be a string")
    return path


def _sample_size_bytes(sample: dict[str, Any]) -> int:
    size_bytes = sample["size_bytes"]
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
        raise TypeError("sample size_bytes must be an integer")
    return size_bytes


def _sample_sha256(sample: dict[str, Any]) -> str:
    sha256 = sample["sha256"]
    if not isinstance(sha256, str):
        raise TypeError("sample sha256 must be a string")
    return sha256


def _display_path(path: Path) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def _output_dir_is_allowed(output_dir: Path) -> bool:
    repo_root = Path(__file__).resolve().parents[1]
    demo_output = (repo_root / "demo-output").resolve()
    resolved = output_dir.resolve()
    return resolved == demo_output or demo_output in resolved.parents


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
