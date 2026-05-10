from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import png
import tifffile
from PIL import Image

from image2dng import OutputExistsError, convert
from image2dng.cli import main
from image2dng.compatibility import run_processor_compatibility
from image2dng.dng_writer import TAG_CFA_PATTERN, TAG_CFA_REPEAT_PATTERN_DIM, TAG_XMP
from image2dng.image_processing import build_cfa_buffer, build_linearraw_buffer
from image2dng.models import AIMetadataModel, CameraProfileModel
from image2dng.pipeline import GenerationScene, run_raw_native_batch
from image2dng.validate import TAG_MAKER_NOTE, validate_dng
from image2dng.xmp import XMP_AI_NAMESPACE


def _fake_processor_executable(command: str) -> str:
    return f"C:/fake/{command}.exe"


def test_generate_64x64_gradient_dng(tmp_path):
    input_path = tmp_path / "gradient.tif"
    output_path = tmp_path / "gradient.dng"
    gradient = _gradient_image(64, 64)
    tifffile.imwrite(input_path, gradient, photometric="rgb")

    exit_code = main(
        [
            str(input_path),
            str(output_path),
            "--input-space",
            "srgb",
            "--mode",
            "linearraw",
            "--iso",
            "100",
            "--white-balance",
            "6500",
            "--prompt-hash",
            "sha256:gradient",
            "--scene-description",
            "64x64 gradient",
            "--model-name",
            "unit-test",
            "--model-version",
            "0",
        ]
    )

    assert exit_code == 0
    result = validate_dng(output_path, run_smoke=False)
    assert result.ok, result.errors


def test_generate_16bit_ramp_dng(tmp_path):
    input_path = tmp_path / "ramp.tif"
    output_path = tmp_path / "ramp.dng"
    ramp = np.linspace(0, 65535, 64 * 64 * 3, dtype=np.uint16).reshape(64, 64, 3)
    tifffile.imwrite(input_path, ramp, photometric="rgb")

    raw, core = build_linearraw_buffer(input_path, "linear-rec709")
    assert raw.dtype == np.uint16
    assert raw.shape == (64, 64, 3)
    from image2dng.dng_writer import write_dng

    write_dng(
        output_path,
        raw,
        core,
        CameraProfileModel.from_white_balance(6500),
        AIMetadataModel(prompt_hash="sha256:ramp"),
    )

    result = validate_dng(output_path, run_smoke=False)
    assert result.ok, result.errors


def test_generate_16bit_png_dng(tmp_path):
    input_path = tmp_path / "gradient.png"
    output_path = tmp_path / "gradient-from-png.dng"
    _write_png(input_path, _gradient_image(32, 32))

    exit_code = main(
        [
            str(input_path),
            str(output_path),
            "--input-space",
            "linear-rec709",
            "--prompt-hash",
            "sha256:png",
        ]
    )

    assert exit_code == 0
    result = validate_dng(output_path, run_smoke=False)
    assert result.ok, result.errors


def test_generate_16bit_prophoto_rgba_tiff_dng(tmp_path):
    input_path = tmp_path / "prophoto-rgba.tif"
    output_path = tmp_path / "prophoto-rgba.dng"
    rgb = _gradient_image(24, 24)
    alpha = np.full((24, 24, 1), 65535, dtype=np.uint16)
    tifffile.imwrite(input_path, np.concatenate([rgb, alpha], axis=2), photometric="rgb")

    exit_code = main(
        [
            str(input_path),
            str(output_path),
            "--input-space",
            "prophoto-rgb",
            "--prompt-hash",
            "sha256:prophoto",
        ]
    )

    assert exit_code == 0
    result = validate_dng(output_path, run_smoke=False)
    assert result.ok, result.errors


def test_public_convert_api_returns_result(tmp_path):
    input_path = tmp_path / "api-input.tif"
    output_path = tmp_path / "api-output.dng"
    tifffile.imwrite(input_path, _gradient_image(24, 24), photometric="rgb")

    result = convert(
        input_path=input_path,
        output_path=output_path,
        input_space="srgb",
        prompt_hash="sha256:api",
        scene_description="api test scene",
    )

    assert result.output_path == output_path
    assert result.mode == "linearraw"
    assert result.width == 24
    assert result.height == 24
    assert result.prompt_hash == "sha256:api"
    assert result.raw_data_unique_id is not None
    validation = validate_dng(output_path, run_smoke=False)
    assert validation.ok, validation.errors


