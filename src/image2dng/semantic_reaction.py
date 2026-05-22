from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import Image

REGION_EXPOSURE_REACTION_MODEL = "region-exposure-mask-v1"
HIGHLIGHT_CLIPPING_REACTION_MODEL = "highlight-clipping-policy-v1"
SUPPORTED_REACTION_INPUT_SPACES = frozenset({"linear-rec709", "acescg", "xyz"})
_HIGHLIGHT_POLICY_SHOULDERS = {
    "preserve-highlights": (0.90, 0.50),
    "soft-rolloff": (0.75, 0.35),
}


@dataclass(frozen=True)
class SemanticReactionModelInfo:
    model_id: str
    status: str
    current_raw_value_effect: bool
    intended_raw_value_effect: bool
    scope: str
    boundary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "status": self.status,
            "current_raw_value_effect": self.current_raw_value_effect,
            "intended_raw_value_effect": self.intended_raw_value_effect,
            "scope": self.scope,
            "boundary": self.boundary,
        }


SEMANTIC_REACTION_MODEL_REGISTRY = {
    REGION_EXPOSURE_REACTION_MODEL: SemanticReactionModelInfo(
        model_id=REGION_EXPOSURE_REACTION_MODEL,
        status="implemented",
        current_raw_value_effect=True,
        intended_raw_value_effect=True,
        scope="mask-bound finite EV modulation for 16-bit scene-linear RGB inputs",
        boundary="deterministic prototype; linear-light only; not a physical sensor model",
    ),
    HIGHLIGHT_CLIPPING_REACTION_MODEL: SemanticReactionModelInfo(
        model_id=HIGHLIGHT_CLIPPING_REACTION_MODEL,
        status="implemented",
        current_raw_value_effect=True,
        intended_raw_value_effect=True,
        scope="deterministic highlight value mapping for explicit clipping policies",
        boundary=(
            "deterministic shoulder mapping only; must not claim camera tone-curve, "
            "ISO response, or preserved sensor detail"
        ),
    ),
}


def semantic_reaction_model_registry() -> dict[str, dict[str, Any]]:
    return {
        model_id: model_info.to_dict()
        for model_id, model_info in SEMANTIC_REACTION_MODEL_REGISTRY.items()
    }


@dataclass(frozen=True)
class SemanticReactionResult:
    model: str
    applied: bool
    status: str
    regions: list[dict[str, Any]]
    affected_pixels: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "applied": self.applied,
            "status": self.status,
            "region_count": len(self.regions),
            "affected_pixels": self.affected_pixels,
            "regions": self.regions,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    def summary_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "applied": self.applied,
            "region_count": len(self.regions),
            "affected_pixels": self.affected_pixels,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def apply_region_exposure_reaction(
    image: np.ndarray,
    *,
    semantic_payload: dict[str, Any],
    semantic_base_dir: Path,
    input_space: str | None = None,
) -> tuple[np.ndarray, SemanticReactionResult]:
    if image.dtype != np.uint16:
        raise ValueError("semantic reaction input image must be uint16")
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("semantic reaction input image must be RGB")
    _validate_scene_binding(image, semantic_payload, input_space)

    asset_paths = _asset_paths(semantic_payload, semantic_base_dir)
    output = image.astype(np.float64, copy=True)
    applied_regions: list[dict[str, Any]] = []
    affected_total = 0

    for region in _regions_with_exposure(semantic_payload):
        mask_asset_id = region["mask_asset_id"]
        mask_path = asset_paths.get(mask_asset_id)
        if mask_path is None:
            raise ValueError(f"semantic reaction mask asset is missing: {mask_asset_id}")
        mask = _read_mask(mask_path)
        if mask.shape != image.shape[:2]:
            raise ValueError(
                f"semantic reaction mask size mismatch for {mask_asset_id}: "
                f"{mask.shape[1]}x{mask.shape[0]} != {image.shape[1]}x{image.shape[0]}"
            )
        active = mask > 0
        affected_pixels = int(np.count_nonzero(active))
        if affected_pixels == 0:
            continue
        exposure_bias_ev = float(region["exposure_bias_ev"])
        output[active] *= 2.0**exposure_bias_ev
        affected_total += affected_pixels
        applied_regions.append(
            {
                "region_id": region["id"],
                "mask_asset_id": mask_asset_id,
                "exposure_bias_ev": exposure_bias_ev,
                "affected_pixels": affected_pixels,
            }
        )

    if not applied_regions:
        return image.copy(), SemanticReactionResult(
            model=REGION_EXPOSURE_REACTION_MODEL,
            applied=False,
            status="no-op",
            reason="no regions with exposure_bias_ev and mask_asset_id",
            regions=[],
        )

    return np.rint(np.clip(output, 0.0, 65535.0)).astype(np.uint16), SemanticReactionResult(
        model=REGION_EXPOSURE_REACTION_MODEL,
        applied=True,
        status="applied",
        regions=applied_regions,
        affected_pixels=affected_total,
    )


