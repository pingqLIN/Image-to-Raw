from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import tifffile
from PIL import Image

from image2dng.api import ConversionResult, convert
from image2dng.dng_writer import DngLayout
from image2dng.image_processing import InputSpace
from image2dng.models import CFA_PATTERN_VALUES
from image2dng.semantic_reaction import (
    SUPPORTED_REACTION_INPUT_SPACES,
    SemanticReactionResult,
    apply_highlight_clipping_policy_reaction,
    apply_region_exposure_reaction,
    apply_target_middle_gray_reaction,
    apply_target_white_balance_reaction,
    load_semantic_payload,
    read_scene_linear_image,
)
from image2dng.semantic_scene import (
    SEMANTIC_SCENE_SCHEMA,
    SemanticSceneValidationResult,
    validate_semantic_scene,
)
from image2dng.validate import find_raw_image_page, validate_dng

SceneStyle = Literal["chart-ramp", "portrait-light-study", "material-still-life"]

SEMANTIC_BOUNDARY = (
    "semantic sidecar is preserved by default; opt-in implemented semantic reactions "
    "can affect raw values"
)


@dataclass(frozen=True)
class GenerationScene:
    slug: str
    prompt: str
    description: str
    lighting: str
    style: SceneStyle
    seed: int
    width: int = 256
    height: int = 256
    weather: str = "indoor synthetic"


@dataclass(frozen=True)
class ExternalSceneLinearInput:
    slug: str
    path: Path
    input_space: InputSpace = "linear-rec709"
    prompt: str = ""
    description: str = ""
    lighting: str = ""
    weather: str = ""
    producer: str = "external-scene-linear"
    semantic_manifest: Path | None = None
    apply_semantic_reaction: bool = False
    producer_metadata: dict[str, Any] | None = None
    producer_metadata_manifest: Path | None = None


@dataclass(frozen=True)
class PipelineNodeRecord:
    node_id: str
    node_type: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    parameters: dict[str, Any]


@dataclass(frozen=True)
class PipelineSceneResult:
    slug: str
    prompt_hash: str
    source_type: str
    nodes: list[PipelineNodeRecord]
    outputs: dict[str, str]
    validations: dict[str, dict[str, Any]]
    raw_data_unique_ids: dict[str, str | None]
    producer: str = ""
    input_space: str = "linear-rec709"
    semantic_artifacts: dict[str, str] | None = None
    semantic_validation: dict[str, Any] | None = None
    semantic_contract: str | None = None
    semantic_to_raw_status: str | None = None
    semantic_reaction: dict[str, Any] | None = None
    semantic_reactions: list[dict[str, Any]] | None = None
    producer_metadata: dict[str, Any] | None = None
    producer_metadata_artifacts: dict[str, str] | None = None


@dataclass(frozen=True)
class PipelineBatchResult:
    output_dir: Path
    manifest_path: Path
    sample_index_path: Path
    scenes: list[PipelineSceneResult]


def default_scenes() -> list[GenerationScene]:
    return [
        GenerationScene(
            slug="chart-ramp",
            prompt="controlled color chart with ramps, clipping patches, and neutral scales",
            description="node-generated chart scene for RAW pipeline calibration",
            lighting="even D65 studio light",
            style="chart-ramp",
            seed=2026051001,
        ),
        GenerationScene(
            slug="portrait-light-study",
            prompt="synthetic portrait lighting study with skin-tone panels and soft background",
            description="node-generated portrait study for tone and white-balance checks",
            lighting="large softbox from camera left",
            style="portrait-light-study",
            seed=2026051002,
        ),
        GenerationScene(
            slug="material-still-life",
            prompt="synthetic still life with glass, metal, fabric, fruit, and paper texture",
            description="node-generated material scene for highlight and texture checks",
            lighting="window light with small specular accents",
            style="material-still-life",
            seed=2026051003,
        ),
    ]


def run_raw_native_batch(
    output_dir: str | Path,
    *,
    scenes: list[GenerationScene] | None = None,
    overwrite: bool = True,
    dng_layout: DngLayout = "preview-subifd",
) -> PipelineBatchResult:
    root = Path(output_dir)
    inputs_dir = root / "inputs"
    raw_dir = root / "raw"
    jpeg_dir = root / "jpeg"
    validation_dir = root / "validation"
    manifest_dir = root / "manifests"
    for directory in (inputs_dir, raw_dir, jpeg_dir, validation_dir, manifest_dir):
        directory.mkdir(parents=True, exist_ok=True)

    scene_results = [
        _run_scene_graph(
            scene=scene,
            inputs_dir=inputs_dir,
            raw_dir=raw_dir,
            jpeg_dir=jpeg_dir,
            validation_dir=validation_dir,
            overwrite=overwrite,
            dng_layout=dng_layout,
        )
        for scene in (scenes or default_scenes())
    ]

    manifest_path = manifest_dir / "raw-native-node-batch.json"
    manifest = _batch_manifest(root, scene_results)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    sample_index_path = manifest_dir / "sample-index.json"
    sample_index = _sample_index(root, scene_results)
    sample_index_path.write_text(json.dumps(sample_index, indent=2), encoding="utf-8")
    return PipelineBatchResult(
        output_dir=root,
        manifest_path=manifest_path,
        sample_index_path=sample_index_path,
        scenes=scene_results,
    )


