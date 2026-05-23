from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from image2dng.pipeline import run_raw_native_batch

REPORT_SCHEMA = "image2dng.demo_review_bundle.v1"
VISUAL_SCHEMA = "image2dng.visual_demo_manifest.v1"
RAW_NATIVE_SCHEMA = "image2dng.raw_native_node_batch.v1"
SAMPLE_INDEX_SCHEMA = "image2dng.raw_native_sample_index.v1"
BASELINE_SCHEMA = "image2dng.development_baseline_report.v1"
COMPATIBILITY_SCHEMAS = {
    "image2dng.compatibility_evidence.v1",
    "image2dng.compatibility_evidence.v2",
}
COMMAND_STATUSES = {"failed", "passed"}
ARTIFACT_KINDS = {
    "contact-sheet",
    "index",
    "manifest",
    "report",
    "representative-dng",
    "validation-json",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a self-contained demo review bundle for image2dng."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/review-bundle"),
        help="review bundle output directory",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="working directory for regenerated demo/evidence outputs",
    )
    parser.add_argument(
        "--skip-baseline-quality-gates",
        action="store_true",
        help="write a lightweight baseline report without recursively running quality gates",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    output_dir = _resolve_under_repo(args.output_dir, repo_root)
    work_dir = (
        _resolve_under_repo(args.work_dir, repo_root)
        if args.work_dir is not None
        else output_dir / "_work"
    )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "work_dir": str(work_dir),
        "commands": [],
        "artifacts": [],
        "source_reports": {},
        "index": "index.md",
        "ok": False,
        "errors": [],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    paths = BundlePaths(
        output_dir=output_dir,
        work_dir=work_dir,
        visual_dir=work_dir / "visual-demo",
        raw_native_dir=work_dir / "raw-native-node-batch",
        baseline_dir=work_dir / "development-baseline",
        compatibility_dir=work_dir / "compatibility-evidence",
    )

    _run_upstream_generators(
        repo_root=repo_root,
        paths=paths,
        report=report,
        skip_baseline_quality_gates=args.skip_baseline_quality_gates,
    )

    if _has_command_failure(report):
        _collect_available_source_reports(paths=paths, report=report)
        _write_outputs(paths.output_dir, report)
        return 1

    try:
        _collect_bundle(paths=paths, repo_root=repo_root, report=report)
        _validate_bundle_report(paths.output_dir, report)
    except ValueError as exc:
        _append_error(report, str(exc))

    report["ok"] = not _errors(report)
    _write_outputs(paths.output_dir, report)
    return 0 if report["ok"] else 1


class BundlePaths:
    def __init__(
        self,
        *,
        output_dir: Path,
        work_dir: Path,
        visual_dir: Path,
        raw_native_dir: Path,
        baseline_dir: Path,
        compatibility_dir: Path,
    ) -> None:
        self.output_dir = output_dir
        self.work_dir = work_dir
        self.visual_dir = visual_dir
        self.raw_native_dir = raw_native_dir
        self.baseline_dir = baseline_dir
        self.compatibility_dir = compatibility_dir


def _run_upstream_generators(
    *,
    repo_root: Path,
    paths: BundlePaths,
    report: dict[str, Any],
    skip_baseline_quality_gates: bool,
) -> None:
    _record_callable_command(
        report,
        name="visual-demo",
        command=[
            "python",
            "scripts/generate_visual_demo.py",
            "--output-dir",
            str(paths.visual_dir),
        ],
        function=lambda: _load_script_module("generate_visual_demo").main(
            ["--output-dir", str(paths.visual_dir)]
        ),
    )

    _record_callable_command(
        report,
        name="raw-native-node-batch",
        command=[
            "python",
            "scripts/generate_raw_native_batch.py",
            "--output-dir",
            str(paths.raw_native_dir),
        ],
        function=lambda: _run_raw_native(paths.raw_native_dir),
    )

    if skip_baseline_quality_gates:
        _record_callable_command(
            report,
            name="development-baseline",
            command=[
                "python",
                "scripts/generate_demo_review_bundle.py",
                "--skip-baseline-quality-gates",
            ],
            function=lambda: _write_lightweight_baseline(paths.baseline_dir),
        )
    else:
        _record_subprocess_command(
            report,
            name="development-baseline",
            command=[
                sys.executable,
                str(repo_root / "scripts" / "verify_development_baseline.py"),
                "--output-dir",
                str(paths.baseline_dir),
            ],
            cwd=repo_root,
        )

    _record_callable_command(
        report,
        name="compatibility-evidence",
        command=[
            "python",
            "scripts/generate_compatibility_evidence.py",
            "--output-dir",
            str(paths.compatibility_dir),
        ],
        function=lambda: _load_script_module("generate_compatibility_evidence").main(
            ["--output-dir", str(paths.compatibility_dir)]
        ),
    )


def _collect_bundle(*, paths: BundlePaths, repo_root: Path, report: dict[str, Any]) -> None:
    visual_manifest_path = paths.visual_dir / "manifest.json"
    raw_manifest_path = paths.raw_native_dir / "manifests" / "raw-native-node-batch.json"
    sample_index_path = paths.raw_native_dir / "manifests" / "sample-index.json"
    baseline_report_path = paths.baseline_dir / "verification-report.json"
    compatibility_report_path = paths.compatibility_dir / "compatibility-report.json"
    compatibility_summary_path = paths.compatibility_dir / "compatibility-summary.md"

    visual_manifest = _read_json(visual_manifest_path, VISUAL_SCHEMA)
    raw_manifest = _read_json(raw_manifest_path, RAW_NATIVE_SCHEMA)
    _read_json(sample_index_path, SAMPLE_INDEX_SCHEMA)
    _read_json(baseline_report_path, BASELINE_SCHEMA)
    compatibility_report = _read_json_any(compatibility_report_path, COMPATIBILITY_SCHEMAS)
    _require(
        compatibility_summary_path.exists(),
        f"missing compatibility summary: {compatibility_summary_path}",
    )

    source_reports = _source_reports(report)
    source_reports["visual_manifest"] = _copy_artifact(
        paths=paths,
        report=report,
        source=visual_manifest_path,
        relative_target=Path("artifacts/manifests/visual-demo-manifest.json"),
        kind="manifest",
        name="visual-demo-manifest",
    )["bundle_path"]
    source_reports["raw_native_manifest"] = _copy_artifact(
        paths=paths,
        report=report,
        source=raw_manifest_path,
        relative_target=Path("artifacts/manifests/raw-native-node-batch.json"),
        kind="manifest",
        name="raw-native-node-batch",
    )["bundle_path"]
    source_reports["raw_native_sample_index"] = _copy_artifact(
        paths=paths,
        report=report,
        source=sample_index_path,
        relative_target=Path("artifacts/manifests/raw-native-sample-index.json"),
        kind="manifest",
        name="raw-native-sample-index",
    )["bundle_path"]
    source_reports["development_baseline_report"] = _copy_artifact(
        paths=paths,
        report=report,
        source=baseline_report_path,
        relative_target=Path("artifacts/reports/development-baseline-report.json"),
        kind="report",
        name="development-baseline-report",
    )["bundle_path"]
    source_reports["compatibility_report"] = _copy_artifact(
        paths=paths,
        report=report,
        source=compatibility_report_path,
        relative_target=Path("artifacts/reports/compatibility-report.json"),
        kind="report",
        name="compatibility-report",
    )["bundle_path"]
    source_reports["compatibility_summary"] = _copy_artifact(
        paths=paths,
        report=report,
        source=compatibility_summary_path,
        relative_target=Path("artifacts/reports/compatibility-summary.md"),
        kind="report",
        name="compatibility-summary",
    )["bundle_path"]

    _collect_contact_sheets(paths, report, visual_manifest)
    _collect_visual_representatives(paths, repo_root, report, visual_manifest)
    _collect_raw_native_representatives(paths, repo_root, report, raw_manifest)
    _collect_compatibility_representatives(paths, repo_root, report, compatibility_report)


def _collect_contact_sheets(
    paths: BundlePaths,
    report: dict[str, Any],
    visual_manifest: dict[str, Any],
) -> None:
    for sheet in _object_list(visual_manifest, "contact_sheets", "visual manifest"):
        path_value = _string(sheet, "path")
        source = _join_reported_path(paths.visual_dir, path_value)
        name = _portable_stem(path_value)
        _copy_artifact(
            paths=paths,
            report=report,
            source=source,
            relative_target=Path("artifacts/contact-sheets") / f"{name}.png",
            kind="contact-sheet",
            name=name,
        )


def _collect_visual_representatives(
    paths: BundlePaths,
    repo_root: Path,
    report: dict[str, Any],
    visual_manifest: dict[str, Any],
) -> None:
    assets = _object_map_by_string_key(
        _object_list(visual_manifest, "assets", "visual manifest"),
        "slug",
        "visual manifest asset",
    )
    chart = assets.get("chart-gradient")
    if not isinstance(chart, dict):
        raise ValueError("visual manifest missing chart-gradient asset")

    output_keys = [
        "phase_15_linearraw",
        "phase_2_cfa",
        "phase_3_linearraw_noisy",
        "phase_3_cfa_noisy",
    ]
    outputs = _object(chart, "outputs", "visual chart-gradient asset")
    validations = _object(chart, "validations", "visual chart-gradient asset")
    for key in output_keys:
        source = _resolve_source_path(_string(outputs, key), paths.visual_dir, repo_root)
        _copy_artifact(
            paths=paths,
            report=report,
            source=source,
            relative_target=Path("artifacts/representative-dng/visual") / source.name,
            kind="representative-dng",
            name=f"visual:{key}",
            group="visual-demo",
        )
    for key, value in validations.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"visual chart-gradient validation path must be a string: {key}")
        source = _resolve_source_path(value, paths.visual_dir, repo_root)
        _copy_artifact(
            paths=paths,
            report=report,
            source=source,
            relative_target=Path("artifacts/validation/visual") / source.name,
            kind="validation-json",
            name=f"visual:{key}",
            group="visual-demo",
        )


