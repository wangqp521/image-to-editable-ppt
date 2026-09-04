"""Fail-closed, isolated background contracts for schema v2."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
from collections import Counter
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .error_codes import ContractIssue, ToolError
from .geometry import validate_bbox
from .hashing import canonical_json_sha256, file_sha256
from .representation_contracts import element_mode_map, require_asset
from .artifact_identity import current_evidence_view
from .schema_contracts import (
    BACKGROUND_ITEM_FIELDS,
    BACKGROUND_MODES,
    BACKGROUND_PROVENANCE_FIELDS,
    BACKGROUND_ROLES,
)
from .schema_io import index_elements
from .spec_identity import content_spec_sha256, input_spec_sha256


MODES = BACKGROUND_MODES
ROLES = BACKGROUND_ROLES
ITEM_FIELDS = BACKGROUND_ITEM_FIELDS
PROVENANCE_FIELDS = BACKGROUND_PROVENANCE_FIELDS
MODULE_FIELDS = frozenset({"items"})
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def _issue(code: str, path: str, detail: str) -> ContractIssue:
    return ContractIssue(code, path, detail)


def _incomplete(path: str, detail: str) -> ContractIssue:
    return _issue("BACKGROUND_INCOMPLETE", path, detail)


def _non_string_key_issue(value: Any, path: str) -> ContractIssue | None:
    if isinstance(value, dict) and any(
        not isinstance(key, str) for key in value
    ):
        return _incomplete(path, "record field names must be strings")
    return None


def _field_issues(
    value: Any, path: str, fields: frozenset[str]
) -> list[ContractIssue]:
    if not isinstance(value, dict):
        return [_incomplete(path, "record must be an object")]
    issues: list[ContractIssue] = []
    key_issue = _non_string_key_issue(value, path)
    if key_issue is not None:
        issues.append(key_issue)
    string_keys = {key for key in value if isinstance(key, str)}
    unknown = sorted(string_keys - fields)
    missing = sorted(fields - string_keys)
    if unknown:
        issues.append(_incomplete(path, f"unknown fields: {', '.join(unknown)}"))
    if missing:
        issues.append(_incomplete(path, f"missing fields: {', '.join(missing)}"))
    return issues


def _require_background_asset(
    asset: Any, path: str
) -> tuple[Path, str, tuple[int, int]]:
    key_issue = _non_string_key_issue(asset, path)
    if key_issue is not None:
        raise ToolError(key_issue.code, key_issue.path, key_issue.detail)
    return require_asset(asset, path)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def _file_snapshot(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        *_stat_identity(value),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@contextmanager
def _open_native_source(
    candidate: Path,
) -> Iterator[tuple[list[int], int]]:
    """Open an absolute path through a no-symlink descriptor chain."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if (
        nofollow is None
        or directory is None
        or nonblock is None
        or os.open not in os.supports_dir_fd
    ):
        raise OSError("descriptor-relative no-symlink opens are unavailable")
    parts = candidate.parts
    if (
        not parts
        or candidate.anchor != os.path.sep
        or parts[0] != candidate.anchor
        or len(parts) < 2
        or any(part in {"", ".", ".."} for part in parts[1:])
    ):
        raise ValueError("native source path is not a constrained absolute path")

    close_on_exit = ExitStack()
    directory_fds: list[int] = []
    try:
        directory_flags = os.O_RDONLY | directory | nofollow
        file_flags = os.O_RDONLY | nofollow | nonblock
        cloexec = getattr(os, "O_CLOEXEC", 0)
        directory_flags |= cloexec
        file_flags |= cloexec

        current_fd = os.open(candidate.anchor, directory_flags)
        close_on_exit.callback(os.close, current_fd)
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise OSError("root descriptor is not a directory")
        directory_fds.append(current_fd)

        for component in parts[1:-1]:
            current_fd = os.open(
                component,
                directory_flags,
                dir_fd=current_fd,
            )
            close_on_exit.callback(os.close, current_fd)
            if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
                raise OSError("path component is not a directory")
            directory_fds.append(current_fd)

        leaf_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        close_on_exit.callback(os.close, leaf_fd)
        if not stat.S_ISREG(os.fstat(leaf_fd).st_mode):
            raise OSError("native source is not a regular file")
        yield directory_fds, leaf_fd
    finally:
        close_on_exit.close()


