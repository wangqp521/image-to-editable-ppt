"""Hash-bound draft delivery with preview-bound rapid success semantics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifact_identity import ensure_unchanged, snapshot_file
from .hashing import canonical_json_sha256
from .spec_identity import content_spec_sha256


DRAFT_GATE_NAMES = (
    "structure",
    "background",
    "content_completeness",
    "main_editability",
)


class _DraftIdentityError(Exception):
    def __init__(self, code: str, path: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.path = path
        self.detail = detail

    def issue(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


def _expect(condition: bool, code: str, path: str, detail: str) -> None:
    if not condition:
        raise _DraftIdentityError(code, path, detail)


def _identity(
    value: Any,
    path: str,
    snapshots: list[Any],
    *,
    json_payload: bool = False,
) -> tuple[dict[str, str], dict[str, Any] | None, bytes]:
    _expect(isinstance(value, dict), "DRAFT_ARTIFACT_INVALID", path, "file identity must be an object")
    raw_path = value.get("path")
    digest = value.get("sha256")
    _expect(
        isinstance(raw_path, str) and Path(raw_path).is_absolute(),
        "DRAFT_ARTIFACT_INVALID",
        f"{path}.path",
        "absolute path is required",
    )
    _expect(
        isinstance(digest, str) and len(digest) == 64,
        "DRAFT_ARTIFACT_INVALID",
        f"{path}.sha256",
        "SHA-256 is required",
    )
    try:
        raw, snapshot = snapshot_file(Path(raw_path), "DRAFT_ARTIFACT_INVALID")
    except Exception as exc:
        raise _DraftIdentityError(
            "DRAFT_ARTIFACT_INVALID",
            path,
            "artifact is missing or unstable",
        ) from exc
    _expect(
        snapshot.sha256 == digest.lower(),
        "DRAFT_ARTIFACT_INVALID",
        f"{path}.sha256",
        "artifact hash is stale",
    )
    snapshots.append(snapshot)
    payload = None
    if json_payload:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _DraftIdentityError(
                "DRAFT_ARTIFACT_INVALID",
                path,
                "artifact must be UTF-8 JSON",
            ) from exc
        _expect(
            isinstance(payload, dict),
            "DRAFT_ARTIFACT_INVALID",
            path,
            "JSON root must be an object",
        )
    return (
        {"path": str(snapshot.original_path), "sha256": snapshot.sha256},
        payload,
        raw,
    )


def _rapid_success_preview(
    visual_gate: dict[str, Any],
    pptx_identity: dict[str, str],
    snapshots: list[Any],
) -> dict[str, str]:
    try:
        preview_identity, _, _ = _identity(
            visual_gate.get("preview"),
            "visual_gate.preview",
            snapshots,
        )
    except _DraftIdentityError as exc:
        raise _DraftIdentityError(
            "DRAFT_RAPID_PREVIEW_REQUIRED",
            exc.path,
            "rapid_validated requires a readable current-hash preview",
        ) from exc
    _expect(
        Path(preview_identity["path"]).parent.name == pptx_identity["sha256"],
        "DRAFT_RAPID_PREVIEW_REQUIRED",
        "visual_gate.preview.path",
        "rapid_validated preview must be stored under the current PPTX SHA-256 directory",
    )
    evidence = visual_gate.get("evidence")
    _expect(
        isinstance(evidence, list) and preview_identity["path"] in evidence,
        "DRAFT_RAPID_PREVIEW_REQUIRED",
        "visual_gate.evidence",
        "rapid_validated evidence must include the current-hash preview path",
    )
    return preview_identity


def _rapid_success_has_open_visual_issue(spec: dict[str, Any]) -> bool:
    modules = spec.get("modules")
    high_risk = modules.get("high_risk") if isinstance(modules, dict) else None
    items = high_risk.get("items") if isinstance(high_risk, dict) else None
    return isinstance(items, list) and any(
        isinstance(item, dict)
        and item.get("severity") in {"P0", "P1"}
        and item.get("result") != "passed"
        for item in items
    )


def draft_delivery_summary(
    spec: Any,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return the current four-gate draft identity and every blocking issue."""
    errors: list[dict[str, str]] = []
    gates = {name: "failed" for name in DRAFT_GATE_NAMES}
    summary: dict[str, Any] = {"gates": gates}
    snapshots: list[Any] = []
    try:
        _expect(isinstance(spec, dict), "DRAFT_SPEC_INVALID", "$", "spec must be an object")
        profile = spec.get("verification_profile")
        status = spec.get("delivery_status")
        allowed_status = {
            "rapid": {"rapid_validated", "rapid_validation_failed"},
            "reviewed": {"reviewed_failed", "reviewed_passed"},
        }
        _expect(profile in allowed_status, "DRAFT_PROFILE_INVALID", "verification_profile", "rapid or reviewed is required")
        _expect(
            status in allowed_status[profile],
            "DRAFT_DELIVERY_STATUS_INVALID",
            "delivery_status",
            "draft requires a rapid or reviewed terminal status",
        )
        page_id = spec.get("page_id")
        _expect(isinstance(page_id, str) and bool(page_id), "DRAFT_PAGE_ID_INVALID", "page_id", "page ID is required")
        visual_gate = spec.get("visual_gate")
        editability_gate = spec.get("editability_gate")
        _expect(isinstance(visual_gate, dict), "DRAFT_GATE_NOT_PASSED", "visual_gate", "visual gate is required")
        _expect(isinstance(editability_gate, dict), "DRAFT_GATE_NOT_PASSED", "editability_gate", "editability gate is required")

        pptx_identity, _, _ = _identity(
            visual_gate.get("pptx"), "visual_gate.pptx", snapshots
        )
        _expect(
            editability_gate.get("pptx") == pptx_identity,
            "DRAFT_ARTIFACT_INVALID",
            "editability_gate.pptx",
            "both gates must bind the same current PPTX",
        )
        structure_identity, structure, _ = _identity(
            editability_gate.get("validator"),
            "editability_gate.validator",
            snapshots,
            json_payload=True,
        )
        background_identity, background, _ = _identity(
            visual_gate.get("background_contract"),
            "visual_gate.background_contract",
            snapshots,
            json_payload=True,
        )
        assert structure is not None and background is not None
        structure_passed = (
            structure.get("valid") is True
            and structure.get("errors") == []
            and structure.get("pptx_sha256") == pptx_identity["sha256"]
            and structure.get("path") == pptx_identity["path"]
            and structure.get("slide_count") == 1
        )
        background_passed = (
            background.get("valid") is True
            and background.get("errors") == []
            and background.get("page_id") == page_id
            and background.get("pptx_sha256") == pptx_identity["sha256"]
            and background.get("spec_sha256") == content_spec_sha256(spec)
        )
        review = editability_gate.get("review")
        content_passed = isinstance(review, dict) and review.get("text_and_data") == "passed"
        editability_fields = {
            "text_and_data",
            "native_text_structure",
            "basic_structure",
            "full_slide_picture_risk",
        }
        editability_passed = (
            editability_gate.get("status") == "passed"
            and isinstance(review, dict)
            and all(review.get(field) == "passed" for field in editability_fields)
        )
        gates.update(
            {
                "structure": "passed" if structure_passed else "failed",
                "background": "passed" if background_passed else "failed",
                "content_completeness": "passed" if content_passed else "failed",
                "main_editability": "passed" if editability_passed else "failed",
            }
        )
        for name, result in gates.items():
            _expect(
                result == "passed",
                "DRAFT_GATE_NOT_PASSED",
                f"gates.{name}",
                f"draft gate {name} must pass",
            )
        rapid_preview = None
        if profile == "rapid" and status == "rapid_validated":
            rapid_preview = _rapid_success_preview(
                visual_gate,
                pptx_identity,
                snapshots,
            )
            _expect(
                not _rapid_success_has_open_visual_issue(spec),
                "DRAFT_RAPID_VISUAL_ISSUES_OPEN",
                "modules.high_risk.items",
                "rapid_validated requires every recorded P0/P1 visual issue to be closed",
            )
        ensure_unchanged(snapshots)
        summary.update(
            {
                "page_id": page_id,
                "verification_profile": profile,
                "delivery_status": status,
                "spec_sha256": canonical_json_sha256(spec),
                "content_spec_sha256": content_spec_sha256(spec),
                "current_pptx": pptx_identity,
                "structure_validation": structure_identity,
                "background_contract": background_identity,
            }
        )
        if rapid_preview is not None:
            summary["rapid_preview"] = rapid_preview
    except _DraftIdentityError as exc:
        errors.append(exc.issue())
    except Exception as exc:
        errors.append(
            {
                "code": "DRAFT_ARTIFACT_INVALID",
                "path": "draft",
                "detail": f"cannot collect current draft evidence: {exc}",
            }
        )
    return summary, errors
