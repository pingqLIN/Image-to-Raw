from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import Image

from image2dng.dng_writer import DngLayout
from image2dng.image_processing import InputSpace, ensure_rgb
from image2dng.pipeline import (
    ExternalSceneLinearInput,
    PipelineBatchResult,
    run_external_scene_linear_batch,
)

COMFYUI_EXTERNAL_SCENE_SCHEMA = "image2dng.comfyui_external_scene_sources.v1"
EXTERNAL_SCENE_SCHEMA = "image2dng.external_scene_linear_sources.v1"
SUPPORTED_IMPORT_INPUT_SPACES = {"srgb", "linear-rec709", "acescg", "xyz", "prophoto-rgb"}
SUPPORTED_INPUT_SPACES = {"srgb", "linear-rec709", "acescg", "xyz", "prophoto-rgb"}


@dataclass(frozen=True)
class ComfyUIMetadataSummary:
    prompt: str
    negative_prompt: str
    checkpoint: str
    seed: int | None
    width: int | None
    height: int | None
    steps: int | None
    cfg: float | None
    sampler: str
    scheduler: str
    denoise: float | None
    has_prompt_metadata: bool
    has_workflow_metadata: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "checkpoint": self.checkpoint,
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "cfg": self.cfg,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
            "denoise": self.denoise,
            "has_prompt_metadata": self.has_prompt_metadata,
            "has_workflow_metadata": self.has_workflow_metadata,
        }


@dataclass(frozen=True)
class ComfyUIImportScene:
    slug: str
    source_path: Path
    prepared_path: Path
    metadata_path: Path
    input_space: InputSpace
    metadata: ComfyUIMetadataSummary

    @property
    def prompt(self) -> str:
        return self.metadata.prompt

    @property
    def description(self) -> str:
        if self.metadata.checkpoint:
            return (
                f"ComfyUI output imported from {self.source_path.name}; "
                f"checkpoint={self.metadata.checkpoint}"
            )
        return f"ComfyUI output imported from {self.source_path.name}"


@dataclass(frozen=True)
class ComfyUIImportResult:
    output_dir: Path
    manifest_path: Path
    scenes: list[ComfyUIImportScene]
    pipeline_result: PipelineBatchResult | None = None


def import_comfyui_outputs(
    inputs: list[str | Path],
    output_dir: str | Path,
    *,
    input_space: InputSpace = "srgb",
    run_pipeline: bool = False,
    overwrite: bool = True,
    dng_layout: DngLayout = "preview-subifd",
) -> ComfyUIImportResult:
    if not inputs:
        raise ValueError("at least one ComfyUI output image is required")
    if input_space not in SUPPORTED_IMPORT_INPUT_SPACES:
        raise ValueError(f"unsupported input_space: {input_space}")
    if input_space not in SUPPORTED_INPUT_SPACES:
        supported = ", ".join(sorted(SUPPORTED_INPUT_SPACES))
        raise ValueError(f"unsupported input_space: {input_space!r}; supported: {supported}")
    root = Path(output_dir)
    inputs_dir = root / "inputs"
    metadata_dir = root / "metadata"
    manifests_dir = root / "manifests"
    for directory in (inputs_dir, metadata_dir, manifests_dir):
        directory.mkdir(parents=True, exist_ok=True)

    scenes: list[ComfyUIImportScene] = []
    used_slugs: set[str] = set()
    for index, source in enumerate(inputs, start=1):
        source_path = Path(source)
        slug = _unique_slug(
            _safe_slug(source_path.stem, fallback=f"comfyui-output-{index:03d}"),
            used_slugs,
        )
        scenes.append(
            _import_one_image(
                source=source_path,
                slug=slug,
                inputs_dir=inputs_dir,
                metadata_dir=metadata_dir,
                input_space=input_space,
                overwrite=overwrite,
            )
        )
    manifest_path = manifests_dir / "comfyui-external-scenes.json"
    manifest = _external_manifest(root, manifest_path.parent, scenes)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    pipeline_result = None
    if run_pipeline:
        pipeline_result = run_external_scene_linear_batch(
            root / "raw-native-node-batch",
            scenes=[
                ExternalSceneLinearInput(
                    slug=scene.slug,
                    path=scene.prepared_path,
                    input_space=scene.input_space,
                    prompt=scene.prompt,
                    description=scene.description,
                    producer="ComfyUI",
                    producer_metadata=scene.metadata.to_dict(),
                    producer_metadata_manifest=scene.metadata_path,
                )
                for scene in scenes
            ],
            overwrite=overwrite,
            dng_layout=dng_layout,
        )
    return ComfyUIImportResult(
        output_dir=root,
        manifest_path=manifest_path,
        scenes=scenes,
        pipeline_result=pipeline_result,
    )


