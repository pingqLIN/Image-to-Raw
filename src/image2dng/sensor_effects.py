from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Integral

import numpy as np


@dataclass(frozen=True)
class SensorEffectModel:
    shot_noise: float = 0.0
    read_noise: float = 0.0
    row_noise: float = 0.0
    seed: int | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("shot_noise", self.shot_noise),
            ("read_noise", self.read_noise),
            ("row_noise", self.row_noise),
        ):
            try:
                noise_value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name} must be non-negative finite") from exc
            if not math.isfinite(noise_value) or noise_value < 0:
                raise ValueError(f"{field_name} must be non-negative finite")
            object.__setattr__(self, field_name, noise_value)
        if self.seed is not None and (
            not isinstance(self.seed, Integral) or self.seed < 0
        ):
            raise ValueError("seed must be a non-negative integer")

    @property
    def enabled(self) -> bool:
        return self.shot_noise > 0 or self.read_noise > 0 or self.row_noise > 0


def apply_sensor_effects(camera_native: np.ndarray, effects: SensorEffectModel) -> np.ndarray:
    if not effects.enabled:
        return camera_native

    rng = np.random.default_rng(effects.seed)
    signal = np.clip(camera_native, 0.0, 1.0).astype(np.float64, copy=True)
    if effects.shot_noise > 0:
        signal += rng.normal(0.0, np.sqrt(signal) * effects.shot_noise, size=signal.shape)
    if effects.read_noise > 0:
        signal += rng.normal(0.0, effects.read_noise, size=signal.shape)
    if effects.row_noise > 0:
        row_offsets = rng.normal(0.0, effects.row_noise, size=(signal.shape[0], 1, 1))
        signal += row_offsets
    return np.clip(signal, 0.0, 1.0)
