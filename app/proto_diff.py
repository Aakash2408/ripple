from __future__ import annotations
"""
ripple/app/proto_diff.py

Protobuf Diff Engine — detects breaking changes in .proto files.

Breaking changes in Protobuf:
- Field removed (consumers still reference it)
- Field number changed (binary wire incompatibility)
- Field type changed (deserialization failure)
- Required field added (proto2) / field made non-optional
- Message renamed (all imports break)
- Enum value removed (consumers switching on it break)
- RPC method removed or signature changed (every caller breaks)

PARSER NOTES
------------
The previous implementation used r'message\\s+(\\w+)\\s*\\{([^}]*)\\}'.
`[^}]*` cannot cross a '}', so a nested `message`, an inner `enum`, or a
`oneof` truncated the body at the first inner brace and every field after
it was invisible -- Ripple silently reported "no breaking changes" on a
schema that had just broken its consumers. Comments were also never
stripped, so `// string phone = 2;` parsed as a live field and deleting
that comment fabricated a breaking change.

This version uses brace-aware extraction from schema_parse and strips
comments first. It is still not a full protobuf compiler (no import
resolution across files), but it is correct for the constructs that appear
in real single-file schemas.
"""

import re
from dataclasses import dataclass, field as dc_field

from .diff_engine import BreakingChange
from .schema_parse import strip_comments, extract_blocks, remove_nested_blocks


# Blocks that can appear nested inside a message and must not have their
# fields attributed to the parent.
_NESTED_KEYWORDS = ("message", "enum", "oneof")

# proto scalar/þknown labels
_LABELS = ("optional", "required", "repeated")


@dataclass
class ProtoField:
    """A field in a protobuf message."""
    name: str
    type: str
    number: int
    label: str  # "optional", "required", "repeated"


@dataclass
class ProtoMessage:
    """A protobuf message definition."""
    name: str
    fields: dict[str, ProtoField]  # field_name -> ProtoField
    reserved_numbers: set = dc_field(default_factory=set)
    reserved_names: set = dc_field(default_factory=set)


@dataclass
class ProtoEnum:
    """A protobuf enum definition."""
    name: str
    values: dict[str, int]  # value_name -> number


@dataclass
class ProtoRpc:
    """A single RPC method within a service."""
    name: str
    request_type: str
    response_type: str
    client_streaming: bool = False
    server_streaming: bool = False

    def signature(self) -> str:
        req = ("stream " if self.client_streaming else "") + self.request_type
        res = ("stream " if self.server_streaming else "") + self.response_type
        return f"({req}) returns ({res})"


@dataclass
class ProtoService:
    """A protobuf service definition."""
    name: str
    rpcs: dict[str, ProtoRpc]  # rpc_name -> ProtoRpc


@dataclass
class ProtoSchema:
    """Everything parsed out of a .proto file."""
    messages: dict[str, ProtoMessage] = dc_field(default_factory=dict)
    enums: dict[str, ProtoEnum] = dc_field(default_factory=dict)
    services: dict[str, ProtoService] = dc_field(default_factory=dict)


# ------------------------------------------------------------------ parse
_FIELD_RE = re.compile(
    r'(?:(optional|required|repeated)\s+)?'      # optional label
    r'((?:map\s*<[^>]+>)|[\w.]+)'                # type (incl. map<k,v>)
    r'\s+(\w+)\s*=\s*(\d+)',                     # name = number
)

_RESERVED_NUM_RE = re.compile(r'reserved\s+([0-9,\s\-]+);')
_RESERVED_NAME_RE = re.compile(r'reserved\s+((?:"\w+"\s*,?\s*)+);')

_RPC_RE = re.compile(
    r'\brpc\s+(\w+)\s*\(\s*(stream\s+)?([\w.]+)\s*\)\s*'
    r'returns\s*\(\s*(stream\s+)?([\w.]+)\s*\)',
)

_ENUM_VALUE_RE = re.compile(r'(\w+)\s*=\s*(\d+)')


def _parse_fields(body: str) -> dict[str, ProtoField]:
    """Parse field declarations from a message body.

    Nested blocks are blanked first so their fields are not misattributed
    to the parent message.
    """
    own = remove_nested_blocks(body, _NESTED_KEYWORDS)
    fields = {}
    for match in _FIELD_RE.finditer(own):
        label = match.group(1) or "optional"
        ftype = re.sub(r'\s+', '', match.group(2))
        fname = match.group(3)
        # `reserved 2, 3;` and option lines must not look like fields
        if fname in _LABELS:
            continue
        fields[fname] = ProtoField(
            name=fname, type=ftype, number=int(match.group(4)), label=label
        )
    return fields


