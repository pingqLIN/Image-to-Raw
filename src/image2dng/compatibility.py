from __future__ import annotations

import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ProcessorResult = Literal["passed", "skipped", "failed", "manual-only"]


@dataclass(frozen=True)
class ProcessorToolSpec:
    name: str
    version_command: list[str] | None
    install_hint: str
    manual_only: bool = False


@dataclass(frozen=True)
class ProcessorCompatibilityResult:
    tool: str
    available: bool
    version: str | None
    command: list[str]
    exit_code: int | None
    duration_seconds: float
    result: ProcessorResult
    stdout_tail: list[str]
    stderr_tail: list[str]
    output_artifacts: list[str]
    notes: str

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "available": self.available,
            "version": self.version,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "result": self.result,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "output_artifacts": self.output_artifacts,
            "notes": self.notes,
        }


PROCESSOR_TOOL_SPECS = {
    "exiftool": ProcessorToolSpec(
        name="exiftool",
        version_command=["exiftool", "-ver"],
        install_hint="Install ExifTool manually or with a local package manager, then rerun.",
    ),
    "dcraw": ProcessorToolSpec(
        name="dcraw",
        version_command=["dcraw", "-h"],
        install_hint="Install dcraw manually and ensure dcraw is on PATH, then rerun.",
    ),
    "darktable-cli": ProcessorToolSpec(
        name="darktable-cli",
        version_command=["darktable-cli", "--version"],
        install_hint="Install Darktable manually and ensure darktable-cli is on PATH, then rerun.",
    ),
    "rawtherapee-cli": ProcessorToolSpec(
        name="rawtherapee-cli",
        version_command=["rawtherapee-cli", "--version"],
        install_hint=(
            "Install RawTherapee manually and ensure rawtherapee-cli is on PATH, then rerun."
        ),
    ),
    "adobe-dng-sdk": ProcessorToolSpec(
        name="adobe-dng-sdk",
        version_command=None,
        install_hint="Manual Adobe DNG SDK validation remains outside the automated gate.",
        manual_only=True,
    ),
}


def processor_tool_inventory(timeout_seconds: int = 60) -> dict[str, dict[str, object]]:
    inventory = {}
    for name, spec in PROCESSOR_TOOL_SPECS.items():
        if spec.manual_only:
            inventory[name] = {
                "available": False,
                "manual_only": True,
                "executable": None,
                "version_command": None,
                "version": None,
                "timeout_seconds": timeout_seconds,
                "install_hint": spec.install_hint,
            }
            continue

        executable = shutil.which(name)
        inventory[name] = {
            "available": executable is not None,
            "manual_only": False,
            "executable": executable,
            "version_command": spec.version_command,
            "version": _tool_version(spec.version_command, timeout_seconds)
            if executable is not None and spec.version_command is not None
            else None,
            "timeout_seconds": timeout_seconds,
            "install_hint": spec.install_hint,
        }
    return inventory


def run_processor_compatibility(
    dng_path: str | Path,
    output_dir: str | Path,
    *,
    timeout_seconds: int = 60,
) -> list[ProcessorCompatibilityResult]:
    source = Path(dng_path)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)

    return [
        _run_exiftool(source, root, timeout_seconds),
        _run_dcraw(source, root, timeout_seconds),
        _run_darktable(source, root, timeout_seconds),
        _run_rawtherapee(source, root, timeout_seconds),
        _manual_only_result("adobe-dng-sdk", timeout_seconds),
    ]


def _run_exiftool(
    source: Path,
    _output_dir: Path,
    timeout_seconds: int,
) -> ProcessorCompatibilityResult:
    return _run_processor_command(
        tool="exiftool",
        source=source,
        command=["exiftool", str(source)],
        output_artifacts=[],
        timeout_seconds=timeout_seconds,
    )


def _run_dcraw(
    source: Path,
    output_dir: Path,
    timeout_seconds: int,
) -> ProcessorCompatibilityResult:
    output = output_dir / f"{source.stem}-dcraw.tiff"
    return _run_processor_command(
        tool="dcraw",
        source=source,
        command=["dcraw", "-T", "-w", "-O", str(output), str(source)],
        output_artifacts=[output],
        timeout_seconds=timeout_seconds,
    )


