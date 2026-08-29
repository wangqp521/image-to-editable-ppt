"""The deterministic capability registry shared by compiler stages."""

from __future__ import annotations

from typing import Any

from .error_codes import ToolError
from .hashing import canonical_json_sha256
from .schema_contracts import (
    BUILDABLE_KINDS,
    CANONICAL_VALUES,
    TEXT_CONTRACT_ALLOWED_FIELDS,
    TEXT_RUN_BASELINE_MAX,
    TEXT_RUN_BASELINE_MIN,
    TEXT_RUN_FIELDS,
    TEXT_RUN_LEGACY_FIELDS,
    TEXT_RUN_MODERN_FIELDS,
)

# (capability ID prefix, renderer metadata field).  This is the single
# cross-stage mapping for schema enum groups and renderer declarations.
CAPABILITY_METADATA = {
    "shape_type": ("shape", "shape_type"),
    "line_dash": ("line_dash", "line"),
    "line_arrow": ("line_arrow", "line"),
    "picture_mode": ("picture_mode", "mode"),
    "bullet_type": ("bullet_type", "text"),
    "chart_type": ("chart", "chart_type"),
}

ATOMIC_CAPABILITY_METADATA = {
    "chart.area.chart": "chart_area",
    "chart.area.plot": "plot_area",
    "chart.axes": "axes",
    "chart.categories": "categories",
    "chart.data_labels": "data_labels",
    "chart.display_blanks.gap": "display_blanks_as",
    "chart.first_slice_angle": "first_slice_angle",
    "chart.gap_width": "gap_width",
    "chart.gridlines": "axes",
    "chart.grouping": "grouping",
    "chart.hole_size": "hole_size",
    "chart.legend": "legend",
    "chart.overlap": "overlap",
    "chart.series.data": "series",
    "chart.series.explicit_color": "series",
    "chart.series.line": "series",
    "chart.series.marker": "series",
    "chart.series.missing_gap": "series",
    "chart.slice.explicit_color": "slices",
    "chart.slice.value_source": "slices",
    "line.arrowhead": "line",
    "line.opacity": "line",
    "line.rotation": "rotation",
    "line.stroke": "line",
    "picture.asset.local_hash": "asset",
    "picture.crop.explicit": "crop",
    "picture.opacity": "opacity",
    "picture.rotation": "rotation",
    "shape.effect.none": "effects",
    "shape.effect.shadow": "effects",
    "shape.fill.linear_gradient": "fill",
    "shape.fill.no_fill": "fill",
    "shape.fill.solid": "fill",
    "shape.line": "line",
    "shape.rotation": "rotation",
    "shape.roundRect.adjustment": "adjustments",
    "table.merge": "cells",
    "table.cell.local_border": "cells",
    "multipart.repeat_sequence": "repeat_sequence",
    "text.frame.margins": "text",
    "text.frame.no_autofit": "text",
    "text.frame.vertical_alignment": "text",
    "text.frame.wrap": "text",
    "text.paragraph.native_bullet": "text",
    "text.paragraph.picture_bullet": "text",
    "text.run.baseline": "text",
    "text.run.bold": "text",
    "text.run.color": "text",
    "text.run.font": "text",
    "text.run.font_size": "text",
    "text.run.italic": "text",
    "text.run.letter_spacing": "text",
    "text.run.strike": "text",
    "text.run.underline": "text",
}

WORKFLOW_CAPABILITIES = frozenset(
    {
        "workflow.background_contract.v1",
    }
)

TEXT_RUN_MODERN_ALLOWED_FIELDS = TEXT_RUN_FIELDS
TEXT_RUN_LEGACY_ALLOWED_FIELDS = TEXT_RUN_LEGACY_FIELDS


def text_run_allowed_fields(stage: str, run: dict[str, Any]) -> frozenset[str]:
    """Return the exact run keys for modern prebuild or legacy final data."""
    if stage == "final" and not TEXT_RUN_MODERN_FIELDS.intersection(run):
        return TEXT_RUN_LEGACY_ALLOWED_FIELDS
    return TEXT_RUN_MODERN_ALLOWED_FIELDS

_CAPABILITY_GROUPS = {
    group: metadata[0] for group, metadata in CAPABILITY_METADATA.items()
}


def require_supported_value(group: str, value: str, path: str) -> None:
    """Fail closed when an enumerated schema value is not buildable."""
    if not isinstance(group, str):
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            path,
            "capability group must be a string",
            "unknown",
        )
    allowed = CANONICAL_VALUES.get(group)
    if allowed is None:
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            path,
            f"unsupported capability group: {group}",
            group,
        )
    if not isinstance(value, str):
        capability_group = _CAPABILITY_GROUPS.get(group, group)
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            path,
            f"unsupported {group}: value must be a string",
            f"{capability_group}.value",
        )
    if value not in allowed:
        capability_group = _CAPABILITY_GROUPS.get(group, group)
        raise ToolError(
            "UNSUPPORTED_CAPABILITY",
            path,
            f"unsupported {group}: {value}",
            f"{capability_group}.{value}",
        )


def capability_manifest() -> dict[str, Any]:
    """Return JSON-only, deterministically ordered registry data."""
    return {
        "atomic_capabilities": sorted(ATOMIC_CAPABILITY_METADATA),
        "buildable_kinds": sorted(BUILDABLE_KINDS),
        "canonical_values": {
            group: sorted(values) for group, values in sorted(CANONICAL_VALUES.items())
        },
        "workflow_capabilities": sorted(WORKFLOW_CAPABILITIES),
    }


def capability_manifest_sha256() -> str:
    """Return the canonical registry digest bound into build artifacts."""
    return canonical_json_sha256(capability_manifest())
