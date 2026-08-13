from __future__ import annotations
"""
ripple/agent/core.py

Ripple Agent — self-hosted daemon that monitors git repos for API changes.

Runs on any network (corporate, on-prem, air-gapped). No external dependencies.
Uses the same diff engines as Ripple Cloud but with local git operations.

Usage:
    # CLI mode (one-shot scan)
    ripple-agent scan /path/to/repo

    # Watch mode (polls every N seconds)
    ripple-agent watch /path/to/repos --interval 60

    # Docker
    docker run -v /repos:/repos ripple-agent watch /repos --interval 60

Config file: ripple-agent.yaml
"""

import os
import sys
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.adapters import PlatformAdapter, GenericGitAdapter, CRUXAdapter, Commit

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ripple-agent")


@dataclass
class AgentConfig:
    """Configuration for the Ripple Agent."""
    repos: list[str] = field(default_factory=list)
    repos_dir: str = ""
    interval: int = 60  # seconds between polls
    platform: str = "generic-git"  # generic-git, crux, phabricator, gerrit
    min_confidence: float = 0.6
    max_fixes_per_scan: int = 10
    spec_patterns: list[str] = field(default_factory=lambda: [
        "*.yaml", "*.yml", "*.json", "*.proto", "*.graphql", "*.gql",
        "*.sql", "*.prisma", "asyncapi*",
    ])
    
    @classmethod
    def from_yaml(cls, path: str) -> "AgentConfig":
        """Load config from a YAML file."""
        try:
            import yaml
            with open(path) as f:
                data = yaml.safe_load(f)
            return cls(
                repos=data.get("repos", []),
                repos_dir=data.get("repos_dir", ""),
                interval=data.get("interval", 60),
                platform=data.get("platform", "generic-git"),
                min_confidence=data.get("min_confidence", 0.6),
                max_fixes_per_scan=data.get("max_fixes_per_scan", 10),
                spec_patterns=data.get("spec_patterns", cls.spec_patterns),
            )
        except Exception:
            return cls()


