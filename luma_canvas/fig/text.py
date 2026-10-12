# SPDX-License-Identifier: MPL-2.0
"""Figma text as Canvas type settings, with per-range styles.

A Figma text layer has a base style (the node's own fields) and a style id
per character (`textData.characterStyleIDs`) naming an entry in
`textData.styleOverrideTable` that overrides some of those fields. Canvas
keeps the base style in the layer's metadata and every run that differs as
`metadata["ranges"]`: `{"start", "end"}` in characters of the layer's text
plus the type settings that differ.
"""

from __future__ import annotations

import re
from typing import Any

from .paints import hex_color, number, visible_paints
from .report import ImportReport

WEIGHT_WORDS = (
    ("extrablack", 950), ("ultrablack", 950), ("extralight", 200), ("ultralight", 200),
    ("semibold", 600), ("demibold", 600), ("extrabold", 800), ("ultrabold", 800),
    ("hairline", 100), ("thin", 100), ("light", 300), ("book", 400), ("regular", 400),
    ("normal", 400), ("medium", 500), ("bold", 700), ("heavy", 900), ("black", 900),
)
WGHT = 0x77676874   # 'wght'

ALIGN_H = {"LEFT": "left", "CENTER": "center", "RIGHT": "right", "JUSTIFIED": "justify"}
ALIGN_V = {"TOP": "top", "CENTER": "middle", "BOTTOM": "bottom"}
CASES = {"ORIGINAL": "none", "UPPER": "upper", "LOWER": "lower", "TITLE": "title",
         "SMALL_CAPS": "small_caps", "SMALL_CAPS_FORCED": "small_caps"}
DECORATIONS = {"NONE": "none", "UNDERLINE": "underline", "STRIKETHROUGH": "strikethrough"}
SIZING = {"NONE": "fixed", "WIDTH_AND_HEIGHT": "auto_width", "HEIGHT": "auto_height"}
RANGE_KEYS = ("font_family", "font_weight", "italic", "font_size", "fill", "fill_gradient", "decoration",
              "letter_spacing", "case", "features")


def weight_and_italic(style: str, props: dict[str, Any] | None = None) -> tuple[int, bool]:
    compact = re.sub(r"[\s_\-]+", "", (style or "").casefold())
    italic = "italic" in compact or "oblique" in compact
    weight = 400
    for word, value in WEIGHT_WORDS:
        if word in compact:
            weight = value
            break
    else:
        digits = re.search(r"\d{3}", compact)
        if digits:
            weight = int(digits.group(0))
    props = props or {}
    for variation in props.get("fontVariations") or []:
        if isinstance(variation, dict) and (variation.get("axisTag") == WGHT
                                            or str(variation.get("axisName", "")).casefold() == "weight"):
            value = number(variation.get("value"), weight)
            if 1 <= value <= 1000:
                weight = int(round(value))
    return max(1, min(1000, weight)), italic


def _line_height(value: Any, font_size: float, natural: float | None) -> dict[str, Any] | None:
    """Figma stores a percentage of the font size as RAW (1.5 is 150%) and
    Auto as PERCENT 100: a percentage of the font's own line height, which
    `natural` (the font's line height per unit of size) converts."""
    if not isinstance(value, dict):
        return None
    units, amount = value.get("units"), number(value.get("value"))
    if units == "PIXELS" and amount > 0:
        return {"unit": "px", "value": round(amount, 4)}
    if units == "RAW" and amount > 0:
        return {"unit": "percent", "value": round(amount * 100, 4)}
    if units == "PERCENT":
        if abs(amount - 100) < 1e-3 or amount <= 0:
            return {"unit": "auto"}
        if natural:
            return {"unit": "percent", "value": round(amount * natural, 4)}
        return {"unit": "percent", "value": round(amount * 1.2, 4)}
    return None


