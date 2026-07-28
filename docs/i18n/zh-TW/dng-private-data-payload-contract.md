# DNGPrivateData payload contract

本文件是 `DNGPrivateData` 的導入合約草案。它描述未來可以如何把大型結構化 provenance / semantic metadata 放入 DNG 私有資料區，但目前專案仍不寫入 `DNGPrivateData`。

## 狀態

Status: proposal / implementation gate

目前已實作：

- XMP synthetic provenance。
- `image2dng.semantic_scene.v1` sidecar 保存與驗證。
- batch manifest / sample index 記錄 semantic sidecar、producer metadata 與 validation artifacts。

目前尚未實作：

- `DNGPrivateData` tag writer。
- semantic sidecar 內嵌到 DNG binary。
- mask/depth/large JSON payload 內嵌。

## 導入目標

`DNGPrivateData` 只能用來保存可追溯 metadata，不得改變 RAW sample values，也不得讓 synthetic DNG 看起來像真實相機 MakerNote。

適合放入的資料：

- semantic sidecar digest。
- producer metadata digest。
- schema id 與 schema version。
- manifest-relative artifact references。
- compact provenance summary。

不適合放入的資料：

- 原始 RAW bytes。
- preview、mask、depth、crop、overlay 等大型或可還原私人內容。
- 真實相機 MakerNote spoofing。
- host-specific absolute paths，例如 `Q:\`、`C:\`、UNC path、user name。

## v1 payload shape

第一版 payload 應保持小而可審查：

```json
{
  "schema": "image2dng.dng_private_data.v1",
  "producer": "image2dng",
  "image2dng_version": "0.1.0",
  "semantic_scene": {
    "schema": "image2dng.semantic_scene.v1",
    "sha256": "sha256:<hex>",
    "artifact_role": "sidecar"
  },
  "producer_metadata": {
    "schema": "example.producer_metadata.v1",
    "sha256": "sha256:<hex>",
    "artifact_role": "sidecar"
  },
  "privacy": {
    "contains_absolute_paths": false,
    "contains_source_pixels": false,
    "contains_private_metadata": false
  }
}
```

## 寫入 gate

開始實作 writer 前，必須先完成：

1. 確認 DNG specification 對 `DNGPrivateData` 的 binary layout 與 reader tolerance。
2. 新增 validator，能確認 payload 存在時 schema、size、privacy flags 與 digest 欄位正確。
3. 新增 fixture，證明沒有 `DNGPrivateData` 時既有 DNG 輸出 byte layout 與 validator 不退化。
4. 新增 opt-in CLI/API flag；預設不得寫入。
5. 確認 Adobe DNG Converter、ExifTool、至少一個 RAW processor 對含 payload 的 DNG 行為。

## size and privacy policy

- v1 payload target size 應小於 16 KiB。
- 若 payload 會超過 16 KiB，改保留 external sidecar 並寫 digest reference。
- 若任何欄位包含 private path、source pixel、EXIF dump 或可還原私人內容，writer 必須拒絕。

## recommended next implementation slice

下一個安全實作批次：

1. 新增 payload builder，輸入 sidecar paths，輸出 canonical JSON bytes。
2. 新增 privacy scanner，拒絕 absolute path 與 source-pixel-like artifact。
3. 新增 tests，但暫時不寫入 DNG。
4. 只有在 Adobe / RAW processor smoke 通過後，再評估 writer opt-in。