class RippleAgent:
    """
    Self-hosted Ripple agent that monitors repos for API breaking changes.
    
    Platform-agnostic: works with any git host via adapter plugins.
    """
    
    def __init__(self, config: AgentConfig, adapter: PlatformAdapter = None):
        self.config = config
        self.adapter = adapter or self._create_adapter()
        self._last_check: dict[str, str] = {}  # repo → last checked timestamp
    
    def _create_adapter(self) -> PlatformAdapter:
        """Create the appropriate platform adapter."""
        if self.config.platform == "crux":
            return CRUXAdapter()
        else:
            return GenericGitAdapter(
                repos_dir=self.config.repos_dir,
                repo_paths=self.config.repos,
            )
    
    def scan(self, repo: str, since: str = "1 hour ago") -> dict:
        """
        Scan a single repo for breaking API changes since last check.
        
        Returns:
            {
                "repo": str,
                "commits_checked": int,
                "breaking_changes": int,
                "fixes_created": int,
                "details": [...],
            }
        """
        logger.info(f"Scanning {repo} (since: {since})")
        
        # Get new commits
        commits = self.adapter.get_new_commits(repo, since=since)
        if not commits:
            return {"repo": repo, "commits_checked": 0, "breaking_changes": 0, "fixes_created": 0, "details": []}
        
        logger.info(f"  Found {len(commits)} new commit(s)")
        
        all_changes = []
        fixes_created = 0
        
        for commit in commits:
            # Find spec files in this commit
            spec_files = [f for f in commit.changed_files if self._is_spec_file(f)]
            if not spec_files:
                continue
            
            logger.info(f"  Commit {commit.sha[:8]}: {len(spec_files)} spec file(s) changed")
            
            for spec_path in spec_files:
                # Get old and new content
                old_content = self.adapter.get_file_at_commit(repo, spec_path, f"{commit.sha}~1")
                new_content = self.adapter.get_file_at_commit(repo, spec_path, commit.sha)
                
                if not old_content or not new_content:
                    continue
                
                # Detect breaking changes using diff engines
                breaking_changes = self._detect_changes(old_content, new_content, spec_path)
                
                if not breaking_changes:
                    continue
                
                logger.info(f"    ⚠️  {len(breaking_changes)} breaking change(s) in {spec_path}")
                all_changes.extend(breaking_changes)
                
                # Find consumers and create fixes
                for change in breaking_changes:
                    consumers = self.adapter.search_code(repo, change.path)
                    
                    for consumer in consumers[:5]:
                        if consumer.file_path == spec_path:
                            continue  # Skip the spec itself
                        
                        # Get consumer content
                        consumer_content = self.adapter.get_file(repo, consumer.file_path)
                        if not consumer_content:
                            continue
                        
                        # Generate fix
                        fixed_content = self._generate_fix(consumer_content, change)
                        if fixed_content and fixed_content != consumer_content:
                            # Create review/fix
                            title = f"fix: Add required field '{change.field_name}' to {change.method} {change.path}"
                            description = f"API spec changed: {change.description}\n\nThis fix updates the consumer code."
                            
                            result = self.adapter.create_fix_review(
                                repo, consumer.file_path, fixed_content, title, description
                            )
                            
                            if result.status == "created":
                                fixes_created += 1
                                logger.info(f"    ✅ Fix created: {result.message}")
                            
                            if fixes_created >= self.config.max_fixes_per_scan:
                                break
        
        return {
            "repo": repo,
            "commits_checked": len(commits),
            "breaking_changes": len(all_changes),
            "fixes_created": fixes_created,
            "details": [{"type": c.change_type, "path": c.path, "field": c.field_name} for c in all_changes],
        }
    
    def scan_all(self, since: str = "1 hour ago") -> list[dict]:
        """Scan all configured repos."""
        repos = self.config.repos or []
        if self.config.repos_dir:
            adapter = GenericGitAdapter(repos_dir=self.config.repos_dir)
            repos.extend(adapter.get_repos())
        
        results = []
        for repo in repos:
            result = self.scan(repo, since=since)
            results.append(result)
        
        return results
    
    def watch(self):
        """
        Run in watch mode — poll repos every N seconds.
        Runs forever until interrupted.
        """
        logger.info(f"Ripple Agent starting (platform: {self.adapter.name}, interval: {self.config.interval}s)")
        logger.info(f"   Monitoring: {self.config.repos or self.config.repos_dir}")
        
        while True:
            try:
                results = self.scan_all(since=f"{self.config.interval * 2} seconds ago")
                
                total_changes = sum(r["breaking_changes"] for r in results)
                total_fixes = sum(r["fixes_created"] for r in results)
                
                if total_changes > 0:
                    logger.info(f"🔍 Scan complete: {total_changes} breaking change(s), {total_fixes} fix(es) created")
                
                time.sleep(self.config.interval)
            
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error(f"Error during scan: {e}")
                time.sleep(self.config.interval)
    
    def _is_spec_file(self, filepath: str) -> bool:
        """Check if a file is an API contract."""
        lower = filepath.lower()
        indicators = [
            "openapi", "swagger", "api-spec", "api_spec", "spec.yaml", "spec.yml",
            ".proto", ".graphql", ".gql", "asyncapi",
            "schema.prisma",
        ]
        if any(ind in lower for ind in indicators):
            return True
        if lower.endswith((".yaml", ".yml", ".json")) and "api" in lower:
            return True
        if lower.endswith(".sql") and any(x in lower for x in ["migration", "schema", "ddl"]):
            return True
        return False
    
    def _detect_changes(self, old_content: str, new_content: str, spec_path: str) -> list:
        """Route to the correct diff engine based on file type."""
        from app.diff_engine import diff_specs, BreakingChange
        from app.proto_diff import diff_proto
        from app.graphql_diff import diff_graphql
        from app.migration_diff import diff_schema
        from app.asyncapi_diff import diff_asyncapi
        
        lower = spec_path.lower()
        
        if ".proto" in lower:
            return diff_proto(old_content, new_content, file_path=spec_path)
        elif ".graphql" in lower or ".gql" in lower:
            return diff_graphql(old_content, new_content, file_path=spec_path)
        elif ".sql" in lower or "prisma" in lower:
            return diff_schema(old_content, new_content, file_path=spec_path)
        elif "asyncapi" in lower:
            return diff_asyncapi(old_content, new_content, file_path=spec_path)
        else:
            # OpenAPI
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(old_content)
                old_path = f.name
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(new_content)
                new_path = f.name
            result = diff_specs(old_path, new_path)
            os.unlink(old_path)
            os.unlink(new_path)
            return result.breaking_changes
    
    def _generate_fix(self, consumer_content: str, change) -> Optional[str]:
        """Generate a fix for the consumer (simple template-based)."""
        # For now: return None (fix generation reuses app.fix_generator)
        # In production, this would call the same LLM/template logic
        try:
            from app.consumer_finder import ConsumerMatch
            from app.fix_generator import _generate_with_template
            
            consumer = ConsumerMatch(
                file_path="", line_number=0, code_snippet="",
                confidence="high", match_reason="Agent scan",
                language=self._detect_lang(consumer_content),
            )
            fixed, explanation = _generate_with_template(consumer_content, consumer, change)
            return fixed if fixed != consumer_content else None
        except Exception:
            return None
    
    def _detect_lang(self, content: str) -> str:
        """Simple language detection from content."""
        if "import " in content and "def " in content:
            return "python"
        if "interface " in content or "const " in content:
            return "typescript"
        if "public class " in content or "private " in content:
            return "java"
        return "unknown"


