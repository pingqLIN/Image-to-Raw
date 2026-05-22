from __future__ import annotations

import argparse
import hashlib
import json
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

from image2dng import __version__, convert
from image2dng.api import OutputMode
from image2dng.compatibility import (
    environment_label,
    processor_tool_inventory,
    run_processor_compatibility,
)
from image2dng.image_processing import InputSpace
from image2dng.models import CfaPattern
from image2dng.validate import validate_dng


@dataclass(frozen=True)
class FixtureSpec:
    slug: str
    input_space: InputSpace
    mode: OutputMode
    cfa_pattern: CfaPattern | None = None
    shot_noise: float = 0.0
    read_noise: float = 0.0
    row_noise: float = 0.0
    sensor_effect_seed: int | None = None

    @property
    def sensor_effects(self) -> dict[str, float | int]:
        effects: dict[str, float | int] = {}
        if self.shot_noise > 0:
            effects["shot_noise"] = self.shot_noise
        if self.read_noise > 0:
            effects["read_noise"] = self.read_noise
        if self.row_noise > 0:
            effects["row_noise"] = self.row_noise
        if self.sensor_effect_seed is not None:
            effects["sensor_effect_seed"] = self.sensor_effect_seed
        return effects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate image2dng compatibility evidence fixtures and reports."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/compatibility-evidence"),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="timeout for each optional RAW processor command",
    )
    args = parser.parse_args(argv)

    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    inputs_dir = root / "inputs"
    raw_dir = root / "raw"
    validation_dir = root / "validation"
    processor_dir = root / "processor-output"
    for directory in (inputs_dir, raw_dir, validation_dir, processor_dir):
        directory.mkdir(parents=True, exist_ok=True)

    tools = processor_tool_inventory(args.timeout_seconds)
    report: dict[str, Any] = {
        "schema": "image2dng.compatibility_evidence.v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "output_dir": str(root),
        "tools": tools,
        "install_policy": {
            "auto_install": False,
            "missing_tool_policy": "skipped",
            "available_tool_failure_policy": "failed",
            "notes": (
                "Install hints are dry-run guidance only; "
                "this script never installs RAW tools."
            ),
        },
        "fixtures": [],
        "matrix": [],
        "ok": False,
        "errors": [],
    }

    for spec in _fixture_specs():
        fixture = _generate_fixture(
            spec=spec,
            inputs_dir=inputs_dir,
            raw_dir=raw_dir,
            validation_dir=validation_dir,
            processor_dir=processor_dir,
            timeout_seconds=args.timeout_seconds,
        )
        _fixtures(report).append(fixture)
        _matrix(report).append(_structural_matrix_entry(fixture))
        _matrix(report).extend(_processor_matrix_entries(fixture))

    _append_failures(report)
    report["ok"] = not _errors(report)

    report_path = root / "compatibility-report.json"
    summary_path = root / "compatibility-summary.md"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path.write_text(_summary_markdown(report), encoding="utf-8")
    print(f"Wrote compatibility report to {report_path}")
    print(f"Wrote compatibility summary to {summary_path}")
    return 0 if report["ok"] else 1


def _fixture_specs() -> list[FixtureSpec]:
    return [
        FixtureSpec("srgb-gradient-linearraw", "srgb", "linearraw"),
        FixtureSpec("linear-rec709-gradient-linearraw", "linear-rec709", "linearraw"),
        FixtureSpec("acescg-gradient-linearraw", "acescg", "linearraw"),
        FixtureSpec("xyz-gradient-linearraw", "xyz", "linearraw"),
        FixtureSpec("prophoto-rgb-chart-linearraw", "prophoto-rgb", "linearraw"),
        FixtureSpec("linear-rec709-cfa-rggb", "linear-rec709", "cfa", "rggb"),
        FixtureSpec("linear-rec709-cfa-bggr", "linear-rec709", "cfa", "bggr"),
        FixtureSpec("linear-rec709-cfa-grbg", "linear-rec709", "cfa", "grbg"),
        FixtureSpec("linear-rec709-cfa-gbrg", "linear-rec709", "cfa", "gbrg"),
        FixtureSpec(
            "linear-rec709-cfa-rggb-noisy",
            "linear-rec709",
            "cfa",
            "rggb",
            shot_noise=0.01,
            read_noise=0.002,
            row_noise=0.001,
            sensor_effect_seed=20260510,
        ),
        FixtureSpec(
            "linear-rec709-linearraw-noisy",
            "linear-rec709",
            "linearraw",
            shot_noise=0.01,
            read_noise=0.002,
            row_noise=0.001,
            sensor_effect_seed=20260510,
        ),
    ]