def _native_source_sha256(candidate: Path) -> str:
    """Hash one stable regular file and revalidate its declared path identity."""
    with _open_native_source(candidate) as (directory_fds, leaf_fd):
        directory_identities = [
            _stat_identity(os.fstat(descriptor)) for descriptor in directory_fds
        ]
        before = _file_snapshot(os.fstat(leaf_fd))
        view = current_evidence_view()
        content_source = (
            _open_native_source(view.content_path(candidate))
            if view is not None
            else nullcontext((directory_fds, leaf_fd))
        )
        digest = hashlib.sha256()
        with content_source as (_content_dirs, content_fd):
            content_before = _file_snapshot(os.fstat(content_fd))
            while True:
                chunk = os.read(content_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            content_after = _file_snapshot(os.fstat(content_fd))
            if content_after != content_before:
                raise OSError("native source content changed while hashing")

        with _open_native_source(candidate) as (check_directories, check_leaf):
            check_directory_identities = [
                _stat_identity(os.fstat(descriptor))
                for descriptor in check_directories
            ]
            if check_directory_identities != directory_identities:
                raise OSError("native source parent path changed while hashing")
            if _file_snapshot(os.fstat(check_leaf)) != before:
                raise OSError("native source path changed while hashing")
        return digest.hexdigest()


def _provenance_issues(value: Any, path: str) -> list[ContractIssue]:
    issues = _field_issues(value, path, PROVENANCE_FIELDS)
    if not isinstance(value, dict):
        return issues
    provenance_kind = value.get("kind")
    if not isinstance(provenance_kind, str) or provenance_kind not in {
        "native_measurement",
        "clean_background_asset",
    }:
        issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", f"{path}.kind", "provenance kind is invalid"))
    source_path = value.get("source_path")
    if (
        not isinstance(source_path, str)
        or not source_path
        or "\x00" in source_path
        or not Path(source_path).is_absolute()
    ):
        issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", f"{path}.source_path", "source_path must be a literal absolute path"))
    source_sha256 = value.get("source_sha256")
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", f"{path}.source_sha256", "source_sha256 must be a SHA-256 digest"))
    if (
        provenance_kind == "native_measurement"
        and isinstance(source_path, str)
        and source_path
        and "\x00" not in source_path
        and Path(source_path).is_absolute()
        and isinstance(source_sha256, str)
        and _SHA256.fullmatch(source_sha256) is not None
    ):
        candidate = Path(source_path)
        try:
            actual_sha256 = _native_source_sha256(candidate)
        except (OSError, RuntimeError, ValueError):
            actual_sha256 = None
        if actual_sha256 is None:
            issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", f"{path}.source_path", "native measurement source must be a readable local regular file"))
        elif actual_sha256.lower() != source_sha256.lower():
            issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", f"{path}.source_sha256", "native measurement source hash does not match the local file"))
    return issues


def _canvas_geometry(spec: dict[str, Any]) -> tuple[Any, Any, list[ContractIssue]]:
    canvas = spec.get("canvas")
    if not isinstance(canvas, dict):
        return None, None, [_issue("BACKGROUND_GEOMETRY_INVALID", "canvas", "canvas must be an object")]
    page_frame = canvas.get("page_frame_bbox")
    slide_size = canvas.get("slide_size_emu")
    issues: list[ContractIssue] = []
    try:
        page_frame = validate_bbox(page_frame, "canvas.page_frame_bbox")
    except ToolError as exc:
        issues.append(_issue("BACKGROUND_GEOMETRY_INVALID", exc.path, exc.detail))
        page_frame = None
    if (
        not isinstance(slide_size, list)
        or len(slide_size) != 2
        or any(type(value) is not int or value <= 0 for value in slide_size)
    ):
        issues.append(_issue("BACKGROUND_GEOMETRY_INVALID", "canvas.slide_size_emu", "slide_size_emu must contain two positive integers"))
        slide_size = None
    return page_frame, slide_size, issues


def _picture_framing_issues(
    element: dict[str, Any],
    element_id: str,
    pixel_size: tuple[int, int],
    slide_size: Any,
) -> list[ContractIssue]:
    path = f"elements.{element_id}"
    content = element.get("content")
    style = element.get("style")
    issues: list[ContractIssue] = []
    if not isinstance(content, dict) or content.get("mode") != "none":
        issues.append(_issue("BACKGROUND_FRAMING_INVALID", f"{path}.content.mode", "full-page background_picture requires mode none"))
    zero_crop = {side: 0 for side in ("left", "top", "right", "bottom")}
    if not isinstance(content, dict) or content.get("crop") != zero_crop:
        issues.append(_issue("BACKGROUND_FRAMING_INVALID", f"{path}.content.crop", "full-page background_picture requires zero crop on every side"))
    opacity = style.get("opacity") if isinstance(style, dict) else None
    if type(opacity) not in {int, float} or opacity != 1:
        issues.append(_issue("BACKGROUND_FRAMING_INVALID", f"{path}.style.opacity", "full-page background_picture requires opacity 1"))
    rotation = style.get("rotation") if isinstance(style, dict) else None
    if type(rotation) not in {int, float} or rotation != 0:
        issues.append(_issue("BACKGROUND_FRAMING_INVALID", f"{path}.style.rotation", "full-page background_picture requires rotation 0"))
    if (
        isinstance(slide_size, list)
        and len(slide_size) == 2
        and pixel_size[0] * slide_size[1] != pixel_size[1] * slide_size[0]
    ):
        issues.append(_issue("BACKGROUND_FRAMING_INVALID", f"{path}.content.asset.pixel_size", "background picture aspect ratio must exactly match the slide"))
    return issues


def _background_module(spec: dict[str, Any]) -> tuple[list[Any] | None, list[ContractIssue]]:
    modules = spec.get("modules")
    if not isinstance(modules, dict) or "background" not in modules:
        return None, []
    module = modules.get("background")
    issues = _field_issues(module, "modules.background", MODULE_FIELDS)
    if not isinstance(module, dict):
        return None, issues
    items = module.get("items")
    if not isinstance(items, list) or not items:
        issues.append(_incomplete("modules.background.items", "items must be a non-empty array"))
        return None, issues
    return items, issues


def _representation_bindings(spec: dict[str, Any]) -> set[str]:
    modules = spec.get("modules")
    plan = modules.get("representation_plan") if isinstance(modules, dict) else None
    items = plan.get("items") if isinstance(plan, dict) else None
    if not isinstance(items, list):
        return set()
    return {
        element_id
        for item in items
        if isinstance(item, dict)
        for element_id in (
            item.get("bound_element_ids")
            if isinstance(item.get("bound_element_ids"), list)
            else []
        )
        if isinstance(element_id, str)
    }


def _icon_bindings(spec: dict[str, Any]) -> set[str]:
    modules = spec.get("modules")
    icons_module = modules.get("icons") if isinstance(modules, dict) else None
    icons = icons_module.get("icons") if isinstance(icons_module, dict) else None
    if not isinstance(icons, list):
        return set()
    return {
        item["element_id"]
        for item in icons
        if isinstance(item, dict)
        and isinstance(item.get("element_id"), str)
        and item["element_id"]
    }


def validate_background_prebuild(spec: dict[str, Any]) -> list[ContractIssue]:
    """Return every defect in the optional, isolated background module."""
    if not isinstance(spec, dict):
        return [_incomplete("$", "schema root must be an object")]
    items, issues = _background_module(spec)
    if items is None:
        return issues
    try:
        elements = index_elements(spec)
    except ToolError as exc:
        return [*issues, _incomplete(exc.path, exc.detail)]

    page_frame, slide_size, canvas_issues = _canvas_geometry(spec)
    issues.extend(canvas_issues)

    representation_bindings = _representation_bindings(spec)
    icon_bindings = _icon_bindings(spec)
    background_ids: set[str] = set()
    bound_ids: set[str] = set()
    declared_bound_ids = {
        item.get("bound_element_id")
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("bound_element_id"), str)
        and item.get("bound_element_id")
    }
    base_count = 0

    for index, item in enumerate(items):
        path = f"modules.background.items[{index}]"
        issues.extend(_field_issues(item, path, ITEM_FIELDS))
        if not isinstance(item, dict):
            continue
        background_id = item.get("background_id")
        if not isinstance(background_id, str) or not background_id.strip():
            issues.append(_incomplete(f"{path}.background_id", "background_id must be non-empty"))
        elif background_id in background_ids:
            issues.append(_issue("BACKGROUND_BINDING_CONFLICT", f"{path}.background_id", "background_id must be unique"))
        else:
            background_ids.add(background_id)

        role = item.get("role")
        if not isinstance(role, str) or role not in ROLES:
            issues.append(_incomplete(f"{path}.role", "role is invalid"))
        elif role == "base":
            base_count += 1
        try:
            item_source_bbox = validate_bbox(item.get("source_bbox"), f"{path}.source_bbox")
        except ToolError as exc:
            issues.append(_incomplete(exc.path, exc.detail))
            item_source_bbox = None
        mode = item.get("selected_mode")
        if not isinstance(mode, str) or mode not in MODES:
            issues.append(_incomplete(f"{path}.selected_mode", "selected_mode is invalid"))
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            issues.append(_incomplete(f"{path}.reason", "reason must be non-empty"))
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(value, str) and value.strip() for value in evidence):
            issues.append(_incomplete(f"{path}.evidence", "evidence must be a non-empty string array"))
        if item.get("contains_foreground_semantics") is not False:
            issues.append(_issue("BACKGROUND_FOREGROUND_CONTAMINATION_RISK", f"{path}.contains_foreground_semantics", "background items must explicitly exclude foreground semantics"))

        provenance = item.get("source_provenance")
        provenance_path = f"{path}.source_provenance"
        issues.extend(_provenance_issues(provenance, provenance_path))
        provenance_kind = provenance.get("kind") if isinstance(provenance, dict) else None

        element_id = item.get("bound_element_id")
        if not isinstance(element_id, str) or not element_id:
            issues.append(_incomplete(f"{path}.bound_element_id", "bound_element_id must be non-empty"))
            continue
        if element_id in bound_ids:
            issues.append(_issue("BACKGROUND_BINDING_CONFLICT", f"{path}.bound_element_id", "bound element ids must be unique"))
        else:
            bound_ids.add(element_id)
        if element_id in representation_bindings:
            issues.append(_issue("BACKGROUND_BINDING_CONFLICT", f"{path}.bound_element_id", "background element also appears in the representation plan"))
        element = elements.get(element_id)
        if element is None:
            issues.append(_incomplete(f"{path}.bound_element_id", f"unknown element: {element_id}"))
            continue
        if item_source_bbox is not None and element.get("source_bbox") != item_source_bbox:
            issues.append(_issue("BACKGROUND_GEOMETRY_INVALID", f"{path}.source_bbox", "background fact source_bbox must exactly match its bound element source_bbox"))
        if role == "base" and page_frame is not None and item_source_bbox != page_frame:
            issues.append(_issue("BACKGROUND_GEOMETRY_INVALID", f"{path}.source_bbox", "base background source_bbox must exactly equal canvas.page_frame_bbox"))
        try:
            element_slide_bbox = validate_bbox(
                element.get("slide_bbox"), f"elements.{element_id}.slide_bbox"
            )
        except ToolError as exc:
            issues.append(_issue("BACKGROUND_GEOMETRY_INVALID", exc.path, exc.detail))
            element_slide_bbox = None
        expected_slide_bbox = (
            [0, 0, slide_size[0], slide_size[1]]
            if isinstance(slide_size, list)
            else None
        )
        is_full_slide = (
            expected_slide_bbox is not None
            and element_slide_bbox == expected_slide_bbox
        )
        if (
            role == "base"
            and expected_slide_bbox is not None
            and element_slide_bbox != expected_slide_bbox
        ):
            issues.append(_issue("BACKGROUND_GEOMETRY_INVALID", f"elements.{element_id}.slide_bbox", "background element slide_bbox must exactly cover the full slide"))
        kind = element.get("kind")
        if kind == "icon" or element_id in icon_bindings:
            issues.append(_issue("BACKGROUND_ICON_BINDING_FORBIDDEN", f"{path}.bound_element_id", "background cannot bind an icon element"))
        if mode == "native":
            if kind != "shape":
                issues.append(_incomplete(f"{path}.bound_element_id", "native background must bind a shape"))
            if provenance_kind != "native_measurement":
                issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", provenance_path, "native background requires native_measurement provenance"))
        elif mode == "background_picture":
            if kind != "picture":
                issues.append(_incomplete(f"{path}.bound_element_id", "background_picture must bind a plain picture"))
            if provenance_kind != "clean_background_asset":
                issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", provenance_path, "background_picture requires clean_background_asset provenance"))
            if kind == "picture":
                content = element.get("content")
                asset = content.get("asset") if isinstance(content, dict) else None
                try:
                    _asset_path, asset_hash, pixel_size = _require_background_asset(
                        asset, f"elements.{element_id}.content.asset"
                    )
                except ToolError as exc:
                    issues.append(ContractIssue(exc.code, exc.path, exc.detail, exc.capability))
                else:
                    if role == "base" or is_full_slide:
                        issues.extend(
                            _picture_framing_issues(
                                element, element_id, pixel_size, slide_size
                            )
                        )
                    provenance_sha256 = (
                        provenance.get("source_sha256")
                        if isinstance(provenance, dict)
                        else None
                    )
                    if isinstance(provenance, dict) and (
                        provenance.get("source_path") != asset.get("path")
                        or not isinstance(provenance_sha256, str)
                        or provenance_sha256.lower() != asset_hash.lower()
                    ):
                        issues.append(_issue("BACKGROUND_PROVENANCE_INVALID", provenance_path, "clean background provenance must match the bound picture asset"))
                    clean_reference = spec.get("clean_visual_reference")
                    clean_hash = clean_reference.get("sha256") if isinstance(clean_reference, dict) else None
                    non_background_exists = any(
                        candidate_id not in declared_bound_ids
                        for candidate_id in elements
                    )
                    if (
                        non_background_exists
                        and isinstance(clean_hash, str)
                        and asset_hash.lower() == clean_hash.lower()
                    ):
                        issues.append(_issue("BACKGROUND_FOREGROUND_CONTAMINATION_RISK", f"elements.{element_id}.content.asset", "original full-slide visual reference cannot be reused as a background asset when foreground elements exist"))

    if base_count != 1:
        issues.append(_incomplete("modules.background.items", "background module must contain exactly one base item"))

    background_layers = [
        elements[element_id].get("layer")
        for element_id in bound_ids
        if element_id in elements
    ]
    foreground_layers = [
        element.get("layer")
        for element_id, element in elements.items()
        if element_id not in bound_ids
    ]
    if background_layers and foreground_layers and (
        not all(type(layer) is int for layer in background_layers + foreground_layers)
        or max(background_layers) >= min(foreground_layers)
    ):
        issues.append(_issue("BACKGROUND_LAYER_INVALID", "elements", "every background element layer must be below every non-background element layer"))
    return issues


