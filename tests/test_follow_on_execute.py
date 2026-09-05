"""
Tests for follow-on PR execution (Stage 9: fork→PR wiring).

These test _execute_follow_on_prs in webhook.py — the function that takes
a follow-on plan and actually opens PRs via fork_pr.open_fork_pr.
"""
import sys
import base64

sys.path.insert(0, '.')

from app import follow_on as _follow_on
from app import fork_pr as _fork_pr


# ─── fork_pr unit tests ─────────────────────────────────────────────

class FakeAPI:
    """Records calls and returns canned responses."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}
        self._default = {}

    def __call__(self, method, path, token, data=None):
        self.calls.append((method, path, data))
        key = f"{method} {path.split('?')[0]}"
        # Check exact match first, then prefix matches
        if key in self._responses:
            return self._responses[key]
        for k, v in self._responses.items():
            if key.startswith(k.split('?')[0]):
                return v
        return self._default


def _no_sleep(n):
    """Injected into fork_pr so tests don't wait."""
    pass


class TestEnsureFork:
    """ensure_fork: idempotent forking with poll-until-ready."""

    def test_existing_fork_returns_immediately(self):
        api = FakeAPI({
            "GET /user": {"login": "ripple-bot"},
            "GET /repos/ripple-bot/consumer-api": {"id": 1},
        })
        result = _fork_pr.ensure_fork("org/consumer-api", "tok", api=api, sleep=_no_sleep)
        assert result.usable
        assert not result.created
        assert result.fork == "ripple-bot/consumer-api"

    def test_fork_created_and_ready(self):
        call_count = [0]
        def api(method, path, token, data=None):
            if method == "GET" and "/user" in path:
                return {"login": "ripple-bot"}
            if method == "GET" and "ripple-bot/consumer-api" in path:
                call_count[0] += 1
                if call_count[0] <= 1:
                    return {"error": "not found"}  # first check: doesn't exist
                return {"id": 1}  # poll: ready
            if method == "POST" and "/forks" in path:
                return {"id": 99}  # 202 queued
            return {}

        result = _fork_pr.ensure_fork("org/consumer-api", "tok", api=api, sleep=_no_sleep)
        assert result.usable
        assert result.created

    def test_fork_timeout_refused(self):
        def api(method, path, token, data=None):
            if method == "GET" and "/user" in path:
                return {"login": "ripple-bot"}
            if method == "GET" and "ripple-bot" in path:
                return {"error": "not found"}  # never ready
            if method == "POST":
                return {"id": 99}
            return {}

        result = _fork_pr.ensure_fork("org/consumer-api", "tok", api=api, sleep=_no_sleep)
        assert not result.usable
        assert result.created  # fork was requested
        assert any("not usable" in r for r in result.refusals)

    def test_no_login_refused(self):
        api = FakeAPI({"GET /user": {}})
        result = _fork_pr.ensure_fork("org/consumer-api", "tok", api=api, sleep=_no_sleep)
        assert not result.usable
        assert any("UNKNOWN" in r for r in result.refusals)


class TestCanAcceptPullRequests:
    def test_archived_repo_refused(self):
        api = FakeAPI({"GET /repos/org/archived-repo": {"archived": True}})
        ok, reasons = _fork_pr.can_accept_pull_requests("org/archived-repo", "tok", api=api)
        assert not ok
        assert any("archived" in r for r in reasons)

    def test_normal_repo_accepted(self):
        api = FakeAPI({"GET /repos/org/normal-repo": {"archived": False}})
        ok, reasons = _fork_pr.can_accept_pull_requests("org/normal-repo", "tok", api=api)
        assert ok
        assert not reasons


class TestExistingPRForHead:
    def test_no_existing_pr(self):
        api = FakeAPI({"GET /repos/org/repo/pulls": [
            {"head": {"label": "other:branch"}}
        ]})
        assert not _fork_pr.existing_pr_for_head("org/repo", "me:fix", "tok", api=api)

    def test_existing_pr_found(self):
        api = FakeAPI({"GET /repos/org/repo/pulls": [
            {"head": {"label": "me:fix"}}
        ]})
        assert _fork_pr.existing_pr_for_head("org/repo", "me:fix", "tok", api=api)

    def test_failed_read_treated_as_open(self):
        api = FakeAPI({"GET /repos/org/repo/pulls": {"error": "timeout"}})
        assert _fork_pr.existing_pr_for_head("org/repo", "me:fix", "tok", api=api)


