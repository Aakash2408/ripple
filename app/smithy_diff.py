from __future__ import annotations
"""
ripple/app/smithy_diff.py

Smithy Diff Engine — detects breaking changes in Smithy model files (.smithy).

Smithy is AWS's IDL for defining services. All AWS services use it.
Breaking changes:
- Operation removed from service (clients get UnknownOperationException)
- Member removed from structure (readers fail)
- Member made @required (old requests missing it fail)
- Shape type changed (serialization incompatible)
- Enum value removed (clients sending it get validation error)
- Resource removed (entire CRUD surface gone)
- Error removed from operation (clients not handling it lose info)

File format: .smithy (custom IDL) or .json (AST JSON)
"""

import re
from typing import Optional
from .diff_engine import BreakingChange
from .schema_parse import strip_comments, extract_blocks


def parse_smithy_structures(content: str) -> dict:
    """Parse Smithy structure definitions."""
    structures = {}
    
    # Brace-aware + comment-stripped via schema_parse: r'\{([^}]*)\}' could
    # not cross a nested brace, and comments parsed as live members.
    clean = strip_comments(content)
    member_pattern = re.compile(r'(?:@required\s+)?(\w+)\s*:\s*(\w+)')
    required_pattern = re.compile(r'@required\s+(\w+)\s*:', re.MULTILINE)
    
    for name, body in extract_blocks(clean, 'structure'):
        members = {}
        required_members = set(m.group(1) for m in required_pattern.finditer(body))
        
        for member_match in member_pattern.finditer(body):
            member_name = member_match.group(1)
            member_type = member_match.group(2)
            members[member_name] = {
                "type": member_type,
                "required": member_name in required_members,
            }
        
        structures[name] = members
    
    return structures


def parse_smithy_operations(content: str) -> dict:
    """Parse Smithy service operations."""
    operations = {}
    
    clean = strip_comments(content)
    
    for name, body in extract_blocks(clean, 'operation'):
        input_match = re.search(r'input\s*:\s*(\w+)', body)
        output_match = re.search(r'output\s*:\s*(\w+)', body)
        operations[name] = {
            "input": input_match.group(1) if input_match else "",
            "output": output_match.group(1) if output_match else "",
        }
    
    return operations


def parse_smithy_enums(content: str) -> dict:
    """Parse Smithy enum definitions."""
    enums = {}
    
    clean = strip_comments(content)
    
    for name, body in extract_blocks(clean, 'enum'):
        values = re.findall(r'(\w+)', body)
        enums[name] = [v for v in values if v and not v[0].islower()]  # Filter out annotations
    
    return enums


def diff_smithy(old_content: str, new_content: str, file_path: str = "model.smithy") -> list[BreakingChange]:
    """Compare two Smithy model files and return breaking changes."""
    changes = []
    
    old_structs = parse_smithy_structures(old_content)
    new_structs = parse_smithy_structures(new_content)
    old_ops = parse_smithy_operations(old_content)
    new_ops = parse_smithy_operations(new_content)
    old_enums = parse_smithy_enums(old_content)
    new_enums = parse_smithy_enums(new_content)
    
    # Structure member changes
    for struct_name in old_structs:
        if struct_name not in new_structs:
            changes.append(BreakingChange(
                change_type="structure_removed", path=file_path, method=struct_name,
                field_name=struct_name, field_type="structure", location="smithy",
                severity="breaking", description=f"Structure '{struct_name}' removed.",
            ))
            continue
        
        old_members = old_structs[struct_name]
        new_members = new_structs[struct_name]
        
        for member_name in old_members:
            if member_name not in new_members:
                changes.append(BreakingChange(
                    change_type="member_removed", path=file_path, method=struct_name,
                    field_name=member_name, field_type=old_members[member_name]["type"],
                    location="smithy", severity="breaking",
                    description=f"Member '{member_name}' removed from '{struct_name}'.",
                ))
            elif old_members[member_name]["type"] != new_members[member_name]["type"]:
                changes.append(BreakingChange(
                    change_type="member_type_changed", path=file_path, method=struct_name,
                    field_name=member_name,
                    field_type=f"{old_members[member_name]['type']} -> {new_members[member_name]['type']}",
                    location="smithy", severity="breaking",
                    description=f"Member '{member_name}' type changed in '{struct_name}'.",
                ))
        
        # @required added to existing member
        for member_name in new_members:
            if member_name in old_members:
                if new_members[member_name]["required"] and not old_members[member_name]["required"]:
                    changes.append(BreakingChange(
                        change_type="member_made_required", path=file_path, method=struct_name,
                        field_name=member_name, field_type=new_members[member_name]["type"],
                        location="smithy", severity="breaking",
                        description=f"Member '{member_name}' in '{struct_name}' is now @required. Old requests without it will fail.",
                    ))
    
    # Operation changes
    for op_name in old_ops:
        if op_name not in new_ops:
            changes.append(BreakingChange(
                change_type="operation_removed", path=file_path, method=op_name,
                field_name=op_name, field_type="operation", location="smithy",
                severity="breaking", description=f"Operation '{op_name}' removed. Clients will get UnknownOperationException.",
            ))
    
    # Enum changes
    for enum_name in old_enums:
        if enum_name in new_enums:
            removed = set(old_enums[enum_name]) - set(new_enums[enum_name])
            for value in removed:
                changes.append(BreakingChange(
                    change_type="enum_value_removed", path=file_path, method=enum_name,
                    field_name=value, field_type="enum", location="smithy",
                    severity="breaking", description=f"Enum value '{value}' removed from '{enum_name}'.",
                ))
    
    return changes