def background_element_modes(spec: dict[str, Any]) -> dict[str, str]:
    """Return selected background modes, rejecting duplicate bindings."""
    items, _issues = _background_module(spec) if isinstance(spec, dict) else (None, [])
    if items is None:
        return {}
    modes: dict[str, str] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        element_id = item.get("bound_element_id")
        mode = item.get("selected_mode")
        if not isinstance(element_id, str):
            continue
        if not isinstance(mode, str) or mode not in MODES:
            raise ToolError(
                "BACKGROUND_INCOMPLETE",
                f"modules.background.items[{index}].selected_mode",
                "selected_mode is invalid",
            )
        if element_id in modes:
            raise ToolError("BACKGROUND_BINDING_CONFLICT", f"modules.background.items[{index}].bound_element_id", f"duplicate background binding: {element_id}")
        modes[element_id] = mode
    return modes


def resolved_element_mode_map(spec: dict[str, Any]) -> dict[str, str]:
    """Merge representation and background modes without silent overwrite."""
    resolved = element_mode_map(spec)
    for element_id, mode in background_element_modes(spec).items():
        if element_id in resolved:
            raise ToolError("BACKGROUND_BINDING_CONFLICT", "modules.background", f"element is bound by both representation and background contracts: {element_id}")
        resolved[element_id] = mode
    return resolved


