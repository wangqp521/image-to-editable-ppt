"""Small, controlled OOXML mutations shared by future renderers."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement

from lib.capabilities import require_supported_value
from lib.error_codes import ToolError
from lib.geometry import (
    DRAWINGML_LINE_WIDTH_MAX,
    DRAWINGML_PERCENT_SCALE,
    quantize_drawingml_percentage,
)
from lib.representation_contracts import require_asset


_BULLET_TAGS = {"a:buChar", "a:buAutoNum", "a:buBlip", "a:buNone"}
_ARROW_TAGS = {"a:headEnd", "a:tailEnd"}
_BORDER_TAGS = {"left": "a:lnL", "right": "a:lnR", "top": "a:lnT", "bottom": "a:lnB"}
_HEX_COLOR = re.compile(r"#?[0-9A-Fa-f]{6}")


def set_native_bullet(
    paragraph: Any,
    contract: dict[str, Any],
    path: str,
    *,
    slide_part: Any | None = None,
) -> None:
    """Set one local native bullet identity and its paragraph level/indent."""
    bullet_type = contract.get("bullet_type")
    bullet = contract.get("bullet")
    if bullet_type not in {"char", "auto_number", "picture"} or not isinstance(bullet, str) or not bullet:
        _invalid(path, "bullet contract must contain a supported type and non-empty bullet")
    properties = paragraph._p.get_or_add_pPr()
    for child in list(properties):
        if child.tag in {qn(tag) for tag in _BULLET_TAGS}:
            properties.remove(child)
    level = contract.get("level")
    if level is not None:
        if type(level) is not int or level < 0:
            _invalid(path, "bullet level must be a non-negative integer")
        properties.set("lvl", str(level))
    indent = contract.get("indent")
    if indent is not None:
        if type(indent) is not int:
            _invalid(path, "bullet indent must be an integer")
        properties.set("indent", str(indent))
    if bullet_type == "picture":
        if bullet != "blip" or slide_part is None:
            _invalid(path, "picture bullet requires blip identity and a slide part")
        asset_path, expected_hash, _ = require_asset(
            contract.get("bullet_asset"), f"{path}.bullet_asset"
        )
        if asset_path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            _invalid(
                f"{path}.bullet_asset.path",
                "picture bullet must be PNG or JPEG",
                "text.paragraph.picture_bullet",
            )
        try:
            image_part, relationship_id = slide_part.get_or_add_image_part(
                str(asset_path)
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                f"{path}.bullet_asset",
                "picture bullet relationship could not be created",
                "text.paragraph.picture_bullet",
            ) from exc
        if hashlib.sha256(image_part.blob).hexdigest() != expected_hash.lower():
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                f"{path}.bullet_asset",
                "embedded picture bullet media hash changed",
                "text.paragraph.picture_bullet",
            )
        bu_blip = OxmlElement("a:buBlip")
        blip = OxmlElement("a:blip")
        blip.set(qn("r:embed"), relationship_id)
        bu_blip.append(blip)
        properties.append(bu_blip)
        return
    element = OxmlElement("a:buChar" if bullet_type == "char" else "a:buAutoNum")
    element.set("char" if bullet_type == "char" else "type", bullet)
    properties.append(element)


def set_round_rect_adjustment(shape: Any, values: list[float], path: str) -> None:
    """Write rounded-rectangle adjustment values through explicit DrawingML."""
    invalid_shape = (
        not isinstance(values, list)
        or len(values) != 1
        or type(values[0]) not in {int, float}
        or not 0 < values[0] <= 0.5
    )
    quantized = (
        None if invalid_shape else quantize_drawingml_percentage(values[0])
    )
    if (
        invalid_shape
        or quantized is None
        or not 1 <= quantized <= DRAWINGML_PERCENT_SCALE // 2
    ):
        _invalid(
            path,
            "roundRect adjustment must quantize from 1 to 50000",
            "shape.roundRect.adjustment",
        )
    try:
        geometry = shape._element.spPr.prstGeom
        adjustments = geometry.avLst
    except AttributeError as exc:
        raise ToolError("BUILD_OUTPUT_INCOMPLETE", path, "shape has no preset geometry") from exc
    for child in list(adjustments):
        adjustments.remove(child)
    adjustment = OxmlElement("a:gd")
    adjustment.set("name", "adj")
    adjustment.set("fmla", f"val {quantized}")
    adjustments.append(adjustment)


def set_line_arrowheads(line: Any, contract: dict[str, Any], path: str) -> None:
    """Replace line arrowheads with the exact head/tail contract."""
    if not isinstance(contract, dict):
        _invalid(path, "line arrow contract must be an object")
    unknown = sorted(set(contract) - {"head_arrow", "tail_arrow"})
    if unknown:
        _invalid(path, f"unknown line arrow fields: {', '.join(unknown)}")
    values: dict[str, str] = {}
    for field in ("head_arrow", "tail_arrow"):
        value = contract.get(field, "none")
        require_supported_value("line_arrow", value, f"{path}.{field}")
        values[field] = value
    try:
        properties = line._element.spPr.get_or_add_ln()
    except AttributeError as exc:
        raise ToolError("BUILD_OUTPUT_INCOMPLETE", path, "line has no line properties") from exc
    for child in list(properties):
        if child.tag in {qn(tag) for tag in _ARROW_TAGS}:
            properties.remove(child)
    for field, tag in (("head_arrow", "a:headEnd"), ("tail_arrow", "a:tailEnd")):
        value = values[field]
        if value != "none":
            arrow = OxmlElement(tag)
            arrow.set("type", value)
            properties.append(arrow)


def set_table_cell_border(
    tc_pr: Any,
    side: str,
    contract: dict[str, Any] | None,
    path: str,
) -> None:
    """Set one table-cell border, using explicit noFill when undeclared."""
    tag = _BORDER_TAGS.get(side)
    if tag is None:
        _invalid(path, "table border requires a supported side, color, and positive integer width")
    for child in list(tc_pr):
        if child.tag == qn(tag):
            tc_pr.remove(child)
    line = OxmlElement(tag)
    if contract is None:
        line.append(OxmlElement("a:noFill"))
        tc_pr.append(line)
        return
    color = contract.get("color") if isinstance(contract, dict) else None
    width = contract.get("width") if isinstance(contract, dict) else None
    if (
        not isinstance(contract, dict)
        or set(contract) != {"color", "width"}
        or not isinstance(color, str)
        or _HEX_COLOR.fullmatch(color) is None
        or type(width) is not int
        or not 1 <= width <= DRAWINGML_LINE_WIDTH_MAX
    ):
        _invalid(path, "table border requires a supported side, color, and positive integer width")
    line.set("w", str(width))
    fill = OxmlElement("a:solidFill")
    rgb = OxmlElement("a:srgbClr")
    rgb.set("val", color.removeprefix("#").upper())
    fill.append(rgb)
    line.append(fill)
    tc_pr.append(line)


def neutralize_shape_effects(shape: Any) -> None:
    """Remove explicit effects and disable the theme effect reference on a shape."""
    properties = shape._element.spPr
    for tag in ("a:effectLst", "a:effectDag"):
        effect = properties.find(qn(tag))
        if effect is not None:
            properties.remove(effect)
    style = shape._element.find(qn("p:style"))
    if style is not None:
        effect_ref = style.find(qn("a:effectRef"))
        if effect_ref is not None:
            effect_ref.set("idx", "0")


def _invalid(path: str, detail: str, capability: str | None = None) -> None:
    raise ToolError("UNSUPPORTED_CAPABILITY", path, detail, capability)