def run_external_scene_linear_batch(
    output_dir: str | Path,
    *,
    scenes: list[ExternalSceneLinearInput],
    overwrite: bool = True,
    dng_layout: DngLayout = "preview-subifd",
) -> PipelineBatchResult:
    if not scenes:
        raise ValueError("at least one external scene-linear input is required")

    root = Path(output_dir)
    inputs_dir = root / "inputs"
    raw_dir = root / "raw"
    jpeg_dir = root / "jpeg"
    validation_dir = root / "validation"
    manifest_dir = root / "manifests"
    for directory in (inputs_dir, raw_dir, jpeg_dir, validation_dir, manifest_dir):
        directory.mkdir(parents=True, exist_ok=True)

    scene_results = [
        _run_external_scene_graph(
            scene=scene,
            inputs_dir=inputs_dir,
            raw_dir=raw_dir,
            jpeg_dir=jpeg_dir,
            validation_dir=validation_dir,
            overwrite=overwrite,
            dng_layout=dng_layout,
        )
        for scene in scenes
    ]

    manifest_path = manifest_dir / "raw-native-node-batch.json"
    manifest = _batch_manifest(root, scene_results)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    sample_index_path = manifest_dir / "sample-index.json"
    sample_index = _sample_index(root, scene_results)
    sample_index_path.write_text(json.dumps(sample_index, indent=2), encoding="utf-8")
    return PipelineBatchResult(
        output_dir=root,
        manifest_path=manifest_path,
        sample_index_path=sample_index_path,
        scenes=scene_results,
    )


def load_external_scene_manifest(path: str | Path) -> list[ExternalSceneLinearInput]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("external scene manifest must be an object")
    if payload.get("schema") != "image2dng.external_scene_linear_sources.v1":
        raise ValueError("unexpected external scene manifest schema")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("external scene manifest must contain at least one scene")
    base = source.parent
    return [_external_scene_from_manifest_item(item, base) for item in scenes]


def _run_scene_graph(
    *,
    scene: GenerationScene,
    inputs_dir: Path,
    raw_dir: Path,
    jpeg_dir: Path,
    validation_dir: Path,
    overwrite: bool,
    dng_layout: DngLayout,
) -> PipelineSceneResult:
    nodes: list[PipelineNodeRecord] = []
    prompt_hash = _prompt_hash(scene)

    nodes.append(
        PipelineNodeRecord(
            node_id=f"{scene.slug}:prompt-intent",
            node_type="PromptIntentNode",
            inputs={},
            outputs={"prompt_hash": prompt_hash},
            parameters={
                "prompt": scene.prompt,
                "scene_description": scene.description,
                "lighting": scene.lighting,
                "weather": scene.weather,
            },
        )
    )

    scene_linear = generate_scene_linear(scene)
    tiff_path = inputs_dir / f"{scene.slug}-scene-linear.tif"
    tifffile.imwrite(tiff_path, scene_linear, photometric="rgb")
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{scene.slug}:scene-linear",
            node_type="SceneLinearGeneratorNode",
            inputs={"prompt_hash": prompt_hash},
            outputs={"scene_linear_tiff": str(tiff_path)},
            parameters={
                "style": scene.style,
                "seed": scene.seed,
                "width": scene.width,
                "height": scene.height,
                "input_space": "linear-rec709",
            },
        )
    )

    return _run_capture_graph(
        slug=scene.slug,
        prompt_hash=prompt_hash,
        scene_linear_path=tiff_path,
        input_space="linear-rec709",
        description=scene.description,
        lighting=scene.lighting,
        weather=scene.weather,
        producer="image2dng procedural scene generator",
        source_type="procedural",
        seed=scene.seed,
        raw_dir=raw_dir,
        jpeg_dir=jpeg_dir,
        validation_dir=validation_dir,
        overwrite=overwrite,
        nodes=nodes,
        semantic_artifacts=None,
        dng_layout=dng_layout,
    )


def _run_external_scene_graph(
    *,
    scene: ExternalSceneLinearInput,
    inputs_dir: Path,
    raw_dir: Path,
    jpeg_dir: Path,
    validation_dir: Path,
    overwrite: bool,
    dng_layout: DngLayout,
) -> PipelineSceneResult:
    nodes: list[PipelineNodeRecord] = []
    source_path = scene.path
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    _validate_semantic_reaction_request(scene)

    target_input = inputs_dir / f"{scene.slug}-scene-linear{source_path.suffix.lower()}"
    shutil.copy2(source_path, target_input)
    semantic_artifacts, semantic_validation = _copy_semantic_manifest(scene, inputs_dir)
    producer_metadata_artifacts = _copy_producer_metadata_manifest(scene, inputs_dir)
    prompt_hash = _external_prompt_hash(
        scene,
        copied_source=target_input,
        semantic_artifacts=semantic_artifacts,
        producer_metadata_artifacts=producer_metadata_artifacts,
    )
    capture_input = target_input
    semantic_reactions: list[SemanticReactionResult] = []
    if scene.apply_semantic_reaction:
        capture_input, semantic_reactions = _apply_semantic_reactions(
            scene=scene,
            scene_linear_path=target_input,
            semantic_artifacts=semantic_artifacts,
            inputs_dir=inputs_dir,
        )
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{scene.slug}:external-scene-linear",
            node_type="ExternalSceneLinearInputNode",
            inputs={"source": str(source_path)},
            outputs={
                "scene_linear_input": str(target_input),
                "capture_scene_linear_input": str(capture_input),
                **semantic_artifacts,
            },
            parameters={
                "input_space": scene.input_space,
                "producer": scene.producer,
                "prompt": scene.prompt,
                "scene_description": scene.description,
                "lighting": scene.lighting,
                "weather": scene.weather,
                "apply_semantic_reaction": scene.apply_semantic_reaction,
                "semantic_boundary": _external_semantic_boundary(scene),
                "producer_metadata": scene.producer_metadata or {},
            },
        )
    )

    return _run_capture_graph(
        slug=scene.slug,
        prompt_hash=prompt_hash,
        scene_linear_path=capture_input,
        original_scene_linear_path=target_input,
        input_space=scene.input_space,
        description=scene.description,
        lighting=scene.lighting,
        weather=scene.weather,
        producer=scene.producer,
        source_type="external-scene-linear",
        seed=None,
        raw_dir=raw_dir,
        jpeg_dir=jpeg_dir,
        validation_dir=validation_dir,
        overwrite=overwrite,
        nodes=nodes,
        semantic_artifacts=semantic_artifacts or None,
        semantic_validation=semantic_validation,
        semantic_reactions=semantic_reactions,
        producer_metadata=scene.producer_metadata,
        producer_metadata_artifacts=producer_metadata_artifacts or None,
        dng_layout=dng_layout,
    )


