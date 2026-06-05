from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Integral
from typing import Literal

PHOTOMETRIC_LINEAR_RAW = 34892
PHOTOMETRIC_CFA = 32803
SYNTHETIC_CAMERA_MAKE = "image2dng"
SYNTHETIC_CAMERA_MODEL = "Synthetic Camera v1"

CfaPattern = Literal["rggb", "bggr", "grbg", "gbrg"]
PhotometricName = Literal["LinearRaw", "ColorFilterArray"]
RawMode = Literal["linearraw", "cfa"]
CFA_PATTERN_VALUES = frozenset({"rggb", "bggr", "grbg", "gbrg"})


@dataclass(frozen=True)
class CoreRawModel:
    width: int
    height: int
    bits_per_sample: int = 16
    samples_per_pixel: int = 3
    photometric: PhotometricName = "LinearRaw"
    black_level: tuple[int, ...] = (512, 512, 512)
    white_level: tuple[int, ...] = (65535, 65535, 65535)
    cfa_pattern: CfaPattern | None = None
    active_area: tuple[int, int, int, int] | None = None
    default_crop_origin: tuple[int, int] = (0, 0)
    default_crop_size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.width, Integral) or not isinstance(self.height, Integral):
            raise ValueError("width and height must be positive integers")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be positive integers")
        if not isinstance(self.bits_per_sample, Integral):
            raise ValueError("bits_per_sample must be an integer")
        if self.bits_per_sample != 16:
            raise ValueError("MVP only supports 16-bit output")
        if not isinstance(self.samples_per_pixel, Integral):
            raise ValueError("samples_per_pixel must be an integer")
        if self.photometric not in {"LinearRaw", "ColorFilterArray"}:
            raise ValueError("photometric must be LinearRaw or ColorFilterArray")
        is_cfa = self.photometric == "ColorFilterArray"
        if self.photometric == "LinearRaw" and self.samples_per_pixel != 3:
            raise ValueError("LinearRaw output expects three samples per pixel")
        if is_cfa and self.samples_per_pixel != 1:
            raise ValueError("CFA output expects one sample per pixel")
        if is_cfa and self.cfa_pattern is None:
            raise ValueError("CFA output requires a CFA pattern")
        if is_cfa and self.cfa_pattern not in CFA_PATTERN_VALUES:
            raise ValueError("cfa_pattern must be one of bggr, gbrg, grbg, rggb")
        if self.photometric == "LinearRaw" and self.cfa_pattern is not None:
            raise ValueError("LinearRaw output must not set a CFA pattern")
        expected_black_count = 4 if is_cfa else 3
        expected_white_count = 1 if is_cfa else 3
        if len(self.black_level) != expected_black_count:
            raise ValueError(
                f"black_level must contain {expected_black_count} values for this output mode"
            )
        if len(self.white_level) != expected_white_count:
            raise ValueError(
                f"white_level must contain {expected_white_count} values for this output mode"
            )
        if any(not isinstance(level, Integral) for level in self.black_level):
            raise ValueError("black levels must be integers")
        if any(not isinstance(level, Integral) for level in self.white_level):
            raise ValueError("white levels must be integers")
        if any(level < 0 for level in self.black_level):
            raise ValueError("black levels must be non-negative")
        if any(level <= 0 for level in self.white_level):
            raise ValueError("white levels must be positive")
        if any(level > 65535 for level in self.white_level):
            raise ValueError("white levels must be <= 65535")
        if any(min(self.white_level) <= black for black in self.black_level):
            raise ValueError("white levels must be greater than black levels")
        self._validate_geometry_tags()

    def _validate_geometry_tags(self) -> None:
        if len(self.default_crop_origin) != 2 or any(
            not isinstance(value, Integral) for value in self.default_crop_origin
        ):
            raise ValueError("default_crop_origin must contain two integer values")
        if any(value < 0 for value in self.default_crop_origin):
            raise ValueError("default_crop_origin must be non-negative")
        if self.default_crop_size is not None:
            if len(self.default_crop_size) != 2 or any(
                not isinstance(value, Integral) for value in self.default_crop_size
            ):
                raise ValueError("default_crop_size must contain two integer values")
            crop_width, crop_height = self.default_crop_size
            if crop_width <= 0 or crop_height <= 0:
                raise ValueError("default_crop_size values must be positive")
            origin_x, origin_y = self.default_crop_origin
            if origin_x + crop_width > self.width or origin_y + crop_height > self.height:
                raise ValueError("default crop must stay within image bounds")
        if self.active_area is None:
            return
        if len(self.active_area) != 4 or any(
            not isinstance(value, Integral) for value in self.active_area
        ):
            raise ValueError("active_area must contain four integer values")
        top, left, bottom, right = self.active_area
        if top < 0 or left < 0:
            raise ValueError("active_area origin must be non-negative")
        if bottom <= top or right <= left:
            raise ValueError("active_area bottom/right must exceed top/left")
        if bottom > self.height or right > self.width:
            raise ValueError("active_area must stay within image bounds")

    @classmethod
    def for_dimensions(
        cls,
        width: int,
        height: int,
        *,
        black_level: int = 512,
        white_level: int = 65535,
    ) -> CoreRawModel:
        return cls.for_linearraw(
            width=width,
            height=height,
            black_level=black_level,
            white_level=white_level,
        )

    @classmethod
    def for_linearraw(
        cls,
        width: int,
        height: int,
        *,
        black_level: int = 512,
        white_level: int = 65535,
    ) -> CoreRawModel:
        return cls(
            width=width,
            height=height,
            black_level=(black_level, black_level, black_level),
            white_level=(white_level, white_level, white_level),
            active_area=(0, 0, height, width),
            default_crop_size=(width, height),
        )

    @classmethod
    def for_cfa(
        cls,
        width: int,
        height: int,
        *,
        cfa_pattern: CfaPattern,
        black_level: int = 512,
        white_level: int = 65535,
    ) -> CoreRawModel:
        return cls(
            width=width,
            height=height,
            samples_per_pixel=1,
            photometric="ColorFilterArray",
            black_level=(black_level, black_level, black_level, black_level),
            white_level=(white_level,),
            cfa_pattern=cfa_pattern,
            active_area=(0, 0, height, width),
            default_crop_size=(width, height),
        )

    @property
    def resolved_active_area(self) -> tuple[int, int, int, int]:
        return self.active_area or (0, 0, self.height, self.width)

    @property
    def resolved_default_crop_size(self) -> tuple[int, int]:
        return self.default_crop_size or (self.width, self.height)


