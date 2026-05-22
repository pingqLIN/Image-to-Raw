from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from image2dng.compatibility import processor_tool_inventory

REPORT_SCHEMA = "image2dng.raw_processor_setup_audit.v1"
TARGET_TOOLS = ("dcraw", "darktable-cli", "rawtherapee-cli")
PACKAGE_MANAGERS = ("winget", "scoop", "choco")

TOOL_QUERIES = {
    "dcraw": {
        "winget": ["dcraw", "libraw"],
        "scoop": ["dcraw", "libraw"],
        "choco": ["dcraw", "libraw"],
    },
    "darktable-cli": {
        "winget": ["darktable"],
        "scoop": ["darktable"],
        "choco": ["darktable"],
    },
    "rawtherapee-cli": {
        "winget": ["rawtherapee"],
        "scoop": ["rawtherapee"],
        "choco": ["rawtherapee"],
    },
}

EXPECTED_PACKAGE_IDS = {
    "winget": {
        "dcraw": ("dcraw",),
        "libraw": ("libraw",),
        "darktable": ("darktable.darktable",),
        "rawtherapee": ("rawtherapee.rawtherapee",),
    },
    "scoop": {
        "dcraw": ("dcraw",),
        "libraw": ("libraw",),
        "darktable": ("darktable",),
        "rawtherapee": ("rawtherapee",),
    },
    "choco": {
        "dcraw": ("dcraw",),
        "libraw": ("libraw",),
        "darktable": ("darktable",),
        "rawtherapee": ("rawtherapee",),
    },
}

RECOMMENDED_PRIORITY = {
    "darktable-cli": "recommended-first",
    "rawtherapee-cli": "recommended-second",
    "dcraw": "legacy-optional",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run audit for optional RAW processor setup."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo-output/raw-processor-setup-audit"),
    )
    parser.add_argument(
        "--search-timeout-seconds",
        type=int,
        default=8,
        help="timeout for each package-manager search command",
    )
    parser.add_argument(
        "--skip-package-search",
        action="store_true",
        help="record search commands without running package-manager searches",
    )
    args = parser.parse_args(argv)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    report = _build_report(
        output_dir=output_dir,
        timeout_seconds=args.search_timeout_seconds,
        skip_package_search=args.skip_package_search,
    )
    report_path = output_dir / "setup-audit-report.json"
    runbook_path = output_dir / "setup-runbook.md"
    prompt_path = output_dir / "external-review-prompt.md"

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    runbook_path.write_text(_runbook_markdown(report), encoding="utf-8")
    prompt_path.write_text(_external_review_prompt(report), encoding="utf-8")

    print(f"Wrote setup audit report to {report_path}")
    print(f"Wrote setup runbook to {runbook_path}")
    print(f"Wrote external review prompt to {prompt_path}")
    return 0


def _build_report(
    *,
    output_dir: Path,
    timeout_seconds: int,
    skip_package_search: bool,
) -> dict[str, Any]:
    managers = _package_manager_inventory()
    tool_inventory = processor_tool_inventory(timeout_seconds)
    tools = {}
    for tool in TARGET_TOOLS:
        tools[tool] = {
            "current": tool_inventory[tool],
            "recommendation": _tool_recommendation(tool),
            "package_searches": _package_searches(
                tool=tool,
                managers=managers,
                timeout_seconds=timeout_seconds,
                skip_package_search=skip_package_search,
            ),
            "post_install_checks": _post_install_checks(tool),
            "expected_evidence_matrix_change": {
                "before": "skipped: not found",
                "after_success": "passed with command, exit_code, duration, and output_artifacts",
                "after_failure": (
                    "failed with command, exit_code, stdout_tail, stderr_tail, and notes"
                ),
            },
        }

    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "output_dir": str(output_dir),
        "policy": {
            "auto_install": False,
            "search_only": True,
            "install_requires_user_approval": True,
            "notes": "This audit never installs or upgrades RAW processor tools.",
        },
        "package_managers": managers,
        "tools": tools,
        "review_questions": [
            "Should darktable-cli be the first recommended smoke target?",
            "Should RawTherapee be kept as a second independent export/open smoke target?",
            (
                "Should dcraw remain legacy-optional because maintained Windows packages "
                "may be harder to source?"
            ),
            (
                "Should Adobe DNG SDK remain manual-only without a reproducible "
                "local SDK validation path?"
            ),
        ],
        "rerun_commands": [
            (
                "uv run python scripts/audit_raw_processor_setup.py "
                "--output-dir demo-output/raw-processor-setup-audit"
            ),
            (
                "uv run python scripts/generate_compatibility_evidence.py "
                "--output-dir demo-output/compatibility-evidence"
            ),
            (
                "uv run python scripts/generate_demo_review_bundle.py "
                "--output-dir demo-output/review-bundle"
            ),
        ],
        "ok": True,
        "errors": [],
    }