def _run_capture_graph(
    *,
    slug: str,
    prompt_hash: str,
    scene_linear_path: Path,
    original_scene_linear_path: Path | None = None,
    input_space: InputSpace,
    description: str,
    lighting: str,
    weather: str,
    producer: str,
    source_type: str,
    seed: int | None,
    raw_dir: Path,
    jpeg_dir: Path,
    validation_dir: Path,
    overwrite: bool,
    nodes: list[PipelineNodeRecord],
    semantic_artifacts: dict[str, str] | None,
    semantic_validation: dict[str, Any] | None = None,
    semantic_reactions: list[SemanticReactionResult] | None = None,
    producer_metadata: dict[str, Any] | None = None,
    producer_metadata_artifacts: dict[str, str] | None = None,
    dng_layout: DngLayout = "preview-subifd",
) -> PipelineSceneResult:
    linear_dng = raw_dir / f"{slug}-linearraw.dng"
    linear_result = _convert_node(
        input_path=scene_linear_path,
        output_path=linear_dng,
        mode="linearraw",
        prompt_hash=prompt_hash,
        input_space=input_space,
        description=description,
        lighting=lighting,
        weather=weather,
        producer=producer,
        seed=seed,
        overwrite=overwrite,
        dng_layout=dng_layout,
    )
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{slug}:linearraw-capture",
            node_type="VirtualCameraLinearRawNode",
            inputs={"scene_linear_input": str(scene_linear_path)},
            outputs={"linearraw_dng": str(linear_dng)},
            parameters={
                "mode": "linearraw",
                "input_space": input_space,
                "dng_layout": dng_layout,
            },
        )
    )

    cfa_dng = raw_dir / f"{slug}-cfa-rggb.dng"
    cfa_result = _convert_node(
        input_path=scene_linear_path,
        output_path=cfa_dng,
        mode="cfa",
        prompt_hash=prompt_hash,
        input_space=input_space,
        description=description,
        lighting=lighting,
        weather=weather,
        producer=producer,
        seed=seed,
        overwrite=overwrite,
        dng_layout=dng_layout,
    )
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{slug}:cfa-capture",
            node_type="VirtualCameraCfaNode",
            inputs={"scene_linear_input": str(scene_linear_path)},
            outputs={"cfa_dng": str(cfa_dng)},
            parameters={
                "mode": "cfa",
                "cfa_pattern": "rggb",
                "input_space": input_space,
                "dng_layout": dng_layout,
            },
        )
    )

    linear_jpeg = jpeg_dir / f"{slug}-linearraw.jpg"
    cfa_jpeg = jpeg_dir / f"{slug}-cfa-rggb.jpg"
    write_jpeg_preview(linear_jpeg, dng_preview(linear_dng))
    write_jpeg_preview(cfa_jpeg, dng_preview(cfa_dng, cfa_pattern="rggb"))
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{slug}:jpeg-preview",
            node_type="JpegPreviewRenderNode",
            inputs={"linearraw_dng": str(linear_dng), "cfa_dng": str(cfa_dng)},
            outputs={"linearraw_jpeg": str(linear_jpeg), "cfa_jpeg": str(cfa_jpeg)},
            parameters={"tone_map": "percentile-0.5-99.5", "jpeg_quality": 92},
        )
    )

    validations = {
        "linearraw": validate_dng(linear_dng, run_smoke=False).to_dict(),
        "cfa": validate_dng(cfa_dng, run_smoke=False).to_dict(),
    }
    for name, report in validations.items():
        report_path = validation_dir / f"{slug}-{name}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{slug}:validation",
            node_type="DngValidationNode",
            inputs={"linearraw_dng": str(linear_dng), "cfa_dng": str(cfa_dng)},
            outputs={
                "linearraw_validation": str(validation_dir / f"{slug}-linearraw.json"),
                "cfa_validation": str(validation_dir / f"{slug}-cfa.json"),
            },
            parameters={"smoke_tests": False},
        )
    )

    return PipelineSceneResult(
        slug=slug,
        prompt_hash=prompt_hash,
        source_type=source_type,
        nodes=nodes,
        outputs={
            "scene_linear_tiff": str(scene_linear_path),
            "scene_linear_input": str(scene_linear_path),
            **(
                {"original_scene_linear_input": str(original_scene_linear_path)}
                if original_scene_linear_path is not None
                and original_scene_linear_path != scene_linear_path
                else {}
            ),
            "linearraw_dng": str(linear_dng),
            "cfa_dng": str(cfa_dng),
            "linearraw_jpeg": str(linear_jpeg),
            "cfa_jpeg": str(cfa_jpeg),
        },
        validations=validations,
        raw_data_unique_ids={
            "linearraw": linear_result.raw_data_unique_id,
            "cfa": cfa_result.raw_data_unique_id,
        },
        producer=producer,
        input_space=input_space,
        semantic_artifacts=semantic_artifacts,
        semantic_validation=semantic_validation,
        semantic_contract=(
            SEMANTIC_SCENE_SCHEMA if semantic_validation is not None else None
        ),
        semantic_to_raw_status=_semantic_to_raw_status(semantic_validation, semantic_reactions),
        semantic_reaction=_primary_semantic_reaction_dict(semantic_reactions),
        semantic_reactions=(
            [reaction.to_dict() for reaction in semantic_reactions]
            if semantic_reactions
            else None
        ),
        producer_metadata=producer_metadata,
        producer_metadata_artifacts=producer_metadata_artifacts,
    )


