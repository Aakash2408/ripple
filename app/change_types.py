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
NON_BREAKING = "non_breaking"


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
    # A whole contract FILE or DIRECTORY was deleted, so every symbol it
    # declared is gone at once. This cannot come from a diff engine: every
    # engine has the shape diff_x(old_content, new_content, file_path) and
    # therefore never sees more than one file. It is detected at the push-event
    # layer from the payload's `removed` list.
    #
    # JUDGMENT, not MECHANICAL: consumers must drop their imports AND replace
    # whatever they were using them for, and Ripple cannot invent the
    # replacement. Deleting the import alone would leave code that does not
    # compile; deleting the call sites would silently remove behaviour.
    "remove_package": (JUDGMENT, "a whole spec file or package directory was deleted"),
    "wire_incompatible": (WIRE_ONLY, "field number/id changed -- wire break, no source fix"),
    # Adding an OPTIONAL field breaks nothing: existing consumers keep
    # compiling and keep deserializing. rag_engine's diff heuristic emits
    # 'field_added' for these, and storing them as fix patterns would pollute
    # the RAG store with entries that can never produce a fix.
    "add_optional": (NON_BREAKING, "an optional field was added -- not a breaking change"),
}


# Change types emitted OUTSIDE the diff engines.
#
# tools/audit_change_types.py discovers emitters by scanning the 10 diff
# engines. That is the right default, but it cannot see these: a diff engine has
# the shape diff_x(old_content, new_content, file_path) and therefore never
# observes a file that ceased to exist. Package and whole-file deletions are
# detected at the push-event layer instead, from the payload's `removed` list.
#
# Registered explicitly so the audit's "emitted by no engine" note stays
# meaningful. Without this, remove_package would be listed alongside
# rename_field -- conflating "emitted somewhere the audit does not scan" with
# "genuinely dead code", which is precisely the distinction the audit exists to
# draw.
EVENT_LAYER_TYPES = {
    "spec_removed": "app/webhook.py::_find_removed_specs",
    "package_removed": "app/webhook.py::_find_removed_specs",
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

    # --- whole file or directory deleted ---------------------------------
    # Emitted by the push-event layer (_find_removed_specs), NOT by a diff
    # engine -- engines compare two versions of ONE file and structurally
    # cannot see a file disappear. Before this existed, `git rm api/user.proto`
    # produced nothing at all: _find_changed_specs read only `modified` and
    # `added` from the payload, so the most severe possible change -- deleting
    # the entire contract -- was not detected, let alone fixed.
    "spec_removed": "remove_package",              # one contract file deleted
    "package_removed": "remove_package",           # a directory of contracts deleted
    "directory_removed": "remove_package",         # alias
    "module_removed": "remove_package",            # alias

    # --- emitted by rag_engine's diff heuristic during PropBench/git
    #     indexing, not by the diff engines ---------------------------------
    "field_added": "add_optional",                 # optional add: not breaking
    # NOTE: rag_engine also emits 'modified', deliberately left UNMAPPED.
    # It means "the diff changed something we could not classify", which is
    # genuinely unknown -- mapping it to an operation would invent a fix
    # strategy for an unidentified change. ingest_examples() filters it out
    # and reports the count instead of silently storing an unusable pattern.
}


def canonical_op(change_type: str) -> str:
    """Canonical operation for an engine-specific change_type.

    Falls back by suffix so a NEW engine dialect degrades to the right
    operation instead of dead-ending at "Unknown change_type".

    IDEMPOTENT. It was not, and that was a live trap: CHANGE_TYPE_MAP is keyed by
    RAW engine dialects, so an already-canonical name like "remove_field" was not a
    key, fell through every suffix heuristic ("remove_field" does not contain
    "removed"), and returned "". Callers then read the empty string as "no
    canonical op":

      * app/outcomes.py blocked_reason() rendered "no transformation exists for
        {op}" with a BLANK where the operation should be -- a user-facing reason
        that explains nothing, in the function written to abolish silence.
      * app/fix_templates.py apply_fix_template() would treat it as an unknown
        change type, leave the code unchanged, and open no PR.
      * The capability registry expects canonical ops, so routing asking about
        "remove_field" got answers about "".

    This is the falsy-read pattern that produced the phantom getattr fields, the
    wrong ANTHROPIC env var, and history_learner returning None: a lookup that
    misses returns something usable-looking instead of failing.
    """
    if not change_type:
        return ""
    ct = change_type.strip()
    if ct in CANONICAL_OPS:          # already canonical -- identity, not ""
        return ct
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
        # PACKAGE-ish first. Without this group, a plausible future dialect like
        # `removed_package` fell through to `remove_field` -- and that is the unsafe
        # direction: remove_package is JUDGMENT while remove_field is MECHANICAL, so
        # a deleted directory of contracts would have been routed into an automated
        # field-removal fix. The four dialects engines actually emit
        # (package_removed, spec_removed, directory_removed, module_removed) are in
        # CHANGE_TYPE_MAP and were never affected; this closes the fallback.
        if any(k in lowered for k in ("package", "module", "directory", "namespace",
                                      "spec", "schema_file")):
            return "remove_package"
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


