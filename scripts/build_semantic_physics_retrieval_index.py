from __future__ import annotations

import argparse
import json
import platform
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from image2dng import __version__

INDEX_SCHEMA = "image2dng.semantic_physics_retrieval_index.v1"
DATASET_SCHEMA = "image2dng.semantic_physics_dataset_manifest.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a local-only retrieval baseline index from semantic-physics data."
    )
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/semantic-physics-retrieval-index"),
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing local research indexes outside demo-output/",
    )
    args = parser.parse_args(argv)

    if not _output_dir_is_allowed(args.output_dir) and not args.allow_output_outside_demo_output:
        print(
            "Refusing to write retrieval indexes outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override.",
            flush=True,
        )
        return 2

    dataset = _read_json(args.dataset_manifest)
    if dataset.get("schema") != DATASET_SCHEMA:
        print(f"Unsupported dataset manifest schema: {dataset.get('schema')!r}", flush=True)
        return 2
    if not dataset.get("all_validations_ok"):
        print("Refusing to index a dataset manifest with failed validations.", flush=True)
        return 1

    records = _region_records(dataset, args.dataset_manifest.parent)
    index = _build_index(args.dataset_manifest, records)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    index_path = args.output_dir / "retrieval-index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote semantic-physics retrieval index to {index_path}")
    return 0


def _region_records(dataset: dict[str, Any], manifest_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for sample in dataset.get("samples", []):
        if not isinstance(sample, dict) or not sample.get("validation_ok"):
            continue
        sidecar_value = sample.get("semantic_sidecar_path")
        if not isinstance(sidecar_value, str):
            continue
        sidecar_path = _resolve_sidecar_path(sidecar_value, manifest_dir)
        sidecar = _read_json(sidecar_path)
        materials = _id_map(sidecar.get("materials"))
        scene = sidecar.get("scene") if isinstance(sidecar.get("scene"), dict) else {}
        for region in sidecar.get("regions", []):
            if not isinstance(region, dict) or not isinstance(region.get("raw_statistics"), dict):
                continue
            material = materials.get(region.get("material_id"), {})
            material_label = _string_or_unknown(material.get("label"))
            region_label = _string_or_unknown(region.get("label"))
            records.append(
                {
                    "sample_id": sample.get("sample_id") or sample.get("scene_id"),
                    "scene_id": scene.get("id"),
                    "region_id": region.get("id"),
                    "region_label": region_label,
                    "material_label": material_label,
                    "lookup_key": _lookup_key(material_label, region_label),
                    "raw_statistics": region["raw_statistics"],
                    "capture_physics": sidecar.get("capture_physics") or {},
                    "camera_response": sidecar.get("camera_response") or {},
                    "semantic_sidecar_sha256": sample.get("semantic_sidecar_sha256"),
                }
            )
    return records


def _build_index(dataset_manifest: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["lookup_key"]].append(record)

    return {
        "schema": INDEX_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "local_research_only": True,
        "dataset_manifest": str(dataset_manifest),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "record_count": len(records),
        "lookup_keys": sorted(groups),
        "priors": {
            key: {
                "sample_count": len(items),
                "mean_linear_rgb": _mean_triplet(
                    [item["raw_statistics"].get("mean_linear_rgb") for item in items]
                ),
                "p95_linear_rgb": _mean_triplet(
                    [item["raw_statistics"].get("p95_linear_rgb") for item in items]
                ),
                "clipped_pixel_ratio": _mean_scalar(
                    [item["raw_statistics"].get("clipped_pixel_ratio") for item in items]
                ),
                "shadow_pixel_ratio": _mean_scalar(
                    [item["raw_statistics"].get("shadow_pixel_ratio") for item in items]
                ),
                "records": items,
            }
            for key, items in sorted(groups.items())
        },
    }


def _resolve_sidecar_path(value: str, manifest_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    candidate = manifest_dir / path
    if candidate.exists():
        return candidate
    if path.exists():
        return path
    for parent in manifest_dir.parents:
        candidate = parent / path
        if candidate.exists():
            return candidate
    return path


def _id_map(items: object) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        item["id"]: item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _lookup_key(material_label: str, region_label: str) -> str:
    return f"material:{_slug(material_label)}|region:{_slug(region_label)}"


def _slug(value: str) -> str:
    return "-".join(value.strip().lower().split()) or "unknown"


def _string_or_unknown(value: object) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _mean_triplet(values: list[object]) -> list[float] | None:
    triplets = [
        value
        for value in values
        if isinstance(value, list) and len(value) == 3 and all(_is_number(item) for item in value)
    ]
    if not triplets:
        return None
    return [
        round(sum(float(triplet[index]) for triplet in triplets) / len(triplets), 6)
        for index in range(3)
    ]


def _mean_scalar(values: list[object]) -> float | None:
    numbers = [float(value) for value in values if _is_number(value)]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _output_dir_is_allowed(output_dir: Path) -> bool:
    repo_root = Path(__file__).resolve().parents[1]
    demo_output = (repo_root / "demo-output").resolve()
    resolved = output_dir.resolve()
    return resolved == demo_output or demo_output in resolved.parents


if __name__ == "__main__":
    raise SystemExit(main())
