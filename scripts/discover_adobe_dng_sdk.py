from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from image2dng import __version__

REPORT_SCHEMA = "image2dng.adobe_dng_sdk_discovery.v1"
ENV_KEYS = ("ADOBE_DNG_SDK_ROOT", "DNG_SDK_ROOT", "DNG_VALIDATE")
EXECUTABLE_NAMES = ("dng_validate.exe", "dng_validate")
OFFICIAL_DNG_PAGE = "https://helpx.adobe.com/camera-raw/digital-negative.html"
OFFICIAL_DNG_SDK_URL = "https://www.adobe.com/go/dng_sdk"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover a local Adobe DNG SDK validator without downloading by default."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/adobe-dng-sdk-discovery"),
    )
    parser.add_argument(
        "--download-if-missing",
        action="store_true",
        help="when no local SDK validator is found, download the SDK from Adobe's official URL",
    )
    parser.add_argument(
        "--download-url",
        default=OFFICIAL_DNG_SDK_URL,
        help="official Adobe SDK download URL; defaults to https://www.adobe.com/go/dng_sdk",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=None,
        help="local-only download directory; defaults under the output directory",
    )
    parser.add_argument(
        "--download-timeout-seconds",
        type=float,
        default=600.0,
        help="network timeout for explicit official SDK downloads",
    )
    parser.add_argument(
        "--extract-if-downloaded",
        action="store_true",
        help="extract downloaded ZIP payloads into a local-only SDK directory",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=None,
        help="local-only extraction directory; defaults under the output directory",
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing local discovery reports outside demo-output/",
    )
    args = parser.parse_args(argv)

    download_dir = args.download_dir or (args.output_dir / "downloads")
    install_dir = args.install_dir or (args.output_dir / "sdk")
    local_dirs = [args.output_dir]
    if args.download_if_missing:
        local_dirs.append(download_dir)
    if args.extract_if_downloaded:
        local_dirs.append(install_dir)

    if not _local_dirs_are_allowed(local_dirs) and not args.allow_output_outside_demo_output:
        print(
            "Refusing to write Adobe DNG SDK reports outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override.",
            flush=True,
        )
        return 2

    report = _discovery_report(
        download_if_missing=args.download_if_missing,
        download_url=args.download_url,
        download_dir=download_dir,
        extract_if_downloaded=args.extract_if_downloaded,
        install_dir=install_dir,
        download_timeout_seconds=args.download_timeout_seconds,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "discovery-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote Adobe DNG SDK discovery report to {report_path}")
    return 0