def vector_for(change_type: str) -> str:
    """Which propagation vector this change travels along.

    Returned from the change type rather than passed by callers, so a caller
    cannot forget to route a package deletion correctly. Querying the wrong
    vector under-reports badly -- measured on kubernetes#109798, a symbol query
    scored 38.5% where the package query scored 90.9% on the same PR.

        "symbol"   a declared identifier was removed; consumers name it
        "package"  a file or directory was deleted; consumers reference its
                   PATH (imports) or lived inside it, and mostly name no single
                   identifier at all
    """
    return "package" if canonical_op(change_type) == "remove_package" else "symbol"


def is_fixable(change_type: str) -> bool:
    """True when a fix pattern for this type could ever produce a change.

    Used to filter the RAG store at ingest. Storing patterns for types that
    can never yield a fix -- wire-only breaks, non-breaking additions, or
    unclassified diffs -- pollutes retrieval: they would be scored against
    real changes and could win, then produce nothing.
    """
    return category(change_type) in (MECHANICAL, JUDGMENT)


#: What to CALL each operation in a PR or MR title. Exhaustive over CANONICAL_OPS,
#: and tests/test_regression.py fails if an op is added without a phrase here.
#:
#: WHY THIS IS A TABLE AND NOT AN f-STRING AT THE CALL SITE
#: The phrase "add required field" was hardcoded and then applied to all twelve
#: operations FOUR separate times:
#:
#:   fix_generator   the LLM PROMPT -- so the model ADDED a parameter when asked to
#:                   remove one, and did exactly as instructed
#:   fix_generator   the EXPLANATION shown in the PR body
#:   webhook         the GitLab and Bitbucket MR title
#:   pr_engine       the CLI title, which survived longest because the governance
#:                   audit lists that entry point as EXEMPT -- nothing watched it
#:
#: A title is not cosmetic. "Remove references to deleted field 'x'" and "Add
#: required field 'x'" are opposite instructions to a reviewer, printed directly
#: above the diff. Whoever trusts the title misreads the change.
_TITLES = {
    "remove_field":       "Remove references to deleted field '{f}'",
    "remove_enum_value":  "Remove references to deleted enum value '{f}'",
    "remove_operation":   "Remove references to deleted operation '{f}'",
    "remove_type":        "Remove references to deleted type '{f}'",
    "remove_package":     "Update references to removed package '{f}'",
    "add_required":       "Add required field '{f}'",
    "add_optional":       "Add optional field '{f}'",
    "rename_field":       "Rename field '{f}'",
    "rename_type":        "Rename type '{f}'",
    "change_field_type":  "Update type of field '{f}'",
    "restrict_schema":    "Adapt to restricted schema for '{f}'",
    # No source edit can fix a wire break, so this title should never reach a PR.
    # It is mapped anyway: an unmapped op falls to the neutral phrase, and a
    # silent fallback is how the four occurrences above spread unnoticed.
    "wire_incompatible":  "Wire-incompatible change to '{f}' -- no source fix exists",
}


def fix_title(change) -> str:
    """The PR/MR title for a breaking change, DERIVED from its operation.

    Reads DECLARED fields directly rather than via getattr with a default:
    test_no_phantom_getattr_on_breaking_change caught the first draft of this, and
    rightly -- a default turns a renamed or absent field into "no field name",
    producing a plausible wrong title instead of a loud failure.

    An unrecognised change_type gets a neutral phrase rather than raising, because
    a webhook must not 500 when a diff engine emits a dialect nobody mapped. The
    exhaustiveness test is what stops that neutral path from becoming the default.
    """
    field = change.field_name or "field"
    where = " ".join(p for p in (change.method, change.path) if p).strip()
    suffix = f" in {where}" if where else ""
    phrase = _TITLES.get(canonical_op(change.change_type or ""),
                         "Update references to '{f}'")
    return phrase.format(f=field) + suffix
