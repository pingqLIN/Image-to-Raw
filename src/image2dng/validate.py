from __future__ import annotations

import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import tifffile

from image2dng.compatibility import resolve_processor_executable
from image2dng.dng_writer import (
    TAG_ACTIVE_AREA,
    TAG_AS_SHOT_NEUTRAL,
    TAG_BLACK_LEVEL,
    TAG_CALIBRATION_ILLUMINANT_1,
    TAG_CFA_PATTERN,
    TAG_CFA_PLANE_COLOR,
    TAG_CFA_REPEAT_PATTERN_DIM,
    TAG_COLOR_MATRIX_1,
    TAG_DEFAULT_CROP_ORIGIN,
    TAG_DEFAULT_CROP_SIZE,
    TAG_DEFAULT_SCALE,
    TAG_DNG_BACKWARD_VERSION,
    TAG_DNG_VERSION,
    TAG_MAKE,
    TAG_MODEL,
    TAG_NEW_SUBFILE_TYPE,
    TAG_RAW_DATA_UNIQUE_ID,
    TAG_UNIQUE_CAMERA_MODEL,
    TAG_WHITE_LEVEL,
    TAG_XMP,
)
from image2dng.models import (
    PHOTOMETRIC_CFA,
    PHOTOMETRIC_LINEAR_RAW,
    SYNTHETIC_CAMERA_MAKE,
    SYNTHETIC_CAMERA_MODEL,
)
from image2dng.xmp import XMP_AI_NAMESPACE

TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_IMAGE_LENGTH = 257
TAG_IMAGE_WIDTH = 256
TAG_MAKER_NOTE = 37500
TAG_ORIENTATION = 274
TAG_PHOTOMETRIC = 262
TAG_SAMPLE_FORMAT = 339
TAG_SAMPLES_PER_PIXEL = 277
TAG_SOFTWARE = 305
TAG_SUB_IFDS = 330
COMPRESSION_JPEG = 7

CheckStatus = Literal["passed", "failed", "skipped", "warning"]


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    status: CheckStatus
    message: str = ""
    tool: str | None = None

    def to_dict(self) -> dict[str, str]:
        item = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.tool is not None:
            item["tool"] = self.tool
        return item


@dataclass
class ValidationResult:
    path: Path
    dng_layout: str | None = None
    ifd0_preview: bool | None = None
    raw_ifd_location: str | None = None
    embedded_preview_compression: str | None = None
    raw_photometric: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[ValidationCheck] = field(default_factory=list)
    smoke_tests: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def has_smoke_failure(self) -> bool:
        return any(
            check.name.startswith("smoke:") and check.status == "failed"
            for check in self.checks
        )

    def add_check(
        self,
        name: str,
        status: CheckStatus,
        message: str = "",
        *,
        tool: str | None = None,
    ) -> None:
        self.checks.append(ValidationCheck(name=name, status=status, message=message, tool=tool))
        if status == "failed":
            self.errors.append(message or f"{name} failed")
        elif status == "warning":
            self.warnings.append(message or f"{name} warning")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "ok": self.ok,
            "dng_layout": self.dng_layout,
            "ifd0_preview": self.ifd0_preview,
            "raw_ifd_location": self.raw_ifd_location,
            "embedded_preview_compression": self.embedded_preview_compression,
            "raw_photometric": self.raw_photometric,
            "checks": [check.to_dict() for check in self.checks],
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
            page, raw_location = _find_raw_image_page_with_location(tif)
            if page is None:
                result.errors.append("no main raw image IFD found")
                return result
            _record_layout_summary(tif, page, raw_location, result)
            _check_embedded_preview_layout(tif, page, result)
            _check_required_tags(page, result)
            _check_identity_tags(page, result)
            _check_geometry(page, result)
            _check_raw_area_tags(page, result)
            _check_levels(page, result)
            _check_camera_profile_tags(page, result)
            _check_cfa_tags(page, result)
            _check_xmp(page, result)
            _check_makernote(page, result)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"failed to parse DNG: {exc}")
        return result

    if not result.errors:
        result.add_check("structure", "passed", "DNG structural checks passed")

    if run_smoke:
        _run_external_smoke_tests(target, result)
    return result


