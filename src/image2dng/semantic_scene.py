from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEMANTIC_SCENE_SCHEMA = "image2dng.semantic_scene.v1"
PHYSICS_SOURCE_VALUES = {"measured", "metadata", "inferred", "synthetic", "retrieved"}
CFA_PATTERN_VALUES = {"rggb", "bggr", "grbg", "gbrg"}


@dataclass(frozen=True)
class SemanticSceneValidationResult:
    path: Path
    ok: bool
    schema: str | None
    errors: list[str]
    warnings: list[str]
    scene_id: str | None = None
    scene_width: int | None = None
    scene_height: int | None = None
    asset_paths: dict[str, Path] | None = None
    counts: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": self.schema,
            "path": str(self.path),
            "scene_id": self.scene_id,
            "scene_dimensions": {
                "width": self.scene_width,
                "height": self.scene_height,
            },
            "counts": self.counts or {},
            "asset_paths": {
                asset_id: str(path) for asset_id, path in (self.asset_paths or {}).items()
            },
            "errors": self.errors,
            "warnings": self.warnings,
        }


def validate_semantic_scene(path: str | Path) -> SemanticSceneValidationResult:
    source = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    schema: str | None = None
    scene_id: str | None = None
    scene_width: int | None = None
    scene_height: int | None = None
    asset_paths: dict[str, Path] = {}
    counts = {"assets": 0, "materials": 0, "lights": 0, "regions": 0}

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _result(source, False, None, [f"invalid JSON: {exc.msg}"], warnings)
    except OSError as exc:
        return _result(source, False, None, [str(exc)], warnings)

    if not isinstance(payload, dict):
        return _result(
            source,
            False,
            None,
            ["semantic scene sidecar must be a JSON object"],
            warnings,
        )

    schema_value = payload.get("schema")
    schema = schema_value if isinstance(schema_value, str) else None
    if schema != SEMANTIC_SCENE_SCHEMA:
        errors.append(f"unsupported semantic scene schema: {schema_value!r}")

    scene = payload.get("scene")
    if not isinstance(scene, dict):
        errors.append("scene must be an object")
    else:
        scene_id = _required_string(scene, "scene.id", errors)
        scene_width = _required_positive_int(scene, "width", "scene.width", errors)
        scene_height = _required_positive_int(scene, "height", "scene.height", errors)
        _required_string(scene, "scene.coordinate_space", errors, key="coordinate_space")
        _required_string(scene, "scene.input_space", errors, key="input_space")

    assets = _optional_array(payload, "assets", errors)
    materials = _optional_array(payload, "materials", errors)
    lights = _optional_array(payload, "lights", errors)
    regions = _optional_array(payload, "regions", errors)
    counts = {
        "assets": len(assets),
        "materials": len(materials),
        "lights": len(lights),
        "regions": len(regions),
    }

    asset_ids = _collect_unique_ids(assets, "assets", errors)
    material_ids = _collect_unique_ids(materials, "materials", errors)
    _collect_unique_ids(lights, "lights", errors)
    _collect_unique_ids(regions, "regions", errors)

    asset_paths = _validate_assets(assets, source.parent, errors, warnings)
    _validate_materials(materials, errors)
    _validate_lights(lights, errors)
    _validate_regions(regions, material_ids, asset_ids, scene_width, scene_height, errors)
    _validate_sensor_response_hints(payload.get("sensor_response_hints"), errors)
    _validate_capture_physics(payload.get("capture_physics"), errors)
    _validate_camera_response(payload.get("camera_response"), errors)

    return SemanticSceneValidationResult(
        path=source,
        ok=not errors,
        schema=schema,
        errors=errors,
        warnings=warnings,
        scene_id=scene_id,
        scene_width=scene_width,
        scene_height=scene_height,
        asset_paths=asset_paths,
        counts=counts,
    )


def _result(
    path: Path,
    ok: bool,
    schema: str | None,
    errors: list[str],
    warnings: list[str],
) -> SemanticSceneValidationResult:
    return SemanticSceneValidationResult(
        path=path,
        ok=ok,
        schema=schema,
        errors=errors,
        warnings=warnings,
        counts={"assets": 0, "materials": 0, "lights": 0, "regions": 0},
    )


def _required_string(
    payload: dict[str, Any],
    label: str,
    errors: list[str],
    *,
    key: str = "id",
) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    errors.append(f"{label} must be a non-empty string")
    return None