def _discovery_report(
    *,
    download_if_missing: bool = False,
    download_url: str = OFFICIAL_DNG_SDK_URL,
    download_dir: Path | None = None,
    extract_if_downloaded: bool = False,
    install_dir: Path | None = None,
    download_timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    candidates = _candidate_records()
    available = [candidate for candidate in candidates if candidate["result"] == "available"]
    download: dict[str, Any] | None = None

    if not available and download_if_missing:
        try:
            download = _download_official_sdk(
                download_url=download_url,
                download_dir=download_dir or Path("demo-output/adobe-dng-sdk-discovery/downloads"),
                extract_if_downloaded=extract_if_downloaded,
                install_dir=install_dir,
                timeout_seconds=download_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - report the failed external download cleanly.
            download = {
                "requested": True,
                "status": "failed",
                "source_url": download_url,
                "official_source_page": OFFICIAL_DNG_PAGE,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "candidates": [],
            }
        candidates.extend(download.get("candidates", []))
        available = [candidate for candidate in candidates if candidate["result"] == "available"]

    result = "available" if available else "skipped"
    if not available and download and download.get("status") == "failed":
        result = "download-failed"

    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "local_research_only": True,
        "policy": {
            "downloads_or_installs": "allowed only when --download-if-missing is explicit",
            "ci_gate": "optional",
            "missing_sdk_result": "skipped",
            "official_source_page": OFFICIAL_DNG_PAGE,
            "official_download_url": OFFICIAL_DNG_SDK_URL,
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "result": result,
        "selected_validator": available[0]["path"] if available else None,
        "candidates": candidates,
        "download": download,
    }


def _candidate_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key in ENV_KEYS:
        value = os.environ.get(key)
        if not value:
            records.append(
                {
                    "source": f"env:{key}",
                    "path": None,
                    "result": "skipped",
                    "notes": "environment variable is not set",
                }
            )
            continue
        records.extend(_records_from_env(key, Path(value)))

    for name in EXECUTABLE_NAMES:
        found = shutil.which(name)
        records.append(
            {
                "source": f"path:{name}",
                "path": found,
                "result": "available" if found else "skipped",
                "notes": "found on PATH" if found else "not found on PATH",
            }
        )
    return records


def _records_from_env(key: str, value: Path) -> list[dict[str, Any]]:
    if key == "DNG_VALIDATE":
        return [_record_for_candidate(f"env:{key}", value)]
    if not value.exists():
        return [
            {
                "source": f"env:{key}",
                "path": str(value),
                "result": "skipped",
                "notes": "configured SDK root does not exist",
            }
        ]
    candidates = (
        [value / executable for executable in EXECUTABLE_NAMES]
        + [value / "bin" / executable for executable in EXECUTABLE_NAMES]
        + [value / "build" / executable for executable in EXECUTABLE_NAMES]
        + [value / "x64" / "Release" / executable for executable in EXECUTABLE_NAMES]
    )
    return [_record_for_candidate(f"env:{key}", candidate) for candidate in candidates]


def _record_for_candidate(source: str, path: Path) -> dict[str, Any]:
    exists = path.exists() and path.is_file()
    return {
        "source": source,
        "path": str(path),
        "result": "available" if exists else "skipped",
        "notes": "candidate executable exists" if exists else "candidate executable not found",
    }


def _download_official_sdk(
    *,
    download_url: str,
    download_dir: Path,
    extract_if_downloaded: bool,
    install_dir: Path | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    _validate_official_adobe_url(download_url)
    download_dir.mkdir(parents=True, exist_ok=True)
    target = _download_file(download_url, download_dir, timeout_seconds=timeout_seconds)
    result: dict[str, Any] = {
        "requested": True,
        "status": "downloaded",
        "source_url": download_url,
        "downloaded_path": str(target),
        "sha256": _sha256_file(target),
        "official_source_page": OFFICIAL_DNG_PAGE,
        "candidates": _records_from_directory(download_dir, "download-dir"),
    }
    if extract_if_downloaded:
        if zipfile.is_zipfile(target):
            sdk_dir = install_dir or (download_dir.parent / "sdk")
            sdk_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target) as archive:
                archive.extractall(sdk_dir)
            result["extracted_to"] = str(sdk_dir)
            result["candidates"].extend(_records_from_directory(sdk_dir, "extracted-sdk"))
        else:
            result["extract_status"] = "skipped: downloaded file is not a zip archive"
    return result


def _download_file(url: str, download_dir: Path, *, timeout_seconds: float) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": "image2dng-sdk-discovery/0.1"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        filename = _download_filename(url, response.headers.get("Content-Disposition"))
        target = download_dir / filename
        partial = target.with_name(f"{target.name}.part")
        try:
            with partial.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            partial.replace(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
    return target


def _download_filename(url: str, content_disposition: str | None) -> str:
    if content_disposition:
        match = re.search(r'filename="?([^";]+)"?', content_disposition)
        if match:
            return Path(match.group(1)).name
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    if name and "." in name:
        return name
    return "adobe-dng-sdk-download.bin"


def _validate_official_adobe_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if parsed.scheme != "https" or not (host == "adobe.com" or host.endswith(".adobe.com")):
        raise ValueError("Adobe DNG SDK downloads must use an official https adobe.com URL")


def _records_from_directory(root: Path, source: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for executable in EXECUTABLE_NAMES:
        for path in root.rglob(executable):
            records.append(_record_for_candidate(source, path))
    return records


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _local_dirs_are_allowed(paths: list[Path]) -> bool:
    return all(_output_dir_is_allowed(path) for path in paths)


def _output_dir_is_allowed(output_dir: Path) -> bool:
    repo_root = Path(__file__).resolve().parents[1]
    demo_output = (repo_root / "demo-output").resolve()
    resolved = output_dir.resolve()
    return resolved == demo_output or demo_output in resolved.parents


if __name__ == "__main__":
    raise SystemExit(main())
