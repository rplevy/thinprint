"""Command-line interface for thinprint."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .core import ThinPrintError, convert, convert_jigsaw


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="thinprint", description="Turn SVG artwork into a thin embossed STL."
    )
    result.add_argument("input", type=Path, help="input SVG file")
    result.add_argument("-o", "--output", type=Path, help="output STL (default: input.stl)")
    result.add_argument(
        "--base-thickness", type=float, default=0.8, metavar="MM", help="plate thickness (default: 0.8)"
    )
    result.add_argument(
        "--emboss-height", type=float, default=0.4, metavar="MM", help="relief height (default: 0.4)"
    )
    result.add_argument(
        "--max-width",
        type=float,
        metavar="MM",
        help="maximum model width; preserves aspect ratio",
    )
    result.add_argument(
        "--max-height",
        type=float,
        metavar="MM",
        help="maximum model height; preserves aspect ratio",
    )
    result.add_argument(
        "--simplify",
        type=float,
        default=0.02,
        metavar="MM",
        help="path simplification tolerance; 0 disables it (default: 0.02)",
    )
    result.add_argument(
        "--jigsaw-cut",
        type=float,
        metavar="FRACTION",
        help="split into interlocking files (0.25 = 4 pieces, 0.125 = 8)",
    )
    result.add_argument(
        "--jigsaw-clearance",
        type=float,
        default=0.15,
        metavar="MM",
        help="extra socket clearance for jigsaw tabs (default: 0.15)",
    )
    result.add_argument(
        "--max-piece-width",
        type=float,
        metavar="MM",
        help="choose a jigsaw layout whose pieces do not exceed this width",
    )
    result.add_argument(
        "--max-piece-height",
        type=float,
        metavar="MM",
        help="choose a jigsaw layout whose pieces do not exceed this height",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output = args.output or args.input.with_suffix(".stl")
    try:
        options = dict(
            base_thickness=args.base_thickness,
            emboss_height=args.emboss_height,
            simplify=args.simplify,
            max_width=args.max_width,
            max_height=args.max_height,
        )
        jigsaw_enabled = (
            args.jigsaw_cut is not None
            or args.max_piece_width is not None
            or args.max_piece_height is not None
        )
        if jigsaw_enabled:
            pieces = convert_jigsaw(
                args.input,
                output,
                args.jigsaw_cut,
                jigsaw_clearance=args.jigsaw_clearance,
                max_piece_width=args.max_piece_width,
                max_piece_height=args.max_piece_height,
                **options,
            )
        else:
            mesh = convert(args.input, output, **options)
    except ThinPrintError as exc:
        print(f"thinprint: error: {exc}", file=sys.stderr)
        return 1
    if jigsaw_enabled:
        print(f"wrote {len(pieces)} interlocking pieces:")
        for path, piece in pieces:
            size = piece.extents
            print(
                f"  {path} ({size[0]:.3f} x {size[1]:.3f} x {size[2]:.3f} mm, "
                f"{len(piece.faces)} triangles)"
            )
    else:
        size = mesh.extents
        print(
            f"wrote {output} ({size[0]:.3f} x {size[1]:.3f} x {size[2]:.3f} mm, "
            f"{len(mesh.faces)} triangles)"
        )
    return 0
