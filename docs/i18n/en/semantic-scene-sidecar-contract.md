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

`semantic_to_raw_status` is currently `preserved-not-applied`, meaning the sidecar is preserved and validated but does not affect raw buffer generation yet.

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
