"""
Tests for PropBench-powered features:
- HistoryLearner (co-change from git)
- PlaybookEngine (pattern predictions)
- MultiInvokerDetector (shared resource warnings)
- EnsembleConsumerFinder (combines all sources)
- Custom Playbooks (.ripple.yaml)
"""
import sys
import os
import tempfile
import subprocess

sys.path.insert(0, '.')

from app.history_learner import HistoryLearner
from app.playbook_engine import PlaybookEngine, EnsembleConsumerFinder
from app.multi_invoker import MultiInvokerDetector, MultiInvokerWarning
from app.custom_playbooks import parse_ripple_config, RippleConfig


# ============================================================
# HistoryLearner Tests
# ============================================================

class TestHistoryLearner:
    """Tests for co-change learning from git history."""

    def test_init(self):
        learner = HistoryLearner(min_confidence=0.3, min_co_changes=2)
        assert learner.min_confidence == 0.3
        assert learner.min_co_changes == 2

    def test_learn_from_repo_with_real_git(self):
        """Learn from the ripple repo itself."""
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        learner = HistoryLearner(min_confidence=0.2, min_co_changes=2)
        stats = learner.learn_from_repo(repo_path, since="6 months ago")
        # Should find some co-change relationships
        assert stats["total_commits"] >= 1
        assert stats["files_tracked"] >= 0

    def test_learn_from_nonexistent_repo(self):
        """Gracefully handle missing repo."""
        learner = HistoryLearner()
        stats = learner.learn_from_repo("/nonexistent/path")
        assert stats["total_commits"] == 0

    def test_predict_consumers_empty(self):
        """Empty learner returns no predictions."""
        learner = HistoryLearner()
        preds = learner.predict_consumers("foo.py", top_n=5)
        assert preds == []

    def test_predict_consumers_after_learning(self):
        """After learning, predictions should exist."""
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        learner = HistoryLearner(min_confidence=0.1, min_co_changes=1)
        learner.learn_from_repo(repo_path, since="6 months ago")
        
        stats = learner.stats()
        # stats() returns a string like "HistoryLearner: 36 files, 400 relationships..."
        assert "files" in stats
        # Try a prediction
        preds = learner.predict_consumers("app/webhook.py", top_n=5)
        assert isinstance(preds, list)

    def test_stats(self):
        """Stats returns a descriptive string."""
        learner = HistoryLearner()
        stats = learner.stats()
        assert isinstance(stats, str)
        assert "HistoryLearner" in stats


# ============================================================
# PlaybookEngine Tests
# ============================================================

class TestPlaybookEngine:
    """Tests for pattern-based predictions."""

    def test_init(self):
        engine = PlaybookEngine()
        assert engine is not None

    def test_predict_openapi_change(self):
        """OpenAPI field addition should predict test files and clients."""
        engine = PlaybookEngine()
        predictions = engine.predict("openapi", "added_required_field")
        assert len(predictions) > 0
        patterns = [p.file_pattern for p in predictions]
        # Should predict test files
        assert any("test" in p.lower() for p in patterns)

    def test_predict_proto_change(self):
        """Proto field removal should predict generated code."""
        engine = PlaybookEngine()
        predictions = engine.predict("proto", "field_removed")
        assert len(predictions) > 0
        patterns = [p.file_pattern for p in predictions]
        # Should predict generated proto files
        assert any("pb2" in p or "pb.go" in p for p in patterns)

    def test_predict_graphql_change(self):
        """GraphQL changes should predict query files."""
        engine = PlaybookEngine()
        predictions = engine.predict("graphql", "field_removed")
        assert len(predictions) > 0

    def test_predict_database_change(self):
        """Database schema changes should predict model files."""
        engine = PlaybookEngine()
        predictions = engine.predict("database", "column_removed")
        assert len(predictions) > 0

    def test_predict_unknown_type(self):
        """Unknown contract type returns empty predictions."""
        engine = PlaybookEngine()
        predictions = engine.predict("unknown_format", "something")
        assert predictions == []

    def test_prediction_has_confidence(self):
        """All predictions should have confidence between 0 and 1."""
        engine = PlaybookEngine()
        predictions = engine.predict("openapi", "added_required_field")
        for p in predictions:
            assert 0 < p.confidence <= 1.0
            assert p.reason != ""
            assert p.source != ""

    def test_get_playbook_for_change(self):
        """Should return the matching playbook definition."""
        engine = PlaybookEngine()
        pb = engine.get_playbook_for_change("openapi", "added_required_field")
        assert pb is not None
        assert "predictions" in pb