def _postbuild_issue(code: str, path: str, detail: str) -> dict[str, str]:
    return {"code": code, "path": path, "detail": detail}


def _append_postbuild_issue(
    report: dict[str, Any],
    item: dict[str, Any] | None,
    code: str,
    path: str,
    detail: str,
) -> None:
    issue = _postbuild_issue(code, path, detail)
    if issue not in report["errors"]:
        report["errors"].append(issue)
    if item is not None and issue not in item["errors"]:
        item["errors"].append(issue)


@dataclass(frozen=True)
class _PostbuildInputError(ToolError):
    file_sha256: str | None = None


def _load_postbuild_json(
    value: dict[str, Any] | str | Path,
    label: str,
) -> tuple[dict[str, Any], str, str | None]:
    """Load one JSON object and return canonical plus optional file identity."""
    if isinstance(value, dict):
        try:
            return value, canonical_json_sha256(value), None
        except (
            TypeError,
            UnicodeError,
            ValueError,
            OverflowError,
            RecursionError,
        ) as exc:
            raise _PostbuildInputError(
                "BACKGROUND_ASSET_INVALID",
                label,
                f"{label} must contain only finite JSON values",
            ) from exc
    if not isinstance(value, (str, Path)):
        raise _PostbuildInputError(
            "BACKGROUND_ASSET_INVALID", label, f"{label} must be an object or path"
        )
    raw_digest: str | None = None
    try:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise OSError("not a regular file")
        raw = path.read_bytes()
        raw_digest = hashlib.sha256(raw).hexdigest()
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise _PostbuildInputError(
            "BACKGROUND_ASSET_INVALID",
            label,
            f"cannot read {label} JSON object",
            file_sha256=raw_digest,
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise _PostbuildInputError(
            "BACKGROUND_ASSET_INVALID",
            label,
            f"cannot read {label} JSON object",
            file_sha256=raw_digest,
        ) from exc
    if not isinstance(payload, dict):
        raise _PostbuildInputError(
            "BACKGROUND_ASSET_INVALID",
            label,
            f"{label} root must be an object",
            file_sha256=raw_digest,
        )
    try:
        canonical_digest = canonical_json_sha256(payload)
    except (
        TypeError,
        UnicodeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise _PostbuildInputError(
            "BACKGROUND_ASSET_INVALID",
            label,
            f"{label} must contain only finite JSON values",
            file_sha256=raw_digest,
        ) from exc
    return payload, canonical_digest, raw_digest


def _postbuild_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "page_id": None,
        "spec_sha256": None,
        "input_spec_sha256": None,
        "pptx_sha256": None,
        "build_report_sha256": None,
        "build_report_file_sha256": None,
        "structure_report_sha256": None,
        "structure_report_file_sha256": None,
        "full_slide_picture_risk": False,
        "items": [],
        "errors": [],
        "valid": False,
    }


def _object_bbox(record: Any) -> list[int] | None:
    if not isinstance(record, dict):
        return None
    bbox = record.get("bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(type(value) is not int for value in bbox)
    ):
        return None
    coordinates = [
        record.get("x"),
        record.get("y"),
        record.get("cx"),
        record.get("cy"),
    ]
    if coordinates != bbox:
        return None
    return list(bbox)


def _background_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    modules = spec.get("modules")
    module = modules.get("background") if isinstance(modules, dict) else None
    items = module.get("items") if isinstance(module, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _background_picture_build_facts(
    build_report: dict[str, Any], background_id: str
) -> list[dict[str, Any]]:
    items = build_report.get("background_pictures")
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and item.get("background_id") == background_id
    ]


def _build_object_facts(
    build_report: dict[str, Any], element_id: str
) -> list[dict[str, Any]]:
    elements = build_report.get("elements")
    element = elements.get(element_id) if isinstance(elements, dict) else None
    objects = element.get("objects") if isinstance(element, dict) else None
    if not isinstance(objects, list):
        return []
    return [item for item in objects if isinstance(item, dict)]


def _matches_bound_object_name(value: Any, exact_name: str | None) -> bool:
    return (
        isinstance(value, str)
        and isinstance(exact_name, str)
        and (value == exact_name or value.startswith(f"{exact_name}:"))
    )


def _typed_postbuild_issues(
    spec: dict[str, Any],
    build_report: dict[str, Any],
    structure_report: dict[str, Any],
) -> list[dict[str, str]]:
    """Return field-level type defects before any keyed or set lookup occurs."""
    issues: list[dict[str, str]] = []

    def invalid(path: str, detail: str) -> None:
        issues.append(_postbuild_issue("BACKGROUND_ASSET_INVALID", path, detail))

    for index, item in enumerate(_background_items(spec)):
        path = f"modules.background.items[{index}]"
        if not isinstance(item.get("selected_mode"), str):
            invalid(f"{path}.selected_mode", "selected_mode must be a string")
        if not isinstance(item.get("bound_element_id"), str):
            invalid(
                f"{path}.bound_element_id", "bound_element_id must be a string"
            )

    elements = build_report.get("elements")
    if not isinstance(elements, dict):
        invalid("build_report.elements", "elements must be an object")
    else:
        for element_id, element in elements.items():
            path = f"build_report.elements.{element_id}"
            if not isinstance(element, dict):
                invalid(path, "build element fact must be an object")
                continue
            if not isinstance(element.get("semantic_kind"), str):
                invalid(f"{path}.semantic_kind", "semantic_kind must be a string")
            if not isinstance(element.get("selected_mode"), str):
                invalid(f"{path}.selected_mode", "selected_mode must be a string")
            objects = element.get("objects")
            if not isinstance(objects, list):
                invalid(f"{path}.objects", "objects must be an array")
            else:
                for object_index, fact in enumerate(objects):
                    object_path = f"{path}.objects[{object_index}]"
                    if not isinstance(fact, dict):
                        invalid(object_path, "build object fact must be an object")
                        continue
                    if not isinstance(fact.get("ooxml_name"), str):
                        invalid(
                            f"{object_path}.ooxml_name",
                            "ooxml_name must be a string",
                        )
                    if not isinstance(fact.get("object_type"), str):
                        invalid(
                            f"{object_path}.object_type",
                            "object_type must be a string",
                        )
                    bbox = fact.get("bbox")
                    if (
                        not isinstance(bbox, list)
                        or len(bbox) != 4
                        or any(type(value) is not int for value in bbox)
                    ):
                        invalid(
                            f"{object_path}.bbox",
                            "bbox and coordinate facts must contain four integers",
                        )

    background_pictures = build_report.get("background_pictures")
    if not isinstance(background_pictures, list):
        invalid(
            "build_report.background_pictures",
            "background_pictures must be an array",
        )
    elif not all(isinstance(value, dict) for value in background_pictures):
        invalid(
            "build_report.background_pictures",
            "background picture facts must be objects",
        )

    for field in ("structure_objects", "picture_objects"):
        records = structure_report.get(field)
        if not isinstance(records, list):
            invalid(f"structure_report.{field}", f"{field} must be an array")
            continue
        for index, record in enumerate(records):
            path = f"structure_report.{field}[{index}]"
            if not isinstance(record, dict):
                invalid(path, "snapshot fact must be an object")
                continue
            if not isinstance(record.get("object_name"), str):
                invalid(f"{path}.object_name", "object_name must be a string")
            if not isinstance(record.get("object_type"), str):
                invalid(f"{path}.object_type", "object_type must be a string")
            if type(record.get("layer")) is not int:
                invalid(f"{path}.layer", "layer must be an integer")
            if type(record.get("visible")) is not bool:
                invalid(f"{path}.visible", "visible must be a boolean")
            if _object_bbox(record) is None:
                invalid(
                    f"{path}.bbox",
                    "bbox and coordinate facts must contain four integers",
                )
            if field == "picture_objects" and type(record.get("full_slide")) is not bool:
                invalid(f"{path}.full_slide", "full_slide must be a boolean")
    return issues


def _trusted_pptx_snapshot(path: Path) -> dict[str, Any]:
    """Load the validator lazily so its background-contract import cannot cycle."""
    validator = importlib.import_module("validate_pptx")
    return validator.trusted_object_snapshot(path)


_SHARED_SNAPSHOT_FIELDS = (
    "slide_part",
    "object_id",
    "object_name",
    "object_type",
    "layer",
    "hidden",
    "x",
    "y",
    "cx",
    "cy",
    "bbox",
    "geometry_known",
    "visible",
    "media_sha256",
)
_STRUCTURE_SNAPSHOT_FIELDS = (
    *_SHARED_SNAPSHOT_FIELDS,
    "has_text",
    "rotation",
    "text_summary",
    "font_declarations",
)
_PICTURE_SNAPSHOT_FIELDS = (
    "object_key",
    "slide_position",
    *_SHARED_SNAPSHOT_FIELDS,
    "relationship_id",
    "media_part",
    "media_basename",
    "full_slide",
)


def _snapshot_multiset(
    records: list[dict[str, Any]], *, snapshot_kind: str = "shared"
) -> Counter[tuple[Any, ...]]:
    """Return an exact object-fact multiset without trusting record order."""
    def hashable(value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        return value

    fields = {
        "shared": _SHARED_SNAPSHOT_FIELDS,
        "structure": _STRUCTURE_SNAPSHOT_FIELDS,
        "picture": _PICTURE_SNAPSHOT_FIELDS,
    }[snapshot_kind]
    values: list[tuple[Any, ...]] = []
    for record in records:
        values.append(tuple(hashable(record.get(field)) for field in fields))
    return Counter(values)


def _local_postbuild_issue(
    issues: list[dict[str, str]], code: str, path: str, detail: str
) -> None:
    issue = _postbuild_issue(code, path, detail)
    if issue not in issues:
        issues.append(issue)


def _postbuild_z_order_facts(
    actual_object: dict[str, Any] | None,
    visible_foreground: list[dict[str, Any]],
    path: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return layer evidence and any bottom-layer defect for one background."""
    actual_layer = actual_object.get("layer") if actual_object is not None else None
    foreground_layers = [value.get("layer") for value in visible_foreground]
    facts = {
        "actual_layer": actual_layer,
        "visible_foreground_layers": foreground_layers,
    }
    issues: list[dict[str, str]] = []
    if (
        type(actual_layer) is not int
        or any(type(value) is not int for value in foreground_layers)
        or any(actual_layer >= value for value in foreground_layers)
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_NOT_BOTTOM_LAYER",
            path,
            "background object must be below every visible foreground object",
        )
    return facts, issues


def _postbuild_media_facts(
    declaration: dict[str, Any],
    item_path: str,
    mode: Any,
    background_id: Any,
    expected_name: str | None,
    expected_media: Any,
    expected_full_slide: bool,
    actual_object: dict[str, Any] | None,
    actual_bbox: list[int] | None,
    actual_layer: Any,
    pictures: list[dict[str, Any]],
    build_report: dict[str, Any],
    build_object: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return media/full-slide facts and all native or picture defects."""
    facts = {
        "media_sha256": (
            actual_object.get("media_sha256")
            if actual_object is not None
            else None
        ),
        "full_slide": False,
    }
    issues: list[dict[str, str]] = []
    if mode == "native":
        if (
            actual_object is None
            or "media_sha256" not in actual_object
            or build_object is None
            or "media_sha256" not in build_object
            or actual_object.get("media_sha256") is not None
            or build_object.get("media_sha256") is not None
            or actual_object.get("media_sha256")
            != build_object.get("media_sha256")
        ):
            _local_postbuild_issue(
                issues,
                "BACKGROUND_ASSET_INVALID",
                f"structure_report.structure_objects[{expected_name}].media_sha256",
                "native background structure and build media facts must both be explicitly null",
            )
        return facts, issues

    if mode != "background_picture":
        return facts, issues

    picture_candidates = [
        value for value in pictures if value.get("object_name") == expected_name
    ]
    build_picture_facts = _background_picture_build_facts(
        build_report, str(background_id)
    )
    if len(picture_candidates) != 1 or len(build_picture_facts) != 1:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            f"{item_path}.selected_mode",
            "background picture must have one structure fact and one build fact",
        )
        return facts, issues

    picture = picture_candidates[0]
    build_picture = build_picture_facts[0]
    expected_build_picture = {**declaration, "media_sha256": expected_media}
    facts["media_sha256"] = picture.get("media_sha256")
    facts["full_slide"] = picture.get("full_slide") is True
    if (
        not isinstance(expected_media, str)
        or build_picture != expected_build_picture
        or picture.get("media_sha256") != expected_media
        or build_picture.get("media_sha256") != expected_media
        or actual_object is None
        or actual_object.get("media_sha256") != expected_media
        or build_object is None
        or build_object.get("media_sha256") != expected_media
        or picture.get("bbox") != actual_bbox
        or picture.get("object_type") != "pic"
        or picture.get("layer") != actual_layer
        or picture.get("full_slide") is not expected_full_slide
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            f"structure_report.picture_objects[{expected_name}]",
            "background media, framing, or three-party object facts differ",
        )
    return facts, issues


def _validate_postbuild_item(
    declaration: dict[str, Any],
    index: int,
    spec: dict[str, Any],
    element_map: dict[str, dict[str, Any]],
    structures: list[dict[str, Any]],
    pictures: list[dict[str, Any]],
    visible_foreground: list[dict[str, Any]],
    build_report: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Validate one declaration without skipping independent evidence branches."""
    item_path = f"modules.background.items[{index}]"
    background_id = declaration.get("background_id")
    element_id = declaration.get("bound_element_id")
    mode = declaration.get("selected_mode")
    element = element_map.get(element_id) if isinstance(element_id, str) else None
    expected_name = f"ia:{element_id}" if isinstance(element_id, str) else None
    expected_bbox = (
        list(element.get("slide_bbox"))
        if isinstance(element, dict) and isinstance(element.get("slide_bbox"), list)
        else None
    )
    expected_type = (
        {"native": "sp", "background_picture": "pic"}.get(mode)
        if isinstance(mode, str)
        else None
    )
    expected_media = None
    if mode == "background_picture" and isinstance(element, dict):
        content = element.get("content")
        asset = content.get("asset") if isinstance(content, dict) else None
        expected_media = asset.get("asset_sha256") if isinstance(asset, dict) else None
    canvas = spec.get("canvas")
    slide_size = canvas.get("slide_size_emu") if isinstance(canvas, dict) else None
    expected_full_slide = (
        isinstance(slide_size, list)
        and len(slide_size) == 2
        and expected_bbox == [0, 0, slide_size[0], slide_size[1]]
    )
    expected = {
        "object_name": expected_name,
        "object_type": expected_type,
        "bbox": expected_bbox,
        "declared_layer": element.get("layer") if isinstance(element, dict) else None,
        "media_sha256": expected_media,
        "full_slide": expected_full_slide,
    }
    item_report: dict[str, Any] = {
        "background_id": background_id,
        "role": declaration.get("role"),
        "bound_element_id": element_id,
        "selected_mode": mode,
        "expected": expected,
        "actual": None,
        "errors": [],
        "valid": False,
    }
    issues: list[dict[str, str]] = item_report["errors"]

    candidates = [
        value
        for value in structures
        if _matches_bound_object_name(value.get("object_name"), expected_name)
    ]
    actual_object = candidates[0] if len(candidates) == 1 else None
    actual_bbox = _object_bbox(actual_object)
    actual: dict[str, Any] | None = None
    if actual_object is None:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            f"{item_path}.bound_element_id",
            f"expected exactly one actual object named {expected_name}",
        )
    else:
        actual = {
            "object_name": actual_object.get("object_name"),
            "object_type": actual_object.get("object_type"),
            "bbox": actual_bbox,
            "layer": actual_object.get("layer"),
            "visible": actual_object.get("visible"),
            "media_sha256": actual_object.get("media_sha256"),
            "full_slide": False,
        }
        item_report["actual"] = actual
        if (
            actual_object.get("visible") is not True
            or actual_object.get("geometry_known") is not True
            or actual_object.get("object_name") != expected_name
            or actual_bbox != expected_bbox
            or actual_object.get("object_type") != expected_type
        ):
            _local_postbuild_issue(
                issues,
                "BACKGROUND_ASSET_INVALID",
                f"structure_report.structure_objects[{expected_name}]",
                "background object visibility, bbox, or type differs from its declaration",
            )

    build_objects = _build_object_facts(build_report, str(element_id))
    build_elements = build_report.get("elements")
    build_element = (
        build_elements.get(element_id)
        if isinstance(build_elements, dict) and isinstance(element_id, str)
        else None
    )
    raw_build_objects = (
        build_element.get("objects") if isinstance(build_element, dict) else None
    )
    if (
        not isinstance(build_element, dict)
        or build_element.get("semantic_kind")
        != (element.get("kind") if isinstance(element, dict) else None)
        or build_element.get("selected_mode") != mode
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            f"build_report.elements.{element_id}",
            "build element kind or selected mode differs from the declaration",
        )
    build_candidates = [
        value
        for value in build_objects
        if _matches_bound_object_name(value.get("ooxml_name"), expected_name)
    ]
    build_object = build_candidates[0] if len(build_candidates) == 1 else None
    if (
        not isinstance(raw_build_objects, list)
        or len(raw_build_objects) != 1
        or not all(isinstance(value, dict) for value in raw_build_objects)
        or len(build_objects) != 1
        or build_object is None
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            f"build_report.elements.{element_id}.objects",
            f"expected exactly one build object named {expected_name}",
        )
    elif (
        build_object.get("bbox") != expected_bbox
        or build_object.get("object_type") != expected_type
        or build_object.get("ooxml_name") != expected_name
        or actual is not None
        and (
            build_object.get("ooxml_name") != actual["object_name"]
            or build_object.get("bbox") != actual["bbox"]
            or build_object.get("object_type") != actual["object_type"]
        )
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            f"build_report.elements.{element_id}.objects",
            "build object facts differ from the declaration or actual OOXML",
        )

    z_order_facts, z_order_issues = _postbuild_z_order_facts(
        actual_object,
        visible_foreground,
        f"structure_report.structure_objects[{expected_name}].layer",
    )
    issues.extend(issue for issue in z_order_issues if issue not in issues)
    media_facts, media_issues = _postbuild_media_facts(
        declaration,
        item_path,
        mode,
        background_id,
        expected_name,
        expected_media,
        expected_full_slide,
        actual_object,
        actual_bbox,
        z_order_facts["actual_layer"],
        pictures,
        build_report,
        build_object,
    )
    issues.extend(issue for issue in media_issues if issue not in issues)
    if actual is not None:
        actual.update(media_facts)

    item_report["valid"] = not issues
    proven = (
        item_report["valid"]
        and mode == "background_picture"
        and expected_full_slide
        and actual is not None
        and isinstance(actual.get("object_name"), str)
        and actual.get("full_slide") is True
    )
    return item_report, proven


def _aggregate_full_slide_evidence(
    pictures: list[dict[str, Any]], proven_names: list[str]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return full-slide closure facts and global contamination issues."""
    full_slide_pictures = [
        value for value in pictures if value.get("full_slide") is True
    ]
    actual_names = [
        value.get("object_name")
        for value in full_slide_pictures
        if isinstance(value.get("object_name"), str)
    ]
    facts = {
        "actual_names": actual_names,
        "proven_names": list(proven_names),
    }
    issues: list[dict[str, str]] = []
    if (
        len(actual_names) != len(full_slide_pictures)
        or sorted(actual_names) != sorted(proven_names)
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            "structure_report.picture_objects",
            "every full-slide picture must be uniquely proven by a passing background_picture declaration",
        )
    return facts, issues


def _load_postbuild_inputs(
    spec: dict[str, Any] | str | Path,
    build_report: dict[str, Any] | str | Path,
    structure_report: dict[str, Any] | str | Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Load inputs while keeping canonical and raw-file identities distinct."""
    facts: dict[str, Any] = {
        "spec": {},
        "build_report": {},
        "structure_report": {},
        "page_id": None,
        "spec_sha256": None,
        "input_spec_sha256": None,
        "build_report_sha256": None,
        "build_report_file_sha256": None,
        "structure_report_sha256": None,
        "structure_report_file_sha256": None,
    }
    issues: list[dict[str, str]] = []
    spec_loaded = False
    for label, value, fact_key in (
        ("spec", spec, "spec"),
        ("build_report", build_report, "build_report"),
        ("structure_report", structure_report, "structure_report"),
    ):
        try:
            payload, canonical_digest, file_digest = _load_postbuild_json(
                value, label
            )
        except ToolError as exc:
            _local_postbuild_issue(issues, exc.code, exc.path, exc.detail)
            if label != "spec":
                facts[f"{label}_file_sha256"] = getattr(
                    exc, "file_sha256", None
                )
            continue
        facts[fact_key] = payload
        if label == "spec":
            spec_loaded = True
        else:
            facts[f"{label}_sha256"] = canonical_digest
            facts[f"{label}_file_sha256"] = file_digest

    loaded_spec = facts["spec"]
    page_id = loaded_spec.get("page_id")
    facts["page_id"] = page_id if isinstance(page_id, str) else None
    if spec_loaded:
        try:
            facts["spec_sha256"] = content_spec_sha256(loaded_spec)
            facts["input_spec_sha256"] = input_spec_sha256(loaded_spec)
        except ToolError as exc:
            _local_postbuild_issue(issues, exc.code, exc.path, exc.detail)
    return facts, issues


def _postbuild_pptx_identity(
    pptx_path: str | Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Resolve and hash the current PPTX as independent input evidence."""
    facts = {"resolved_path": None, "pptx_sha256": None}
    issues: list[dict[str, str]] = []
    try:
        resolved = Path(pptx_path).expanduser().resolve()
        if not resolved.is_file():
            raise OSError("not a regular file")
        facts["resolved_path"] = resolved
        facts["pptx_sha256"] = file_sha256(resolved)
    except (OSError, RuntimeError, UnicodeError, ValueError, TypeError):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "pptx",
            "cannot hash the current PPTX",
        )
    return facts, issues


def _postbuild_binding_issues(
    spec: dict[str, Any],
    build_report: dict[str, Any],
    structure_report: dict[str, Any],
    identities: dict[str, Any],
) -> list[dict[str, str]]:
    """Return prebuild, identity, passing-report, and typed-field defects."""
    issues: list[dict[str, str]] = []
    for issue in validate_background_prebuild(spec):
        _local_postbuild_issue(issues, issue.code, issue.path, issue.detail)
    if build_report.get("valid") is not True:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "build_report.valid",
            "build report must be a passing report",
        )
    for field, expected in (
        ("content_spec_sha256", identities["spec_sha256"]),
        ("input_spec_sha256", identities["input_spec_sha256"]),
        ("pptx_sha256", identities["pptx_sha256"]),
    ):
        if not isinstance(expected, str) or build_report.get(field) != expected:
            _local_postbuild_issue(
                issues,
                "BACKGROUND_ASSET_INVALID",
                f"build_report.{field}",
                f"build report does not bind the current {field}",
            )
    if structure_report.get("valid") is not True:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "structure_report.valid",
            "structure report must be a passing report",
        )
    if structure_report.get("pptx_sha256") != identities["pptx_sha256"]:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "structure_report.pptx_sha256",
            "structure report does not bind the current PPTX",
        )
    structure_errors = structure_report.get("errors")
    if not isinstance(structure_errors, list) or structure_errors:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "structure_report.errors",
            "structure report errors must be an empty array",
        )
    for issue in _typed_postbuild_issues(spec, build_report, structure_report):
        if issue not in issues:
            issues.append(issue)
    return issues