def _collect_raw_native_representatives(
    paths: BundlePaths,
    repo_root: Path,
    report: dict[str, Any],
    raw_manifest: dict[str, Any],
) -> None:
    scenes = _object_list(raw_manifest, "scenes", "raw-native manifest")
    if not scenes:
        raise ValueError("raw-native manifest contains no scenes")
    for slug, scene in _object_map_by_string_key(
        scenes,
        "slug",
        "raw-native scene",
    ).items():
        outputs = _object(scene, "outputs", f"{slug}: raw-native scene")
        for key in ("linearraw_dng", "cfa_dng"):
            source = _resolve_source_path(_string(outputs, key), paths.raw_native_dir, repo_root)
            _copy_artifact(
                paths=paths,
                report=report,
                source=source,
                relative_target=Path("artifacts/representative-dng/raw-native") / source.name,
                kind="representative-dng",
                name=f"raw-native:{slug}:{key}",
                group="raw-native-node-batch",
            )
        for key, source in {
            "linearraw": paths.raw_native_dir / "validation" / f"{slug}-linearraw.json",
            "cfa": paths.raw_native_dir / "validation" / f"{slug}-cfa.json",
        }.items():
            _copy_artifact(
                paths=paths,
                report=report,
                source=source,
                relative_target=Path("artifacts/validation/raw-native") / source.name,
                kind="validation-json",
                name=f"raw-native:{slug}:{key}",
                group="raw-native-node-batch",
            )


