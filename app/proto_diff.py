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
- Enum value removed

This is a HUGE market — every gRPC shop has this pain.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .diff_engine import BreakingChange


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
    fields: dict[str, ProtoField]  # field_name → ProtoField


def parse_proto(content: str) -> dict[str, ProtoMessage]:
    """
    Parse a .proto file into message definitions.
    Simple regex-based parser (not a full protobuf compiler).
    """
    messages = {}
    
    # Find all message blocks
    message_pattern = re.compile(
        r'message\s+(\w+)\s*\{([^}]*)\}',
        re.DOTALL
    )
    
    for match in message_pattern.finditer(content):
        msg_name = match.group(1)
        msg_body = match.group(2)
        
        fields = {}
        # Parse fields: [optional|required|repeated] type name = number;
        field_pattern = re.compile(
            r'(optional|required|repeated)?\s*(\w+)\s+(\w+)\s*=\s*(\d+)\s*;'
        )
        
        for field_match in field_pattern.finditer(msg_body):
            label = field_match.group(1) or "optional"
            field_type = field_match.group(2)
            field_name = field_match.group(3)
            field_number = int(field_match.group(4))
            
            fields[field_name] = ProtoField(
                name=field_name,
                type=field_type,
                number=field_number,
                label=label,
            )
        
        messages[msg_name] = ProtoMessage(name=msg_name, fields=fields)
    
    return messages


def diff_proto(old_content: str, new_content: str, file_path: str = "schema.proto") -> list[BreakingChange]:
    """
    Compare two .proto files and return breaking changes.
    """
    old_messages = parse_proto(old_content)
    new_messages = parse_proto(new_content)
    
    changes = []
    
    for msg_name, old_msg in old_messages.items():
        if msg_name not in new_messages:
            # Message removed entirely
            changes.append(BreakingChange(
                change_type="message_removed",
                path=file_path,
                method=msg_name,
                field_name=msg_name,
                field_type="message",
                location="proto",
                severity="breaking",
                description=f"Message '{msg_name}' was removed. All imports and references will break.",
            ))
            continue
        
        new_msg = new_messages[msg_name]
        
        # Check for removed fields
        for field_name, old_field in old_msg.fields.items():
            if field_name not in new_msg.fields:
                changes.append(BreakingChange(
                    change_type="field_removed",
                    path=file_path,
                    method=msg_name,
                    field_name=field_name,
                    field_type=old_field.type,
                    location="proto",
                    severity="breaking",
                    description=f"Field '{field_name}' removed from message '{msg_name}'. Consumers still referencing this field will get compilation errors.",
                ))
            else:
                new_field = new_msg.fields[field_name]
                
                # Check type change
                if old_field.type != new_field.type:
                    changes.append(BreakingChange(
                        change_type="field_type_changed",
                        path=file_path,
                        method=msg_name,
                        field_name=field_name,
                        field_type=f"{old_field.type} → {new_field.type}",
                        location="proto",
                        severity="breaking",
                        description=f"Field '{field_name}' in '{msg_name}' changed type from '{old_field.type}' to '{new_field.type}'. Binary wire format is incompatible.",
                    ))
                
                # Check number change (VERY breaking)
                if old_field.number != new_field.number:
                    changes.append(BreakingChange(
                        change_type="field_number_changed",
                        path=file_path,
                        method=msg_name,
                        field_name=field_name,
                        field_type=old_field.type,
                        location="proto",
                        severity="breaking",
                        description=f"Field '{field_name}' in '{msg_name}' changed number from {old_field.number} to {new_field.number}. This breaks all existing serialized data.",
                    ))
        
        # Check for new required fields (proto2)
        for field_name, new_field in new_msg.fields.items():
            if field_name not in old_msg.fields and new_field.label == "required":
                changes.append(BreakingChange(
                    change_type="required_field_added",
                    path=file_path,
                    method=msg_name,
                    field_name=field_name,
                    field_type=new_field.type,
                    location="proto",
                    severity="breaking",
                    description=f"New required field '{field_name}' added to '{msg_name}'. Existing producers that don't set this field will cause deserialization failures.",
                ))
    
    # Check for new messages that might indicate renames
    for msg_name in new_messages:
        if msg_name not in old_messages:
            # New message — check if it looks like a rename of a removed one
            # (heuristic: similar field structure)
            for old_name, old_msg in old_messages.items():
                if old_name not in new_messages:
                    # Compare field overlap
                    old_fields = set(old_msg.fields.keys())
                    new_fields = set(new_messages[msg_name].fields.keys())
                    overlap = len(old_fields & new_fields) / max(len(old_fields), 1)
                    if overlap > 0.7:
                        changes.append(BreakingChange(
                            change_type="message_renamed",
                            path=file_path,
                            method=msg_name,
                            field_name=f"{old_name} → {msg_name}",
                            field_type="message",
                            location="proto",
                            severity="breaking",
                            description=f"Message appears renamed from '{old_name}' to '{msg_name}' (70%+ field overlap). All imports need updating.",
                        ))
                        break
    
    return changes


def format_proto_changes(changes: list[BreakingChange]) -> str:
    """Format proto breaking changes for display."""
    if not changes:
        return "✅ No breaking changes in proto file."
    
    lines = [f"⚠️  {len(changes)} breaking change(s) in proto:", ""]
    for i, c in enumerate(changes, 1):
        lines.append(f"  [{i}] {c.change_type}")
        lines.append(f"      Message: {c.method}")
        lines.append(f"      Field: {c.field_name} ({c.field_type})")
        lines.append(f"      {c.description}")
        lines.append("")
    return "\n".join(lines)
