from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image

StepStatus = Literal["passed", "failed"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the image2dng development baseline checks and write evidence JSON."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/development-baseline"),
        help="directory for generated batch outputs and verification-report.json",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = repo_root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, object] = {
        "schema": "image2dng.development_baseline_report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(repo_root),
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "steps": [],
        "batch": {},
        "ok": False,
        "errors": [],
    }

    steps = [
        ("pytest", ["uv", "run", "pytest"]),
        ("ruff", ["uv", "run", "ruff", "check"]),
        ("build", ["uv", "build"]),
        (
            "raw-native-batch",
            [
                "uv",
                "run",
                "python",
                "scripts/generate_raw_native_batch.py",
                "--output-dir",
                str(output_dir / "raw-native-node-batch"),
            ],
        ),
    ]

    for name, command in steps:
        result = _run_step(name, command, repo_root)
        _append_step(report, result)
        if result["status"] == "failed":
            _write_report(output_dir, report)
            return 1

    wheel_smoke = _run_wheel_smoke_step(output_dir=output_dir, repo_root=repo_root)
    _append_step(report, wheel_smoke)
    if wheel_smoke["status"] == "failed":
        _write_report(output_dir, report)
        return 1

    try:
        report["batch"] = _inspect_batch(output_dir / "raw-native-node-batch", repo_root)
    except ValueError as exc:
        _append_error(report, str(exc))

    report["ok"] = not _errors(report) and all(
        step["status"] == "passed" for step in _steps(report)
    )
    _write_report(output_dir, report)
    return 0 if report["ok"] else 1


def _run_step(name: str, command: list[str], cwd: Path) -> dict[str, object]:
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    duration = round(time.perf_counter() - started, 3)
    return {
        "name": name,
        "command": command,
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "status": "passed" if completed.returncode == 0 else "failed",
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _run_wheel_smoke_step(*, output_dir: Path, repo_root: Path) -> dict[str, object]:
    try:
        wheel = _latest_wheel(repo_root)
    except FileNotFoundError as exc:
        return {
            "name": "wheel-install-smoke",
            "command": [],
            "exit_code": 1,
            "duration_seconds": 0.0,
            "status": "failed",
            "stdout_tail": [],
            "stderr_tail": [str(exc)],
        }
    venv = output_dir / "wheel-smoke-venv"
    if venv.exists():
        shutil.rmtree(venv)

    python_executable = _venv_python(venv)
    image2dng_executable = _venv_script(venv, "image2dng")
    commands = [
        ["uv", "venv", str(venv)],
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python_executable),
            str(wheel),
        ],
        [str(image2dng_executable), "--help"],
        [str(image2dng_executable), "validate", "--help"],
    ]

    started = time.perf_counter()
    stdout = []
    stderr = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout.extend(_tail(completed.stdout, max_lines=12))
        stderr.extend(_tail(completed.stderr, max_lines=12))
        if completed.returncode != 0:
            duration = round(time.perf_counter() - started, 3)
            return {
                "name": "wheel-install-smoke",
                "command": commands,
                "exit_code": completed.returncode,
                "duration_seconds": duration,
                "status": "failed",
                "stdout_tail": stdout[-40:],
                "stderr_tail": stderr[-40:],
            }

    duration = round(time.perf_counter() - started, 3)
    return {
        "name": "wheel-install-smoke",
        "command": commands,
        "exit_code": 0,
        "duration_seconds": duration,
        "status": "passed",
        "stdout_tail": stdout[-40:],
        "stderr_tail": stderr[-40:],
    }


def _venv_python(venv: Path) -> Path:
    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _latest_wheel(repo_root: Path) -> Path:
    wheels = sorted(
        (repo_root / "dist").glob("image2dng-*.whl"),
        key=lambda path: path.stat().st_mtime,
    )
    if not wheels:
        raise FileNotFoundError("no built image2dng wheel found under dist/")
    return wheels[-1]


def _venv_script(venv: Path, name: str) -> Path:
    if platform.system() == "Windows":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def _append_step(report: dict[str, object], step: dict[str, object]) -> None:
    _steps(report).append(step)
    if step["status"] == "failed":
        _append_error(report, f"{step['name']} failed with exit code {step['exit_code']}")


