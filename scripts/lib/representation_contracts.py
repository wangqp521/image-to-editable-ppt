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
    CLASSIFICATION_BASIS_VALUES,
    CLASSIFICATION_EVIDENCE_FIELDS,
    COVERAGE_VALUES,
    EDITABILITY_VALUES,
    FALLBACK_VALUES,
    REPRESENTATION_ITEM_FIELDS,
    REPRESENTATION_MODES,
    VISUAL_ROLE_VALUES,
)
REQUIRED_FIELDS = REPRESENTATION_ITEM_FIELDS
MODES = REPRESENTATION_MODES
EDITABILITY = EDITABILITY_VALUES
FALLBACKS = FALLBACK_VALUES
COVERAGE = COVERAGE_VALUES
VISUAL_ROLES = VISUAL_ROLE_VALUES
ASSET_VISUAL_ROLES = frozenset(
    {"icon", "pictogram", "logo", "photo", "illustration", "texture"}
)
ICON_VISUAL_ROLES = frozenset({"icon", "pictogram", "logo"})
ROLE_RENDER_MODES = {
    "text": frozenset({"native_text"}),
    "data": frozenset(
        {"native_text", "native_table", "native_chart", "composite_native"}
    ),
    "icon": frozenset({"picture_asset"}),
    "pictogram": frozenset({"picture_asset"}),
    "logo": frozenset({"picture_asset"}),
    "photo": frozenset({"picture_asset"}),
    "illustration": frozenset({"picture_asset"}),
    "texture": frozenset({"picture_asset"}),
    "ornament": frozenset({"picture_asset"}),
    "container": frozenset(
        {"native_shape", "composite_native", "picture_asset"}
    ),
    "connector": frozenset({"native_line", "picture_asset"}),
    "diagram_node": frozenset(
        {"native_shape", "composite_native", "picture_asset"}
    ),
    "diagram_geometry": frozenset(
        {"native_shape", "native_line", "composite_native", "picture_asset"}
    ),
    "chart": frozenset({"native_chart", "picture_asset"}),
    "background": frozenset(),
}
CLASSIFICATION_ROLES = {
    "editable_text": frozenset({"text"}),
    "editable_data": frozenset({"data"}),
    "text_adjacent_symbol": ICON_VISUAL_ROLES,
    "repeated_icon_slot": ICON_VISUAL_ROLES,
    "standalone_semantic_symbol": ICON_VISUAL_ROLES,
    "literal_image": frozenset({"photo", "illustration", "texture", "ornament"}),
    "structural_container": frozenset({"container"}),
    "connector_path": frozenset({"connector"}),
    "connector_endpoint_node": frozenset({"diagram_node"}),
    "diagram_geometry": frozenset({"diagram_geometry"}),
    "data_chart": frozenset({"chart"}),
}
RENDERER_MODE_BY_RENDER_MODE = {
    "native_text": "native",
    "native_shape": "native",
    "native_line": "native",
    "native_table": "native",
    "native_chart": "native",
    "composite_native": "composite",
    "picture_asset": "asset",
}
PRIMARY_KINDS_BY_RENDER_MODE = {
    "native_text": frozenset({"text"}),
    "native_shape": frozenset({"shape"}),
    "native_line": frozenset({"line"}),
    "native_table": frozenset({"table"}),
    "native_chart": frozenset({"chart"}),
    "composite_native": frozenset({"matrix", "status"}),
}
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


def _classification_evidence_issues(
    value: Any,
    path: str,
) -> list[ContractIssue]:
    if not isinstance(value, dict):
        return [_incomplete(path, "classification_evidence must be an object")]
    unknown = sorted(set(value) - CLASSIFICATION_EVIDENCE_FIELDS)
    if unknown:
        return [_incomplete(path, f"unknown fields: {', '.join(unknown)}")]
    issues: list[ContractIssue] = []
    for field in (
        "adjacent_text_fact_ids",
        "attached_connector_fact_ids",
        "contained_label_fact_ids",
    ):
        if field not in value:
            continue
        values = value.get(field)
        if (
            not isinstance(values, list)
            or not all(isinstance(item, str) and item.strip() for item in values)
            or len(values) != len(set(values))
        ):
            issues.append(
                _incomplete(
                    f"{path}.{field}",
                    f"{field} must be a unique string array",
                )
            )
    repeat_group_id = value.get("repeat_group_id")
    if "repeat_group_id" in value and repeat_group_id is not None and (
        not isinstance(repeat_group_id, str) or not repeat_group_id.strip()
    ):
        issues.append(
            _incomplete(
                f"{path}.repeat_group_id",
                "repeat_group_id must be a non-empty string or null",
            )
        )
    for field in ("structural_boundary", "full_contour_match"):
        if field in value and type(value.get(field)) is not bool:
            issues.append(
                _incomplete(f"{path}.{field}", f"{field} must be boolean")
            )
    return issues