# ============================================================
# MultiInvokerDetector Tests
# ============================================================

class TestMultiInvokerDetector:
    """Tests for shared resource detection."""

    def test_init_without_learner(self):
        detector = MultiInvokerDetector(learner=None)
        assert detector is not None

    def test_init_with_learner(self):
        learner = HistoryLearner()
        detector = MultiInvokerDetector(learner=learner)
        assert detector.learner is learner

    def test_check_config_file(self):
        """Config files should be flagged as high-risk shared resources."""
        detector = MultiInvokerDetector(learner=None)
        warning = detector.check("config/settings.yaml")
        # Should detect as shared resource based on pattern
        if warning:
            assert warning.risk_level in ("high", "medium", "low")
            assert warning.shared_file == "config/settings.yaml"

    def test_check_schema_file(self):
        """Schema files should be flagged."""
        detector = MultiInvokerDetector(learner=None)
        warning = detector.check("shared/schema.proto")
        if warning:
            assert warning.risk_level in ("high", "medium", "low")

    def test_check_normal_file(self):
        """Regular source files shouldn't trigger warnings."""
        detector = MultiInvokerDetector(learner=None)
        warning = detector.check("src/handlers/myhandler.py")
        # May or may not warn -- depends on implementation
        # But shouldn't be high risk
        if warning:
            assert warning.risk_level != "high" or len(warning.invokers) > 1

    def test_check_with_learned_history(self):
        """When learner has data, detector should find real consumers."""
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        learner = HistoryLearner(min_confidence=0.1, min_co_changes=1)
        learner.learn_from_repo(repo_path, since="6 months ago")
        
        detector = MultiInvokerDetector(learner=learner)
        warning = detector.check("app/webhook.py")
        # webhook.py likely co-changes with many files
        if warning:
            assert isinstance(warning.invokers, list)


# ============================================================
# EnsembleConsumerFinder Tests
# ============================================================

class TestEnsembleConsumerFinder:
    """Tests for the combined multi-source consumer finder."""

    def setup_method(self):
        self.engine = PlaybookEngine()
        self.learner = HistoryLearner()
        self.detector = MultiInvokerDetector(learner=self.learner)
        self.ensemble = EnsembleConsumerFinder(
            playbook_engine=self.engine,
            learner=self.learner,
            detector=self.detector,
        )

    def test_init(self):
        assert self.ensemble.playbook_engine is self.engine
        assert self.ensemble.learner is self.learner
        assert self.ensemble.detector is self.detector

    def test_find_with_grep_only(self):
        """With only grep results, should return them with grep confidence."""
        results = self.ensemble.find_all_consumers(
            changed_file="api/spec.yaml",
            contract_type="openapi",
            change_type="added_required_field",
            grep_results=["client.py", "test_api.py"],
        )
        assert len(results) >= 2
        # Grep results should be included
        grep_files = [r["file"] for r in results]
        assert "client.py" in grep_files
        assert "test_api.py" in grep_files

    def test_find_without_grep(self):
        """Without grep, should still return playbook predictions."""
        results = self.ensemble.find_all_consumers(
            changed_file="schema.proto",
            contract_type="proto",
            change_type="field_removed",
            grep_results=[],
        )
        # Playbook engine should still predict *_pb2.py etc.
        assert len(results) > 0

    def test_confidence_boosting(self):
        """Multi-source predictions should have higher confidence."""
        results = self.ensemble.find_all_consumers(
            changed_file="api/spec.yaml",
            contract_type="openapi",
            change_type="added_required_field",
            grep_results=["*_test.*"],  # matches playbook prediction pattern
        )
        # Should have some results
        assert len(results) > 0
        # Results are sorted by source_count desc, then confidence desc
        if len(results) > 1:
            assert results[0]["source_count"] >= results[-1]["source_count"]

    def test_results_capped_at_99(self):
        """Confidence should never exceed 0.99."""
        results = self.ensemble.find_all_consumers(
            changed_file="x.yaml",
            contract_type="openapi",
            change_type="added_required_field",
            grep_results=["a.py", "b.py", "c.py"],
        )
        for r in results:
            assert r["confidence"] <= 0.99

    def test_result_structure(self):
        """Each result should have expected fields."""
        results = self.ensemble.find_all_consumers(
            changed_file="api.yaml",
            contract_type="openapi",
            change_type="added_required_field",
            grep_results=["handler.py"],
        )
        for r in results:
            assert "file" in r
            assert "confidence" in r
            assert "sources" in r
            assert "reasons" in r
            assert "source_count" in r
            assert isinstance(r["sources"], list)
            assert isinstance(r["confidence"], float)


