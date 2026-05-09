from __future__ import annotations

import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import tifffile

from image2dng.dng_writer import (
    TAG_AS_SHOT_NEUTRAL,
    TAG_BLACK_LEVEL,
    TAG_CALIBRATION_ILLUMINANT_1,
    TAG_COLOR_MATRIX_1,
    TAG_DNG_BACKWARD_VERSION,
    TAG_DNG_VERSION,
    TAG_UNIQUE_CAMERA_MODEL,
    TAG_WHITE_LEVEL,
    TAG_XMP,
)
from image2dng.models import PHOTOMETRIC_LINEAR_RAW
from image2dng.xmp import XMP_AI_NAMESPACE

TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_IMAGE_LENGTH = 257
TAG_IMAGE_WIDTH = 256
TAG_MAKER_NOTE = 37500
TAG_ORIENTATION = 274
TAG_PHOTOMETRIC = 262
TAG_SAMPLES_PER_PIXEL = 277
TAG_SOFTWARE = 305


@dataclass
class ValidationResult:
    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    smoke_tests: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "smoke_tests": self.smoke_tests,
        }


def validate_dng(path: str | Path, *, run_smoke: bool = True) -> ValidationResult:
    target = Path(path)
    result = ValidationResult(path=target)
    if not target.exists():
        result.errors.append(f"file does not exist: {target}")
        return result

    try:
        with tifffile.TiffFile(target) as tif:
            if not tif.pages:
                result.errors.append("no TIFF/DNG pages found")
                return result
            page = tif.pages[0]
            _check_required_tags(page, result)
            _check_geometry(page, result)
            _check_levels(page, result)
            _check_xmp(page, result)
            _check_makernote(page, result)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"failed to parse DNG: {exc}")
        return result

    if run_smoke:
        _run_external_smoke_tests(target, result)
    return result


def _check_required_tags(page: tifffile.TiffPage, result: ValidationResult) -> None:
    required = {
        TAG_DNG_VERSION: "DNGVersion",
        TAG_DNG_BACKWARD_VERSION: "DNGBackwardVersion",
        TAG_UNIQUE_CAMERA_MODEL: "UniqueCameraModel",
        TAG_ORIENTATION: "Orientation",
        TAG_IMAGE_WIDTH: "ImageWidth",
        TAG_IMAGE_LENGTH: "ImageLength",
        TAG_BITS_PER_SAMPLE: "BitsPerSample",
        TAG_SAMPLES_PER_PIXEL: "SamplesPerPixel",
        TAG_COMPRESSION: "Compression",
        TAG_PHOTOMETRIC: "PhotometricInterpretation",
        TAG_BLACK_LEVEL: "BlackLevel",
        TAG_WHITE_LEVEL: "WhiteLevel",
        TAG_COLOR_MATRIX_1: "ColorMatrix1",
        TAG_CALIBRATION_ILLUMINANT_1: "CalibrationIlluminant1",
        TAG_AS_SHOT_NEUTRAL: "AsShotNeutral",
        TAG_SOFTWARE: "Software",
        TAG_XMP: "XMP",
    }
    for code, name in required.items():
        if code not in page.tags:
            result.errors.append(f"missing required tag: {name} ({code})")

    photometric = _tag_value(page, TAG_PHOTOMETRIC)
    if photometric is not None and int(photometric) != PHOTOMETRIC_LINEAR_RAW:
        result.errors.append(
            "PhotometricInterpretation must be LinearRaw "
            f"({PHOTOMETRIC_LINEAR_RAW}), got {photometric}"
        )


def _check_geometry(page: tifffile.TiffPage, result: ValidationResult) -> None:
    width = _tag_value(page, TAG_IMAGE_WIDTH)
    height = _tag_value(page, TAG_IMAGE_LENGTH)
    bits = _as_tuple(_tag_value(page, TAG_BITS_PER_SAMPLE))
    samples = _tag_value(page, TAG_SAMPLES_PER_PIXEL)
    if width is None or height is None or not bits or samples is None:
        return
    if any(int(bit) != 16 for bit in bits):
        result.errors.append(f"BitsPerSample must be 16 for MVP output, got {bits}")
    if int(samples) != 3:
        result.errors.append(f"SamplesPerPixel must be 3 for LinearRaw MVP, got {samples}")

    try:
        data = page.asarray()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"failed to read image buffer: {exc}")
        return
    expected_shape = (int(height), int(width), int(samples))
    if data.shape != expected_shape:
        result.errors.append(f"image shape mismatch: expected {expected_shape}, got {data.shape}")

    expected_bytes = int(width) * int(height) * int(samples) * 2
    actual_bytes = sum(int(count) for count in _as_tuple(page.databytecounts))
    if actual_bytes != expected_bytes:
        result.errors.append(
            f"buffer byte count mismatch: expected {expected_bytes}, got {actual_bytes}"
        )