def test_public_convert_api_generates_cfa_dng(tmp_path):
    input_path = tmp_path / "api-cfa-input.tif"
    output_path = tmp_path / "api-cfa-output.dng"
    tifffile.imwrite(input_path, _gradient_image(24, 24), photometric="rgb")

    result = convert(
        input_path=input_path,
        output_path=output_path,
        input_space="linear-rec709",
        mode="cfa",
        cfa_pattern="rggb",
        prompt_hash="sha256:cfa",
    )

    assert result.mode == "cfa"
    validation = validate_dng(output_path, run_smoke=False)
    assert validation.ok, validation.errors
    with tifffile.TiffFile(output_path) as tif:
        page = tif.pages[0]
        assert page.asarray().shape == (24, 24)
        assert page.tags[TAG_CFA_REPEAT_PATTERN_DIM].value == (2, 2)
        assert tuple(page.tags[TAG_CFA_PATTERN].value) == (0, 1, 1, 2)
        xmp = page.tags[TAG_XMP].value.decode("utf-8")
    assert 'xmpAI:rawMode="cfa"' in xmp
    assert 'xmpAI:cfaPattern="rggb"' in xmp


def test_raw_native_pipeline_generates_dng_jpeg_and_manifest(tmp_path):
    scene = GenerationScene(
        slug="unit-chart",
        prompt="unit test chart",
        description="unit test raw-native pipeline scene",
        lighting="test D65",
        style="chart-ramp",
        seed=123,
        width=32,
        height=32,
    )

    result = run_raw_native_batch(tmp_path / "raw-native", scenes=[scene])

    assert result.manifest_path.exists()
    assert result.sample_index_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "image2dng.raw_native_node_batch.v1"
    assert manifest["decision"]["core_pipeline"] == "built-in image2dng Python graph"
    assert manifest["scenes"][0]["slug"] == "unit-chart"
    sample_index = json.loads(result.sample_index_path.read_text(encoding="utf-8"))
    assert sample_index["schema"] == "image2dng.raw_native_sample_index.v1"
    assert sample_index["scene_count"] == 1
    assert sample_index["all_validations_ok"] is True
    assert sample_index["samples"][0]["slug"] == "unit-chart"

    outputs = result.scenes[0].outputs
    assert validate_dng(outputs["linearraw_dng"], run_smoke=False).ok
    assert validate_dng(outputs["cfa_dng"], run_smoke=False).ok
    with Image.open(outputs["linearraw_jpeg"]) as image:
        assert image.format == "JPEG"
        assert image.size == (32, 32)
    with Image.open(outputs["cfa_jpeg"]) as image:
        assert image.format == "JPEG"
        assert image.size == (32, 32)


