from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path
from typing import Literal

import numpy as np

from image2dng.dng_writer import DngLayout, write_dng
from image2dng.image_processing import InputSpace, build_cfa_buffer, build_linearraw_buffer
from image2dng.models import AIMetadataModel, CameraProfileModel, CfaPattern
from image2dng.sensor_effects import SensorEffectModel

OutputMode = Literal["linearraw", "cfa"]


class Image2DNGError(Exception):
    """Base exception for public image2dng API errors."""


class UnsupportedInputError(Image2DNGError):
    """Raised when the input image cannot be loaded or converted."""


class InvalidMetadataError(Image2DNGError):
    """Raised when simulated camera or provenance metadata is invalid."""


class OutputExistsError(Image2DNGError):
    """Raised when the output path exists and overwrite is disabled."""


class ValidationError(Image2DNGError):
    """Raised when generated output fails a post-generation validation step."""


@dataclass(frozen=True)
class ConversionResult:
    output_path: Path
    input_space: str
    mode: str
    width: int
    height: int
    prompt_hash: str | None
    raw_data_unique_id: str | None = None


def convert(
    input_path: str | Path,
    output_path: str | Path,
    *,
    input_space: InputSpace = "srgb",
    mode: OutputMode = "linearraw",
    cfa_pattern: CfaPattern = "rggb",
    iso: int = 100,
    white_balance_kelvin: float = 6500.0,
    color_matrix_1: Sequence[float] | None = None,
    as_shot_neutral: Sequence[float] | None = None,
    shot_noise: float = 0.0,
    read_noise: float = 0.0,
    row_noise: float = 0.0,
    sensor_effect_seed: int | None = None,
    prompt_hash: str | None = None,
    prompt_plaintext: str | None = None,
    scene_description: str = "",
    model_name: str = "",
    model_version: str = "",
    lighting: str = "",
    weather: str = "",
    overwrite: bool = False,
    dng_layout: DngLayout = "preview-subifd",
) -> ConversionResult:
    target = Path(output_path)
    if target.exists() and not overwrite:
        raise OutputExistsError(f"output already exists: {target}")
    if mode not in {"linearraw", "cfa"}:
        raise InvalidMetadataError(f"unsupported output mode: {mode}")
    if dng_layout not in {"single-raw-ifd", "preview-subifd"}:
        raise InvalidMetadataError(f"unsupported DNG layout: {dng_layout}")
    if not isinstance(iso, Integral) or iso <= 0:
        raise InvalidMetadataError("iso must be positive")
    if not math.isfinite(float(white_balance_kelvin)) or white_balance_kelvin <= 0:
        raise InvalidMetadataError("white_balance_kelvin must be positive finite")
    try:
        sensor_effects = SensorEffectModel(
            shot_noise=shot_noise,
            read_noise=read_noise,
            row_noise=row_noise,
            seed=sensor_effect_seed,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidMetadataError(str(exc)) from exc

    try:
        if mode == "cfa":
            raw_buffer, core = build_cfa_buffer(
                input_path,
                input_space,
                cfa_pattern=cfa_pattern,
                sensor_effects=sensor_effects,
            )
        else:
            raw_buffer, core = build_linearraw_buffer(
                input_path,
                input_space,
                sensor_effects=sensor_effects,
            )
    except ValueError as exc:
        raise UnsupportedInputError(str(exc)) from exc

    try:
        camera = CameraProfileModel.from_white_balance(white_balance_kelvin)
        if color_matrix_1 is not None or as_shot_neutral is not None:
            camera = CameraProfileModel(
                color_matrix_1=(
                    tuple(float(value) for value in color_matrix_1)
                    if color_matrix_1 is not None
                    else camera.color_matrix_1
                ),
                as_shot_neutral=(
                    tuple(float(value) for value in as_shot_neutral)
                    if as_shot_neutral is not None
                    else camera.as_shot_neutral
                ),
            )
        ai = AIMetadataModel(
            model_name=model_name,
            model_version=model_version,
            prompt_hash=prompt_hash or "",
            scene_description=scene_description,
            lighting=lighting,
            weather=weather,
            iso=iso,
            white_balance_kelvin=white_balance_kelvin,
            prompt_plaintext=prompt_plaintext,
            raw_mode=mode,
            cfa_pattern=cfa_pattern if mode == "cfa" else None,
            sensor_noise_model="synthetic-simple-v1" if sensor_effects.enabled else None,
            shot_noise=shot_noise if shot_noise > 0 else None,
            read_noise=read_noise if read_noise > 0 else None,
            row_noise=row_noise if row_noise > 0 else None,
            sensor_effect_seed=sensor_effect_seed if sensor_effects.enabled else None,
        )
    except ValueError as exc:
        raise InvalidMetadataError(str(exc)) from exc

    write_dng(target, raw_buffer, core, camera, ai, dng_layout=dng_layout)
    return ConversionResult(
        output_path=target,
        input_space=input_space,
        mode=mode,
        width=int(core.width),
        height=int(core.height),
        prompt_hash=prompt_hash,
        raw_data_unique_id=_raw_data_unique_id_hex(raw_buffer),
    )


def _raw_data_unique_id_hex(raw_buffer: np.ndarray) -> str:
    import hashlib

    return hashlib.md5(raw_buffer.tobytes()).hexdigest().upper()
