# thinprint

`thinprint` turns filled SVG artwork into a printable STL: the SVG canvas
becomes a thin rectangular plate and its closed artwork is embossed above it.

## Install and use

Python 3.9 or newer is required.

```sh
python -m pip install .
thinprint logo.svg -o logo.stl
```

The defaults produce a 0.8 mm plate and a 0.4 mm relief. Both are configurable:

```sh
thinprint logo.svg --base-thickness 1.0 --emboss-height 0.6
```

To fit a printer's build area, set either or both maximum dimensions. The SVG
is scaled down uniformly, preserving its aspect ratio, and is never enlarged:

```sh
thinprint image.svg \
  --max-width 65 \
  --max-height 75 \
  --base-thickness 1.0 \
  --emboss-height 0.6 \
  --simplify 0.05 \
  -o image.stl
```

For example, a 100 × 200 mm SVG constrained to 65 × 75 mm becomes 37.5 × 75
mm. Thickness and emboss height are absolute millimetre values and are not
scaled.

Artwork is simplified to a 0.02 mm tolerance by default to keep traced SVGs
from producing multi-million-triangle files. Adjust the tolerance or disable it:

```sh
thinprint detailed.svg --simplify 0.01
thinprint detailed.svg --simplify 0
```

The output uses millimetres. Explicit SVG units (`mm`, `cm`, `in`, `px`, `pt`,
`pc`, and `q`) are converted to millimetres. If a `viewBox` has no explicit
physical width and height, one SVG user unit is treated as one millimetre.

## SVG guidance

- Use closed, filled vector shapes. Convert text to paths before exporting.
- The SVG `viewBox` (or `width` and `height`) defines the plate dimensions.
- Artwork is clipped to the SVG canvas.
- Raster images, open strokes, filters, clipping paths, and masks are not
  rasterized. Convert such artwork to filled paths first.

The STL is binary and the base and relief are Boolean-unioned into one
watertight mesh.
