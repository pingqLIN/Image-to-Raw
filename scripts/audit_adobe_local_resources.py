from __future__ import annotations

import argparse
import json
import platform
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from image2dng import __version__

REPORT_SCHEMA = "image2dng.adobe_local_resource_audit.v1"

REQUIRED_KINDS = {
    "dng-converter-resource",
    "dng-sdk-archive",
    "dng-specification",
}

INSTALLED_CONVERTER_FILENAMES = {
    "adobe dng converter.exe",
}
CONVERTER_RESOURCE_EXTENSIONS = {".exe", ".msi"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit local Adobe DNG/Profile resources without installing, executing, "
            "or extracting them."
        )
    )
    parser.add_argument(
        "--adobe-dir",
        type=Path,
        default=Path("Adobe"),
        help="local Adobe resource cache to inspect",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/adobe-local-resource-audit"),
        help="local-only report directory; defaults under demo-output/",
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing the audit report outside demo-output/",
    )
    parser.add_argument(
        "--zip-entry-limit",
        type=int,
        default=40,
        help="maximum number of zip entries to sample per archive",
    )
    args = parser.parse_args(argv)

    if not _output_dir_is_allowed(args.output_dir) and not args.allow_output_outside_demo_output:
        print(
            "Refusing to write Adobe local resource audit outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override.",
            flush=True,
        )
        return 2

    report = build_report(args.adobe_dir, zip_entry_limit=max(0, args.zip_entry_limit))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "adobe-local-resource-report.json"
    summary_path = args.output_dir / "adobe-local-resource-summary.md"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.write_text(_summary_markdown(report), encoding="utf-8")
    print(f"Wrote Adobe local resource audit report to {report_path}")
    print(f"Wrote Adobe local resource audit summary to {summary_path}")
    return 0 if report["ok"] else 1


def build_report(adobe_dir: Path, *, zip_entry_limit: int = 40) -> dict[str, Any]:
    root = Path(adobe_dir)
    resources = _resource_records(root, zip_entry_limit=zip_entry_limit) if root.exists() else []
    present_kinds = sorted({resource["kind"] for resource in resources})
    missing_required = sorted(REQUIRED_KINDS.difference(present_kinds))
    zip_findings = _zip_findings(resources)
    blocking_findings = _blocking_findings(missing_required, zip_findings)
    converter_state = _converter_resource_state(resources)
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "ok": root.is_dir() and not blocking_findings,
        "local_only": True,
        "adobe_dir": _display_path(root),
        "adobe_dir_exists": root.exists(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "image2dng_version": __version__,
        },
        "policy": {
            "installer_execution": "not-attempted",
            "archive_extraction": "not-attempted",
            "sdk_build": "manual-only",
            "ci_gate": False,
            "notes": (
                "This audit only inspects filenames and zip central directories. "
                "Adobe binaries remain local-only resources."
            ),
        },
        "required_kinds": sorted(REQUIRED_KINDS),
        "present_kinds": present_kinds,
        "missing_required_kinds": missing_required,
        "blocking_findings": blocking_findings,
        "readiness": {
            "dng_converter_installer_or_resource": "dng-converter-resource" in present_kinds,
            "dng_converter_resource_state": converter_state,
            "dng_converter_installed_executable": converter_state == "installed-executable",
            "dng_sdk_archive": "dng-sdk-archive" in present_kinds,
            "dng_sdk_validate_project_detected": zip_findings[
                "dng_sdk_validate_project_detected"
            ],
            "dng_specification": "dng-specification" in present_kinds,
            "tiff_reference": "tiff-reference" in present_kinds,
            "profile_sdk_or_tools": any(
                kind in present_kinds
                for kind in (
                    "profile-sdk-archive",
                    "dng-profile-editor",
                    "lens-profile-creator-archive",
                )
            ),
        },
        "next_steps": [
            (
                "Treat Adobe DNG Converter files in Adobe/ as resources or installers until a real "
                "installed converter executable path is provided."
            ),
            (
                "Keep Adobe DNG SDK validation manual-only unless an explicit local build path "
                "is approved."
            ),
            "Do not commit Adobe binaries, archives, generated reports, or extracted SDK contents.",
        ],
        "resources": resources,
    }


def _resource_records(root: Path, *, zip_entry_limit: int) -> list[dict[str, Any]]:
    records = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        record: dict[str, Any] = {
            "name": path.name,
            "path": _display_path(path),
            "kind": _classify_resource(path.name),
            "size_bytes": path.stat().st_size,
        }
        if path.suffix.lower() == ".zip":
            record["zip"] = _zip_summary(path, entry_limit=zip_entry_limit)
        records.append(record)
    return records


