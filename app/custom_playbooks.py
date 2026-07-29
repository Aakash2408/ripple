"""
ripple/app/custom_playbooks.py

Custom Playbook Loader — lets users define their own co-change patterns.

Users add a `.ripple.yaml` in their repo root to teach Ripple about
their codebase-specific patterns.

Example .ripple.yaml:

```yaml
playbooks:
  - name: "Our API gateway"
    trigger:
      files: ["api/openapi.yaml"]
      change_types: ["added_required_field", "removed_field"]
    consumers:
      - pattern: "sdk/python/**/*.py"
        confidence: 0.95
        reason: "Python SDK wraps this API"
      - pattern: "sdk/node/**/*.ts"
        confidence: 0.95
        reason: "Node SDK wraps this API"
      - pattern: "tests/integration/**/*"
        confidence: 0.85
        reason: "Integration tests call this API"
      - pattern: "docs/api/**/*.md"
        confidence: 0.70
        reason: "API docs reference these endpoints"

  - name: "Database migrations"
    trigger:
      files: ["db/migrations/*.sql", "prisma/schema.prisma"]
      change_types: ["*"]
    consumers:
      - pattern: "src/models/**/*"
        confidence: 0.90
        reason: "ORM models mirror DB schema"
      - pattern: "src/repositories/**/*"
        confidence: 0.85
        reason: "Repository layer queries these tables"

ignore:
  # Files to never open PRs for
  - "*.lock"
  - "node_modules/**"
  - ".git/**"
  - "dist/**"
  - "build/**"

settings:
  # Only open PRs for consumers above this confidence
  min_confidence: 0.6
  # Auto-learn from git history on install (default: true)
  auto_learn: true
  # Max PRs to open per push event
  max_prs_per_push: 10
```
"""

import fnmatch
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


@dataclass
class CustomConsumer:
    """A user-defined consumer pattern."""
    pattern: str
    confidence: float
    reason: str


@dataclass
class CustomPlaybook:
    """A user-defined playbook."""
    name: str
    trigger_files: list[str]
    trigger_change_types: list[str]
    consumers: list[CustomConsumer]


@dataclass
class RippleConfig:
    """Parsed .ripple.yaml configuration."""
    playbooks: list[CustomPlaybook] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)
    min_confidence: float = 0.6
    auto_learn: bool = True
    max_prs_per_push: int = 10

    def should_ignore(self, file_path: str) -> bool:
        """Check if a file matches any ignore pattern."""
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return True
        return False

    def get_predictions_for_change(
        self, changed_file: str, change_type: str
    ) -> list[dict]:
        """
        Get custom playbook predictions for a specific file change.
        Returns list of {pattern, confidence, reason, playbook_name}.
        """
        predictions = []

        for playbook in self.playbooks:
            # Check if this change matches the playbook trigger
            trigger_matches = False

            # Check trigger files (glob match)
            for trigger_pattern in playbook.trigger_files:
                if fnmatch.fnmatch(changed_file, trigger_pattern):
                    trigger_matches = True
                    break

            if not trigger_matches:
                continue

            # Check change type
            if "*" not in playbook.trigger_change_types:
                if change_type not in playbook.trigger_change_types:
                    continue

            # This playbook matches — return its consumer predictions
            for consumer in playbook.consumers:
                predictions.append({
                    "pattern": consumer.pattern,
                    "confidence": consumer.confidence,
                    "reason": consumer.reason,
                    "source": f"custom:{playbook.name}",
                })

        return predictions


def parse_ripple_config(content: str) -> Optional[RippleConfig]:
    """
    Parse a .ripple.yaml file content into a RippleConfig.
    Returns None if parsing fails.
    """
    if yaml is None:
        # Fallback: try to parse without PyYAML (basic JSON-like subset)
        return _parse_without_yaml(content)

    try:
        data = yaml.safe_load(content)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    config = RippleConfig()

    # Parse playbooks
    for pb_data in data.get("playbooks", []):
        trigger = pb_data.get("trigger", {})
        consumers = []
        for c in pb_data.get("consumers", []):
            consumers.append(CustomConsumer(
                pattern=c.get("pattern", ""),
                confidence=c.get("confidence", 0.7),
                reason=c.get("reason", "Custom playbook match"),
            ))

        config.playbooks.append(CustomPlaybook(
            name=pb_data.get("name", "unnamed"),
            trigger_files=trigger.get("files", []),
            trigger_change_types=trigger.get("change_types", ["*"]),
            consumers=consumers,
        ))

    # Parse ignore patterns
    config.ignore_patterns = data.get("ignore", [])

    # Parse settings
    settings = data.get("settings", {})
    config.min_confidence = settings.get("min_confidence", 0.6)
    config.auto_learn = settings.get("auto_learn", True)
    config.max_prs_per_push = settings.get("max_prs_per_push", 10)

    return config


def _parse_without_yaml(content: str) -> Optional[RippleConfig]:
    """Minimal parser for when PyYAML isn't available."""
    # Return empty config -- users need PyYAML for custom playbooks
    return RippleConfig()


# === Default .ripple.yaml template ===

DEFAULT_TEMPLATE = """# .ripple.yaml — Teach Ripple about your codebase
# See: https://github.com/Aakash2408/ripple#custom-playbooks

playbooks:
  - name: "API spec"
    trigger:
      files: ["**/openapi.yaml", "**/openapi.json", "**/swagger.*"]
      change_types: ["added_required_field", "removed_field"]
    consumers:
      - pattern: "src/client.*"
        confidence: 0.85
        reason: "Client code calls this API"
      - pattern: "tests/**/*"
        confidence: 0.80
        reason: "Tests exercise this API"

ignore:
  - "*.lock"
  - "node_modules/**"
  - "dist/**"
  - "build/**"

settings:
  min_confidence: 0.6
  auto_learn: true
  max_prs_per_push: 10
"""
