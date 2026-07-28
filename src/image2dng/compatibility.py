from __future__ import annotations

import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ProcessorResult = Literal["passed", "skipped", "failed", "manual-only"]
ConverterResourceState = Literal[
    "installed-executable",
    "resource-present-not-installed",
    "missing",
]


@dataclass(frozen=True)
class ProcessorToolSpec:
    name: str
    version_command: list[str] | None
    install_hint: str
    manual_only: bool = False
    common_install_paths: tuple[Path, ...] = ()


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
    missing_output_artifacts: list[str] = field(default_factory=list)

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
            "missing_output_artifacts": self.missing_output_artifacts,
            "notes": self.notes,
        }


ADOBE_DNG_CONVERTER_TOOL = "adobe-dng-converter"
ADOBE_DNG_CONVERTER_INSTALLED_FILENAMES = ("adobe dng converter.exe",)
ADOBE_DNG_CONVERTER_RESOURCE_EXTENSIONS = {".exe", ".msi"}

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
        install_hint=(
            "Install Darktable manually. The evidence adapter checks PATH and the "
            "standard Windows install path."
        ),
        common_install_paths=(
            Path("C:/Program Files/darktable/bin/darktable-cli.exe"),
            Path("C:/Program Files (x86)/darktable/bin/darktable-cli.exe"),
        ),
    ),
    "rawtherapee-cli": ProcessorToolSpec(
        name="rawtherapee-cli",
        version_command=["rawtherapee-cli", "--version"],
        install_hint=(
            "Install RawTherapee manually. The evidence adapter checks PATH and the "
            "standard Windows install path."
        ),
        common_install_paths=(
            Path("C:/Program Files/RawTherapee/5.12/rawtherapee-cli.exe"),
            Path("C:/Program Files (x86)/RawTherapee/5.12/rawtherapee-cli.exe"),
        ),
    ),
    "adobe-dng-sdk": ProcessorToolSpec(
        name="adobe-dng-sdk",
        version_command=None,
        install_hint="Manual Adobe DNG SDK validation remains outside the automated gate.",
        manual_only=True,
    ),
    ADOBE_DNG_CONVERTER_TOOL: ProcessorToolSpec(
        name=ADOBE_DNG_CONVERTER_TOOL,
        version_command=None,
        install_hint=(
            "Install Adobe DNG Converter manually from Adobe, then rerun the "
            "Adobe converter regression script."
        ),
        common_install_paths=(
            Path("C:/Program Files/Adobe/Adobe DNG Converter/Adobe DNG Converter.exe"),
            Path("C:/Program Files (x86)/Adobe/Adobe DNG Converter/Adobe DNG Converter.exe"),
        ),
    ),
}


def processor_tool_inventory(
    timeout_seconds: int = 60,
    *,
    include_adobe_dng_converter: bool = False,
) -> dict[str, dict[str, object]]:
    inventory = {}
    for name, spec in PROCESSOR_TOOL_SPECS.items():
        if name == ADOBE_DNG_CONVERTER_TOOL and not include_adobe_dng_converter:
            continue
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

        executable, discovery = resolve_processor_executable(name)
        version_command = _resolved_version_command(spec, executable)
        inventory[name] = {
            "available": executable is not None,
            "manual_only": False,
            "executable": executable,
            "discovery": discovery,
            "version_command": spec.version_command,
            "resolved_version_command": version_command,
            "version": _tool_version(version_command, timeout_seconds)
            if version_command is not None
            else None,
            "timeout_seconds": timeout_seconds,
            "install_hint": spec.install_hint,
        }
    return inventory


def resolve_processor_executable(tool: str) -> tuple[str | None, str | None]:
    executable = shutil.which(tool)
    if executable is not None:
        return executable, "PATH"

    spec = PROCESSOR_TOOL_SPECS.get(tool)
    if spec is None:
        return None, None

    for candidate in spec.common_install_paths:
        if candidate.exists():
            return str(candidate), "common-install-path"
    return None, None


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


def run_adobe_dng_converter(
    dng_path: str | Path,
    output_dir: str | Path,
    *,
    converter_path: str | Path | None = None,
    timeout_seconds: int = 60,
) -> ProcessorCompatibilityResult:
    source = Path(dng_path).resolve()
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = adobe_dng_converter_output_path(source, root)
    return _run_processor_command(
        tool=ADOBE_DNG_CONVERTER_TOOL,
        source=source,
        command=[
            ADOBE_DNG_CONVERTER_TOOL,
            "-c",
            "-d",
            str(root),
            str(source),
        ],
        output_artifacts=[output],
        executable_override=str(converter_path) if converter_path is not None else None,
        timeout_seconds=timeout_seconds,
    )


