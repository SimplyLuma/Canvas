# SPDX-License-Identifier: MPL-2.0
"""Write `.fig` files for tests: a Kiwi encoder and a Figma-shaped schema.

The schema here is written for these tests. Its definition and field names
follow the ones Figma files carry (so the importer reads fixtures exactly as
it reads real files), but it holds only what the fixtures use, and its field
numbers are its own: a `.fig` file carries its schema, so readers never
assume numbers.
"""

from __future__ import annotations

import io
import math
import struct
import zipfile
import zlib
from typing import Any

# ── Kiwi encoding ────────────────────────────────────────────────────────

PRIMITIVES = ("bool", "byte", "int", "uint", "float", "string", "int64", "uint64")
KIND_CODES = {"ENUM": 0, "STRUCT": 1, "MESSAGE": 2}


def uint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        if value:
            out.append(byte | 128)
        else:
            out.append(byte)
            return bytes(out)


def sint(value: int) -> bytes:
    return uint(((value << 1) ^ (value >> 31)) & 0xFFFFFFFF)


def uint64(value: int) -> bytes:
    out = bytearray()
    for _ in range(8):
        byte = value & 127
        value >>= 7
        if value:
            out.append(byte | 128)
        else:
            out.append(byte)
            return bytes(out)
    out.append(value & 255)
    return bytes(out)


def vfloat(value: float) -> bytes:
    bits = struct.unpack("<I", struct.pack("<f", value))[0]
    bits = ((bits >> 23) | (bits << 9)) & 0xFFFFFFFF
    if bits & 255 == 0:
        return b"\x00"
    return struct.pack("<I", bits)


def string(value: str) -> bytes:
    return value.encode("utf-8") + b"\x00"


class SchemaBuilder:
    def __init__(self) -> None:
        self.definitions: list[tuple[str, str, list[tuple[str, str, bool, int]]]] = []

    def enum(self, name: str, values: list[str]) -> None:
        self.definitions.append((name, "ENUM", [(value, "", False, index) for index, value in enumerate(values)]))

    def struct(self, name: str, fields: list[tuple[str, str]]) -> None:
        self.definitions.append((name, "STRUCT", [(field, kind.rstrip("[]"), kind.endswith("[]"), index + 1)
                                                  for index, (field, kind) in enumerate(fields)]))

    def message(self, name: str, fields: list[tuple[str, str]]) -> None:
        self.struct(name, fields)
        self.definitions[-1] = (name, "MESSAGE", self.definitions[-1][2])

    def index(self, name: str) -> int:
        return next(position for position, item in enumerate(self.definitions) if item[0] == name)

    def encode(self) -> bytes:
        out = bytearray(uint(len(self.definitions)))
        for name, kind, fields in self.definitions:
            out += string(name) + bytes([KIND_CODES[kind]]) + uint(len(fields))
            for field_name, field_type, is_array, value in fields:
                if kind == "ENUM":
                    type_index = 0
                elif field_type in PRIMITIVES:
                    type_index = ~PRIMITIVES.index(field_type)
                else:
                    type_index = self.index(field_type)
                out += string(field_name) + sint(type_index) + bytes([1 if is_array else 0]) + uint(value)
        return bytes(out)

    def encode_value(self, type_name: str, value: Any) -> bytes:
        if type_name in PRIMITIVES:
            if type_name == "bool":
                return b"\x01" if value else b"\x00"
            if type_name == "byte":
                return bytes([value & 255])
            if type_name == "int":
                return sint(int(value))
            if type_name == "uint":
                return uint(int(value))
            if type_name == "float":
                return vfloat(float(value))
            if type_name == "string":
                return string(value)
            if type_name == "uint64":
                return uint64(int(value))
            if type_name == "int64":
                return uint64((int(value) << 1) ^ (int(value) >> 63))
        name, kind, fields = self.definitions[self.index(type_name)]
        if kind == "ENUM":
            number = next(item[3] for item in fields if item[0] == value)
            return uint(number)
        out = bytearray()
        if kind == "STRUCT":
            for field_name, field_type, is_array, _ in fields:
                out += self._field(field_type, is_array, value[field_name])
            return bytes(out)
        known = {item[0]: item for item in fields}
        for key, item in value.items():
            if key not in known:
                raise KeyError(f"{name} has no field {key}")
            field_name, field_type, is_array, number = known[key]
            out += uint(number) + self._field(field_type, is_array, item)
        out += b"\x00"
        return bytes(out)

    def _field(self, field_type: str, is_array: bool, value: Any) -> bytes:
        if not is_array:
            return self.encode_value(field_type, value)
        if field_type == "byte":
            data = bytes(value)
            return uint(len(data)) + data
        out = bytearray(uint(len(value)))
        for item in value:
            out += self.encode_value(field_type, item)
        return bytes(out)


