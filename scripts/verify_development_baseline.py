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
    wheel = _latest_wheel(repo_root)
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

    scene_reports = [_inspect_scene(scene, repo_root) for scene in scenes]
    return {
        "batch_dir": str(batch_dir),
        "manifest_path": str(manifest_path),
        "sample_index_path": str(sample_index_path),
        "scene_count": len(scene_reports),
        "sample_index_all_validations_ok": sample_index.get("all_validations_ok"),
        "scenes": scene_reports,
    }


def _inspect_scene(scene: object, repo_root: Path) -> dict[str, object]:
    if not isinstance(scene, dict):
        raise ValueError("scene entry must be an object")
    slug = _string(scene, "slug")
    outputs = scene.get("outputs")
    validations = scene.get("validations")
    raw_ids = scene.get("raw_data_unique_ids")
    nodes = scene.get("nodes")
    if not isinstance(outputs, dict):
        raise ValueError(f"{slug}: outputs must be an object")
    if not isinstance(validations, dict):
        raise ValueError(f"{slug}: validations must be an object")
    if not isinstance(raw_ids, dict):
        raise ValueError(f"{slug}: raw_data_unique_ids must be an object")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError(f"{slug}: nodes must be a non-empty list")

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

    linear_dng_path = _resolve_path(str(outputs["linearraw_dng"]), repo_root)
    validation_dir = linear_dng_path.parent.parent / "validation"

    artifact_reports = [
        _artifact_record(key, _resolve_path(str(outputs[key]), repo_root))
        for key in sorted(expected_outputs)
    ]
    artifact_reports.extend(
        [
            _artifact_record("linearraw_validation", validation_dir / f"{slug}-linearraw.json"),
            _artifact_record("cfa_validation", validation_dir / f"{slug}-cfa.json"),
        ]
    )
    for key in ("linearraw_jpeg", "cfa_jpeg"):
        _inspect_jpeg(slug, key, _resolve_path(str(outputs[key]), repo_root), artifact_reports)
    for key in ("linearraw", "cfa"):
        validation = validations.get(key)
        if not isinstance(validation, dict) or validation.get("ok") is not True:
            raise ValueError(f"{slug}: validation summary for {key} is not ok")
        if not raw_ids.get(key):
            raise ValueError(f"{slug}: raw data unique id for {key} is missing")

    return {
        "slug": slug,
        "prompt_hash": _string(scene, "prompt_hash"),
        "node_count": len(nodes),
        "artifacts": artifact_reports,
        "validations": validations,
        "raw_data_unique_ids": raw_ids,
    }


def _artifact_record(key: str, path: Path) -> dict[str, object]:
    if not path.exists():
        raise ValueError(f"{key} missing: {path}")
    return {
        "key": key,
        "path": str(path),
        "bytes": path.stat().st_size,
    }


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