def _generate_fixture(
    *,
    spec: FixtureSpec,
    inputs_dir: Path,
    raw_dir: Path,
    validation_dir: Path,
    processor_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    input_path = inputs_dir / f"{spec.slug}.tif"
    dng_path = raw_dir / f"{spec.slug}.dng"
    validation_path = validation_dir / f"{spec.slug}.json"
    fixture_processor_dir = processor_dir / spec.slug
    tifffile.imwrite(input_path, _compatibility_chart(), photometric="rgb")

    convert(
        input_path=input_path,
        output_path=dng_path,
        input_space=spec.input_space,
        mode=spec.mode,
        cfa_pattern=spec.cfa_pattern or "rggb",
        shot_noise=spec.shot_noise,
        read_noise=spec.read_noise,
        row_noise=spec.row_noise,
        sensor_effect_seed=spec.sensor_effect_seed,
        prompt_hash=f"sha256:compatibility-{spec.slug}",
        scene_description=f"compatibility fixture: {spec.slug}",
        model_name="image2dng compatibility evidence generator",
        model_version=__version__,
        overwrite=True,
    )

    validation = validate_dng(dng_path, run_smoke=False).to_dict()
    processor_results = [
        result.to_dict()
        for result in run_processor_compatibility(
            dng_path,
            fixture_processor_dir,
            timeout_seconds=timeout_seconds,
        )
    ]
    validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
    return {
        "slug": spec.slug,
        "mode": spec.mode,
        "input_space": spec.input_space,
        "cfa_pattern": spec.cfa_pattern,
        "sensor_effects": spec.sensor_effects,
        "input": str(input_path),
        "dng": str(dng_path),
        "dng_bytes": dng_path.stat().st_size,
        "dng_sha256": _sha256_file(dng_path),
        "dng_layout": validation["dng_layout"],
        "raw_ifd_location": validation["raw_ifd_location"],
        "ifd0_preview": validation["ifd0_preview"],
        "validation_json": str(validation_path),
        "validation_json_bytes": validation_path.stat().st_size,
        "validation_json_sha256": _sha256_file(validation_path),
        "validation_ok": _validation_ok(validation),
        "structural_validation": validation,
        "processor_output_dir": str(fixture_processor_dir),
        "processor_results": processor_results,
    }


def _compatibility_chart(size: int = 64) -> np.ndarray:
    image = np.zeros((size, size, 3), dtype=np.uint16)
    x = np.linspace(0, 65535, size, dtype=np.uint16)
    y = np.linspace(0, 65535, size, dtype=np.uint16)
    image[..., 0] = x[np.newaxis, :]
    image[..., 1] = y[:, np.newaxis]
    image[..., 2] = ((image[..., 0].astype(np.uint32) + image[..., 1]) // 2).astype(np.uint16)

    colors = np.array(
        [
            [65535, 0, 0],
            [0, 65535, 0],
            [0, 0, 65535],
            [65535, 65535, 0],
            [0, 65535, 65535],
            [65535, 0, 65535],
            [65535, 65535, 65535],
            [0, 0, 0],
        ],
        dtype=np.uint16,
    )
    patch_width = size // len(colors)
    for index, color in enumerate(colors):
        x0 = index * patch_width
        x1 = size if index == len(colors) - 1 else (index + 1) * patch_width
        image[8:18, x0:x1] = color

    for row in range(3):
        for col in range(8):
            level = int((row * 8 + col) / 23 * 65535)
            image[24 + row * 8 : 30 + row * 8, col * 8 : (col + 1) * 8] = level
    return image


def _structural_matrix_entry(fixture: dict[str, Any]) -> dict[str, str]:
    result = "passed" if _fixture_validation_ok(fixture) else "failed"
    return {
        "fixture": fixture["slug"],
        "tool": "image2dng validate",
        "tool_version": __version__,
        "command": f"image2dng validate {fixture['dng']} --json",
        "result": result,
        "evidence": fixture["validation_json"],
        "environment": environment_label(),
        "notes": "Structural baseline",
    }


def _processor_matrix_entries(fixture: dict[str, Any]) -> list[dict[str, object]]:
    entries = []
    for processor in fixture["processor_results"]:
        entries.append(
            {
                "fixture": fixture["slug"],
                "tool": processor["tool"],
                "tool_version": processor["version"] or "not available",
                "command": " ".join(str(part) for part in processor["command"]),
                "result": processor["result"],
                "evidence": fixture["validation_json"],
                "environment": environment_label(),
                "notes": processor["notes"],
                "exit_code": processor["exit_code"],
                "duration_seconds": processor["duration_seconds"],
                "output_artifacts": processor["output_artifacts"],
                "missing_output_artifacts": processor["missing_output_artifacts"],
            }
        )
    return entries


def _append_failures(report: dict[str, Any]) -> None:
    for fixture in _fixtures(report):
        if not _fixture_validation_ok(fixture):
            _errors(report).append(f"{fixture['slug']} structural validation failed")
    for entry in _matrix(report):
        if _matrix_result(entry) == "failed":
            _errors(report).append(
                f"{entry['fixture']} failed {entry['tool']}: {_matrix_notes(entry)}"
            )


def _summary_markdown(report: dict[str, Any]) -> str:
    install_policy = _install_policy(report)
    lines = [
        "# Compatibility Evidence Summary",
        "",
        f"- Schema: `{_report_schema(report)}`",
        f"- Generated at: `{_generated_at(report)}`",
        f"- Output dir: `{_output_dir(report)}`",
        f"- Overall ok: `{_report_ok(report)}`",
        "",
        "## Tool Inventory",
        "",
        "| Tool | Available | Version | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for name, tool in _tools(report).items():
        notes = tool.get("notes", "")
        lines.append(
            f"| `{name}` | `{tool.get('available')}` | `{tool.get('version')}` | {notes} |"
        )
    lines.extend(
        [
            "",
            "## Evidence Matrix",
            "",
            "| Fixture | Tool | Result | Evidence | Notes |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for entry in _matrix(report):
        fixture = _fixture_by_slug(report, str(entry["fixture"]))
        notes = entry["notes"]
        if fixture is not None and entry["tool"] == "image2dng validate":
            notes = (
                f"{notes}; layout={fixture['dng_layout']}; "
                f"raw_ifd_location={fixture['raw_ifd_location']}"
            )
        lines.append(
            "| "
            f"`{entry['fixture']}` | `{entry['tool']}` | `{entry['result']}` | "
            f"`{entry['evidence']}` | {notes} |"
        )
    lines.extend(
        [
            "",
            "## Fixture Integrity",
            "",
            "| Fixture | DNG bytes | DNG SHA-256 | "
            "Validation JSON bytes | Validation JSON SHA-256 |",
            "| --- | ---: | --- | ---: | --- |",
        ]
    )
    for fixture in _fixtures(report):
        lines.append(
            "| "
            f"`{fixture['slug']}` | `{fixture['dng_bytes']}` | `{fixture['dng_sha256']}` | "
            f"`{fixture['validation_json_bytes']}` | `{fixture['validation_json_sha256']}` |"
        )
    lines.extend(
        [
            "",
            "## Install Policy",
            "",
            f"- Auto install: `{_install_policy_auto_install(install_policy)}`",
            f"- Missing tool policy: `{_install_policy_missing_tool_policy(install_policy)}`",
            (
                "- Available tool failure policy: "
                f"`{_install_policy_available_tool_failure_policy(install_policy)}`"
            ),
            "- Install hints are dry-run guidance only.",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def _fixture_by_slug(report: dict[str, Any], slug: str) -> dict[str, Any] | None:
    for fixture in _fixtures(report):
        if fixture["slug"] == slug:
            return fixture
    return None


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


def _output_dir(report: dict[str, Any]) -> str:
    output_dir = report["output_dir"]
    if not isinstance(output_dir, str):
        raise TypeError("report output_dir must be a string")
    return output_dir


def _report_ok(report: dict[str, Any]) -> bool:
    ok = report["ok"]
    if not isinstance(ok, bool):
        raise TypeError("report ok must be a boolean")
    return ok


def _install_policy(report: dict[str, Any]) -> dict[str, Any]:
    policy = report["install_policy"]
    if not isinstance(policy, dict):
        raise TypeError("report install_policy must be an object")
    return policy


def _install_policy_auto_install(policy: dict[str, Any]) -> bool:
    auto_install = policy["auto_install"]
    if not isinstance(auto_install, bool):
        raise TypeError("install_policy auto_install must be a boolean")
    return auto_install


def _install_policy_missing_tool_policy(policy: dict[str, Any]) -> str:
    missing_tool_policy = policy["missing_tool_policy"]
    if not isinstance(missing_tool_policy, str):
        raise TypeError("install_policy missing_tool_policy must be a string")
    return missing_tool_policy


def _install_policy_available_tool_failure_policy(policy: dict[str, Any]) -> str:
    failure_policy = policy["available_tool_failure_policy"]
    if not isinstance(failure_policy, str):
        raise TypeError("install_policy available_tool_failure_policy must be a string")
    return failure_policy


def _fixtures(report: dict[str, Any]) -> list[dict[str, Any]]:
    fixtures = report["fixtures"]
    if not isinstance(fixtures, list):
        raise TypeError("report fixtures must be a list")
    if not all(isinstance(fixture, dict) for fixture in fixtures):
        raise TypeError("report fixtures must contain objects")
    return fixtures


def _validation_ok(validation: dict[str, Any]) -> bool:
    ok = validation["ok"]
    if not isinstance(ok, bool):
        raise TypeError("validation ok must be a boolean")
    return ok


def _fixture_validation_ok(fixture: dict[str, Any]) -> bool:
    validation_ok = fixture["validation_ok"]
    if not isinstance(validation_ok, bool):
        raise TypeError("fixture validation_ok must be a boolean")
    return validation_ok


def _tools(report: dict[str, Any]) -> dict[str, Any]:
    tools = report["tools"]
    if not isinstance(tools, dict):
        raise TypeError("report tools must be an object")
    return tools


def _matrix(report: dict[str, Any]) -> list[dict[str, Any]]:
    matrix = report["matrix"]
    if not isinstance(matrix, list):
        raise TypeError("report matrix must be a list")
    if not all(isinstance(entry, dict) for entry in matrix):
        raise TypeError("report matrix must contain objects")
    return matrix


def _matrix_result(entry: dict[str, Any]) -> str:
    result = entry["result"]
    if not isinstance(result, str):
        raise TypeError("matrix result must be a string")
    return result


def _matrix_notes(entry: dict[str, Any]) -> str:
    notes = entry["notes"]
    if not isinstance(notes, str):
        raise TypeError("matrix notes must be a string")
    return notes


def _errors(report: dict[str, Any]) -> list[Any]:
    errors = report["errors"]
    if not isinstance(errors, list):
        raise TypeError("report errors must be a list")
    return errors


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
