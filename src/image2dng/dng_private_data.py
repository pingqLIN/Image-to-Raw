from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from image2dng import __version__

DNG_PRIVATE_DATA_SCHEMA = "image2dng.dng_private_data.v1"
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class DngPrivateDataError(ValueError):
    """Raised when a private-data payload would violate the safety contract."""


def build_dng_private_data_payload(
    *,
    semantic_scene_path: str | Path | None = None,
    producer_metadata_path: str | Path | None = None,
) -> bytes:
    """Build canonical private-data JSON bytes without writing them into a DNG."""

    if semantic_scene_path is None and producer_metadata_path is None:
        raise DngPrivateDataError("at least one sidecar path is required")

    payload: dict[str, Any] = {
        "schema": DNG_PRIVATE_DATA_SCHEMA,
        "producer": "image2dng",
        "image2dng_version": __version__,
        "privacy": {
            "contains_absolute_paths": False,
            "contains_source_pixels": False,
            "contains_private_metadata": False,
        },
    }

    if semantic_scene_path is not None:
        payload["semantic_scene"] = _sidecar_record(
            Path(semantic_scene_path),
            expected_schema="image2dng.semantic_scene.v1",
        )
    if producer_metadata_path is not None:
        payload["producer_metadata"] = _sidecar_record(
            Path(producer_metadata_path),
            expected_schema=None,
        )

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sidecar_record(path: Path, *, expected_schema: str | None) -> dict[str, Any]:
    data = _read_json(path)
    schema = data.get("schema")
    if expected_schema is not None and schema != expected_schema:
        raise DngPrivateDataError(f"unsupported sidecar schema: {schema!r}")
    _reject_absolute_paths(data)
    return {
        "schema": schema if isinstance(schema, str) else None,
        "sha256": _sha256_file(path),
        "artifact_role": "sidecar",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DngPrivateDataError(f"cannot read sidecar: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DngPrivateDataError(f"invalid sidecar JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise DngPrivateDataError("sidecar must be a JSON object")
    return payload


def _reject_absolute_paths(value: Any) -> None:
    for item in _walk_values(value):
        if isinstance(item, str) and _looks_like_absolute_path(item):
            raise DngPrivateDataError("sidecar contains an absolute path")


def _walk_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        items: list[Any] = []
        for key, child in value.items():
            items.append(key)
            items.extend(_walk_values(child))
        return items
    if isinstance(value, list):
        items = []
        for child in value:
            items.extend(_walk_values(child))
        return items
    return [value]


def _looks_like_absolute_path(value: str) -> bool:
    return (
        bool(_WINDOWS_ABSOLUTE_PATH.match(value))
        or value.startswith("\\\\")
        or value.startswith("//")
        or value.startswith("/")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
