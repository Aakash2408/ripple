from __future__ import annotations
"""
ripple/app/thrift_diff.py

Apache Thrift Diff Engine — detects breaking changes in .thrift IDL files.

Breaking changes in Thrift:
- Field removed from struct (readers fail)
- Field ID changed (wire incompatibility)
- Field type changed (deserialization failure)
- Required field added (old writers don't send it)
- Service method removed (clients get TApplicationException)
- Enum value removed (unknown enum on deserialize)
- Struct renamed (all imports break)
"""

import re
from dataclasses import dataclass
from typing import Optional
from .diff_engine import BreakingChange
from .schema_parse import strip_comments, extract_blocks


def parse_thrift_structs(content: str) -> dict:
    """Parse Thrift struct definitions."""
    structs = {}
    
    # schema_parse: brace-aware so a container default like `= {}` cannot
    # truncate the body, and '#'/'//' comments are stripped so a commented
    # field is not parsed as live.
    clean = strip_comments(content, hash_comments=True)
    field_pattern = re.compile(r'(\d+)\s*:\s*(required|optional)?\s*(\w+)\s+(\w+)')
    
    for name, body in extract_blocks(clean, 'struct'):
        fields = {}
        
        for field_match in field_pattern.finditer(body):
            field_id = int(field_match.group(1))
            required = field_match.group(2) or "default"
            field_type = field_match.group(3)
            field_name = field_match.group(4)
            fields[field_name] = {"id": field_id, "type": field_type, "required": required}
        
        structs[name] = fields
    
    return structs


def parse_thrift_services(content: str) -> dict:
    """Parse Thrift service definitions."""
    services = {}
    
    clean = strip_comments(content, hash_comments=True)
    method_pattern = re.compile(r'(\w+)\s+(\w+)\s*\(([^)]*)\)')
    
    for name, body in extract_blocks(clean, 'service'):
        methods = {}
        
        for method_match in method_pattern.finditer(body):
            ret_type = method_match.group(1)
            method_name = method_match.group(2)
            params = method_match.group(3).strip()
            methods[method_name] = {"return_type": ret_type, "params": params}
        
        services[name] = methods
    
    return services


def parse_thrift_enums(content: str) -> dict:
    """Parse Thrift enum definitions."""
    enums = {}
    clean = strip_comments(content, hash_comments=True)
    value_pattern = re.compile(r'(\w+)')
    
    for name, body in extract_blocks(clean, 'enum'):
        values = [v.group(1) for v in value_pattern.finditer(body) if not v.group(1).isdigit()]
        enums[name] = values
    
    return enums


def diff_thrift(old_content: str, new_content: str, file_path: str = "service.thrift") -> list[BreakingChange]:
    """Compare two Thrift IDL files and return breaking changes."""
    changes = []
    
    old_structs = parse_thrift_structs(old_content)
    new_structs = parse_thrift_structs(new_content)
    old_services = parse_thrift_services(old_content)
    new_services = parse_thrift_services(new_content)
    old_enums = parse_thrift_enums(old_content)
    new_enums = parse_thrift_enums(new_content)
    
    # Struct field changes
    for struct_name in old_structs:
        if struct_name not in new_structs:
            changes.append(BreakingChange(
                change_type="struct_removed", path=file_path, method=struct_name,
                field_name=struct_name, field_type="struct", location="thrift",
                severity="breaking", description=f"Struct '{struct_name}' removed. All references will fail.",
            ))
            continue
        
        old_fields = old_structs[struct_name]
        new_fields = new_structs[struct_name]
        
        for field_name in old_fields:
            if field_name not in new_fields:
                changes.append(BreakingChange(
                    change_type="field_removed", path=file_path, method=struct_name,
                    field_name=field_name, field_type=old_fields[field_name]["type"],
                    location="thrift", severity="breaking",
                    description=f"Field '{field_name}' removed from struct '{struct_name}'.",
                ))
            elif old_fields[field_name]["type"] != new_fields[field_name]["type"]:
                changes.append(BreakingChange(
                    change_type="field_type_changed", path=file_path, method=struct_name,
                    field_name=field_name, 
                    field_type=f"{old_fields[field_name]['type']} -> {new_fields[field_name]['type']}",
                    location="thrift", severity="breaking",
                    description=f"Field '{field_name}' type changed in struct '{struct_name}'.",
                ))
            elif old_fields[field_name]["id"] != new_fields[field_name]["id"]:
                changes.append(BreakingChange(
                    change_type="field_id_changed", path=file_path, method=struct_name,
                    field_name=field_name, field_type="id",
                    location="thrift", severity="breaking",
                    description=f"Field '{field_name}' ID changed from {old_fields[field_name]['id']} to {new_fields[field_name]['id']}. Wire incompatible.",
                ))
        
        # Required field added
        for field_name in new_fields:
            if field_name not in old_fields and new_fields[field_name]["required"] == "required":
                changes.append(BreakingChange(
                    change_type="required_field_added", path=file_path, method=struct_name,
                    field_name=field_name, field_type=new_fields[field_name]["type"],
                    location="thrift", severity="breaking",
                    description=f"Required field '{field_name}' added to struct '{struct_name}'. Old writers will fail.",
                ))
    
    # Service method changes
    for svc_name in old_services:
        if svc_name not in new_services:
            continue
        for method_name in old_services[svc_name]:
            if method_name not in new_services[svc_name]:
                changes.append(BreakingChange(
                    change_type="method_removed", path=file_path, method=f"{svc_name}.{method_name}",
                    field_name=method_name, field_type="service method",
                    location="thrift", severity="breaking",
                    description=f"Method '{method_name}' removed from service '{svc_name}'. Clients will get TApplicationException.",
                ))
    
    # Enum changes
    for enum_name in old_enums:
        if enum_name not in new_enums:
            continue
        removed_values = set(old_enums[enum_name]) - set(new_enums.get(enum_name, []))
        for value in removed_values:
            changes.append(BreakingChange(
                change_type="enum_value_removed", path=file_path, method=enum_name,
                field_name=value, field_type="enum",
                location="thrift", severity="breaking",
                description=f"Enum value '{value}' removed from '{enum_name}'.",
            ))
    
    return changes
