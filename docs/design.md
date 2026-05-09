# Image-to-DNG RAW Generator Design

## Research background

這個專案目前偏工程實作與格式設計，不以重現某一篇論文的方法為目標。不過有三份外部資料很適合當作研究背景，幫助界定問題空間：

1. **RawGen: Learning Camera Raw Image Generation**  
   <https://arxiv.org/abs/2604.00093>  
   這篇論文把 `text-to-raw` 與 `sRGB-to-raw inversion` 明確定義成值得研究的問題，說明「從既有影像或生成流程回到 camera-centric linear representation」本身就是一個活躍題目。對本專案而言，它主要用來支持問題定義；目前 MVP 不以複製其 diffusion 方法為目標。

2. **DxO Linear DNG background**  
   <https://www.dxo.com/technology/linear-dng/>  
   DxO 的 Linear DNG 說明不是學術論文，但很有價值，因為它反映了真實攝影工作流中 Linear DNG 的定位：檔案可以已經過部分線性處理，仍保留相當程度的 RAW 編修彈性。這支持本專案選擇先做 `LinearRaw` MVP，而不是一開始就追求 CFA 偽原始資料。不過 DxO 的場景是由真實相機 RAW 經過處理後導出 Linear DNG，與本專案從 rendered RGB 或 scene-linear 影像生成 synthetic DNG 的目標不同。

3. **Linear DNG overview (Kronometric)**  
   <https://kronometric.org/phot/processing/DNG/Linear%20DNG.htm>  
   這份資料較早，但對概念釐清很有幫助。它說明 Linear DNG 不一定承載傳統意義上的未處理感光資料，也可以承載線性化後的多通道影像資料。對本專案而言，這有助於確認「合法、可解析、誠實標示的 synthetic LinearRaw DNG」在格式概念上是站得住腳的。

綜合來看，這三份資料支持的不是同一件事：

- RawGen 支持「這個問題值得研究」；
- DxO 支持「Linear DNG 是實際工作流中有意義的交付形式」；
- Kronometric 支持「Linear DNG 在格式概念上可以容納非傳統相機 RAW 的線性影像表示」。

因此設計路線維持：

- 先把 DNG/TIFF container correctness 做穩；
- 先把 synthetic provenance 與 simulated camera metadata 寫誠實；
- 先以 `LinearRaw` 作為可互通的 MVP；
- CFA 必須是明確 opt-in 的 simulated mode；
- noise、depth、semantic mask，以及更接近研究型 inverse-ISP / raw-generation 的方向，不屬於 LinearRaw MVP。

## LinearRaw MVP scope

MVP 目標是把 16-bit TIFF/PNG 或 scene-linear RGB 影像轉成合法、可解析、誠實標示來源的 DNG。主影像 IFD 使用：

- `PhotometricInterpretation = LinearRaw`
- `SamplesPerPixel = 3`
- `BitsPerSample = 16,16,16`
- `Compression = 1` uncompressed
- `UniqueCameraModel = "Synthetic Camera v1"`

輸入流程：

1. 讀取 RGB 或 grayscale 16-bit TIFF/PNG。
2. `srgb` 輸入套用 inverse sRGB OETF，其它 input space 視為 scene-linear。
3. `linear-rec709`/`srgb` 直接視為 virtual camera native RGB。
4. `acescg`/`xyz` 先轉到 XYZ，再轉到 virtual camera native RGB。
5. 加入 black level offset，clip 到 white level。
6. quantize 為 16-bit LinearRaw buffer。

這個 MVP 不嘗試偽裝成真實相機檔案；它輸出的是 synthetic LinearRaw DNG。

## 為何不先做 CFA

CFA DNG 需要決定 Bayer pattern、CFA plane color、去馬賽克前的 aliasing 行為、黑邊/遮蔽像素、固定樣式噪聲、shot/read noise、white balance 與 color matrix 的一致性。若先從 rendered RGB 反推 CFA，容易產生看似 RAW 但語意不誠實、也不穩定的檔案。

LinearRaw 先保證：

- DNG/TIFF container 可被解析。
- RAW 軟體能看到線性主影像與基本 profile。
- AI metadata 不影響主影像可讀性。
- 不需要在概念驗證階段承諾 CFA sensor simulation。

