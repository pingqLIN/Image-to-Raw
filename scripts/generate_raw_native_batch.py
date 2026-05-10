from __future__ import annotations

import argparse
from pathlib import Path

from image2dng.pipeline import run_raw_native_batch


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a raw-native node-graph batch with DNG and JPEG outputs."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/raw-native-node-batch"),
    )
    args = parser.parse_args()

    result = run_raw_native_batch(args.output_dir, overwrite=True)
    print(f"Wrote raw-native node batch to {result.output_dir}")
    print(f"Wrote manifest to {result.manifest_path}")
    print(f"Wrote sample index to {result.sample_index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
