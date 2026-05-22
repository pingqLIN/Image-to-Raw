from __future__ import annotations

import argparse
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

from image2dng import __version__, convert
from image2dng.compatibility import (
    ADOBE_DNG_CONVERTER_TOOL,
    adobe_dng_converter_output_path,
    adobe_dng_converter_resource_state,
    resolve_processor_executable,
    run_adobe_dng_converter,
)
from image2dng.validate import inspect_adobe_converted_dng, validate_dng


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a single-raw-ifd DNG fixture and verify Adobe DNG "
            "Converter can rewrite it into an output DNG."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/adobe-dng-converter-verification"),
    )
    parser.add_argument(
        "--converter",
        type=Path,
        help="explicit path to Adobe DNG Converter.exe",
    )
    parser.add_argument(
        "--adobe-dir",
        type=Path,
        default=Path("Adobe"),
        help="local Adobe resource cache used only for resource-state reporting",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="generate and validate the fixture, but do not invoke Adobe DNG Converter",
    )
    args = parser.parse_args(argv)

    root = args.output_dir
    inputs_dir = root / "inputs"
    source_dir = root / "source-dng"
    converted_dir = root / "converted"
    reports_dir = root / "reports"
    for directory in (inputs_dir, source_dir, converted_dir, reports_dir):
        directory.mkdir(parents=True, exist_ok=True)

    converter, discovery = _resolve_converter(args.converter)
    resource_state = adobe_dng_converter_resource_state(
        converter_path=args.converter,
        adobe_dir=args.adobe_dir,
    )
    input_path = inputs_dir / "adobe-single-raw-ifd-fixture.tif"
    source_dng = source_dir / "adobe-single-raw-ifd-fixture.dng"
    converted_dng = adobe_dng_converter_output_path(source_dng, converted_dir)

    tifffile.imwrite(input_path, _fixture_image(), photometric="rgb")
    convert(
        input_path=input_path,
        output_path=source_dng,
        input_space="linear-rec709",
        mode="linearraw",
        prompt_hash="sha256:adobe-dng-converter-regression",
        scene_description="Adobe DNG Converter single-raw-ifd regression fixture",
        model_name="image2dng Adobe DNG Converter verifier",
        model_version=__version__,
        overwrite=True,
        dng_layout="single-raw-ifd",
    )
    source_validation = validate_dng(source_dng, run_smoke=False).to_dict()

    command = [
        converter or ADOBE_DNG_CONVERTER_TOOL,
        "-c",
        "-d",
        str(converted_dir.resolve()),
        str(source_dng.resolve()),
    ]
    converter_result: dict[str, Any] | None = None
    converted_inspection: dict[str, Any] | None = None
    moved_existing_converted_dng: str | None = None
    errors: list[str] = []

    if not _report_ok(source_validation, "source contract validation"):
        errors.append("source image2dng contract validation failed")

    if args.dry_run:
        status = "dry-run" if not errors else "failed"
    elif converter is None:
        status = "failed"
        errors.append("Adobe DNG Converter executable was not found")
    else:
        if converted_dng.exists():
            moved_existing_converted_dng = str(_move_existing_output_aside(converted_dng))
        processor_result = run_adobe_dng_converter(
            source_dng,
            converted_dir,
            converter_path=converter,
            timeout_seconds=args.timeout_seconds,
        )
        converter_result = processor_result.to_dict()
        if processor_result.result != "passed":
            errors.append(f"Adobe DNG Converter failed: {processor_result.notes}")
        converted_inspection = inspect_adobe_converted_dng(
            converted_dng,
            run_smoke=False,
        ).to_dict()
        if not _report_ok(converted_inspection, "Adobe-converted artifact inspection"):
            errors.append("Adobe-converted artifact inspection failed")
        status = "passed" if not errors else "failed"

    report = {
        "schema": "image2dng.adobe_dng_converter_verification.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": args.dry_run,
        "status": status,
        "ok": not errors,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "converter": {
            "tool": ADOBE_DNG_CONVERTER_TOOL,
            "available": converter is not None,
            "executable": converter,
            "discovery": discovery,
            "resource_state": resource_state,
        },
        "command": command,
        "artifacts": {
            "input": str(input_path),
            "source_dng": str(source_dng),
            "converted_dng": str(converted_dng),
            "moved_existing_converted_dng": moved_existing_converted_dng,
        },
        "source_contract_validation": source_validation,
        "converter_result": converter_result,
        "converted_artifact_inspection": converted_inspection,
        "errors": errors,
    }

    report_path = reports_dir / "adobe-dng-converter-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote Adobe DNG Converter verification report to {report_path}")
    if args.dry_run:
        print("Dry run: Adobe DNG Converter was not invoked.")
    elif converter is None:
        print("Adobe DNG Converter executable was not found.")
    elif converted_dng.exists():
        print(f"Adobe DNG Converter output: {converted_dng}")
    else:
        print("Adobe DNG Converter did not emit the expected output DNG.")
    return 0 if report["ok"] else 1


def _resolve_converter(explicit: Path | None) -> tuple[str | None, str | None]:
    if explicit is not None:
        if explicit.exists():
            return str(explicit.resolve()), "explicit"
        return None, "explicit-missing"
    return resolve_processor_executable(ADOBE_DNG_CONVERTER_TOOL)


def _report_ok(report: dict[str, Any], label: str) -> bool:
    ok = report["ok"]
    if not isinstance(ok, bool):
        raise TypeError(f"{label} ok must be a boolean")
    return ok


def _fixture_image(size: int = 64) -> np.ndarray:
    image = np.zeros((size, size, 3), dtype=np.uint16)
    x = np.linspace(0, 65535, size, dtype=np.uint16)
    y = np.linspace(0, 65535, size, dtype=np.uint16)
    image[..., 0] = x[np.newaxis, :]
    image[..., 1] = y[:, np.newaxis]
    image[..., 2] = ((image[..., 0].astype(np.uint32) + image[..., 1]) // 2).astype(np.uint16)
    image[8:24, 8:24] = [65535, 0, 0]
    image[8:24, 24:40] = [0, 65535, 0]
    image[8:24, 40:56] = [0, 0, 65535]
    return image


def _move_existing_output_aside(path: Path) -> Path:
    clean_dir = path.parent / ".clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = clean_dir / f"{path.stem}-{timestamp}{path.suffix}"
    counter = 1
    while candidate.exists():
        candidate = clean_dir / f"{path.stem}-{timestamp}-{counter}{path.suffix}"
        counter += 1
    path.replace(candidate)
    return candidate


if __name__ == "__main__":
    raise SystemExit(main())
