from __future__ import annotations
"""
ripple/app/graphql_diff.py

GraphQL Schema Diff Engine — detects breaking changes in GraphQL schemas.

Breaking changes in GraphQL:
- Field removed from type (all queries using it break)
- Field made non-nullable (null → String! breaks optional handling)
- Type removed (all queries referencing it break)
- Argument added as required (existing queries missing it fail)
- Enum value removed (clients sending it get errors)
- Union member removed

GraphQL is a HUGE market — every modern frontend team uses it.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .schema_parse import strip_comments, extract_blocks

from .diff_engine import BreakingChange


@dataclass
class GraphQLField:
    """A field in a GraphQL type."""
    name: str
    type: str
    nullable: bool
    arguments: list[dict]  # [{name, type, required}]


@dataclass
class GraphQLType:
    """A GraphQL type definition."""
    name: str
    kind: str  # "type", "input", "enum", "interface", "union"
    fields: dict[str, GraphQLField]
    enum_values: list[str]  # for enums
    union_members: list[str]  # for unions


def parse_graphql(content: str) -> dict[str, GraphQLType]:
    """Parse a .graphql schema file into type definitions.

    Uses schema_parse for brace-aware block extraction and comment
    stripping. The previous r'\\{([^}]*)\\}' could not cross a nested brace,
    so a field default like `arg: Input = {x: 1}` truncated the body and hid
    every field after it. Comments were also parsed as live fields, so
    deleting a `# commented: String` line looked like a breaking change.
    """
    # GraphQL uses '#' line comments; '"""' descriptions are left alone
    # because they cannot contain a bare '}' that would unbalance a block.
    clean = strip_comments(content, hash_comments=True)

    types = {}

    for kind in ("type", "input", "interface"):
        for name, body in extract_blocks(clean, kind):
            fields = {}
            # Parse fields: name(args): Type or name: Type
            field_pattern = re.compile(r'(\w+)(?:\(([^)]*)\))?\s*:\s*([^\n]+)')

            for field_match in field_pattern.finditer(body):
                field_name = field_match.group(1)
                args_str = field_match.group(2) or ""
                raw_type = field_match.group(3).strip()
                # Drop an inline default so it is not treated as the type
                raw_type = raw_type.split('=')[0].strip()
                field_type = raw_type.rstrip('!').strip()
                nullable = not raw_type.endswith('!')

                arguments = []
                if args_str:
                    arg_pattern = re.compile(r'(\w+)\s*:\s*(\S+)')
                    for arg_match in arg_pattern.finditer(args_str):
                        arg_name = arg_match.group(1)
                        arg_type = arg_match.group(2)
                        required = arg_type.endswith('!')
                        arguments.append({
                            "name": arg_name,
                            "type": arg_type.rstrip('!'),
                            "required": required,
                        })

                fields[field_name] = GraphQLField(
                    name=field_name, type=field_type,
                    nullable=nullable, arguments=arguments,
                )

            types[name] = GraphQLType(
                name=name, kind=kind, fields=fields,
                enum_values=[], union_members=[],
            )

    # Parse enums
    for name, body in extract_blocks(clean, "enum"):
        values = [v.strip() for v in body.strip().split('\n') if v.strip()]
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


def diff_graphql(old_content: str, new_content: str, file_path: str = "schema.graphql") -> list[BreakingChange]:
    """Compare two GraphQL schemas and return breaking changes."""
    old_types = parse_graphql(old_content)
    new_types = parse_graphql(new_content)
    
    changes = []
    
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
                if field_name not in new_type.fields:
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
                    
                    # Nullable → Non-nullable (breaking for responses)
                    if old_field.nullable and not new_field.nullable:
                        changes.append(BreakingChange(
                            change_type="field_made_required",
                            path=file_path,
                            method=type_name,
                            field_name=field_name,
                            field_type=new_field.type,
                            location="graphql",
                            severity="breaking",
                            description=f"Field '{field_name}' in '{type_name}' changed from nullable to required (non-null). Clients expecting null will break.",
                        ))
                    
                    # Check new required arguments
                    old_arg_names = {a["name"] for a in old_field.arguments}
                    for arg in new_field.arguments:
                        if arg["name"] not in old_arg_names and arg["required"]:
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
