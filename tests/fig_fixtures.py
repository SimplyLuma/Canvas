# SPDX-License-Identifier: MPL-2.0
"""Synthetic `.fig` fixtures, one per feature, each with a reference SVG.

The SVG says what Figma draws for the fixture in terms a second, unrelated
renderer (librsvg) can draw, so a render of the imported document can be
compared with it pixel by pixel. Fixtures whose look SVG cannot express
(text shaping, blur radii) carry `svg=None` and are checked structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from fig_writer import (FigWriter, commands, gradient, guid, hexcolor, matrix, network, png, solid, vector)


@dataclass
class Fixture:
    name: str
    data: bytes
    size: tuple[int, int]          # render area, from the page origin
    svg: str | None
    tolerance: float = 1.0          # percent of pixels allowed to differ
    # (x, y, "#rrggbb"): colors Figma's definition puts at page points, for
    # paints SVG cannot express (angular and diamond gradients).
    probes: tuple = ()


def _frame(w: FigWriter, name: str, width: float, height: float, fill: str = "#ffffff", **extra):
    return w.add("FRAME", name, w.page, size=vector(width, height), transform=matrix(), fillPaints=[solid(fill)],
                 frameMaskDisabled=False, **extra)


def _svg(width: int, height: int, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">{body}</svg>')


def shapes() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Shapes", 400, 300)
    w.add("ROUNDED_RECTANGLE", "Card", frame, size=vector(120, 80), transform=matrix(20, 20),
          fillPaints=[solid("#3366cc")], rectangleCornerRadiiIndependent=True, rectangleTopLeftCornerRadius=0,
          rectangleTopRightCornerRadius=12, rectangleBottomRightCornerRadius=24, rectangleBottomLeftCornerRadius=36)
    w.add("ELLIPSE", "Dot", frame, size=vector(100, 60), transform=matrix(170, 30), fillPaints=[solid("#cc3355")])
    w.add("RECTANGLE", "Rotated", frame, size=vector(80, 40), transform=matrix(330, 20, 30),
          fillPaints=[solid("#22aa66")])
    w.add("LINE", "Rule", frame, size=vector(160, 0), transform=matrix(20, 150), strokeWeight=6,
          strokePaints=[solid("#222222")], strokeCap="ROUND", strokeAlign="CENTER")
    w.add("RECTANGLE", "Half", frame, size=vector(100, 100), transform=matrix(220, 140), opacity=0.5,
          fillPaints=[solid("#000000")])
    w.add("RECTANGLE", "Hidden", frame, size=vector(50, 50), transform=matrix(0, 0), visible=False,
          fillPaints=[solid("#ff00ff")])
    w.add("RECTANGLE", "Translucent fill", frame, size=vector(60, 60), transform=matrix(40, 200),
          fillPaints=[solid("#ff8800", 0.4)])
    a = math.cos(math.radians(30))
    b = math.sin(math.radians(30))
    body = ('<rect width="400" height="300" fill="#ffffff"/>'
            '<path d="M0 0 H108 A12 12 0 0 1 120 12 V56 A24 24 0 0 1 96 80 H36 A36 36 0 0 1 0 44 Z" '
            'transform="translate(20 20)" fill="#3366cc"/>'
            '<ellipse cx="220" cy="60" rx="50" ry="30" fill="#cc3355"/>'
            f'<rect width="80" height="40" fill="#22aa66" transform="matrix({a} {b} {-b} {a} 330 20)"/>'
            '<line x1="20" y1="150" x2="180" y2="150" stroke="#222222" stroke-width="6" stroke-linecap="round"/>'
            '<rect x="220" y="140" width="80" height="100" fill="#000000" fill-opacity="0.5"/>'
            '<rect x="300" y="140" width="20" height="100" fill="#000000" fill-opacity="0.5"/>'
            '<rect x="40" y="200" width="60" height="60" fill="#ff8800" fill-opacity="0.4"/>')
    return Fixture("shapes", w.build(), (400, 300), _svg(400, 300, body))


def polygons() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Polygons", 300, 140)
    # Figma's own geometry for a triangle fills the box; Canvas's parametric
    # polygon does not, so the importer must fall back to this outline.
    triangle = w.blob(commands(("M", 50, 0), ("L", 100, 100), ("L", 0, 100), ("Z",)))
    w.add("REGULAR_POLYGON", "Triangle", frame, size=vector(100, 100), transform=matrix(20, 20), count=3,
          fillPaints=[solid("#8844cc")], fillGeometry=[{"windingRule": "NONZERO", "commandsBlob": triangle}])
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        radius = 50 if index % 2 == 0 else 50 * 0.4
        points.append((50 + radius * math.cos(angle), 50 + radius * math.sin(angle)))
    star = w.blob(commands(("M", *points[0]), *[("L", *point) for point in points[1:]], ("Z",)))
    w.add("STAR", "Star", frame, size=vector(100, 100), transform=matrix(160, 20), count=5, starInnerScale=0.4,
          fillPaints=[solid("#ddaa00")], fillGeometry=[{"windingRule": "NONZERO", "commandsBlob": star}])
    body = ('<rect width="300" height="140" fill="#fff"/>'
            '<path d="M70 20 L120 120 L20 120 Z" fill="#8844cc"/>'
            '<path d="M' + " L".join(f"{160 + x} {20 + y}" for x, y in points) + ' Z" fill="#ddaa00"/>')
    return Fixture("polygons", w.build(), (300, 140), _svg(300, 140, body))


def strokes() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Strokes", 420, 200)
    for index, align in enumerate(("INSIDE", "CENTER", "OUTSIDE")):
        w.add("RECTANGLE", f"Stroke {align.lower()}", frame, size=vector(80, 60),
              transform=matrix(30 + index * 130, 30), fillPaints=[solid("#ccddee")],
              strokePaints=[solid("#223344")], strokeWeight=10, strokeAlign=align, strokeJoin="MITER")
    w.add("RECTANGLE", "Dashed", frame, size=vector(120, 50), transform=matrix(30, 130),
          strokePaints=[solid("#aa2222")], strokeWeight=4, strokeAlign="CENTER", dashPattern=[12, 8],
          strokeCap="NONE")
    w.add("FRAME", "Bottom border", frame, size=vector(160, 40), transform=matrix(220, 130),
          fillPaints=[solid("#eeeeee")], strokePaints=[solid("#111111")], strokeWeight=1, strokeAlign="INSIDE",
          borderStrokeWeightsIndependent=True, borderTopWeight=0, borderRightWeight=0, borderLeftWeight=0,
          borderBottomWeight=6, frameMaskDisabled=False)
    body = ('<rect width="420" height="200" fill="#fff"/>'
            '<rect x="30" y="30" width="80" height="60" fill="#ccddee"/>'
            '<rect x="35" y="35" width="70" height="50" fill="none" stroke="#223344" stroke-width="10"/>'
            '<rect x="160" y="30" width="80" height="60" fill="#ccddee" stroke="#223344" stroke-width="10"/>'
            '<rect x="285" y="25" width="90" height="70" fill="none" stroke="#223344" stroke-width="10"/>'
            '<rect x="290" y="30" width="80" height="60" fill="#ccddee"/>'
            '<rect x="30" y="130" width="120" height="50" fill="none" stroke="#aa2222" stroke-width="4" '
            'stroke-dasharray="12 8"/>'
            '<rect x="220" y="130" width="160" height="40" fill="#eeeeee"/>'
            '<rect x="220" y="164" width="160" height="6" fill="#111111"/>')
    return Fixture("strokes", w.build(), (420, 200), _svg(420, 200, body), tolerance=1.5)


def gradients() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Gradients", 460, 180)
    # Figma's gradient transform maps the layer's unit box to gradient space.
    diagonal = matrix(a=0.5, b=-0.5, c=0.5, d=0.5, tx=0.0, ty=0.5)
    w.add("RECTANGLE", "Linear", frame, size=vector(200, 100), transform=matrix(20, 20),
          fillPaints=[gradient("GRADIENT_LINEAR", [(0, "#ff0000"), (1, "#0000ff")], diagonal)])
    w.add("RECTANGLE", "Radial", frame, size=vector(160, 120), transform=matrix(260, 20),
          fillPaints=[gradient("GRADIENT_RADIAL", [(0, "#ffffff"), (0.5, "#ffcc00"), (1, "#003300")])])
    w.add("ELLIPSE", "Faded", frame, size=vector(100, 40), transform=matrix(40, 130),
          fillPaints=[gradient("GRADIENT_LINEAR", [(0, "#000000"), (1, "#00aa00")], opacity=0.5)])
    # Inverse of the diagonal transform: gradient space to the unit box.
    inverse = (1.0, 1.0, -1.0, 1.0, 0.5, -0.5)
    body = ('<defs>'
            f'<linearGradient id="l" gradientUnits="objectBoundingBox" x1="0" y1="0.5" x2="1" y2="0.5" '
            f'gradientTransform="matrix({inverse[0]} {inverse[1]} {inverse[2]} {inverse[3]} {inverse[4]} {inverse[5]})">'
            '<stop offset="0" stop-color="#ff0000"/><stop offset="1" stop-color="#0000ff"/></linearGradient>'
            '<radialGradient id="r" gradientUnits="objectBoundingBox" cx="0.5" cy="0.5" r="0.5">'
            '<stop offset="0" stop-color="#ffffff"/><stop offset="0.5" stop-color="#ffcc00"/>'
            '<stop offset="1" stop-color="#003300"/></radialGradient>'
            '<linearGradient id="f" x1="0" y1="0.5" x2="1" y2="0.5"><stop offset="0" stop-color="#000"/>'
            '<stop offset="1" stop-color="#00aa00"/></linearGradient>'
            '</defs><rect width="460" height="180" fill="#fff"/>'
            '<rect x="20" y="20" width="200" height="100" fill="url(#l)"/>'
            '<rect x="260" y="20" width="160" height="120" fill="url(#r)"/>'
            '<ellipse cx="90" cy="150" rx="50" ry="20" fill="url(#f)" fill-opacity="0.5"/>')
    return Fixture("gradients", w.build(), (460, 180), _svg(460, 180, body), tolerance=2.0)


def conic() -> Fixture:
    """Angular and diamond gradients, checked at points against Figma's definition.

    In gradient space an angular gradient starts along +x and turns clockwise
    (y down); a diamond gradient's value is 2(|x - 0.5| + |y - 0.5|). The
    layer's unit box maps to gradient space through the paint transform.
    """
    w = FigWriter()
    frame = _frame(w, "Conic", 320, 140)
    stops = [(0, "#ff0000"), (0.25, "#00ff00"), (0.5, "#0000ff"), (0.75, "#ffff00"), (1, "#ff0000")]
    w.add("RECTANGLE", "Angular", frame, size=vector(100, 100), transform=matrix(20, 20),
          fillPaints=[gradient("GRADIENT_ANGULAR", stops)])
    # Figma's default orientation: the unit box rotated a quarter turn, so the start points down.
    quarter = matrix(a=0.0, b=-1.0, c=1.0, d=0.0, tx=0.0, ty=1.0)
    w.add("RECTANGLE", "Angular turned", frame, size=vector(100, 100), transform=matrix(130, 20),
          fillPaints=[gradient("GRADIENT_ANGULAR", stops, quarter)])
    w.add("RECTANGLE", "Diamond", frame, size=vector(80, 120), transform=matrix(235, 10),
          fillPaints=[gradient("GRADIENT_DIAMOND", [(0, "#000000"), (1, "#ffffff")])])
    probes = (
        (70 + 30, 70 + 1, "#ff0000"),    # just clockwise of the start (+x)
        (70, 70 + 30, "#00ff00"),        # a quarter turn: straight down
        (70 - 30, 70, "#0000ff"),        # half a turn: left
        (70, 70 - 30, "#ffff00"),        # three quarters: up
        (180 - 1, 70 + 30, "#ff0000"),   # turned: the start points down
        (180 - 30, 70, "#00ff00"),       # a quarter turn later: left
        (275, 70, "#000000"),            # diamond center
        (275 + 20, 70, "#808080"),       # halfway to the right edge
        (275, 70 + 30, "#808080"),       # halfway to the bottom edge
        (275 + 10, 70 + 15, "#808080"),  # |x| + |y| is half the reach
    )
    return Fixture("conic", w.build(), (320, 140), None, probes=probes)


def vectors() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Vectors", 420, 200)
    # A donut: two loops, even-odd, from a vector network.
    ring = network(
        [(0, 50), (50, 0), (100, 50), (50, 100), (25, 50), (50, 25), (75, 50), (50, 75)],
        [(0, 1, 0, -27.6, -27.6, 0), (1, 2, 27.6, 0, 0, -27.6), (2, 3, 0, 27.6, 27.6, 0), (3, 0, -27.6, 0, 0, 27.6),
         (4, 5, 0, -13.8, -13.8, 0), (5, 6, 13.8, 0, 0, -13.8), (6, 7, 0, 13.8, 13.8, 0), (7, 4, -13.8, 0, 0, 13.8)],
        [("ODD", [[0, 1, 2, 3], [4, 5, 6, 7]])])
    w.add("VECTOR", "Donut", frame, size=vector(100, 100), transform=matrix(20, 20),
          fillPaints=[solid("#0077aa")], vectorData={"vectorNetworkBlob": w.blob(ring),
                                                     "normalizedSize": vector(100, 100)})
    # An open stroked zigzag.
    zigzag = network([(0, 40), (30, 0), (60, 40), (90, 0)], [(0, 1), (1, 2), (2, 3)], [])
    w.add("VECTOR", "Zigzag", frame, size=vector(90, 40), transform=matrix(150, 30),
          strokePaints=[solid("#aa3300")], strokeWeight=5, strokeJoin="ROUND", strokeCap="ROUND",
          vectorData={"vectorNetworkBlob": w.blob(zigzag), "normalizedSize": vector(90, 40)})
    # A triangle with rounded vertices (vertex corner radius) and a normalized size twice as big.
    tri = network([(0, 160), (80, 0), (160, 160)], [(0, 1), (1, 2), (2, 0)], [("NONZERO", [[0, 1, 2]])])
    w.add("VECTOR", "Rounded triangle", frame, size=vector(80, 80), transform=matrix(290, 20),
          fillPaints=[solid("#44aa44")], cornerRadius=10,
          vectorData={"vectorNetworkBlob": w.blob(tri), "normalizedSize": vector(160, 160)})
    # Boolean: a square minus a circle, from Figma's computed geometry.
    result = w.blob(commands(("M", 0, 0), ("L", 80, 0), ("L", 80, 80), ("L", 0, 80), ("Z",),
                             ("M", 60, 40), ("C", 60, 51.05, 51.05, 60, 40, 60), ("C", 28.95, 60, 20, 51.05, 20, 40),
                             ("C", 20, 28.95, 28.95, 20, 40, 20), ("C", 51.05, 20, 60, 28.95, 60, 40), ("Z",)))
    boolean = w.add("BOOLEAN_OPERATION", "Punched", frame, size=vector(80, 80), transform=matrix(150, 100),
                    booleanOperation="SUBTRACT", fillPaints=[solid("#553399")],
                    fillGeometry=[{"windingRule": "ODD", "commandsBlob": result}])
    w.add("RECTANGLE", "Square", boolean, size=vector(80, 80), transform=matrix(), fillPaints=[solid("#999999")])
    w.add("ELLIPSE", "Hole", boolean, size=vector(40, 40), transform=matrix(20, 20), fillPaints=[solid("#999999")])
    # Rounded triangle by hand: fillets of radius 10 at each vertex (80 x 80 box).
    body = ('<rect width="420" height="200" fill="#fff"/>'
            '<path fill-rule="evenodd" fill="#0077aa" transform="translate(20 20)" '
            'd="M0 50 C0 22.4 22.4 0 50 0 C77.6 0 100 22.4 100 50 C100 77.6 77.6 100 50 100 C22.4 100 0 77.6 0 50 Z '
            'M25 50 C25 36.2 36.2 25 50 25 C63.8 25 75 36.2 75 50 C75 63.8 63.8 75 50 75 C36.2 75 25 63.8 25 50 Z"/>'
            '<polyline points="150,70 180,30 210,70 240,30" fill="none" stroke="#aa3300" stroke-width="5" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
            '<path fill-rule="evenodd" fill="#553399" transform="translate(150 100)" '
            'd="M0 0 H80 V80 H0 Z M60 40 A20 20 0 1 0 20 40 A20 20 0 1 0 60 40 Z"/>'
            + _rounded_triangle_svg())
    return Fixture("vectors", w.build(), (420, 200), _svg(420, 200, body), tolerance=1.5)


def _rounded_triangle_svg() -> str:
    points = [(290, 100), (330, 20), (370, 100)]
    radius = 10
    parts = []
    corners = []
    for index, (px, py) in enumerate(points):
        ax, ay = points[index - 1]
        bx, by = points[(index + 1) % 3]
        la, lb = math.hypot(ax - px, ay - py), math.hypot(bx - px, by - py)
        ux, uy, vx, vy = (ax - px) / la, (ay - py) / la, (bx - px) / lb, (by - py) / lb
        angle = math.acos(ux * vx + uy * vy)
        tangent = radius / math.tan(angle / 2)
        corners.append(((px + ux * tangent, py + uy * tangent), (px + vx * tangent, py + vy * tangent)))
    start = corners[0][1]
    parts.append(f"M{start[0]} {start[1]}")
    for index in (1, 2, 0):
        t1, t2 = corners[index]
        parts.append(f"L{t1[0]} {t1[1]} A{radius} {radius} 0 0 1 {t2[0]} {t2[1]}")
    return f'<path d="{" ".join(parts)} Z" fill="#44aa44"/>'


def _checker(size: int = 16) -> bytes:
    def pixel(x, y):
        if x < size // 2 and y < size // 2:
            return (230, 40, 40, 255)
        if x >= size // 2 and y >= size // 2:
            return (40, 90, 220, 255)
        return (250, 250, 250, 255)
    return png(size, size, pixel)


def images() -> Fixture:
    import base64
    w = FigWriter()
    frame = _frame(w, "Images", 460, 160)
    wide = png(40, 20, lambda x, y: (40, 160, 90, 255) if x < 20 else (240, 200, 30, 255))
    wide_hash = w.image(wide)
    checker = _checker()
    checker_hash = w.image(checker)

    def image_paint(image_hash, mode, width, height, **extra):
        return {"type": "IMAGE", "opacity": 1.0, "visible": True, "blendMode": "NORMAL",
                "image": {"hash": image_hash, "name": "fixture"}, "imageScaleMode": mode, "transform": matrix(),
                "scale": 1.0, "originalImageWidth": width, "originalImageHeight": height, **extra}

    w.add("RECTANGLE", "Fill", frame, size=vector(80, 80), transform=matrix(20, 20),
          fillPaints=[image_paint(wide_hash, "FILL", 40, 20)])
    w.add("RECTANGLE", "Fit", frame, size=vector(80, 80), transform=matrix(120, 20),
          fillPaints=[solid("#dddddd"), image_paint(wide_hash, "FIT", 40, 20)])
    w.add("ROUNDED_RECTANGLE", "Tile", frame, size=vector(96, 64), transform=matrix(220, 20), cornerRadius=16,
          fillPaints=[image_paint(checker_hash, "TILE", 16, 16, scale=2.0)])
    # Crop: the image's unit square is placed at 25%..125% of the box horizontally.
    w.add("RECTANGLE", "Crop", frame, size=vector(80, 80), transform=matrix(340, 20),
          fillPaints=[image_paint(wide_hash, "STRETCH", 40, 20,
                                  transform=matrix(a=1.0, b=0.0, c=0.0, d=1.0, tx=-0.25, ty=0.0))])
    w.add("RECTANGLE", "Photo", frame, size=vector(80, 40), transform=matrix(20, 110),
          fillPaints=[image_paint(wide_hash, "FILL", 40, 20)])
    wide_uri = "data:image/png;base64," + base64.b64encode(wide).decode()
    checker_uri = "data:image/png;base64," + base64.b64encode(checker).decode()
    body = ('<defs><pattern id="tile" patternUnits="userSpaceOnUse" x="220" y="20" width="32" height="32">'
            f'<image href="{checker_uri}" width="32" height="32" style="image-rendering:pixelated"/></pattern>'
            '<clipPath id="c1"><rect x="20" y="20" width="80" height="80"/></clipPath>'
            '<clipPath id="c4"><rect x="340" y="20" width="80" height="80"/></clipPath></defs>'
            '<rect width="460" height="160" fill="#fff"/>'
            f'<image href="{wide_uri}" x="-20" y="20" width="160" height="80" clip-path="url(#c1)" '
            'preserveAspectRatio="none"/>'
            '<rect x="120" y="20" width="80" height="80" fill="#dddddd"/>'
            f'<image href="{wide_uri}" x="120" y="40" width="80" height="40" preserveAspectRatio="none"/>'
            '<rect x="220" y="20" width="96" height="64" rx="16" fill="url(#tile)"/>'
            f'<image href="{wide_uri}" x="360" y="20" width="80" height="80" clip-path="url(#c4)" '
            'preserveAspectRatio="none"/>'
            f'<image href="{wide_uri}" x="20" y="110" width="80" height="40" preserveAspectRatio="none"/>')
    return Fixture("images", w.build(), (460, 160), _svg(460, 160, body), tolerance=3.0)


def clipping() -> Fixture:
    w = FigWriter()
    clip = _frame(w, "Clips", 120, 120, "#eeeeee")
    w.add("ELLIPSE", "Overflow", clip, size=vector(100, 100), transform=matrix(60, 60), fillPaints=[solid("#dd4444")])
    open_frame = w.add("FRAME", "Shows overflow", w.page, size=vector(120, 120), transform=matrix(160, 0),
                       fillPaints=[solid("#eeeeee")], frameMaskDisabled=True)
    w.add("ELLIPSE", "Overflow", open_frame, size=vector(100, 100), transform=matrix(60, 60),
          fillPaints=[solid("#4444dd")])
    group = w.add("FRAME", "Masked", w.page, size=vector(120, 120), transform=matrix(320, 0), resizeToFit=True)
    w.add("ELLIPSE", "Mask", group, size=vector(120, 120), transform=matrix(), mask=True, maskType="OUTLINE",
          fillPaints=[solid("#000000")])
    w.add("RECTANGLE", "Masked stripe", group, size=vector(120, 40), transform=matrix(0, 40),
          fillPaints=[solid("#22aa22")])
    w.add("RECTANGLE", "Masked block", group, size=vector(60, 120), transform=matrix(60, 0),
          fillPaints=[solid("#2222aa")])
    body = ('<defs><clipPath id="m"><circle cx="380" cy="60" r="60"/></clipPath></defs>'
            '<rect width="480" height="180" fill="#f5f5f5"/>'
            '<rect width="120" height="120" fill="#eeeeee"/>'
            '<path d="M60 60 H120 V110 A50 50 0 0 1 110 120 H60 V110 A50 50 0 0 1 60 110 Z" fill="none"/>'
            '<clipPath id="c"><rect width="120" height="120"/></clipPath>'
            '<ellipse cx="110" cy="110" rx="50" ry="50" fill="#dd4444" clip-path="url(#c)"/>'
            '<rect x="160" width="120" height="120" fill="#eeeeee"/>'
            '<ellipse cx="270" cy="110" rx="50" ry="50" fill="#4444dd"/>'
            '<g clip-path="url(#m)"><rect x="320" y="40" width="120" height="40" fill="#22aa22"/>'
            '<rect x="380" y="0" width="60" height="120" fill="#2222aa"/></g>')
    return Fixture("clipping", w.build(), (480, 180), _svg(480, 180, body), tolerance=1.5)


def groups() -> Fixture:
    w = FigWriter()
    outer = w.add("FRAME", "Outer group", w.page, size=vector(200, 200), transform=matrix(20, 20), resizeToFit=True)
    inner = w.add("FRAME", "Rotated group", outer, size=vector(100, 100), transform=matrix(100, 0, 45),
                  resizeToFit=True)
    w.add("RECTANGLE", "A", inner, size=vector(50, 50), transform=matrix(), fillPaints=[solid("#aa0000")])
    w.add("RECTANGLE", "B", inner, size=vector(50, 50), transform=matrix(50, 50), fillPaints=[solid("#00aa00")])
    w.add("ROUNDED_RECTANGLE", "Flipped", outer, size=vector(60, 40), transform=matrix(a=-1, b=0, c=0, d=1, tx=80, ty=120),
          fillPaints=[gradient("GRADIENT_LINEAR", [(0, "#000000"), (1, "#ffffff")])],
          cornerRadius=0)
    c, s = math.cos(math.radians(45)), math.sin(math.radians(45))
    body = ('<defs><linearGradient id="g" x1="0" y1="0.5" x2="1" y2="0.5"><stop offset="0" stop-color="#000"/>'
            '<stop offset="1" stop-color="#fff"/></linearGradient></defs>'
            '<rect width="260" height="220" fill="#f5f5f5"/>'
            f'<g transform="translate(20 20) matrix({c} {s} {-s} {c} 100 0)">'
            '<rect width="50" height="50" fill="#aa0000"/><rect x="50" y="50" width="50" height="50" fill="#00aa00"/></g>'
            '<g transform="translate(20 20) matrix(-1 0 0 1 80 120)"><rect width="60" height="40" fill="url(#g)"/></g>')
    return Fixture("groups", w.build(), (260, 220), _svg(260, 220, body), tolerance=1.5)


def effects() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Effects", 360, 160, "#f0f0f0")
    w.add("ROUNDED_RECTANGLE", "Shadowed", frame, size=vector(100, 80), transform=matrix(30, 30), cornerRadius=12,
          fillPaints=[solid("#ffffff")],
          effects=[{"type": "DROP_SHADOW", "color": hexcolor("#000000", 0.25), "offset": vector(0, 8),
                    "radius": 24, "spread": 0, "visible": True, "blendMode": "NORMAL"}])
    w.add("RECTANGLE", "Inset", frame, size=vector(100, 80), transform=matrix(160, 30),
          fillPaints=[solid("#ddeeff")],
          effects=[{"type": "INNER_SHADOW", "color": hexcolor("#003366", 0.5), "offset": vector(4, 4),
                    "radius": 8, "spread": 0, "visible": True, "blendMode": "NORMAL"}])
    w.add("ELLIPSE", "Blurred", frame, size=vector(60, 60), transform=matrix(280, 40), fillPaints=[solid("#cc0066")],
          effects=[{"type": "FOREGROUND_BLUR", "radius": 8, "visible": True}])
    w.add("RECTANGLE", "Frosted", frame, size=vector(60, 40), transform=matrix(280, 110),
          fillPaints=[solid("#ffffff", 0.5)], effects=[{"type": "BACKGROUND_BLUR", "radius": 20, "visible": True}])
    return Fixture("effects", w.build(), (360, 160), None)


def arrows() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Arrows", 300, 100)
    arrow = network([(0, 20, 1), (100, 20, 2)], [(0, 1)], [])
    w.add("VECTOR", "Arrow", frame, size=vector(100, 0), transform=matrix(20, 30),
          strokePaints=[solid("#000000")], strokeWeight=2, strokeCap="NONE",
          vectorData={"vectorNetworkBlob": w.blob(arrow), "normalizedSize": vector(100, 40),
                      "styleOverrideTable": [{"styleID": 1, "strokeCap": "CIRCLE_FILLED"},
                                             {"styleID": 2, "strokeCap": "ARROW_EQUILATERAL"}]})
    w.add("LINE", "Both ends", frame, size=vector(100, 0), transform=matrix(150, 30), strokeWeight=2,
          strokePaints=[solid("#000000")], strokeCap="ARROW_LINES")
    return Fixture("arrows", w.build(), (300, 100), None)


def text() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Text", 400, 200)
    characters = "Mixed bold and colored text"
    ids = [0] * 6 + [1] * 4 + [0] * 5 + [2] * 7 + [0] * 5
    w.add("TEXT", "Mixed", frame, size=vector(360, 40), transform=matrix(20, 20), fontSize=24,
          fontName={"family": "Figtree", "style": "Regular", "postscript": "Figtree-Regular"},
          lineHeight={"value": 1.5, "units": "RAW"}, letterSpacing={"value": 2, "units": "PERCENT"},
          textAlignHorizontal="LEFT", textAlignVertical="TOP", textAutoResize="NONE",
          fillPaints=[solid("#222222")],
          textData={"characters": characters, "characterStyleIDs": ids, "styleOverrideTable": [
              {"styleID": 1, "fontName": {"family": "Figtree", "style": "Bold", "postscript": ""}},
              {"styleID": 2, "fillPaints": [solid("#cc0033")], "textDecoration": "UNDERLINE", "fontSize": 28}]})
    w.add("TEXT", "Centered", frame, size=vector(200, 80), transform=matrix(100, 90, 0), fontSize=16,
          fontName={"family": "Missing Grotesk", "style": "Semi Bold Italic", "postscript": ""},
          lineHeight={"value": 20, "units": "PIXELS"}, textAlignHorizontal="CENTER", textAlignVertical="BOTTOM",
          textAutoResize="HEIGHT", textCase="UPPER", textTruncation="ENDING", maxLines=2,
          fillPaints=[solid("#0055aa")], textData={"characters": "Second paragraph\nwraps here"})
    return Fixture("text", w.build(), (400, 200), None)


def components() -> Fixture:
    w = FigWriter()
    frame = _frame(w, "Components", 400, 220)
    symbol = w.add("SYMBOL", "Button", frame, size=vector(120, 40), transform=matrix(20, 20),
                   fillPaints=[solid("#3355ff")], cornerRadius=8, frameMaskDisabled=False)
    label_def = guid(900)
    icon_def = guid(901)
    label = w.add("TEXT", "Label", symbol, size=vector(100, 20), transform=matrix(10, 10), fontSize=14,
                  fontName={"family": "Figtree", "style": "Regular", "postscript": ""},
                  fillPaints=[solid("#ffffff")], textData={"characters": "Default"},
                  componentPropRefs=[{"defID": label_def, "componentPropNodeField": "TEXT_DATA"}])
    icon = w.add("ELLIPSE", "Icon", symbol, size=vector(10, 10), transform=matrix(104, 15),
                 fillPaints=[solid("#ffffff")],
                 componentPropRefs=[{"defID": icon_def, "componentPropNodeField": "VISIBLE"}])
    other = w.add("SYMBOL", "Badge", frame, size=vector(40, 40), transform=matrix(300, 20),
                  fillPaints=[solid("#ff9900")], cornerRadius=20)
    w.add("INSTANCE", "Plain", frame, size=vector(120, 40), transform=matrix(20, 80),
          fillPaints=[solid("#3355ff")], cornerRadius=8, symbolData={"symbolID": symbol, "symbolOverrides": []})
    w.add("INSTANCE", "Overridden", frame, size=vector(120, 40), transform=matrix(160, 80),
          fillPaints=[solid("#3355ff")], cornerRadius=8,
          symbolData={"symbolID": symbol, "symbolOverrides": [
              {"guidPath": {"guids": [label]}, "fillPaints": [solid("#ffff00")]}]},
          componentPropAssignments=[
              {"defID": label_def, "value": {"textValue": {"characters": "Buy now"}}},
              {"defID": icon_def, "value": {"boolValue": False}}])
    # A wider instance: Figma's derived data places the icon at the new right edge.
    w.add("INSTANCE", "Stretched", frame, size=vector(200, 40), transform=matrix(20, 140),
          fillPaints=[solid("#3355ff")], cornerRadius=8,
          symbolData={"symbolID": symbol, "symbolOverrides": []},
          derivedSymbolData=[{"guidPath": {"guids": [icon]}, "transform": matrix(184, 15), "size": vector(10, 10)}])
    # A nested instance swapped to another component by an outer override.
    holder = w.add("SYMBOL", "Holder", w.page, size=vector(60, 60), transform=matrix(500, 0),
                   fillPaints=[solid("#eeeeee")])
    nested = w.add("INSTANCE", "Nested", holder, size=vector(120, 40), transform=matrix(0, 10),
                   symbolData={"symbolID": symbol, "symbolOverrides": []}, fillPaints=[solid("#3355ff")])
    w.add("INSTANCE", "Holder instance", frame, size=vector(60, 60), transform=matrix(300, 140),
          fillPaints=[solid("#eeeeee")],
          symbolData={"symbolID": holder, "symbolOverrides": [
              {"guidPath": {"guids": [nested]}, "overriddenSymbolID": other}]})
    w.add("INSTANCE", "Missing library component", frame, size=vector(40, 40), transform=matrix(240, 140),
          symbolData={"symbolID": guid(99999, 77), "symbolOverrides": []})
    return Fixture("components", w.build(), (400, 220), None)


def pages() -> Fixture:
    w = FigWriter(page_name="Cover")
    w.add("RECTANGLE", "On cover", w.page, size=vector(10, 10), transform=matrix(), fillPaints=[solid("#000000")])
    second = w.add("CANVAS", "Details", w.document, backgroundColor=hexcolor("#222222"), backgroundEnabled=True)
    frame = w.add("FRAME", "Auto layout", second, size=vector(200, 60), transform=matrix(), stackMode="HORIZONTAL",
                  stackSpacing=8, fillPaints=[solid("#ffffff")])
    w.add("RECTANGLE", "Pinned", frame, size=vector(40, 40), transform=matrix(10, 10), horizontalConstraint="MAX",
          verticalConstraint="STRETCH", fillPaints=[solid("#ff0000")], blendMode="MULTIPLY")
    w.add("STICKY", "Sticky", second, size=vector(100, 100), transform=matrix(300, 0),
          fillGeometry=[{"windingRule": "NONZERO", "commandsBlob": w.blob(commands(
              ("M", 0, 0), ("L", 100, 0), ("L", 100, 100), ("L", 0, 100), ("Z",)))}],
          fillPaints=[solid("#ffe066")], textData={"characters": "Remember"})
    w.add("FUTURE_THING", "Hologram", second, size=vector(10, 10), transform=matrix(500, 0))
    internal = w.add("CANVAS", "Internal Only Canvas", w.document, internalOnly=True)
    w.add("RECTANGLE", "Library thing", internal, size=vector(10, 10), transform=matrix())
    return Fixture("pages", w.build(), (10, 10), None)


def exports() -> Fixture:
    """Figma export rows on a frame and a layer, including a format Canvas doesn't write."""
    w = FigWriter()

    def row(image_type, suffix="", constraint="CONTENT_SCALE", value=1.0):
        return {"suffix": suffix, "imageType": image_type, "constraint": {"type": constraint, "value": value}}

    frame = _frame(w, "Card", 200, 100, exportSettings=[
        row("PNG"), row("PNG", "@2x", value=2.0), row("JPEG", "-thumb", "CONTENT_WIDTH", 512.0),
        row("SVG"), row("PDF"), row("BMP")])
    w.add("ELLIPSE", "icons/dot", frame, size=vector(40, 40), transform=matrix(10, 10), fillPaints=[solid("#cc3355")],
          exportSettings=[row("PNG", "", "CONTENT_HEIGHT", 120.0)])
    w.add("RECTANGLE", "Plain", frame, size=vector(20, 20), transform=matrix(100, 10), fillPaints=[solid("#000000")])
    return Fixture("exports", w.build(), (200, 100), None)


