from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import png
import pytest
import tifffile
from PIL import Image

from image2dng import OutputExistsError, convert
from image2dng.cli import main
from image2dng.compatibility import (
    PROCESSOR_TOOL_SPECS,
    ProcessorToolSpec,
    adobe_dng_converter_resource_state,
    processor_tool_inventory,
    run_processor_compatibility,
)
from image2dng.dng_writer import (
    TAG_CFA_PATTERN,
    TAG_CFA_REPEAT_PATTERN_DIM,
    TAG_DEFAULT_SCALE,
    TAG_DNG_BACKWARD_VERSION,
    TAG_DNG_VERSION,
    TAG_MAKE,
    TAG_MODEL,
    TAG_NEW_SUBFILE_TYPE,
    TAG_ORIENTATION,
    TAG_RAW_DATA_UNIQUE_ID,
    TAG_UNIQUE_CAMERA_MODEL,
    TAG_XMP,
)
from image2dng.image_processing import build_cfa_buffer, build_linearraw_buffer
from image2dng.models import PHOTOMETRIC_LINEAR_RAW, AIMetadataModel, CameraProfileModel
from image2dng.pipeline import (
    ExternalSceneLinearInput,
    GenerationScene,
    _validation_summary,
    load_external_scene_manifest,
    run_external_scene_linear_batch,
    run_raw_native_batch,
)
from image2dng.semantic_reaction import (
    HIGHLIGHT_CLIPPING_REACTION_MODEL,
    REGION_EXPOSURE_REACTION_MODEL,
    SEMANTIC_REACTION_CHAIN_MODEL,
    apply_highlight_clipping_policy,
    apply_region_exposure_reaction,
    apply_semantic_reaction_chain,
    load_semantic_payload,
    semantic_reaction_model_registry,
)
from image2dng.semantic_scene import SEMANTIC_SCENE_SCHEMA, validate_semantic_scene
from image2dng.validate import (
    TAG_MAKER_NOTE,
    find_raw_image_page,
    inspect_adobe_converted_dng,
    validate_dng,
)
from image2dng.xmp import XMP_AI_NAMESPACE


def _fake_processor_executable(command: str) -> str:
    return f"C:/fake/{command}.exe"


def _missing_local_markdown_links(markdown_path: Path) -> list[str]:
    link_pattern = re.compile(r"!?\[[^\]]+\]\(([^)]+)\)")
    markdown = markdown_path.read_text(encoding="utf-8")
    missing = []

    for match in link_pattern.finditer(markdown):
        href = match.group(1).split("#", 1)[0]
        if not href or href.startswith(("http://", "https://", "#")):
            continue

        target = (markdown_path.parent / unquote(href)).resolve()
        if not target.exists():
            missing.append(href)

    return missing


def _first_markdown_link_with_label(markdown_path: Path, label: str) -> Path:
    markdown = markdown_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"{re.escape(label)}:\s*\[[^\]]+\]\(([^)]+)\)")
    match = pattern.search(markdown)
    if match is None:
        raise AssertionError(f"missing {label!r} link in {markdown_path}")
    href = match.group(1).split("#", 1)[0]
    return (markdown_path.parent / unquote(href)).resolve()


def test_readme_local_markdown_links_resolve():
    repo_root = Path(__file__).resolve().parents[1]

    for readme_name in ("README.md", "README.zh-tw.md"):
        readme_path = repo_root / readme_name
        assert _missing_local_markdown_links(readme_path) == []


def test_readme_language_links_are_reciprocal():
    repo_root = Path(__file__).resolve().parents[1]
    english = (repo_root / "README.md").read_text(encoding="utf-8")
    zh_tw = (repo_root / "README.zh-tw.md").read_text(encoding="utf-8")

    assert "[繁體中文](README.zh-tw.md)" in english
    assert "[English](README.md)" in zh_tw


def test_readme_workflows_keep_local_output_and_install_boundaries():
    repo_root = Path(__file__).resolve().parents[1]
    english = (repo_root / "README.md").read_text(encoding="utf-8")
    zh_tw = (repo_root / "README.zh-tw.md").read_text(encoding="utf-8")

    assert "`demo-output/` is local output and binary samples should not be committed." in english
    assert (
        "`demo-output/` remains local output and binary samples should not be committed."
        in english
    )
    assert "It does not install or update any tool" in english
    assert "installing one RAW processor requires explicit user approval" in english
    assert (
        "preserves already generated source reports and manifests as diagnostic evidence"
        in english
    )
    assert "`demo-output/` 是本機輸出資料夾，不應提交 binary 樣片。" in zh_tw
    assert "`demo-output/` 仍是本機輸出資料夾，不應提交 binary 樣片。" in zh_tw
    assert "它不會安裝或更新任何工具" in zh_tw
    assert "安裝其中一個 RAW processor 必須等使用者明確批准" in zh_tw
    assert "source reports / manifests 作為診斷 evidence" in zh_tw


def test_docs_local_markdown_links_resolve():
    repo_root = Path(__file__).resolve().parents[1]
    broken_links = {}

    for path in sorted((repo_root / "docs").rglob("*.md")):
        missing = _missing_local_markdown_links(path)
        if missing:
            broken_links[str(path.relative_to(repo_root))] = missing

    assert broken_links == {}


def test_public_docs_reference_zh_tw_sources_bidirectionally():
    repo_root = Path(__file__).resolve().parents[1]
    public_docs = sorted(
        path
        for path in (repo_root / "docs").glob("*.md")
        if "Traditional Chinese source manuscript:" in path.read_text(encoding="utf-8")
    )

    assert public_docs
    for public_doc in public_docs:
        zh_source = _first_markdown_link_with_label(
            public_doc,
            "Traditional Chinese source manuscript",
        )
        assert zh_source.is_relative_to(repo_root / "docs" / "i18n" / "zh-TW")
        english_baseline = _first_markdown_link_with_label(
            zh_source,
            "English public baseline",
        )
        assert english_baseline == public_doc.resolve()


def test_i18n_english_docs_have_zh_tw_counterparts():
    repo_root = Path(__file__).resolve().parents[1]
    en_docs = sorted((repo_root / "docs" / "i18n" / "en").glob("*.md"))
    zh_tw_dir = repo_root / "docs" / "i18n" / "zh-TW"

    assert en_docs
    missing = [path.name for path in en_docs if not (zh_tw_dir / path.name).exists()]

    assert missing == []


def test_compatibility_docs_keep_setup_audit_safety_boundary():
    repo_root = Path(__file__).resolve().parents[1]
    english = (repo_root / "docs" / "compatibility.md").read_text(encoding="utf-8")
    zh_tw = (
        repo_root / "docs" / "i18n" / "zh-TW" / "compatibility-evidence.md"
    ).read_text(encoding="utf-8")

    assert "The setup audit never installs or upgrades RAW processor tools." in english
    assert "If a tool is approved and installed later" in english
    assert (
        "generated fixtures live under `demo-output/compatibility-evidence/` "
        "and should not be committed as binary artifacts."
        in english
    )
    assert (
        "fixture integrity table with DNG / validation JSON byte counts and SHA-256"
        in english
    )
    assert "Dedicated local SDK validation scripts can produce sidecar evidence" in english
    assert "scripts/run_adobe_dng_sdk_validation.py" in english
    assert "scripts/verify_adobe_validation_stack.py" in english
    assert "不會安裝、不會升級任何 RAW processor。" in zh_tw
    assert "若使用者後續批准安裝其中一個工具" in zh_tw
    assert "`demo-output/` 是本機輸出，不應提交 binary fixtures。" in zh_tw
    assert "fixture integrity 表" in zh_tw
    assert "本機 SDK evidence 由 dedicated local validation scripts" in zh_tw
    assert "scripts/run_adobe_dng_sdk_validation.py" in zh_tw
    assert "scripts/verify_adobe_validation_stack.py" in zh_tw


def test_dng_tag_contract_keeps_adobe_sdk_local_evidence_boundary():
    repo_root = Path(__file__).resolve().parents[1]
    english = (repo_root / "docs" / "i18n" / "en" / "dng-tag-contract.md").read_text(
        encoding="utf-8"
    )
    zh_tw = (repo_root / "docs" / "i18n" / "zh-TW" / "dng-tag-contract.md").read_text(
        encoding="utf-8"
    )

    assert "generic compatibility matrix" in english
    assert "dedicated local scripts can produce local SDK sidecar evidence" in english
    assert "without a reproducible local validation path" not in english
    assert "generic compatibility matrix" in zh_tw
    assert "dedicated local scripts 可產出本機 SDK sidecar evidence" in zh_tw
    assert "沒有可重現的本機驗證路徑前" not in zh_tw


def test_current_public_status_mentions_review_bundle_failure_diagnostics():
    repo_root = Path(__file__).resolve().parents[1]
    english = (repo_root / "docs" / "current-public-status.md").read_text(encoding="utf-8")
    zh_tw = (
        repo_root / "docs" / "i18n" / "zh-TW" / "current-public-status.md"
    ).read_text(encoding="utf-8")

    assert "source reports and manifests are preserved as diagnostic evidence" in english
    assert "source report diagnostic preservation on command failure" in english
    assert "source reports / manifests 作為診斷 evidence" in zh_tw
    assert "command failure 時的 source report diagnostic preservation" in zh_tw


def test_repo_agent_instructions_keep_zh_tw_source_pair():
    repo_root = Path(__file__).resolve().parents[1]
    english = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    zh_tw = (repo_root / "AGENTS.zh-tw.md").read_text(encoding="utf-8")

    assert "`AGENTS.zh-tw.md` is the Traditional Chinese original source manuscript" in english
    assert "`AGENTS.md` remains the English canonical baseline" in english
    assert "edit `AGENTS.zh-tw.md` first, then update this English file" in english
    assert "`AGENTS.zh-tw.md` 是本專案 repo-local agent 指令的繁體中文原始母檔" in zh_tw
    assert "`AGENTS.md` 是英文 canonical baseline" in zh_tw
    assert "先修改 `AGENTS.zh-tw.md`，再同步更新英文 `AGENTS.md`" in zh_tw


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


def test_dng_writes_explicit_photoshop_baseline_metadata(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:photoshop-baseline")

    with tifffile.TiffFile(output_path) as tif:
        assert tif.pages[0].tags[TAG_NEW_SUBFILE_TYPE].value == 1
        assert len(tif.pages[0].pages) == 1
        tags = _raw_page(tif).tags
        baseline_values = {
            "new_subfile_type": tags[TAG_NEW_SUBFILE_TYPE].value,
            "make": tags[TAG_MAKE].value,
            "model": tags[TAG_MODEL].value,
            "unique_camera_model": tags[TAG_UNIQUE_CAMERA_MODEL].value,
            "default_scale": tags[TAG_DEFAULT_SCALE].value,
            "raw_data_unique_id": tags[TAG_RAW_DATA_UNIQUE_ID].value,
        }

    assert baseline_values["new_subfile_type"] == 0
    assert baseline_values["make"] == "image2dng"
    assert baseline_values["model"] == "Synthetic Camera v1"
    assert baseline_values["unique_camera_model"] == "Synthetic Camera v1"
    assert baseline_values["default_scale"] == (1, 1, 1, 1)
    assert len(baseline_values["raw_data_unique_id"]) == 16


def test_dng_writes_embedded_jpeg_preview_with_raw_subifd(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:embedded-preview")

    with tifffile.TiffFile(output_path) as tif:
        preview = tif.pages[0]
        raw = _raw_page(tif)

        assert preview.tags[TAG_NEW_SUBFILE_TYPE].value == 1
        assert preview.tags["Compression"].value == 7
        assert preview.tags["PhotometricInterpretation"].value == 2
        assert preview.asarray().shape == (16, 16, 3)
        assert raw in tuple(preview.pages)
        assert raw.tags[TAG_NEW_SUBFILE_TYPE].value == 0
        assert raw.asarray().shape == (16, 16, 3)

    result = validate_dng(output_path, run_smoke=False)
    assert result.ok, result.errors
    assert any(check.name == "embedded-preview" for check in result.checks)


def test_dng_single_raw_ifd_layout_remains_available(tmp_path):
    input_path = tmp_path / "single-layout-input.tif"
    output_path = tmp_path / "single-layout-output.dng"
    tifffile.imwrite(input_path, _gradient_image(16, 16), photometric="rgb")
    raw, core = build_linearraw_buffer(input_path, "srgb")

    from image2dng.dng_writer import write_dng

    write_dng(
        output_path,
        raw,
        core,
        CameraProfileModel.from_white_balance(6500),
        AIMetadataModel(prompt_hash="sha256:single-raw-ifd"),
        dng_layout="single-raw-ifd",
    )

    with tifffile.TiffFile(output_path) as tif:
        assert len(tif.pages) == 1
        assert not tif.pages[0].pages
        assert tif.pages[0].tags[TAG_NEW_SUBFILE_TYPE].value == 0
        assert _raw_page(tif).asarray().shape == (16, 16, 3)

    result = validate_dng(output_path, run_smoke=False)
    assert result.ok, result.errors
    report = result.to_dict()
    assert report["dng_layout"] == "single-raw-ifd"
    assert report["ifd0_preview"] is False
    assert report["raw_ifd_location"] == "IFD0"
    assert report["embedded_preview_compression"] is None
    assert report["raw_photometric"] == "LinearRaw"
    assert not any(check.name == "embedded-preview" for check in result.checks)


def test_raw_data_unique_id_tracks_raw_buffer_only(tmp_path):
    input_path = tmp_path / "identity-input.tif"
    tifffile.imwrite(input_path, _gradient_image(16, 16), photometric="rgb")
    raw, core = build_linearraw_buffer(input_path, "srgb")

    original_path = tmp_path / "identity-original.dng"
    metadata_changed_path = tmp_path / "identity-metadata-changed.dng"
    raw_changed_path = tmp_path / "identity-raw-changed.dng"

    from image2dng.dng_writer import write_dng

    camera = CameraProfileModel.from_white_balance(6500)
    write_dng(
        original_path,
        raw,
        core,
        camera,
        AIMetadataModel(prompt_hash="sha256:original", scene_description="original"),
    )
    write_dng(
        metadata_changed_path,
        raw,
        core,
        camera,
        AIMetadataModel(prompt_hash="sha256:metadata", scene_description="metadata changed"),
    )

    raw_changed = raw.copy()
    raw_changed[0, 0, 0] = np.uint16(int(raw_changed[0, 0, 0]) ^ 1)
    write_dng(
        raw_changed_path,
        raw_changed,
        core,
        camera,
        AIMetadataModel(prompt_hash="sha256:raw-changed", scene_description="raw changed"),
    )

    original_id = _raw_data_unique_id(original_path)
    metadata_changed_id = _raw_data_unique_id(metadata_changed_path)
    raw_changed_id = _raw_data_unique_id(raw_changed_path)

    assert original_id == metadata_changed_id
    assert raw_changed_id != original_id
    assert raw_changed_id == tuple(hashlib.md5(raw_changed.tobytes()).digest())


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
    assert result.raw_data_unique_id == _raw_data_unique_id_hex(output_path)
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
        page = _raw_page(tif)
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


def test_raw_native_validation_summary_requires_contract_keys():
    summary = _validation_summary(
        {
            "ok": True,
            "dng_layout": "preview-subifd",
            "raw_ifd_location": "IFD0/SubIFD0",
            "errors": [],
            "warnings": [],
            "extra": "kept out of manifest summary",
        }
    )

    assert summary == {
        "ok": True,
        "dng_layout": "preview-subifd",
        "raw_ifd_location": "IFD0/SubIFD0",
        "errors": [],
        "warnings": [],
    }
    with pytest.raises(ValueError, match="validation report missing keys: warnings"):
        _validation_summary(
            {
                "ok": True,
                "dng_layout": "preview-subifd",
                "raw_ifd_location": "IFD0/SubIFD0",
                "errors": [],
            }
        )
    with pytest.raises(ValueError, match="validation report ok must be a boolean"):
        _validation_summary(
            {
                "ok": "yes",
                "dng_layout": "preview-subifd",
                "raw_ifd_location": "IFD0/SubIFD0",
                "errors": [],
                "warnings": [],
            }
        )
    with pytest.raises(ValueError, match="validation report dng_layout must be a string"):
        _validation_summary(
            {
                "ok": True,
                "dng_layout": [],
                "raw_ifd_location": "IFD0/SubIFD0",
                "errors": [],
                "warnings": [],
            }
        )
    with pytest.raises(ValueError, match="validation report raw_ifd_location must be a string"):
        _validation_summary(
            {
                "ok": True,
                "dng_layout": "preview-subifd",
                "raw_ifd_location": False,
                "errors": [],
                "warnings": [],
            }
        )
    with pytest.raises(ValueError, match="validation report errors must be a string list"):
        _validation_summary(
            {
                "ok": True,
                "dng_layout": "preview-subifd",
                "raw_ifd_location": "IFD0/SubIFD0",
                "errors": ["ok", 7],
                "warnings": [],
            }
        )
    with pytest.raises(ValueError, match="validation report warnings must be a string list"):
        _validation_summary(
            {
                "ok": True,
                "dng_layout": "preview-subifd",
                "raw_ifd_location": "IFD0/SubIFD0",
                "errors": [],
                "warnings": "none",
            }
        )


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
        "preview_artifact": "sidecar JPEG rendered from generated RAW buffers",
        "dng_layout": "preview-subifd",
        "embedded_preview": "IFD0 JPEG preview",
        "raw_ifd_location": "Raw SubIFD referenced from IFD0",
        "external_scene_linear_boundary": "available",
        "semantic_boundary": (
            "semantic sidecar is preserved by default; opt-in semantic-reaction-chain-v1 "
            "helpers can affect raw values"
        ),
    }
    assert scene_manifest.keys() >= {
        "slug",
        "prompt_hash",
        "outputs",
        "raw_data_unique_ids",
        "validations",
        "nodes",
    }
    assert scene_manifest["raw_data_unique_ids"]["linearraw"] == _raw_data_unique_id_hex(
        Path(scene_manifest["outputs"]["linearraw_dng"])
    )
    assert scene_manifest["raw_data_unique_ids"]["cfa"] == _raw_data_unique_id_hex(
        Path(scene_manifest["outputs"]["cfa_dng"])
    )
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
                "source_type": "procedural",
                "producer": "image2dng procedural scene generator",
                "input_space": "linear-rec709",
                "prompt_hash": scene_manifest["prompt_hash"],
                "artifacts": scene_manifest["outputs"],
                "validation_ok": {"linearraw": True, "cfa": True},
                "raw_data_unique_ids": scene_manifest["raw_data_unique_ids"],
            }
        ],
    }


