from __future__ import annotations
"""
ripple/app/graphql_diff.py

GraphQL Schema Diff Engine — detects breaking changes in GraphQL schemas.

Breaking changes in GraphQL:
- Field removed from type (all queries using it break)
- Field made non-nullable (null → String! breaks optional handling)
- Type removed (all queries referencing it break)
- Argument added as required (existing queries missing it fail)
- Argument removed (queries still passing it fail validation)
- Enum value removed (clients sending it get errors)
- Union member removed

GraphQL is a HUGE market — every modern frontend team uses it.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .schema_parse import extract_blocks, _find_matching_brace

from .diff_engine import BreakingChange


@dataclass
class GraphQLField:
    """A field in a GraphQL type."""
    name: str
    type: str
    nullable: bool
    arguments: list[dict]  # [{name, type, required, raw_type, has_default}]
    # The full wrapped type as written ("[String!]!"). `type` only strips the
    # outer '!', so "[String!]" vs "[String]" and "ID" vs "Int" were compared
    # nowhere -- every type change was invisible.
    raw_type: str = ""
    has_default: bool = False


@dataclass
class GraphQLType:
    """A GraphQL type definition."""
    name: str
    kind: str  # "type", "input", "enum", "interface", "union"
    fields: dict[str, GraphQLField]
    enum_values: list[str]  # for enums
    union_members: list[str]  # for unions


def _blank_strings_and_comments(content: str) -> str:
    """`content` with every description, string and `#` comment replaced by
    spaces (newlines kept, so offsets and line structure survive).

    Descriptions were parsed as schema. On saleor's real schema, diffing the
    file against ITSELF reported two breaking changes: the field pattern read
    `Note: this API...` inside a `\"\"\"` block as a field called `Note`, and
    `{rate: null}` in an argument's description as an argument `rate`. A `#`
    inside a string is not a comment, and a quote inside a comment is not a
    string, so the three are recognised in one left-to-right pass.
    """
    out, i, n = [], 0, len(content)

    def blank(text: str) -> str:
        return re.sub(r'[^\n]', ' ', text)

    while i < n:
        if content.startswith('"""', i):
            j = i + 3
            while j < n and not (content.startswith('"""', j)
                                 and content[j - 1] != '\\'):
                j += 1
            j = min(n, j + 3)
        elif content[i] == '"':
            j = i + 1
            while j < n and content[j] not in '"\n':
                j += 2 if content[j] == '\\' else 1
            j = min(n, j + 1)
        elif content[i] == '#':
            j = content.find('\n', i)
            j = n if j == -1 else j
        else:
            j = i + 1
            out.append(content[i])
            i = j
            continue
        out.append(blank(content[i:j]))
        i = j
    return ''.join(out)