def _parse_reserved(body: str) -> tuple[set, set]:
    """Collect reserved field numbers and names."""
    numbers, names = set(), set()
    for m in _RESERVED_NUM_RE.finditer(body):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:  # range: "5 - 9"
                bits = [b.strip() for b in part.split("-")]
                try:
                    lo, hi = int(bits[0]), int(bits[1])
                    numbers.update(range(lo, hi + 1))
                except (ValueError, IndexError):
                    continue
            else:
                try:
                    numbers.add(int(part))
                except ValueError:
                    continue
    for m in _RESERVED_NAME_RE.finditer(body):
        names.update(re.findall(r'"(\w+)"', m.group(1)))
    return numbers, names


def _parse_messages(text: str, into: ProtoSchema, prefix: str = "") -> None:
    """Recursively parse message blocks, including nested messages."""
    for name, body in extract_blocks(text, "message"):
        full = f"{prefix}{name}"
        reserved_nums, reserved_names = _parse_reserved(body)
        into.messages[full] = ProtoMessage(
            name=full,
            fields=_parse_fields(body),
            reserved_numbers=reserved_nums,
            reserved_names=reserved_names,
        )
        # Nested messages and enums are real types consumers can reference
        _parse_messages(body, into, prefix=f"{full}.")
        _parse_enums(body, into, prefix=f"{full}.")
        # oneof members are fields of the parent for compatibility purposes
        for _, oneof_body in extract_blocks(body, "oneof"):
            into.messages[full].fields.update(_parse_fields(oneof_body))


def _parse_enums(text: str, into: ProtoSchema, prefix: str = "") -> None:
    for name, body in extract_blocks(text, "enum"):
        values = {}
        for m in _ENUM_VALUE_RE.finditer(body):
            values[m.group(1)] = int(m.group(2))
        into.enums[f"{prefix}{name}"] = ProtoEnum(name=f"{prefix}{name}", values=values)


def _parse_services(text: str, into: ProtoSchema) -> None:
    for name, body in extract_blocks(text, "service"):
        rpcs = {}
        for m in _RPC_RE.finditer(body):
            rpcs[m.group(1)] = ProtoRpc(
                name=m.group(1),
                request_type=m.group(3),
                response_type=m.group(5),
                client_streaming=bool(m.group(2)),
                server_streaming=bool(m.group(4)),
            )
        into.services[name] = ProtoService(name=name, rpcs=rpcs)


def parse_proto_schema(content: str) -> ProtoSchema:
    """Parse a .proto file into messages, enums and services."""
    clean = strip_comments(content, hash_comments=False)
    schema = ProtoSchema()
    _parse_messages(clean, schema)
    _parse_enums(clean, schema)
    _parse_services(clean, schema)
    return schema


def parse_proto(content: str) -> dict[str, ProtoMessage]:
    """Backwards-compatible entry point: messages only."""
    return parse_proto_schema(content).messages


# ------------------------------------------------------------------- diff
def _bc(change_type: str, path: str, method: str, field_name: str,
        field_type: str, location: str, description: str) -> BreakingChange:
    return BreakingChange(
        change_type=change_type,
        path=path,
        method=method,
        field_name=field_name,
        field_type=field_type,
        location=location,
        severity="breaking",
        description=description,
    )


def diff_proto(old_content: str, new_content: str,
               file_path: str = "schema.proto") -> list[BreakingChange]:
    """Compare two .proto files and return breaking changes."""
    old = parse_proto_schema(old_content)
    new = parse_proto_schema(new_content)

    changes = []
    changes.extend(_diff_messages(old, new, file_path))
    changes.extend(_diff_enums(old, new, file_path))
    changes.extend(_diff_services(old, new, file_path))
    return changes


