"""Editable preset-shape renderer with controlled DrawingML effects."""

from __future__ import annotations

import re
from typing import Any

from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu

from lib.capabilities import ATOMIC_CAPABILITY_METADATA, CANONICAL_VALUES
from lib.error_codes import ContractIssue, ToolError
from lib.geometry import (
    DRAWINGML_FULL_CIRCLE,
    DRAWINGML_LINE_WIDTH_MAX,
    DRAWINGML_PERCENT_SCALE,
    quantize_drawingml_angle,
    quantize_drawingml_percentage,
)

from .common import RenderContext, register_renderer
from .ooxml import neutralize_shape_effects, set_round_rect_adjustment


_RGB = re.compile(r"#[0-9A-Fa-f]{6}")
_SHAPE_TYPES = {
    "rectangle": MSO_AUTO_SHAPE_TYPE.RECTANGLE,
    "roundRect": MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE,
    "ellipse": MSO_AUTO_SHAPE_TYPE.OVAL,
    "triangle": MSO_AUTO_SHAPE_TYPE.ISOSCELES_TRIANGLE,
    "chevron": MSO_AUTO_SHAPE_TYPE.CHEVRON,
    "rightArrow": MSO_AUTO_SHAPE_TYPE.RIGHT_ARROW,
}
_DASH_STYLES = {
    "solid": MSO_LINE_DASH_STYLE.SOLID,
    "dash": MSO_LINE_DASH_STYLE.DASH,
    "dot": MSO_LINE_DASH_STYLE.ROUND_DOT,
    "dashDot": MSO_LINE_DASH_STYLE.DASH_DOT,
}
_SHAPE_FIELDS = frozenset({"shape_type", "adjustments", "fill", "line", "effects", "rotation"})


def _issue(path: str, detail: str, capability: str | None = None) -> ContractIssue:
    return ContractIssue("UNSUPPORTED_CAPABILITY", path, detail, capability)


def _number(value: Any) -> bool:
    return type(value) in {int, float}


def _valid_color(value: Any) -> bool:
    return isinstance(value, str) and _RGB.fullmatch(value) is not None


def _valid_opacity(value: Any) -> bool:
    return _number(value) and 0 <= value <= 1


def _valid_drawingml_angle(value: Any) -> bool:
    return (
        _number(value)
        and 0 <= value < 360
        and 0 <= quantize_drawingml_angle(value) < DRAWINGML_FULL_CIRCLE
    )


def _unknown(value: Any, allowed: set[str], path: str) -> ContractIssue | None:
    if not isinstance(value, dict):
        return _issue(path, "contract must be an object")
    fields = sorted(set(value) - allowed)
    return _issue(path, f"unknown fields: {', '.join(fields)}") if fields else None


def validate_stroke(value: Any, path: str, capability: str) -> list[ContractIssue]:
    issue = _unknown(value, {"color", "width", "dash", "opacity"}, path)
    if issue is not None:
        return [issue]
    assert isinstance(value, dict)
    missing = sorted({"color", "width", "dash", "opacity"} - set(value))
    if missing:
        return [_issue(path, f"missing line fields: {', '.join(missing)}")]
    if not _valid_color(value["color"]):
        return [_issue(f"{path}.color", "line color must be #RRGGBB")]
    if (
        type(value["width"]) is not int
        or not 1 <= value["width"] <= DRAWINGML_LINE_WIDTH_MAX
    ):
        return [_issue(
            f"{path}.width",
            "line width must be an integer from 1 to 20116800 EMU",
            capability,
        )]
    if value["dash"] not in CANONICAL_VALUES["line_dash"]:
        return [_issue(f"{path}.dash", "unsupported line dash", f"line_dash.{value['dash']}")]
    if not _valid_opacity(value["opacity"]):
        return [_issue(f"{path}.opacity", "line opacity must be from 0 to 1")]
    return []


def _append_color(parent: Any, color: str, opacity: float) -> Any:
    rgb = OxmlElement("a:srgbClr")
    rgb.set("val", color[1:].upper())
    if opacity != 1:
        alpha = OxmlElement("a:alpha")
        alpha.set("val", str(quantize_drawingml_percentage(opacity)))
        rgb.append(alpha)
    parent.append(rgb)
    return rgb


def _remove_children(parent: Any, tags: set[str]) -> None:
    qualified = {qn(tag) for tag in tags}
    for child in list(parent):
        if child.tag in qualified:
            parent.remove(child)