def _classify_resource(name: str) -> str:
    lowered = name.lower()
    suffix = Path(name).suffix.lower()
    compact = lowered.replace(" ", "").replace("_", "")
    if lowered in INSTALLED_CONVERTER_FILENAMES:
        return "dng-converter-resource"
    if suffix in CONVERTER_RESOURCE_EXTENSIONS and "dngconverter" in compact:
        return "dng-converter-resource"
    if lowered.startswith("dng_sdk") and lowered.endswith(".zip"):
        return "dng-sdk-archive"
    if lowered.startswith("dng_spec"):
        return "dng-specification"
    if lowered in {"tiff6.pdf", "tiffphotoshop.pdf"}:
        return "tiff-reference"
    if "acr_and_lightroom_profile_sdk" in lowered:
        return "profile-sdk-archive"
    if "dng_profile_editor" in lowered or "dngprofile_editor" in lowered:
        return "dng-profile-editor"
    if "lensprofile_creator" in lowered or "lensprofilecreator" in lowered:
        return "lens-profile-creator-archive"
    if lowered.endswith(".pdf"):
        return "adobe-documentation"
    return "other"


def _zip_summary(path: Path, *, entry_limit: int) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as exc:
        return {
            "ok": False,
            "entry_count": 0,
            "sample_entries": [],
            "findings": {},
            "error": f"bad zip file: {exc}",
        }

    sample_entries = names[:entry_limit]
    normalized = [name.replace("\\", "/").lower() for name in names]
    findings = {
        "dng_validate_solution": any(name.endswith("dng_validate.sln") for name in normalized),
        "dng_validate_project": any(
            name.endswith("dng_validate/dng_validate.vcxproj") for name in normalized
        ),
        "profile_example_dng": any(name.endswith(".dng") for name in normalized),
        "lens_profile_creator_executable": any(
            name.endswith("adobe lens profile creator.exe") for name in normalized
        ),
    }
    return {
        "ok": True,
        "entry_count": len(names),
        "sample_entries": sample_entries,
        "findings": findings,
    }


def _zip_findings(resources: list[dict[str, Any]]) -> dict[str, bool]:
    dng_sdk_validate_project_detected = False
    for resource in resources:
        if resource.get("kind") != "dng-sdk-archive":
            continue
        zip_summary = resource.get("zip")
        if not isinstance(zip_summary, dict):
            continue
        findings = zip_summary.get("findings")
        if not isinstance(findings, dict):
            continue
        dng_sdk_validate_project_detected = dng_sdk_validate_project_detected or bool(
            findings.get("dng_validate_solution") and findings.get("dng_validate_project")
        )
    return {"dng_sdk_validate_project_detected": dng_sdk_validate_project_detected}


def _blocking_findings(
    missing_required: list[str], zip_findings: dict[str, bool]
) -> list[str]:
    findings = [f"missing required resource kind: {kind}" for kind in missing_required]
    if (
        "dng-sdk-archive" not in missing_required
        and not zip_findings["dng_sdk_validate_project_detected"]
    ):
        findings.append("no readable DNG SDK archive with dng_validate project detected")
    return findings


def _converter_resource_state(resources: list[dict[str, Any]]) -> str:
    converter_resources = [
        resource for resource in resources if resource.get("kind") == "dng-converter-resource"
    ]
    if not converter_resources:
        return "missing"
    if any(
        str(resource.get("name", "")).lower() in INSTALLED_CONVERTER_FILENAMES
        for resource in converter_resources
    ):
        return "installed-executable"
    return "resource-present-not-installed"


def _summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Adobe Local Resource Audit",
        "",
        f"- Schema: `{report['schema']}`",
        f"- OK: `{str(report['ok']).lower()}`",
        f"- Adobe dir: `{report['adobe_dir']}`",
        f"- Local-only: `{str(report['local_only']).lower()}`",
        "",
        "## Readiness",
        "",
    ]
    readiness = _readiness(report)
    for key in sorted(readiness):
        lines.append(f"- `{key}`: `{str(readiness[key]).lower()}`")
    lines.extend(["", "## Missing Required Kinds", ""])
    missing = report["missing_required_kinds"]
    if missing:
        lines.extend(f"- `{kind}`" for kind in missing)
    else:
        lines.append("- none")
    lines.extend(["", "## Blocking Findings", ""])
    blocking_findings = report["blocking_findings"]
    if blocking_findings:
        lines.extend(f"- {finding}" for finding in blocking_findings)
    else:
        lines.append("- none")
    lines.extend(["", "## Resources", ""])
    for resource in report["resources"]:
        lines.append(
            f"- `{resource['name']}`: `{resource['kind']}`, {resource['size_bytes']} bytes"
        )
    lines.extend(["", "## Policy", ""])
    for key, value in _policy(report).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.append("")
    return "\n".join(lines)


def _display_path(path: Path) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def _readiness(report: dict[str, Any]) -> dict[str, Any]:
    readiness = report["readiness"]
    if not isinstance(readiness, dict):
        raise TypeError("report readiness must be an object")
    return readiness


def _policy(report: dict[str, Any]) -> dict[str, Any]:
    policy = report["policy"]
    if not isinstance(policy, dict):
        raise TypeError("report policy must be an object")
    return policy


def _output_dir_is_allowed(output_dir: Path) -> bool:
    repo_root = Path(__file__).resolve().parents[1]
    demo_output = (repo_root / "demo-output").resolve()
    resolved = output_dir.resolve()
    return resolved == demo_output or demo_output in resolved.parents


if __name__ == "__main__":
    raise SystemExit(main())