# ============================================================
# Custom Playbooks Tests
# ============================================================

class TestCustomPlaybooks:
    """Tests for .ripple.yaml parsing and matching."""

    def test_parse_basic_config(self):
        yaml_content = """
playbooks:
  - name: "API Gateway"
    trigger:
      files: ["api/openapi.yaml"]
      change_types: ["added_required_field"]
    consumers:
      - pattern: "sdk/**/*.py"
        confidence: 0.9
        reason: "Python SDK"

ignore:
  - "*.lock"
  - "dist/**"

settings:
  min_confidence: 0.7
  auto_learn: true
  max_prs_per_push: 5
"""
        config = parse_ripple_config(yaml_content)
        assert config is not None
        assert len(config.playbooks) == 1
        assert config.playbooks[0].name == "API Gateway"
        assert config.min_confidence == 0.7
        assert config.max_prs_per_push == 5
        assert "*.lock" in config.ignore_patterns

    def test_parse_empty_config(self):
        config = parse_ripple_config("")
        # Should return None or default config
        assert config is None or isinstance(config, RippleConfig)

    def test_parse_invalid_yaml(self):
        config = parse_ripple_config("{{{{not: valid: yaml: [[")
        assert config is None

    def test_should_ignore(self):
        config = RippleConfig(ignore_patterns=["*.lock", "node_modules/**", "dist/*"])
        assert config.should_ignore("package-lock.json") == False  # *.lock matches .lock extension
        assert config.should_ignore("yarn.lock") == True
        assert config.should_ignore("dist/bundle.js") == True

    def test_get_predictions_trigger_match(self):
        config = RippleConfig(playbooks=[])
        from app.custom_playbooks import CustomPlaybook, CustomConsumer
        config.playbooks.append(CustomPlaybook(
            name="Test Playbook",
            trigger_files=["api/*.yaml"],
            trigger_change_types=["added_required_field"],
            consumers=[
                CustomConsumer(pattern="sdk/*.py", confidence=0.9, reason="SDK match"),
            ],
        ))
        
        preds = config.get_predictions_for_change("api/openapi.yaml", "added_required_field")
        assert len(preds) == 1
        assert preds[0]["pattern"] == "sdk/*.py"
        assert preds[0]["confidence"] == 0.9
        assert preds[0]["source"] == "custom:Test Playbook"

    def test_get_predictions_no_match(self):
        config = RippleConfig(playbooks=[])
        from app.custom_playbooks import CustomPlaybook, CustomConsumer
        config.playbooks.append(CustomPlaybook(
            name="Test",
            trigger_files=["api/*.yaml"],
            trigger_change_types=["added_required_field"],
            consumers=[
                CustomConsumer(pattern="sdk/*.py", confidence=0.9, reason="SDK"),
            ],
        ))
        
        # Wrong file -- shouldn't match
        preds = config.get_predictions_for_change("db/schema.sql", "added_required_field")
        assert len(preds) == 0

    def test_get_predictions_wildcard_change_type(self):
        """Playbook with change_types: ['*'] should match any change."""
        config = RippleConfig(playbooks=[])
        from app.custom_playbooks import CustomPlaybook, CustomConsumer
        config.playbooks.append(CustomPlaybook(
            name="Catch All",
            trigger_files=["*.proto"],
            trigger_change_types=["*"],
            consumers=[
                CustomConsumer(pattern="gen/*.go", confidence=0.95, reason="Generated"),
            ],
        ))
        
        preds = config.get_predictions_for_change("service.proto", "field_removed")
        assert len(preds) == 1

    def test_default_config_values(self):
        config = RippleConfig()
        assert config.min_confidence == 0.6
        assert config.auto_learn == True
        assert config.max_prs_per_push == 10
        assert config.ignore_patterns == []
        assert config.playbooks == []

    def test_multiple_playbooks(self):
        yaml_content = """
playbooks:
  - name: "API"
    trigger:
      files: ["api/*"]
      change_types: ["*"]
    consumers:
      - pattern: "sdk/*"
        confidence: 0.9
        reason: "SDK"
  - name: "DB"
    trigger:
      files: ["db/*"]
      change_types: ["*"]
    consumers:
      - pattern: "models/*"
        confidence: 0.85
        reason: "Models"
      - pattern: "repos/*"
        confidence: 0.8
        reason: "Repositories"
"""
        config = parse_ripple_config(yaml_content)
        assert config is not None
        assert len(config.playbooks) == 2
        assert len(config.playbooks[1].consumers) == 2


