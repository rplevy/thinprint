"""SVG to STL conversion."""

from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import numpy as np
from shapely.geometry import box
import trimesh


class ThinPrintError(Exception):
    """An input or geometry error which can be shown directly to a user."""


@dataclass(frozen=True)
class Canvas:
    x: float
    y: float
    width: float
    height: float
    output_width: float
    output_height: float


_LENGTH = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z]*)\s*$"
)
_MM_PER_UNIT = {
    "": 1.0,
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "px": 25.4 / 96.0,
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
    "q": 0.25,
}


def _length(value: str | None, name: str) -> float | None:
    if value is None:
        return None
    match = _LENGTH.match(value)
    if not match:
        raise ThinPrintError(f"invalid SVG {name}: {value!r}")
    unit = match.group(2).lower()
    if unit == "%" or unit not in _MM_PER_UNIT:
        raise ThinPrintError(f"unsupported unit in SVG {name}: {value!r}")
    result = float(match.group(1)) * _MM_PER_UNIT[unit]
    if result <= 0:
        raise ThinPrintError(f"SVG {name} must be positive")
    return result


def read_canvas(svg: Path) -> Canvas:
    """Read the root viewport and resolve output dimensions in millimetres."""
    try:
        root = ET.parse(svg).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ThinPrintError(f"cannot read SVG: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ThinPrintError("input root element is not <svg>")

    width = _length(root.get("width"), "width")
    height = _length(root.get("height"), "height")
    raw_viewbox = root.get("viewBox") or root.get("viewbox")
    if raw_viewbox:
        try:
            values = [float(v) for v in re.split(r"[\s,]+", raw_viewbox.strip())]
        except ValueError as exc:
            raise ThinPrintError(f"invalid SVG viewBox: {raw_viewbox!r}") from exc
        if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
            raise ThinPrintError(f"invalid SVG viewBox: {raw_viewbox!r}")
        x, y, view_width, view_height = values
        # Unitless dimensions and viewBox coordinates are interpreted as mm.
        output_width = width if width is not None else view_width
        output_height = height if height is not None else view_height
        return Canvas(x, y, view_width, view_height, output_width, output_height)

    if width is None or height is None:
        raise ThinPrintError("SVG needs a viewBox or positive width and height")
    # Without a viewBox, SVG user units are CSS pixels. Trimesh loads those
    # coordinates directly, so this viewport maps them to the physical size.
    raw_width = float(_LENGTH.match(root.get("width", ""))[1])
    raw_height = float(_LENGTH.match(root.get("height", ""))[1])
    return Canvas(0.0, 0.0, raw_width, raw_height, width, height)


def _svg_for_loader(svg: Path) -> io.BytesIO:
    """Convert basic SVG shape elements to paths understood by trimesh."""
    try:
        tree = ET.parse(svg)
    except (ET.ParseError, OSError) as exc:
        raise ThinPrintError(f"cannot read SVG: {exc}") from exc

    def number(element: ET.Element, attribute: str, default: float = 0.0) -> float:
        raw = element.get(attribute)
        if raw is None:
            return default
        match = _LENGTH.match(raw)
        if not match or match.group(2) not in ("", "px"):
            raise ThinPrintError(
                f"unsupported {element.tag.rsplit('}', 1)[-1]} {attribute}: {raw!r}"
            )
        return float(match.group(1))

    for element in tree.iter():
        name = element.tag.rsplit("}", 1)[-1]
        style = element.get("style", "").replace(" ", "").lower()
        if (
            element.get("fill", "").lower() == "none"
            or "fill:none" in style
            or element.get("display", "").lower() == "none"
            or "display:none" in style
        ):
            element.tag = element.tag.replace(name, "g")
            element.attrib.pop("d", None)
            continue
        d = None
        if name == "rect":
            x, y = number(element, "x"), number(element, "y")
            w, h = number(element, "width"), number(element, "height")
            if w > 0 and h > 0:
                d = f"M{x},{y} H{x + w} V{y + h} H{x} Z"
        elif name in ("circle", "ellipse"):
            cx, cy = number(element, "cx"), number(element, "cy")
            rx = number(element, "r") if name == "circle" else number(element, "rx")
            ry = number(element, "r") if name == "circle" else number(element, "ry")
            if rx > 0 and ry > 0:
                d = (
                    f"M{cx - rx},{cy} A{rx},{ry} 0 1,0 {cx + rx},{cy} "
                    f"A{rx},{ry} 0 1,0 {cx - rx},{cy} Z"
                )
        elif name == "polygon":
            points = element.get("points", "").strip()
            if points:
                d = f"M{points} Z"
        if d is not None:
            element.tag = element.tag.replace(name, "path")
            element.set("d", d)
    return io.BytesIO(ET.tostring(tree.getroot(), encoding="utf-8"))


def _artwork_mesh(
    svg: Path, canvas: Canvas, height: float, simplify: float
) -> trimesh.Trimesh:
    try:
        drawing = trimesh.load_path(_svg_for_loader(svg), file_type="svg")
    except Exception as exc:
        raise ThinPrintError(f"could not parse SVG geometry: {exc}") from exc
    if not isinstance(drawing, trimesh.path.Path2D):
        raise ThinPrintError("SVG did not produce two-dimensional paths")

    try:
        polygons = drawing.polygons_full
    except Exception as exc:
        raise ThinPrintError(f"could not resolve closed filled regions: {exc}") from exc
    if not polygons:
        raise ThinPrintError("SVG contains no closed geometry to emboss")

    sx = canvas.output_width / canvas.width
    sy = canvas.output_height / canvas.height
    meshes: list[trimesh.Trimesh] = []
    for polygon in polygons:
        polygon = polygon.intersection(
            box(
                canvas.x,
                canvas.y,
                canvas.x + canvas.width,
                canvas.y + canvas.height,
            )
        )
        if simplify > 0:
            # Simplify in SVG coordinates while bounding the displacement in
            # output millimetres, including non-uniform viewBox scaling.
            polygon = polygon.simplify(
                simplify / max(abs(sx), abs(sy)), preserve_topology=True
            )
        if polygon.is_empty or polygon.area <= 0:
            continue
        pieces = list(polygon.geoms) if polygon.geom_type == "MultiPolygon" else [polygon]
        extrusions = [
            trimesh.creation.extrude_polygon(piece, height, engine="earcut")
            for piece in pieces
            if piece.area > 0
        ]
        mesh = trimesh.util.concatenate(extrusions)
        # Curved paths can leave zero-volume sliver components at a repeated
        # closing point. They are not printable and upset solid booleans.
        solids = [part for part in mesh.split(only_watertight=False) if abs(part.volume) > 1e-10]
        if not solids:
            continue
        mesh = trimesh.util.concatenate(solids)
        # SVG path coordinates are retained by trimesh. Move the viewBox origin
        # to (0, 0), map it to the requested physical dimensions, and mirror Y
        # so the model uses the usual Cartesian orientation.
        matrix = np.array(
            [
                [sx, 0.0, 0.0, -canvas.x * sx],
                [0.0, -sy, 0.0, (canvas.y + canvas.height) * sy],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )
        mesh.apply_transform(matrix)
        meshes.append(mesh)
    if not meshes:
        raise ThinPrintError("SVG contains no non-empty closed geometry to emboss")
    return trimesh.util.concatenate(meshes)


def build_mesh(
    svg: str | Path,
    *,
    base_thickness: float = 0.8,
    emboss_height: float = 0.4,
    simplify: float = 0.02,
) -> trimesh.Trimesh:
    """Build a watertight embossed mesh from *svg*. Dimensions are millimetres."""
    source = Path(svg)
    if base_thickness <= 0 or emboss_height <= 0:
        raise ThinPrintError("base thickness and emboss height must be positive")
    if simplify < 0:
        raise ThinPrintError("simplification tolerance cannot be negative")
    canvas = read_canvas(source)

    base = trimesh.creation.box(
        extents=(canvas.output_width, canvas.output_height, base_thickness)
    )
    base.apply_translation(
        (canvas.output_width / 2, canvas.output_height / 2, base_thickness / 2)
    )
    # Let the relief overlap the plate. This makes the Boolean robust and
    # removes hidden coplanar faces from the final STL.
    artwork = _artwork_mesh(
        source, canvas, base_thickness + emboss_height, simplify
    )
    try:
        result = trimesh.boolean.union([base, artwork], engine="manifold")
    except Exception as exc:
        raise ThinPrintError(f"could not join base and emboss: {exc}") from exc
    if result is None or result.is_empty:
        raise ThinPrintError("joining base and emboss produced an empty model")
    result.remove_unreferenced_vertices()
    return result


def convert(
    svg: str | Path,
    output: str | Path,
    *,
    base_thickness: float = 0.8,
    emboss_height: float = 0.4,
    simplify: float = 0.02,
) -> trimesh.Trimesh:
    """Convert an SVG and atomically write a binary STL."""
    destination = Path(output)
    mesh = build_mesh(
        svg,
        base_thickness=base_thickness,
        emboss_height=emboss_height,
        simplify=simplify,
    )
    try:
        data = mesh.export(file_type="stl")
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)
    except OSError as exc:
        raise ThinPrintError(f"cannot write {destination}: {exc}") from exc
    return mesh