def test_raw_native_manifest_contract_is_stable(tmp_path):
    scene = GenerationScene(
        slug="contract-chart",
        prompt="contract test chart",
        description="manifest contract test scene",
        lighting="contract D65",
        style="chart-ramp",
        seed=456,
        width=24,
        height=24,
    )

    result = run_raw_native_batch(tmp_path / "raw-native-contract", scenes=[scene])
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    scene_manifest = manifest["scenes"][0]

    assert manifest["schema"] == "image2dng.raw_native_node_batch.v1"
    assert manifest["graph"] == {
        "nodes": [
            "PromptIntentNode",
            "SceneLinearGeneratorNode",
            "VirtualCameraLinearRawNode",
            "VirtualCameraCfaNode",
            "JpegPreviewRenderNode",
            "DngValidationNode",
        ],
        "primary_artifact": "synthetic DNG",
        "preview_artifact": "JPEG rendered from generated RAW buffers",
    }
    assert scene_manifest.keys() >= {
        "slug",
        "prompt_hash",
        "outputs",
        "raw_data_unique_ids",
        "validations",
        "nodes",
    }
    assert scene_manifest["raw_data_unique_ids"]["linearraw"].startswith("sha256:")
    assert scene_manifest["raw_data_unique_ids"]["cfa"].startswith("sha256:")
    assert scene_manifest["validations"]["linearraw"]["ok"] is True
    assert scene_manifest["validations"]["cfa"]["ok"] is True

    node_types = [node["type"] for node in scene_manifest["nodes"]]
    assert node_types == manifest["graph"]["nodes"]
    for node in scene_manifest["nodes"]:
        assert node.keys() == {"id", "type", "inputs", "outputs", "parameters"}
        assert isinstance(node["inputs"], dict)
        assert isinstance(node["outputs"], dict)
        assert isinstance(node["parameters"], dict)

    for artifact_path in scene_manifest["outputs"].values():
        assert Path(artifact_path).exists()

    sample_index = json.loads(result.sample_index_path.read_text(encoding="utf-8"))
    assert sample_index == {
        "schema": "image2dng.raw_native_sample_index.v1",
        "output_dir": str(tmp_path / "raw-native-contract"),
        "scene_count": 1,
        "all_validations_ok": True,
        "samples": [
            {
                "slug": "contract-chart",
                "prompt_hash": scene_manifest["prompt_hash"],
                "artifacts": scene_manifest["outputs"],
                "validation_ok": {"linearraw": True, "cfa": True},
                "raw_data_unique_ids": scene_manifest["raw_data_unique_ids"],
            }
        ],
    }


def test_visual_demo_generates_phase3_evidence(tmp_path):
    output_dir = tmp_path / "visual-demo"

    assert _run_visual_demo(output_dir) == 0

    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "image2dng.visual_demo_manifest.v1"
    assert [asset["slug"] for asset in manifest["assets"]] == [
        "chart-gradient",
        "skin-tones",
        "daily-objects",
    ]

    for asset in manifest["assets"]:
        assert set(asset["outputs"]) == {
            "phase_15_linearraw",
            "phase_2_cfa",
            "phase_3_linearraw_noisy",
            "phase_3_cfa_noisy",
        }
        assert set(asset["previews"]) == {
            "source",
            "linearraw",
            "cfa",
            "linearraw_noisy",
            "cfa_noisy",
            "noise_diff",
        }
        assert all(asset["validation_ok"].values())
        for output in asset["outputs"].values():
            assert _resolve_manifest_path(output, output_dir).exists()
        for preview in asset["previews"].values():
            with Image.open(_resolve_manifest_path(preview, output_dir)) as image:
                assert image.format == "PNG"
                assert image.size == (256, 256)
        for validation_path in asset["validations"].values():
            validation = json.loads(
                _resolve_manifest_path(validation_path, output_dir).read_text(encoding="utf-8")
            )
            assert validation["ok"] is True

    contact_sheets = {sheet["path"]: sheet for sheet in manifest["contact_sheets"]}
    assert set(contact_sheets) == {
        "contact-sheets\\phase-overview.png",
        "contact-sheets\\sensor-effects-comparison.png",
        "contact-sheets\\cfa-pattern-comparison.png",
    }
    for sheet in manifest["contact_sheets"]:
        with Image.open(_resolve_manifest_path(sheet["path"], output_dir)) as image:
            assert image.format == "PNG"
            assert image.width > 0
            assert image.height > 0

    cfa = manifest["cfa_pattern_comparison"]
    assert cfa["patterns"] == ["rggb", "bggr", "grbg", "gbrg"]
    assert cfa["all_validations_ok"] is True
    assert {sample["pattern"] for sample in cfa["samples"]} == {"rggb", "bggr", "grbg", "gbrg"}

    sensor = manifest["sensor_effects_comparison"]
    assert sensor["all_validations_ok"] is True
    assert {sample["name"] for sample in sensor["samples"]} == {
        "none",
        "shot",
        "read",
        "row",
        "combined",
    }


