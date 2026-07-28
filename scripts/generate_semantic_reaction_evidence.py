from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import png
import tifffile

from image2dng import __version__
from image2dng.pipeline import ExternalSceneLinearInput, run_external_scene_linear_batch
from image2dng.semantic_scene import SEMANTIC_SCENE_SCHEMA

REPORT_SCHEMA = "image2dng.semantic_reaction_evidence.v1"
INPUT_SPACE = "linear-rec709"
WIDTH = 20
HEIGHT = 18


@dataclass(frozen=True)
class ScenarioSpec:
    slug: str
    exposure_bias_ev: float | None
    clipping_policy: str | None
    mask_columns: int | None = 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic semantic RAW reaction evidence fixtures."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/semantic-reaction-evidence"),
    )
    args = parser.parse_args(argv)

    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    inputs_dir = root / "inputs"
    masks_dir = root / "masks"
    scenarios_dir = root / "scenarios"
    for directory in (inputs_dir, masks_dir, scenarios_dir):
        directory.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "output_dir": str(root),
        "input_space": INPUT_SPACE,
        "scenarios": [],
        "artifacts": [],
        "ok": False,
        "errors": [],
    }

    for spec in _scenario_specs():
        scenario = _generate_scenario(
            spec=spec,
            root=root,
            inputs_dir=inputs_dir,
            masks_dir=masks_dir,
            scenarios_dir=scenarios_dir,
        )
        report["scenarios"].append(scenario)
        report["artifacts"].extend(scenario["artifacts"])

    _append_failures(report)
    report["ok"] = not report["errors"]

    report_path = root / "semantic-reaction-evidence-report.json"
    summary_path = root / "semantic-reaction-evidence-summary.md"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path.write_text(_summary_markdown(report), encoding="utf-8")
    print(f"Wrote semantic reaction evidence report to {report_path}")
    print(f"Wrote semantic reaction evidence summary to {summary_path}")
    return 0 if report["ok"] else 1


def _scenario_specs() -> list[ScenarioSpec]:
    return [
        ScenarioSpec("exposure-only", exposure_bias_ev=1.0, clipping_policy=None),
        ScenarioSpec("highlight-only", exposure_bias_ev=None, clipping_policy="soft-rolloff"),
        ScenarioSpec(
            "exposure-and-highlight",
            exposure_bias_ev=1.0,
            clipping_policy="preserve-highlights",
        ),
        ScenarioSpec("clip-noop-baseline", exposure_bias_ev=None, clipping_policy="clip"),
    ]