def _convert_node(
    *,
    input_path: Path,
    output_path: Path,
    mode: Literal["linearraw", "cfa"],
    prompt_hash: str,
    input_space: InputSpace,
    description: str,
    lighting: str,
    weather: str,
    producer: str,
    seed: int | None,
    overwrite: bool,
    dng_layout: DngLayout,
) -> ConversionResult:
    return convert(
        input_path=input_path,
        output_path=output_path,
        input_space=input_space,
        mode=mode,
        cfa_pattern="rggb",
        iso=100,
        white_balance_kelvin=6500.0,
        shot_noise=0.004 if mode == "cfa" else 0.0,
        read_noise=0.001 if mode == "cfa" else 0.0,
        row_noise=0.0005 if mode == "cfa" else 0.0,
        sensor_effect_seed=seed if mode == "cfa" else None,
        prompt_hash=prompt_hash,
        scene_description=description,
        model_name=producer,
        model_version="0.1.0",
        lighting=lighting,
        weather=weather,
        dng_layout=dng_layout,
        overwrite=overwrite,
    )


def generate_scene_linear(scene: GenerationScene) -> np.ndarray:
    rng = np.random.default_rng(scene.seed)
    if scene.style == "chart-ramp":
        image = _chart_ramp(scene.width, scene.height)
    elif scene.style == "portrait-light-study":
        image = _portrait_light_study(scene.width, scene.height)
    elif scene.style == "material-still-life":
        image = _material_still_life(scene.width, scene.height)
    else:
        raise ValueError(f"unsupported scene style: {scene.style}")

    dither = rng.normal(0.0, 0.0012, size=image.shape)
    return np.rint(np.clip(image + dither, 0.0, 1.0) * 65535.0).astype(np.uint16)


def dng_preview(path: str | Path, *, cfa_pattern: str | None = None) -> np.ndarray:
    with tifffile.TiffFile(path) as tif:
        page = find_raw_image_page(tif)
        if page is None:
            raise ValueError(f"no main raw image IFD found: {path}")
        data = page.asarray()
    if data.ndim == 2:
        return _cfa_false_color(data, cfa_pattern or "rggb")
    return _preview_rgb(data)


def write_jpeg_preview(path: str | Path, image: np.ndarray) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image).save(target, format="JPEG", quality=92, subsampling=1, optimize=True)


def _batch_manifest(root: Path, scenes: list[PipelineSceneResult]) -> dict[str, Any]:
    graph_nodes = [node.node_type for node in scenes[0].nodes] if scenes else []
    return {
        "schema": "image2dng.raw_native_node_batch.v1",
        "decision": {
            "core_pipeline": "built-in image2dng Python graph",
            "external_generator_bridge": (
                "external generators enter through scene-linear manifests; "
                "ComfyUI and Stable Diffusion adapters live outside the core package"
            ),
        },
        "output_dir": str(root),
        "graph": {
            "nodes": graph_nodes,
            "primary_artifact": "synthetic DNG",
            "preview_artifact": "sidecar JPEG rendered from generated RAW buffers",
            "dng_layout": _batch_dng_layout(scenes),
            "embedded_preview": _batch_embedded_preview(scenes),
            "raw_ifd_location": _batch_raw_ifd_location(scenes),
            "dng_payload_boundary": _dng_payload_boundary(),
            "external_scene_linear_boundary": "available",
            "semantic_boundary": SEMANTIC_BOUNDARY,
        },
        "scenes": [_scene_result_to_dict(scene) for scene in scenes],
    }


def _batch_dng_layout(scenes: list[PipelineSceneResult]) -> str:
    if not scenes:
        return "preview-subifd"
    for node in scenes[0].nodes:
        if node.node_type == "VirtualCameraLinearRawNode":
            return str(node.parameters.get("dng_layout") or "preview-subifd")
    return "preview-subifd"


def _batch_embedded_preview(scenes: list[PipelineSceneResult]) -> str:
    if _batch_dng_layout(scenes) == "single-raw-ifd":
        return "absent; sidecar JPEG preview only"
    return "IFD0 JPEG preview"


def _batch_raw_ifd_location(scenes: list[PipelineSceneResult]) -> str:
    if _batch_dng_layout(scenes) == "single-raw-ifd":
        return "IFD0"
    return "Raw SubIFD referenced from IFD0"


