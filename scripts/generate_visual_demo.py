from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import png
import tifffile

from image2dng import convert
from image2dng.validate import find_raw_image_page, validate_dng

Asset = tuple[str, str, np.ndarray]
CFA_PATTERNS = ("rggb", "bggr", "grbg", "gbrg")


@dataclass(frozen=True)
class ManifestSheet:
    image: np.ndarray
    manifest: dict[str, Any]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate staged visual demo samples.")
    parser.add_argument("--output-dir", type=Path, default=Path("demo-output"))
    parser.add_argument(
        "--prophoto-tiff",
        action="append",
        type=Path,
        default=[],
        help="external 16-bit ProPhoto RGB TIFF to include in the local demo run",
    )
    args = parser.parse_args(argv)

    root = args.output_dir
    inputs_dir = root / "inputs"
    phase15_dir = root / "phase-15-linearraw"
    phase2_dir = root / "phase-2-cfa"
    phase3_dir = root / "phase-3-sensor-effects"
    contact_dir = root / "contact-sheets"
    for directory in (inputs_dir, phase15_dir, phase2_dir, phase3_dir, contact_dir):
        directory.mkdir(parents=True, exist_ok=True)

    assets = [
        ("chart-gradient", "standard chart, tonal ramps, saturated patches", chart_gradient()),
        ("skin-tones", "synthetic skin-tone panels and portrait shapes", skin_tones()),
        (
            "daily-objects",
            "synthetic fruit, fabric, metal, glass, and print objects",
            daily_objects(),
        ),
    ]

    manifest: dict[str, object] = {
        "schema": "image2dng.visual_demo_manifest.v1",
        "description": "Staged image2dng visual demo outputs.",
        "external_assets": [],
        "assets": [],
        "contact_sheets": [],
        "cfa_pattern_comparison": {},
        "sensor_effects_comparison": {},
    }
    for path in args.prophoto_tiff:
        if not path.exists():
            raise FileNotFoundError(path)
        image = tifffile.imread(path)
        assets.append((safe_slug(path.stem), f"external 16-bit ProPhoto TIFF: {path.name}", image))
        manifest["external_assets"].append(
            {
                "name": path.name,
                "status": "included in local run as prophoto-rgb input",
            }
        )

    contact_rows = []
    for slug, description, image in assets:
        input_path = inputs_dir / f"{slug}.tif"
        tifffile.imwrite(input_path, image, photometric="rgb")
        write_png_rgb8(inputs_dir / f"{slug}-source-preview.png", fit_preview(preview_rgb(image)))

        outputs = generate_asset_outputs(
            slug=slug,
            description=description,
            input_path=input_path,
            input_space="prophoto-rgb" if "ProPhoto" in description else "linear-rec709",
            phase15_dir=phase15_dir,
            phase2_dir=phase2_dir,
            phase3_dir=phase3_dir,
        )
        manifest["assets"].append(outputs["manifest"])
        contact_rows.append(outputs["contact_row"])

    phase_overview = make_contact_sheet(contact_rows)
    phase_overview_path = contact_dir / "phase-overview.png"
    write_png_rgb8(phase_overview_path, phase_overview)
    manifest["contact_sheets"].append(
        {
            "path": str(phase_overview_path.relative_to(root)),
            "columns": ["source", "linearraw", "cfa", "linearraw-noisy", "cfa-noisy"],
            "rows": [asset[0] for asset in assets],
        }
    )

    sensor_sheet = make_sensor_effect_sheet(assets[0][2], root)
    sensor_sheet_path = contact_dir / "sensor-effects-comparison.png"
    write_png_rgb8(sensor_sheet_path, sensor_sheet.image)
    manifest["contact_sheets"].append(
        {
            "path": str(sensor_sheet_path.relative_to(root)),
            "columns": ["none", "shot", "read", "row", "combined"],
            "rows": ["chart-gradient"],
        }
    )
    manifest["sensor_effects_comparison"] = sensor_sheet.manifest

    cfa_sheet = make_cfa_pattern_sheet(
        source=assets[0][2],
        root=root,
        phase2_dir=phase2_dir,
    )
    cfa_sheet_path = contact_dir / "cfa-pattern-comparison.png"
    write_png_rgb8(cfa_sheet_path, cfa_sheet.image)
    manifest["contact_sheets"].append(
        {
            "path": str(cfa_sheet_path.relative_to(root)),
            "columns": list(CFA_PATTERNS),
            "rows": ["chart-gradient"],
        }
    )
    manifest["cfa_pattern_comparison"] = cfa_sheet.manifest

    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote visual demo samples to {root}")
    return 0