def apply_highlight_clipping_policy(
    image: np.ndarray,
    *,
    semantic_payload: dict[str, Any],
    input_space: str | None = None,
) -> tuple[np.ndarray, SemanticReactionResult]:
    if image.dtype != np.uint16:
        raise ValueError("semantic reaction input image must be uint16")
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("semantic reaction input image must be RGB")
    _validate_scene_binding(image, semantic_payload, input_space)

    hints = semantic_payload.get("sensor_response_hints")
    policy = hints.get("clipping_policy") if isinstance(hints, dict) else None
    if policy not in _HIGHLIGHT_POLICY_SHOULDERS:
        return image.copy(), SemanticReactionResult(
            model=HIGHLIGHT_CLIPPING_REACTION_MODEL,
            applied=False,
            status="no-op",
            reason="no highlight clipping policy requiring value mapping",
            regions=[],
        )

    threshold_ratio, compression = _HIGHLIGHT_POLICY_SHOULDERS[policy]
    threshold = 65535.0 * threshold_ratio
    source = image.astype(np.float64, copy=False)
    highlight_channels = source > threshold
    affected_pixels = int(np.count_nonzero(np.any(highlight_channels, axis=2)))
    if affected_pixels == 0:
        return image.copy(), SemanticReactionResult(
            model=HIGHLIGHT_CLIPPING_REACTION_MODEL,
            applied=False,
            status="no-op",
            reason="no pixels above highlight policy shoulder",
            regions=[],
        )

    mapped = source.copy()
    mapped[highlight_channels] = (
        threshold + (mapped[highlight_channels] - threshold) * compression
    )
    changed_channels = int(np.count_nonzero(highlight_channels))
    return np.rint(np.clip(mapped, 0.0, 65535.0)).astype(np.uint16), SemanticReactionResult(
        model=HIGHLIGHT_CLIPPING_REACTION_MODEL,
        applied=True,
        status="applied",
        regions=[
            {
                "region_id": "global-highlight-shoulder",
                "clipping_policy": policy,
                "shoulder_start": round(threshold_ratio, 4),
                "compression": compression,
                "affected_pixels": affected_pixels,
                "changed_channels": changed_channels,
            }
        ],
        affected_pixels=affected_pixels,
    )


def load_semantic_payload(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("semantic payload must be a JSON object")
    return payload


def read_scene_linear_image(path: str | Path) -> np.ndarray:
    source = Path(path)
    if source.suffix.lower() in {".tif", ".tiff", ".dng"}:
        image = tifffile.imread(source)
    else:
        image = np.asarray(Image.open(source))
    if image.ndim == 2:
        raise ValueError("semantic reaction input image must be RGB")
    if image.dtype != np.uint16:
        raise ValueError("semantic reaction input image must be uint16")
    return image


def _asset_paths(payload: dict[str, Any], base_dir: Path) -> dict[str, Path]:
    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        return {}
    paths: dict[str, Path] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("id")
        asset_path = asset.get("path")
        if not isinstance(asset_id, str) or not isinstance(asset_path, str):
            continue
        resolved = Path(asset_path)
        paths[asset_id] = resolved if resolved.is_absolute() else base_dir / resolved
    return paths


def _validate_scene_binding(
    image: np.ndarray,
    payload: dict[str, Any],
    input_space: str | None,
) -> None:
    scene = payload.get("scene")
    if not isinstance(scene, dict):
        raise ValueError("semantic reaction scene metadata is missing")
    width = scene.get("width")
    height = scene.get("height")
    if width != image.shape[1] or height != image.shape[0]:
        raise ValueError(
            "semantic reaction scene dimensions mismatch: "
            f"{width}x{height} != {image.shape[1]}x{image.shape[0]}"
        )
    scene_input_space = scene.get("input_space")
    if input_space is not None and scene_input_space != input_space:
        raise ValueError(
            "semantic reaction input_space mismatch: "
            f"{scene_input_space!r} != {input_space!r}"
        )


def _regions_with_exposure(payload: dict[str, Any]) -> list[dict[str, Any]]:
    regions = payload.get("regions", [])
    if not isinstance(regions, list):
        return []
    reaction_regions: list[dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        response_hints = region.get("response_hints")
        if not isinstance(response_hints, dict):
            continue
        exposure_bias_ev = response_hints.get("exposure_bias_ev")
        mask_asset_id = region.get("mask_asset_id")
        region_id = region.get("id")
        if (
            isinstance(exposure_bias_ev, int | float)
            and not isinstance(exposure_bias_ev, bool)
            and not math.isfinite(exposure_bias_ev)
        ):
            raise ValueError("semantic reaction exposure_bias_ev must be finite")
        if (
            isinstance(exposure_bias_ev, int | float)
            and not isinstance(exposure_bias_ev, bool)
            and isinstance(mask_asset_id, str)
            and isinstance(region_id, str)
        ):
            reaction_regions.append(
                {
                    "id": region_id,
                    "mask_asset_id": mask_asset_id,
                    "exposure_bias_ev": exposure_bias_ev,
                }
            )
    return reaction_regions


def _read_mask(path: Path) -> np.ndarray:
    if path.suffix.lower() in {".tif", ".tiff"}:
        mask = tifffile.imread(path)
    else:
        mask = np.asarray(Image.open(path))
    if mask.ndim == 3:
        mask = mask[..., 0]
    return mask