def test_compatibility_evidence_handles_missing_optional_tools(tmp_path, monkeypatch):
    module = _load_script_module("generate_compatibility_evidence")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", lambda _command: None)

    output_dir = tmp_path / "compatibility-evidence"
    assert module.main(["--output-dir", str(output_dir)]) == 0

    report_path = output_dir / "compatibility-report.json"
    summary_path = output_dir / "compatibility-summary.md"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["schema"] == "image2dng.compatibility_evidence.v2"
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["install_policy"] == {
        "auto_install": False,
        "missing_tool_policy": "skipped",
        "available_tool_failure_policy": "failed",
        "notes": "Install hints are dry-run guidance only; this script never installs RAW tools.",
    }
    assert len(report["fixtures"]) >= 11
    assert all(fixture["validation_ok"] is True for fixture in report["fixtures"])
    assert all(Path(fixture["dng"]).exists() for fixture in report["fixtures"])
    assert all(Path(fixture["validation_json"]).exists() for fixture in report["fixtures"])
    assert all("processor_results" in fixture for fixture in report["fixtures"])

    optional_tools = {"exiftool", "dcraw", "darktable-cli", "rawtherapee-cli"}
    optional_entries = [entry for entry in report["matrix"] if entry["tool"] in optional_tools]
    assert optional_entries
    assert {entry["result"] for entry in optional_entries} == {"skipped"}
    assert all(entry["notes"] == "skipped: not found" for entry in optional_entries)
    assert all(entry["exit_code"] is None for entry in optional_entries)

    adobe_entries = [entry for entry in report["matrix"] if entry["tool"] == "adobe-dng-sdk"]
    assert adobe_entries
    assert {entry["result"] for entry in adobe_entries} == {"manual-only"}
    summary = summary_path.read_text(encoding="utf-8")
    assert "| Fixture | Tool | Result | Evidence | Notes |" in summary
    assert "adobe-dng-sdk" in summary
    assert "Auto install: `False`" in summary


def test_compatibility_evidence_fails_when_available_processor_fails(tmp_path, monkeypatch):
    module = _load_script_module("generate_compatibility_evidence")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", _fake_processor_executable)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=False, return_code=7),
    )

    output_dir = tmp_path / "compatibility-evidence-failed"
    assert module.main(["--output-dir", str(output_dir)]) == 1

    report = json.loads((output_dir / "compatibility-report.json").read_text(encoding="utf-8"))
    assert report["schema"] == "image2dng.compatibility_evidence.v2"
    assert report["ok"] is False
    assert report["errors"]
    assert any("failed exiftool" in error for error in report["errors"])
    failed_entries = [entry for entry in report["matrix"] if entry["result"] == "failed"]
    assert failed_entries
    assert {entry["exit_code"] for entry in failed_entries} == {7}


def test_processor_compatibility_records_successful_fake_tools(tmp_path, monkeypatch):
    dng_path = _write_test_dng(tmp_path, prompt_hash="sha256:processor-success")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", _fake_processor_executable)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=True, return_code=0),
    )

    results = run_processor_compatibility(dng_path, tmp_path / "processors")
    payload = {result.tool: result.to_dict() for result in results}

    assert payload["exiftool"]["result"] == "passed"
    assert payload["dcraw"]["result"] == "passed"
    assert payload["darktable-cli"]["result"] == "passed"
    assert payload["rawtherapee-cli"]["result"] == "passed"
    assert payload["adobe-dng-sdk"]["result"] == "manual-only"
    assert payload["dcraw"]["exit_code"] == 0
    assert payload["dcraw"]["output_artifacts"]
    assert Path(payload["dcraw"]["output_artifacts"][0]).exists()


def test_processor_compatibility_records_command_failure(tmp_path, monkeypatch):
    dng_path = _write_test_dng(tmp_path, prompt_hash="sha256:processor-failed")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", _fake_processor_executable)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=False, return_code=7),
    )

    results = run_processor_compatibility(dng_path, tmp_path / "processors")
    payload = {result.tool: result.to_dict() for result in results}

    assert payload["exiftool"]["result"] == "failed"
    assert payload["dcraw"]["result"] == "failed"
    assert payload["dcraw"]["exit_code"] == 7
    assert "simulated failure" in payload["dcraw"]["notes"]


