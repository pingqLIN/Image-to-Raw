# Semantic Scene Sidecar Contract v1

`image2dng.semantic_scene.v1` is the semantic sidecar contract for external renderers, AI generators, simulation engines, and ComfyUI nodes in bridge projects that hand scene-linear images to this project.

The v1 goal is semantic preservation and validation. It lets the RAW-native pipeline preserve traceable scene, material, light, region, mask/depth asset, and sensor response hint data. Preservation remains the default behavior; implemented deterministic reaction models modify the copied scene-linear input only when the external scene manifest explicitly sets `apply_semantic_reaction: true`. The sidecar is not currently written into `DNGPrivateData`.

## Minimal Shape

```json
{
  "schema": "image2dng.semantic_scene.v1",
  "scene": {
    "id": "renderer-frame-001",
    "width": 256,
    "height": 256,
    "coordinate_space": "pixel",
    "input_space": "linear-rec709"
  },
  "assets": [],
  "materials": [],
  "lights": [],
  "regions": []
}
```

## Field Rules

- The root object must include `schema` and `scene`.
- `schema` must be `image2dng.semantic_scene.v1`.
- `scene.id`, `scene.width`, `scene.height`, `scene.coordinate_space`, and `scene.input_space` are required.
- `assets`, `materials`, `lights`, and `regions` may be omitted or empty arrays.
- `assets[].id`, `materials[].id`, `lights[].id`, and `regions[].id` must be unique inside their own collections.
- `assets[].path`, when present, must be a file path relative to the folder containing the semantic sidecar; it must not be absolute, point at a directory, or escape the sidecar directory.
- `assets[].sha256`, when present, must be `sha256:<hex>` and match the asset bytes.
- `regions[].material_id`, when present, must reference a defined material.
- `regions[].mask_asset_id`, when present, must reference a defined asset.
- `capture_physics`, `camera_response`, and `regions[].raw_statistics` are optional semantic-physics research fields. They can be preserved and validated, but they do not mean that real capture physics was measured.
- `capture_physics.source` and `regions[].response_hints.source`, when present, must be `measured`, `metadata`, `inferred`, `synthetic`, or `retrieved`.
- Confidence and ratio fields must be finite numbers between 0 and 1. ISO, exposure time, aperture, white balance, lux, and white level values must be positive when present. `ev100`, when present, must be finite and may be zero or negative for low-light scenes.
- `sensor_response_hints.target_middle_gray_policy`, when present, must be `global-gain-v1`; `sensor_response_hints.target_middle_gray_max_gain_ev`, when present, must be positive.
- `camera_response.cfa_pattern`, when present, must be `rggb`, `bggr`, `grbg`, or `gbrg`. Black level values must be non-negative and less than white level.
- `regions[].raw_statistics.mean_linear_rgb`, `p50_linear_rgb`, and `p95_linear_rgb`, when present, must be three finite non-negative numbers.
- Unknown fields are tolerated and preserved so upstream producers can extend the sidecar.

Missing `assets[].sha256` values produce warnings, not failures. This means the asset can be resolved, but the sidecar has not locked its integrity. When a hash is provided, the validator verifies it against the asset contents.

## Semantic-Physics Field Example

```json
{
  "capture_physics": {
    "source": "metadata",
    "iso": 100,
    "exposure_time_seconds": 0.008,
    "aperture_f_number": 5.6,
    "white_balance_kelvin": 6500,
    "illuminant_confidence": 0.75
  },
  "camera_response": {
    "cfa_pattern": "rggb",
    "black_level": [512, 512, 512, 512],
    "white_level": 16383
  },
  "regions": [
    {
      "id": "region-neutral-card",
      "raw_statistics": {
        "mean_linear_rgb": [0.18, 0.18, 0.18],
        "p50_linear_rgb": [0.18, 0.18, 0.18],
        "p95_linear_rgb": [0.72, 0.72, 0.72],
        "clipped_pixel_ratio": 0.0,
        "shadow_pixel_ratio": 0.01
      },
      "response_hints": {
        "source": "inferred",
        "confidence": 0.82
      }
    }
  ]
}
```

These fields are for auditable and traceable research sidecars. `source: inferred`, `retrieved`, or `synthetic` must not be interpreted as real physical measurement.

## Pipeline Behavior

When using an `image2dng.external_scene_linear_sources.v1` manifest, a scene entry can point at `semantic_manifest`:

```json
{
  "schema": "image2dng.external_scene_linear_sources.v1",
  "scenes": [
    {
      "slug": "renderer-frame-001",
      "path": "renderer-frame-001.tif",
      "input_space": "linear-rec709",
      "producer": "external renderer",
      "semantic_manifest": "renderer-frame-001.semantic.json"
    }
  ]
}
```

When `semantic_manifest` is present, the pipeline:

