"""
Ripple Agent — self-hosted API change propagation daemon.

Works on any network, any git host, any code review system.
"""
from agent.core import RippleAgent, AgentConfig
from agent.adapters import (
    PlatformAdapter,
    GenericGitAdapter,
    CRUXAdapter,
    Commit,
    ReviewResult,
)
