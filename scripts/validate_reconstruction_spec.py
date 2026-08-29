#!/usr/bin/env python3
"""Validate one page-reconstruction.json before generation or delivery."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from lib.atomic_write import atomic_write_bytes
from lib.artifact_identity import is_sha256
from lib.hashing import canonical_json_sha256, file_sha256
from lib.background_contracts import (
    resolved_element_mode_map,
    validate_background_prebuild,
)
from lib.capabilities import (
    TEXT_CONTRACT_ALLOWED_FIELDS,
    TEXT_RUN_BASELINE_MAX,
    TEXT_RUN_BASELINE_MIN,
    TEXT_RUN_MODERN_FIELDS,
    text_run_allowed_fields,
)
from lib.element_contracts import validate_element_contract
from lib.error_codes import ToolError
from lib.final_identity import collect_current_artifacts
from lib.font_runtime import validate_font_runtime
from lib.representation_contracts import require_asset, validate_representation_plan
from lib.schema_io import (
    NonStandardJsonNumberError,
    non_finite_number_paths,
    reject_nonstandard_json_number,
)
from lib.schema_contracts import (
    ELEMENT_FIELDS,
    ICON_ITEM_FIELDS,
    ICON_MODULE_FIELDS,
    LOCAL_PICTURE_ITEM_FIELDS,
    PICTURE_FRAMING_MODULE_FIELDS,
    schema_envelope_issues,
    unknown_field_detail,
)
from lib.spec_identity import content_spec_sha256
from lib.visual_review_contracts import visual_review_record_issues


ALLOWED_KINDS = {
    "text",
    "shape",
    "line",
    "table",
    "matrix",
    "status",
    "icon",
    "picture",
    "diagram",
    "chart",
    "special_text",
}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_MODULES = {"page_layout", "typography", "icons", "special_text", "picture_framing", "graphics", "diagram", "chart", "high_risk", "representation_plan", "background"}
VERIFICATION_PROFILES = {"rapid", "reviewed"}
PROFILE_DELIVERY_STATUSES = {
    "rapid": {"pending", "rapid_validated", "rapid_validation_failed"},
    "reviewed": {"pending", "reviewed_passed", "reviewed_failed"},
}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
RGB_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_IMAGE_PIXELS = 100_000_000
COORDINATE_MANIFEST_METADATA_KEY = "coordinate_overlay_manifest_sha256"
PDF_PAGE_SIZE_PT = (960.0, 540.0)
PDF_PAGE_SIZE_TOLERANCE_PT = 1.0
_LOCAL_MODULE_CACHE: dict[str, Any] = {}


def _error(errors: list[dict[str, str]], code: str, path: str, detail: str) -> None:
    errors.append({"code": code, "path": path, "detail": detail})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_typography_allowed_fields(
    value: dict[str, Any],
    allowed: frozenset[str],
    path: str,
    errors: list[dict[str, str]],
) -> None:
    for field in sorted(set(value) - allowed):
        _error(
            errors,
            "SPEC_TYPOGRAPHY_FIELD_UNKNOWN",
            f"{path}.{field}",
            f"unknown field: {field}",
        )


def fixed_font_issues(
    spec: Any,
    font_runtime: Any,
) -> list[dict[str, str]]:
    """Return inconsistent preferred-font declarations.

    A runtime report is optional. When present on the compatibility prebuild
    path, check its resolved family and real Bold face. Otherwise derive the
    preferred family from the first typography item and only enforce consistent
    declarations inside the build specification.
    """
    runtime = (
        validate_font_runtime(font_runtime)
        if font_runtime is not None
        else None
    )
    modules = spec.get("modules") if isinstance(spec, dict) else None
    typography_module = (
        modules.get("typography") if isinstance(modules, dict) else None
    )
    items = (
        typography_module.get("items")
        if isinstance(typography_module, dict)
        else None
    )
    first_selected = (
        items[0].get("selected_font")
        if isinstance(items, list) and items and isinstance(items[0], dict)
        else None
    )
    family = runtime["family"] if runtime is not None else first_selected
    issues: list[dict[str, str]] = []

    if not isinstance(family, str) or not family.strip():
        _error(
            issues,
            "SPEC_PREFERRED_FONT_MISSING",
            "modules.typography.items[0].selected_font",
            "the first typography item must declare a preferred font",
        )
        return issues

    mismatch_code = (
        "SPEC_FIXED_FONT_MISMATCH"
        if runtime is not None
        else "SPEC_PREFERRED_FONT_MISMATCH"
    )

    def mismatch(path: str, value: Any) -> None:
        if isinstance(value, str) and value and value != family:
            _error(
                issues,
                mismatch_code,
                path,
                f"expected {family!r}, got {value!r}",
            )

    def walk(value: Any, path: str, *, typography: bool) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", typography=typography)
            return
        if not isinstance(value, dict):
            return
        for key in (
            "selected_font",
            "internal_font_declaration",
            "bullet_font",
            "font_name",
        ):
            if key in value and (typography or key == "font_name"):
                candidate = value.get(key)
                if key != "bullet_font" or candidate != "follow_text":
                    mismatch(f"{path}.{key}", candidate)
        font = value.get("font")
        if isinstance(font, dict):
            mismatch(f"{path}.font.name", font.get("name"))
            weight = font.get("weight")
            if (
                runtime is not None
                and runtime["bold_available"] is False
                and _is_number(weight)
                and float(weight) >= 600
            ):
                _error(
                    issues,
                    "SPEC_FIXED_FONT_BOLD_UNAVAILABLE",
                    f"{path}.font.weight",
                    f"{family} has no true Bold font face",
                )
        weight = value.get("font_weight")
        if (
            runtime is not None
            and runtime["bold_available"] is False
            and _is_number(weight)
            and float(weight) >= 600
        ):
            _error(
                issues,
                "SPEC_FIXED_FONT_BOLD_UNAVAILABLE",
                f"{path}.font_weight",
                f"{family} has no true Bold font face",
            )
        for key, item in value.items():
            if key != "font":
                walk(item, f"{path}.{key}", typography=typography)

    if isinstance(typography_module, dict):
        walk(typography_module, "modules.typography", typography=True)
        if runtime is not None and isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict) and item.get("fallback_trace") is not None:
                    _error(
                        issues,
                        "SPEC_FIXED_FONT_FALLBACK_TRACE_FORBIDDEN",
                        f"modules.typography.items[{index}].fallback_trace",
                        "fixed-font mode requires null fallback_trace",
                    )
    elements = spec.get("elements") if isinstance(spec, dict) else None
    if isinstance(elements, list):
        for index, element in enumerate(elements):
            if not isinstance(element, dict):
                continue
            for field in ("content", "style"):
                if field in element:
                    walk(
                        element[field],
                        f"elements[{index}].{field}",
                        typography=False,
                    )
    return issues


def preferred_font_from_spec(spec: Any) -> str | None:
    """Return the page's explicit preferred font without consulting runtime."""
    modules = spec.get("modules") if isinstance(spec, dict) else None
    typography = modules.get("typography") if isinstance(modules, dict) else None
    items = typography.get("items") if isinstance(typography, dict) else None
    value = (
        items[0].get("selected_font")
        if isinstance(items, list) and items and isinstance(items[0], dict)
        else None
    )
    return value.strip() if isinstance(value, str) and value.strip() else None


def _pdf_page_size_matches(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(PDF_PAGE_SIZE_PT)
        and all(_is_number(item) and math.isfinite(float(item)) for item in value)
        and all(
            abs(float(actual) - expected) <= PDF_PAGE_SIZE_TOLERANCE_PT
            for actual, expected in zip(value, PDF_PAGE_SIZE_PT)
        )
    )


def _verification_profile(spec: dict[str, Any]) -> str:
    value = spec.get("verification_profile")
    return "rapid" if value is None else value


def _validate_verification_identity(
    spec: dict[str, Any],
    profile: str,
    errors: list[dict[str, str]],
) -> None:
    explicit_profile = spec.get("verification_profile")
    if explicit_profile is not None and explicit_profile not in VERIFICATION_PROFILES:
        _error(
            errors,
            "SPEC_VERIFICATION_PROFILE_INVALID",
            "verification_profile",
            "verification_profile must be rapid or reviewed",
        )
        return
    delivery_status = spec.get("delivery_status")
    if explicit_profile is None and delivery_status is None:
        return
    if delivery_status not in PROFILE_DELIVERY_STATUSES.get(profile, set()):
        _error(
            errors,
            "SPEC_DELIVERY_STATUS_INVALID",
            "delivery_status",
            f"delivery_status is invalid for {profile} verification",
        )


def _valid_size(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_number(item) and item > 0 for item in value)
    )


def _valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(_is_number(item) for item in value)
        and value[2] > 0
        and value[3] > 0
    )


def _canonical_rotation_text(value: int | float) -> str:
    canonical = value % 360
    if float(canonical).is_integer():
        return str(int(canonical))
    return str(canonical)


def _validate_canonical_element_rotation(
    element: dict[str, Any],
    index: int,
    stage: str,
    errors: list[dict[str, str]],
) -> None:
    if stage == "final":
        return
    style = element.get("style")
    if not isinstance(style, dict):
        return
    rotation = style.get("rotation")
    if not _is_number(rotation) or not math.isfinite(float(rotation)):
        return
    if 0 <= rotation < 360:
        return
    _error(
        errors,
        "SPEC_ROTATION_NOT_CANONICAL",
        f"elements[{index}].style.rotation",
        "rotation must use canonical [0, 360) form; "
        f"use {_canonical_rotation_text(rotation)}",
    )


def _slide_bbox_unit_suspect(
    source_bbox: Any,
    slide_bbox: Any,
    canvas: Any,
    kind: Any,
) -> bool:
    if not _valid_bbox(source_bbox) or not _valid_bbox(slide_bbox) or not isinstance(canvas, dict):
        return False
    visual_size = canvas.get("visual_size")
    slide_size = canvas.get("slide_size_emu")
    if not _valid_size(visual_size) or not _valid_size(slide_size):
        return False
    dimensions = (0,) if kind == "line" and source_bbox[2] >= source_bbox[3] else (1,) if kind == "line" else (0, 1)
    for dimension in dimensions:
        source_ratio = source_bbox[dimension + 2] / visual_size[dimension]
        slide_ratio = slide_bbox[dimension + 2] / slide_size[dimension]
        if source_ratio <= 0:
            continue
        relative_scale = slide_ratio / source_ratio
        if relative_scale < 0.05 or relative_scale > 20:
            return True
    return False


def _bbox_in_bounds(bbox: Any, size: Any) -> bool:
    return _valid_bbox(bbox) and _valid_size(size) and bbox[0] >= 0 and bbox[1] >= 0 and bbox[0] + bbox[2] <= size[0] and bbox[1] + bbox[3] <= size[1]