def _letter_spacing(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    units, amount = value.get("units"), number(value.get("value"))
    if units == "PIXELS":
        return {"unit": "px", "value": round(amount, 4)}
    if units == "PERCENT":
        return {"unit": "percent", "value": round(amount, 4)}
    if units == "RAW":
        return {"unit": "percent", "value": round(amount * 100, 4)}
    return None


def _features(props: dict[str, Any], report: ImportReport, layer: str) -> dict[str, bool]:
    features: dict[str, bool] = {}
    toggles = {"fontVariantCommonLigatures": "liga", "fontVariantContextualLigatures": "calt",
               "fontVariantDiscretionaryLigatures": "dlig", "fontVariantHistoricalLigatures": "hlig",
               "fontVariantOrdinal": "ordn", "fontVariantSlashedZero": "zero"}
    defaults = {"liga": True, "calt": True}
    for key, tag in toggles.items():
        if key in props and bool(props[key]) != defaults.get(tag, False):
            features[tag] = bool(props[key])
    figures = {"LINING": "lnum", "OLDSTYLE": "onum"}.get(props.get("fontVariantNumericFigure"))
    if figures:
        features[figures] = True
    spacing = {"PROPORTIONAL": "pnum", "TABULAR": "tnum"}.get(props.get("fontVariantNumericSpacing"))
    if spacing:
        features[spacing] = True
    fraction = {"DIAGONAL": "frac", "STACKED": "afrc"}.get(props.get("fontVariantNumericFraction"))
    if fraction:
        features[fraction] = True
    position = {"SUB": "subs", "SUPER": "sups"}.get(props.get("fontVariantPosition"))
    if position:
        features[position] = True
    caps = {"SMALL": "smcp", "ALL_SMALL": "c2sc", "PETITE": "pcap", "ALL_PETITE": "c2pc",
            "UNICASE": "unic", "TITLING": "titl"}.get(props.get("fontVariantCaps"))
    if caps:
        features[caps] = True
    for key, value in (("toggledOnOTFeatures", True), ("toggledOffOTFeatures", False)):
        for feature in props.get(key) or []:
            tag = feature if isinstance(feature, str) else None
            if tag and re.fullmatch(r"[A-Za-z0-9]{4}", tag):
                features[tag.lower()] = value
            elif feature:
                report.note("text_features", layer)
    return features


class TextStyle:
    """One run's resolved Canvas type settings."""

    @staticmethod
    def of(props: dict[str, Any], fills: dict[str, Any], report: ImportReport, layer: str) -> dict[str, Any]:
        font = props.get("fontName") if isinstance(props.get("fontName"), dict) else {}
        family = str(font.get("family") or "Inter")
        weight, italic = weight_and_italic(str(font.get("style") or ""), props)
        for meta in (props.get("_font_meta") or []):
            key = meta.get("key") if isinstance(meta, dict) else None
            if isinstance(key, dict) and key.get("family") == family and key.get("style") == font.get("style"):
                if isinstance(meta.get("fontWeight"), int) and 1 <= meta["fontWeight"] <= 1000 \
                        and not props.get("fontVariations"):
                    weight = meta["fontWeight"]
                if meta.get("fontStyle") == "ITALIC":
                    italic = True
        size = max(0.5, number(props.get("fontSize"), 12.0))
        style: dict[str, Any] = {
            "font_family": family, "font_weight": weight, "italic": italic, "font_size": round(size, 4),
            "decoration": DECORATIONS.get(props.get("textDecoration"), "none"),
            "case": CASES.get(props.get("textCase"), "none"),
            "fill": fills.get("fill"), "fill_gradient": fills.get("fill_gradient"),
        }
        spacing = _letter_spacing(props.get("letterSpacing"))
        style["letter_spacing"] = spacing or {"unit": "percent", "value": 0}
        natural = None
        for meta in (props.get("_font_meta") or []):
            key = meta.get("key") if isinstance(meta, dict) else None
            if isinstance(key, dict) and key.get("family") == family and number(meta.get("fontLineHeight")) > 0:
                natural = number(meta.get("fontLineHeight"))
                break
        line_height = _line_height(props.get("lineHeight"), size, natural)
        style["line_height"] = line_height or {"unit": "auto"}
        style["features"] = _features(props, report, layer)
        return style


def text_fields(props: dict[str, Any], fill_for: Any, report: ImportReport, layer: str) -> dict[str, Any]:
    """`text` and `metadata` for a text layer.

    `fill_for(style_props)` converts a props dictionary's fills the way the
    layer's own fills are converted.
    """
    data = props.get("textData") if isinstance(props.get("textData"), dict) else {}
    characters = data.get("characters")
    text = characters if isinstance(characters, str) else ""
    derived = props.get("derivedTextData") if isinstance(props.get("derivedTextData"), dict) else {}
    font_meta = data.get("fontMetaData") or derived.get("fontMetaData") or []
    base_props = dict(props)
    base_props["_font_meta"] = font_meta
    base_fills = fill_for(props)
    base = TextStyle.of(base_props, base_fills, report, layer)

    metadata: dict[str, Any] = {key: base[key] for key in ("font_family", "font_weight", "italic", "font_size",
                                                           "decoration", "case", "letter_spacing",
                                                           "line_height")}
    if base["features"]:
        metadata["features"] = base["features"]
    metadata["align_h"] = ALIGN_H.get(props.get("textAlignHorizontal"), "left")
    metadata["align_v"] = ALIGN_V.get(props.get("textAlignVertical"), "top")
    metadata["sizing"] = SIZING.get(props.get("textAutoResize"), "fixed")
    if props.get("textTruncation") == "ENDING":
        metadata["truncate"] = True
        max_lines = props.get("maxLines")
        if isinstance(max_lines, int) and max_lines > 0:
            metadata["max_lines"] = max_lines
    if number(props.get("paragraphSpacing")):
        metadata["paragraph_spacing"] = round(number(props.get("paragraphSpacing")), 4)
    if number(props.get("paragraphIndent")):
        metadata["paragraph_indent"] = round(number(props.get("paragraphIndent")), 4)
    if props.get("leadingTrim") == "CAP_HEIGHT":
        metadata["vertical_trim"] = "cap_height"
    if props.get("hangingPunctuation"):
        metadata["hanging_punctuation"] = True

    lines = [line for line in data.get("lines") or [] if isinstance(line, dict)]
    kinds = {line.get("lineType") for line in lines} - {None}
    if kinds == {"UNORDERED_LIST"}:
        metadata["list_style"] = "bullets"
    elif kinds == {"ORDERED_LIST"}:
        metadata["list_style"] = "numbers"
    elif len(kinds - {"PLAIN"}) >= 1:
        report.note("text_lists", layer)
        listed = kinds - {"PLAIN"}
        metadata["list_style"] = "bullets" if "UNORDERED_LIST" in listed else "numbers"

    if props.get("hyperlink"):
        report.note("text_links", layer)

    ranges = _ranges(props, data, text, base, fill_for, report, layer, font_meta)
    if ranges:
        metadata["ranges"] = ranges
    return {"text": text, "metadata": metadata, "fill": base["fill"], "fill_gradient": base["fill_gradient"]}


def _ranges(props, data, text, base, fill_for, report, layer, font_meta) -> list[dict[str, Any]]:
    ids = data.get("characterStyleIDs")
    table = data.get("styleOverrideTable")
    if not isinstance(ids, list) or not ids or not isinstance(table, list):
        return []
    overrides: dict[int, dict[str, Any]] = {}
    for entry in table:
        if isinstance(entry, dict) and isinstance(entry.get("styleID"), int):
            overrides[entry["styleID"]] = entry
    styles: dict[int, dict[str, Any]] = {}
    runs: list[dict[str, Any]] = []
    length = len(text)
    start = 0
    current = ids[0] if isinstance(ids[0], int) else 0
    positions = list(ids[:length]) + [0] * max(0, length - len(ids))

    def style_for(style_id: int) -> dict[str, Any]:
        if style_id not in styles:
            entry = overrides.get(style_id)
            if not entry:
                styles[style_id] = base
            else:
                merged = dict(props)
                merged.update({key: value for key, value in entry.items() if key != "styleID"})
                merged["_font_meta"] = font_meta
                fills = fill_for(merged) if "fillPaints" in entry else {"fill": base["fill"],
                                                                         "fill_gradient": base["fill_gradient"]}
                styles[style_id] = TextStyle.of(merged, fills, report, layer)
                if "lineHeight" in entry and styles[style_id]["line_height"] != base["line_height"]:
                    report.note("text_line_height", layer)
                if entry.get("hyperlink"):
                    report.note("text_links", layer)
        return styles[style_id]

    for index in range(1, length + 1):
        value = positions[index] if index < length else None
        if value is not None and not isinstance(value, int):
            value = 0
        if index == length or value != current:
            style = style_for(current)
            if style is not base:
                difference = {key: style[key] for key in RANGE_KEYS if style.get(key) != base.get(key)}
                if difference:
                    if runs and runs[-1]["end"] == start and all(
                            runs[-1].get(key) == difference.get(key) for key in set(runs[-1]) - {"start", "end"}) \
                            and set(runs[-1]) - {"start", "end"} == set(difference):
                        runs[-1]["end"] = index
                    else:
                        runs.append({"start": start, "end": index, **difference})
            start = index
            current = value if value is not None else 0
    return runs


def fill_hex(paints: Any) -> str | None:
    for paint in reversed(visible_paints(paints)):
        if paint.get("type") == "SOLID":
            return hex_color(paint.get("color"), number(paint.get("opacity"), 1.0))
    return None