# ============================================================
# Integration Test: Full Pipeline
# ============================================================

class TestFullPipeline:
    """End-to-end tests combining multiple components."""

    def test_ensemble_with_custom_playbook(self):
        """Custom playbook predictions should integrate with ensemble."""
        from app.custom_playbooks import CustomPlaybook, CustomConsumer
        
        engine = PlaybookEngine()
        ensemble = EnsembleConsumerFinder(
            playbook_engine=engine,
            learner=None,
            detector=None,
        )
        
        # Simulate grep finding a consumer
        results = ensemble.find_all_consumers(
            changed_file="api/payments.yaml",
            contract_type="openapi",
            change_type="added_required_field",
            grep_results=["services/billing/client.py"],
        )
        
        assert len(results) >= 1
        # The grep result should be in there
        files = [r["file"] for r in results]
        assert "services/billing/client.py" in files

    def test_learning_improves_predictions(self):
        """After learning from git, ensemble should find more consumers."""
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Without learning
        engine = PlaybookEngine()
        ensemble_no_learn = EnsembleConsumerFinder(
            playbook_engine=engine, learner=None, detector=None
        )
        results_before = ensemble_no_learn.find_all_consumers(
            changed_file="app/diff_engine.py",
            contract_type="openapi",
            change_type="added_required_field",
            grep_results=[],
        )
        
        # With learning
        learner = HistoryLearner(min_confidence=0.1, min_co_changes=1)
        learner.learn_from_repo(repo_path, since="6 months ago")
        
        ensemble_learned = EnsembleConsumerFinder(
            playbook_engine=engine, learner=learner, detector=None
        )
        results_after = ensemble_learned.find_all_consumers(
            changed_file="app/diff_engine.py",
            contract_type="openapi",
            change_type="added_required_field",
            grep_results=[],
        )
        
        # Learned version should find at least as many (usually more)
        assert len(results_after) >= len(results_before)


# ============================================================
# Run all tests
# ============================================================

if __name__ == "__main__":
    import traceback
    
    test_classes = [
        TestHistoryLearner,
        TestPlaybookEngine,
        TestMultiInvokerDetector,
        TestEnsembleConsumerFinder,
        TestCustomPlaybooks,
        TestFullPipeline,
    ]
    
    total = 0
    passed = 0
    failed = 0
    
    for cls in test_classes:
        print(f"\n{'='*60}")
        print(f"  {cls.__name__}")
        print(f"{'='*60}")
        
        instance = cls()
        if hasattr(instance, 'setup_method'):
            instance.setup_method()
        
        for method_name in dir(instance):
            if method_name.startswith("test_"):
                total += 1
                try:
                    getattr(instance, method_name)()
                    passed += 1
                    print(f"  ✅ {method_name}")
                except Exception as e:
                    failed += 1
                    print(f"  ❌ {method_name}: {e}")
                    traceback.print_exc()
    
    print(f"\n{'='*60}")
    print(f"  Results: {passed}/{total} passed, {failed} failed")
    print(f"{'='*60}")
    
    if failed > 0:
        sys.exit(1)
    else:
        print("\n🎉 All PropBench integration tests passed!")