def blending() -> Fixture:
    """Blend modes, paint stacks, several strokes, text strokes and blur.

    The probes are the compositing spec's own numbers (see
    `blend_reference`), so the fixture says what Figma draws without needing a
    stored image.
    """
    from blend_reference import BACKDROP, SOURCE, composite, hex_color

    back, front = hex_color(BACKDROP), hex_color(SOURCE)
    w = FigWriter()
    frame = _frame(w, "Blending", 480, 380)

    # A layer's own blend mode, over a plain layer under it.
    w.add("RECTANGLE", "Backdrop", frame, size=vector(100, 60), transform=matrix(20, 20),
          fillPaints=[solid(back)])
    w.add("RECTANGLE", "Multiply", frame, size=vector(100, 60), transform=matrix(20, 20),
          fillPaints=[solid(front)], blendMode="MULTIPLY")

    # Two fills in one layer, the upper one with its own blend mode.
    w.add("RECTANGLE", "Two fills", frame, size=vector(100, 60), transform=matrix(140, 20),
          fillPaints=[solid(back), solid(front, blendMode="SCREEN")])

    # A group with a blend mode: its layers blend among themselves, then the
    # group blends with what is under it.
    w.add("RECTANGLE", "Under group", frame, size=vector(100, 60), transform=matrix(260, 20),
          fillPaints=[solid(back)])
    group = w.add("FRAME", "Difference group", frame, size=vector(100, 60), transform=matrix(260, 20),
                  resizeToFit=True, blendMode="DIFFERENCE")
    w.add("RECTANGLE", "Ground", group, size=vector(100, 60), transform=matrix(), fillPaints=[solid("#ffffff")])
    w.add("RECTANGLE", "Over", group, size=vector(100, 60), transform=matrix(), fillPaints=[solid(front)])

    # Several strokes on one layer, the upper one with a blend mode.
    w.add("RECTANGLE", "Two strokes", frame, size=vector(100, 60), transform=matrix(20, 120),
          fillPaints=[], strokePaints=[solid(back), solid(front, blendMode="MULTIPLY")],
          strokeWeight=16, strokeAlign="INSIDE", strokeJoin="MITER")

    # Text with two fills and a stroke of its own.
    w.add("TEXT", "Outlined text", frame, size=vector(200, 48), transform=matrix(140, 120), fontSize=32,
          fontName={"family": "Figtree", "style": "Bold", "postscript": "Figtree-Bold"},
          textAlignHorizontal="LEFT", textAlignVertical="TOP", textAutoResize="NONE",
          fillPaints=[solid("#ffcc00"), solid(front, blendMode="OVERLAY")],
          strokePaints=[solid("#102040")], strokeWeight=2, strokeAlign="OUTSIDE",
          textData={"characters": "Halo"})

    # Background blur over two colors, and a mode Canvas approximates.
    w.add("RECTANGLE", "Dark half", frame, size=vector(60, 60), transform=matrix(340, 120),
          fillPaints=[solid("#112233")])
    w.add("RECTANGLE", "Light half", frame, size=vector(60, 60), transform=matrix(400, 120),
          fillPaints=[solid("#eeddcc")])
    w.add("RECTANGLE", "Frosted", frame, size=vector(80, 40), transform=matrix(360, 130),
          fillPaints=[solid("#ffffff", 0.35)],
          effects=[{"type": "BACKGROUND_BLUR", "radius": 18, "visible": True}])
    w.add("RECTANGLE", "Linear dodge", frame, size=vector(60, 40), transform=matrix(20, 200),
          fillPaints=[solid(front)], blendMode="LINEAR_DODGE")

    # A shadow on a group: cast by the two overlapping rectangles in it
    # together, so it never falls on one of them, only outside them both.
    shadowed = w.add("FRAME", "Shadowed group", frame, size=vector(60, 60), transform=matrix(120, 195),
                     resizeToFit=True,
                     effects=[{"type": "DROP_SHADOW", "color": hexcolor("#000000", 1.0), "offset": vector(-20, -10),
                               "radius": 0, "spread": 0, "visible": True, "blendMode": "NORMAL",
                               "showShadowBehindNode": True}])
    w.add("RECTANGLE", "Lower block", shadowed, size=vector(40, 50), transform=matrix(),
          fillPaints=[solid("#ffffff")])
    w.add("RECTANGLE", "Upper block", shadowed, size=vector(40, 50), transform=matrix(20, 10),
          fillPaints=[solid("#ffffff")])

    # Two see-through layers with the same shadow, one of which the shadow is
    # cut out of ("Show behind transparent areas", off).
    for index, behind in enumerate((True, False)):
        w.add("RECTANGLE", f"Glass {'behind' if behind else 'knockout'}", frame,
              size=vector(60, 40), transform=matrix(260 + index * 80, 200),
              fillPaints=[solid("#ffffff", 0.2)],
              effects=[{"type": "DROP_SHADOW", "color": hexcolor("#000000", 1.0), "offset": vector(0, 0),
                        "radius": 0, "spread": 0, "visible": True, "blendMode": "NORMAL",
                        "showShadowBehindNode": behind}])

    # A blur that grows along its own line (Figma's progressive blur): this
    # layer's left edge stays sharp and its right edge smears.
    w.add("RECTANGLE", "Ramp", frame, size=vector(200, 40), transform=matrix(140, 320),
          fillPaints=[solid("#000000")],
          effects=[{"type": "FOREGROUND_BLUR", "radius": 40, "visible": True, "blurOpType": "PROGRESSIVE",
                    "startRadius": 0.0, "startOffset": vector(0.0, 0.5), "endOffset": vector(1.0, 0.5)}])

    # Smoothed corners (squircles).
    w.add("ROUNDED_RECTANGLE", "Squircle", frame, size=vector(80, 60), transform=matrix(20, 245),
          fillPaints=[solid("#3366cc")], cornerRadius=12, cornerSmoothing=0.6)

    # A stroke painted with a four-cornered gradient: no SVG paint server
    # draws one, so the exporter masks the band and draws the paint through it.
    w.add("ELLIPSE", "Angular stroke", frame, size=vector(70, 70), transform=matrix(300, 250),
          fillPaints=[], strokePaints=[gradient("GRADIENT_ANGULAR", [(0, "#ff0055"), (0.5, "#00aa88"),
                                                                     (1, "#ff0055")])],
          strokeWeight=10, strokeAlign="CENTER")

    # A leaning layer: Figma stores the shear in its transform.
    w.add("FRAME", "Leaning", frame, size=vector(60, 60),
          transform=matrix(a=1.0, b=0.0, c=0.5, d=1.0, tx=380, ty=20),
          fillPaints=[solid("#cc6600")], frameMaskDisabled=True)

    probes = (
        # The lean moves the layer right as it goes down.
        (465, 75, "#cc6600"),
        (385, 75, "#ffffff"),
        # The group's shadow falls outside its layers, never on one of them.
        (110, 215, "#000000"),
        (130, 215, "#ffffff"),
        # A see-through layer over its own shadow: the shadow shows through it
        # (a 20% layer casts a 20% shadow, as Figma does), and with "show
        # behind transparent areas" off nothing shows through at all.
        (290, 220, "#d6d6d6"),
        (370, 220, "#ffffff"),
        (70, 50, hex_color(composite("multiply", BACKDROP, SOURCE, 1.0))),
        (190, 50, hex_color(composite("screen", BACKDROP, SOURCE, 1.0))),
        (310, 50, hex_color(composite("difference", BACKDROP, SOURCE, 1.0))),
        (26, 150, hex_color(composite("multiply", BACKDROP, SOURCE, 1.0))),
    )
    return Fixture("blending", w.build(), (480, 380), None, probes=probes)


