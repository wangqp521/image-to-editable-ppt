"""Pure deterministic contracts for independent visual review."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .hashing import canonical_json_sha256


REVIEW_CONTEXT_ARTIFACT_FIELDS = (
    "build_spec_snapshot",
    "build_report",
    "current_pptx",
    "source",
    "preview",
    "structure_validation",
    "background_contract",
)
RAW_REVIEWER_RESPONSE_FIELDS = (
    "response_schema_version",
    "review_context_sha256",
    "page_id",
    "review_round",
    "verification_profile",
    "decision",
    "coverage",
    "findings",
    "p2_disclosures",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_PROFILES = frozenset({"reviewed"})
VISUAL_REVIEW_COVERAGE_FIELDS = {
    "canvas_and_regions",
    "objects_and_geometry",
    "text_and_typography",
    "tables_and_matrices",
    "graphics_connectors_charts",
    "pictures_crop_layers",
    "high_risk_regions",
}
VISUAL_REVIEW_COVERAGE_RESULTS = {
    "checked",
    "not_applicable",
    "not_reviewable",
}
VISUAL_REVIEW_DECISIONS = {"passed", "changes_required", "not_reviewable"}
VISUAL_REVIEW_FINDING_FIELDS = {
    "severity",
    "category",
    "location",
    "source_fact",
    "observed_difference",
    "evidence",
}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def valid_visual_review_finding(
    value: Any,
    *,
    evidence_mode: str,
    required_severity: str | None = None,
) -> bool:
    """Validate one finding under the final or raw-response evidence profile."""
    if not isinstance(value, dict) or not VISUAL_REVIEW_FINDING_FIELDS.issubset(value):
        return False
    severity = value.get("severity")
    if (
        not isinstance(severity, str)
        or severity not in {"P0", "P1", "P2"}
        or (required_severity is not None and severity != required_severity)
        or not isinstance(value.get("category"), str)
        or value["category"] not in VISUAL_REVIEW_COVERAGE_FIELDS
        or not all(
            _nonempty_string(value.get(field))
            for field in ("location", "source_fact", "observed_difference")
        )
    ):
        return False
    evidence = value.get("evidence")
    if evidence_mode == "string":
        return _nonempty_string(evidence)
    if evidence_mode == "string_list":
        return (
            isinstance(evidence, list)
            and bool(evidence)
            and all(_nonempty_string(item) for item in evidence)
        )
    raise ValueError(f"unsupported reviewer evidence mode: {evidence_mode}")


def valid_response_p2_disclosures(value: Any) -> bool:
    """Require each raw-response disclosure to be one complete P2 finding."""
    return isinstance(value, list) and all(
        valid_visual_review_finding(
            item,
            evidence_mode="string_list",
            required_severity="P2",
        )
        for item in value
    )


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _identity(value: Any, *, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError(f"{field} must contain exactly path and sha256")
    path = value.get("path")
    sha256 = value.get("sha256")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError(f"{field}.path must be an absolute path")
    if not _valid_sha256(sha256):
        raise ValueError(f"{field}.sha256 must be lowercase 64-character hex")
    return {"path": path, "sha256": sha256}


def build_review_context(
    *,
    page_id: str,
    review_round: int,
    verification_profile: str,
    content_spec_sha256: str,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Build canonical reviewer inputs from already-validated JSON identities."""
    if not _nonempty_string(page_id):
        raise ValueError("page_id must be a non-empty string")
    if type(review_round) is not int or review_round not in {1, 2}:
        raise ValueError("review_round must be integer 1 or 2")
    if verification_profile not in _REVIEW_PROFILES:
        raise ValueError("verification_profile must be reviewed")
    if not _valid_sha256(content_spec_sha256):
        raise ValueError("content_spec_sha256 must be lowercase 64-character hex")
    if not isinstance(artifacts, dict) or set(artifacts) != set(
        REVIEW_CONTEXT_ARTIFACT_FIELDS
    ):
        raise ValueError("artifacts must contain exactly the review artifact fields")
    context: dict[str, Any] = {
        "context_schema_version": "2",
        "page_id": page_id,
        "review_round": review_round,
        "verification_profile": verification_profile,
        "content_spec_sha256": content_spec_sha256,
    }
    for name in REVIEW_CONTEXT_ARTIFACT_FIELDS:
        context[name] = _identity(artifacts[name], field=name)
    return context