def adobe_dng_converter_resource_state(
    *,
    converter_path: str | Path | None = None,
    adobe_dir: str | Path | None = Path("Adobe"),
) -> dict[str, object]:
    if converter_path is not None:
        path = Path(converter_path)
        return {
            "state": "installed-executable" if path.exists() else "missing",
            "source": "explicit" if path.exists() else "explicit-missing",
            "executable": str(path.resolve()) if path.exists() else None,
            "resource_path": None,
        }

    executable, discovery = resolve_processor_executable(ADOBE_DNG_CONVERTER_TOOL)
    if executable is not None:
        return {
            "state": "installed-executable",
            "source": discovery,
            "executable": executable,
            "resource_path": None,
        }

    resource_path = _find_local_adobe_converter_resource(adobe_dir)
    if resource_path is None:
        return {
            "state": "missing",
            "source": None,
            "executable": None,
            "resource_path": None,
        }

    state: ConverterResourceState = (
        "installed-executable"
        if resource_path.name.lower() in ADOBE_DNG_CONVERTER_INSTALLED_FILENAMES
        else "resource-present-not-installed"
    )
    return {
        "state": state,
        "source": "adobe-dir",
        "executable": str(resource_path.resolve()) if state == "installed-executable" else None,
        "resource_path": str(resource_path.resolve()),
    }


def adobe_dng_converter_output_path(source: str | Path, output_dir: str | Path) -> Path:
    return Path(output_dir) / Path(source).name


def _run_exiftool(
    source: Path,
    _output_dir: Path,
    timeout_seconds: int,
) -> ProcessorCompatibilityResult:
    return _run_processor_command(
        tool="exiftool",
        source=source,
        command=["exiftool", _processor_path(source)],
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
        command=["dcraw", "-T", "-w", "-O", _processor_path(output), _processor_path(source)],
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
        command=["darktable-cli", _processor_path(source), _processor_path(output)],
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
        command=[
            "rawtherapee-cli",
            "-Y",
            "-t",
            "-o",
            _processor_path(output),
            "-c",
            _processor_path(source),
        ],
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
    executable_override: str | None = None,
) -> ProcessorCompatibilityResult:
    executable = executable_override
    if executable is None:
        executable, _discovery = resolve_processor_executable(tool)
    resolved_version_command = _resolved_version_command(PROCESSOR_TOOL_SPECS[tool], executable)
    version = (
        _tool_version(resolved_version_command, timeout_seconds)
        if resolved_version_command is not None
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

    command = [executable, *command[1:]]
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
        missing_output_artifacts=missing_outputs,
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


def _resolved_version_command(
    spec: ProcessorToolSpec, executable: str | None
) -> list[str] | None:
    if spec.version_command is None or executable is None:
        return None
    return [executable, *spec.version_command[1:]]


def _tool_version(command: list[str] | None, timeout_seconds: int) -> str | None:
    if command is None:
        return None
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"[:200]
    text = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    first_line = text.splitlines()[0] if text.splitlines() else f"exit code {completed.returncode}"
    return first_line[:200]


def _tail(text: str, *, max_lines: int = 20) -> list[str]:
    lines = text.strip().splitlines()
    return lines[-max_lines:]


def _last_line(lines: list[str]) -> str:
    return lines[-1] if lines else ""


def _find_local_adobe_converter_resource(adobe_dir: str | Path | None) -> Path | None:
    if adobe_dir is None:
        return None
    root = Path(adobe_dir)
    if not root.is_dir():
        return None
    for candidate in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if not candidate.is_file():
            continue
        lowered = candidate.name.lower()
        compact = lowered.replace(" ", "").replace("_", "")
        if lowered in ADOBE_DNG_CONVERTER_INSTALLED_FILENAMES:
            return candidate
        if candidate.suffix.lower() in ADOBE_DNG_CONVERTER_RESOURCE_EXTENSIONS and (
            "dngconverter" in compact
        ):
            return candidate
    return None


def _processor_path(path: Path) -> str:
    return path.as_posix()


def environment_label() -> str:
    return f"{platform.system()} Python {platform.python_version()}"
