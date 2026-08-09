"""Pure contracts for the reviewed main-agent visual audit."""

from __future__ import annotations

from typing import Any


CURRENT_VISUAL_ARTIFACT_FIELDS = (
    "build_spec_snapshot",
    "build_report",
    "current_pptx",
    "source",
    "preview",
    "structure_validation",
    "background_contract",
)
VISUAL_REVIEW_COVERAGE_FIELDS = frozenset(
    {
        "canvas_and_regions",
        "objects_and_geometry",
        "text_and_typography",
        "tables_and_matrices",
        "graphics_connectors_charts",
        "pictures_crop_layers",
        "high_risk_regions",
    }
)
VISUAL_REVIEW_SUCCESS_RESULTS = frozenset({"checked", "not_applicable"})
VISUAL_REVIEW_RECORD_FIELDS = frozenset(
    {
        "mode",
        "decision",
        "coverage",
        "repair_applied",
        "post_repair_verification",
    }
)


def visual_review_record_issues(
    value: Any,
    *,
    required_coverage: frozenset[str],
) -> list[dict[str, str]]:
    """Return stable validation issues for a successful visual audit record."""

    issues: list[dict[str, str]] = []
    if not isinstance(value, dict) or set(value) != VISUAL_REVIEW_RECORD_FIELDS:
        return [
            {
                "code": "VISUAL_REVIEW_RECORD_INVALID",
                "path": "visual_gate.review",
                "detail": "visual review must contain exactly the five success fields",
            }
        ]
    if value.get("mode") != "main_agent_read_only_visual_audit":
        issues.append(
            {
                "code": "VISUAL_REVIEW_RECORD_INVALID",
                "path": "visual_gate.review.mode",
                "detail": "mode must be main_agent_read_only_visual_audit",
            }
        )
    if value.get("decision") != "passed":
        issues.append(
            {
                "code": "VISUAL_REVIEW_RECORD_INVALID",
                "path": "visual_gate.review.decision",
                "detail": "reviewed success requires decision passed",
            }
        )
    coverage = value.get("coverage")
    coverage_valid = (
        isinstance(coverage, dict)
        and set(coverage) == VISUAL_REVIEW_COVERAGE_FIELDS
        and all(
            isinstance(result, str) and result in VISUAL_REVIEW_SUCCESS_RESULTS
            for result in coverage.values()
        )
    )
    if not coverage_valid:
        issues.append(
            {
                "code": "VISUAL_REVIEW_COVERAGE_INVALID",
                "path": "visual_gate.review.coverage",
                "detail": "coverage must contain seven exact categories with success values",
            }
        )
    elif any(coverage.get(field) != "checked" for field in required_coverage):
        issues.append(
            {
                "code": "VISUAL_REVIEW_COVERAGE_INVALID",
                "path": "visual_gate.review.coverage",
                "detail": "all active content categories must be checked",
            }
        )
    repair_applied = value.get("repair_applied")
    verification = value.get("post_repair_verification")
    repair_state_valid = (
        type(repair_applied) is bool
        and (
            (repair_applied is False and verification == "not_required")
            or (repair_applied is True and verification == "passed")
        )
    )
    if not repair_state_valid:
        issues.append(
            {
                "code": "VISUAL_REVIEW_REPAIR_STATE_INVALID",
                "path": "visual_gate.review.post_repair_verification",
                "detail": "repair flag and post-repair verification are inconsistent",
            }
        )
    return issues