def _structure_report_evidence_issue(
    structure_report: dict[str, Any],
) -> dict[str, str] | None:
    """Reject display summaries before they cascade into background defects."""
    complete_arrays = all(
        isinstance(structure_report.get(field), list)
        and all(isinstance(value, dict) for value in structure_report[field])
        for field in ("structure_objects", "picture_objects")
    )
    if (
        structure_report.get("evidence_level") == "summary"
        or structure_report.get("usable_as_background_evidence") is False
        or not complete_arrays
    ):
        return _postbuild_issue(
            "STRUCTURE_REPORT_INCOMPLETE",
            "structure_report",
            (
                "background validation requires complete structure_objects "
                "and picture_objects arrays"
            ),
        )
    return None


def _typed_snapshot_facts(
    structure_report: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Normalize the untrusted report arrays without discarding type defects."""
    facts: dict[str, Any] = {"structures": [], "pictures": []}
    issues: list[dict[str, str]] = []
    for field, fact_key, detail in (
        (
            "structure_objects",
            "structures",
            "structure objects must be a complete object array",
        ),
        (
            "picture_objects",
            "pictures",
            "picture objects must be a complete object array",
        ),
    ):
        records = structure_report.get(field)
        if not isinstance(records, list) or not all(
            isinstance(value, dict) for value in records
        ):
            _local_postbuild_issue(
                issues,
                "BACKGROUND_ASSET_INVALID",
                f"structure_report.{field}",
                detail,
            )
        else:
            facts[fact_key] = records
    return facts, issues


def _trusted_snapshot_closure(
    resolved_pptx: Path | None,
    pptx_sha256: str | None,
    structures: list[dict[str, Any]],
    pictures: list[dict[str, Any]],
    structure_report: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Close report multisets against a PPTX-only trusted snapshot."""
    facts = {"full_slide_picture_risk": False}
    issues: list[dict[str, str]] = []
    if resolved_pptx is None or pptx_sha256 is None:
        return facts, issues
    trusted = _trusted_pptx_snapshot(resolved_pptx)
    trusted_structures = trusted.get("structure_objects", [])
    trusted_pictures = trusted.get("picture_objects", [])
    facts["full_slide_picture_risk"] = (
        trusted.get("full_slide_picture_risk") is True
    )
    if (
        trusted.get("valid") is not True
        or trusted.get("pptx_sha256") != pptx_sha256
        or not isinstance(trusted_structures, list)
        or not all(isinstance(value, dict) for value in trusted_structures)
        or not isinstance(trusted_pictures, list)
        or not all(isinstance(value, dict) for value in trusted_pictures)
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "pptx.object_snapshot",
            "cannot independently rebuild a passing object snapshot from the current PPTX",
        )
        return facts, issues
    if _snapshot_multiset(
        structures, snapshot_kind="structure"
    ) != _snapshot_multiset(trusted_structures, snapshot_kind="structure"):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "structure_report.structure_objects",
            "structure object facts do not exactly match the current PPTX",
        )
    if _snapshot_multiset(
        pictures, snapshot_kind="picture"
    ) != _snapshot_multiset(
        trusted_pictures, snapshot_kind="picture"
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "structure_report.picture_objects",
            "picture object facts do not exactly match the current PPTX",
        )
    structure_pictures = [
        value for value in structures if value.get("object_type") == "pic"
    ]
    if _snapshot_multiset(structure_pictures) != _snapshot_multiset(pictures):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "structure_report.picture_objects",
            "picture facts must close one-to-one with structure picture objects",
        )
    reported_risk = structure_report.get("full_slide_picture_risk")
    if (
        type(reported_risk) is not bool
        or reported_risk is not facts["full_slide_picture_risk"]
    ):
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "structure_report.full_slide_picture_risk",
            "full-slide picture risk differs from the picture object snapshot",
        )
    return facts, issues


