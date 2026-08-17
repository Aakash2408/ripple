from __future__ import annotations
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

    # Operation-specific detail. Three sites already READ these via
    # getattr(change, 'new_name', '') on a dataclass that never declared them,
    # so every read resolved to the falsy default and the code behind it was
    # dead:
    #
    #   fix_generator  the rename branch required new_name to delegate, so
    #                  renames never reached a template and fell through to
    #                  "Unsupported change type" -- unchanged code, no PR,
    #                  silence.
    #   fix_generator  the type-change branch could only recover old_type by
    #                  string-splitting field_type on ' -> '.
    #   webhook        the rename COMMIT MESSAGE rendered literally
    #                  "fix: Rename field 'phone_number' to 'new name'".
    #
    # Defaulted and appended, so all 65 existing construction sites are
    # unaffected. Engines populate them in the next commit.
    new_name: str = ""        # rename_field / rename_type: the new name
    old_type: str = ""        # change_field_type: the type before
    new_type: str = ""        # change_field_type: the type after


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
    
    # Detect removed fields
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
    
    # Detect field type changes
    for path, methods in new_paths.items():
        for method, operation in methods.items():
            if method.startswith("x-") or method == "parameters":
                continue
            if path not in old_paths or method not in old_paths[path]:
                continue
            
            old_operation = old_paths[path][method]
            old_props = _get_body_properties(old_operation, old_spec)
            new_props = _get_body_properties(operation, new_spec)
            
            for field_name in set(old_props.keys()) & set(new_props.keys()):
                old_type = old_props[field_name].get("type", "")
                new_type = new_props[field_name].get("type", "")
                if old_type and new_type and old_type != new_type:
                    result.breaking_changes.append(BreakingChange(
                        change_type="field_type_changed",
                        path=path,
                        method=method,
                        field_name=field_name,
                        field_type=f"{old_type} -> {new_type}",
                        old_type=old_type,
                        new_type=new_type,
                        location="request_body",
                        severity="breaking",
                        description=f"Field '{field_name}' type changed from '{old_type}' to '{new_type}'. Consumers sending the old type will get validation errors.",
                    ))
    
    # Detect removed endpoints
    for path, methods in old_paths.items():
        for method, operation in methods.items():
            if method.startswith("x-") or method == "parameters":
                continue
            if path not in new_paths or method not in new_paths.get(path, {}):
                result.breaking_changes.append(BreakingChange(
                    change_type="endpoint_removed",
                    path=path,
                    method=method,
                    field_name=f"{method.upper()} {path}",
                    field_type="endpoint",
                    location="path",
                    severity="breaking",
                    description=f"Endpoint '{method.upper()} {path}' was removed. All consumers calling this endpoint will get 404.",
                ))
    
    # Detect removed response fields
    for path, methods in new_paths.items():
        for method, operation in methods.items():
            if method.startswith("x-") or method == "parameters":
                continue
            if path not in old_paths or method not in old_paths[path]:
                continue
            
            old_operation = old_paths[path][method]
            old_response_fields = _get_response_fields(old_operation, old_spec)
            new_response_fields = _get_response_fields(operation, new_spec)
            
            removed_response = old_response_fields - new_response_fields
            for field_name in removed_response:
                result.breaking_changes.append(BreakingChange(
                    change_type="response_field_removed",
                    path=path,
                    method=method,
                    field_name=field_name,
                    field_type="response",
                    location="response_body",
                    severity="breaking",
                    description=f"Response field '{field_name}' was removed. Consumers reading this field will get null/undefined.",
                ))
    
    # Detect required headers added
    for path, methods in new_paths.items():
        for method, operation in methods.items():
            if method.startswith("x-") or method == "parameters":
                continue
            
            new_required_params = _get_required_parameters(operation, new_spec, "header")
            old_required_params = set()
            
            if path in old_paths and method in old_paths[path]:
                old_required_params = _get_required_parameters(old_paths[path][method], old_spec, "header")
            
            added_headers = new_required_params - old_required_params
            for header_name in added_headers:
                result.breaking_changes.append(BreakingChange(
                    change_type="required_header_added",
                    path=path,
                    method=method,
                    field_name=header_name,
                    field_type="header",
                    location="parameter",
                    severity="breaking",
                    description=f"Required header '{header_name}' added. Requests without this header will be rejected.",
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



def _get_body_properties(operation: dict, spec: dict) -> dict:
    """Get all properties from request body schema as {name: schema_dict}."""
    request_body = operation.get("requestBody", {})
    if "$ref" in request_body:
        request_body = _resolve_ref(request_body["$ref"], spec)
    
    content = request_body.get("content", {})
    for media_type in ["application/json", "application/x-www-form-urlencoded"]:
        if media_type in content:
            schema = content[media_type].get("schema", {})
            if "$ref" in schema:
                schema = _resolve_ref(schema["$ref"], spec)
            return schema.get("properties", {})
    return {}


def _get_response_fields(operation: dict, spec: dict) -> set:
    """Get all field names from the 200/201 response schema."""
    responses = operation.get("responses", {})
    for status in ["200", "201", "2XX"]:
        if status in responses:
            response = responses[status]
            if "$ref" in response:
                response = _resolve_ref(response["$ref"], spec)
            content = response.get("content", {})
            for media_type in ["application/json"]:
                if media_type in content:
                    schema = content[media_type].get("schema", {})
                    if "$ref" in schema:
                        schema = _resolve_ref(schema["$ref"], spec)
                    return set(schema.get("properties", {}).keys())
    return set()


def _get_required_parameters(operation: dict, spec: dict, location: str) -> set:
    """Get required parameter names for a given location (header, query, path)."""
    params = operation.get("parameters", [])
    required = set()
    for param in params:
        if "$ref" in param:
            param = _resolve_ref(param["$ref"], spec)
        if param.get("in") == location and param.get("required", False):
            required.add(param.get("name", ""))
    return required


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