def parse_graphql(content: str) -> dict[str, GraphQLType]:
    """Parse a .graphql schema file into type definitions.

    Uses schema_parse for brace-aware block extraction and comment
    stripping. The previous r'\\{([^}]*)\\}' could not cross a nested brace,
    so a field default like `arg: Input = {x: 1}` truncated the body and hid
    every field after it. Comments were also parsed as live fields, so
    deleting a `# commented: String` line looked like a breaking change.
    """
    clean = _blank_strings_and_comments(content)

    types = {}

    for kind in ("type", "input", "interface"):
        for name, body in extract_blocks(clean, kind):
            fields = {}
            # Parse fields: name(args): Type or name: Type
            field_pattern = re.compile(r'(\w+)(?:\(([^)]*)\))?\s*:\s*([^\n]+)')

            for field_match in field_pattern.finditer(body):
                field_name = field_match.group(1)
                args_str = field_match.group(2) or ""
                tail = field_match.group(3)
                has_default = "=" in tail.split("@", 1)[0]
                raw_type = _clean_type(tail)
                field_type = raw_type.rstrip('!').strip()
                nullable = not raw_type.endswith('!')

                arguments = []
                if args_str:
                    arg_pattern = re.compile(
                        r'(\w+)\s*:\s*([\[\]!\w]+)(\s*=)?')
                    for arg_match in arg_pattern.finditer(args_str):
                        arg_name = arg_match.group(1)
                        arg_type = _clean_type(arg_match.group(2))
                        required = arg_type.endswith('!')
                        arguments.append({
                            "name": arg_name,
                            "type": arg_type.rstrip('!'),
                            "required": required,
                            "raw_type": arg_type,
                            "has_default": bool(arg_match.group(3)),
                        })

                fields[field_name] = GraphQLField(
                    name=field_name, type=field_type,
                    nullable=nullable, arguments=arguments,
                    raw_type=raw_type, has_default=has_default,
                )

            types[name] = GraphQLType(
                name=name, kind=kind, fields=fields,
                enum_values=[], union_members=[],
            )

    # Parse enums. A value is its NAME: `DIGITAL @deprecated(reason: "x")` is
    # still DIGITAL. Read per line it became a different value, so deprecating
    # one was reported as removing it.
    for name, body in extract_blocks(clean, "enum"):
        body = re.sub(r'@\w+(?:\s*\([^)]*\))?', ' ', body)
        values = re.findall(r'[_A-Za-z]\w*', body)
        types[name] = GraphQLType(
            name=name, kind="enum", fields={},
            enum_values=values, union_members=[],
        )
    
    # Parse unions
    union_pattern = re.compile(r'union\s+(\w+)\s*=\s*([^\n]+)')
    for match in union_pattern.finditer(clean):
        name = match.group(1)
        members = [m.strip() for m in match.group(2).split('|')]
        types[name] = GraphQLType(
            name=name, kind="union", fields={},
            enum_values=[], union_members=members,
        )
    
    return types


def _clean_type(text: str) -> str:
    """The type reference alone: no default, directive, trailing comma or space."""
    t = text.split("=", 1)[0].split("@", 1)[0]
    return re.sub(r"\s+", "", t).rstrip(",")


def _parse_type(t: str):
    """("named", non_null, Name) or ("list", non_null, inner)."""
    non_null = t.endswith("!")
    if non_null:
        t = t[:-1]
    if t.startswith("[") and t.endswith("]"):
        return ("list", non_null, _parse_type(t[1:-1]))
    return ("named", non_null, t)


def _type_change(old: str, new: str, position: str) -> str:
    """How a type reference changed, by GraphQL's variance rules.

    position "output" (fields of type/interface): clients READ the value, so
    making it nullable breaks them (they assumed a value), while making it
    non-null is safe. position "input" (input fields, arguments): clients
    WRITE it, so the reverse holds. A different named type or list shape breaks
    either way.

    Returns "" (compatible), "type" (named type or list shape changed),
    "weakened" (output lost a non-null) or "required" (input gained one).
    """
    if not old or not new or old == new:
        return ""
    a, b = _parse_type(old), _parse_type(new)
    nullability = ""
    while True:
        if a[0] != b[0]:
            return "type"
        if position == "output" and a[1] and not b[1]:
            nullability = nullability or "weakened"
        if position == "input" and not a[1] and b[1]:
            nullability = nullability or "required"
        if a[0] == "named":
            return "type" if a[2] != b[2] else nullability
        a, b = a[2], b[2]


def _root_types(content: str) -> set:
    """Names of the operation root types: a `schema { query: X ... }` block's
    entries when one is declared, else the default Query/Mutation/Subscription
    (which GraphQL uses only when no schema block exists)."""
    clean = _blank_strings_and_comments(content)
    m = re.search(r'\bschema\s*(?:@\w+(?:\([^)]*\))?\s*)*\{', clean)
    if m:
        end = _find_matching_brace(clean, m.end())
        body = clean[m.end():end] if end != -1 else ""
        return set(re.findall(r'\b(?:query|mutation|subscription)\s*:\s*(\w+)',
                              body))
    return {"Query", "Mutation", "Subscription"}