@dataclass(frozen=True)
class CameraProfileModel:
    make: str = SYNTHETIC_CAMERA_MAKE
    model: str = SYNTHETIC_CAMERA_MODEL
    unique_camera_model: str = SYNTHETIC_CAMERA_MODEL
    calibration_illuminant_1: int = 21
    color_matrix_1: tuple[float, ...] = (
        3.2404542,
        -1.5371385,
        -0.4985314,
        -0.9692660,
        1.8760108,
        0.0415560,
        0.0556434,
        -0.2040259,
        1.0572252,
    )
    as_shot_neutral: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("make", self.make),
            ("model", self.model),
            ("unique_camera_model", self.unique_camera_model),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if (
            not isinstance(self.calibration_illuminant_1, Integral)
            or self.calibration_illuminant_1 <= 0
        ):
            raise ValueError("calibration_illuminant_1 must be a positive integer")
        if len(self.color_matrix_1) != 9:
            raise ValueError("color_matrix_1 must contain exactly 9 values")
        if not all(math.isfinite(value) for value in self.color_matrix_1):
            raise ValueError("color_matrix_1 values must be finite")
        if len(self.as_shot_neutral) != 3:
            raise ValueError("as_shot_neutral must contain exactly 3 values")
        if not all(
            math.isfinite(value) and value > 0 for value in self.as_shot_neutral
        ):
            raise ValueError("as_shot_neutral values must be positive finite numbers")

    @classmethod
    def from_white_balance(cls, kelvin: float) -> CameraProfileModel:
        return cls(as_shot_neutral=cct_to_as_shot_neutral(kelvin))


