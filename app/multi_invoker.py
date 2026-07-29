"""
ripple/app/multi_invoker.py

Multi-Invoker Detection — prevents silent breaks from shared resources.

Based on PropBench CSG GeoRaven finding:
  A config file was removed for one consumer, but a SECOND consumer
  also read that file and silently broke. Even the expert missed it.

This module detects when a changed file has MULTIPLE consumers and warns
about ALL of them — not just the obvious one.

Use cases:
- Config file read by multiple Lambdas
- Shared schema imported by multiple services
- API spec consumed by SDK + webhook handler + docs generator
- Database table queried by multiple services
"""

from dataclasses import dataclass
from typing import Optional

from .history_learner import HistoryLearner


@dataclass
class InvokerInfo:
    """A service/file that reads/invokes a shared resource."""
    file_path: str
    relationship: str  # "imports", "reads", "calls", "references"
    confidence: float
    last_seen: Optional[float] = None


@dataclass
class MultiInvokerWarning:
    """Warning that a change affects multiple consumers."""
    shared_file: str
    invokers: list[InvokerInfo]
    risk_level: str  # "high", "medium", "low"
    message: str


class MultiInvokerDetector:
    """
    Detects when a file change could silently break OTHER consumers
    that also depend on the same file.
    
    Uses co-change history to identify all known readers of a file.
    If a file has 2+ consumers and you're only fixing one, WARN.
    
    This is the feature that catches what senior engineers miss.
    """
    
    def __init__(self, learner: Optional[HistoryLearner] = None):
        self.learner = learner
        # Hard-coded patterns for common shared resources
        self.shared_patterns = [
            # Config files (often read by multiple services)
            {"pattern": "config", "risk": "high", "reason": "Config files often have multiple consumers"},
            {"pattern": "constants", "risk": "medium", "reason": "Constants may be imported by multiple modules"},
            {"pattern": "schema", "risk": "high", "reason": "Schema files define contracts for multiple consumers"},
            {"pattern": "shared", "risk": "high", "reason": "Files in shared/ directories have multiple consumers by definition"},
            {"pattern": "common", "risk": "medium", "reason": "Common utilities may be used widely"},
            {"pattern": "types", "risk": "high", "reason": "Type definitions are imported by multiple modules"},
            {"pattern": "proto", "risk": "high", "reason": "Proto files generate code for multiple services"},
            {"pattern": "migration", "risk": "high", "reason": "Schema changes affect all ORM models"},
        ]
    
    def check(self, changed_file: str, known_consumer: str = None) -> Optional[MultiInvokerWarning]:
        """
        Check if a changed file has multiple invokers.
        
        Args:
            changed_file: The file being modified
            known_consumer: The consumer we already know about (optional)
            
        Returns:
            MultiInvokerWarning if multiple consumers detected, None otherwise
        """
        invokers = []
        
        # Method 1: Check co-change history for other consumers
        if self.learner:
            predictions = self.learner.predict_consumers(changed_file, top_n=20)
            for file_path, confidence, reason in predictions:
                if file_path != known_consumer and confidence >= 0.3:
                    invokers.append(InvokerInfo(
                        file_path=file_path,
                        relationship="co-changes-with",
                        confidence=confidence,
                    ))
        
        # Method 2: Check if file matches shared resource patterns
        risk_level = self._assess_risk(changed_file)
        
        # Only warn if we found OTHER consumers beyond the known one
        if len(invokers) >= 1 or (risk_level == "high" and not known_consumer):
            message = self._build_warning_message(changed_file, invokers, known_consumer, risk_level)
            return MultiInvokerWarning(
                shared_file=changed_file,
                invokers=invokers,
                risk_level=risk_level,
                message=message,
            )
        
        return None
    
    def check_removal(self, removed_content: str, file_path: str) -> Optional[MultiInvokerWarning]:
        """
        Special check for REMOVALS (the CSG GeoRaven case).
        
        When something is being removed from a shared file,
        the risk is highest — other consumers still expect it.
        """
        warning = self.check(file_path)
        
        if warning:
            warning.risk_level = "high"
            warning.message = (
                f"⚠️  REMOVAL DETECTED in shared file '{file_path}'.\n"
                f"This file has {len(warning.invokers)} other known consumers.\n"
                f"Removing content may silently break:\n" +
                "\n".join(f"  • {inv.file_path} ({inv.confidence:.0%} confidence)"
                         for inv in warning.invokers[:5]) +
                f"\n\nConsider: add a per-consumer flag instead of removing the shared entry."
            )
        
        return warning
    
    def _assess_risk(self, file_path: str) -> str:
        """Assess the risk level of a file being a shared resource."""
        lower = file_path.lower()
        
        for pattern in self.shared_patterns:
            if pattern["pattern"] in lower:
                return pattern["risk"]
        
        return "low"
    
    def _build_warning_message(
        self, changed_file: str, invokers: list[InvokerInfo],
        known_consumer: str, risk_level: str
    ) -> str:
        """Build a human-readable warning message."""
        lines = []
        
        if risk_level == "high":
            lines.append(f"🔴 HIGH RISK: '{changed_file}' has multiple consumers")
        elif risk_level == "medium":
            lines.append(f"🟡 CAUTION: '{changed_file}' may have other consumers")
        else:
            lines.append(f"ℹ️  '{changed_file}' might be shared")
        
        if known_consumer:
            lines.append(f"\n  Known consumer: {known_consumer}")
        
        if invokers:
            lines.append(f"\n  Other consumers detected ({len(invokers)}):")
            for inv in invokers[:5]:
                lines.append(f"    • {inv.file_path} — {inv.confidence:.0%} confidence ({inv.relationship})")
            
            if len(invokers) > 5:
                lines.append(f"    ... and {len(invokers) - 5} more")
        
        lines.append(f"\n  💡 Recommendation: verify ALL consumers handle this change, not just the obvious one.")
        
        return "\n".join(lines)


def format_warning_for_pr(warning: MultiInvokerWarning) -> str:
    """Format the warning for inclusion in a PR body."""
    if not warning:
        return ""
    
    lines = [
        "### ⚠️ Multi-Consumer Warning",
        "",
        f"The file `{warning.shared_file}` appears to have **multiple consumers**.",
        f"Risk level: **{warning.risk_level.upper()}**",
        "",
    ]
    
    if warning.invokers:
        lines.append("| Consumer | Confidence | Relationship |")
        lines.append("|----------|-----------|--------------|")
        for inv in warning.invokers[:10]:
            lines.append(f"| `{inv.file_path}` | {inv.confidence:.0%} | {inv.relationship} |")
        lines.append("")
    
    lines.extend([
        "> **Action required:** Please verify that ALL consumers of this file",
        "> can handle the change — not just the one this PR fixes.",
        "> ",
        "> *This warning is based on historical co-change analysis (PropBench research).*",
    ])
    
    return "\n".join(lines)
