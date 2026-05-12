# Semantic Scene Sidecar Contract v1

`image2dng.semantic_scene.v1` 是外部 renderer、AI generator、simulation engine 或未來 ComfyUI node 交付 scene-linear 影像時可附帶的語意 sidecar contract。

v1 的目標是保存並驗證語意資料，讓 RAW-native pipeline 能追溯 scene、material、light、region、mask/depth asset 與 sensor response hints。它目前不把語意資訊轉成 raw sample values，也不把 sidecar 寫入 `DNGPrivateData`。

## 最小結構

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

## 欄位規則

- root 必須有 `schema` 與 `scene`。
- `schema` 必須是 `image2dng.semantic_scene.v1`。
- `scene.id`、`scene.width`、`scene.height`、`scene.coordinate_space`、`scene.input_space` 必填。
- `assets`、`materials`、`lights`、`regions` 可省略或為空陣列。
- `assets[].id`、`materials[].id`、`lights[].id`、`regions[].id` 在各自集合中不可重複。
- `assets[].path` 若存在，必須能從 semantic sidecar 所在資料夾 resolve。
- `regions[].material_id` 若存在，必須引用已定義 material。
- `regions[].mask_asset_id` 若存在，必須引用已定義 asset。
- 未知欄位會被保留並容忍，方便外部 producer 擴充。

缺少 `assets[].sha256` 會產生 warning，但不會讓 validation 失敗。這代表 asset 可被 resolve，但完整性尚未被 sidecar 自身鎖定。

## Pipeline 行為

使用 `image2dng.external_scene_linear_sources.v1` manifest 時，可在 scene entry 指定 `semantic_manifest`：

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

當 `semantic_manifest` 存在時，pipeline 會：

- 在產生 DNG 前驗證 semantic sidecar。
- 驗證失敗時停止 batch，避免產出缺少語意一致性的 artifacts。
- 將 sidecar 複製到 batch `inputs/`。
- 將 sidecar 引用且可 resolve 的 local assets 複製到 `inputs/<slug>-semantic-assets/`。
- 在 `raw-native-node-batch.json` 寫入 `semantic_artifacts`、`semantic_contract`、`semantic_to_raw_status` 與 `semantic_validation`。
- 在 `sample-index.json` 寫入 `semantic_contract`、`semantic_to_raw_status` 與 `semantic_validation`。

`semantic_to_raw_status` 目前固定為 `preserved-not-applied`，表示 sidecar 已保存並驗證，但未參與 raw buffer 生成。

## 驗證

可透過 Python API 驗證 sidecar：

```python
from image2dng import validate_semantic_scene

result = validate_semantic_scene("renderer-frame-001.semantic.json")
assert result.ok, result.errors
```

本階段的完整驗證仍以 repo 測試與 RAW-native batch smoke 為主：

```powershell
uv run pytest
uv run ruff check
uv run python scripts/generate_raw_native_batch.py `
  --output-dir demo-output/semantic-scene-v1-smoke `
  --external-manifest demo-output/semantic-scene-v1-smoke-input/external-scenes.json
```
