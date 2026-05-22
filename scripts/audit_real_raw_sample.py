from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import tifffile

from image2dng import __version__
from image2dng.compatibility import resolve_processor_executable

REPORT_SCHEMA = "image2dng.real_raw_sample_research.v1"
TRI_PRESENT: Literal["present"] = "present"
TRI_ABSENT: Literal["absent"] = "absent"
TRI_UNKNOWN: Literal["unknown"] = "unknown"
TriState = Literal["present", "absent", "unknown"]

RAW_FORMATS = {
    ".arw": "arw",
    ".cr2": "cr2",
    ".cr3": "cr3",
    ".dng": "dng",
    ".nef": "nef",
    ".orf": "orf",
    ".raf": "raf",
    ".rw2": "rw2",
    ".tif": "tiff",
    ".tiff": "tiff",
}

TIFF_COMPATIBLE_FORMATS = {"dng", "tiff"}
GPS_TAG_CODES = {34853}
OWNER_TAG_NAMES = {"Artist", "Copyright", "ImageDescription", "OwnerName", "UserComment"}
CAMERA_SERIAL_TAG_NAMES = {
    "BodySerialNumber",
    "CameraSerialNumber",
    "InternalSerialNumber",
    "SerialNumber",
}
LENS_SERIAL_TAG_NAMES = {"LensSerialNumber"}
GPS_TAG_NAMES = {
    "GPSAltitude",
    "GPSCoordinates",
    "GPSDateStamp",
    "GPSLatitude",
    "GPSLongitude",
    "GPSPosition",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create local-only audit reports for a real RAW sample."
    )
    parser.add_argument("--input", type=Path, required=True, help="real RAW sample to inspect")
    parser.add_argument("--sample-id", required=True, help="stable local identifier for the sample")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="local-only report directory; defaults are expected under demo-output/",
    )
    parser.add_argument("--source-name", default=None)
    parser.add_argument("--source-url", default=None)
    parser.add_argument(
        "--license-summary",
        default="local research only; redistribution not approved",
    )
    parser.add_argument(
        "--redistribution-allowed",
        action="store_true",
        help="mark source as redistribution-approved in the local ledger",
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing local research reports outside demo-output/",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=20,
        help="timeout for optional metadata tools",
    )
    args = parser.parse_args(argv)

    input_path = args.input
    if not input_path.exists():
        print(f"Input sample does not exist: {input_path}", flush=True)
        return 2
    if not input_path.is_file():
        print(f"Input sample is not a file: {input_path}", flush=True)
        return 2

    output_dir = args.output_dir
    if not _output_dir_is_allowed(output_dir) and not args.allow_output_outside_demo_output:
        print(
            "Refusing to write RAW sample research reports outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override.",
            flush=True,
        )
        return 2
    if _output_dir_overlaps_input(output_dir, input_path):
        print(
            "Refusing to write reports into the input sample directory or below it.",
            flush=True,
        )
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    report_bundle = build_reports(
        input_path=input_path,
        output_dir=output_dir,
        sample_id=args.sample_id,
        source_name=args.source_name,
        source_url=args.source_url,
        license_summary=args.license_summary,
        redistribution_allowed=args.redistribution_allowed,
        timeout_seconds=args.timeout_seconds,
    )
    paths = _write_reports(output_dir, report_bundle)

    print(f"Wrote metadata summary to {paths['metadata_summary']}")
    print(f"Wrote redaction report to {paths['redaction_report']}")
    print(f"Wrote research ledger entry to {paths['research_ledger_entry']}")
    return 0


