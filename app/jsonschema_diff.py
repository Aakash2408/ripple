from __future__ import annotations
"""
ripple/app/jsonschema_diff.py

JSON Schema Diff Engine — detects breaking changes in JSON Schema files.

Breaking changes in JSON Schema:
- Required property added (existing data missing it fails validation)
- Property removed from schema (consumers reading it get undefined)
- Property type changed (validation/parsing fails)
- Enum value removed (existing data with that value fails)
- Pattern made stricter (previously valid strings now fail)
- minItems/maxItems made stricter (arrays that were valid now fail)
- additionalProperties set to false (previously accepted props now rejected)
"""

import json
from typing import Optional
from .diff_engine import BreakingChange


def parse_json_schema(content: str) -> dict:
    """Parse JSON Schema from JSON or detect schema in YAML."""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        try:
            import yaml
            return yaml.safe_load(content) or {}
        except:
            return {}


def diff_jsonschema(old_content: str, new_content: str, file_path: str = "schema.json") -> list[BreakingChange]:
    """Compare two JSON Schemas and return breaking changes."""
    old_schema = parse_json_schema(old_content)
    new_schema = parse_json_schema(new_content)
    
    if not old_schema or not new_schema:
        return []
    
    changes = []
    _diff_schema_recursive(old_schema, new_schema, file_path, "", changes)
    return changes


def _diff_schema_recursive(old: dict, new: dict, file_path: str, path_prefix: str, changes: list):
    """Recursively diff two schema objects."""
    
    # Required properties added
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    added_required = new_required - old_required
    
    for prop in added_required:
        changes.append(BreakingChange(
            change_type="required_property_added",
            path=file_path,
            method=path_prefix or "root",
            field_name=prop,
            field_type="required",
            location="jsonschema",
            severity="breaking",
            description=f"Property '{prop}' is now required at '{path_prefix or 'root'}'. Existing data without it will fail validation.",
        ))
    
    # Property changes
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    
    # Property removed
    for prop_name in old_props:
        if prop_name not in new_props:
            changes.append(BreakingChange(
                change_type="property_removed",
                path=file_path,
                method=path_prefix or "root",
                field_name=prop_name,
                field_type=old_props[prop_name].get("type", "unknown"),
                location="jsonschema",
                severity="breaking",
                description=f"Property '{prop_name}' removed from schema at '{path_prefix or 'root'}'.",
            ))
    
    # Property type changed
    for prop_name in set(old_props.keys()) & set(new_props.keys()):
        old_type = old_props[prop_name].get("type", "")
        new_type = new_props[prop_name].get("type", "")
        if old_type and new_type and old_type != new_type:
            changes.append(BreakingChange(
                change_type="property_type_changed",
                path=file_path,
                method=path_prefix or "root",
                field_name=prop_name,
                field_type=f"{old_type} -> {new_type}",
                old_type=old_type,
                new_type=new_type,
                location="jsonschema",
                severity="breaking",
                description=f"Property '{prop_name}' type changed from '{old_type}' to '{new_type}'.",
            ))
        
        # Recurse into nested objects
        if old_props[prop_name].get("type") == "object" and new_props[prop_name].get("type") == "object":
            nested_path = f"{path_prefix}.{prop_name}" if path_prefix else prop_name
            _diff_schema_recursive(old_props[prop_name], new_props[prop_name], file_path, nested_path, changes)
    
    # Enum value removed
    old_enum = set(old.get("enum", []))
    new_enum = set(new.get("enum", []))
    if old_enum and new_enum:
        removed_values = old_enum - new_enum
        for value in removed_values:
            changes.append(BreakingChange(
                change_type="enum_value_removed",
                path=file_path,
                method=path_prefix or "root",
                field_name=str(value),
                field_type="enum",
                location="jsonschema",
                severity="breaking",
                description=f"Enum value '{value}' removed. Data with this value will fail validation.",
            ))
    
    # additionalProperties changed from true/unset to false
    old_additional = old.get("additionalProperties", True)
    new_additional = new.get("additionalProperties", True)
    if old_additional is not False and new_additional is False:
        changes.append(BreakingChange(
            change_type="additional_properties_restricted",
            path=file_path,
            method=path_prefix or "root",
            field_name="additionalProperties",
            field_type="boolean",
            location="jsonschema",
            severity="breaking",
            description=f"additionalProperties set to false at '{path_prefix or 'root'}'. Extra properties now rejected.",
        ))