def figma_schema() -> SchemaBuilder:
    s = SchemaBuilder()
    s.enum("MessageType", ["JOIN_START", "NODE_CHANGES"])
    s.enum("NodePhase", ["CREATED", "REMOVED"])
    s.enum("NodeType", ["NONE", "DOCUMENT", "CANVAS", "GROUP", "FRAME", "BOOLEAN_OPERATION", "VECTOR", "STAR",
                        "LINE", "ELLIPSE", "RECTANGLE", "REGULAR_POLYGON", "ROUNDED_RECTANGLE", "TEXT", "SLICE",
                        "SYMBOL", "INSTANCE", "STICKY", "SHAPE_WITH_TEXT", "CONNECTOR", "CODE_BLOCK", "WIDGET",
                        "STAMP", "MEDIA", "HIGHLIGHT", "SECTION", "SECTION_OVERLAY", "WASHI_TAPE", "VARIABLE",
                        "TABLE", "TABLE_CELL", "FUTURE_THING"])
    s.enum("BlendMode", ["PASS_THROUGH", "NORMAL", "DARKEN", "MULTIPLY", "LINEAR_BURN", "COLOR_BURN", "LIGHTEN",
                         "SCREEN", "LINEAR_DODGE", "COLOR_DODGE", "OVERLAY", "SOFT_LIGHT", "HARD_LIGHT",
                         "DIFFERENCE", "EXCLUSION", "HUE", "SATURATION", "COLOR", "LUMINOSITY"])
    s.enum("PaintType", ["SOLID", "GRADIENT_LINEAR", "GRADIENT_RADIAL", "GRADIENT_ANGULAR", "GRADIENT_DIAMOND",
                         "IMAGE", "EMOJI", "VIDEO", "PATTERN", "NOISE"])
    s.enum("ImageScaleMode", ["STRETCH", "FIT", "FILL", "TILE"])
    s.enum("EffectType", ["INNER_SHADOW", "DROP_SHADOW", "FOREGROUND_BLUR", "BACKGROUND_BLUR", "REPEAT",
                          "SYMMETRY", "GRAIN", "NOISE", "GLASS"])
    s.enum("StrokeAlign", ["CENTER", "INSIDE", "OUTSIDE"])
    s.enum("StrokeCap", ["NONE", "ROUND", "SQUARE", "ARROW_LINES", "ARROW_EQUILATERAL", "DIAMOND_FILLED",
                         "TRIANGLE_FILLED", "HIGHLIGHT", "WASHI_TAPE_1", "WASHI_TAPE_2", "WASHI_TAPE_3",
                         "WASHI_TAPE_4", "WASHI_TAPE_5", "WASHI_TAPE_6", "CIRCLE_FILLED"])
    s.enum("StrokeJoin", ["MITER", "BEVEL", "ROUND"])
    s.enum("WindingRule", ["NONZERO", "ODD"])
    s.enum("TextAlignHorizontal", ["LEFT", "CENTER", "RIGHT", "JUSTIFIED"])
    s.enum("TextAlignVertical", ["TOP", "CENTER", "BOTTOM"])
    s.enum("TextCase", ["ORIGINAL", "UPPER", "LOWER", "TITLE", "SMALL_CAPS", "SMALL_CAPS_FORCED"])
    s.enum("TextDecoration", ["NONE", "UNDERLINE", "STRIKETHROUGH"])
    s.enum("TextAutoResize", ["NONE", "WIDTH_AND_HEIGHT", "HEIGHT"])
    s.enum("TextTruncation", ["DISABLED", "ENDING"])
    s.enum("NumberUnits", ["RAW", "PIXELS", "PERCENT"])
    s.enum("ConstraintType", ["MIN", "CENTER", "MAX", "STRETCH", "SCALE"])
    s.enum("MaskType", ["ALPHA", "OUTLINE", "LUMINANCE"])
    s.enum("BooleanOperation", ["UNION", "INTERSECT", "SUBTRACT", "XOR"])
    s.enum("StackMode", ["NONE", "HORIZONTAL", "VERTICAL"])
    s.enum("ComponentPropNodeField", ["VISIBLE", "TEXT_DATA", "OVERRIDDEN_SYMBOL_ID", "INHERIT_FILL_STYLE_ID"])
    s.enum("LineType", ["PLAIN", "ORDERED_LIST", "UNORDERED_LIST"])
    s.struct("GUID", [("sessionID", "uint"), ("localID", "uint")])
    s.struct("Color", [("r", "float"), ("g", "float"), ("b", "float"), ("a", "float")])
    s.struct("Vector", [("x", "float"), ("y", "float")])
    s.struct("Matrix", [("m00", "float"), ("m01", "float"), ("m02", "float"),
                        ("m10", "float"), ("m11", "float"), ("m12", "float")])
    s.struct("ColorStop", [("color", "Color"), ("position", "float")])
    s.struct("FontName", [("family", "string"), ("style", "string"), ("postscript", "string")])
    s.struct("Blob", [("bytes", "byte[]")])
    s.message("ParentIndex", [("guid", "GUID"), ("position", "string")])
    s.message("Number", [("value", "float"), ("units", "NumberUnits")])
    s.message("Image", [("hash", "byte[]"), ("name", "string"), ("dataBlob", "uint")])
    s.message("Paint", [("type", "PaintType"), ("color", "Color"), ("opacity", "float"), ("visible", "bool"),
                        ("blendMode", "BlendMode"), ("stops", "ColorStop[]"), ("transform", "Matrix"),
                        ("image", "Image"), ("imageScaleMode", "ImageScaleMode"), ("rotation", "float"),
                        ("scale", "float"), ("originalImageWidth", "uint"), ("originalImageHeight", "uint")])
    s.enum("BlurOpType", ["UNIFORM", "PROGRESSIVE"])
    s.message("Effect", [("type", "EffectType"), ("color", "Color"), ("offset", "Vector"), ("radius", "float"),
                         ("visible", "bool"), ("blendMode", "BlendMode"), ("spread", "float"),
                         ("showShadowBehindNode", "bool"), ("blurOpType", "BlurOpType"),
                         ("startRadius", "float"), ("startOffset", "Vector"), ("endOffset", "Vector")])
    s.message("Path", [("windingRule", "WindingRule"), ("commandsBlob", "uint"), ("styleID", "uint")])
    s.message("ArcData", [("startingAngle", "float"), ("endingAngle", "float"), ("innerRadius", "float")])
    s.message("GUIDPath", [("guids", "GUID[]")])
    s.message("TextLineData", [("lineType", "LineType"), ("indentationLevel", "uint")])
    s.message("FontMetaData", [("key", "FontName"), ("fontLineHeight", "float"), ("fontWeight", "int")])
    s.message("TextData", [("characters", "string"), ("characterStyleIDs", "uint[]"),
                           ("styleOverrideTable", "NodeChange[]"), ("lines", "TextLineData[]"),
                           ("fontMetaData", "FontMetaData[]")])
    s.message("VectorData", [("vectorNetworkBlob", "uint"), ("normalizedSize", "Vector"),
                             ("styleOverrideTable", "NodeChange[]")])
    s.message("SymbolData", [("symbolID", "GUID"), ("symbolOverrides", "NodeChange[]"),
                             ("uniformScaleFactor", "float")])
    s.message("ComponentPropRef", [("defID", "GUID"), ("componentPropNodeField", "ComponentPropNodeField"),
                                   ("isDeleted", "bool")])
    s.message("ComponentPropValue", [("boolValue", "bool"), ("textValue", "TextData"), ("guidValue", "GUID")])
    s.message("ComponentPropAssignment", [("defID", "GUID"), ("value", "ComponentPropValue")])
    s.enum("ExportImageType", ["PNG", "JPEG", "SVG", "PDF", "BMP"])
    s.enum("ExportConstraintType", ["CONTENT_SCALE", "CONTENT_WIDTH", "CONTENT_HEIGHT"])
    s.message("ExportConstraint", [("type", "ExportConstraintType"), ("value", "float")])
    s.message("ExportSettings", [("suffix", "string"), ("imageType", "ExportImageType"),
                                 ("constraint", "ExportConstraint"), ("svgOutlineText", "bool")])
    s.message("NodeChange", [
        ("guid", "GUID"), ("phase", "NodePhase"), ("parentIndex", "ParentIndex"), ("type", "NodeType"),
        ("name", "string"), ("visible", "bool"), ("locked", "bool"), ("opacity", "float"),
        ("blendMode", "BlendMode"), ("size", "Vector"), ("transform", "Matrix"), ("mask", "bool"),
        ("maskType", "MaskType"), ("backgroundColor", "Color"), ("backgroundOpacity", "float"),
        ("backgroundEnabled", "bool"), ("internalOnly", "bool"),
        ("cornerRadius", "float"), ("rectangleTopLeftCornerRadius", "float"),
        ("rectangleTopRightCornerRadius", "float"), ("rectangleBottomLeftCornerRadius", "float"),
        ("rectangleBottomRightCornerRadius", "float"), ("rectangleCornerRadiiIndependent", "bool"),
        ("cornerSmoothing", "float"),
        ("strokeWeight", "float"), ("strokeAlign", "StrokeAlign"), ("strokeCap", "StrokeCap"),
        ("strokeJoin", "StrokeJoin"), ("dashPattern", "float[]"), ("miterLimit", "float"),
        ("borderTopWeight", "float"), ("borderBottomWeight", "float"), ("borderLeftWeight", "float"),
        ("borderRightWeight", "float"), ("borderStrokeWeightsIndependent", "bool"),
        ("fillPaints", "Paint[]"), ("strokePaints", "Paint[]"), ("effects", "Effect[]"),
        ("fillGeometry", "Path[]"), ("strokeGeometry", "Path[]"),
        ("frameMaskDisabled", "bool"), ("resizeToFit", "bool"),
        ("horizontalConstraint", "ConstraintType"), ("verticalConstraint", "ConstraintType"),
        ("stackMode", "StackMode"), ("stackSpacing", "float"),
        ("booleanOperation", "BooleanOperation"), ("count", "uint"), ("starInnerScale", "float"),
        ("arcData", "ArcData"), ("vectorData", "VectorData"),
        ("fontSize", "float"), ("fontName", "FontName"), ("lineHeight", "Number"), ("letterSpacing", "Number"),
        ("textAlignHorizontal", "TextAlignHorizontal"), ("textAlignVertical", "TextAlignVertical"),
        ("textCase", "TextCase"), ("textDecoration", "TextDecoration"), ("textAutoResize", "TextAutoResize"),
        ("textTruncation", "TextTruncation"), ("maxLines", "int"), ("paragraphSpacing", "float"),
        ("textData", "TextData"),
        ("symbolData", "SymbolData"), ("derivedSymbolData", "NodeChange[]"), ("overrideKey", "GUID"),
        ("guidPath", "GUIDPath"), ("overriddenSymbolID", "GUID"),
        ("componentPropRefs", "ComponentPropRef[]"), ("componentPropAssignments", "ComponentPropAssignment[]"),
        ("styleID", "uint"), ("exportSettings", "ExportSettings[]"),
    ])
    s.message("Message", [("type", "MessageType"), ("sessionID", "uint"), ("ackID", "uint"),
                          ("nodeChanges", "NodeChange[]"), ("blobs", "Blob[]")])
    return s


