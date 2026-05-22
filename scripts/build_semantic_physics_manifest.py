from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from image2dng import __version__
from image2dng.semantic_scene import validate_semantic_scene

MANIFEST_SCHEMA = "image2dng.semantic_physics_dataset_manifest.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a local-only semantic-physics dataset manifest from sidecars."
    )
    parser.add_argument(
        "--semantic-sidecar",
        type=Path,
        action="append",
        required=True,
        help="semantic scene sidecar to index; repeat for multiple samples",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/semantic-physics-dataset"),
        help="local-only manifest directory; defaults under demo-output/",
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing local research manifests outside demo-output/",
    )
    args = parser.parse_args(argv)

    output_dir = args.output_dir
    if not _output_dir_is_allowed(output_dir) and not args.allow_output_outside_demo_output:
        print(
            "Refusing to write semantic-physics manifests outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override.",
            flush=True,
        )
        return 2

    samples = [_sample_record(path) for path in args.semantic_sidecar]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "local_research_only": True,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "sample_count": len(samples),
        "all_validations_ok": _all_validations_ok(samples),
        "samples": samples,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote semantic-physics dataset manifest to {manifest_path}")
    return 0 if manifest["all_validations_ok"] else 1


def _sample_record(path: Path) -> dict[str, Any]:
    validation = validate_semantic_scene(path)
    payload = _read_payload(path)
    scene = payload.get("scene") if isinstance(payload, dict) else {}
    regions = payload.get("regions") if isinstance(payload, dict) else []
    return {
        "sample_id": _sample_id(path, scene),
        "semantic_sidecar_path": str(path),
        "semantic_sidecar_sha256": _sha256_file(path) if path.exists() else None,
        "validation_ok": validation.ok,
        "validation": validation.to_dict(),
        "scene_id": validation.scene_id,
        "scene_dimensions": {
            "width": validation.scene_width,
            "height": validation.scene_height,
        },
        "counts": validation.counts or {},
        "semantic_physics_fields": {
            "capture_physics": isinstance(payload, dict)
            and isinstance(payload.get("capture_physics"), dict),
            "camera_response": isinstance(payload, dict)
            and isinstance(payload.get("camera_response"), dict),
            "region_raw_statistics_count": _region_raw_statistics_count(regions),
        },
    }


def _all_validations_ok(samples: list[dict[str, Any]]) -> bool:
    return all(_sample_validation_ok(sample) for sample in samples)


def _sample_validation_ok(sample: dict[str, Any]) -> bool:
    validation_ok = sample["validation_ok"]
    if not isinstance(validation_ok, bool):
        raise TypeError("sample validation_ok must be a boolean")
    return validation_ok


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sample_id(path: Path, scene: object) -> str:
    if isinstance(scene, dict) and isinstance(scene.get("id"), str) and scene["id"]:
        return scene["id"]
    return path.stem


def _region_raw_statistics_count(regions: object) -> int:
    if not isinstance(regions, list):
        return 0
    return sum(
        1
        for region in regions
        if isinstance(region, dict) and isinstance(region.get("raw_statistics"), dict)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _output_dir_is_allowed(output_dir: Path) -> bool:
    repo_root = Path(__file__).resolve().parents[1]
    demo_output = (repo_root / "demo-output").resolve()
    resolved = output_dir.resolve()
    return resolved == demo_output or demo_output in resolved.parents


if __name__ == "__main__":
    raise SystemExit(main())
