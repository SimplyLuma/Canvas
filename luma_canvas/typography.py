# SPDX-License-Identifier: MPL-2.0
"""Text layout: one Pango layout for drawing, hit testing and sizing a text layer.

Type settings live in a text layer's `metadata` (schema 1). Every key is
optional; older documents' `leading` (percent), `tracking` (per mille) and
`align` are still read.

| Key | Values |
| --- | --- |
| `font_family`, `font_size`, `font_weight`, `italic` | family, px, 100..1000, bool |
| `line_height` | `{"unit": "auto" | "px" | "percent", "value": n}` |
| `letter_spacing` | `{"unit": "px" | "percent", "value": n}` |
| `paragraph_spacing`, `paragraph_indent` | px |
| `align_h` | `left`, `center`, `right`, `justify` |
| `align_v` | `top`, `middle`, `bottom` |
| `decoration` | `none`, `underline`, `strikethrough` |
| `case` | `none`, `upper`, `lower`, `title`, `small_caps` |
| `sizing` | `auto_width`, `auto_height`, `fixed` |
| `list_style` | `none`, `bullets`, `numbers` |
| `truncate`, `max_lines` | bool, int or null |
| `vertical_trim` | `standard`, `cap_height` (stored; drawn as standard) |
| `hanging_punctuation` | bool (stored) |
| `features` | `{"liga": true, "tnum": false, ...}` OpenType features |
| `ranges` | `[{"start", "end", ...}]`: runs of the text (character offsets) with their own `font_family`, `font_weight`, `italic`, `font_size`, `fill`, `decoration`, `letter_spacing`, `case` or `features` (imported mixed styles) |

Lists prefix each paragraph for display; `TextLayout.display_index` and
`source_index` convert caret positions between the stored text and what is
drawn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Pango", "1.0")
from gi.repository import Pango  # noqa: E402

TYPE_DEFAULTS: dict[str, Any] = {
    "font_family": "Figtree", "font_size": 16, "font_weight": 400, "italic": False,
    "line_height": {"unit": "auto"}, "letter_spacing": {"unit": "percent", "value": 0},
    "paragraph_spacing": 0, "paragraph_indent": 0, "align_h": "left", "align_v": "top",
    "decoration": "none", "case": "none", "sizing": "fixed", "list_style": "none",
    "truncate": False, "max_lines": None, "vertical_trim": "standard",
    "hanging_punctuation": False, "features": {},
}
ALIGN_H = {"left", "center", "right", "justify"}
ALIGN_V = {"top", "middle", "bottom"}
DECORATIONS = {"none", "underline", "strikethrough"}
CASES = {"none", "upper", "lower", "title", "small_caps"}
SIZING = {"auto_width", "auto_height", "fixed"}
LIST_STYLES = {"none", "bullets", "numbers"}


def type_settings(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Every type setting for a text layer, legacy keys translated."""
    metadata = metadata or {}
    settings = {key: metadata.get(key, value) for key, value in TYPE_DEFAULTS.items()}
    if "line_height" not in metadata and metadata.get("leading"):
        settings["line_height"] = {"unit": "percent", "value": float(metadata["leading"])}
    if "letter_spacing" not in metadata and metadata.get("tracking"):
        settings["letter_spacing"] = {"unit": "percent", "value": float(metadata["tracking"]) / 10}
    if "align_h" not in metadata and metadata.get("align") in ALIGN_H:
        settings["align_h"] = metadata["align"]
    return settings


@dataclass
class TextLayout:
    layout: Pango.Layout
    display: str
    offset_y: float
    width: float
    height: float
    prefixes: list[tuple[int, int]] = field(default_factory=list)   # (source paragraph start, prefix length)

    def display_index(self, source: int) -> int:
        shift = 0
        for start, length in self.prefixes:
            if start <= source:
                shift += length
        return source + shift

    def source_index(self, display: int) -> int:
        shift = 0
        for start, length in self.prefixes:
            display_start = start + shift
            if display < display_start:
                break
            if display < display_start + length:
                return start
            shift += length
        return max(0, display - shift)

    def byte_index(self, source: int) -> int:
        return len(self.display[: self.display_index(source)].encode("utf-8"))

    def from_byte(self, byte: int) -> int:
        encoded = self.display.encode("utf-8")
        return self.source_index(len(encoded[:max(0, byte)].decode("utf-8", errors="ignore")))


def _display_text(text: str, list_style: str) -> tuple[str, list[tuple[int, int]]]:
    if list_style not in {"bullets", "numbers"}:
        return text, []
    parts: list[str] = []
    prefixes: list[tuple[int, int]] = []
    position = 0
    for number, paragraph in enumerate(text.split("\n"), start=1):
        prefix = "• " if list_style == "bullets" else f"{number}. "
        prefixes.append((position, len(prefix)))
        parts.append(prefix + paragraph)
        position += len(paragraph) + 1
    return "\n".join(parts), prefixes