def inspect_adobe_converted_dng(
    path: str | Path,
    *,
    run_smoke: bool = True,
) -> ValidationResult:
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
            page, raw_location = _find_raw_image_page_with_location(tif)
            if page is None:
                result.errors.append("no Adobe-converted raw image IFD found")
                return result
            _record_layout_summary(tif, page, raw_location, result)
            _check_adobe_converted_identity(tif, page, result)
            _check_adobe_converted_geometry(page, result)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"failed to parse Adobe-converted DNG: {exc}")
        return result

    if not result.errors:
        result.add_check(
            "adobe-converted-artifact",
            "passed",
            "Adobe-converted DNG artifact inspection passed",
        )

    if run_smoke:
        _run_external_smoke_tests(target, result)
    return result


def find_raw_image_page(tif: tifffile.TiffFile) -> tifffile.TiffPage | None:
    page, _location = _find_raw_image_page_with_location(tif)
    return page


def _find_raw_image_page_with_location(
    tif: tifffile.TiffFile,
) -> tuple[tifffile.TiffPage | None, str | None]:
    for page in _iter_pages(tif):
        new_subfile_type = _tag_value(page, TAG_NEW_SUBFILE_TYPE)
        if new_subfile_type is not None and int(new_subfile_type) == 0:
            return page, _page_location(tif, page)
    return None, None


def _iter_pages(tif: tifffile.TiffFile):
    for page in tif.pages:
        yield page
        yield from page.pages or ()


def _record_layout_summary(
    tif: tifffile.TiffFile,
    raw_page: tifffile.TiffPage,
    raw_location: str | None,
    result: ValidationResult,
) -> None:
    root_page = tif.pages[0]
    root_type = _tag_value(root_page, TAG_NEW_SUBFILE_TYPE)
    raw_photometric = _tag_value(raw_page, TAG_PHOTOMETRIC)
    result.raw_ifd_location = raw_location
    result.raw_photometric = _photometric_name(raw_photometric)
    if root_type is not None and int(root_type) == 0 and raw_page is root_page:
        result.dng_layout = "single-raw-ifd"
        result.ifd0_preview = False
        result.embedded_preview_compression = None
        return

    result.ifd0_preview = root_type is not None and int(root_type) == 1
    result.embedded_preview_compression = _compression_name(_tag_value(root_page, TAG_COMPRESSION))
    if result.ifd0_preview and raw_page in tuple(root_page.pages or ()):
        result.dng_layout = "preview-subifd"
    else:
        result.dng_layout = "unknown"


def _check_embedded_preview_layout(
    tif: tifffile.TiffFile,
    raw_page: tifffile.TiffPage,
    result: ValidationResult,
) -> None:
    root_page = tif.pages[0]
    root_type = _tag_value(root_page, TAG_NEW_SUBFILE_TYPE)
    if root_type is None or int(root_type) == 0:
        return
    if int(root_type) != 1:
        result.errors.append(f"IFD0 preview NewSubFileType must be 1, got {root_type}")
        return
    if TAG_SUB_IFDS not in root_page.tags:
        result.errors.append("IFD0 preview must reference the raw SubIFD")
        return
    if raw_page not in tuple(root_page.pages):
        result.errors.append("main raw image must be stored as an IFD0 SubIFD")
        return
    compression = _tag_value(root_page, TAG_COMPRESSION)
    photometric = _tag_value(root_page, TAG_PHOTOMETRIC)
    samples = _tag_value(root_page, TAG_SAMPLES_PER_PIXEL)
    if compression is not None and int(compression) != COMPRESSION_JPEG:
        result.errors.append(f"embedded preview must use JPEG compression, got {compression}")
    if photometric is not None and int(photometric) != 2:
        result.errors.append(f"embedded preview must be RGB, got photometric {photometric}")
    if samples is not None and int(samples) != 3:
        result.errors.append(f"embedded preview SamplesPerPixel must be 3, got {samples}")
    if not result.errors:
        result.add_check(
            "embedded-preview",
            "passed",
            "IFD0 JPEG preview references the main raw SubIFD",
        )