## Simulated CFA mode

Phase 2 introduces an explicit `--mode cfa` path for compatibility and workflow research. This mode converts the virtual camera RGB buffer into a single-channel 2x2 Bayer mosaic. Supported patterns are:

- `rggb`
- `bggr`
- `grbg`
- `gbrg`

The CFA DNG writes:

- `PhotometricInterpretation = 32803` (`ColorFilterArray`)
- `SamplesPerPixel = 1`
- `CFARepeatPatternDim = 2,2`
- `CFAPattern` for the selected Bayer pattern
- `CFAPlaneColor = 0,1,2`
- `BlackLevelRepeatDim = 2,2`

This is still synthetic data. The XMP packet records `xmpAI:rawMode="cfa"` and `xmpAI:cfaPattern`, and camera parameters remain marked as simulated. CFA mode does not add sensor noise by default, does not add optical black borders, and does not claim to represent a real camera sensor capture.

## Synthetic sensor effects

Phase 3 adds optional deterministic sensor-effect controls for demos and compatibility experiments:

- `shot_noise`: signal-dependent Gaussian variation.
- `read_noise`: additive Gaussian variation.
- `row_noise`: row-level offset variation.
- `sensor_effect_seed`: deterministic seed for reproducible sample generation.

These effects are applied in virtual camera RGB before quantization or CFA mosaicing. XMP records `xmpAI:sensorNoiseModel="synthetic-simple-v1"` plus the enabled parameters. The model is intentionally simple; it is not a physical sensor simulator and should not be used to impersonate real camera behavior.

## DNG tag layout

目前 MVP 寫入單一主 IFD。另一種可行 layout 是 IFD0 + Raw SubIFD，但現階段主 IFD 已包含可獨立讀取的 Raw image data 與 DNG metadata。

| Tag | Value |
| --- | --- |
| `DNGVersion` | `1.4.0.0` |
| `DNGBackwardVersion` | `1.1.0.0` |
| `UniqueCameraModel` | `Synthetic Camera v1` |
| `Orientation` | `1` |
| `ImageWidth` / `ImageLength` | 由輸入影像決定 |
| `BitsPerSample` | `16,16,16` |
| `SamplesPerPixel` | `3` |
| `Compression` | `1` |
| `PhotometricInterpretation` | `34892` (`LinearRaw`) |
| `BlackLevelRepeatDim` | `1,1` |
| `BlackLevel` | `512,512,512` |
| `WhiteLevel` | `65535,65535,65535` |
| `ActiveArea` | `0,0,height,width` |
| `DefaultCropOrigin` | `0,0` |
| `DefaultCropSize` | `width,height` |
| `ColorMatrix1` | virtual camera XYZ-to-native matrix |
| `CalibrationIlluminant1` | `21` (`D65`) |
| `AsShotNeutral` | CCT-derived neutral, normalized to green |
| `Software` | `image2dng 0.1.0` |
| `XMP` | custom AI provenance packet |

MakerNote is intentionally not written.

## XMP AI metadata schema

Namespace:

```text
https://example.org/ns/xmp/ai/1.0/
```

MVP fields:

- `xmpAI:provenanceType="synthetic"`
- `xmpAI:modelName`
- `xmpAI:modelVersion`
- `xmpAI:promptHash`
- `xmpAI:sceneDescription`
- `xmpAI:lighting`
- `xmpAI:weather`
- `xmpAI:cameraParametersAreSimulated="True"`
- `xmpAI:simulatedISO`
- `xmpAI:simulatedWhiteBalanceKelvin`

Plaintext prompt is not written by default. The CLI only embeds it when the user explicitly passes `--prompt-plaintext`.

## Validation plan

`image2dng validate output.dng` checks:

- required DNG tags are present;
- `PhotometricInterpretation` is LinearRaw;
- XMP parses as XML;
- synthetic provenance and simulated camera flag exist;
- black/white levels are sane;
- image dimensions match the decoded buffer;
- raw data byte count matches dimensions;
- MakerNote is absent;
- optional local smoke tools run when available.
