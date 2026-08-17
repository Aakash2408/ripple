"""
Natural Language Change Description module.

Allows developers to describe intent in plain English instead of just pushing a diff.
Parses intent via Claude API with regex fallback, converts to structured breaking changes,
predicts consequences using the consumer graph, and formats markdown previews.
"""

import os
import re
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

import requests
from flask import request, jsonify

from app.diff_engine import BreakingChange
from app.consumer_graph import ConsumerGraph
from app.consumer_finder import find_consumers

logger = logging.getLogger(__name__)

# Read through llm_config so an ANTHROPIC_BASE_URL override reaches this caller
# too. The URL used to be a module-level constant pinned to api.anthropic.com,
# which meant this site silently ignored the override every other site honoured.
from app.llm_config import api_key as _llm_api_key, messages_url as _llm_messages_url, model as _llm_model

VALID_ACTIONS = {"add", "remove", "rename", "deprecate"}
VALID_TARGETS = {"field", "endpoint", "type", "parameter", "header", "schema"}


@dataclass
class ChangeIntent:
    """Structured representation of a developer's intended change."""
    action: str  # add, remove, rename, deprecate
    target: str  # field, endpoint, type, parameter, header, schema
    name: str
    details: str = ""
    contract_type: str = "openapi"


# --- Regex fallback patterns ---

PATTERNS = [
    # "remove X from Y"
    (r"(?:remove|delete|drop)\s+(?:the\s+)?(.+?)\s+(?:from|in)\s+(?:the\s+)?(.+)",
     lambda m: ChangeIntent(action="remove", target=_infer_target(m.group(1)),
                            name=m.group(1).strip(), details=m.group(2).strip())),
    # "add X to Y"
    (r"(?:add|create|introduce)\s+(?:a\s+)?(?:new\s+)?(.+?)\s+(?:to|in|on)\s+(?:the\s+)?(.+)",
     lambda m: ChangeIntent(action="add", target=_infer_target(m.group(1)),
                            name=m.group(1).strip(), details=m.group(2).strip())),
    # "rename X to Y"
    (r"(?:rename|change)\s+(?:the\s+)?(.+?)\s+to\s+(.+)",
     lambda m: ChangeIntent(action="rename", target=_infer_target(m.group(1)),
                            name=m.group(1).strip(), details=m.group(2).strip())),
    # "deprecate X"
    (r"(?:deprecate|sunset|phase out)\s+(?:the\s+)?(.+)",
     lambda m: ChangeIntent(action="deprecate", target=_infer_target(m.group(1)),
                            name=m.group(1).strip(), details="")),
]


def _infer_target(text: str) -> str:
    """Infer the target type from the name text."""
    text_lower = text.lower()
    if any(kw in text_lower for kw in ["endpoint", "route", "path", "url", "api"]):
        return "endpoint"
    if any(kw in text_lower for kw in ["type", "model", "schema", "class"]):
        return "type"
    if any(kw in text_lower for kw in ["param", "query", "header"]):
        return "parameter"
    return "field"


def _regex_parse(text: str) -> Optional[ChangeIntent]:
    """Attempt regex-based parsing for common patterns."""
    text_clean = text.strip().rstrip(".")
    for pattern, builder in PATTERNS:
        match = re.search(pattern, text_clean, re.IGNORECASE)
        if match:
            return builder(match)
    return None


