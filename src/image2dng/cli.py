from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from image2dng import __version__
from image2dng.api import Image2DNGError, convert
from image2dng.semantic_reaction import semantic_reaction_model_registry
from image2dng.semantic_scene import validate_semantic_scene
from image2dng.validate import validate_dng


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    if tokens and tokens[0] == "validate":
        args = build_validate_parser().parse_args(tokens[1:])
        return _run_validate(args)
    if tokens and tokens[0] == "validate-semantic":
        args = build_validate_semantic_parser().parse_args(tokens[1:])
        return _run_validate_semantic(args)
    if tokens and tokens[0] == "semantic-reactions":
        args = build_semantic_reactions_parser().parse_args(tokens[1:])
        return _run_semantic_reactions(args)
    args = build_generate_parser().parse_args(tokens)
    return _run_generate(args)


def build_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image2dng",
        description="Generate truthful synthetic LinearRaw DNG files from 16-bit images.",
        epilog=(
            "Subcommands: validate, validate-semantic, semantic-reactions. "
            "Run 'image2dng <subcommand> --help' for subcommand options."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("input", type=Path, help="input 16-bit TIFF/PNG")
    parser.add_argument("output", type=Path, help="output DNG path")
    parser.add_argument(
        "--input-space",
        choices=["srgb", "linear-rec709", "acescg", "xyz", "prophoto-rgb"],
        default="srgb",
    )
    parser.add_argument("--mode", choices=["linearraw", "cfa"], default="linearraw")
    parser.add_argument("--cfa-pattern", choices=["rggb", "bggr", "grbg", "gbrg"], default="rggb")
    parser.add_argument("--iso", type=int, default=100)
    parser.add_argument("--white-balance", type=float, default=6500.0)
    parser.add_argument("--shot-noise", type=float, default=0.0)
    parser.add_argument("--read-noise", type=float, default=0.0)
    parser.add_argument("--row-noise", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None, help="deterministic sensor-effect seed")
    parser.add_argument("--prompt-hash", default="")
    parser.add_argument("--scene-description", default="")
    parser.add_argument("--model-name", default="")
    parser.add_argument("--model-version", default="")
    parser.add_argument("--lighting", default="")
    parser.add_argument("--weather", default="")
    parser.add_argument(
        "--dng-layout",
        choices=["preview-subifd", "single-raw-ifd"],
        default="preview-subifd",
        help="DNG IFD layout to write",
    )
    parser.add_argument(
        "--prompt-plaintext",
        default=None,
        help="opt-in only: embed plaintext prompt in XMP",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output DNG",
    )
    return parser


def build_validate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="image2dng validate", description="Validate a DNG file.")
    parser.add_argument("dng", type=Path)
    parser.add_argument(
        "--no-smoke",
        action="store_true",
        help="skip optional external smoke tests",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write a structured validation report as JSON",
    )
    return parser


def build_semantic_reactions_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image2dng semantic-reactions",
        description="List semantic-to-RAW reaction model boundaries.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the semantic reaction model registry as JSON",
    )
    return parser


def build_validate_semantic_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image2dng validate-semantic",
        description="Validate an image2dng semantic scene sidecar.",
    )
    parser.add_argument("sidecar", type=Path)
    parser.add_argument(
        "--json",
        action="store_true",
        help="write a structured semantic scene validation report as JSON",
    )
    return parser


def _run_generate(args: argparse.Namespace) -> int:
    try:
        result = convert(
            input_path=args.input,
            output_path=args.output,
            input_space=args.input_space,
            mode=args.mode,
            cfa_pattern=args.cfa_pattern,
            iso=args.iso,
            white_balance_kelvin=args.white_balance,
            shot_noise=args.shot_noise,
            read_noise=args.read_noise,
            row_noise=args.row_noise,
            sensor_effect_seed=args.seed,
            prompt_hash=args.prompt_hash,
            prompt_plaintext=args.prompt_plaintext,
            scene_description=args.scene_description,
            model_name=args.model_name,
            model_version=args.model_version,
            lighting=args.lighting,
            weather=args.weather,
            dng_layout=args.dng_layout,
            overwrite=args.overwrite,
        )
    except Image2DNGError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"Wrote {result.output_path}")
    return 0


def _run_validate(args: argparse.Namespace) -> int:
    result = validate_dng(args.dng, run_smoke=not args.no_smoke)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        if result.ok:
            return 0
        return 2 if result.has_smoke_failure else 1

    for warning in result.warnings:
        print(f"warning: {warning}")
    for name, status in result.smoke_tests.items():
        print(f"smoke:{name}: {status}")
    if result.ok:
        print(f"ok: {result.path}")
        return 0
    for error in result.errors:
        print(f"error: {error}")
    return 2 if result.has_smoke_failure else 1


def _run_semantic_reactions(args: argparse.Namespace) -> int:
    registry = semantic_reaction_model_registry()
    if args.json:
        print(json.dumps(registry, indent=2, sort_keys=True))
        return 0

    for model_id in sorted(registry):
        entry = registry[model_id]
        print(
            f"{model_id}: {entry['status']}; "
            f"current_raw_value_effect={entry['current_raw_value_effect']}; "
            f"intended_raw_value_effect={entry['intended_raw_value_effect']}"
        )
        print(f"  scope: {entry['scope']}")
        print(f"  boundary: {entry['boundary']}")
    return 0


def _run_validate_semantic(args: argparse.Namespace) -> int:
    result = validate_semantic_scene(args.sidecar)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.ok else 1

    for warning in result.warnings:
        print(f"warning: {warning}")
    if result.ok:
        print(f"ok: {result.path}")
        return 0
    for error in result.errors:
        print(f"error: {error}")
    return 1