def test_processor_compatibility_fails_when_export_output_is_missing(tmp_path, monkeypatch):
    dng_path = _write_test_dng(tmp_path, prompt_hash="sha256:processor-missing-output")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", _fake_processor_executable)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=False, return_code=0),
    )

    results = run_processor_compatibility(dng_path, tmp_path / "processors")
    payload = {result.tool: result.to_dict() for result in results}

    assert payload["exiftool"]["result"] == "passed"
    assert payload["dcraw"]["result"] == "failed"
    assert "missing output artifact" in payload["dcraw"]["notes"]


def test_demo_review_bundle_generates_portable_index(tmp_path, monkeypatch):
    module = _load_script_module("generate_demo_review_bundle")
    monkeypatch.setattr("shutil.which", lambda _command: None)
    monkeypatch.setattr("image2dng.validate.shutil.which", lambda _command: None)

    output_dir = tmp_path / "review-bundle"
    assert module.main(
        ["--output-dir", str(output_dir), "--skip-baseline-quality-gates"]
    ) == 0

    report_path = output_dir / "review-bundle-report.json"
    index_path = output_dir / "index.md"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["schema"] == "image2dng.demo_review_bundle.v1"
    assert report["ok"] is True
    assert report["errors"] == []
    assert index_path.exists()
    assert {command["name"] for command in report["commands"]} == {
        "visual-demo",
        "raw-native-node-batch",
        "development-baseline",
        "compatibility-evidence",
    }
    assert all(command["exit_code"] == 0 for command in report["commands"])

    artifact_paths = [artifact["bundle_path"] for artifact in report["artifacts"]]
    assert all(not Path(path).is_absolute() for path in artifact_paths)
    assert all((output_dir / path).exists() for path in artifact_paths)
    assert all(artifact["sha256"] for artifact in report["artifacts"])

    kinds = {}
    for artifact in report["artifacts"]:
        kinds[artifact["kind"]] = kinds.get(artifact["kind"], 0) + 1
    assert kinds["contact-sheet"] >= 3
    assert kinds["representative-dng"] >= 14
    assert kinds["validation-json"] >= 14
    assert kinds["report"] >= 3
    assert kinds["manifest"] >= 3

    dng_names = [
        artifact["bundle_path"]
        for artifact in report["artifacts"]
        if artifact["kind"] == "representative-dng"
    ]
    assert any("linearraw" in name for name in dng_names)
    assert any("cfa" in name for name in dng_names)
    assert any("noisy" in name for name in dng_names)
    assert any("compatibility/" in name for name in dng_names)

    index = index_path.read_text(encoding="utf-8")
    assert "Review Entry Points" in index
    assert "review-bundle-report.json" in index
    assert "uv run python scripts/generate_demo_review_bundle.py" in index


def test_raw_processor_setup_audit_writes_dry_run_package(tmp_path, monkeypatch):
    module = _load_script_module("audit_raw_processor_setup")

    def which(command):
        if command in {"winget", "scoop", "choco"}:
            return f"C:/fake/{command}.exe"
        if command == "exiftool":
            return "C:/fake/exiftool.exe"
        return None

    monkeypatch.setattr(module.shutil, "which", which)
    output_dir = tmp_path / "setup-audit"

    assert module.main(["--output-dir", str(output_dir), "--skip-package-search"]) == 0

    report = json.loads((output_dir / "setup-audit-report.json").read_text(encoding="utf-8"))
    runbook = (output_dir / "setup-runbook.md").read_text(encoding="utf-8")
    prompt = (output_dir / "external-review-prompt.md").read_text(encoding="utf-8")

    assert report["schema"] == "image2dng.raw_processor_setup_audit.v1"
    assert report["policy"] == {
        "auto_install": False,
        "search_only": True,
        "install_requires_user_approval": True,
        "notes": "This audit never installs or upgrades RAW processor tools.",
    }
    assert set(report["tools"]) == {"dcraw", "darktable-cli", "rawtherapee-cli"}
    assert report["tools"]["darktable-cli"]["recommendation"]["priority"] == "recommended-first"
    assert report["tools"]["dcraw"]["recommendation"]["priority"] == "legacy-optional"
    assert all(
        search["status"] == "not-run"
        for tool in report["tools"].values()
        for search in tool["package_searches"]
    )
    assert "uv run python scripts/generate_compatibility_evidence.py" in runbook
    assert "Auto install is `False`" in prompt


