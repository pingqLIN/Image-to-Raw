from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from image2dng import __version__

REPORT_SCHEMA = "image2dng.adobe_dng_sdk_validation_report.v1"

DEFAULT_VALIDATOR = Path(
    "Adobe/dng_sdk_1_7_1/dng_sdk/targets/win/release64_x64/dng_validate.exe"
)
DEFAULT_OUTPUT_DIR = Path("demo-output/adobe-dng-sdk-validation")
DEFAULT_FIXTURE_CANDIDATES = (
    Path("demo-output/review-bundle-phase6/artifacts/representative-dng"),
    Path("demo-output/adobe-dng-converter-verification/source-dng"),
    Path("demo-output/adobe-compatible-user-samples/adobe-converted"),
)
ERROR_MARKERS = {
    "fatal": re.compile(r"^\s*(?:\*+\s*)?fatal\b", re.IGNORECASE),
    "error": re.compile(r"^\s*(?:\*+\s*)?error\b|^\s*\*+\s*error:", re.IGNORECASE),
    "exception": re.compile(r"\bexception\b", re.IGNORECASE),
    "failed": re.compile(
        r"\bvalidation failed\b|^\s*(?:\*+\s*)?failed\b", re.IGNORECASE
    ),
    "corrupt": re.compile(r"\bcorrupt(?:ed)?\b", re.IGNORECASE),
    "invalid": re.compile(
        r"^\s*(?:\*+\s*)?invalid\b|\bis invalid\b", re.IGNORECASE
    ),
}
VERSION_PATTERN = re.compile(r"dng_validate,\s+version\s+([^\r\n]+)", re.IGNORECASE)
TAIL_LINE_LIMIT = 40
TAIL_LINE_WIDTH = 300


