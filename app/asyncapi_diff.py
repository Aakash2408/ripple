from __future__ import annotations
"""
ripple/app/asyncapi_diff.py

AsyncAPI Diff Engine — detects breaking changes in AsyncAPI spec files.

AsyncAPI defines event-driven APIs: messages published/subscribed via
Kafka, RabbitMQ, SNS/SQS, WebSockets, MQTT, NATS, etc.

Breaking changes in AsyncAPI:
- Channel removed (consumers subscribed to it get nothing)
- Message payload field removed (consumers parsing it get null)
- Message payload field type changed (deserialization failure)
- Required field added to published message (consumers missing it)
- Channel renamed (subscribers get no messages)
- Message renamed (consumers expecting old name fail)
- Server removed (connection config breaks)

This covers: Kafka topics, SNS topics, SQS queues, RabbitMQ exchanges,
MQTT topics, NATS subjects, WebSocket channels.

Spec format: YAML or JSON, version 2.x or 3.x
"""

import re
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from .diff_engine import BreakingChange


@dataclass
class AsyncChannel:
    """A channel (topic/queue) in an AsyncAPI spec."""
    name: str
    description: str
    publish_message: Optional[dict]  # message schema for publish
    subscribe_message: Optional[dict]  # message schema for subscribe
    servers: list[str]


@dataclass
class AsyncMessage:
    """A message definition."""
    name: str
    payload_fields: dict[str, dict]  # field_name -> {type, required, ...}
    required_fields: set[str]


def parse_asyncapi(content: str) -> dict:
    """
    Parse an AsyncAPI spec (YAML or JSON) into a normalized structure.
    
    Returns:
        {
            "version": "2.6.0",
            "channels": {"user/signup": AsyncChannel, ...},
            "messages": {"UserSignedUp": AsyncMessage, ...},
            "servers": {"production": {...}, ...},
        }
    """
    if yaml:
        spec = yaml.safe_load(content)
    else:
        import json
        spec = json.loads(content)
    
    if not isinstance(spec, dict):
        return {"version": "", "channels": {}, "messages": {}, "servers": {}}
    
    version = spec.get("asyncapi", spec.get("info", {}).get("version", ""))
    
    # Parse channels
    channels = {}
    raw_channels = spec.get("channels", {})
    for channel_name, channel_data in raw_channels.items():
        if not isinstance(channel_data, dict):
            continue
        
        pub_msg = None
        sub_msg = None
        
        # AsyncAPI 2.x format
        if "publish" in channel_data:
            pub_msg = _extract_message_schema(channel_data["publish"], spec)
        if "subscribe" in channel_data:
            sub_msg = _extract_message_schema(channel_data["subscribe"], spec)
        
        # AsyncAPI 3.x format (uses 'messages' directly)
        if "messages" in channel_data:
            msgs = channel_data["messages"]
            if isinstance(msgs, dict):
                for msg_name, msg_data in msgs.items():
                    sub_msg = _extract_payload(msg_data, spec)
        
        channels[channel_name] = AsyncChannel(
            name=channel_name,
            description=channel_data.get("description", ""),
            publish_message=pub_msg,
            subscribe_message=sub_msg,
            servers=channel_data.get("servers", []),
        )
    
    # Parse standalone messages
    messages = {}
    raw_messages = spec.get("components", {}).get("messages", {})
    for msg_name, msg_data in raw_messages.items():
        if not isinstance(msg_data, dict):
            continue
        payload = _extract_payload(msg_data, spec)
        if payload:
            fields = payload.get("properties", {})
            required = set(payload.get("required", []))
            messages[msg_name] = AsyncMessage(
                name=msg_name,
                payload_fields=fields,
                required_fields=required,
            )
    
    # Parse servers
    servers = spec.get("servers", {})
    
    return {
        "version": version,
        "channels": channels,
        "messages": messages,
        "servers": servers,
    }


