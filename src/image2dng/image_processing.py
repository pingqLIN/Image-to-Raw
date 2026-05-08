from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import png
import tifffile

from image2dng.models import CoreRawModel

InputSpace = Literal["srgb", "linear-rec709", "acescg", "xyz"]

ACESCG_TO_XYZ = np.array(
    [
        [0.66245418, 0.13400421, 0.15618769],
        [0.27222872, 0.67408177, 0.05368952],
        [-0.00557465, 0.00406073, 1.01033910],
    ],
    dtype=np.float64,
)

XYZ_TO_CAMERA_NATIVE = np.array(
    [
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ],
    dtype=np.float64,
)


def load_input_image(path: str | Path) -> np.ndarray:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix in {".tif", ".tiff", ".dng"}:
        image = tifffile.imread(source)
    elif suffix == ".png":
        image = read_png(source)
    else:
        raise ValueError(f"unsupported input format: {source.suffix}")
    return ensure_rgb(image)


def read_png(path: str | Path) -> np.ndarray:
    reader = png.Reader(filename=str(path))
    width, height, rows, info = reader.asDirect()
    bitdepth = int(info["bitdepth"])
    if bitdepth != 16:
        raise ValueError(f"PNG input must be 16-bit, got {bitdepth}-bit")

    planes = int(info["planes"])
    row_arrays = [np.fromiter(row, dtype=np.uint16, count=width * planes) for row in rows]
    return np.vstack(row_arrays).reshape(height, width, planes)


def ensure_rgb(image: np.ndarray) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim == 2:
        return np.repeat(array[..., np.newaxis], 3, axis=2)
    if array.ndim != 3:
        raise ValueError(f"expected 2D grayscale or 3D RGB image, got shape {array.shape}")
    if array.shape[2] == 1:
        return np.repeat(array, 3, axis=2)
    if array.shape[2] in {3, 4}:
        return array[..., :3]
    raise ValueError(f"expected 1, 3, or 4 channels, got {array.shape[2]}")


def normalize_to_float(image: np.ndarray) -> np.ndarray:
    if np.issubdtype(image.dtype, np.integer):
        if image.dtype.itemsize < 2:
            raise ValueError(
                "MVP expects 16-bit integer input or floating-point scene-linear input"
            )
        max_value = float(np.iinfo(image.dtype).max)
        return image.astype(np.float64) / max_value
    if np.issubdtype(image.dtype, np.floating):
        return image.astype(np.float64)
    raise ValueError(f"unsupported image dtype: {image.dtype}")


def inverse_srgb_oetf(rgb: np.ndarray) -> np.ndarray:
    clipped = np.clip(rgb, 0.0, 1.0)
    return np.where(clipped <= 0.04045, clipped / 12.92, ((clipped + 0.055) / 1.055) ** 2.4)


def to_camera_native(linear: np.ndarray, input_space: InputSpace) -> np.ndarray:
    if input_space in {"srgb", "linear-rec709"}:
        return linear
    if input_space == "xyz":
        return linear @ XYZ_TO_CAMERA_NATIVE.T
    if input_space == "acescg":
        xyz = linear @ ACESCG_TO_XYZ.T
        return xyz @ XYZ_TO_CAMERA_NATIVE.T
    raise ValueError(f"unsupported input space: {input_space}")


def build_linearraw_buffer(
    input_path: str | Path,
    input_space: InputSpace,
    *,
    black_level: int = 512,
    white_level: int = 65535,
) -> tuple[np.ndarray, CoreRawModel]:
    rgb = load_input_image(input_path)
    height, width, _ = rgb.shape
    core = CoreRawModel.for_dimensions(
        width=width,
        height=height,
        black_level=black_level,
        white_level=white_level,
    )
    normalized = normalize_to_float(rgb)
    scene_linear = inverse_srgb_oetf(normalized) if input_space == "srgb" else normalized
    camera_native = to_camera_native(scene_linear, input_space)
    quantized = quantize_linearraw(camera_native, core)
    return quantized, core


def quantize_linearraw(camera_native: np.ndarray, core: CoreRawModel) -> np.ndarray:
    clipped = np.clip(camera_native, 0.0, 1.0)
    black = np.asarray(core.black_level, dtype=np.float64)
    white = np.asarray(core.white_level, dtype=np.float64)
    scaled = black + clipped * (white - black)
    return np.rint(np.clip(scaled, black, white)).astype(np.uint16)