def _inspect_batch(batch_dir: Path, repo_root: Path) -> dict[str, object]:
    manifest_path = batch_dir / "manifests" / "raw-native-node-batch.json"
    sample_index_path = batch_dir / "manifests" / "sample-index.json"
    if not manifest_path.exists():
        raise ValueError(f"manifest missing: {manifest_path}")
    if not sample_index_path.exists():
        raise ValueError(f"sample index missing: {sample_index_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sample_index = json.loads(sample_index_path.read_text(encoding="utf-8"))
    _require(manifest.get("schema") == "image2dng.raw_native_node_batch.v1", "unexpected schema")
    _require(
        sample_index.get("schema") == "image2dng.raw_native_sample_index.v1",
        "unexpected sample index schema",
    )
    _require(
        manifest.get("decision", {}).get("core_pipeline") == "built-in image2dng Python graph",
        "unexpected core pipeline decision",
    )
    scenes = manifest.get("scenes")
    _require(isinstance(scenes, list) and len(scenes) >= 3, "expected at least 3 scenes")
    _require(
        sample_index.get("scene_count") == len(scenes),
        "sample index scene count does not match manifest",
    )
    _require(
        sample_index.get("all_validations_ok") is True,
        "sample index reports validation failure",
    )

    scene_reports = [_inspect_scene(scene, repo_root, batch_dir) for scene in scenes]
    return {
        "batch_dir": str(batch_dir),
        "manifest_path": str(manifest_path),
        "sample_index_path": str(sample_index_path),
        "scene_count": len(scene_reports),
        "sample_index_all_validations_ok": sample_index.get("all_validations_ok"),
        "scenes": scene_reports,
    }


def _inspect_scene(scene: object, repo_root: Path, batch_dir: Path) -> dict[str, object]:
    if not isinstance(scene, dict):
        raise ValueError("scene entry must be an object")
    slug = _string(scene, "slug")
    outputs = _scene_outputs(scene, slug)
    validations = _scene_validations(scene, slug)
    raw_ids = _scene_raw_data_unique_ids(scene, slug)
    nodes = _scene_nodes(scene, slug)

    expected_outputs = {
        "scene_linear_tiff",
        "linearraw_dng",
        "cfa_dng",
        "linearraw_jpeg",
        "cfa_jpeg",
    }
    missing_outputs = sorted(expected_outputs - set(outputs))
    if missing_outputs:
        raise ValueError(f"{slug}: missing outputs: {', '.join(missing_outputs)}")
    for key in ("linearraw", "cfa"):
        if not _validation_summary_ok(validations, key, slug):
            raise ValueError(f"{slug}: validation summary for {key} is not ok")
        if not _raw_data_unique_id(raw_ids, key, slug):
            raise ValueError(f"{slug}: raw data unique id for {key} is missing")

    linear_dng_path = _output_path(outputs, "linearraw_dng", repo_root, batch_dir, slug)
    validation_dir = linear_dng_path.parent.parent / "validation"

    artifact_reports = [
        _artifact_record(key, _output_path(outputs, key, repo_root, batch_dir, slug))
        for key in sorted(expected_outputs)
    ]
    artifact_reports.extend(
        [
            _validation_artifact_record(
                "linearraw_validation",
                validation_dir / f"{slug}-linearraw.json",
            ),
            _validation_artifact_record("cfa_validation", validation_dir / f"{slug}-cfa.json"),
        ]
    )
    for key in ("linearraw_jpeg", "cfa_jpeg"):
        _inspect_jpeg(
            slug,
            key,
            _output_path(outputs, key, repo_root, batch_dir, slug),
            artifact_reports,
        )

    return {
        "slug": slug,
        "prompt_hash": _string(scene, "prompt_hash"),
        "node_count": len(nodes),
        "artifacts": artifact_reports,
        "validations": validations,
        "raw_data_unique_ids": raw_ids,
    }


def _scene_outputs(scene: dict[str, object], slug: str) -> dict[str, object]:
    outputs = scene.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError(f"{slug}: outputs must be an object")
    return outputs


def _scene_validations(scene: dict[str, object], slug: str) -> dict[str, object]:
    validations = scene.get("validations")
    if not isinstance(validations, dict):
        raise ValueError(f"{slug}: validations must be an object")
    return validations


def _scene_raw_data_unique_ids(scene: dict[str, object], slug: str) -> dict[str, object]:
    raw_ids = scene.get("raw_data_unique_ids")
    if not isinstance(raw_ids, dict):
        raise ValueError(f"{slug}: raw_data_unique_ids must be an object")
    return raw_ids


def _scene_nodes(scene: dict[str, object], slug: str) -> list[object]:
    nodes = scene.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError(f"{slug}: nodes must be a non-empty list")
    return nodes


def _output_path(
    outputs: dict[str, object],
    key: str,
    repo_root: Path,
    batch_dir: Path,
    slug: str,
) -> Path:
    value = outputs[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{slug}: output {key} must be a non-empty string")
    return _resolve_batch_path(value, repo_root, batch_dir)


def _validation_summary_ok(validations: dict[str, object], key: str, slug: str) -> bool:
    validation = validations.get(key)
    if not isinstance(validation, dict):
        raise ValueError(f"{slug}: validation summary for {key} must be an object")
    ok = validation.get("ok")
    if not isinstance(ok, bool):
        raise ValueError(f"{slug}: validation summary for {key} ok must be a boolean")
    return ok


def _raw_data_unique_id(raw_ids: dict[str, object], key: str, slug: str) -> str | None:
    value = raw_ids.get(key)
    if isinstance(value, str) or value is None:
        return value
    raise ValueError(f"{slug}: raw data unique id for {key} must be a string or null")


def _artifact_record(key: str, path: Path) -> dict[str, object]:
    if not path.exists():
        raise ValueError(f"{key} missing: {path}")
    return {
        "key": key,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _validation_artifact_record(key: str, path: Path) -> dict[str, object]:
    record = _artifact_record(key, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{key}: validation JSON must be an object")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise ValueError(f"{key}: validation JSON ok must be a boolean")
    if ok is not True:
        raise ValueError(f"{key}: validation JSON is not ok")
    errors = payload.get("errors")
    if not isinstance(errors, list):
        raise ValueError(f"{key}: validation JSON errors must be a list")
    record["validation_ok"] = ok
    record["validation_error_count"] = len(errors)
    return record


def _inspect_jpeg(
    slug: str,
    key: str,
    path: Path,
    artifacts: list[dict[str, object]],
) -> None:
    with Image.open(path) as image:
        if image.format != "JPEG":
            raise ValueError(f"{slug}: {key} is not a JPEG")
        width, height = image.size
    for artifact in artifacts:
        if artifact["key"] == key:
            artifact["format"] = "JPEG"
            artifact["width"] = width
            artifact["height"] = height
            return


def _resolve_path(value: str, repo_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _resolve_batch_path(value: str, repo_root: Path, batch_dir: Path) -> Path:
    path = _resolve_path(value, repo_root).resolve()
    if not _is_path_within(path, batch_dir):
        raise ValueError(f"batch artifact path is outside batch dir: {path}")
    return path


def _is_path_within(path: Path, root: Path) -> bool:
    root = root.resolve()
    return path == root or root in path.parents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _append_error(report: dict[str, object], message: str) -> None:
    _errors(report).append(message)


def _steps(report: dict[str, object]) -> list[dict[str, object]]:
    steps = report["steps"]
    if not isinstance(steps, list):
        raise TypeError("report steps must be a list")
    if not all(isinstance(step, dict) for step in steps):
        raise TypeError("report steps must contain objects")
    return steps


def _errors(report: dict[str, object]) -> list[object]:
    errors = report["errors"]
    if not isinstance(errors, list):
        raise TypeError("report errors must be a list")
    return errors


def _tail(text: str, *, max_lines: int = 40) -> list[str]:
    lines = text.strip().splitlines()
    return lines[-max_lines:]


def _write_report(output_dir: Path, report: dict[str, object]) -> None:
    report_path = output_dir / "verification-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote verification report to {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
