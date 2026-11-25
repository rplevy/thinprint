"""Command-line interface for thinprint."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .core import ThinPrintError, convert


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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output = args.output or args.input.with_suffix(".stl")
    try:
        mesh = convert(
            args.input,
            output,
            base_thickness=args.base_thickness,
            emboss_height=args.emboss_height,
            simplify=args.simplify,
            max_width=args.max_width,
            max_height=args.max_height,
        )
    except ThinPrintError as exc:
        print(f"thinprint: error: {exc}", file=sys.stderr)
        return 1
    size = mesh.extents
    print(
        f"wrote {output} ({size[0]:.3f} x {size[1]:.3f} x {size[2]:.3f} mm, "
        f"{len(mesh.faces)} triangles)"
    )
    return 0