def _bbox_mapping_invalid(source_bbox: Any, slide_bbox: Any, canvas: Any) -> bool:
    if not _valid_bbox(source_bbox) or not _valid_bbox(slide_bbox) or not isinstance(canvas, dict):
        return False
    frame = canvas.get("page_frame_bbox")
    slide_size = canvas.get("slide_size_emu")
    if not _valid_bbox(frame) or not _valid_size(slide_size):
        return False
    source_norm = [
        (source_bbox[0] - frame[0]) / frame[2],
        (source_bbox[1] - frame[1]) / frame[3],
        source_bbox[2] / frame[2],
        source_bbox[3] / frame[3],
    ]
    slide_norm = [
        slide_bbox[0] / slide_size[0],
        slide_bbox[1] / slide_size[1],
        slide_bbox[2] / slide_size[0],
        slide_bbox[3] / slide_size[1],
    ]
    return any(abs(left - right) > 0.01 for left, right in zip(source_norm, slide_norm))


def _validate_reference(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
    expected_size: Any = None,
) -> None:
    if not isinstance(value, dict):
        _error(errors, "SPEC_REFERENCE_INVALID", path, "reference must be an object")
        return
    source_path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(source_path, str) or not source_path or not Path(source_path).is_absolute():
        _error(errors, "SPEC_REFERENCE_PATH_INVALID", f"{path}.path", "path must be absolute")
        return
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        _error(errors, "SPEC_REFERENCE_SHA256_INVALID", f"{path}.sha256", "sha256 must contain 64 hex characters")
        return
    source = Path(source_path).expanduser()
    if source.is_symlink() or not source.is_file():
        _error(errors, "SPEC_REFERENCE_NOT_FOUND", f"{path}.path", "reference must be a readable non-symlink file")
        return
    resolved = source.resolve()
    if _file_sha256(resolved).lower() != digest.lower():
        _error(errors, "SPEC_REFERENCE_HASH_MISMATCH", f"{path}.sha256", "reference sha256 does not match current file")
    if expected_size is None:
        return
    try:
        with Image.open(resolved) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed the supported limit")
            image.load()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        _error(errors, "SPEC_REFERENCE_IMAGE_INVALID", f"{path}.path", "reference must be a decodable image within resource limits")
        return
    if _valid_size(expected_size) and (width, height) != tuple(expected_size):
        _error(
            errors,
            "SPEC_REFERENCE_DIMENSIONS_MISMATCH",
            path,
            f"decoded image size {(width, height)} does not match {tuple(expected_size)}",
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_coordinate_overlay_module() -> Any:
    filename = "create_coordinate_overlay.py"
    cached = _LOCAL_MODULE_CACHE.get(filename)
    if cached is not None:
        return cached
    script_path = Path(__file__).resolve().with_name(filename)
    module_spec = importlib.util.spec_from_file_location(
        f"ia_prebuild_evidence_{script_path.stem}",
        script_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load {script_path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    try:
        module_spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_spec.name, None)
        raise
    _LOCAL_MODULE_CACHE[filename] = module
    return module


def _validate_png_evidence(
    evidence: Any,
    *,
    path: str,
    missing_code: str,
    stale_code: str,
    metadata_key: str,
    errors: list[dict[str, str]],
) -> tuple[Path, str] | None:
    if not isinstance(evidence, dict):
        _error(errors, missing_code, path, "current prebuild visual evidence is required")
        return None
    required = {"path", "sha256", "inspection"}
    if not required.issubset(evidence):
        _error(errors, missing_code, path, "evidence requires path, sha256, and inspection")
        return None
    if evidence.get("inspection") != "passed":
        _error(
            errors,
            "SPEC_PREBUILD_VISUAL_INSPECTION_NOT_PASSED",
            f"{path}.inspection",
            "prebuild visual evidence must be displayed, inspected, and passed",
        )
    evidence_path_value = evidence.get("path")
    evidence_hash = evidence.get("sha256")
    if (
        not isinstance(evidence_path_value, str)
        or not evidence_path_value
        or not Path(evidence_path_value).is_absolute()
        or not isinstance(evidence_hash, str)
        or not SHA256_PATTERN.fullmatch(evidence_hash)
    ):
        _error(errors, stale_code, path, "evidence path and sha256 must be current and absolute")
        return None
    evidence_path = Path(evidence_path_value).expanduser()
    if evidence_path.is_symlink() or not evidence_path.is_file() or evidence_path.suffix.lower() != ".png":
        _error(errors, stale_code, f"{path}.path", "evidence must be a readable non-symlink PNG")
        return None
    resolved = evidence_path.resolve()
    if _file_sha256(resolved).lower() != evidence_hash.lower():
        _error(errors, stale_code, f"{path}.sha256", "evidence sha256 does not match the current PNG")
        return None
    try:
        with Image.open(resolved) as image:
            image.load()
            metadata_value = image.info.get(metadata_key)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        _error(errors, stale_code, f"{path}.path", "evidence must be a decodable PNG")
        return None
    if not isinstance(metadata_value, str) or not SHA256_PATTERN.fullmatch(metadata_value):
        _error(errors, stale_code, path, f"PNG metadata {metadata_key} is missing or invalid")
        return None
    return resolved, metadata_value.lower()


def _validate_coordinate_overlay_evidence(
    page_layout: Any,
    clean_visual_reference: Any,
    errors: list[dict[str, str]],
    *,
    stage: str,
) -> None:
    path = "modules.page_layout.coordinate_overlay_evidence"
    evidence = page_layout.get("coordinate_overlay_evidence") if isinstance(page_layout, dict) else None
    checked = _validate_png_evidence(
        evidence,
        path=path,
        missing_code="SPEC_COORDINATE_OVERLAY_EVIDENCE_MISSING",
        stale_code="SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE",
        metadata_key=COORDINATE_MANIFEST_METADATA_KEY,
        errors=errors,
    )
    if checked is None or not isinstance(evidence, dict):
        return
    source_path = clean_visual_reference.get("path") if isinstance(clean_visual_reference, dict) else None
    source_sha256 = clean_visual_reference.get("sha256") if isinstance(clean_visual_reference, dict) else None
    grid = evidence.get("grid")
    declared_manifest = evidence.get("manifest_sha256")
    if (
        not isinstance(source_path, str)
        or evidence.get("source_sha256") != source_sha256
        or not isinstance(grid, dict)
        or type(grid.get("cols")) is not int
        or type(grid.get("rows")) is not int
        or grid.get("labels") not in {"none", "x", "y", "both"}
        or not isinstance(declared_manifest, str)
        or not SHA256_PATTERN.fullmatch(declared_manifest)
    ):
        _error(errors, "SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE", path, "coordinate evidence binding is incomplete or stale")
        return
    metadata_manifest = checked[1]
    if declared_manifest.lower() != metadata_manifest:
        _error(errors, "SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE", path, "coordinate overlay manifest differs from PNG metadata")
        return
    if stage == "final":
        return
    try:
        coordinate_module = _load_coordinate_overlay_module()
        expected = coordinate_module.coordinate_overlay_manifest(
            source_path,
            cols=grid["cols"],
            rows=grid["rows"],
            labels=grid["labels"],
        )[COORDINATE_MANIFEST_METADATA_KEY]
    except (OSError, ValueError, UnidentifiedImageError, RuntimeError):
        _error(errors, "SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE", path, "cannot recompute coordinate overlay manifest")
        return
    if declared_manifest.lower() != expected.lower() or metadata_manifest != expected.lower():
        _error(errors, "SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE", path, "coordinate overlay does not bind the current source and grid")


def _module_element_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "element_id" and isinstance(child, str):
                references.add(child)
            elif key == "element_ids" and isinstance(child, list):
                references.update(item for item in child if isinstance(item, str))
            else:
                references.update(_module_element_references(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_module_element_references(child))
    return references


def _validate_coverage(
    items: Any,
    text: str,
    path: str,
    code: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(items, list) or not items:
        _error(errors, code, path, "segments must be a non-empty array")
        return
    cursor = 0
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _error(errors, code, item_path, "segment must be an object")
            continue
        start = item.get("start")
        end = item.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start != cursor or end <= start:
            _error(errors, code, item_path, f"expected continuous range beginning at {cursor}")
            return
        cursor = end
    if cursor != len(text):
        _error(errors, code, path, f"segments end at {cursor}, text length is {len(text)}")


def _validate_text_run_styles(
    runs: Any,
    path: str,
    stage: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(runs, list):
        return
    required = {"font_size", "font_weight", "color", "letter_spacing"}
    for index, run in enumerate(runs):
        run_path = f"{path}[{index}]"
        if not isinstance(run, dict):
            continue
        _validate_typography_allowed_fields(
            run,
            text_run_allowed_fields(stage, run),
            run_path,
            errors,
        )
        modern_present = TEXT_RUN_MODERN_FIELDS & set(run)
        required_for_run = set(required)
        if stage == "prebuild" or modern_present:
            required_for_run.update(TEXT_RUN_MODERN_FIELDS)
        elif "decoration" not in run:
            required_for_run.add("decoration")
        missing = sorted(required_for_run - set(run))
        font_size = run.get("font_size")
        font_weight = run.get("font_weight")
        color = run.get("color")
        letter_spacing = run.get("letter_spacing")
        baseline = run.get("baseline")
        modern_invalid = (
            any(type(run.get(field)) is not bool for field in ("italic", "underline", "strike"))
            or type(baseline) is not int
            or not TEXT_RUN_BASELINE_MIN <= baseline <= TEXT_RUN_BASELINE_MAX
        ) if stage == "prebuild" or modern_present else False
        legacy_invalid = (
            stage != "prebuild"
            and not modern_present
            and (not isinstance(run.get("decoration"), str) or not run["decoration"])
        )
        if (
            missing
            or not _is_number(font_size)
            or font_size <= 0
            or not _is_number(font_weight)
            or not 1 <= font_weight <= 1000
            or not isinstance(color, str)
            or not color
            or not _is_number(letter_spacing)
            or modern_invalid
            or legacy_invalid
        ):
            detail = f"missing fields: {', '.join(missing)}" if missing else "invalid run style values"
            _error(errors, "SPEC_TEXT_RUN_STYLE_INVALID", run_path, detail)


def _validate_paragraphs(
    paragraphs: Any,
    text: str,
    text_box: Any,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    _validate_coverage(
        paragraphs,
        text,
        path,
        "SPEC_PARAGRAPH_COVERAGE_INVALID",
        errors,
    )
    if not isinstance(paragraphs, list) or not paragraphs:
        return

    for index, paragraph in enumerate(paragraphs):
        paragraph_path = f"{path}[{index}]"
        if not isinstance(paragraph, dict):
            continue
        _validate_typography_allowed_fields(
            paragraph,
            TEXT_CONTRACT_ALLOWED_FIELDS["paragraph"],
            paragraph_path,
            errors,
        )
        list_contract = paragraph.get("list")
        if not isinstance(list_contract, dict) or not isinstance(list_contract.get("is_list"), bool):
            _error(
                errors,
                "SPEC_PARAGRAPH_LIST_INVALID",
                f"{paragraph_path}.list",
                "list must be an object with boolean is_list",
            )
            continue
        _validate_typography_allowed_fields(
            list_contract,
            TEXT_CONTRACT_ALLOWED_FIELDS["list"],
            f"{paragraph_path}.list",
            errors,
        )
        level = list_contract.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or level < 0:
            _error(
                errors,
                "SPEC_PARAGRAPH_LIST_INVALID",
                f"{paragraph_path}.list.level",
                "level must be a non-negative integer",
            )
        if not list_contract["is_list"]:
            if list_contract.get("bullet") is not None:
                _error(
                    errors,
                    "SPEC_PARAGRAPH_LIST_INVALID",
                    f"{paragraph_path}.list.bullet",
                    "non-list paragraph bullet must be null",
                )
            if list_contract.get("bullet_asset") is not None:
                _error(
                    errors,
                    "SPEC_PARAGRAPH_LIST_INVALID",
                    f"{paragraph_path}.list.bullet_asset",
                    "non-list paragraph bullet_asset must be null or absent",
                )
            continue

        required = {
            "is_list",
            "level",
            "bullet_type",
            "bullet",
            "bullet_font",
            "bullet_size_mode",
            "bullet_size_value",
            "bullet_color",
        }
        missing = sorted(required - set(list_contract))
        bullet_type = list_contract.get("bullet_type")
        bullet = list_contract.get("bullet")
        bullet_font = list_contract.get("bullet_font")
        size_mode = list_contract.get("bullet_size_mode")
        size_value = list_contract.get("bullet_size_value")
        bullet_color = list_contract.get("bullet_color")
        contract_invalid = bool(missing)
        contract_invalid = contract_invalid or bullet_type not in {"char", "auto_number", "picture"}
        contract_invalid = contract_invalid or not isinstance(bullet, str) or not bullet
        contract_invalid = contract_invalid or not isinstance(bullet_font, str) or not bullet_font
        contract_invalid = contract_invalid or size_mode not in {"follow_text", "percent", "points"}
        contract_invalid = contract_invalid or (
            size_mode == "follow_text" and size_value is not None
        )
        contract_invalid = contract_invalid or (
            size_mode in {"percent", "points"}
            and (not _is_number(size_value) or size_value <= 0)
        )
        contract_invalid = contract_invalid or not isinstance(bullet_color, str) or not bullet_color
        contract_invalid = contract_invalid or (
            isinstance(bullet_color, str)
            and bullet_color != "follow_text"
            and not RGB_PATTERN.fullmatch(bullet_color)
        )
        if contract_invalid:
            detail = f"missing fields: {', '.join(missing)}" if missing else "invalid native bullet fields"
            _error(
                errors,
                "SPEC_NATIVE_LIST_CONTRACT_INVALID",
                f"{paragraph_path}.list",
                detail,
            )
        if bullet_type == "picture":
            if (
                bullet != "blip"
                or bullet_font != "follow_text"
                or bullet_color != "follow_text"
            ):
                _error(
                    errors,
                    "SPEC_NATIVE_LIST_CONTRACT_INVALID",
                    f"{paragraph_path}.list",
                    "picture bullet requires blip identity and follow_text font/color",
                )
            asset_path = f"{paragraph_path}.list.bullet_asset"
            try:
                resolved_asset, _, _ = require_asset(
                    list_contract.get("bullet_asset"), asset_path
                )
            except ToolError as exc:
                _error(errors, exc.code, exc.path, exc.detail)
            else:
                if resolved_asset.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                    _error(
                        errors,
                        "SPEC_PICTURE_BULLET_FORMAT_UNSUPPORTED",
                        f"{asset_path}.path",
                        "picture bullet must be PNG or JPEG",
                    )
        elif list_contract.get("bullet_asset") is not None:
            _error(
                errors,
                "SPEC_NATIVE_LIST_CONTRACT_INVALID",
                f"{paragraph_path}.list.bullet_asset",
                "bullet_asset is allowed only for picture bullets",
            )
        if not _is_number(paragraph.get("margin_left")) or not _is_number(paragraph.get("indent")):
            _error(
                errors,
                "SPEC_NATIVE_LIST_INDENT_INVALID",
                paragraph_path,
                "native list paragraph requires numeric margin_left and indent in EMU",
            )

    expected_breaks = [
        paragraph.get("end")
        for paragraph in paragraphs[:-1]
        if isinstance(paragraph, dict)
    ]
    actual_breaks = text_box.get("paragraph_breaks") if isinstance(text_box, dict) else None
    if actual_breaks != expected_breaks:
        _error(
            errors,
            "SPEC_PARAGRAPH_BREAKS_INVALID",
            f"{path.rsplit('.', 1)[0]}.text_box.paragraph_breaks",
            f"expected {expected_breaks!r}",
        )
        return

    newline_spans = [match.span() for match in re.finditer(r"[\r\n]+", text)]
    for index, paragraph_break in enumerate(actual_breaks):
        if not isinstance(paragraph_break, int) or isinstance(paragraph_break, bool):
            continue
        conflict = next(
            (
                (start, end)
                for start, end in newline_spans
                if start <= paragraph_break <= end
            ),
            None,
        )
        if conflict is None:
            continue
        start, end = conflict
        _error(
            errors,
            "SPEC_PARAGRAPH_BREAK_ENCODING_CONFLICT",
            f"{path.rsplit('.', 1)[0]}.text_box.paragraph_breaks[{index}]",
            f"paragraph boundary {paragraph_break} duplicates text newline [{start}, {end})",
                )


def _validate_source_text_layout(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(value, dict):
        _error(
            errors,
            "SPEC_SOURCE_TEXT_LAYOUT_INVALID",
            path,
            "source_layout must be an object",
        )
        return
    _validate_typography_allowed_fields(
        value,
        TEXT_CONTRACT_ALLOWED_FIELDS["source_layout"],
        path,
        errors,
    )
    required = TEXT_CONTRACT_ALLOWED_FIELDS["source_layout"]
    missing = sorted(required - set(value))
    if missing:
        _error(
            errors,
            "SPEC_SOURCE_TEXT_LAYOUT_INVALID",
            path,
            f"missing fields: {', '.join(missing)}",
        )
        return
    distances = value.get("line_center_distances_pt")
    if not isinstance(distances, list) or any(
        not _is_number(item) or item <= 0 for item in distances
    ):
        _error(
            errors,
            "SPEC_SOURCE_TEXT_LAYOUT_INVALID",
            f"{path}.line_center_distances_pt",
            "line center distances must be positive point values",
        )
    center_offset = value.get("text_block_center_offset_y_pt")
    if not _is_number(center_offset):
        _error(
            errors,
            "SPEC_SOURCE_TEXT_LAYOUT_INVALID",
            f"{path}.text_block_center_offset_y_pt",
            "text block center offset must be a point value",
        )


def _validate_typography(
    module: Any,
    element_map: dict[str, dict[str, Any]],
    canvas: Any,
    stage: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(module, dict):
        _error(errors, "SPEC_MODULE_INVALID", "modules.typography", "module must be an object")
        return
    if module.get("slide_coordinate_unit") != "EMU":
        _error(
            errors,
            "SPEC_TYPOGRAPHY_UNIT_INVALID",
            "modules.typography.slide_coordinate_unit",
            "typography coordinates must use EMU",
        )
    items = module.get("items")
    if not isinstance(items, list) or not items:
        _error(errors, "SPEC_TYPOGRAPHY_ITEMS_INVALID", "modules.typography.items", "items must be non-empty")
        return
    seen: set[str] = set()
    required = {
        "element_id",
        "text",
        "source_font_guess",
        "selected_font",
        "fallback_reason",
        "fallback_trace",
        "runs",
        "paragraphs",
        "text_box",
        "internal_font_declaration",
        "font_declaration_verified",
    }
    for index, item in enumerate(items):
        path = f"modules.typography.items[{index}]"
        if not isinstance(item, dict):
            _error(errors, "SPEC_TYPOGRAPHY_ITEM_INVALID", path, "item must be an object")
            continue
        _validate_typography_allowed_fields(
            item,
            TEXT_CONTRACT_ALLOWED_FIELDS["item"],
            path,
            errors,
        )
        missing = sorted(required - set(item))
        if missing:
            _error(errors, "SPEC_TYPOGRAPHY_FIELD_MISSING", path, f"missing fields: {', '.join(missing)}")
            continue
        element_id = item.get("element_id")
        if not isinstance(element_id, str) or element_id not in element_map:
            _error(errors, "SPEC_ELEMENT_REFERENCE_INVALID", f"{path}.element_id", "unknown element_id")
        elif element_id in seen:
            _error(errors, "SPEC_TYPOGRAPHY_ELEMENT_DUPLICATE", f"{path}.element_id", element_id)
        else:
            seen.add(element_id)
        text = item.get("text")
        if not isinstance(text, str) or not text:
            _error(errors, "SPEC_TYPOGRAPHY_TEXT_INVALID", f"{path}.text", "text must be non-empty")
            continue
        removed_fields = {
            "candidates",
            "candidate_trials",
            "render_metrics",
            "font_trial_report",
        }
        for field in sorted(removed_fields.intersection(item)):
            _error(
                errors,
                "SPEC_REMOVED_FONT_WORKFLOW_FIELD",
                f"{path}.{field}",
                f"{field} was removed from the single-font typography workflow",
            )
        selected = item.get("selected_font")
        if not isinstance(selected, str) or not selected.strip():
            _error(
                errors,
                "SPEC_SELECTED_FONT_INVALID",
                f"{path}.selected_font",
                "selected_font must be a non-empty font family",
            )
        runs = item.get("runs")
        _validate_coverage(runs, text, f"{path}.runs", "SPEC_TEXT_RUN_COVERAGE_INVALID", errors)
        _validate_text_run_styles(runs, f"{path}.runs", stage, errors)
        text_box = item.get("text_box")
        if isinstance(text_box, dict):
            _validate_typography_allowed_fields(
                text_box,
                TEXT_CONTRACT_ALLOWED_FIELDS["text_box"],
                f"{path}.text_box",
                errors,
            )
        _validate_paragraphs(
            item.get("paragraphs"),
            text,
            text_box,
            f"{path}.paragraphs",
            errors,
        )
        if "source_layout" in item:
            _validate_source_text_layout(
                item.get("source_layout"),
                f"{path}.source_layout",
                errors,
            )
        if not isinstance(text_box, dict) or not all(
            _is_number(text_box.get(key)) and (text_box[key] > 0 if key in {"w", "h"} else True)
            for key in ("x", "y", "w", "h")
        ):
            _error(errors, "SPEC_TEXT_BOX_INVALID", f"{path}.text_box", "x/y/w/h must be numeric and w/h positive")
        elif isinstance(element_id, str) and element_id in element_map:
            expected = element_map[element_id].get("slide_bbox")
            actual = [text_box.get(key) for key in ("x", "y", "w", "h")]
            slide_size = canvas.get("slide_size_emu") if isinstance(canvas, dict) else None
            if _valid_bbox(expected) and _valid_bbox(actual) and _valid_size(slide_size):
                if any(abs(a - e) / slide_size[index % 2] > 0.01 for index, (a, e) in enumerate(zip(actual, expected))):
                    _error(errors, "SPEC_TEXT_BOX_MAPPING_INVALID", f"{path}.text_box", "text_box must match its element EMU bbox")
        if not isinstance(item.get("font_declaration_verified"), bool):
            _error(errors, "SPEC_FONT_VERIFICATION_INVALID", f"{path}.font_declaration_verified", "must be boolean")
        elif stage == "final" and not item["font_declaration_verified"]:
            _error(errors, "SPEC_FONT_NOT_VERIFIED", f"{path}.font_declaration_verified", "final spec requires verified font declaration")


def _validate_icons(
    module: Any,
    element_map: dict[str, dict[str, Any]],
    canvas: Any,
    clean_visual_reference: Any,
    page_id: Any,
    stage: str,
    errors: list[dict[str, str]],
) -> None:
    """Validate the narrow icon-crop contract used by this reconstruction skill."""
    if not isinstance(module, dict):
        _error(errors, "SPEC_MODULE_INVALID", "modules.icons", "module must be an object")
        return
    required_module = ICON_MODULE_FIELDS
    module_unknown = unknown_field_detail("IconsModule", module)
    if module_unknown is not None:
        _error(errors, "UNSUPPORTED_CAPABILITY", "modules.icons", module_unknown)
        return
    missing_module = sorted(required_module - set(module))
    if missing_module:
        _error(errors, "SPEC_ICONS_FIELD_MISSING", "modules.icons", f"missing fields: {', '.join(missing_module)}")
        return
    if module.get("schema_version") != 2:
        _error(errors, "SPEC_ICONS_SCHEMA_VERSION_INVALID", "modules.icons.schema_version", "expected schema_version 2")
    if module.get("page_id") != page_id:
        _error(errors, "SPEC_ICONS_PAGE_ID_INVALID", "modules.icons.page_id", "must match page_id")
    if module.get("slide_coordinate_unit") != "EMU":
        _error(errors, "SPEC_ICONS_UNIT_INVALID", "modules.icons.slide_coordinate_unit", "icon slide coordinates must use EMU")

    expected_reference_path = clean_visual_reference.get("path") if isinstance(clean_visual_reference, dict) else None
    expected_reference_hash = clean_visual_reference.get("sha256") if isinstance(clean_visual_reference, dict) else None
    if module.get("clean_visual_reference") != expected_reference_path:
        _error(errors, "SPEC_ICONS_REFERENCE_INVALID", "modules.icons.clean_visual_reference", "must match clean_visual_reference.path")
    if module.get("clean_visual_sha256") != expected_reference_hash:
        _error(errors, "SPEC_ICONS_REFERENCE_INVALID", "modules.icons.clean_visual_sha256", "must match clean_visual_reference.sha256")

    icons = module.get("icons")
    if not isinstance(icons, list) or not icons:
        _error(errors, "SPEC_ICONS_ITEMS_INVALID", "modules.icons.icons", "icons must be a non-empty array")
        return

    icon_element_ids = {element_id for element_id, element in element_map.items() if element.get("kind") == "icon"}
    seen_icon_ids: set[str] = set()
    seen_element_ids: set[str] = set()
    required_item = ICON_ITEM_FIELDS
    visual_size = canvas.get("visual_size") if isinstance(canvas, dict) else None
    for index, item in enumerate(icons):
        path = f"modules.icons.icons[{index}]"
        if not isinstance(item, dict):
            _error(errors, "SPEC_ICON_ITEM_INVALID", path, "icon must be an object")
            continue
        item_unknown = unknown_field_detail("IconItem", item)
        if item_unknown is not None:
            element_id = item.get("element_id")
            contract_path = (
                f"modules.icons.icons.{element_id}"
                if isinstance(element_id, str) and element_id
                else path
            )
            _error(errors, "UNSUPPORTED_CAPABILITY", contract_path, item_unknown)
            continue
        missing_item = sorted(required_item - set(item))
        if missing_item:
            _error(errors, "SPEC_ICON_FIELD_MISSING", path, f"missing fields: {', '.join(missing_item)}")
            continue

        icon_id = item.get("icon_id")
        if not isinstance(icon_id, str) or not icon_id or icon_id in seen_icon_ids:
            _error(errors, "SPEC_ICON_ID_INVALID", f"{path}.icon_id", "icon_id must be unique and non-empty")
        else:
            seen_icon_ids.add(icon_id)
        element_id = item.get("element_id")
        if not isinstance(element_id, str) or element_id not in icon_element_ids:
            _error(errors, "SPEC_ICON_ELEMENT_REFERENCE_INVALID", f"{path}.element_id", "must reference an icon element")
        elif element_id in seen_element_ids:
            _error(errors, "SPEC_ICON_ELEMENT_DUPLICATE", f"{path}.element_id", "icon element may appear once")
        else:
            seen_element_ids.add(element_id)

        if not isinstance(item.get("category"), str) or not item["category"]:
            _error(errors, "SPEC_ICON_CATEGORY_INVALID", f"{path}.category", "category must be non-empty")
        if not isinstance(item.get("instance_count"), int) or item["instance_count"] <= 0:
            _error(errors, "SPEC_ICON_INSTANCE_COUNT_INVALID", f"{path}.instance_count", "must be a positive integer")
        if item.get("repeat_group") is not None and (not isinstance(item.get("repeat_group"), str) or not item["repeat_group"]):
            _error(errors, "SPEC_ICON_REPEAT_GROUP_INVALID", f"{path}.repeat_group", "must be a non-empty string or null")
        if item.get("semantic_scope") not in {"icon_only", "intentional_composite"}:
            _error(errors, "SPEC_ICON_SEMANTIC_SCOPE_INVALID", f"{path}.semantic_scope", "must be icon_only or intentional_composite")

        source_bbox = item.get("source_bbox")
        slide_bbox = item.get("slide_bbox")
        if not _valid_bbox(source_bbox) or not _valid_bbox(slide_bbox):
            _error(errors, "SPEC_ICON_BBOX_INVALID", path, "source_bbox and slide_bbox must be valid")
        else:
            if not _bbox_in_bounds(source_bbox, visual_size):
                _error(errors, "SPEC_ICON_BBOX_OUT_OF_BOUNDS", f"{path}.source_bbox", "source bbox exceeds visual canvas")
            if isinstance(element_id, str) and element_id in element_map:
                element = element_map[element_id]
                if source_bbox != element.get("source_bbox") or slide_bbox != element.get("slide_bbox"):
                    _error(errors, "SPEC_ICON_ELEMENT_MAPPING_INVALID", path, "icon bboxes must match the referenced element")
                if item.get("layer") != element.get("layer"):
                    _error(errors, "SPEC_ICON_ELEMENT_MAPPING_INVALID", f"{path}.layer", "layer must match the referenced element")
        if not isinstance(item.get("layer"), int) or item["layer"] <= 0:
            _error(errors, "SPEC_ICON_LAYER_INVALID", f"{path}.layer", "must be a positive integer")

        padding = item.get("padding")
        if not isinstance(padding, int) or padding < 0:
            _error(errors, "SPEC_ICON_PADDING_INVALID", f"{path}.padding", "must be a non-negative integer")
        elif _valid_bbox(source_bbox) and _valid_size(visual_size):
            crop_bounds = [source_bbox[0] - padding, source_bbox[1] - padding, source_bbox[2] + padding * 2, source_bbox[3] + padding * 2]
            if not _bbox_in_bounds(crop_bounds, visual_size):
                _error(errors, "SPEC_ICON_CROP_OUT_OF_BOUNDS", f"{path}.padding", "crop bbox plus padding exceeds visual canvas")

        if item.get("source_path") != expected_reference_path or item.get("source_sha256") != expected_reference_hash:
            _error(errors, "SPEC_ICON_SOURCE_BINDING_INVALID", path, "source must exactly bind to clean_visual_reference")
        source_path = Path(item["source_path"]).expanduser() if isinstance(item.get("source_path"), str) else None
        if source_path is None or not source_path.is_absolute() or source_path.is_symlink() or not source_path.is_file():
            _error(errors, "SPEC_ICON_SOURCE_INVALID", f"{path}.source_path", "source must be a readable non-symlink file")
        elif isinstance(item.get("source_sha256"), str) and SHA256_PATTERN.fullmatch(item["source_sha256"]):
            if _file_sha256(source_path.resolve()).lower() != item["source_sha256"].lower():
                _error(errors, "SPEC_ICON_SOURCE_HASH_MISMATCH", f"{path}.source_sha256", "source hash does not match current file")

        crop_mode = item.get("crop_mode")
        if crop_mode != "alpha_isolation":
            _error(
                errors,
                "SPEC_ICON_CROP_MODE_INVALID",
                f"{path}.crop_mode",
                "must be alpha_isolation",
            )
        alpha_hash = item.get("alpha_mask_sha256")
        if not isinstance(item.get("background_handling"), str) or not item["background_handling"]:
            _error(errors, "SPEC_ICON_BACKGROUND_HANDLING_INVALID", f"{path}.background_handling", "must be non-empty")
        if not isinstance(alpha_hash, str) or not SHA256_PATTERN.fullmatch(alpha_hash):
            _error(errors, "SPEC_ICON_ALPHA_MASK_INVALID", f"{path}.alpha_mask_sha256", "alpha isolation requires alpha_mask_sha256")

        asset_path = Path(item["asset_path"]).expanduser() if isinstance(item.get("asset_path"), str) else None
        if asset_path is None or not asset_path.is_absolute() or asset_path.is_symlink() or not asset_path.is_file():
            _error(errors, "SPEC_ICON_ASSET_INVALID", f"{path}.asset_path", "asset must be a readable non-symlink file")
        else:
            resolved_asset = asset_path.resolve()
            if resolved_asset.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                _error(errors, "SPEC_ICON_ASSET_INVALID", f"{path}.asset_path", "asset must be PNG, JPEG, or WEBP")
            if (
                resolved_asset.parent.name != "icons"
                or resolved_asset.parent.parent.name != "assets"
            ):
                _error(
                    errors,
                    "SPEC_ICON_ASSET_LOCATION_INVALID",
                    f"{path}.asset_path",
                    "asset parent must be assets/icons",
                )
            asset_hash = item.get("asset_sha256")
            if not isinstance(asset_hash, str) or not SHA256_PATTERN.fullmatch(asset_hash):
                _error(errors, "SPEC_ICON_ASSET_HASH_INVALID", f"{path}.asset_sha256", "asset sha256 must contain 64 hex characters")
            elif _file_sha256(resolved_asset).lower() != asset_hash.lower():
                _error(errors, "SPEC_ICON_ASSET_HASH_MISMATCH", f"{path}.asset_sha256", "asset hash does not match current file")
            if resolved_asset.suffix.lower() != ".png":
                _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon asset must be a PNG")
            elif crop_mode == "alpha_isolation":
                try:
                    with Image.open(resolved_asset) as image:
                        image.load()
                        declared_width = item.get("final_width")
                        declared_height = item.get("final_height")
                        if (
                            isinstance(declared_width, int)
                            and not isinstance(declared_width, bool)
                            and isinstance(declared_height, int)
                            and not isinstance(declared_height, bool)
                            and image.size != (declared_width, declared_height)
                        ):
                            _error(
                                errors,
                                "SPEC_ICON_ASSET_DIMENSIONS_INVALID",
                                path,
                                "decoded asset dimensions must match final_width/final_height",
                            )
                        if image.mode != "RGBA":
                            _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon PNG must use RGBA mode")
                        else:
                            alpha = image.getchannel("A")
                            minimum, maximum = alpha.getextrema()
                            if minimum != 0 or maximum == 0:
                                _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon alpha must contain transparent background and visible foreground")
                            actual_alpha_hash = hashlib.sha256(alpha.tobytes()).hexdigest()
                            if isinstance(alpha_hash, str) and SHA256_PATTERN.fullmatch(alpha_hash) and actual_alpha_hash.lower() != alpha_hash.lower():
                                _error(errors, "SPEC_ICON_ALPHA_MASK_MISMATCH", f"{path}.alpha_mask_sha256", "alpha mask hash does not match the current icon asset")
                            foreground = alpha.getbbox()
                            if foreground is not None and (
                                foreground[0] == 0
                                or foreground[1] == 0
                                or foreground[2] == image.width
                                or foreground[3] == image.height
                            ):
                                _error(errors, "SPEC_ICON_FOREGROUND_TOUCHES_EDGE", f"{path}.asset_path", "visible icon pixels must not touch the crop boundary")
                        if (
                            source_path is not None
                            and source_path.is_absolute()
                            and source_path.is_file()
                            and _valid_bbox(source_bbox)
                            and isinstance(padding, int)
                            and padding >= 0
                        ):
                            with Image.open(source_path.resolve()) as source_image:
                                source_image.load()
                                left = source_bbox[0] - padding
                                top = source_bbox[1] - padding
                                right = source_bbox[0] + source_bbox[2] + padding
                                bottom = source_bbox[1] + source_bbox[3] + padding
                                source_crop = source_image.convert("RGB").crop(
                                    (left, top, right, bottom)
                                )
                            asset_rgb = image.convert("RGB")
                            if (
                                source_crop.size == asset_rgb.size
                                and source_crop.tobytes() != asset_rgb.tobytes()
                            ):
                                _error(
                                    errors,
                                    "SPEC_ICON_RGB_MISMATCH",
                                    f"{path}.asset_path",
                                    "asset RGB pixels must exactly match the bound source crop",
                                )
                except (OSError, UnidentifiedImageError):
                    _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon asset is not a readable PNG")

        final_width = item.get("final_width")
        final_height = item.get("final_height")
        if not isinstance(final_width, int) or final_width <= 0 or not isinstance(final_height, int) or final_height <= 0:
            _error(errors, "SPEC_ICON_ASSET_DIMENSIONS_INVALID", path, "final_width and final_height must be positive integers")
        elif _valid_bbox(source_bbox) and isinstance(padding, int) and padding >= 0:
            expected_width = source_bbox[2] + padding * 2
            expected_height = source_bbox[3] + padding * 2
            if final_width != expected_width or final_height != expected_height:
                _error(errors, "SPEC_ICON_ASSET_DIMENSIONS_INVALID", path, "asset dimensions must equal crop bbox plus padding")
        if not isinstance(item.get("sharpness"), str) or not item["sharpness"]:
            _error(errors, "SPEC_ICON_SHARPNESS_INVALID", f"{path}.sharpness", "must be non-empty")

        if item.get("validation") != "passed":
            _error(errors, "SPEC_ICON_VALIDATION_INVALID", f"{path}.validation", "must be passed")
        if item.get("native_redraw") is not False:
            _error(errors, "SPEC_ICON_NATIVE_REDRAW_INVALID", f"{path}.native_redraw", "must be false")
        if not isinstance(item.get("selectable_picture_verified"), bool):
            _error(errors, "SPEC_ICON_SELECTABILITY_INVALID", f"{path}.selectable_picture_verified", "must be boolean")
        elif stage == "final" and not item["selectable_picture_verified"]:
            _error(errors, "SPEC_ICON_SELECTABILITY_NOT_VERIFIED", f"{path}.selectable_picture_verified", "final spec requires independently selectable picture")
        if item.get("object_type") != "picture":
            _error(errors, "SPEC_ICON_OBJECT_TYPE_INVALID", f"{path}.object_type", "must be picture")

    missing_elements = sorted(icon_element_ids - seen_element_ids)
    if missing_elements:
        _error(errors, "SPEC_ICON_ELEMENT_MISSING", "modules.icons.icons", f"missing icon records: {', '.join(missing_elements)}")


def _validate_picture_framing(
    module: Any,
    element_map: dict[str, dict[str, Any]],
    canvas: Any,
    clean_visual_reference: Any,
    page_id: Any,
    stage: str,
    errors: list[dict[str, str]],
) -> None:
    """Validate seeded transparent assets for non-icon complex decorations."""
    module_path = "modules.picture_framing"
    if not isinstance(module, dict):
        _error(errors, "SPEC_MODULE_INVALID", module_path, "module must be an object")
        return
    module_unknown = unknown_field_detail("PictureFramingModule", module)
    if module_unknown is not None:
        _error(errors, "UNSUPPORTED_CAPABILITY", module_path, module_unknown)
        return
    missing_module = sorted(PICTURE_FRAMING_MODULE_FIELDS - set(module))
    if missing_module:
        _error(
            errors,
            "SPEC_LOCAL_PICTURE_FIELD_MISSING",
            module_path,
            f"missing fields: {', '.join(missing_module)}",
        )
        return
    if module.get("schema_version") != 2:
        _error(errors, "SPEC_LOCAL_PICTURE_SCHEMA_VERSION_INVALID", f"{module_path}.schema_version", "expected schema_version 2")
    if module.get("page_id") != page_id:
        _error(errors, "SPEC_LOCAL_PICTURE_PAGE_ID_INVALID", f"{module_path}.page_id", "must match page_id")
    if module.get("slide_coordinate_unit") != "EMU":
        _error(errors, "SPEC_LOCAL_PICTURE_UNIT_INVALID", f"{module_path}.slide_coordinate_unit", "picture slide coordinates must use EMU")

    expected_reference_path = clean_visual_reference.get("path") if isinstance(clean_visual_reference, dict) else None
    expected_reference_hash = clean_visual_reference.get("sha256") if isinstance(clean_visual_reference, dict) else None
    if module.get("clean_visual_reference") != expected_reference_path:
        _error(errors, "SPEC_LOCAL_PICTURE_REFERENCE_INVALID", f"{module_path}.clean_visual_reference", "must match clean_visual_reference.path")
    if module.get("clean_visual_sha256") != expected_reference_hash:
        _error(errors, "SPEC_LOCAL_PICTURE_REFERENCE_INVALID", f"{module_path}.clean_visual_sha256", "must match clean_visual_reference.sha256")

    pictures = module.get("pictures")
    if not isinstance(pictures, list) or not pictures:
        _error(errors, "SPEC_LOCAL_PICTURE_ITEMS_INVALID", f"{module_path}.pictures", "pictures must be a non-empty array")
        return

    visual_size = canvas.get("visual_size") if isinstance(canvas, dict) else None
    seen_picture_ids: set[str] = set()
    seen_element_ids: set[str] = set()
    for index, item in enumerate(pictures):
        path = f"{module_path}.pictures[{index}]"
        if not isinstance(item, dict):
            _error(errors, "SPEC_LOCAL_PICTURE_ITEM_INVALID", path, "picture record must be an object")
            continue
        item_unknown = unknown_field_detail("LocalPictureItem", item)
        if item_unknown is not None:
            _error(errors, "UNSUPPORTED_CAPABILITY", path, item_unknown)
            continue
        missing_item = sorted(LOCAL_PICTURE_ITEM_FIELDS - set(item))
        if missing_item:
            _error(errors, "SPEC_LOCAL_PICTURE_FIELD_MISSING", path, f"missing fields: {', '.join(missing_item)}")
            continue

        picture_id = item.get("picture_id")
        if not isinstance(picture_id, str) or not picture_id or picture_id in seen_picture_ids:
            _error(errors, "SPEC_LOCAL_PICTURE_ID_INVALID", f"{path}.picture_id", "picture_id must be unique and non-empty")
        else:
            seen_picture_ids.add(picture_id)
        element_id = item.get("element_id")
        element = element_map.get(element_id) if isinstance(element_id, str) else None
        if not isinstance(element, dict) or element.get("kind") != "picture":
            _error(errors, "SPEC_LOCAL_PICTURE_ELEMENT_REFERENCE_INVALID", f"{path}.element_id", "must reference a picture element")
        elif element_id in seen_element_ids:
            _error(errors, "SPEC_LOCAL_PICTURE_ELEMENT_DUPLICATE", f"{path}.element_id", "picture element may appear once")
        else:
            seen_element_ids.add(element_id)

        if not isinstance(item.get("semantic_role"), str) or not item["semantic_role"]:
            _error(errors, "SPEC_LOCAL_PICTURE_ROLE_INVALID", f"{path}.semantic_role", "must be non-empty")
        source_bbox = item.get("source_bbox")
        slide_bbox = item.get("slide_bbox")
        if not _valid_bbox(source_bbox) or not _valid_bbox(slide_bbox):
            _error(errors, "SPEC_LOCAL_PICTURE_BBOX_INVALID", path, "source_bbox and slide_bbox must be valid")
        else:
            if not _bbox_in_bounds(source_bbox, visual_size):
                _error(errors, "SPEC_LOCAL_PICTURE_BBOX_OUT_OF_BOUNDS", f"{path}.source_bbox", "source bbox exceeds visual canvas")
            if isinstance(element, dict):
                if source_bbox != element.get("source_bbox") or slide_bbox != element.get("slide_bbox"):
                    _error(errors, "SPEC_LOCAL_PICTURE_ELEMENT_MAPPING_INVALID", path, "picture bboxes must match the referenced element")
                if item.get("layer") != element.get("layer"):
                    _error(errors, "SPEC_LOCAL_PICTURE_ELEMENT_MAPPING_INVALID", f"{path}.layer", "layer must match the referenced element")
        if not isinstance(item.get("layer"), int) or isinstance(item.get("layer"), bool) or item["layer"] <= 0:
            _error(errors, "SPEC_LOCAL_PICTURE_LAYER_INVALID", f"{path}.layer", "must be a positive integer")

        if item.get("source_path") != expected_reference_path or item.get("source_sha256") != expected_reference_hash:
            _error(errors, "SPEC_LOCAL_PICTURE_SOURCE_BINDING_INVALID", path, "source must exactly bind to clean_visual_reference")
        source_path = Path(item["source_path"]).expanduser() if isinstance(item.get("source_path"), str) else None
        if source_path is None or not source_path.is_absolute() or source_path.is_symlink() or not source_path.is_file():
            _error(errors, "SPEC_LOCAL_PICTURE_SOURCE_INVALID", f"{path}.source_path", "source must be a readable non-symlink file")
        elif isinstance(item.get("source_sha256"), str) and SHA256_PATTERN.fullmatch(item["source_sha256"]):
            if _file_sha256(source_path.resolve()).lower() != item["source_sha256"].lower():
                _error(errors, "SPEC_LOCAL_PICTURE_SOURCE_HASH_MISMATCH", f"{path}.source_sha256", "source hash does not match current file")

        if item.get("crop_mode") != "alpha_isolation_seeded":
            _error(errors, "SPEC_LOCAL_PICTURE_CROP_MODE_INVALID", f"{path}.crop_mode", "must be alpha_isolation_seeded")
        seeds = item.get("foreground_seeds")
        valid_seeds = (
            isinstance(seeds, list)
            and bool(seeds)
            and all(
                isinstance(seed, list)
                and len(seed) == 2
                and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in seed)
                for seed in seeds
            )
        )
        if not valid_seeds:
            _error(errors, "SPEC_LOCAL_PICTURE_SEEDS_INVALID", f"{path}.foreground_seeds", "must contain one or more source-coordinate [x, y] integer seeds")
        elif _valid_bbox(source_bbox):
            left, top, width, height = source_bbox
            if any(not (left <= seed[0] < left + width and top <= seed[1] < top + height) for seed in seeds):
                _error(errors, "SPEC_LOCAL_PICTURE_SEEDS_INVALID", f"{path}.foreground_seeds", "every seed must lie inside source_bbox")

        pixel_size = item.get("pixel_size")
        if not _valid_size(pixel_size) or any(not isinstance(value, int) or isinstance(value, bool) for value in pixel_size):
            _error(errors, "SPEC_LOCAL_PICTURE_ASSET_DIMENSIONS_INVALID", f"{path}.pixel_size", "must contain two positive integers")
        elif _valid_bbox(source_bbox) and pixel_size != source_bbox[2:4]:
            _error(errors, "SPEC_LOCAL_PICTURE_ASSET_DIMENSIONS_INVALID", f"{path}.pixel_size", "pixel_size must equal source_bbox width and height")

        alpha_hash = item.get("alpha_mask_sha256")
        if not isinstance(alpha_hash, str) or not SHA256_PATTERN.fullmatch(alpha_hash):
            _error(errors, "SPEC_LOCAL_PICTURE_ALPHA_MASK_INVALID", f"{path}.alpha_mask_sha256", "must contain 64 hex characters")
        asset_path = Path(item["asset_path"]).expanduser() if isinstance(item.get("asset_path"), str) else None
        if asset_path is None or not asset_path.is_absolute() or asset_path.is_symlink() or not asset_path.is_file():
            _error(errors, "SPEC_LOCAL_PICTURE_ASSET_INVALID", f"{path}.asset_path", "asset must be a readable non-symlink PNG file")
        else:
            resolved_asset = asset_path.resolve()
            if resolved_asset.suffix.lower() != ".png":
                _error(errors, "SPEC_LOCAL_PICTURE_ASSET_INVALID", f"{path}.asset_path", "asset must be PNG")
            if resolved_asset.parent.name != "pictures" or resolved_asset.parent.parent.name != "assets":
                _error(errors, "SPEC_LOCAL_PICTURE_ASSET_LOCATION_INVALID", f"{path}.asset_path", "asset parent must be assets/pictures")
            asset_hash = item.get("asset_sha256")
            if not isinstance(asset_hash, str) or not SHA256_PATTERN.fullmatch(asset_hash):
                _error(errors, "SPEC_LOCAL_PICTURE_ASSET_HASH_INVALID", f"{path}.asset_sha256", "asset sha256 must contain 64 hex characters")
            elif _file_sha256(resolved_asset).lower() != asset_hash.lower():
                _error(errors, "SPEC_LOCAL_PICTURE_ASSET_HASH_MISMATCH", f"{path}.asset_sha256", "asset hash does not match current file")
            if resolved_asset.suffix.lower() == ".png":
                try:
                    with Image.open(resolved_asset) as image:
                        image.load()
                        if _valid_size(pixel_size) and image.size != tuple(pixel_size):
                            _error(errors, "SPEC_LOCAL_PICTURE_ASSET_DIMENSIONS_INVALID", path, "decoded asset dimensions must match pixel_size")
                        if image.mode != "RGBA":
                            _error(errors, "SPEC_LOCAL_PICTURE_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "picture PNG must use RGBA mode")
                        else:
                            alpha = image.getchannel("A")
                            minimum, maximum = alpha.getextrema()
                            if minimum != 0 or maximum == 0:
                                _error(errors, "SPEC_LOCAL_PICTURE_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "picture alpha must contain transparent background and visible foreground")
                            actual_alpha_hash = hashlib.sha256(alpha.tobytes()).hexdigest()
                            if isinstance(alpha_hash, str) and SHA256_PATTERN.fullmatch(alpha_hash) and actual_alpha_hash.lower() != alpha_hash.lower():
                                _error(errors, "SPEC_LOCAL_PICTURE_ALPHA_MASK_MISMATCH", f"{path}.alpha_mask_sha256", "alpha mask hash does not match the current picture asset")
                            foreground = alpha.getbbox()
                            if foreground is not None and (
                                foreground[0] == 0
                                or foreground[1] == 0
                                or foreground[2] == image.width
                                or foreground[3] == image.height
                            ):
                                _error(errors, "SPEC_LOCAL_PICTURE_FOREGROUND_TOUCHES_EDGE", f"{path}.asset_path", "visible picture pixels must not touch the crop boundary")
                            if valid_seeds and _valid_bbox(source_bbox):
                                left, top = source_bbox[:2]
                                if any(alpha.getpixel((seed[0] - left, seed[1] - top)) == 0 for seed in seeds):
                                    _error(errors, "SPEC_LOCAL_PICTURE_SEEDS_INVALID", f"{path}.foreground_seeds", "every seed must select visible foreground in the current asset")
                        if source_path is not None and source_path.is_absolute() and source_path.is_file() and _valid_bbox(source_bbox):
                            with Image.open(source_path.resolve()) as source_image:
                                source_image.load()
                                left, top, width, height = source_bbox
                                source_crop = source_image.convert("RGB").crop((left, top, left + width, top + height))
                            asset_rgb = image.convert("RGB")
                            if source_crop.size != asset_rgb.size or source_crop.tobytes() != asset_rgb.tobytes():
                                _error(errors, "SPEC_LOCAL_PICTURE_RGB_MISMATCH", f"{path}.asset_path", "asset RGB pixels must exactly match the bound source crop")
                except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
                    _error(errors, "SPEC_LOCAL_PICTURE_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "picture asset is not a readable PNG")

        if isinstance(element, dict):
            content = element.get("content")
            element_asset = content.get("asset") if isinstance(content, dict) else None
            if not isinstance(element_asset, dict) or (
                element_asset.get("path") != item.get("asset_path")
                or element_asset.get("asset_sha256") != item.get("asset_sha256")
                or element_asset.get("pixel_size") != item.get("pixel_size")
            ):
                _error(errors, "SPEC_LOCAL_PICTURE_ELEMENT_ASSET_INVALID", f"{path}.element_id", "element asset must match the local picture record")
            if not isinstance(content, dict) or content.get("mode") != "none":
                _error(errors, "SPEC_LOCAL_PICTURE_FRAMING_INVALID", f"{path}.element_id", "local transparent picture requires mode none")
            zero_crop = {side: 0 for side in ("left", "top", "right", "bottom")}
            if not isinstance(content, dict) or content.get("crop") != zero_crop:
                _error(errors, "SPEC_LOCAL_PICTURE_FRAMING_INVALID", f"{path}.element_id", "local transparent picture requires zero crop")

        if item.get("rgb_preserved") is not True:
            _error(errors, "SPEC_LOCAL_PICTURE_RGB_PRESERVATION_INVALID", f"{path}.rgb_preserved", "must be true")
        if item.get("validation") != "passed":
            _error(errors, "SPEC_LOCAL_PICTURE_VALIDATION_INVALID", f"{path}.validation", "must be passed")
        if not isinstance(item.get("selectable_picture_verified"), bool):
            _error(errors, "SPEC_LOCAL_PICTURE_SELECTABILITY_INVALID", f"{path}.selectable_picture_verified", "must be boolean")
        elif stage == "final" and not item["selectable_picture_verified"]:
            _error(errors, "SPEC_LOCAL_PICTURE_SELECTABILITY_NOT_VERIFIED", f"{path}.selectable_picture_verified", "final spec requires an independently selectable picture")
        if item.get("object_type") != "picture":
            _error(errors, "SPEC_LOCAL_PICTURE_OBJECT_TYPE_INVALID", f"{path}.object_type", "must be picture")


def _identity_only_final(
    spec: dict[str, Any],
    *,
    verification_profile: str,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Validate final delivery from current immutable artifacts only."""
    visual_gate = spec.get("visual_gate")
    editability_gate = spec.get("editability_gate")
    expected_delivery = {
        "rapid": "rapid_validated",
        "reviewed": "reviewed_passed",
    }.get(verification_profile)
    if spec.get("delivery_status") != expected_delivery:
        _error(
            errors,
            "SPEC_DELIVERY_STATUS_INVALID",
            "delivery_status",
            f"final {verification_profile} delivery requires {expected_delivery}",
        )
    if not isinstance(visual_gate, dict):
        _error(errors, "SPEC_VISUAL_GATE_NOT_PASSED", "visual_gate", "final visual gate is required")
        visual_gate = {}
    if not isinstance(editability_gate, dict):
        _error(errors, "SPEC_EDITABILITY_GATE_NOT_PASSED", "editability_gate", "final editability gate is required")
        editability_gate = {}

    expected_visual_status = (
        "not_independently_reviewed" if verification_profile == "rapid" else "passed"
    )
    if visual_gate.get("status") != expected_visual_status:
        _error(
            errors,
            "SPEC_VISUAL_GATE_NOT_PASSED",
            "visual_gate.status",
            f"final {verification_profile} requires {expected_visual_status}",
        )
    if editability_gate.get("status") != "passed":
        _error(errors, "SPEC_EDITABILITY_GATE_NOT_PASSED", "editability_gate.status", "editability gate must pass")
    for gate_name, gate in (("visual_gate", visual_gate), ("editability_gate", editability_gate)):
        evidence = gate.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(item, str) and bool(item) for item in evidence
        ):
            _error(errors, "SPEC_GATE_EVIDENCE_INVALID", f"{gate_name}.evidence", "non-empty evidence paths are required")

    tripwire = visual_gate.get("tripwire")
    tripwire_valid = tripwire is None and verification_profile == "reviewed"
    if not tripwire_valid:
        tripwire_valid = isinstance(tripwire, dict) and (
            (
                tripwire.get("available") is False
                and tripwire.get("triggered") is None
                and tripwire.get("reason") == "no_approved_baseline"
            )
            or (
                tripwire.get("available") is True
                and tripwire.get("triggered") is False
            )
        )
    if not tripwire_valid:
        _error(errors, "SPEC_VISUAL_TRIPWIRE_INVALID", "visual_gate.tripwire", "tripwire must be explicitly safe")

    edit_review = editability_gate.get("review")
    required_edit = {
        "text_and_data",
        "native_text_structure",
        "basic_structure",
        "full_slide_picture_risk",
    }
    if not isinstance(edit_review, dict) or any(
        edit_review.get(field) != "passed" for field in required_edit
    ):
        _error(errors, "SPEC_EDITABILITY_REVIEW_INVALID", "editability_gate.review", "all editability checks must pass")

    artifacts, artifact_errors = collect_current_artifacts(spec)
    errors.extend(artifact_errors)
    summary: dict[str, Any] = {
        "content_spec_sha256": content_spec_sha256(spec),
        "delivery_status": spec.get("delivery_status"),
    }
    high_risk = spec.get("modules", {}).get("high_risk") if isinstance(spec.get("modules"), dict) else None
    items = high_risk.get("items", []) if isinstance(high_risk, dict) else []
    for index, item in enumerate(items if isinstance(items, list) else []):
        if isinstance(item, dict) and item.get("severity") in {"P0", "P1"} and item.get("result") != "passed":
            _error(errors, "SPEC_OPEN_BLOCKING_DIFFERENCE", f"modules.high_risk.items[{index}]", "open P0/P1 blocks final delivery")

    if artifacts is None:
        return summary
    summary["current_pptx"] = artifacts.identities["current_pptx"]

    review = visual_gate.get("review")
    if verification_profile == "rapid":
        if review is not None:
            _error(
                errors,
                "SPEC_RAPID_VISUAL_REVIEW_FORBIDDEN",
                "visual_gate.review",
                "rapid must not carry the reviewed extension visual audit",
            )
    else:
        review_issues = visual_review_record_issues(
            review,
            required_coverage=artifacts.required_coverage,
        )
        errors.extend(review_issues)
        if not review_issues:
            summary["current_artifacts"] = artifacts.identities
            summary["visual_review_outcome"] = copy.deepcopy(review)
    return summary


def validate_spec(
    spec: Any,
    stage: str = "prebuild",
    *,
    font_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable validation report for a reconstruction specification."""
    if stage not in {"prebuild", "final"}:
        raise ValueError("stage must be prebuild or final")
    non_finite_paths = non_finite_number_paths(spec)
    if non_finite_paths:
        declared_profile = (
            spec.get("verification_profile") if isinstance(spec, dict) else None
        )
        verification_profile = (
            declared_profile
            if isinstance(declared_profile, str)
            and declared_profile in VERIFICATION_PROFILES
            else "rapid"
        )
        return {
            "valid": False,
            "stage": stage,
            "verification_profile": verification_profile,
            "spec_sha256": None,
            "errors": [
                {
                    "code": "SPEC_NUMBER_NON_FINITE",
                    "path": path,
                    "detail": "number must be finite",
                }
                for path in non_finite_paths
            ],
            "warnings": [],
        }
    spec_sha256 = canonical_json_sha256(spec)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(spec, dict):
        return {
            "valid": False,
            "stage": stage,
            "verification_profile": "rapid",
            "spec_sha256": spec_sha256,
            "errors": [{"code": "SPEC_ROOT_INVALID", "path": "$", "detail": "root must be an object"}],
            "warnings": [],
        }

    if stage == "prebuild":
        errors.extend(fixed_font_issues(spec, font_runtime))

    verification_profile = _verification_profile(spec)
    envelope_issues = schema_envelope_issues(spec)
    if any(detail.startswith("missing fields: ") for _, detail in envelope_issues):
        return {
            "valid": False,
            "stage": stage,
            "verification_profile": verification_profile,
            "spec_sha256": spec_sha256,
            "errors": [
                {
                    "code": "UNSUPPORTED_CAPABILITY",
                    "path": path,
                    "detail": detail,
                }
                for path, detail in envelope_issues
            ],
            "warnings": [],
        }
    for path, detail in envelope_issues:
        _error(errors, "UNSUPPORTED_CAPABILITY", path, detail)
    _validate_verification_identity(spec, verification_profile, errors)

    if spec.get("schema_version") != 2:
        _error(errors, "SPEC_SCHEMA_VERSION_UNSUPPORTED", "schema_version", "expected schema_version 2")
    if not isinstance(spec.get("page_id"), str) or not re.fullmatch(r"page-\d{3}", spec.get("page_id", "")):
        _error(errors, "SPEC_PAGE_ID_INVALID", "page_id", "expected page-NNN")

    session = spec.get("session_reuse")
    if not isinstance(session, dict) or session.get("mode") not in {"fresh_reconstruction", "same_session_reuse"}:
        _error(errors, "SPEC_SESSION_REUSE_INVALID", "session_reuse", "invalid session reuse mode")
    elif session["mode"] == "fresh_reconstruction" and session.get("artifacts") != []:
        _error(errors, "SPEC_SESSION_REUSE_INVALID", "session_reuse.artifacts", "fresh reconstruction requires no artifacts")
    elif session["mode"] == "same_session_reuse":
        artifacts = session.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            _error(errors, "SPEC_SESSION_ARTIFACTS_INVALID", "session_reuse.artifacts", "same-session reuse requires verified artifacts")
        else:
            for index, artifact in enumerate(artifacts):
                path = f"session_reuse.artifacts[{index}]"
                if not isinstance(artifact, dict) or artifact.get("identity_verified") is not True:
                    _error(errors, "SPEC_SESSION_ARTIFACTS_INVALID", path, "artifact requires identity_verified: true")
                    continue
                _validate_reference(artifact, path, errors)

    canvas = spec.get("canvas")
    if not isinstance(canvas, dict):
        _error(errors, "SPEC_CANVAS_INVALID", "canvas", "canvas must be an object")
    else:
        for key in ("source_size", "visual_size", "slide_size_emu"):
            if not _valid_size(canvas.get(key)):
                _error(errors, "SPEC_CANVAS_FIELD_INVALID", f"canvas.{key}", "expected two positive numbers")
        if not _valid_bbox(canvas.get("page_frame_bbox")):
            _error(errors, "SPEC_CANVAS_FIELD_INVALID", "canvas.page_frame_bbox", "invalid bbox")
        if not isinstance(canvas.get("mapping_mode"), str) or not canvas.get("mapping_mode"):
            _error(errors, "SPEC_CANVAS_FIELD_INVALID", "canvas.mapping_mode", "mapping_mode is required")
        if not isinstance(canvas.get("background"), str) or not canvas.get("background"):
            _error(errors, "SPEC_CANVAS_FIELD_INVALID", "canvas.background", "background is required")
    _validate_reference(
        spec.get("content_reference"),
        "content_reference",
        errors,
        canvas.get("source_size") if isinstance(canvas, dict) else None,
    )
    _validate_reference(
        spec.get("clean_visual_reference"),
        "clean_visual_reference",
        errors,
        canvas.get("visual_size") if isinstance(canvas, dict) else None,
    )

    elements = spec.get("elements")
    element_ids: set[str] = set()
    element_map: dict[str, dict[str, Any]] = {}
    if not isinstance(elements, list) or not elements:
        _error(errors, "SPEC_ELEMENTS_INVALID", "elements", "elements must be non-empty")
    else:
        for index, element in enumerate(elements):
            path = f"elements[{index}]"
            if not isinstance(element, dict):
                _error(errors, "SPEC_ELEMENT_INVALID", path, "element must be an object")
                continue
            missing = sorted(ELEMENT_FIELDS - set(element))
            if missing:
                _error(errors, "SPEC_ELEMENT_FIELD_MISSING", path, f"missing fields: {', '.join(missing)}")
                continue
            element_id = element.get("element_id")
            if not isinstance(element_id, str) or not element_id or element_id in element_ids:
                _error(errors, "SPEC_ELEMENT_ID_INVALID", f"{path}.element_id", "element_id must be unique and non-empty")
            else:
                element_ids.add(element_id)
                element_map[element_id] = element
            if element.get("kind") not in ALLOWED_KINDS:
                _error(errors, "SPEC_ELEMENT_KIND_INVALID", f"{path}.kind", "unsupported kind")
            if not _valid_bbox(element.get("source_bbox")) or not _valid_bbox(element.get("slide_bbox")):
                _error(errors, "SPEC_ELEMENT_BBOX_INVALID", path, "source_bbox and slide_bbox must be valid")
            elif _slide_bbox_unit_suspect(
                element.get("source_bbox"),
                element.get("slide_bbox"),
                canvas,
                element.get("kind"),
            ):
                _error(
                    errors,
                    "SPEC_SLIDE_BBOX_UNIT_SUSPECT",
                    f"{path}.slide_bbox",
                    "slide_bbox scale is inconsistent with source_bbox and may use pixel coordinates",
                )
            elif _bbox_mapping_invalid(element.get("source_bbox"), element.get("slide_bbox"), canvas):
                _error(errors, "SPEC_SLIDE_BBOX_MAPPING_INVALID", f"{path}.slide_bbox", "slide bbox does not match canvas mapping")
            if isinstance(canvas, dict):
                if not _bbox_in_bounds(element.get("source_bbox"), canvas.get("visual_size")):
                    _error(errors, "SPEC_ELEMENT_BBOX_OUT_OF_BOUNDS", f"{path}.source_bbox", "source bbox exceeds visual canvas")
                if not _bbox_in_bounds(element.get("slide_bbox"), canvas.get("slide_size_emu")):
                    _error(errors, "SPEC_ELEMENT_BBOX_OUT_OF_BOUNDS", f"{path}.slide_bbox", "slide bbox exceeds slide canvas")
            if not isinstance(element.get("layer"), int):
                _error(errors, "SPEC_ELEMENT_LAYER_INVALID", f"{path}.layer", "layer must be integer")
            if not isinstance(element.get("editable"), bool):
                _error(errors, "SPEC_ELEMENT_EDITABLE_INVALID", f"{path}.editable", "editable must be boolean")
            if element.get("confidence") not in ALLOWED_CONFIDENCE:
                _error(errors, "SPEC_ELEMENT_CONFIDENCE_INVALID", f"{path}.confidence", "invalid confidence")
            if not isinstance(element.get("style"), dict) or not isinstance(element.get("content"), dict):
                _error(errors, "SPEC_ELEMENT_PAYLOAD_INVALID", path, "style and content must be objects")
            else:
                _validate_canonical_element_rotation(
                    element,
                    index,
                    stage,
                    errors,
                )
                errors.extend(
                    issue.as_dict() for issue in validate_element_contract(element)
                )

    regions = spec.get("regions")
    region_element_ids: set[str] = set()
    if not isinstance(regions, list) or not regions:
        _error(errors, "SPEC_REGIONS_INVALID", "regions", "regions must be non-empty")
    else:
        region_ids: set[str] = set()
        for index, region in enumerate(regions):
            path = f"regions[{index}]"
            if not isinstance(region, dict):
                _error(errors, "SPEC_REGION_INVALID", path, "region must be an object")
                continue
            region_id = region.get("region_id")
            if not isinstance(region_id, str) or not region_id or region_id in region_ids:
                _error(errors, "SPEC_REGION_ID_INVALID", f"{path}.region_id", "region_id must be unique")
            else:
                region_ids.add(region_id)
            if not _valid_bbox(region.get("source_bbox")) or not _valid_bbox(region.get("slide_bbox")):
                _error(errors, "SPEC_REGION_BBOX_INVALID", path, "invalid region bbox")
            else:
                if isinstance(canvas, dict) and not _bbox_in_bounds(region.get("source_bbox"), canvas.get("visual_size")):
                    _error(errors, "SPEC_REGION_BBOX_OUT_OF_BOUNDS", f"{path}.source_bbox", "region exceeds visual canvas")
                if isinstance(canvas, dict) and not _bbox_in_bounds(region.get("slide_bbox"), canvas.get("slide_size_emu")):
                    _error(errors, "SPEC_REGION_BBOX_OUT_OF_BOUNDS", f"{path}.slide_bbox", "region exceeds slide canvas")
                if _slide_bbox_unit_suspect(region.get("source_bbox"), region.get("slide_bbox"), canvas, "shape"):
                    _error(errors, "SPEC_SLIDE_BBOX_UNIT_SUSPECT", f"{path}.slide_bbox", "region slide_bbox may use pixels")
                elif _bbox_mapping_invalid(region.get("source_bbox"), region.get("slide_bbox"), canvas):
                    _error(errors, "SPEC_SLIDE_BBOX_MAPPING_INVALID", f"{path}.slide_bbox", "region bbox does not match canvas mapping")
            references = region.get("element_ids")
            if not isinstance(references, list) or any(item not in element_ids for item in references):
                _error(errors, "SPEC_ELEMENT_REFERENCE_INVALID", f"{path}.element_ids", "unknown element reference")
            else:
                region_element_ids.update(item for item in references if isinstance(item, str))
        if region_element_ids != element_ids:
            _error(
                errors,
                "SPEC_REGION_COVERAGE_INVALID",
                "regions",
                f"regions must cover every element; missing: {', '.join(sorted(element_ids - region_element_ids))}",
            )

    reading_order = spec.get("reading_order")
    if not isinstance(reading_order, list) or not reading_order or len(reading_order) != len(set(reading_order)):
        _error(errors, "SPEC_READING_ORDER_INVALID", "reading_order", "must be non-empty and unique")
    elif any(item not in element_ids for item in reading_order):
        _error(errors, "SPEC_ELEMENT_REFERENCE_INVALID", "reading_order", "unknown element reference")
    elif set(reading_order) != element_ids:
        _error(
            errors,
            "SPEC_READING_ORDER_COVERAGE_INVALID",
            "reading_order",
            f"reading_order must cover every element; missing: {', '.join(sorted(element_ids - set(reading_order)))}",
        )

    activated = spec.get("activated_modules")
    modules = spec.get("modules")
    if not isinstance(activated, list) or len(activated) != len(set(activated)):
        _error(errors, "SPEC_ACTIVATED_MODULES_INVALID", "activated_modules", "must be a unique array")
        activated = []
    if not isinstance(modules, dict):
        _error(errors, "SPEC_MODULES_INVALID", "modules", "modules must be an object")
        modules = {}
    if modules.get("picture_framing") and "picture_framing" not in activated:
        _error(
            errors,
            "SPEC_MODULE_NOT_ACTIVATED",
            "modules.picture_framing",
            "non-empty picture_framing module must be listed in activated_modules",
        )
    for module_name in activated:
        if module_name not in ALLOWED_MODULES:
            _error(errors, "SPEC_ACTIVATED_MODULE_UNKNOWN", f"activated_modules.{module_name}", "unknown module name")
        if module_name not in modules:
            _error(errors, "SPEC_ACTIVATED_MODULE_MISSING", f"modules.{module_name}", "activated module is absent")
            continue
        module = modules.get(module_name)
        if not isinstance(module, dict) or not module:
            if module_name == "representation_plan" and stage == "prebuild":
                continue
            _error(errors, "SPEC_ACTIVATED_MODULE_EMPTY", f"modules.{module_name}", "activated module must be a non-empty object")
            continue
        unknown_references = sorted(_module_element_references(module) - element_ids)
        if unknown_references:
            _error(
                errors,
                "SPEC_MODULE_ELEMENT_REFERENCE_INVALID",
                f"modules.{module_name}",
                f"unknown element references: {', '.join(unknown_references)}",
            )
    if stage == "prebuild":
        if "representation_plan" not in activated:
            _error(
                errors,
                "SPEC_ACTIVATED_MODULES_INVALID",
                "activated_modules",
                "prebuild requires representation_plan activation",
            )
        else:
            errors.extend(issue.as_dict() for issue in validate_representation_plan(spec))
        if "background" not in activated:
            _error(
                errors,
                "SPEC_ACTIVATED_MODULES_INVALID",
                "activated_modules",
                "current schema requires background activation",
            )
        errors.extend(issue.as_dict() for issue in validate_background_prebuild(spec))
    _validate_coordinate_overlay_evidence(
        modules.get("page_layout"),
        spec.get("clean_visual_reference"),
        errors,
        stage=stage,
    )
    if "typography" in activated:
        _validate_typography(
            modules.get("typography"),
            element_map,
            canvas,
            stage,
            errors,
        )
    if "icons" in activated:
        _validate_icons(
            modules.get("icons"),
            element_map,
            canvas,
            spec.get("clean_visual_reference"),
            spec.get("page_id"),
            stage,
            errors,
        )
    if "picture_framing" in activated:
        _validate_picture_framing(
            modules.get("picture_framing"),
            element_map,
            canvas,
            spec.get("clean_visual_reference"),
            spec.get("page_id"),
            stage,
            errors,
        )

    if stage == "prebuild" and not errors:
        from pptx_builder import validate_renderer_contracts

        typography_items = (
            modules.get("typography", {}).get("items", [])
            if isinstance(modules.get("typography"), dict)
            else []
        )
        typography_index = {
            item["element_id"]: item
            for item in typography_items
            if isinstance(item, dict) and isinstance(item.get("element_id"), str)
        }
        modes = resolved_element_mode_map(spec)
        errors.extend(
            issue.as_dict()
            for issue in validate_renderer_contracts(
                spec,
                element_map,
                modes,
                typography_index,
            )
        )

    if stage == "final":
        final_summary = _identity_only_final(
            spec,
            verification_profile=verification_profile,
            errors=errors,
        )
        return {
            "valid": not errors,
            "stage": stage,
            "verification_profile": verification_profile,
            "spec_sha256": spec_sha256,
            "errors": errors,
            "warnings": warnings,
            **final_summary,
        }

    report = {
        "valid": not errors,
        "stage": stage,
        "verification_profile": verification_profile,
        "spec_sha256": spec_sha256,
        "errors": errors,
        "warnings": warnings,
    }
    if stage == "prebuild":
        report["preferred_font"] = preferred_font_from_spec(spec)
    return report


def validate_spec_file(
    spec_path: Path,
    *,
    stage: str,
    snapshot_path: Path | None = None,
    runtime_path: Path | None = None,
) -> dict[str, Any]:
    """Validate one exact JSON byte stream and optionally freeze it for build."""
    if snapshot_path is not None and stage != "prebuild":
        raise ValueError("--snapshot is only valid with --stage prebuild")
    if stage == "final" and runtime_path is not None:
        raise ValueError("--runtime is not accepted with --stage final")
    runtime: dict[str, Any] | None = None
    runtime_identity: dict[str, str] | None = None
    if runtime_path is not None:
        resolved_runtime = runtime_path.expanduser().resolve()
        runtime_payload = json.loads(
            resolved_runtime.read_text(encoding="utf-8"),
            parse_constant=reject_nonstandard_json_number,
        )
        if (
            not isinstance(runtime_payload, dict)
            or runtime_payload.get("valid") is not True
            or runtime_payload.get("errors") != []
        ):
            raise ValueError("runtime preflight must be a passing report")
        runtime = validate_font_runtime(runtime_payload.get("font_runtime"))
        runtime_identity = {
            "path": str(resolved_runtime),
            "sha256": file_sha256(resolved_runtime),
        }
    raw = spec_path.read_bytes()
    spec = json.loads(
        raw.decode("utf-8"),
        parse_constant=reject_nonstandard_json_number,
    )
    report = validate_spec(spec, stage=stage, font_runtime=runtime)
    if runtime is not None and runtime_identity is not None:
        report["runtime_preflight"] = runtime_identity
        report["font_runtime"] = runtime
    if snapshot_path is not None and report["valid"]:
        resolved_snapshot = snapshot_path.expanduser().resolve()
        resolved_snapshot.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(resolved_snapshot, raw)
        report["snapshot"] = {
            "path": str(resolved_snapshot),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="Path to page-reconstruction.json")
    parser.add_argument(
        "--stage",
        choices=("prebuild", "final"),
        default="prebuild",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="atomically save the same JSON emitted to stdout",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="atomically freeze the exact validated bytes for compiler input",
    )
    parser.add_argument(
        "--runtime",
        type=Path,
        help="optional passing batch/runtime-preflight.json for compatibility prebuild validation",
    )
    args = parser.parse_args(argv)
    if args.snapshot is not None and args.stage != "prebuild":
        parser.error("--snapshot is only valid with --stage prebuild")
    if args.snapshot is not None and args.output is None:
        parser.error("--snapshot requires --output")
    if args.stage == "final" and args.runtime is not None:
        parser.error("--runtime is not accepted with --stage final")
    return args


def _emit_json(payload: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, output)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    print(text, end="")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = validate_spec_file(
            args.spec,
            stage=args.stage,
            snapshot_path=args.snapshot,
            runtime_path=args.runtime,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        NonStandardJsonNumberError,
        ValueError,
    ) as exc:
        result = {
            "valid": False,
            "stage": args.stage,
            "errors": [{"code": "SPEC_FILE_INVALID", "path": str(args.spec), "detail": str(exc)}],
            "warnings": [],
        }
    _emit_json(result, args.output)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
