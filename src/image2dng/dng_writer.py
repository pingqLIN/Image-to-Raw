from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path
from typing import Literal

import numpy as np
import tifffile

from image2dng import __version__
from image2dng.models import (
    PHOTOMETRIC_CFA,
    PHOTOMETRIC_LINEAR_RAW,
    AIMetadataModel,
    CameraProfileModel,
    CfaPattern,
    CoreRawModel,
)
from image2dng.xmp import build_xmp_packet

TAG_XMP = 700
TAG_NEW_SUBFILE_TYPE = 254
TAG_MAKE = 271
TAG_MODEL = 272
TAG_ORIENTATION = 274
TAG_CFA_REPEAT_PATTERN_DIM = 33421
TAG_CFA_PATTERN = 33422
TAG_DNG_VERSION = 50706
TAG_DNG_BACKWARD_VERSION = 50707
TAG_UNIQUE_CAMERA_MODEL = 50708
TAG_CFA_PLANE_COLOR = 50710
TAG_CFA_LAYOUT = 50711
TAG_BLACK_LEVEL_REPEAT_DIM = 50713
TAG_BLACK_LEVEL = 50714
TAG_WHITE_LEVEL = 50717
TAG_DEFAULT_SCALE = 50718
TAG_DEFAULT_CROP_ORIGIN = 50719
TAG_DEFAULT_CROP_SIZE = 50720
TAG_COLOR_MATRIX_1 = 50721
TAG_AS_SHOT_NEUTRAL = 50728
TAG_CALIBRATION_ILLUMINANT_1 = 50778
TAG_ACTIVE_AREA = 50829
TAG_RAW_DATA_UNIQUE_ID = 50781

DngLayout = Literal["single-raw-ifd", "preview-subifd"]


def write_dng(
    output_path: str | Path,
    raw_buffer: np.ndarray,
    core: CoreRawModel,
    camera_profile: CameraProfileModel,
    ai_metadata: AIMetadataModel,
    *,
    dng_layout: DngLayout = "preview-subifd",
) -> None:
    if raw_buffer.dtype != np.uint16:
        raise ValueError("raw_buffer must be uint16")
    expected_shape = (
        (core.height, core.width, core.samples_per_pixel)
        if core.samples_per_pixel > 1
        else (core.height, core.width)
    )
    if raw_buffer.shape != expected_shape:
        raise ValueError(
            "raw_buffer shape does not match CoreRawModel: "
            f"{raw_buffer.shape} != {expected_shape}"
        )
    if dng_layout not in {"single-raw-ifd", "preview-subifd"}:
        raise ValueError(f"unsupported DNG layout: {dng_layout}")

    if dng_layout == "single-raw-ifd":
        _write_single_raw_ifd(output_path, raw_buffer, core, camera_profile, ai_metadata)
        return

    _write_preview_subifd(output_path, raw_buffer, core, camera_profile, ai_metadata)


def _write_single_raw_ifd(
    output_path: str | Path,
    raw_buffer: np.ndarray,
    core: CoreRawModel,
    camera_profile: CameraProfileModel,
    ai_metadata: AIMetadataModel,
) -> None:
    tifffile.imwrite(
        output_path,
        raw_buffer,
        photometric=_photometric_code(core),
        compression=None,
        metadata=None,
        software=f"image2dng {__version__}",
        extratags=_raw_extratags(raw_buffer, core, camera_profile, ai_metadata),
        **_raw_write_kwargs(core),
    )


def _write_preview_subifd(
    output_path: str | Path,
    raw_buffer: np.ndarray,
    core: CoreRawModel,
    camera_profile: CameraProfileModel,
    ai_metadata: AIMetadataModel,
) -> None:
    preview = _embedded_preview(raw_buffer, core)
    with tifffile.TiffWriter(output_path) as writer:
        writer.write(
            preview,
            photometric="rgb",
            compression="jpeg",
            compressionargs={"level": 92, "outcolorspace": "RGB"},
            metadata=None,
            software=f"image2dng {__version__}",
            subifds=1,
            extratags=_preview_extratags(camera_profile, ai_metadata),
            planarconfig="contig",
        )
        writer.write(
            raw_buffer,
            photometric=_photometric_code(core),
            compression=None,
            metadata=None,
            software=f"image2dng {__version__}",
            extratags=_raw_extratags(raw_buffer, core, camera_profile, ai_metadata),
            **_raw_write_kwargs(core),
        )