def _bbox_contains(outer: Any, inner: Any, *, tolerance: int = 2) -> bool:
    if (
        not isinstance(outer, list)
        or not isinstance(inner, list)
        or len(outer) != 4
        or len(inner) != 4
        or any(type(value) is not int for value in (*outer, *inner))
    ):
        return False
    outer_x, outer_y, outer_w, outer_h = outer
    inner_x, inner_y, inner_w, inner_h = inner
    return (
        inner_x >= outer_x - tolerance
        and inner_y >= outer_y - tolerance
        and inner_x + inner_w <= outer_x + outer_w + tolerance
        and inner_y + inner_h <= outer_y + outer_h + tolerance
    )


def _bboxes_touch_or_overlap(first: Any, second: Any, *, tolerance: int = 2) -> bool:
    if (
        not isinstance(first, list)
        or not isinstance(second, list)
        or len(first) != 4
        or len(second) != 4
        or any(type(value) is not int for value in (*first, *second))
    ):
        return False
    first_x, first_y, first_w, first_h = first
    second_x, second_y, second_w, second_h = second
    return not (
        first_x + first_w < second_x - tolerance
        or second_x + second_w < first_x - tolerance
        or first_y + first_h < second_y - tolerance
        or second_y + second_h < first_y - tolerance
    )


