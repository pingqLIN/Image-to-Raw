from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import tifffile

from image2dng import convert


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate image2dng demo sample outputs.")
    parser.add_argument("--output-dir", type=Path, default=Path("demo-output"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    input_path = args.output_dir / "demo-gradient.tif"
    _write_demo_input(input_path)

    convert(
        input_path=input_path,
        output_path=args.output_dir / "demo-linearraw.dng",
        input_space="linear-rec709",
        mode="linearraw",
        prompt_hash="sha256:demo-linearraw",
        scene_description="deterministic gradient demo for LinearRaw mode",
        overwrite=True,
    )
    convert(
        input_path=input_path,
        output_path=args.output_dir / "demo-cfa-rggb.dng",
        input_space="linear-rec709",
        mode="cfa",
        cfa_pattern="rggb",
        prompt_hash="sha256:demo-cfa",
        scene_description="deterministic gradient demo for simulated CFA mode",
        overwrite=True,
    )
    convert(
        input_path=input_path,
        output_path=args.output_dir / "demo-cfa-rggb-noisy.dng",
        input_space="linear-rec709",
        mode="cfa",
        cfa_pattern="rggb",
        shot_noise=0.01,
        read_noise=0.002,
        row_noise=0.001,
        sensor_effect_seed=20260510,
        prompt_hash="sha256:demo-cfa-noisy",
        scene_description="deterministic gradient demo for simulated CFA with sensor effects",
        overwrite=True,
    )
    print(f"Wrote demo samples to {args.output_dir}")
    return 0


def _write_demo_input(path: Path) -> None:
    width = 64
    height = 64
    x = np.linspace(0, 65535, width, dtype=np.uint16)
    y = np.linspace(0, 65535, height, dtype=np.uint16)
    red = np.tile(x, (height, 1))
    green = np.tile(y[:, np.newaxis], (1, width))
    blue = ((red.astype(np.uint32) + green.astype(np.uint32)) // 2).astype(np.uint16)
    image = np.stack([red, green, blue], axis=2)
    tifffile.imwrite(path, image, photometric="rgb")


if __name__ == "__main__":
    raise SystemExit(main())
