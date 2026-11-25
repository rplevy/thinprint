from __future__ import annotations

import struct

import pytest

from thinprint.cli import main
from thinprint.core import ThinPrintError, build_mesh, read_canvas


def write_svg(tmp_path, body, root='viewBox="0 0 20 10"'):
    path = tmp_path / "art.svg"
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" {root}>{body}</svg>',
        encoding="utf-8",
    )
    return path


def test_canvas_uses_viewbox_and_physical_dimensions(tmp_path):
    svg = write_svg(
        tmp_path,
        '<rect x="2" y="2" width="4" height="4"/>',
        'viewBox="10 20 40 20" width="80mm" height="1in"',
    )
    canvas = read_canvas(svg)
    assert (canvas.x, canvas.y, canvas.width, canvas.height) == (10, 20, 40, 20)
    assert canvas.output_width == 80
    assert canvas.output_height == pytest.approx(25.4)


def test_builds_watertight_model_with_expected_size(tmp_path):
    svg = write_svg(tmp_path, '<rect x="2" y="2" width="5" height="4"/>')
    mesh = build_mesh(svg, base_thickness=1.0, emboss_height=0.5)
    assert mesh.is_watertight
    assert mesh.extents == pytest.approx([20, 10, 1.5])
    assert mesh.bounds[0] == pytest.approx([0, 0, 0])


def test_artwork_is_clipped_to_canvas(tmp_path):
    svg = write_svg(tmp_path, '<path d="M-5,-5 H25 V15 H-5 Z"/>')
    mesh = build_mesh(svg)
    assert mesh.is_watertight
    assert mesh.extents == pytest.approx([20, 10, 1.2])


def test_cli_writes_binary_stl(tmp_path, capsys):
    svg = write_svg(tmp_path, '<circle cx="10" cy="5" r="3"/>')
    output = tmp_path / "result.stl"
    assert main([str(svg), "-o", str(output)]) == 0
    data = output.read_bytes()
    triangle_count = struct.unpack("<I", data[80:84])[0]
    assert len(data) == 84 + triangle_count * 50
    assert "wrote" in capsys.readouterr().out


def test_rejects_empty_svg_without_partial_output(tmp_path, capsys):
    svg = write_svg(tmp_path, "")
    output = tmp_path / "result.stl"
    assert main([str(svg), "-o", str(output)]) == 1
    assert not output.exists()
    assert "error" in capsys.readouterr().err


def test_rejects_nonpositive_thickness(tmp_path):
    svg = write_svg(tmp_path, '<rect width="3" height="3"/>')
    with pytest.raises(ThinPrintError, match="must be positive"):
        build_mesh(svg, base_thickness=0)


def test_rejects_negative_simplification(tmp_path):
    svg = write_svg(tmp_path, '<rect width="3" height="3"/>')
    with pytest.raises(ThinPrintError, match="cannot be negative"):
        build_mesh(svg, simplify=-0.1)


def test_touching_traced_contours_can_be_joined(tmp_path):
    svg = write_svg(
        tmp_path,
        '<path d="M2,2 H6 V6 H2 Z M6,6 H10 V9 H6 Z"/>',
    )
    mesh = build_mesh(svg)
    assert not mesh.is_empty
    assert mesh.extents == pytest.approx([20, 10, 1.2])


def test_max_dimensions_scale_down_proportionally(tmp_path):
    svg = write_svg(tmp_path, '<rect width="20" height="10"/>')
    mesh = build_mesh(svg, max_width=13, max_height=7.5)
    assert mesh.extents == pytest.approx([13, 6.5, 1.2])


def test_max_dimensions_do_not_enlarge(tmp_path):
    svg = write_svg(tmp_path, '<rect width="20" height="10"/>')
    mesh = build_mesh(svg, max_width=65, max_height=75)
    assert mesh.extents == pytest.approx([20, 10, 1.2])


def test_cli_accepts_max_dimensions(tmp_path, capsys):
    svg = write_svg(tmp_path, '<rect width="20" height="10"/>')
    output = tmp_path / "scaled.stl"
    assert main(
        [
            str(svg),
            "--max-width",
            "13",
            "--max-height",
            "7.5",
            "-o",
            str(output),
        ]
    ) == 0
    assert output.exists()
    assert "13.000 x 6.500" in capsys.readouterr().out


def test_rejects_nonpositive_max_dimensions(tmp_path):
    svg = write_svg(tmp_path, '<rect width="20" height="10"/>')
    with pytest.raises(ThinPrintError, match="maximum width must be positive"):
        build_mesh(svg, max_width=0)