def test_raw_native_batch_can_write_single_raw_ifd_for_adobe_converter(tmp_path):
    scene = GenerationScene(
        slug="adobe-contract-chart",
        prompt="adobe converter layout test",
        description="single raw IFD compatibility test scene",
        lighting="contract D65",
        style="chart-ramp",
        seed=789,
        width=24,
        height=24,
    )

    result = run_raw_native_batch(
        tmp_path / "adobe-raw-native-contract",
        scenes=[scene],
        dng_layout="single-raw-ifd",
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    scene_manifest = manifest["scenes"][0]
    assert manifest["graph"]["dng_layout"] == "single-raw-ifd"
    assert manifest["graph"]["embedded_preview"] == "absent; sidecar JPEG preview only"
    assert manifest["graph"]["raw_ifd_location"] == "IFD0"
    assert scene_manifest["nodes"][2]["parameters"]["dng_layout"] == "single-raw-ifd"
    assert scene_manifest["nodes"][3]["parameters"]["dng_layout"] == "single-raw-ifd"
    assert scene_manifest["validations"]["linearraw"]["dng_layout"] == "single-raw-ifd"
    assert scene_manifest["validations"]["cfa"]["dng_layout"] == "single-raw-ifd"
    assert validate_dng(scene_manifest["outputs"]["linearraw_dng"], run_smoke=False).ok
    assert validate_dng(scene_manifest["outputs"]["cfa_dng"], run_smoke=False).ok
    assert Path(scene_manifest["outputs"]["linearraw_jpeg"]).exists()
    assert Path(scene_manifest["outputs"]["cfa_jpeg"]).exists()


def test_semantic_scene_validator_accepts_minimal_valid_sidecar(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12, include_hash=True)

    result = validate_semantic_scene(semantic_path)

    assert result.ok, result.errors
    assert result.warnings == []
    assert result.schema == SEMANTIC_SCENE_SCHEMA
    assert result.to_dict()["counts"] == {
        "assets": 1,
        "materials": 1,
        "lights": 1,
        "regions": 1,
    }


def test_semantic_scene_validator_rejects_duplicate_ids_and_bad_references(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["materials"].append({"id": "mat-neutral-card"})
    payload["regions"][0]["material_id"] = "missing-material"
    payload["regions"][0]["mask_asset_id"] = "missing-asset"
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert "duplicate materials id: mat-neutral-card" in result.errors
    assert any("unknown material: missing-material" in error for error in result.errors)
    assert any("unknown asset: missing-asset" in error for error in result.errors)


def test_semantic_scene_validator_warns_when_optional_asset_hash_is_missing(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12)

    result = validate_semantic_scene(semantic_path)

    assert result.ok, result.errors
    assert result.warnings == ["assets[0].sha256 is missing; asset integrity is unverified"]


def test_semantic_scene_validator_rejects_unsupported_schema(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["schema"] = "example.semantic_scene.v1"
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert "unsupported semantic scene schema: 'example.semantic_scene.v1'" in result.errors


def test_semantic_scene_validator_rejects_boolean_numeric_values(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["scene"]["width"] = True
    payload["regions"][0]["response_hints"]["exposure_bias_ev"] = False
    payload["sensor_response_hints"]["target_middle_gray"] = True
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert "scene.width must be a positive integer" in result.errors
    assert any("exposure_bias_ev must be numeric" in error for error in result.errors)
    assert "sensor_response_hints.target_middle_gray must be between 0 and 1" in result.errors


def test_semantic_scene_validator_rejects_non_finite_numeric_values(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["regions"][0]["response_hints"]["exposure_bias_ev"] = float("inf")
    payload["sensor_response_hints"]["target_middle_gray"] = float("nan")
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert any("exposure_bias_ev must be numeric" in error for error in result.errors)
    assert "sensor_response_hints.target_middle_gray must be between 0 and 1" in result.errors


def test_semantic_scene_validator_verifies_asset_paths_and_hashes(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12, include_hash=True)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["assets"][0]["sha256"] = "sha256:" + "0" * 64
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert "assets[0].sha256 does not match asset contents" in result.errors


def test_semantic_scene_validator_rejects_asset_paths_outside_sidecar(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["assets"][0]["path"] = "../outside-mask.png"
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert "assets[0].path must stay within the semantic sidecar directory" in result.errors


def test_semantic_scene_validator_rejects_asset_directory_paths(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12)
    asset_dir = tmp_path / "asset-dir"
    asset_dir.mkdir()
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["assets"][0]["path"] = asset_dir.name
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert any("asset path must reference a file" in error for error in result.errors)


def test_semantic_scene_validator_accepts_semantic_physics_fields(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=16,
        height=12,
        include_hash=True,
        include_semantic_physics=True,
    )

    result = validate_semantic_scene(semantic_path)

    assert result.ok, result.errors
    assert result.warnings == []


def test_semantic_scene_validator_accepts_zero_and_negative_ev100(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=16,
        height=12,
        include_hash=True,
        include_semantic_physics=True,
    )
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["capture_physics"]["ev100"] = -2.0
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert result.ok, result.errors


def test_semantic_scene_validator_rejects_bad_semantic_physics_fields(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=16,
        height=12,
        include_hash=True,
        include_semantic_physics=True,
    )
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["capture_physics"]["source"] = "guessed"
    payload["capture_physics"]["white_balance_kelvin"] = 0
    payload["capture_physics"]["illuminant_confidence"] = 1.5
    payload["capture_physics"]["ev100"] = False
    payload["camera_response"]["cfa_pattern"] = "rgb"
    payload["regions"][0]["raw_statistics"]["mean_linear_rgb"] = [0.1, -0.1, 0.2]
    payload["regions"][0]["response_hints"]["confidence"] = False
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert any("capture_physics.source must be one of" in error for error in result.errors)
    assert "capture_physics.white_balance_kelvin must be a positive number" in result.errors
    assert "capture_physics.illuminant_confidence must be between 0 and 1" in result.errors
    assert "capture_physics.ev100 must be a finite number" in result.errors
    assert "camera_response.cfa_pattern must be one of bggr, gbrg, grbg, rggb" in result.errors
    assert any(
        "mean_linear_rgb must contain three non-negative" in error for error in result.errors
    )
    assert "regions[0].response_hints.confidence must be between 0 and 1" in result.errors


def test_semantic_scene_validator_rejects_black_level_array_above_white_level(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=16,
        height=12,
        include_hash=True,
        include_semantic_physics=True,
    )
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["camera_response"]["black_level"] = [512, 20000, 512, 512]
    payload["camera_response"]["white_level"] = 16383
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert "camera_response.black_level must be less than white_level" in result.errors


def test_semantic_scene_validator_rejects_non_string_semantic_physics_enums(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=16,
        height=12,
        include_hash=True,
        include_semantic_physics=True,
    )
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["capture_physics"]["source"] = ["metadata"]
    payload["camera_response"]["cfa_pattern"] = {"pattern": "rggb"}
    payload["regions"][0]["response_hints"]["source"] = ["inferred"]
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_semantic_scene(semantic_path)

    assert not result.ok
    assert any("capture_physics.source must be one of" in error for error in result.errors)
    assert "camera_response.cfa_pattern must be one of bggr, gbrg, grbg, rggb" in result.errors
    assert any(
        "regions[0].response_hints.source must be one of" in error
        for error in result.errors
    )


def test_semantic_reaction_model_registry_reports_model_boundaries():
    registry = semantic_reaction_model_registry()

    assert registry[REGION_EXPOSURE_REACTION_MODEL]["status"] == "implemented"
    assert registry[REGION_EXPOSURE_REACTION_MODEL]["current_raw_value_effect"] is True
    assert registry[REGION_EXPOSURE_REACTION_MODEL]["intended_raw_value_effect"] is True
    assert "linear-light only" in registry[REGION_EXPOSURE_REACTION_MODEL]["boundary"]
    assert registry[HIGHLIGHT_CLIPPING_REACTION_MODEL]["status"] == "implemented"
    assert registry[HIGHLIGHT_CLIPPING_REACTION_MODEL]["current_raw_value_effect"] is True
    assert registry[HIGHLIGHT_CLIPPING_REACTION_MODEL]["intended_raw_value_effect"] is True
    assert (
        "deterministic shoulder mapping"
        in registry[HIGHLIGHT_CLIPPING_REACTION_MODEL]["boundary"]
    )
    assert "camera tone-curve" in registry[HIGHLIGHT_CLIPPING_REACTION_MODEL]["boundary"]
    assert registry[SEMANTIC_REACTION_CHAIN_MODEL]["status"] == "implemented"
    assert registry[SEMANTIC_REACTION_CHAIN_MODEL]["current_raw_value_effect"] is True
    assert "ordered composition" in registry[SEMANTIC_REACTION_CHAIN_MODEL]["scope"]


def test_semantic_reaction_applies_exposure_to_masked_region_only(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=4,
        height=3,
        exposure_bias_ev=1.0,
        mask_columns=2,
    )
    image = np.full((3, 4, 3), 1000, dtype=np.uint16)

    reacted, result = apply_region_exposure_reaction(
        image,
        semantic_payload=load_semantic_payload(semantic_path),
        semantic_base_dir=tmp_path,
    )

    assert result.applied is True
    assert result.status == "applied"
    assert result.affected_pixels == 6
    assert np.all(reacted[:, :2] == 2000)
    assert np.all(reacted[:, 2:] == 1000)


def test_semantic_reaction_reports_noop_without_exposure_regions(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=4,
        height=3,
        exposure_bias_ev=None,
    )
    image = np.full((3, 4, 3), 1000, dtype=np.uint16)

    reacted, result = apply_region_exposure_reaction(
        image,
        semantic_payload=load_semantic_payload(semantic_path),
        semantic_base_dir=tmp_path,
    )

    assert result.applied is False
    assert result.status == "no-op"
    assert result.reason == "no regions with exposure_bias_ev and mask_asset_id"
    assert np.array_equal(reacted, image)


def test_semantic_reaction_rejects_bad_mask_size(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=4, height=3)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    bad_mask = tmp_path / "bad-mask.png"
    _write_gray_png(bad_mask, np.ones((2, 4), dtype=np.uint16))
    payload["assets"][0]["path"] = bad_mask.name
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")
    image = np.full((3, 4, 3), 1000, dtype=np.uint16)

    try:
        apply_region_exposure_reaction(
            image,
            semantic_payload=load_semantic_payload(semantic_path),
            semantic_base_dir=tmp_path,
        )
    except ValueError as exc:
        assert "semantic reaction mask size mismatch" in str(exc)
    else:
        raise AssertionError("expected bad mask size to fail")


def test_semantic_reaction_rejects_non_finite_exposure_bias(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=4, height=3)
    payload = load_semantic_payload(semantic_path)
    payload["regions"][0]["response_hints"]["exposure_bias_ev"] = float("nan")
    image = np.full((3, 4, 3), 1000, dtype=np.uint16)

    try:
        apply_region_exposure_reaction(
            image,
            semantic_payload=payload,
            semantic_base_dir=tmp_path,
        )
    except ValueError as exc:
        assert "semantic reaction exposure_bias_ev must be finite" in str(exc)
    else:
        raise AssertionError("expected non-finite exposure bias to fail")


def test_highlight_clipping_policy_applies_global_shoulder(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=3, height=2)
    image = np.array(
        [
            [[1000, 59000, 65535], [2000, 3000, 4000], [62000, 63000, 64000]],
            [[100, 200, 300], [65535, 65535, 65535], [58000, 59000, 60000]],
        ],
        dtype=np.uint16,
    )

    reacted, result = apply_highlight_clipping_policy(
        image,
        semantic_payload=load_semantic_payload(semantic_path),
    )

    assert result.applied is True
    assert result.status == "applied"
    assert result.model == "highlight-clipping-policy-v1"
    assert result.affected_pixels == 4
    assert result.regions == [
        {
            "region_id": "global-highlight-shoulder",
            "clipping_policy": "preserve-highlights",
            "shoulder_start": 0.9,
            "compression": 0.5,
            "affected_pixels": 4,
            "changed_channels": 10,
        }
    ]
    assert np.array_equal(reacted[0, 1], image[0, 1])
    assert reacted[0, 0, 1] < image[0, 0, 1]
    assert reacted[0, 0, 2] < image[0, 0, 2]
    assert reacted[1, 1, 0] == reacted[1, 1, 1] == reacted[1, 1, 2]


def test_highlight_clipping_policy_reports_noop_for_clip_policy(tmp_path):
    semantic_path = _write_semantic_scene(tmp_path, width=2, height=2)
    payload = load_semantic_payload(semantic_path)
    payload["sensor_response_hints"]["clipping_policy"] = "clip"
    image = np.full((2, 2, 3), 65535, dtype=np.uint16)

    reacted, result = apply_highlight_clipping_policy(
        image,
        semantic_payload=payload,
    )

    assert result.applied is False
    assert result.status == "no-op"
    assert result.reason == "no highlight clipping policy requiring value mapping"
    assert np.array_equal(reacted, image)


def test_semantic_reaction_chain_composes_region_exposure_and_highlight_policy(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=4,
        height=3,
        exposure_bias_ev=1.0,
        mask_columns=2,
    )
    image = np.full((3, 4, 3), 40000, dtype=np.uint16)

    reacted, result = apply_semantic_reaction_chain(
        image,
        semantic_payload=load_semantic_payload(semantic_path),
        semantic_base_dir=tmp_path,
    )

    assert result.applied is True
    assert result.status == "applied"
    assert result.model == SEMANTIC_REACTION_CHAIN_MODEL
    assert result.affected_pixels == 12
    assert result.affected_pixel_count_semantics == "sum-of-child-affected-pixels"
    assert [entry["model"] for entry in result.regions] == [
        REGION_EXPOSURE_REACTION_MODEL,
        HIGHLIGHT_CLIPPING_REACTION_MODEL,
    ]
    assert result.regions[0]["affected_pixels"] == 6
    assert result.regions[1]["affected_pixels"] == 6
    assert np.all(reacted[:, :2] < 65535)
    assert np.all(reacted[:, :2] > image[:, :2])
    assert np.array_equal(reacted[:, 2:], image[:, 2:])


def test_semantic_reaction_chain_reports_single_active_highlight_helper(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=3,
        height=2,
        exposure_bias_ev=None,
    )
    image = np.full((2, 3, 3), 65535, dtype=np.uint16)

    reacted, result = apply_semantic_reaction_chain(
        image,
        semantic_payload=load_semantic_payload(semantic_path),
        semantic_base_dir=tmp_path,
    )

    assert result.applied is True
    assert result.status == "applied"
    assert result.model == HIGHLIGHT_CLIPPING_REACTION_MODEL
    assert result.affected_pixels == 6
    assert len(result.regions) == 1
    assert result.regions[0]["region_id"] == "global-highlight-shoulder"
    assert np.all(reacted < image)


def test_semantic_reaction_chain_noop_keeps_child_reasons(tmp_path):
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=3,
        height=2,
        exposure_bias_ev=None,
    )
    payload = load_semantic_payload(semantic_path)
    payload["sensor_response_hints"]["clipping_policy"] = "clip"
    image = np.full((2, 3, 3), 1000, dtype=np.uint16)

    reacted, result = apply_semantic_reaction_chain(
        image,
        semantic_payload=payload,
        semantic_base_dir=tmp_path,
    )

    assert result.applied is False
    assert result.status == "no-op"
    assert result.model == SEMANTIC_REACTION_CHAIN_MODEL
    assert "no regions with exposure_bias_ev and mask_asset_id" in result.reason
    assert "no highlight clipping policy requiring value mapping" in result.reason
    assert np.array_equal(reacted, image)


def test_external_scene_linear_batch_preserves_producer_boundary(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(tmp_path, width=20, height=18)

    result = run_external_scene_linear_batch(
        tmp_path / "external-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                prompt="external rendered chart",
                description="external scene-linear producer test",
                lighting="virtual studio",
                producer="unit-test-renderer",
                semantic_manifest=semantic_path,
            )
        ],
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    scene_manifest = manifest["scenes"][0]
    outputs = scene_manifest["outputs"]

    assert manifest["graph"]["nodes"][0] == "ExternalSceneLinearInputNode"
    assert scene_manifest["source_type"] == "external-scene-linear"
    assert scene_manifest["producer"] == "unit-test-renderer"
    assert scene_manifest["input_space"] == "linear-rec709"
    assert scene_manifest["semantic_artifacts"].keys() == {
        "semantic_manifest",
        "asset:mask-material-chart",
    }
    assert scene_manifest["semantic_contract"] == SEMANTIC_SCENE_SCHEMA
    assert scene_manifest["semantic_to_raw_status"] == "preserved-not-applied"
    assert scene_manifest["semantic_validation"]["ok"] is True
    assert scene_manifest["semantic_validation"]["warnings"]
    assert Path(outputs["scene_linear_input"]).exists()
    copied_semantic = Path(scene_manifest["semantic_artifacts"]["semantic_manifest"])
    assert copied_semantic.exists()
    assert Path(scene_manifest["semantic_artifacts"]["asset:mask-material-chart"]).exists()
    copied_validation = validate_semantic_scene(copied_semantic)
    assert copied_validation.ok, copied_validation.errors
    assert validate_dng(outputs["linearraw_dng"], run_smoke=False).ok
    assert validate_dng(outputs["cfa_dng"], run_smoke=False).ok
    assert scene_manifest["validations"]["linearraw"]["ok"] is True
    assert scene_manifest["validations"]["cfa"]["ok"] is True

    sample_index = json.loads(result.sample_index_path.read_text(encoding="utf-8"))
    sample = sample_index["samples"][0]
    assert sample["semantic_contract"] == SEMANTIC_SCENE_SCHEMA
    assert sample["semantic_to_raw_status"] == "preserved-not-applied"
    assert sample["semantic_validation"]["ok"] is True


def test_external_scene_linear_batch_preserves_semantic_physics_sidecar(tmp_path):
    source_path = tmp_path / "semantic-physics-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=20,
        height=18,
        include_hash=True,
        include_semantic_physics=True,
    )

    result = run_external_scene_linear_batch(
        tmp_path / "semantic-physics-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="semantic-physics-scene",
                path=source_path,
                description="semantic physics preservation test",
                producer="unit-test-renderer",
                semantic_manifest=semantic_path,
            )
        ],
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    scene_manifest = manifest["scenes"][0]
    copied_semantic = Path(scene_manifest["semantic_artifacts"]["semantic_manifest"])
    copied_payload = json.loads(copied_semantic.read_text(encoding="utf-8"))
    sample = json.loads(result.sample_index_path.read_text(encoding="utf-8"))["samples"][0]

    assert scene_manifest["semantic_validation"]["ok"] is True
    assert scene_manifest["semantic_to_raw_status"] == "preserved-not-applied"
    assert copied_payload["capture_physics"]["source"] == "metadata"
    assert copied_payload["camera_response"]["cfa_pattern"] == "rggb"
    assert copied_payload["regions"][0]["raw_statistics"]["clipped_pixel_ratio"] == 0.0
    assert sample["semantic_to_raw_status"] == "preserved-not-applied"


def test_external_scene_linear_batch_rejects_invalid_semantic_sidecar(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    semantic_path = tmp_path / "bad-semantics.json"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path.write_text(
        json.dumps({"schema": SEMANTIC_SCENE_SCHEMA, "scene": {"id": "missing-dimensions"}}),
        encoding="utf-8",
    )

    try:
        run_external_scene_linear_batch(
            tmp_path / "external-batch",
            scenes=[
                ExternalSceneLinearInput(
                    slug="external-scene",
                    path=source_path,
                    semantic_manifest=semantic_path,
                )
            ],
        )
    except ValueError as exc:
        assert "invalid semantic scene sidecar" in str(exc)
        assert "scene.width must be a positive integer" in str(exc)
    else:
        raise AssertionError("expected invalid semantic sidecar to stop the batch")


def test_external_scene_linear_batch_preserves_same_basename_semantic_assets(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    asset_a = tmp_path / "a" / "mask.png"
    asset_b = tmp_path / "b" / "mask.png"
    asset_a.parent.mkdir()
    asset_b.parent.mkdir()
    _write_gray_png(asset_a, np.zeros((18, 20), dtype=np.uint16))
    _write_gray_png(asset_b, np.full((18, 20), 65535, dtype=np.uint16))
    semantic_path = _write_semantic_scene(tmp_path, width=20, height=18)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["assets"] = [
        {"id": "mask-dark", "kind": "mask", "path": "a/mask.png", "space": "pixel"},
        {"id": "mask-bright", "kind": "mask", "path": "b/mask.png", "space": "pixel"},
    ]
    payload["regions"][0]["mask_asset_id"] = "mask-dark"
    payload["regions"].append(
        {
            "id": "region-bright",
            "material_id": "mat-neutral-card",
            "mask_asset_id": "mask-bright",
            "bbox": [0, 0, 20, 18],
        }
    )
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = run_external_scene_linear_batch(
        tmp_path / "external-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                semantic_manifest=semantic_path,
            )
        ],
    )

    scene_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    dark_copy = Path(scene_manifest["semantic_artifacts"]["asset:mask-dark"])
    bright_copy = Path(scene_manifest["semantic_artifacts"]["asset:mask-bright"])
    assert dark_copy != bright_copy
    assert dark_copy.name == "mask-dark.png"
    assert bright_copy.name == "mask-bright.png"
    assert dark_copy.read_bytes() != bright_copy.read_bytes()
    copied_semantic = Path(scene_manifest["semantic_artifacts"]["semantic_manifest"])
    assert validate_semantic_scene(copied_semantic).ok


def test_external_scene_linear_batch_applies_semantic_reaction_when_opted_in(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=20,
        height=18,
        exposure_bias_ev=1.0,
        mask_columns=10,
    )

    preserved = run_external_scene_linear_batch(
        tmp_path / "preserved-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                semantic_manifest=semantic_path,
            )
        ],
    )
    reacted = run_external_scene_linear_batch(
        tmp_path / "reacted-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                semantic_manifest=semantic_path,
                apply_semantic_reaction=True,
            )
        ],
    )

    preserved_scene = json.loads(preserved.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    reacted_scene = json.loads(reacted.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    assert preserved_scene["semantic_to_raw_status"] == "preserved-not-applied"
    assert reacted_scene["semantic_to_raw_status"] == "applied"
    assert reacted_scene["semantic_reaction"]["model"] == "semantic-reaction-chain-v1"
    assert reacted_scene["semantic_reaction"]["affected_pixels"] == 340
    assert (
        reacted_scene["semantic_reaction"]["affected_pixel_count_semantics"]
        == "sum-of-child-affected-pixels"
    )
    assert [
        entry["model"] for entry in reacted_scene["semantic_reaction"]["regions"]
    ] == ["region-exposure-mask-v1", "highlight-clipping-policy-v1"]
    assert reacted_scene["semantic_reaction"]["regions"][0]["affected_pixels"] == 180
    assert reacted_scene["semantic_reaction"]["regions"][1]["affected_pixels"] == 160
    assert Path(reacted_scene["outputs"]["original_scene_linear_input"]).exists()
    assert Path(reacted_scene["outputs"]["scene_linear_input"]).name.endswith(
        "-semantic-reaction.tif"
    )
    assert reacted_scene["nodes"][0]["parameters"]["semantic_boundary"] == (
        "semantic sidecar applied through opt-in semantic-reaction-chain-v1 helpers"
    )
    assert (
        preserved_scene["raw_data_unique_ids"]["linearraw"]
        != reacted_scene["raw_data_unique_ids"]["linearraw"]
    )
    assert preserved_scene["prompt_hash"] != reacted_scene["prompt_hash"]

    sample = json.loads(reacted.sample_index_path.read_text(encoding="utf-8"))["samples"][0]
    assert sample["semantic_to_raw_status"] == "applied"
    assert sample["semantic_reaction"] == {
        "model": "semantic-reaction-chain-v1",
        "applied": True,
        "region_count": 2,
        "affected_pixels": 340,
        "affected_pixel_count_semantics": "sum-of-child-affected-pixels",
    }


def test_external_scene_linear_batch_preserves_inline_producer_metadata(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(12, 10), photometric="rgb")

    result = run_external_scene_linear_batch(
        tmp_path / "external-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                producer="unit-test-renderer",
                producer_metadata={
                    "generator": "unit-test-renderer",
                    "seed": 42,
                    "workflow": "inline-only",
                },
            )
        ],
    )

    scene_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    assert scene_manifest["producer_metadata"] == {
        "generator": "unit-test-renderer",
        "seed": 42,
        "workflow": "inline-only",
    }
    assert scene_manifest["producer_metadata_artifacts"] == {}
    sample = json.loads(result.sample_index_path.read_text(encoding="utf-8"))["samples"][0]
    assert sample["producer_metadata"]["seed"] == 42
    assert "producer_metadata_artifacts" not in sample


