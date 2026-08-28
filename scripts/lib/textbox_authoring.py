"""Compile one source TextBox directly into schema-v2 element and typography records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


_RUN_STYLE_FIELDS = frozenset(
    {
        "font_size",
        "font_weight",
        "color",
        "letter_spacing",
        "italic",
        "underline",
        "strike",
        "baseline",
    }
)
_PARAGRAPH_FIELDS = frozenset(
    {
        "alignment",
        "line_spacing",
        "space_before",
        "space_after",
        "indent",
        "margin_left",
        "list",
    }
)
_MARGIN_FIELDS = frozenset({"left", "right", "top", "bottom"})
_ALIGNMENTS = frozenset({"left", "center", "right", "justify", "distributed"})
_VERTICAL_ALIGNMENTS = frozenset({"top", "middle", "bottom"})
_TEXT_SAFETY_MODES = frozenset({"free_text", "container_bound"})


def _number(value: Any) -> bool:
    return type(value) in {int, float}


def _bbox(value: Any, name: str) -> list[int | float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 4
        or any(not _number(item) for item in value)
        or value[2] <= 0
        or value[3] <= 0
    ):
        raise ValueError(f"{name} must be [x, y, width, height] with positive size")
    return list(value)


def _paragraph_texts(value: str | Sequence[str]) -> list[str]:
    items = [value] if isinstance(value, str) else list(value)
    if not items or any(
        not isinstance(item, str)
        or not item
        or "\n" in item
        or "\r" in item
        for item in items
    ):
        raise ValueError("paragraphs_text must contain non-empty strings without CR/LF")
    return items


def _run_style(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if set(value) != _RUN_STYLE_FIELDS:
        missing = sorted(_RUN_STYLE_FIELDS - set(value))
        unknown = sorted(set(value) - _RUN_STYLE_FIELDS)
        detail = f"missing {missing}" if missing else f"unknown {unknown}"
        raise ValueError(f"{name} run style fields are invalid: {detail}")
    result = dict(value)
    if not _number(result["font_size"]) or result["font_size"] <= 0:
        raise ValueError(f"{name}.font_size must be positive")
    if not _number(result["font_weight"]) or not 1 <= result["font_weight"] <= 1000:
        raise ValueError(f"{name}.font_weight must be from 1 to 1000")
    color = result["color"]
    if (
        not isinstance(color, str)
        or len(color) != 7
        or not color.startswith("#")
        or any(character not in "0123456789abcdefABCDEF" for character in color[1:])
    ):
        raise ValueError(f"{name}.color must be #RRGGBB")
    result["color"] = color.upper()
    if not _number(result["letter_spacing"]):
        raise ValueError(f"{name}.letter_spacing must be numeric")
    if any(type(result[field]) is not bool for field in ("italic", "underline", "strike")):
        raise ValueError(f"{name} style flags must be boolean")
    if type(result["baseline"]) is not int or not -100_000 <= result["baseline"] <= 100_000:
        raise ValueError(f"{name}.baseline must be an integer from -100000 to 100000")
    return result


def _span_range(span: Mapping[str, Any], text: str, index: int) -> tuple[int, int]:
    has_offsets = "start" in span or "end" in span
    has_text = "text" in span
    if has_offsets and has_text:
        raise ValueError(f"spans[{index}] must use offsets or text, not both")
    if has_offsets:
        start = span.get("start")
        end = span.get("end")
        if type(start) is not int or type(end) is not int:
            raise ValueError(f"spans[{index}] start/end must be integers")
    elif has_text:
        needle = span.get("text")
        occurrence = span.get("occurrence")
        if not isinstance(needle, str) or not needle:
            raise ValueError(f"spans[{index}].text must be non-empty")
        positions: list[int] = []
        cursor = 0
        while True:
            position = text.find(needle, cursor)
            if position < 0:
                break
            positions.append(position)
            cursor = position + 1
        if not positions:
            raise ValueError(f"spans[{index}].text was not found")
        if occurrence is None:
            if len(positions) != 1:
                raise ValueError(
                    f"spans[{index}].text is ambiguous; provide occurrence or explicit start/end"
                )
            start = positions[0]
        else:
            if type(occurrence) is not int or not 1 <= occurrence <= len(positions):
                raise ValueError(f"spans[{index}].occurrence is out of range")
            start = positions[occurrence - 1]
        end = start + len(needle)
    else:
        raise ValueError(f"spans[{index}] requires text or start/end")
    if not 0 <= start < end <= len(text):
        raise ValueError(f"spans[{index}] range is outside the TextBox text")
    return start, end


def _compile_runs(
    text: str,
    base_run_style: Mapping[str, Any],
    spans: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    base = _run_style(base_run_style, "base_run_style")
    resolved: list[tuple[int, int, dict[str, Any]]] = []
    boundaries = {0, len(text)}
    locator_fields = {"start", "end", "text", "occurrence"}
    for index, span in enumerate(spans):
        if not isinstance(span, Mapping):
            raise ValueError(f"spans[{index}] must be an object")
        start, end = _span_range(span, text, index)
        overrides = {key: value for key, value in span.items() if key not in locator_fields}
        unknown = sorted(set(overrides) - _RUN_STYLE_FIELDS)
        if unknown or not overrides:
            raise ValueError(
                f"spans[{index}] must contain only run-style overrides; unknown={unknown}"
            )
        candidate = _run_style({**base, **overrides}, f"spans[{index}]")
        override_style = {field: candidate[field] for field in overrides}
        resolved.append((start, end, override_style))
        boundaries.update({start, end})

    runs: list[dict[str, Any]] = []
    ordered = sorted(boundaries)
    for start, end in zip(ordered, ordered[1:]):
        style = dict(base)
        for span_start, span_end, overrides in resolved:
            if span_start <= start and end <= span_end:
                style.update(overrides)
        if runs and all(runs[-1][field] == style[field] for field in _RUN_STYLE_FIELDS):
            runs[-1]["end"] = end
        else:
            runs.append({"start": start, "end": end, **style})
    return runs


def _compile_paragraphs(
    paragraph_texts: Sequence[str],
    paragraphs: Sequence[Mapping[str, Any]] | None,
    alignment: str,
) -> tuple[list[dict[str, Any]], list[int]]:
    options = (
        [
            {
                "alignment": alignment,
                "line_spacing": 1,
                "space_before": 0,
                "space_after": 0,
                "indent": 0,
                "list": {"is_list": False, "level": 0, "bullet": None},
            }
            for _ in paragraph_texts
        ]
        if paragraphs is None
        else list(paragraphs)
    )
    if len(options) != len(paragraph_texts):
        raise ValueError("paragraphs must have the same length as paragraphs_text")

    result: list[dict[str, Any]] = []
    cursor = 0
    for index, (paragraph_text, option) in enumerate(zip(paragraph_texts, options)):
        if not isinstance(option, Mapping):
            raise ValueError(f"paragraphs[{index}] must be an object")
        unknown = sorted(set(option) - _PARAGRAPH_FIELDS)
        required = {
            "alignment",
            "line_spacing",
            "space_before",
            "space_after",
            "indent",
            "list",
        }
        missing = sorted(required - set(option))
        if unknown or missing:
            raise ValueError(
                f"paragraphs[{index}] fields are invalid; missing={missing}, unknown={unknown}"
            )
        if option["alignment"] not in _ALIGNMENTS:
            raise ValueError(f"paragraphs[{index}].alignment is unsupported")
        list_contract = option["list"]
        if not isinstance(list_contract, Mapping) or type(list_contract.get("is_list")) is not bool:
            raise ValueError(f"paragraphs[{index}].list is invalid")
        if list_contract["is_list"] and "margin_left" not in option:
            raise ValueError(f"paragraphs[{index}] native list requires margin_left")
        end = cursor + len(paragraph_text)
        record = deepcopy(dict(option))
        record.update({"start": cursor, "end": end})
        result.append(record)
        cursor = end
    return result, [paragraph["end"] for paragraph in result[:-1]]


def compile_textbox(
    *,
    element_id: str,
    paragraphs_text: str | Sequence[str],
    spans: Sequence[Mapping[str, Any]] | None,
    paragraphs: Sequence[Mapping[str, Any]] | None,
    source_bbox: Sequence[int | float],
    slide_bbox: Sequence[int | float],
    layer: int,
    selected_font: str,
    source_font_guess: str,
    fallback_reason: str | None,
    fallback_trace: Mapping[str, Any] | None,
    font_declaration_verified: bool,
    base_run_style: Mapping[str, Any],
    margins: Mapping[str, int | float],
    alignment: str,
    wrap: bool,
    overflow: bool,
    vertical_alignment: str,
    text_safety: str,
    source_layout: Mapping[str, Any] | None = None,
    confidence: str = "high",
    rotation: int | float = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return one exact schema-v2 text element and its typography item.

    ``text_safety`` is intentionally required and is not serialized. The caller
    applies the documented page-coordinate safety adjustment before passing the
    final bboxes and margins to this compiler.
    """
    if not isinstance(element_id, str) or not element_id:
        raise ValueError("element_id must be non-empty")
    if alignment not in _ALIGNMENTS:
        raise ValueError("alignment is unsupported")
    if vertical_alignment not in _VERTICAL_ALIGNMENTS:
        raise ValueError("vertical_alignment is unsupported")
    if text_safety not in _TEXT_SAFETY_MODES:
        raise ValueError("text_safety must be free_text or container_bound")
    if type(wrap) is not bool or type(overflow) is not bool:
        raise ValueError("wrap and overflow must be boolean")
    if type(font_declaration_verified) is not bool:
        raise ValueError("font_declaration_verified must be boolean")
    if not isinstance(layer, int) or isinstance(layer, bool):
        raise ValueError("layer must be an integer")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError("confidence is unsupported")
    if not _number(rotation) or not -360 <= rotation <= 360:
        raise ValueError("rotation must be from -360 to 360")
    if not isinstance(selected_font, str) or not selected_font:
        raise ValueError("selected_font must be non-empty")
    if not isinstance(source_font_guess, str) or not source_font_guess:
        raise ValueError("source_font_guess must be non-empty")
    if set(margins) != _MARGIN_FIELDS or any(
        not _number(margins[side]) or margins[side] < 0 for side in _MARGIN_FIELDS
    ):
        raise ValueError("margins must contain four non-negative EMU values")

    source_box = _bbox(source_bbox, "source_bbox")
    slide_box = _bbox(slide_bbox, "slide_bbox")
    text_parts = _paragraph_texts(paragraphs_text)
    text = "".join(text_parts)
    compiled_runs = _compile_runs(text, base_run_style, list(spans or []))
    compiled_paragraphs, paragraph_breaks = _compile_paragraphs(
        text_parts, paragraphs, alignment
    )
    text_box = {
        "x": slide_box[0],
        "y": slide_box[1],
        "w": slide_box[2],
        "h": slide_box[3],
        "margins": dict(margins),
        "alignment": alignment,
        "vertical_alignment": vertical_alignment,
        "wrap": wrap,
        "overflow": overflow,
        "soft_breaks": [],
        "paragraph_breaks": paragraph_breaks,
    }
    element = {
        "element_id": element_id,
        "kind": "text",
        "source_bbox": source_box,
        "slide_bbox": slide_box,
        "layer": layer,
        "editable": True,
        "confidence": confidence,
        "style": {
            "fill": "noFill",
            "margins": dict(margins),
            "vertical_alignment": vertical_alignment,
            "wrap": wrap,
            "rotation": rotation,
        },
        "content": {"text": text},
    }
    typography = {
        "element_id": element_id,
        "text": text,
        "source_font_guess": source_font_guess,
        "selected_font": selected_font,
        "fallback_reason": fallback_reason,
        "fallback_trace": deepcopy(fallback_trace),
        "runs": compiled_runs,
        "paragraphs": compiled_paragraphs,
        "text_box": text_box,
        "internal_font_declaration": selected_font,
        "font_declaration_verified": font_declaration_verified,
    }
    if source_layout is not None:
        typography["source_layout"] = deepcopy(dict(source_layout))
    return element, typography