def _check_levels(page: tifffile.TiffPage, result: ValidationResult) -> None:
    black_levels = [int(value) for value in _as_tuple(_tag_value(page, TAG_BLACK_LEVEL))]
    white_levels = [int(value) for value in _as_tuple(_tag_value(page, TAG_WHITE_LEVEL))]
    if not black_levels or not white_levels:
        return
    if len(black_levels) not in {1, 3}:
        result.errors.append(f"BlackLevel should have 1 or 3 values, got {len(black_levels)}")
    if len(white_levels) not in {1, 3}:
        result.errors.append(f"WhiteLevel should have 1 or 3 values, got {len(white_levels)}")
    for black, white in zip(
        _expand_to_three(black_levels), _expand_to_three(white_levels), strict=True
    ):
        if black < 0:
            result.errors.append(f"BlackLevel must be non-negative, got {black}")
        if white > 65535:
            result.errors.append(f"WhiteLevel must be <= 65535, got {white}")
        if black >= white:
            result.errors.append(f"BlackLevel must be less than WhiteLevel, got {black}/{white}")


def _check_xmp(page: tifffile.TiffPage, result: ValidationResult) -> None:
    xmp = _tag_value(page, TAG_XMP)
    if xmp is None:
        return
    if isinstance(xmp, str):
        xmp_text = xmp
    elif isinstance(xmp, bytes):
        xmp_text = xmp.decode("utf-8")
    else:
        xmp_text = bytes(xmp).decode("utf-8")
    try:
        root = ET.fromstring(xmp_text)
    except ET.ParseError as exc:
        result.errors.append(f"XMP is not parseable XML: {exc}")
        return
    descriptions = root.findall(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    if not descriptions:
        result.errors.append("XMP has no rdf:Description")
        return
    attrs = descriptions[0].attrib
    provenance = attrs.get(f"{{{XMP_AI_NAMESPACE}}}provenanceType")
    simulated = attrs.get(f"{{{XMP_AI_NAMESPACE}}}cameraParametersAreSimulated")
    if provenance != "synthetic":
        result.errors.append("XMP synthetic provenance is missing or incorrect")
    if simulated != "True":
        result.errors.append("XMP simulated camera parameter flag is missing or incorrect")


def _check_makernote(page: tifffile.TiffPage, result: ValidationResult) -> None:
    if TAG_MAKER_NOTE in page.tags:
        result.errors.append("MakerNote tag must not be written for synthetic DNG")


def _run_external_smoke_tests(path: Path, result: ValidationResult) -> None:
    smoke_specs = [
        ("exiftool", ["exiftool", str(path)]),
        ("dcraw", ["dcraw", "-i", "-v", str(path)]),
    ]
    for name, command in smoke_specs:
        _run_optional_command(name, command, result)

    with tempfile.TemporaryDirectory(prefix="image2dng-validate-") as temp_dir:
        temp = Path(temp_dir)
        _run_optional_command(
            "darktable-cli",
            ["darktable-cli", str(path), str(temp / "darktable.tif")],
            result,
        )
        _run_optional_command(
            "rawtherapee-cli",
            ["rawtherapee-cli", "-Y", "-o", str(temp / "rawtherapee.tif"), "-c", str(path)],
            result,
        )


def _run_optional_command(name: str, command: list[str], result: ValidationResult) -> None:
    if shutil.which(command[0]) is None:
        result.smoke_tests[name] = "skipped: not found"
        return
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"{name} smoke test failed to run: {exc}")
        return
    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        detail = stderr[-1] if stderr else f"exit code {completed.returncode}"
        result.errors.append(f"{name} smoke test failed: {detail}")
    else:
        result.smoke_tests[name] = "ok"


def _tag_value(page: tifffile.TiffPage, code: int):
    tag = page.tags.get(code)
    return None if tag is None else tag.value


def _as_tuple(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _expand_to_three(values: list[int]) -> tuple[int, int, int]:
    if len(values) == 1:
        return (values[0], values[0], values[0])
    return tuple(values[:3])  # type: ignore[return-value]
