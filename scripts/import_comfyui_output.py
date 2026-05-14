from __future__ import annotations

import argparse
from pathlib import Path

from image2dng.comfyui_importer import import_comfyui_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import ComfyUI image outputs into the image2dng external scene pipeline."
    )
    parser.add_argument("input", type=Path, nargs="+", help="ComfyUI output image file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/comfyui-import"),
        help=(
            "directory for prepared TIFF inputs, metadata, importer manifest, "
            "and optional raw-native-node-batch"
        ),
    )
    parser.add_argument(
        "--input-space",
        choices=["srgb", "linear-rec709", "acescg", "xyz", "prophoto-rgb"],
        default="srgb",
        help="color-space interpretation for prepared inputs; ComfyUI PNG outputs are usually srgb",
    )
    parser.add_argument(
        "--run-pipeline",
        action="store_true",
        help="run the RAW-native external scene pipeline after writing the importer manifest",
    )
    parser.add_argument(
        "--dng-layout",
        choices=["preview-subifd", "single-raw-ifd"],
        default="preview-subifd",
        help=(
            "DNG IFD layout for --run-pipeline; use single-raw-ifd for Adobe DNG "
            "Converter input compatibility"
        ),
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="fail if importer outputs already exist",
    )
    args = parser.parse_args(argv)

    result = import_comfyui_outputs(
        args.input,
        args.output_dir,
        input_space=args.input_space,
        run_pipeline=args.run_pipeline,
        overwrite=not args.no_overwrite,
        dng_layout=args.dng_layout,
    )
    print(f"Wrote ComfyUI external scene manifest to {result.manifest_path}")
    for scene in result.scenes:
        print(f"Imported {scene.source_path} -> {scene.prepared_path}")
        print(f"Wrote metadata summary to {scene.metadata_path}")
    if result.pipeline_result is not None:
        print(f"Wrote RAW-native batch to {result.pipeline_result.output_dir}")
        print(f"Wrote batch manifest to {result.pipeline_result.manifest_path}")
        print(f"Wrote sample index to {result.pipeline_result.sample_index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
