"""Read-only collection and cross-binding of current reconstruction artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_identity import EvidenceSnapshot, ensure_unchanged, is_sha256, snapshot_file
from .hashing import canonical_json_sha256
from .spec_identity import content_spec_sha256, input_spec_sha256
from .visual_review_contracts import CURRENT_VISUAL_ARTIFACT_FIELDS


@dataclass(frozen=True)
class CurrentArtifacts:
    identities: dict[str, dict[str, str]]
    required_coverage: frozenset[str]


class _InvalidIdentity(Exception):
    def __init__(self, path: str, detail: str, code: str = "FINAL_IDENTITY_INVALID") -> None:
        super().__init__(detail)
        self.code = code
        self.path = path
        self.detail = detail

    def issue(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


def _expect(condition: bool, path: str, detail: str) -> None:
    if not condition:
        raise _InvalidIdentity(path, detail)


def _record(
    value: Any,
    label: str,
    snapshots: list[EvidenceSnapshot],
    *,
    expected_path: str | None = None,
    reject_symlink: bool = False,
) -> tuple[bytes, EvidenceSnapshot, dict[str, str]]:
    _expect(isinstance(value, dict), label, "file identity must be an object")
    path = value.get("path")
    digest = value.get("sha256")
    _expect(isinstance(path, str) and Path(path).is_absolute(), f"{label}.path", "absolute path is required")
    _expect(is_sha256(digest), f"{label}.sha256", "lowercase SHA-256 is required")
    if reject_symlink:
        try:
            _expect(not Path(path).is_symlink(), f"{label}.path", "symlink is not allowed")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _InvalidIdentity(f"{label}.path", "required artifact is missing or unstable") from exc
    try:
        raw, snapshot = snapshot_file(path, f"{label}.path")
    except Exception as exc:
        raise _InvalidIdentity(f"{label}.path", "required artifact is missing or unstable") from exc
    _expect(snapshot.sha256 == digest, f"{label}.sha256", "reported file hash is stale")
    if expected_path is not None:
        try:
            expected = str(Path(expected_path).resolve(strict=True))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _InvalidIdentity(f"{label}.path", "expected artifact path is invalid") from exc
        _expect(str(snapshot.original_path) == expected, f"{label}.path", "reported path is stale")
    snapshots.append(snapshot)
    return raw, snapshot, {"path": str(snapshot.original_path), "sha256": snapshot.sha256}


def _json_record(
    value: Any,
    label: str,
    snapshots: list[EvidenceSnapshot],
    *,
    expected_path: str | None = None,
) -> tuple[dict[str, Any], EvidenceSnapshot, dict[str, str]]:
    raw, snapshot, identity = _record(value, label, snapshots, expected_path=expected_path)
    try:
        payload = json.loads(raw.decode("utf-8"))
        _expect(isinstance(payload, dict), label, "JSON root must be an object")
        canonical_json_sha256(payload)
    except _InvalidIdentity:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _InvalidIdentity(label, "artifact must contain finite UTF-8 JSON") from exc
    return payload, snapshot, identity


def _dict(value: Any, path: str) -> dict[str, Any]:
    _expect(isinstance(value, dict), path, "object is required")
    return value


def _same_identity(value: Any, identity: dict[str, str], path: str) -> None:
    _expect(isinstance(value, dict), path, "file identity is required")
    _expect(value.get("path") == identity["path"], f"{path}.path", "path is stale")
    _expect(value.get("sha256") == identity["sha256"], f"{path}.sha256", "hash is stale")


def _required_coverage(spec: dict[str, Any], profile: str) -> frozenset[str]:
    required = {"canvas_and_regions", "objects_and_geometry"}
    elements = spec.get("elements")
    kinds = {
        item.get("kind")
        for item in (elements if isinstance(elements, list) else [])
        if isinstance(item, dict)
    }
    activated = set(spec.get("activated_modules", [])) if isinstance(spec.get("activated_modules"), list) else set()
    if kinds & {"text", "special_text"} or activated & {"typography", "special_text"}:
        required.add("text_and_typography")
    if kinds & {"table", "matrix"}:
        required.add("tables_and_matrices")
    if kinds & {"shape", "line", "status", "diagram", "chart"} or activated & {"graphics", "diagram", "chart"}:
        required.add("graphics_connectors_charts")
    if kinds & {"icon", "picture"} or activated & {"icons", "picture_framing"}:
        required.add("pictures_crop_layers")
    modules = spec.get("modules")
    high_risk = modules.get("high_risk") if isinstance(modules, dict) else None
    if "high_risk" in activated and isinstance(high_risk, dict) and high_risk.get("items"):
        required.add("high_risk_regions")
    return frozenset(required)


def collect_current_artifacts(
    spec: Any,
) -> tuple[CurrentArtifacts | None, list[dict[str, str]]]:
    """Hash and cross-check existing artifacts without running any producer."""
    snapshots: list[EvidenceSnapshot] = []
    try:
        _expect(isinstance(spec, dict), "spec", "spec must be a JSON object")
        profile = spec.get("verification_profile")
        _expect(profile in {"rapid", "reviewed"}, "verification_profile", "rapid or reviewed profile is required")
        page_id = spec.get("page_id")
        _expect(isinstance(page_id, str) and bool(page_id), "page_id", "page ID is required")
        content_hash = content_spec_sha256(spec)

        visual_gate = _dict(spec.get("visual_gate"), "visual_gate")
        editability_gate = _dict(spec.get("editability_gate"), "editability_gate")
        _, pptx, pptx_identity = _record(visual_gate.get("pptx"), "visual_gate.pptx", snapshots)
        _same_identity(editability_gate.get("pptx"), pptx_identity, "editability_gate.pptx")
        _, source, source_identity = _record(
            spec.get("clean_visual_reference"),
            "clean_visual_reference",
            snapshots,
            reject_symlink=True,
        )
        _, preview, preview_identity = _record(
            visual_gate.get("preview"),
            "visual_gate.preview",
            snapshots,
            reject_symlink=True,
        )
        _expect(
            Path(preview_identity["path"]).parent.name == pptx.sha256,
            "visual_gate.preview.path",
            "preview must be stored under the current PPTX SHA-256 directory",
        )

        structure, structure_snapshot, structure_identity = _json_record(editability_gate.get("validator"), "editability_gate.validator", snapshots)
        background, background_snapshot, background_identity = _json_record(visual_gate.get("background_contract"), "visual_gate.background_contract", snapshots)

        build_spec, build_spec_snapshot, build_spec_identity = _json_record(
            editability_gate.get("build_spec_snapshot"),
            "editability_gate.build_spec_snapshot",
            snapshots,
        )
        build, build_snapshot, build_identity = _json_record(
            editability_gate.get("build_report"),
            "editability_gate.build_report",
            snapshots,
        )
        _expect(content_spec_sha256(build_spec) == content_hash, "build_spec_snapshot", "build snapshot content identity is stale")
        build_input_hash = input_spec_sha256(build_spec)
        _expect(build.get("schema_version") == 1 and build.get("valid") is True, "build_report.valid", "build report must pass")
        _expect(
            "errors" not in build or build.get("errors") == [],
            "build_report.errors",
            "build report errors block review",
        )
        for field, expected in (
            ("schema_sha256", canonical_json_sha256(build_spec)),
            ("content_spec_sha256", content_hash),
            ("input_spec_sha256", build_input_hash),
            ("pptx_sha256", pptx.sha256),
        ):
            _expect(build.get(field) == expected, f"build_report.{field}", "build report identity is stale")
        _expect(build.get("unsupported") == [], "build_report.unsupported", "unsupported build output blocks review")

        _expect(structure.get("valid") is True and structure.get("errors") == [], "structure_validation.valid", "structure validation must pass")
        _expect(structure.get("pptx_sha256") == pptx.sha256, "structure_validation.pptx_sha256", "structure PPTX identity is stale")
        _expect(structure.get("path") == pptx_identity["path"], "structure_validation.path", "structure report points to another PPTX")
        _expect(structure.get("slide_count") == 1, "structure_validation.slide_count", "exactly one slide is required")

        background_expected = {
            "schema_version": 1,
            "page_id": page_id,
            "spec_sha256": content_hash,
            "input_spec_sha256": build_input_hash,
            "pptx_sha256": pptx.sha256,
            "build_report_sha256": canonical_json_sha256(build),
            "build_report_file_sha256": build_snapshot.sha256,
            "structure_report_sha256": canonical_json_sha256(structure),
            "structure_report_file_sha256": structure_snapshot.sha256,
        }
        for field, expected in background_expected.items():
            _expect(background.get(field) == expected, f"background_contract.{field}", "background identity is stale")
        _expect(background.get("valid") is True and background.get("errors") == [], "background_contract.valid", "background contract must pass")
        _expect(isinstance(background.get("items"), list) and bool(background["items"]) and all(isinstance(item, dict) and item.get("valid") is True for item in background["items"]), "background_contract.items", "all background items must pass")

        ensure_unchanged(snapshots)
        identities = {
            "build_spec_snapshot": build_spec_identity,
            "build_report": build_identity,
            "current_pptx": pptx_identity,
            "source": source_identity,
            "preview": preview_identity,
            "structure_validation": structure_identity,
            "background_contract": background_identity,
        }
        _expect(tuple(identities) == CURRENT_VISUAL_ARTIFACT_FIELDS, "artifacts", "artifact field order is invalid")
        return CurrentArtifacts(
            identities=identities,
            required_coverage=_required_coverage(spec, profile),
        ), []
    except _InvalidIdentity as exc:
        return None, [exc.issue()]
    except Exception as exc:
        return None, [{"code": "FINAL_IDENTITY_INVALID", "path": "artifacts", "detail": f"cannot collect current artifact identities: {exc}"}]