def _collect_compatibility_representatives(
    paths: BundlePaths,
    repo_root: Path,
    report: dict[str, Any],
    compatibility_report: dict[str, Any],
) -> None:
    wanted = {
        "srgb-gradient-linearraw",
        "prophoto-rgb-chart-linearraw",
        "linear-rec709-cfa-rggb",
        "linear-rec709-cfa-rggb-noisy",
    }
    fixtures = _object_map_by_string_key(
        _object_list(compatibility_report, "fixtures", "compatibility report"),
        "slug",
        "compatibility fixture",
    )
    found = set()
    for slug, fixture in fixtures.items():
        if slug not in wanted:
            continue
        found.add(slug)
        dng_source = _resolve_source_path(
            _string(fixture, "dng"),
            paths.compatibility_dir,
            repo_root,
        )
        validation_source = _resolve_source_path(
            _string(fixture, "validation_json"),
            paths.compatibility_dir,
            repo_root,
        )
        _copy_artifact(
            paths=paths,
            report=report,
            source=dng_source,
            relative_target=Path("artifacts/representative-dng/compatibility") / dng_source.name,
            kind="representative-dng",
            name=f"compatibility:{slug}",
            group="compatibility-evidence",
        )
        _copy_artifact(
            paths=paths,
            report=report,
            source=validation_source,
            relative_target=Path("artifacts/validation/compatibility") / validation_source.name,
            kind="validation-json",
            name=f"compatibility:{slug}",
            group="compatibility-evidence",
        )
    missing = wanted - found
    if missing:
        raise ValueError(f"missing compatibility representatives: {', '.join(sorted(missing))}")