class TestOpenForkPR:
    def test_full_success_path(self):
        """Fork exists, branch created, file committed, PR opened."""
        content = base64.b64encode(b"fixed content").decode()
        api = FakeAPI({
            "GET /repos/org/upstream": {"archived": False, "default_branch": "main"},
            "GET /user": {"login": "bot"},
            "GET /repos/bot/upstream": {"id": 1},
            "GET /repos/org/upstream/pulls": [],
            "GET /repos/org/upstream/git/ref/heads/main": {"object": {"sha": "abc123"}},
            "POST /repos/bot/upstream/git/refs": {"ref": "refs/heads/fix"},
            "GET /repos/bot/upstream/contents/src/api.py": {"error": "not found"},
            "PUT /repos/bot/upstream/contents/src/api.py": {"content": {"sha": "new"}},
            "POST /repos/org/upstream/pulls": {"number": 42, "html_url": "https://github.com/org/upstream/pull/42"},
        })
        result = _fork_pr.open_fork_pr(
            "org/upstream", "fix-branch", "fix: drop X", "Body",
            [("src/api.py", content)], "tok", api=api, sleep=_no_sleep)
        assert result.opened
        assert result.number == 42
        assert result.url == "https://github.com/org/upstream/pull/42"

    def test_already_open_skipped(self):
        api = FakeAPI({
            "GET /repos/org/upstream": {"archived": False},
            "GET /user": {"login": "bot"},
            "GET /repos/bot/upstream": {"id": 1},
            "GET /repos/org/upstream/pulls": [{"head": {"label": "bot:fix-branch"}}],
        })
        result = _fork_pr.open_fork_pr(
            "org/upstream", "fix-branch", "fix: drop X", "Body",
            [], "tok", api=api, sleep=_no_sleep)
        assert result.already_open
        assert not result.opened

    def test_archived_refused(self):
        api = FakeAPI({
            "GET /repos/org/archived": {"archived": True},
        })
        result = _fork_pr.open_fork_pr(
            "org/archived", "fix", "title", "body",
            [], "tok", api=api, sleep=_no_sleep)
        assert not result.opened
        assert any("archived" in r for r in result.refusals)


# ─── follow_on unit tests ───────────────────────────────────────────

class TestPlanFollowOns:
    def test_exact_matches_become_proposals(self):
        impact = {"affected": [
            {"repo": "org/consumer", "path": "src/api.py",
             "confidence": "exact", "language": "python"},
        ]}
        plan = _follow_on.plan_follow_ons("getUser", "org/upstream", 1, impact)
        assert len(plan.proposals) == 1
        assert plan.proposals[0].repo == "org/consumer"

    def test_approximate_matches_are_mentioned_only(self):
        impact = {"affected": [
            {"repo": "org/consumer", "path": "src/api.py",
             "confidence": "approximate", "language": "python"},
        ]}
        plan = _follow_on.plan_follow_ons("getUser", "org/upstream", 1, impact)
        assert len(plan.proposals) == 0
        assert len(plan.mentioned_only) == 1

    def test_same_repo_excluded(self):
        impact = {"affected": [
            {"repo": "org/upstream", "path": "src/self.py",
             "confidence": "exact", "language": "python"},
        ]}
        plan = _follow_on.plan_follow_ons("getUser", "org/upstream", 1, impact)
        assert len(plan.proposals) == 0
        assert len(plan.mentioned_only) == 1

    def test_max_repos_capped(self):
        impact = {"affected": [
            {"repo": f"org/consumer-{i}", "path": "src/api.py",
             "confidence": "exact", "language": "python"}
            for i in range(10)
        ]}
        plan = _follow_on.plan_follow_ons("getUser", "org/upstream", 1, impact)
        assert len(plan.proposals) == _follow_on.MAX_FOLLOW_ON_REPOS
        assert plan.truncated_at == _follow_on.MAX_FOLLOW_ON_REPOS

    def test_branch_name_deterministic(self):
        b1 = _follow_on.branch_name("getUser", "org/upstream", 42)
        b2 = _follow_on.branch_name("getUser", "org/upstream", 42)
        assert b1 == b2
        assert b1.startswith("ripple/")


class TestAlreadyOpen:
    def test_no_match(self):
        api = FakeAPI({"GET /repos/org/repo/pulls": [
            {"head": {"ref": "other-branch"}}
        ]})
        proposal = _follow_on.FollowOn(
            repo="org/repo", symbol="x", files=(), branch="ripple/fix", title="", body="")
        assert not _follow_on.already_open(proposal, "tok", api=api)

    def test_match_found(self):
        api = FakeAPI({"GET /repos/org/repo/pulls": [
            {"head": {"ref": "ripple/fix"}}
        ]})
        proposal = _follow_on.FollowOn(
            repo="org/repo", symbol="x", files=(), branch="ripple/fix", title="", body="")
        assert _follow_on.already_open(proposal, "tok", api=api)

    def test_failed_read_conservative(self):
        api = FakeAPI({"GET /repos/org/repo/pulls": {"error": "boom"}})
        proposal = _follow_on.FollowOn(
            repo="org/repo", symbol="x", files=(), branch="ripple/fix", title="", body="")
        assert _follow_on.already_open(proposal, "tok", api=api)