def _generate_scenario(
    *,
    spec: ScenarioSpec,
    root: Path,
    inputs_dir: Path,
    masks_dir: Path,
    scenarios_dir: Path,
) -> dict[str, Any]:
    source_path = inputs_dir / f"{spec.slug}-scene-linear.tif"
    mask_path = masks_dir / f"{spec.slug}-mask.png"
    semantic_path = root / f"{spec.slug}.semantic.json"
    tifffile.imwrite(source_path, _reaction_chart(WIDTH, HEIGHT), photometric="rgb")
    _write_mask(mask_path, WIDTH, HEIGHT, spec.mask_columns or WIDTH)
    _write_semantic_scene(
        semantic_path=semantic_path,
        mask_path=mask_path,
        width=WIDTH,
        height=HEIGHT,
        exposure_bias_ev=spec.exposure_bias_ev,
        clipping_policy=spec.clipping_policy,
    )

    preserved = _run_batch(
        scenarios_dir / spec.slug / "preserved",
        spec=spec,
        source_path=source_path,
        semantic_path=semantic_path,
        apply_semantic_reaction=False,
    )
    reacted = _run_batch(
        scenarios_dir / spec.slug / "reacted",
        spec=spec,
        source_path=source_path,
        semantic_path=semantic_path,
        apply_semantic_reaction=True,
    )
    preserved_scene = _read_first_scene(preserved.manifest_path)
    reacted_scene = _read_first_scene(reacted.manifest_path)
    preserved_sample = _read_first_sample(preserved.sample_index_path)
    reacted_sample = _read_first_sample(reacted.sample_index_path)

    copied_source = _copied_source_path(reacted_scene)
    reaction_input = Path(reacted_scene["outputs"]["scene_linear_input"])
    copied_semantic = Path(reacted_scene["semantic_artifacts"]["semantic_manifest"])
    copied_assets = {
        key.removeprefix("asset:"): Path(value)
        for key, value in reacted_scene["semantic_artifacts"].items()
        if key.startswith("asset:")
    }
    raw_value_changed = (
        preserved_scene["raw_data_unique_ids"]["linearraw"]
        != reacted_scene["raw_data_unique_ids"]["linearraw"]
    )
    provenance_changed = preserved_scene["prompt_hash"] != reacted_scene["prompt_hash"]

    artifacts = [
        _artifact_entry(root, source_path, "source-fixture"),
        _artifact_entry(root, semantic_path, "semantic-fixture"),
        _artifact_entry(root, mask_path, "semantic-asset-fixture"),
        _artifact_entry(root, preserved.manifest_path, "preserved-manifest"),
        _artifact_entry(root, preserved.sample_index_path, "preserved-sample-index"),
        _artifact_entry(root, reacted.manifest_path, "reacted-manifest"),
        _artifact_entry(root, reacted.sample_index_path, "reacted-sample-index"),
        _artifact_entry(root, copied_source, "copied-source"),
        _artifact_entry(root, copied_semantic, "copied-semantic-manifest"),
        _artifact_entry(root, Path(reacted_scene["outputs"]["linearraw_dng"]), "reacted-dng"),
        _artifact_entry(root, Path(reacted_scene["outputs"]["cfa_dng"]), "reacted-dng"),
    ]
    artifacts.extend(
        _artifact_entry(root, asset_path, f"copied-semantic-asset:{asset_id}")
        for asset_id, asset_path in sorted(copied_assets.items())
    )
    if reaction_input != copied_source:
        artifacts.append(_artifact_entry(root, reaction_input, "reaction-input"))

    return {
        "slug": spec.slug,
        "input_space": INPUT_SPACE,
        "mode": "linearraw+cfa",
        "semantic_policy": {
            "exposure_bias_ev": spec.exposure_bias_ev,
            "clipping_policy": spec.clipping_policy,
        },
        "paths": {
            "source_fixture": _relative_path(source_path, root),
            "semantic_fixture": _relative_path(semantic_path, root),
            "mask_fixture": _relative_path(mask_path, root),
            "copied_source": _relative_path(copied_source, root),
            "copied_semantic_manifest": _relative_path(copied_semantic, root),
            "reaction_input": _relative_path(reaction_input, root),
        },
        "hashes": {
            "source_fixture": _sha256_file(source_path),
            "semantic_fixture": _sha256_file(semantic_path),
            "mask_fixture": _sha256_file(mask_path),
            "copied_source": _sha256_file(copied_source),
            "copied_semantic_manifest": _sha256_file(copied_semantic),
            "copied_semantic_assets": {
                asset_id: _sha256_file(path) for asset_id, path in sorted(copied_assets.items())
            },
            **(
                {"reaction_input": _sha256_file(reaction_input)}
                if reaction_input != copied_source
                else {}
            ),
        },
        "batches": {
            "preserved_manifest": _relative_path(preserved.manifest_path, root),
            "preserved_sample_index": _relative_path(preserved.sample_index_path, root),
            "reacted_manifest": _relative_path(reacted.manifest_path, root),
            "reacted_sample_index": _relative_path(reacted.sample_index_path, root),
        },
        "validations": {
            "preserved": preserved_scene["validations"],
            "reacted": reacted_scene["validations"],
        },
        "raw_data_unique_ids": {
            "preserved": preserved_scene["raw_data_unique_ids"],
            "reacted": reacted_scene["raw_data_unique_ids"],
        },
        "prompt_hashes": {
            "preserved": preserved_scene["prompt_hash"],
            "reacted": reacted_scene["prompt_hash"],
        },
        "raw_value_changed": raw_value_changed,
        "provenance_changed": provenance_changed,
        "semantic_to_raw_status": reacted_scene["semantic_to_raw_status"],
        "semantic_reaction": reacted_scene["semantic_reaction"],
        "semantic_reactions": reacted_scene["semantic_reactions"],
        "sample_index": {
            "preserved": preserved_sample,
            "reacted": reacted_sample,
        },
        "artifacts": artifacts,
    }


def _run_batch(
    output_dir: Path,
    *,
    spec: ScenarioSpec,
    source_path: Path,
    semantic_path: Path,
    apply_semantic_reaction: bool,
):
    return run_external_scene_linear_batch(
        output_dir,
        scenes=[
            ExternalSceneLinearInput(
                slug=spec.slug,
                path=source_path,
                input_space=INPUT_SPACE,
                description=f"semantic reaction evidence scenario: {spec.slug}",
                producer="image2dng semantic reaction evidence generator",
                semantic_manifest=semantic_path,
                apply_semantic_reaction=apply_semantic_reaction,
            )
        ],
    )