def apply_stroke(shape: Any, value: dict[str, Any]) -> None:
    line = shape.line
    line.color.rgb = RGBColor.from_string(value["color"][1:])
    line.width = Emu(value["width"])
    line.dash_style = _DASH_STYLES[value["dash"]]
    properties = shape._element.spPr.get_or_add_ln()
    fill = properties.find(qn("a:solidFill"))
    if fill is None:
        raise ToolError("BUILD_OUTPUT_INCOMPLETE", "line", "line solid fill is missing")
    color = fill.find(qn("a:srgbClr"))
    if color is None:
        raise ToolError("BUILD_OUTPUT_INCOMPLETE", "line", "line RGB color is missing")
    for alpha in list(color.findall(qn("a:alpha"))):
        color.remove(alpha)
    if value["opacity"] != 1:
        alpha = OxmlElement("a:alpha")
        alpha.set("val", str(quantize_drawingml_percentage(value["opacity"])))
        color.append(alpha)


def _validate_fill(value: Any, path: str) -> list[ContractIssue]:
    if value == "noFill":
        return []
    issue = _unknown(value, {"type", "color", "opacity", "angle", "stops"}, path)
    if issue is not None:
        return [issue]
    assert isinstance(value, dict)
    fill_type = value.get("type")
    if fill_type == "solid":
        if set(value) != {"type", "color", "opacity"}:
            return [_issue(path, "solid fill requires only type, color, and opacity")]
        if not _valid_color(value["color"]) or not _valid_opacity(value["opacity"]):
            return [_issue(path, "solid fill requires #RRGGBB color and opacity from 0 to 1")]
        return []
    if fill_type != "linear_gradient":
        return [_issue(f"{path}.type", "fill type must be solid or linear_gradient")]
    if set(value) != {"type", "angle", "stops"}:
        return [_issue(path, "linear gradient requires only type, angle, and stops")]
    if not _valid_drawingml_angle(value["angle"]):
        return [_issue(
            f"{path}.angle",
            "gradient angle must be from 0 (inclusive) to 360 (exclusive)",
            "shape.fill.linear_gradient",
        )]
    stops = value["stops"]
    if not isinstance(stops, list) or len(stops) < 2:
        return [_issue(f"{path}.stops", "gradient requires at least two stops")]
    positions: list[float] = []
    for index, stop in enumerate(stops):
        stop_path = f"{path}.stops[{index}]"
        issue = _unknown(stop, {"position", "color", "opacity"}, stop_path)
        if issue is not None:
            return [issue]
        assert isinstance(stop, dict)
        if set(stop) != {"position", "color", "opacity"}:
            return [_issue(stop_path, "gradient stop requires position, color, and opacity")]
        if not _number(stop["position"]) or not 0 <= stop["position"] <= 1:
            return [_issue(f"{stop_path}.position", "gradient stop position must be from 0 to 1")]
        if not _valid_color(stop["color"]) or not _valid_opacity(stop["opacity"]):
            return [_issue(stop_path, "gradient stop requires #RRGGBB color and opacity from 0 to 1")]
        positions.append(float(stop["position"]))
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        return [_issue(f"{path}.stops", "gradient stop positions must be strictly increasing")]
    return []


def _validate_effects(value: Any, path: str) -> list[ContractIssue]:
    if value == "none":
        return []
    issue = _unknown(value, {"outer_shadow"}, path)
    if issue is not None:
        return [issue]
    assert isinstance(value, dict)
    if set(value) != {"outer_shadow"}:
        return [_issue(path, "effects requires exactly outer_shadow or none")]
    shadow = value["outer_shadow"]
    issue = _unknown(shadow, {"color", "opacity", "blur_radius", "distance", "angle"}, f"{path}.outer_shadow")
    if issue is not None:
        return [issue]
    if not isinstance(shadow, dict) or set(shadow) != {"color", "opacity", "blur_radius", "distance", "angle"}:
        return [_issue(f"{path}.outer_shadow", "outer shadow fields are incomplete")]
    if not _valid_color(shadow["color"]) or not _valid_opacity(shadow["opacity"]):
        return [_issue(f"{path}.outer_shadow", "shadow requires #RRGGBB color and opacity from 0 to 1")]
    if type(shadow["blur_radius"]) is not int or shadow["blur_radius"] < 0:
        return [_issue(f"{path}.outer_shadow.blur_radius", "blur radius must be a non-negative integer EMU")]
    if type(shadow["distance"]) is not int or shadow["distance"] < 0:
        return [_issue(f"{path}.outer_shadow.distance", "distance must be a non-negative integer EMU")]
    if not _valid_drawingml_angle(shadow["angle"]):
        return [_issue(
            f"{path}.outer_shadow.angle",
            "shadow angle must be from 0 (inclusive) to 360 (exclusive)",
            "shape.effect.shadow",
        )]
    return []