def _run_darktable(
    source: Path,
    output_dir: Path,
    timeout_seconds: int,
) -> ProcessorCompatibilityResult:
    output = output_dir / f"{source.stem}-darktable.tif"
    return _run_processor_command(
        tool="darktable-cli",
        source=source,
        command=["darktable-cli", str(source), str(output)],
        output_artifacts=[output],
        timeout_seconds=timeout_seconds,
    )


def _run_rawtherapee(
    source: Path,
    output_dir: Path,
    timeout_seconds: int,
) -> ProcessorCompatibilityResult:
    output = output_dir / f"{source.stem}-rawtherapee.tif"
    return _run_processor_command(
        tool="rawtherapee-cli",
        source=source,
        command=["rawtherapee-cli", "-Y", "-o", str(output), "-c", str(source)],
        output_artifacts=[output],
        timeout_seconds=timeout_seconds,
    )


def _run_processor_command(
    *,
    tool: str,
    source: Path,
    command: list[str],
    output_artifacts: list[Path],
    timeout_seconds: int,
) -> ProcessorCompatibilityResult:
    executable = shutil.which(command[0])
    version_command = PROCESSOR_TOOL_SPECS[tool].version_command
    version = (
        _tool_version(version_command, timeout_seconds)
        if executable is not None and version_command is not None
        else None
    )
    if executable is None:
        return ProcessorCompatibilityResult(
            tool=tool,
            available=False,
            version=None,
            command=command,
            exit_code=None,
            duration_seconds=0.0,
            result="skipped",
            stdout_tail=[],
            stderr_tail=[],
            output_artifacts=[],
            notes="skipped: not found",
        )

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
        duration = round(time.perf_counter() - started, 3)
        return ProcessorCompatibilityResult(
            tool=tool,
            available=True,
            version=version,
            command=command,
            exit_code=None,
            duration_seconds=duration,
            result="failed",
            stdout_tail=[],
            stderr_tail=[],
            output_artifacts=[],
            notes=f"failed to run: {exc}",
        )

    duration = round(time.perf_counter() - started, 3)
    stdout_tail = _tail(completed.stdout)
    stderr_tail = _tail(completed.stderr)
    existing_outputs = [str(path) for path in output_artifacts if path.exists()]
    missing_outputs = [str(path) for path in output_artifacts if not path.exists()]

    if completed.returncode != 0:
        notes = (
            _last_line(stderr_tail)
            or _last_line(stdout_tail)
            or f"exit code {completed.returncode}"
        )
        result: ProcessorResult = "failed"
    elif missing_outputs:
        notes = f"missing output artifact: {', '.join(missing_outputs)}"
        result = "failed"
    else:
        notes = f"{tool} accepted {source.name}"
        result = "passed"

    return ProcessorCompatibilityResult(
        tool=tool,
        available=True,
        version=version,
        command=command,
        exit_code=completed.returncode,
        duration_seconds=duration,
        result=result,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        output_artifacts=existing_outputs,
        notes=notes,
    )


def _manual_only_result(tool: str, timeout_seconds: int) -> ProcessorCompatibilityResult:
    spec = PROCESSOR_TOOL_SPECS[tool]
    return ProcessorCompatibilityResult(
        tool=tool,
        available=False,
        version=None,
        command=["manual", "Adobe DNG SDK validation"],
        exit_code=None,
        duration_seconds=0.0,
        result="manual-only",
        stdout_tail=[],
        stderr_tail=[],
        output_artifacts=[],
        notes=f"{spec.install_hint} Timeout policy: {timeout_seconds}s for automated tools.",
    )


def _tool_version(command: list[str] | None, timeout_seconds: int) -> str | None:
    if command is None:
        return None
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    text = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    first_line = text.splitlines()[0] if text.splitlines() else f"exit code {completed.returncode}"
    return first_line[:200]


def _tail(text: str, *, max_lines: int = 20) -> list[str]:
    lines = text.strip().splitlines()
    return lines[-max_lines:]


def _last_line(lines: list[str]) -> str:
    return lines[-1] if lines else ""


def environment_label() -> str:
    return f"{platform.system()} Python {platform.python_version()}"
