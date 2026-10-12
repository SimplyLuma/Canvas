# SPDX-License-Identifier: Apache-2.0
"""A retained image stage with an inset fit area (IM3).

The app owns decode, cancellation and storage.  This widget only snapshots an
already supplied paintable, so changing a preview never changes source pixels.
The returned bounds are the untransformed fitted image in widget coordinates.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402

from . import lumaui_tokens as tokens
from . import media_style as style

__all__ = ["ImageViewport", "fit_bounds", "normalise_adjustments"]

V = tokens.MEDIA["viewport"]
_CHANNELS = ("e", "c", "hi", "sh", "w", "sat")
_PRESETS = ("", "vivid", "warm", "cool", "mono", "fade")


def fit_bounds(width: float, height: float, image_width: float, image_height: float,
               insets: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Fit an unrotated image inside top/right/bottom/left inset space."""
    top, right, bottom, left = insets
    available_width = max(0.0, width - left - right)
    available_height = max(0.0, height - top - bottom)
    if min(available_width, available_height, image_width, image_height) <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    scale = min(available_width / image_width, available_height / image_height)
    fitted_width, fitted_height = image_width * scale, image_height * scale
    return (left + (available_width - fitted_width) / 2,
            top + (available_height - fitted_height) / 2, fitted_width, fitted_height)


def normalise_adjustments(values: Mapping | None) -> dict:
    """Copy and validate the Photos preview schema without changing the caller's map."""
    values = dict(values or {})
    unknown = values.keys() - set(_CHANNELS) - {"pre", "rot", "flip", "crop"}
    if unknown:
        raise ValueError(f"unknown image adjustments: {sorted(unknown)}")
    result = {}
    for key in _CHANNELS:
        value = values.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number from -100 to 100")
        result[key] = max(-100.0, min(100.0, float(value)))
    result["pre"] = values.get("pre", "")
    if result["pre"] not in _PRESETS:
        raise ValueError(f"unknown image preset: {result['pre']!r}")
    result["rot"] = values.get("rot", 0)
    if isinstance(result["rot"], bool) or result["rot"] not in (0, 90, 180, 270):
        raise ValueError("rot must be 0, 90, 180 or 270 degrees")
    result["flip"] = values.get("flip", False)
    if not isinstance(result["flip"], bool):
        raise ValueError("flip must be boolean")
    result["crop"] = values.get("crop", "original")
    if result["crop"] not in ("original", "square", "4:3", "16:9"):
        raise ValueError("crop must be original, square, 4:3 or 16:9")
    return result


def _cropped_size(width: float, height: float, crop: str) -> tuple[float, float]:
    if crop == "original" or min(width, height) <= 0:
        return width, height
    numerator, denominator = {"square": (1, 1), "4:3": (4, 3), "16:9": (16, 9)}[crop]
    ratio = numerator / denominator
    return (height * ratio, height) if width / height > ratio else (width, width / ratio)


def _colour_matrix(values: dict) -> tuple[Graphene.Matrix, Graphene.Vec4] | None:
    """Compose bounded preview controls into one GPU colour matrix."""
    if not any(values[key] for key in _CHANNELS) and not values["pre"]:
        return None
    preset = values["pre"]
    exposure = 2 ** (values["e"] / 100)
    contrast = max(0.25, 1 + values["c"] / 150)
    # Highlight/shadow controls are an intentionally bounded linear preview.
    # The app's authoritative edit values remain untouched.
    lift = values["sh"] / 500 - values["hi"] / 1000
    gain = exposure * contrast * (1 + values["hi"] / 500)
    if preset == "fade":
        gain *= 0.85
        lift += 0.075
    if preset == "vivid":
        gain *= 1.07
    saturation = max(0, 1 + values["sat"] / 100 + (0.25 if preset == "vivid" else 0))
    if preset == "mono":
        saturation = 0
    warmth = values["w"] / 100 + (0.2 if preset == "warm" else -0.2 if preset == "cool" else 0)
    red, green, blue = 1 + warmth * 0.12, 1, 1 - warmth * 0.12
    luma = (0.2126, 0.7152, 0.0722)
    channel_gain = (red, green, blue)
    matrix = [0.0] * 16
    # Graphene multiplies row vectors: store each output channel as a column.
    for row in range(3):
        for col in range(3):
            matrix[col * 4 + row] = gain * channel_gain[row] * ((1 - saturation) * luma[col] +
                                                                  (saturation if row == col else 0))
    matrix[15] = 1.0
    offset = 0.5 * (1 - contrast) + lift
    return (Graphene.Matrix().init_from_float(matrix),
            Graphene.Vec4().init(offset, offset, offset, 0))


