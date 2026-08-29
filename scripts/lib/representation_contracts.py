"""Fail-closed representation-readiness contracts for schema v2."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .error_codes import ContractIssue, ToolError
from .geometry import is_near_full_page_bbox, validate_bbox
from .hashing import file_sha256
from .artifact_identity import stable_content_path
from .schema_io import index_elements
from .schema_contracts import (
    ASSET_FIELDS,
    COVERAGE_VALUES,
    EDITABILITY_VALUES,
    FALLBACK_VALUES,
    REPRESENTATION_ITEM_FIELDS,
    REPRESENTATION_MODES,
)
REQUIRED_FIELDS = REPRESENTATION_ITEM_FIELDS
MODES = REPRESENTATION_MODES
EDITABILITY = EDITABILITY_VALUES
FALLBACKS = FALLBACK_VALUES
COVERAGE = COVERAGE_VALUES
_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")
_ASSET_FIELDS = ASSET_FIELDS
_ASSET_CAPABILITY = "picture.asset.local_hash"
_ASSET_FORMAT_BY_SUFFIX = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
}


def _incomplete(path: str, detail: str) -> ContractIssue:
    return ContractIssue("REPRESENTATION_INCOMPLETE", path, detail)


def require_asset(
    asset: Any, path: str
) -> tuple[Path, str, tuple[int, int]]:
    """Validate one immutable local image identity for fallback rendering."""
    if not isinstance(asset, dict):
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            path,
            "asset must be an object",
            _ASSET_CAPABILITY,
        )
    unknown = sorted(set(asset) - _ASSET_FIELDS)
    missing = sorted(_ASSET_FIELDS - set(asset))
    if unknown or missing:
        detail = (
            f"unknown asset fields: {', '.join(unknown)}"
            if unknown
            else f"missing asset fields: {', '.join(missing)}"
        )
        raise ToolError(
            "UNSUPPORTED_CAPABILITY", path, detail, _ASSET_CAPABILITY
        )
    raw_path = asset["path"]
    if not isinstance(raw_path, str) or not raw_path:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset path must be non-empty",
            _ASSET_CAPABILITY,
        )
    if "\x00" in raw_path:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset path must not contain NUL characters",
            _ASSET_CAPABILITY,
        )
    try:
        candidate = Path(raw_path)
        is_absolute = candidate.is_absolute()
    except ToolError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset path cannot be parsed",
            _ASSET_CAPABILITY,
        ) from exc
    if not is_absolute:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset path must be a literal absolute path without user expansion",
            _ASSET_CAPABILITY,
        )
    try:
        has_symlink = any(
            part.is_symlink() for part in (candidate, *candidate.parents)
        )
        is_file = candidate.is_file()
        resolved = candidate.resolve(strict=True)
    except ToolError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset path cannot be inspected or resolved",
            _ASSET_CAPABILITY,
        ) from exc
    if has_symlink or not is_file:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset must be a readable, non-symlink local file",
            _ASSET_CAPABILITY,
        )
    declared_format = _ASSET_FORMAT_BY_SUFFIX.get(resolved.suffix.lower())
    if declared_format is None:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset must use a PNG, JPEG, or WEBP extension",
            _ASSET_CAPABILITY,
        )
    expected = asset["asset_sha256"]
    if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.asset_sha256",
            "asset sha256 must contain 64 hex characters",
            _ASSET_CAPABILITY,
        )
    try:
        content_path = stable_content_path(candidate)
        actual = file_sha256(content_path)
    except ToolError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset file cannot be read for hashing",
            _ASSET_CAPABILITY,
        ) from exc
    if actual.lower() != expected.lower():
        raise ToolError(
            "ASSET_HASH_MISMATCH",
            f"{path}.asset_sha256",
            "asset hash does not match current file",
            _ASSET_CAPABILITY,
        )
    pixel_size = asset["pixel_size"]
    if (
        not isinstance(pixel_size, list)
        or len(pixel_size) != 2
        or any(type(value) is not int or value <= 0 for value in pixel_size)
    ):
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.pixel_size",
            "pixel_size must contain two positive integers",
            _ASSET_CAPABILITY,
        )
    try:
        with Image.open(content_path) as image:
            actual_format = str(image.format or "").upper()
            image.load()
            actual_size = image.size
    except ToolError:
        raise
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        RuntimeError,
    ) as exc:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            "asset is not a readable image",
            _ASSET_CAPABILITY,
        ) from exc
    if actual_format != declared_format:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.path",
            f"asset content format {actual_format or '<unknown>'} does not match {declared_format} extension",
            _ASSET_CAPABILITY,
        )
    if tuple(pixel_size) != actual_size:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.pixel_size",
            "declared pixel_size does not match current asset",
            _ASSET_CAPABILITY,
        )
    return resolved, actual, actual_size


def _binding_mode(
    selected_mode: Any, element: dict[str, Any] | None
) -> str | None:
    """Resolve renderer mode for a fact binding.

    An asset fact describes its one raster fallback. Editable labels bound to
    that same fact remain native PPT text and therefore keep native renderer
    mode instead of inheriting the fact's asset mode.
    """
    if (
        selected_mode == "asset"
        and isinstance(element, dict)
        and element.get("kind") in {"text", "special_text"}
    ):
        return "native"
    return selected_mode if isinstance(selected_mode, str) else None


def _validate_asset_fact(
    spec: dict[str, Any],
    item: dict[str, Any],
    path: str,
    bindings: list[str],
    elements: dict[str, dict[str, Any]],
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    editability = item.get("required_editability")
    if (
        item.get("fallback_policy") != "allow_minimal_asset"
        or editability in {"full", "labels_and_geometry"}
    ):
        issues.append(
            ContractIssue(
                "REPRESENTATION_FALLBACK_FORBIDDEN",
                path,
                "asset mode requires allow_minimal_asset and permits only labels_only or none editability",
            )
        )
        return issues

    bound_elements = [
        elements[element_id] for element_id in bindings if element_id in elements
    ]
    pictures = [
        element
        for element in bound_elements
        if element.get("kind") in {"picture", "icon"}
    ]
    labels = [
        element
        for element in bound_elements
        if element.get("kind") in {"text", "special_text"}
    ]
    unsupported = [
        element.get("element_id", "<unknown>")
        for element in bound_elements
        if element.get("kind") not in {"picture", "icon", "text", "special_text"}
    ]
    if unsupported:
        issues.append(
            _incomplete(
                f"{path}.bound_element_ids",
                "asset facts allow only one picture/icon and native text labels; "
                f"wrong-kind bindings: {', '.join(unsupported)}",
            )
        )
    if len(pictures) != 1:
        issues.append(
            _incomplete(
                f"{path}.bound_element_ids",
                "asset fact must bind exactly one picture or icon",
            )
        )
        return issues

    picture = pictures[0]
    picture_id = picture.get("element_id", "<unknown>")
    if picture.get("source_bbox") != item.get("source_bbox"):
        issues.append(
            _incomplete(
                f"elements.{picture_id}.source_bbox",
                "asset picture source_bbox must exactly equal the fact source_bbox in source-pixel XYWH units (zero tolerance)",
            )
        )
    canvas = spec.get("canvas")
    page_bbox = canvas.get("page_frame_bbox") if isinstance(canvas, dict) else None
    try:
        near_full_page = is_near_full_page_bbox(
            item.get("source_bbox"), page_bbox
        )
    except ToolError as exc:
        issues.append(_incomplete(exc.path, exc.detail))
        near_full_page = False
    if near_full_page:
        issues.append(
            ContractIssue(
                "REPRESENTATION_FALLBACK_FORBIDDEN",
                f"{path}.source_bbox",
                "asset fallback must be local and cannot be a near-full source page image",
            )
        )
    content = picture.get("content")
    asset = content.get("asset") if isinstance(content, dict) else None
    try:
        require_asset(asset, f"elements.{picture_id}.content.asset")
    except ToolError as exc:
        issues.append(ContractIssue(exc.code, exc.path, exc.detail, exc.capability))
    if editability == "labels_only" and not any(
        label.get("editable") is True for label in labels
    ):
        issues.append(
            _incomplete(
                f"{path}.bound_element_ids",
                "labels_only asset fact requires at least one editable text or special_text label",
            )
        )
    return issues


def _plan_items(spec: dict[str, Any]) -> tuple[list[Any] | None, list[ContractIssue]]:
    modules = spec.get("modules")
    if not isinstance(modules, dict):
        return None, [_incomplete("modules", "modules must be an object")]
    plan = modules.get("representation_plan")
    if not isinstance(plan, dict):
        return None, [_incomplete("modules.representation_plan", "representation_plan must be an object")]
    unknown = sorted(set(plan) - {"items"})
    if unknown:
        return None, [
            _incomplete(
                "modules.representation_plan",
                f"unknown fields: {', '.join(unknown)}",
            )
        ]
    items = plan.get("items")
    if not isinstance(items, list) or not items:
        return None, [_incomplete("modules.representation_plan.items", "items must be a non-empty array")]
    return items, []


def validate_representation_plan(spec: dict[str, Any]) -> list[ContractIssue]:
    """Return every representation-readiness defect without raising."""
    if not isinstance(spec, dict):
        return [_incomplete("$", "schema root must be an object")]
    items, issues = _plan_items(spec)
    if items is None:
        return issues
    try:
        elements = index_elements(spec)
    except ToolError as exc:
        return [_incomplete(exc.path, exc.detail)]

    fact_ids: set[str] = set()
    element_modes: dict[str, str] = {}
    for index, item in enumerate(items):
        path = f"modules.representation_plan.items[{index}]"
        if not isinstance(item, dict):
            issues.append(_incomplete(path, "item must be an object"))
            continue
        unknown = sorted(set(item) - REQUIRED_FIELDS)
        missing = sorted(REQUIRED_FIELDS - set(item))
        if unknown:
            issues.append(_incomplete(path, f"unknown fields: {', '.join(unknown)}"))
        if missing:
            issues.append(_incomplete(path, f"missing fields: {', '.join(missing)}"))

        fact_id = item.get("source_fact_id")
        if not isinstance(fact_id, str) or not fact_id.strip():
            issues.append(_incomplete(f"{path}.source_fact_id", "source_fact_id must be non-empty"))
        elif fact_id in fact_ids:
            issues.append(_incomplete(f"{path}.source_fact_id", "source_fact_id must be unique"))
        else:
            fact_ids.add(fact_id)
        if not isinstance(item.get("semantic_role"), str) or not item["semantic_role"].strip():
            issues.append(_incomplete(f"{path}.semantic_role", "semantic_role must be non-empty"))
        try:
            validate_bbox(item.get("source_bbox"), f"{path}.source_bbox")
        except ToolError as exc:
            issues.append(_incomplete(exc.path, exc.detail))

        required = item.get("required")
        if type(required) is not bool:
            issues.append(_incomplete(f"{path}.required", "required must be boolean"))
        selected_mode = item.get("selected_mode")
        if selected_mode is not None and (
            not isinstance(selected_mode, str) or selected_mode not in MODES
        ):
            issues.append(_incomplete(f"{path}.selected_mode", "selected_mode is invalid"))
        editability = item.get("required_editability")
        if not isinstance(editability, str) or editability not in EDITABILITY:
            issues.append(_incomplete(f"{path}.required_editability", "required_editability is invalid"))
        fallback = item.get("fallback_policy")
        if not isinstance(fallback, str) or fallback not in FALLBACKS:
            issues.append(_incomplete(f"{path}.fallback_policy", "fallback_policy is invalid"))
        coverage = item.get("coverage_status")
        if not isinstance(coverage, str) or coverage not in COVERAGE:
            issues.append(_incomplete(f"{path}.coverage_status", "coverage_status is invalid"))
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            issues.append(_incomplete(f"{path}.reason", "reason must be non-empty"))
        evidence = item.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(value, str) and value.strip() for value in evidence)
        ):
            issues.append(_incomplete(f"{path}.evidence", "evidence must be a non-empty string array"))

        bindings = item.get("bound_element_ids")
        valid_bindings = isinstance(bindings, list) and all(
            isinstance(value, str) and value for value in bindings
        )
        if not valid_bindings:
            issues.append(_incomplete(f"{path}.bound_element_ids", "bindings must be a string array"))
            bindings = []
        elif len(bindings) != len(set(bindings)):
            issues.append(_incomplete(f"{path}.bound_element_ids", "bindings must be unique"))
        else:
            for element_id in bindings:
                if element_id not in elements:
                    issues.append(_incomplete(f"{path}.bound_element_ids", f"unknown element: {element_id}"))
                    continue
                if isinstance(selected_mode, str) and selected_mode in MODES:
                    binding_mode = _binding_mode(selected_mode, elements[element_id])
                    previous = element_modes.get(element_id)
                    if previous is not None and previous != binding_mode:
                        issues.append(_incomplete(f"{path}.bound_element_ids", f"conflicting mode for element: {element_id}"))
                    else:
                        assert binding_mode is not None
                        element_modes[element_id] = binding_mode

        if required is True and (
            coverage != "covered"
            or not bindings
            or not isinstance(selected_mode, str)
            or selected_mode not in MODES
        ):
            issues.append(_incomplete(path, "required facts must be covered with bindings and a mode"))
        if coverage == "not_applicable" and (
            required is not False or bindings or selected_mode is not None
        ):
            issues.append(_incomplete(path, "not_applicable facts must be optional and unbound"))
        if selected_mode is None and coverage != "not_applicable":
            issues.append(_incomplete(f"{path}.selected_mode", "null mode requires not_applicable coverage"))
        if selected_mode == "asset":
            issues.extend(_validate_asset_fact(spec, item, path, bindings, elements))
    return issues


def element_mode_map(spec: dict[str, Any]) -> dict[str, str]:
    """Return the selected representation mode for every bound element."""
    items, _ = _plan_items(spec) if isinstance(spec, dict) else (None, [])
    if items is None:
        return {}
    try:
        elements = index_elements(spec)
    except ToolError:
        return {}
    modes: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        selected_mode = item.get("selected_mode")
        if not isinstance(selected_mode, str) or selected_mode not in MODES:
            continue
        bindings = item.get("bound_element_ids")
        if isinstance(bindings, list):
            for element_id in bindings:
                if isinstance(element_id, str) and element_id in elements:
                    binding_mode = _binding_mode(selected_mode, elements[element_id])
                    if binding_mode is not None:
                        modes[element_id] = binding_mode
    return modes


def representation_summary(spec: dict[str, Any]) -> dict[str, int]:
    """Return deterministic counts by selected mode and not-applicable coverage."""
    summary = {mode: 0 for mode in sorted(MODES)}
    summary["not_applicable"] = 0
    items, _ = _plan_items(spec) if isinstance(spec, dict) else (None, [])
    if items is None:
        return summary
    for item in items:
        if not isinstance(item, dict):
            continue
        mode = item.get("selected_mode")
        if isinstance(mode, str) and mode in MODES:
            summary[mode] += 1
        elif item.get("coverage_status") == "not_applicable":
            summary["not_applicable"] += 1
    return summary
