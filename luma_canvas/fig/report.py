# SPDX-License-Identifier: MPL-2.0
"""What an import changed: everything approximated, missing or left out.

Nothing an importer cannot carry across disappears quietly. Each finding
has a topic (so a thousand frames with auto layout are one line, not a
thousand), a severity, a sentence for the person, and the layers it touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

UNSUPPORTED = "unsupported"   # left out
MISSING = "missing"           # referenced but not in the file, or not on this machine
APPROXIMATED = "approximated"  # imported, drawn or edited differently
INFO = "info"                 # nothing visible changed

SEVERITIES = (UNSUPPORTED, MISSING, APPROXIMATED, INFO)
LAYER_SAMPLE = 12

# Topic: (severity, title, detail). Sentence case, American spelling, no em dashes.
TOPICS: dict[str, tuple[str, str, str]] = {
    "auto_layout": (APPROXIMATED, "Auto layout is kept as fixed positions",
                    "Layers stay where Figma laid them out, but resizing these frames won’t reflow them."),
    "blend_approximate": (APPROXIMATED, "Two blend modes use the nearest one Canvas draws",
                          "Linear burn is drawn as color burn and linear dodge as color dodge."),
    "effect_blend": (APPROXIMATED, "Shadow blend modes are drawn as Normal",
                     "A shadow with a blend mode of its own is drawn as Normal."),
    "effects": (UNSUPPORTED, "Some effects aren’t supported",
                "Noise, texture, glass and other new effects are left out."),
    "group_inner_shadow": (APPROXIMATED, "A group’s inner shadow is drawn on each layer in the group",
                           "An inner shadow needs a shape to fall inside, so every layer in the group casts its own."),
    "stroke_geometry": (APPROXIMATED, "Some strokes are outlined",
                        "Strokes Canvas can’t rebuild from the path are imported as filled shapes."),
    "text_fill": (APPROXIMATED, "Some text fills are simplified",
                  "Text with both per character colors and several fills keeps the first fill."),
    "text_lists": (APPROXIMATED, "Mixed list styles use one style per text layer",
                   "Canvas applies one list style to a whole text layer."),
    "text_line_height": (APPROXIMATED, "Mixed line heights use the layer’s line height", ""),
    "text_links": (INFO, "Text links aren’t kept", "The text is kept without its links."),
    "text_features": (APPROXIMATED, "Some font variants are simplified",
                      "Stylistic sets and other OpenType settings Canvas doesn’t offer are left out."),
    "images_missing": (MISSING, "Some images aren’t in the file",
                       "Their layers are kept with an empty image fill."),
    "image_preview": (APPROXIMATED, "Some images use their preview",
                      "The full image in the file can’t be read, so the smaller preview Figma saved is shown."),
    "progressive_blur": (APPROXIMATED, "Progressive blur is drawn as an even blur",
                         "Canvas uses the lighter end of the blur across the whole layer."),
    "image_filters": (APPROXIMATED, "Image adjustments aren’t applied",
                      "Exposure, contrast and other image adjustments are left out."),
    "video": (APPROXIMATED, "Videos show their poster image", ""),
    "paints": (UNSUPPORTED, "Some fills aren’t supported",
               "Pattern, noise, emoji and custom fills are left out."),
    "instances": (APPROXIMATED, "Instances are imported as frames",
                  "Each instance shows its component’s content with its overrides, but isn’t linked to the component."),
    "components_missing": (MISSING, "Some components aren’t in the file",
                           "Instances of components from libraries that weren’t saved with the file are empty frames."),
    "instance_depth": (UNSUPPORTED, "Some deeply nested instances are left out",
                       "Instances nested too deeply, or inside themselves, are left empty."),
    "figjam": (APPROXIMATED, "FigJam objects are imported as shapes and text", ""),
    "layer_types": (UNSUPPORTED, "Some layer types aren’t supported", "These layers are left out."),
    "text_scale": (APPROXIMATED, "Scaled text keeps its font size",
                   "Text scaled with Figma’s scale tool is imported at its own size in a scaled box."),
    "layout_grids": (UNSUPPORTED, "Some layout grid rules aren’t supported", "Standard grids, columns and rows remain editable; these rules are left out."),
    "prototype": (UNSUPPORTED, "Some prototype interactions need attention",
                  "Supported interactions are kept. The details identify actions or targets Canvas could not reproduce."),
    "export_settings": (INFO, "Some export settings aren’t imported",
                        "Export rows in formats Canvas doesn’t write are left out. The others are kept."),
    "variables": (INFO, "Variables are imported as their current values", ""),
    "styles": (INFO, "Shared styles are imported as their current values", ""),
    "geometry": (UNSUPPORTED, "Some shapes couldn’t be read",
                 "Their geometry is damaged in the file, so these layers are left out."),
    "fonts": (MISSING, "Some fonts aren’t installed", ""),
    "editor": (APPROXIMATED, "This file was made in another Figma editor",
               "Canvas imports its layers as design layers."),
    "hidden_pages": (INFO, "Embedded components are retained separately",
                     "Required embedded definitions are linked on the Imported components page. Unused internal library content is not shown."),
}


@dataclass
class Finding:
    topic: str
    severity: str
    title: str
    detail: str
    layers: list[str] = field(default_factory=list)
    count: int = 0
    extra: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "severity": self.severity, "title": self.title,
                "detail": self.detail, "layers": list(self.layers), "count": self.count,
                "extra": sorted(self.extra)}


class ImportReport:
    def __init__(self) -> None:
        self._findings: dict[str, Finding] = {}
        self.stats: dict[str, Any] = {}

    def note(self, topic: str, layer: str | None = None, *, extra: str | None = None,
             detail: str | None = None, severity: str | None = None, title: str | None = None) -> None:
        finding = self._findings.get(topic)
        if finding is None:
            default_severity, default_title, default_detail = TOPICS.get(topic, (APPROXIMATED, topic, ""))
            finding = Finding(topic, severity or default_severity, title or default_title,
                              detail if detail is not None else default_detail)
            self._findings[topic] = finding
        finding.count += 1
        if layer and len(finding.layers) < LAYER_SAMPLE and layer not in finding.layers:
            finding.layers.append(layer)
        if extra and len(finding.extra) < 64:
            finding.extra.add(extra)

    def has(self, topic: str) -> bool:
        return topic in self._findings

    @property
    def findings(self) -> list[Finding]:
        order = {severity: index for index, severity in enumerate(SEVERITIES)}
        return sorted(self._findings.values(), key=lambda item: (order.get(item.severity, 9), item.title))

    @property
    def visible_findings(self) -> list[Finding]:
        """What the report sheet lists: everything but pure information."""
        return [item for item in self.findings if item.severity != INFO]

    def __bool__(self) -> bool:
        return bool(self._findings)

    def to_dict(self) -> dict[str, Any]:
        return {"findings": [item.to_dict() for item in self.findings], "stats": dict(self.stats)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ImportReport":
        report = cls()
        for item in value.get("findings") or []:
            if not isinstance(item, dict) or not isinstance(item.get("topic"), str):
                continue
            finding = Finding(item["topic"], str(item.get("severity", APPROXIMATED)), str(item.get("title", "")),
                              str(item.get("detail", "")), [str(name) for name in item.get("layers") or []][:LAYER_SAMPLE],
                              int(item.get("count", 0) or 0), {str(name) for name in item.get("extra") or []})
            report._findings[finding.topic] = finding
        report.stats = dict(value.get("stats") or {})
        return report
