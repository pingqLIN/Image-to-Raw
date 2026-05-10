from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import tifffile

from image2dng import __version__, convert
from image2dng.api import OutputMode
from image2dng.image_processing import InputSpace
from image2dng.models import CfaPattern
from image2dng.validate import validate_dng

ToolName = Literal["exiftool", "dcraw", "darktable-cli", "rawtherapee-cli"]


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
    args = parser.parse_args(argv)

    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    inputs_dir = root / "inputs"
    raw_dir = root / "raw"
    validation_dir = root / "validation"
    for directory in (inputs_dir, raw_dir, validation_dir):
        directory.mkdir(parents=True, exist_ok=True)

    tools = _tool_inventory()
    report: dict[str, Any] = {
        "schema": "image2dng.compatibility_evidence.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "output_dir": str(root),
        "tools": tools,
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
        )
        report["fixtures"].append(fixture)
        report["matrix"].append(_structural_matrix_entry(fixture))
        report["matrix"].extend(_smoke_matrix_entries(fixture))

    report["matrix"].append(_adobe_dng_sdk_matrix_entry())
    _append_failures(report)
    report["ok"] = not report["errors"]

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
) -> dict[str, Any]:
    input_path = inputs_dir / f"{spec.slug}.tif"
    dng_path = raw_dir / f"{spec.slug}.dng"
    validation_path = validation_dir / f"{spec.slug}.json"
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

    validation = validate_dng(dng_path, run_smoke=True).to_dict()
    validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
    return {
        "slug": spec.slug,
        "mode": spec.mode,
        "input_space": spec.input_space,
        "cfa_pattern": spec.cfa_pattern,
        "sensor_effects": spec.sensor_effects,
        "input": str(input_path),
        "dng": str(dng_path),
        "validation_json": str(validation_path),
        "validation_ok": validation["ok"],
        "smoke_tests": validation["smoke_tests"],
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


def _tool_inventory() -> dict[str, dict[str, Any]]:
    tools: dict[str, dict[str, Any]] = {}
    version_commands: dict[ToolName, list[str]] = {
        "exiftool": ["exiftool", "-ver"],
        "dcraw": ["dcraw", "-h"],
        "darktable-cli": ["darktable-cli", "--version"],
        "rawtherapee-cli": ["rawtherapee-cli", "--version"],
    }
    for name, command in version_commands.items():
        executable = shutil.which(command[0])
        tools[name] = {
            "available": executable is not None,
            "executable": executable,
            "version_command": command,
            "version": _tool_version(command) if executable is not None else None,
            "timeout_seconds": 30,
        }
    tools["adobe-dng-sdk"] = {
        "available": False,
        "manual_only": True,
        "version": None,
        "notes": "Manual validation only until a reproducible local SDK path exists.",
    }
    return tools


def _tool_version(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    text = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    first_line = text.splitlines()[0] if text.splitlines() else f"exit code {completed.returncode}"
    return first_line[:200]


def _structural_matrix_entry(fixture: dict[str, Any]) -> dict[str, str]:
    result = "passed" if fixture["validation_ok"] else "failed"
    return {
        "fixture": fixture["slug"],
        "tool": "image2dng validate",
        "tool_version": __version__,
        "command": f"image2dng validate {fixture['dng']} --json",
        "result": result,
        "evidence": fixture["validation_json"],
        "environment": f"{platform.system()} Python {platform.python_version()}",
        "notes": "Structural baseline",
    }


def _smoke_matrix_entries(fixture: dict[str, Any]) -> list[dict[str, str]]:
    entries = []
    for tool, status in sorted(fixture["smoke_tests"].items()):
        result = _matrix_result(str(status))
        entries.append(
            {
                "fixture": fixture["slug"],
                "tool": tool,
                "tool_version": "see tools inventory",
                "command": "optional smoke via image2dng validate",
                "result": result,
                "evidence": fixture["validation_json"],
                "environment": f"{platform.system()} Python {platform.python_version()}",
                "notes": str(status),
            }
        )
    return entries


def _matrix_result(status: str) -> str:
    if status == "ok":
        return "passed"
    if status.startswith("skipped:"):
        return "skipped"
    return "failed"


def _adobe_dng_sdk_matrix_entry() -> dict[str, str]:
    return {
        "fixture": "all",
        "tool": "Adobe DNG SDK",
        "tool_version": "manual-only",
        "command": "manual SDK validation",
        "result": "manual-only",
        "evidence": "pending",
        "environment": "local workstation",
        "notes": "Not a CI gate",
    }


def _append_failures(report: dict[str, Any]) -> None:
    for fixture in report["fixtures"]:
        if not fixture["validation_ok"]:
            report["errors"].append(f"{fixture['slug']} structural validation failed")
    for entry in report["matrix"]:
        if entry["result"] == "failed":
            report["errors"].append(f"{entry['fixture']} failed {entry['tool']}: {entry['notes']}")


def _summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Compatibility Evidence Summary",
        "",
        f"- Schema: `{report['schema']}`",
        f"- Generated at: `{report['generated_at']}`",
        f"- Output dir: `{report['output_dir']}`",
        f"- Overall ok: `{report['ok']}`",
        "",
        "## Tool Inventory",
        "",
        "| Tool | Available | Version | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for name, tool in report["tools"].items():
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
    for entry in report["matrix"]:
        lines.append(
            "| "
            f"`{entry['fixture']}` | `{entry['tool']}` | `{entry['result']}` | "
            f"`{entry['evidence']}` | {entry['notes']} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