def _dng_payload_boundary() -> dict[str, str]:
    return {
        "exif_ifd": "not-written",
        "semantic_mask_ifd": "not-written",
        "depth_ifd": "not-written",
        "dng_private_data": "not-written",
        "semantic_sidecar": "preserved as copied sidecar and manifest artifacts when provided",
    }


def _sample_index(root: Path, scenes: list[PipelineSceneResult]) -> dict[str, Any]:
    return {
        "schema": "image2dng.raw_native_sample_index.v1",
        "output_dir": str(root),
        "scene_count": len(scenes),
        "all_validations_ok": all(
            report["ok"] for scene in scenes for report in scene.validations.values()
        ),
        "samples": [_sample_index_scene(scene) for scene in scenes],
    }


def _sample_index_scene(scene: PipelineSceneResult) -> dict[str, Any]:
    sample = {
        "slug": scene.slug,
        "source_type": scene.source_type,
        "producer": scene.producer,
        "input_space": scene.input_space,
        "prompt_hash": scene.prompt_hash,
        "artifacts": scene.outputs,
        "validation_ok": {
            name: report["ok"] for name, report in scene.validations.items()
        },
        "raw_data_unique_ids": scene.raw_data_unique_ids,
    }
    if scene.semantic_validation is not None:
        sample.update(
            {
                "semantic_contract": scene.semantic_contract,
                "semantic_to_raw_status": scene.semantic_to_raw_status,
                "semantic_validation": scene.semantic_validation,
            }
        )
    if scene.semantic_reaction is not None:
        sample["semantic_reaction"] = {
            "model": scene.semantic_reaction["model"],
            "applied": scene.semantic_reaction["applied"],
            "region_count": scene.semantic_reaction["region_count"],
            "affected_pixels": scene.semantic_reaction["affected_pixels"],
            **(
                {"reason": scene.semantic_reaction["reason"]}
                if "reason" in scene.semantic_reaction
                else {}
            ),
        }
    if scene.semantic_reactions is not None:
        sample["semantic_reactions"] = [
            {
                "model": reaction["model"],
                "applied": reaction["applied"],
                "region_count": reaction["region_count"],
                "affected_pixels": reaction["affected_pixels"],
                **({"reason": reaction["reason"]} if "reason" in reaction else {}),
            }
            for reaction in scene.semantic_reactions
        ]
    if scene.producer_metadata is not None:
        sample["producer_metadata"] = scene.producer_metadata
    if scene.producer_metadata_artifacts is not None:
        sample["producer_metadata_artifacts"] = scene.producer_metadata_artifacts
    return sample


def _scene_result_to_dict(scene: PipelineSceneResult) -> dict[str, Any]:
    return {
        "slug": scene.slug,
        "source_type": scene.source_type,
        "producer": scene.producer,
        "input_space": scene.input_space,
        "prompt_hash": scene.prompt_hash,
        "outputs": scene.outputs,
        "semantic_artifacts": scene.semantic_artifacts or {},
        "semantic_validation": scene.semantic_validation or {},
        "semantic_contract": scene.semantic_contract,
        "semantic_to_raw_status": scene.semantic_to_raw_status,
        "semantic_reaction": scene.semantic_reaction or {},
        "semantic_reactions": scene.semantic_reactions or [],
        "producer_metadata": scene.producer_metadata or {},
        "producer_metadata_artifacts": scene.producer_metadata_artifacts or {},
        "raw_data_unique_ids": scene.raw_data_unique_ids,
        "validations": {
            key: {
                "ok": report["ok"],
                "dng_layout": report["dng_layout"],
                "raw_ifd_location": report["raw_ifd_location"],
                "errors": report["errors"],
                "warnings": report["warnings"],
            }
            for key, report in scene.validations.items()
        },
        "nodes": [
            {
                "id": node.node_id,
                "type": node.node_type,
                "inputs": node.inputs,
                "outputs": node.outputs,
                "parameters": node.parameters,
            }
            for node in scene.nodes
        ],
    }


def _external_scene_from_manifest_item(
    item: object,
    base: Path,
) -> ExternalSceneLinearInput:
    if not isinstance(item, dict):
        raise ValueError("external scene manifest entries must be objects")
    slug = _manifest_string(item, "slug")
    source_path = _manifest_path(item, "path", base)
    semantic_manifest = (
        _manifest_path(item, "semantic_manifest", base) if item.get("semantic_manifest") else None
    )
    input_space = item.get("input_space", "linear-rec709")
    if input_space not in {"srgb", "linear-rec709", "acescg", "xyz", "prophoto-rgb"}:
        raise ValueError(f"{slug}: unsupported input_space: {input_space}")
    apply_semantic_reaction = item.get("apply_semantic_reaction", False)
    if not isinstance(apply_semantic_reaction, bool):
        raise ValueError(f"{slug}: apply_semantic_reaction must be boolean when present")
    producer_metadata = item.get("producer_metadata", item.get("comfyui"))
    if producer_metadata is not None and not isinstance(producer_metadata, dict):
        raise ValueError(f"{slug}: producer_metadata must be an object when present")
    producer_metadata_manifest = _optional_manifest_path(
        item,
        ("producer_metadata_manifest", "comfyui_metadata"),
        base,
    )
    return ExternalSceneLinearInput(
        slug=slug,
        path=source_path,
        input_space=input_space,
        prompt=_optional_manifest_string(item, "prompt", "", slug),
        description=_optional_manifest_string(item, "description", "", slug),
        lighting=_optional_manifest_string(item, "lighting", "", slug),
        weather=_optional_manifest_string(item, "weather", "", slug),
        producer=_optional_manifest_string(
            item,
            "producer",
            "external-scene-linear",
            slug,
        ),
        semantic_manifest=semantic_manifest,
        apply_semantic_reaction=apply_semantic_reaction,
        producer_metadata=producer_metadata,
        producer_metadata_manifest=producer_metadata_manifest,
    )


