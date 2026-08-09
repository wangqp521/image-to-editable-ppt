"""Single machine-readable source for schema-v2 structural contracts.

The declarations in this module intentionally describe structure only.  File
identity, coordinate mapping, representation readiness, renderer support, and
final-delivery evidence remain enforced by their existing semantic validators.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .hashing import canonical_json_sha256


CONTRACT_ID = "page-reconstruction-v2"
SCHEMA_VERSION = 2

CANONICAL_VALUES = {
    "shape_type": frozenset(
        {"rectangle", "roundRect", "ellipse", "triangle", "chevron", "rightArrow"}
    ),
    "line_dash": frozenset({"solid", "dash", "dot", "dashDot"}),
    "line_arrow": frozenset(
        {"none", "triangle", "stealth", "diamond", "oval", "arrow"}
    ),
    "picture_mode": frozenset({"contain", "cover", "none"}),
    "bullet_type": frozenset({"char", "auto_number", "picture"}),
    "chart_type": frozenset({"pie", "doughnut", "column", "bar", "line"}),
}
BUILDABLE_KINDS = frozenset(
    {"text", "shape", "line", "table", "matrix", "status", "picture", "icon", "chart"}
)
CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})
VERIFICATION_PROFILES = frozenset({"rapid", "reviewed"})
DELIVERY_STATUSES = frozenset(
    {
        "pending",
        "rapid_validated",
        "rapid_validation_failed",
        "reviewed_passed",
        "reviewed_failed",
    }
)
SESSION_MODES = frozenset({"fresh_reconstruction", "same_session_reuse"})
MODULE_NAMES = frozenset(
    {
        "page_layout",
        "typography",
        "icons",
        "special_text",
        "picture_framing",
        "graphics",
        "diagram",
        "chart",
        "high_risk",
        "representation_plan",
        "background",
    }
)

ELEMENT_FIELDS = frozenset(
    {
        "element_id",
        "kind",
        "source_bbox",
        "slide_bbox",
        "layer",
        "editable",
        "confidence",
        "style",
        "content",
    }
)
KIND_STYLE_FIELDS = {
    "text": frozenset(
        {
            "fill",
            "line",
            "margins",
            "vertical_alignment",
            "wrap",
            "rotation",
            "effects",
        }
    ),
    "shape": frozenset(
        {"shape_type", "adjustments", "fill", "line", "effects", "rotation"}
    ),
    "line": frozenset({"line", "head_arrow", "tail_arrow", "rotation"}),
    "table": frozenset({"rotation"}),
    "matrix": frozenset({"rotation"}),
    "status": frozenset({"rotation"}),
    "picture": frozenset({"rotation", "opacity"}),
    "icon": frozenset({"rotation", "opacity"}),
    "chart": frozenset(
        {
            "first_slice_angle",
            "hole_size",
            "gap_width",
            "overlap",
            "chart_area",
            "plot_area",
        }
    ),
}
KIND_CONTENT_FIELDS = {
    "text": frozenset({"text"}),
    "shape": frozenset(),
    "line": frozenset(),
    "table": frozenset({"rows", "columns", "cells"}),
    "matrix": frozenset(
        {"part_defaults", "parts", "repeat_sequence", "allow_overlap"}
    ),
    "status": frozenset(
        {"part_defaults", "parts", "repeat_sequence", "allow_overlap"}
    ),
    "picture": frozenset({"asset", "mode", "crop"}),
    "icon": frozenset({"asset", "mode", "crop"}),
    "chart": frozenset(
        {
            "chart_type",
            "slices",
            "grouping",
            "categories",
            "series",
            "axes",
            "legend",
            "data_labels",
            "display_blanks_as",
        }
    ),
}
KIND_REQUIRED_STYLE_FIELDS = {
    "text": frozenset(),
    "shape": frozenset({"shape_type"}),
    "line": frozenset({"line"}),
    "table": frozenset(),
    "matrix": frozenset(),
    "status": frozenset(),
    "picture": frozenset(),
    "icon": frozenset(),
    "chart": frozenset(),
}
KIND_REQUIRED_CONTENT_FIELDS = {
    "text": frozenset({"text"}),
    "shape": frozenset(),
    "line": frozenset(),
    "table": frozenset({"rows", "columns", "cells"}),
    "matrix": frozenset({"part_defaults"}),
    "status": frozenset({"part_defaults"}),
    "picture": frozenset({"asset", "mode", "crop"}),
    "icon": frozenset({"asset", "mode", "crop"}),
    "chart": frozenset({"chart_type", "data_labels"}),
}
CHART_SLICE_FIELDS = frozenset({"category", "value", "color", "value_source"})
CHART_DATA_LABEL_FIELDS = frozenset(
    {
        "enabled",
        "show_category",
        "show_value",
        "show_percentage",
        "position",
        "number_format",
        "font_size",
        "font_weight",
        "color",
    }
)
CHART_LABEL_POSITIONS = frozenset(
    {"best_fit", "center", "inside_end", "outside_end"}
)
CHART_VALUE_SOURCES = frozenset({"explicit", "derived_complement"})
CHART_GROUPINGS = frozenset({"clustered", "stacked", "percent_stacked", "standard"})
CHART_MARKER_STYLES = frozenset({"none", "circle", "square", "diamond", "triangle"})
CHART_LEGEND_POSITIONS = frozenset({"top", "bottom", "left", "right"})
CHART_DISPLAY_BLANKS = frozenset({"gap"})
CHART_AXIS_POSITIONS = frozenset({"top", "bottom", "left", "right"})
CHART_AXIS_LABEL_POSITIONS = frozenset({"next_to_axis", "low", "high", "none"})
CHART_LINE_DASHES = frozenset({"solid", "dash", "dot", "dashDot"})
CARTESIAN_DATA_LABEL_FIELDS = frozenset(
    {
        "enabled",
        "show_category",
        "show_series_name",
        "show_value",
        "position",
        "number_format",
        "font_name",
        "font_size",
        "font_weight",
        "color",
    }
)
CHART_SERIES_FIELDS = frozenset(
    {"name", "values", "color", "line", "marker", "smooth"}
)
CHART_SERIES_LINE_FIELDS = frozenset({"width", "dash"})
CHART_MARKER_FIELDS = frozenset(
    {"style", "size", "fill", "line_color", "line_width"}
)
CHART_CATEGORY_AXIS_FIELDS = frozenset(
    {
        "visible",
        "position",
        "reverse_order",
        "label_position",
        "font_name",
        "font_size",
        "font_weight",
        "color",
        "line",
    }
)
CHART_VALUE_AXIS_FIELDS = frozenset(
    {
        "visible",
        "position",
        "minimum",
        "maximum",
        "major_unit",
        "number_format",
        "font_name",
        "font_size",
        "font_weight",
        "color",
        "line",
        "major_gridlines",
    }
)
CHART_GRIDLINES_FIELDS = frozenset({"visible", "line"})
CHART_LEGEND_FIELDS = frozenset(
    {
        "enabled",
        "position",
        "overlay",
        "font_name",
        "font_size",
        "font_weight",
        "color",
    }
)
PART_FIELDS = frozenset({"part_id", "slide_bbox", "style", "content"})
PART_STYLE_FIELDS = frozenset(
    {
        "shape_type",
        "adjustments",
        "fill",
        "line",
        "effects",
        "rotation",
        "text_style",
    }
)
PART_CONTENT_FIELDS = frozenset({"text"})
MULTIPART_CONTENT_FIELDS = frozenset(
    {"part_defaults", "parts", "repeat_sequence", "allow_overlap"}
)
TABLE_CELL_FIELDS = frozenset(
    {
        "row",
        "column",
        "row_span",
        "column_span",
        "text",
        "fill",
        "margins",
        "alignment",
        "vertical_alignment",
        "font",
        "borders",
    }
)
TABLE_MARGIN_FIELDS = frozenset({"left", "right", "top", "bottom"})
TABLE_FONT_FIELDS = frozenset({"name", "size", "weight", "color", "italic"})
TABLE_BORDER_SIDES = frozenset({"left", "right", "top", "bottom"})
TABLE_BORDER_FIELDS = frozenset({"color", "width"})
MULTIPART_TEXT_STYLE_FIELDS = frozenset(
    {
        "font_name",
        "font_size",
        "font_weight",
        "color",
        "italic",
        "alignment",
        "vertical_alignment",
        "margins",
        "wrap",
    }
)

REPRESENTATION_ITEM_FIELDS = frozenset(
    {
        "source_fact_id",
        "semantic_role",
        "source_bbox",
        "required",
        "selected_mode",
        "required_editability",
        "fallback_policy",
        "bound_element_ids",
        "reason",
        "coverage_status",
        "evidence",
    }
)
REPRESENTATION_MODES = frozenset({"native", "composite", "asset"})
EDITABILITY_VALUES = frozenset({"full", "labels_and_geometry", "labels_only", "none"})
FALLBACK_VALUES = frozenset({"forbid", "allow_minimal_asset"})
COVERAGE_VALUES = frozenset({"covered", "not_applicable"})
ASSET_FIELDS = frozenset({"path", "asset_sha256", "pixel_size"})
BACKGROUND_PROVENANCE_FIELDS = frozenset(
    {"kind", "source_path", "source_sha256"}
)
BACKGROUND_ITEM_FIELDS = frozenset(
    {
        "background_id",
        "role",
        "source_bbox",
        "selected_mode",
        "bound_element_id",
        "source_provenance",
        "reason",
        "evidence",
        "contains_foreground_semantics",
    }
)
BACKGROUND_ROLES = frozenset(
    {"base", "texture", "light_bands", "decorative_ambient_layers"}
)
BACKGROUND_MODES = frozenset({"native", "background_picture"})

TEXT_RUN_MODERN_FIELDS = frozenset({"italic", "underline", "strike", "baseline"})
TEXT_RUN_BASELINE_MIN = -100_000
TEXT_RUN_BASELINE_MAX = 100_000
TEXT_CONTRACT_ALLOWED_FIELDS = {
    "item": frozenset(
        {
            "element_id",
            "text",
            "source_font_guess",
            "selected_font",
            "fallback_reason",
            "fallback_trace",
            "runs",
            "paragraphs",
            "text_box",
            "source_layout",
            "internal_font_declaration",
            "font_declaration_verified",
        }
    ),
    "source_layout": frozenset(
        {
            "line_center_distances_pt",
            "text_block_center_offset_y_pt",
        }
    ),
    "text_box": frozenset(
        {
            "x",
            "y",
            "w",
            "h",
            "margins",
            "alignment",
            "vertical_alignment",
            "wrap",
            "overflow",
            "soft_breaks",
            "paragraph_breaks",
        }
    ),
    "paragraph": frozenset(
        {
            "start",
            "end",
            "alignment",
            "line_spacing",
            "space_before",
            "space_after",
            "margin_left",
            "indent",
            "list",
        }
    ),
    "list": frozenset(
        {
            "is_list",
            "level",
            "bullet_type",
            "bullet",
            "bullet_font",
            "bullet_size_mode",
            "bullet_size_value",
            "bullet_color",
            "bullet_asset",
        }
    ),
}
TEXT_RUN_FIELDS = frozenset(
    {
        "start",
        "end",
        "font_size",
        "font_weight",
        "color",
        "letter_spacing",
        *TEXT_RUN_MODERN_FIELDS,
    }
)
TEXT_RUN_LEGACY_FIELDS = frozenset(
    {
        "start",
        "end",
        "font_size",
        "font_weight",
        "color",
        "letter_spacing",
        "decoration",
    }
)

ICON_MODULE_FIELDS = frozenset(
    {
        "schema_version",
        "page_id",
        "slide_coordinate_unit",
        "clean_visual_reference",
        "clean_visual_sha256",
        "icons",
    }
)
ICON_ITEM_FIELDS = frozenset(
    {
        "icon_id",
        "element_id",
        "category",
        "instance_count",
        "repeat_group",
        "semantic_scope",
        "source_bbox",
        "slide_bbox",
        "layer",
        "source_path",
        "source_sha256",
        "crop_mode",
        "padding",
        "background_handling",
        "asset_path",
        "asset_sha256",
        "alpha_mask_sha256",
        "final_width",
        "final_height",
        "sharpness",
        "validation",
        "native_redraw",
        "selectable_picture_verified",
        "object_type",
    }
)


class ContractConstructionError(ValueError):
    """Raised when a record constructor receives stale or missing fields."""


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def _array(items: dict[str, Any], *, minimum: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "array", "items": items}
    if minimum is not None:
        value["minItems"] = minimum
    return value


def _object(
    properties: dict[str, Any],
    *,
    required: set[str] | frozenset[str] | None = None,
    examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": sorted(properties if required is None else required),
        "additionalProperties": False,
    }
    if examples:
        value["examples"] = examples
    return value


def _tuple(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "prefixItems": list(items),
        "items": False,
        "minItems": len(items),
        "maxItems": len(items),
    }


STRING = {"type": "string"}
NON_EMPTY_STRING = {"type": "string", "minLength": 1}
NULLABLE_STRING = {"type": ["string", "null"]}
BOOLEAN = {"type": "boolean"}
NUMBER = {"type": "number"}
INTEGER = {"type": "integer"}
NONNEGATIVE_INTEGER = {"type": "integer", "minimum": 0}
POSITIVE_INTEGER = {"type": "integer", "minimum": 1}
NONNEGATIVE_NUMBER = {"type": "number", "minimum": 0}
POSITIVE_NUMBER = {"type": "number", "exclusiveMinimum": 0}
OPACITY = {"type": "number", "minimum": 0, "maximum": 1}
ROTATION = {"type": "number", "minimum": -360, "maximum": 360}
ANGLE = {"type": "number", "minimum": 0, "exclusiveMaximum": 360}
ABSOLUTE_PATH = {"type": "string", "pattern": "^/", "minLength": 2}
SHA256 = {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
RGB = {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}
BBOX = _tuple(NONNEGATIVE_INTEGER, NONNEGATIVE_INTEGER, POSITIVE_INTEGER, POSITIVE_INTEGER)
SIZE = _tuple(POSITIVE_INTEGER, POSITIVE_INTEGER)
MARGINS = _object(
    {side: NONNEGATIVE_NUMBER for side in ("left", "right", "top", "bottom")}
)


_TEXT_EXAMPLE = {
    "element_id": "title",
    "kind": "text",
    "source_bbox": [80, 60, 640, 72],
    "slide_bbox": [609600, 457200, 4876800, 548640],
    "layer": 2,
    "editable": True,
    "confidence": "high",
    "style": {"fill": "noFill"},
    "content": {"text": "Editable title"},
}
_SHAPE_EXAMPLE = {
    "element_id": "card",
    "kind": "shape",
    "source_bbox": [80, 180, 520, 280],
    "slide_bbox": [609600, 1371600, 3962400, 2133600],
    "layer": 1,
    "editable": True,
    "confidence": "high",
    "style": {
        "shape_type": "roundRect",
        "adjustments": [0.25],
        "fill": {"type": "solid", "color": "#FFFFFF", "opacity": 1},
        "line": {
            "color": "#D0D7DE",
            "width": 12700,
            "dash": "solid",
            "opacity": 1,
        },
        "effects": "none",
        "rotation": 0,
    },
    "content": {},
}
_PICTURE_EXAMPLE = {
    "element_id": "icon-001",
    "kind": "icon",
    "source_bbox": [720, 200, 48, 48],
    "slide_bbox": [5486400, 1524000, 365760, 365760],
    "layer": 3,
    "editable": False,
    "confidence": "high",
    "style": {"rotation": 0, "opacity": 1},
    "content": {
        "asset": {
            "path": "/absolute/work/assets/icons/icon-001.png",
            "asset_sha256": "0" * 64,
            "pixel_size": [48, 48],
        },
        "mode": "none",
        "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
    },
}


_RECORDS: dict[str, dict[str, Any]] = {
    "Reference": _object({"path": ABSOLUTE_PATH, "sha256": SHA256}),
    "SessionArtifact": _object(
        {"path": ABSOLUTE_PATH, "sha256": SHA256, "identity_verified": BOOLEAN}
    ),
    "SessionReuse": _object(
        {
            "mode": {"enum": sorted(SESSION_MODES)},
            "reason": NON_EMPTY_STRING,
            "artifacts": _array(_ref("SessionArtifact")),
        }
    ),
    "Canvas": _object(
        {
            "source_size": SIZE,
            "visual_size": SIZE,
            "page_frame_bbox": BBOX,
            "slide_size_emu": SIZE,
            "mapping_mode": NON_EMPTY_STRING,
            "background": NON_EMPTY_STRING,
        }
    ),
    "CoordinateGrid": _object(
        {
            "cols": POSITIVE_INTEGER,
            "rows": POSITIVE_INTEGER,
            "labels": {"enum": ["none", "x", "y", "both"]},
        }
    ),
    "CoordinateOverlayEvidence": _object(
        {
            "path": ABSOLUTE_PATH,
            "sha256": SHA256,
            "source_sha256": SHA256,
            "manifest_sha256": SHA256,
            "grid": _ref("CoordinateGrid"),
            "inspection": {"const": "passed"},
        }
    ),
    "PageLayoutModule": _object(
        {
            "anchors": _array({"type": "object"}),
            "relationships": _array({"type": "object"}),
            "layout_invariants": _array({}),
            "density_targets": {"type": "object"},
            "coordinate_overlay_evidence": _ref("CoordinateOverlayEvidence"),
        }
    ),
    "Margins": MARGINS,
    "Asset": _object(
        {"path": ABSOLUTE_PATH, "asset_sha256": SHA256, "pixel_size": SIZE}
    ),
    "Crop": _object(
        {
            side: {"type": "number", "minimum": 0, "exclusiveMaximum": 1}
            for side in ("left", "top", "right", "bottom")
        }
    ),
    "Stroke": _object(
        {
            "color": RGB,
            "width": {"type": "integer", "minimum": 1, "maximum": 20_116_800},
            "dash": {"enum": sorted(CANONICAL_VALUES["line_dash"])},
            "opacity": OPACITY,
        }
    ),
    "SolidFill": _object(
        {"type": {"const": "solid"}, "color": RGB, "opacity": OPACITY}
    ),
    "GradientStop": _object(
        {"position": OPACITY, "color": RGB, "opacity": OPACITY}
    ),
    "LinearGradientFill": _object(
        {
            "type": {"const": "linear_gradient"},
            "angle": ANGLE,
            "stops": _array(_ref("GradientStop"), minimum=2),
        }
    ),
    "OuterShadow": _object(
        {
            "color": RGB,
            "opacity": OPACITY,
            "blur_radius": NONNEGATIVE_INTEGER,
            "distance": NONNEGATIVE_INTEGER,
            "angle": ANGLE,
        }
    ),
    "Effects": _object({"outer_shadow": _ref("OuterShadow")}),
    "TextRun": _object(
        {
            "start": NONNEGATIVE_INTEGER,
            "end": POSITIVE_INTEGER,
            "font_size": POSITIVE_NUMBER,
            "font_weight": {"type": "number", "minimum": 1, "maximum": 1000},
            "color": RGB,
            "letter_spacing": NUMBER,
            "italic": BOOLEAN,
            "underline": BOOLEAN,
            "strike": BOOLEAN,
            "baseline": {
                "type": "integer",
                "minimum": TEXT_RUN_BASELINE_MIN,
                "maximum": TEXT_RUN_BASELINE_MAX,
            },
        }
    ),
    "ParagraphList": _object(
        {
            "is_list": BOOLEAN,
            "level": NONNEGATIVE_INTEGER,
            "bullet_type": {"type": ["string", "null"]},
            "bullet": {"type": ["string", "null"]},
            "bullet_font": {"type": ["string", "null"]},
            "bullet_size_mode": {"type": ["string", "null"]},
            "bullet_size_value": {"type": ["number", "null"]},
            "bullet_color": {"type": ["string", "null"]},
            "bullet_asset": {
                "anyOf": [_ref("Asset"), {"type": "null"}]
            },
        },
        required={"is_list", "level", "bullet"},
    ),
    "Paragraph": _object(
        {
            "start": NONNEGATIVE_INTEGER,
            "end": POSITIVE_INTEGER,
            "alignment": {
                "enum": ["left", "center", "right", "justify", "distributed"]
            },
            "line_spacing": POSITIVE_NUMBER,
            "space_before": NONNEGATIVE_NUMBER,
            "space_after": NONNEGATIVE_NUMBER,
            "margin_left": NUMBER,
            "indent": NUMBER,
            "list": _ref("ParagraphList"),
        },
        required={
            "start",
            "end",
            "alignment",
            "line_spacing",
            "space_before",
            "space_after",
            "indent",
            "list",
        },
    ),
    "TextBox": _object(
        {
            "x": NUMBER,
            "y": NUMBER,
            "w": POSITIVE_NUMBER,
            "h": POSITIVE_NUMBER,
            "margins": _ref("Margins"),
            "alignment": {
                "enum": ["left", "center", "right", "justify", "distributed"]
            },
            "vertical_alignment": {"enum": ["top", "middle", "bottom"]},
            "wrap": BOOLEAN,
            "overflow": BOOLEAN,
            "soft_breaks": _array(NONNEGATIVE_INTEGER),
            "paragraph_breaks": _array(NONNEGATIVE_INTEGER),
        }
    ),
    "SourceTextLayout": _object(
        {
            "line_center_distances_pt": _array(POSITIVE_NUMBER),
            "text_block_center_offset_y_pt": NUMBER,
        }
    ),
    "TypographyItem": _object(
        {
            "element_id": NON_EMPTY_STRING,
            "text": NON_EMPTY_STRING,
            "source_font_guess": NON_EMPTY_STRING,
            "selected_font": NON_EMPTY_STRING,
            "fallback_reason": NULLABLE_STRING,
            "fallback_trace": {"type": ["object", "null"]},
            "runs": _array(_ref("TextRun"), minimum=1),
            "paragraphs": _array(_ref("Paragraph"), minimum=1),
            "text_box": _ref("TextBox"),
            "source_layout": _ref("SourceTextLayout"),
            "internal_font_declaration": NON_EMPTY_STRING,
            "font_declaration_verified": BOOLEAN,
        },
        required={
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
        },
        examples=[
            {
                "element_id": "title",
                "text": "Editable title",
                "source_font_guess": "unknown",
                "selected_font": "Microsoft YaHei",
                "fallback_reason": "source_font_uncertain",
                "fallback_trace": None,
                "runs": [
                    {
                        "start": 0,
                        "end": 14,
                        "font_size": 24,
                        "font_weight": 700,
                        "color": "#111111",
                        "letter_spacing": 0,
                        "italic": False,
                        "underline": False,
                        "strike": False,
                        "baseline": 0,
                    }
                ],
                "paragraphs": [
                    {
                        "start": 0,
                        "end": 14,
                        "alignment": "left",
                        "line_spacing": 1,
                        "space_before": 0,
                        "space_after": 0,
                        "indent": 0,
                        "list": {"is_list": False, "level": 0, "bullet": None},
                    }
                ],
                "text_box": {
                    "x": 609600,
                    "y": 457200,
                    "w": 4876800,
                    "h": 548640,
                    "margins": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                    "alignment": "left",
                    "vertical_alignment": "top",
                    "wrap": False,
                    "overflow": False,
                    "soft_breaks": [],
                    "paragraph_breaks": [],
                },
                "internal_font_declaration": "Microsoft YaHei",
                "font_declaration_verified": False,
            }
        ],
    ),
    "TypographyModule": _object(
        {
            "slide_coordinate_unit": {"const": "EMU"},
            "items": _array(_ref("TypographyItem")),
        }
    ),
    "RepresentationItem": _object(
        {
            "source_fact_id": NON_EMPTY_STRING,
            "semantic_role": NON_EMPTY_STRING,
            "source_bbox": BBOX,
            "required": BOOLEAN,
            "selected_mode": {
                "anyOf": [
                    {"enum": sorted(REPRESENTATION_MODES)},
                    {"type": "null"},
                ]
            },
            "required_editability": {"enum": sorted(EDITABILITY_VALUES)},
            "fallback_policy": {"enum": sorted(FALLBACK_VALUES)},
            "bound_element_ids": _array(NON_EMPTY_STRING),
            "reason": NON_EMPTY_STRING,
            "coverage_status": {"enum": sorted(COVERAGE_VALUES)},
            "evidence": _array(NON_EMPTY_STRING, minimum=1),
        },
        examples=[
            {
                "source_fact_id": "fact-title",
                "semantic_role": "title",
                "source_bbox": [80, 60, 640, 72],
                "required": True,
                "selected_mode": "native",
                "required_editability": "full",
                "fallback_policy": "forbid",
                "bound_element_ids": ["title"],
                "reason": "source title is editable text",
                "coverage_status": "covered",
                "evidence": ["/absolute/work/measurement.json"],
            }
        ],
    ),
    "RepresentationPlanModule": _object(
        {"items": _array(_ref("RepresentationItem"))}
    ),
    "BackgroundProvenance": _object(
        {
            "kind": {
                "enum": ["native_measurement", "clean_background_asset"]
            },
            "source_path": ABSOLUTE_PATH,
            "source_sha256": SHA256,
        }
    ),
    "BackgroundItem": _object(
        {
            "background_id": NON_EMPTY_STRING,
            "role": {"enum": sorted(BACKGROUND_ROLES)},
            "source_bbox": BBOX,
            "selected_mode": {"enum": sorted(BACKGROUND_MODES)},
            "bound_element_id": NON_EMPTY_STRING,
            "source_provenance": _ref("BackgroundProvenance"),
            "reason": NON_EMPTY_STRING,
            "evidence": _array(NON_EMPTY_STRING, minimum=1),
            "contains_foreground_semantics": {"const": False},
        }
    ),
    "BackgroundModule": _object(
        {"items": _array(_ref("BackgroundItem"), minimum=1)}
    ),
    "IconItem": _object(
        {
            "icon_id": NON_EMPTY_STRING,
            "element_id": NON_EMPTY_STRING,
            "category": NON_EMPTY_STRING,
            "instance_count": POSITIVE_INTEGER,
            "repeat_group": NULLABLE_STRING,
            "semantic_scope": {"enum": ["icon_only", "intentional_composite"]},
            "source_bbox": BBOX,
            "slide_bbox": BBOX,
            "layer": POSITIVE_INTEGER,
            "source_path": ABSOLUTE_PATH,
            "source_sha256": SHA256,
            "crop_mode": {"const": "alpha_isolation"},
            "padding": NONNEGATIVE_INTEGER,
            "background_handling": NON_EMPTY_STRING,
            "asset_path": ABSOLUTE_PATH,
            "asset_sha256": SHA256,
            "alpha_mask_sha256": SHA256,
            "final_width": POSITIVE_INTEGER,
            "final_height": POSITIVE_INTEGER,
            "sharpness": NON_EMPTY_STRING,
            "validation": {"const": "passed"},
            "native_redraw": {"const": False},
            "selectable_picture_verified": BOOLEAN,
            "object_type": {"const": "picture"},
        },
        examples=[
            {
                "icon_id": "icon-001",
                "element_id": "icon-001",
                "category": "status",
                "instance_count": 1,
                "repeat_group": None,
                "semantic_scope": "icon_only",
                "source_bbox": [720, 200, 48, 48],
                "slide_bbox": [5486400, 1524000, 365760, 365760],
                "layer": 3,
                "source_path": "/absolute/source.png",
                "source_sha256": "0" * 64,
                "crop_mode": "alpha_isolation",
                "padding": 0,
                "background_handling": "transparent",
                "asset_path": "/absolute/work/assets/icons/icon-001.png",
                "asset_sha256": "1" * 64,
                "alpha_mask_sha256": "2" * 64,
                "final_width": 48,
                "final_height": 48,
                "sharpness": "source_pixels",
                "validation": "passed",
                "native_redraw": False,
                "selectable_picture_verified": False,
                "object_type": "picture",
            }
        ],
    ),
    "IconsModule": _object(
        {
            "schema_version": {"const": 2},
            "page_id": {"type": "string", "pattern": "^page-[0-9]{3}$"},
            "slide_coordinate_unit": {"const": "EMU"},
            "clean_visual_reference": ABSOLUTE_PATH,
            "clean_visual_sha256": SHA256,
            "icons": _array(_ref("IconItem"), minimum=1),
        }
    ),
    "ChartSlice": _object(
        {
            "category": NULLABLE_STRING,
            "value": NONNEGATIVE_NUMBER,
            "color": RGB,
            "value_source": {"enum": sorted(CHART_VALUE_SOURCES)},
        }
    ),
    "ChartDataLabels": _object(
        {
            "enabled": BOOLEAN,
            "show_category": BOOLEAN,
            "show_value": BOOLEAN,
            "show_percentage": BOOLEAN,
            "position": {"enum": sorted(CHART_LABEL_POSITIONS)},
            "number_format": NON_EMPTY_STRING,
            "font_size": POSITIVE_NUMBER,
            "font_weight": {"type": "integer", "minimum": 1, "maximum": 1000},
            "color": RGB,
        }
    ),
    "CartesianDataLabels": _object(
        {
            "enabled": BOOLEAN,
            "show_category": BOOLEAN,
            "show_series_name": BOOLEAN,
            "show_value": BOOLEAN,
            "position": {
                "enum": [
                    "above",
                    "below",
                    "center",
                    "inside_base",
                    "inside_end",
                    "left",
                    "outside_end",
                    "right",
                ]
            },
            "number_format": NON_EMPTY_STRING,
            "font_name": NON_EMPTY_STRING,
            "font_size": POSITIVE_NUMBER,
            "font_weight": {"type": "integer", "minimum": 1, "maximum": 1000},
            "color": RGB,
        }
    ),
    "ChartSeriesLine": _object(
        {
            "width": {"type": "integer", "minimum": 1, "maximum": 20116800},
            "dash": {"enum": sorted(CHART_LINE_DASHES)},
        }
    ),
    "ChartMarker": _object(
        {
            "style": {"enum": sorted(CHART_MARKER_STYLES)},
            "size": {"type": "integer", "minimum": 2, "maximum": 72},
            "fill": RGB,
            "line_color": RGB,
            "line_width": {"type": "integer", "minimum": 1, "maximum": 20116800},
        }
    ),
    "ChartSeries": _object(
        {
            "name": NULLABLE_STRING,
            "values": _array({"type": ["number", "null"]}, minimum=1),
            "color": RGB,
            "line": _ref("ChartSeriesLine"),
            "marker": _ref("ChartMarker"),
            "smooth": BOOLEAN,
        },
        required={"name", "values", "color"},
    ),
    "ChartGridlines": _object(
        {
            "visible": BOOLEAN,
            "line": {"oneOf": [{"const": "noFill"}, _ref("Stroke")]},
        }
    ),
    "ChartCategoryAxis": _object(
        {
            "visible": BOOLEAN,
            "position": {"enum": sorted(CHART_AXIS_POSITIONS)},
            "reverse_order": BOOLEAN,
            "label_position": {"enum": sorted(CHART_AXIS_LABEL_POSITIONS)},
            "font_name": NON_EMPTY_STRING,
            "font_size": POSITIVE_NUMBER,
            "font_weight": {"type": "integer", "minimum": 1, "maximum": 1000},
            "color": RGB,
            "line": {"oneOf": [{"const": "noFill"}, _ref("Stroke")]},
        }
    ),
    "ChartValueAxis": _object(
        {
            "visible": BOOLEAN,
            "position": {"enum": sorted(CHART_AXIS_POSITIONS)},
            "minimum": {"type": ["number", "null"]},
            "maximum": {"type": ["number", "null"]},
            "major_unit": {"type": ["number", "null"]},
            "number_format": NON_EMPTY_STRING,
            "font_name": NON_EMPTY_STRING,
            "font_size": POSITIVE_NUMBER,
            "font_weight": {"type": "integer", "minimum": 1, "maximum": 1000},
            "color": RGB,
            "line": {"oneOf": [{"const": "noFill"}, _ref("Stroke")]},
            "major_gridlines": _ref("ChartGridlines"),
        }
    ),
    "ChartAxes": _object(
        {
            "category": _ref("ChartCategoryAxis"),
            "value": _ref("ChartValueAxis"),
        }
    ),
    "ChartLegend": _object(
        {
            "enabled": BOOLEAN,
            "position": {"enum": sorted(CHART_LEGEND_POSITIONS)},
            "overlay": BOOLEAN,
            "font_name": NON_EMPTY_STRING,
            "font_size": POSITIVE_NUMBER,
            "font_weight": {"type": "integer", "minimum": 1, "maximum": 1000},
            "color": RGB,
        }
    ),
    "Region": _object(
        {
            "region_id": NON_EMPTY_STRING,
            "source_bbox": BBOX,
            "slide_bbox": BBOX,
            "layer": INTEGER,
            "padding": _ref("Margins"),
            "element_ids": _array(NON_EMPTY_STRING),
        },
        examples=[
            {
                "region_id": "region-main",
                "source_bbox": [0, 0, 1600, 900],
                "slide_bbox": [0, 0, 12192000, 6858000],
                "layer": 0,
                "padding": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                "element_ids": ["title", "card", "icon-001"],
            }
        ],
    ),
    "Element": _object(
        {
            "element_id": NON_EMPTY_STRING,
            "kind": {"enum": sorted(BUILDABLE_KINDS)},
            "source_bbox": BBOX,
            "slide_bbox": BBOX,
            "layer": INTEGER,
            "editable": BOOLEAN,
            "confidence": {"enum": sorted(CONFIDENCE_VALUES)},
            "style": {"type": "object"},
            "content": {"type": "object"},
        },
        examples=[_TEXT_EXAMPLE, _SHAPE_EXAMPLE, _PICTURE_EXAMPLE],
    ),
    "VisualReviewCoverage": _object(
        {
            field: {"enum": ["checked", "not_applicable"]}
            for field in (
                "canvas_and_regions",
                "objects_and_geometry",
                "text_and_typography",
                "tables_and_matrices",
                "graphics_connectors_charts",
                "pictures_crop_layers",
                "high_risk_regions",
            )
        }
    ),
    "VisualReviewRecord": _object(
        {
            "mode": {"const": "main_agent_read_only_visual_audit"},
            "decision": {"const": "passed"},
            "coverage": _ref("VisualReviewCoverage"),
            "repair_applied": BOOLEAN,
            "post_repair_verification": {"enum": ["not_required", "passed"]},
        }
    ),
    "VisualGate": _object(
        {
            "status": NON_EMPTY_STRING,
            "evidence": _array(NON_EMPTY_STRING),
            "tripwire": {"type": ["object", "null"]},
            "review": {"anyOf": [_ref("VisualReviewRecord"), {"type": "null"}]},
            "pptx": {"type": ["object", "null"]},
            "preview": {"type": ["object", "null"]},
            "report": {"type": ["object", "null"]},
            "render_report": {"type": ["object", "null"]},
            "background_contract": {"type": ["object", "null"]},
            "rendered_text_geometry": {"type": ["object", "null"]},
        },
        required={"status", "evidence", "tripwire"},
    ),
    "EditabilityGate": _object(
        {
            "status": NON_EMPTY_STRING,
            "evidence": _array(NON_EMPTY_STRING),
            "review": {"type": ["object", "null"]},
            "pptx": {"type": ["object", "null"]},
            "validator": {"type": ["object", "null"]},
            "build_spec_snapshot": {
                "anyOf": [_ref("Reference"), {"type": "null"}]
            },
            "build_report": {
                "anyOf": [_ref("Reference"), {"type": "null"}]
            },
        },
        required={"status", "evidence"},
    ),
    "RuntimePreflight": _object(
        {"path": ABSOLUTE_PATH, "sha256": SHA256},
        required={"path", "sha256"},
    ),
}


def _kind_object(
    properties: set[str] | frozenset[str],
    required: set[str] | frozenset[str],
) -> dict[str, Any]:
    return _object({field: {} for field in sorted(properties)}, required=required)


for _kind in sorted(BUILDABLE_KINDS):
    _RECORDS[f"{_kind.title()}Style"] = _kind_object(
        KIND_STYLE_FIELDS[_kind], KIND_REQUIRED_STYLE_FIELDS[_kind]
    )
    _RECORDS[f"{_kind.title()}Content"] = _kind_object(
        KIND_CONTENT_FIELDS[_kind], KIND_REQUIRED_CONTENT_FIELDS[_kind]
    )

_FILL = {
    "oneOf": [
        {"const": "noFill"},
        _ref("SolidFill"),
        _ref("LinearGradientFill"),
    ]
}
_CHART_FILL = {
    "oneOf": [
        {"const": "noFill"},
        _ref("SolidFill"),
    ]
}
_CHART_LINE = {"oneOf": [{"const": "noFill"}, _ref("Stroke")]}
_EFFECTS = {"oneOf": [{"const": "none"}, _ref("Effects")]}
_RECORDS["TextStyle"] = _object(
    {
        "fill": {"const": "noFill"},
        "line": _ref("Stroke"),
        "margins": _ref("Margins"),
        "vertical_alignment": {"enum": ["top", "middle", "bottom"]},
        "wrap": BOOLEAN,
        "rotation": ROTATION,
        "effects": _EFFECTS,
    },
    required=KIND_REQUIRED_STYLE_FIELDS["text"],
)
_RECORDS["TextContent"] = _object(
    {"text": NON_EMPTY_STRING}, required=KIND_REQUIRED_CONTENT_FIELDS["text"]
)
_RECORDS["ShapeStyle"] = _object(
    {
        "shape_type": {"enum": sorted(CANONICAL_VALUES["shape_type"])},
        "adjustments": {
            "type": "array",
            "items": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 0.5,
            },
            "minItems": 1,
            "maxItems": 1,
        },
        "fill": _FILL,
        "line": _ref("Stroke"),
        "effects": _EFFECTS,
        "rotation": ROTATION,
    },
    required=KIND_REQUIRED_STYLE_FIELDS["shape"],
)
_RECORDS["ShapeContent"] = _object({}, required=set())
_RECORDS["LineStyle"] = _object(
    {
        "line": _ref("Stroke"),
        "head_arrow": {"enum": sorted(CANONICAL_VALUES["line_arrow"])},
        "tail_arrow": {"enum": sorted(CANONICAL_VALUES["line_arrow"])},
        "rotation": ROTATION,
    },
    required=KIND_REQUIRED_STYLE_FIELDS["line"],
)
_RECORDS["LineContent"] = _object({}, required=set())
for _kind in ("picture", "icon"):
    _RECORDS[f"{_kind.title()}Style"] = _object(
        {"rotation": ROTATION, "opacity": OPACITY}, required=set()
    )
    _RECORDS[f"{_kind.title()}Content"] = _object(
        {
            "asset": _ref("Asset"),
            "mode": {"enum": sorted(CANONICAL_VALUES["picture_mode"])},
            "crop": _ref("Crop"),
        },
        required=KIND_REQUIRED_CONTENT_FIELDS[_kind],
    )

_RECORDS["ChartAreaStyle"] = _object(
    {
        "fill": _CHART_FILL,
        "line": _CHART_LINE,
    }
)
_RECORDS["ChartStyle"] = _object(
    {
        "first_slice_angle": {"type": "integer", "minimum": 0, "maximum": 359},
        "hole_size": {"type": "integer", "minimum": 10, "maximum": 90},
        "gap_width": {"type": "integer", "minimum": 0, "maximum": 500},
        "overlap": {"type": "integer", "minimum": -100, "maximum": 100},
        "chart_area": _ref("ChartAreaStyle"),
        "plot_area": _ref("ChartAreaStyle"),
    },
    required=KIND_REQUIRED_STYLE_FIELDS["chart"],
)
_RECORDS["ChartContent"] = _object(
    {
        "chart_type": {"enum": sorted(CANONICAL_VALUES["chart_type"])},
        "slices": _array(_ref("ChartSlice"), minimum=2),
        "grouping": {"enum": sorted(CHART_GROUPINGS)},
        "categories": _array(NON_EMPTY_STRING, minimum=1),
        "series": _array(_ref("ChartSeries"), minimum=1),
        "axes": _ref("ChartAxes"),
        "legend": _ref("ChartLegend"),
        "data_labels": {
            "oneOf": [_ref("ChartDataLabels"), _ref("CartesianDataLabels")]
        },
        "display_blanks_as": {"enum": sorted(CHART_DISPLAY_BLANKS)},
    },
    required=KIND_REQUIRED_CONTENT_FIELDS["chart"],
)

_RECORDS["Modules"] = _object(
    {
        "page_layout": _ref("PageLayoutModule"),
        "typography": _ref("TypographyModule"),
        "icons": _ref("IconsModule"),
        "representation_plan": _ref("RepresentationPlanModule"),
        "background": _ref("BackgroundModule"),
        "special_text": {"type": "object"},
        "picture_framing": {"type": "object"},
        "graphics": {"type": "object"},
        "diagram": {"type": "object"},
        "chart": {"type": "object"},
        "high_risk": {"type": "object"},
    },
    required={"page_layout", "typography", "representation_plan", "background"},
)
_RECORDS["PageReconstruction"] = _object(
    {
        "schema_version": {"const": 2},
        "page_id": {"type": "string", "pattern": "^page-[0-9]{3}$"},
        "verification_profile": {"enum": sorted(VERIFICATION_PROFILES)},
        "delivery_status": {"enum": sorted(DELIVERY_STATUSES)},
        "session_reuse": _ref("SessionReuse"),
        "content_reference": _ref("Reference"),
        "clean_visual_reference": _ref("Reference"),
        "canvas": _ref("Canvas"),
        "activated_modules": _array({"enum": sorted(MODULE_NAMES)}),
        "modules": _ref("Modules"),
        "regions": _array(_ref("Region")),
        "elements": _array(_ref("Element")),
        "reading_order": _array(NON_EMPTY_STRING),
        "visual_gate": _ref("VisualGate"),
        "editability_gate": _ref("EditabilityGate"),
        "runtime_preflight": _ref("RuntimePreflight"),
    },
    required={
        "schema_version",
        "page_id",
        "verification_profile",
        "session_reuse",
        "content_reference",
        "clean_visual_reference",
        "canvas",
        "activated_modules",
        "modules",
        "regions",
        "elements",
        "reading_order",
        "visual_gate",
        "editability_gate",
    },
)

PAGE_RECONSTRUCTION_FIELDS = frozenset(
    _RECORDS["PageReconstruction"]["properties"]
)
PAGE_RECONSTRUCTION_REQUIRED_FIELDS = frozenset(
    _RECORDS["PageReconstruction"]["required"]
)
REGION_FIELDS = frozenset(_RECORDS["Region"]["properties"])
REGION_REQUIRED_FIELDS = frozenset(_RECORDS["Region"]["required"])
EXACT_SCHEMA_ENVELOPE_RECORDS = frozenset(
    {
        "PageReconstruction",
        "SessionReuse",
        "Reference",
        "Canvas",
        "Modules",
        "PageLayoutModule",
        "CoordinateOverlayEvidence",
        "Region",
        "VisualGate",
    }
)
_ENVELOPE_ID_FIELDS = {"Region": "region_id"}


def _contract_payload() -> dict[str, Any]:
    return {
        "contract_id": CONTRACT_ID,
        "schema_version": SCHEMA_VERSION,
        "enums": {
            "buildable_kinds": sorted(BUILDABLE_KINDS),
            "capability_values": {
                key: sorted(value) for key, value in sorted(CANONICAL_VALUES.items())
            },
            "modules": sorted(MODULE_NAMES),
            "representation_modes": sorted(REPRESENTATION_MODES),
            "background_modes": sorted(BACKGROUND_MODES),
            "background_roles": sorted(BACKGROUND_ROLES),
            "verification_profiles": sorted(VERIFICATION_PROFILES),
        },
        "records": copy.deepcopy(_RECORDS),
    }


def schema_contract_sha256() -> str:
    """Return the stable identity of declarations excluding the identity itself."""
    return canonical_json_sha256(_contract_payload())


def schema_contract_manifest() -> dict[str, Any]:
    """Return a deterministic JSON-only contract manifest."""
    payload = _contract_payload()
    payload["contract_sha256"] = schema_contract_sha256()
    return payload


def json_schema_document() -> dict[str, Any]:
    """Generate the public Draft 2020-12 structural schema."""
    definitions = copy.deepcopy(_RECORDS)
    element = definitions["Element"]
    element["allOf"] = [
        {
            "if": {"properties": {"kind": {"const": kind}}},
            "then": {
                "properties": {
                    "style": _ref(f"{kind.title()}Style"),
                    "content": _ref(f"{kind.title()}Content"),
                }
            },
        }
        for kind in sorted(BUILDABLE_KINDS)
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://schemas.openai.local/ia-image-to-editable-ppt/page-reconstruction-v2.schema.json",
        "title": "Editable PPT page reconstruction schema v2",
        "description": (
            "Structural schema contract only; semantic, path/hash, representation, "
            "renderer, visual, and final gates remain authoritative in public CLIs."
        ),
        "x-schema-contract-id": CONTRACT_ID,
        "x-schema-contract-sha256": schema_contract_sha256(),
        "$ref": "#/$defs/PageReconstruction",
        "$defs": definitions,
    }


def canonical_json_schema() -> str:
    """Return canonical tracked JSON Schema bytes as UTF-8 text."""
    return (
        json.dumps(
            json_schema_document(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_json_schema(path: str | Path) -> None:
    """Atomically write the generated schema without maintaining a second template."""
    from .atomic_write import atomic_write_bytes

    atomic_write_bytes(path, canonical_json_schema().encode("utf-8"))


def construct_record(name: str, /, **values: Any) -> dict[str, Any]:
    """Construct one exact record and reject missing or stale aliases."""
    contract = _RECORDS.get(name)
    if not isinstance(contract, dict) or contract.get("type") != "object":
        raise ContractConstructionError(f"unknown record contract: {name}")
    allowed = set(contract.get("properties", {}))
    required = set(contract.get("required", []))
    unknown = sorted(set(values) - allowed)
    missing = sorted(required - set(values))
    if unknown:
        raise ContractConstructionError(
            f"{name} has unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise ContractConstructionError(
            f"{name} is missing fields: {', '.join(missing)}"
        )
    return copy.deepcopy(values)


def record_field_diff(name: str, value: Any) -> tuple[list[str], list[str]]:
    """Return stable unknown/missing field lists for one declared record."""
    contract = _RECORDS.get(name)
    if not isinstance(contract, dict) or contract.get("type") != "object":
        raise ContractConstructionError(f"unknown record contract: {name}")
    if not isinstance(value, dict):
        return [], sorted(contract.get("required", []))
    allowed = set(contract.get("properties", {}))
    required = set(contract.get("required", []))
    return sorted(set(value) - allowed), sorted(required - set(value))


def unknown_field_detail(name: str, value: Any) -> str | None:
    """Return the shared stable detail for unknown fields, if any."""
    unknown, _ = record_field_diff(name, value)
    return f"unknown fields: {', '.join(unknown)}" if unknown else None


def _referenced_record(contract: Any) -> str | None:
    if not isinstance(contract, dict):
        return None
    reference = contract.get("$ref")
    prefix = "#/$defs/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        return None
    return reference.removeprefix(prefix)


def _child_path(parent: str, field: str) -> str:
    return field if parent == "$" else f"{parent}.{field}"


def _iter_exact_schema_envelopes(
    record_name: str,
    value: Any,
    path: str,
) -> Iterator[tuple[str, Any, str]]:
    yield record_name, value, path
    if not isinstance(value, dict):
        return
    record = _RECORDS[record_name]
    for field, field_contract in record["properties"].items():
        if field not in value:
            continue
        child_record = _referenced_record(field_contract)
        if child_record in EXACT_SCHEMA_ENVELOPE_RECORDS:
            yield from _iter_exact_schema_envelopes(
                child_record,
                value[field],
                _child_path(path, field),
            )
            continue
        if not isinstance(field_contract, dict):
            continue
        child_record = _referenced_record(field_contract.get("items"))
        if (
            child_record not in EXACT_SCHEMA_ENVELOPE_RECORDS
            or not isinstance(value[field], list)
        ):
            continue
        collection_path = _child_path(path, field)
        identifier_field = _ENVELOPE_ID_FIELDS.get(child_record)
        for index, item in enumerate(value[field]):
            identifier = item.get(identifier_field) if isinstance(item, dict) else None
            item_path = (
                f"{collection_path}.{identifier}"
                if isinstance(identifier, str) and identifier
                else f"{collection_path}[{index}]"
            )
            yield from _iter_exact_schema_envelopes(child_record, item, item_path)


def iter_exact_schema_envelopes(spec: Any) -> Iterator[tuple[str, Any, str]]:
    """Yield exact structural envelopes by following shared schema references."""
    yield from _iter_exact_schema_envelopes("PageReconstruction", spec, "$")


def schema_envelope_issues(spec: Any) -> list[tuple[str, str]]:
    """Return stable path/detail pairs for exact-envelope field differences."""
    issues: list[tuple[str, str]] = []
    for record_name, value, path in iter_exact_schema_envelopes(spec):
        unknown, missing = record_field_diff(record_name, value)
        if unknown:
            issues.append((path, f"unknown fields: {', '.join(unknown)}"))
        if missing:
            issues.append((path, f"missing fields: {', '.join(missing)}"))
    return issues