def test_external_scene_linear_batch_preserves_metadata_manifest_without_inline_metadata(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    metadata_path = tmp_path / "producer-metadata.json"
    tifffile.imwrite(source_path, _gradient_image(12, 10), photometric="rgb")
    metadata_path.write_text(
        json.dumps(
            {
                "schema": "example.producer_metadata.v1",
                "producer": "unit-test-renderer",
                "seed": 42,
            }
        ),
        encoding="utf-8",
    )

    result = run_external_scene_linear_batch(
        tmp_path / "external-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                producer="unit-test-renderer",
                producer_metadata_manifest=metadata_path,
            )
        ],
    )

    scene_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    copied_metadata = Path(
        scene_manifest["producer_metadata_artifacts"]["producer_metadata_manifest"]
    )
    assert scene_manifest["producer_metadata"] == {}
    assert copied_metadata.exists()
    assert json.loads(copied_metadata.read_text(encoding="utf-8"))["seed"] == 42
    sample = json.loads(result.sample_index_path.read_text(encoding="utf-8"))["samples"][0]
    assert "producer_metadata" not in sample
    assert Path(sample["producer_metadata_artifacts"]["producer_metadata_manifest"]).exists()


def test_external_scene_linear_batch_semantic_reaction_noop(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=20,
        height=18,
        exposure_bias_ev=None,
    )
    payload = load_semantic_payload(semantic_path)
    payload["sensor_response_hints"]["clipping_policy"] = "clip"
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    result = run_external_scene_linear_batch(
        tmp_path / "noop-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                semantic_manifest=semantic_path,
                apply_semantic_reaction=True,
            )
        ],
    )

    scene_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    assert scene_manifest["semantic_to_raw_status"] == "no-op"
    assert scene_manifest["semantic_reaction"]["applied"] is False
    assert "no regions with exposure_bias_ev and mask_asset_id" in scene_manifest[
        "semantic_reaction"
    ]["reason"]
    assert "no highlight clipping policy requiring value mapping" in scene_manifest[
        "semantic_reaction"
    ]["reason"]

    sample = json.loads(result.sample_index_path.read_text(encoding="utf-8"))["samples"][0]
    assert sample["semantic_to_raw_status"] == "no-op"
    assert sample["semantic_reaction"]["applied"] is False
    assert "no regions with exposure_bias_ev and mask_asset_id" in sample[
        "semantic_reaction"
    ]["reason"]
    assert "no highlight clipping policy requiring value mapping" in sample[
        "semantic_reaction"
    ]["reason"]


