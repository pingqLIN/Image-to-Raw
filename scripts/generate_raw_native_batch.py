from __future__ import annotations

import argparse
import sys
from pathlib import Path

from image2dng.pipeline import (
    ExternalSceneLinearInput,
    load_external_scene_manifest,
    run_external_scene_linear_batch,
    run_raw_native_batch,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a raw-native node-graph batch with DNG and JPEG outputs."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/raw-native-node-batch"),
    )
    parser.add_argument(
        "--external-manifest",
        type=Path,
        default=None,
        help="JSON manifest with image2dng.external_scene_linear_sources.v1 scenes",
    )
    parser.add_argument(
        "--scene-linear",
        type=Path,
        action="append",
        default=[],
        help="external scene-linear TIFF/PNG input; may be passed multiple times",
    )
    parser.add_argument(
        "--dng-layout",
        choices=["preview-subifd", "single-raw-ifd"],
        default="preview-subifd",
        help=(
            "DNG IFD layout to write; use single-raw-ifd for Adobe DNG Converter "
            "input compatibility"
        ),
    )
    parser.add_argument(
        "--highlight-headroom-ev",
        type=float,
        default=0.0,
        help="scene-linear highlight headroom for --scene-linear inputs",
    )
    parser.add_argument(
        "--exposure-bias-ev",
        type=float,
        default=0.0,
        help="scene-linear exposure placement bias for --scene-linear inputs",
    )
    args = parser.parse_args(argv)

    external_scenes = []
    if args.external_manifest is not None:
        external_scenes.extend(load_external_scene_manifest(args.external_manifest))
    external_scenes.extend(
        ExternalSceneLinearInput(
            slug=path.stem,
            path=path,
            description=f"external scene-linear input: {path.name}",
            producer="image2dng CLI external scene-linear input",
            highlight_headroom_ev=args.highlight_headroom_ev,
            exposure_bias_ev=args.exposure_bias_ev,
        )

        if external_scenes:
            result = run_external_scene_linear_batch(
                args.output_dir,
                scenes=external_scenes,
                overwrite=True,
                dng_layout=args.dng_layout,
            )
        else:
            result = run_raw_native_batch(
                args.output_dir,
                overwrite=True,
                dng_layout=args.dng_layout,
            )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote raw-native node batch to {result.output_dir}")
    print(f"Wrote manifest to {result.manifest_path}")
    print(f"Wrote sample index to {result.sample_index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
