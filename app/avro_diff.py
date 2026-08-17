from __future__ import annotations
"""
ripple/app/avro_diff.py

Avro Schema Diff Engine — detects breaking changes in Avro schemas (.avsc files).

Breaking changes in Avro:
- Field removed (readers expecting it get default or fail)
- Field type changed (deserialization failure)
- Required field added without default (old writers don't send it)
- Enum symbol removed (readers with old symbol fail)
- Record name changed (schema registry rejects)
- Union type removed (narrowing breaks existing data)

Used by: Kafka (Confluent Schema Registry), Hadoop, Spark pipelines.
"""

import json
from dataclasses import dataclass
from typing import Optional
from .diff_engine import BreakingChange


def parse_avro(content: str) -> dict:
    """Parse Avro schema from JSON."""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {}


def _get_fields(schema: dict) -> dict:
    """Extract fields from a record schema as {name: field_def}."""
    if schema.get("type") == "record":
        return {f["name"]: f for f in schema.get("fields", [])}
    return {}


def _get_symbols(schema: dict) -> list:
    """Extract enum symbols."""
    if schema.get("type") == "enum":
        return schema.get("symbols", [])
    return []


def diff_avro(old_content: str, new_content: str, file_path: str = "schema.avsc") -> list[BreakingChange]:
    """Compare two Avro schemas and return breaking changes."""
    old_schema = parse_avro(old_content)
    new_schema = parse_avro(new_content)
    
    if not old_schema or not new_schema:
        return []
    
    changes = []
    
    # Record schemas
    if old_schema.get("type") == "record" and new_schema.get("type") == "record":
        old_fields = _get_fields(old_schema)
        new_fields = _get_fields(new_schema)
        
        # Field removed
        for name in old_fields:
            if name not in new_fields:
                changes.append(BreakingChange(
                    change_type="field_removed",
                    path=file_path,
                    method=old_schema.get("name", ""),
                    field_name=name,
                    field_type=str(old_fields[name].get("type", "unknown")),
                    location="avro",
                    severity="breaking",
                    description=f"Field '{name}' removed from record '{old_schema.get('name')}'. Readers expecting this field will fail.",
                ))
        
        # Required field added (no default)
        for name in new_fields:
            if name not in old_fields:
                field = new_fields[name]
                if "default" not in field:
                    changes.append(BreakingChange(
                        change_type="required_field_added",
                        path=file_path,
                        method=new_schema.get("name", ""),
                        field_name=name,
                        field_type=str(field.get("type", "unknown")),
                        location="avro",
                        severity="breaking",
                        description=f"Field '{name}' added without default. Old data missing this field will fail deserialization.",
                    ))
        
        # Field type changed
        for name in set(old_fields.keys()) & set(new_fields.keys()):
            old_type = str(old_fields[name].get("type", ""))
            new_type = str(new_fields[name].get("type", ""))
            if old_type != new_type:
                changes.append(BreakingChange(
                    change_type="field_type_changed",
                    path=file_path,
                    method=old_schema.get("name", ""),
                    field_name=name,
                    field_type=f"{old_type} -> {new_type}",
                    old_type=old_type,
                    new_type=new_type,
                    location="avro",
                    severity="breaking",
                    description=f"Field '{name}' type changed from '{old_type}' to '{new_type}'. Schema evolution incompatible.",
                ))
        
        # Record name changed
        if old_schema.get("name") != new_schema.get("name"):
            changes.append(BreakingChange(
                change_type="record_renamed",
                new_name=str(new_schema.get("name", "")),
                path=file_path,
                method=new_schema.get("name", ""),
                field_name=f"{old_schema.get('name')} -> {new_schema.get('name')}",
                field_type="record",
                location="avro",
                severity="breaking",
                description=f"Record renamed from '{old_schema.get('name')}' to '{new_schema.get('name')}'. Schema registry will reject.",
            ))
    
    # Enum schemas
    if old_schema.get("type") == "enum" and new_schema.get("type") == "enum":
        old_symbols = set(_get_symbols(old_schema))
        new_symbols = set(_get_symbols(new_schema))
        
        for symbol in old_symbols - new_symbols:
            changes.append(BreakingChange(
                change_type="enum_symbol_removed",
                path=file_path,
                method=old_schema.get("name", ""),
                field_name=symbol,
                field_type="enum",
                location="avro",
                severity="breaking",
                description=f"Enum symbol '{symbol}' removed. Data with this value will fail deserialization.",
            ))
    
    return changes
