from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import Image

from image2dng import __version__
from image2dng.semantic_scene import validate_semantic_scene

MANIFEST_SCHEMA = "image2dng.semantic_physics_dataset_manifest.v1"
SEMANTIC_SCENE_SCHEMA = "image2dng.semantic_scene.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a local-only semantic-physics sample from the FiveK smoke pair."
    )
    parser.add_argument(
        "--fivek-dir",
        type=Path,
        default=Path("data/fivek-smoke"),
        help="directory containing the ignored FiveK smoke DNG/TIFF pair",
    )
    parser.add_argument(
        "--sample-id",
        default="a0001-jmac_DSC1459",
        help="FiveK smoke sample id without extension",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/semantic-physics-dataset/fivek-smoke"),
        help="local-only output directory; defaults under demo-output/",
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing local research manifests outside demo-output/",
    )
    args = parser.parse_args(argv)

    if not _output_dir_is_allowed(args.output_dir) and not args.allow_output_outside_demo_output:
        print(
            "Refusing to write semantic-physics sample outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override.",
            flush=True,
        )
        return 2

    if not _sample_id_is_safe(args.sample_id):
        print(
            "Refusing sample id with path components. Use a plain file stem such as "
            "'a0001-jmac_DSC1459'.",
            flush=True,
        )
        return 2

    dng_path = args.fivek_dir / f"{args.sample_id}.dng"
    tiff_path = args.fivek_dir / f"{args.sample_id}.tif"
    missing = [str(path) for path in (dng_path, tiff_path) if not path.exists()]
    if missing:
        print(f"Missing FiveK smoke input(s): {', '.join(missing)}", flush=True)
        return 2

    output_dir = args.output_dir
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    image = _read_rgb16(tiff_path)
    height, width = image.shape[:2]
    preview_path = assets_dir / f"{args.sample_id}-neutral-preview.jpg"
    mask_path = assets_dir / f"{args.sample_id}-center-mask.png"
    _write_preview(image, preview_path)
    bbox = _center_bbox(width, height)
    _write_mask(width, height, bbox, mask_path)

    stats = _region_stats(image, bbox)
    semantic_path = output_dir / f"{args.sample_id}.semantic.json"
    semantic_payload = _semantic_payload(
        sample_id=args.sample_id,
        width=width,
        height=height,
        bbox=bbox,
        stats=stats,
        dng_path=dng_path,
        tiff_path=tiff_path,
        preview_path=preview_path,
        mask_path=mask_path,
        output_dir=output_dir,
    )
    semantic_path.write_text(
        json.dumps(semantic_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    manifest = _manifest(output_dir, [semantic_path])
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote semantic-physics sidecar to {semantic_path}")
    print(f"Wrote semantic-physics dataset manifest to {manifest_path}")
    return 0 if manifest["all_validations_ok"] else 1


def _read_rgb16(path: Path) -> np.ndarray:
    image = tifffile.imread(path)
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError(f"Expected RGB TIFF input: {path}")
    rgb = image[:, :, :3]
    if rgb.dtype != np.uint16:
        if np.issubdtype(rgb.dtype, np.integer):
            max_value = np.iinfo(rgb.dtype).max
            rgb = np.clip(rgb.astype(np.float64) / max_value * 65535.0, 0, 65535).astype(
                np.uint16
            )
        else:
            rgb = np.clip(rgb.astype(np.float64), 0.0, 1.0)
            rgb = (rgb * 65535.0).round().astype(np.uint16)
    return rgb


def _write_preview(image: np.ndarray, path: Path) -> None:
    rgb = image.astype(np.float32)
    low, high = np.percentile(rgb, [0.5, 99.5])
    if high <= low:
        low, high = 0.0, 65535.0
    normalized = np.clip((rgb - low) / (high - low), 0.0, 1.0)
    srgb = np.power(normalized, 1.0 / 2.2)
    preview = (srgb * 255.0).round().astype(np.uint8)
    Image.fromarray(preview, mode="RGB").save(path, format="JPEG", quality=92)


def _center_bbox(width: int, height: int) -> list[int]:
    box_width = max(1, width // 3)
    box_height = max(1, height // 3)
    x = (width - box_width) // 2
    y = (height - box_height) // 2
    return [x, y, box_width, box_height]


def _write_mask(width: int, height: int, bbox: list[int], path: Path) -> None:
    x, y, box_width, box_height = bbox
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y : y + box_height, x : x + box_width] = 255
    Image.fromarray(mask, mode="L").save(path, format="PNG")


def _region_stats(image: np.ndarray, bbox: list[int]) -> dict[str, Any]:
    x, y, width, height = bbox
    region = image[y : y + height, x : x + width, :3].astype(np.float64) / 65535.0
    pixels = region.reshape(-1, 3)
    return {
        "mean_linear_rgb": _round_triplet(pixels.mean(axis=0)),
        "p50_linear_rgb": _round_triplet(np.percentile(pixels, 50, axis=0)),
        "p95_linear_rgb": _round_triplet(np.percentile(pixels, 95, axis=0)),
        "clipped_pixel_ratio": round(float(np.any(region >= 0.999, axis=2).mean()), 6),
        "shadow_pixel_ratio": round(float(np.all(region <= 0.01, axis=2).mean()), 6),
    }


def _round_triplet(values: np.ndarray) -> list[float]:
    return [round(float(value), 6) for value in values.tolist()]


def _semantic_payload(
    *,
    sample_id: str,
    width: int,
    height: int,
    bbox: list[int],
    stats: dict[str, Any],
    dng_path: Path,
    tiff_path: Path,
    preview_path: Path,
    mask_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    return {
        "schema": SEMANTIC_SCENE_SCHEMA,
        "scene": {
            "id": f"fivek-smoke-{sample_id}",
            "description": "FiveK smoke DNG plus rendered TIFF16 semantic-physics fixture",
            "width": width,
            "height": height,
            "coordinate_space": "pixel",
            "input_space": "srgb",
        },
        "producer": {
            "name": "MIT-Adobe FiveK smoke sample",
            "version": "local-smoke",
            "source_dng_sha256": _sha256_file(dng_path),
            "source_tiff_sha256": _sha256_file(tiff_path),
            "notes": "DNG/TIFF bytes remain local-only under ignored data/.",
        },
        "assets": [
            {
                "id": "neutral-preview",
                "kind": "preview",
                "path": _relative_path(preview_path, output_dir),
                "sha256": _sha256_file(preview_path),
                "space": "srgb",
            },
            {
                "id": "center-region-mask",
                "kind": "mask",
                "path": _relative_path(mask_path, output_dir),
                "sha256": _sha256_file(mask_path),
                "space": "pixel",
            },
        ],
        "materials": [
            {
                "id": "mat-center-region",
                "label": "unlabeled FiveK center crop",
                "base_color": stats["mean_linear_rgb"],
                "roughness": 0.5,
                "metallic": 0.0,
                "emission": [0.0, 0.0, 0.0],
            }
        ],
        "lights": [
            {
                "id": "metadata-daylight",
                "type": "ambient",
                "color_temperature_kelvin": 5200,
                "relative_intensity": 1.0,
            }
        ],
        "regions": [
            {
                "id": "center-region",
                "label": "center crop",
                "material_id": "mat-center-region",
                "mask_asset_id": "center-region-mask",
                "bbox": bbox,
                "response_hints": {
                    "source": "metadata",
                    "confidence": 0.5,
                    "exposure_bias_ev": 0.0,
                    "preserve_highlight_detail": True,
                    "noise_priority": "medium",
                },
                "raw_statistics": stats,
            }
        ],
        "capture_physics": {
            "source": "metadata",
            "iso": 200,
            "exposure_time_seconds": 0.0025,
            "aperture_f_number": 10.0,
            "white_balance_kelvin": 5200,
            "illuminant_confidence": 0.5,
            "ev100": 14.3,
        },
        "camera_response": {
            "camera_make": "NIKON CORPORATION",
            "camera_model": "NIKON D70",
            "cfa_pattern": "gbrg",
            "black_level": [0, 0, 0, 0],
            "white_level": 4095,
            "color_matrix_1": [],
        },
    }


def _manifest(output_dir: Path, semantic_paths: list[Path]) -> dict[str, Any]:
    samples = [_sample_record(path) for path in semantic_paths]
    return {
        "schema": MANIFEST_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "local_research_only": True,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "output_dir": str(output_dir),
        "sample_count": len(samples),
        "all_validations_ok": all(sample["validation_ok"] for sample in samples),
        "samples": samples,
    }


def _sample_record(path: Path) -> dict[str, Any]:
    validation = validate_semantic_scene(path)
    payload = _read_payload(path)
    regions = payload.get("regions", [])
    return {
        "sample_id": validation.scene_id or path.stem,
        "semantic_sidecar_path": str(path),
        "semantic_sidecar_sha256": _sha256_file(path),
        "validation_ok": validation.ok,
        "validation": validation.to_dict(),
        "scene_id": validation.scene_id,
        "scene_dimensions": {
            "width": validation.scene_width,
            "height": validation.scene_height,
        },
        "counts": validation.counts or {},
        "semantic_physics_fields": {
            "capture_physics": isinstance(payload.get("capture_physics"), dict),
            "camera_response": isinstance(payload.get("camera_response"), dict),
            "region_raw_statistics_count": sum(
                1
                for region in regions
                if isinstance(region, dict) and isinstance(region.get("raw_statistics"), dict)
            ),
        },
    }


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _relative_path(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _sample_id_is_safe(sample_id: str) -> bool:
    if not sample_id:
        return False
    sample_path = Path(sample_id)
    return (
        not sample_path.is_absolute()
        and sample_path.name == sample_id
        and ".." not in sample_path.parts
        and "/" not in sample_id
        and "\\" not in sample_id
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
