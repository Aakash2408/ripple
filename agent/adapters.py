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


# ---------------------------------------------------------------------------
# Phabricator Adapter (Meta, Uber, Pinterest)
# ---------------------------------------------------------------------------

class PhabricatorAdapter(PlatformAdapter):
    """
    Phabricator adapter — uses `arc diff` CLI or Conduit API.
    
    Phabricator is used by: Meta, Uber, Pinterest, Dropbox, and many others.
    
    Config:
        phabricator_url: URL of the Phabricator instance (e.g. https://phabricator.example.com)
        conduit_token: API token for Conduit API calls
        use_arc: If True, uses `arc diff` CLI. If False, uses Conduit REST API.
    """
    
    def __init__(self, phabricator_url: str = "", conduit_token: str = "",
                 use_arc: bool = True):
        self.phabricator_url = phabricator_url or os.environ.get("PHABRICATOR_URL", "")
        self.conduit_token = conduit_token or os.environ.get("PHABRICATOR_TOKEN", "")
        self.use_arc = use_arc
    
    @property
    def name(self) -> str:
        return "phabricator"
    
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
        Create a Phabricator Differential revision.
        
        Strategy:
        1. Create branch + commit (standard git flow)
        2a. If use_arc=True: run `arc diff` to create a Differential
        2b. If use_arc=False: use Conduit API (differential.createrevision)
        """
        branch_name = f"ripple/fix-{int(time.time())}"
        
        try:
            # Create branch + commit
            subprocess.run(["git", "checkout", "-b", branch_name],
                         cwd=repo, capture_output=True, timeout=10)
            
            full_path = os.path.join(repo, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            
            subprocess.run(["git", "add", file_path],
                         cwd=repo, capture_output=True, timeout=10)
            
            commit_msg = f"{title}\n\n{description}\n\n[Auto-generated by Ripple Agent]"
            subprocess.run(["git", "commit", "-m", commit_msg],
                         cwd=repo, capture_output=True, timeout=10)
            
            if self.use_arc:
                return self._create_via_arc(repo, branch_name, title, description)
            else:
                return self._create_via_conduit(repo, branch_name, title, description)
        
        except Exception as e:
            # Always try to switch back to original branch
            subprocess.run(["git", "checkout", "-"], cwd=repo,
                         capture_output=True, timeout=10)
            return ReviewResult(url="", id="", status="error", message=str(e))
    
    def _create_via_arc(self, repo: str, branch_name: str,
                        title: str, description: str) -> ReviewResult:
        """Create Differential revision via `arc diff`."""
        try:
            # arc diff creates a Differential revision from the current commit
            arc_result = subprocess.run(
                ["arc", "diff", "--create", "--message", title,
                 "--allow-untracked", "HEAD~1"],
                cwd=repo, capture_output=True, text=True, timeout=120
            )
            
            # Switch back to original branch
            subprocess.run(["git", "checkout", "-"], cwd=repo,
                         capture_output=True, timeout=10)
            
            if arc_result.returncode == 0:
                # Extract Differential ID from output (e.g., "Created D12345")
                diff_id = ""
                for line in arc_result.stdout.split("\n"):
                    if "D" in line and any(c.isdigit() for c in line):
                        import re
                        match = re.search(r'D(\d+)', line)
                        if match:
                            diff_id = f"D{match.group(1)}"
                            break
                
                url = f"{self.phabricator_url}/D{diff_id.lstrip('D')}" if diff_id else ""
                
                return ReviewResult(
                    url=url,
                    id=diff_id or branch_name,
                    status="created",
                    message=f"Differential created: {diff_id}",
                )
            else:
                return ReviewResult(
                    url="",
                    id=branch_name,
                    status="created",
                    message=f"Branch created, arc diff failed: {arc_result.stderr[:200]}",
                )
        
        except FileNotFoundError:
            subprocess.run(["git", "checkout", "-"], cwd=repo,
                         capture_output=True, timeout=10)
            return ReviewResult(
                url="", id=branch_name, status="created",
                message="arc CLI not found. Install Arcanist or set use_arc=False for Conduit API.",
            )
    
    def _create_via_conduit(self, repo: str, branch_name: str,
                            title: str, description: str) -> ReviewResult:
        """Create Differential revision via Conduit REST API."""
        import json
        
        try:
            import requests
        except ImportError:
            subprocess.run(["git", "checkout", "-"], cwd=repo,
                         capture_output=True, timeout=10)
            return ReviewResult(
                url="", id=branch_name, status="created",
                message="requests library not installed. Branch created, push manually.",
            )
        
        # Get the diff content
        diff_result = subprocess.run(
            ["git", "diff", "HEAD~1", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=30
        )
        
        # Switch back before making API call
        subprocess.run(["git", "checkout", "-"], cwd=repo,
                     capture_output=True, timeout=10)
        
        if not self.phabricator_url or not self.conduit_token:
            return ReviewResult(
                url="", id=branch_name, status="created",
                message="No Phabricator URL/token configured. Branch created, push manually.",
            )
        
        # Step 1: Create a raw diff
        create_diff_resp = requests.post(
            f"{self.phabricator_url}/api/differential.createrawdiff",
            data={
                "api.token": self.conduit_token,
                "diff": diff_result.stdout,
            },
            timeout=30,
        )
        
        if create_diff_resp.status_code != 200:
            return ReviewResult(
                url="", id=branch_name, status="created",
                message=f"Conduit API error: {create_diff_resp.status_code}",
            )
        
        diff_data = create_diff_resp.json()
        diff_id = diff_data.get("result", {}).get("id", "")
        
        if not diff_id:
            return ReviewResult(
                url="", id=branch_name, status="created",
                message=f"Failed to create raw diff: {diff_data}",
            )
        
        # Step 2: Create a revision from the diff
        create_rev_resp = requests.post(
            f"{self.phabricator_url}/api/differential.createrevision",
            data={
                "api.token": self.conduit_token,
                "diffid": diff_id,
                "fields[title]": title,
                "fields[summary]": f"{description}\n\n[Auto-generated by Ripple Agent]",
            },
            timeout=30,
        )
        
        if create_rev_resp.status_code == 200:
            rev_data = create_rev_resp.json()
            rev_id = rev_data.get("result", {}).get("revisionid", "")
            phid = rev_data.get("result", {}).get("phid", "")
            
            url = f"{self.phabricator_url}/D{rev_id}" if rev_id else ""
            
            return ReviewResult(
                url=url,
                id=f"D{rev_id}" if rev_id else branch_name,
                status="created",
                message=f"Differential D{rev_id} created via Conduit API",
            )
        else:
            return ReviewResult(
                url="", id=branch_name, status="created",
                message=f"Diff created ({diff_id}) but revision failed: {create_rev_resp.text[:200]}",
            )
    
    def search_code(self, repo: str, query: str) -> list[CodeSearchResult]:
        generic = GenericGitAdapter()
        return generic.search_code(repo, query)


# ---------------------------------------------------------------------------
# Gerrit Adapter (Google, Android, Chromium, Qt)
# ---------------------------------------------------------------------------

class GerritAdapter(PlatformAdapter):
    """
    Gerrit adapter — uses Gerrit REST API to create changes.
    
    Gerrit is used by: Google, Android (AOSP), Chromium, Eclipse, Qt, MediaWiki.
    
    Gerrit uses a different model than PR-based systems:
    - Each commit = one "change" (not a branch with multiple commits)
    - Changes are identified by a Change-Id in the commit message
    - Reviews happen on the change itself (not on a PR)
    - Submitting = merging
    
    Config:
        gerrit_url: URL of the Gerrit instance (e.g. https://gerrit-review.googlesource.com)
        username: Gerrit username (HTTP credentials)
        password: Gerrit HTTP password (from Settings → HTTP Credentials)
        project: Gerrit project name (e.g. "chromium/src")
    """
    
    def __init__(self, gerrit_url: str = "", username: str = "",
                 password: str = "", project: str = ""):
        self.gerrit_url = gerrit_url or os.environ.get("GERRIT_URL", "")
        self.username = username or os.environ.get("GERRIT_USERNAME", "")
        self.password = password or os.environ.get("GERRIT_PASSWORD", "")
        self.project = project or os.environ.get("GERRIT_PROJECT", "")
    
    @property
    def name(self) -> str:
        return "gerrit"
    
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
        Create a Gerrit change.
        
        Strategy:
        1. Write fix + commit with Change-Id trailer
        2. Push to refs/for/main (Gerrit's review ref)
        
        OR if push isn't possible:
        3. Use Gerrit REST API to create a change directly
        """
        import uuid
        
        # Generate Gerrit Change-Id (format: I + 40 hex chars)
        change_id = f"I{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
        
        branch_name = f"ripple/fix-{int(time.time())}"
        
        try:
            # Create branch
            subprocess.run(["git", "checkout", "-b", branch_name],
                         cwd=repo, capture_output=True, timeout=10)
            
            # Write fix
            full_path = os.path.join(repo, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            
            subprocess.run(["git", "add", file_path],
                         cwd=repo, capture_output=True, timeout=10)
            
            # Commit with Change-Id trailer (Gerrit requirement)
            commit_msg = (
                f"{title}\n\n"
                f"{description}\n\n"
                f"[Auto-generated by Ripple Agent]\n\n"
                f"Change-Id: {change_id}"
            )
            subprocess.run(["git", "commit", "-m", commit_msg],
                         cwd=repo, capture_output=True, timeout=10)
            
            # Try pushing to Gerrit review ref
            push_result = self._push_for_review(repo, branch_name)
            
            if push_result:
                return push_result
            
            # Fallback: use REST API
            api_result = self._create_via_api(repo, file_path, content,
                                             title, description, change_id)
            
            # Switch back
            subprocess.run(["git", "checkout", "-"], cwd=repo,
                         capture_output=True, timeout=10)
            
            return api_result
        
        except Exception as e:
            subprocess.run(["git", "checkout", "-"], cwd=repo,
                         capture_output=True, timeout=10)
            return ReviewResult(url="", id="", status="error", message=str(e))
    
    def _push_for_review(self, repo: str, branch_name: str) -> Optional[ReviewResult]:
        """Push to refs/for/main to create a Gerrit change."""
        try:
            # Gerrit's magic ref for code review
            target_branch = "main"  # Could be configurable
            push_result = subprocess.run(
                ["git", "push", "origin", f"HEAD:refs/for/{target_branch}"],
                cwd=repo, capture_output=True, text=True, timeout=60
            )
            
            subprocess.run(["git", "checkout", "-"], cwd=repo,
                         capture_output=True, timeout=10)
            
            if push_result.returncode == 0:
                # Extract change URL from Gerrit push output
                # Gerrit outputs: remote: https://gerrit.example.com/c/project/+/12345
                import re
                change_url = ""
                change_number = ""
                for line in (push_result.stdout + push_result.stderr).split("\n"):
                    match = re.search(r'(https?://[^\s]+/\+/\d+)', line)
                    if match:
                        change_url = match.group(1)
                        num_match = re.search(r'/\+/(\d+)', change_url)
                        if num_match:
                            change_number = num_match.group(1)
                        break
                
                return ReviewResult(
                    url=change_url,
                    id=change_number or branch_name,
                    status="created",
                    message=f"Gerrit change created: {change_url or 'pushed to refs/for/main'}",
                )
            
            return None  # Push failed, try API
        
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
    
    def _create_via_api(self, repo: str, file_path: str, content: str,
                        title: str, description: str,
                        change_id: str) -> ReviewResult:
        """Create a Gerrit change via REST API."""
        try:
            import requests
        except ImportError:
            return ReviewResult(
                url="", id="", status="created",
                message="requests not installed. Commit created locally with Change-Id.",
            )
        
        if not self.gerrit_url or not self.username:
            return ReviewResult(
                url="", id=change_id, status="created",
                message=f"No Gerrit credentials. Commit created with Change-Id: {change_id}",
            )
        
        # Gerrit REST API: create change
        auth = (self.username, self.password)
        
        # Step 1: Create the change
        create_resp = requests.post(
            f"{self.gerrit_url}/a/changes/",
            json={
                "project": self.project,
                "subject": title,
                "branch": "main",
                "topic": "ripple-fix",
                "status": "NEW",
            },
            auth=auth,
            timeout=30,
        )
        
        # Gerrit returns )]}\n prefix for XSSI protection
        if create_resp.status_code in (200, 201):
            import json
            text = create_resp.text
            if text.startswith(")]}'"):
                text = text[4:].strip()
            
            change_data = json.loads(text)
            change_number = change_data.get("_number", "")
            change_url = f"{self.gerrit_url}/c/{self.project}/+/{change_number}"
            
            # Step 2: Upload the file content to the change's edit
            edit_resp = requests.put(
                f"{self.gerrit_url}/a/changes/{change_number}/edit/{file_path}",
                data=content.encode("utf-8"),
                auth=auth,
                headers={"Content-Type": "application/octet-stream"},
                timeout=30,
            )
            
            # Step 3: Publish the edit (makes it a new patchset)
            if edit_resp.status_code in (200, 201, 204):
                requests.post(
                    f"{self.gerrit_url}/a/changes/{change_number}/edit:publish",
                    auth=auth,
                    timeout=30,
                )
            
            return ReviewResult(
                url=change_url,
                id=str(change_number),
                status="created",
                message=f"Gerrit change {change_number} created via API",
            )
        else:
            return ReviewResult(
                url="", id=change_id, status="created",
                message=f"API create failed ({create_resp.status_code}). Commit has Change-Id: {change_id}",
            )
    
    def search_code(self, repo: str, query: str) -> list[CodeSearchResult]:
        """Search locally via git grep."""
        generic = GenericGitAdapter()
        return generic.search_code(repo, query)
