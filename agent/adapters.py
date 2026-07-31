from __future__ import annotations
"""
ripple/agent/adapters.py

Platform Adapters — plugin system for different code hosting platforms.

Each adapter implements:
- get_new_commits(repo, since) → list of commits with changed files
- get_file_at_commit(repo, path, sha) → file content
- get_file(repo, path) → current file content
- create_fix_review(repo, branch, file, content, title, description) → review URL
- search_code(repo, query) → list of matching files

Adapters:
- GenericGitAdapter: works with any git repo on disk (polls via git log)
- GitHubAdapter: uses GitHub API (existing ripple cloud logic)
- GitLabAdapter: uses GitLab API (existing ripple cloud logic)
- BitbucketAdapter: uses Bitbucket Cloud API
- CRUXAdapter: uses Amazon's cr CLI
- PhabricatorAdapter: uses arc diff / Conduit API
- GerritAdapter: uses Gerrit REST API
"""

import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Commit:
    """A commit with metadata."""
    sha: str
    message: str
    author: str
    timestamp: float
    changed_files: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


@dataclass
class CodeSearchResult:
    """A code search hit."""
    file_path: str
    line_number: int
    snippet: str


@dataclass
class ReviewResult:
    """Result of creating a fix review/PR/MR."""
    url: str
    id: str
    status: str  # "created", "error"
    message: str