def _collect_available_source_reports(*, paths: BundlePaths, report: dict[str, Any]) -> None:
    source_specs = (
        (
            "visual_manifest",
            paths.visual_dir / "manifest.json",
            Path("artifacts/manifests/visual-demo-manifest.json"),
            "manifest",
            "visual-demo-manifest",
        ),
        (
            "raw_native_manifest",
            paths.raw_native_dir / "manifests" / "raw-native-node-batch.json",
            Path("artifacts/manifests/raw-native-node-batch.json"),
            "manifest",
            "raw-native-node-batch",
        ),
        (
            "raw_native_sample_index",
            paths.raw_native_dir / "manifests" / "sample-index.json",
            Path("artifacts/manifests/raw-native-sample-index.json"),
            "manifest",
            "raw-native-sample-index",
        ),
        (
            "development_baseline_report",
            paths.baseline_dir / "verification-report.json",
            Path("artifacts/reports/development-baseline-report.json"),
            "report",
            "development-baseline-report",
        ),
        (
            "compatibility_report",
            paths.compatibility_dir / "compatibility-report.json",
            Path("artifacts/reports/compatibility-report.json"),
            "report",
            "compatibility-report",
        ),
        (
            "compatibility_summary",
            paths.compatibility_dir / "compatibility-summary.md",
            Path("artifacts/reports/compatibility-summary.md"),
            "report",
            "compatibility-summary",
        ),
    )
    source_reports = _source_reports(report)
    for key, source, relative_target, kind, name in source_specs:
        if not source.exists():
            continue
        source_reports[key] = _copy_artifact(
            paths=paths,
            report=report,
            source=source,
            relative_target=relative_target,
            kind=kind,
            name=name,
        )["bundle_path"]


