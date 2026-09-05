"""
Tests for the polling endpoint (Phase 2: autonomous agent support).

Tests poll_store (SQLite persistence) and poll_endpoint (spec change detection).
"""
import sys
import os
import tempfile

sys.path.insert(0, '.')

from app import poll_store


# ─── poll_store tests ────────────────────────────────────────────────

class TestPollStore:
    """SQLite-backed poll state persistence."""

    def setup_method(self):
        """Use a temp DB for each test."""
        self._orig = poll_store._DB_PATH
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        poll_store._DB_PATH = type(poll_store._DB_PATH)(path)

    def teardown_method(self):
        try:
            os.unlink(str(poll_store._DB_PATH))
        except OSError:
            pass
        poll_store._DB_PATH = self._orig

    def test_get_empty(self):
        assert poll_store.get_last_sha("org/repo") == ""

    def test_set_and_get(self):
        poll_store.set_last_sha("org/repo", "abc123", "clean")
        assert poll_store.get_last_sha("org/repo") == "abc123"

    def test_update_overwrites(self):
        poll_store.set_last_sha("org/repo", "aaa", "first")
        poll_store.set_last_sha("org/repo", "bbb", "second")
        assert poll_store.get_last_sha("org/repo") == "bbb"

    def test_multiple_repos(self):
        poll_store.set_last_sha("org/a", "sha1")
        poll_store.set_last_sha("org/b", "sha2")
        assert poll_store.get_last_sha("org/a") == "sha1"
        assert poll_store.get_last_sha("org/b") == "sha2"

    def test_get_all_watched(self):
        poll_store.set_last_sha("org/a", "sha1")
        poll_store.set_last_sha("org/b", "sha2")
        watched = poll_store.get_all_watched()
        assert len(watched) == 2
        repos = {w["repo"] for w in watched}
        assert repos == {"org/a", "org/b"}

    def test_remove_repo(self):
        poll_store.set_last_sha("org/a", "sha1")
        assert poll_store.remove_repo("org/a")
        assert poll_store.get_last_sha("org/a") == ""

    def test_remove_nonexistent(self):
        assert not poll_store.remove_repo("org/nope")

    def test_clear_all(self):
        poll_store.set_last_sha("org/a", "sha1")
        poll_store.set_last_sha("org/b", "sha2")
        poll_store.clear_all()
        assert poll_store.get_all_watched() == []


# ─── poll_endpoint unit tests ────────────────────────────────────────

from app.poll_endpoint import (
    _is_spec_file,
    _detect_contract_type,
)


class TestSpecDetection:
    """Spec file detection and contract type inference."""

    def test_openapi_yaml(self):
        assert _is_spec_file("api/openapi.yaml")
        assert _is_spec_file("api-spec.yml")
        assert _is_spec_file("swagger.json")
        assert _is_spec_file("docs/api-users.yaml")

    def test_proto(self):
        assert _is_spec_file("service.proto")
        assert _is_spec_file("api/v1/user.proto")

    def test_graphql(self):
        assert _is_spec_file("schema.graphql")
        assert _is_spec_file("api.gql")

    def test_asyncapi(self):
        assert _is_spec_file("asyncapi.yaml")

    def test_avro(self):
        assert _is_spec_file("user.avsc")
        assert _is_spec_file("avro-schema.json")

    def test_thrift(self):
        assert _is_spec_file("service.thrift")

    def test_smithy(self):
        assert _is_spec_file("model.smithy")

    def test_non_spec(self):
        assert not _is_spec_file("main.py")
        assert not _is_spec_file("README.md")
        assert not _is_spec_file("package.json")
        assert not _is_spec_file("config.yaml")

    def test_contract_type_inference(self):
        assert _detect_contract_type("service.proto") == "protobuf"
        assert _detect_contract_type("schema.graphql") == "graphql"
        assert _detect_contract_type("asyncapi.yaml") == "asyncapi"
        assert _detect_contract_type("user.avsc") == "avro"
        assert _detect_contract_type("api.thrift") == "thrift"
        assert _detect_contract_type("model.smithy") == "smithy"
        assert _detect_contract_type("openapi.yaml") == "openapi"