PANGO_WEIGHTS = (100, 200, 300, 350, 380, 400, 500, 600, 700, 800, 900, 1000)


def font_description(family: str, weight: int, italic: bool, size: float) -> Pango.FontDescription:
    """A font description with an exact weight.

    A string like "Sans 700" reads the number as the size, which left every
    layer at weight 400. The weight is set directly: the nearest named Pango
    weight selects the face, and the `wght` variation gives variable fonts
    such as Figtree the exact value (550, 650).
    """
    font = Pango.FontDescription()
    font.set_family(family)
    nearest = min(PANGO_WEIGHTS, key=lambda value: abs(value - weight))
    font.set_weight(Pango.Weight(nearest))
    font.set_variations(f"wght={weight}")
    font.set_style(Pango.Style.ITALIC if italic else Pango.Style.NORMAL)
    font.set_absolute_size(size * Pango.SCALE)
    return font


def build_layout(context: Pango.Context, item: Any, text: str | None = None) -> TextLayout:
    settings = type_settings(item.metadata)
    source = item.text if text is None else text
    display, prefixes = _display_text(source or "", settings["list_style"])
    font_size = max(1.0, float(settings["font_size"] or 16))
    try:
        weight = max(100, min(1000, int(float(settings["font_weight"]))))
    except (TypeError, ValueError):
        weight = 400
    font = font_description(str(settings["font_family"] or "Figtree"), weight, bool(settings["italic"]),
                            font_size)

    layout = Pango.Layout.new(context)
    layout.set_font_description(font)
    sizing = settings["sizing"] if settings["sizing"] in SIZING else "fixed"
    if sizing == "auto_width":
        layout.set_width(-1)
    else:
        layout.set_width(max(1, round(float(item.width) * Pango.SCALE)))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    align = settings["align_h"] if settings["align_h"] in ALIGN_H else "left"
    layout.set_alignment({"center": Pango.Alignment.CENTER, "right": Pango.Alignment.RIGHT}.get(
        align, Pango.Alignment.LEFT))
    layout.set_justify(align == "justify")
    indent = float(settings["paragraph_indent"] or 0)
    if indent:
        layout.set_indent(round(indent * Pango.SCALE))
    if settings["truncate"]:
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        max_lines = settings["max_lines"]
        if max_lines:
            layout.set_height(-max(1, int(max_lines)))
        elif sizing != "auto_width":
            layout.set_height(max(1, round(float(item.height) * Pango.SCALE)))
    layout.set_text(display, -1)

    attributes = Pango.AttrList()
    spacing = settings["letter_spacing"] or {}
    if isinstance(spacing, dict) and spacing.get("value"):
        amount = float(spacing["value"])
        pixels = amount if spacing.get("unit") == "px" else font_size * amount / 100
        attributes.insert(Pango.attr_letter_spacing_new(round(pixels * Pango.SCALE)))
    line_height = settings["line_height"] or {}
    if isinstance(line_height, dict) and line_height.get("unit") in {"px", "percent"} and line_height.get("value"):
        if line_height["unit"] == "px":
            attributes.insert(Pango.attr_line_height_new_absolute(round(float(line_height["value"]) * Pango.SCALE)))
        else:
            attributes.insert(Pango.attr_line_height_new_absolute(
                round(font_size * float(line_height["value"]) / 100 * Pango.SCALE)))
    decoration = settings["decoration"]
    if decoration == "underline":
        attributes.insert(Pango.attr_underline_new(Pango.Underline.SINGLE))
    elif decoration == "strikethrough":
        attributes.insert(Pango.attr_strikethrough_new(True))
    case = settings["case"]
    transform = {"upper": "UPPERCASE", "lower": "LOWERCASE", "title": "CAPITALIZE"}.get(case)
    if transform and hasattr(Pango, "TextTransform"):
        attributes.insert(Pango.attr_text_transform_new(getattr(Pango.TextTransform, transform)))
    features = [f"{name} {1 if on else 0}" for name, on in (settings["features"] or {}).items()]
    if case == "small_caps":
        features.append("smcp 1")
    if features:
        attributes.insert(Pango.attr_font_features_new(", ".join(features)))
    paragraph_spacing = float(settings["paragraph_spacing"] or 0)
    if paragraph_spacing and "\n" in display:
        # Pango has no paragraph spacing: a taller first line after each break
        # stands in for it.
        for index, character in enumerate(display):
            if character == "\n" and index + 1 < len(display):
                start = len(display[: index + 1].encode("utf-8"))
                end = start + len(display[index + 1].encode("utf-8"))
                base = font_size * 1.2
                if isinstance(line_height, dict) and line_height.get("unit") == "px":
                    base = float(line_height.get("value") or base)
                elif isinstance(line_height, dict) and line_height.get("unit") == "percent":
                    base = font_size * float(line_height.get("value") or 120) / 100
                attribute = Pango.attr_line_height_new_absolute(round((base + paragraph_spacing) * Pango.SCALE))
                attribute.start_index, attribute.end_index = start, end
                attributes.insert(attribute)
    ranges = (item.metadata or {}).get("ranges")
    if ranges and (text is None or text == item.text):
        _apply_ranges(attributes, ranges, display, prefixes, settings, font_size, weight)
    layout.set_attributes(attributes)

    _, logical = layout.get_pixel_extents()
    width, height = float(logical.width), float(logical.height)
    align_v = settings["align_v"] if settings["align_v"] in ALIGN_V else "top"
    offset_y = 0.0
    if sizing == "fixed" and align_v != "top":
        offset_y = (float(item.height) - height) * (0.5 if align_v == "middle" else 1.0)
    return TextLayout(layout, display, offset_y, width, height, prefixes)


