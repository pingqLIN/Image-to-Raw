from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import numpy as np
import png
import tifffile

from image2dng import OutputExistsError, convert
from image2dng.cli import main
from image2dng.dng_writer import TAG_XMP
from image2dng.image_processing import build_linearraw_buffer
from image2dng.models import AIMetadataModel, CameraProfileModel
from image2dng.validate import TAG_MAKER_NOTE, validate_dng
from image2dng.xmp import XMP_AI_NAMESPACE


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
    assert report["checks"] == []
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
    assert {check.status for check in result.checks} == {"skipped"}


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