class PlatformAdapter(ABC):
    """Base class for all platform adapters."""
    
    @abstractmethod
    def get_new_commits(self, repo: str, since: str = "1 hour ago") -> list[Commit]:
        """Get commits since a given time."""
        ...
    
    @abstractmethod
    def get_file_at_commit(self, repo: str, path: str, sha: str) -> str:
        """Get file content at a specific commit."""
        ...
    
    @abstractmethod
    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> str:
        """Get current file content."""
        ...
    
    @abstractmethod
    def create_fix_review(self, repo: str, file_path: str, content: str,
                          title: str, description: str) -> ReviewResult:
        """Create a fix (PR, MR, CR, diff) on the platform."""
        ...
    
    @abstractmethod
    def search_code(self, repo: str, query: str) -> list[CodeSearchResult]:
        """Search for code patterns in a repo."""
        ...
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Platform name for display."""
        ...


class GenericGitAdapter(PlatformAdapter):
    """
    Works with any git repository on the local filesystem.
    
    Polls via `git log` and searches via `grep`.
    Creates fixes by committing to a branch and leaving a patch file.
    
    Config:
        repos_dir: directory containing git repos (or list of paths)
    """
    
    def __init__(self, repos_dir: str = "", repo_paths: list[str] = None):
        self.repos_dir = repos_dir
        self.repo_paths = repo_paths or []
    
    @property
    def name(self) -> str:
        return "generic-git"
    
    def get_repos(self) -> list[str]:
        """Discover all git repos."""
        if self.repo_paths:
            return self.repo_paths
        
        repos = []
        if self.repos_dir and os.path.isdir(self.repos_dir):
            for entry in os.listdir(self.repos_dir):
                path = os.path.join(self.repos_dir, entry)
                if os.path.isdir(os.path.join(path, ".git")):
                    repos.append(path)
        return repos
    
    def get_new_commits(self, repo: str, since: str = "1 hour ago") -> list[Commit]:
        """Get recent commits via git log."""
        try:
            result = subprocess.run(
                ["git", "log", f"--since={since}", "--format=%H|%s|%an|%at", "--name-only"],
                cwd=repo, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return []
            
            commits = []
            current_commit = None
            
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                if "|" in line and line.count("|") >= 3:
                    # This is a commit header line
                    parts = line.split("|", 3)
                    if len(parts) >= 4:
                        if current_commit:
                            commits.append(current_commit)
                        current_commit = Commit(
                            sha=parts[0],
                            message=parts[1],
                            author=parts[2],
                            timestamp=float(parts[3]) if parts[3].isdigit() else time.time(),
                        )
                elif current_commit:
                    # This is a changed file
                    current_commit.changed_files.append(line.strip())
                    current_commit.modified_files.append(line.strip())
            
            if current_commit:
                commits.append(current_commit)
            
            return commits
        
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
    
    def get_file_at_commit(self, repo: str, path: str, sha: str) -> str:
        """Get file content at a specific commit via git show."""
        try:
            result = subprocess.run(
                ["git", "show", f"{sha}:{path}"],
                cwd=repo, capture_output=True, text=True, timeout=10
            )
            return result.stdout if result.returncode == 0 else ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
    
    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> str:
        """Get file content at a given ref."""
        return self.get_file_at_commit(repo, path, ref)
    
    def create_fix_review(self, repo: str, file_path: str, content: str,
                          title: str, description: str) -> ReviewResult:
        """
        Create a fix on a new branch.
        
        For generic git, we:
        1. Create a branch: ripple/fix-<timestamp>
        2. Write the fixed file
        3. Commit with the description
        4. Leave the branch for the user to review/push
        """
        branch_name = f"ripple/fix-{int(time.time())}"
        
        try:
            # Create branch
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=repo, capture_output=True, timeout=10
            )
            
            # Write fix
            full_path = os.path.join(repo, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            
            # Commit
            subprocess.run(["git", "add", file_path], cwd=repo, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "commit", "-m", f"{title}\n\n{description}\n\n[Auto-generated by Ripple Agent]"],
                cwd=repo, capture_output=True, timeout=10
            )
            
            # Switch back to original branch
            subprocess.run(["git", "checkout", "-"], cwd=repo, capture_output=True, timeout=10)
            
            return ReviewResult(
                url=f"git branch: {branch_name}",
                id=branch_name,
                status="created",
                message=f"Fix committed on branch '{branch_name}'. Review and merge manually.",
            )
        
        except Exception as e:
            return ReviewResult(url="", id="", status="error", message=str(e))
    
    def search_code(self, repo: str, query: str) -> list[CodeSearchResult]:
        """Search via git grep."""
        try:
            result = subprocess.run(
                ["git", "grep", "-n", "--fixed-strings", query],
                cwd=repo, capture_output=True, text=True, timeout=30
            )
            
            results = []
            for line in result.stdout.strip().split("\n")[:20]:  # Limit to 20 results
                if ":" in line:
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        results.append(CodeSearchResult(
                            file_path=parts[0],
                            line_number=int(parts[1]) if parts[1].isdigit() else 0,
                            snippet=parts[2][:200],
                        ))
            return results
        
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []


class CRUXAdapter(PlatformAdapter):
    """
    Amazon CRUX adapter — uses `cr` CLI to create code reviews.
    
    Requires: Amazon dev desktop with mwinit, Brazil workspace.
    
    This adapter is for internal Amazon use only.
    """
    
    def __init__(self, workspace_path: str = ""):
        self.workspace_path = workspace_path
    
    @property
    def name(self) -> str:
        return "crux"
    
    def get_new_commits(self, repo: str, since: str = "1 hour ago") -> list[Commit]:
        """Uses git log (same as GenericGitAdapter)."""
        generic = GenericGitAdapter()
        return generic.get_new_commits(repo, since)
    
    def get_file_at_commit(self, repo: str, path: str, sha: str) -> str:
        generic = GenericGitAdapter()
        return generic.get_file_at_commit(repo, path, sha)
    
    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> str:
        generic = GenericGitAdapter()
        return generic.get_file(repo, path, ref)
    
    def create_fix_review(self, repo: str, file_path: str, content: str,
                          title: str, description: str) -> ReviewResult:
        """
        Create a CRUX code review using `cr` CLI.
        
        Steps:
        1. Create branch
        2. Write fix + commit
        3. Run `cr` to create a code review
        """
        branch_name = f"ripple/fix-{int(time.time())}"
        
        try:
            # Create branch + commit (same as GenericGitAdapter)
            subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo, capture_output=True, timeout=10)
            
            full_path = os.path.join(repo, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            
            subprocess.run(["git", "add", file_path], cwd=repo, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "commit", "-m", f"{title}\n\n{description}\n\n[Auto-generated by Ripple Agent]"],
                cwd=repo, capture_output=True, timeout=10
            )
            
            # Create CR using `cr` CLI
            cr_result = subprocess.run(
                ["cr", "--parent", "HEAD~1"],
                cwd=repo, capture_output=True, text=True, timeout=60
            )
            
            # Switch back
            subprocess.run(["git", "checkout", "-"], cwd=repo, capture_output=True, timeout=10)
            
            if cr_result.returncode == 0:
                # Extract CR ID from output
                cr_id = ""
                for line in cr_result.stdout.split("\n"):
                    if "CR-" in line:
                        cr_id = line.strip()
                        break
                
                return ReviewResult(
                    url=f"https://code.amazon.com/reviews/{cr_id}" if cr_id else "",
                    id=cr_id or branch_name,
                    status="created",
                    message=f"Code review created: {cr_id}",
                )
            else:
                return ReviewResult(
                    url="",
                    id=branch_name,
                    status="created",
                    message=f"Branch created but CR failed: {cr_result.stderr[:200]}. Push manually.",
                )
        
        except Exception as e:
            return ReviewResult(url="", id="", status="error", message=str(e))
    
    def search_code(self, repo: str, query: str) -> list[CodeSearchResult]:
        generic = GenericGitAdapter()
        return generic.search_code(repo, query)