def generate_asset_outputs(
    *,
    slug: str,
    description: str,
    input_path: Path,
    input_space: str,
    phase15_dir: Path,
    phase2_dir: Path,
    phase3_dir: Path,
) -> dict[str, object]:
    linear_path = phase15_dir / f"{slug}-linearraw.dng"
    cfa_path = phase2_dir / f"{slug}-cfa-rggb.dng"
    linear_noisy_path = phase3_dir / f"{slug}-linearraw-noisy.dng"
    cfa_noisy_path = phase3_dir / f"{slug}-cfa-rggb-noisy.dng"

    convert(
        input_path=input_path,
        output_path=linear_path,
        input_space=input_space,
        mode="linearraw",
        prompt_hash=f"sha256:demo-{slug}-linearraw",
        scene_description=f"{description}; LinearRaw visual demo",
        overwrite=True,
    )
    convert(
        input_path=input_path,
        output_path=cfa_path,
        input_space=input_space,
        mode="cfa",
        cfa_pattern="rggb",
        prompt_hash=f"sha256:demo-{slug}-cfa",
        scene_description=f"{description}; CFA visual demo",
        overwrite=True,
    )
    convert(
        input_path=input_path,
        output_path=linear_noisy_path,
        input_space=input_space,
        mode="linearraw",
        shot_noise=0.01,
        read_noise=0.002,
        row_noise=0.001,
        sensor_effect_seed=20260510,
        prompt_hash=f"sha256:demo-{slug}-linearraw-noisy",
        scene_description=f"{description}; LinearRaw sensor-effects visual demo",
        overwrite=True,
    )
    convert(
        input_path=input_path,
        output_path=cfa_noisy_path,
        input_space=input_space,
        mode="cfa",
        cfa_pattern="rggb",
        shot_noise=0.01,
        read_noise=0.002,
        row_noise=0.001,
        sensor_effect_seed=20260510,
        prompt_hash=f"sha256:demo-{slug}-cfa-noisy",
        scene_description=f"{description}; CFA sensor-effects visual demo",
        overwrite=True,
    )

    source_preview = fit_preview(preview_rgb(tifffile.imread(input_path)))
    linear_preview = dng_preview(linear_path)
    cfa_preview = dng_preview(cfa_path, cfa_pattern="rggb")
    linear_noisy_preview = dng_preview(linear_noisy_path)
    cfa_noisy_preview = dng_preview(cfa_noisy_path, cfa_pattern="rggb")

    preview_paths = {
        "source": input_path.with_name(f"{slug}-source-preview.png"),
        "linearraw": linear_path.with_name(f"{slug}-linearraw-preview.png"),
        "cfa": cfa_path.with_name(f"{slug}-cfa-rggb-mosaic-preview.png"),
        "linearraw_noisy": linear_noisy_path.with_name(f"{slug}-linearraw-noisy-preview.png"),
        "cfa_noisy": cfa_noisy_path.with_name(f"{slug}-cfa-rggb-noisy-preview.png"),
        "noise_diff": linear_noisy_path.with_name(f"{slug}-noise-diff.png"),
    }
    write_png_rgb8(preview_paths["source"], source_preview)
    write_png_rgb8(preview_paths["linearraw"], linear_preview)
    write_png_rgb8(preview_paths["cfa"], cfa_preview)
    write_png_rgb8(preview_paths["linearraw_noisy"], linear_noisy_preview)
    write_png_rgb8(preview_paths["cfa_noisy"], cfa_noisy_preview)
    write_png_rgb8(preview_paths["noise_diff"], diff_heatmap(linear_preview, linear_noisy_preview))

    validation_paths = {}
    validation_ok = {}
    for name, path in {
        "linearraw": linear_path,
        "cfa": cfa_path,
        "linearraw_noisy": linear_noisy_path,
        "cfa_noisy": cfa_noisy_path,
    }.items():
        validation_path = path.with_suffix(".validation.json")
        validation = validate_dng(path, run_smoke=False).to_dict()
        validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
        validation_paths[name] = validation_path
        validation_ok[name] = validation["ok"]

    return {
        "manifest": {
            "slug": slug,
            "description": description,
            "input_space": input_space,
            "input": str(input_path),
            "outputs": {
                "phase_15_linearraw": str(linear_path),
                "phase_2_cfa": str(cfa_path),
                "phase_3_linearraw_noisy": str(linear_noisy_path),
                "phase_3_cfa_noisy": str(cfa_noisy_path),
            },
            "previews": {key: str(path) for key, path in preview_paths.items()},
            "validations": {key: str(path) for key, path in validation_paths.items()},
            "validation_ok": validation_ok,
        },
        "contact_row": [
            source_preview,
            linear_preview,
            cfa_preview,
            linear_noisy_preview,
            cfa_noisy_preview,
        ],
    }