def test_raw_processor_setup_audit_records_search_version_hints(tmp_path, monkeypatch):
    module = _load_script_module("audit_raw_processor_setup")
    monkeypatch.setattr(module.shutil, "which", lambda command: f"C:/fake/{command}.exe")
    monkeypatch.setattr(module.subprocess, "run", _fake_setup_audit_search_run)

    output_dir = tmp_path / "setup-audit-search"
    assert module.main(["--output-dir", str(output_dir), "--search-timeout-seconds", "1"]) == 0

    report = json.loads((output_dir / "setup-audit-report.json").read_text(encoding="utf-8"))
    searches = [
        search
        for tool in report["tools"].values()
        for search in tool["package_searches"]
        if search["query"] == "darktable"
    ]

    assert searches
    assert {search["status"] for search in searches} == {"completed"}
    assert {search["version_hint"] for search in searches} == {"4.8.1"}
    assert all("search-only" in search["notes"] for search in searches)
    assert report["ok"] is True
    assert report["errors"] == []


def test_raw_processor_setup_audit_ignores_non_exact_version_hints(tmp_path, monkeypatch):
    module = _load_script_module("audit_raw_processor_setup")
    monkeypatch.setattr(module.shutil, "which", lambda command: f"C:/fake/{command}.exe")

    def fake_run(command, **_kwargs):
        executable = Path(command[0]).name.lower()
        query = command[2] if executable.startswith(("winget", "choco")) else command[-1]
        stdout = {
            "dcraw": "rawtherapee|5.8.0\nufraw|0.19.2\n",
            "rawtherapee": "art|1.26.4\nrawtherapee|5.8.0\n",
        }.get(query, f"{query}|4.8.1\n")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    output_dir = tmp_path / "setup-audit-exact-search"
    assert module.main(["--output-dir", str(output_dir), "--search-timeout-seconds", "1"]) == 0

    report = json.loads((output_dir / "setup-audit-report.json").read_text(encoding="utf-8"))
    dcraw_choco = next(
        search
        for search in report["tools"]["dcraw"]["package_searches"]
        if search["manager"] == "choco" and search["query"] == "dcraw"
    )
    rawtherapee_choco = next(
        search
        for search in report["tools"]["rawtherapee-cli"]["package_searches"]
        if search["manager"] == "choco" and search["query"] == "rawtherapee"
    )

    assert dcraw_choco["version_hint"] is None
    assert rawtherapee_choco["version_hint"] == "5.8.0"


def test_sensor_effects_are_deterministic_and_recorded(tmp_path):
    input_path = tmp_path / "sensor-effects.tif"
    output_a = tmp_path / "sensor-effects-a.dng"
    output_b = tmp_path / "sensor-effects-b.dng"
    tifffile.imwrite(input_path, _gradient_image(24, 24), photometric="rgb")

    result_a = convert(
        input_path=input_path,
        output_path=output_a,
        input_space="linear-rec709",
        shot_noise=0.01,
        read_noise=0.002,
        row_noise=0.001,
        sensor_effect_seed=1234,
        prompt_hash="sha256:sensor-effects",
    )
    result_b = convert(
        input_path=input_path,
        output_path=output_b,
        input_space="linear-rec709",
        shot_noise=0.01,
        read_noise=0.002,
        row_noise=0.001,
        sensor_effect_seed=1234,
        prompt_hash="sha256:sensor-effects",
    )

    assert result_a.raw_data_unique_id == result_b.raw_data_unique_id
    validation = validate_dng(output_a, run_smoke=False)
    assert validation.ok, validation.errors
    with tifffile.TiffFile(output_a) as tif:
        xmp = tif.pages[0].tags[TAG_XMP].value.decode("utf-8")
    assert 'xmpAI:sensorNoiseModel="synthetic-simple-v1"' in xmp
    assert 'xmpAI:shotNoise="0.01"' in xmp
    assert 'xmpAI:readNoise="0.002"' in xmp
    assert 'xmpAI:rowNoise="0.001"' in xmp
    assert 'xmpAI:sensorEffectSeed="1234"' in xmp


