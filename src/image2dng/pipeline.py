from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import tifffile
from PIL import Image

from image2dng.api import ConversionResult, convert
from image2dng.validate import find_raw_image_page, validate_dng

SceneStyle = Literal["chart-ramp", "portrait-light-study", "material-still-life"]


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
    nodes: list[PipelineNodeRecord]
    outputs: dict[str, str]
    validations: dict[str, dict[str, Any]]
    raw_data_unique_ids: dict[str, str | None]


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


def _run_scene_graph(
    *,
    scene: GenerationScene,
    inputs_dir: Path,
    raw_dir: Path,
    jpeg_dir: Path,
    validation_dir: Path,
    overwrite: bool,
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

    linear_dng = raw_dir / f"{scene.slug}-linearraw.dng"
    linear_result = _convert_node(
        input_path=tiff_path,
        output_path=linear_dng,
        mode="linearraw",
        prompt_hash=prompt_hash,
        scene=scene,
        overwrite=overwrite,
    )
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{scene.slug}:linearraw-capture",
            node_type="VirtualCameraLinearRawNode",
            inputs={"scene_linear_tiff": str(tiff_path)},
            outputs={"linearraw_dng": str(linear_dng)},
            parameters={"mode": "linearraw", "input_space": "linear-rec709"},
        )
    )

    cfa_dng = raw_dir / f"{scene.slug}-cfa-rggb.dng"
    cfa_result = _convert_node(
        input_path=tiff_path,
        output_path=cfa_dng,
        mode="cfa",
        prompt_hash=prompt_hash,
        scene=scene,
        overwrite=overwrite,
    )
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{scene.slug}:cfa-capture",
            node_type="VirtualCameraCfaNode",
            inputs={"scene_linear_tiff": str(tiff_path)},
            outputs={"cfa_dng": str(cfa_dng)},
            parameters={"mode": "cfa", "cfa_pattern": "rggb", "input_space": "linear-rec709"},
        )
    )

    linear_jpeg = jpeg_dir / f"{scene.slug}-linearraw.jpg"
    cfa_jpeg = jpeg_dir / f"{scene.slug}-cfa-rggb.jpg"
    write_jpeg_preview(linear_jpeg, dng_preview(linear_dng))
    write_jpeg_preview(cfa_jpeg, dng_preview(cfa_dng, cfa_pattern="rggb"))
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{scene.slug}:jpeg-preview",
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
        report_path = validation_dir / f"{scene.slug}-{name}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    nodes.append(
        PipelineNodeRecord(
            node_id=f"{scene.slug}:validation",
            node_type="DngValidationNode",
            inputs={"linearraw_dng": str(linear_dng), "cfa_dng": str(cfa_dng)},
            outputs={
                "linearraw_validation": str(validation_dir / f"{scene.slug}-linearraw.json"),
                "cfa_validation": str(validation_dir / f"{scene.slug}-cfa.json"),
            },
            parameters={"smoke_tests": False},
        )
    )

    return PipelineSceneResult(
        slug=scene.slug,
        prompt_hash=prompt_hash,
        nodes=nodes,
        outputs={
            "scene_linear_tiff": str(tiff_path),
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
    )


def _convert_node(
    *,
    input_path: Path,
    output_path: Path,
    mode: Literal["linearraw", "cfa"],
    prompt_hash: str,
    scene: GenerationScene,
    overwrite: bool,
) -> ConversionResult:
    return convert(
        input_path=input_path,
        output_path=output_path,
        input_space="linear-rec709",
        mode=mode,
        cfa_pattern="rggb",
        iso=100,
        white_balance_kelvin=6500.0,
        shot_noise=0.004 if mode == "cfa" else 0.0,
        read_noise=0.001 if mode == "cfa" else 0.0,
        row_noise=0.0005 if mode == "cfa" else 0.0,
        sensor_effect_seed=scene.seed if mode == "cfa" else None,
        prompt_hash=prompt_hash,
        scene_description=scene.description,
        model_name="image2dng raw-native procedural pipeline",
        model_version="0.1.0",
        lighting=scene.lighting,
        weather=scene.weather,
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
    return {
        "schema": "image2dng.raw_native_node_batch.v1",
        "decision": {
            "core_pipeline": "built-in image2dng Python graph",
            "comfyui_role": "optional visual orchestration layer after core semantics stabilize",
        },
        "output_dir": str(root),
        "graph": {
            "nodes": [
                "PromptIntentNode",
                "SceneLinearGeneratorNode",
                "VirtualCameraLinearRawNode",
                "VirtualCameraCfaNode",
                "JpegPreviewRenderNode",
                "DngValidationNode",
            ],
            "primary_artifact": "synthetic DNG",
            "preview_artifact": "sidecar JPEG rendered from generated RAW buffers",
            "dng_layout": "preview-subifd",
            "embedded_preview": "IFD0 JPEG preview",
            "raw_ifd_location": "Raw SubIFD referenced from IFD0",
        },
        "scenes": [_scene_result_to_dict(scene) for scene in scenes],
    }


def _sample_index(root: Path, scenes: list[PipelineSceneResult]) -> dict[str, Any]:
    return {
        "schema": "image2dng.raw_native_sample_index.v1",
        "output_dir": str(root),
        "scene_count": len(scenes),
        "all_validations_ok": all(
            report["ok"] for scene in scenes for report in scene.validations.values()
        ),
        "samples": [
            {
                "slug": scene.slug,
                "prompt_hash": scene.prompt_hash,
                "artifacts": scene.outputs,
                "validation_ok": {
                    name: report["ok"] for name, report in scene.validations.items()
                },
                "raw_data_unique_ids": scene.raw_data_unique_ids,
            }
            for scene in scenes
        ],
    }


def _scene_result_to_dict(scene: PipelineSceneResult) -> dict[str, Any]:
    return {
        "slug": scene.slug,
        "prompt_hash": scene.prompt_hash,
        "outputs": scene.outputs,
        "raw_data_unique_ids": scene.raw_data_unique_ids,
        "validations": {
            key: {
                "ok": report["ok"],
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
