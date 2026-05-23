# Semantic Scene Sidecar Contract v1

`image2dng.semantic_scene.v1` 是外部 renderer、AI generator、simulation engine 或 bridge project 中的 ComfyUI node 交付 scene-linear 影像時可附帶的語意 sidecar contract。

v1 的目標是保存並驗證語意資料，讓 RAW-native pipeline 能追溯 scene、material、light、region、mask/depth asset 與 sensor response hints。預設情況下，sidecar 只會被保存與驗證；只有在 external scene manifest 明確 opt-in 時，已實作的 deterministic reaction model 才會改變 copied scene-linear input 的 raw sample values。sidecar 目前不會寫入 `DNGPrivateData`。

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
- `assets[].path` 若存在，必須是相對於 semantic sidecar 所在資料夾的檔案路徑，且不得指向絕對路徑、資料夾或跳出 sidecar 目錄。
- `assets[].sha256` 若存在，必須是 `sha256:<hex>`，且會與 asset bytes 比對。
- `regions[].material_id` 若存在，必須引用已定義 material。
- `regions[].mask_asset_id` 若存在，必須引用已定義 asset。
- `capture_physics`、`camera_response` 與 `regions[].raw_statistics` 是 optional semantic-physics 研究欄位；它們可被保存與驗證，但不代表已量測真實拍攝現場。
- `capture_physics.source` 與 `regions[].response_hints.source` 若存在，必須是 `measured`、`metadata`、`inferred`、`synthetic` 或 `retrieved`。
- confidence、ratio 類欄位必須是 0 到 1 之間的有限數字；ISO、曝光時間、光圈、白平衡、lux 與 white level 等量值若存在必須為正數。`ev100` 若存在必須是有限數字，低光場景可為 0 或負值。
- `camera_response.cfa_pattern` 若存在，必須是 `rggb`、`bggr`、`grbg` 或 `gbrg`；black level 必須為非負，且必須小於 white level。
- `regions[].raw_statistics.mean_linear_rgb`、`p50_linear_rgb`、`p95_linear_rgb` 若存在，必須是三個非負有限數字。
- 未知欄位會被保留並容忍，方便外部 producer 擴充。

缺少 `assets[].sha256` 會產生 warning，但不會讓 validation 失敗。這代表 asset 可被 resolve，但完整性尚未被 sidecar 自身鎖定；若提供 hash，validator 會驗證內容是否相符。

## Semantic-Physics 欄位範例

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

這些欄位用來建立可審查、可追溯的研究 sidecar。`source: inferred`、`retrieved` 或 `synthetic` 不可被解讀為真實物理量測。

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

預設 `semantic_to_raw_status` 為 `preserved-not-applied`，表示 sidecar 已保存並驗證，但未參與 raw buffer 生成。

若 external scene manifest 明確設定 `apply_semantic_reaction: true`，pipeline 可啟用 deterministic reaction models。`region-exposure-mask-v1` 會讀取 finite `regions[].response_hints.exposure_bias_ev` values 與 `regions[].mask_asset_id`，對 mask 內的 16-bit scene-linear RGB values 做 EV modulation。`highlight-clipping-policy-v1` 會讀取 `sensor_response_hints.clipping_policy`，對高亮值做 deterministic value mapping；`clip` 只記錄既有 uint16 clamp baseline，`preserve-highlights` 與 `soft-rolloff` 使用不同固定 threshold 做 rolloff。這些 reactions 只支援 linear-light external inputs：`linear-rec709`、`acescg`、`xyz`。它們不是完整物理 sensor model，也不宣稱光譜或相機模擬正確性。

對 applied reactions 而言，pipeline 會把 provenance 綁定到已複製進 batch 的 inputs：`prompt_hash` 會納入 copied scene-linear source、copied semantic manifest、copied semantic asset bytes、`apply_semantic_reaction` flag，以及可套用的 reaction model / policy context。若 sidecar 的 `scene.width`、`scene.height` 或 `scene.input_space` 與實際 external scene-linear input 不一致，reaction 會拒絕執行。

manifest 會保留 `semantic_reaction` 作為 primary summary，並在 `semantic_reactions` list 中記錄每個 applied 或 explicit no-op reaction model 的詳細 summary。這讓舊讀取端可繼續讀單一 summary，新讀取端可分辨多個 model。

目前 reaction model matrix：

| Semantic hint | Model status | Current raw effect | Intended raw effect | Boundary |
| --- | --- | --- | --- | --- |
| `regions[].response_hints.exposure_bias_ev` | `region-exposure-mask-v1` 已實作 | Yes | Yes | finite EV、mask-bound、linear-light only。 |
| `sensor_response_hints.clipping_policy` | `highlight-clipping-policy-v1` 已實作 | Yes | Yes | deterministic value mapping；不是 camera tone curve、ISO response，也不證明 sensor clipping 後仍保留真實細節。 |
| `regions[].response_hints.noise_priority` | metadata / research | No | Deferred | 避免把 deterministic reaction proof 與 stochastic CFA noise 混在一起。 |
| `sensor_response_hints.target_middle_gray` | research | No | Deferred | 需要 calibration policy 才能影響 values。 |
| `sensor_response_hints.target_white_balance_kelvin` | metadata / research | No | Deferred | 需要 color pipeline 與 illuminant policy 才能影響 values。 |

目前 `semantic_to_raw_status` 有三種狀態：

| Status | Manifest shape | 意義 |
| --- | --- | --- |
| `preserved-not-applied` | 有 `semantic_validation`，`semantic_reaction` 為空 | sidecar 已複製並驗證，但 raw values 仍由原始 scene-linear input 產生。 |
| `applied` | `semantic_reaction.applied` 為 `true`，並可包含 `semantic_reactions` list | opt-in reaction model 在 RAW generation 前修改了複製後的 scene-linear input。 |
| `no-op` | `semantic_reaction.applied` 為 `false`，並包含 `reason` | 已要求並驗證 reaction，但沒有符合條件的 exposure-mask/highlight policy 造成 pixel 變更，或 `clip` 僅記錄既有 clamp baseline。 |

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