def test_cli_rejects_negative_sensor_effects(tmp_path, capsys):
    input_path = tmp_path / "negative-noise.tif"
    output_path = tmp_path / "negative-noise.dng"
    tifffile.imwrite(input_path, _gradient_image(8, 8), photometric="rgb")

    exit_code = main([str(input_path), str(output_path), "--shot-noise", "-0.1"])

    assert exit_code == 3
    assert "shot_noise must be non-negative" in capsys.readouterr().err


def test_cfa_mosaic_uses_requested_pattern(tmp_path):
    input_path = tmp_path / "cfa-pattern.tif"
    source = np.zeros((2, 2, 3), dtype=np.uint16)
    source[0, 0] = (1000, 2000, 3000)
    source[0, 1] = (4000, 5000, 6000)
    source[1, 0] = (7000, 8000, 9000)
    source[1, 1] = (10000, 11000, 12000)
    tifffile.imwrite(input_path, source, photometric="rgb")

    raw, core = build_cfa_buffer(
        input_path,
        "linear-rec709",
        cfa_pattern="bggr",
        black_level=0,
        white_level=65535,
    )

    assert core.photometric == "ColorFilterArray"
    assert raw.shape == (2, 2)
    assert raw[0, 0] == 3000
    assert raw[0, 1] == 5000
    assert raw[1, 0] == 8000
    assert raw[1, 1] == 10000


def test_public_convert_refuses_existing_output_without_overwrite(tmp_path):
    input_path = tmp_path / "api-input.tif"
    output_path = tmp_path / "api-output.dng"
    tifffile.imwrite(input_path, _gradient_image(8, 8), photometric="rgb")
    output_path.write_bytes(b"existing")

    try:
        convert(input_path=input_path, output_path=output_path)
    except OutputExistsError:
        pass
    else:
        raise AssertionError("expected OutputExistsError")


def test_cli_returns_usage_error_for_invalid_metadata(tmp_path, capsys):
    input_path = tmp_path / "invalid-metadata.tif"
    output_path = tmp_path / "invalid-metadata.dng"
    tifffile.imwrite(input_path, _gradient_image(8, 8), photometric="rgb")

    exit_code = main([str(input_path), str(output_path), "--iso", "0"])

    assert exit_code == 3
    assert "iso must be positive" in capsys.readouterr().err


