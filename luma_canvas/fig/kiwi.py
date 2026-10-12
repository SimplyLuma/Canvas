# SPDX-License-Identifier: MPL-2.0
"""A bounded decoder for Kiwi binary schemas and the messages they describe.

Kiwi is the schema-based binary format Figma stores documents in (format by
Evan Wallace, https://github.com/evanw/kiwi, MIT). This is an independent
implementation written from the format description; no Kiwi source is copied.

A `.fig` file carries its own schema, so the decoder is built from that schema
at run time: every definition becomes a small Python function, generated once,
which is several times faster than interpreting the schema per value. Field
names only ever enter the generated source through `repr()`, so a hostile
schema cannot inject code.

Every read is bounds checked. Array lengths are limited by the bytes that
remain (every element needs at least one byte), recursion is limited, and any
malformed input raises `KiwiError`, never an arbitrary exception or an
unbounded allocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import Any, Callable, Iterable

PRIMITIVES = ("bool", "byte", "int", "uint", "float", "string", "int64", "uint64")
KINDS = ("ENUM", "STRUCT", "MESSAGE")

MAX_DEFINITIONS = 20_000
MAX_FIELDS = 20_000
# Arrays of zero-width structs are the one case bytes cannot bound.
MAX_EMPTY_ARRAY = 1 << 16


class KiwiError(ValueError):
    """The data is not valid Kiwi for its schema."""


@dataclass(slots=True)
class Field:
    name: str
    type: int          # < 0: primitive (~type indexes PRIMITIVES); >= 0: definition index
    is_array: bool
    value: int         # message field id or enum value


@dataclass(slots=True)
class Definition:
    name: str
    kind: str
    fields: list[Field] = field(default_factory=list)


@dataclass(slots=True)
class Schema:
    definitions: list[Definition]

    def index(self, name: str) -> int:
        for position, definition in enumerate(self.definitions):
            if definition.name == name:
                return position
        raise KiwiError(f"the schema has no {name} definition")

    def definition(self, name: str) -> Definition | None:
        return next((item for item in self.definitions if item.name == name), None)


# ── primitive reads ──────────────────────────────────────────────────────

_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")


def read_uint(d: bytes, i: int) -> tuple[int, int]:
    b = d[i]
    i += 1
    if b < 128:
        return b, i
    value = b & 127
    shift = 7
    while True:
        b = d[i]
        i += 1
        value |= (b & 127) << shift
        shift += 7
        if b < 128 or shift >= 35:
            return value & 0xFFFFFFFF, i


def read_int(d: bytes, i: int) -> tuple[int, int]:
    value, i = read_uint(d, i)
    return (-(value >> 1) - 1 if value & 1 else value >> 1), i


def read_uint64(d: bytes, i: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        b = d[i]
        i += 1
        if b & 128 and shift < 56:
            value |= (b & 127) << shift
            shift += 7
            continue
        return value | (b << shift), i


def read_int64(d: bytes, i: int) -> tuple[int, int]:
    value, i = read_uint64(d, i)
    return (-(value >> 1) - 1 if value & 1 else value >> 1), i


def read_float(d: bytes, i: int) -> tuple[float, int]:
    if d[i] == 0:
        return 0.0, i + 1
    bits = _U32.unpack_from(d, i)[0]
    bits = ((bits << 23) | (bits >> 9)) & 0xFFFFFFFF
    return _F32.unpack(_U32.pack(bits))[0], i + 4


def read_string(d: bytes, i: int) -> tuple[str, int]:
    end = d.index(0, i)
    return d[i:end].decode("utf-8", "replace"), end + 1


def read_bool(d: bytes, i: int) -> tuple[bool, int]:
    return d[i] != 0, i + 1


def read_byte(d: bytes, i: int) -> tuple[int, int]:
    return d[i], i + 1


def read_bytes(d: bytes, i: int) -> tuple[bytes, int]:
    length, i = read_uint(d, i)
    end = i + length
    if end > len(d):
        raise KiwiError("a byte array runs past the end of the data")
    return d[i:end], end


# Skipping never builds a value: long glyph tables and geometry caches that
# the importer does not read cost almost nothing.

def skip_float(d: bytes, i: int) -> tuple[None, int]:
    if d[i] == 0:
        return None, i + 1
    if i + 4 > len(d):
        raise KiwiError("a number runs past the end of the data")
    return None, i + 4


def skip_string(d: bytes, i: int) -> tuple[None, int]:
    return None, d.index(0, i) + 1


def skip_bytes(d: bytes, i: int) -> tuple[None, int]:
    length, i = read_uint(d, i)
    if i + length > len(d):
        raise KiwiError("a byte array runs past the end of the data")
    return None, i + length


def skip_one(d: bytes, i: int) -> tuple[None, int]:
    d[i]
    return None, i + 1


def skip_varint(d: bytes, i: int) -> tuple[None, int]:
    return None, read_uint64(d, i)[1]


READERS: dict[str, Callable[[bytes, int], tuple[Any, int]]] = {
    "bool": read_bool, "byte": read_byte, "int": read_int, "uint": read_uint,
    "float": read_float, "string": read_string, "int64": read_int64, "uint64": read_uint64,
}
SKIPPERS: dict[str, Callable[[bytes, int], tuple[None, int]]] = {
    "bool": skip_one, "byte": skip_one, "int": skip_varint, "uint": skip_varint,
    "float": skip_float, "string": skip_string, "int64": skip_varint, "uint64": skip_varint,
}


# ── the binary schema ────────────────────────────────────────────────────

def decode_schema(data: bytes) -> Schema:
    """Read a binary schema (kiwi `encodeBinarySchema`)."""
    data = bytes(data)
    try:
        count, i = read_uint(data, 0)
        if count > MAX_DEFINITIONS or count > len(data):
            raise KiwiError("the schema declares too many definitions")
        definitions: list[Definition] = []
        for _ in range(count):
            name, i = read_string(data, i)
            kind_index, i = read_byte(data, i)
            if kind_index >= len(KINDS):
                raise KiwiError(f"definition {name!r} has an unknown kind")
            field_count, i = read_uint(data, i)
            if field_count > MAX_FIELDS or field_count > len(data) - i:
                raise KiwiError(f"definition {name!r} declares too many fields")
            fields = []
            for _ in range(field_count):
                field_name, i = read_string(data, i)
                type_index, i = read_int(data, i)
                flags, i = read_byte(data, i)
                value, i = read_uint(data, i)
                fields.append(Field(field_name, type_index, bool(flags & 1), value))
            definitions.append(Definition(name, KINDS[kind_index], fields))
    except (IndexError, ValueError, struct.error) as error:
        if isinstance(error, KiwiError):
            raise
        raise KiwiError(f"the schema is truncated or damaged ({error})") from error
    for definition in definitions:
        if definition.kind == "ENUM":
            continue
        for item in definition.fields:
            if item.type < 0:
                if ~item.type >= len(PRIMITIVES):
                    raise KiwiError(f"field {definition.name}.{item.name} has an unknown type")
            elif item.type >= len(definitions):
                raise KiwiError(f"field {definition.name}.{item.name} names a missing definition")
        if definition.kind == "MESSAGE":
            ids = [item.value for item in definition.fields]
            if 0 in ids or len(ids) != len(set(ids)):
                raise KiwiError(f"message {definition.name} has invalid field ids")
    return Schema(definitions)


# ── compiling a decoder ──────────────────────────────────────────────────

class Decoder:
    """Decoders for every definition of one schema.

    `skip` names fields (`"Definition.field"`) whose values are never needed:
    they are walked over without building values and appear in the result
    as `True`, so callers can still tell that they were present.

    `watch` maps an array field to a callback called with the fraction of
    the data read, every 256 elements: progress for long node lists, and a
    place to cancel (an exception raised by the callback propagates).
    """

    def __init__(self, schema: Schema, skip: Iterable[str] = (),
                 watch: dict[str, Callable[[float], None] | None] | None = None) -> None:
        self.schema = schema
        skipped = set(skip)
        watched = {name: callback for name, callback in (watch or {}).items() if callback is not None}
        definitions = schema.definitions
        namespace: dict[str, Any] = {
            "KiwiError": KiwiError, "read_uint": read_uint, "_bytes": read_bytes,
            "_skip_bytes": skip_bytes, "MAX_EMPTY_ARRAY": MAX_EMPTY_ARRAY,
        }
        for name, reader in READERS.items():
            namespace[f"R_{name}"] = reader
        for name, skipper in SKIPPERS.items():
            namespace[f"K_{name}"] = skipper

        # Which structs can take zero bytes (all fields zero-width structs).
        empty: dict[int, bool] = {}

        def zero_width(index: int, trail: tuple[int, ...] = ()) -> bool:
            if index in empty:
                return empty[index]
            definition = definitions[index]
            if definition.kind != "STRUCT" or index in trail:
                empty[index] = False
                return False
            result = all(item.type >= 0 and not item.is_array and zero_width(item.type, trail + (index,))
                         for item in definition.fields)
            empty[index] = result
            return result

        source: list[str] = []
        helpers: list[str] = []
        defined: set[str] = set()

        def element(type_index: int, skipping: bool) -> str:
            if type_index < 0:
                primitive = PRIMITIVES[~type_index]
                return f"K_{primitive}" if skipping else f"R_{primitive}"
            prefix = "SK" if skipping else "D"
            return f"{prefix}{type_index}"

        def array_reader(item: Field, skipping: bool, tick: str | None = None) -> str:
            """A function name that reads (or skips) an array of one element type."""
            if item.type < 0 and PRIMITIVES[~item.type] == "byte":
                return "_skip_bytes" if skipping else "_bytes"
            name = f"{'SA' if skipping else 'A'}{'p' if item.type < 0 else ''}{abs(item.type)}"
            if tick is not None:
                name = f"W{tick}"
            if name in defined:
                return name
            defined.add(name)
            inner = element(item.type, skipping)
            bounded = item.type >= 0 and zero_width(item.type)
            limit = "MAX_EMPTY_ARRAY" if bounded else "len(d) - i"
            helpers.append(f"def {name}(d, i):")
            helpers.append("    n, i = read_uint(d, i)")
            helpers.append(f"    if n > {limit}:")
            helpers.append("        raise KiwiError('an array is longer than the data that holds it')")
            if skipping:
                helpers.append("    for _ in range(n):")
                helpers.append(f"        _, i = {inner}(d, i)")
                helpers.append("    return None, i")
            else:
                helpers.append("    out = []")
                helpers.append("    append = out.append")
                if tick is not None:
                    helpers.append("    total = len(d) or 1")
                    helpers.append("    for k in range(n):")
                    helpers.append("        if not k & 255:")
                    helpers.append(f"            T{tick}(i / total)")
                else:
                    helpers.append("    for _ in range(n):")
                helpers.append(f"        v, i = {inner}(d, i)")
                helpers.append("        append(v)")
                helpers.append("    return out, i")
            return name

        handler_tables: list[tuple[str, dict[int, tuple[str | None, str]]]] = []
        for index, definition in enumerate(definitions):
            if definition.kind == "ENUM":
                table = {item.value: item.name for item in definition.fields}
                namespace[f"E{index}"] = table
                for prefix, body in (("D", "    v, i = read_uint(d, i)\n    return E{0}.get(v, v), i"),
                                     ("SK", "    return None, read_uint(d, i)[1]")):
                    source.append(f"def {prefix}{index}(d, i):")
                    source.append(body.format(index))
                continue
            if definition.kind == "STRUCT":
                for skipping in (False, True):
                    source.append(f"def {'SK' if skipping else 'D'}{index}(d, i):")
                    keys = []
                    for position, item in enumerate(definition.fields):
                        reader = (array_reader(item, skipping) if item.is_array
                                  else element(item.type, skipping))
                        source.append(f"    v{position}, i = {reader}(d, i)")
                        keys.append(f"{item.name!r}: v{position}")
                    if skipping:
                        source.append("    return None, i")
                    else:
                        source.append("    return {" + ", ".join(keys) + "}, i")
                continue
            # Messages dispatch on field id through a table.
            handlers: dict[int, tuple[str | None, str]] = {}
            skip_handlers: dict[int, tuple[str | None, str]] = {}
            for item in definition.fields:
                qualified = f"{definition.name}.{item.name}"
                dropped = qualified in skipped
                tick = None
                if qualified in watched and item.is_array and not dropped:
                    tick = f"{index}_{item.value}"
                    namespace[f"T{tick}"] = watched[qualified]
                reader = (array_reader(item, dropped, tick) if item.is_array
                          else element(item.type, dropped))
                handlers[item.value] = ((f"\0{item.name}" if dropped else item.name), reader)
                skipper = (array_reader(item, True) if item.is_array else element(item.type, True))
                skip_handlers[item.value] = (None, skipper)
            handler_tables.append((f"H{index}", handlers))
            handler_tables.append((f"HS{index}", skip_handlers))
            source.append(f"def D{index}(d, i):")
            source.append("    r = {}")
            source.append(f"    get = H{index}.get")
            source.append("    while True:")
            source.append("        f = d[i]")
            source.append("        i += 1")
            source.append("        if f >= 128:")
            source.append("            f, i = read_uint(d, i - 1)")
            source.append("        if f == 0:")
            source.append("            return r, i")
            source.append("        h = get(f)")
            source.append("        if h is None:")
            source.append(f"            raise KiwiError('a {definition.name!s} has an unknown field ' + str(f))"
                          if definition.name.isidentifier() else
                          "            raise KiwiError('a message has an unknown field ' + str(f))")
            source.append("        key, fn = h")
            source.append("        v, i = fn(d, i)")
            source.append("        r[key] = v")
            source.append(f"def SK{index}(d, i):")
            source.append(f"    get = HS{index}.get")
            source.append("    while True:")
            source.append("        f, i = read_uint(d, i)")
            source.append("        if f == 0:")
            source.append("            return None, i")
            source.append("        h = get(f)")
            source.append("        if h is None:")
            source.append("            raise KiwiError('a message has an unknown field')")
            source.append("        _, i = h[1](d, i)")

        code = compile("\n".join(helpers + source) + "\n", "<kiwi schema>", "exec")
        exec(code, namespace)  # noqa: S102 - generated from validated indexes and repr() names
        for table_name, handlers in handler_tables:
            table = {}
            for key, (name, reader) in handlers.items():
                function = namespace[reader]
                if name is not None and name.startswith("\0"):
                    # A skipped field records only that it was there.
                    name = name[1:]
                    function = (lambda skipper: lambda d, i: (True, skipper(d, i)[1]))(function)
                table[key] = (name, function)
            namespace[table_name] = table
        self._namespace = namespace

    def decode(self, data: bytes, root: str = "Message") -> Any:
        index = self.schema.index(root)
        reader = self._namespace[f"D{index}"]
        data = bytes(data)
        try:
            value, end = reader(data, 0)
        except KiwiError:
            raise
        except RecursionError as error:
            raise KiwiError("the data is nested too deeply") from error
        except (IndexError, ValueError, struct.error, TypeError) as error:
            raise KiwiError(f"the data is truncated or damaged ({error})") from error
        return value


def decode_message(schema: Schema, data: bytes, root: str = "Message", skip: Iterable[str] = ()) -> Any:
    return Decoder(schema, skip).decode(data, root)
