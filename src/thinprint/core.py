"""SVG to STL conversion."""

from __future__ import annotations

from dataclasses import dataclass
import io
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import manifold3d
import numpy as np
from shapely.geometry import Point, box
from shapely.geometry.polygon import orient
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


def _artwork_solid(
    svg: Path, canvas: Canvas, height: float, simplify: float
) -> manifold3d.Manifold:
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
    # Keep relief walls microscopically inside the plate walls. Exact contact
    # at a canvas edge creates coplanar vertical faces during the Boolean and
    # can yield four-face edges in otherwise valid traced artwork.
    inset_mm = min(0.001, canvas.output_width / 10000, canvas.output_height / 10000)
    inset_x = inset_mm / abs(sx)
    inset_y = inset_mm / abs(sy)
    contours: list[np.ndarray] = []
    for polygon in polygons:
        polygon = polygon.intersection(
            box(
                canvas.x + inset_x,
                canvas.y + inset_y,
                canvas.x + canvas.width - inset_x,
                canvas.y + canvas.height - inset_y,
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
        for piece in pieces:
            if piece.geom_type != "Polygon" or piece.area <= 0:
                continue
            # Positive winding for shells and negative winding for holes lets
            # Clipper resolve touching and overlapping traced SVG contours
            # before extrusion, guaranteeing a manifold intermediate solid.
            piece = orient(piece, sign=1.0)
            rings = [piece.exterior, *piece.interiors]
            for ring in rings:
                points = np.asarray(ring.coords[:-1], dtype=np.float64).copy()
                points[:, 0] = (points[:, 0] - canvas.x) * sx
                points[:, 1] = (canvas.y + canvas.height - points[:, 1]) * sy
                # Mirroring SVG's downward Y axis reverses winding; reverse
                # each contour so shells remain positive and holes negative.
                contours.append(points[::-1])
    if not contours:
        raise ThinPrintError("SVG contains no non-empty closed geometry to emboss")

    try:
        return manifold3d.CrossSection(
            contours, manifold3d.FillRule.Positive
        ).extrude(height)
    except Exception as exc:
        raise ThinPrintError(f"could not extrude SVG geometry: {exc}") from exc


def _validate_dimensions(
    base_thickness: float,
    emboss_height: float,
    simplify: float,
    max_width: float | None,
    max_height: float | None,
) -> None:
    if base_thickness <= 0 or emboss_height <= 0:
        raise ThinPrintError("base thickness and emboss height must be positive")
    if simplify < 0:
        raise ThinPrintError("simplification tolerance cannot be negative")
    if max_width is not None and max_width <= 0:
        raise ThinPrintError("maximum width must be positive")
    if max_height is not None and max_height <= 0:
        raise ThinPrintError("maximum height must be positive")


def _fit_canvas(
    canvas: Canvas, max_width: float | None, max_height: float | None
) -> Canvas:
    scale = min(
        1.0,
        max_width / canvas.output_width if max_width is not None else 1.0,
        max_height / canvas.output_height if max_height is not None else 1.0,
    )
    if scale == 1.0:
        return canvas
    return Canvas(
        canvas.x,
        canvas.y,
        canvas.width,
        canvas.height,
        canvas.output_width * scale,
        canvas.output_height * scale,
    )


def _solid_to_mesh(solid: manifold3d.Manifold) -> trimesh.Trimesh:
    data = solid.to_mesh()
    result = trimesh.Trimesh(
        vertices=data.vert_properties[:, :3], faces=data.tri_verts, process=False
    )
    if result.is_empty:
        raise ThinPrintError("solid operation produced an empty model")
    result.remove_unreferenced_vertices()
    return result


def _build_solid(
    svg: str | Path,
    *,
    base_thickness: float = 0.8,
    emboss_height: float = 0.4,
    simplify: float = 0.02,
    max_width: float | None = None,
    max_height: float | None = None,
) -> tuple[manifold3d.Manifold, Canvas]:
    source = Path(svg)
    _validate_dimensions(
        base_thickness, emboss_height, simplify, max_width, max_height
    )
    canvas = _fit_canvas(read_canvas(source), max_width, max_height)

    base = manifold3d.Manifold.cube(
        (canvas.output_width, canvas.output_height, base_thickness)
    )
    # Let the relief overlap the top of the plate slightly. Starting it at the
    # plate bottom would create a large coplanar surface, which can leave
    # non-manifold seams in complex Boolean results.
    overlap = min(0.01, base_thickness / 10.0, emboss_height / 10.0)
    artwork = _artwork_solid(
        source, canvas, emboss_height + overlap, simplify
    ).translate((0.0, 0.0, base_thickness - overlap))
    try:
        # Keeping the detailed operand first avoids pinch-edge triangulations
        # observed when a complex traced relief is unioned into a simple box.
        return artwork + base, canvas
    except Exception as exc:
        raise ThinPrintError(f"could not join base and emboss: {exc}") from exc


def build_mesh(
    svg: str | Path,
    *,
    base_thickness: float = 0.8,
    emboss_height: float = 0.4,
    simplify: float = 0.02,
    max_width: float | None = None,
    max_height: float | None = None,
) -> trimesh.Trimesh:
    """Build a printable embossed mesh from *svg*. Dimensions are millimetres."""
    solid, _ = _build_solid(
        svg,
        base_thickness=base_thickness,
        emboss_height=emboss_height,
        simplify=simplify,
        max_width=max_width,
        max_height=max_height,
    )
    return _solid_to_mesh(solid)


def _jigsaw_piece_count(value: float) -> int:
    if not math.isfinite(value) or value <= 0 or value >= 1:
        raise ThinPrintError("jigsaw cut must be a fraction between 0 and 1")
    reciprocal = 1.0 / value
    count = round(reciprocal)
    if count < 2 or not math.isclose(reciprocal, count, rel_tol=1e-7, abs_tol=1e-7):
        raise ThinPrintError(
            "jigsaw cut must be a unit fraction such as 0.25 or 0.125"
        )
    return count


def _jigsaw_layout(count: int, width: float, height: float) -> tuple[int, int]:
    """Return rows and columns whose physical cells are closest to square."""
    candidates = []
    for rows in range(1, count + 1):
        if count % rows == 0:
            columns = count // rows
            cell_aspect = (width / columns) / (height / rows)
            candidates.append(
                (round(abs(math.log(cell_aspect)), 12), abs(rows - columns), rows, columns)
            )
    _, _, rows, columns = min(candidates)
    return rows, columns


def _jigsaw_footprints(
    width: float, height: float, count: int, clearance: float
) -> tuple[int, int, list]:
    if clearance < 0:
        raise ThinPrintError("jigsaw clearance cannot be negative")
    rows, columns = _jigsaw_layout(count, width, height)
    cell_width = width / columns
    cell_height = height / rows
    radius = min(cell_width, cell_height) * 0.18
    if radius <= clearance:
        raise ThinPrintError("jigsaw pieces are too small for the requested clearance")

    # Extend outer cutter edges beyond the model to avoid coplanar Boolean
    # faces. Intersections with the model restore the exact outer dimensions.
    margin = 0.1
    pieces = []
    for row in range(rows):
        y_top = height - row * cell_height
        y_bottom = height - (row + 1) * cell_height
        for column in range(columns):
            x_left = column * cell_width
            x_right = (column + 1) * cell_width
            pieces.append(
                box(
                    -margin if column == 0 else x_left,
                    -margin if row == rows - 1 else y_bottom,
                    width + margin if column == columns - 1 else x_right,
                    height + margin if row == 0 else y_top,
                )
            )

    def index(row: int, column: int) -> int:
        return row * columns + column

    # A circle centred on a shared edge contributes one semicircle to the tab
    # owner. Subtracting a slightly larger circle from its neighbour creates a
    # matching socket with printable clearance.
    for row in range(rows):
        center_y = height - (row + 0.5) * cell_height
        for boundary in range(1, columns):
            center_x = boundary * cell_width
            tab = Point(center_x, center_y).buffer(radius, resolution=16)
            socket = Point(center_x, center_y).buffer(
                radius + clearance, resolution=16
            )
            left, right = index(row, boundary - 1), index(row, boundary)
            tab_owner, socket_owner = (
                (left, right) if (row + boundary) % 2 == 0 else (right, left)
            )
            pieces[tab_owner] = pieces[tab_owner].union(tab)
            pieces[socket_owner] = pieces[socket_owner].difference(socket)

    for boundary in range(1, rows):
        center_y = height - boundary * cell_height
        for column in range(columns):
            center_x = (column + 0.5) * cell_width
            tab = Point(center_x, center_y).buffer(radius, resolution=16)
            socket = Point(center_x, center_y).buffer(
                radius + clearance, resolution=16
            )
            above, below = index(boundary - 1, column), index(boundary, column)
            tab_owner, socket_owner = (
                (above, below) if (boundary + column) % 2 == 0 else (below, above)
            )
            pieces[tab_owner] = pieces[tab_owner].union(tab)
            pieces[socket_owner] = pieces[socket_owner].difference(socket)

    return rows, columns, pieces


def _footprint_solid(geometry, height: float) -> manifold3d.Manifold:
    polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    contours = []
    for polygon in polygons:
        if polygon.geom_type != "Polygon" or polygon.is_empty:
            continue
        polygon = orient(polygon, sign=1.0)
        contours.append(np.asarray(polygon.exterior.coords[:-1], dtype=np.float64))
        contours.extend(
            np.asarray(ring.coords[:-1], dtype=np.float64)
            for ring in polygon.interiors
        )
    if not contours:
        raise ThinPrintError("jigsaw operation produced an empty piece")
    return manifold3d.CrossSection(
        contours, manifold3d.FillRule.Positive
    ).extrude(height)


def build_jigsaw_meshes(
    svg: str | Path,
    jigsaw_cut: float,
    *,
    base_thickness: float = 0.8,
    emboss_height: float = 0.4,
    simplify: float = 0.02,
    max_width: float | None = None,
    max_height: float | None = None,
    jigsaw_clearance: float = 0.15,
) -> list[trimesh.Trimesh]:
    """Build separate, interlocking pieces in top-to-bottom row-major order."""
    count = _jigsaw_piece_count(jigsaw_cut)
    solid, canvas = _build_solid(
        svg,
        base_thickness=base_thickness,
        emboss_height=emboss_height,
        simplify=simplify,
        max_width=max_width,
        max_height=max_height,
    )
    _, _, footprints = _jigsaw_footprints(
        canvas.output_width, canvas.output_height, count, jigsaw_clearance
    )
    total_height = base_thickness + emboss_height
    meshes = []
    for footprint in footprints:
        cutter = _footprint_solid(footprint, total_height + 0.2).translate(
            (0.0, 0.0, -0.1)
        )
        try:
            mesh = _solid_to_mesh(solid ^ cutter)
        except Exception as exc:
            raise ThinPrintError(f"could not cut jigsaw piece: {exc}") from exc
        # Individual files are easier for slicers to place when each piece is
        # located at its own origin rather than at its full-image coordinates.
        mesh.apply_translation((-mesh.bounds[0][0], -mesh.bounds[0][1], 0.0))
        meshes.append(mesh)
    return meshes


def jigsaw_output_paths(output: str | Path, count: int) -> list[Path]:
    destination = Path(output)
    stem = destination.with_suffix("") if destination.suffix.lower() == ".stl" else destination
    digits = max(2, len(str(count)))
    return [
        stem.with_name(f"{stem.name}-{number:0{digits}d}.stl")
        for number in range(1, count + 1)
    ]


def convert_jigsaw(
    svg: str | Path,
    output: str | Path,
    jigsaw_cut: float,
    **kwargs,
) -> list[tuple[Path, trimesh.Trimesh]]:
    """Build and atomically write each jigsaw piece as a binary STL."""
    meshes = build_jigsaw_meshes(svg, jigsaw_cut, **kwargs)
    paths = jigsaw_output_paths(output, len(meshes))
    temporary_paths = [path.with_name(path.name + ".tmp") for path in paths]
    try:
        for temporary, mesh in zip(temporary_paths, meshes):
            temporary.write_bytes(mesh.export(file_type="stl"))
        for temporary, destination in zip(temporary_paths, paths):
            temporary.replace(destination)
    except OSError as exc:
        for temporary in temporary_paths:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise ThinPrintError(f"cannot write jigsaw pieces: {exc}") from exc
    return list(zip(paths, meshes))


def convert(
    svg: str | Path,
    output: str | Path,
    *,
    base_thickness: float = 0.8,
    emboss_height: float = 0.4,
    simplify: float = 0.02,
    max_width: float | None = None,
    max_height: float | None = None,
) -> trimesh.Trimesh:
    """Convert an SVG and atomically write a binary STL."""
    destination = Path(output)
    mesh = build_mesh(
        svg,
        base_thickness=base_thickness,
        emboss_height=emboss_height,
        simplify=simplify,
        max_width=max_width,
        max_height=max_height,
    )
    try:
        data = mesh.export(file_type="stl")
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)
    except OSError as exc:
        raise ThinPrintError(f"cannot write {destination}: {exc}") from exc
    return mesh
