
---

# Image‑to‑DNG RAW Generator – Current Status and Development Suggestions

## Project overview

* **Goal** – The `image2dng` tool is a command‑line interface (CLI) for converting 16‑bit TIFF/PNG or scene‑linear RGB images into “truthful” synthetic DNG files. It produces uncompressed 16‑bit `LinearRaw` DNGs, embeds AI provenance metadata in a custom XMP namespace and avoids spoofing camera maker notes.

* **Installation and usage** – Development installation uses `uv sync --extra dev`. Generation is invoked as `uv run image2dng input.tif output.dng` with options for input colour space, ISO, white balance, prompt hash and scene description. Validation runs `uv run image2dng validate output.dng` to check required DNG tags and XMP sanity.

* **Supported input spaces** – `srgb` (display‑referred), `linear-rec709` (scene‑linear Rec.709), `acescg`, or `xyz`; conversions map inputs to a virtual camera native space.  When `srgb` is used the tool applies the inverse sRGB opto‑electronic transfer function.  The `acescg` or `xyz` inputs are converted through an ACEScg→XYZ matrix followed by an XYZ→camera matrix.

* **Design choices** – The minimal viable product (MVP) intentionally does **not** impersonate a real camera.  Generated DNGs use `UniqueCameraModel = "Synthetic Camera v1"` and embed AI provenance in a custom XMP namespace to mark camera parameters as simulated.  The design document argues that LinearRaw DNG is the safest starting point because it is a valid DNG photometric interpretation that avoids having to simulate a colour filter array (CFA) demosaic.

* **Limitations** –  Currently the tool writes a single uncompressed LinearRaw IFD.  It does not implement CFA mosaics, shot/read noise models, preview or semantic‑mask IFDs and does not embed `DNGPrivateData` payloads.  The roadmap lists these enhancements: (1) CFA RGGB/BGGR mosaic mode; (2) shot/read noise and fixed‑pattern noise; (3) semantic mask IFD; (4) depth IFD; (5) `DNGPrivateData` structured payload; (6) Adobe DNG SDK compatibility testing.

## Code structure

The implementation is pure Python (PEP 517 project using `pyproject.toml`) with the following key modules:

| Module                | Purpose                                                                                                                                                                                                                                                                                                    | Notes                                                                                                                                                                                                                                 |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli.py`              | Defines the CLI.  Builds parsers for `generate` and `validate` sub‑commands and calls into the processing modules.                                                                                                                                                                                         | Validates positive ISO and white‑balance arguments before generation.                                                                                                                                                                 |
| `image_processing.py` | Loads 16‑bit TIFF/PNG via `tifffile` or `png` modules and normalises integer inputs to floating‑point; applies inverse sRGB OETF when needed; converts to camera native colour space; quantises to 16‑bit using black/white levels.                                                                        | Supports `srgb`, `linear-rec709`, `acescg` and `xyz` input spaces; missing support for HDR or other colour gamuts.                                                                                                                    |
| `models.py`           | Defines dataclasses for core RAW properties, camera profile and AI metadata.  `CoreRawModel.for_dimensions` populates derived properties like active area and crop size.                                                                                                                                   | Currently fixed to 16‑bit uncompressed LinearRaw; white/black levels per channel are constant for all pixels.  `CameraProfileModel` hard‑codes a generic colour matrix and uses CCT→AsShotNeutral conversion to derive white balance. |
| `dng_writer.py`       | Uses `tifffile.imwrite` to write the LinearRaw buffer plus an array of extratags for DNG metadata.  Generates XMP via `xmp.build_xmp_packet` and writes the result as tag 700.                                                                                                                             | Compression is always `None` (uncompressed).  A fixed list of required tags is written; there is no EXIF IFD or preview IFD.                                                                                                          |
| `xmp.py`              | Defines the custom XMP namespace `https://example.org/ns/xmp/ai/1.0/` and builds an XMP packet from the AI metadata model.                                                                                                                                                                                 | Currently writes `modelName`, `modelVersion`, `promptHash`, `sceneDescription`, `lighting`, `weather`, `cameraParametersAreSimulated`, `simulatedISO`, `simulatedWhiteBalanceKelvin` and optional plaintext prompt.                   |
| `validate.py`         | Provides a validation routine that reads a DNG via `tifffile`, checks the presence and validity of required tags, ensures geometry/bit‑depth correctness, parses and validates the XMP packet and (optionally) runs external smoke tests with `exiftool`, `dcraw`, `darktable-cli`, and `rawtherapee-cli`. | Focuses on LinearRaw; many DNG fields and compatibility checks are not yet implemented.                                                                                                                                               |

