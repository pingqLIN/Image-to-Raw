from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

PHOTOMETRIC_LINEAR_RAW = 34892
PHOTOMETRIC_CFA = 32803
SYNTHETIC_CAMERA_MAKE = "image2dng"
SYNTHETIC_CAMERA_MODEL = "Synthetic Camera v1"

CfaPattern = Literal["rggb", "bggr", "grbg", "gbrg"]
PhotometricName = Literal["LinearRaw", "ColorFilterArray"]
SceneLinearInputSpace = Literal["linear-rec709", "acescg", "xyz"]

SCENE_LINEAR_INPUT_SPACES = frozenset({"linear-rec709", "acescg", "xyz"})


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
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be positive")
        if self.bits_per_sample != 16:
            raise ValueError("MVP only supports 16-bit output")
        if self.photometric == "LinearRaw" and self.samples_per_pixel != 3:
            raise ValueError("LinearRaw output expects three samples per pixel")
        if self.photometric == "ColorFilterArray" and self.samples_per_pixel != 1:
            raise ValueError("CFA output expects one sample per pixel")
        if self.photometric == "ColorFilterArray" and self.cfa_pattern is None:
            raise ValueError("CFA output requires a CFA pattern")
        if self.photometric == "LinearRaw" and self.cfa_pattern is not None:
            raise ValueError("LinearRaw output must not set a CFA pattern")
        if any(level < 0 for level in self.black_level):
            raise ValueError("black levels must be non-negative")
        if not self.white_level:
            raise ValueError("at least one white level is required")
        if any(self.white_level[0] <= black for black in self.black_level):
            raise ValueError("white levels must be greater than black levels")

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

    @classmethod
    def from_white_balance(cls, kelvin: float) -> CameraProfileModel:
        return cls(as_shot_neutral=cct_to_as_shot_neutral(kelvin))


@dataclass(frozen=True)
class ExposurePlacementModel:
    highlight_headroom_ev: float = 0.0
    exposure_bias_ev: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.highlight_headroom_ev):
            raise ValueError("highlight_headroom_ev must be finite")
        if not math.isfinite(self.exposure_bias_ev):
            raise ValueError("exposure_bias_ev must be finite")
        if self.highlight_headroom_ev < 0:
            raise ValueError("highlight_headroom_ev must be non-negative")

    @property
    def enabled(self) -> bool:
        return self.highlight_headroom_ev != 0.0 or self.exposure_bias_ev != 0.0

    @property
    def scale(self) -> float:
        return 2.0 ** (self.exposure_bias_ev - self.highlight_headroom_ev)

    def validate_input_space(self, input_space: str) -> None:
        if self.enabled and input_space not in SCENE_LINEAR_INPUT_SPACES:
            supported = ", ".join(sorted(SCENE_LINEAR_INPUT_SPACES))
            raise ValueError(
                "exposure placement requires true scene-linear input_space; "
                f"got {input_space!r}, supported: {supported}"
            )


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
    raw_mode: str = "linearraw"
    cfa_pattern: str | None = None
    sensor_noise_model: str | None = None
    shot_noise: float | None = None
    read_noise: float | None = None
    row_noise: float | None = None
    sensor_effect_seed: int | None = None
    highlight_headroom_ev: float | None = None
    exposure_bias_ev: float | None = None

    def __post_init__(self) -> None:
        if self.provenance_type != "synthetic":
            raise ValueError("synthetic provenance is required for this generator")
        if not self.camera_parameters_are_simulated:
            raise ValueError("camera parameters must be marked simulated")
        if self.prompt_plaintext is not None and not self.prompt_plaintext.strip():
            raise ValueError("prompt_plaintext must be non-empty when provided")


def cct_to_as_shot_neutral(kelvin: float) -> tuple[float, float, float]:
    """Approximate white point coordinates normalized to green for DNG AsShotNeutral."""
    if kelvin <= 0:
        raise ValueError("white balance Kelvin must be positive")

    temperature = max(1000.0, min(40000.0, kelvin)) / 100.0
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