def extract_comfyui_metadata(path: str | Path) -> ComfyUIMetadataSummary:
    with Image.open(path) as image:
        info = dict(image.info)
    prompt_payload = _json_metadata(info.get("prompt"))
    workflow_payload = _json_metadata(info.get("workflow"))
    return _metadata_from_prompt_graph(
        prompt_payload,
        has_workflow_metadata=workflow_payload is not None,
    )


def _import_one_image(
    *,
    source: Path,
    slug: str,
    inputs_dir: Path,
    metadata_dir: Path,
    input_space: InputSpace,
    overwrite: bool,
) -> ComfyUIImportScene:
    if not source.exists():
        raise FileNotFoundError(source)
    prepared_path = inputs_dir / f"{slug}-scene-linear.tif"
    metadata_path = metadata_dir / f"{slug}-comfyui-metadata.json"
    if not overwrite and (prepared_path.exists() or metadata_path.exists()):
        raise FileExistsError(f"ComfyUI import output already exists for slug: {slug}")

    image = _load_as_uint16_rgb(source)
    tifffile.imwrite(prepared_path, image, photometric="rgb")
    metadata = extract_comfyui_metadata(source)
    metadata_path.write_text(
        json.dumps(
            {
                "schema": COMFYUI_EXTERNAL_SCENE_SCHEMA,
                "source_path": str(source),
                "prepared_path": str(prepared_path),
                "input_space": input_space,
                "metadata": metadata.to_dict(),
                "notes": [
                    "ComfyUI PNG/JPEG outputs are treated as display-referred inputs by default.",
                    "The prepared TIFF is a 16-bit RGB handoff artifact for image2dng conversion.",
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return ComfyUIImportScene(
        slug=slug,
        source_path=source,
        prepared_path=prepared_path,
        metadata_path=metadata_path,
        input_space=input_space,
        metadata=metadata,
    )


def _load_as_uint16_rgb(path: Path) -> np.ndarray:
    if path.suffix.lower() in {".tif", ".tiff"}:
        return _to_uint16_rgb(ensure_rgb(tifffile.imread(path)))
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        array = np.asarray(rgb)
    return _to_uint16_rgb(array)


def _to_uint16_rgb(array: np.ndarray) -> np.ndarray:
    if np.issubdtype(array.dtype, np.integer) and array.dtype.itemsize >= 2:
        return array.astype(np.uint16)
    if np.issubdtype(array.dtype, np.integer):
        max_value = np.iinfo(array.dtype).max
        scaled = np.rint(array.astype(np.float64) / max_value * 65535.0)
        return np.clip(scaled, 0, 65535).astype(np.uint16)
    if np.issubdtype(array.dtype, np.floating):
        return np.rint(np.clip(array, 0.0, 1.0) * 65535.0).astype(np.uint16)
    raise ValueError(f"unsupported image dtype for ComfyUI import: {array.dtype}")


def _external_manifest(
    root: Path,
    manifest_dir: Path,
    scenes: list[ComfyUIImportScene],
) -> dict[str, Any]:
    return {
        "schema": EXTERNAL_SCENE_SCHEMA,
        "source": {
            "producer": "ComfyUI",
            "importer_schema": COMFYUI_EXTERNAL_SCENE_SCHEMA,
            "output_dir": str(root),
        },
        "scenes": [
            {
                "slug": scene.slug,
                "path": _relative_path(scene.prepared_path, manifest_dir),
                "input_space": scene.input_space,
                "producer": "ComfyUI",
                "prompt": scene.metadata.prompt,
                "description": scene.description,
                "lighting": "",
                "weather": "",
                "producer_metadata_manifest": _relative_path(scene.metadata_path, manifest_dir),
                "producer_metadata": scene.metadata.to_dict(),
                "comfyui_metadata": _relative_path(scene.metadata_path, manifest_dir),
                "comfyui": scene.metadata.to_dict(),
            }
            for scene in scenes
        ],
    }


def _metadata_from_prompt_graph(
    payload: Any,
    *,
    has_workflow_metadata: bool,
) -> ComfyUIMetadataSummary:
    if not isinstance(payload, dict):
        return ComfyUIMetadataSummary(
            prompt="",
            negative_prompt="",
            checkpoint="",
            seed=None,
            width=None,
            height=None,
            steps=None,
            cfg=None,
            sampler="",
            scheduler="",
            denoise=None,
            has_prompt_metadata=False,
            has_workflow_metadata=has_workflow_metadata,
        )

    sampler_node = _first_node(payload, "KSampler")
    positive_prompt = ""
    negative_prompt = ""
    if sampler_node is not None:
        inputs = _node_inputs(sampler_node)
        positive_prompt = _clip_text_from_ref(payload, inputs.get("positive"))
        negative_prompt = _clip_text_from_ref(payload, inputs.get("negative"))

    latent_node = _first_node(payload, "EmptyLatentImage")
    checkpoint_node = _first_node(payload, "CheckpointLoaderSimple")
    sampler_inputs = _node_inputs(sampler_node)
    latent_inputs = _node_inputs(latent_node)
    checkpoint_inputs = _node_inputs(checkpoint_node)
    return ComfyUIMetadataSummary(
        prompt=positive_prompt,
        negative_prompt=negative_prompt,
        checkpoint=str(checkpoint_inputs.get("ckpt_name", "")),
        seed=_optional_int(sampler_inputs.get("seed")),
        width=_optional_int(latent_inputs.get("width")),
        height=_optional_int(latent_inputs.get("height")),
        steps=_optional_int(sampler_inputs.get("steps")),
        cfg=_optional_float(sampler_inputs.get("cfg")),
        sampler=str(sampler_inputs.get("sampler_name", "")),
        scheduler=str(sampler_inputs.get("scheduler", "")),
        denoise=_optional_float(sampler_inputs.get("denoise")),
        has_prompt_metadata=True,
        has_workflow_metadata=has_workflow_metadata,
    )


def _json_metadata(value: object) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _first_node(payload: dict[str, Any], class_type: str) -> dict[str, Any] | None:
    for value in payload.values():
        if isinstance(value, dict) and value.get("class_type") == class_type:
            return value
    return None


def _node_inputs(node: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _clip_text_from_ref(payload: dict[str, Any], ref: object) -> str:
    node_id = _node_id_from_ref(ref)
    if node_id is None:
        return ""
    node = payload.get(node_id)
    if not isinstance(node, dict) or node.get("class_type") != "CLIPTextEncode":
        return ""
    text = _node_inputs(node).get("text", "")
    return text if isinstance(text, str) else ""


def _node_id_from_ref(value: object) -> str | None:
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        return value[0]
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-").lower()
    return slug or fallback


def _unique_slug(slug: str, used: set[str]) -> str:
    if slug not in used:
        used.add(slug)
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in used:
        suffix += 1
    unique = f"{slug}-{suffix}"
    used.add(unique)
    return unique


def _relative_path(path: Path, base: Path) -> str:
    return os.path.relpath(path, base).replace("\\", "/")
