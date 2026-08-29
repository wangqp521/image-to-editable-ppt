"""Fail-closed element and multipart contracts for the schema v2 compiler."""

from __future__ import annotations

import copy
import math
from typing import Any

from .capabilities import BUILDABLE_KINDS, require_supported_value
from .error_codes import ContractIssue, ToolError
from .geometry import (
    DRAWINGML_PERCENT_SCALE,
    bbox_contains,
    bbox_overlaps,
    bbox_union,
    quantize_drawingml_percentage,
    valid_drawingml_rotation,
    valid_font_size_pt,
    valid_nonnegative_coordinate32,
    validate_bbox,
)
from .schema_contracts import (
    CARTESIAN_DATA_LABEL_FIELDS,
    CHART_AXIS_LABEL_POSITIONS,
    CHART_AXIS_POSITIONS,
    CHART_CATEGORY_AXIS_FIELDS,
    CHART_DATA_LABEL_FIELDS,
    CHART_DISPLAY_BLANKS,
    CHART_GRIDLINES_FIELDS,
    CHART_GROUPINGS,
    CHART_LEGEND_FIELDS,
    CHART_LEGEND_POSITIONS,
    CHART_LINE_DASHES,
    CHART_MARKER_FIELDS,
    CHART_MARKER_STYLES,
    CHART_SERIES_FIELDS,
    CHART_SERIES_LINE_FIELDS,
    CHART_SLICE_FIELDS,
    CHART_VALUE_AXIS_FIELDS,
    ELEMENT_FIELDS,
    KIND_CONTENT_FIELDS,
    KIND_REQUIRED_CONTENT_FIELDS,
    KIND_REQUIRED_STYLE_FIELDS,
    KIND_STYLE_FIELDS,
    MULTIPART_CONTENT_FIELDS,
    MULTIPART_TEXT_STYLE_FIELDS,
    PART_CONTENT_FIELDS,
    PART_FIELDS,
    PART_STYLE_FIELDS,
    TABLE_BORDER_FIELDS,
    TABLE_BORDER_SIDES,
    TABLE_CELL_FIELDS,
    TABLE_FONT_FIELDS,
    TABLE_MARGIN_FIELDS,
)

EXPECTED_OBJECT_TYPES = {
    "text": frozenset({"sp"}),
    "shape": frozenset({"sp"}),
    "line": frozenset({"cxnSp"}),
    "table": frozenset({"graphicFrame"}),
    "matrix": frozenset({"sp"}),
    "status": frozenset({"sp"}),
    "picture": frozenset({"pic"}),
    "icon": frozenset({"pic"}),
    "chart": frozenset({"graphicFrame"}),
}

def expected_object_types(kind: str) -> frozenset[str]:
    """Return OOXML object types permitted for a buildable element kind."""
    return EXPECTED_OBJECT_TYPES.get(kind, frozenset())


def _issue(
    code: str,
    path: str,
    detail: str,
    capability: str | None = None,
) -> ContractIssue:
    return ContractIssue(code, path, detail, capability)


def _element_path(element: Any) -> str:
    if isinstance(element, dict) and isinstance(element.get("element_id"), str) and element["element_id"]:
        return f"elements.{element['element_id']}"
    return "elements.<unknown>"


def _unknown_field_issue(path: str, fields: Any, allowed: frozenset[str]) -> list[ContractIssue]:
    if not isinstance(fields, dict):
        return [_issue("UNSUPPORTED_CAPABILITY", path, "payload must be an object")]
    unknown = sorted(set(fields) - allowed)
    if not unknown:
        return []
    return [
        _issue(
            "UNSUPPORTED_CAPABILITY",
            path,
            f"unknown fields: {', '.join(unknown)}",
        )
    ]


def _deep_merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _multipart_sequence(content: Any, path: str) -> tuple[dict[str, Any], list[Any], str] | None:
    if not isinstance(content, dict):
        return None
    defaults = content.get("part_defaults")
    has_parts = "parts" in content
    has_repeat = "repeat_sequence" in content
    if not isinstance(defaults, dict) or has_parts == has_repeat:
        return None
    sequence_name = "parts" if has_parts else "repeat_sequence"
    sequence = content.get(sequence_name)
    if not isinstance(sequence, list) or not sequence:
        return None
    return defaults, sequence, sequence_name