def test_external_scene_linear_batch_semantic_reaction_rejects_encoded_input(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(tmp_path, width=20, height=18)

    try:
        run_external_scene_linear_batch(
            tmp_path / "encoded-batch",
            scenes=[
                ExternalSceneLinearInput(
                    slug="external-scene",
                    path=source_path,
                    input_space="srgb",
                    semantic_manifest=semantic_path,
                    apply_semantic_reaction=True,
                )
            ],
        )
    except ValueError as exc:
        assert "semantic reaction requires linear-light input_space" in str(exc)
    else:
        raise AssertionError("expected encoded semantic reaction input to fail")


def test_external_scene_linear_batch_semantic_reaction_hashes_mask_asset_bytes(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=20,
        height=18,
        exposure_bias_ev=1.0,
        mask_columns=5,
    )

    first = run_external_scene_linear_batch(
        tmp_path / "first-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                semantic_manifest=semantic_path,
                apply_semantic_reaction=True,
            )
        ],
    )
    _write_gray_png(
        tmp_path / "renderer-frame-001-mask.png",
        np.full((18, 20), 65535, dtype=np.uint16),
    )
    second = run_external_scene_linear_batch(
        tmp_path / "second-batch",
        scenes=[
            ExternalSceneLinearInput(
                slug="external-scene",
                path=source_path,
                semantic_manifest=semantic_path,
                apply_semantic_reaction=True,
            )
        ],
    )

    first_scene = json.loads(first.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    second_scene = json.loads(second.manifest_path.read_text(encoding="utf-8"))["scenes"][0]
    assert first_scene["prompt_hash"] != second_scene["prompt_hash"]
    assert first_scene["semantic_reaction"]["regions"][0]["affected_pixels"] == 90
    assert first_scene["semantic_reaction"]["regions"][1]["affected_pixels"] == 112
    assert second_scene["semantic_reaction"]["regions"][0]["affected_pixels"] == 360
    assert second_scene["semantic_reaction"]["regions"][1]["affected_pixels"] == 288
    assert (
        first_scene["raw_data_unique_ids"]["linearraw"]
        != second_scene["raw_data_unique_ids"]["linearraw"]
    )


def test_external_scene_linear_batch_semantic_reaction_rejects_dimension_mismatch(tmp_path):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(tmp_path, width=19, height=18)

    try:
        run_external_scene_linear_batch(
            tmp_path / "bad-dimensions-batch",
            scenes=[
                ExternalSceneLinearInput(
                    slug="external-scene",
                    path=source_path,
                    semantic_manifest=semantic_path,
                    apply_semantic_reaction=True,
                )
            ],
        )
    except ValueError as exc:
        assert "semantic reaction scene dimensions mismatch" in str(exc)
    else:
        raise AssertionError("expected dimension-mismatched semantic reaction to fail")


def test_external_scene_linear_batch_semantic_reaction_rejects_sidecar_input_space_mismatch(
    tmp_path,
):
    source_path = tmp_path / "external-scene.tif"
    tifffile.imwrite(source_path, _gradient_image(20, 18), photometric="rgb")
    semantic_path = _write_semantic_scene(tmp_path, width=20, height=18, input_space="acescg")

    try:
        run_external_scene_linear_batch(
            tmp_path / "bad-input-space-batch",
            scenes=[
                ExternalSceneLinearInput(
                    slug="external-scene",
                    path=source_path,
                    input_space="linear-rec709",
                    semantic_manifest=semantic_path,
                    apply_semantic_reaction=True,
                )
            ],
        )
    except ValueError as exc:
        assert "semantic reaction input_space mismatch" in str(exc)
    else:
        raise AssertionError("expected sidecar input_space-mismatched semantic reaction to fail")


def test_external_scene_manifest_loader_resolves_relative_paths(tmp_path):
    source_path = tmp_path / "manifest-scene.tif"
    semantic_path = tmp_path / "manifest-semantics.json"
    manifest_path = tmp_path / "external-scenes.json"
    tifffile.imwrite(source_path, _gradient_image(8, 8), photometric="rgb")
    semantic_path.write_text("{}", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "image2dng.external_scene_linear_sources.v1",
                "scenes": [
                    {
                        "slug": "manifest-scene",
                        "path": source_path.name,
                        "input_space": "linear-rec709",
                        "producer": "manifest-renderer",
                        "semantic_manifest": semantic_path.name,
                        "apply_semantic_reaction": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    scenes = load_external_scene_manifest(manifest_path)

    assert scenes == [
        ExternalSceneLinearInput(
            slug="manifest-scene",
            path=source_path,
            input_space="linear-rec709",
            producer="manifest-renderer",
            semantic_manifest=semantic_path,
            apply_semantic_reaction=True,
        )
    ]


def test_external_scene_manifest_loader_rejects_malformed_schema(tmp_path):
    manifest_path = tmp_path / "external-scenes.json"

    manifest_path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError, match="external scene manifest must be an object"):
        load_external_scene_manifest(manifest_path)

    manifest_path.write_text(json.dumps({"schema": False, "scenes": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="external scene manifest schema must be a string"):
        load_external_scene_manifest(manifest_path)

    manifest_path.write_text(json.dumps({"schema": "example.v0", "scenes": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected external scene manifest schema"):
        load_external_scene_manifest(manifest_path)


def test_external_scene_manifest_loader_rejects_malformed_optional_strings(tmp_path):
    source_path = tmp_path / "manifest-scene.tif"
    manifest_path = tmp_path / "external-scenes.json"
    tifffile.imwrite(source_path, _gradient_image(8, 8), photometric="rgb")
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "image2dng.external_scene_linear_sources.v1",
                "scenes": [
                    {
                        "slug": "manifest-scene",
                        "path": source_path.name,
                        "producer": ["not", "a", "string"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="external scene manifest field must be a string when present: producer",
    ):
        load_external_scene_manifest(manifest_path)


def test_external_scene_manifest_loader_rejects_malformed_input_space(tmp_path):
    source_path = tmp_path / "manifest-scene.tif"
    manifest_path = tmp_path / "external-scenes.json"
    tifffile.imwrite(source_path, _gradient_image(8, 8), photometric="rgb")
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "image2dng.external_scene_linear_sources.v1",
                "scenes": [
                    {
                        "slug": "manifest-scene",
                        "path": source_path.name,
                        "input_space": ["linear-rec709"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="external scene manifest field must be a string when present: input_space",
    ):
        load_external_scene_manifest(manifest_path)


def test_external_scene_manifest_loader_rejects_empty_optional_paths(tmp_path):
    source_path = tmp_path / "manifest-scene.tif"
    manifest_path = tmp_path / "external-scenes.json"
    tifffile.imwrite(source_path, _gradient_image(8, 8), photometric="rgb")
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "image2dng.external_scene_linear_sources.v1",
                "scenes": [
                    {
                        "slug": "manifest-scene",
                        "path": source_path.name,
                        "semantic_manifest": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="external scene manifest entry missing string field: semantic_manifest",
    ):
        load_external_scene_manifest(manifest_path)


def test_external_scene_manifest_loader_rejects_ambiguous_producer_metadata_aliases(
    tmp_path,
):
    source_path = tmp_path / "manifest-scene.tif"
    metadata_path = tmp_path / "producer-metadata.json"
    manifest_path = tmp_path / "external-scenes.json"
    tifffile.imwrite(source_path, _gradient_image(8, 8), photometric="rgb")
    metadata_path.write_text("{}", encoding="utf-8")

    manifest_path.write_text(
        json.dumps(
            {
                "schema": "image2dng.external_scene_linear_sources.v1",
                "scenes": [
                    {
                        "slug": "manifest-scene",
                        "path": source_path.name,
                        "producer_metadata": {"seed": 1},
                        "comfyui": {"seed": 2},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest-scene: use only one producer metadata field"):
        load_external_scene_manifest(manifest_path)

    manifest_path.write_text(
        json.dumps(
            {
                "schema": "image2dng.external_scene_linear_sources.v1",
                "scenes": [
                    {
                        "slug": "manifest-scene",
                        "path": source_path.name,
                        "producer_metadata_manifest": metadata_path.name,
                        "comfyui_metadata": metadata_path.name,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="manifest-scene: use only one producer metadata manifest field",
    ):
        load_external_scene_manifest(manifest_path)


def test_raw_native_batch_cli_reports_manifest_errors_without_traceback(tmp_path, capsys):
    module = _load_script_module("generate_raw_native_batch")
    manifest_path = tmp_path / "external-scenes.json"
    manifest_path.write_text(json.dumps({"schema": False, "scenes": []}), encoding="utf-8")

    exit_code = module.main(
        [
            "--external-manifest",
            str(manifest_path),
            "--output-dir",
            str(tmp_path / "batch"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "error: external scene manifest schema must be a string\n"
    assert "Traceback" not in captured.err


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
    monkeypatch.setattr(
        "image2dng.compatibility.resolve_processor_executable",
        lambda _command: (None, None),
    )

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
    assert {fixture["dng_layout"] for fixture in report["fixtures"]} == {"preview-subifd"}
    assert {fixture["raw_ifd_location"] for fixture in report["fixtures"]} == {
        "IFD0/SubIFD0"
    }
    assert {fixture["ifd0_preview"] for fixture in report["fixtures"]} == {True}
    assert all(Path(fixture["dng"]).exists() for fixture in report["fixtures"])
    assert all(Path(fixture["validation_json"]).exists() for fixture in report["fixtures"])
    assert all(fixture["dng_bytes"] > 0 for fixture in report["fixtures"])
    assert all(fixture["dng_sha256"].startswith("sha256:") for fixture in report["fixtures"])
    assert all(fixture["validation_json_bytes"] > 0 for fixture in report["fixtures"])
    assert all(
        fixture["validation_json_sha256"].startswith("sha256:")
        for fixture in report["fixtures"]
    )
    assert all("processor_results" in fixture for fixture in report["fixtures"])
    first_fixture = report["fixtures"][0]
    first_dng = Path(first_fixture["dng"])
    first_validation = Path(first_fixture["validation_json"])
    assert first_fixture["dng_bytes"] == first_dng.stat().st_size
    assert first_fixture["dng_sha256"] == _sha256_test_file(first_dng)
    assert first_fixture["validation_json_bytes"] == first_validation.stat().st_size
    assert first_fixture["validation_json_sha256"] == _sha256_test_file(first_validation)

    optional_tools = {"exiftool", "dcraw", "darktable-cli", "rawtherapee-cli"}
    optional_entries = [entry for entry in report["matrix"] if entry["tool"] in optional_tools]
    assert optional_entries
    assert {entry["result"] for entry in optional_entries} == {"skipped"}
    assert all(entry["notes"] == "skipped: not found" for entry in optional_entries)
    assert all(entry["exit_code"] is None for entry in optional_entries)
    assert all(entry["missing_output_artifacts"] == [] for entry in optional_entries)

    adobe_entries = [entry for entry in report["matrix"] if entry["tool"] == "adobe-dng-sdk"]
    assert adobe_entries
    assert {entry["result"] for entry in adobe_entries} == {"manual-only"}
    summary = summary_path.read_text(encoding="utf-8")
    assert "| Fixture | Tool | Result | Evidence | Notes |" in summary
    assert (
        "| Fixture | DNG bytes | DNG SHA-256 | "
        "Validation JSON bytes | Validation JSON SHA-256 |"
    ) in summary
    assert first_fixture["dng_sha256"] in summary
    assert first_fixture["validation_json_sha256"] in summary
    assert "layout=preview-subifd" in summary
    assert "raw_ifd_location=IFD0/SubIFD0" in summary
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
    assert all("missing_output_artifacts" in entry for entry in failed_entries)


def test_compatibility_evidence_rejects_malformed_fixture_validation_flag():
    module = _load_script_module("generate_compatibility_evidence")

    with pytest.raises(TypeError, match="fixture validation_ok must be a boolean"):
        module._structural_matrix_entry(
            {
                "validation_ok": "yes",
                "slug": "sample",
                "dng": "sample.dng",
                "validation_json": "sample.validation.json",
            }
        )


def test_compatibility_evidence_rejects_malformed_report_accessors():
    module = _load_script_module("generate_compatibility_evidence")
    report = {
        "schema": "image2dng.compatibility_evidence.v2",
        "generated_at": "2026-05-23T00:00:00Z",
        "output_dir": "demo-output/compatibility-evidence",
        "ok": True,
        "tools": {},
        "matrix": [
            {
                "fixture": "sample",
                "tool": "image2dng validate",
                "result": "passed",
                "evidence": "sample.validation.json",
                "notes": "ok",
            }
        ],
        "fixtures": [
            {
                "slug": "sample",
                "dng_layout": "preview-subifd",
                "raw_ifd_location": "IFD0/SubIFD0",
                "dng_bytes": 128,
                "dng_sha256": "sha256:dng",
                "validation_json_bytes": 64,
                "validation_json_sha256": "sha256:validation",
            }
        ],
        "errors": [],
        "install_policy": {
            "auto_install": False,
            "missing_tool_policy": "skipped",
            "available_tool_failure_policy": "failed",
        },
    }

    malformed = report | {"schema": 1}
    with pytest.raises(TypeError, match="report schema must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {"generated_at": []}
    with pytest.raises(TypeError, match="report generated_at must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {"output_dir": False}
    with pytest.raises(TypeError, match="report output_dir must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {"ok": "true"}
    with pytest.raises(TypeError, match="report ok must be a boolean"):
        module._summary_markdown(malformed)

    malformed = report | {"install_policy": []}
    with pytest.raises(TypeError, match="report install_policy must be an object"):
        module._summary_markdown(malformed)

    malformed = report | {"install_policy": report["install_policy"] | {"auto_install": "no"}}
    with pytest.raises(TypeError, match="install_policy auto_install must be a boolean"):
        module._summary_markdown(malformed)

    malformed = report | {
        "install_policy": report["install_policy"] | {"missing_tool_policy": []}
    }
    with pytest.raises(TypeError, match="install_policy missing_tool_policy must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {
        "install_policy": report["install_policy"]
        | {"available_tool_failure_policy": False}
    }
    with pytest.raises(
        TypeError,
        match="install_policy available_tool_failure_policy must be a string",
    ):
        module._summary_markdown(malformed)

    malformed = report | {"matrix": [report["matrix"][0] | {"fixture": []}]}
    with pytest.raises(TypeError, match="matrix fixture must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {"matrix": [report["matrix"][0] | {"tool": False}]}
    with pytest.raises(TypeError, match="matrix tool must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {"matrix": [report["matrix"][0] | {"evidence": []}]}
    with pytest.raises(TypeError, match="matrix evidence must be a string or null"):
        module._summary_markdown(malformed)

    malformed = report | {"fixtures": [report["fixtures"][0] | {"slug": []}]}
    with pytest.raises(TypeError, match="fixture slug must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {"fixtures": [report["fixtures"][0] | {"dng_layout": False}]}
    with pytest.raises(TypeError, match="fixture dng_layout must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {
        "fixtures": [report["fixtures"][0] | {"raw_ifd_location": []}]
    }
    with pytest.raises(TypeError, match="fixture raw_ifd_location must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {"fixtures": [report["fixtures"][0] | {"dng_bytes": True}]}
    with pytest.raises(TypeError, match="fixture dng_bytes must be an integer"):
        module._summary_markdown(malformed)

    malformed = report | {"fixtures": [report["fixtures"][0] | {"dng_sha256": 7}]}
    with pytest.raises(TypeError, match="fixture dng_sha256 must be a string"):
        module._summary_markdown(malformed)

    malformed = report | {
        "fixtures": [report["fixtures"][0] | {"validation_json_bytes": False}]
    }
    with pytest.raises(
        TypeError,
        match="fixture validation_json_bytes must be an integer",
    ):
        module._summary_markdown(malformed)

    malformed = report | {
        "fixtures": [report["fixtures"][0] | {"validation_json_sha256": []}]
    }
    with pytest.raises(
        TypeError,
        match="fixture validation_json_sha256 must be a string",
    ):
        module._summary_markdown(malformed)

    with pytest.raises(TypeError, match="report fixtures must contain objects"):
        module._fixtures({"fixtures": ["not-a-fixture"]})

    with pytest.raises(TypeError, match="report tools must be an object"):
        module._tools({"tools": []})

    with pytest.raises(TypeError, match="report matrix must contain objects"):
        module._matrix({"matrix": ["not-a-matrix-entry"]})

    with pytest.raises(TypeError, match="matrix result must be a string"):
        module._matrix_result({"result": False})

    with pytest.raises(TypeError, match="matrix notes must be a string"):
        module._matrix_notes({"notes": []})

    with pytest.raises(TypeError, match="report errors must be a list"):
        module._errors({"errors": "none"})

    malformed = report | {"errors": [False]}
    with pytest.raises(TypeError, match="report errors must be a string list"):
        module._summary_markdown(malformed)

    summary = module._summary_markdown(report | {"ok": False, "errors": ["synthetic failure"]})
    assert "## Errors" in summary
    assert "- synthetic failure" in summary


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
    assert "\\" not in " ".join(payload["darktable-cli"]["command"][1:])
    assert "\\" not in " ".join(payload["rawtherapee-cli"]["command"][1:])
    assert "-t" in payload["rawtherapee-cli"]["command"]


def test_adobe_converted_artifact_inspection_is_relaxed_for_rewritten_tags(tmp_path):
    dng_path = tmp_path / "adobe-rewritten-like.dng"
    _write_adobe_rewritten_like_dng(dng_path)

    strict = validate_dng(dng_path, run_smoke=False)
    adobe = inspect_adobe_converted_dng(dng_path, run_smoke=False)

    assert strict.ok is False
    assert any("missing required tag: DNGVersion" in error for error in strict.errors)
    assert adobe.ok is True, adobe.errors
    assert adobe.dng_layout == "preview-subifd"
    assert adobe.raw_ifd_location == "IFD0/SubIFD0"
    assert adobe.checks[-1].name == "adobe-converted-artifact"


def test_adobe_dng_converter_verifier_dry_run_writes_report(tmp_path, monkeypatch):
    module = _load_script_module("verify_adobe_dng_converter")
    monkeypatch.setattr(module, "_resolve_converter", lambda _explicit: (None, None))
    monkeypatch.setattr(
        module,
        "adobe_dng_converter_resource_state",
        lambda **_kwargs: {
            "state": "missing",
            "source": None,
            "executable": None,
            "resource_path": None,
        },
    )
    output_dir = tmp_path / "adobe-dry-run"

    exit_code = module.main(
        [
            "--output-dir",
            str(output_dir),
            "--adobe-dir",
            str(tmp_path / "missing-adobe"),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    report = json.loads(
        (output_dir / "reports" / "adobe-dng-converter-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["schema"] == "image2dng.adobe_dng_converter_verification.v1"
    assert report["dry_run"] is True
    assert report["status"] == "dry-run"
    assert report["source_contract_validation"]["ok"] is True
    assert report["converter"]["available"] is False
    assert report["converter"]["resource_state"]["state"] == "missing"
    assert report["converter_result"] is None
    assert report["converted_artifact_inspection"] is None


def test_adobe_dng_converter_verifier_dry_run_fails_invalid_source(tmp_path, monkeypatch):
    module = _load_script_module("verify_adobe_dng_converter")
    monkeypatch.setattr(module, "_resolve_converter", lambda _explicit: (None, None))

    class FakeValidation:
        def to_dict(self):
            return {"ok": False, "errors": ["synthetic validation failure"]}

    monkeypatch.setattr(module, "validate_dng", lambda *_args, **_kwargs: FakeValidation())
    output_dir = tmp_path / "adobe-dry-run-invalid-source"

    exit_code = module.main(
        [
            "--output-dir",
            str(output_dir),
            "--adobe-dir",
            str(tmp_path / "missing-adobe"),
            "--dry-run",
        ]
    )

    report = json.loads(
        (output_dir / "reports" / "adobe-dng-converter-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert exit_code == 1
    assert report["dry_run"] is True
    assert report["status"] == "failed"
    assert report["ok"] is False
    assert report["errors"] == ["source image2dng contract validation failed"]


def test_adobe_dng_converter_verifier_rejects_malformed_validation_ok():
    module = _load_script_module("verify_adobe_dng_converter")

    with pytest.raises(TypeError, match="source contract validation ok must be a boolean"):
        module._report_ok({"ok": "yes"}, "source contract validation")

    with pytest.raises(
        TypeError,
        match="Adobe-converted artifact inspection ok must be a boolean",
    ):
        module._report_ok({"ok": None}, "Adobe-converted artifact inspection")


def test_adobe_dng_converter_verifier_records_fake_conversion(tmp_path, monkeypatch):
    module = _load_script_module("verify_adobe_dng_converter")
    fake_converter = tmp_path / "Adobe DNG Converter.exe"
    fake_converter.write_text("fake exe", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_resolve_converter",
        lambda _explicit: (str(fake_converter), "fake"),
    )

    def fake_adobe_run(command, **_kwargs):
        output_dir = Path(command[command.index("-d") + 1])
        source = Path(command[-1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / source.name).write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(command, 0, stdout="converted\n", stderr="")

    monkeypatch.setattr("image2dng.compatibility.subprocess.run", fake_adobe_run)
    output_dir = tmp_path / "adobe-run"
    previous_output = output_dir / "converted" / "adobe-single-raw-ifd-fixture.dng"
    previous_output.parent.mkdir(parents=True)
    previous_output.write_bytes(b"previous converted artifact")

    exit_code = module.main(
        [
            "--output-dir",
            str(output_dir),
            "--converter",
            str(fake_converter),
            "--timeout-seconds",
            "1",
        ]
    )

    report = json.loads(
        (output_dir / "reports" / "adobe-dng-converter-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert exit_code == 0
    assert report["ok"] is True
    assert report["converter_result"]["result"] == "passed"
    assert report["converter"]["resource_state"]["state"] == "installed-executable"
    assert report["converter"]["resource_state"]["source"] == "explicit"
    moved_existing = Path(report["artifacts"]["moved_existing_converted_dng"])
    assert moved_existing.exists()
    assert moved_existing.read_bytes() == b"previous converted artifact"
    assert Path(report["converter_result"]["output_artifacts"][0]).exists()
    assert report["converted_artifact_inspection"]["ok"] is True
    assert report["errors"] == []


def test_adobe_dng_converter_resource_state_reports_local_installer_resource(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "image2dng.compatibility.resolve_processor_executable",
        lambda _tool: (None, None),
    )
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    resource = adobe_dir / "AdobeDNGConverter_x64_18_3_1.exe"
    resource.write_bytes(b"fake installer")

    state = adobe_dng_converter_resource_state(adobe_dir=adobe_dir)

    assert state["state"] == "resource-present-not-installed"
    assert state["source"] == "adobe-dir"
    assert state["executable"] is None
    assert state["resource_path"] == str(resource.resolve())


def test_adobe_dng_converter_resource_state_ignores_matching_text_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "image2dng.compatibility.resolve_processor_executable",
        lambda _tool: (None, None),
    )
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    (adobe_dir / "DNGConverter-release-notes.txt").write_text("not a converter", encoding="utf-8")

    state = adobe_dng_converter_resource_state(adobe_dir=adobe_dir)

    assert state["state"] == "missing"


def test_adobe_local_resource_audit_writes_readiness_report(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    (adobe_dir / "AdobeDNGConverter_x64_18_3_1.exe").write_bytes(
        b"fake converter installer resource"
    )
    (adobe_dir / "DNG_Spec_1_7_1_0.pdf").write_bytes(b"fake dng spec")
    (adobe_dir / "TIFF6.pdf").write_bytes(b"fake tiff spec")
    _write_zip(
        adobe_dir / "dng_sdk_1_7_1_2573_20260512.zip",
        {
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate.sln": "solution",
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate/dng_validate.vcxproj": (
                "project"
            ),
        },
    )
    _write_zip(
        adobe_dir / "ACR_and_Lightroom_Profile_SDK.zip",
        {"ACR_and_Lightroom_Profile_SDK/Adobe_Color_example.DNG": "dng"},
    )
    output_dir = tmp_path / "audit-output"

    exit_code = module.main(
        [
            "--adobe-dir",
            str(adobe_dir),
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    report = json.loads(
        (output_dir / "adobe-local-resource-report.json").read_text(encoding="utf-8")
    )
    assert exit_code == 0
    assert report["schema"] == "image2dng.adobe_local_resource_audit.v1"
    assert report["ok"] is True
    assert report["policy"]["installer_execution"] == "not-attempted"
    assert report["policy"]["archive_extraction"] == "not-attempted"
    assert report["policy"]["sdk_build"] == "manual-only"
    assert report["readiness"] == {
        "dng_converter_installer_or_resource": True,
        "dng_converter_installed_executable": False,
        "dng_converter_resource_state": "resource-present-not-installed",
        "dng_sdk_archive": True,
        "dng_sdk_validate_project_detected": True,
        "dng_specification": True,
        "tiff_reference": True,
        "profile_sdk_or_tools": True,
    }
    assert report["missing_required_kinds"] == []
    resource_hashes = {
        resource["name"]: resource["sha256"] for resource in report["resources"]
    }
    assert resource_hashes["AdobeDNGConverter_x64_18_3_1.exe"] == _sha256_test_file(
        adobe_dir / "AdobeDNGConverter_x64_18_3_1.exe"
    )
    assert resource_hashes["DNG_Spec_1_7_1_0.pdf"] == _sha256_test_file(
        adobe_dir / "DNG_Spec_1_7_1_0.pdf"
    )
    assert resource_hashes["dng_sdk_1_7_1_2573_20260512.zip"] == _sha256_test_file(
        adobe_dir / "dng_sdk_1_7_1_2573_20260512.zip"
    )
    summary = (output_dir / "adobe-local-resource-summary.md").read_text(encoding="utf-8")
    assert resource_hashes["AdobeDNGConverter_x64_18_3_1.exe"] in summary
    assert resource_hashes["DNG_Spec_1_7_1_0.pdf"] in summary
    assert resource_hashes["dng_sdk_1_7_1_2573_20260512.zip"] in summary


def test_adobe_local_resource_audit_refuses_tracked_output(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    output_dir = tmp_path / "tracked-output"

    exit_code = module.main(
        [
            "--adobe-dir",
            str(adobe_dir),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()


def test_adobe_local_resource_audit_reports_missing_required_resources(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    (adobe_dir / "DNG_Spec_1_7_1_0.pdf").write_bytes(b"fake dng spec")
    output_dir = tmp_path / "audit-output"

    exit_code = module.main(
        [
            "--adobe-dir",
            str(adobe_dir),
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    report = json.loads(
        (output_dir / "adobe-local-resource-report.json").read_text(encoding="utf-8")
    )
    assert exit_code == 1
    assert report["ok"] is False
    assert report["missing_required_kinds"] == [
        "dng-converter-resource",
        "dng-sdk-archive",
    ]
    assert report["blocking_findings"] == [
        "missing required resource kind: dng-converter-resource",
        "missing required resource kind: dng-sdk-archive",
    ]


def test_adobe_local_resource_audit_rejects_malformed_summary_lists(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    report = module.build_report(adobe_dir)

    report["schema"] = 1
    with pytest.raises(TypeError, match="report schema must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["ok"] = "true"
    with pytest.raises(TypeError, match="report ok must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["adobe_dir"] = []
    with pytest.raises(TypeError, match="report adobe_dir must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["local_only"] = "yes"
    with pytest.raises(TypeError, match="report local_only must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["readiness"] = {"dng_sdk_archive": []}
    with pytest.raises(TypeError, match="report readiness must be an object"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["readiness"] = {False: True}
    with pytest.raises(TypeError, match="report readiness must be an object"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["missing_required_kinds"] = ["dng-sdk-archive", 7]
    with pytest.raises(
        TypeError,
        match="report missing_required_kinds must be a string list",
    ):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["blocking_findings"] = "missing"
    with pytest.raises(TypeError, match="report blocking_findings must be a string list"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["resources"] = ["not-a-resource"]
    with pytest.raises(TypeError, match="report resources must be an object list"):
        module._summary_markdown(report)

    resource_path = adobe_dir / "DNG_Spec_1_7_1_0.pdf"
    resource_path.write_bytes(b"fake dng spec")

    report = module.build_report(adobe_dir)
    report["resources"][0]["name"] = []
    with pytest.raises(TypeError, match="resource name must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["resources"][0]["kind"] = False
    with pytest.raises(TypeError, match="resource kind must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["resources"][0]["size_bytes"] = True
    with pytest.raises(TypeError, match="resource size_bytes must be an integer"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir)
    report["resources"][0]["sha256"] = 7
    with pytest.raises(TypeError, match="resource sha256 must be a string"):
        module._summary_markdown(report)

    with pytest.raises(TypeError, match="resource kind must be a string"):
        module._converter_resource_state([{"kind": False, "name": "Adobe DNG Converter.exe"}])

    with pytest.raises(TypeError, match="resource name must be a string"):
        module._converter_resource_state([{"kind": "dng-converter-resource", "name": []}])

    with pytest.raises(TypeError, match="resource zip must be an object"):
        module._zip_findings([{"kind": "dng-sdk-archive", "zip": []}])

    with pytest.raises(TypeError, match="resource zip findings must be an object"):
        module._zip_findings([{"kind": "dng-sdk-archive", "zip": {"findings": []}}])

    with pytest.raises(
        TypeError,
        match="resource zip finding dng_validate_solution must be a boolean",
    ):
        module._zip_findings(
            [
                {
                    "kind": "dng-sdk-archive",
                    "zip": {"findings": {"dng_validate_solution": "yes"}},
                }
            ]
        )


def test_adobe_local_resource_audit_recognizes_spaced_converter_name(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    (adobe_dir / "Adobe DNG Converter.exe").write_bytes(b"fake installed converter")
    (adobe_dir / "DNG_Spec_1_7_1_0.pdf").write_bytes(b"fake dng spec")
    _write_zip(
        adobe_dir / "dng_sdk_1_7_1.zip",
        {
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate.sln": "solution",
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate/dng_validate.vcxproj": (
                "project"
            ),
        },
    )

    report = module.build_report(adobe_dir)

    assert report["ok"] is True
    assert report["readiness"]["dng_converter_resource_state"] == "installed-executable"
    assert report["readiness"]["dng_converter_installed_executable"] is True
    assert report["missing_required_kinds"] == []


def test_adobe_local_resource_audit_preserves_any_valid_sdk_archive(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    (adobe_dir / "AdobeDNGConverter_x64_18_3_1.exe").write_bytes(b"fake installer")
    (adobe_dir / "DNG_Spec_1_7_1_0.pdf").write_bytes(b"fake dng spec")
    _write_zip(
        adobe_dir / "dng_sdk_1_7_1_valid.zip",
        {
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate.sln": "solution",
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate/dng_validate.vcxproj": (
                "project"
            ),
        },
    )
    _write_zip(adobe_dir / "dng_sdk_1_7_1_without_validate.zip", {"README.txt": "docs"})

    report = module.build_report(adobe_dir)

    assert report["ok"] is True
    assert report["readiness"]["dng_sdk_validate_project_detected"] is True


def test_adobe_local_resource_audit_rejects_unreadable_sdk_archive(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    (adobe_dir / "AdobeDNGConverter_x64_18_3_1.exe").write_bytes(b"fake installer")
    (adobe_dir / "DNG_Spec_1_7_1_0.pdf").write_bytes(b"fake dng spec")
    (adobe_dir / "dng_sdk_corrupt.zip").write_bytes(b"not a zip archive")

    report = module.build_report(adobe_dir)

    assert report["ok"] is False
    assert report["missing_required_kinds"] == []
    assert report["blocking_findings"] == [
        "no readable DNG SDK archive with dng_validate project detected"
    ]
    assert report["readiness"]["dng_sdk_archive"] is True
    assert report["readiness"]["dng_sdk_validate_project_detected"] is False


def test_adobe_local_resource_audit_ignores_converter_text_file(tmp_path):
    module = _load_script_module("audit_adobe_local_resources")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    (adobe_dir / "DNGConverter-release-notes.txt").write_text("not a converter", encoding="utf-8")
    (adobe_dir / "DNG_Spec_1_7_1_0.pdf").write_bytes(b"fake dng spec")
    _write_zip(
        adobe_dir / "dng_sdk_1_7_1.zip",
        {
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate.sln": "solution",
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate/dng_validate.vcxproj": (
                "project"
            ),
        },
    )

    report = module.build_report(adobe_dir)

    assert report["readiness"]["dng_converter_resource_state"] == "missing"
    assert "dng-converter-resource" in report["missing_required_kinds"]


def test_prepare_adobe_dng_sdk_manual_validation_writes_plan(tmp_path):
    module = _load_script_module("prepare_adobe_dng_sdk_manual_validation")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    _write_zip(
        adobe_dir / "dng_sdk_1_7_1.zip",
        {
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate.sln": "solution",
            "dng_sdk_1_7_1/dng_sdk/projects/win/dng_validate/dng_validate.vcxproj": (
                "project"
            ),
        },
    )
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "sample.dng").write_bytes(b"fake dng fixture")
    output_dir = tmp_path / "sdk-plan"

    exit_code = module.main(
        [
            "--adobe-dir",
            str(adobe_dir),
            "--fixture-dir",
            str(fixture_dir),
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    report = json.loads(
        (output_dir / "adobe-dng-sdk-manual-validation-plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert exit_code == 0
    assert report["schema"] == "image2dng.adobe_dng_sdk_manual_validation_plan.v1"
    assert report["ok"] is True
    assert report["policy"] == {
        "archive_extraction": "not-attempted",
        "ci_gate": False,
        "sdk_build": "manual-only",
        "sdk_execution": "not-attempted",
    }
    assert report["selected_sdk_archive"]["name"] == "dng_sdk_1_7_1.zip"
    assert report["representative_fixtures"][0]["dng_count"] == 1
    assert report["representative_fixtures"][0]["sample_dngs"] == [
        {
            "path": str(fixture_dir / "sample.dng"),
            "size_bytes": (fixture_dir / "sample.dng").stat().st_size,
            "sha256": _sha256_test_file(fixture_dir / "sample.dng"),
        }
    ]
    assert all(step["manual_only"] is True for step in report["manual_steps"])
    summary = (output_dir / "adobe-dng-sdk-manual-validation-plan.md").read_text(
        encoding="utf-8"
    )
    assert str(fixture_dir / "sample.dng") in summary
    assert _sha256_test_file(fixture_dir / "sample.dng") in summary


def test_prepare_adobe_dng_sdk_manual_validation_refuses_tracked_output(tmp_path):
    module = _load_script_module("prepare_adobe_dng_sdk_manual_validation")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    output_dir = tmp_path / "tracked-output"

    exit_code = module.main(
        [
            "--adobe-dir",
            str(adobe_dir),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()


def test_prepare_adobe_dng_sdk_manual_validation_reports_missing_sdk(tmp_path):
    module = _load_script_module("prepare_adobe_dng_sdk_manual_validation")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    output_dir = tmp_path / "sdk-plan"

    exit_code = module.main(
        [
            "--adobe-dir",
            str(adobe_dir),
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    report = json.loads(
        (output_dir / "adobe-dng-sdk-manual-validation-plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert exit_code == 1
    assert report["ok"] is False
    assert report["status"] == "missing-sdk-validate-project"
    assert report["selected_sdk_archive"] is None


def test_prepare_adobe_dng_sdk_manual_validation_rejects_malformed_summary_records(tmp_path):
    module = _load_script_module("prepare_adobe_dng_sdk_manual_validation")
    adobe_dir = tmp_path / "Adobe"
    adobe_dir.mkdir()
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "sample.dng").write_bytes(b"fake dng")
    report = module.build_report(adobe_dir, fixture_dirs=(tmp_path / "fixtures",))

    report["schema"] = 1
    with pytest.raises(TypeError, match="report schema must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["ok"] = "false"
    with pytest.raises(TypeError, match="report ok must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["status"] = []
    with pytest.raises(TypeError, match="report status must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["local_only"] = "yes"
    with pytest.raises(TypeError, match="report local_only must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["selected_sdk_archive"] = {"path": []}
    with pytest.raises(TypeError, match="selected SDK archive path must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["selected_sdk_archive"] = "dng_sdk.zip"
    with pytest.raises(
        TypeError,
        match="report selected_sdk_archive must be an object or null",
    ):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(tmp_path / "fixtures",))
    report["representative_fixtures"] = ["not-a-fixture"]
    with pytest.raises(
        TypeError,
        match="report representative_fixtures must be an object list",
    ):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["representative_fixtures"][0]["directory"] = []
    with pytest.raises(TypeError, match="fixture directory must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["representative_fixtures"][0]["exists"] = "yes"
    with pytest.raises(TypeError, match="fixture exists must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["representative_fixtures"][0]["dng_count"] = False
    with pytest.raises(TypeError, match="fixture dng_count must be an integer"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["representative_fixtures"][0]["sample_dngs"] = ["sample.dng"]
    with pytest.raises(TypeError, match="fixture sample_dngs must be an object list"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["representative_fixtures"][0]["sample_dngs"][0]["path"] = []
    with pytest.raises(TypeError, match="sample path must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["representative_fixtures"][0]["sample_dngs"][0]["size_bytes"] = True
    with pytest.raises(TypeError, match="sample size_bytes must be an integer"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(fixture_dir,))
    report["representative_fixtures"][0]["sample_dngs"][0]["sha256"] = 7
    with pytest.raises(TypeError, match="sample sha256 must be a string"):
        module._summary_markdown(report)

    report = module.build_report(adobe_dir, fixture_dirs=(tmp_path / "fixtures",))
    report["manual_steps"] = ["not-a-step"]
    with pytest.raises(TypeError, match="report manual_steps must be an object list"):
        module._summary_markdown(report)


def test_run_adobe_dng_sdk_validation_writes_passing_batch_report(tmp_path):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    validator = tmp_path / "Adobe DNG Validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    fixture_dir = tmp_path / "fixtures with space"
    nested_dir = fixture_dir / "nested"
    nested_dir.mkdir(parents=True)
    sample = nested_dir / "sample image.dng"
    sample.write_bytes(b"fake dng")
    runner = _FakeDngValidateRunner()

    report = module.build_report(
        validator=validator,
        fixture_roots=(fixture_dir, fixture_dir),
        output_dir=tmp_path / "reports",
        timeout_seconds=5,
        allow_empty=False,
        runner=runner,
    )

    assert report["schema"] == "image2dng.adobe_dng_sdk_validation_report.v1"
    assert report["ok"] is True
    assert report["validator"]["version_probe"]["version_text"] == "1.7.1 (2573) (64-bit)"
    assert report["summary"] == {
        "failed": 0,
        "marker_blocked": 0,
        "passed": 1,
        "selected": 1,
        "skipped": 0,
        "timeout": 0,
    }
    assert report["results"][0]["command"] == [str(validator.resolve()), str(sample.resolve())]
    assert report["results"][0]["sha256"] == _sha256_test_file(sample)
    assert report["results"][0]["status"] == "passed"
    assert report["error_markers"] == []


def test_run_adobe_dng_sdk_validation_records_failure_timeout_and_markers(tmp_path):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    validator = tmp_path / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "bad.dng").write_bytes(b"bad")
    (fixture_dir / "hang.dng").write_bytes(b"hang")
    runner = _FakeDngValidateRunner(fail_names={"bad.dng"}, timeout_names={"hang.dng"})

    report = module.build_report(
        validator=validator,
        fixture_roots=(fixture_dir,),
        output_dir=tmp_path / "reports",
        timeout_seconds=0.1,
        allow_empty=False,
        runner=runner,
    )

    assert report["ok"] is False
    assert report["summary"] == {
        "failed": 1,
        "marker_blocked": 0,
        "passed": 0,
        "selected": 2,
        "skipped": 0,
        "timeout": 1,
    }
    assert "1 DNG fixture validation(s) failed" in report["blocking_findings"]
    assert "1 DNG fixture validation(s) timed out" in report["blocking_findings"]
    marker_fixtures = {record["fixture"] for record in report["error_markers"]}
    assert any("bad.dng" in fixture for fixture in marker_fixtures)
    assert any("hang.dng" in fixture for fixture in marker_fixtures)


def test_run_adobe_dng_sdk_validation_reports_missing_validator_and_empty_fixtures(
    tmp_path,
):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    runner = _FakeDngValidateRunner()

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )

    assert report["ok"] is False
    assert "validator executable missing" in report["blocking_findings"]
    assert (
        "validator version probe did not detect dng_validate version text"
        in report["blocking_findings"]
    )
    assert "no DNG fixtures selected" in report["blocking_findings"]


def test_run_adobe_dng_sdk_validation_rejects_malformed_summary_lists(tmp_path):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    runner = _FakeDngValidateRunner()
    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )

    report["schema"] = 1
    with pytest.raises(TypeError, match="report schema must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["ok"] = "false"
    with pytest.raises(TypeError, match="report ok must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["local_only"] = "yes"
    with pytest.raises(TypeError, match="report local_only must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["summary"]["failed"] = True
    with pytest.raises(TypeError, match="summary failed must be an integer"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["validator"] = []
    with pytest.raises(TypeError, match="report validator must be an object"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["validator"]["path"] = []
    with pytest.raises(TypeError, match="validator path must be an object"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["validator"]["path"]["repo_relative"] = False
    with pytest.raises(
        TypeError,
        match="validator repo_relative path must be a string or null",
    ):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["validator"]["version_probe"] = []
    with pytest.raises(TypeError, match="validator version_probe must be an object"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["validator"]["version_probe"]["version_text"] = []
    with pytest.raises(TypeError, match="validator version_text must be a string or null"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["blocking_findings"] = [False]
    with pytest.raises(TypeError, match="report blocking_findings must be a string list"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["error_markers"] = ["not-a-marker-record"]
    with pytest.raises(TypeError, match="report error_markers must be an object list"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["error_markers"] = [{"fixture": [], "status": "failed", "markers": ["error"]}]
    with pytest.raises(TypeError, match="error marker fixture must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["error_markers"] = [{"fixture": "sample.dng", "status": False, "markers": ["error"]}]
    with pytest.raises(TypeError, match="error marker status must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["error_markers"] = [{"fixture": "sample.dng", "status": "failed", "markers": [7]}]
    with pytest.raises(TypeError, match="error marker markers must be a string list"):
        module._summary_markdown(report)

    report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=runner,
    )
    report["results"] = ["not-a-result-record"]
    with pytest.raises(TypeError, match="report results must be an object list"):
        module._summary_markdown(report)

    result_report = module.build_report(
        validator=tmp_path / "missing.exe",
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=True,
        runner=runner,
    )
    result_report["results"] = [
        {
            "fixture": {"repo_relative": "sample.dng"},
            "status": "failed",
            "exit_code": 1,
            "timeout": False,
            "duration_seconds": 0.1,
        }
    ]

    malformed = result_report | {
        "results": [result_report["results"][0] | {"fixture": []}]
    }
    with pytest.raises(TypeError, match="result fixture must be an object"):
        module._summary_markdown(malformed)

    malformed = result_report | {
        "results": [
            result_report["results"][0]
            | {"fixture": {"repo_relative": False}}
        ]
    }
    with pytest.raises(
        TypeError,
        match="result fixture repo_relative must be a string or null",
    ):
        module._summary_markdown(malformed)

    malformed = result_report | {
        "results": [result_report["results"][0] | {"status": []}]
    }
    with pytest.raises(
        TypeError,
        match="result status must be passed, failed, timeout, or marker-blocked",
    ):
        module._summary_markdown(malformed)

    malformed = result_report | {
        "results": [result_report["results"][0] | {"exit_code": True}]
    }
    with pytest.raises(TypeError, match="result exit_code must be an integer or null"):
        module._summary_markdown(malformed)

    malformed = result_report | {
        "results": [result_report["results"][0] | {"timeout": "false"}]
    }
    with pytest.raises(TypeError, match="result timeout must be a boolean"):
        module._summary_markdown(malformed)

    malformed = result_report | {
        "results": [result_report["results"][0] | {"duration_seconds": "slow"}]
    }
    with pytest.raises(TypeError, match="result duration_seconds must be numeric"):
        module._summary_markdown(malformed)


def test_run_adobe_dng_sdk_validation_rejects_malformed_summary_count_status():
    module = _load_script_module("run_adobe_dng_sdk_validation")

    assert module._summary_counts(
        [
            {"status": "passed"},
            {"status": "failed"},
            {"status": "timeout"},
            {"status": "marker-blocked"},
        ],
        selected_count=4,
    ) == {
        "selected": 4,
        "passed": 1,
        "failed": 1,
        "marker_blocked": 1,
        "timeout": 1,
        "skipped": 0,
    }

    with pytest.raises(
        TypeError,
        match="result status must be passed, failed, timeout, or marker-blocked",
    ):
        module._summary_counts([{"status": "skipped"}], selected_count=1)

    with pytest.raises(
        TypeError,
        match="result status must be passed, failed, timeout, or marker-blocked",
    ):
        module._summary_counts([{"status": False}], selected_count=1)


def test_run_adobe_dng_sdk_validation_blocks_version_probe_timeout_with_version_text(
    tmp_path,
):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    validator = tmp_path / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "sample.dng").write_bytes(b"fake dng")

    report = module.build_report(
        validator=validator,
        fixture_roots=(fixture_dir,),
        output_dir=tmp_path / "reports",
        timeout_seconds=0.1,
        allow_empty=False,
        runner=_FakeDngValidateRunner(version_timeout=True),
    )

    assert report["ok"] is False
    assert report["validator"]["version_probe"]["timeout"] is True
    assert report["validator"]["version_probe"]["version_text"] == "1.7.1 (2573) (64-bit)"
    assert "validator version probe timed out" in report["blocking_findings"]


def test_run_adobe_dng_sdk_validation_blocks_version_probe_without_version_text(
    tmp_path,
):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    validator = tmp_path / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "sample.dng").write_bytes(b"fake dng")

    report = module.build_report(
        validator=validator,
        fixture_roots=(fixture_dir,),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=_FakeDngValidateRunner(version_exit_code=1, version_stdout="Usage only\n"),
    )

    assert report["ok"] is False
    assert report["validator"]["version_probe"]["version_text"] is None
    assert (
        "validator version probe did not detect dng_validate version text"
        in report["blocking_findings"]
    )


def test_run_adobe_dng_sdk_validation_blocks_marker_with_zero_exit(tmp_path):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    validator = tmp_path / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "sample.dng").write_bytes(b"fake dng")

    report = module.build_report(
        validator=validator,
        fixture_roots=(fixture_dir,),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=False,
        runner=_FakeDngValidateRunner(marker_pass_names={"sample.dng"}),
    )

    assert report["ok"] is False
    assert report["results"][0]["status"] == "marker-blocked"
    assert report["error_markers"] == [
        {
            "fixture": str((fixture_dir / "sample.dng").resolve()),
            "markers": ["error"],
            "status": "marker-blocked",
        }
    ]
    assert report["summary"]["marker_blocked"] == 1
    assert "1 DNG fixture validation(s) emitted error markers" in report["blocking_findings"]


def test_run_adobe_dng_sdk_validation_markers_ignore_benign_success_text():
    module = _load_script_module("run_adobe_dng_sdk_validation")

    assert (
        module._markers(
            'Validating "C:/fixtures/invalid-name-error-case.dng"...\n'
            'Validating "C:/fixtures/corrupt-exception-validation-failed-case.dng"...\n'
            "Validation complete\n"
            "No errors found\n"
            "0 errors\n",
            "",
        )
        == []
    )
    assert module._markers("ERROR: corrupt image\n", "") == ["error", "corrupt"]


def test_run_adobe_dng_sdk_validation_fixture_discovery_skips_symlink_escape(
    tmp_path,
):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    fixture_dir = tmp_path / "fixtures"
    outside_dir = tmp_path / "outside"
    fixture_dir.mkdir()
    outside_dir.mkdir()
    inside = fixture_dir / "inside.dng"
    outside = outside_dir / "outside.dng"
    inside.write_bytes(b"inside")
    outside.write_bytes(b"outside")
    escaped = fixture_dir / "escaped.dng"
    try:
        escaped.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available in this environment")

    discovered = module._discover_fixtures((fixture_dir,))

    assert inside.resolve() in discovered
    assert outside.resolve() not in discovered


def test_run_adobe_dng_sdk_validation_fixture_discovery_skips_in_root_symlink(
    tmp_path,
):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    real_dng = fixture_dir / "real.dng"
    real_dng.write_bytes(b"real")
    linked_dng = fixture_dir / "linked.dng"
    try:
        linked_dng.symlink_to(real_dng)
    except OSError:
        pytest.skip("symlink creation is not available in this environment")

    discovered = module._discover_fixtures((fixture_dir,))

    assert discovered == [real_dng.resolve()]


def test_run_adobe_dng_sdk_validation_allows_empty_when_explicit(tmp_path):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    validator = tmp_path / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")

    report = module.build_report(
        validator=validator,
        fixture_roots=(tmp_path / "missing-fixtures",),
        output_dir=tmp_path / "reports",
        timeout_seconds=1,
        allow_empty=True,
        runner=_FakeDngValidateRunner(),
    )

    assert report["ok"] is True
    assert report["summary"]["selected"] == 0
    assert report["results"] == []


def test_run_adobe_dng_sdk_validation_refuses_unsafe_output_dirs(tmp_path):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    repo_root = tmp_path / "repo"
    adobe_dir = repo_root / "Adobe"
    fixture_dir = repo_root / "fixtures"
    adobe_dir.mkdir(parents=True)
    fixture_dir.mkdir()

    assert module._output_dir_refusal_reason(
        repo_root / "tracked-report",
        repo_root=repo_root,
        fixture_roots=(fixture_dir,),
        allow_outside_demo_output=False,
    ).startswith("Refusing to write Adobe DNG SDK validation report outside demo-output/")
    assert module._output_dir_refusal_reason(
        adobe_dir / "report",
        repo_root=repo_root,
        fixture_roots=(fixture_dir,),
        allow_outside_demo_output=True,
    ) == "Refusing to write Adobe DNG SDK validation report inside Adobe/."
    assert module._output_dir_refusal_reason(
        fixture_dir / "report",
        repo_root=repo_root,
        fixture_roots=(fixture_dir,),
        allow_outside_demo_output=True,
    ) == "Refusing to write Adobe DNG SDK validation report inside a fixture directory."
    assert module._output_dir_refusal_reason(
        repo_root / "tracked-report",
        repo_root=repo_root,
        fixture_roots=(fixture_dir,),
        allow_outside_demo_output=True,
    ).startswith("Refusing to write Adobe DNG SDK validation report inside a tracked repo area")
    assert (
        module._output_dir_refusal_reason(
            repo_root / "demo-output" / "adobe-dng-sdk-validation",
            repo_root=repo_root,
            fixture_roots=(fixture_dir,),
            allow_outside_demo_output=False,
        )
        is None
    )


def test_run_adobe_dng_sdk_validation_main_writes_reports(tmp_path):
    module = _load_script_module("run_adobe_dng_sdk_validation")
    validator = tmp_path / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "sample.dng").write_bytes(b"fake dng")
    output_dir = tmp_path / "reports"
    original_run = module.subprocess.run
    module.subprocess.run = _FakeDngValidateRunner()
    try:
        exit_code = module.main(
            [
                "--validator",
                str(validator),
                "--fixture-dir",
                str(fixture_dir),
                "--output-dir",
                str(output_dir),
                "--allow-output-outside-demo-output",
            ]
        )
    finally:
        module.subprocess.run = original_run

    report = json.loads(
        (output_dir / "adobe-dng-sdk-validation-report.json").read_text(encoding="utf-8")
    )
    assert exit_code == 0
    assert report["ok"] is True
    assert (output_dir / "adobe-dng-sdk-validation-report.md").exists()


def test_verify_adobe_validation_stack_writes_passing_report(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    adobe_dir = repo_root / "Adobe"
    adobe_dir.mkdir()
    validator = adobe_dir / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    output_dir = repo_root / "demo-output" / "adobe-validation-stack"
    runner = _FakeAdobeValidationStackRunner()

    report = module.build_report(
        output_dir=output_dir,
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=runner,
    )

    assert report["schema"] == "image2dng.adobe_validation_stack_report.v1"
    assert report["ok"] is True
    assert report["summary"] == {
        "blocking_finding_count": 0,
        "failed": 0,
        "passed": 4,
        "step_count": 4,
    }
    project_step = _stack_step(report, "project-dng-fixtures")
    assert project_step["command"][-2:] == ["--dng-layout", "single-raw-ifd"]
    inspection = project_step["project_fixture_inspection"]
    assert inspection["schema"] == "image2dng.raw_native_node_batch.v1"
    assert inspection["sample_index_schema"] == "image2dng.raw_native_sample_index.v1"
    assert inspection["all_validations_ok"] is True
    assert inspection["dng_count"] == 2
    assert inspection["fixture_roots"]
    sdk_step = _stack_step(report, "adobe-dng-sdk-validation")
    sdk_command = sdk_step["command"]
    assert isinstance(sdk_command, list)
    sdk_fixture_dirs = [
        Path(sdk_command[index + 1])
        for index, value in enumerate(sdk_command)
        if value == "--fixture-dir"
    ]
    sdk_fixture_dirs = [path.resolve() for path in sdk_fixture_dirs]
    assert (
        output_dir / "project-dng-fixtures" / "raw-native-node-batch" / "raw"
    ).resolve() in sdk_fixture_dirs
    assert (
        output_dir / "adobe-dng-converter-verification" / "source-dng"
    ).resolve() in sdk_fixture_dirs
    assert (
        output_dir / "adobe-dng-converter-verification" / "converted"
    ).resolve() in sdk_fixture_dirs
    assert all(
        "--allow-output-outside-demo-output" not in step["command"] for step in report["steps"]
    )


def test_verify_adobe_validation_stack_rejects_malformed_summary_findings(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    adobe_dir = repo_root / "Adobe"
    adobe_dir.mkdir()
    validator = adobe_dir / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")
    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )

    report["schema"] = 1
    with pytest.raises(TypeError, match="report schema must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["ok"] = "false"
    with pytest.raises(TypeError, match="report ok must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["run_id"] = []
    with pytest.raises(TypeError, match="report run_id must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["output_dir"] = []
    with pytest.raises(TypeError, match="report output_dir must be an object"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["output_dir"]["display"] = False
    with pytest.raises(TypeError, match="report output_dir display must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["local_only"] = "yes"
    with pytest.raises(TypeError, match="report local_only must be a boolean"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["blocking_findings"] = [False]
    with pytest.raises(TypeError, match="report blocking_findings must be a string list"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["steps"][0]["name"] = []
    with pytest.raises(TypeError, match="step name must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["steps"][0]["status"] = False
    with pytest.raises(TypeError, match="step status must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["steps"][0]["exit_code"] = True
    with pytest.raises(TypeError, match="step exit_code must be an integer or null"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["steps"][0]["child_report_path"] = []
    with pytest.raises(TypeError, match="step child_report_path must be an object"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["steps"][0]["child_report_path"]["display"] = []
    with pytest.raises(TypeError, match="child report display must be a string"):
        module._summary_markdown(report)

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )
    report["steps"][0]["blocking_findings"] = ["ok", False]
    with pytest.raises(TypeError, match="step blocking_findings must be a string list"):
        module._summary_markdown(report)


def test_verify_adobe_validation_stack_rejects_malformed_integration_booleans():
    module = _load_script_module("verify_adobe_validation_stack")

    with pytest.raises(TypeError, match="child report ok must be a boolean"):
        module._child_report_ok({"ok": "true"})

    with pytest.raises(TypeError, match="child report schema must be a string"):
        module._child_report_schema({"schema": False})

    with pytest.raises(TypeError, match="child report generated_at must be a string"):
        module._child_report_generated_at({"generated_at": False})

    assert module._child_report_generated_at({}) is None

    with pytest.raises(TypeError, match="child report status must be a string"):
        module._child_report_status({"status": False})

    assert module._child_report_status({}) is None

    with pytest.raises(
        TypeError,
        match="child report blocking_findings must be a string list",
    ):
        module._child_blocking_findings({"blocking_findings": ["ok", False]})

    with pytest.raises(TypeError, match="child report errors must be a string list"):
        module._child_blocking_findings({"errors": ["ok", False]})

    assert module._child_blocking_findings({"errors": ["synthetic child failure"]}) == [
        "synthetic child failure"
    ]

    with pytest.raises(TypeError, match="step blocking_findings must be a string list"):
        module._extend_blocking_findings([], {"blocking_findings": ["ok", False]})

    with pytest.raises(TypeError, match="step blocking_findings must be a string list"):
        module._finalize_step_status({"blocking_findings": [False]})

    with pytest.raises(TypeError, match="step project_fixture_roots must be an object list"):
        module._project_fixture_roots({"project_fixture_roots": ["not-a-root-record"]})

    with pytest.raises(TypeError, match="path record absolute must be a string"):
        module._project_fixture_roots({"project_fixture_roots": [{"absolute": []}]})

    assert module._project_fixture_roots(
        {"project_fixture_roots": [{"absolute": str(Path.cwd())}]}
    ) == [Path.cwd()]

    with pytest.raises(
        TypeError,
        match="project fixture manifest schema must be a string",
    ):
        module._project_fixture_manifest_schema({"schema": False})

    assert module._project_fixture_manifest_schema({}) is None

    with pytest.raises(
        TypeError,
        match="project fixture sample index schema must be a string",
    ):
        module._project_fixture_sample_index_schema({"schema": []})

    assert module._project_fixture_sample_index_schema({}) is None

    with pytest.raises(
        TypeError,
        match="project fixture manifest scenes must be a list",
    ):
        module._project_fixture_scenes({"scenes": {}})

    assert module._project_fixture_scenes({}) == []

    with pytest.raises(
        TypeError,
        match="project fixture scene outputs must be an object",
    ):
        module._project_fixture_scene_outputs({"outputs": []})

    assert module._project_fixture_scene_outputs({}) is None

    with pytest.raises(
        TypeError,
        match="project fixture output linearraw_dng must be a string",
    ):
        module._project_fixture_output_path({"linearraw_dng": []}, "linearraw_dng")

    assert module._project_fixture_output_path({}, "linearraw_dng") is None

    with pytest.raises(
        TypeError,
        match="project fixture validation linearraw must be an object",
    ):
        module._project_fixture_validation_record({"linearraw": []}, "linearraw")

    assert module._project_fixture_validation_record({}, "linearraw") is None

    with pytest.raises(
        TypeError,
        match="project fixture sample index all_validations_ok must be a boolean",
    ):
        module._sample_index_all_validations_ok({"all_validations_ok": "yes"})

    with pytest.raises(
        TypeError,
        match="project fixture validation linearraw ok must be a boolean",
    ):
        module._validation_ok({"ok": []}, "project fixture validation linearraw")


def test_verify_adobe_validation_stack_dry_run_converter_is_not_readiness(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    adobe_dir = repo_root / "Adobe"
    adobe_dir.mkdir()
    validator = adobe_dir / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=True,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(),
    )

    assert report["ok"] is False
    assert (
        "Adobe DNG Converter dry-run mode does not prove full Adobe readiness"
        in report["blocking_findings"]
    )
    converter_step = _stack_step(report, "adobe-dng-converter")
    assert converter_step["child_report"]["dry_run"] is True


def test_verify_adobe_validation_stack_rejects_stale_child_report(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    adobe_dir = repo_root / "Adobe"
    adobe_dir.mkdir()
    validator = adobe_dir / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(
            stale_report_names={"adobe-dng-sdk-validation"}
        ),
    )

    assert report["ok"] is False
    sdk_step = _stack_step(report, "adobe-dng-sdk-validation")
    assert "child report generated_at predates the child step start time" in sdk_step[
        "blocking_findings"
    ]


def test_verify_adobe_validation_stack_rejects_future_dated_child_report(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    adobe_dir = repo_root / "Adobe"
    adobe_dir.mkdir()
    validator = adobe_dir / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(
            future_report_names={"adobe-dng-converter"}
        ),
    )

    assert report["ok"] is False
    converter_step = _stack_step(report, "adobe-dng-converter")
    assert "child report generated_at is after the child step finish time" in converter_step[
        "blocking_findings"
    ]


def test_verify_adobe_validation_stack_reports_missing_child_report(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    adobe_dir = repo_root / "Adobe"
    adobe_dir.mkdir()
    validator = adobe_dir / "dng_validate.exe"
    validator.write_text("fake validator", encoding="utf-8")

    report = module.build_report(
        output_dir=repo_root / "demo-output" / "adobe-validation-stack",
        adobe_dir=adobe_dir,
        converter=None,
        validator=validator,
        timeout_seconds=1,
        dry_run_converter=False,
        repo_root=repo_root,
        runner=_FakeAdobeValidationStackRunner(no_report_names={"adobe-dng-converter"}),
    )

    assert report["ok"] is False
    converter_step = _stack_step(report, "adobe-dng-converter")
    assert converter_step["exit_code"] == 1
    assert any(
        finding.startswith("child report missing:")
        for finding in converter_step["blocking_findings"]
    )


def test_verify_adobe_validation_stack_rejects_external_project_fixture_paths(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    batch_dir = repo_root / "demo-output" / "adobe-validation-stack" / "project-fixtures"
    manifest_dir = batch_dir / "manifests"
    external_dir = tmp_path / "external-fixtures"
    manifest_dir.mkdir(parents=True)
    external_dir.mkdir()
    external_dng = external_dir / "outside.dng"
    external_dng.write_bytes(b"outside dng")
    manifest = {
        "schema": "image2dng.raw_native_node_batch.v1",
        "scenes": [
            {
                "slug": "sample",
                "outputs": {
                    "linearraw_dng": str(external_dng),
                    "cfa_dng": str(batch_dir / "raw" / "sample-cfa.dng"),
                },
                "validations": {"linearraw": {"ok": True}, "cfa": {"ok": True}},
            }
        ],
    }
    sample_index = {
        "schema": "image2dng.raw_native_sample_index.v1",
        "scene_count": 1,
        "all_validations_ok": True,
    }
    (manifest_dir / "raw-native-node-batch.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (manifest_dir / "sample-index.json").write_text(
        json.dumps(sample_index),
        encoding="utf-8",
    )

    _inspection, errors = module._inspect_project_dng_fixtures(batch_dir, repo_root)

    assert any("project fixture DNG outside batch dir" in error for error in errors)


def test_verify_adobe_validation_stack_refuses_unsafe_output_dirs(tmp_path):
    module = _load_script_module("verify_adobe_validation_stack")
    repo_root = tmp_path / "repo"
    adobe_dir = repo_root / "Adobe"
    adobe_dir.mkdir(parents=True)
    demo_output = repo_root / "demo-output" / "adobe-validation-stack"

    assert module._output_dir_refusal_reason(
        adobe_dir / "report",
        repo_root=repo_root,
    ) == "Refusing to write Adobe Validation Stack report inside Adobe/."
    assert module._output_dir_refusal_reason(
        repo_root / "reports",
        repo_root=repo_root,
    ) == "Refusing to write Adobe Validation Stack report outside demo-output/."
    assert module._output_dir_refusal_reason(
        repo_root / "scripts" / "report",
        repo_root=repo_root,
    ) == "Refusing to write Adobe Validation Stack report outside demo-output/."
    assert module._output_dir_refusal_reason(
        repo_root
        / "demo-output"
        / "review-bundle-phase6"
        / "artifacts"
        / "representative-dng"
        / "report",
        repo_root=repo_root,
    ) == "Refusing to write Adobe Validation Stack report inside a fixture directory."
    assert module._output_dir_refusal_reason(demo_output, repo_root=repo_root) is None


def test_processor_inventory_discovers_darktable_common_install_path(tmp_path, monkeypatch):
    common_executable = tmp_path / "darktable" / "bin" / "darktable-cli.exe"
    common_executable.parent.mkdir(parents=True)
    common_executable.write_text("fake exe", encoding="utf-8")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", lambda _command: None)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=False, return_code=0),
    )
    monkeypatch.setitem(
        PROCESSOR_TOOL_SPECS,
        "darktable-cli",
        ProcessorToolSpec(
            name="darktable-cli",
            version_command=["darktable-cli", "--version"],
            install_hint="fake darktable hint",
            common_install_paths=(common_executable,),
        ),
    )

    inventory = processor_tool_inventory(timeout_seconds=1)

    assert inventory["darktable-cli"]["available"] is True
    assert inventory["darktable-cli"]["executable"] == str(common_executable)
    assert inventory["darktable-cli"]["discovery"] == "common-install-path"
    assert inventory["darktable-cli"]["version"] == "fake-tool 1.0"


def test_processor_inventory_discovers_rawtherapee_common_install_path(
    tmp_path, monkeypatch
):
    common_executable = tmp_path / "RawTherapee" / "5.12" / "rawtherapee-cli.exe"
    common_executable.parent.mkdir(parents=True)
    common_executable.write_text("fake exe", encoding="utf-8")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", lambda _command: None)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=False, return_code=0),
    )
    monkeypatch.setitem(
        PROCESSOR_TOOL_SPECS,
        "rawtherapee-cli",
        ProcessorToolSpec(
            name="rawtherapee-cli",
            version_command=["rawtherapee-cli", "--version"],
            install_hint="fake rawtherapee hint",
            common_install_paths=(common_executable,),
        ),
    )

    inventory = processor_tool_inventory(timeout_seconds=1)

    assert inventory["rawtherapee-cli"]["available"] is True
    assert inventory["rawtherapee-cli"]["executable"] == str(common_executable)
    assert inventory["rawtherapee-cli"]["discovery"] == "common-install-path"
    assert inventory["rawtherapee-cli"]["version"] == "fake-tool 1.0"


def test_real_raw_sample_audit_writes_local_research_reports(tmp_path, monkeypatch):
    module = _load_script_module("audit_real_raw_sample")
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    dng_path = _write_test_dng(sample_dir, prompt_hash="sha256:real-raw-audit")
    output_dir = tmp_path / "real-raw-reports"
    monkeypatch.setattr(module, "resolve_processor_executable", lambda _tool: (None, None))

    exit_code = module.main(
        [
            "--input",
            str(dng_path),
            "--sample-id",
            "synthetic-dng-local",
            "--source-name",
            "unit-test local sample",
            "--license-summary",
            "local research only",
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    assert exit_code == 0
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "metadata-summary.json",
        "redaction-report.json",
        "research-ledger-entry.json",
    ]
    metadata = json.loads((output_dir / "metadata-summary.json").read_text(encoding="utf-8"))
    redaction = json.loads((output_dir / "redaction-report.json").read_text(encoding="utf-8"))
    ledger = json.loads((output_dir / "research-ledger-entry.json").read_text(encoding="utf-8"))

    assert metadata["schema"] == "image2dng.real_raw_sample_research.v1"
    assert metadata["sample_id"] == "synthetic-dng-local"
    assert metadata["raw_format"] == "dng"
    assert metadata["input_sha256"].startswith("sha256:")
    assert metadata["detected_metadata"]["camera_make"] == "image2dng"
    assert metadata["detected_metadata"]["preview_ifd_present"] is True
    assert metadata["optional_tools"]["exiftool"]["result"] == "skipped"
    assert "metadata" not in metadata["optional_tools"]["exiftool"]

    assert redaction["schema"] == "image2dng.real_raw_sample_research.v1"
    assert set(redaction["fields"].values()) <= {"present", "absent", "unknown"}
    assert redaction["fields"]["embedded_preview_present"] == "present"
    assert redaction["policy"]["public_release_allowed_by_this_report"] is False

    assert ledger["schema"] == "image2dng.real_raw_sample_research.v1"
    assert ledger["local_research_only"] is True
    assert ledger["source"]["redistribution_allowed"] is False
    assert ledger["input_sha256"] == metadata["input_sha256"]
    assert "metadata" not in ledger["optional_tools"]["exiftool"]


def test_real_raw_sample_audit_rejects_malformed_tiff_metadata():
    module = _load_script_module("audit_real_raw_sample")

    with pytest.raises(TypeError, match="tifffile metadata tags must be an object"):
        module._detected_metadata(
            {
                "status": "passed",
                "warnings": [],
                "tags": [],
                "preview_ifd_present": True,
                "raw_ifd_present": True,
                "subifd_present": False,
            },
            {
                "result": "skipped",
                "metadata": {},
            },
        )


def test_real_raw_sample_audit_rejects_malformed_tiff_metadata_shape():
    module = _load_script_module("audit_real_raw_sample")

    with pytest.raises(TypeError, match="tifffile metadata warnings must be a string list"):
        module._redaction_report(
            sample_id="bad-warnings",
            input_path=Path("sample.dng"),
            input_sha256="sha256:unit",
            raw_format="dng",
            tiff_metadata={
                "status": "passed",
                "warnings": ["ok", 3],
                "tags": {},
                "preview_ifd_present": False,
                "raw_ifd_present": True,
                "subifd_present": False,
            },
            exiftool={"result": "skipped", "metadata": {}},
        )

    with pytest.raises(
        TypeError,
        match="tifffile metadata preview_ifd_present must be a boolean or null",
    ):
        module._detected_metadata(
            {
                "status": "passed",
                "warnings": [],
                "tags": {},
                "preview_ifd_present": "yes",
                "raw_ifd_present": True,
                "subifd_present": False,
            },
            {"result": "skipped", "metadata": {}},
        )


def test_real_raw_sample_audit_does_not_treat_boolean_tags_as_ifd_markers():
    module = _load_script_module("audit_real_raw_sample")
    detected = module._detected_metadata(
        {
            "status": "passed",
            "warnings": [],
            "tags": {
                "NewSubfileType": False,
                "254": [True],
            },
            "preview_ifd_present": module._preview_ifd_present(
                [{"NewSubfileType": False, "254": [True]}]
            ),
            "raw_ifd_present": module._raw_ifd_present(
                [{"NewSubfileType": False, "254": [True]}]
            ),
            "subifd_present": False,
        },
        {"result": "skipped", "metadata": {}},
    )

    assert detected["preview_ifd_present"] is False
    assert detected["raw_ifd_present"] is False


def test_real_raw_sample_audit_rejects_malformed_tool_summary():
    module = _load_script_module("audit_real_raw_sample")

    with pytest.raises(TypeError, match="tool exit_code must be an integer or null"):
        module._tool_summary(
            {
                "available": True,
                "discovery": None,
                "command": [],
                "exit_code": False,
                "duration_seconds": 0.0,
                "result": "skipped",
                "stdout_tail": [],
                "stderr_tail": [],
                "notes": "tool not found",
            }
        )

    with pytest.raises(TypeError, match="tool duration_seconds must be numeric"):
        module._tool_summary(
            {
                "available": True,
                "discovery": None,
                "command": [],
                "exit_code": None,
                "duration_seconds": True,
                "result": "skipped",
                "stdout_tail": [],
                "stderr_tail": [],
                "notes": "tool not found",
            }
        )


def test_real_raw_sample_audit_rejects_malformed_redaction_status():
    module = _load_script_module("audit_real_raw_sample")

    with pytest.raises(TypeError, match="redaction status must be a string"):
        module._next_actions({"status": []}, redistribution_allowed=True)


def test_real_raw_sample_audit_rejects_missing_input(tmp_path):
    module = _load_script_module("audit_real_raw_sample")

    exit_code = module.main(
        [
            "--input",
            str(tmp_path / "missing.dng"),
            "--sample-id",
            "missing",
            "--output-dir",
            str(tmp_path / "reports"),
            "--allow-output-outside-demo-output",
        ]
    )

    assert exit_code == 2
    assert not (tmp_path / "reports").exists()


def test_real_raw_sample_audit_refuses_tracked_output_without_override(tmp_path):
    module = _load_script_module("audit_real_raw_sample")
    dng_path = _write_test_dng(tmp_path, prompt_hash="sha256:real-raw-refuse")
    output_dir = tmp_path / "reports"

    exit_code = module.main(
        [
            "--input",
            str(dng_path),
            "--sample-id",
            "refuse-outside-demo-output",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()


def test_real_raw_sample_audit_refuses_output_inside_sample_directory(tmp_path):
    module = _load_script_module("audit_real_raw_sample")
    sample_dir = tmp_path / "real-raw-samples"
    sample_dir.mkdir()
    raw_path = sample_dir / "sample.nef"
    raw_path.write_bytes(b"not a real nef")
    output_dir = sample_dir / "reports"

    exit_code = module.main(
        [
            "--input",
            str(raw_path),
            "--sample-id",
            "refuse-sample-dir-output",
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()


def test_real_raw_sample_audit_refuses_output_inside_input_directory(tmp_path):
    module = _load_script_module("audit_real_raw_sample")
    dng_path = _write_test_dng(tmp_path, prompt_hash="sha256:real-raw-overlap")
    output_dir = dng_path.parent / "reports"

    exit_code = module.main(
        [
            "--input",
            str(dng_path),
            "--sample-id",
            "refuse-overlap",
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()


def test_real_raw_sample_audit_marks_proprietary_raw_redaction_unknown(tmp_path, monkeypatch):
    module = _load_script_module("audit_real_raw_sample")
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    raw_path = sample_dir / "sample.nef"
    raw_path.write_bytes(b"not a real nef, enough for scaffold hashing")
    output_dir = tmp_path / "nef-reports"
    monkeypatch.setattr(module, "resolve_processor_executable", lambda _tool: (None, None))

    assert (
        module.main(
            [
                "--input",
                str(raw_path),
                "--sample-id",
                "nef-local",
                "--output-dir",
                str(output_dir),
                "--allow-output-outside-demo-output",
            ]
        )
        == 0
    )

    metadata = json.loads((output_dir / "metadata-summary.json").read_text(encoding="utf-8"))
    redaction = json.loads((output_dir / "redaction-report.json").read_text(encoding="utf-8"))
    assert metadata["raw_format"] == "nef"
    assert metadata["optional_tools"]["exiftool"]["result"] == "skipped"
    assert set(redaction["fields"].values()) == {"unknown"}
    assert redaction["status"] == "needs-manual-review"
    assert any("Proprietary RAW metadata needs ExifTool" in item for item in redaction["warnings"])


def test_semantic_physics_manifest_builder_writes_local_manifest(tmp_path):
    module = _load_script_module("build_semantic_physics_manifest")
    semantic_path = _write_semantic_scene(
        tmp_path,
        width=16,
        height=12,
        include_hash=True,
        include_semantic_physics=True,
    )
    output_dir = tmp_path / "semantic-physics-manifest"

    exit_code = module.main(
        [
            "--semantic-sidecar",
            str(semantic_path),
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    sample = manifest["samples"][0]
    assert exit_code == 0
    assert manifest["schema"] == "image2dng.semantic_physics_dataset_manifest.v1"
    assert manifest["local_research_only"] is True
    assert manifest["all_validations_ok"] is True
    assert sample["semantic_sidecar_bytes"] == semantic_path.stat().st_size
    assert sample["semantic_sidecar_sha256"].startswith("sha256:")
    assert sample["semantic_sidecar_sha256"] == _sha256_test_file(semantic_path)
    assert sample["semantic_physics_fields"] == {
        "capture_physics": True,
        "camera_response": True,
        "region_raw_statistics_count": 1,
    }


def test_semantic_physics_manifest_builder_refuses_tracked_output(tmp_path):
    module = _load_script_module("build_semantic_physics_manifest")
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12, include_hash=True)
    output_dir = tmp_path / "tracked-manifest"

    exit_code = module.main(
        [
            "--semantic-sidecar",
            str(semantic_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()


def test_semantic_physics_manifest_builder_records_invalid_sidecar(tmp_path):
    module = _load_script_module("build_semantic_physics_manifest")
    semantic_path = _write_semantic_scene(tmp_path, width=16, height=12, include_hash=True)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["capture_physics"] = {"source": "guessed"}
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")
    output_dir = tmp_path / "invalid-semantic-physics-manifest"

    exit_code = module.main(
        [
            "--semantic-sidecar",
            str(semantic_path),
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert manifest["all_validations_ok"] is False
    assert manifest["samples"][0]["validation_ok"] is False
    assert any(
        "capture_physics.source must be one of" in error
        for error in manifest["samples"][0]["validation"]["errors"]
    )


def test_semantic_physics_manifest_builder_rejects_malformed_validation_flag():
    module = _load_script_module("build_semantic_physics_manifest")

    with pytest.raises(TypeError, match="sample validation_ok must be a boolean"):
        module._all_validations_ok([{"validation_ok": "yes"}])


def test_generate_fivek_semantic_physics_sample_rejects_malformed_validation_flag():
    module = _load_script_module("generate_fivek_semantic_physics_sample")

    with pytest.raises(TypeError, match="sample validation_ok must be a boolean"):
        module._all_validations_ok([{"validation_ok": "yes"}])


def test_generate_fivek_semantic_physics_sample_writes_passing_manifest(tmp_path):
    module = _load_script_module("generate_fivek_semantic_physics_sample")
    fivek_dir = tmp_path / "fivek-smoke"
    fivek_dir.mkdir()
    sample_id = "sample-001"
    (fivek_dir / f"{sample_id}.dng").write_bytes(b"fake local dng bytes")
    rgb = _gradient_image(18, 12)
    tifffile.imwrite(fivek_dir / f"{sample_id}.tif", rgb, photometric="rgb")
    output_dir = tmp_path / "semantic-physics-dataset"

    exit_code = module.main(
        [
            "--fivek-dir",
            str(fivek_dir),
            "--sample-id",
            sample_id,
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    sidecar = json.loads((output_dir / f"{sample_id}.semantic.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert manifest["schema"] == "image2dng.semantic_physics_dataset_manifest.v1"
    assert manifest["all_validations_ok"] is True
    assert manifest["samples"][0]["semantic_sidecar_bytes"] == (
        output_dir / f"{sample_id}.semantic.json"
    ).stat().st_size
    assert manifest["samples"][0]["semantic_sidecar_sha256"] == _sha256_test_file(
        output_dir / f"{sample_id}.semantic.json"
    )
    assert manifest["samples"][0]["semantic_physics_fields"] == {
        "capture_physics": True,
        "camera_response": True,
        "region_raw_statistics_count": 1,
    }
    assert (output_dir / "assets" / f"{sample_id}-neutral-preview.jpg").exists()
    assert (output_dir / "assets" / f"{sample_id}-center-mask.png").exists()
    assert sidecar["producer"]["source_dng_bytes"] == (
        fivek_dir / f"{sample_id}.dng"
    ).stat().st_size
    assert sidecar["producer"]["source_dng_sha256"].startswith("sha256:")
    assert sidecar["producer"]["source_dng_sha256"] == _sha256_test_file(
        fivek_dir / f"{sample_id}.dng"
    )
    assert sidecar["producer"]["source_tiff_bytes"] == (
        fivek_dir / f"{sample_id}.tif"
    ).stat().st_size
    assert sidecar["producer"]["source_tiff_sha256"] == _sha256_test_file(
        fivek_dir / f"{sample_id}.tif"
    )
    assert sidecar["regions"][0]["raw_statistics"]["clipped_pixel_ratio"] >= 0


def test_generate_fivek_semantic_physics_sample_rejects_path_like_sample_id(tmp_path):
    module = _load_script_module("generate_fivek_semantic_physics_sample")
    fivek_dir = tmp_path / "fivek-smoke"
    fivek_dir.mkdir()
    output_dir = tmp_path / "semantic-physics-dataset"

    exit_code = module.main(
        [
            "--fivek-dir",
            str(fivek_dir),
            "--sample-id",
            "..\\outside",
            "--output-dir",
            str(output_dir),
            "--allow-output-outside-demo-output",
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()
    assert not (tmp_path / "outside.semantic.json").exists()


def test_generate_fivek_semantic_physics_sample_refuses_tracked_output(tmp_path):
    module = _load_script_module("generate_fivek_semantic_physics_sample")
    fivek_dir = tmp_path / "fivek-smoke"
    fivek_dir.mkdir()
    sample_id = "sample-001"
    (fivek_dir / f"{sample_id}.dng").write_bytes(b"fake local dng bytes")
    tifffile.imwrite(fivek_dir / f"{sample_id}.tif", _gradient_image(18, 12))
    output_dir = tmp_path / "tracked-semantic-physics"

    exit_code = module.main(
        [
            "--fivek-dir",
            str(fivek_dir),
            "--sample-id",
            sample_id,
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 2
    assert not output_dir.exists()


def test_generate_visual_demo_rejects_malformed_validation_flag():
    module = _load_script_module("generate_visual_demo")

    with pytest.raises(TypeError, match="validation ok must be a boolean"):
        module._validation_ok({"ok": "yes"})

    with pytest.raises(TypeError, match="sample validation_ok must be a boolean"):
        module._all_validations_ok([{"validation_ok": "yes"}])


def test_processor_compatibility_uses_darktable_common_install_path(tmp_path, monkeypatch):
    dng_path = _write_test_dng(tmp_path, prompt_hash="sha256:darktable-common-path")
    common_executable = tmp_path / "darktable" / "bin" / "darktable-cli.exe"
    common_executable.parent.mkdir(parents=True)
    common_executable.write_text("fake exe", encoding="utf-8")
    monkeypatch.setattr("image2dng.compatibility.shutil.which", lambda _command: None)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=True, return_code=0),
    )
    monkeypatch.setitem(
        PROCESSOR_TOOL_SPECS,
        "darktable-cli",
        ProcessorToolSpec(
            name="darktable-cli",
            version_command=["darktable-cli", "--version"],
            install_hint="fake darktable hint",
            common_install_paths=(common_executable,),
        ),
    )

    results = run_processor_compatibility(dng_path, tmp_path / "processors")
    payload = {result.tool: result.to_dict() for result in results}

    assert payload["darktable-cli"]["available"] is True
    assert payload["darktable-cli"]["command"][0] == str(common_executable)
    assert payload["darktable-cli"]["result"] == "passed"
    assert payload["darktable-cli"]["output_artifacts"]


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
    assert payload["dcraw"]["missing_output_artifacts"]
    assert payload["dcraw"]["output_artifacts"] == []


def test_demo_review_bundle_generates_portable_index(tmp_path, monkeypatch):
    module = _load_script_module("generate_demo_review_bundle")
    monkeypatch.setattr("shutil.which", lambda _command: None)
    monkeypatch.setattr(
        "image2dng.validate.resolve_processor_executable",
        lambda _command: (None, None),
    )
    monkeypatch.setattr(
        "image2dng.compatibility.resolve_processor_executable",
        lambda _command: (None, None),
    )

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

    baseline_path = output_dir / report["source_reports"]["development_baseline_report"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert [step["name"] for step in baseline["steps"]] == [
        "pytest",
        "ruff",
        "build",
        "raw-native-batch",
        "wheel-install-smoke",
    ]

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
    assert f"--output-dir '{output_dir}'" in index


def test_demo_review_bundle_records_callable_exception():
    module = _load_script_module("generate_demo_review_bundle")
    report = {"commands": [], "errors": []}

    def fail():
        raise RuntimeError("synthetic callable failure")

    module._record_callable_command(
        report,
        name="synthetic-step",
        command=["synthetic", "command"],
        function=fail,
    )

    assert len(report["commands"]) == 1
    command = report["commands"][0]
    assert command["name"] == "synthetic-step"
    assert command["command"] == ["synthetic", "command"]
    assert command["exit_code"] == 1
    assert isinstance(command["duration_seconds"], float)
    assert command["status"] == "failed"
    assert command["error"] == "synthetic callable failure"
    assert report["errors"] == ["synthetic-step failed: synthetic callable failure"]


def test_demo_review_bundle_collects_source_reports_after_command_failure(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"
    work_dir = output_dir / "_work"
    paths = module.BundlePaths(
        output_dir=output_dir,
        work_dir=work_dir,
        visual_dir=work_dir / "visual-demo",
        raw_native_dir=work_dir / "raw-native-node-batch",
        baseline_dir=work_dir / "development-baseline",
        compatibility_dir=work_dir / "compatibility-evidence",
    )
    report = {
        "artifacts": [],
        "source_reports": {},
    }
    source_files = {
        paths.visual_dir / "manifest.json": {"schema": "visual"},
        paths.raw_native_dir / "manifests" / "raw-native-node-batch.json": {"schema": "raw"},
        paths.raw_native_dir / "manifests" / "sample-index.json": {"schema": "sample"},
        paths.baseline_dir / "verification-report.json": {"schema": "baseline"},
        paths.compatibility_dir / "compatibility-report.json": {"schema": "compatibility"},
    }
    for path, payload in source_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    (paths.compatibility_dir / "compatibility-summary.md").write_text(
        "# Compatibility Summary\n\n## Fixture Integrity\n",
        encoding="utf-8",
    )

    module._collect_available_source_reports(paths=paths, report=report)

    assert report["source_reports"] == {
        "visual_manifest": "artifacts/manifests/visual-demo-manifest.json",
        "raw_native_manifest": "artifacts/manifests/raw-native-node-batch.json",
        "raw_native_sample_index": "artifacts/manifests/raw-native-sample-index.json",
        "development_baseline_report": "artifacts/reports/development-baseline-report.json",
        "compatibility_report": "artifacts/reports/compatibility-report.json",
        "compatibility_summary": "artifacts/reports/compatibility-summary.md",
    }
    assert all((output_dir / path).exists() for path in report["source_reports"].values())
    assert all(artifact["sha256"] for artifact in report["artifacts"])
    module._validate_bundle_report(output_dir, report)


def test_demo_review_bundle_main_writes_partial_bundle_after_command_failure(
    tmp_path, monkeypatch
):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"

    def fail_after_writing_source_reports(**kwargs):
        paths = kwargs["paths"]
        report = kwargs["report"]
        source_files = {
            paths.visual_dir / "manifest.json": {"schema": "visual"},
            paths.raw_native_dir
            / "manifests"
            / "raw-native-node-batch.json": {"schema": "raw"},
            paths.raw_native_dir / "manifests" / "sample-index.json": {"schema": "sample"},
            paths.baseline_dir / "verification-report.json": {"schema": "baseline"},
            paths.compatibility_dir / "compatibility-report.json": {"schema": "compatibility"},
        }
        for path, payload in source_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        (paths.compatibility_dir / "compatibility-summary.md").write_text(
            "# Compatibility Summary\n\n## Fixture Integrity\n",
            encoding="utf-8",
        )
        report["commands"].append(
            {
                "name": "compatibility-evidence",
                "command": ["synthetic", "compatibility-evidence"],
                "exit_code": 7,
                "duration_seconds": 0.0,
                "status": "failed",
            }
        )
        report["errors"].append("compatibility-evidence failed with exit code 7")

    monkeypatch.setattr(module, "_run_upstream_generators", fail_after_writing_source_reports)

    assert module.main(["--output-dir", str(output_dir)]) == 1

    report_path = output_dir / "review-bundle-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    source_reports = report["source_reports"]
    artifact_paths = {artifact["bundle_path"] for artifact in report["artifacts"]}

    assert report["ok"] is False
    assert report["errors"] == ["compatibility-evidence failed with exit code 7"]
    assert source_reports["compatibility_summary"] == (
        "artifacts/reports/compatibility-summary.md"
    )
    assert all(path in artifact_paths for path in source_reports.values())
    assert all((output_dir / path).exists() for path in source_reports.values())
    assert "index.md" in artifact_paths
    assert "## Errors" in (output_dir / "index.md").read_text(encoding="utf-8")
    module._validate_bundle_report(output_dir, report)


def test_demo_review_bundle_rejects_malformed_command_status():
    module = _load_script_module("generate_demo_review_bundle")

    with pytest.raises(TypeError, match="command status must be a string"):
        module._has_command_failure({"commands": [{"status": False}]})


def test_demo_review_bundle_rejects_malformed_source_report_schema(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    report_path = tmp_path / "report.json"

    report_path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON report must be an object"):
        module._read_json(report_path, "example.schema.v1")

    report_path.write_text(json.dumps({"schema": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON report schema must be a string"):
        module._read_json_any(report_path, {"example.schema.v1"})

    report_path.write_text(json.dumps({"schema": "example.other.v1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected schema"):
        module._read_json(report_path, "example.schema.v1")


def test_demo_review_bundle_rejects_malformed_visual_manifest_lists():
    module = _load_script_module("generate_demo_review_bundle")

    with pytest.raises(ValueError, match="visual manifest contact_sheets must be an object list"):
        module._collect_contact_sheets(
            paths=None,
            report={},
            visual_manifest={"contact_sheets": ["not-a-sheet"]},
        )

    with pytest.raises(ValueError, match="visual manifest assets must be an object list"):
        module._collect_visual_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            visual_manifest={"assets": ["not-an-asset"]},
        )


def test_demo_review_bundle_rejects_malformed_visual_chart_fields():
    module = _load_script_module("generate_demo_review_bundle")

    with pytest.raises(
        ValueError,
        match="duplicate visual manifest asset slug: chart-gradient",
    ):
        module._collect_visual_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            visual_manifest={
                "assets": [
                    {"slug": "chart-gradient"},
                    {"slug": "chart-gradient"},
                ]
            },
        )

    with pytest.raises(
        ValueError,
        match="visual chart-gradient asset outputs must be an object",
    ):
        module._collect_visual_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            visual_manifest={"assets": [{"slug": "chart-gradient", "outputs": []}]},
        )

    with pytest.raises(
        ValueError,
        match="visual chart-gradient asset validations must be an object",
    ):
        module._collect_visual_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            visual_manifest={
                "assets": [
                    {
                        "slug": "chart-gradient",
                        "outputs": {
                            "phase_15_linearraw": "a.dng",
                            "phase_2_cfa": "b.dng",
                            "phase_3_linearraw_noisy": "c.dng",
                            "phase_3_cfa_noisy": "d.dng",
                        },
                        "validations": [],
                    }
                ]
            },
        )


def test_demo_review_bundle_rejects_malformed_compatibility_fixture_rows():
    module = _load_script_module("generate_demo_review_bundle")

    with pytest.raises(
        ValueError,
        match="compatibility report fixtures must be an object list",
    ):
        module._collect_compatibility_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            compatibility_report={"fixtures": ["not-a-fixture"]},
        )

    with pytest.raises(
        ValueError,
        match="duplicate compatibility fixture slug: srgb-gradient-linearraw",
    ):
        module._collect_compatibility_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            compatibility_report={
                "fixtures": [
                    {"slug": "srgb-gradient-linearraw"},
                    {"slug": "srgb-gradient-linearraw"},
                ]
            },
        )


def test_demo_review_bundle_rejects_malformed_raw_native_scene_rows():
    module = _load_script_module("generate_demo_review_bundle")

    with pytest.raises(
        ValueError,
        match="raw-native manifest scenes must be an object list",
    ):
        module._collect_raw_native_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            raw_manifest={"scenes": ["not-a-scene"]},
        )

    with pytest.raises(
        ValueError,
        match="sample: raw-native scene outputs must be an object",
    ):
        module._collect_raw_native_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            raw_manifest={"scenes": [{"slug": "sample", "outputs": []}]},
        )

    with pytest.raises(
        ValueError,
        match="duplicate raw-native scene slug: sample",
    ):
        module._collect_raw_native_representatives(
            paths=None,
            repo_root=Path.cwd(),
            report={},
            raw_manifest={
                "scenes": [
                    {"slug": "sample", "outputs": {}},
                    {"slug": "sample", "outputs": {}},
                ]
            },
        )


def test_demo_review_bundle_rejects_malformed_report_accessors():
    module = _load_script_module("generate_demo_review_bundle")
    report = {
        "schema": "image2dng.demo_review_bundle.v1",
        "generated_at": "2026-05-23T00:00:00Z",
        "ok": True,
        "output_dir": "demo-output/review-bundle",
        "commands": [
            {
                "name": "baseline",
                "status": "passed",
                "exit_code": 0,
                "duration_seconds": 0.1,
            }
        ],
        "artifacts": [
            {
                "kind": "contact-sheet",
                "name": "sheet",
                "bundle_path": "artifacts/contact-sheets/sheet.png",
            }
        ],
        "source_reports": {},
        "errors": [],
    }

    with pytest.raises(TypeError, match="report schema must be a string"):
        module._write_index(Path.cwd(), report | {"schema": 1})

    with pytest.raises(TypeError, match="report generated_at must be a string"):
        module._write_index(Path.cwd(), report | {"generated_at": []})

    with pytest.raises(TypeError, match="report ok must be a boolean"):
        module._write_index(Path.cwd(), report | {"ok": "true"})

    with pytest.raises(TypeError, match="report output_dir must be a string"):
        module._write_index(Path.cwd(), report | {"output_dir": False})

    malformed = report | {"commands": [report["commands"][0] | {"name": []}]}
    with pytest.raises(TypeError, match="command name must be a string"):
        module._write_index(Path.cwd(), malformed)

    malformed = report | {"commands": [report["commands"][0] | {"exit_code": True}]}
    with pytest.raises(TypeError, match="command exit_code must be an integer or null"):
        module._write_index(Path.cwd(), malformed)

    malformed = report | {
        "commands": [report["commands"][0] | {"duration_seconds": "slow"}]
    }
    with pytest.raises(TypeError, match="command duration_seconds must be numeric"):
        module._write_index(Path.cwd(), malformed)

    malformed = report | {"artifacts": [report["artifacts"][0] | {"kind": False}]}
    with pytest.raises(TypeError, match="artifact kind must be a string"):
        module._write_index(Path.cwd(), malformed)

    malformed = report | {"artifacts": [report["artifacts"][0] | {"name": []}]}
    with pytest.raises(TypeError, match="artifact name must be a string"):
        module._write_index(Path.cwd(), malformed)

    malformed = report | {"artifacts": [report["artifacts"][0] | {"bundle_path": 7}]}
    with pytest.raises(TypeError, match="artifact bundle_path must be a string"):
        module._write_index(Path.cwd(), malformed)

    with pytest.raises(TypeError, match="report commands must contain objects"):
        module._commands({"commands": ["not-a-command"]})

    with pytest.raises(TypeError, match="report artifacts must contain objects"):
        module._artifacts({"artifacts": ["not-an-artifact"]})

    with pytest.raises(TypeError, match="artifact bundle_path must be a string"):
        module._append_existing_artifact(
            output_dir=Path.cwd(),
            report={
                "artifacts": [
                    {
                        "kind": "index",
                        "name": "index",
                        "bundle_path": False,
                    }
                ]
            },
            source=Path.cwd() / "index.md",
            kind="index",
            name="index",
        )

    with pytest.raises(TypeError, match="report source_reports must be an object"):
        module._source_reports({"source_reports": []})

    malformed = report | {"source_reports": {False: "artifacts/reports/report.txt"}}
    with pytest.raises(TypeError, match="source report name must be a string"):
        module._write_index(Path.cwd(), malformed)

    malformed = report | {"source_reports": {"development_baseline_report": []}}
    with pytest.raises(TypeError, match="source report path must be a string"):
        module._write_index(Path.cwd(), malformed)

    with pytest.raises(TypeError, match="report errors must be a list"):
        module._errors({"errors": "none"})

    with pytest.raises(TypeError, match="report errors must be a string list"):
        module._write_index(Path.cwd(), report | {"errors": [False]})


def test_demo_review_bundle_rejects_stale_artifact_integrity(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"
    artifact_path = output_dir / "artifacts" / "reports" / "report.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("current report", encoding="utf-8")
    report = {
        "artifacts": [
            {
                "bundle_path": "artifacts/reports/report.txt",
                "path": "artifacts/reports/report.txt",
                "bytes": artifact_path.stat().st_size,
                "sha256": module._sha256(artifact_path),
            }
        ],
        "source_reports": {},
    }

    module._validate_bundle_report(output_dir, report)
    report["artifacts"][0]["bytes"] += 1
    with pytest.raises(ValueError, match="artifact byte count mismatch"):
        module._validate_bundle_report(output_dir, report)

    report["artifacts"][0]["bytes"] = artifact_path.stat().st_size
    report["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact sha256 mismatch"):
        module._validate_bundle_report(output_dir, report)


def test_demo_review_bundle_rejects_artifact_path_alias(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"
    artifact_path = output_dir / "artifacts" / "reports" / "report.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("current report", encoding="utf-8")
    report = {
        "artifacts": [
            {
                "bundle_path": "artifacts/reports/report.txt",
                "path": "artifacts/reports/alias.txt",
                "bytes": artifact_path.stat().st_size,
                "sha256": module._sha256(artifact_path),
            }
        ],
        "source_reports": {},
    }

    with pytest.raises(ValueError, match="artifact path must match bundle_path"):
        module._validate_bundle_report(output_dir, report)


def test_demo_review_bundle_rejects_malformed_artifact_integrity_metadata(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"
    artifact_path = output_dir / "artifacts" / "reports" / "report.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("x", encoding="utf-8")
    report = {
        "artifacts": [
            {
                "bundle_path": "artifacts/reports/report.txt",
                "path": "artifacts/reports/report.txt",
                "bytes": artifact_path.stat().st_size,
                "sha256": module._sha256(artifact_path),
            }
        ],
        "source_reports": {},
    }

    malformed = report | {"artifacts": [report["artifacts"][0] | {"path": []}]}
    with pytest.raises(TypeError, match="artifact path must be a string"):
        module._validate_bundle_report(output_dir, malformed)

    malformed = report | {"artifacts": [report["artifacts"][0] | {"bytes": True}]}
    with pytest.raises(TypeError, match="artifact bytes must be an integer"):
        module._validate_bundle_report(output_dir, malformed)

    malformed = report | {"artifacts": [report["artifacts"][0] | {"sha256": []}]}
    with pytest.raises(TypeError, match="artifact sha256 must be a string"):
        module._validate_bundle_report(output_dir, malformed)


def test_demo_review_bundle_rejects_malformed_source_report_paths(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"
    artifact_path = output_dir / "artifacts" / "reports" / "report.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("x", encoding="utf-8")
    report = {
        "artifacts": [
            {
                "bundle_path": "artifacts/reports/report.txt",
                "path": "artifacts/reports/report.txt",
                "bytes": artifact_path.stat().st_size,
                "sha256": module._sha256(artifact_path),
            }
        ],
        "source_reports": {
            "development_baseline_report": "artifacts/reports/report.txt",
        },
    }

    malformed = report | {"source_reports": {False: "artifacts/reports/report.txt"}}
    with pytest.raises(TypeError, match="source report name must be a string"):
        module._validate_bundle_report(output_dir, malformed)

    malformed = report | {"source_reports": {"development_baseline_report": []}}
    with pytest.raises(TypeError, match="source report path must be a string"):
        module._validate_bundle_report(output_dir, malformed)


def test_demo_review_bundle_rejects_unsafe_manifest_paths(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"

    for value in ("../outside.txt", "/tmp/outside.txt", "C:\\tmp\\outside.txt", "C:outside.txt"):
        with pytest.raises(ValueError, match="bundle path must be relative and local"):
            module._validate_relative_existing_path(output_dir, value)


def test_demo_review_bundle_requires_source_reports_to_be_artifacts(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    output_dir = tmp_path / "review-bundle"
    artifact_path = output_dir / "artifacts" / "reports" / "report.txt"
    extra_path = output_dir / "artifacts" / "reports" / "extra.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("registered report", encoding="utf-8")
    extra_path.write_text("unregistered report", encoding="utf-8")
    report = {
        "artifacts": [
            {
                "bundle_path": "artifacts/reports/report.txt",
                "path": "artifacts/reports/report.txt",
                "bytes": artifact_path.stat().st_size,
                "sha256": module._sha256(artifact_path),
            }
        ],
        "source_reports": {
            "development_baseline_report": "artifacts/reports/report.txt",
            "compatibility_report": "artifacts/reports/extra.txt",
        },
    }

    with pytest.raises(ValueError, match="source report is not a registered artifact"):
        module._validate_bundle_report(output_dir, report)

    report["source_reports"]["compatibility_report"] = "artifacts/reports/report.txt"
    module._validate_bundle_report(output_dir, report)


def test_demo_review_bundle_rejects_unsafe_reported_paths(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")

    assert module._join_reported_path(tmp_path, "contact-sheets\\phase-overview.png") == (
        tmp_path / "contact-sheets" / "phase-overview.png"
    )
    for value in ("../outside.png", "/tmp/outside.png", "C:\\tmp\\outside.png", "C:outside.png"):
        with pytest.raises(ValueError, match="unsafe relative path in report"):
            module._join_reported_path(tmp_path, value)


def test_demo_review_bundle_restricts_absolute_source_paths(tmp_path):
    module = _load_script_module("generate_demo_review_bundle")
    default_root = tmp_path / "work" / "visual-demo"
    repo_root = tmp_path / "repo"
    allowed_work_source = default_root / "phase-2-cfa" / "sample.dng"
    allowed_repo_source = repo_root / "docs" / "compatibility.md"
    outside_source = tmp_path / "outside" / "sample.dng"

    assert module._resolve_source_path(str(allowed_work_source), default_root, repo_root) == (
        allowed_work_source
    )
    assert module._resolve_source_path(str(allowed_repo_source), default_root, repo_root) == (
        allowed_repo_source
    )
    with pytest.raises(ValueError, match="unsafe source path in report"):
        module._resolve_source_path(str(outside_source), default_root, repo_root)


def test_demo_review_bundle_skip_baseline_help_is_gate_oriented(capsys):
    module = _load_script_module("generate_demo_review_bundle")

    with pytest.raises(SystemExit) as exc_info:
        module.main(["--help"])

    assert exc_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "without recursively running quality gates" in help_text
    assert "without recursively running pytest/ruff" not in help_text


def test_raw_processor_setup_audit_writes_dry_run_package(tmp_path, monkeypatch):
    module = _load_script_module("audit_raw_processor_setup")

    def which(command):
        if command in {"winget", "scoop", "choco"}:
            return f"C:/fake/{command}.exe"
        if command == "exiftool":
            return "C:/fake/exiftool.exe"
        return None

    monkeypatch.setattr(module.shutil, "which", which)
    monkeypatch.setattr(
        "image2dng.compatibility.subprocess.run",
        _fake_processor_run(create_outputs=False, return_code=0),
    )
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
    assert "Discovery" in runbook
    assert "- Auto install: `False`" in runbook
    assert "Do not install tools until the user approves." in runbook
    assert "uv run python scripts/generate_compatibility_evidence.py" in runbook
    assert "Auto install is `False`" in prompt
    assert "Missing tools are allowed to remain `skipped`." in prompt
    assert (
        "Available tools that fail should remain hard failures in the compatibility report."
        in prompt
    )
    assert "Binary demo outputs stay local-only under `demo-output/`." in prompt
    assert any(
        question
        == (
            "Should Adobe DNG SDK remain manual-only in the generic compatibility "
            "matrix while dedicated local SDK scripts produce sidecar evidence?"
        )
        for question in report["review_questions"]
    )
    assert "without a reproducible local SDK validation path" not in json.dumps(report)
    assert "without a reproducible local SDK validation path" not in prompt


def test_raw_processor_setup_audit_rejects_malformed_runbook_tool_state(tmp_path):
    module = _load_script_module("audit_raw_processor_setup")
    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["tools"]["darktable-cli"]["current"]["available"] = "yes"

    with pytest.raises(TypeError, match="tool current available must be a boolean"):
        module._runbook_markdown(report)


def test_raw_processor_setup_audit_rejects_malformed_report_accessors(tmp_path):
    module = _load_script_module("audit_raw_processor_setup")

    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["schema"] = 1
    with pytest.raises(TypeError, match="report schema must be a string"):
        module._runbook_markdown(report)

    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["generated_at"] = []
    with pytest.raises(TypeError, match="report generated_at must be a string"):
        module._runbook_markdown(report)

    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["tools"] = []
    with pytest.raises(TypeError, match="report tools must be an object"):
        module._runbook_markdown(report)

    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["policy"] = []
    with pytest.raises(TypeError, match="report policy must be an object"):
        module._runbook_markdown(report)

    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["policy"]["auto_install"] = "false"
    with pytest.raises(TypeError, match="report policy auto_install must be a boolean"):
        module._external_review_prompt(report)

    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["rerun_commands"] = ["uv run", 7]
    with pytest.raises(TypeError, match="report rerun_commands must be a string list"):
        module._runbook_markdown(report)


def test_raw_processor_setup_audit_rejects_malformed_package_search(tmp_path):
    module = _load_script_module("audit_raw_processor_setup")
    report = module._build_report(
        output_dir=tmp_path,
        timeout_seconds=1,
        skip_package_search=True,
    )
    report["tools"]["darktable-cli"]["package_searches"][0]["version_hint"] = []

    with pytest.raises(TypeError, match="package search version_hint must be a string or null"):
        module._runbook_markdown(report)


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


def test_development_baseline_wheel_smoke_uses_built_wheel(tmp_path, monkeypatch):
    module = _load_script_module("verify_development_baseline")
    repo_root = tmp_path / "repo"
    dist_dir = repo_root / "dist"
    dist_dir.mkdir(parents=True)
    wheel = dist_dir / "image2dng-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"fake wheel")
    output_dir = tmp_path / "baseline-output"
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")

    step = module._run_wheel_smoke_step(output_dir=output_dir, repo_root=repo_root)

    assert step["status"] == "passed"
    assert [command[:2] for command in calls] == [
        ["uv", "venv"],
        ["uv", "pip"],
        [str(output_dir / "wheel-smoke-venv" / "Scripts" / "image2dng.exe"), "--help"],
        [str(output_dir / "wheel-smoke-venv" / "Scripts" / "image2dng.exe"), "validate"],
    ]
    assert str(wheel) in calls[1]


def test_development_baseline_wheel_smoke_reports_missing_wheel(tmp_path):
    module = _load_script_module("verify_development_baseline")
    repo_root = tmp_path / "repo"
    (repo_root / "dist").mkdir(parents=True)

    step = module._run_wheel_smoke_step(
        output_dir=tmp_path / "baseline-output",
        repo_root=repo_root,
    )

    assert step["name"] == "wheel-install-smoke"
    assert step["status"] == "failed"
    assert step["exit_code"] == 1
    assert step["stderr_tail"] == ["no built image2dng wheel found under dist/"]


def test_development_baseline_validation_artifact_reads_json(tmp_path):
    module = _load_script_module("verify_development_baseline")
    validation_path = tmp_path / "sample-validation.json"
    validation_path.write_text(
        json.dumps({"ok": True, "errors": [], "checks": []}),
        encoding="utf-8",
    )

    record = module._validation_artifact_record("linearraw_validation", validation_path)

    assert record["validation_ok"] is True
    assert record["validation_error_count"] == 0
    assert record["sha256"] == module._sha256(validation_path)


def test_development_baseline_validation_artifact_rejects_failed_json(tmp_path):
    module = _load_script_module("verify_development_baseline")
    validation_path = tmp_path / "sample-validation.json"
    validation_path.write_text(
        json.dumps({"ok": False, "errors": ["synthetic failure"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="validation JSON is not ok"):
        module._validation_artifact_record("linearraw_validation", validation_path)


def test_development_baseline_validation_artifact_rejects_malformed_metadata(tmp_path):
    module = _load_script_module("verify_development_baseline")
    validation_path = tmp_path / "sample-validation.json"

    validation_path.write_text(json.dumps({"ok": "yes", "errors": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="validation JSON ok must be a boolean"):
        module._validation_artifact_record("linearraw_validation", validation_path)

    validation_path.write_text(json.dumps({"ok": True, "errors": "none"}), encoding="utf-8")
    with pytest.raises(ValueError, match="validation JSON errors must be a list"):
        module._validation_artifact_record("linearraw_validation", validation_path)


def test_development_baseline_rejects_malformed_report_accessors():
    module = _load_script_module("verify_development_baseline")

    with pytest.raises(TypeError, match="report steps must contain objects"):
        module._steps({"steps": ["not-a-step"]})

    with pytest.raises(TypeError, match="report errors must be a list"):
        module._errors({"errors": "none"})

    with pytest.raises(TypeError, match="report errors must be a string list"):
        module._error_messages({"errors": ["ok", 7]})

    assert module._error_messages({"errors": ["synthetic failure"]}) == [
        "synthetic failure"
    ]


def test_development_baseline_rejects_malformed_step_records():
    module = _load_script_module("verify_development_baseline")

    with pytest.raises(TypeError, match="step status must be passed or failed"):
        module._append_step(
            {"steps": [], "errors": []},
            {"name": "pytest", "status": "skipped", "exit_code": 0},
        )

    with pytest.raises(TypeError, match="step name must be a non-empty string"):
        module._append_step(
            {"steps": [], "errors": []},
            {"name": "", "status": "failed", "exit_code": 1},
        )

    with pytest.raises(TypeError, match="step exit_code must be an integer"):
        module._append_step(
            {"steps": [], "errors": []},
            {"name": "pytest", "status": "failed", "exit_code": False},
        )


def test_development_baseline_rejects_external_batch_artifact_paths(tmp_path):
    module = _load_script_module("verify_development_baseline")
    repo_root = tmp_path / "repo"
    batch_dir = repo_root / "demo-output" / "development-baseline" / "raw-native-node-batch"
    raw_dir = batch_dir / "raw"
    external_dir = tmp_path / "external-artifacts"
    raw_dir.mkdir(parents=True)
    external_dir.mkdir()
    external_dng = external_dir / "outside-linearraw.dng"
    external_dng.write_bytes(b"outside dng")
    scene = {
        "slug": "sample",
        "prompt_hash": "sha256:sample",
        "nodes": [{"id": "source"}],
        "outputs": {
            "scene_linear_tiff": str(raw_dir / "sample.tif"),
            "linearraw_dng": str(external_dng),
            "cfa_dng": str(raw_dir / "sample-cfa.dng"),
            "linearraw_jpeg": str(raw_dir / "sample-linearraw.jpg"),
            "cfa_jpeg": str(raw_dir / "sample-cfa.jpg"),
        },
        "validations": {"linearraw": {"ok": True}, "cfa": {"ok": True}},
        "raw_data_unique_ids": {"linearraw": "linear-id", "cfa": "cfa-id"},
    }

    with pytest.raises(ValueError, match="batch artifact path is outside batch dir"):
        module._inspect_scene(scene, repo_root, batch_dir)


def test_development_baseline_rejects_malformed_scene_manifest_records(tmp_path):
    module = _load_script_module("verify_development_baseline")
    repo_root = tmp_path / "repo"
    batch_dir = repo_root / "demo-output" / "development-baseline" / "raw-native-node-batch"
    raw_dir = batch_dir / "raw"
    raw_dir.mkdir(parents=True)
    scene = {
        "slug": "sample",
        "prompt_hash": "sha256:sample",
        "nodes": [{"id": "source"}],
        "outputs": {
            "scene_linear_tiff": str(raw_dir / "sample.tif"),
            "linearraw_dng": str(raw_dir / "sample.dng"),
            "cfa_dng": str(raw_dir / "sample-cfa.dng"),
            "linearraw_jpeg": str(raw_dir / "sample-linearraw.jpg"),
            "cfa_jpeg": str(raw_dir / "sample-cfa.jpg"),
        },
        "validations": {"linearraw": {"ok": True}, "cfa": {"ok": True}},
        "raw_data_unique_ids": {"linearraw": "linear-id", "cfa": "cfa-id"},
    }

    malformed = scene | {"outputs": scene["outputs"] | {"linearraw_dng": []}}
    with pytest.raises(ValueError, match="sample: output linearraw_dng must be a non-empty string"):
        module._inspect_scene(malformed, repo_root, batch_dir)

    malformed = scene | {"validations": scene["validations"] | {"linearraw": []}}
    with pytest.raises(
        ValueError,
        match="sample: validation summary for linearraw must be an object",
    ):
        module._inspect_scene(malformed, repo_root, batch_dir)

    malformed = scene | {"validations": scene["validations"] | {"linearraw": {"ok": "yes"}}}
    with pytest.raises(
        ValueError,
        match="sample: validation summary for linearraw ok must be a boolean",
    ):
        module._inspect_scene(malformed, repo_root, batch_dir)

    malformed = scene | {
        "raw_data_unique_ids": scene["raw_data_unique_ids"] | {"linearraw": []}
    }
    with pytest.raises(
        ValueError,
        match="sample: raw data unique id for linearraw must be a string or null",
    ):
        module._inspect_scene(malformed, repo_root, batch_dir)


def test_development_baseline_rejects_malformed_sample_index_validation_flag():
    module = _load_script_module("verify_development_baseline")

    with pytest.raises(ValueError, match="sample index all_validations_ok must be a boolean"):
        module._sample_index_all_validations_ok({"all_validations_ok": "yes"})


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
        xmp = _raw_page(tif).tags[TAG_XMP].value.decode("utf-8")
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
        xmp = _raw_page(tif).tags[TAG_XMP].value.decode("utf-8")
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
        xmp = _raw_page(tif).tags[TAG_XMP].value.decode("utf-8")
    assert 'xmpAI:provenanceType="synthetic"' in xmp


def test_plaintext_prompt_is_not_written_by_default(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:privacy")
    with tifffile.TiffFile(output_path) as tif:
        xmp = _raw_page(tif).tags[TAG_XMP].value.decode("utf-8")

    assert "xmpAI:promptHash" in xmp
    assert "xmpAI:promptPlaintext" not in xmp


def test_makernote_is_not_written(tmp_path):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:no-makernote")
    with tifffile.TiffFile(output_path) as tif:
        assert TAG_MAKER_NOTE not in tif.pages[0].tags
        assert TAG_MAKER_NOTE not in _raw_page(tif).tags


def test_validate_json_output(tmp_path, capsys):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:json")

    exit_code = main(["validate", str(output_path), "--no-smoke", "--json"])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["path"] == str(output_path)
    assert report["dng_layout"] == "preview-subifd"
    assert report["ifd0_preview"] is True
    assert report["raw_ifd_location"] == "IFD0/SubIFD0"
    assert report["embedded_preview_compression"] == "JPEG"
    assert report["raw_photometric"] == "LinearRaw"
    assert report["checks"] == [
        {
            "message": "IFD0 JPEG preview references the main raw SubIFD",
            "name": "embedded-preview",
            "status": "passed",
        },
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
    monkeypatch.setattr(
        "image2dng.validate.resolve_processor_executable",
        lambda _command: (None, None),
    )

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


def test_failed_smoke_tools_are_recorded_in_smoke_summary(tmp_path, monkeypatch):
    output_path = _write_test_dng(tmp_path, prompt_hash="sha256:smoke-failed")
    monkeypatch.setattr(
        "image2dng.validate.resolve_processor_executable",
        lambda command: (f"C:/fake/{command}.exe", "fake"),
    )

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="synthetic failure\n")

    monkeypatch.setattr("image2dng.validate.subprocess.run", fake_run)

    result = validate_dng(output_path, run_smoke=True)

    assert not result.ok
    assert set(result.smoke_tests) == {
        "exiftool",
        "dcraw",
        "darktable-cli",
        "rawtherapee-cli",
    }
    assert all(value == "failed: synthetic failure" for value in result.smoke_tests.values())
    smoke_checks = [check for check in result.checks if check.name.startswith("smoke:")]
    assert {check.status for check in smoke_checks} == {"failed"}


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


def _write_adobe_rewritten_like_dng(path: Path) -> None:
    raw = _gradient_image(12, 10)
    preview = np.zeros((10, 12, 3), dtype=np.uint8)
    root_tags = [
        (TAG_NEW_SUBFILE_TYPE, "I", 1, 1, False),
        (TAG_DNG_VERSION, "B", 4, (1, 4, 0, 0), False),
        (TAG_DNG_BACKWARD_VERSION, "B", 4, (1, 1, 0, 0), False),
        (TAG_MAKE, "s", 0, "Adobe rewritten fixture", False),
        (TAG_MODEL, "s", 0, "Synthetic DNG", False),
        (TAG_UNIQUE_CAMERA_MODEL, "s", 0, "Adobe rewritten synthetic fixture", False),
        (TAG_ORIENTATION, "H", 1, 1, False),
    ]
    raw_tags = [
        (TAG_NEW_SUBFILE_TYPE, "I", 1, 0, False),
    ]
    with tifffile.TiffWriter(path) as writer:
        writer.write(
            preview,
            photometric="rgb",
            metadata=None,
            subifds=1,
            extratags=root_tags,
        )
        writer.write(
            raw,
            photometric=PHOTOMETRIC_LINEAR_RAW,
            metadata=None,
            planarconfig="contig",
            extratags=raw_tags,
        )


def _raw_data_unique_id(path: Path) -> tuple[int, ...]:
    with tifffile.TiffFile(path) as tif:
        return tuple(_raw_page(tif).tags[TAG_RAW_DATA_UNIQUE_ID].value)


def _raw_data_unique_id_hex(path: Path) -> str:
    return "".join(f"{value:02X}" for value in _raw_data_unique_id(path))


def _raw_page(tif: tifffile.TiffFile) -> tifffile.TiffPage:
    page = find_raw_image_page(tif)
    assert page is not None
    return page


def _gradient_image(width: int, height: int) -> np.ndarray:
    x = np.linspace(0, 65535, width, dtype=np.uint16)
    y = np.linspace(0, 65535, height, dtype=np.uint16)
    red = np.tile(x, (height, 1))
    green = np.tile(y[:, np.newaxis], (1, width))
    blue = ((red.astype(np.uint32) + green.astype(np.uint32)) // 2).astype(np.uint16)
    return np.stack([red, green, blue], axis=2)


def _write_semantic_scene(
    directory: Path,
    *,
    width: int,
    height: int,
    input_space: str = "linear-rec709",
    include_hash: bool = False,
    include_semantic_physics: bool = False,
    exposure_bias_ev: float | None = 0.0,
    mask_columns: int | None = None,
) -> Path:
    mask_path = directory / "renderer-frame-001-mask.png"
    mask = np.zeros((height, width), dtype=np.uint16)
    mask[:, : (mask_columns or width)] = 65535
    with mask_path.open("wb") as handle:
        png.Writer(width=width, height=height, bitdepth=16, greyscale=True).write(
            handle,
            mask.tolist(),
        )
    asset = {
        "id": "mask-material-chart",
        "kind": "mask",
        "path": mask_path.name,
        "space": "pixel",
    }
    if include_hash:
        asset["sha256"] = f"sha256:{hashlib.sha256(mask_path.read_bytes()).hexdigest()}"
    response_hints = {
        "exposure_bias_ev": exposure_bias_ev,
        "preserve_highlight_detail": True,
        "noise_priority": "low",
    }
    if include_semantic_physics:
        response_hints.update(
            {
                "source": "inferred",
                "confidence": 0.82,
            }
        )
    region = {
        "id": "region-neutral-card",
        "label": "neutral card",
        "material_id": "mat-neutral-card",
        "mask_asset_id": "mask-material-chart",
        "bbox": [0, 0, width, height],
        **({"response_hints": response_hints} if exposure_bias_ev is not None else {}),
    }
    if include_semantic_physics:
        region["raw_statistics"] = {
            "mean_linear_rgb": [0.18, 0.18, 0.18],
            "p50_linear_rgb": [0.18, 0.18, 0.18],
            "p95_linear_rgb": [0.72, 0.72, 0.72],
            "clipped_pixel_ratio": 0.0,
            "shadow_pixel_ratio": 0.01,
        }
    payload = {
        "schema": SEMANTIC_SCENE_SCHEMA,
        "scene": {
            "id": "renderer-frame-001",
            "description": "scene-linear output from an upstream generator",
            "width": width,
            "height": height,
            "coordinate_space": "pixel",
            "input_space": input_space,
        },
        "producer": {
            "name": "external renderer",
            "version": "0.1.0",
            "prompt_hash": "sha256:unit",
        },
        "assets": [asset],
        "materials": [
            {
                "id": "mat-neutral-card",
                "label": "neutral gray card",
                "base_color": [0.18, 0.18, 0.18],
                "roughness": 0.5,
                "metallic": 0.0,
                "emission": [0.0, 0.0, 0.0],
            }
        ],
        "lights": [
            {
                "id": "key-light",
                "type": "area",
                "color_temperature_kelvin": 6500,
                "relative_intensity": 1.0,
                "direction": [0.0, -0.5, -1.0],
            }
        ],
        "regions": [region],
        "sensor_response_hints": {
            "target_white_balance_kelvin": 6500,
            "target_middle_gray": 0.18,
            "clipping_policy": "preserve-highlights",
        },
    }
    if include_semantic_physics:
        payload.update(
            {
                "capture_physics": {
                    "source": "metadata",
                    "iso": 100,
                    "exposure_time_seconds": 0.008,
                    "aperture_f_number": 5.6,
                    "white_balance_kelvin": 6500,
                    "illuminant_confidence": 0.75,
                    "lux": 450,
                    "ev100": 10.0,
                },
                "camera_response": {
                    "camera_make": "unit-test",
                    "camera_model": "semantic physics fixture",
                    "cfa_pattern": "rggb",
                    "black_level": [512, 512, 512, 512],
                    "white_level": 16383,
                    "color_matrix_1": [],
                },
            }
        )
    semantic_path = directory / "renderer-frame-001.semantic.json"
    semantic_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return semantic_path


def _write_gray_png(path: Path, image: np.ndarray) -> None:
    height, width = image.shape
    with path.open("wb") as handle:
        png.Writer(width=width, height=height, bitdepth=16, greyscale=True).write(
            handle,
            image.tolist(),
        )


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


class _FakeDngValidateRunner:
    def __init__(
        self,
        *,
        fail_names: set[str] | None = None,
        timeout_names: set[str] | None = None,
        marker_pass_names: set[str] | None = None,
        version_exit_code: int = 1,
        version_stdout: str | None = None,
        version_timeout: bool = False,
    ) -> None:
        self.fail_names = fail_names or set()
        self.timeout_names = timeout_names or set()
        self.marker_pass_names = marker_pass_names or set()
        self.version_exit_code = version_exit_code
        self.version_stdout = version_stdout
        self.version_timeout = version_timeout

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
        assert cwd.exists()
        assert capture_output is True
        assert text is True
        assert check is False
        if len(command) == 1:
            stdout = (
                self.version_stdout
                if self.version_stdout is not None
                else (
                    "dng_validate, version 1.7.1 (2573) (64-bit)\n"
                    "Usage: dng_validate.exe [options] file1 file2 ...\n"
                )
            )
            if self.version_timeout:
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr="")
            return subprocess.CompletedProcess(
                command,
                self.version_exit_code,
                stdout=stdout,
                stderr="",
            )
        fixture = Path(command[-1])
        if fixture.name in self.timeout_names:
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output="fatal timeout while validating",
                stderr="",
            )
        if fixture.name in self.fail_names:
            return subprocess.CompletedProcess(
                command,
                7,
                stdout=f'Validating "{fixture}"...\nERROR: corrupt image\n',
                stderr="validation failed",
            )
        if fixture.name in self.marker_pass_names:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f'Validating "{fixture}"...\nValidation complete\nERROR marker\n',
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f'Validating "{fixture}"...\nValidation complete\n',
            stderr="",
        )


def _sha256_test_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class _FakeAdobeValidationStackRunner:
    def __init__(
        self,
        *,
        stale_report_names: set[str] | None = None,
        future_report_names: set[str] | None = None,
        no_report_names: set[str] | None = None,
    ) -> None:
        self.stale_report_names = stale_report_names or set()
        self.future_report_names = future_report_names or set()
        self.no_report_names = no_report_names or set()

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
        assert cwd.exists()
        assert timeout > 0
        assert capture_output is True
        assert text is True
        assert check is False
        script_name = Path(command[1]).name
        if script_name == "audit_adobe_local_resources.py":
            return self._write_resource_audit(command)
        if script_name == "generate_raw_native_batch.py":
            return self._write_project_fixtures(command)
        if script_name == "verify_adobe_dng_converter.py":
            return self._write_converter_report(command)
        if script_name == "run_adobe_dng_sdk_validation.py":
            return self._write_sdk_report(command)
        return subprocess.CompletedProcess(command, 9, stdout="", stderr="unexpected command")

    def _generated_at(self, name: str) -> str:
        if name in self.stale_report_names:
            return "2000-01-01T00:00:00+00:00"
        if name in self.future_report_names:
            return "2999-01-01T00:00:00+00:00"
        return datetime.now(UTC).isoformat()

    def _write_resource_audit(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        name = "resource-audit"
        output_dir = _command_path(command, "--output-dir")
        if name not in self.no_report_names:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "adobe-local-resource-report.json").write_text(
                json.dumps(
                    {
                        "schema": "image2dng.adobe_local_resource_audit.v1",
                        "generated_at": self._generated_at(name),
                        "ok": True,
                        "blocking_findings": [],
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, stdout="resource audit\n", stderr="")

    def _write_project_fixtures(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        output_dir = _command_path(command, "--output-dir")
        raw_dir = output_dir / "raw"
        manifest_dir = output_dir / "manifests"
        raw_dir.mkdir(parents=True, exist_ok=True)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        linear_dng = raw_dir / "sample-linearraw.dng"
        cfa_dng = raw_dir / "sample-cfa-rggb.dng"
        linear_dng.write_bytes(b"linear dng")
        cfa_dng.write_bytes(b"cfa dng")
        manifest = {
            "schema": "image2dng.raw_native_node_batch.v1",
            "scenes": [
                {
                    "slug": "sample",
                    "outputs": {
                        "linearraw_dng": str(linear_dng),
                        "cfa_dng": str(cfa_dng),
                    },
                    "validations": {
                        "linearraw": {"ok": True},
                        "cfa": {"ok": True},
                    },
                }
            ],
        }
        sample_index = {
            "schema": "image2dng.raw_native_sample_index.v1",
            "scene_count": 1,
            "all_validations_ok": True,
        }
        (manifest_dir / "raw-native-node-batch.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        (manifest_dir / "sample-index.json").write_text(
            json.dumps(sample_index),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="project fixtures\n", stderr="")

    def _write_converter_report(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        name = "adobe-dng-converter"
        output_dir = _command_path(command, "--output-dir")
        source_dir = output_dir / "source-dng"
        converted_dir = output_dir / "converted"
        source_dir.mkdir(parents=True, exist_ok=True)
        converted_dir.mkdir(parents=True, exist_ok=True)
        source_dng = source_dir / "adobe-single-raw-ifd-fixture.dng"
        converted_dng = converted_dir / "adobe-single-raw-ifd-fixture.dng"
        source_dng.write_bytes(b"source dng")
        dry_run = "--dry-run" in command
        if not dry_run:
            converted_dng.write_bytes(b"converted dng")
        if name in self.no_report_names:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="no report\n")
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "adobe-dng-converter-report.json").write_text(
            json.dumps(
                {
                    "schema": "image2dng.adobe_dng_converter_verification.v1",
                    "generated_at": self._generated_at(name),
                    "ok": True,
                    "dry_run": dry_run,
                    "status": "dry-run" if dry_run else "passed",
                    "artifacts": {
                        "source_dng": str(source_dng),
                        "converted_dng": str(converted_dng),
                    },
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="converter\n", stderr="")

    def _write_sdk_report(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        name = "adobe-dng-sdk-validation"
        output_dir = _command_path(command, "--output-dir")
        output_dir.mkdir(parents=True, exist_ok=True)
        fixture_dirs = [
            Path(command[index + 1])
            for index, value in enumerate(command)
            if value == "--fixture-dir"
        ]
        selected = sum(len(list(path.glob("*.dng"))) for path in fixture_dirs if path.exists())
        if name not in self.no_report_names:
            (output_dir / "adobe-dng-sdk-validation-report.json").write_text(
                json.dumps(
                    {
                        "schema": "image2dng.adobe_dng_sdk_validation_report.v1",
                        "generated_at": self._generated_at(name),
                        "ok": True,
                        "summary": {
                            "selected": selected,
                            "passed": selected,
                            "failed": 0,
                            "marker_blocked": 0,
                            "timeout": 0,
                            "skipped": 0,
                        },
                        "blocking_findings": [],
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, stdout="sdk\n", stderr="")


def _command_path(command: list[str], flag: str) -> Path:
    return Path(command[command.index(flag) + 1])


def _stack_step(report: dict[str, object], name: str) -> dict[str, object]:
    steps = report["steps"]
    assert isinstance(steps, list)
    for step in steps:
        assert isinstance(step, dict)
        if step["name"] == name:
            return step
    raise AssertionError(f"missing stack step: {name}")


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
    tool = Path(command[0]).stem.lower()
    if tool == "dcraw":
        output = Path(command[command.index("-O") + 1])
    elif tool == "darktable-cli":
        output = Path(command[2])
    elif tool == "rawtherapee-cli":
        output = Path(command[command.index("-o") + 1])
    elif tool == "adobe dng converter":
        output = Path(command[command.index("-d") + 1]) / Path(command[-1]).name
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