- validates the semantic sidecar before DNG generation;
- stops the batch on validation failure;
- copies the sidecar into the batch `inputs/` folder;
- copies resolvable local assets referenced by the sidecar into `inputs/<slug>-semantic-assets/`;
- records `semantic_artifacts`, `semantic_contract`, `semantic_to_raw_status`, and `semantic_validation` in `raw-native-node-batch.json`;
- records `semantic_contract`, `semantic_to_raw_status`, and `semantic_validation` in `sample-index.json`.

By default, `semantic_to_raw_status` is `preserved-not-applied`, meaning the sidecar is preserved and validated but does not affect raw buffer generation yet.

When the external scene manifest explicitly sets `apply_semantic_reaction: true`, the pipeline can enable deterministic reaction models:

- `region-exposure-mask-v1` reads finite `regions[].response_hints.exposure_bias_ev` values and `regions[].mask_asset_id`, then applies EV modulation to 16-bit scene-linear RGB values inside the mask.
- `target-middle-gray-policy-v1` reads `sensor_response_hints.target_middle_gray` only when `sensor_response_hints.target_middle_gray_policy` explicitly opts in to `global-gain-v1`, then uses a bounded global gain to move scene-linear median luminance toward the target middle gray.
- `highlight-clipping-policy-v1` reads `sensor_response_hints.clipping_policy`. `clip` records an explicit no-op baseline; `preserve-highlights` and `soft-rolloff` apply a deterministic soft shoulder to 16-bit scene-linear RGB values above fixed thresholds.

Reactions only support linear-light external inputs: `linear-rec709`, `acescg`, and `xyz`. These models are not full physical sensor models and do not claim spectral accuracy, ISO response, camera metering, camera tone-curve accuracy, or recovery of real detail after sensor clipping.

For applied reactions, the pipeline binds provenance to the copied batch inputs: `prompt_hash` includes the copied scene-linear source, copied semantic manifest, copied semantic asset bytes, and the `apply_semantic_reaction` flag. The reaction also rejects sidecars whose `scene.width`, `scene.height`, or `scene.input_space` do not match the actual external scene-linear input. Manifests keep the backward-compatible `semantic_reaction` primary summary and add a `semantic_reactions` list for every reaction model evaluated during the opt-in pass.

`semantic_reaction_model_registry()` exposes both implemented reaction models and deferred candidate model IDs. Deferred entries provide scope and boundary evidence only; they do not mean raw values are currently changed.

Current reaction model matrix:

| Semantic hint | Model status | Current raw effect | Intended raw effect | Boundary |
| --- | --- | --- | --- | --- |
| `regions[].response_hints.exposure_bias_ev` | `region-exposure-mask-v1` implemented | Yes | Yes | Finite EV, mask-bound, linear-light only. |
| `sensor_response_hints.clipping_policy` | `highlight-clipping-policy-v1` implemented | Yes | Yes | Deterministic soft shoulder; not a camera tone curve, ISO response, or proof of preserved sensor detail. |
| `regions[].response_hints.noise_priority` | `noise-priority-policy-v1` deferred | No | Deferred | Avoid mixing deterministic reaction proof with stochastic CFA noise. |
| `sensor_response_hints.target_middle_gray` + `target_middle_gray_policy: global-gain-v1` | `target-middle-gray-policy-v1` implemented | Yes | Yes | Deterministic median-luminance gain; not a real camera metering model. |
| `sensor_response_hints.target_white_balance_kelvin` | `target-white-balance-policy-v1` deferred | No | Deferred | Requires a color pipeline and illuminant policy before it can affect values. |

`semantic_to_raw_status` has three current states:

| Status | Manifest shape | Meaning |
| --- | --- | --- |
| `preserved-not-applied` | `semantic_validation` is present, `semantic_reaction` is empty | Sidecar was copied and validated, but raw values were generated from the original scene-linear input. |
| `applied` | `semantic_reaction.applied` is `true`, and `semantic_reactions` records each model summary | At least one opt-in reaction modified a copied scene-linear input before RAW generation. |
| `no-op` | `semantic_reactions` contains only no-op results with `reason` values | Reaction was requested and validated, but no eligible exposure-mask region or highlight policy changed pixels. |

## Validation

Use the CLI to validate a sidecar:

```powershell
uv run image2dng validate-semantic renderer-frame-001.semantic.json
uv run image2dng validate-semantic renderer-frame-001.semantic.json --json
```

The Python API is also available:

```python
from image2dng import validate_semantic_scene

result = validate_semantic_scene("renderer-frame-001.semantic.json")
assert result.ok, result.errors
```

The broader local verification path remains the repo tests plus a RAW-native batch smoke:

```powershell
uv run pytest
uv run ruff check
uv run python scripts/generate_raw_native_batch.py `
  --output-dir demo-output/semantic-scene-v1-smoke `
  --external-manifest demo-output/semantic-scene-v1-smoke-input/external-scenes.json
```