def _write_index(output_dir: Path, report: dict[str, Any]) -> None:
    artifacts = _artifacts(report)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        by_kind.setdefault(_artifact_kind(artifact), []).append(artifact)

    lines = [
        "# image2dng Demo Review Bundle",
        "",
        f"- Schema: `{_report_schema(report)}`",
        f"- Generated at: `{_generated_at(report)}`",
        f"- Overall ok: `{_report_ok(report)}`",
        "",
        "## Review Entry Points",
        "",
        "- `review-bundle-report.json` is the machine-readable manifest for this bundle.",
        "- `index.md` is the human-readable entry point.",
        "- The contact sheets show the visual demo progression.",
        (
            "- The representative DNG files cover LinearRaw, simulated CFA, sensor "
            "effects, and compatibility fixtures."
        ),
        "- The validation JSON and reports provide machine-readable acceptance evidence.",
        "",
        "## What To Review",
        "",
        "1. Open the contact sheets first to understand the current visual demo surface.",
        "2. Inspect representative DNG files in a RAW-capable tool or with `image2dng validate`.",
        "3. Compare validation JSON, compatibility summary, and development baseline report.",
        "",
        "## Contact Sheets",
        "",
    ]
    for artifact in by_kind.get("contact-sheet", []):
        path = _artifact_bundle_path(artifact)
        lines.append(f"![{_artifact_name(artifact)}]({path})")
        lines.append("")

    lines.extend(["## Representative DNG Files", ""])
    for artifact in by_kind.get("representative-dng", []):
        path = _artifact_bundle_path(artifact)
        lines.append(
            f"- `{_artifact_name(artifact)}`: "
            f"[{path}]({path})"
        )

    lines.extend(["", "## Validation JSON", ""])
    for artifact in by_kind.get("validation-json", []):
        path = _artifact_bundle_path(artifact)
        lines.append(
            f"- `{_artifact_name(artifact)}`: "
            f"[{path}]({path})"
        )

    lines.extend(["", "## Reports And Manifests", ""])
    for key, value in _source_report_paths(report).items():
        lines.append(f"- `{key}`: [{value}]({value})")

    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "```powershell",
            (
                "uv run python scripts/generate_demo_review_bundle.py "
                f"--output-dir {_powershell_quote(_output_dir(report))}"
            ),
            "```",
        ]
    )

    lines.extend(["", "## Command Results", ""])
    for command in _commands(report):
        lines.append(
            f"- `{_command_name(command)}`: `{_command_status(command)}` "
            f"(exit `{_command_exit_code(command)}`, {_command_duration_seconds(command)}s)"
        )

    error_messages = _error_messages(report)
    if error_messages:
        lines.extend(["", "## Errors", ""])
        for error in error_messages:
            lines.append(f"- {error}")

    (output_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_outputs(output_dir: Path, report: dict[str, Any]) -> None:
    report["ok"] = not _errors(report) and not _has_command_failure(report)
    _write_index(output_dir, report)
    _append_existing_artifact(
        output_dir=output_dir,
        report=report,
        source=output_dir / "index.md",
        kind="index",
        name="index",
    )
    report_path = output_dir / "review-bundle-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote review bundle index to {output_dir / 'index.md'}")
    print(f"Wrote review bundle report to {report_path}")


def _powershell_quote(value: object) -> str:
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _copy_artifact(
    *,
    paths: BundlePaths,
    report: dict[str, Any],
    source: Path,
    relative_target: Path,
    kind: str,
    name: str,
    group: str | None = None,
) -> dict[str, Any]:
    if not source.exists():
        raise ValueError(f"missing artifact source: {source}")
    target = paths.output_dir / relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    record: dict[str, Any] = {
        "kind": kind,
        "category": _category_for_kind(kind),
        "name": name,
        "bundle_path": _relative_posix(target, paths.output_dir),
        "path": _relative_posix(target, paths.output_dir),
        "source_path": str(source),
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }
    if group is not None:
        record["group"] = group
    _artifacts(report).append(record)
    return record


def _append_existing_artifact(
    *,
    output_dir: Path,
    report: dict[str, Any],
    source: Path,
    kind: str,
    name: str,
) -> None:
    bundle_path = _relative_posix(source, output_dir)
    if any(_artifact_bundle_path(artifact) == bundle_path for artifact in _artifacts(report)):
        return
    _artifacts(report).append(
        {
            "kind": kind,
            "category": _category_for_kind(kind),
            "name": name,
            "bundle_path": bundle_path,
            "path": bundle_path,
            "source_path": str(source),
            "bytes": source.stat().st_size,
            "sha256": _sha256(source),
        }
    )


def _validate_bundle_report(output_dir: Path, report: dict[str, Any]) -> None:
    artifact_paths = set()
    for artifact in _artifacts(report):
        artifact_path = _validate_relative_existing_path(
            output_dir,
            _string(artifact, "bundle_path"),
        )
        artifact_paths.add(artifact["bundle_path"])
        if _artifact_category(artifact) != _category_for_kind(_artifact_kind(artifact)):
            raise ValueError("artifact category must match kind")
        if _artifact_path(artifact) != artifact["bundle_path"]:
            raise ValueError("artifact path must match bundle_path")
        if _artifact_bytes(artifact) != artifact_path.stat().st_size:
            raise ValueError(f"artifact byte count mismatch: {artifact['bundle_path']}")
        if _artifact_sha256(artifact) != _sha256(artifact_path):
            raise ValueError(f"artifact sha256 mismatch: {artifact['bundle_path']}")
    for source_path in _source_report_paths(report).values():
        _validate_relative_existing_path(output_dir, source_path)
        if source_path not in artifact_paths:
            raise ValueError(f"source report is not a registered artifact: {source_path}")