def _package_manager_inventory() -> dict[str, dict[str, Any]]:
    inventory = {}
    for manager in PACKAGE_MANAGERS:
        executable = shutil.which(manager)
        inventory[manager] = {
            "available": executable is not None,
            "executable": executable,
        }
    return inventory


def _package_searches(
    *,
    tool: str,
    managers: dict[str, dict[str, Any]],
    timeout_seconds: int,
    skip_package_search: bool,
) -> list[dict[str, Any]]:
    searches = []
    for manager, queries in TOOL_QUERIES[tool].items():
        for query in queries:
            executable = managers[manager]["executable"] or manager
            command = _search_command(manager, query, str(executable))
            if not managers[manager]["available"]:
                searches.append(
                    {
                        "manager": manager,
                        "query": query,
                        "command": command,
                        "status": "skipped",
                        "exit_code": None,
                        "duration_seconds": 0.0,
                        "stdout_tail": [],
                        "stderr_tail": [],
                        "version_hint": None,
                        "notes": f"{manager} not found",
                    }
                )
                continue
            if skip_package_search:
                searches.append(
                    {
                        "manager": manager,
                        "query": query,
                        "command": command,
                        "status": "not-run",
                        "exit_code": None,
                        "duration_seconds": 0.0,
                        "stdout_tail": [],
                        "stderr_tail": [],
                        "version_hint": None,
                        "notes": "package search skipped by --skip-package-search",
                    }
                )
                continue
            searches.append(_run_search(manager, query, command, timeout_seconds))
    return searches


def _run_search(
    manager: str,
    query: str,
    command: list[str],
    timeout_seconds: int,
) -> dict[str, Any]:
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
            "manager": manager,
            "query": query,
            "command": command,
            "status": "failed",
            "exit_code": None,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "stdout_tail": [],
            "stderr_tail": [],
            "version_hint": None,
            "notes": f"search failed to run: {exc}",
        }

    stdout_tail = _tail(completed.stdout)
    stderr_tail = _tail(completed.stderr)
    return {
        "manager": manager,
        "query": query,
        "command": command,
        "status": "completed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "version_hint": _version_hint(
            manager=manager,
            query=query,
            lines=stdout_tail + stderr_tail,
        ),
        "notes": "search-only; no install attempted",
    }


def _search_command(manager: str, query: str, executable: str) -> list[str]:
    if manager == "winget":
        return [executable, "search", query, "--source", "winget"]
    if manager == "scoop":
        return [executable, "search", query]
    if manager == "choco":
        return [executable, "search", query, "--limit-output"]
    raise ValueError(f"unsupported package manager: {manager}")


def _tool_recommendation(tool: str) -> dict[str, str]:
    details = {
        "darktable-cli": (
            "Best first smoke target because it is actively maintained "
            "and has a CLI export path."
        ),
        "rawtherapee-cli": (
            "Good second target because it exercises a separate RAW processor pipeline."
        ),
        "dcraw": (
            "Useful legacy parser, but Windows package availability may be weaker "
            "than maintained tools."
        ),
    }
    return {
        "priority": RECOMMENDED_PRIORITY[tool],
        "rationale": details[tool],
    }


def _post_install_checks(tool: str) -> list[list[str]]:
    checks = {
        "dcraw": [["dcraw", "-h"]],
        "darktable-cli": [["darktable-cli", "--version"]],
        "rawtherapee-cli": [["rawtherapee-cli", "--version"]],
    }
    return checks[tool]


