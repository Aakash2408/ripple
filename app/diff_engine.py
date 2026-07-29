"""
ripple/app/diff_engine.py

OpenAPI Diff Engine — detects breaking changes between two spec versions.

V0 scope: detect ONE change type only:
  - Added required field to request body

That's enough for the YC demo.
"""

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class BreakingChange:
    """A single breaking change detected between two specs."""
    change_type: str          # "added_required_field", "removed_field", "renamed_field"
    path: str                 # e.g., "/users"
    method: str               # e.g., "post"
    field_name: str           # e.g., "country"
    field_type: str           # e.g., "string"
    location: str             # "request_body" or "parameter"
    severity: str             # "breaking", "warning", "info"
    description: str


@dataclass
class DiffResult:
    """Result of comparing two OpenAPI specs."""
    old_spec_path: str
    new_spec_path: str
    breaking_changes: list[BreakingChange] = field(default_factory=list)
    
    @property
    def has_breaking_changes(self) -> bool:
        return len(self.breaking_changes) > 0
    
    def format(self) -> str:
        if not self.breaking_changes:
            return "✅ No breaking changes detected."
        
        lines = [
            f"⚠️  {len(self.breaking_changes)} breaking change(s) detected:",
            "",
        ]
        for i, change in enumerate(self.breaking_changes, 1):
            lines.append(f"  [{i}] {change.change_type}")
            lines.append(f"      Endpoint: {change.method.upper()} {change.path}")
            lines.append(f"      Field: {change.field_name} ({change.field_type})")
            lines.append(f"      Severity: {change.severity}")
            lines.append(f"      {change.description}")
            lines.append("")
        
        return "\n".join(lines)


def load_spec(path: str) -> dict:
    """Load an OpenAPI spec from YAML or JSON."""
    with open(path) as f:
        if path.endswith(".json"):
            import json
            return json.load(f)
        return yaml.safe_load(f)


def diff_specs(old_path: str, new_path: str) -> DiffResult:
    """
    Compare two OpenAPI specs and return breaking changes.
    
    V0: Only detects added required fields in request bodies.
    """
    old_spec = load_spec(old_path)
    new_spec = load_spec(new_path)
    
    result = DiffResult(old_spec_path=old_path, new_spec_path=new_path)
    
    # Get all paths in new spec
    old_paths = old_spec.get("paths", {})
    new_paths = new_spec.get("paths", {})
    
    for path, methods in new_paths.items():
        for method, operation in methods.items():
            if method.startswith("x-") or method == "parameters":
                continue
            
            # Check request body for new required fields
            new_required = _get_required_body_fields(operation, new_spec)
            old_required = set()
            
            if path in old_paths and method in old_paths[path]:
                old_operation = old_paths[path][method]
                old_required = _get_required_body_fields(old_operation, old_spec)
            
            # Fields that are required in new but weren't in old
            added_required = new_required - old_required
            
            for field_name in added_required:
                field_type = _get_field_type(operation, new_spec, field_name)
                result.breaking_changes.append(BreakingChange(
                    change_type="added_required_field",
                    path=path,
                    method=method,
                    field_name=field_name,
                    field_type=field_type,
                    location="request_body",
                    severity="breaking",
                    description=f"New required field '{field_name}' added to request body. All consumers must include this field.",
                ))
    
    # Also detect removed fields (V0.1)
    for path, methods in old_paths.items():
        for method, operation in methods.items():
            if method.startswith("x-") or method == "parameters":
                continue
            
            old_fields = _get_all_body_fields(operation, old_spec)
            new_fields = set()
            
            if path in new_paths and method in new_paths[path]:
                new_operation = new_paths[path][method]
                new_fields = _get_all_body_fields(new_operation, new_spec)
            
            removed_fields = old_fields - new_fields
            
            for field_name in removed_fields:
                result.breaking_changes.append(BreakingChange(
                    change_type="removed_field",
                    path=path,
                    method=method,
                    field_name=field_name,
                    field_type="unknown",
                    location="request_body",
                    severity="breaking",
                    description=f"Field '{field_name}' was removed from request body. Consumers still sending this field may get errors.",
                ))
    
    return result


def _get_required_body_fields(operation: dict, spec: dict) -> set[str]:
    """Extract required fields from a request body schema."""
    request_body = operation.get("requestBody", {})
    
    # Handle $ref
    if "$ref" in request_body:
        request_body = _resolve_ref(request_body["$ref"], spec)
    
    content = request_body.get("content", {})
    
    # Try application/json first
    for media_type in ["application/json", "application/x-www-form-urlencoded"]:
        if media_type in content:
            schema = content[media_type].get("schema", {})
            if "$ref" in schema:
                schema = _resolve_ref(schema["$ref"], spec)
            return set(schema.get("required", []))
    
    return set()


def _get_all_body_fields(operation: dict, spec: dict) -> set[str]:
    """Extract all fields (required + optional) from a request body schema."""
    request_body = operation.get("requestBody", {})
    
    if "$ref" in request_body:
        request_body = _resolve_ref(request_body["$ref"], spec)
    
    content = request_body.get("content", {})
    
    for media_type in ["application/json", "application/x-www-form-urlencoded"]:
        if media_type in content:
            schema = content[media_type].get("schema", {})
            if "$ref" in schema:
                schema = _resolve_ref(schema["$ref"], spec)
            return set(schema.get("properties", {}).keys())
    
    return set()


def _get_field_type(operation: dict, spec: dict, field_name: str) -> str:
    """Get the type of a specific field in the request body."""
    request_body = operation.get("requestBody", {})
    
    if "$ref" in request_body:
        request_body = _resolve_ref(request_body["$ref"], spec)
    
    content = request_body.get("content", {})
    
    for media_type in ["application/json", "application/x-www-form-urlencoded"]:
        if media_type in content:
            schema = content[media_type].get("schema", {})
            if "$ref" in schema:
                schema = _resolve_ref(schema["$ref"], spec)
            properties = schema.get("properties", {})
            if field_name in properties:
                return properties[field_name].get("type", "unknown")
    
    return "unknown"


def _resolve_ref(ref: str, spec: dict) -> dict:
    """Resolve a $ref pointer in the spec."""
    # e.g., "#/components/schemas/User"
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
