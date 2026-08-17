from __future__ import annotations
"""
ripple/app/trpc_diff.py

tRPC Diff Engine — detects breaking changes in tRPC router definitions.

tRPC defines APIs as TypeScript router files. Breaking changes:
- Procedure removed (clients calling it get runtime error)
- Input schema changed (validation fails)
- Output type changed (clients reading wrong shape)
- Procedure renamed (old name returns 404/NOT_FOUND)
- Middleware removed (auth/validation bypassed or broken)

tRPC routers are TypeScript files — we parse them with regex patterns.
"""

import re
from dataclasses import dataclass
from typing import Optional
from .diff_engine import BreakingChange


def parse_trpc_procedures(content: str) -> dict:
    """
    Extract tRPC procedure definitions from TypeScript source.
    Returns {procedure_name: {type, input_schema, ...}}
    """
    procedures = {}
    
    # Match patterns like: .query("getUser", ...) or .mutation("createUser", ...)
    # Also: getUser: t.procedure.query(...)  (new tRPC v11 style)
    
    # Pattern 1: router({ name: t.procedure.input(z.object({...})).query/mutation(...)})
    proc_pattern = re.compile(
        r'(\w+)\s*:\s*\w+\.procedure'
        r'(?:\.input\(([^)]+)\))?'
        r'\.(query|mutation|subscription)',
        re.MULTILINE
    )
    
    for match in proc_pattern.finditer(content):
        name = match.group(1)
        input_schema = match.group(2) or ""
        proc_type = match.group(3)
        procedures[name] = {
            "type": proc_type,
            "input": input_schema.strip(),
        }
    
    # Pattern 2: older style .query("name", { input: ..., resolve: ... })
    old_pattern = re.compile(
        r'\.(query|mutation|subscription)\(\s*["\'](\w+)["\']',
        re.MULTILINE
    )
    
    for match in old_pattern.finditer(content):
        proc_type = match.group(1)
        name = match.group(2)
        if name not in procedures:
            procedures[name] = {"type": proc_type, "input": ""}
    
    return procedures


def diff_trpc(old_content: str, new_content: str, file_path: str = "router.ts") -> list[BreakingChange]:
    """Compare two tRPC router files and return breaking changes."""
    old_procs = parse_trpc_procedures(old_content)
    new_procs = parse_trpc_procedures(new_content)
    
    changes = []
    
    # Procedure removed
    for name in old_procs:
        if name not in new_procs:
            changes.append(BreakingChange(
                change_type="procedure_removed",
                path=file_path,
                method=name,
                field_name=name,
                field_type=old_procs[name]["type"],
                location="trpc",
                severity="breaking",
                description=f"tRPC procedure '{name}' ({old_procs[name]['type']}) removed. Clients calling it will get NOT_FOUND error.",
            ))
    
    # Procedure type changed (query → mutation or vice versa)
    for name in set(old_procs.keys()) & set(new_procs.keys()):
        if old_procs[name]["type"] != new_procs[name]["type"]:
            changes.append(BreakingChange(
                change_type="procedure_type_changed",
                path=file_path,
                method=name,
                field_name=name,
                field_type=f"{old_procs[name]['type']} -> {new_procs[name]['type']}",
                old_type=old_procs[name]["type"],
                new_type=new_procs[name]["type"],
                location="trpc",
                severity="breaking",
                description=f"tRPC procedure '{name}' changed from {old_procs[name]['type']} to {new_procs[name]['type']}. Clients using wrong method will fail.",
            ))
        
        # Input schema changed
        old_input = old_procs[name]["input"]
        new_input = new_procs[name]["input"]
        if old_input and new_input and old_input != new_input:
            changes.append(BreakingChange(
                change_type="input_schema_changed",
                path=file_path,
                method=name,
                field_name=f"{name}.input",
                field_type="zod schema",
                location="trpc",
                severity="breaking",
                description=f"tRPC procedure '{name}' input schema changed. Clients sending old shape will fail validation.",
            ))
    
    return changes