def diff_asyncapi(old_content: str, new_content: str, file_path: str = "asyncapi.yaml") -> list[BreakingChange]:
    """
    Compare two AsyncAPI specs and return breaking changes.
    
    Detects:
    - Channel removed
    - Channel message payload field removed
    - Channel message payload field type changed
    - Required field added to message payload
    - Message removed from components
    - Server removed
    """
    old_spec = parse_asyncapi(old_content)
    new_spec = parse_asyncapi(new_content)
    
    changes = []
    
    old_channels = old_spec["channels"]
    new_channels = new_spec["channels"]
    old_messages = old_spec["messages"]
    new_messages = new_spec["messages"]
    old_servers = old_spec["servers"]
    new_servers = new_spec["servers"]
    
    # Detect removed channels
    for channel_name in old_channels:
        if channel_name not in new_channels:
            changes.append(BreakingChange(
                change_type="channel_removed",
                path=file_path,
                method=channel_name,
                field_name=channel_name,
                field_type="channel",
                location="asyncapi",
                severity="breaking",
                description=f"Channel '{channel_name}' was removed. All subscribers will stop receiving messages.",
            ))
    
    # Detect changes within channels (payload field changes)
    for channel_name, old_channel in old_channels.items():
        if channel_name not in new_channels:
            continue
        
        new_channel = new_channels[channel_name]
        
        # Compare subscribe message payloads
        old_payload = old_channel.subscribe_message or old_channel.publish_message
        new_payload = new_channel.subscribe_message or new_channel.publish_message
        
        if old_payload and new_payload:
            old_fields = set(old_payload.get("properties", {}).keys())
            new_fields = set(new_payload.get("properties", {}).keys())
            old_required = set(old_payload.get("required", []))
            new_required = set(new_payload.get("required", []))
            
            # Fields removed from payload
            for field_name in old_fields - new_fields:
                changes.append(BreakingChange(
                    change_type="message_field_removed",
                    path=file_path,
                    method=channel_name,
                    field_name=field_name,
                    field_type="payload",
                    location="asyncapi",
                    severity="breaking",
                    description=f"Field '{field_name}' removed from channel '{channel_name}' message payload. Consumers parsing this field will get null.",
                ))
            
            # New required fields added
            added_required = new_required - old_required
            for field_name in added_required:
                if field_name not in old_fields:
                    changes.append(BreakingChange(
                        change_type="required_field_added",
                        path=file_path,
                        method=channel_name,
                        field_name=field_name,
                        field_type=_get_field_type(new_payload, field_name),
                        location="asyncapi",
                        severity="breaking",
                        description=f"Required field '{field_name}' added to channel '{channel_name}'. Publishers must include this field.",
                    ))
            
            # Field type changes
            old_props = old_payload.get("properties", {})
            new_props = new_payload.get("properties", {})
            for field_name in old_fields & new_fields:
                old_type = old_props.get(field_name, {}).get("type", "")
                new_type = new_props.get(field_name, {}).get("type", "")
                if old_type and new_type and old_type != new_type:
                    changes.append(BreakingChange(
                        change_type="field_type_changed",
                        path=file_path,
                        method=channel_name,
                        field_name=field_name,
                        field_type=f"{old_type} -> {new_type}",
                        old_type=old_type,
                        new_type=new_type,
                        location="asyncapi",
                        severity="breaking",
                        description=f"Field '{field_name}' in channel '{channel_name}' type changed from '{old_type}' to '{new_type}'. Consumers will fail to deserialize.",
                    ))
    
    # Detect removed messages (from components)
    for msg_name in old_messages:
        if msg_name not in new_messages:
            changes.append(BreakingChange(
                change_type="message_removed",
                path=file_path,
                method="components",
                field_name=msg_name,
                field_type="message",
                location="asyncapi",
                severity="breaking",
                description=f"Message '{msg_name}' removed from components. Channels referencing it will break.",
            ))
    
    # Detect removed servers
    for server_name in old_servers:
        if server_name not in new_servers:
            changes.append(BreakingChange(
                change_type="server_removed",
                path=file_path,
                method="servers",
                field_name=server_name,
                field_type="server",
                location="asyncapi",
                severity="breaking",
                description=f"Server '{server_name}' removed. Clients configured to connect to this server will fail.",
            ))
    
    return changes


def format_asyncapi_changes(changes: list[BreakingChange]) -> str:
    """Format AsyncAPI breaking changes for display."""
    if not changes:
        return "✅ No breaking changes in AsyncAPI spec."
    
    lines = [f"⚠️  {len(changes)} breaking change(s) in AsyncAPI spec:", ""]
    for i, c in enumerate(changes, 1):
        lines.append(f"  [{i}] {c.change_type}")
        lines.append(f"      Channel: {c.method}")
        lines.append(f"      Field: {c.field_name} ({c.field_type})")
        lines.append(f"      {c.description}")
        lines.append("")
    return "\n".join(lines)


# === Helper Functions ===

def _extract_message_schema(operation: dict, spec: dict) -> Optional[dict]:
    """Extract message payload schema from a publish/subscribe operation."""
    message = operation.get("message", {})
    if "$ref" in message:
        message = _resolve_ref(message["$ref"], spec)
    return _extract_payload(message, spec)


def _extract_payload(message: dict, spec: dict) -> Optional[dict]:
    """Extract payload schema from a message definition."""
    payload = message.get("payload", {})
    if "$ref" in payload:
        payload = _resolve_ref(payload["$ref"], spec)
    if not payload:
        return None
    return payload


def _get_field_type(payload: dict, field_name: str) -> str:
    """Get the type of a field in a payload schema."""
    props = payload.get("properties", {})
    if field_name in props:
        return props[field_name].get("type", "unknown")
    return "unknown"


def _resolve_ref(ref: str, spec: dict) -> dict:
    """Resolve a $ref pointer in the spec."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    current = spec
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return {}
    return current if isinstance(current, dict) else {}