def _required_positive_int(
    payload: dict[str, Any],
    key: str,
    label: str,
    errors: list[str],
) -> int | None:
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    errors.append(f"{label} must be a positive integer")
    return None


def _optional_array(payload: dict[str, Any], key: str, errors: list[str]) -> list[Any]:
    value = payload.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{key} must be an array when present")
        return []
    return value


def _collect_unique_ids(items: list[Any], label: str, errors: list[str]) -> set[str]:
    ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{label}[{index}] must be an object")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            errors.append(f"{label}[{index}].id must be a non-empty string")
            continue
        if item_id in ids:
            errors.append(f"duplicate {label} id: {item_id}")
        ids.add(item_id)
    return ids


def _validate_assets(
    assets: list[Any],
    base: Path,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Path]:
    resolved_paths: dict[str, Path] = {}
    for index, item in enumerate(assets):
        if not isinstance(item, dict):
            continue
        asset_id = item.get("id")
        asset_path = item.get("path")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        if asset_path is None:
            continue
        if not isinstance(asset_path, str) or not asset_path:
            errors.append(f"assets[{index}].path must be a non-empty string when present")
            continue
        resolved = _resolve_asset_path(asset_path, base, index, errors)
        if resolved is None:
            continue
        if not resolved.exists():
            errors.append(f"asset path does not exist for {asset_id}: {resolved}")
        elif not resolved.is_file():
            errors.append(f"asset path must reference a file for {asset_id}: {resolved}")
        else:
            _validate_asset_sha256(item.get("sha256"), resolved, f"assets[{index}]", errors)
            resolved_paths[asset_id] = resolved
        if not isinstance(item.get("sha256"), str):
            warnings.append(f"assets[{index}].sha256 is missing; asset integrity is unverified")
    return resolved_paths


def _resolve_asset_path(
    asset_path: str,
    base: Path,
    index: int,
    errors: list[str],
) -> Path | None:
    candidate = Path(asset_path)
    if candidate.is_absolute():
        errors.append(f"assets[{index}].path must be relative to the semantic sidecar")
        return None
    resolved_base = base.resolve()
    resolved = (base / candidate).resolve()
    if resolved != resolved_base and resolved_base not in resolved.parents:
        errors.append(f"assets[{index}].path must stay within the semantic sidecar directory")
        return None
    return resolved


def _validate_asset_sha256(
    value: Any,
    path: Path,
    label: str,
    errors: list[str],
) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        errors.append(f"{label}.sha256 must be a sha256:<hex> string when present")
        return
    prefix = "sha256:"
    digest = value.removeprefix(prefix)
    if not value.startswith(prefix) or len(digest) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in digest
    ):
        errors.append(f"{label}.sha256 must be a sha256:<hex> string when present")
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest.lower() != actual:
        errors.append(f"{label}.sha256 does not match asset contents")


def _validate_materials(materials: list[Any], errors: list[str]) -> None:
    for index, item in enumerate(materials):
        if not isinstance(item, dict):
            continue
        _validate_rgb_triplet(item.get("base_color"), f"materials[{index}].base_color", errors)
        _validate_rgb_triplet(item.get("emission"), f"materials[{index}].emission", errors)
        _validate_unit_value(item.get("roughness"), f"materials[{index}].roughness", errors)
        _validate_unit_value(item.get("metallic"), f"materials[{index}].metallic", errors)


def _validate_lights(lights: list[Any], errors: list[str]) -> None:
    for index, item in enumerate(lights):
        if not isinstance(item, dict):
            continue
        color_temperature = item.get("color_temperature_kelvin")
        if color_temperature is not None and (
            not _is_number(color_temperature) or color_temperature <= 0
        ):
            errors.append(f"lights[{index}].color_temperature_kelvin must be positive")
        intensity = item.get("relative_intensity")
        if intensity is not None and (not _is_number(intensity) or intensity < 0):
            errors.append(f"lights[{index}].relative_intensity must be non-negative")
        direction = item.get("direction")
        if direction is not None and not _is_number_list(direction, 3):
            errors.append(f"lights[{index}].direction must contain three numeric values")