# ── value helpers ────────────────────────────────────────────────────────

def guid(local: int, session: int = 1) -> dict[str, int]:
    return {"sessionID": session, "localID": local}


def color(r: float, g: float, b: float, a: float = 1.0) -> dict[str, float]:
    return {"r": r, "g": g, "b": b, "a": a}


def hexcolor(text: str, a: float = 1.0) -> dict[str, float]:
    text = text.lstrip("#")
    return color(int(text[0:2], 16) / 255, int(text[2:4], 16) / 255, int(text[4:6], 16) / 255, a)


def matrix(tx: float = 0.0, ty: float = 0.0, degrees: float = 0.0, *, a=None, b=None, c=None, d=None):
    if a is None:
        angle = math.radians(degrees)
        a, b, c, d = math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle)
    return {"m00": a, "m01": c, "m02": tx, "m10": b, "m11": d, "m12": ty}


def vector(x: float, y: float) -> dict[str, float]:
    return {"x": x, "y": y}


def solid(text: str, opacity: float = 1.0, **extra) -> dict[str, Any]:
    return {"type": "SOLID", "color": hexcolor(text), "opacity": opacity, "visible": True, "blendMode": "NORMAL",
            **extra}


def gradient(kind: str, stops: list[tuple[float, str]], transform: dict[str, float] | None = None,
             opacity: float = 1.0) -> dict[str, Any]:
    return {"type": kind, "stops": [{"color": hexcolor(value), "position": position} for position, value in stops],
            "transform": transform or matrix(), "opacity": opacity, "visible": True, "blendMode": "NORMAL"}


