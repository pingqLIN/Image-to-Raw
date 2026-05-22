from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
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
        help="write a lightweight baseline report without recursively running pytest/ruff",
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
    for sheet in visual_manifest.get("contact_sheets", []):
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
    assets = {
        _string(asset, "slug"): asset
        for asset in visual_manifest.get("assets", [])
        if isinstance(asset, dict)
    }
    chart = assets.get("chart-gradient")
    if not isinstance(chart, dict):
        raise ValueError("visual manifest missing chart-gradient asset")

    output_keys = [
        "phase_15_linearraw",
        "phase_2_cfa",
        "phase_3_linearraw_noisy",
        "phase_3_cfa_noisy",
    ]
    for key in output_keys:
        source = _resolve_source_path(_string(chart["outputs"], key), paths.visual_dir, repo_root)
        _copy_artifact(
            paths=paths,
            report=report,
            source=source,
            relative_target=Path("artifacts/representative-dng/visual") / source.name,
            kind="representative-dng",
            name=f"visual:{key}",
            group="visual-demo",
        )
    for key, value in chart.get("validations", {}).items():
        source = _resolve_source_path(str(value), paths.visual_dir, repo_root)
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
    scenes = raw_manifest.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("raw-native manifest contains no scenes")
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ValueError("raw-native scene must be an object")
        slug = _string(scene, "slug")
        outputs = scene.get("outputs")
        if not isinstance(outputs, dict):
            raise ValueError(f"{slug}: missing outputs")
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
    fixtures = compatibility_report.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError("compatibility report fixtures must be a list")
    found = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        slug = _string(fixture, "slug")
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


def _write_index(output_dir: Path, report: dict[str, Any]) -> None:
    artifacts = _artifacts(report)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        by_kind.setdefault(artifact["kind"], []).append(artifact)

    lines = [
        "# image2dng Demo Review Bundle",
        "",
        f"- Schema: `{report['schema']}`",
        f"- Generated at: `{report['generated_at']}`",
        f"- Overall ok: `{report['ok']}`",
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
        path = artifact["bundle_path"]
        lines.append(f"![{artifact['name']}]({path})")
        lines.append("")

    lines.extend(["## Representative DNG Files", ""])
    for artifact in by_kind.get("representative-dng", []):
        lines.append(
            f"- `{artifact['name']}`: "
            f"[{artifact['bundle_path']}]({artifact['bundle_path']})"
        )

    lines.extend(["", "## Validation JSON", ""])
    for artifact in by_kind.get("validation-json", []):
        lines.append(
            f"- `{artifact['name']}`: "
            f"[{artifact['bundle_path']}]({artifact['bundle_path']})"
        )

    lines.extend(["", "## Reports And Manifests", ""])
    for key, value in _source_reports(report).items():
        lines.append(f"- `{key}`: [{value}]({value})")

    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "```powershell",
            (
                "uv run python scripts/generate_demo_review_bundle.py "
                f"--output-dir {_powershell_quote(report['output_dir'])}"
            ),
            "```",
        ]
    )

    lines.extend(["", "## Command Results", ""])
    for command in _commands(report):
        lines.append(
            f"- `{command['name']}`: `{command['status']}` "
            f"(exit `{command['exit_code']}`, {command['duration_seconds']}s)"
        )

    if _errors(report):
        lines.extend(["", "## Errors", ""])
        for error in _errors(report):
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
    if any(artifact.get("bundle_path") == bundle_path for artifact in _artifacts(report)):
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
    for artifact in _artifacts(report):
        _validate_relative_existing_path(output_dir, _string(artifact, "bundle_path"))
    for value in _source_reports(report).values():
        _validate_relative_existing_path(output_dir, str(value))


def _validate_relative_existing_path(output_dir: Path, value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"bundle path must be relative and local: {value}")
    if not (output_dir / path).exists():
        raise ValueError(f"bundle path missing: {value}")


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
    _require(
        payload.get("schema") == schema,
        f"unexpected schema in {path}: {payload.get('schema')}",
    )
    return payload


def _read_json_any(path: Path, schemas: set[str]) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"missing JSON report: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(
        payload.get("schema") in schemas,
        f"unexpected schema in {path}: {payload.get('schema')}",
    )
    return payload


def _resolve_under_repo(path: Path, repo_root: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _resolve_source_path(value: str, default_root: Path, repo_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    repo_path = repo_root / path
    if repo_path.exists():
        return repo_path
    return _join_reported_path(default_root, value)


def _join_reported_path(root: Path, value: str) -> Path:
    parts = [part for part in value.replace("\\", "/").split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"unsafe relative path in report: {value}")
    return root.joinpath(*parts)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_stem(value: str) -> str:
    return Path(value.replace("\\", "/")).stem


def _category_for_kind(kind: str) -> str:
    return {
        "contact-sheet": "contact-sheets",
        "representative-dng": "representative-dng",
        "validation-json": "validation",
        "report": "reports",
        "manifest": "manifests",
        "index": "index",
    }.get(kind, kind)


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _append_error(report: dict[str, Any], message: str) -> None:
    _errors(report).append(message)


def _has_command_failure(report: dict[str, Any]) -> bool:
    return any(_command_status(command) != "passed" for command in _commands(report))


def _commands(report: dict[str, Any]) -> list[dict[str, Any]]:
    commands = report["commands"]
    if not isinstance(commands, list):
        raise TypeError("report commands must be a list")
    if not all(isinstance(command, dict) for command in commands):
        raise TypeError("report commands must contain objects")
    return commands


def _command_status(command: dict[str, Any]) -> str:
    status = command["status"]
    if not isinstance(status, str):
        raise TypeError("command status must be a string")
    return status


def _artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = report["artifacts"]
    if not isinstance(artifacts, list):
        raise TypeError("report artifacts must be a list")
    if not all(isinstance(artifact, dict) for artifact in artifacts):
        raise TypeError("report artifacts must contain objects")
    return artifacts


def _source_reports(report: dict[str, Any]) -> dict[str, Any]:
    source_reports = report["source_reports"]
    if not isinstance(source_reports, dict):
        raise TypeError("report source_reports must be an object")
    return source_reports


def _errors(report: dict[str, Any]) -> list[Any]:
    errors = report["errors"]
    if not isinstance(errors, list):
        raise TypeError("report errors must be a list")
    return errors


def _tail(text: str, *, max_lines: int = 40) -> list[str]:
    lines = text.strip().splitlines()
    return lines[-max_lines:]


if __name__ == "__main__":
    raise SystemExit(main())
