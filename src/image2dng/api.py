from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from image2dng.dng_writer import write_dng
from image2dng.image_processing import InputSpace, build_cfa_buffer, build_linearraw_buffer
from image2dng.models import AIMetadataModel, CameraProfileModel, CfaPattern

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
    prompt_hash: str | None = None,
    prompt_plaintext: str | None = None,
    scene_description: str = "",
    model_name: str = "",
    model_version: str = "",
    lighting: str = "",
    weather: str = "",
    overwrite: bool = False,
) -> ConversionResult:
    target = Path(output_path)
    if target.exists() and not overwrite:
        raise OutputExistsError(f"output already exists: {target}")
    if mode not in {"linearraw", "cfa"}:
        raise InvalidMetadataError(f"unsupported output mode: {mode}")
    if iso <= 0:
        raise InvalidMetadataError("iso must be positive")
    if white_balance_kelvin <= 0:
        raise InvalidMetadataError("white_balance_kelvin must be positive")

    try:
        if mode == "cfa":
            raw_buffer, core = build_cfa_buffer(
                input_path,
                input_space,
                cfa_pattern=cfa_pattern,
            )
        else:
            raw_buffer, core = build_linearraw_buffer(input_path, input_space)
    except ValueError as exc:
        raise UnsupportedInputError(str(exc)) from exc

    try:
        camera = CameraProfileModel.from_white_balance(white_balance_kelvin)
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
        )
    except ValueError as exc:
        raise InvalidMetadataError(str(exc)) from exc

    write_dng(target, raw_buffer, core, camera, ai)
    return ConversionResult(
        output_path=target,
        input_space=input_space,
        mode=mode,
        width=int(core.width),
        height=int(core.height),
        prompt_hash=prompt_hash,
        raw_data_unique_id=_stable_buffer_id(raw_buffer),
    )


def _stable_buffer_id(raw_buffer: np.ndarray) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(raw_buffer.tobytes()).hexdigest()}"