def chart_gradient(size: int = 256) -> np.ndarray:
    image = np.zeros((size, size, 3), dtype=np.uint16)
    x = np.linspace(0, 65535, size, dtype=np.uint16)
    image[:64, :, :] = x[np.newaxis, :, np.newaxis]

    colors = np.array(
        [
            [65535, 0, 0],
            [0, 65535, 0],
            [0, 0, 65535],
            [65535, 65535, 0],
            [0, 65535, 65535],
            [65535, 0, 65535],
            [65535, 65535, 65535],
            [0, 0, 0],
        ],
        dtype=np.uint16,
    )
    patch_h = 48
    patch_w = size // len(colors)
    for index, color in enumerate(colors):
        image[72 : 72 + patch_h, index * patch_w : (index + 1) * patch_w] = color

    for row in range(3):
        for col in range(8):
            level = int((row * 8 + col) / 23 * 65535)
            image[136 + row * 28 : 160 + row * 28, col * 32 : (col + 1) * 32] = level

    yy, xx = np.indices((size, size))
    image[224:, :, 0] = (xx[224:] / (size - 1) * 65535).astype(np.uint16)
    image[224:, :, 1] = (yy[224:] / (size - 1) * 65535).astype(np.uint16)
    image[224:, :, 2] = 32768
    return image


def skin_tones(size: int = 256) -> np.ndarray:
    image = np.full((size, size, 3), 56000, dtype=np.uint16)
    skin_palette = np.array(
        [
            [64200, 47000, 39000],
            [59000, 39000, 30000],
            [47000, 28500, 21000],
            [31000, 17500, 12500],
        ],
        dtype=np.uint16,
    )
    for index, color in enumerate(skin_palette):
        x0 = index * 64
        image[:70, x0 : x0 + 64] = color
        image[70:96, x0 : x0 + 64] = np.clip(color * 0.72, 0, 65535).astype(np.uint16)

    yy, xx = np.indices((size, size))
    centers = [(48, 160), (104, 160), (160, 160), (216, 160)]
    for index, (cx, cy) in enumerate(centers):
        color = skin_palette[index]
        face = ((xx - cx) / 23) ** 2 + ((yy - cy) / 34) ** 2 <= 1
        hair = ((xx - cx) / 27) ** 2 + ((yy - (cy - 17)) / 19) ** 2 <= 1
        shirt = (np.abs(xx - cx) < 31) & (yy > cy + 35) & (yy < cy + 74)
        image[face] = color
        image[hair] = np.array([9000, 7000, 5000], dtype=np.uint16) + index * 1200
        image[shirt] = np.array([12000 + index * 6000, 20000, 43000], dtype=np.uint16)
        for eye_x in (cx - 8, cx + 8):
            eye = (xx - eye_x) ** 2 + (yy - (cy - 3)) ** 2 <= 6
            image[eye] = 2500
        mouth = ((xx - cx) / 10) ** 2 + ((yy - (cy + 15)) / 4) ** 2 <= 1
        image[mouth] = np.array([42000, 9000, 12000], dtype=np.uint16)
    return image