@dataclass(frozen=True)
class AIMetadataModel:
    provenance_type: str = "synthetic"
    model_name: str = ""
    model_version: str = ""
    prompt_hash: str = ""
    scene_description: str = ""
    lighting: str = ""
    weather: str = ""
    camera_parameters_are_simulated: bool = True
    iso: int | None = None
    white_balance_kelvin: float | None = None
    prompt_plaintext: str | None = None
    raw_mode: RawMode = "linearraw"
    cfa_pattern: CfaPattern | None = None
    sensor_noise_model: str | None = None
    shot_noise: float | None = None
    read_noise: float | None = None
    row_noise: float | None = None
    sensor_effect_seed: int | None = None

    def __post_init__(self) -> None:
        if self.provenance_type != "synthetic":
            raise ValueError("synthetic provenance is required for this generator")
        if not self.camera_parameters_are_simulated:
            raise ValueError("camera parameters must be marked simulated")
        if self.prompt_plaintext is not None and not self.prompt_plaintext.strip():
            raise ValueError("prompt_plaintext must be non-empty when provided")
        if self.iso is not None and (
            not isinstance(self.iso, Integral) or self.iso <= 0
        ):
            raise ValueError("iso must be positive")
        if self.white_balance_kelvin is not None:
            try:
                white_balance_kelvin = float(self.white_balance_kelvin)
            except (TypeError, ValueError) as exc:
                raise ValueError("white_balance_kelvin must be positive finite") from exc
            if not math.isfinite(white_balance_kelvin) or white_balance_kelvin <= 0:
                raise ValueError("white_balance_kelvin must be positive finite")
        if self.raw_mode not in {"linearraw", "cfa"}:
            raise ValueError("raw_mode must be 'linearraw' or 'cfa'")
        if self.raw_mode == "linearraw" and self.cfa_pattern is not None:
            raise ValueError("linearraw metadata must not set cfa_pattern")
        if self.raw_mode == "cfa" and self.cfa_pattern not in CFA_PATTERN_VALUES:
            raise ValueError("cfa metadata requires a supported cfa_pattern")
        for field_name, value in (
            ("shot_noise", self.shot_noise),
            ("read_noise", self.read_noise),
            ("row_noise", self.row_noise),
        ):
            if value is not None and (not math.isfinite(float(value)) or value < 0):
                raise ValueError(f"{field_name} must be non-negative finite")


def cct_to_as_shot_neutral(kelvin: float) -> tuple[float, float, float]:
    """Approximate white point coordinates normalized to green for DNG AsShotNeutral."""
    try:
        kelvin_value = float(kelvin)
    except (TypeError, ValueError) as exc:
        raise ValueError("white balance Kelvin must be positive finite") from exc
    if not math.isfinite(kelvin_value) or kelvin_value <= 0:
        raise ValueError("white balance Kelvin must be positive finite")

    temperature = max(1000.0, min(40000.0, kelvin_value)) / 100.0
    if temperature <= 66.0:
        red = 255.0
        green = 99.4708025861 * math.log(temperature) - 161.1195681661
        blue = (
            0.0
            if temperature <= 19.0
            else 138.5177312231 * math.log(temperature - 10.0) - 305.0447927307
        )
    else:
        red = 329.698727446 * ((temperature - 60.0) ** -0.1332047592)
        green = 288.1221695283 * ((temperature - 60.0) ** -0.0755148492)
        blue = 255.0

    red = max(1.0, min(255.0, red))
    green = max(1.0, min(255.0, green))
    blue = max(1.0, min(255.0, blue))
    return (red / green, 1.0, blue / green)