def _classification_relationship_issues(
    items_by_fact: dict[str, tuple[dict[str, Any], str]],
    icon_families: list[Any],
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for fact_id, (item, path) in items_by_fact.items():
        basis = item.get("classification_basis")
        evidence = item.get("classification_evidence")
        if not isinstance(evidence, dict):
            continue
        adjacent_ids = evidence.get("adjacent_text_fact_ids", [])
        attached_ids = evidence.get("attached_connector_fact_ids", [])
        contained_ids = evidence.get("contained_label_fact_ids", [])
        if not all(
            isinstance(values, list)
            for values in (adjacent_ids, attached_ids, contained_ids)
        ):
            continue

        def facts_have_roles(values: list[Any], roles: frozenset[str]) -> bool:
            return bool(values) and all(
                isinstance(value, str)
                and value in items_by_fact
                and items_by_fact[value][0].get("visual_role") in roles
                for value in values
            )

        if basis == "text_adjacent_symbol" and not facts_have_roles(
            adjacent_ids, frozenset({"text", "data"})
        ):
            issues.append(
                ContractIssue(
                    "SPEC_CLASSIFICATION_EVIDENCE_INVALID",
                    f"{path}.classification_evidence.adjacent_text_fact_ids",
                    "text_adjacent_symbol requires at least one existing text or data fact",
                )
            )
        if basis == "repeated_icon_slot":
            repeat_group_id = evidence.get("repeat_group_id")
            memberships = [
                family
                for family in icon_families
                if isinstance(family, dict)
                and isinstance(family.get("member_fact_ids"), list)
                and fact_id in family["member_fact_ids"]
            ]
            if (
                not isinstance(repeat_group_id, str)
                or not repeat_group_id
                or len(memberships) != 1
                or memberships[0].get("family_id") != repeat_group_id
            ):
                issues.append(
                    ContractIssue(
                        "SPEC_CLASSIFICATION_EVIDENCE_INVALID",
                        f"{path}.classification_evidence.repeat_group_id",
                        "repeated_icon_slot must resolve to its one declared icon family",
                    )
                )
        if basis == "connector_endpoint_node":
            node_bbox = item.get("source_bbox")
            connector_evidence = facts_have_roles(
                attached_ids, frozenset({"connector"})
            ) and all(
                _bboxes_touch_or_overlap(
                    node_bbox,
                    items_by_fact[connector_id][0].get("source_bbox"),
                )
                for connector_id in attached_ids
            )
            label_evidence = facts_have_roles(
                contained_ids, frozenset({"text", "data"})
            ) and all(
                _bbox_contains(
                    node_bbox,
                    items_by_fact[label_id][0].get("source_bbox"),
                )
                for label_id in contained_ids
            )
            valid_structure = (
                evidence.get("structural_boundary") is True
                and (connector_evidence or label_evidence)
                and (
                    item.get("render_mode") != "native_shape"
                    or evidence.get("full_contour_match") is True
                )
            )
            if not valid_structure:
                issues.append(
                    ContractIssue(
                        "SPEC_NATIVE_NODE_EVIDENCE_MISSING",
                        f"{path}.classification_evidence",
                        "native diagram nodes require a structural boundary, a resolved connector or contained label, and full contour match for native_shape",
                    )
                )
        if basis == "structural_container" and (
            evidence.get("structural_boundary") is not True
            or (
                item.get("render_mode") == "native_shape"
                and evidence.get("full_contour_match") is not True
            )
        ):
            issues.append(
                ContractIssue(
                    "SPEC_STRUCTURAL_EVIDENCE_MISSING",
                    f"{path}.classification_evidence",
                    "native containers require a structural boundary and full contour match for native_shape",
                )
            )
        if (
            basis == "diagram_geometry"
            and item.get("render_mode") == "native_shape"
            and evidence.get("full_contour_match") is not True
        ):
            issues.append(
                ContractIssue(
                    "SPEC_STRUCTURAL_EVIDENCE_MISSING",
                    f"{path}.classification_evidence.full_contour_match",
                    "native_shape diagram geometry requires full contour match",
                )
            )
    return issues


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
    render_mode: Any, element: dict[str, Any] | None
) -> str | None:
    """Resolve renderer mode for a fact binding.

    An asset fact describes its one raster fallback. Editable labels bound to
    that same fact remain native PPT text and therefore keep native renderer
    mode instead of inheriting the fact's asset mode.
    """
    if (
        render_mode == "picture_asset"
        and isinstance(element, dict)
        and element.get("kind") in {"text", "special_text"}
    ):
        return "native"
    return RENDERER_MODE_BY_RENDER_MODE.get(render_mode)