def _check_required_tags(page: tifffile.TiffPage, result: ValidationResult) -> None:
    required = {
        TAG_NEW_SUBFILE_TYPE: "NewSubFileType",
        TAG_DNG_VERSION: "DNGVersion",
        TAG_DNG_BACKWARD_VERSION: "DNGBackwardVersion",
        TAG_MAKE: "Make",
        TAG_MODEL: "Model",
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
        TAG_DEFAULT_SCALE: "DefaultScale",
        TAG_COLOR_MATRIX_1: "ColorMatrix1",
        TAG_CALIBRATION_ILLUMINANT_1: "CalibrationIlluminant1",
        TAG_AS_SHOT_NEUTRAL: "AsShotNeutral",
        TAG_RAW_DATA_UNIQUE_ID: "RawDataUniqueID",
        TAG_SOFTWARE: "Software",
        TAG_XMP: "XMP",
    }
    for code, name in required.items():
        if code not in page.tags:
            result.errors.append(f"missing required tag: {name} ({code})")

    photometric = _tag_value(page, TAG_PHOTOMETRIC)
    if photometric is not None and int(photometric) not in {
        PHOTOMETRIC_LINEAR_RAW,
        PHOTOMETRIC_CFA,
    }:
        result.errors.append(
            "PhotometricInterpretation must be LinearRaw "
            f"({PHOTOMETRIC_LINEAR_RAW}) or CFA ({PHOTOMETRIC_CFA}), got {photometric}"
        )

    new_subfile_type = _tag_value(page, TAG_NEW_SUBFILE_TYPE)
    if new_subfile_type is not None and int(new_subfile_type) != 0:
        result.errors.append(
            f"NewSubFileType must identify the main raw image, got {new_subfile_type}"
        )

    raw_data_unique_id = _as_tuple(_tag_value(page, TAG_RAW_DATA_UNIQUE_ID))
    if raw_data_unique_id and len(raw_data_unique_id) != 16:
        result.errors.append(
            f"RawDataUniqueID must contain 16 bytes, got {len(raw_data_unique_id)}"
        )


def _check_identity_tags(page: tifffile.TiffPage, result: ValidationResult) -> None:
    expectations = {
        TAG_DNG_VERSION: ("DNGVersion", (1, 4, 0, 0)),
        TAG_DNG_BACKWARD_VERSION: ("DNGBackwardVersion", (1, 1, 0, 0)),
        TAG_MAKE: ("Make", SYNTHETIC_CAMERA_MAKE),
        TAG_MODEL: ("Model", SYNTHETIC_CAMERA_MODEL),
        TAG_UNIQUE_CAMERA_MODEL: ("UniqueCameraModel", SYNTHETIC_CAMERA_MODEL),
        TAG_ORIENTATION: ("Orientation", 1),
        TAG_COMPRESSION: ("Compression", 1),
        TAG_CALIBRATION_ILLUMINANT_1: ("CalibrationIlluminant1", 21),
    }
    for tag, (name, expected) in expectations.items():
        value = _tag_value(page, tag)
        if value is None:
            continue
        normalized = _as_tuple(value) if isinstance(expected, tuple) else value
        if normalized != expected:
            result.errors.append(f"{name} must be {expected}, got {normalized}")

    software = _tag_value(page, TAG_SOFTWARE)
    if software is not None and not str(software).startswith("image2dng "):
        result.errors.append(f"Software must start with 'image2dng ', got {software}")


def _check_geometry(page: tifffile.TiffPage, result: ValidationResult) -> None:
    width = _tag_value(page, TAG_IMAGE_WIDTH)
    height = _tag_value(page, TAG_IMAGE_LENGTH)
    bits = _as_tuple(_tag_value(page, TAG_BITS_PER_SAMPLE))
    sample_format = _as_tuple(_tag_value(page, TAG_SAMPLE_FORMAT))
    samples = _tag_value(page, TAG_SAMPLES_PER_PIXEL)
    photometric = _tag_value(page, TAG_PHOTOMETRIC)
    if width is None or height is None or not bits or samples is None:
        return
    if any(int(bit) != 16 for bit in bits):
        result.errors.append(f"BitsPerSample must be 16 for MVP output, got {bits}")
    if sample_format and any(int(value) != 1 for value in sample_format):
        result.errors.append(f"SampleFormat must be unsigned integer (1), got {sample_format}")
    expected_samples = 1 if int(photometric or 0) == PHOTOMETRIC_CFA else 3
    if int(samples) != expected_samples:
        result.errors.append(
            f"SamplesPerPixel must be {expected_samples} for this output mode, got {samples}"
        )

    try:
        data = page.asarray()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"failed to read image buffer: {exc}")
        return
    expected_shape = (
        (int(height), int(width), int(samples))
        if int(samples) > 1
        else (int(height), int(width))
    )
    if data.shape != expected_shape:
        result.errors.append(f"image shape mismatch: expected {expected_shape}, got {data.shape}")

    expected_bytes = int(width) * int(height) * int(samples) * 2
    actual_bytes = sum(int(count) for count in _as_tuple(page.databytecounts))
    if actual_bytes != expected_bytes:
        result.errors.append(
            f"buffer byte count mismatch: expected {expected_bytes}, got {actual_bytes}"
        )