def _validate_regions(
    regions: list[Any],
    material_ids: set[str],
    asset_ids: set[str],
    scene_width: int | None,
    scene_height: int | None,
    errors: list[str],
) -> None:
    for index, item in enumerate(regions):
        if not isinstance(item, dict):
            continue
        material_id = item.get("material_id")
        if material_id is not None and material_id not in material_ids:
            errors.append(
                f"regions[{index}].material_id references unknown material: {material_id}"
            )
        mask_asset_id = item.get("mask_asset_id")
        if mask_asset_id is not None and mask_asset_id not in asset_ids:
            errors.append(
                f"regions[{index}].mask_asset_id references unknown asset: {mask_asset_id}"
            )
        _validate_bbox(item.get("bbox"), index, scene_width, scene_height, errors)
        hints = item.get("response_hints")
        if hints is not None:
            _validate_response_hints(hints, f"regions[{index}].response_hints", errors)
        raw_statistics = item.get("raw_statistics")
        if raw_statistics is not None:
            _validate_raw_statistics(
                raw_statistics,
                f"regions[{index}].raw_statistics",
                errors,
            )


def _validate_bbox(
    value: Any,
    index: int,
    scene_width: int | None,
    scene_height: int | None,
    errors: list[str],
) -> None:
    if value is None:
        return
    if not _is_number_list(value, 4):
        errors.append(f"regions[{index}].bbox must contain four numeric values")
        return
    x, y, width, height = value
    if width <= 0 or height <= 0:
        errors.append(f"regions[{index}].bbox width and height must be positive")
    if x < 0 or y < 0:
        errors.append(f"regions[{index}].bbox origin must be non-negative")
    if scene_width is not None and x + width > scene_width:
        errors.append(f"regions[{index}].bbox exceeds scene width")
    if scene_height is not None and y + height > scene_height:
        errors.append(f"regions[{index}].bbox exceeds scene height")