class TestPollOneRepo:
    """_poll_one_repo with mocked GitHub API."""

    def setup_method(self):
        self._orig = poll_store._DB_PATH
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        poll_store._DB_PATH = type(poll_store._DB_PATH)(path)

    def teardown_method(self):
        try:
            os.unlink(str(poll_store._DB_PATH))
        except OSError:
            pass
        poll_store._DB_PATH = self._orig

    def test_unchanged_repo(self):
        """When HEAD matches stored SHA, status is 'unchanged'."""
        from unittest.mock import patch
        from app.poll_endpoint import _poll_one_repo

        poll_store.set_last_sha("org/demo", "abc123")

        with patch("app.poll_endpoint._get_head_sha", return_value="abc123"):
            result = _poll_one_repo("org/demo", "fake-token", dry_run=True)

        assert result.status == "unchanged"
        assert result.current_sha == "abc123"

    def test_changed_no_specs(self):
        """New commits but no spec files changed."""
        from unittest.mock import patch
        from app.poll_endpoint import _poll_one_repo

        poll_store.set_last_sha("org/demo", "old_sha")

        with patch("app.poll_endpoint._get_head_sha", return_value="new_sha"), \
             patch("app.poll_endpoint._get_changed_files",
                   return_value=["src/main.py", "README.md"]):
            result = _poll_one_repo("org/demo", "fake-token", dry_run=True)

        assert result.status == "no_specs"
        # SHA should be updated even when no specs changed
        assert poll_store.get_last_sha("org/demo") == "new_sha"

    def test_changed_with_specs(self):
        """New commits with spec file changes — triggers analysis."""
        from unittest.mock import patch
        from app.poll_endpoint import _poll_one_repo

        poll_store.set_last_sha("org/demo", "old_sha")

        # Minimal OpenAPI specs
        old_spec = '{"openapi": "3.0.0", "paths": {"/users": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}}}}}}}}}'
        new_spec = '{"openapi": "3.0.0", "paths": {"/users": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}}}}}}}}}'

        with patch("app.poll_endpoint._get_head_sha", return_value="new_sha"), \
             patch("app.poll_endpoint._get_changed_files",
                   return_value=["api/openapi.json", "src/main.py"]), \
             patch("app.poll_endpoint._fetch_file",
                   side_effect=lambda repo, path, ref, token:
                       old_spec if ref == "old_sha" else new_spec):
            result = _poll_one_repo("org/demo", "fake-token", dry_run=True)

        assert result.status == "analysed"
        assert result.changed_specs == ["api/openapi.json"]
        assert len(result.breaking_changes) > 0
        assert poll_store.get_last_sha("org/demo") == "new_sha"

    def test_unreachable_repo(self):
        """Repo that can't be reached returns error."""
        from unittest.mock import patch
        from app.poll_endpoint import _poll_one_repo

        with patch("app.poll_endpoint._get_head_sha", return_value=""):
            result = _poll_one_repo("org/nope", "fake-token", dry_run=True)

        assert result.status == "error"
        assert "could not resolve" in result.error

    def test_first_poll_no_stored_sha(self):
        """First time polling a repo — no stored SHA."""
        from unittest.mock import patch
        from app.poll_endpoint import _poll_one_repo

        with patch("app.poll_endpoint._get_head_sha", return_value="first_sha"), \
             patch("app.poll_endpoint._get_changed_files",
                   return_value=["README.md"]):
            result = _poll_one_repo("org/new", "fake-token", dry_run=True)

        assert result.previous_sha == ""
        assert result.current_sha == "first_sha"
        assert result.status == "no_specs"
        assert poll_store.get_last_sha("org/new") == "first_sha"