def _check_raw_area_tags(page: tifffile.TiffPage, result: ValidationResult) -> None:
    width = _tag_value(page, TAG_IMAGE_WIDTH)
    height = _tag_value(page, TAG_IMAGE_LENGTH)
    if width is None or height is None:
        return
    expected_active_area = (0, 0, int(height), int(width))
    expected_crop_origin = (0, 0)
    expected_crop_size = (int(width), int(height))

    default_scale = _rational_tag_values(
        _tag_value(page, TAG_DEFAULT_SCALE),
        "DefaultScale",
        result,
    )
    if default_scale and len(default_scale) != 2:
        result.errors.append(
            f"DefaultScale must contain 2 rational values, got {len(default_scale)}"
        )
    elif default_scale and any(value != 1.0 for value in default_scale):
        result.errors.append(f"DefaultScale must be 1/1, 1/1, got {default_scale}")

    active_area = _as_tuple(_tag_value(page, TAG_ACTIVE_AREA))
    if active_area and tuple(int(value) for value in active_area) != expected_active_area:
        result.errors.append(
            f"ActiveArea must be {expected_active_area}, got {active_area}"
        )

    crop_origin = _as_tuple(_tag_value(page, TAG_DEFAULT_CROP_ORIGIN))
    if crop_origin and tuple(int(value) for value in crop_origin) != expected_crop_origin:
        result.errors.append(
            f"DefaultCropOrigin must be {expected_crop_origin}, got {crop_origin}"
        )

    crop_size = _as_tuple(_tag_value(page, TAG_DEFAULT_CROP_SIZE))
    if crop_size and tuple(int(value) for value in crop_size) != expected_crop_size:
        result.errors.append(f"DefaultCropSize must be {expected_crop_size}, got {crop_size}")


def _check_levels(page: tifffile.TiffPage, result: ValidationResult) -> None:
    black_levels = [int(value) for value in _as_tuple(_tag_value(page, TAG_BLACK_LEVEL))]
    white_levels = [int(value) for value in _as_tuple(_tag_value(page, TAG_WHITE_LEVEL))]
    if not black_levels or not white_levels:
        return
    photometric = _tag_value(page, TAG_PHOTOMETRIC)
    is_cfa = photometric is not None and int(photometric) == PHOTOMETRIC_CFA
    expected_black_count = 4 if is_cfa else 3
    expected_white_count = 1 if is_cfa else 3
    if len(black_levels) != expected_black_count:
        result.errors.append(
            f"BlackLevel must contain {expected_black_count} values for this output mode, "
            f"got {len(black_levels)}"
        )
    if len(white_levels) != expected_white_count:
        result.errors.append(
            f"WhiteLevel must contain {expected_white_count} values for this output mode, "
            f"got {len(white_levels)}"
        )
    white_reference = white_levels[0]
    for black in black_levels:
        if black < 0:
            result.errors.append(f"BlackLevel must be non-negative, got {black}")
        if black >= white_reference:
            result.errors.append(
                f"BlackLevel must be less than WhiteLevel, got {black}/{white_reference}"
            )
    for white in white_levels:
        if white > 65535:
            result.errors.append(f"WhiteLevel must be <= 65535, got {white}")