def _apply_ranges(attributes: Pango.AttrList, ranges: list[Any], display: str,
                  prefixes: list[tuple[int, int]], settings: dict[str, Any], base_size: float,
                  base_weight: int) -> None:
    """Per-run styles as Pango attributes over the displayed text."""
    from .paint import color_of

    length = len(display)

    def display_index(source: int) -> int:
        return source + sum(size for start, size in prefixes if start <= source)

    def byte_offset(index: int) -> int:
        return len(display[: max(0, min(length, index))].encode("utf-8"))

    def insert(attribute, start: int, end: int) -> None:
        attribute.start_index, attribute.end_index = start, end
        attributes.insert(attribute)

    for run in ranges:
        if not isinstance(run, dict):
            continue
        try:
            first, last = int(run.get("start", 0)), int(run.get("end", 0))
        except (TypeError, ValueError):
            continue
        if last <= first:
            continue
        start, end = byte_offset(display_index(first)), byte_offset(display_index(last))
        if end <= start:
            continue
        size = base_size
        if any(key in run for key in ("font_family", "font_weight", "italic", "font_size")):
            try:
                size = max(1.0, float(run.get("font_size", base_size)))
                weight = max(100, min(1000, int(float(run.get("font_weight", base_weight)))))
            except (TypeError, ValueError):
                size, weight = base_size, base_weight
            family = str(run.get("font_family") or settings["font_family"] or "Figtree")
            italic = bool(run.get("italic", settings["italic"]))
            insert(Pango.attr_font_desc_new(font_description(family, weight, italic, size)), start, end)
        if "fill" in run:
            color = color_of(run.get("fill"))
            if color is not None:
                insert(Pango.attr_foreground_new(*(int(round(channel * 65535)) for channel in
                                                   (color.r, color.g, color.b))), start, end)
                insert(Pango.attr_foreground_alpha_new(max(1, int(round(color.a * 65535)))), start, end)
        if "decoration" in run:
            decoration = run.get("decoration")
            insert(Pango.attr_underline_new(Pango.Underline.SINGLE if decoration == "underline"
                                            else Pango.Underline.NONE), start, end)
            insert(Pango.attr_strikethrough_new(decoration == "strikethrough"), start, end)
        spacing = run.get("letter_spacing")
        if isinstance(spacing, dict):
            try:
                amount = float(spacing.get("value") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            pixels = amount if spacing.get("unit") == "px" else size * amount / 100
            insert(Pango.attr_letter_spacing_new(round(pixels * Pango.SCALE)), start, end)
        case = run.get("case")
        if case is not None and hasattr(Pango, "TextTransform"):
            transform = {"upper": "UPPERCASE", "lower": "LOWERCASE", "title": "CAPITALIZE"}.get(case, "NONE")
            insert(Pango.attr_text_transform_new(getattr(Pango.TextTransform, transform)), start, end)
        features = run.get("features")
        if isinstance(features, dict) and features:
            tags = [f"{name} {1 if on else 0}" for name, on in features.items()]
            if case == "small_caps":
                tags.append("smcp 1")
            insert(Pango.attr_font_features_new(", ".join(tags)), start, end)


def fitted_size(context: Pango.Context, item: Any) -> tuple[float, float] | None:
    """The box an auto width or auto height text layer should have."""
    sizing = type_settings(item.metadata)["sizing"]
    if sizing not in {"auto_width", "auto_height"}:
        return None
    text = build_layout(context, item)
    font_size = float(type_settings(item.metadata)["font_size"] or 16)
    height = max(text.height, font_size * 1.2)
    if sizing == "auto_width":
        return max(1.0, text.width + 1.0), height
    return float(item.width), height