def _validate_asset_fact(
    spec: dict[str, Any],
    item: dict[str, Any],
    path: str,
    bindings: list[str],
    elements: dict[str, dict[str, Any]],
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    editability = item.get("required_editability")
    visual_role = item.get("visual_role")
    fallback_policy = item.get("fallback_policy")
    if (
        fallback_policy not in {"allow_minimal_asset", "required_source_asset"}
        or editability in {"full", "labels_and_geometry"}
    ):
        issues.append(
            ContractIssue(
                "REPRESENTATION_FALLBACK_FORBIDDEN",
                path,
                "picture_asset requires an asset policy and permits only labels_only or none editability",
            )
        )
        return issues
    if visual_role in ASSET_VISUAL_ROLES and fallback_policy != "required_source_asset":
        issues.append(
            ContractIssue(
                "REPRESENTATION_FALLBACK_FORBIDDEN",
                f"{path}.fallback_policy",
                "source imagery and pictograms require required_source_asset",
            )
        )

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
    modules = spec.get("modules")
    icons_module = modules.get("icons") if isinstance(modules, dict) else None
    icon_records = (
        icons_module.get("icons")
        if isinstance(icons_module, dict)
        and isinstance(icons_module.get("icons"), list)
        else []
    )
    icon_families = (
        icons_module.get("families")
        if isinstance(icons_module, dict)
        and isinstance(icons_module.get("families"), list)
        else []
    )

    fact_ids: set[str] = set()
    items_by_fact: dict[str, tuple[dict[str, Any], str]] = {}
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
            items_by_fact[fact_id] = (item, path)
        visual_role = item.get("visual_role")
        if not isinstance(visual_role, str) or visual_role not in VISUAL_ROLES:
            issues.append(_incomplete(f"{path}.visual_role", "visual_role is invalid"))
        classification_basis = item.get("classification_basis")
        if (
            not isinstance(classification_basis, str)
            or classification_basis not in CLASSIFICATION_BASIS_VALUES
        ):
            issues.append(
                _incomplete(
                    f"{path}.classification_basis",
                    "classification_basis is invalid",
                )
            )
        issues.extend(
            _classification_evidence_issues(
                item.get("classification_evidence"),
                f"{path}.classification_evidence",
            )
        )
        try:
            validate_bbox(item.get("source_bbox"), f"{path}.source_bbox")
        except ToolError as exc:
            issues.append(_incomplete(exc.path, exc.detail))

        required = item.get("required")
        if type(required) is not bool:
            issues.append(_incomplete(f"{path}.required", "required must be boolean"))
        render_mode = item.get("render_mode")
        if render_mode is not None and (
            not isinstance(render_mode, str) or render_mode not in MODES
        ):
            issues.append(_incomplete(f"{path}.render_mode", "render_mode is invalid"))
        coverage = item.get("coverage_status")
        if (
            coverage != "not_applicable"
            and isinstance(visual_role, str)
            and visual_role in ROLE_RENDER_MODES
            and isinstance(render_mode, str)
            and render_mode in MODES
            and render_mode not in ROLE_RENDER_MODES[visual_role]
        ):
            issues.append(
                ContractIssue(
                    "SPEC_ROLE_MODE_CONFLICT",
                    f"{path}.render_mode",
                    f"{visual_role} does not permit {render_mode}",
                )
            )
        if coverage != "not_applicable" and classification_basis == "not_applicable":
            issues.append(
                ContractIssue(
                    "SPEC_VISUAL_ROLE_CONTEXT_CONFLICT",
                    f"{path}.classification_basis",
                    "not_applicable classification requires not_applicable coverage",
                )
            )
        allowed_roles = CLASSIFICATION_ROLES.get(classification_basis)
        if (
            coverage != "not_applicable"
            and allowed_roles is not None
            and visual_role not in allowed_roles
        ):
            issues.append(
                ContractIssue(
                    "SPEC_VISUAL_ROLE_CONTEXT_CONFLICT",
                    f"{path}.classification_basis",
                    "classification basis conflicts with visual_role or render_mode",
                )
            )
        if visual_role in {"icon", "pictogram", "logo"} and render_mode != "picture_asset":
            issues.append(
                ContractIssue(
                    "SPEC_ICON_ROLE_MODE_CONFLICT",
                    f"{path}.render_mode",
                    "icon, pictogram, and logo roles require picture_asset",
                )
            )
        editability = item.get("required_editability")
        if not isinstance(editability, str) or editability not in EDITABILITY:
            issues.append(_incomplete(f"{path}.required_editability", "required_editability is invalid"))
        fallback = item.get("fallback_policy")
        if not isinstance(fallback, str) or fallback not in FALLBACKS:
            issues.append(_incomplete(f"{path}.fallback_policy", "fallback_policy is invalid"))
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
                if isinstance(render_mode, str) and render_mode in MODES:
                    binding_mode = _binding_mode(render_mode, elements[element_id])
                    previous = element_modes.get(element_id)
                    if previous is not None and previous != binding_mode:
                        issues.append(_incomplete(f"{path}.bound_element_ids", f"conflicting mode for element: {element_id}"))
                    else:
                        assert binding_mode is not None
                        element_modes[element_id] = binding_mode

        expected_kinds = PRIMARY_KINDS_BY_RENDER_MODE.get(render_mode)
        if expected_kinds is not None:
            bound_kinds = [
                (element_id, elements[element_id].get("kind"))
                for element_id in bindings
                if element_id in elements
            ]
            primary_bindings = [
                element_id
                for element_id, kind in bound_kinds
                if kind in expected_kinds
            ]
            auxiliary_kinds = (
                frozenset() if render_mode == "native_text" else frozenset({"text"})
            )
            wrong_kinds = [
                element_id
                for element_id, kind in bound_kinds
                if kind not in expected_kinds and kind not in auxiliary_kinds
            ]
            if not primary_bindings or wrong_kinds:
                detail = (
                    f"{render_mode} requires at least one element of kind "
                    f"{'|'.join(sorted(expected_kinds))}"
                )
                if wrong_kinds:
                    detail += f"; wrong bindings: {', '.join(wrong_kinds)}"
                issues.append(
                    ContractIssue(
                        "REPRESENTATION_KIND_MODE_CONFLICT",
                        f"{path}.bound_element_ids",
                        detail,
                    )
                )
            bbox_mismatches = [
                element_id
                for element_id in primary_bindings
                if elements[element_id].get("source_bbox")
                != item.get("source_bbox")
            ]
            if bbox_mismatches:
                issues.append(
                    ContractIssue(
                        "SPEC_NATIVE_BINDING_BBOX_CONFLICT",
                        f"{path}.bound_element_ids",
                        "native fact source_bbox must exactly equal every primary bound element source_bbox; "
                        f"mismatches: {', '.join(bbox_mismatches)}",
                    )
                )

        if visual_role in ICON_VISUAL_ROLES and required is True:
            primary_assets = [
                elements[element_id]
                for element_id in bindings
                if element_id in elements
                and elements[element_id].get("kind") in {"picture", "icon"}
            ]
            if (
                len(primary_assets) != 1
                or primary_assets[0].get("kind") != "icon"
            ):
                issues.append(
                    ContractIssue(
                        "SPEC_ICON_ROLE_BINDING_CONFLICT",
                        f"{path}.bound_element_ids",
                        "required icon, pictogram, and logo facts must bind "
                        "exactly one kind=icon element",
                    )
                )
            matching_records = [
                record
                for record in icon_records
                if isinstance(record, dict)
                and record.get("source_fact_id") == fact_id
            ]
            family_memberships = [
                family
                for family in icon_families
                if isinstance(family, dict)
                and isinstance(family.get("member_fact_ids"), list)
                and fact_id in family["member_fact_ids"]
            ]
            record = matching_records[0] if len(matching_records) == 1 else None
            family = family_memberships[0] if len(family_memberships) == 1 else None
            record_element_id = (
                record.get("element_id") if isinstance(record, dict) else None
            )
            primary_element_id = (
                primary_assets[0].get("element_id")
                if len(primary_assets) == 1
                and primary_assets[0].get("kind") == "icon"
                else None
            )
            if (
                record is None
                or family is None
                or record.get("family_id") != family.get("family_id")
                or record_element_id != primary_element_id
                or family.get("required_render_mode") != "picture_asset"
            ):
                issues.append(
                    ContractIssue(
                        "SPEC_ICON_FAMILY_INCOMPLETE",
                        path,
                        "required icon, pictogram, and logo facts must appear "
                        "in exactly one icon record and one picture_asset family",
                    )
                )

        if required is True and (
            coverage != "covered"
            or not bindings
            or not isinstance(render_mode, str)
            or render_mode not in MODES
        ):
            issues.append(_incomplete(path, "required facts must be covered with bindings and a mode"))
        if coverage == "not_applicable" and (
            required is not False or bindings or render_mode is not None
        ):
            issues.append(_incomplete(path, "not_applicable facts must be optional and unbound"))
        if render_mode is None and coverage != "not_applicable":
            issues.append(_incomplete(f"{path}.render_mode", "null mode requires not_applicable coverage"))
        if render_mode == "picture_asset":
            issues.extend(_validate_asset_fact(spec, item, path, bindings, elements))
    issues.extend(_classification_relationship_issues(items_by_fact, icon_families))
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
        render_mode = item.get("render_mode")
        if not isinstance(render_mode, str) or render_mode not in MODES:
            continue
        bindings = item.get("bound_element_ids")
        if isinstance(bindings, list):
            for element_id in bindings:
                if isinstance(element_id, str) and element_id in elements:
                    binding_mode = _binding_mode(render_mode, elements[element_id])
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
        mode = item.get("render_mode")
        if isinstance(mode, str) and mode in MODES:
            summary[mode] += 1
        elif item.get("coverage_status") == "not_applicable":
            summary["not_applicable"] += 1
    return summary