## Areas for improvement and development suggestions

### 1 – Broaden input and colour‑space support

* **Support 32‑bit floating‑point inputs** – The tool currently rejects integer inputs smaller than 16 bits and accepts only 16‑bit or floating‑point data.  Extending the loader to accept 12‑ or 14‑bit integer images (common in some pipelines) or 32‑bit EXR files will broaden applicability.

* **HDR encodings and PQ/HLG** – Many modern rendering systems produce HDR PNG or JPEG‑XL images.  Converting these to scene‑linear values requires handling OETF curves like PQ/HLG.  A future version could support an `--input-transfer` option to invert different tone‑mapping curves before linearisation.

* **Custom colour matrices** – `CameraProfileModel` currently uses a hard‑coded Rec. 709→XYZ matrix.  Real camera DNGs include calibrated `ColorMatrix1` and `AsShotNeutral` specific to a device.  Exposing an option such as `--color-matrix` and `--as-shot-neutral` would allow advanced users to supply their own transforms instead of the default synthetic camera matrix.

### 2 – Implement optional CFA mosaic mode

The roadmap’s top item is to generate CFA mosaics (e.g., RGGB) rather than full RGB LinearRaw.  To implement this:

1. **Add a `--mosaic` flag**: allow values like `none` (LinearRaw), `rggb`, `bggr`, etc.  Changing `samples_per_pixel` to 1 and specifying `CFAPattern`, `CFARepeatPatternDim` and `BlackLevelRepeatDim` in the DNG tags will be necessary.

2. **Generate a CFA pattern**: adapt the quantisation routine to output a single channel of pixel data with an RGGB pattern.  For each pixel location `(y,x)`, select the appropriate colour channel (R/G/B) from the camera‑native linear RGB and write it into the output buffer.

3. **Update DNG tags and XMP**: set `PhotometricInterpretation` to CFA, write `ColorFilterArray`, `CFAPlaneColor`, `CFARepeatPatternDim`, `BlackLevelRepeatDim` and optionally `DNGPrivateData` to describe the noise model.  The design report emphasises that choosing a CFA pattern requires decisions on Bayer pattern, aliasing, masked pixels and noise models.  Starting with a simple RGGB mosaic and no noise may be acceptable for a first version.

4. **Update the validator**: support verifying CFA DNGs by checking `PhotometricInterpretation` code `2` (CFA) and the presence of CFA‑related tags.  The existing `validate_dng` function checks only LinearRaw photometric values.

Implementing CFA generation will likely require more robust modelling of sensor noise and black‑level variation, discussed below.

### 3 – Model noise and fixed‑pattern effects

To increase realism, incorporate sensor noise models:

* **Shot and read noise** – Add functions to perturb the camera‑native linear RGB with Gaussian (read noise) and Poisson (shot noise) distributions.  Parameters could be user‑configurable (e.g., `--shot-noise=0.005` and `--read-noise=5` electrons).  Scale noise according to pixel signal; then quantise to 16‑bit.  The design report lists noise modelling as a roadmap item.

* **Fixed pattern noise and PRNU** – Many sensors exhibit row/column noise or photo‑response non‑uniformity (PRNU).  Simulate row‑dependent offsets and multiplicative pixel gains.  Black‑ and white‑level tags should reflect the resulting distribution.

* **Masked pixels and active area** – Real sensors include optical black regions.  Generating DNGs with a masked border would involve writing a larger raw buffer, updating `ActiveArea`, `DefaultCropOrigin` and `DefaultCropSize` tags, and marking the masked region in `MaskedAreas`.  This will prepare the code for more advanced semantic and depth channels later.

### 4 – Enhance metadata and semantics