def _apply_fill(shape: Any, value: Any) -> None:
    if value == "noFill" or value is None:
        shape.fill.background()
        return
    if value["type"] == "solid":
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string(value["color"][1:])
        fill = shape._element.spPr.find(qn("a:solidFill"))
        color = fill.find(qn("a:srgbClr"))
        if value["opacity"] != 1:
            alpha = OxmlElement("a:alpha")
            alpha.set("val", str(quantize_drawingml_percentage(value["opacity"])))
            color.append(alpha)
        return
    properties = shape._element.spPr
    _remove_children(properties, {"a:noFill", "a:solidFill", "a:gradFill", "a:blipFill", "a:pattFill", "a:grpFill"})
    gradient = OxmlElement("a:gradFill")
    stop_list = OxmlElement("a:gsLst")
    for stop in value["stops"]:
        node = OxmlElement("a:gs")
        node.set("pos", str(quantize_drawingml_percentage(stop["position"])))
        _append_color(node, stop["color"], stop["opacity"])
        stop_list.append(node)
    gradient.append(stop_list)
    linear = OxmlElement("a:lin")
    linear.set("ang", str(quantize_drawingml_angle(value["angle"])))
    linear.set("scaled", "1")
    gradient.append(linear)
    geometry = properties.find(qn("a:prstGeom"))
    if geometry is None:
        raise ToolError("BUILD_OUTPUT_INCOMPLETE", "shape.fill", "shape preset geometry is missing")
    properties.insert(properties.index(geometry) + 1, gradient)


def _apply_shadow(shape: Any, value: Any) -> None:
    neutralize_shape_effects(shape)
    if value is None or value == "none":
        return
    shadow = value["outer_shadow"]
    effects = OxmlElement("a:effectLst")
    outer = OxmlElement("a:outerShdw")
    outer.set("blurRad", str(shadow["blur_radius"]))
    outer.set("dist", str(shadow["distance"]))
    outer.set("dir", str(quantize_drawingml_angle(shadow["angle"])))
    outer.set("algn", "ctr")
    outer.set("rotWithShape", "0")
    _append_color(outer, shadow["color"], shadow["opacity"])
    effects.append(outer)
    shape._element.spPr.append(effects)


class ShapeRenderer:
    kind = "shape"
    supported_fields = _SHAPE_FIELDS
    supported_values = {"shape_type": CANONICAL_VALUES["shape_type"]}
    required_fields = frozenset({"shape_type"})
    capability_ids = frozenset(
        f"shape.{value}" for value in CANONICAL_VALUES["shape_type"]
    ) | frozenset(
        capability for capability, field in ATOMIC_CAPABILITY_METADATA.items()
        if field in _SHAPE_FIELDS and capability.startswith("shape.")
    )

    def validate_contract(self, element: dict[str, Any], context: RenderContext) -> list[ContractIssue]:
        path = f"elements.{element.get('element_id', '<unknown>')}"
        style = element.get("style", {})
        shape_type = style.get("shape_type")
        if shape_type not in _SHAPE_TYPES:
            return [_issue(f"{path}.style.shape_type", "unsupported shape type", f"shape.{shape_type}")]
        if shape_type == "roundRect":
            adjustments = style.get("adjustments")
            invalid_shape = (
                not isinstance(adjustments, list)
                or len(adjustments) != 1
                or not _number(adjustments[0])
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
                return [_issue(
                    f"{path}.style.adjustments",
                    "roundRect adjustment must quantize from 1 to 50000",
                    "shape.roundRect.adjustment",
                )]
        elif "adjustments" in style:
            return [_issue(f"{path}.style.adjustments", "adjustments are supported only for roundRect")]
        if "fill" in style:
            issues = _validate_fill(style["fill"], f"{path}.style.fill")
            if issues:
                return issues
        if "line" in style:
            issues = validate_stroke(style["line"], f"{path}.style.line", "shape.line")
            if issues:
                return issues
        if "effects" in style:
            issues = _validate_effects(style["effects"], f"{path}.style.effects")
            if issues:
                return issues
        rotation = style.get("rotation", 0)
        if not _number(rotation) or not -360 <= rotation <= 360:
            return [_issue(f"{path}.style.rotation", "rotation must be from -360 to 360 degrees")]
        return []

    def render(self, element: dict[str, Any], context: RenderContext) -> None:
        style = element["style"]
        x, y, width, height = element["slide_bbox"]
        shape = context.slide.shapes.add_shape(
            _SHAPE_TYPES[style["shape_type"]], Emu(x), Emu(y), Emu(width), Emu(height)
        )
        _apply_fill(shape, style.get("fill"))
        if "line" in style:
            apply_stroke(shape, style["line"])
        else:
            shape.line.fill.background()
        _apply_shadow(shape, style.get("effects"))
        if style["shape_type"] == "roundRect":
            set_round_rect_adjustment(shape, style["adjustments"], f"elements.{element['element_id']}.style.adjustments")
        shape.rotation = style.get("rotation", 0)
        context.registry.register(
            element["element_id"], shape, "sp", semantic_kind="shape",
            selected_mode=context.representation_modes[element["element_id"]],
        )


SHAPE_RENDERER = ShapeRenderer()
register_renderer("shape", SHAPE_RENDERER)