class ImageViewport(Gtk.Widget):
    """ImageViewport(surround='black') with paintable, preview and fit-inset setters.

    `set_paintable` takes `None` or a Gdk.Paintable. `get_content_bounds()`
    returns `(x, y, width, height)` relative to the widget's full allocation.
    Rotation and horizontal flip transform the already fitted image; clipping
    is at the full stage edge so a portrait quarter-turn can extend into the
    inset region. The parent island may clip the entire stage further.
    """

    __gtype_name__ = "LumaUIImageViewport"

    def __init__(self, *, surround: str = "black") -> None:
        if surround != "black":
            raise ValueError("ImageViewport currently supports surround='black'")
        super().__init__(css_name="lumaui-image-viewport", hexpand=True, vexpand=True,
                         overflow=Gtk.Overflow.HIDDEN)
        self.surround = surround
        self._paintable: Gdk.Paintable | None = None
        self._insets = (0.0, 0.0, 0.0, 0.0)
        self._adjustments = normalise_adjustments(None)
        self.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Photo"])

    def set_paintable(self, paintable: Gdk.Paintable | None) -> None:
        if paintable is not None and not isinstance(paintable, Gdk.Paintable):
            raise TypeError("paintable must be a Gdk.Paintable or None")
        self._paintable = paintable
        self.queue_draw()

    def set_adjustments(self, values: Mapping | None) -> None:
        self._adjustments = normalise_adjustments(values)
        self.queue_draw()

    def set_fit_insets(self, top: float, right: float, bottom: float, left: float) -> None:
        values = (top, right, bottom, left)
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("fit insets must be finite nonnegative numbers")
        self._insets = tuple(float(v) for v in values)
        self.queue_draw()

    def get_content_bounds(self) -> tuple[float, float, float, float]:
        picture = self._paintable
        if picture is None:
            return (0.0, 0.0, 0.0, 0.0)
        width, height = _cropped_size(picture.get_intrinsic_width(),
                                      picture.get_intrinsic_height(), self._adjustments["crop"])
        return fit_bounds(self.get_width(), self.get_height(), width, height, self._insets)

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        return (1, 1, -1, -1)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        root = self.get_root()
        phone = isinstance(root, Gtk.Widget) and root.has_css_class("lumaui-phone-device")
        snapshot.push_clip(style.rect(0, 0, width, height))
        surround = "luma_media_viewport_phone_black" if phone else "luma_media_viewport_black"
        snapshot.append_color(style.colour(self, surround), style.rect(0, 0, width, height))
        picture = self._paintable
        x, y, image_width, image_height = self.get_content_bounds()
        if picture is None or image_width <= 0 or image_height <= 0:
            snapshot.pop()
            return
        bounds = style.rect(x, y, image_width, image_height)
        radius = min(float(V["phone_radius"] if phone else V["radius"]), image_width / 2, image_height / 2)
        shape = style.rounded(bounds, radius)
        if not phone:
            snapshot.append_outset_shadow(shape, style.colour(self, "luma_media_viewport_shadow"),
                                          0, float(V["shadow_y"]), float(V["shadow_blur"]), 0)
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x + image_width / 2, y + image_height / 2))
        snapshot.rotate(float(self._adjustments["rot"]))
        if self._adjustments["flip"]:
            snapshot.scale(-1, 1)
        snapshot.translate(Graphene.Point().init(-image_width / 2, -image_height / 2))
        snapshot.push_rounded_clip(style.rounded(style.rect(0, 0, image_width, image_height), radius))
        colour = _colour_matrix(self._adjustments)
        if colour is not None:
            snapshot.push_color_matrix(*colour)
        cropped_width, cropped_height = _cropped_size(picture.get_intrinsic_width(),
                                                      picture.get_intrinsic_height(), self._adjustments["crop"])
        full_width = image_width * picture.get_intrinsic_width() / cropped_width
        full_height = image_height * picture.get_intrinsic_height() / cropped_height
        source = style.rect((image_width-full_width)/2, (image_height-full_height)/2,
                            full_width, full_height)
        if isinstance(picture, Gdk.Texture):
            snapshot.append_scaled_texture(picture, Gsk.ScalingFilter.TRILINEAR,
                                           source)
        else:
            snapshot.save()
            snapshot.translate(Graphene.Point().init(source.origin.x, source.origin.y))
            picture.snapshot(snapshot, full_width, full_height)
            snapshot.restore()
        if colour is not None:
            snapshot.pop()
        snapshot.pop()
        snapshot.restore()
        snapshot.pop()