* **XMP schema expansion** – The current XMP namespace defines basic provenance, model names and optional plain text prompts.  Future versions could include fields for `confidence`, `positivePrompt`, `negativePrompt`, `depthMapSha256`, etc.  A formal schema file (RDF/OWL) would allow other software to parse and validate the metadata.

* **EXIF and IPTC tags** – Many digital imaging tools still rely on EXIF fields for camera make, model, exposure time, aperture and ISO.  For synthetic images the actual values are simulated; however, populating these fields (with `Make = "Synthetic"`, `Model = "Synthetic Camera v1"`, `ISOSpeedRatings = simulated ISO`, `ExposureTime` = 1/100 or so) will improve compatibility with raw editors.  `dng_writer.py` should be extended to write an EXIF IFD containing the standard tags.

* **DNGPrivateData payload** – The design report suggests storing large structured metadata (e.g., depth maps, segmentation masks) in `DNGPrivateData` encoded as CBOR.  Implementing this will require designing a binary format and writing a top‑level tag (tag 700 in IFD0) referencing the private data block.  The CLI could accept paths to mask/depth files and embed them.

### 5 – Improve validation and compatibility testing

* **Comprehensive tag coverage** – The validator currently checks a limited set of tags.  Extend it to verify `ColorMatrix2`, `NoiseProfile`, `ActiveArea`, `MaskedAreas`, `DNGPrivateData` and CFA‑related tags when present.  Validate that per‑channel black/white levels are within 0–65535 and increasing.

* **Adobe DNG SDK** – Use the official DNG SDK to test whether generated files open without errors in Adobe products.  This step is on the roadmap.  Automate this in CI with the Windows or macOS build of the SDK.

* **Round‑trip tests with raw processors** – Besides darktable and RawTherapee smoke tests, load the generated DNG in Lightroom, Capture One or dcraw and check that white balance and colour matrix behave as expected.  Document any quirks in a compatibility matrix.

### 6 – Packaging and ecosystem integration

* **Library API** – Expose `image2dng` functionality as a Python function (e.g., `convert(input_path, output_path, **options)`) rather than only as a CLI.  This will allow downstream projects to integrate synthetic DNG generation.  `cli.py` can remain a thin wrapper around this API.

* **Pip distribution** – Prepare a `pyproject.toml` with correct dependencies (`tifffile`, `numpy`, `pypng`) and publish to PyPI.  Document installation instructions and versioning (SemVer).  Provide type hints across the code; the current modules already use type annotations and PEP 561 packaging should be considered.

* **CI/CD** – Set up GitHub Actions workflows to run unit tests (`pytest`), flake8/mypy static analysis, and build wheel distributions.  Add test images (small 16‑bit PNGs) to exercise the `build_linearraw_buffer` and `write_dng` routines.  Use the provided validator in CI to ensure generated DNGs pass.

### 7 – Documentation and user guidance

* **Detailed usage examples** – Expand the README with examples of converting sRGB, ACEScg and XYZ inputs, including explanation of how colour space affects output.  Add a section describing how to choose ISO and white balance parameters, along with typical ranges.

* **Design rationale** – Translate the design document into English and include it in the repository.  Many developers and users outside Chinese‑speaking regions will benefit from an accessible explanation.  Link to the official DNG specification and summarise key requirements (IFD layout, mandatory tags, allowed compression schemes, etc.).

* **Roadmap tracking** – Create GitHub issues for each roadmap item (CFA, noise model, semantic masks, DNG SDK testing).  Use milestones to plan releases and maintain transparency.

## Conclusion

The **Image‑to‑DNG RAW Generator** project lays a solid foundation for producing synthetic LinearRaw DNG files from 16‑bit images.  Its minimal design emphasises transparency (“Synthetic Camera” identifiers, AI provenance metadata) and correctness (strict validation of required tags).  To move towards a more complete synthetic RAW solution, development should expand input support, implement CFA mosaics and noise models, enrich metadata, broaden validation and compatibility tests, and improve packaging and documentation.  Following the suggestions above will help the project evolve into a versatile tool that meets the needs of researchers and developers working with AI‑generated images and DNG pipelines.
