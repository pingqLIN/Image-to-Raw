from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from image2dng import __version__

REPORT_SCHEMA = "image2dng.adobe_validation_stack_report.v1"
STEP_STATUSES = {"failed", "passed"}

DEFAULT_OUTPUT_DIR = Path("demo-output/adobe-validation-stack")
DEFAULT_ADOBE_DIR = Path("Adobe")
DEFAULT_VALIDATOR = Path(
    "Adobe/dng_sdk_1_7_1/dng_sdk/targets/win/release64_x64/dng_validate.exe"
)
EXISTING_FIXTURE_CANDIDATES = (
    Path("demo-output/review-bundle-phase6/artifacts/representative-dng"),
    Path("demo-output/adobe-compatible-user-samples/adobe-converted"),
)
TAIL_LINE_LIMIT = 40
TAIL_LINE_WIDTH = 300


class Runner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: float,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        ...


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the local-only Adobe Validation Stack and write DNG evidence."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="local-only stack report directory; defaults under demo-output/",
    )
    parser.add_argument(
        "--adobe-dir",
        type=Path,
        default=DEFAULT_ADOBE_DIR,
        help="local Adobe resource cache to inspect",
    )
    parser.add_argument(
        "--converter",
        type=Path,
        help="explicit path to Adobe DNG Converter.exe",
    )
    parser.add_argument(
        "--validator",
        type=Path,
        default=DEFAULT_VALIDATOR,
        help="path to the locally built dng_validate.exe",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="timeout for each child validation step",
    )
    parser.add_argument(
        "--dry-run-converter",
        action="store_true",
        help="generate converter fixture evidence but do not invoke Adobe DNG Converter",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    output_dir = _resolve_from_repo(args.output_dir, repo_root)
    adobe_dir = _resolve_from_repo(args.adobe_dir, repo_root)
    converter = _resolve_from_repo(args.converter, repo_root) if args.converter else None
    validator = _resolve_from_repo(args.validator, repo_root)

    output_error = _output_dir_refusal_reason(output_dir, repo_root=repo_root)
    if output_error is not None:
        print(output_error, flush=True)
        return 2

    report = build_report(
        output_dir=output_dir,
        adobe_dir=adobe_dir,
        converter=converter,
        validator=validator,
        timeout_seconds=max(0.001, args.timeout_seconds),
        dry_run_converter=args.dry_run_converter,
        repo_root=repo_root,
        runner=subprocess.run,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "adobe-validation-stack-report.json"
    summary_path = output_dir / "adobe-validation-stack-report.md"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.write_text(_summary_markdown(report), encoding="utf-8")
    print(f"Wrote Adobe Validation Stack report to {report_path}")
    print(f"Wrote Adobe Validation Stack summary to {summary_path}")
    return 0 if report["ok"] else 1


def build_report(
    *,
    output_dir: Path,
    adobe_dir: Path,
    converter: Path | None,
    validator: Path,
    timeout_seconds: float,
    dry_run_converter: bool,
    repo_root: Path | None = None,
    runner: Runner,
) -> dict[str, Any]:
    repo_root = (repo_root or _repo_root()).resolve()
    output_dir = output_dir.resolve()
    adobe_dir = adobe_dir.resolve()
    converter = converter.resolve() if converter is not None else None
    validator = validator.resolve()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")

    child_dirs = {
        "resource-audit": output_dir / "adobe-local-resource-audit",
        "project-dng-fixtures": output_dir / "project-dng-fixtures" / "raw-native-node-batch",
        "adobe-dng-converter": output_dir / "adobe-dng-converter-verification",
        "adobe-dng-sdk-validation": output_dir / "adobe-dng-sdk-validation",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for child_dir in child_dirs.values():
        _move_existing_output_aside(child_dir)

    steps: list[dict[str, Any]] = []
    blocking_findings: list[str] = []

    resource_step = _run_child_step(
        name="resource-audit",
        command=[
            sys.executable,
            str(repo_root / "scripts" / "audit_adobe_local_resources.py"),
            "--adobe-dir",
            str(adobe_dir),
            "--output-dir",
            str(child_dirs["resource-audit"]),
        ],
        report_path=child_dirs["resource-audit"] / "adobe-local-resource-report.json",
        expected_schema="image2dng.adobe_local_resource_audit.v1",
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    steps.append(resource_step)
    _extend_blocking_findings(blocking_findings, resource_step)

    project_step = _run_project_dng_fixture_step(
        output_dir=child_dirs["project-dng-fixtures"],
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    steps.append(project_step)
    _extend_blocking_findings(blocking_findings, project_step)
    project_fixture_roots = _project_fixture_roots(project_step)

    converter_command = [
        sys.executable,
        str(repo_root / "scripts" / "verify_adobe_dng_converter.py"),
        "--output-dir",
        str(child_dirs["adobe-dng-converter"]),
        "--adobe-dir",
        str(adobe_dir),
        "--timeout-seconds",
        str(max(1, int(timeout_seconds))),
    ]
    if converter is not None:
        converter_command.extend(["--converter", str(converter)])
    if dry_run_converter:
        converter_command.append("--dry-run")
    converter_step = _run_child_step(
        name="adobe-dng-converter",
        command=converter_command,
        report_path=(
            child_dirs["adobe-dng-converter"]
            / "reports"
            / "adobe-dng-converter-report.json"
        ),
        expected_schema="image2dng.adobe_dng_converter_verification.v1",
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    steps.append(converter_step)
    _extend_blocking_findings(blocking_findings, converter_step)
    if dry_run_converter:
        blocking_findings.append(
            "Adobe DNG Converter dry-run mode does not prove full Adobe readiness"
        )

    sdk_fixture_roots = _sdk_fixture_roots(
        output_dir=output_dir,
        project_fixture_roots=project_fixture_roots,
        repo_root=repo_root,
    )
    sdk_command = [
        sys.executable,
        str(repo_root / "scripts" / "run_adobe_dng_sdk_validation.py"),
        "--validator",
        str(validator),
        "--output-dir",
        str(child_dirs["adobe-dng-sdk-validation"]),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    for fixture_root in sdk_fixture_roots:
        sdk_command.extend(["--fixture-dir", str(fixture_root)])
    sdk_step = _run_child_step(
        name="adobe-dng-sdk-validation",
        command=sdk_command,
        report_path=(
            child_dirs["adobe-dng-sdk-validation"]
            / "adobe-dng-sdk-validation-report.json"
        ),
        expected_schema="image2dng.adobe_dng_sdk_validation_report.v1",
        repo_root=repo_root,
        timeout_seconds=max(timeout_seconds * max(1, len(sdk_fixture_roots)), timeout_seconds),
        runner=runner,
    )
    sdk_step["fixture_roots"] = [
        _path_record(path.resolve(), repo_root) for path in sdk_fixture_roots
    ]
    steps.append(sdk_step)
    _extend_blocking_findings(blocking_findings, sdk_step)

    blocking_findings = _dedupe(blocking_findings)
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "ok": not blocking_findings,
        "local_only": True,
        "repo_root": _display_path(repo_root, repo_root),
        "output_dir": _path_record(output_dir, repo_root),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "image2dng_version": __version__,
        },
        "inputs": {
            "adobe_dir": _path_record(adobe_dir, repo_root),
            "converter": _path_record(converter, repo_root) if converter is not None else None,
            "validator": _path_record(validator, repo_root),
            "dry_run_converter": dry_run_converter,
            "timeout_seconds": timeout_seconds,
        },
        "policy": {
            "adobe_download": "not-attempted",
            "adobe_install": "not-attempted",
            "adobe_extraction": "not-attempted",
            "adobe_resource_modification": "not-attempted",
            "ci_gate": False,
            "reports_local_only": True,
            "child_reports_must_be_current_run": True,
        },
        "summary": {
            "step_count": len(steps),
            "passed": sum(1 for step in steps if step["status"] == "passed"),
            "failed": sum(1 for step in steps if step["status"] == "failed"),
            "blocking_finding_count": len(blocking_findings),
        },
        "blocking_findings": blocking_findings,
        "steps": steps,
    }


def _run_project_dng_fixture_step(
    *,
    output_dir: Path,
    repo_root: Path,
    timeout_seconds: float,
    runner: Runner,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(repo_root / "scripts" / "generate_raw_native_batch.py"),
        "--output-dir",
        str(output_dir),
        "--dng-layout",
        "single-raw-ifd",
    ]
    step = _run_command_step(
        name="project-dng-fixtures",
        command=command,
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    inspection, inspection_errors = _inspect_project_dng_fixtures(output_dir, repo_root)
    step["project_fixture_inspection"] = inspection
    step["project_fixture_roots"] = _project_fixture_inspection_roots(inspection)
    step["blocking_findings"].extend(inspection_errors)
    _finalize_step_status(step)
    return step


def _run_child_step(
    *,
    name: str,
    command: list[str],
    report_path: Path,
    expected_schema: str,
    repo_root: Path,
    timeout_seconds: float,
    runner: Runner,
) -> dict[str, Any]:
    step = _run_command_step(
        name=name,
        command=command,
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    child_report, report_errors = _load_current_child_report(
        report_path=report_path,
        expected_schema=expected_schema,
        started_at=step["started_at"],
        finished_at=step["finished_at"],
        repo_root=repo_root,
    )
    step["child_report_path"] = _path_record(report_path, repo_root)
    step["child_report"] = child_report
    step["blocking_findings"].extend(report_errors)
    if child_report is not None and not _child_report_ok(child_report):
        for finding in _child_blocking_findings(child_report):
            step["blocking_findings"].append(f"{name}: {finding}")
    _finalize_step_status(step)
    return step


def _run_command_step(
    *,
    name: str,
    command: list[str],
    repo_root: Path,
    timeout_seconds: float,
    runner: Runner,
) -> dict[str, Any]:
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    try:
        completed = runner(
            command,
            cwd=repo_root,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = _string_output(exc.output)
        stderr = _string_output(exc.stderr)
        timed_out = True
    finished_at = datetime.now(UTC)
    duration = round(time.perf_counter() - started, 3)
    blocking_findings = []
    if timed_out:
        blocking_findings.append(f"{name} timed out")
    elif exit_code != 0:
        blocking_findings.append(f"{name} exited with code {exit_code}")
    return {
        "name": name,
        "command": command,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration,
        "exit_code": exit_code,
        "timeout": timed_out,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "blocking_findings": blocking_findings,
        "status": "failed",
    }


def _load_current_child_report(
    *,
    report_path: Path,
    expected_schema: str,
    started_at: str,
    finished_at: str,
    repo_root: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not report_path.exists():
        return None, [f"child report missing: {_display_path(report_path, repo_root)}"]
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [
            f"child report is not valid JSON: {_display_path(report_path, repo_root)}: {exc}"
        ]
    if not isinstance(report, dict):
        return None, [f"child report is not an object: {_display_path(report_path, repo_root)}"]
    errors = []
    schema = _child_report_schema(report)
    if schema != expected_schema:
        errors.append(
            f"child report schema mismatch: expected {expected_schema}, got {schema}"
        )
    generated_at = _child_report_generated_at(report)
    if generated_at is None:
        errors.append("child report missing generated_at")
    else:
        child_generated_at = _parse_datetime(generated_at)
        child_started_at = _parse_datetime(started_at)
        if child_generated_at is None:
            errors.append(f"child report generated_at is invalid: {generated_at}")
        elif child_started_at is not None and child_generated_at < child_started_at:
            errors.append("child report generated_at predates the child step start time")
        else:
            child_finished_at = _parse_datetime(finished_at)
            if child_finished_at is not None and child_generated_at > child_finished_at:
                errors.append("child report generated_at is after the child step finish time")
    return report, errors


def _inspect_project_dng_fixtures(
    batch_dir: Path,
    repo_root: Path,
) -> tuple[dict[str, Any], list[str]]:
    manifest_path = batch_dir / "manifests" / "raw-native-node-batch.json"
    sample_index_path = batch_dir / "manifests" / "sample-index.json"
    inspection: dict[str, Any] = {
        "manifest_path": _path_record(manifest_path, repo_root),
        "sample_index_path": _path_record(sample_index_path, repo_root),
        "schema": None,
        "sample_index_schema": None,
        "scene_count": 0,
        "all_validations_ok": False,
        "dng_count": 0,
        "fixture_roots": [],
    }
    errors: list[str] = []
    if not manifest_path.exists():
        return inspection, [
            f"project fixture manifest missing: {_display_path(manifest_path, repo_root)}"
        ]
    if not sample_index_path.exists():
        return inspection, [
            f"project fixture sample index missing: {_display_path(sample_index_path, repo_root)}"
        ]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sample_index = json.loads(sample_index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return inspection, [f"project fixture JSON is invalid: {exc}"]
    if not isinstance(manifest, dict) or not isinstance(sample_index, dict):
        return inspection, ["project fixture manifest and sample index must be JSON objects"]

    manifest_schema = _project_fixture_manifest_schema(manifest)
    sample_index_schema = _project_fixture_sample_index_schema(sample_index)
    inspection["schema"] = manifest_schema
    inspection["sample_index_schema"] = sample_index_schema
    if manifest_schema != "image2dng.raw_native_node_batch.v1":
        errors.append("project fixture manifest schema mismatch")
    if sample_index_schema != "image2dng.raw_native_sample_index.v1":
        errors.append("project fixture sample index schema mismatch")
    if not _sample_index_all_validations_ok(sample_index):
        errors.append("project fixture sample index reports validation failure")
    scenes = _project_fixture_scenes(manifest)
    if not scenes:
        errors.append("project fixture manifest has no scenes")
    inspection["scene_count"] = len(scenes)
    inspection["all_validations_ok"] = _sample_index_all_validations_ok(sample_index)

    dng_paths: list[Path] = []
    validation_failures = 0
    for scene in scenes:
        if not isinstance(scene, dict):
            errors.append("project fixture scene entry is not an object")
            continue
        outputs = _project_fixture_scene_outputs(scene)
        validations = _project_fixture_scene_validations(scene)
        if outputs is None:
            errors.append("project fixture scene outputs missing")
            continue
        for key in ("linearraw_dng", "cfa_dng"):
            value = _project_fixture_output_path(outputs, key)
            if value is None:
                errors.append(f"project fixture output missing: {key}")
                continue
            dng_path = _resolve_from_repo(Path(value), repo_root)
            resolved_dng_path = dng_path.resolve()
            if not _is_path_within(resolved_dng_path, batch_dir):
                errors.append(
                    f"project fixture DNG outside batch dir: "
                    f"{_display_path(resolved_dng_path, repo_root)}"
                )
            elif not resolved_dng_path.exists():
                errors.append(f"project fixture DNG missing: {_display_path(dng_path, repo_root)}")
            else:
                dng_paths.append(resolved_dng_path)
        if not isinstance(validations, dict):
            errors.append("project fixture scene validations missing")
            continue
        for key in ("linearraw", "cfa"):
            validation = _project_fixture_validation_record(validations, key)
            if validation is None or not _validation_ok(
                validation,
                f"project fixture validation {key}",
            ):
                validation_failures += 1
    if validation_failures:
        errors.append(f"project fixture validation failure count: {validation_failures}")

    fixture_roots = sorted({path.parent for path in dng_paths})
    inspection["dng_count"] = len(dng_paths)
    inspection["fixture_roots"] = [_path_record(path, repo_root) for path in fixture_roots]
    if not dng_paths:
        errors.append("project fixture generation produced no DNG files")
    if not fixture_roots:
        errors.append("project fixture generation produced no SDK fixture roots")
    return inspection, errors


def _sdk_fixture_roots(
    *,
    output_dir: Path,
    project_fixture_roots: list[Path],
    repo_root: Path,
) -> list[Path]:
    candidates = [
        *project_fixture_roots,
        output_dir / "adobe-dng-converter-verification" / "source-dng",
        output_dir / "adobe-dng-converter-verification" / "converted",
        *(repo_root / path for path in EXISTING_FIXTURE_CANDIDATES),
    ]
    roots = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _project_fixture_roots(step: dict[str, Any]) -> list[Path]:
    records = step.get("project_fixture_roots", [])
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise TypeError("step project_fixture_roots must be an object list")
    return [Path(_path_record_absolute(record)) for record in records]


def _project_fixture_inspection_roots(inspection: dict[str, Any]) -> list[dict[str, Any]]:
    records = inspection.get("fixture_roots")
    if records is None:
        return []
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise TypeError("project fixture inspection fixture_roots must be an object list")
    return records


def _child_blocking_findings(report: dict[str, Any]) -> list[str]:
    findings = _child_report_string_list(report, "blocking_findings")
    if findings:
        return findings
    errors = _child_report_string_list(report, "errors")
    if errors:
        return errors
    status = _child_report_status(report)
    if status is not None and status not in {"passed", "dry-run"}:
        return [f"child report status is {status}"]
    return ["child report ok is false"]


def _child_report_schema(report: dict[str, Any]) -> str:
    schema = report.get("schema")
    if not isinstance(schema, str):
        raise TypeError("child report schema must be a string")
    return schema


def _child_report_generated_at(report: dict[str, Any]) -> str | None:
    generated_at = report.get("generated_at")
    if generated_at is None:
        return None
    if not isinstance(generated_at, str):
        raise TypeError("child report generated_at must be a string")
    return generated_at


def _child_report_status(report: dict[str, Any]) -> str | None:
    status = report.get("status")
    if status is None:
        return None
    if not isinstance(status, str):
        raise TypeError("child report status must be a string")
    return status


def _child_report_string_list(report: dict[str, Any], key: str) -> list[str]:
    value = report.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"child report {key} must be a string list")
    return value


def _child_report_ok(report: dict[str, Any]) -> bool:
    ok = report.get("ok")
    if not isinstance(ok, bool):
        raise TypeError("child report ok must be a boolean")
    return ok


def _sample_index_all_validations_ok(sample_index: dict[str, Any]) -> bool:
    ok = sample_index.get("all_validations_ok")
    if not isinstance(ok, bool):
        raise TypeError("project fixture sample index all_validations_ok must be a boolean")
    return ok


def _project_fixture_manifest_schema(manifest: dict[str, Any]) -> str | None:
    schema = manifest.get("schema")
    if schema is None:
        return None
    if not isinstance(schema, str):
        raise TypeError("project fixture manifest schema must be a string")
    return schema


def _project_fixture_sample_index_schema(sample_index: dict[str, Any]) -> str | None:
    schema = sample_index.get("schema")
    if schema is None:
        return None
    if not isinstance(schema, str):
        raise TypeError("project fixture sample index schema must be a string")
    return schema


def _project_fixture_scenes(manifest: dict[str, Any]) -> list[Any]:
    scenes = manifest.get("scenes")
    if scenes is None:
        return []
    if not isinstance(scenes, list):
        raise TypeError("project fixture manifest scenes must be a list")
    return scenes


def _project_fixture_scene_outputs(scene: dict[str, Any]) -> dict[str, Any] | None:
    outputs = scene.get("outputs")
    if outputs is None:
        return None
    if not isinstance(outputs, dict):
        raise TypeError("project fixture scene outputs must be an object")
    return outputs


def _project_fixture_scene_validations(scene: dict[str, Any]) -> dict[str, Any] | None:
    validations = scene.get("validations")
    if validations is None:
        return None
    if not isinstance(validations, dict):
        raise TypeError("project fixture scene validations must be an object")
    return validations


def _project_fixture_output_path(outputs: dict[str, Any], key: str) -> str | None:
    value = outputs.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"project fixture output {key} must be a string")
    return value


def _project_fixture_validation_record(
    validations: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    validation = validations.get(key)
    if validation is None:
        return None
    if not isinstance(validation, dict):
        raise TypeError(f"project fixture validation {key} must be an object")
    return validation


def _validation_ok(validation: dict[str, Any], label: str) -> bool:
    ok = validation.get("ok")
    if not isinstance(ok, bool):
        raise TypeError(f"{label} ok must be a boolean")
    return ok


def _extend_blocking_findings(blocking_findings: list[str], step: dict[str, Any]) -> None:
    blocking_findings.extend(_step_blocking_findings(step))


def _finalize_step_status(step: dict[str, Any]) -> None:
    step["blocking_findings"] = _dedupe(_step_blocking_findings(step))
    step["status"] = "passed" if not step["blocking_findings"] else "failed"


def _output_dir_refusal_reason(output_dir: Path, *, repo_root: Path) -> str | None:
    resolved = output_dir.resolve()
    repo_root = repo_root.resolve()
    adobe_dir = (repo_root / "Adobe").resolve()
    demo_output_dir = (repo_root / "demo-output").resolve()
    tracked_roots = [
        (repo_root / name).resolve()
        for name in ("src", "scripts", "tests", "docs")
    ]
    fixture_roots = [
        (repo_root / path).resolve() for path in EXISTING_FIXTURE_CANDIDATES
    ]
    if _is_path_within(resolved, adobe_dir):
        return "Refusing to write Adobe Validation Stack report inside Adobe/."
    if not _is_path_within(resolved, demo_output_dir):
        return "Refusing to write Adobe Validation Stack report outside demo-output/."
    if any(_is_path_within(resolved, root) for root in tracked_roots):
        return "Refusing to write Adobe Validation Stack report inside a tracked source area."
    if any(_is_path_within(resolved, root) for root in fixture_roots):
        return "Refusing to write Adobe Validation Stack report inside a fixture directory."
    return None


def _move_existing_output_aside(path: Path) -> Path | None:
    if not path.exists():
        return None
    clean_dir = path.parent / ".clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = clean_dir / f"{path.name}-{timestamp}"
    counter = 1
    while candidate.exists():
        candidate = clean_dir / f"{path.name}-{timestamp}-{counter}"
        counter += 1
    shutil.move(str(path), str(candidate))
    return candidate


def _summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Adobe Validation Stack",
        "",
        f"- Schema: `{_report_schema(report)}`",
        f"- OK: `{str(_report_ok(report)).lower()}`",
        f"- Run ID: `{_run_id(report)}`",
        f"- Output: `{_output_dir_display(report)}`",
        f"- Local-only: `{str(_local_only(report)).lower()}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in _summary(report).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Blocking Findings", ""])
    blocking_findings = _blocking_findings_record(report)
    if blocking_findings:
        lines.extend(f"- {finding}" for finding in blocking_findings)
    else:
        lines.append("- none")
    lines.extend(["", "## Steps", ""])
    for step in _steps(report):
        lines.append(f"- `{_step_name(step)}`: `{_step_status(step)}`")
        lines.append(f"  - Exit code: `{_step_exit_code(step)}`")
        child_report_path = _step_child_report_path(step)
        if child_report_path is not None:
            lines.append(f"  - Child report: `{_child_report_display(child_report_path)}`")
        step_findings = _step_blocking_findings(step)
        if step_findings:
            lines.append("  - Findings:")
            lines.extend(f"    - {finding}" for finding in step_findings)
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


def _run_id(report: dict[str, Any]) -> str:
    run_id = report["run_id"]
    if not isinstance(run_id, str):
        raise TypeError("report run_id must be a string")
    return run_id


def _output_dir_display(report: dict[str, Any]) -> str:
    output_dir = report["output_dir"]
    if not isinstance(output_dir, dict):
        raise TypeError("report output_dir must be an object")
    _path_record_absolute(output_dir)
    display = output_dir["display"]
    if not isinstance(display, str):
        raise TypeError("report output_dir display must be a string")
    return display


def _local_only(report: dict[str, Any]) -> bool:
    local_only = report["local_only"]
    if not isinstance(local_only, bool):
        raise TypeError("report local_only must be a boolean")
    return local_only


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = report["summary"]
    if not isinstance(summary, dict):
        raise TypeError("report summary must be an object")
    return summary


def _blocking_findings_record(report: dict[str, Any]) -> list[str]:
    findings = report["blocking_findings"]
    if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
        raise TypeError("report blocking_findings must be a string list")
    return findings


def _steps(report: dict[str, Any]) -> list[dict[str, Any]]:
    steps = report["steps"]
    if not isinstance(steps, list):
        raise TypeError("report steps must be a list")
    if not all(isinstance(step, dict) for step in steps):
        raise TypeError("report steps must contain objects")
    return steps


def _step_blocking_findings(step: dict[str, Any]) -> list[str]:
    findings = step.get("blocking_findings", [])
    if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
        raise TypeError("step blocking_findings must be a string list")
    return findings


def _step_name(step: dict[str, Any]) -> str:
    name = step["name"]
    if not isinstance(name, str):
        raise TypeError("step name must be a string")
    return name


def _step_status(step: dict[str, Any]) -> str:
    status = step["status"]
    if not isinstance(status, str):
        raise TypeError("step status must be a string")
    if status not in STEP_STATUSES:
        raise TypeError("step status must be passed or failed")
    return status


def _step_exit_code(step: dict[str, Any]) -> int | None:
    exit_code = step["exit_code"]
    if isinstance(exit_code, bool):
        raise TypeError("step exit_code must be an integer or null")
    if isinstance(exit_code, int) or exit_code is None:
        return exit_code
    raise TypeError("step exit_code must be an integer or null")


def _step_child_report_path(step: dict[str, Any]) -> dict[str, Any] | None:
    child_report_path = step.get("child_report_path")
    if child_report_path is None:
        return None
    if not isinstance(child_report_path, dict):
        raise TypeError("step child_report_path must be an object")
    _path_record_absolute(child_report_path)
    return child_report_path


def _child_report_display(child_report_path: dict[str, Any]) -> str:
    display = child_report_path["display"]
    if not isinstance(display, str):
        raise TypeError("child report display must be a string")
    return display


def _path_record_absolute(path_record: dict[str, Any]) -> str:
    absolute = path_record["absolute"]
    if not isinstance(absolute, str):
        raise TypeError("path record absolute must be a string")
    return absolute


def _resolve_from_repo(path: Path, repo_root: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _path_record(path: Path, repo_root: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "display": _display_path(resolved, repo_root),
        "absolute": str(resolved),
    }


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _is_path_within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    root = root.resolve()
    return resolved == root or root in resolved.parents


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _tail(text: str, *, max_lines: int = TAIL_LINE_LIMIT) -> list[str]:
    lines = text.strip().splitlines()
    return [line[:TAIL_LINE_WIDTH] for line in lines[-max_lines:]]


def _string_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _dedupe(values: list[str]) -> list[str]:
    deduped = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


if __name__ == "__main__":
    raise SystemExit(main())