def _global_background_facts(
    items: list[dict[str, Any]],
    build_report: dict[str, Any],
    structures: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return declaration summary, picture inventory, and foreground facts."""
    issues: list[dict[str, str]] = []
    expected_summary = {"native": 0, "background_picture": 0}
    for item in items:
        mode = item.get("selected_mode")
        if isinstance(mode, str) and mode in expected_summary:
            expected_summary[mode] += 1
    if build_report.get("background_summary") != expected_summary:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "build_report.background_summary",
            "background summary differs from the current declaration",
        )
    build_picture_items = build_report.get("background_pictures")
    expected_picture_ids = sorted(
        str(item.get("background_id"))
        for item in items
        if item.get("selected_mode") == "background_picture"
    )
    actual_picture_ids = (
        sorted(
            str(item.get("background_id"))
            for item in build_picture_items
            if isinstance(item, dict)
        )
        if isinstance(build_picture_items, list)
        and all(isinstance(item, dict) for item in build_picture_items)
        else None
    )
    if actual_picture_ids != expected_picture_ids:
        _local_postbuild_issue(
            issues,
            "BACKGROUND_ASSET_INVALID",
            "build_report.background_pictures",
            "background picture facts differ from the declared picture backgrounds",
        )
    background_names = {
        f"ia:{item.get('bound_element_id')}"
        for item in items
        if isinstance(item.get("bound_element_id"), str)
    }
    visible_foreground = [
        value
        for value in structures
        if value.get("visible") is True
        and isinstance(value.get("object_name"), str)
        and value.get("object_name") not in background_names
    ]
    facts = {
        "expected_summary": expected_summary,
        "expected_picture_ids": expected_picture_ids,
        "actual_picture_ids": actual_picture_ids,
        "visible_foreground": visible_foreground,
    }
    return facts, issues


def validate_background_postbuild(
    spec: dict[str, Any] | str | Path,
    pptx_path: str | Path,
    build_report: dict[str, Any] | str | Path,
    structure_report: dict[str, Any] | str | Path,
) -> dict[str, Any]:
    """Bind declared backgrounds to the current build and OOXML object snapshot."""
    report = _postbuild_report()
    input_facts, input_issues = _load_postbuild_inputs(
        spec, build_report, structure_report
    )
    loaded_spec = input_facts["spec"]
    loaded_build = input_facts["build_report"]
    loaded_structure = input_facts["structure_report"]
    for field in (
        "page_id",
        "spec_sha256",
        "input_spec_sha256",
        "build_report_sha256",
        "build_report_file_sha256",
        "structure_report_sha256",
        "structure_report_file_sha256",
    ):
        report[field] = input_facts[field]

    pptx_facts, pptx_issues = _postbuild_pptx_identity(pptx_path)
    report["pptx_sha256"] = pptx_facts["pptx_sha256"]
    identities = {**input_facts, **pptx_facts}
    structure_evidence_issue = _structure_report_evidence_issue(loaded_structure)
    if structure_evidence_issue is not None:
        for issue in [*input_issues, *pptx_issues, structure_evidence_issue]:
            _append_postbuild_issue(
                report, None, issue["code"], issue["path"], issue["detail"]
            )
        return report
    for issue in [
        *input_issues,
        *pptx_issues,
        *_postbuild_binding_issues(
            loaded_spec, loaded_build, loaded_structure, identities
        ),
    ]:
        _append_postbuild_issue(
            report, None, issue["code"], issue["path"], issue["detail"]
        )

    items = _background_items(loaded_spec)
    if not items:
        _append_postbuild_issue(
            report,
            None,
            "BACKGROUND_DECLARATION_MISSING",
            "modules.background.items",
            "a background declaration is required",
        )
        return report
    elements = loaded_spec.get("elements")
    element_map = {
        element.get("element_id"): element
        for element in (elements if isinstance(elements, list) else [])
        if isinstance(element, dict)
        and isinstance(element.get("element_id"), str)
    }
    snapshot_facts, snapshot_issues = _typed_snapshot_facts(loaded_structure)
    structures = snapshot_facts["structures"]
    pictures = snapshot_facts["pictures"]
    closure_facts, closure_issues = _trusted_snapshot_closure(
        pptx_facts["resolved_path"],
        report["pptx_sha256"],
        structures,
        pictures,
        loaded_structure,
    )
    report["full_slide_picture_risk"] = closure_facts[
        "full_slide_picture_risk"
    ]
    global_facts, global_issues = _global_background_facts(
        items, loaded_build, structures
    )
    visible_foreground = global_facts["visible_foreground"]
    for issue in [*snapshot_issues, *closure_issues, *global_issues]:
        _append_postbuild_issue(
            report, None, issue["code"], issue["path"], issue["detail"]
        )

    proven_full_slide_picture_names: list[str] = []

    for index, declaration in enumerate(items):
        item_report, proven = _validate_postbuild_item(
            declaration,
            index,
            loaded_spec,
            element_map,
            structures,
            pictures,
            visible_foreground,
            loaded_build,
        )
        report["items"].append(item_report)
        for issue in item_report["errors"]:
            _append_postbuild_issue(
                report,
                item_report,
                issue["code"],
                issue["path"],
                issue["detail"],
            )
        actual = item_report.get("actual")
        if (
            proven
            and isinstance(actual, dict)
            and isinstance(actual.get("object_name"), str)
        ):
            proven_full_slide_picture_names.append(actual["object_name"])

    _full_slide_facts, full_slide_issues = _aggregate_full_slide_evidence(
        pictures, proven_full_slide_picture_names
    )
    for issue in full_slide_issues:
        _append_postbuild_issue(
            report, None, issue["code"], issue["path"], issue["detail"]
        )

    report["valid"] = not report["errors"] and all(
        item.get("valid") is True for item in report["items"]
    )
    return report