def _semantic_to_raw_status(
    semantic_validation: dict[str, Any] | None,
    semantic_reactions: list[SemanticReactionResult] | None,
) -> str | None:
    if semantic_reactions:
        if any(reaction.applied for reaction in semantic_reactions):
            return "applied"
        return "no-op"
    if semantic_validation is not None:
        return "preserved-not-applied"
    return None


def _primary_semantic_reaction(
    semantic_reactions: list[SemanticReactionResult] | None,
) -> SemanticReactionResult | None:
    if not semantic_reactions:
        return None
    for reaction in semantic_reactions:
        if reaction.applied:
            return reaction
    return semantic_reactions[0]


def _primary_semantic_reaction_dict(
    semantic_reactions: list[SemanticReactionResult] | None,
) -> dict[str, Any] | None:
    reaction = _primary_semantic_reaction(semantic_reactions)
    return reaction.to_dict() if reaction is not None else None


def _external_semantic_boundary(scene: ExternalSceneLinearInput) -> str:
    if scene.apply_semantic_reaction:
        return "semantic sidecar applied through opt-in deterministic semantic reactions"
    if scene.semantic_manifest is not None:
        return "semantic sidecar preserved and validated, but not applied to raw values"
    return "semantic sidecar absent"


def _validate_semantic_reaction_request(scene: ExternalSceneLinearInput) -> None:
    if not scene.apply_semantic_reaction:
        return
    if scene.semantic_manifest is None:
        raise ValueError(f"{scene.slug}: apply_semantic_reaction requires semantic_manifest")
    if scene.input_space not in SUPPORTED_REACTION_INPUT_SPACES:
        supported = ", ".join(sorted(SUPPORTED_REACTION_INPUT_SPACES))
        raise ValueError(
            f"{scene.slug}: semantic reaction requires linear-light input_space; "
            f"got {scene.input_space!r}, supported: {supported}"
        )


def _apply_semantic_reactions(
    *,
    scene: ExternalSceneLinearInput,
    scene_linear_path: Path,
    semantic_artifacts: dict[str, str],
    inputs_dir: Path,
) -> tuple[Path, list[SemanticReactionResult]]:
    semantic_path_value = semantic_artifacts.get("semantic_manifest")
    if semantic_path_value is None:
        raise ValueError(f"{scene.slug}: semantic_manifest artifact missing")
    semantic_path = Path(semantic_path_value)
    payload = load_semantic_payload(semantic_path)
    image = read_scene_linear_image(scene_linear_path)
    reactions: list[SemanticReactionResult] = []
    reacted_image, exposure_reaction = apply_region_exposure_reaction(
        image,
        semantic_payload=payload,
        semantic_base_dir=semantic_path.parent,
        input_space=scene.input_space,
    )
    reactions.append(exposure_reaction)
    reacted_image, white_balance_reaction = apply_target_white_balance_reaction(
        reacted_image,
        semantic_payload=payload,
        input_space=scene.input_space,
    )
    if white_balance_reaction is not None:
        reactions.append(white_balance_reaction)
    reacted_image, middle_gray_reaction = apply_target_middle_gray_reaction(
        reacted_image,
        semantic_payload=payload,
        input_space=scene.input_space,
    )
    if middle_gray_reaction is not None:
        reactions.append(middle_gray_reaction)
    reacted_image, highlight_reaction = apply_highlight_clipping_policy_reaction(
        reacted_image,
        semantic_payload=payload,
        input_space=scene.input_space,
    )
    if highlight_reaction is not None:
        reactions.append(highlight_reaction)
    if not any(reaction.applied for reaction in reactions):
        return scene_linear_path, reactions
    target = inputs_dir / f"{scene.slug}-semantic-reaction.tif"
    tifffile.imwrite(target, reacted_image, photometric="rgb")
    return target, reactions


