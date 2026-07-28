# DNGPrivateData Payload Contract

This document is the import contract draft for `DNGPrivateData`. It describes how a future implementation may place compact structured provenance or semantic metadata in DNG private data. The project still does not write `DNGPrivateData`.

## Status

Status: proposal / implementation gate

Implemented today:

- XMP synthetic provenance.
- `image2dng.semantic_scene.v1` sidecar preservation and validation.
- Batch manifest / sample index records for semantic sidecars, producer metadata, and validation artifacts.

Not implemented today:

- `DNGPrivateData` tag writer.
- Embedded semantic sidecar payloads in DNG binaries.
- Embedded masks, depth maps, or large JSON payloads.

## Goal

`DNGPrivateData` may only preserve traceable metadata. It must not change RAW sample values, and it must not make a synthetic DNG look like a real camera MakerNote.

Suitable content:

- Semantic sidecar digest.
- Producer metadata digest.
- Schema id and schema version.
- Manifest-relative artifact references.
- Compact provenance summary.

Unsuitable content:

- Original RAW bytes.
- Previews, masks, depth maps, crops, overlays, or other large/private derived content.
- Real camera MakerNote spoofing.
- Host-specific absolute paths such as `Q:\`, `C:\`, UNC paths, or user names.

## v1 Payload Shape

The first payload should stay small and reviewable:

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

## Writer Gate

Before implementing a writer, finish these gates:

1. Confirm the DNG specification requirements for `DNGPrivateData` binary layout and reader tolerance.
2. Add a validator for schema, size, privacy flags, and digest fields when a payload exists.
3. Add fixtures proving that DNG output and validation do not regress when `DNGPrivateData` is absent.
4. Add an opt-in CLI/API flag; writing must stay disabled by default.
5. Verify behavior with Adobe DNG Converter, ExifTool, and at least one RAW processor.

## Size And Privacy Policy

- v1 payload target size should stay under 16 KiB.
- Larger data should remain external sidecars with digest references.
- The writer must reject payloads containing private paths, source pixels, EXIF dumps, or recoverable private content.

## Recommended Next Slice

The next safe implementation batch:

1. Add a payload builder that accepts sidecar paths and emits canonical JSON bytes.
2. Add a privacy scanner that rejects absolute paths and source-pixel-like artifacts.
3. Add tests without writing into DNG yet.
4. Evaluate an opt-in writer only after Adobe / RAW processor smoke tests pass.