def build_reports(
    *,
    input_path: Path,
    output_dir: Path,
    sample_id: str,
    source_name: str | None,
    source_url: str | None,
    license_summary: str,
    redistribution_allowed: bool,
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    generated_at = datetime.now(UTC).isoformat()
    input_sha256 = _sha256_file(input_path)
    raw_format = infer_raw_format(input_path)
    tiff_metadata = _inspect_tiff_metadata(input_path, raw_format)
    exiftool = _run_exiftool(input_path, timeout_seconds)
    detected_metadata = _detected_metadata(tiff_metadata, exiftool)
    redaction = _redaction_report(
        sample_id=sample_id,
        input_path=input_path,
        input_sha256=input_sha256,
        raw_format=raw_format,
        tiff_metadata=tiff_metadata,
        exiftool=exiftool,
    )

    metadata_summary = {
        "schema": REPORT_SCHEMA,
        "report": "metadata-summary",
        "generated_at": generated_at,
        "environment": _environment(),
        "sample_id": sample_id,
        "input_path": str(input_path),
        "input_sha256": input_sha256,
        "raw_format": raw_format,
        "detected_metadata": detected_metadata,
        "optional_tools": {
            "exiftool": _tool_summary(exiftool),
        },
        "notes": [
            "This report records metadata only; it does not copy or transform the RAW sample.",
            (
                "Keep this output in local-only ignored paths unless manually redacted "
                "for publication."
            ),
        ],
    }
    ledger_entry = {
        "schema": REPORT_SCHEMA,
        "report": "research-ledger-entry",
        "generated_at": generated_at,
        "sample_id": sample_id,
        "source": {
            "name": source_name,
            "url": source_url,
            "license_summary": license_summary,
            "redistribution_allowed": redistribution_allowed,
        },
        "local_research_only": True,
        "input_path": str(input_path),
        "input_sha256": input_sha256,
        "raw_format": raw_format,
        "metadata_summary_path": str(output_dir / "metadata-summary.json"),
        "redaction_report_path": str(output_dir / "redaction-report.json"),
        "redaction_status": _redaction_status(redaction),
        "detected_metadata": detected_metadata,
        "optional_tools": {
            "exiftool": _tool_summary(exiftool),
        },
        "next_actions": _next_actions(redaction, redistribution_allowed),
    }
    return {
        "metadata_summary": metadata_summary,
        "redaction_report": redaction,
        "research_ledger_entry": ledger_entry,
    }


def infer_raw_format(path: Path) -> str:
    return RAW_FORMATS.get(path.suffix.lower(), "unknown")


def _inspect_tiff_metadata(path: Path, raw_format: str) -> dict[str, Any]:
    if raw_format not in TIFF_COMPATIBLE_FORMATS:
        return {
            "status": "skipped",
            "warnings": [f"{raw_format} is not TIFF/DNG-compatible for tifffile inspection"],
            "tags": {},
            "preview_ifd_present": None,
            "raw_ifd_present": None,
            "subifd_present": None,
        }

    try:
        with tifffile.TiffFile(path) as tif:
            pages = _all_tiff_pages(tif)
            page_tags = [_page_tags(page) for page in pages]
            all_tags = _merge_first_values(page_tags)
            return {
                "status": "passed",
                "warnings": [],
                "tags": all_tags,
                "preview_ifd_present": _preview_ifd_present(page_tags),
                "raw_ifd_present": _raw_ifd_present(page_tags),
                "subifd_present": any("SubIFDs" in tags for tags in page_tags),
                "ifd_count": len(page_tags),
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "warnings": [f"tifffile metadata inspection failed: {exc}"],
            "tags": {},
            "preview_ifd_present": None,
            "raw_ifd_present": None,
            "subifd_present": None,
        }


def _page_tags(page: tifffile.TiffPage) -> dict[str, Any]:
    tags: dict[str, Any] = {}
    for tag in page.tags.values():
        name = tag.name or str(tag.code)
        tags[name] = _jsonable_tag_value(tag.value)
        tags[str(tag.code)] = _jsonable_tag_value(tag.value)
    return tags


def _all_tiff_pages(tif: tifffile.TiffFile) -> list[tifffile.TiffPage]:
    pages: list[tifffile.TiffPage] = []
    pending = list(tif.pages)
    while pending:
        page = pending.pop(0)
        pages.append(page)
        child_pages = getattr(page, "pages", None)
        if child_pages is not None:
            pending.extend(list(child_pages))
    return pages


def _jsonable_tag_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, tuple):
        return [_jsonable_tag_value(item) for item in value]
    if isinstance(value, list):
        return [_jsonable_tag_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable_tag_value(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _merge_first_values(page_tags: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for tags in page_tags:
        for key, value in tags.items():
            merged.setdefault(key, value)
    return merged


def _preview_ifd_present(page_tags: list[dict[str, Any]]) -> bool:
    return any(
        _tag_int(tags, "NewSubfileType") not in {None, 0}
        or _tag_int(tags, "254") not in {None, 0}
        for tags in page_tags
    )


def _raw_ifd_present(page_tags: list[dict[str, Any]]) -> bool:
    return any(
        _tag_int(tags, "NewSubfileType") == 0 or _tag_int(tags, "254") == 0
        for tags in page_tags
    )


def _tag_int(tags: dict[str, Any], name: str) -> int | None:
    value = tags.get(name)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if (
        isinstance(value, list)
        and value
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
    ):
        return value[0]
    return None


def _run_exiftool(path: Path, timeout_seconds: int) -> dict[str, Any]:
    executable, discovery = resolve_processor_executable("exiftool")
    command = ["exiftool", "-json", str(path)]
    if executable is None:
        return {
            "available": False,
            "discovery": discovery,
            "command": command,
            "exit_code": None,
            "duration_seconds": 0.0,
            "result": "skipped",
            "metadata": {},
            "stdout_tail": [],
            "stderr_tail": [],
            "notes": "skipped: not found",
        }

    command = [executable, "-json", str(path)]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "available": True,
            "discovery": discovery,
            "command": command,
            "exit_code": None,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "result": "failed",
            "metadata": {},
            "stdout_tail": [],
            "stderr_tail": [],
            "notes": f"failed to run: {exc}",
        }

    metadata = _parse_exiftool_json(completed.stdout) if completed.returncode == 0 else {}
    result = "passed" if completed.returncode == 0 else "failed"
    return {
        "available": True,
        "discovery": discovery,
        "command": command,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "result": result,
        "metadata": metadata,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
        "notes": "exiftool metadata read completed"
        if result == "passed"
        else (_last_line(_tail(completed.stderr)) or f"exit code {completed.returncode}"),
    }


def _parse_exiftool_json(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return {}
    return {str(key): _jsonable_tag_value(value) for key, value in payload[0].items()}


def _detected_metadata(
    tiff_metadata: dict[str, Any],
    exiftool: dict[str, Any],
) -> dict[str, Any]:
    tags = _tiff_tags(tiff_metadata)
    exif = _exiftool_metadata(exiftool)
    return {
        "camera_make": _first_present(exif, tags, keys=("Make", "271")),
        "camera_model": _first_present(exif, tags, keys=("Model", "272")),
        "cfa_pattern": _first_present(exif, tags, keys=("CFAPattern", "CFA Pattern", "33422")),
        "black_level": _first_present(exif, tags, keys=("BlackLevel", "Black Level", "50714")),
        "white_level": _first_present(exif, tags, keys=("WhiteLevel", "White Level", "50717")),
        "preview_ifd_present": _tiff_preview_ifd_present(tiff_metadata),
        "raw_ifd_present": _tiff_raw_ifd_present(tiff_metadata),
        "subifd_present": _tiff_subifd_present(tiff_metadata),
        "metadata_reader_status": {
            "tifffile": _tiff_status(tiff_metadata),
            "exiftool": _exiftool_result(exiftool),
        },
    }


def _redaction_report(
    *,
    sample_id: str,
    input_path: Path,
    input_sha256: str,
    raw_format: str,
    tiff_metadata: dict[str, Any],
    exiftool: dict[str, Any],
) -> dict[str, Any]:
    tags = _tiff_tags(tiff_metadata)
    exif = _exiftool_metadata(exiftool)
    reader_known = _tiff_status(tiff_metadata) == "passed" or _exiftool_result(exiftool) == "passed"
    fields = {
        "gps_present": _field_presence(
            tags, exif, GPS_TAG_NAMES, code_names=GPS_TAG_CODES, known=reader_known
        ),
        "owner_or_artist_present": _field_presence(tags, exif, OWNER_TAG_NAMES, known=reader_known),
        "camera_serial_present": _field_presence(
            tags, exif, CAMERA_SERIAL_TAG_NAMES, known=reader_known
        ),
        "lens_serial_present": _field_presence(
            tags, exif, LENS_SERIAL_TAG_NAMES, known=reader_known
        ),
        "embedded_preview_present": _preview_presence(tiff_metadata, exiftool),
    }
    warnings = list(_tiff_warnings(tiff_metadata))
    if raw_format not in TIFF_COMPATIBLE_FORMATS and _exiftool_result(exiftool) != "passed":
        warnings.append(
            "Proprietary RAW metadata needs ExifTool or a format-specific reader for redaction."
        )
    if any(value == TRI_PRESENT for value in fields.values()):
        status = "needs-redaction"
    elif any(value == TRI_UNKNOWN for value in fields.values()):
        status = "needs-manual-review"
    else:
        status = "no-sensitive-fields-detected"

    return {
        "schema": REPORT_SCHEMA,
        "report": "redaction-report",
        "generated_at": datetime.now(UTC).isoformat(),
        "sample_id": sample_id,
        "input_path": str(input_path),
        "input_sha256": input_sha256,
        "raw_format": raw_format,
        "status": status,
        "fields": fields,
        "warnings": warnings,
        "policy": {
            "public_release_allowed_by_this_report": False,
            "tri_state_values": [TRI_PRESENT, TRI_ABSENT, TRI_UNKNOWN],
        },
    }


def _field_presence(
    tags: dict[str, Any],
    exif: dict[str, Any],
    names: tuple[str, ...] | set[str],
    *,
    code_names: set[int] | None = None,
    known: bool,
) -> TriState:
    code_names = code_names or set()
    candidate_names = set(names) | {str(code) for code in code_names}
    if any(_has_value(tags, name) or _has_value(exif, name) for name in candidate_names):
        return TRI_PRESENT
    if known:
        return TRI_ABSENT
    return TRI_UNKNOWN


def _preview_presence(tiff_metadata: dict[str, Any], exiftool: dict[str, Any]) -> TriState:
    preview = _tiff_preview_ifd_present(tiff_metadata)
    if isinstance(preview, bool):
        return TRI_PRESENT if preview else TRI_ABSENT
    if _exiftool_result(exiftool) == "passed":
        exif = _exiftool_metadata(exiftool)
        if any(
            _has_value(exif, name)
            for name in ("JpgFromRaw", "PreviewImage", "PreviewTIFF", "ThumbnailImage")
        ):
            return TRI_PRESENT
        return TRI_ABSENT
    return TRI_UNKNOWN


def _tiff_tags(tiff_metadata: dict[str, Any]) -> dict[str, Any]:
    tags = tiff_metadata["tags"]
    if not isinstance(tags, dict):
        raise TypeError("tifffile metadata tags must be an object")
    return tags


def _tiff_status(tiff_metadata: dict[str, Any]) -> str:
    status = tiff_metadata["status"]
    if not isinstance(status, str):
        raise TypeError("tifffile metadata status must be a string")
    return status


def _tiff_warnings(tiff_metadata: dict[str, Any]) -> list[str]:
    warnings = tiff_metadata["warnings"]
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise TypeError("tifffile metadata warnings must be a string list")
    return warnings


def _tiff_preview_ifd_present(tiff_metadata: dict[str, Any]) -> bool | None:
    return _optional_bool(tiff_metadata["preview_ifd_present"], "preview_ifd_present")


def _tiff_raw_ifd_present(tiff_metadata: dict[str, Any]) -> bool | None:
    return _optional_bool(tiff_metadata["raw_ifd_present"], "raw_ifd_present")


def _tiff_subifd_present(tiff_metadata: dict[str, Any]) -> bool | None:
    return _optional_bool(tiff_metadata["subifd_present"], "subifd_present")


def _exiftool_metadata(exiftool: dict[str, Any]) -> dict[str, Any]:
    metadata = exiftool["metadata"]
    if not isinstance(metadata, dict):
        raise TypeError("exiftool metadata must be an object")
    return metadata


def _exiftool_result(exiftool: dict[str, Any]) -> str:
    result = exiftool["result"]
    if not isinstance(result, str):
        raise TypeError("exiftool result must be a string")
    return result


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if isinstance(value, bool) or value is None:
        return value
    raise TypeError(f"tifffile metadata {field_name} must be a boolean or null")


def _tool_summary(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": _tool_available(tool),
        "discovery": _tool_discovery(tool),
        "command": _tool_command(tool),
        "exit_code": _tool_exit_code(tool),
        "duration_seconds": _tool_duration_seconds(tool),
        "result": _tool_result(tool),
        "stdout_tail": _tool_tail(tool, "stdout_tail"),
        "stderr_tail": _tool_tail(tool, "stderr_tail"),
        "notes": _tool_notes(tool),
    }


def _tool_available(tool: dict[str, Any]) -> bool:
    available = tool["available"]
    if not isinstance(available, bool):
        raise TypeError("tool available must be a boolean")
    return available


def _tool_discovery(tool: dict[str, Any]) -> str | None:
    discovery = tool["discovery"]
    if isinstance(discovery, str) or discovery is None:
        return discovery
    raise TypeError("tool discovery must be a string or null")


def _tool_command(tool: dict[str, Any]) -> list[str]:
    command = tool["command"]
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise TypeError("tool command must be a string list")
    return command


def _tool_exit_code(tool: dict[str, Any]) -> int | None:
    exit_code = tool["exit_code"]
    if (isinstance(exit_code, int) and not isinstance(exit_code, bool)) or exit_code is None:
        return exit_code
    raise TypeError("tool exit_code must be an integer or null")


def _tool_duration_seconds(tool: dict[str, Any]) -> float:
    duration = tool["duration_seconds"]
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        return float(duration)
    raise TypeError("tool duration_seconds must be numeric")


def _tool_result(tool: dict[str, Any]) -> str:
    result = tool["result"]
    if not isinstance(result, str):
        raise TypeError("tool result must be a string")
    return result


def _tool_tail(tool: dict[str, Any], field_name: str) -> list[str]:
    tail = tool[field_name]
    if not isinstance(tail, list) or not all(isinstance(item, str) for item in tail):
        raise TypeError(f"tool {field_name} must be a string list")
    return tail


def _tool_notes(tool: dict[str, Any]) -> str:
    notes = tool["notes"]
    if not isinstance(notes, str):
        raise TypeError("tool notes must be a string")
    return notes


def _redaction_status(redaction: dict[str, Any]) -> str:
    status = redaction["status"]
    if not isinstance(status, str):
        raise TypeError("redaction status must be a string")
    return status


def _has_value(mapping: dict[str, Any], key: str) -> bool:
    value = mapping.get(key)
    return value not in (None, "", [], {})


def _first_present(*mappings: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for mapping in mappings:
        for key in keys:
            if _has_value(mapping, key):
                return mapping[key]
    return None


def _next_actions(redaction: dict[str, Any], redistribution_allowed: bool) -> list[str]:
    actions = []
    if _redaction_status(redaction) != "no-sensitive-fields-detected":
        actions.append(
            "complete manual redaction review before using this sample in public evidence"
        )
    if not redistribution_allowed:
        actions.append("keep sample and reports local-only; redistribution is not approved")
    if not actions:
        actions.append(
            "sample may proceed to local compatibility comparison after manual confirmation"
        )
    return actions


def _write_reports(output_dir: Path, reports: dict[str, dict[str, Any]]) -> dict[str, Path]:
    paths = {
        "metadata_summary": output_dir / "metadata-summary.json",
        "redaction_report": output_dir / "redaction-report.json",
        "research_ledger_entry": output_dir / "research-ledger-entry.json",
    }
    for key, path in paths.items():
        path.write_text(json.dumps(reports[key], indent=2, sort_keys=True), encoding="utf-8")
    return paths


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _output_dir_is_allowed(output_dir: Path) -> bool:
    repo_root = Path(__file__).resolve().parents[1]
    demo_output = (repo_root / "demo-output").resolve()
    resolved = output_dir.resolve()
    return resolved == demo_output or demo_output in resolved.parents


def _output_dir_overlaps_input(output_dir: Path, input_path: Path) -> bool:
    resolved_output = output_dir.resolve()
    resolved_input_parent = input_path.resolve().parent
    return (
        resolved_output == resolved_input_parent
        or resolved_input_parent in resolved_output.parents
    )


def _environment() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "image2dng_version": __version__,
    }


def _tail(text: str, *, max_lines: int = 20) -> list[str]:
    lines = text.strip().splitlines()
    return lines[-max_lines:]


def _last_line(lines: list[str]) -> str:
    return lines[-1] if lines else ""


if __name__ == "__main__":
    raise SystemExit(main())