def _raw_extratags(
    raw_buffer: np.ndarray,
    core: CoreRawModel,
    camera_profile: CameraProfileModel,
    ai_metadata: AIMetadataModel,
) -> list[tuple[int, str, int, object, bool]]:
    xmp_packet = build_xmp_packet(ai_metadata)
    extratags: list[tuple[int, str, int, object, bool]] = [
        (TAG_NEW_SUBFILE_TYPE, "I", 1, 0, False),
        (TAG_DNG_VERSION, "B", 4, (1, 4, 0, 0), False),
        (TAG_DNG_BACKWARD_VERSION, "B", 4, (1, 1, 0, 0), False),
        (TAG_MAKE, "s", 0, camera_profile.make, False),
        (TAG_MODEL, "s", 0, camera_profile.model, False),
        (TAG_UNIQUE_CAMERA_MODEL, "s", 0, camera_profile.unique_camera_model, False),
        (TAG_ORIENTATION, "H", 1, 1, False),
        (TAG_BLACK_LEVEL_REPEAT_DIM, "H", 2, _black_level_repeat_dim(core), False),
        (TAG_BLACK_LEVEL, "I", len(core.black_level), core.black_level, False),
        (TAG_WHITE_LEVEL, "I", len(core.white_level), core.white_level, False),
        (TAG_DEFAULT_SCALE, "2I", 2, (_rational(1.0), _rational(1.0)), False),
        (TAG_ACTIVE_AREA, "I", 4, core.resolved_active_area, False),
        (TAG_DEFAULT_CROP_ORIGIN, "I", 2, core.default_crop_origin, False),
        (TAG_DEFAULT_CROP_SIZE, "I", 2, core.resolved_default_crop_size, False),
        (
            TAG_COLOR_MATRIX_1,
            "2i",
            9,
            tuple(_srational(value) for value in camera_profile.color_matrix_1),
            False,
        ),
        (
            TAG_AS_SHOT_NEUTRAL,
            "2I",
            3,
            tuple(_rational(value) for value in camera_profile.as_shot_neutral),
            False,
        ),
        (TAG_CALIBRATION_ILLUMINANT_1, "H", 1, camera_profile.calibration_illuminant_1, False),
        (TAG_XMP, "B", len(xmp_packet), xmp_packet, False),
        (TAG_RAW_DATA_UNIQUE_ID, "B", 16, _raw_data_unique_id(raw_buffer), False),
    ]
    if core.photometric == "ColorFilterArray":
        extratags.extend(_cfa_extratags(core.cfa_pattern))
    return extratags


def _preview_extratags(
    camera_profile: CameraProfileModel,
    ai_metadata: AIMetadataModel,
) -> list[tuple[int, str, int, object, bool]]:
    xmp_packet = build_xmp_packet(ai_metadata)
    return [
        (TAG_NEW_SUBFILE_TYPE, "I", 1, 1, False),
        (TAG_DNG_VERSION, "B", 4, (1, 4, 0, 0), False),
        (TAG_DNG_BACKWARD_VERSION, "B", 4, (1, 1, 0, 0), False),
        (TAG_MAKE, "s", 0, camera_profile.make, False),
        (TAG_MODEL, "s", 0, camera_profile.model, False),
        (TAG_UNIQUE_CAMERA_MODEL, "s", 0, camera_profile.unique_camera_model, False),
        (TAG_ORIENTATION, "H", 1, 1, False),
        (TAG_XMP, "B", len(xmp_packet), xmp_packet, False),
    ]


def _raw_write_kwargs(core: CoreRawModel) -> dict[str, str]:
    return {"planarconfig": "contig"} if core.samples_per_pixel > 1 else {}


def _photometric_code(core: CoreRawModel) -> int:
    if core.photometric == "ColorFilterArray":
        return PHOTOMETRIC_CFA
    return PHOTOMETRIC_LINEAR_RAW


def _black_level_repeat_dim(core: CoreRawModel) -> tuple[int, int]:
    if core.photometric == "ColorFilterArray":
        return (2, 2)
    return (1, 1)


def _raw_data_unique_id(raw_buffer: np.ndarray) -> tuple[int, ...]:
    # DNG RawDataUniqueID is a fixed 16-byte identifier for the raw image data.
    return tuple(hashlib.md5(raw_buffer.tobytes()).digest())


def _embedded_preview(raw_buffer: np.ndarray, core: CoreRawModel) -> np.ndarray:
    if raw_buffer.ndim == 2:
        return _cfa_false_color(raw_buffer, core.cfa_pattern or "rggb")
    return np.stack([_tone_map(raw_buffer[..., channel]) for channel in range(3)], axis=2)


def _tone_map(channel: np.ndarray) -> np.ndarray:
    data = channel.astype(np.float64)
    low, high = np.percentile(data, [0.5, 99.5])
    if high <= low:
        high = low + 1.0
    return np.clip((data - low) / (high - low) * 255.0, 0, 255).astype(np.uint8)


def _cfa_false_color(mosaic: np.ndarray, cfa_pattern: CfaPattern) -> np.ndarray:
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


def _cfa_extratags(cfa_pattern: CfaPattern | None) -> list[tuple[int, str, int, object, bool]]:
    if cfa_pattern is None:
        raise ValueError("CFA DNG requires cfa_pattern")
    return [
        (TAG_CFA_REPEAT_PATTERN_DIM, "H", 2, (2, 2), False),
        (TAG_CFA_PATTERN, "B", 4, _cfa_pattern_values(cfa_pattern), False),
        (TAG_CFA_PLANE_COLOR, "B", 3, (0, 1, 2), False),
        (TAG_CFA_LAYOUT, "H", 1, 1, False),
    ]


def _cfa_pattern_values(cfa_pattern: CfaPattern) -> tuple[int, int, int, int]:
    patterns = {
        "rggb": (0, 1, 1, 2),
        "bggr": (2, 1, 1, 0),
        "grbg": (1, 0, 2, 1),
        "gbrg": (1, 2, 0, 1),
    }
    return patterns[cfa_pattern]


def _rational(value: float) -> tuple[int, int]:
    fraction = Fraction(float(value)).limit_denominator(1_000_000)
    if fraction.numerator < 0:
        raise ValueError("RATIONAL values must be non-negative")
    return (fraction.numerator, fraction.denominator)


def _srational(value: float) -> tuple[int, int]:
    fraction = Fraction(float(value)).limit_denominator(1_000_000)
    return (fraction.numerator, fraction.denominator)