def daily_objects(size: int = 256) -> np.ndarray:
    yy, xx = np.indices((size, size))
    image = np.zeros((size, size, 3), dtype=np.uint16)
    image[..., 0] = (xx / (size - 1) * 18000 + 8000).astype(np.uint16)
    image[..., 1] = (yy / (size - 1) * 18000 + 9000).astype(np.uint16)
    image[..., 2] = 26000

    apple = (xx - 62) ** 2 + (yy - 78) ** 2 <= 38**2
    orange = (xx - 132) ** 2 + (yy - 72) ** 2 <= 32**2
    glass = ((xx - 205) / 24) ** 2 + ((yy - 73) / 42) ** 2 <= 1
    fabric = yy > 150
    metal = (xx > 145) & (yy > 116) & (yy < 150)
    print_patch = (xx < 116) & (yy > 122) & (yy < 148)

    image[apple] = np.array([56000, 7000, 6500], dtype=np.uint16)
    image[orange] = np.array([62000, 31000, 4500], dtype=np.uint16)
    image[glass] = np.array([30000, 48000, 56000], dtype=np.uint16)
    image[fabric] = np.stack(
        [
            np.full(np.count_nonzero(fabric), 7000, dtype=np.uint16),
            (9000 + ((xx[fabric] + yy[fabric]) % 28) * 900).astype(np.uint16),
            (16000 + ((xx[fabric] * 3) % 36) * 900).astype(np.uint16),
        ],
        axis=1,
    )
    image[metal] = np.stack(
        [
            (18000 + (xx[metal] - 145) * 600).astype(np.uint16),
            (18000 + (xx[metal] - 145) * 600).astype(np.uint16),
            (19000 + (xx[metal] - 145) * 650).astype(np.uint16),
        ],
        axis=1,
    )
    image[print_patch] = np.where(((xx[print_patch] // 5) % 2)[:, None] == 0, 62000, 3000)
    return image


def dng_preview(path: Path, *, cfa_pattern: str | None = None) -> np.ndarray:
    with tifffile.TiffFile(path) as tif:
        page = find_raw_image_page(tif)
        if page is None:
            raise ValueError(f"no main raw image IFD found: {path}")
        data = page.asarray()
    if data.ndim == 2:
        return fit_preview(cfa_false_color(data, cfa_pattern or "rggb"))
    return fit_preview(preview_rgb(data))


def cfa_false_color(mosaic: np.ndarray, cfa_pattern: str) -> np.ndarray:
    channels = {
        "rggb": ((0, 1), (1, 2)),
        "bggr": ((2, 1), (1, 0)),
        "grbg": ((1, 0), (2, 1)),
        "gbrg": ((1, 2), (0, 1)),
    }[cfa_pattern]
    scaled = tone_map(mosaic)
    preview = np.zeros((*mosaic.shape, 3), dtype=np.uint8)
    for y in range(mosaic.shape[0]):
        for x in range(mosaic.shape[1]):
            channel = channels[y % 2][x % 2]
            preview[y, x, channel] = scaled[y, x]
    return preview


def preview_rgb(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        gray = tone_map(image)
        return np.stack([gray, gray, gray], axis=2)
    return np.stack([tone_map(image[..., channel]) for channel in range(3)], axis=2)


def tone_map(channel: np.ndarray) -> np.ndarray:
    data = channel.astype(np.float64)
    low, high = np.percentile(data, [0.5, 99.5])
    if high <= low:
        high = low + 1.0
    return np.clip((data - low) / (high - low) * 255.0, 0, 255).astype(np.uint8)


def diff_heatmap(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    diff = np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16)), axis=2)
    scaled = tone_map(diff)
    return np.stack([scaled, np.zeros_like(scaled), 255 - scaled], axis=2)


def make_contact_sheet(rows: list[list[np.ndarray]], gutter: int = 8) -> np.ndarray:
    cell_h, cell_w, _ = rows[0][0].shape
    width = len(rows[0]) * cell_w + (len(rows[0]) + 1) * gutter
    height = len(rows) * cell_h + (len(rows) + 1) * gutter
    sheet = np.full((height, width, 3), 230, dtype=np.uint8)
    for row_index, row in enumerate(rows):
        for col_index, image in enumerate(row):
            y0 = gutter + row_index * (cell_h + gutter)
            x0 = gutter + col_index * (cell_w + gutter)
            sheet[y0 : y0 + cell_h, x0 : x0 + cell_w] = image
    return sheet


def fit_preview(image: np.ndarray, *, size: int = 256) -> np.ndarray:
    height, width = image.shape[:2]
    scale = size / min(height, width)
    new_height = max(size, int(round(height * scale)))
    new_width = max(size, int(round(width * scale)))
    y_index = np.linspace(0, height - 1, new_height).astype(int)
    x_index = np.linspace(0, width - 1, new_width).astype(int)
    resized = image[y_index][:, x_index]
    y0 = (new_height - size) // 2
    x0 = (new_width - size) // 2
    return resized[y0 : y0 + size, x0 : x0 + size]


def safe_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "external-prophoto"


def make_sensor_effect_sheet(source: np.ndarray, root: Path) -> ManifestSheet:
    temp_input = root / "inputs" / "sensor-effects-source.tif"
    tifffile.imwrite(temp_input, source, photometric="rgb")
    specs = [
        ("none", {}),
        ("shot", {"shot_noise": 0.01}),
        ("read", {"read_noise": 0.002}),
        ("row", {"row_noise": 0.001}),
        ("combined", {"shot_noise": 0.01, "read_noise": 0.002, "row_noise": 0.001}),
    ]
    row = []
    samples = []
    for name, effects in specs:
        output = root / "phase-3-sensor-effects" / f"chart-gradient-linearraw-{name}.dng"
        preview_path = output.with_name(f"chart-gradient-linearraw-{name}-preview.png")
        validation_path = output.with_suffix(".validation.json")
        convert(
            input_path=temp_input,
            output_path=output,
            input_space="linear-rec709",
            mode="linearraw",
            sensor_effect_seed=20260510,
            prompt_hash=f"sha256:demo-sensor-{name}",
            scene_description=f"sensor effect comparison: {name}",
            overwrite=True,
            **effects,
        )
        preview = dng_preview(output)
        validation = validate_dng(output, run_smoke=False).to_dict()
        write_png_rgb8(preview_path, preview)
        validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
        row.append(preview)
        samples.append(
            {
                "name": name,
                "effects": effects,
                "output": str(output),
                "preview": str(preview_path),
                "validation": str(validation_path),
                "validation_ok": validation["ok"],
            }
        )
    return ManifestSheet(
        image=make_contact_sheet([row]),
        manifest={
            "source": str(temp_input),
            "samples": samples,
            "all_validations_ok": _all_validations_ok(samples),
        },
    )


def make_cfa_pattern_sheet(
    *,
    source: np.ndarray,
    root: Path,
    phase2_dir: Path,
) -> ManifestSheet:
    temp_input = root / "inputs" / "cfa-pattern-source.tif"
    tifffile.imwrite(temp_input, source, photometric="rgb")
    row = []
    samples = []
    for pattern in CFA_PATTERNS:
        output = phase2_dir / f"chart-gradient-cfa-{pattern}.dng"
        preview_path = output.with_name(f"chart-gradient-cfa-{pattern}-mosaic-preview.png")
        validation_path = output.with_suffix(".validation.json")
        convert(
            input_path=temp_input,
            output_path=output,
            input_space="linear-rec709",
            mode="cfa",
            cfa_pattern=pattern,
            prompt_hash=f"sha256:demo-cfa-pattern-{pattern}",
            scene_description=f"CFA pattern comparison: {pattern}",
            overwrite=True,
        )
        preview = dng_preview(output, cfa_pattern=pattern)
        validation = validate_dng(output, run_smoke=False).to_dict()
        write_png_rgb8(preview_path, preview)
        validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
        row.append(preview)
        samples.append(
            {
                "pattern": pattern,
                "output": str(output),
                "preview": str(preview_path),
                "validation": str(validation_path),
                "validation_ok": validation["ok"],
            }
        )
    return ManifestSheet(
        image=make_contact_sheet([row]),
        manifest={
            "source": str(temp_input),
            "patterns": list(CFA_PATTERNS),
            "samples": samples,
            "all_validations_ok": _all_validations_ok(samples),
        },
    )


def write_png_rgb8(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if image.ndim == 2:
        writer = png.Writer(width=image.shape[1], height=image.shape[0], bitdepth=8, greyscale=True)
        rows = image.tolist()
    else:
        writer = png.Writer(
            width=image.shape[1],
            height=image.shape[0],
            bitdepth=8,
            greyscale=False,
        )
        rows = image.reshape(image.shape[0], image.shape[1] * 3).tolist()
    with path.open("wb") as handle:
        writer.write(handle, rows)


def _all_validations_ok(samples: list[dict[str, Any]]) -> bool:
    return all(_sample_validation_ok(sample) for sample in samples)


def _sample_validation_ok(sample: dict[str, Any]) -> bool:
    validation_ok = sample["validation_ok"]
    if not isinstance(validation_ok, bool):
        raise TypeError("sample validation_ok must be a boolean")
    return validation_ok


if __name__ == "__main__":
    raise SystemExit(main())