def _diff_messages(old: ProtoSchema, new: ProtoSchema,
                   file_path: str) -> list[BreakingChange]:
    changes = []
    for msg_name, old_msg in old.messages.items():
        new_msg = new.messages.get(msg_name)

        if new_msg is None:
            # Renamed (same field shape under a different name) or removed
            renamed_to = _find_rename(old_msg, new.messages, old.messages)
            if renamed_to:
                changes.append(_bc(
                    "message_renamed", file_path, msg_name, msg_name, "message",
                    "message",
                    f"Message '{msg_name}' renamed to '{renamed_to}' — "
                    f"all imports and references break",
                ))
            else:
                changes.append(_bc(
                    "message_removed", file_path, msg_name, msg_name, "message",
                    "message",
                    f"Message '{msg_name}' was removed — consumers importing it break",
                ))
            continue

        for fname, old_field in old_msg.fields.items():
            new_field = new_msg.fields.get(fname)

            if new_field is None:
                # Reserving a removed field is correct protobuf hygiene, but
                # it is still breaking for source-level consumers.
                reserved = (old_field.number in new_msg.reserved_numbers
                            or fname in new_msg.reserved_names)
                note = " (number reserved — wire-safe, but source consumers still break)" if reserved else ""
                changes.append(_bc(
                    "field_removed", file_path, msg_name, fname, old_field.type,
                    "message_field",
                    f"Field '{fname}' removed from message '{msg_name}'{note}",
                ))
                continue

            if new_field.type != old_field.type:
                changes.append(_bc(
                    "field_type_changed", file_path, msg_name, fname, new_field.type,
                    "message_field",
                    f"Field '{fname}' type changed from '{old_field.type}' "
                    f"to '{new_field.type}' — deserialization fails",
                ))

            if new_field.number != old_field.number:
                changes.append(_bc(
                    "field_number_changed", file_path, msg_name, fname, new_field.type,
                    "message_field",
                    f"Field '{fname}' number changed from {old_field.number} "
                    f"to {new_field.number} — wire format incompatible",
                ))

        # A newly added required field breaks existing producers (proto2)
        for fname, new_field in new_msg.fields.items():
            if fname not in old_msg.fields and new_field.label == "required":
                changes.append(_bc(
                    "required_field_added", file_path, msg_name, fname,
                    new_field.type, "message_field",
                    f"Required field '{fname}' added to '{msg_name}' — "
                    f"existing producers omit it",
                ))
    return changes


def _diff_enums(old: ProtoSchema, new: ProtoSchema,
                file_path: str) -> list[BreakingChange]:
    changes = []
    for enum_name, old_enum in old.enums.items():
        new_enum = new.enums.get(enum_name)
        if new_enum is None:
            changes.append(_bc(
                "enum_removed", file_path, enum_name, enum_name, "enum", "enum",
                f"Enum '{enum_name}' was removed — consumers referencing it break",
            ))
            continue
        for value_name, number in old_enum.values.items():
            if value_name not in new_enum.values:
                changes.append(_bc(
                    "enum_value_removed", file_path, enum_name, value_name,
                    "enum_value", "enum",
                    f"Enum value '{value_name}' removed from '{enum_name}' — "
                    f"consumers switching on it break",
                ))
            elif new_enum.values[value_name] != number:
                changes.append(_bc(
                    "enum_value_changed", file_path, enum_name, value_name,
                    "enum_value", "enum",
                    f"Enum value '{value_name}' number changed from {number} "
                    f"to {new_enum.values[value_name]} — wire incompatible",
                ))
    return changes


def _diff_services(old: ProtoSchema, new: ProtoSchema,
                   file_path: str) -> list[BreakingChange]:
    """Service/RPC changes were previously not detected AT ALL.

    Removing an rpc breaks every caller -- the most severe change in a
    gRPC contract -- and the old parser never looked at `service` blocks.
    """
    changes = []
    for svc_name, old_svc in old.services.items():
        new_svc = new.services.get(svc_name)
        if new_svc is None:
            changes.append(_bc(
                "service_removed", file_path, svc_name, svc_name, "service",
                "service",
                f"Service '{svc_name}' was removed — all clients break",
            ))
            continue
        for rpc_name, old_rpc in old_svc.rpcs.items():
            new_rpc = new_svc.rpcs.get(rpc_name)
            if new_rpc is None:
                changes.append(_bc(
                    "rpc_removed", file_path, svc_name, rpc_name, "rpc", "service",
                    f"RPC '{svc_name}.{rpc_name}' was removed — every caller breaks",
                ))
                continue
            if (new_rpc.request_type != old_rpc.request_type
                    or new_rpc.response_type != old_rpc.response_type
                    or new_rpc.client_streaming != old_rpc.client_streaming
                    or new_rpc.server_streaming != old_rpc.server_streaming):
                changes.append(_bc(
                    "rpc_signature_changed", file_path, svc_name, rpc_name,
                    "rpc", "service",
                    f"RPC '{svc_name}.{rpc_name}' signature changed from "
                    f"{old_rpc.signature()} to {new_rpc.signature()}",
                ))
    return changes


def _find_rename(old_msg: ProtoMessage, new_messages: dict,
                 old_messages: dict) -> str:
    """Heuristic: a message with an identical field shape that is new."""
    old_shape = {(f.name, f.type, f.number) for f in old_msg.fields.values()}
    if not old_shape:
        return ""
    for cand_name, cand in new_messages.items():
        if cand_name in old_messages:
            continue
        cand_shape = {(f.name, f.type, f.number) for f in cand.fields.values()}
        if cand_shape == old_shape:
            return cand_name
    return ""


def format_proto_changes(changes: list[BreakingChange]) -> str:
    """Format proto breaking changes for display."""
    if not changes:
        return "✅ No breaking changes in proto file."

    lines = [f"⚠️  {len(changes)} breaking change(s) in proto:", ""]
    for c in changes:
        lines.append(f"  • [{c.change_type}] {c.description}")
    return "\n".join(lines)