def _runbook_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# RAW Processor Setup Audit Runbook",
        "",
        f"- Schema: `{report['schema']}`",
        f"- Generated at: `{report['generated_at']}`",
        f"- Auto install: `{report['policy']['auto_install']}`",
        "- This document is a dry-run setup guide. Do not install tools until the user approves.",
        "",
        "## Current Tool State",
        "",
        "| Tool | Available | Executable | Discovery | Recommendation |",
        "| --- | --- | --- | --- | --- |",
    ]
    for tool, info in _tools(report).items():
        current = info["current"]
        lines.append(
            f"| `{tool}` | `{current['available']}` | `{current['executable']}` | "
            f"`{current.get('discovery')}` | "
            f"{info['recommendation']['priority']} |"
        )

    lines.extend(["", "## Package Search Evidence", ""])
    for tool, info in _tools(report).items():
        lines.append(f"### `{tool}`")
        lines.append("")
        lines.append(info["recommendation"]["rationale"])
        lines.append("")
        lines.append("| Manager | Query | Status | Version hint | Notes |")
        lines.append("| --- | --- | --- | --- | --- |")
        for search in info["package_searches"]:
            lines.append(
                f"| `{search['manager']}` | `{search['query']}` | `{search['status']}` | "
                f"`{search['version_hint']}` | {search['notes']} |"
            )
        lines.append("")

    lines.extend(
        [
            "## After Approved Install",
            "",
            "1. Confirm the selected package and install command with the user.",
            "2. Install exactly one approved RAW processor first.",
            "3. Run the tool's version command.",
            "4. Regenerate the setup audit, compatibility evidence, and review bundle:",
            "",
            "```powershell",
        ]
    )
    lines.extend(report["rerun_commands"])
    lines.extend(
        [
            "```",
            "",
            "Expected matrix change: the installed tool should move from `skipped` to "
            "`passed` or `failed` with command evidence and output artifacts.",
        ]
    )
    return "\n".join(lines) + "\n"


def _external_review_prompt(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# External Review Prompt: RAW Processor Setup Audit",
            "",
            "Review the attached RAW processor setup audit for `image2dng`.",
            "",
            "Please evaluate:",
            "",
            "- Which optional RAW processor should be the first recommended smoke target.",
            "- Whether Darktable and RawTherapee provide enough independent coverage.",
            "- Whether dcraw should remain legacy-optional.",
            "- Whether Adobe DNG SDK should remain manual-only.",
            "- Whether the post-install rerun commands are sufficient evidence.",
            "- Whether package search version hints are based on exact package matches.",
            "",
            "Important constraints:",
            "",
            f"- Auto install is `{_policy(report)['auto_install']}`.",
            "- Missing tools are allowed to remain `skipped`.",
            "- Available tools that fail should remain hard failures in the compatibility report.",
            "- Binary demo outputs stay local-only under `demo-output/`.",
        ]
    ) + "\n"


def _version_hint(*, manager: str, query: str, lines: list[str]) -> str | None:
    expected_ids = EXPECTED_PACKAGE_IDS.get(manager, {}).get(query, (query,))
    for line in lines:
        hint = _version_hint_from_exact_line(manager, expected_ids, line)
        if hint:
            return hint
    return None


def _tools(report: dict[str, Any]) -> dict[str, Any]:
    tools = report["tools"]
    if not isinstance(tools, dict):
        raise TypeError("report tools must be an object")
    return tools


def _policy(report: dict[str, Any]) -> dict[str, Any]:
    policy = report["policy"]
    if not isinstance(policy, dict):
        raise TypeError("report policy must be an object")
    return policy


def _version_hint_from_exact_line(
    manager: str, expected_ids: tuple[str, ...], line: str
) -> str | None:
    if manager == "choco" and "|" in line:
        package_id, _, rest = line.partition("|")
        if _matches_expected_package(package_id, expected_ids):
            return _first_version(rest)
        return None

    for package_id in expected_ids:
        pattern = re.compile(
            rf"(?i)(?:^|\s){re.escape(package_id)}(?:\s+|\|)(?P<rest>.*)$"
        )
        match = pattern.search(line)
        if match:
            return _first_version(match.group("rest"))
    return None


def _matches_expected_package(package_id: str, expected_ids: tuple[str, ...]) -> bool:
    normalized = package_id.strip().lower()
    return normalized in {expected.lower() for expected in expected_ids}


def _first_version(text: str) -> str | None:
    match = re.search(r"\b\d+(?:\.\d+){1,3}(?:[-+~][0-9A-Za-z.\-]+)?\b", text)
    return match.group(0) if match else None


def _tail(text: str, *, max_lines: int = 20) -> list[str]:
    lines = text.strip().splitlines()
    return lines[-max_lines:]


if __name__ == "__main__":
    raise SystemExit(main())