def _reaction_chart(width: int, height: int) -> np.ndarray:
    x = np.linspace(4096, 65535, width, dtype=np.uint16)
    y = np.linspace(2048, 65535, height, dtype=np.uint16)
    red = np.tile(x, (height, 1))
    green = np.tile(y[:, np.newaxis], (1, width))
    blue = ((red.astype(np.uint32) + green.astype(np.uint32)) // 2).astype(np.uint16)
    return np.stack([red, green, blue], axis=2)


def _write_mask(path: Path, width: int, height: int, mask_columns: int) -> None:
    mask = np.zeros((height, width), dtype=np.uint16)
    mask[:, :mask_columns] = 65535
    with path.open("wb") as handle:
        png.Writer(width=width, height=height, bitdepth=16, greyscale=True).write(
            handle,
            mask.tolist(),
        )


def _write_semantic_scene(
    *,
    semantic_path: Path,
    mask_path: Path,
    width: int,
    height: int,
    exposure_bias_ev: float | None,
    clipping_policy: str | None,
) -> None:
    response_hints: dict[str, Any] = {
        "preserve_highlight_detail": True,
        "noise_priority": "low",
    }
    if exposure_bias_ev is not None:
        response_hints["exposure_bias_ev"] = exposure_bias_ev
    payload = {
        "schema": SEMANTIC_SCENE_SCHEMA,
        "scene": {
            "id": semantic_path.stem,
            "description": "synthetic scene-linear semantic reaction evidence fixture",
            "width": width,
            "height": height,
            "coordinate_space": "pixel",
            "input_space": INPUT_SPACE,
        },
        "producer": {
            "name": "image2dng semantic reaction evidence generator",
            "version": __version__,
            "prompt_hash": f"sha256:semantic-reaction-evidence-{semantic_path.stem}",
        },
        "assets": [
            {
                "id": "mask-evidence-region",
                "kind": "mask",
                "path": os.path.relpath(mask_path, semantic_path.parent),
                "space": "pixel",
                "sha256": f"sha256:{_sha256_file(mask_path)}",
            }
        ],
        "materials": [
            {
                "id": "mat-evidence-region",
                "label": "evidence region",
                "base_color": [0.18, 0.18, 0.18],
                "roughness": 0.5,
                "metallic": 0.0,
                "emission": [0.0, 0.0, 0.0],
            }
        ],
        "lights": [
            {
                "id": "key-light",
                "type": "area",
                "color_temperature_kelvin": 6500,
                "relative_intensity": 1.0,
                "direction": [0.0, -0.5, -1.0],
            }
        ],
        "regions": [
            {
                "id": "region-evidence-mask",
                "label": "masked reaction region",
                "material_id": "mat-evidence-region",
                "mask_asset_id": "mask-evidence-region",
                "bbox": [0, 0, width, height],
                "response_hints": response_hints,
            }
        ],
        "sensor_response_hints": {
            "target_white_balance_kelvin": 6500,
            "target_middle_gray": 0.18,
            **({"clipping_policy": clipping_policy} if clipping_policy is not None else {}),
        },
    }
    semantic_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_first_scene(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["scenes"][0]


def _read_first_sample(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["samples"][0]


def _copied_source_path(scene: dict[str, Any]) -> Path:
    outputs = scene["outputs"]
    return Path(outputs.get("original_scene_linear_input", outputs["scene_linear_input"]))


def _append_failures(report: dict[str, Any]) -> None:
    for scenario in report["scenarios"]:
        slug = scenario["slug"]
        for batch_name, validations in scenario["validations"].items():
            for mode, validation in validations.items():
                if not validation["ok"]:
                    report["errors"].append(f"{slug}: {batch_name} {mode} validation failed")
        if not scenario["provenance_changed"]:
            report["errors"].append(f"{slug}: expected provenance context to change")
        if scenario["slug"] == "clip-noop-baseline":
            if scenario["raw_value_changed"]:
                report["errors"].append("clip-noop-baseline: raw values changed")
            if scenario["semantic_to_raw_status"] != "no-op":
                report["errors"].append("clip-noop-baseline: semantic status was not no-op")
        elif not scenario["raw_value_changed"]:
            report["errors"].append(f"{slug}: expected raw values to change")


def _artifact_entry(root: Path, path: Path, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": _relative_path(path, root),
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Semantic Reaction Evidence Summary",
        "",
        f"- Schema: `{report['schema']}`",
        f"- OK: `{report['ok']}`",
        f"- Output: `{report['output_dir']}`",
        "",
        "| Scenario | Status | Raw value changed | Reactions |",
        "| --- | --- | --- | --- |",
    ]
    for scenario in report["scenarios"]:
        reactions = ", ".join(
            reaction["model"] for reaction in scenario["semantic_reactions"]
        )
        lines.append(
            "| {slug} | {status} | {changed} | {reactions} |".format(
                slug=scenario["slug"],
                status=scenario["semantic_to_raw_status"],
                changed=scenario["raw_value_changed"],
                reactions=reactions,
            )
        )
    if report["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