def _check_camera_profile_tags(page: tifffile.TiffPage, result: ValidationResult) -> None:
    color_matrix = _rational_tag_values(
        _tag_value(page, TAG_COLOR_MATRIX_1),
        "ColorMatrix1",
        result,
    )
    if color_matrix and len(color_matrix) != 9:
        result.errors.append(
            f"ColorMatrix1 must contain 9 rational values, got {len(color_matrix)}"
        )
    as_shot_neutral = _rational_tag_values(
        _tag_value(page, TAG_AS_SHOT_NEUTRAL),
        "AsShotNeutral",
        result,
    )
    if as_shot_neutral and len(as_shot_neutral) != 3:
        result.errors.append(
            f"AsShotNeutral must contain 3 rational values, got {len(as_shot_neutral)}"
        )
    for value in as_shot_neutral:
        if value <= 0:
            result.errors.append(f"AsShotNeutral values must be positive, got {value:g}")


def _check_cfa_tags(page: tifffile.TiffPage, result: ValidationResult) -> None:
    photometric = _tag_value(page, TAG_PHOTOMETRIC)
    if photometric is None or int(photometric) != PHOTOMETRIC_CFA:
        return
    required = {
        TAG_CFA_REPEAT_PATTERN_DIM: "CFARepeatPatternDim",
        TAG_CFA_PATTERN: "CFAPattern",
        TAG_CFA_PLANE_COLOR: "CFAPlaneColor",
    }
    for code, name in required.items():
        if code not in page.tags:
            result.errors.append(f"missing CFA tag: {name} ({code})")
    repeat = _as_tuple(_tag_value(page, TAG_CFA_REPEAT_PATTERN_DIM))
    if repeat and tuple(int(value) for value in repeat) != (2, 2):
        result.errors.append(f"CFARepeatPatternDim must be 2x2, got {repeat}")
    pattern = _as_tuple(_tag_value(page, TAG_CFA_PATTERN))
    if pattern and len(pattern) != 4:
        result.errors.append(f"CFAPattern must contain 4 entries, got {len(pattern)}")


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


def _check_adobe_converted_identity(
    tif: tifffile.TiffFile,
    raw_page: tifffile.TiffPage,
    result: ValidationResult,
) -> None:
    dng_version = _first_tag_value(tif, TAG_DNG_VERSION)
    if dng_version is None:
        result.errors.append("Adobe-converted artifact is missing DNGVersion")

    camera_identity_tags = {
        TAG_MAKE: "Make",
        TAG_MODEL: "Model",
        TAG_UNIQUE_CAMERA_MODEL: "UniqueCameraModel",
    }
    for code, name in camera_identity_tags.items():
        if _first_tag_value(tif, code) is None:
            result.errors.append(f"Adobe-converted artifact is missing {name}")

    photometric = _tag_value(raw_page, TAG_PHOTOMETRIC)
    if photometric is None:
        result.errors.append("Adobe-converted raw IFD is missing PhotometricInterpretation")
    elif int(photometric) not in {PHOTOMETRIC_LINEAR_RAW, PHOTOMETRIC_CFA}:
        result.errors.append(
            "Adobe-converted raw IFD must remain LinearRaw or CFA, "
            f"got {photometric}"
        )

    raw_data_unique_id = _as_tuple(_first_tag_value(tif, TAG_RAW_DATA_UNIQUE_ID))
    if raw_data_unique_id and len(raw_data_unique_id) != 16:
        result.errors.append(
            f"Adobe-converted RawDataUniqueID must contain 16 bytes, got {len(raw_data_unique_id)}"
        )


def _check_adobe_converted_geometry(
    page: tifffile.TiffPage,
    result: ValidationResult,
) -> None:
    width = _tag_value(page, TAG_IMAGE_WIDTH)
    height = _tag_value(page, TAG_IMAGE_LENGTH)
    bits = _as_tuple(_tag_value(page, TAG_BITS_PER_SAMPLE))
    samples = _tag_value(page, TAG_SAMPLES_PER_PIXEL)
    if width is None or height is None:
        result.errors.append("Adobe-converted raw IFD is missing image dimensions")
    elif int(width) <= 0 or int(height) <= 0:
        result.errors.append(
            f"Adobe-converted raw dimensions must be positive, got {width}x{height}"
        )

    if bits and any(int(bit) <= 0 for bit in bits):
        result.errors.append(f"Adobe-converted BitsPerSample must be positive, got {bits}")
    if samples is not None and int(samples) <= 0:
        result.errors.append(f"Adobe-converted SamplesPerPixel must be positive, got {samples}")

    compression = _tag_value(page, TAG_COMPRESSION)
    if compression is not None and int(compression) != 1:
        result.add_check(
            "adobe-converted-compression",
            "warning",
            (
                "Adobe-converted raw IFD is compressed; byte-count equality is not "
                "part of artifact inspection"
            ),
        )


