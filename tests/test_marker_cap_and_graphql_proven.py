"""Tests for marker-noise cap and proven GraphQL fragment removal.

Task 1: When >5 sites match, only 5 individual markers are inserted and a
summary is placed at the file top. The returned count stays accurate so the
explanation is truthful.

Task 2: _remove_field_graphql only removes selections whose enclosing type is
provable from the document (fragment type condition, inline fragment). Selections
inside a field's sub-selection are left alone.
"""
from __future__ import annotations
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Task 1: marker cap ────────────────────────────────────────────────────

def test_marker_cap_limits_individual_markers_to_5():
    """20 matching lines produce 1 summary + 5 individual markers, not 20."""
    from app.fix_templates import (
        _annotate_sites, _symbol_names, name_variants, MARKER)
    code = "\n".join(f"const s{i} = status + {i};" for i in range(20))
    variants = name_variants("status")
    result, sites = _annotate_sites(
        code, _symbol_names(variants), "typescript", "test note")
    lines = result.splitlines()
    marker_lines = [ln for ln in lines if MARKER in ln]
    # 1 summary + 5 individual = 6 marker lines
    assert len(marker_lines) == 6, f"expected 6 marker lines, got {len(marker_lines)}"
    # returned total is the TRUE count, not the capped count
    assert sites == 20, f"expected 20 sites, got {sites}"
    # summary mentions the total and the cap
    assert "20 sites" in marker_lines[0], marker_lines[0]
    assert "first 5" in marker_lines[0], marker_lines[0]


def test_small_file_gets_no_summary():
    """3 matching lines get 3 individual markers, no summary."""
    from app.fix_templates import (
        _annotate_sites, _symbol_names, name_variants, MARKER)
    code = "\n".join(f"const s{i} = status + {i};" for i in range(3))
    variants = name_variants("status")
    result, sites = _annotate_sites(
        code, _symbol_names(variants), "typescript", "test note")
    marker_lines = [ln for ln in result.splitlines() if MARKER in ln]
    assert len(marker_lines) == 3
    assert sites == 3
    # no summary line
    assert "sites in this file" not in result


def test_exactly_5_gets_no_summary():
    """5 matching lines get 5 individual markers, no summary."""
    from app.fix_templates import (
        _annotate_sites, _symbol_names, name_variants, MARKER)
    code = "\n".join(f"const s{i} = status + {i};" for i in range(5))
    variants = name_variants("status")
    result, sites = _annotate_sites(
        code, _symbol_names(variants), "typescript", "test note")
    marker_lines = [ln for ln in result.splitlines() if MARKER in ln]
    assert len(marker_lines) == 5
    assert sites == 5
    assert "sites in this file" not in result


def test_6_gets_summary_plus_5():
    """6 matching lines trigger the cap: 1 summary + 5 individual."""
    from app.fix_templates import (
        _annotate_sites, _symbol_names, name_variants, MARKER)
    code = "\n".join(f"const s{i} = status + {i};" for i in range(6))
    variants = name_variants("status")
    result, sites = _annotate_sites(
        code, _symbol_names(variants), "typescript", "test note")
    marker_lines = [ln for ln in result.splitlines() if MARKER in ln]
    assert len(marker_lines) == 6, f"expected 6, got {len(marker_lines)}"
    assert sites == 6
    assert "6 sites" in marker_lines[0]


def test_annotate_references_respects_cap():
    """annotate_references (used by rename, type-change, bracket-refusal)
    also respects the marker cap."""
    from app.fix_templates import annotate_references, MARKER
    code = "\n".join(f"use_target({i});" for i in range(15))
    result, _expl = annotate_references(
        code, "typescript", "target", "renamed to something")
    marker_lines = [ln for ln in result.splitlines() if MARKER in ln]
    # 1 summary + 5 individual = 6
    assert len(marker_lines) == 6, f"expected 6, got {len(marker_lines)}"


def test_original_lines_preserved_under_cap():
    """Every original line appears exactly once in the output."""
    from app.fix_templates import (
        _annotate_sites, _symbol_names, name_variants)
    code = "\n".join(f"const s{i} = status + {i};" for i in range(20))
    variants = name_variants("status")
    result, _ = _annotate_sites(
        code, _symbol_names(variants), "typescript", "test note")
    for i in range(20):
        assert f"const s{i} = status + {i};" in result


# ─── Task 2: proven GraphQL fragment removal ────────────────────────────────