# === CLI Entry Point ===

def main():
    """CLI entry point for ripple-agent."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Ripple Agent — self-hosted API change propagation")
    subparsers = parser.add_subparsers(dest="command")
    
    # scan command
    scan_parser = subparsers.add_parser("scan", help="One-shot scan of repo(s)")
    scan_parser.add_argument("repos", nargs="+", help="Path(s) to git repos")
    scan_parser.add_argument("--since", default="1 day ago", help="How far back to look")
    scan_parser.add_argument("--platform", default="generic-git", help="Platform adapter")
    
    # watch command
    watch_parser = subparsers.add_parser("watch", help="Continuously monitor repos")
    watch_parser.add_argument("repos", nargs="+", help="Path(s) to git repos or directory")
    watch_parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")
    watch_parser.add_argument("--platform", default="generic-git", help="Platform adapter")
    
    # config command
    config_parser = subparsers.add_parser("config", help="Generate sample config file")
    
    args = parser.parse_args()
    
    if args.command == "scan":
        config = AgentConfig(repos=args.repos, platform=args.platform)
        agent = RippleAgent(config)
        results = agent.scan_all(since=args.since)
        
        for r in results:
            if r["breaking_changes"] > 0:
                print(f"⚠️  {r['repo']}: {r['breaking_changes']} breaking change(s), {r['fixes_created']} fix(es)")
                for d in r["details"]:
                    print(f"    {d['type']}: {d['field']} in {d['path']}")
            else:
                print(f"✅ {r['repo']}: no breaking changes")
    
    elif args.command == "watch":
        # Check if first arg is a directory (repos_dir) or individual repos
        if len(args.repos) == 1 and os.path.isdir(args.repos[0]) and not os.path.isdir(os.path.join(args.repos[0], ".git")):
            config = AgentConfig(repos_dir=args.repos[0], interval=args.interval, platform=args.platform)
        else:
            config = AgentConfig(repos=args.repos, interval=args.interval, platform=args.platform)
        
        agent = RippleAgent(config)
        agent.watch()
    
    elif args.command == "config":
        print(SAMPLE_CONFIG)
    
    else:
        parser.print_help()


SAMPLE_CONFIG = """# ripple-agent.yaml — Self-hosted Ripple Agent configuration

# Repos to monitor (absolute paths)
repos:
  - /path/to/your/api-repo
  - /path/to/another/repo

# Or specify a directory containing multiple repos
# repos_dir: /home/user/projects

# Poll interval (seconds)
interval: 60

# Platform adapter: generic-git, crux, phabricator, gerrit
platform: generic-git

# Minimum confidence to create a fix (0.0 - 1.0)
min_confidence: 0.6

# Maximum fixes to create per scan cycle
max_fixes_per_scan: 10

# Spec file patterns to watch
spec_patterns:
  - "*.yaml"
  - "*.yml"
  - "*.proto"
  - "*.graphql"
  - "*.gql"
  - "*.sql"
  - "*.prisma"
  - "asyncapi*"
"""


if __name__ == "__main__":
    main()
