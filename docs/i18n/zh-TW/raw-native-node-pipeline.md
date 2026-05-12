# RAW-native 節點式生成流程

本文件是 `image2dng` 從「既有影像轉 synthetic DNG」走向「AI 生成流程原生產出 synthetic RAW/DNG」的設計備忘。

## 核心判斷

短期先自建 repo 內的最小節點式 pipeline，而不是直接把 ComfyUI 安裝成核心依賴。

理由：

- RAW/DNG 語意、XMP provenance、synthetic camera 標示、validation contract 是本專案的核心責任，應先在本 repo 內保持可測試、可版本化、可回歸。
- 現有 `convert()`、DNG writer、validator、sensor effects 已經提供足夠基礎，可以快速拆成 graph artifacts。
- ComfyUI 很適合視覺化節點編排與生成模型生態，但若一開始綁為核心依賴，會讓 RAW 格式語意、模型工作流、UI extension 生命週期耦合過早。
- 第一批驗證目標是產生含 IFD0 JPEG preview 的 DNG、sidecar JPEG preview、validation JSON 與 graph manifest，不需要先引入大型 diffusion runtime。

ComfyUI 仍然是 Phase 2 整合目標。官方文件顯示 ComfyUI 具備 node/custom-node 與 CLI 管理路徑，適合之後包成 custom node 或由 API workflow 呼叫本 repo 的核心 pipeline：

- <https://docs.comfy.org/development/core-concepts/custom-nodes>
- <https://docs.comfy.org/comfy-cli/getting-started>
- <https://github.com/Comfy-Org/ComfyUI>

## 目標流程

```mermaid
flowchart LR
  A["Prompt / Intent"] --> B["Scene Linear Node"]
  B --> C["Virtual Camera Node"]
  C --> D["Sensor Node"]
  D --> E{"RAW Mode"}
  E --> F["LinearRaw DNG"]
  E --> G["Simulated CFA DNG"]
  F --> H["JPEG Preview"]
  G --> H
  F --> I["Validation JSON"]
  G --> I
  I --> J["Graph Manifest"]
```

## External scene-linear producer boundary

外部 renderer、AI generator、simulation engine 或未來 ComfyUI node 不需要直接理解 DNG writer。它們可以先交出 scene-linear TIFF/PNG，讓本 repo 負責 virtual camera、sensor effect、DNG layout、sidecar preview、validation 與 manifest。

最小 CLI：

```powershell
uv run python scripts/generate_raw_native_batch.py `
  --output-dir demo-output/external-scene-linear-batch `
  --scene-linear path\to\scene-linear.tif
```

多張圖或需要 metadata 時，使用 `image2dng.external_scene_linear_sources.v1` manifest：

```json
{
  "schema": "image2dng.external_scene_linear_sources.v1",
  "scenes": [
    {
      "slug": "renderer-frame-001",
      "path": "renderer-frame-001.tif",
      "input_space": "linear-rec709",
      "producer": "external renderer",
      "prompt": "studio material test",
      "description": "scene-linear output from an upstream generator",
      "lighting": "virtual D65 studio",
      "semantic_manifest": "renderer-frame-001.semantic.json"
    }
  ]
}
```

`semantic_manifest` 是刻意保留的下一階段接點。若 sidecar 使用 `image2dng.semantic_scene.v1`，pipeline 會在 DNG 產生前驗證它、複製 sidecar、複製可 resolve 的 local assets，並在 batch manifest 與 sample index 記錄 validation summary。預設仍不把語意資訊轉成 raw sample values。

若 external scene manifest 明確設定 `apply_semantic_reaction: true`，pipeline 會啟用 `region-exposure-mask-v1` prototype，使用 region mask 與 `exposure_bias_ev` 對 16-bit scene-linear RGB input 進行 deterministic EV modulation。這只支援 `linear-rec709`、`acescg`、`xyz`，不支援 encoded `srgb` 或 `prophoto-rgb` 直接套用 reaction。詳細 contract 見 [Semantic Scene Sidecar Contract v1](semantic-scene-sidecar-contract.md)。

## Artifact contract

每次 batch 至少輸出：

- `inputs/*-scene-linear.tif`：scene-linear RGB 中間影像。
- `raw/*-linearraw.dng`：三通道 synthetic LinearRaw DNG，預設包含 IFD0 JPEG preview 與 Raw SubIFD。
- `raw/*-cfa-rggb.dng`：single-channel simulated RGGB CFA DNG，預設包含 IFD0 false-color JPEG preview 與 Raw SubIFD。
- `jpeg/*-linearraw.jpg`：由 LinearRaw DNG raw page render 的 sidecar JPEG preview。
- `jpeg/*-cfa-rggb.jpg`：由 CFA DNG raw page false-color render 的 sidecar JPEG preview。
- `validation/*.json`：validator 結果。
- `manifests/raw-native-node-batch.json`：節點流程、輸入輸出、參數與驗證摘要。
- 若使用 semantic sidecar，batch manifest 會記錄 `semantic_artifacts`、`semantic_contract`、`semantic_to_raw_status` 與 `semantic_validation`；sample index 會記錄 `semantic_contract`、`semantic_to_raw_status` 與 `semantic_validation`。
- 若啟用 semantic reaction，batch manifest 與 sample index 會記錄 `semantic_reaction` summary；reaction-applied input 會以 `inputs/*-semantic-reaction.tif` 保存。

## 目前實作

```powershell
uv run python scripts/generate_raw_native_batch.py --output-dir demo-output/raw-native-node-batch
```

目前節點：

- `PromptIntentNode`
- `SceneLinearGeneratorNode`
- `ExternalSceneLinearInputNode`：只在使用外部 scene-linear input 時出現
- `VirtualCameraLinearRawNode`
- `VirtualCameraCfaNode`
- `JpegPreviewRenderNode`
- `DngValidationNode`

目前的 scene generator 是 deterministic procedural generator，不是最終 AI diffusion model。這是刻意設計：第一階段先驗證 RAW-native pipeline 的檔案語意、manifest、DNG 可解析性、embedded preview layout 與 sidecar JPEG preview 交付流程。

目前 `raw-native-node-batch.json` 與 `sample-index.json` 已由 focused tests 固定為 contract。Demo review bundle 也會收斂 RAW-native DNG、JPEG preview、validation JSON、manifest 與 compatibility evidence，供外部審查使用：

```powershell
uv run python scripts/generate_demo_review_bundle.py --output-dir demo-output/review-bundle
```

## 下一個 gate

1. 用代表樣本持續驗證 `preview-subifd` layout 在 RAW tools 中的行為；這是格式實驗，不直接宣稱完整 Adobe 相容。
2. 在已驗證的 semantic sidecar contract 上，設計語意、材質、光照、mask/depth 等如何進入 photon/sensor-response mapping。
3. 在 DNG tag contract 與 compatibility evidence 穩定後，再做 ComfyUI custom node 原型：輸入 prompt/scene-linear tensor/semantic sidecar，輸出 DNG path、sidecar JPEG path、manifest。
4. 若 ComfyUI custom node 穩定，再加入 ComfyUI 安裝與 smoke workflow 文件。