def _validate_response_hints(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    exposure_bias = value.get("exposure_bias_ev")
    if exposure_bias is not None and not _is_number(exposure_bias):
        errors.append(f"{label}.exposure_bias_ev must be numeric")
    preserve_highlight = value.get("preserve_highlight_detail")
    if preserve_highlight is not None and not isinstance(preserve_highlight, bool):
        errors.append(f"{label}.preserve_highlight_detail must be boolean")
    noise_priority = value.get("noise_priority")
    if noise_priority is not None and noise_priority not in {"low", "medium", "high"}:
        errors.append(f"{label}.noise_priority must be one of low, medium, high")
    _validate_source(value.get("source"), f"{label}.source", errors)
    confidence = value.get("confidence")
    if confidence is not None:
        _validate_unit_value(confidence, f"{label}.confidence", errors)


def _validate_sensor_response_hints(value: Any, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append("sensor_response_hints must be an object")
        return
    white_balance = value.get("target_white_balance_kelvin")
    if white_balance is not None and (not _is_number(white_balance) or white_balance <= 0):
        errors.append("sensor_response_hints.target_white_balance_kelvin must be positive")
    white_balance_policy = value.get("target_white_balance_policy")
    if white_balance_policy is not None and white_balance_policy != "channel-gain-v1":
        errors.append("sensor_response_hints.target_white_balance_policy is unsupported")
    white_balance_max_gain = value.get("target_white_balance_max_gain_ev")
    if white_balance_max_gain is not None and (
        not _is_number(white_balance_max_gain) or white_balance_max_gain <= 0
    ):
        errors.append("sensor_response_hints.target_white_balance_max_gain_ev must be positive")
    middle_gray = value.get("target_middle_gray")
    if middle_gray is not None and (not _is_number(middle_gray) or not 0 < middle_gray < 1):
        errors.append("sensor_response_hints.target_middle_gray must be between 0 and 1")
    middle_gray_policy = value.get("target_middle_gray_policy")
    if middle_gray_policy is not None and middle_gray_policy != "global-gain-v1":
        errors.append("sensor_response_hints.target_middle_gray_policy is unsupported")
    middle_gray_max_gain = value.get("target_middle_gray_max_gain_ev")
    if middle_gray_max_gain is not None and (
        not _is_number(middle_gray_max_gain) or middle_gray_max_gain <= 0
    ):
        errors.append("sensor_response_hints.target_middle_gray_max_gain_ev must be positive")
    clipping_policy = value.get("clipping_policy")
    if clipping_policy is not None and clipping_policy not in {
        "preserve-highlights",
        "clip",
        "soft-rolloff",
    }:
        errors.append("sensor_response_hints.clipping_policy is unsupported")


def _validate_capture_physics(value: Any, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append("capture_physics must be an object")
        return
    _validate_source(value.get("source"), "capture_physics.source", errors)
    for key in (
        "iso",
        "exposure_time_seconds",
        "aperture_f_number",
        "white_balance_kelvin",
        "lux",
    ):
        _validate_optional_positive_number(value.get(key), f"capture_physics.{key}", errors)
    _validate_optional_finite_number(value.get("ev100"), "capture_physics.ev100", errors)
    confidence = value.get("illuminant_confidence")
    if confidence is not None:
        _validate_unit_value(confidence, "capture_physics.illuminant_confidence", errors)


def _validate_camera_response(value: Any, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append("camera_response must be an object")
        return
    cfa_pattern = value.get("cfa_pattern")
    if (
        cfa_pattern is not None
        and (not isinstance(cfa_pattern, str) or cfa_pattern not in CFA_PATTERN_VALUES)
    ):
        errors.append("camera_response.cfa_pattern must be one of bggr, gbrg, grbg, rggb")
    black_level = value.get("black_level")
    white_level = value.get("white_level")
    _validate_black_level(black_level, errors)
    _validate_optional_positive_number(white_level, "camera_response.white_level", errors)
    _validate_black_level_less_than_white_level(black_level, white_level, errors)


def _validate_black_level(value: Any, errors: list[str]) -> None:
    if value is None:
        return
    if _is_number(value):
        if value < 0:
            errors.append("camera_response.black_level must be non-negative")
        return
    if not isinstance(value, list) or not value:
        errors.append("camera_response.black_level must be a non-negative number or array")
        return
    if any(not _is_number(item) or item < 0 for item in value):
        errors.append("camera_response.black_level must contain non-negative numeric values")


def _validate_black_level_less_than_white_level(
    black_level: Any,
    white_level: Any,
    errors: list[str],
) -> None:
    if not _is_number(white_level):
        return
    if _is_number(black_level):
        if black_level >= white_level:
            errors.append("camera_response.black_level must be less than white_level")
        return
    if not isinstance(black_level, list):
        return
    numeric_black_levels = [item for item in black_level if _is_number(item)]
    if any(item >= white_level for item in numeric_black_levels):
        errors.append("camera_response.black_level must be less than white_level")


def _validate_raw_statistics(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    for key in ("mean_linear_rgb", "p50_linear_rgb", "p95_linear_rgb"):
        _validate_non_negative_rgb_triplet(value.get(key), f"{label}.{key}", errors)
    for key in ("clipped_pixel_ratio", "shadow_pixel_ratio"):
        ratio = value.get(key)
        if ratio is not None:
            _validate_unit_value(ratio, f"{label}.{key}", errors)


def _validate_source(value: Any, label: str, errors: list[str]) -> None:
    if (
        value is not None
        and (not isinstance(value, str) or value not in PHYSICS_SOURCE_VALUES)
    ):
        errors.append(
            f"{label} must be one of inferred, measured, metadata, retrieved, synthetic"
        )


def _validate_rgb_triplet(value: Any, label: str, errors: list[str]) -> None:
    if value is not None and (
        not _is_number_list(value, 3) or any(channel < 0 for channel in value)
    ):
        errors.append(f"{label} must contain three non-negative numeric values")


def _validate_non_negative_rgb_triplet(value: Any, label: str, errors: list[str]) -> None:
    if value is not None and (
        not _is_number_list(value, 3) or any(channel < 0 for channel in value)
    ):
        errors.append(f"{label} must contain three non-negative numeric values")


def _validate_unit_value(value: Any, label: str, errors: list[str]) -> None:
    if value is not None and (not _is_number(value) or not 0 <= value <= 1):
        errors.append(f"{label} must be between 0 and 1")


def _validate_optional_positive_number(value: Any, label: str, errors: list[str]) -> None:
    if value is not None and (not _is_number(value) or value <= 0):
        errors.append(f"{label} must be a positive number")


def _validate_optional_finite_number(value: Any, label: str, errors: list[str]) -> None:
    if value is not None and not _is_number(value):
        errors.append(f"{label} must be a finite number")


def _is_number_list(value: Any, length: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == length
        and all(_is_number(item) for item in value)
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