def test_metadata_round_trip(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:roundtrip")
    with tifffile.TiffFile(output_path) as tif:
        xmp = tif.pages[0].tags[TAG_XMP].value.decode("utf-8")
    root = ET.fromstring(xmp)
    description = root.find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert description is not None
    assert description.attrib[f"{{{XMP_AI_NAMESPACE}}}modelName"] == "unit-test-model"
    assert description.attrib[f"{{{XMP_AI_NAMESPACE}}}promptHash"] == "sha256:roundtrip"
    assert description.attrib[f"{{{XMP_AI_NAMESPACE}}}cameraParametersAreSimulated"] == "True"


def test_synthetic_provenance_is_required(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:provenance")
    result = validate_dng(output_path, run_smoke=False)
    assert result.ok, result.errors

    with tifffile.TiffFile(output_path) as tif:
        xmp = tif.pages[0].tags[TAG_XMP].value.decode("utf-8")
    assert 'xmpAI:provenanceType="synthetic"' in xmp


def test_plaintext_prompt_is_not_written_by_default(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:privacy")
    with tifffile.TiffFile(output_path) as tif:
        xmp = tif.pages[0].tags[TAG_XMP].value.decode("utf-8")

    assert "xmpAI:promptHash" in xmp
    assert "xmpAI:promptPlaintext" not in xmp


def test_makernote_is_not_written(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:no-makernote")
    with tifffile.TiffFile(output_path) as tif:
        assert TAG_MAKER_NOTE not in tif.pages[0].tags


def test_validate_json_output(tmp_path, capsys):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:json")

    exit_code = main(["validate", str(output_path), "--no-smoke", "--json"])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["path"] == str(output_path)
    assert report["checks"] == [
        {
            "message": "DNG structural checks passed",
            "name": "structure",
            "status": "passed",
        }
    ]
    assert report["errors"] == []
    assert report["warnings"] == []
    assert report["smoke_tests"] == {}


def test_missing_smoke_tools_are_reported_as_skipped(tmp_path, monkeypatch):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:smoke-skipped")
    monkeypatch.setattr("image2dng.validate.shutil.which", lambda _command: None)

    result = validate_dng(output_path, run_smoke=True)

    assert result.ok, result.errors
    assert result.smoke_tests == {
        "exiftool": "skipped: not found",
        "dcraw": "skipped: not found",
        "darktable-cli": "skipped: not found",
        "rawtherapee-cli": "skipped: not found",
    }
    smoke_checks = [check for check in result.checks if check.name.startswith("smoke:")]
    assert {check.status for check in smoke_checks} == {"skipped"}


def _write_test_dng(tmp_path, *, prompt_hash: str):
    input_path = tmp_path / "input.tif"
    output_path = tmp_path / "output.dng"
    tifffile.imwrite(input_path, _gradient_image(16, 16), photometric="rgb")
    raw, core = build_linearraw_buffer(input_path, "srgb")
    from image2dng.dng_writer import write_dng

    write_dng(
        output_path,
        raw,
        core,
        CameraProfileModel.from_white_balance(6500),
        AIMetadataModel(
            model_name="unit-test-model",
            model_version="1",
            prompt_hash=prompt_hash,
            scene_description="metadata round trip",
        ),
    )
    return output_path


def _gradient_image(width: int, height: int) -> np.ndarray:
    x = np.linspace(0, 65535, width, dtype=np.uint16)
    y = np.linspace(0, 65535, height, dtype=np.uint16)
    red = np.tile(x, (height, 1))
    green = np.tile(y[:, np.newaxis], (1, width))
    blue = ((red.astype(np.uint32) + green.astype(np.uint32)) // 2).astype(np.uint16)
    return np.stack([red, green, blue], axis=2)


def _write_png(path, image: np.ndarray) -> None:
    height, width, samples = image.shape
    assert samples == 3
    writer = png.Writer(width=width, height=height, bitdepth=16, greyscale=False)
    with path.open("wb") as handle:
        writer.write(handle, image.reshape(height, width * samples).tolist())


def _fake_processor_run(*, create_outputs: bool, return_code: int):
    def run(command, **_kwargs):
        version_flags = {"-ver", "-h", "--version"}
        if any(flag in command for flag in version_flags):
            return subprocess.CompletedProcess(command, 0, stdout="fake-tool 1.0\n", stderr="")

        if create_outputs and return_code == 0:
            _create_fake_processor_output(command)
        stderr = "" if return_code == 0 else "simulated failure\n"
        return subprocess.CompletedProcess(
            command,
            return_code,
            stdout="fake stdout\n",
            stderr=stderr,
        )

    return run


def _fake_setup_audit_search_run(command, **_kwargs):
    executable = Path(command[0]).name.lower()
    if executable.startswith("winget"):
        query = command[2]
        package_id = {
            "darktable": "darktable.darktable",
            "rawtherapee": "RawTherapee.RawTherapee",
        }.get(query, query)
        stdout = f"{query} {package_id} 4.8.1\n"
    elif executable.startswith("choco"):
        query = command[2]
        stdout = f"{query}|4.8.1\n"
    else:
        query = command[-1]
        stdout = f"{query} 4.8.1\n"
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=stdout,
        stderr="",
    )


def _create_fake_processor_output(command: list[str]) -> None:
    if command[0] == "dcraw":
        output = Path(command[command.index("-O") + 1])
    elif command[0] == "darktable-cli":
        output = Path(command[2])
    elif command[0] == "rawtherapee-cli":
        output = Path(command[command.index("-o") + 1])
    else:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"fake processor output")


def _resolve_manifest_path(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _run_visual_demo(output_dir: Path) -> int:
    module = _load_script_module("generate_visual_demo")
    return module.main(["--output-dir", str(output_dir)])


def _load_script_module(name: str):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