def commands(*ops: tuple) -> bytes:
    """("M", x, y), ("L", x, y), ("Q", x1, y1, x, y), ("C", x1, y1, x2, y2, x, y), ("Z",)."""
    codes = {"Z": 0, "M": 1, "L": 2, "Q": 3, "C": 4}
    out = bytearray()
    for op in ops:
        out.append(codes[op[0]])
        for value in op[1:]:
            out += struct.pack("<f", value)
    return bytes(out)


def network(vertices: list[tuple[float, float] | tuple[float, float, int]],
            segments: list[tuple], regions: list[tuple[str, list[list[int]]] | tuple[str, list[list[int]], int]]
            ) -> bytes:
    """vertices (x, y[, style]); segments (start, end[, tsx, tsy, tex, tey]); regions (winding, loops[, style])."""
    out = bytearray(struct.pack("<III", len(vertices), len(segments), len(regions)))
    for vertex in vertices:
        style = vertex[2] if len(vertex) > 2 else 0
        out += struct.pack("<Iff", style, vertex[0], vertex[1])
    for segment in segments:
        start, end = segment[0], segment[1]
        tsx, tsy, tex, tey = (segment[2:6] if len(segment) >= 6 else (0, 0, 0, 0))
        out += struct.pack("<IIffIff", 0, start, tsx, tsy, end, tex, tey)
    for region in regions:
        winding, loops = region[0], region[1]
        style = region[2] if len(region) > 2 else 0
        out += struct.pack("<II", (style << 1) | (1 if winding == "NONZERO" else 0), len(loops))
        for loop in loops:
            out += struct.pack("<I", len(loop)) + struct.pack(f"<{len(loop)}I", *loop)
    return bytes(out)