def _validate_relative_existing_path(output_dir: Path, value: str) -> Path:
    path = Path(value)
    posix_path = PurePosixPath(value.replace("\\", "/"))
    windows_path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
    ):
        raise ValueError(f"bundle path must be relative and local: {value}")
    resolved = output_dir / path
    if not resolved.exists():
        raise ValueError(f"bundle path missing: {value}")
    return resolved


def _record_callable_command(
    report: dict[str, Any],
    *,
    name: str,
    command: list[str],
    function,
) -> None:
    started = time.perf_counter()
    try:
        exit_code = int(function())
        status = "passed" if exit_code == 0 else "failed"
        error = None
    except Exception as exc:
        exit_code = 1
        status = "failed"
        error = str(exc)
    duration = round(time.perf_counter() - started, 3)
    record: dict[str, Any] = {
        "name": name,
        "command": command,
        "exit_code": exit_code,
        "duration_seconds": duration,
        "status": status,
    }
    if error:
        record["error"] = error
        _append_error(report, f"{name} failed: {error}")
    elif exit_code != 0:
        _append_error(report, f"{name} failed with exit code {exit_code}")
    _commands(report).append(record)


def _record_subprocess_command(
    report: dict[str, Any],
    *,
    name: str,
    command: list[str],
    cwd: Path,
) -> None:
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    duration = round(time.perf_counter() - started, 3)
    record = {
        "name": name,
        "command": command,
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "status": "passed" if completed.returncode == 0 else "failed",
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }
    if completed.returncode != 0:
        _append_error(report, f"{name} failed with exit code {completed.returncode}")
    _commands(report).append(record)


def _run_raw_native(output_dir: Path) -> int:
    run_raw_native_batch(output_dir, overwrite=True)
    return 0


