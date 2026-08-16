from __future__ import annotations
"""
ripple/app/change_types.py

Canonical taxonomy for every change_type the 10 diff engines emit.

WHY THIS EXISTS
---------------
The engines emit 47 distinct change_type strings, but they are mostly the
SAME operation in different domain vocabulary:

    field_removed  member_removed  property_removed  column_removed
    message_field_removed  response_field_removed  removed_field
        -> all "a field was removed from a container"

fix_templates.py only recognised 3 strings (field_removed, field_renamed,
type_changed) and returned "Unknown change_type" for everything else. Since
an unknown type leaves the code unchanged, fixed_code == content and NO PR is
opened -- so Ripple would detect a breaking change and then silently produce
nothing. Worst case: rpc_removed, which breaks every caller of a gRPC method.

Writing 47 handlers would be the wrong fix. This maps the vocabulary onto a
small set of canonical operations, so a template is written once per
operation and every engine's dialect routes to it.

CATEGORIES
----------
MECHANICAL   A deterministic source transform exists. Safe to automate.
WIRE_ONLY    Serialization-level break with NO source-level fix. Source code
             never references proto field numbers or thrift field ids, so
             emitting a code change would be wrong. Must report
             "detected, no source change required" -- NOT an error, and NOT
             a silent no-op.
JUDGMENT     A partial mechanical fix is possible but completing it changes
             behaviour (deleting a call site, inventing a required value).
             Ripple applies the safe part and flags the rest, the same way
             residual field references are flagged.
"""

MECHANICAL = "mechanical"
WIRE_ONLY = "wire_only"
JUDGMENT = "judgment"


# canonical operation -> (category, human description)
CANONICAL_OPS = {
    "remove_field": (MECHANICAL, "a field/member/property/column was removed"),
    "remove_type": (MECHANICAL, "a type/message/struct/table was removed"),
    "remove_enum_value": (MECHANICAL, "an enum value/symbol was removed"),
    "rename_type": (MECHANICAL, "a type/table/record was renamed"),
    "rename_field": (MECHANICAL, "a field was renamed"),
    "change_field_type": (MECHANICAL, "a field's type changed"),
    "remove_operation": (JUDGMENT, "an rpc/method/endpoint/procedure was removed"),
    "add_required": (JUDGMENT, "a required field/argument/header was added"),
    "restrict_schema": (JUDGMENT, "schema was narrowed (signature, additionalProperties)"),
    "wire_incompatible": (WIRE_ONLY, "field number/id changed -- wire break, no source fix"),
}


# every emitted change_type -> canonical operation
# Verified against all 10 engines by tools/audit_change_types.py
CHANGE_TYPE_MAP = {
    # --- remove a field within a container -------------------------------
    "field_removed": "remove_field",
    "removed_field": "remove_field",              # legacy alias
    "message_field_removed": "remove_field",       # asyncapi
    "response_field_removed": "remove_field",      # openapi
    "member_removed": "remove_field",              # smithy
    "property_removed": "remove_field",            # jsonschema
    "column_removed": "remove_field",              # prisma/sql
    "union_member_removed": "remove_field",        # graphql union arm

    # --- remove a whole type ---------------------------------------------
    "message_removed": "remove_type",              # proto / asyncapi
    "type_removed": "remove_type",                 # graphql
    "struct_removed": "remove_type",               # thrift
    "structure_removed": "remove_type",            # smithy
    "table_removed": "remove_type",                # sql
    "enum_removed": "remove_type",                 # proto

    # --- remove an enum value --------------------------------------------
    "enum_value_removed": "remove_enum_value",
    "enum_symbol_removed": "remove_enum_value",    # avro
    "enum_value_changed": "remove_enum_value",     # proto: renumbered == old gone

    # --- rename ----------------------------------------------------------
    "message_renamed": "rename_type",
    "table_renamed": "rename_type",
    "record_renamed": "rename_type",               # avro
    "field_renamed": "rename_field",
    "renamed_field": "rename_field",               # legacy alias

    # --- type changes ----------------------------------------------------
    "field_type_changed": "change_field_type",
    "type_changed": "change_field_type",           # legacy alias
    "column_type_changed": "change_field_type",
    "member_type_changed": "change_field_type",
    "property_type_changed": "change_field_type",

    # --- operation removal (judgment) ------------------------------------
    "rpc_removed": "remove_operation",
    "method_removed": "remove_operation",          # thrift
    "operation_removed": "remove_operation",       # smithy
    "procedure_removed": "remove_operation",       # trpc
    "endpoint_removed": "remove_operation",        # openapi
    "channel_removed": "remove_operation",         # asyncapi
    "server_removed": "remove_operation",          # asyncapi
    "service_removed": "remove_operation",         # proto

    # --- newly-required (judgment) ---------------------------------------
    "required_field_added": "add_required",
    "added_required_field": "add_required",         # legacy alias
    "required_property_added": "add_required",
    "required_argument_added": "add_required",
    "required_header_added": "add_required",
    "not_null_column_added": "add_required",
    "field_made_required": "add_required",
    "member_made_required": "add_required",
    "column_made_not_null": "add_required",

    # --- narrowing (judgment) --------------------------------------------
    "rpc_signature_changed": "restrict_schema",
    "procedure_type_changed": "restrict_schema",
    "input_schema_changed": "restrict_schema",
    "additional_properties_restricted": "restrict_schema",

    # --- wire-only -------------------------------------------------------
    "field_number_changed": "wire_incompatible",   # proto
    "field_id_changed": "wire_incompatible",       # thrift
}