def review_context_sha256(context: Any) -> str:
    """Return the SHA-256 digest of canonical UTF-8 context JSON."""
    return canonical_json_sha256(context)


def render_reviewer_prompt(context: dict[str, Any]) -> str:
    """Render a deterministic, read-only visual-review prompt without I/O."""
    canonical_context = json.dumps(
        context,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    context_sha256 = review_context_sha256(context)
    coverage_fields = ", ".join(sorted(VISUAL_REVIEW_COVERAGE_FIELDS))
    coverage_results = ", ".join(sorted(VISUAL_REVIEW_COVERAGE_RESULTS))
    finding_fields = ", ".join(sorted(VISUAL_REVIEW_FINDING_FIELDS))
    response_fields = ", ".join(RAW_REVIEWER_RESPONSE_FIELDS)
    response_skeleton = json.dumps(
        {
            "response_schema_version": "1",
            "review_context_sha256": context_sha256,
            "page_id": context["page_id"],
            "review_round": context["review_round"],
            "verification_profile": context["verification_profile"],
            "decision": "passed",
            "coverage": {
                field: "checked" for field in sorted(VISUAL_REVIEW_COVERAGE_FIELDS)
            },
            "findings": [],
            "p2_disclosures": [],
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "\n".join(
        (
            "你是独立视觉审核者。执行 read-only 审核，不得修改任何文件，也不得运行 producer。",
            f"review_context_sha256: {context_sha256}",
            f"canonical review context: {canonical_context}",
            "只使用 context 中列出的当前证据绝对路径，并核对 source 与 preview。",
            '协议版本不得混用：context_schema_version 固定为字符串 "2"；'
            'response_schema_version 必须固定为字符串 "1"，不得复制前者。',
            f"coverage 必须精确包含七类：{coverage_fields}。",
            f"coverage 值只允许：{coverage_results}。",
            f"只返回一个 JSON object，精确包含九个字段：{response_fields}。",
            f"finding 必须包含以下六个必需字段：{finding_fields}。",
            "finding.severity 只允许 P0、P1、P2；finding.category 必须是上述七类之一。",
            "decision 只允许 passed、changes_required、not_reviewable。",
            "findings 与 p2_disclosures 的 evidence 必须是 context 中证据绝对路径组成的非空数组。",
            "p2_disclosures 中的每一项都必须是 severity=P2 的完整 finding。",
            "P0/P1 必须对应 changes_required；passed 不得包含 P0/P1 或 not_reviewable coverage。",
            f"response JSON skeleton: {response_skeleton}",
            "skeleton 仅示范字段、类型和当前 context 绑定；如发现问题，必须改写 decision 并填写完整 finding，不得盲目复制 passed。",
            "输出前自检：仅输出 JSON，顶层恰好九个字段，版本、context identity、coverage 和 finding 均符合上述契约。",
        )
    )


def _response_issue(code: str, path: str, detail: str) -> dict[str, str]:
    return {"code": code, "path": path, "detail": detail}


def _evidence_issues(
    entries: Any,
    *,
    path: str,
    required_severity: str | None,
    allowed_evidence: frozenset[str],
) -> tuple[list[dict[str, str]], bool, bool]:
    issues: list[dict[str, str]] = []
    if not isinstance(entries, list):
        return (
            [
                _response_issue(
                    "REVIEW_RESPONSE_INVALID",
                    path,
                    "value must be an array of complete findings",
                )
            ],
            False,
            False,
        )
    valid = True
    blocking = False
    for index, finding in enumerate(entries):
        if not valid_visual_review_finding(
            finding,
            evidence_mode="string_list",
            required_severity=required_severity,
        ):
            valid = False
            issues.append(
                _response_issue(
                    "REVIEW_RESPONSE_INVALID",
                    f"{path}[{index}]",
                    "finding fields must be typed, non-empty, and complete",
                )
            )
            continue
        severity = finding["severity"]
        if severity in {"P0", "P1"}:
            blocking = True
        for evidence_index, evidence in enumerate(finding["evidence"]):
            if evidence not in allowed_evidence:
                valid = False
                issues.append(
                    _response_issue(
                        "REVIEW_RESPONSE_EVIDENCE_NOT_ALLOWED",
                        f"{path}[{index}].evidence[{evidence_index}]",
                        "evidence path is not bound by the current review context",
                    )
                )
    return issues, valid, blocking


def reviewer_response_issues(
    response: Any,
    *,
    expected_context_sha256: str,
    page_id: str,
    review_round: int,
    verification_profile: str,
    required_coverage: frozenset[str],
    allowed_evidence: frozenset[str],
) -> list[dict[str, str]]:
    """Return stable validation issues for one raw reviewer response."""
    if not isinstance(response, dict):
        return [
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response",
                "review response must be a JSON object",
            )
        ]

    issues: list[dict[str, str]] = []
    if set(response) != set(RAW_REVIEWER_RESPONSE_FIELDS):
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response",
                "review response fields must exactly match the nine-field contract",
            )
        )
    if response.get("response_schema_version") != "1":
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.response_schema_version",
                "response_schema_version must be string 1",
            )
        )

    expected_identity = (
        (
            "review_context_sha256",
            expected_context_sha256,
            "REVIEW_RESPONSE_CONTEXT_MISMATCH",
        ),
        ("page_id", page_id, "REVIEW_RESPONSE_PAGE_MISMATCH"),
        ("review_round", review_round, "REVIEW_RESPONSE_ROUND_MISMATCH"),
        (
            "verification_profile",
            verification_profile,
            "REVIEW_RESPONSE_PROFILE_MISMATCH",
        ),
    )
    for field, expected, code in expected_identity:
        actual = response.get(field)
        same = type(actual) is type(expected) and actual == expected
        if not same:
            issues.append(
                _response_issue(
                    code,
                    f"response.{field}",
                    f"{field} does not match the current review context",
                )
            )

    decision = response.get("decision")
    if not isinstance(decision, str) or decision not in VISUAL_REVIEW_DECISIONS:
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.decision",
                "decision must be passed, changes_required, or not_reviewable",
            )
        )

    coverage = response.get("coverage")
    coverage_valid = (
        isinstance(coverage, dict)
        and set(coverage) == VISUAL_REVIEW_COVERAGE_FIELDS
        and all(
            isinstance(value, str) and value in VISUAL_REVIEW_COVERAGE_RESULTS
            for value in coverage.values()
        )
    )
    if not coverage_valid:
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.coverage",
                "coverage must contain exactly seven keys with allowed values",
            )
        )
    elif not all(coverage.get(field) == "checked" for field in required_coverage):
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.coverage",
                "all coverage required by the current profile must be checked",
            )
        )
    if (
        decision == "passed"
        and isinstance(coverage, dict)
        and "not_reviewable" in coverage.values()
    ):
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.coverage",
                "passed responses cannot contain not_reviewable coverage",
            )
        )

    finding_issues, findings_valid, blocking = _evidence_issues(
        response.get("findings"),
        path="response.findings",
        required_severity=None,
        allowed_evidence=allowed_evidence,
    )
    issues.extend(finding_issues)
    disclosure_issues, _, _ = _evidence_issues(
        response.get("p2_disclosures"),
        path="response.p2_disclosures",
        required_severity="P2",
        allowed_evidence=allowed_evidence,
    )
    issues.extend(disclosure_issues)

    if findings_valid and blocking and decision != "changes_required":
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.decision",
                "P0/P1 findings require changes_required",
            )
        )
    if findings_valid and not blocking and decision == "changes_required":
        issues.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.decision",
                "changes_required requires at least one P0/P1 finding",
            )
        )
    return issues