def _run_external_smoke_tests(path: Path, result: ValidationResult) -> None:
    smoke_specs = [
        ("exiftool", ["exiftool", path.as_posix()]),
        ("dcraw", ["dcraw", "-i", "-v", path.as_posix()]),
    ]
    for name, command in smoke_specs:
        _run_optional_command(name, command, result)

    with tempfile.TemporaryDirectory(prefix="image2dng-validate-") as temp_dir:
        temp = Path(temp_dir)
        _run_optional_command(
            "darktable-cli",
            ["darktable-cli", path.as_posix(), (temp / "darktable.tif").as_posix()],
            result,
        )
        _run_optional_command(
            "rawtherapee-cli",
            [
                "rawtherapee-cli",
                "-Y",
                "-o",
                (temp / "rawtherapee.tif").as_posix(),
                "-c",
                path.as_posix(),
            ],
            result,
        )


def _run_optional_command(name: str, command: list[str], result: ValidationResult) -> None:
    executable, _discovery = resolve_processor_executable(name)
    if executable is None:
        result.smoke_tests[name] = "skipped: not found"
        result.add_check(
            f"smoke:{name}",
            "skipped",
            f"{command[0]} was not found",
            tool=name,
        )
        return
    command = [executable, *command[1:]]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001
        result.smoke_tests[name] = f"failed: {exc}"
        result.add_check(
            f"smoke:{name}",
            "failed",
            f"{name} smoke test failed to run: {exc}",
            tool=name,
        )
        return
    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        detail = stderr[-1] if stderr else f"exit code {completed.returncode}"
        result.smoke_tests[name] = f"failed: {detail}"
        result.add_check(
            f"smoke:{name}",
            "failed",
            f"{name} smoke test failed: {detail}",
            tool=name,
        )
    else:
        result.smoke_tests[name] = "ok"
        result.add_check(f"smoke:{name}", "passed", f"{name} parsed the DNG", tool=name)


def _tag_value(page: tifffile.TiffPage, code: int):
    tag = page.tags.get(code)
    return None if tag is None else tag.value


def _first_tag_value(tif: tifffile.TiffFile, code: int):
    for page in _iter_pages(tif):
        value = _tag_value(page, code)
        if value is not None:
            return value
    return None


def _page_location(tif: tifffile.TiffFile, target: tifffile.TiffPage) -> str:
    for page_index, page in enumerate(tif.pages):
        if page is target:
            return f"IFD{page_index}"
        for subifd_index, child in enumerate(page.pages or ()):
            if child is target:
                return f"IFD{page_index}/SubIFD{subifd_index}"
    return "unknown"


def _compression_name(value) -> str | None:
    if value is None:
        return None
    code = int(value)
    if code == 1:
        return "Uncompressed"
    if code == COMPRESSION_JPEG:
        return "JPEG"
    return str(code)


def _photometric_name(value) -> str | None:
    if value is None:
        return None
    code = int(value)
    if code == PHOTOMETRIC_LINEAR_RAW:
        return "LinearRaw"
    if code == PHOTOMETRIC_CFA:
        return "ColorFilterArray"
    if code == 2:
        return "RGB"
    return str(code)


def _as_tuple(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, bytes):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _rational_tag_values(
    value,
    tag_name: str,
    result: ValidationResult,
) -> list[float]:
    raw_values = _as_tuple(value)
    if not raw_values:
        return []
    if len(raw_values) % 2:
        result.errors.append(f"{tag_name} must use numerator/denominator pairs")
        return []
    values: list[float] = []
    for numerator, denominator in zip(raw_values[::2], raw_values[1::2], strict=True):
        denominator = int(denominator)
        if denominator == 0:
            result.errors.append(f"{tag_name} denominator must not be zero")
            continue
        values.append(float(numerator) / denominator)
    return values


def _expand_to_three(values: list[int]) -> tuple[int, int, int]:
    if len(values) == 1:
        return (values[0], values[0], values[0])
    return tuple(values[:3])  # type: ignore[return-value]