def expand_multipart_parts(element: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand explicit or repeat parts without mutating the schema element.

    ``repeat_sequence`` is intentionally an explicit ordered list of part
    exceptions.  Each item owns its absolute ``slide_bbox`` so repeated
    rendering cannot accumulate positional drift.
    """
    path = _element_path(element)
    if not isinstance(element, dict) or element.get("kind") not in {"matrix", "status"}:
        raise ToolError("PART_CONTRACT_INVALID", path, "multipart expansion requires matrix or status")
    sequence_data = _multipart_sequence(element.get("content"), f"{path}.content")
    if sequence_data is None:
        raise ToolError("PART_CONTRACT_INVALID", f"{path}.content", "requires part_defaults and exactly one part sequence")
    defaults, sequence, sequence_name = sequence_data
    expanded: list[dict[str, Any]] = []
    for index, item in enumerate(sequence):
        item_path = f"{path}.content.{sequence_name}[{index}]"
        if not isinstance(item, dict):
            raise ToolError("PART_CONTRACT_INVALID", item_path, "part must be an object")
        expanded.append(_deep_merge(defaults, item))
    return expanded


def _validate_round_rect(style: dict[str, Any], path: str) -> list[ContractIssue]:
    if style.get("shape_type") != "roundRect":
        return []
    adjustments = style.get("adjustments")
    invalid_shape = (
        not isinstance(adjustments, list)
        or len(adjustments) != 1
        or type(adjustments[0]) not in {int, float}
        or not 0 < adjustments[0] <= 0.5
    )
    quantized = (
        None
        if invalid_shape
        else quantize_drawingml_percentage(adjustments[0])
    )
    if (
        invalid_shape
        or quantized is None
        or not 1 <= quantized <= DRAWINGML_PERCENT_SCALE // 2
    ):
        return [
            _issue(
                "UNSUPPORTED_CAPABILITY",
                f"{path}.adjustments",
                "roundRect adjustment must quantize from 1 to 50000",
                "shape.roundRect.adjustment",
            )
        ]
    return []


def _validate_part_style_capabilities(
    style: dict[str, Any], path: str
) -> list[ContractIssue]:
    text_style = style.get("text_style")
    if "text_style" in style:
        issues = _unknown_field_issue(
            f"{path}.text_style", text_style, MULTIPART_TEXT_STYLE_FIELDS
        )
        if issues:
            return issues
        assert isinstance(text_style, dict)
        missing = sorted(MULTIPART_TEXT_STYLE_FIELDS - set(text_style))
        if missing:
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{path}.text_style",
                f"missing text_style fields: {', '.join(missing)}",
            )]
        if "margins" in text_style:
            issues = _unknown_field_issue(
                f"{path}.text_style.margins",
                text_style["margins"],
                TABLE_MARGIN_FIELDS,
            )
            if issues:
                return issues
            assert isinstance(text_style["margins"], dict)
            if set(text_style["margins"]) != TABLE_MARGIN_FIELDS:
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{path}.text_style.margins",
                    "all four margins must be non-negative integer EMU",
                )]
            for side in sorted(TABLE_MARGIN_FIELDS):
                if not valid_nonnegative_coordinate32(text_style["margins"][side]):
                    return [_issue(
                        "UNSUPPORTED_CAPABILITY",
                        f"{path}.text_style.margins.{side}",
                        "margin must be a non-negative integer EMU no greater than 2147483647",
                    )]
        scalar_checks = (
            (
                "font_name",
                lambda value: isinstance(value, str) and bool(value),
                "font_name must be a non-empty string",
            ),
            (
                "font_size",
                valid_font_size_pt,
                "font_size must be finite and from 1 to 4000 pt",
            ),
            (
                "font_weight",
                lambda value: type(value) is int and 1 <= value <= 1000,
                "font_weight must be an integer from 1 to 1000",
            ),
            ("color", _valid_rgb, "color must be #RRGGBB"),
            (
                "italic",
                lambda value: type(value) is bool,
                "italic must be boolean",
            ),
            (
                "alignment",
                lambda value: isinstance(value, str)
                and value in {"left", "center", "right", "justify"},
                "unsupported alignment",
            ),
            (
                "vertical_alignment",
                lambda value: isinstance(value, str)
                and value in {"top", "middle", "bottom"},
                "unsupported vertical_alignment",
            ),
            ("wrap", lambda value: type(value) is bool, "wrap must be boolean"),
        )
        for field, validator, detail in scalar_checks:
            if field in text_style and not validator(text_style[field]):
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{path}.text_style.{field}",
                    detail,
                )]
    if "rotation" in style and not valid_drawingml_rotation(style["rotation"]):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.rotation",
            "rotation must have a faithful DrawingML value within (-360, 360)",
        )]
    if "shape_type" in style:
        try:
            require_supported_value("shape_type", style["shape_type"], f"{path}.shape_type")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    line = style.get("line")
    if isinstance(line, dict) and "dash" in line:
        try:
            require_supported_value("line_dash", line["dash"], f"{path}.line.dash")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    return []


def _valid_rgb(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 7
        and value.startswith("#")
        and all(character in "0123456789abcdefABCDEF" for character in value[1:])
    )


def _validate_pie_chart_contract(
    element: dict[str, Any], path: str | None = None
) -> list[ContractIssue]:
    """Validate the closed simple 2D single-series pie/doughnut contract."""
    path = path or _element_path(element)
    style = element.get("style")
    content = element.get("content")
    if not isinstance(style, dict) or not isinstance(content, dict):
        return [_issue("UNSUPPORTED_CAPABILITY", path, "chart style and content must be objects")]
    issues = _unknown_field_issue(
        f"{path}.style", style, frozenset({"first_slice_angle", "hole_size"})
    )
    if issues:
        return issues
    issues = _unknown_field_issue(
        f"{path}.content",
        content,
        frozenset({"chart_type", "slices", "data_labels"}),
    )
    if issues:
        return issues

    chart_type = content.get("chart_type")
    if chart_type not in {"pie", "doughnut"}:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.chart_type",
            "pie chart contract requires pie or doughnut",
            f"chart.{chart_type}",
        )]
    try:
        require_supported_value("chart_type", chart_type, f"{path}.content.chart_type")
    except ToolError as exc:
        return [_issue(exc.code, exc.path, exc.detail, exc.capability)]

    angle = style.get("first_slice_angle")
    if type(angle) is not int or not 0 <= angle <= 359:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.style.first_slice_angle",
            "first_slice_angle must be an integer from 0 to 359",
            "chart.first_slice_angle",
        )]
    hole_size = style.get("hole_size")
    if chart_type == "pie" and "hole_size" in style:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.style.hole_size",
            "pie must not declare hole_size",
            "chart.hole_size",
        )]
    if chart_type == "doughnut" and (
        type(hole_size) is not int or not 10 <= hole_size <= 90
    ):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.style.hole_size",
            "doughnut hole_size must be an integer from 10 to 90",
            "chart.hole_size",
        )]

    slices = content.get("slices")
    if not isinstance(slices, list) or len(slices) < 2:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.slices",
            "chart requires at least two slices",
        )]
    total = 0.0
    derived: list[dict[str, Any]] = []
    explicit: list[dict[str, Any]] = []
    for index, item in enumerate(slices):
        item_path = f"{path}.content.slices[{index}]"
        issues = _unknown_field_issue(item_path, item, CHART_SLICE_FIELDS)
        if issues:
            return issues
        assert isinstance(item, dict)
        missing = sorted(CHART_SLICE_FIELDS - set(item))
        if missing:
            return [_issue(
                "UNSUPPORTED_CAPABILITY", item_path, f"missing fields: {', '.join(missing)}"
            )]
        category = item["category"]
        if category is not None and (not isinstance(category, str) or not category):
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{item_path}.category",
                "category must be null or a non-empty string",
            )]
        value = item["value"]
        if (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or value < 0
        ):
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{item_path}.value",
                "slice value must be finite and non-negative",
            )]
        if not _valid_rgb(item["color"]):
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{item_path}.color",
                "slice color must be #RRGGBB",
                "chart.slice.explicit_color",
            )]
        value_source = item["value_source"]
        if value_source not in {"explicit", "derived_complement"}:
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{item_path}.value_source",
                "value_source must be explicit or derived_complement",
                "chart.slice.value_source",
            )]
        total += float(value)
        (derived if value_source == "derived_complement" else explicit).append(item)
    if total <= 0:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.slices",
            "slice values must have a positive total",
        )]
    if derived and (
        len(slices) != 2
        or len(derived) != 1
        or len(explicit) != 1
        or not 0 <= explicit[0]["value"] <= 100
        or derived[0]["value"] != 100 - explicit[0]["value"]
    ):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.slices",
            "derived_complement requires exactly 100 minus one explicit percentage in a two-slice chart",
            "chart.slice.value_source",
        )]

    labels = content.get("data_labels")
    issues = _unknown_field_issue(
        f"{path}.content.data_labels", labels, CHART_DATA_LABEL_FIELDS
    )
    if issues:
        return issues
    assert isinstance(labels, dict)
    missing = sorted(CHART_DATA_LABEL_FIELDS - set(labels))
    if missing:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.data_labels",
            f"missing fields: {', '.join(missing)}",
        )]
    for field in ("enabled", "show_category", "show_value", "show_percentage"):
        if type(labels[field]) is not bool:
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{path}.content.data_labels.{field}",
                f"{field} must be boolean",
                "chart.data_labels",
            )]
    if labels["position"] not in {"best_fit", "center", "inside_end", "outside_end"}:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.data_labels.position",
            "unsupported data label position",
            "chart.data_labels",
        )]
    if not isinstance(labels["number_format"], str) or not labels["number_format"]:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.data_labels.number_format",
            "number_format must be a non-empty string",
            "chart.data_labels",
        )]
    if not valid_font_size_pt(labels["font_size"]):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.data_labels.font_size",
            "font_size must be finite and from 1 to 4000 pt",
            "chart.data_labels",
        )]
    if type(labels["font_weight"]) is not int or not 1 <= labels["font_weight"] <= 1000:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.data_labels.font_weight",
            "font_weight must be an integer from 1 to 1000",
            "chart.data_labels",
        )]
    if not _valid_rgb(labels["color"]):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.data_labels.color",
            "data label color must be #RRGGBB",
            "chart.data_labels",
        )]
    if labels["show_category"] and any(item["category"] is None for item in slices):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.slices",
            "category cannot be null when show_category is true",
            "chart.data_labels",
        )]
    return []


def _chart_exact_fields(
    path: str,
    value: Any,
    allowed: frozenset[str],
    required: frozenset[str] | None = None,
) -> list[ContractIssue]:
    issues = _unknown_field_issue(path, value, allowed)
    if issues:
        return issues
    assert isinstance(value, dict)
    missing = sorted((required or allowed) - set(value))
    if not missing:
        return []
    return [_issue(
        "UNSUPPORTED_CAPABILITY",
        path,
        f"missing fields: {', '.join(missing)}",
    )]


def _validate_chart_line_style(value: Any, path: str) -> list[ContractIssue]:
    if value == "noFill":
        return []
    allowed = frozenset({"color", "width", "dash", "opacity"})
    issues = _chart_exact_fields(path, value, allowed)
    if issues:
        return issues
    assert isinstance(value, dict)
    if not _valid_rgb(value["color"]):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.color", "line color must be #RRGGBB")]
    if type(value["width"]) is not int or not 1 <= value["width"] <= 20116800:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.width", "line width must be an integer from 1 to 20116800 EMU")]
    if value["dash"] not in CHART_LINE_DASHES:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.dash", "unsupported chart line dash")]
    opacity = value["opacity"]
    if type(opacity) not in {int, float} or not math.isfinite(opacity) or not 0 <= opacity <= 1:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.opacity", "line opacity must be finite from 0 to 1")]
    return []


def _validate_chart_area(value: Any, path: str) -> list[ContractIssue]:
    issues = _chart_exact_fields(path, value, frozenset({"fill", "line"}))
    if issues:
        return issues
    assert isinstance(value, dict)
    fill = value["fill"]
    if fill != "noFill":
        fill_issues = _chart_exact_fields(
            f"{path}.fill", fill, frozenset({"type", "color", "opacity"})
        )
        if fill_issues:
            return fill_issues
        assert isinstance(fill, dict)
        if fill["type"] != "solid":
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.fill.type", "chart area fill must be solid or noFill")]
        if not _valid_rgb(fill["color"]):
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.fill.color", "fill color must be #RRGGBB")]
        opacity = fill["opacity"]
        if type(opacity) not in {int, float} or not math.isfinite(opacity) or not 0 <= opacity <= 1:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.fill.opacity", "fill opacity must be finite from 0 to 1")]
    return _validate_chart_line_style(value["line"], f"{path}.line")


def _validate_chart_font(value: dict[str, Any], path: str) -> list[ContractIssue]:
    if not isinstance(value["font_name"], str) or not value["font_name"]:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.font_name", "font_name must be a non-empty string")]
    if not valid_font_size_pt(value["font_size"]):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.font_size", "font_size must be finite and from 1 to 4000 pt")]
    if type(value["font_weight"]) is not int or not 1 <= value["font_weight"] <= 1000:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.font_weight", "font_weight must be an integer from 1 to 1000")]
    if not _valid_rgb(value["color"]):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.color", "font color must be #RRGGBB")]
    return []


def _validate_cartesian_labels(
    value: Any, path: str, chart_type: str
) -> list[ContractIssue]:
    issues = _chart_exact_fields(path, value, CARTESIAN_DATA_LABEL_FIELDS)
    if issues:
        return issues
    assert isinstance(value, dict)
    for field in ("enabled", "show_category", "show_series_name", "show_value"):
        if type(value[field]) is not bool:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.{field}", f"{field} must be boolean")]
    positions = (
        {"center", "inside_base", "inside_end", "outside_end"}
        if chart_type in {"column", "bar"}
        else {"above", "below", "center", "left", "right"}
    )
    if value["position"] not in positions:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.position", "unsupported data label position for chart type")]
    if not isinstance(value["number_format"], str) or not value["number_format"]:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.number_format", "number_format must be a non-empty string")]
    return _validate_chart_font(value, path)


def _validate_chart_axes(
    value: Any, path: str, chart_type: str, grouping: str
) -> list[ContractIssue]:
    issues = _chart_exact_fields(path, value, frozenset({"category", "value"}))
    if issues:
        return issues
    assert isinstance(value, dict)
    category = value["category"]
    category_path = f"{path}.category"
    issues = _chart_exact_fields(category_path, category, CHART_CATEGORY_AXIS_FIELDS)
    if issues:
        return issues
    assert isinstance(category, dict)
    if type(category["visible"]) is not bool or type(category["reverse_order"]) is not bool:
        return [_issue("UNSUPPORTED_CAPABILITY", category_path, "axis visibility and reverse_order must be boolean")]
    if category["position"] not in CHART_AXIS_POSITIONS:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{category_path}.position", "unsupported category axis position")]
    allowed_category_positions = {"left", "right"} if chart_type == "bar" else {"top", "bottom"}
    if category["position"] not in allowed_category_positions:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{category_path}.position", "category axis position conflicts with chart direction")]
    if category["label_position"] not in CHART_AXIS_LABEL_POSITIONS:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{category_path}.label_position", "unsupported category label position")]
    issues = _validate_chart_font(category, category_path)
    if issues:
        return issues
    issues = _validate_chart_line_style(category["line"], f"{category_path}.line")
    if issues:
        return issues

    value_axis = value["value"]
    value_path = f"{path}.value"
    issues = _chart_exact_fields(value_path, value_axis, CHART_VALUE_AXIS_FIELDS)
    if issues:
        return issues
    assert isinstance(value_axis, dict)
    if type(value_axis["visible"]) is not bool:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{value_path}.visible", "axis visibility must be boolean")]
    if value_axis["position"] not in CHART_AXIS_POSITIONS:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{value_path}.position", "unsupported value axis position")]
    allowed_value_positions = {"top", "bottom"} if chart_type == "bar" else {"left", "right"}
    if value_axis["position"] not in allowed_value_positions:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{value_path}.position", "value axis position conflicts with chart direction")]
    numeric: dict[str, float | None] = {}
    for field in ("minimum", "maximum", "major_unit"):
        item = value_axis[field]
        if item is not None and (
            type(item) not in {int, float} or not math.isfinite(item)
        ):
            return [_issue("UNSUPPORTED_CAPABILITY", f"{value_path}.{field}", f"{field} must be null or finite")]
        numeric[field] = None if item is None else float(item)
    if numeric["minimum"] is not None and numeric["maximum"] is not None and numeric["minimum"] >= numeric["maximum"]:
        return [_issue("UNSUPPORTED_CAPABILITY", value_path, "value axis minimum must be less than maximum")]
    if numeric["major_unit"] is not None and numeric["major_unit"] <= 0:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{value_path}.major_unit", "major_unit must be positive")]
    if not isinstance(value_axis["number_format"], str) or not value_axis["number_format"]:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{value_path}.number_format", "number_format must be a non-empty string")]
    if grouping == "percent_stacked" and any(item is not None for item in numeric.values()):
        if not (
            numeric["minimum"] == 0
            and numeric["maximum"] == 1
            and numeric["major_unit"] is not None
            and 0 < numeric["major_unit"] <= 1
            and "%" in value_axis["number_format"]
        ):
            return [_issue("UNSUPPORTED_CAPABILITY", value_path, "percent_stacked explicit axis requires 0..1 bounds, percentage format, and major_unit in (0,1]")]
    issues = _validate_chart_font(value_axis, value_path)
    if issues:
        return issues
    issues = _validate_chart_line_style(value_axis["line"], f"{value_path}.line")
    if issues:
        return issues
    gridlines = value_axis["major_gridlines"]
    grid_path = f"{value_path}.major_gridlines"
    issues = _chart_exact_fields(grid_path, gridlines, CHART_GRIDLINES_FIELDS)
    if issues:
        return issues
    assert isinstance(gridlines, dict)
    if type(gridlines["visible"]) is not bool:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{grid_path}.visible", "gridline visibility must be boolean")]
    return _validate_chart_line_style(gridlines["line"], f"{grid_path}.line")


def _validate_chart_legend(value: Any, path: str) -> list[ContractIssue]:
    issues = _chart_exact_fields(path, value, CHART_LEGEND_FIELDS)
    if issues:
        return issues
    assert isinstance(value, dict)
    if type(value["enabled"]) is not bool or type(value["overlay"]) is not bool:
        return [_issue("UNSUPPORTED_CAPABILITY", path, "legend enabled and overlay must be boolean")]
    if value["position"] not in CHART_LEGEND_POSITIONS:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.position", "unsupported legend position")]
    return _validate_chart_font(value, path)


def _validate_chart_series(
    value: Any,
    path: str,
    chart_type: str,
    category_count: int,
) -> list[ContractIssue]:
    required = (
        CHART_SERIES_FIELDS
        if chart_type == "line"
        else frozenset({"name", "values", "color"})
    )
    issues = _chart_exact_fields(path, value, required)
    if issues:
        return issues
    assert isinstance(value, dict)
    name = value["name"]
    if name is not None and (not isinstance(name, str) or not name):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.name", "series name must be null or a non-empty string")]
    values = value["values"]
    if not isinstance(values, list) or len(values) != category_count:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.values", "series values length must equal categories length")]
    non_missing = 0
    for index, item in enumerate(values):
        if item is None:
            if chart_type != "line":
                return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.values[{index}]", "column and bar values cannot be null")]
            continue
        if type(item) not in {int, float} or not math.isfinite(item):
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.values[{index}]", "series value must be finite or an allowed null gap")]
        non_missing += 1
    if non_missing == 0:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.values", "series requires at least one finite value")]
    if not _valid_rgb(value["color"]):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.color", "series color must be #RRGGBB")]
    if chart_type != "line":
        return []
    line_path = f"{path}.line"
    issues = _chart_exact_fields(line_path, value["line"], CHART_SERIES_LINE_FIELDS)
    if issues:
        return issues
    line = value["line"]
    assert isinstance(line, dict)
    if type(line["width"]) is not int or not 1 <= line["width"] <= 20116800:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{line_path}.width", "line width must be an integer from 1 to 20116800 EMU")]
    if line["dash"] not in CHART_LINE_DASHES:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{line_path}.dash", "unsupported series line dash")]
    marker_path = f"{path}.marker"
    issues = _chart_exact_fields(marker_path, value["marker"], CHART_MARKER_FIELDS)
    if issues:
        return issues
    marker = value["marker"]
    assert isinstance(marker, dict)
    if marker["style"] not in CHART_MARKER_STYLES:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{marker_path}.style", "unsupported marker style")]
    if type(marker["size"]) is not int or not 2 <= marker["size"] <= 72:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{marker_path}.size", "marker size must be an integer from 2 to 72 pt")]
    if not _valid_rgb(marker["fill"]) or not _valid_rgb(marker["line_color"]):
        return [_issue("UNSUPPORTED_CAPABILITY", marker_path, "marker colors must be #RRGGBB")]
    if type(marker["line_width"]) is not int or not 1 <= marker["line_width"] <= 20116800:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{marker_path}.line_width", "marker line_width must be an integer from 1 to 20116800 EMU")]
    if value["smooth"] is not False:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.smooth", "line smooth must be false")]
    return []


def _validate_cartesian_chart_contract(
    element: dict[str, Any], path: str
) -> list[ContractIssue]:
    style = element.get("style")
    content = element.get("content")
    if not isinstance(style, dict) or not isinstance(content, dict):
        return [_issue("UNSUPPORTED_CAPABILITY", path, "chart style and content must be objects")]
    content_fields = frozenset(
        {
            "chart_type",
            "grouping",
            "categories",
            "series",
            "axes",
            "legend",
            "data_labels",
            "display_blanks_as",
        }
    )
    issues = _chart_exact_fields(f"{path}.content", content, content_fields)
    if issues:
        return issues
    chart_type = content["chart_type"]
    if chart_type not in {"column", "bar", "line"}:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.chart_type", "cartesian chart requires column, bar, or line")]
    grouping = content["grouping"]
    if grouping not in CHART_GROUPINGS:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.grouping", "unsupported chart grouping", "chart.grouping")]
    if chart_type == "line" and grouping != "standard":
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.grouping", "line grouping must be standard", "chart.grouping")]
    if chart_type in {"column", "bar"} and grouping not in {"clustered", "stacked", "percent_stacked"}:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.grouping", "column and bar grouping must be clustered, stacked, or percent_stacked", "chart.grouping")]
    if content["display_blanks_as"] not in CHART_DISPLAY_BLANKS:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.display_blanks_as", "display_blanks_as must be gap", "chart.display_blanks.gap")]
    categories = content["categories"]
    if not isinstance(categories, list) or not categories or any(
        not isinstance(item, str) or not item for item in categories
    ):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.categories", "categories must be a non-empty array of non-empty strings")]
    series = content["series"]
    if not isinstance(series, list) or not series:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.series", "series must be a non-empty array")]
    for index, item in enumerate(series):
        issues = _validate_chart_series(
            item, f"{path}.content.series[{index}]", chart_type, len(categories)
        )
        if issues:
            return issues
    issues = _validate_chart_axes(content["axes"], f"{path}.content.axes", chart_type, grouping)
    if issues:
        return issues
    issues = _validate_chart_legend(content["legend"], f"{path}.content.legend")
    if issues:
        return issues
    issues = _validate_cartesian_labels(content["data_labels"], f"{path}.content.data_labels", chart_type)
    if issues:
        return issues

    style_fields = (
        frozenset({"chart_area", "plot_area"})
        if chart_type == "line"
        else frozenset({"gap_width", "overlap", "chart_area", "plot_area"})
    )
    issues = _chart_exact_fields(f"{path}.style", style, style_fields)
    if issues:
        return issues
    for field in ("chart_area", "plot_area"):
        issues = _validate_chart_area(style[field], f"{path}.style.{field}")
        if issues:
            return issues
    if chart_type in {"column", "bar"}:
        if type(style["gap_width"]) is not int or not 0 <= style["gap_width"] <= 500:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.style.gap_width", "gap_width must be an integer from 0 to 500", "chart.gap_width")]
        if type(style["overlap"]) is not int or not -100 <= style["overlap"] <= 100:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.style.overlap", "overlap must be an integer from -100 to 100", "chart.overlap")]
        if grouping in {"stacked", "percent_stacked"} and style["overlap"] != 100:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.style.overlap", "stacked and percent_stacked charts require overlap 100", "chart.overlap")]
    return []


def validate_chart_contract(
    element: dict[str, Any], path: str | None = None
) -> list[ContractIssue]:
    """Validate the discriminated native 2D chart contract."""
    path = path or _element_path(element)
    content = element.get("content") if isinstance(element, dict) else None
    chart_type = content.get("chart_type") if isinstance(content, dict) else None
    try:
        require_supported_value("chart_type", chart_type, f"{path}.content.chart_type")
    except ToolError as exc:
        return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    if chart_type in {"pie", "doughnut"}:
        return _validate_pie_chart_contract(element, path)
    return _validate_cartesian_chart_contract(element, path)


def _validate_table(element: dict[str, Any], path: str) -> list[ContractIssue]:
    content = element["content"]
    rows = content.get("rows")
    columns = content.get("columns")
    cells = content.get("cells")
    if (
        not isinstance(rows, list)
        or not rows
        or any(type(value) is not int or value <= 0 for value in rows)
    ):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.rows", "rows must be a non-empty array of positive EMU heights")]
    if (
        not isinstance(columns, list)
        or not columns
        or any(type(value) is not int or value <= 0 for value in columns)
    ):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.columns", "columns must be a non-empty array of positive EMU widths")]
    try:
        bbox = validate_bbox(element.get("slide_bbox"), f"{path}.slide_bbox")
    except ToolError as exc:
        return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    if sum(rows) != bbox[3] or sum(columns) != bbox[2]:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content", "table row and column sizes must exactly equal slide_bbox")]
    if not isinstance(cells, list) or not cells:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.cells", "cells must be a non-empty array")]

    occupied: set[tuple[int, int]] = set()
    for index, cell in enumerate(cells):
        cell_path = f"{path}.content.cells[{index}]"
        issues = _unknown_field_issue(cell_path, cell, TABLE_CELL_FIELDS)
        if issues:
            return issues
        assert isinstance(cell, dict)
        missing = sorted(TABLE_CELL_FIELDS - set(cell))
        if missing:
            return [_issue("UNSUPPORTED_CAPABILITY", cell_path, f"missing cell fields: {', '.join(missing)}")]
        row = cell["row"]
        column = cell["column"]
        row_span = cell["row_span"]
        column_span = cell["column_span"]
        if any(type(value) is not int for value in (row, column, row_span, column_span)) or row_span <= 0 or column_span <= 0:
            return [_issue("UNSUPPORTED_CAPABILITY", cell_path, "cell coordinates and spans must be positive integers")]
        coordinates = {
            (row_index, column_index)
            for row_index in range(row, row + row_span)
            for column_index in range(column, column + column_span)
        }
        if (
            not coordinates
            or any(
                row_index < 0
                or column_index < 0
                or row_index >= len(rows)
                or column_index >= len(columns)
                for row_index, column_index in coordinates
            )
            or occupied & coordinates
        ):
            return [_issue("UNSUPPORTED_CAPABILITY", cell_path, "cell span is overlapping or out of range")]
        occupied.update(coordinates)
        if not isinstance(cell["text"], str):
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.text", "cell text must be a string")]
        if cell["fill"] != "noFill" and not _valid_rgb(cell["fill"]):
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.fill", "cell fill must be noFill or #RRGGBB")]
        margins = cell["margins"]
        issues = _unknown_field_issue(f"{cell_path}.margins", margins, TABLE_MARGIN_FIELDS)
        if issues:
            return issues
        assert isinstance(margins, dict)
        if set(margins) != TABLE_MARGIN_FIELDS:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.margins", "all four margins must be non-negative integer EMU")]
        for side in sorted(TABLE_MARGIN_FIELDS):
            if not valid_nonnegative_coordinate32(margins[side]):
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{cell_path}.margins.{side}",
                    "margin must be an integer from 0 to 2147483647 EMU",
                )]
        if not isinstance(cell["alignment"], str) or cell["alignment"] not in {
            "left", "center", "right", "justify"
        }:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.alignment", "unsupported cell alignment")]
        if not isinstance(cell["vertical_alignment"], str) or cell[
            "vertical_alignment"
        ] not in {"top", "middle", "bottom"}:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.vertical_alignment", "unsupported cell vertical alignment")]
        font = cell["font"]
        issues = _unknown_field_issue(f"{cell_path}.font", font, TABLE_FONT_FIELDS)
        if issues:
            return issues
        assert isinstance(font, dict)
        if set(font) != TABLE_FONT_FIELDS:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.font", "font contract is incomplete")]
        font_checks = (
            ("name", isinstance(font["name"], str) and bool(font["name"]), "font name must be non-empty"),
            ("size", valid_font_size_pt(font["size"]), "font size must be finite and from 1 to 4000 pt"),
            ("weight", type(font["weight"]) is int and 1 <= font["weight"] <= 1000, "font weight must be an integer from 1 to 1000"),
            ("color", _valid_rgb(font["color"]), "font color must be #RRGGBB"),
            ("italic", type(font["italic"]) is bool, "font italic must be boolean"),
        )
        for field, valid, detail in font_checks:
            if not valid:
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{cell_path}.font.{field}",
                    detail,
                )]
        borders = cell["borders"]
        issues = _unknown_field_issue(f"{cell_path}.borders", borders, TABLE_BORDER_SIDES)
        if issues:
            return issues
        assert isinstance(borders, dict)
        for side, border in borders.items():
            border_path = f"{cell_path}.borders.{side}"
            issues = _unknown_field_issue(border_path, border, TABLE_BORDER_FIELDS)
            if issues:
                return issues
            assert isinstance(border, dict)
            if (
                set(border) != TABLE_BORDER_FIELDS
                or not _valid_rgb(border.get("color"))
                or type(border.get("width")) is not int
                or not 1 <= border["width"] <= 20_116_800
            ):
                return [_issue("UNSUPPORTED_CAPABILITY", border_path, "border requires #RRGGBB color and valid DrawingML width")]
    expected = {
        (row_index, column_index)
        for row_index in range(len(rows))
        for column_index in range(len(columns))
    }
    if occupied != expected:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.cells", "cells must completely cover the table without overlap")]
    rotation = element["style"].get("rotation", 0)
    if not valid_drawingml_rotation(rotation):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.style.rotation",
            "rotation must have a faithful DrawingML value within (-360, 360)",
        )]
    return []


def _validate_multipart(element: dict[str, Any], path: str) -> list[ContractIssue]:
    content = element.get("content")
    issues = _unknown_field_issue(f"{path}.content", content, MULTIPART_CONTENT_FIELDS)
    if issues:
        return issues
    assert isinstance(content, dict)
    sequence_data = _multipart_sequence(content, f"{path}.content")
    if sequence_data is None:
        return [
            _issue(
                "PART_CONTRACT_INVALID",
                f"{path}.content",
                "requires part_defaults and exactly one of parts or repeat_sequence",
            )
        ]
    defaults, sequence, sequence_name = sequence_data
    defaults_issues = _unknown_field_issue(f"{path}.content.part_defaults", defaults, PART_FIELDS - {"part_id", "slide_bbox"})
    if defaults_issues:
        return defaults_issues
    for payload_name, allowed in (("style", PART_STYLE_FIELDS), ("content", PART_CONTENT_FIELDS)):
        if payload_name in defaults:
            payload_issues = _unknown_field_issue(
                f"{path}.content.part_defaults.{payload_name}", defaults[payload_name], allowed
            )
            if payload_issues:
                return payload_issues
    if "style" in defaults:
        default_capability_issues = _validate_part_style_capabilities(
            defaults["style"], f"{path}.content.part_defaults.style"
        )
        if default_capability_issues:
            return default_capability_issues
    if (
        isinstance(defaults.get("content"), dict)
        and "text" in defaults["content"]
        and not isinstance(defaults["content"]["text"], str)
    ):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.part_defaults.content.text",
            "part text must be a string",
        )]
    allow_overlap = content.get("allow_overlap", False)
    if type(allow_overlap) is not bool:
        return [_issue("PART_CONTRACT_INVALID", f"{path}.content.allow_overlap", "must be boolean")]

    try:
        parent_bbox = validate_bbox(element.get("slide_bbox"), f"{path}.slide_bbox")
        parts = expand_multipart_parts(element)
    except ToolError as exc:
        return [_issue(exc.code, exc.path, exc.detail, exc.capability)]

    seen_ids: set[str] = set()
    prior: list[list[int]] = []
    boxes: list[list[int]] = []
    for index, part in enumerate(parts):
        part_path = f"{path}.content.{sequence_name}[{index}]"
        raw_part = sequence[index]
        assert isinstance(raw_part, dict)
        if "style" in raw_part:
            raw_style_issues = _unknown_field_issue(
                f"{part_path}.style", raw_part["style"], PART_STYLE_FIELDS
            )
            if raw_style_issues:
                return raw_style_issues
            assert isinstance(raw_part["style"], dict)
            raw_capability_issues = _validate_part_style_capabilities(
                raw_part["style"], f"{part_path}.style"
            )
            if raw_capability_issues:
                return raw_capability_issues
        unknown = _unknown_field_issue(part_path, part, PART_FIELDS)
        if unknown:
            return unknown
        part_id = part.get("part_id")
        if not isinstance(part_id, str) or not part_id:
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.part_id", "part_id must be non-empty")]
        if part_id in seen_ids:
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.part_id", "part_id must be unique")]
        seen_ids.add(part_id)
        for payload_name, allowed in (("style", PART_STYLE_FIELDS), ("content", PART_CONTENT_FIELDS)):
            if payload_name in part:
                payload_issues = _unknown_field_issue(f"{part_path}.{payload_name}", part[payload_name], allowed)
                if payload_issues:
                    return payload_issues
        if "style" in part:
            capability_issues = _validate_part_style_capabilities(
                part["style"], f"{part_path}.style"
            )
            if capability_issues:
                return capability_issues
        if (
            isinstance(part.get("content"), dict)
            and "text" in part["content"]
            and not isinstance(part["content"]["text"], str)
        ):
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{part_path}.content.text",
                "part text must be a string",
            )]
        try:
            bbox = validate_bbox(part.get("slide_bbox"), f"{part_path}.slide_bbox")
        except ToolError as exc:
            return [_issue("PART_CONTRACT_INVALID", exc.path, exc.detail)]
        if not bbox_contains(parent_bbox, bbox):
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.slide_bbox", "part bbox must be inside parent bbox")]
        if not allow_overlap and any(bbox_overlaps(bbox, previous) for previous in prior):
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.slide_bbox", "overlap requires allow_overlap: true")]
        prior.append(bbox)
        boxes.append(bbox)
    try:
        union = bbox_union(boxes)
    except ToolError as exc:
        return [_issue("PART_CONTRACT_INVALID", exc.path, exc.detail)]
    if union != parent_bbox:
        return [_issue("PART_CONTRACT_INVALID", f"{path}.content", "part union must equal parent slide_bbox")]
    return []


def validate_element_contract(element: Any) -> list[ContractIssue]:
    """Return stable fail-closed issues for one schema v2 element."""
    path = _element_path(element)
    if not isinstance(element, dict):
        return [_issue("UNSUPPORTED_CAPABILITY", path, "element must be an object")]
    unknown = sorted(set(element) - ELEMENT_FIELDS)
    if unknown:
        return [_issue("UNSUPPORTED_CAPABILITY", path, f"unknown fields: {', '.join(unknown)}")]
    missing = sorted(ELEMENT_FIELDS - set(element))
    if missing:
        return [_issue("UNSUPPORTED_CAPABILITY", path, f"missing fields: {', '.join(missing)}")]
    kind = element.get("kind")
    if not isinstance(kind, str) or kind not in BUILDABLE_KINDS:
        return [_issue("UNSUPPORTED_KIND", f"{path}.kind", "kind is not buildable")]
    style = element.get("style")
    content = element.get("content")
    style_issues = _unknown_field_issue(f"{path}.style", style, KIND_STYLE_FIELDS[kind])
    if style_issues:
        return style_issues
    content_issues = _unknown_field_issue(f"{path}.content", content, KIND_CONTENT_FIELDS[kind])
    if content_issues:
        return content_issues
    assert isinstance(style, dict)
    assert isinstance(content, dict)
    missing_style = sorted(KIND_REQUIRED_STYLE_FIELDS[kind] - set(style))
    if missing_style:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.style",
            f"missing fields: {', '.join(missing_style)}",
        )]
    missing_content = sorted(KIND_REQUIRED_CONTENT_FIELDS[kind] - set(content))
    if missing_content:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content",
            f"missing fields: {', '.join(missing_content)}",
        )]

    if kind == "shape":
        try:
            require_supported_value("shape_type", style["shape_type"], f"{path}.style.shape_type")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
        return _validate_round_rect(style, f"{path}.style")
    if kind == "line" and isinstance(style.get("line"), dict) and "dash" in style["line"]:
        try:
            require_supported_value("line_dash", style["line"]["dash"], f"{path}.style.line.dash")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    if kind in {"picture", "icon"} and "mode" in content:
        try:
            require_supported_value("picture_mode", content["mode"], f"{path}.content.mode")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    if kind in {"matrix", "status"}:
        return _validate_multipart(element, path)
    if kind == "table":
        return _validate_table(element, path)
    if kind == "chart":
        return validate_chart_contract(element, path)
    return []