def _claude_parse(text: str) -> Optional[ChangeIntent]:
    """Parse intent using Claude API."""
    if not _llm_api_key():
        return None

    prompt = f"""Parse this developer intent into structured fields.
Return ONLY valid JSON with keys: action, target, name, details, contract_type.

- action: one of [add, remove, rename, deprecate]
- target: one of [field, endpoint, type, parameter, header, schema]
- name: the specific thing being changed
- details: additional context (destination for rename, location for add/remove)
- contract_type: one of [openapi, proto, graphql, asyncapi, avro, trpc, thrift, json_schema, smithy, db]

Developer said: "{text}"

JSON:"""

    try:
        resp = requests.post(
            _llm_messages_url(),
            headers={
                "x-api-key": _llm_api_key(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _llm_model(),
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=10,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"].strip()
        # Extract JSON from response
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return ChangeIntent(
                action=data.get("action", "remove"),
                target=data.get("target", "field"),
                name=data.get("name", ""),
                details=data.get("details", ""),
                contract_type=data.get("contract_type", "openapi"),
            )
    except Exception as e:
        logger.warning(f"Claude parse failed, falling back to regex: {e}")
    return None


def parse_intent(text: str) -> ChangeIntent:
    """
    Parse a natural language change description into a structured ChangeIntent.
    Tries Claude API first, falls back to regex patterns.
    """
    # Try Claude first
    intent = _claude_parse(text)
    if intent:
        return intent

    # Regex fallback
    intent = _regex_parse(text)
    if intent:
        return intent

    # Last resort: treat entire text as a removal of unknown target
    return ChangeIntent(
        action="remove",
        target="field",
        name=text.strip(),
        details="(could not parse — please be more specific)",
    )


def intent_to_breaking_changes(intent: ChangeIntent) -> List[BreakingChange]:
    """Convert a parsed ChangeIntent into structured BreakingChange objects."""
    changes = []

    if intent.action == "remove":
        changes.append(BreakingChange(
            change_type="removal",
            path=intent.name,
            description=f"Removing {intent.target} '{intent.name}' from {intent.details}",
            severity="breaking",
            contract_type=intent.contract_type,
        ))
    elif intent.action == "rename":
        changes.append(BreakingChange(
            change_type="rename",
            path=intent.name,
            description=f"Renaming {intent.target} '{intent.name}' to '{intent.details}'",
            severity="breaking",
            contract_type=intent.contract_type,
        ))
    elif intent.action == "deprecate":
        changes.append(BreakingChange(
            change_type="deprecation",
            path=intent.name,
            description=f"Deprecating {intent.target} '{intent.name}'",
            severity="warning",
            contract_type=intent.contract_type,
        ))
    elif intent.action == "add":
        changes.append(BreakingChange(
            change_type="addition",
            path=intent.name,
            description=f"Adding {intent.target} '{intent.name}' to {intent.details}",
            severity="non-breaking",
            contract_type=intent.contract_type,
        ))

    return changes


def predict_consequences(intent: ChangeIntent, graph: ConsumerGraph) -> List[Dict]:
    """
    Use the consumer graph to predict what will break from this intent.
    Returns a list of affected services with impact details.
    """
    consequences = []
    breaking_changes = intent_to_breaking_changes(intent)

    for change in breaking_changes:
        # Find consumers that reference this path
        consumers = graph.find_consumers_of(change.path)
        for consumer in consumers:
            consequences.append({
                "service": consumer.name,
                "repo": consumer.repo,
                "file": consumer.file_path,
                "usage": consumer.usage_type,
                "severity": change.severity,
                "change": change.description,
                "auto_fixable": change.severity != "breaking" or intent.action == "rename",
            })

    return consequences


def format_preview(intent: ChangeIntent, consequences: List[Dict]) -> str:
    """Format a markdown preview of what will happen if the change is made."""
    action_verb = {
        "remove": "remove",
        "add": "add",
        "rename": "rename",
        "deprecate": "deprecate",
    }.get(intent.action, intent.action)

    lines = [
        f"## 🔮 Change Preview",
        f"",
        f"**Intent:** {action_verb} {intent.target} `{intent.name}`",
    ]

    if intent.details:
        if intent.action == "rename":
            lines.append(f"**New name:** `{intent.details}`")
        else:
            lines.append(f"**Context:** {intent.details}")

    lines.append(f"**Contract type:** {intent.contract_type}")
    lines.append("")

    if not consequences:
        lines.append("✅ **No consumers found** — this change appears safe.")
    else:
        breaking = [c for c in consequences if c["severity"] == "breaking"]
        warnings = [c for c in consequences if c["severity"] == "warning"]
        safe = [c for c in consequences if c["severity"] == "non-breaking"]

        lines.append(f"### Impact: {len(consequences)} service(s) affected")
        lines.append("")

        if breaking:
            lines.append(f"#### 🚨 Breaking ({len(breaking)})")
            for c in breaking:
                fix_tag = " *(auto-fixable)*" if c["auto_fixable"] else ""
                lines.append(f"- **{c['service']}** — `{c['file']}`{fix_tag}")
            lines.append("")

        if warnings:
            lines.append(f"#### ⚠️ Warnings ({len(warnings)})")
            for c in warnings:
                lines.append(f"- **{c['service']}** — {c['change']}")
            lines.append("")

        if safe:
            lines.append(f"#### ✅ Non-breaking ({len(safe)})")
            for c in safe:
                lines.append(f"- **{c['service']}**")
            lines.append("")

        fixable = sum(1 for c in consequences if c["auto_fixable"])
        if fixable:
            lines.append(f"💡 **{fixable}/{len(consequences)}** can be auto-fixed by Ripple.")

    return "\n".join(lines)


# --- Flask endpoint handler ---

def handle_describe(consumer_graph: ConsumerGraph):
    """
    POST /describe
    Body: {"text": "I want to remove the phone field from user API"}
    Returns: markdown preview of consequences
    """
    data = request.get_json(force=True)
    text = data.get("text", "").strip()

    if not text:
        return jsonify({"error": "Missing 'text' field"}), 400

    intent = parse_intent(text)
    consequences = predict_consequences(intent, consumer_graph)
    preview = format_preview(intent, consequences)

    return jsonify({
        "intent": asdict(intent),
        "consequences": consequences,
        "preview": preview,
        "breaking_changes": [
            {"type": bc.change_type, "path": bc.path, "severity": bc.severity}
            for bc in intent_to_breaking_changes(intent)
        ],
    })
