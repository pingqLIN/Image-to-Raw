# Semantic Scene Sidecar Contract v1

`image2dng.semantic_scene.v1` is the semantic sidecar contract for external renderers, AI generators, simulation engines, and future ComfyUI nodes that hand scene-linear images to this project.

The v1 goal is semantic preservation and validation. It lets the RAW-native pipeline preserve traceable scene, material, light, region, mask/depth asset, and sensor response hint data. It does not convert semantic information into raw sample values yet, and it does not write the sidecar into `DNGPrivateData`.

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
- `assets[].path`, when present, must resolve from the folder containing the semantic sidecar.
- `regions[].material_id`, when present, must reference a defined material.
- `regions[].mask_asset_id`, when present, must reference a defined asset.
- Unknown fields are tolerated and preserved so upstream producers can extend the sidecar.

Missing `assets[].sha256` values produce warnings, not failures. This means the asset can be resolved, but the sidecar has not locked its integrity.

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

When the external scene manifest explicitly sets `apply_semantic_reaction: true`, the pipeline can enable the deterministic `region-exposure-mask-v1` prototype. This prototype reads finite `regions[].response_hints.exposure_bias_ev` values and `regions[].mask_asset_id`, applies EV modulation to 16-bit scene-linear RGB values inside the mask, and records a `semantic_reaction` summary in the manifests. It only supports linear-light external inputs: `linear-rec709`, `acescg`, and `xyz`. It is not a full physical sensor model and does not claim spectral or camera-simulation accuracy.

For applied reactions, the pipeline binds provenance to the copied batch inputs: `prompt_hash` includes the copied scene-linear source, copied semantic manifest, copied semantic asset bytes, and the `apply_semantic_reaction` flag. The reaction also rejects sidecars whose `scene.width`, `scene.height`, or `scene.input_space` do not match the actual external scene-linear input.

Current reaction model matrix:

| Semantic hint | Model status | Current raw effect | Intended raw effect | Boundary |
| --- | --- | --- | --- | --- |
| `regions[].response_hints.exposure_bias_ev` | `region-exposure-mask-v1` implemented | Yes | Yes | Finite EV, mask-bound, linear-light only. |
| `sensor_response_hints.clipping_policy` | `highlight-clipping-policy-v1` candidate | No | Yes | Future deterministic value mapping only; not a camera tone curve, ISO response, or proof of preserved sensor detail. |
| `regions[].response_hints.noise_priority` | Metadata/research | No | Deferred | Avoid mixing deterministic reaction proof with stochastic CFA noise. |
| `sensor_response_hints.target_middle_gray` | Research | No | Deferred | Requires calibration policy before it can affect values. |
| `sensor_response_hints.target_white_balance_kelvin` | Metadata/research | No | Deferred | Requires a color pipeline and illuminant policy before it can affect values. |

`semantic_to_raw_status` has three current states:

| Status | Manifest shape | Meaning |
| --- | --- | --- |
| `preserved-not-applied` | `semantic_validation` is present, `semantic_reaction` is empty | Sidecar was copied and validated, but raw values were generated from the original scene-linear input. |
| `applied` | `semantic_reaction.applied` is `true` with affected-region counts | Opt-in `region-exposure-mask-v1` modified a copied scene-linear input before RAW generation. |
| `no-op` | `semantic_reaction.applied` is `false` with a `reason` | Reaction was requested and validated, but no eligible exposure-mask region changed pixels. |

## Validation

Use the Python API to validate a sidecar:

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