class Runner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: float,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        ...


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run local-only Adobe DNG SDK dng_validate.exe against representative DNG fixtures."
        )
    )
    parser.add_argument(
        "--validator",
        type=Path,
        default=DEFAULT_VALIDATOR,
        help="path to the locally built dng_validate.exe",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        action="append",
        help="representative DNG fixture directory; repeat for multiple dirs",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="local-only validation report directory; defaults under demo-output/",
    )
    parser.add_argument(
        "--allow-output-outside-demo-output",
        action="store_true",
        help="explicitly allow writing local reports outside demo-output/",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="allow zero selected DNG fixtures and write a skipped report",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="timeout for each dng_validate invocation",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    validator = _resolve_from_repo(args.validator, repo_root)
    fixture_roots = tuple(
        _resolve_from_repo(path, repo_root)
        for path in (args.fixture_dir if args.fixture_dir else DEFAULT_FIXTURE_CANDIDATES)
    )
    output_dir = _resolve_from_repo(args.output_dir, repo_root)

    output_error = _output_dir_refusal_reason(
        output_dir,
        repo_root=repo_root,
        fixture_roots=fixture_roots,
        allow_outside_demo_output=args.allow_output_outside_demo_output,
    )
    if output_error is not None:
        print(output_error, flush=True)
        return 2

    report = build_report(
        validator=validator,
        fixture_roots=fixture_roots,
        output_dir=output_dir,
        timeout_seconds=max(0.001, args.timeout_seconds),
        allow_empty=args.allow_empty,
        runner=subprocess.run,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "adobe-dng-sdk-validation-report.json"
    summary_path = output_dir / "adobe-dng-sdk-validation-report.md"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.write_text(_summary_markdown(report), encoding="utf-8")
    print(f"Wrote Adobe DNG SDK validation report to {report_path}")
    print(f"Wrote Adobe DNG SDK validation summary to {summary_path}")
    return 0 if report["ok"] else 1


def build_report(
    *,
    validator: Path,
    fixture_roots: tuple[Path, ...],
    output_dir: Path,
    timeout_seconds: float,
    allow_empty: bool,
    runner: Runner,
) -> dict[str, Any]:
    repo_root = _repo_root()
    validator = validator.resolve()
    fixture_roots = tuple(path.resolve() for path in fixture_roots)
    output_dir = output_dir.resolve()
    fixtures = _discover_fixtures(fixture_roots)
    version_probe = _version_probe(
        validator=validator,
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    results = [
        _validate_fixture(
            validator=validator,
            fixture=fixture,
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        for fixture in fixtures
    ]
    summary = _summary_counts(results, selected_count=len(fixtures))
    error_markers = _error_markers(results)
    blocking_findings = _blocking_findings(
        validator=validator,
        version_probe=version_probe,
        selected_count=len(fixtures),
        summary=summary,
        marker_count=len(error_markers),
        allow_empty=allow_empty,
    )
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "ok": not blocking_findings,
        "local_only": True,
        "repo_root": _display_path(repo_root, repo_root),
        "output_dir": _path_record(output_dir, repo_root),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "image2dng_version": __version__,
        },
        "policy": {
            "adobe_download": "not-attempted",
            "adobe_install": "not-attempted",
            "sdk_extraction": "not-attempted",
            "sdk_project_modification": "not-attempted",
            "ci_gate": False,
            "reports_local_only": True,
            "version_probe_accepted_exit_codes": [0, 1],
            "marker_policy": "blocking",
        },
        "validator": {
            "path": _path_record(validator, repo_root),
            "exists": validator.is_file(),
            "version_probe": version_probe,
        },
        "fixture_roots": [_fixture_root_record(root, repo_root) for root in fixture_roots],
        "summary": summary,
        "blocking_findings": blocking_findings,
        "error_markers": error_markers,
        "results": results,
    }


def _version_probe(
    *,
    validator: Path,
    repo_root: Path,
    timeout_seconds: float,
    runner: Runner,
) -> dict[str, Any]:
    command = [str(validator)]
    if not validator.is_file():
        return {
            "command": command,
            "exit_code": None,
            "duration_seconds": 0.0,
            "status": "missing-validator",
            "version_text": None,
            "timeout": False,
            "stdout_tail": [],
            "stderr_tail": [],
            "markers": [],
        }
    result = _run_command(
        command,
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    combined = "\n".join(result["stdout_tail"] + result["stderr_tail"])
    match = VERSION_PATTERN.search(combined)
    result["version_text"] = match.group(1).strip() if match else None
    version_exit_code_is_accepted = result["exit_code"] in (0, 1)
    if result["version_text"] and version_exit_code_is_accepted and not result["timeout"]:
        result["status"] = "passed"
    return result


def _validate_fixture(
    *,
    validator: Path,
    fixture: Path,
    repo_root: Path,
    timeout_seconds: float,
    runner: Runner,
) -> dict[str, Any]:
    command = [str(validator), str(fixture)]
    process = _run_command(
        command,
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if process["exit_code"] == 0 and not process["timeout"] and process["markers"]:
        status = "marker-blocked"
    elif process["exit_code"] == 0 and not process["timeout"]:
        status = "passed"
    else:
        status = process["status"]
    return {
        "fixture": _path_record(fixture, repo_root),
        "size_bytes": fixture.stat().st_size,
        "sha256": _sha256_file(fixture),
        "command": command,
        "working_directory": _display_path(repo_root, repo_root),
        "exit_code": process["exit_code"],
        "duration_seconds": process["duration_seconds"],
        "timeout": process["timeout"],
        "status": status,
        "stdout_tail": process["stdout_tail"],
        "stderr_tail": process["stderr_tail"],
        "markers": process["markers"],
    }


def _run_command(
    command: list[str],
    *,
    repo_root: Path,
    timeout_seconds: float,
    runner: Runner,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = runner(
            command,
            cwd=repo_root,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.perf_counter() - started, 3)
        stdout = _safe_text(exc.stdout)
        stderr = _safe_text(exc.stderr)
        return {
            "command": command,
            "exit_code": None,
            "duration_seconds": duration,
            "status": "timeout",
            "timeout": True,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
            "markers": _markers(stdout, stderr),
        }
    except Exception as exc:  # noqa: BLE001
        duration = round(time.perf_counter() - started, 3)
        return {
            "command": command,
            "exit_code": None,
            "duration_seconds": duration,
            "status": "failed",
            "timeout": False,
            "stdout_tail": [],
            "stderr_tail": _tail(str(exc)),
            "markers": _markers("", str(exc)),
        }
    duration = round(time.perf_counter() - started, 3)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    return {
        "command": command,
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "status": "passed" if completed.returncode == 0 else "failed",
        "timeout": False,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "markers": _markers(stdout, stderr),
    }


def _discover_fixtures(fixture_roots: tuple[Path, ...]) -> list[Path]:
    seen: set[Path] = set()
    fixtures: list[Path] = []
    for root in fixture_roots:
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        for path in sorted(root.rglob("*.dng"), key=lambda item: item.as_posix().lower()):
            if path.is_symlink():
                continue
            resolved = path.resolve()
            if not _same_or_child(resolved, resolved_root):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            fixtures.append(resolved)
    return fixtures


def _fixture_root_record(root: Path, repo_root: Path) -> dict[str, Any]:
    dngs = _discover_fixtures((root,)) if root.is_dir() else []
    return {
        "path": _path_record(root, repo_root),
        "exists": root.is_dir(),
        "dng_count": len(dngs),
    }


def _summary_counts(results: list[dict[str, Any]], *, selected_count: int) -> dict[str, int]:
    statuses = [_result_status(result) for result in results]
    passed = statuses.count("passed")
    failed = statuses.count("failed")
    timed_out = statuses.count("timeout")
    marker_blocked = statuses.count("marker-blocked")
    return {
        "selected": selected_count,
        "passed": passed,
        "failed": failed,
        "marker_blocked": marker_blocked,
        "timeout": timed_out,
        "skipped": 0,
    }


def _blocking_findings(
    *,
    validator: Path,
    version_probe: dict[str, Any],
    selected_count: int,
    summary: dict[str, int],
    marker_count: int,
    allow_empty: bool,
) -> list[str]:
    findings = []
    if not validator.is_file():
        findings.append("validator executable missing")
    if version_probe["timeout"]:
        findings.append("validator version probe timed out")
    elif version_probe["status"] != "passed":
        findings.append("validator version probe did not detect dng_validate version text")
    if selected_count == 0 and not allow_empty:
        findings.append("no DNG fixtures selected")
    if summary["failed"]:
        findings.append(f"{summary['failed']} DNG fixture validation(s) failed")
    if summary["timeout"]:
        findings.append(f"{summary['timeout']} DNG fixture validation(s) timed out")
    if marker_count:
        findings.append(f"{marker_count} DNG fixture validation(s) emitted error markers")
    return findings


def _error_markers(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for result in results:
        if not result["markers"]:
            continue
        records.append(
            {
                "fixture": result["fixture"]["repo_relative"],
                "status": result["status"],
                "markers": result["markers"],
            }
        )
    return records


def _markers(stdout: str, stderr: str) -> list[str]:
    lines = [
        line
        for line in f"{stdout}\n{stderr}".splitlines()
        if not line.lstrip().lower().startswith("validating ")
    ]
    return [
        marker
        for marker, pattern in ERROR_MARKERS.items()
        if any(pattern.search(line) for line in lines)
    ]


def _tail(text: str) -> list[str]:
    lines = text.splitlines()[-TAIL_LINE_LIMIT:]
    return [line[:TAIL_LINE_WIDTH] for line in lines]


def _safe_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _summary_markdown(report: dict[str, Any]) -> str:
    summary = _summary(report)
    lines = [
        "# Adobe DNG SDK Validation Report",
        "",
        f"- Schema: `{_report_schema(report)}`",
        f"- OK: `{str(_report_ok(report)).lower()}`",
        f"- Local-only: `{str(_local_only(report)).lower()}`",
        f"- Validator: `{_validator_repo_relative(_validator(report))}`",
        f"- Version: `{_validator_version_text(_validator(report))}`",
        "",
        "## Summary",
        "",
    ]
    for key in ("selected", "passed", "failed", "marker_blocked", "timeout", "skipped"):
        lines.append(f"- `{key}`: `{_summary_count(summary, key)}`")
    lines.extend(["", "## Blocking Findings", ""])
    blocking_findings = _blocking_findings_record(report)
    if blocking_findings:
        lines.extend(f"- {finding}" for finding in blocking_findings)
    else:
        lines.append("- none")
    lines.extend(["", "## Error Markers", ""])
    error_markers = _error_marker_records(report)
    if error_markers:
        for record in error_markers:
            lines.append(
                f"- `{_error_marker_fixture(record)}` ({_error_marker_status(record)}): "
                f"{', '.join(_error_marker_markers(record))}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Results", ""])
    for result in _result_records(report):
        lines.append(
            f"- `{_result_fixture_repo_relative(result)}`: {_result_status(result)} "
            f"(exit={_result_exit_code(result)}, timeout={_result_timeout(result)}, "
            f"{_result_duration_seconds(result)}s)"
        )
    lines.append("")
    return "\n".join(lines)


def _report_schema(report: dict[str, Any]) -> str:
    schema = report["schema"]
    if not isinstance(schema, str):
        raise TypeError("report schema must be a string")
    return schema


def _report_ok(report: dict[str, Any]) -> bool:
    ok = report["ok"]
    if not isinstance(ok, bool):
        raise TypeError("report ok must be a boolean")
    return ok


def _local_only(report: dict[str, Any]) -> bool:
    local_only = report["local_only"]
    if not isinstance(local_only, bool):
        raise TypeError("report local_only must be a boolean")
    return local_only


def _validator(report: dict[str, Any]) -> dict[str, Any]:
    validator = report["validator"]
    if not isinstance(validator, dict):
        raise TypeError("report validator must be an object")
    return validator


def _validator_repo_relative(validator: dict[str, Any]) -> str | None:
    path = validator["path"]
    if not isinstance(path, dict):
        raise TypeError("validator path must be an object")
    repo_relative = path["repo_relative"]
    if isinstance(repo_relative, str) or repo_relative is None:
        return repo_relative
    raise TypeError("validator repo_relative path must be a string or null")


def _validator_version_text(validator: dict[str, Any]) -> str | None:
    version_probe = validator["version_probe"]
    if not isinstance(version_probe, dict):
        raise TypeError("validator version_probe must be an object")
    version_text = version_probe.get("version_text")
    if isinstance(version_text, str) or version_text is None:
        return version_text
    raise TypeError("validator version_text must be a string or null")


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = report["summary"]
    if not isinstance(summary, dict):
        raise TypeError("report summary must be an object")
    return summary


def _summary_count(summary: dict[str, Any], key: str) -> int:
    count = summary[key]
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError(f"summary {key} must be an integer")
    return count


def _blocking_findings_record(report: dict[str, Any]) -> list[str]:
    findings = report["blocking_findings"]
    if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
        raise TypeError("report blocking_findings must be a string list")
    return findings


def _error_marker_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    records = report["error_markers"]
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise TypeError("report error_markers must be an object list")
    return records


def _error_marker_fixture(record: dict[str, Any]) -> str:
    fixture = record["fixture"]
    if not isinstance(fixture, str):
        raise TypeError("error marker fixture must be a string")
    return fixture


def _error_marker_status(record: dict[str, Any]) -> str:
    status = record["status"]
    if not isinstance(status, str):
        raise TypeError("error marker status must be a string")
    return status


def _error_marker_markers(record: dict[str, Any]) -> list[str]:
    markers = record["markers"]
    if not isinstance(markers, list) or not all(isinstance(item, str) for item in markers):
        raise TypeError("error marker markers must be a string list")
    return markers


def _result_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    results = report["results"]
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise TypeError("report results must be an object list")
    return results


def _result_fixture_repo_relative(result: dict[str, Any]) -> str | None:
    fixture = result["fixture"]
    if not isinstance(fixture, dict):
        raise TypeError("result fixture must be an object")
    repo_relative = fixture["repo_relative"]
    if isinstance(repo_relative, str) or repo_relative is None:
        return repo_relative
    raise TypeError("result fixture repo_relative must be a string or null")


def _result_status(result: dict[str, Any]) -> str:
    status = result["status"]
    if not isinstance(status, str) or status not in {
        "passed",
        "failed",
        "timeout",
        "marker-blocked",
    }:
        raise TypeError("result status must be passed, failed, timeout, or marker-blocked")
    return status


def _result_exit_code(result: dict[str, Any]) -> int | None:
    exit_code = result["exit_code"]
    if isinstance(exit_code, bool):
        raise TypeError("result exit_code must be an integer or null")
    if isinstance(exit_code, int) or exit_code is None:
        return exit_code
    raise TypeError("result exit_code must be an integer or null")


def _result_timeout(result: dict[str, Any]) -> bool:
    timeout = result["timeout"]
    if not isinstance(timeout, bool):
        raise TypeError("result timeout must be a boolean")
    return timeout


def _result_duration_seconds(result: dict[str, Any]) -> float | int:
    duration = result["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not math.isfinite(duration)
    ):
        raise TypeError("result duration_seconds must be finite numeric")
    return duration


def _path_record(path: Path, repo_root: Path) -> dict[str, str | None]:
    return {
        "repo_relative": _display_path(path, repo_root),
        "absolute_local": str(path),
    }


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def _resolve_from_repo(path: Path, repo_root: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _output_dir_refusal_reason(
    output_dir: Path,
    *,
    repo_root: Path,
    fixture_roots: tuple[Path, ...],
    allow_outside_demo_output: bool,
) -> str | None:
    resolved = output_dir.resolve()
    demo_output = (repo_root / "demo-output").resolve()
    adobe_dir = (repo_root / "Adobe").resolve()
    docs_local = (repo_root / "docs" / "local").resolve()
    if _same_or_child(resolved, adobe_dir):
        return "Refusing to write Adobe DNG SDK validation report inside Adobe/."
    if not allow_outside_demo_output and not _same_or_child(resolved, demo_output):
        return (
            "Refusing to write Adobe DNG SDK validation report outside demo-output/. "
            "Pass --allow-output-outside-demo-output for an explicit local override."
        )
    for root in fixture_roots:
        if root.exists() and _same_or_child(resolved, root.resolve()):
            return "Refusing to write Adobe DNG SDK validation report inside a fixture directory."
    if (
        allow_outside_demo_output
        and _same_or_child(resolved, repo_root)
        and not _same_or_child(resolved, demo_output)
        and not _same_or_child(resolved, docs_local)
    ):
        return (
            "Refusing to write Adobe DNG SDK validation report inside a tracked repo area. "
            "Use demo-output/, docs/local/, or an explicit path outside the repo."
        )
    return None


def _same_or_child(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    raise SystemExit(main())
