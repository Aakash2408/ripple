"""
Tests for confidence scoring module.
"""
import sys
sys.path.insert(0, '.')

from app.confidence import (
    classify_confidence,
    format_confidence_table,
    format_pr_body,
    should_create_pr,
    should_add_warning,
    ConfidenceLevel,
    CONFIDENCE_LEVELS,
)


class TestClassifyConfidence:
    """Tests for confidence level classification."""

    def test_high_confidence(self):
        level = classify_confidence(0.90)
        assert level.label == "high"
        assert level.emoji == "🟢"

    def test_high_boundary(self):
        level = classify_confidence(0.75)
        assert level.label == "high"

    def test_medium_confidence(self):
        level = classify_confidence(0.60)
        assert level.label == "medium"
        assert level.emoji == "🟡"

    def test_medium_boundary(self):
        level = classify_confidence(0.50)
        assert level.label == "medium"

    def test_low_confidence(self):
        level = classify_confidence(0.40)
        assert level.label == "low"
        assert level.emoji == "🟠"

    def test_low_boundary(self):
        level = classify_confidence(0.30)
        assert level.label == "low"

    def test_skip_confidence(self):
        level = classify_confidence(0.10)
        assert level.label == "skip"
        assert level.emoji == "⚪"

    def test_zero_confidence(self):
        level = classify_confidence(0.0)
        assert level.label == "skip"

    def test_max_confidence(self):
        level = classify_confidence(1.0)
        assert level.label == "high"


class TestShouldCreatePr:
    """Tests for PR creation decision."""

    def test_high_confidence_creates_pr(self):
        assert should_create_pr(0.85) == True

    def test_medium_confidence_creates_pr(self):
        assert should_create_pr(0.60) == True

    def test_low_confidence_no_pr(self):
        assert should_create_pr(0.40) == False

    def test_custom_threshold(self):
        assert should_create_pr(0.40, min_confidence=0.3) == True
        assert should_create_pr(0.20, min_confidence=0.3) == False

    def test_exact_threshold(self):
        assert should_create_pr(0.50, min_confidence=0.5) == True
        assert should_create_pr(0.49, min_confidence=0.5) == False


class TestShouldAddWarning:
    """Tests for warning decision."""

    def test_high_no_warning(self):
        assert should_add_warning(0.85) == False

    def test_medium_gets_warning(self):
        assert should_add_warning(0.60) == True

    def test_low_no_warning(self):
        # Low doesn't get a warning — it doesn't get a PR at all
        assert should_add_warning(0.35) == False


class TestFormatConfidenceTable:
    """Tests for markdown table generation."""

    def test_empty_predictions(self):
        result = format_confidence_table([])
        assert result == ""

    def test_single_prediction(self):
        predictions = [{
            "file": "client.py",
            "confidence": 0.92,
            "sources": ["grep", "history"],
            "reasons": ["Direct API call found"],
        }]
        result = format_confidence_table(predictions)
        assert "Confidence Report" in result
        assert "client.py" in result
        assert "92%" in result
        assert "🟢" in result
        assert "grep + history" in result

    def test_multiple_predictions(self):
        predictions = [
            {"file": "client.py", "confidence": 0.95, "sources": ["grep", "history", "playbook"], "reasons": ["Direct reference"]},
            {"file": "test_client.py", "confidence": 0.70, "sources": ["playbook"], "reasons": ["Test file predicted"]},
            {"file": "docs/api.md", "confidence": 0.35, "sources": ["grep"], "reasons": ["Mentions endpoint"]},
        ]
        result = format_confidence_table(predictions)
        assert "client.py" in result
        assert "test_client.py" in result
        assert "docs/api.md" in result
        assert "95%" in result
        assert "70%" in result
        assert "35%" in result
        assert "🟢" in result
        assert "🟡" in result
        assert "🟠" in result

    def test_caps_at_10_rows(self):
        predictions = [
            {"file": f"file{i}.py", "confidence": 0.5, "sources": ["grep"], "reasons": ["match"]}
            for i in range(20)
        ]
        result = format_confidence_table(predictions)
        # Should only have 10 data rows (not 20) — count backtick entries (file paths)
        data_rows = [l for l in result.split("\n") if l.startswith("|") and "`" in l]
        assert len(data_rows) <= 10

    def test_long_file_path_truncated(self):
        predictions = [{
            "file": "very/long/path/to/some/deeply/nested/directory/structure/consumer_client.py",
            "confidence": 0.80,
            "sources": ["grep"],
            "reasons": ["Found reference"],
        }]
        result = format_confidence_table(predictions)
        # Should contain truncated path with ...
        assert "..." in result


class TestFormatPrBody:
    """Tests for full PR body generation."""

    def test_basic_pr_body(self):
        body = format_pr_body(
            change_description="Added required field `country` to `POST /users`",
            source_repo="org/api-spec",
            confidence=0.88,
            sources=["grep", "history"],
            reasons=["Direct API endpoint reference found", "Co-changed 8/10 times"],
        )
        assert "🌊 Ripple" in body
        assert "org/api-spec" in body
        assert "88%" in body
        assert "🟢" in body
        assert "high" in body
        assert "grep + history" in body
        assert "Direct API endpoint reference found" in body
        assert "Co-changed 8/10 times" in body
        assert "Auto-generated by" in body

    def test_pr_body_with_other_predictions(self):
        body = format_pr_body(
            change_description="Removed field `age`",
            source_repo="org/schema",
            confidence=0.75,
            sources=["playbook"],
            reasons=["Client SDK predicted"],
            all_predictions=[
                {"file": "current", "confidence": 0.75, "sources": ["playbook"]},
                {"file": "other_client.py", "confidence": 0.60, "sources": ["grep"]},
                {"file": "docs.md", "confidence": 0.35, "sources": ["grep"]},
            ],
        )
        assert "Other consumers detected" in body
        assert "other_client.py" in body
        assert "docs.md" in body

    def test_pr_body_medium_confidence(self):
        body = format_pr_body(
            change_description="Type changed",
            source_repo="org/proto",
            confidence=0.55,
            sources=["grep"],
            reasons=["Mentions endpoint"],
        )
        assert "55%" in body
        assert "🟡" in body
        assert "medium" in body

    def test_pr_body_no_predictions(self):
        body = format_pr_body(
            change_description="Field removed",
            source_repo="org/spec",
            confidence=0.90,
            sources=["grep", "history", "playbook"],
            reasons=["Strong match"],
            all_predictions=None,
        )
        assert "Other consumers detected" not in body
        assert "🌊 Ripple" in body


class TestConfidenceLevels:
    """Tests for confidence level configuration."""

    def test_levels_are_ordered(self):
        """Thresholds should be in descending order."""
        thresholds = [l.threshold for l in CONFIDENCE_LEVELS]
        assert thresholds == sorted(thresholds, reverse=True)

    def test_all_levels_have_required_fields(self):
        for level in CONFIDENCE_LEVELS:
            assert level.label != ""
            assert level.emoji != ""
            assert level.action != ""
            assert 0.0 <= level.threshold <= 1.0


# === Run all tests ===

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestClassifyConfidence,
        TestShouldCreatePr,
        TestShouldAddWarning,
        TestFormatConfidenceTable,
        TestFormatPrBody,
        TestConfidenceLevels,
    ]

    total = 0
    passed = 0
    failed = 0

    for cls in test_classes:
        print(f"\n{'='*60}")
        print(f"  {cls.__name__}")
        print(f"{'='*60}")

        instance = cls()

        for method_name in sorted(dir(instance)):
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
        import sys
        sys.exit(1)
    else:
        print("\n🎉 All confidence tests passed!")