def test_fragment_on_type_removes_proven_selection():
    """email is removed from 'fragment F on User { id email }'."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = "fragment F on User {\n  id\n  email\n  phone\n}\n"
    result = _remove_field_graphql(gql, name_variants("email"))
    assert "email" not in result
    assert "id" in result
    assert "phone" in result


def test_inline_fragment_removes_proven_selection():
    """email is removed from '... on User { id email }'."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = ("query {\n  node {\n    ... on User {\n      id\n      email\n"
           "    }\n  }\n}\n")
    result = _remove_field_graphql(gql, name_variants("email"))
    assert "email" not in result
    assert "id" in result


def test_sub_selection_is_left_alone():
    """email inside 'user { email }' is NOT removed (type unproven)."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = "query {\n  user {\n    id\n    email\n    name\n  }\n}\n"
    result = _remove_field_graphql(gql, name_variants("email"))
    assert result == gql, "sub-selection email should not be removed"


def test_mixed_proven_and_unproven():
    """Proven selections are removed, unproven are left."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = ("fragment F on User {\n  id\n  email\n  orders {\n"
           "    email\n    total\n  }\n}\n")
    result = _remove_field_graphql(gql, name_variants("email"))
    lines = result.splitlines()
    # The fragment-level email is gone
    fragment_level = [ln for ln in lines
                      if "email" in ln and "orders" not in ln
                      and ln.strip() != "" and not ln.strip().startswith("#")]
    # Only the sub-selection email should remain
    assert len([ln for ln in lines if "email" in ln.strip()
                and "orders" not in ln]) == 1, (
        f"expected exactly 1 email (in sub-selection), got:\n{result}")
    # orders block is intact
    assert "orders" in result
    assert "total" in result


def test_multiple_fragments_on_different_types():
    """email is removed from both fragments when both have it."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = ("fragment A on User {\n  id\n  email\n}\n\n"
           "fragment B on Admin {\n  id\n  email\n  role\n}\n")
    result = _remove_field_graphql(gql, name_variants("email"))
    assert "email" not in result
    assert "role" in result
    assert "id" in result


def test_empty_selection_set_refuses_removal():
    """Removing the only field would leave '{ }', so the removal is refused."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = "fragment F on User { email }\n"
    result = _remove_field_graphql(gql, name_variants("email"))
    # Should be unchanged: removing email leaves empty selection set
    assert result == gql, f"expected unchanged, got: {result}"


def test_field_with_sub_selection_is_removed_entirely():
    """A proven field with its own sub-selection is fully removed."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = ("fragment F on User {\n  id\n  address {\n    city\n"
           "    zip\n  }\n  name\n}\n")
    result = _remove_field_graphql(gql, name_variants("address"))
    assert "address" not in result
    assert "city" not in result
    assert "zip" not in result
    assert "id" in result
    assert "name" in result


def test_alias_is_removed_with_the_selection():
    """'myEmail: email' in a fragment is fully removed."""
    from app.fix_templates import _remove_field_graphql, name_variants
    gql = "fragment F on User {\n  id\n  myEmail: email\n  name\n}\n"
    result = _remove_field_graphql(gql, name_variants("email"))
    assert "email" not in result
    assert "myEmail" not in result
    assert "id" in result
    assert "name" in result


def test_codemod_result_reports_unproven_count():
    """When some selections are unproven, the codemod result notes it."""
    from app.fix_templates import (_remove_field_graphql, name_variants,
                                    _LAST_CODEMOD_RESULT)
    gql = ("fragment F on User {\n  id\n  email\n  orders {\n"
           "    email\n  }\n}\n")
    _remove_field_graphql(gql, name_variants("email"))
    notes = _LAST_CODEMOD_RESULT.get("notes", [])
    assert any("1 selection" in n and "sub-selection" in n for n in notes), (
        f"expected a note about 1 unproven selection, got: {notes}")


# ─── Mutation targets ──────────────────────────────────────────────────────

def test_cap_value_is_5_not_higher():
    """Verifying the cap is exactly 5, not some other constant."""
    from app.fix_templates import (
        _annotate_sites, _symbol_names, name_variants, MARKER)
    code = "\n".join(f"x.status = {i};" for i in range(10))
    variants = name_variants("status")
    result, _ = _annotate_sites(
        code, _symbol_names(variants), "typescript", "note")
    # Count individual markers (exclude summary)
    lines = result.splitlines()
    individual = [ln for ln in lines
                  if MARKER in ln and "sites in this file" not in ln]
    assert len(individual) == 5, f"expected 5 individual markers, got {len(individual)}"


def test_proven_removal_uses_typed_selections_not_all():
    """Regression: the old code used graphql_selections (all), the new code
    uses graphql_typed_selections (only proven). A sub-selection field must
    survive."""
    from app.fix_templates import _remove_field_graphql, name_variants
    # 'status' appears at fragment level (proven on Order) AND inside a
    # sub-selection (unproven -- could be any type).
    gql = ("fragment F on Order {\n  id\n  status\n  items {\n"
           "    status\n    price\n  }\n}\n")
    result = _remove_field_graphql(gql, name_variants("status"))
    # Fragment-level status is gone
    frag_lines = [ln for ln in result.splitlines()
                  if ln.strip() == "status"]
    assert len(frag_lines) == 1, (
        f"expected exactly 1 'status' (in sub-selection), found "
        f"{len(frag_lines)}:\n{result}")


if __name__ == "__main__":
    from _runner import run
    sys.exit(run(globals()))