ALL = (shapes, polygons, strokes, gradients, conic, vectors, images, clipping, groups, effects, arrows, text,
       components, pages, exports, blending)
RENDERED = (shapes, polygons, strokes, gradients, vectors, images, clipping, groups)


def stress(nodes: int = 5000, *, images_mb: int = 0) -> bytes:
    """A large file: many frames of mixed layers, optionally with filler images."""
    w = FigWriter()
    import os
    shared_symbol = None
    per_frame = 25
    frames = max(1, nodes // per_frame)
    vector_blob = w.blob(network([(0, 0), (20, 0), (20, 20), (0, 20)], [(0, 1), (1, 2), (2, 3), (3, 0)],
                                 [("NONZERO", [[0, 1, 2, 3]])]))
    for index in range(frames):
        frame = w.add("FRAME", f"Screen {index}", w.page, size=vector(390, 844),
                      transform=matrix((index % 40) * 420, (index // 40) * 900), fillPaints=[solid("#ffffff")],
                      stackMode="VERTICAL")
        if shared_symbol is None:
            shared_symbol = w.add("SYMBOL", "Row", frame, size=vector(350, 44), transform=matrix(20, 20),
                                  fillPaints=[solid("#f0f0f0")], cornerRadius=6)
            w.add("TEXT", "Title", shared_symbol, size=vector(300, 20), transform=matrix(12, 12), fontSize=15,
                  fontName={"family": "Figtree", "style": "Medium", "postscript": ""},
                  fillPaints=[solid("#111111")], textData={"characters": "Row title"})
        for row in range(per_frame - 5):
            kind = row % 5
            y = 80 + row * 36
            if kind == 0:
                w.add("INSTANCE", f"Row {row}", frame, size=vector(350, 44), transform=matrix(20, y),
                      symbolData={"symbolID": shared_symbol, "symbolOverrides": []},
                      fillPaints=[solid("#f0f0f0")])
            elif kind == 1:
                w.add("TEXT", f"Label {row}", frame, size=vector(300, 20), transform=matrix(20, y), fontSize=14,
                      fontName={"family": "Figtree", "style": "Regular", "postscript": ""},
                      fillPaints=[solid("#333333")], textData={"characters": f"Item {index}.{row}",
                                                               "characterStyleIDs": [0, 0, 0, 0, 1],
                                                               "styleOverrideTable": [{"styleID": 1, "fontSize": 18}]})
            elif kind == 2:
                w.add("VECTOR", f"Icon {row}", frame, size=vector(20, 20), transform=matrix(330, y),
                      strokePaints=[solid("#666666")], strokeWeight=2,
                      vectorData={"vectorNetworkBlob": vector_blob, "normalizedSize": vector(20, 20)})
            elif kind == 3:
                w.add("ROUNDED_RECTANGLE", f"Chip {row}", frame, size=vector(80, 24), transform=matrix(20, y),
                      cornerRadius=12, fillPaints=[gradient("GRADIENT_LINEAR", [(0, "#66aaff"), (1, "#3366cc")])],
                      effects=[{"type": "DROP_SHADOW", "color": hexcolor("#000000", 0.2), "offset": vector(0, 2),
                                "radius": 4, "visible": True}])
            else:
                group = w.add("FRAME", f"Group {row}", frame, size=vector(60, 20), transform=matrix(120, y),
                              resizeToFit=True)
                w.add("ELLIPSE", "Dot", group, size=vector(20, 20), transform=matrix(), fillPaints=[solid("#ff6600")])
                w.add("RECTANGLE", "Bar", group, size=vector(40, 8), transform=matrix(20, 6),
                      fillPaints=[solid("#999999")])
    if images_mb:
        # Incompressible filler images, as photos are.
        chunk = 1 << 20
        for index in range(images_mb):
            data = b"\x89PNG\r\n\x1a\n" + os.urandom(chunk - 8)
            digest = w.image(data)
            w.add("RECTANGLE", f"Photo {index}", w.page, size=vector(400, 300), transform=matrix(-500, index * 320),
                  fillPaints=[{"type": "IMAGE", "image": {"hash": digest, "name": "photo"}, "imageScaleMode": "FILL",
                               "opacity": 1.0, "visible": True, "transform": matrix(), "scale": 1.0,
                               "originalImageWidth": 400, "originalImageHeight": 300}])
    return w.build(compression="zstd")