def _copy_semantic_manifest(
    scene: ExternalSceneLinearInput,
    inputs_dir: Path,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    if scene.semantic_manifest is None:
        return {}, None
    if not scene.semantic_manifest.exists():
        raise FileNotFoundError(scene.semantic_manifest)
    validation = validate_semantic_scene(scene.semantic_manifest)
    if not validation.ok:
        raise ValueError(
            f"{scene.slug}: invalid semantic scene sidecar: {'; '.join(validation.errors)}"
        )
    target = inputs_dir / f"{scene.slug}-semantic{scene.semantic_manifest.suffix.lower()}"
    payload = json.loads(scene.semantic_manifest.read_text(encoding="utf-8"))
    artifacts = {"semantic_manifest": str(target)}
    copied_assets = _copy_semantic_assets(scene.slug, validation, inputs_dir)
    artifacts.update(copied_assets)
    _rewrite_semantic_asset_paths(payload, copied_assets, inputs_dir)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    copied_validation = validate_semantic_scene(target)
    if not copied_validation.ok:
        raise ValueError(
            f"{scene.slug}: copied semantic scene sidecar is invalid: "
            f"{'; '.join(copied_validation.errors)}"
        )
    return artifacts, copied_validation.to_dict()


def _copy_producer_metadata_manifest(
    scene: ExternalSceneLinearInput,
    inputs_dir: Path,
) -> dict[str, str]:
    if scene.producer_metadata_manifest is None:
        return {}
    if not scene.producer_metadata_manifest.exists():
        raise FileNotFoundError(scene.producer_metadata_manifest)
    target = (
        inputs_dir
        / f"{scene.slug}-producer-metadata{scene.producer_metadata_manifest.suffix.lower()}"
    )
    shutil.copy2(scene.producer_metadata_manifest, target)
    return {"producer_metadata_manifest": str(target)}


def _copy_semantic_assets(
    slug: str,
    validation: SemanticSceneValidationResult,
    inputs_dir: Path,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    asset_paths = validation.asset_paths or {}
    if not asset_paths:
        return artifacts
    asset_dir = inputs_dir / f"{slug}-semantic-assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    for asset_id, source in asset_paths.items():
        target = asset_dir / _semantic_asset_filename(asset_id, source)
        shutil.copy2(source, target)
        artifacts[f"asset:{asset_id}"] = str(target)
    return artifacts


def _rewrite_semantic_asset_paths(
    payload: Any,
    copied_assets: dict[str, str],
    inputs_dir: Path,
) -> None:
    if not isinstance(payload, dict):
        return
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("id")
        copied_path = copied_assets.get(f"asset:{asset_id}")
        if copied_path is None:
            continue
        asset["path"] = str(Path(copied_path).relative_to(inputs_dir))


def _semantic_asset_filename(asset_id: str, source: Path) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", asset_id).strip(".-")
    return f"{safe_id or 'asset'}{source.suffix.lower()}"


def _manifest_string(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"external scene manifest entry missing string field: {key}")
    return value


def _optional_manifest_string(
    item: dict[str, object],
    key: str,
    default: str,
    slug: str,
) -> str:
    value = item.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{slug}: {key} must be a string when present")
    return value


def _manifest_path(item: dict[str, object], key: str, base: Path) -> Path:
    path = Path(_manifest_string(item, key))
    return path if path.is_absolute() else base / path


def _optional_manifest_path(
    item: dict[str, object],
    keys: tuple[str, ...],
    base: Path,
) -> Path | None:
    for key in keys:
        if item.get(key):
            return _manifest_path(item, key, base)
    return None


def _external_prompt_hash(
    scene: ExternalSceneLinearInput,
    *,
    copied_source: Path,
    semantic_artifacts: dict[str, str],
    producer_metadata_artifacts: dict[str, str],
) -> str:
    semantic_manifest = semantic_artifacts.get("semantic_manifest")
    producer_metadata_manifest = producer_metadata_artifacts.get("producer_metadata_manifest")
    payload = json.dumps(
        {
            "source_sha256": _sha256_file(copied_source),
            "semantic_sha256": _sha256_file(Path(semantic_manifest)) if semantic_manifest else "",
            "semantic_asset_sha256": _semantic_asset_hashes(semantic_artifacts),
            "producer_metadata_sha256": (
                _sha256_file(Path(producer_metadata_manifest))
                if producer_metadata_manifest
                else ""
            ),
            "producer_metadata": scene.producer_metadata or {},
            "prompt": scene.prompt,
            "description": scene.description,
            "lighting": scene.lighting,
            "weather": scene.weather,
            "producer": scene.producer,
            "input_space": scene.input_space,
            "apply_semantic_reaction": scene.apply_semantic_reaction,
        },
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _semantic_asset_hashes(semantic_artifacts: dict[str, str]) -> dict[str, str]:
    return {
        artifact_key.removeprefix("asset:"): _sha256_file(Path(path))
        for artifact_key, path in sorted(semantic_artifacts.items())
        if artifact_key.startswith("asset:")
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prompt_hash(scene: GenerationScene) -> str:
    payload = json.dumps(
        {
            "prompt": scene.prompt,
            "description": scene.description,
            "lighting": scene.lighting,
            "style": scene.style,
            "seed": scene.seed,
        },
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _chart_ramp(width: int, height: int) -> np.ndarray:
    yy, xx = np.indices((height, width))
    image = np.zeros((height, width, 3), dtype=np.float64)
    image[..., 0] = xx / max(1, width - 1)
    image[..., 1] = yy / max(1, height - 1)
    image[..., 2] = (image[..., 0] + image[..., 1]) * 0.5

    colors = np.array(
        [
            [1.0, 0.04, 0.04],
            [0.04, 0.85, 0.08],
            [0.04, 0.12, 1.0],
            [1.0, 0.9, 0.05],
            [0.04, 0.85, 0.95],
            [0.9, 0.05, 0.95],
            [0.95, 0.95, 0.95],
            [0.02, 0.02, 0.02],
        ]
    )
    patch_h = max(8, height // 6)
    patch_w = max(1, width // len(colors))
    y0 = max(4, height // 5)
    for index, color in enumerate(colors):
        x0 = index * patch_w
        image[y0 : y0 + patch_h, x0 : min(width, x0 + patch_w)] = color

    scale_y = min(height - 1, y0 + patch_h + height // 12)
    for row in range(3):
        for col in range(8):
            level = (row * 8 + col) / 23
            y1 = scale_y + row * max(4, height // 14)
            y2 = min(height, y1 + max(3, height // 18))
            x1 = col * patch_w
            image[y1:y2, x1 : min(width, x1 + patch_w)] = level
    return np.clip(image, 0.0, 1.0)


def _portrait_light_study(width: int, height: int) -> np.ndarray:
    yy, xx = np.indices((height, width))
    image = np.full((height, width, 3), (0.58, 0.62, 0.68), dtype=np.float64)
    image *= (0.8 + 0.35 * (1.0 - xx / max(1, width - 1)))[..., None]

    skin = np.array([0.78, 0.48, 0.34])
    shadow_skin = np.array([0.46, 0.25, 0.18])
    cx, cy = width * 0.5, height * 0.46
    face = ((xx - cx) / (width * 0.18)) ** 2 + ((yy - cy) / (height * 0.25)) ** 2 <= 1
    light = np.clip(1.12 - (xx / max(1, width - 1)) * 0.55, 0.42, 1.08)
    image[face] = skin * light[face, None] + shadow_skin * (1.0 - light[face, None]) * 0.25

    hair = (
        ((xx - cx) / (width * 0.22)) ** 2
        + ((yy - (cy - height * 0.12)) / (height * 0.13)) ** 2
        <= 1
    )
    image[hair] = np.array([0.08, 0.055, 0.04])
    shirt = (np.abs(xx - cx) < width * 0.28) & (yy > cy + height * 0.27)
    image[shirt] = np.array([0.08, 0.18, 0.42])

    for eye_x in (cx - width * 0.06, cx + width * 0.06):
        eye = (xx - eye_x) ** 2 + (yy - (cy - height * 0.03)) ** 2 <= (width * 0.018) ** 2
        image[eye] = 0.03
    mouth = (
        ((xx - cx) / (width * 0.055)) ** 2
        + ((yy - (cy + height * 0.095)) / (height * 0.018)) ** 2
        <= 1
    )
    image[mouth] = np.array([0.45, 0.08, 0.1])

    for index, color in enumerate(
        ([0.86, 0.57, 0.43], [0.68, 0.39, 0.27], [0.39, 0.2, 0.14], [0.22, 0.12, 0.08])
    ):
        x0 = int(index * width / 4)
        x1 = int((index + 1) * width / 4)
        image[: max(8, height // 7), x0:x1] = color
    return np.clip(image, 0.0, 1.0)


def _material_still_life(width: int, height: int) -> np.ndarray:
    yy, xx = np.indices((height, width))
    image = np.zeros((height, width, 3), dtype=np.float64)
    image[..., 0] = 0.12 + 0.22 * xx / max(1, width - 1)
    image[..., 1] = 0.14 + 0.18 * yy / max(1, height - 1)
    image[..., 2] = 0.2 + 0.12 * (1.0 - xx / max(1, width - 1))

    fruit = (xx - width * 0.24) ** 2 + (yy - height * 0.32) ** 2 <= (width * 0.14) ** 2
    orange = (xx - width * 0.52) ** 2 + (yy - height * 0.31) ** 2 <= (width * 0.12) ** 2
    glass = ((xx - width * 0.79) / (width * 0.09)) ** 2 + (
        (yy - height * 0.31) / (height * 0.18)
    ) ** 2 <= 1
    fabric = yy > height * 0.62
    metal = (xx > width * 0.56) & (yy > height * 0.47) & (yy < height * 0.59)
    paper = (xx < width * 0.45) & (yy > height * 0.48) & (yy < height * 0.59)

    image[fruit] = np.array([0.82, 0.08, 0.06])
    image[orange] = np.array([0.95, 0.44, 0.05])
    image[glass] = np.array([0.42, 0.7, 0.86])
    image[glass] *= 0.65 + 0.35 * (xx[glass] / max(1, width - 1))[:, None]
    image[fabric] = np.stack(
        [
            np.full(np.count_nonzero(fabric), 0.07),
            0.13 + ((xx[fabric] + yy[fabric]) % 28) / 120,
            0.22 + ((xx[fabric] * 3) % 36) / 120,
        ],
        axis=1,
    )
    image[metal] = np.stack(
        [
            0.28 + (xx[metal] / max(1, width - 1)) * 0.35,
            0.28 + (xx[metal] / max(1, width - 1)) * 0.35,
            0.3 + (xx[metal] / max(1, width - 1)) * 0.38,
        ],
        axis=1,
    )
    image[paper] = np.where(((xx[paper] // 5) % 2)[:, None] == 0, 0.92, 0.08)
    return np.clip(image, 0.0, 1.0)


def _preview_rgb(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        gray = _tone_map(image)
        return np.stack([gray, gray, gray], axis=2)
    return np.stack([_tone_map(image[..., channel]) for channel in range(3)], axis=2)


def _tone_map(channel: np.ndarray) -> np.ndarray:
    data = channel.astype(np.float64)
    low, high = np.percentile(data, [0.5, 99.5])
    if high <= low:
        high = low + 1.0
    return np.clip((data - low) / (high - low) * 255.0, 0, 255).astype(np.uint8)


def _cfa_false_color(mosaic: np.ndarray, cfa_pattern: str) -> np.ndarray:
    if cfa_pattern not in CFA_PATTERN_VALUES:
        raise ValueError("cfa_pattern must be one of bggr, gbrg, grbg, rggb")
    channels = {
        "rggb": ((0, 1), (1, 2)),
        "bggr": ((2, 1), (1, 0)),
        "grbg": ((1, 0), (2, 1)),
        "gbrg": ((1, 2), (0, 1)),
    }[cfa_pattern]
    scaled = _tone_map(mosaic)
    preview = np.zeros((*mosaic.shape, 3), dtype=np.uint8)
    for y in range(mosaic.shape[0]):
        for x in range(mosaic.shape[1]):
            preview[y, x, channels[y % 2][x % 2]] = scaled[y, x]
    return preview