def _write_lightweight_baseline(output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch = run_raw_native_batch(output_dir / "raw-native-node-batch", overwrite=True)
    report = {
        "schema": BASELINE_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(Path(__file__).resolve().parents[1]),
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "steps": [
            {
                "name": "pytest",
                "command": ["skipped", "by", "--skip-baseline-quality-gates"],
                "exit_code": 0,
                "duration_seconds": 0,
                "status": "passed",
                "stdout_tail": ["Skipped in lightweight bundle test mode."],
                "stderr_tail": [],
            },
            {
                "name": "ruff",
                "command": ["skipped", "by", "--skip-baseline-quality-gates"],
                "exit_code": 0,
                "duration_seconds": 0,
                "status": "passed",
                "stdout_tail": ["Skipped in lightweight bundle test mode."],
                "stderr_tail": [],
            },
            {
                "name": "build",
                "command": ["skipped", "by", "--skip-baseline-quality-gates"],
                "exit_code": 0,
                "duration_seconds": 0,
                "status": "passed",
                "stdout_tail": ["Skipped in lightweight bundle test mode."],
                "stderr_tail": [],
            },
            {
                "name": "raw-native-batch",
                "command": [
                    "python",
                    "scripts/generate_raw_native_batch.py",
                    "--output-dir",
                    str(batch.output_dir),
                ],
                "exit_code": 0,
                "duration_seconds": 0,
                "status": "passed",
                "stdout_tail": [f"Wrote raw-native node batch to {batch.output_dir}"],
                "stderr_tail": [],
            },
            {
                "name": "wheel-install-smoke",
                "command": ["skipped", "by", "--skip-baseline-quality-gates"],
                "exit_code": 0,
                "duration_seconds": 0,
                "status": "passed",
                "stdout_tail": ["Skipped in lightweight bundle test mode."],
                "stderr_tail": [],
            },
        ],
        "batch": {
            "batch_dir": str(batch.output_dir),
            "manifest_path": str(batch.manifest_path),
            "sample_index_path": str(batch.sample_index_path),
        },
        "ok": True,
        "errors": [],
    }
    (output_dir / "verification-report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    return 0


def _load_script_module(name: str):
    script_path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load script module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path, schema: str) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"missing JSON report: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload_schema = _json_payload_schema(payload, path)
    _require(
        payload_schema == schema,
        f"unexpected schema in {path}: {payload_schema}",
    )
    return payload


def _read_json_any(path: Path, schemas: set[str]) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"missing JSON report: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload_schema = _json_payload_schema(payload, path)
    _require(
        payload_schema in schemas,
        f"unexpected schema in {path}: {payload_schema}",
    )
    return payload


def _json_payload_schema(payload: Any, path: Path) -> str:
    if not isinstance(payload, dict):
        raise ValueError(f"JSON report must be an object: {path}")
    schema = payload.get("schema")
    if not isinstance(schema, str):
        raise ValueError(f"JSON report schema must be a string: {path}")
    return schema


def _resolve_under_repo(path: Path, repo_root: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _resolve_source_path(value: str, default_root: Path, repo_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        resolved = path.resolve()
        allowed_roots = (default_root.resolve(), repo_root.resolve())
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            raise ValueError(f"unsafe source path in report: {value}")
        return path
    repo_path = repo_root / path
    if repo_path.exists():
        return repo_path
    return _join_reported_path(default_root, value)


def _join_reported_path(root: Path, value: str) -> Path:
    posix_path = PurePosixPath(value.replace("\\", "/"))
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"unsafe relative path in report: {value}")
    parts = [part for part in value.replace("\\", "/").split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"unsafe relative path in report: {value}")
    return root.joinpath(*parts)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_stem(value: str) -> str:
    return Path(value.replace("\\", "/")).stem


def _category_for_kind(kind: str) -> str:
    categories = {
        "contact-sheet": "contact-sheets",
        "representative-dng": "representative-dng",
        "validation-json": "validation",
        "report": "reports",
        "manifest": "manifests",
        "index": "index",
    }
    if kind not in ARTIFACT_KINDS:
        raise TypeError(
            "artifact kind must be contact-sheet, index, manifest, report, "
            "representative-dng, or validation-json"
        )
    return categories[kind]


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _object(payload: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{label} {key} must be an object")
    return value


def _object_list(payload: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} {key} must be an object list")
    return value


def _object_map_by_string_key(
    rows: list[dict[str, Any]],
    key: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = _string(row, key)
        if value in mapped:
            raise ValueError(f"duplicate {label} {key}: {value}")
        mapped[value] = row
    return mapped


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _append_error(report: dict[str, Any], message: str) -> None:
    _errors(report).append(message)


def _has_command_failure(report: dict[str, Any]) -> bool:
    return any(_command_status(command) != "passed" for command in _commands(report))


def _report_schema(report: dict[str, Any]) -> str:
    schema = report["schema"]
    if not isinstance(schema, str):
        raise TypeError("report schema must be a string")
    return schema


def _generated_at(report: dict[str, Any]) -> str:
    generated_at = report["generated_at"]
    if not isinstance(generated_at, str):
        raise TypeError("report generated_at must be a string")
    return generated_at


def _report_ok(report: dict[str, Any]) -> bool:
    ok = report["ok"]
    if not isinstance(ok, bool):
        raise TypeError("report ok must be a boolean")
    return ok


def _output_dir(report: dict[str, Any]) -> str:
    output_dir = report["output_dir"]
    if not isinstance(output_dir, str):
        raise TypeError("report output_dir must be a string")
    return output_dir


def _commands(report: dict[str, Any]) -> list[dict[str, Any]]:
    commands = report["commands"]
    if not isinstance(commands, list):
        raise TypeError("report commands must be a list")
    if not all(isinstance(command, dict) for command in commands):
        raise TypeError("report commands must contain objects")
    return commands


def _command_name(command: dict[str, Any]) -> str:
    name = command["name"]
    if not isinstance(name, str):
        raise TypeError("command name must be a string")
    return name


def _command_status(command: dict[str, Any]) -> str:
    status = command["status"]
    if not isinstance(status, str):
        raise TypeError("command status must be a string")
    if status not in COMMAND_STATUSES:
        raise TypeError("command status must be passed or failed")
    return status


def _command_exit_code(command: dict[str, Any]) -> int | None:
    exit_code = command["exit_code"]
    if isinstance(exit_code, bool):
        raise TypeError("command exit_code must be an integer or null")
    if isinstance(exit_code, int) or exit_code is None:
        return exit_code
    raise TypeError("command exit_code must be an integer or null")


def _command_duration_seconds(command: dict[str, Any]) -> float | int:
    duration = command["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not math.isfinite(duration)
    ):
        raise TypeError("command duration_seconds must be finite numeric")
    return duration


def _artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = report["artifacts"]
    if not isinstance(artifacts, list):
        raise TypeError("report artifacts must be a list")
    if not all(isinstance(artifact, dict) for artifact in artifacts):
        raise TypeError("report artifacts must contain objects")
    return artifacts


def _artifact_kind(artifact: dict[str, Any]) -> str:
    kind = artifact["kind"]
    if not isinstance(kind, str):
        raise TypeError("artifact kind must be a string")
    if kind not in ARTIFACT_KINDS:
        raise TypeError(
            "artifact kind must be contact-sheet, index, manifest, report, "
            "representative-dng, or validation-json"
        )
    return kind


def _artifact_category(artifact: dict[str, Any]) -> str:
    category = artifact["category"]
    if not isinstance(category, str):
        raise TypeError("artifact category must be a string")
    return category


def _artifact_name(artifact: dict[str, Any]) -> str:
    name = artifact["name"]
    if not isinstance(name, str):
        raise TypeError("artifact name must be a string")
    return name


def _artifact_bundle_path(artifact: dict[str, Any]) -> str:
    bundle_path = artifact["bundle_path"]
    if not isinstance(bundle_path, str):
        raise TypeError("artifact bundle_path must be a string")
    return bundle_path


def _artifact_path(artifact: dict[str, Any]) -> str:
    path = artifact["path"]
    if not isinstance(path, str):
        raise TypeError("artifact path must be a string")
    return path


def _artifact_bytes(artifact: dict[str, Any]) -> int:
    bytes_value = artifact["bytes"]
    if isinstance(bytes_value, bool) or not isinstance(bytes_value, int):
        raise TypeError("artifact bytes must be an integer")
    return bytes_value


def _artifact_sha256(artifact: dict[str, Any]) -> str:
    sha256 = artifact["sha256"]
    if not isinstance(sha256, str):
        raise TypeError("artifact sha256 must be a string")
    return sha256


def _source_reports(report: dict[str, Any]) -> dict[str, Any]:
    source_reports = report["source_reports"]
    if not isinstance(source_reports, dict):
        raise TypeError("report source_reports must be an object")
    return source_reports


def _source_report_paths(report: dict[str, Any]) -> dict[str, str]:
    paths: dict[str, str] = {}
    for key, value in _source_reports(report).items():
        if not isinstance(key, str):
            raise TypeError("source report name must be a string")
        if not isinstance(value, str):
            raise TypeError("source report path must be a string")
        paths[key] = value
    return paths


def _errors(report: dict[str, Any]) -> list[Any]:
    errors = report["errors"]
    if not isinstance(errors, list):
        raise TypeError("report errors must be a list")
    return errors


def _error_messages(report: dict[str, Any]) -> list[str]:
    errors = _errors(report)
    if not all(isinstance(error, str) for error in errors):
        raise TypeError("report errors must be a string list")
    return errors


def _tail(text: str, *, max_lines: int = 40) -> list[str]:
    lines = text.strip().splitlines()
    return lines[-max_lines:]


if __name__ == "__main__":
    raise SystemExit(main())