def canonical_op(change_type: str) -> str:
    """Canonical operation for an engine-specific change_type.

    Falls back by suffix so a NEW engine dialect degrades to the right
    operation instead of dead-ending at "Unknown change_type".
    """
    if not change_type:
        return ""
    ct = change_type.strip()
    if ct in CHANGE_TYPE_MAP:
        return CHANGE_TYPE_MAP[ct]

    # Suffix heuristics for unmapped dialects
    lowered = ct.lower()
    if "renamed" in lowered:
        return "rename_type" if any(k in lowered for k in
                                    ("type", "table", "record", "message", "struct")) else "rename_field"
    if "type_changed" in lowered:
        return "change_field_type"
    if "required" in lowered or "not_null" in lowered:
        return "add_required"
    if any(k in lowered for k in ("rpc", "method", "operation", "procedure",
                                  "endpoint", "channel", "server", "service")):
        return "remove_operation"
    if "enum" in lowered and "removed" in lowered:
        return "remove_enum_value"
    if "removed" in lowered:
        return "remove_type" if any(k in lowered for k in
                                    ("type", "table", "message", "struct", "structure")) else "remove_field"
    if "number_changed" in lowered or "id_changed" in lowered:
        return "wire_incompatible"
    return ""


def category(change_type: str) -> str:
    """MECHANICAL / WIRE_ONLY / JUDGMENT, or '' if unclassifiable."""
    op = canonical_op(change_type)
    if not op:
        return ""
    return CANONICAL_OPS.get(op, ("", ""))[0]


def describe(change_type: str) -> str:
    op = canonical_op(change_type)
    if not op:
        return f"unclassified change type '{change_type}'"
    return CANONICAL_OPS.get(op, ("", ""))[1]


def all_known_change_types() -> list:
    return sorted(CHANGE_TYPE_MAP)


def is_wire_only(change_type: str) -> bool:
    """True when NO source-level fix exists or is appropriate.

    Callers need this to distinguish two situations that look identical from
    the outside, because both leave the code unchanged:

        wire-only      correctly nothing to fix -- must NOT open a PR, but
                       MUST still be reported, since a changed field number
                       silently corrupts data between old and new peers
        failure        we could not produce a fix -- a gap worth surfacing

    Without the distinction, a wire break would either be reported as a
    failure (noise) or swallowed as a no-op (silence).
    """
    return category(change_type) == WIRE_ONLY


def is_judgment(change_type: str) -> bool:
    """True when a fix needs a human decision (partial fix + marker)."""
    return category(change_type) == JUDGMENT