def diff_graphql(old_content: str, new_content: str, file_path: str = "schema.graphql") -> list[BreakingChange]:
    """Compare two GraphQL schemas and return breaking changes."""
    old_types = parse_graphql(old_content)
    new_types = parse_graphql(new_content)
    
    changes = []
    roots = _root_types(old_content)

    for type_name, old_type in old_types.items():
        if type_name not in new_types:
            changes.append(BreakingChange(
                change_type="type_removed",
                path=file_path,
                method=type_name,
                field_name=type_name,
                field_type=old_type.kind,
                location="graphql",
                severity="breaking",
                description=f"{old_type.kind.title()} '{type_name}' was removed. All queries referencing it will fail.",
            ))
            continue
        
        new_type = new_types[type_name]
        
        # Check field removals
        if old_type.kind in ("type", "input", "interface"):
            for field_name, old_field in old_type.fields.items():
                if field_name not in new_type.fields and type_name in roots:
                    # A Query/Mutation/Subscription field IS an operation.
                    # As field_removed it was routed to the mechanical
                    # remove-field template, which deleted the consumer's
                    # whole `return client.query(...)` line -- silently
                    # removing behaviour. remove_operation marks it instead.
                    changes.append(BreakingChange(
                        change_type="operation_removed",
                        path=file_path,
                        method=type_name,
                        field_name=field_name,
                        field_type=old_field.type,
                        location="graphql",
                        severity="breaking",
                        description=f"Operation '{type_name}.{field_name}' was removed. Every query or mutation calling it will fail.",
                    ))
                elif field_name not in new_type.fields:
                    changes.append(BreakingChange(
                        change_type="field_removed",
                        path=file_path,
                        method=type_name,
                        field_name=field_name,
                        field_type=old_field.type,
                        location="graphql",
                        severity="breaking",
                        description=f"Field '{field_name}' removed from {old_type.kind} '{type_name}'. Queries selecting this field will error.",
                    ))
                else:
                    new_field = new_type.fields[field_name]
                    position = "input" if old_type.kind == "input" else "output"
                    how = _type_change(old_field.raw_type, new_field.raw_type, position)
                    if how == "type":
                        changes.append(BreakingChange(
                            change_type="field_type_changed",
                            path=file_path,
                            method=type_name,
                            field_name=field_name,
                            field_type=new_field.raw_type,
                            location="graphql",
                            severity="breaking",
                            description=f"Field '{field_name}' in {old_type.kind} '{type_name}' changed type from {old_field.raw_type} to {new_field.raw_type}.",
                            old_type=old_field.raw_type,
                            new_type=new_field.raw_type,
                        ))
                    elif how == "weakened":
                        # Output field may now be null: clients that read it
                        # without a null check break. Not add_required -- the
                        # client sends nothing; the fix is null handling.
                        changes.append(BreakingChange(
                            change_type="field_type_changed",
                            path=file_path,
                            method=type_name,
                            field_name=field_name,
                            field_type=new_field.raw_type,
                            location="graphql",
                            severity="breaking",
                            description=f"Field '{field_name}' in '{type_name}' changed from non-null to nullable ({old_field.raw_type} -> {new_field.raw_type}). Clients that assume a value will break on null.",
                            old_type=old_field.raw_type,
                            new_type=new_field.raw_type,
                        ))
                    elif how == "required" and not new_field.has_default:
                        # Input field now non-null: existing requests that omit
                        # or null it are rejected. A default keeps them valid.
                        changes.append(BreakingChange(
                            change_type="field_made_required",
                            path=file_path,
                            method=type_name,
                            field_name=field_name,
                            field_type=new_field.type,
                            location="graphql",
                            severity="breaking",
                            description=f"Field '{field_name}' in input '{type_name}' changed from nullable to required (non-null). Requests that omit it will be rejected.",
                        ))
                    # Output nullable -> non-null is SAFE (clients already
                    # handle a value) and is deliberately not reported. It used
                    # to be reported as field_made_required, i.e. "supply a
                    # value", for a field the client only reads.

                    # Existing arguments: type changed, or made required
                    new_args = {a["name"]: a for a in new_field.arguments}
                    for old_arg in old_field.arguments:
                        new_arg = new_args.get(old_arg["name"])
                        label = f"{field_name}({old_arg['name']})"
                        if new_arg is None:
                            # GraphQL validation rejects an unknown argument,
                            # so every query that still passes it fails --
                            # optional or not, defaulted or not.
                            changes.append(BreakingChange(
                                change_type="argument_removed",
                                path=file_path,
                                method=type_name,
                                field_name=label,
                                field_type=old_arg["raw_type"],
                                location="graphql",
                                severity="breaking",
                                description=f"Argument '{old_arg['name']}' removed from '{type_name}.{field_name}'. Queries that still pass it fail validation.",
                            ))
                            continue
                        arg_how = _type_change(old_arg["raw_type"],
                                               new_arg["raw_type"], "input")
                        if arg_how == "type":
                            changes.append(BreakingChange(
                                change_type="field_type_changed",
                                path=file_path,
                                method=type_name,
                                field_name=label,
                                field_type=new_arg["raw_type"],
                                location="graphql",
                                severity="breaking",
                                description=f"Argument '{old_arg['name']}' of '{type_name}.{field_name}' changed type from {old_arg['raw_type']} to {new_arg['raw_type']}.",
                                old_type=old_arg["raw_type"],
                                new_type=new_arg["raw_type"],
                            ))
                        elif arg_how == "required" and not new_arg["has_default"]:
                            changes.append(BreakingChange(
                                change_type="required_argument_added",
                                path=file_path,
                                method=type_name,
                                field_name=label,
                                field_type=new_arg["type"],
                                location="graphql",
                                severity="breaking",
                                description=f"Argument '{old_arg['name']}' of '{type_name}.{field_name}' is now required ({old_arg['raw_type']} -> {new_arg['raw_type']}). Queries that omit it will fail.",
                            ))

                    # Check new required arguments
                    old_arg_names = {a["name"] for a in old_field.arguments}
                    for arg in new_field.arguments:
                        if (arg["name"] not in old_arg_names and arg["required"]
                                and not arg["has_default"]):
                            changes.append(BreakingChange(
                                change_type="required_argument_added",
                                path=file_path,
                                method=type_name,
                                field_name=f"{field_name}({arg['name']})",
                                field_type=arg["type"],
                                location="graphql",
                                severity="breaking",
                                description=f"New required argument '{arg['name']}' added to '{type_name}.{field_name}'. Existing queries without it will fail.",
                            ))
        
        # New non-null input field with no default: every existing request
        # that builds this input omits it and is rejected.
        if old_type.kind == "input" and new_type.kind == "input":
            for field_name, new_field in new_type.fields.items():
                if (field_name not in old_type.fields and not new_field.nullable
                        and not new_field.has_default):
                    changes.append(BreakingChange(
                        change_type="required_field_added",
                        path=file_path,
                        method=type_name,
                        field_name=field_name,
                        field_type=new_field.type,
                        location="graphql",
                        severity="breaking",
                        description=f"New required field '{field_name}' added to input '{type_name}'. Requests that build this input without it will be rejected.",
                    ))

        # Check enum value removals
        if old_type.kind == "enum":
            removed_values = set(old_type.enum_values) - set(new_type.enum_values)
            for value in removed_values:
                changes.append(BreakingChange(
                    change_type="enum_value_removed",
                    path=file_path,
                    method=type_name,
                    field_name=value,
                    field_type="enum",
                    location="graphql",
                    severity="breaking",
                    description=f"Enum value '{value}' removed from '{type_name}'. Clients sending this value will get validation errors.",
                ))
        
        # Check union member removals
        if old_type.kind == "union":
            removed_members = set(old_type.union_members) - set(new_type.union_members)
            for member in removed_members:
                changes.append(BreakingChange(
                    change_type="union_member_removed",
                    path=file_path,
                    method=type_name,
                    field_name=member,
                    field_type="union",
                    location="graphql",
                    severity="breaking",
                    description=f"Union member '{member}' removed from '{type_name}'. Fragment spreads on this type will fail.",
                ))
    
    return changes


def format_graphql_changes(changes: list[BreakingChange]) -> str:
    """Format GraphQL breaking changes for display."""
    if not changes:
        return "✅ No breaking changes in GraphQL schema."
    
    lines = [f"⚠️  {len(changes)} breaking change(s) in GraphQL schema:", ""]
    for i, c in enumerate(changes, 1):
        lines.append(f"  [{i}] {c.change_type}")
        lines.append(f"      Type: {c.method}")
        lines.append(f"      Field: {c.field_name}")
        lines.append(f"      {c.description}")
        lines.append("")
    return "\n".join(lines)