def png(width: int, height: int, pixel) -> bytes:
    """An RGBA PNG; `pixel(x, y)` returns (r, g, b, a) bytes."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes(pixel(x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


# ── the file ─────────────────────────────────────────────────────────────

class FigWriter:
    def __init__(self, *, page_name: str = "Page 1") -> None:
        self.schema = figma_schema()
        self.nodes: list[dict[str, Any]] = []
        self.blobs: list[bytes] = []
        self.images: dict[str, bytes] = {}
        self._next = 10
        self._positions: dict[int, int] = {}
        self.document = self.add("DOCUMENT", "Document", None, guid_value=guid(0, 0))
        self.page = self.add("CANVAS", page_name, self.document, backgroundColor=hexcolor("#f5f5f5"),
                             backgroundOpacity=1.0, backgroundEnabled=True)

    def add(self, node_type: str, name: str, parent: dict[str, int] | None, *, guid_value=None,
            **fields: Any) -> dict[str, int]:
        if guid_value is None:
            guid_value = guid(self._next)
            self._next += 1
        node: dict[str, Any] = {"guid": guid_value, "phase": "CREATED", "type": node_type, "name": name,
                                "visible": True, "opacity": 1.0}
        if parent is not None:
            key = parent["localID"] * 7919 + parent["sessionID"]
            index = self._positions.get(key, 0)
            self._positions[key] = index + 1
            node["parentIndex"] = {"guid": parent, "position": _position(index)}
        node.update(fields)
        self.nodes.append(node)
        return guid_value

    def blob(self, data: bytes) -> int:
        self.blobs.append(bytes(data))
        return len(self.blobs) - 1

    def image(self, data: bytes) -> bytes:
        import hashlib
        digest = hashlib.sha1(data).digest()
        self.images[digest.hex()] = data
        return digest

    def canvas(self, *, version: int = 48, compression: str = "deflate") -> bytes:
        schema = self.schema.encode()
        message = self.schema.encode_value("Message", {
            "type": "NODE_CHANGES", "sessionID": 0, "ackID": 0, "nodeChanges": self.nodes,
            "blobs": [{"bytes": blob} for blob in self.blobs]})
        return pack_canvas(schema, message, version=version, compression=compression)

    def build(self, *, zipped: bool = True, version: int = 48, compression: str = "deflate") -> bytes:
        canvas = self.canvas(version=version, compression=compression)
        if not zipped:
            return canvas
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("canvas.fig", canvas)
            archive.writestr("meta.json", '{"file_name": "Fixture", "client_meta": {}}')
            for sha1, data in self.images.items():
                archive.writestr(f"images/{sha1}", data)
        return stream.getvalue()


def _position(index: int) -> str:
    # Figma's fractional positions compare as strings; these do too.
    digits = "!\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}"
    text = ""
    value = index
    for _ in range(3):
        text = digits[value % len(digits)] + text
        value //= len(digits)
    return text


def deflate_raw(data: bytes) -> bytes:
    compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def pack_canvas(schema: bytes, message: bytes, *, version: int = 48, compression: str = "deflate",
                magic: bytes = b"fig-kiwi") -> bytes:
    if compression == "zstd":
        from compression import zstd
        packed_message = zstd.compress(message)
    else:
        packed_message = deflate_raw(message)
    packed_schema = deflate_raw(schema)
    return (magic + struct.pack("<I", version) + struct.pack("<I", len(packed_schema)) + packed_schema
            + struct.pack("<I", len(packed_message)) + packed_message)


def prototype_schema():
    """Figma-shaped exported prototype schema; field tags are fixture-local."""
    schema=figma_schema()
    schema.enum('InteractionType',['ON_CLICK','AFTER_TIMEOUT','MOUSE_IN','MOUSE_OUT','ON_HOVER','MOUSE_DOWN','MOUSE_UP','ON_PRESS','NONE','DRAG','ON_KEY_DOWN'])
    schema.enum('ConnectionType',['NONE','INTERNAL_NODE','URL','BACK','CLOSE','SET_VARIABLE','CONDITIONAL'])
    schema.enum('NavigationType',['NAVIGATE','OVERLAY','SWAP','SWAP_STATE','SCROLL_TO'])
    schema.enum('TransitionType',['INSTANT_TRANSITION','DISSOLVE','FADE','SLIDE_FROM_LEFT','SLIDE_FROM_RIGHT','SLIDE_FROM_TOP','SLIDE_FROM_BOTTOM','PUSH_FROM_LEFT','PUSH_FROM_RIGHT','PUSH_FROM_TOP','PUSH_FROM_BOTTOM','MOVE_FROM_LEFT','MOVE_FROM_RIGHT','MOVE_FROM_TOP','MOVE_FROM_BOTTOM','SLIDE_OUT_TO_LEFT','SLIDE_OUT_TO_RIGHT','SLIDE_OUT_TO_TOP','SLIDE_OUT_TO_BOTTOM','MOVE_OUT_TO_LEFT','MOVE_OUT_TO_RIGHT','MOVE_OUT_TO_TOP','MOVE_OUT_TO_BOTTOM','MAGIC_MOVE','SMART_ANIMATE','SCROLL_ANIMATE'])
    schema.enum('EasingType',['IN_CUBIC','OUT_CUBIC','INOUT_CUBIC','LINEAR','CUSTOM_CUBIC','SPRING'])
    schema.enum('ScrollDirection',['NONE','HORIZONTAL','VERTICAL','BOTH'])
    schema.enum('ScrollBehavior',['SCROLLS','FIXED_WHEN_CHILD_OF_SCROLLING_FRAME','STICKY_SCROLLS'])
    schema.enum('OverlayPositionType',['CENTER','TOP_LEFT','TOP_CENTER','TOP_RIGHT','BOTTOM_LEFT','BOTTOM_CENTER','BOTTOM_RIGHT','MANUAL'])
    schema.enum('OverlayBackgroundInteraction',['NONE','CLOSE_ON_CLICK_OUTSIDE'])
    schema.enum('OverlayBackgroundType',['NONE','SOLID_COLOR'])
    schema.message('KeyTrigger',[('keyCodes','int[]')])
    schema.message('PrototypeEvent',[('interactionType','InteractionType'),('transitionTimeout','float'),('keyTrigger','KeyTrigger')])
    schema.message('PrototypeAction',[('transitionNodeID','GUID'),('connectionType','ConnectionType'),('navigationType','NavigationType'),('transitionType','TransitionType'),('transitionDuration','float'),('easingType','EasingType'),('transitionShouldSmartAnimate','bool'),('transitionPreserveScroll','bool'),('connectionURL','string'),('overlayRelativePosition','Vector')])
    schema.message('PrototypeInteraction',[('id','GUID'),('event','PrototypeEvent'),('actions','PrototypeAction[]'),('isDeleted','bool')])
    schema.message('PrototypeStartingPoint',[('name','string')])
    schema.message('OverlayBackgroundAppearance',[('backgroundType','OverlayBackgroundType'),('backgroundColor','Color')])
    schema.enum('VariableResolvedDataType',['BOOLEAN','FLOAT','STRING','COLOR'])
    schema.enum('ExpressionFunction',['ADDITION','SUBTRACTION','MULTIPLY','DIVIDE','EQUALS','NOT_EQUAL','LESS_THAN','LESS_THAN_OR_EQUAL','GREATER_THAN','GREATER_THAN_OR_EQUAL','AND','OR','NOT','STRINGIFY','TERNARY','NEGATE','IS_TRUTHY'])
    schema.message('VariableID',[('guid','GUID')])
    schema.message('Expression',[('expressionFunction','ExpressionFunction'),('expressionArguments','VariableData[]')])
    schema.message('VariableAnyValue',[('boolValue','bool'),('textValue','string'),('floatValue','float'),('colorValue','Color'),('alias','VariableID'),('expressionValue','Expression')])
    schema.message('VariableData',[('value','VariableAnyValue')])
    schema.message('VariableDataValuesEntry',[('modeID','GUID'),('variableData','VariableData')])
    schema.message('VariableDataValues',[('entries','VariableDataValuesEntry[]')])
    schema.message('ConditionalActions',[('condition','VariableData'),('actions','PrototypeAction[]')])
    name,kind,fields=schema.definitions[schema.index('PrototypeAction')]
    schema.definitions[schema.index('PrototypeAction')]=(name,kind,fields+[(key,value,False,len(fields)+index+1) for index,(key,value) in enumerate([('targetVariableID','VariableID'),('targetVariableData','VariableData')])]+[('conditionalActions','ConditionalActions',True,len(fields)+3)])
    name,kind,fields=schema.definitions[schema.index('NodeChange')]
    extras=[('variableResolvedType','VariableResolvedDataType'),('variableDataValues','VariableDataValues'),('variableData','VariableData'),('prototypeInteractions','PrototypeInteraction[]'),('transitionNodeID','GUID'),('transitionType','TransitionType'),('transitionDuration','float'),('connectionType','ConnectionType'),('prototypeStartNodeID','GUID'),('prototypeStartingPoint','PrototypeStartingPoint'),('scrollDirection','ScrollDirection'),('scrollBehavior','ScrollBehavior'),('overlayPositionType','OverlayPositionType'),('overlayBackgroundInteraction','OverlayBackgroundInteraction'),('overlayBackgroundAppearance','OverlayBackgroundAppearance')]
    schema.definitions[schema.index('NodeChange')]=(name,kind,fields+[(key,value.rstrip('[]'),value.endswith('[]'),len(fields)+index+1) for index,(key,value) in enumerate(extras)])
    return schema
