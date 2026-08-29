"""Stable identities for immutable reconstruction-spec build state."""

from __future__ import annotations

import json
from typing import Any

from .error_codes import ToolError
from .hashing import canonical_json_sha256


def _validated_copy(spec: dict[str, Any]) -> dict[str, Any]:
    """Return normalized JSON containers after validating canonical JSON input."""
    if not isinstance(spec, dict):
        raise ToolError("SPEC_IDENTITY_INVALID", "$", "spec must be an object")
    try:
        canonical_json = json.dumps(
            spec,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        canonical_json.encode("utf-8")
        return json.loads(canonical_json)
    except (TypeError, UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise ToolError(
            "SPEC_IDENTITY_INVALID", "$", "spec must contain only finite JSON values"
        ) from exc


def build_content_projection(spec: dict[str, Any]) -> dict[str, Any]:
    """Remove only post-build evidence from a deep-copied content specification."""
    projected = _validated_copy(spec)
    for field in (
        "delivery_status",
        "runtime_preflight",
        "visual_gate",
        "editability_gate",
    ):
        projected.pop(field, None)
    modules = projected.get("modules")
    if isinstance(modules, dict):
        modules.pop("high_risk", None)
        typography_module = modules.get("typography")
        typography = (
            typography_module.get("items", [])
            if isinstance(typography_module, dict)
            else []
        )
        for item in typography if isinstance(typography, list) else []:
            if isinstance(item, dict):
                item.pop("font_declaration_verified", None)
        icons_module = modules.get("icons")
        icons = icons_module.get("icons", []) if isinstance(icons_module, dict) else []
        for item in icons if isinstance(icons, list) else []:
            if isinstance(item, dict):
                item.pop("selectable_picture_verified", None)
        picture_module = modules.get("picture_framing")
        pictures = (
            picture_module.get("pictures", [])
            if isinstance(picture_module, dict)
            else []
        )
        for item in pictures if isinstance(pictures, list) else []:
            if isinstance(item, dict):
                item.pop("selectable_picture_verified", None)
    activated = projected.get("activated_modules")
    if isinstance(activated, list):
        projected["activated_modules"] = [
            value for value in activated if value != "high_risk"
        ]
    return projected


def content_spec_sha256(spec: dict[str, Any]) -> str:
    """Return the frozen content identity for a reconstruction specification."""
    return canonical_json_sha256(build_content_projection(spec))


def input_spec_sha256(spec: dict[str, Any]) -> str:
    """Return the identity of the exact supplied reconstruction specification."""
    return canonical_json_sha256(_validated_copy(spec))
