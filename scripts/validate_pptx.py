#!/usr/bin/env python3
"""Validate PPTX structure, widescreen layout, and basic editability."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import posixpath
import re
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from PIL import Image

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from lib.capabilities import capability_manifest_sha256
from lib.background_contracts import (
    resolved_element_mode_map,
    validate_background_prebuild,
)
from lib.element_contracts import expand_multipart_parts, expected_object_types
from lib.error_codes import ToolError
from lib.font_runtime import validate_font_runtime
from lib.geometry import bbox_union, is_near_full_page_bbox
from lib.hashing import canonical_json_sha256
from lib.representation_contracts import (
    REQUIRED_FIELDS as REPRESENTATION_FACT_FIELDS,
    representation_summary,
    validate_representation_plan,
)
from lib.schema_contracts import BACKGROUND_ITEM_FIELDS
from lib.spec_identity import content_spec_sha256, input_spec_sha256


NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}
RID = f"{{{NS['r']}}}id"
REMBED = f"{{{NS['r']}}}embed"
REQUIRED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
    "ppt/_rels/presentation.xml.rels",
}
SLIDE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
)
MAX_ZIP_MEMBERS = 2048
MAX_MEMBER_UNCOMPRESSED = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_MEDIA_DIMENSION = 32768
MAX_MEDIA_PIXELS = 100_000_000
MAX_MEDIA_RGBA_BYTES = 400_000_000
STRICT_TO_TRANSITIONAL = {
    "http://purl.oclc.org/ooxml/presentationml/main": NS["p"],
    "http://purl.oclc.org/ooxml/drawingml/main": NS["a"],
    "http://purl.oclc.org/ooxml/officeDocument/relationships": NS["r"],
    "http://purl.oclc.org/ooxml/package/relationships": NS["pr"],
}
BUILD_REPORT_FIELDS = frozenset(
    {
        "valid",
        "schema_version",
        "schema_sha256",
        "content_spec_sha256",
        "input_spec_sha256",
        "preferred_font",
        "runtime_preflight",
        "font_runtime",
        "compiler_sha256",
        "capability_manifest_sha256",
        "pptx_sha256",
        "environment",
        "elements",
        "representation_summary",
        "asset_fallbacks",
        "background_summary",
        "background_pictures",
        "normalization",
        "warnings",
        "unsupported",
    }
)
BUILD_REPORT_ELEMENT_FIELDS = frozenset(
    {"semantic_kind", "selected_mode", "object_type", "objects"}
)
BUILD_REPORT_OBJECT_FIELDS = frozenset(
    {
        "ooxml_name",
        "object_type",
        "bbox",
        "rotation",
        "part_id",
        "media_sha256",
        "text_summary",
        "font_declarations",
    }
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
TEXT_RUN_STYLE_PROPERTIES = (
    "font_size",
    "font_weight",
    "color",
    "italic",
    "underline",
    "strike",
    "baseline",
    "letter_spacing",
)


class ValidationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result(path: Path) -> dict[str, Any]:
    return {
        "evidence_level": "full",
        "usable_as_background_evidence": True,
        "path": str(Path(path).expanduser().resolve()),
        "pptx_sha256": None,
        "valid": False,
        "errors": [],
        "warnings": [],
        "slide_count": 0,
        "width_emu": None,
        "height_emu": None,
        "aspect_ratio": None,
        "editable_object_count": 0,
        "text_shape_count": 0,
        "graphic_frame_count": 0,
        "picture_count": 0,
        "font_declarations": [],
        "font_sizes_pt": [],
        "text_runs": 0,
        "native_list_paragraphs": 0,
        "native_list_contracts_checked": 0,
        "native_chart_contracts_checked": 0,
        "text_run_contracts_checked": 0,
        "text_run_style_mismatch_count": 0,
        "text_run_style_mismatches": [],
        "text_run_style_mismatches_by_property": {
            name: 0 for name in TEXT_RUN_STYLE_PROPERTIES
        },
        "build_report_objects_checked": 0,
        "multipart_contracts_checked": 0,
        "representation_facts_checked": 0,
        "asset_fallbacks_checked": 0,
        "text_objects": [],
        "native_shape_objects": [],
        "native_chart_objects": [],
        "picture_objects": [],
        "structure_objects": [],
        "full_slide_picture_risk": False,
        "external_relationships": [],
        "slides": [],
    }


def _canonicalize_namespaces(root: ET.Element) -> ET.Element:
    for element in root.iter():
        if element.tag.startswith("{"):
            uri, local = element.tag[1:].split("}", 1)
            element.tag = f"{{{STRICT_TO_TRANSITIONAL.get(uri, uri)}}}{local}"
        for key, value in list(element.attrib.items()):
            if key.startswith("{"):
                uri, local = key[1:].split("}", 1)
                canonical = f"{{{STRICT_TO_TRANSITIONAL.get(uri, uri)}}}{local}"
                if canonical != key:
                    del element.attrib[key]
                    element.attrib[canonical] = value
    return root


def _xml(archive: zipfile.ZipFile, part: str) -> ET.Element:
    try:
        info = archive.getinfo(part)
        if info.file_size > MAX_XML_BYTES:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"XML part too large: {part}")
        payload = archive.read(part)
        upper = payload.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ValidationError("XML_DTD_FORBIDDEN", f"DTD/entity forbidden: {part}")
        return _canonicalize_namespaces(ET.fromstring(payload))
    except KeyError as exc:
        raise ValidationError("PPTX_REQUIRED_PART_MISSING", f"missing XML part: {part}") from exc
    except ET.ParseError as exc:
        raise ValidationError("XML_INVALID", f"invalid XML part: {part}") from exc


def _source_part_for_rels(rels_part: str) -> str:
    if rels_part == "_rels/.rels":
        return ""
    path = PurePosixPath(rels_part)
    if path.parent.name != "_rels" or not path.name.endswith(".rels"):
        raise ValueError(f"invalid relationships part path: {rels_part}")
    source_name = path.name[: -len(".rels")]
    return str(path.parent.parent / source_name)


def _resolve_target(source_part: str, target: str) -> str:
    if not isinstance(target, str) or not target or "\\" in target:
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Invalid relationship Target: {target!r}")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Ambiguous internal Target: {target}")
    decoded = unquote(parsed.path)
    if decoded != parsed.path and ("/" in decoded or "\\" in decoded or ".." in decoded.split("/")):
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Encoded ambiguous Target: {target}")
    if target.startswith("/"):
        candidate = target.lstrip("/")
    else:
        base = posixpath.dirname(source_part)
        candidate = posixpath.join(base, target)
    parts = PurePosixPath(candidate).parts
    if any(part in {"", "."} for part in parts) or candidate.startswith("/"):
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Target escapes package: {target}")
    normalized = posixpath.normpath(candidate)
    if normalized == ".." or normalized.startswith("../"):
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Target escapes package: {target}")
    return normalized


def _relationship_map(
    archive: zipfile.ZipFile, rels_part: str
) -> dict[str, tuple[str, str, bool]]:
    root = _xml(archive, rels_part)
    if root.tag != f"{{{NS['pr']}}}Relationships":
        raise ValidationError(
            "RELATIONSHIP_SEMANTICS_INVALID",
            f"Unexpected Relationships root QName in {rels_part}",
        )
    source = _source_part_for_rels(rels_part)
    relationships: dict[str, tuple[str, str, bool]] = {}
    for rel in list(root):
        if rel.tag != f"{{{NS['pr']}}}Relationship":
            raise ValidationError("RELATIONSHIP_SEMANTICS_INVALID", f"Unknown child in {rels_part}")
        relationship_id = rel.get("Id")
        target = rel.get("Target")
        relationship_type = rel.get("Type", "")
        target_mode = rel.get("TargetMode")
        if (
            not relationship_id or not target or not relationship_type
            or target_mode not in {None, "Internal", "External"}
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", relationship_id)
            or not urlsplit(relationship_type).scheme
        ):
            raise ValidationError(
                "RELATIONSHIP_SEMANTICS_INVALID",
                f"Relationship requires valid Id/Type/Target/TargetMode in {rels_part}",
            )
        external = target_mode == "External"
        if relationship_id in relationships:
            raise ValidationError("DUPLICATE_RELATIONSHIP_ID", f"Duplicate relationship Id in {rels_part}: {relationship_id}")
        resolved = target if external else _resolve_target(source, target)
        relationships[relationship_id] = (resolved, relationship_type, external)
    return relationships


def _slide_rels_part(slide_part: str) -> str:
    path = PurePosixPath(slide_part)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def _int_attr(element: ET.Element | None, name: str) -> int | None:
    if element is None:
        return None
    try:
        return int(element.get(name, ""))
    except ValueError:
        return None


def _float_attr(element: ET.Element | None, name: str) -> float | None:
    if element is None:
        return None
    try:
        value = float(element.get(name, ""))
    except ValueError:
        return None
    return value if math.isfinite(value) else None




def _archive_sha256(archive: zipfile.ZipFile, part: str) -> str:
    digest = hashlib.sha256()
    with archive.open(part) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _font_properties(slide: ET.Element) -> list[ET.Element]:
    properties: list[ET.Element] = []
    for tag in ("a:rPr", "a:defRPr", "a:endParaRPr"):
        properties.extend(slide.findall(f".//{tag}", NS))
    return properties


def _declared_fonts(properties: list[ET.Element]) -> set[str]:
    fonts: set[str] = set()
    for prop in properties:
        typeface = prop.get("typeface")
        if typeface:
            fonts.add(typeface)
        for child_name in ("latin", "ea", "cs", "sym"):
            child = prop.find(f"a:{child_name}", NS)
            if child is not None and child.get("typeface"):
                fonts.add(child.get("typeface", ""))
    return fonts


def _declared_font_sizes(properties: list[ET.Element]) -> set[float]:
    sizes: set[float] = set()
    for prop in properties:
        size = _int_attr(prop, "sz")
        if size is not None and size > 0:
            sizes.add(size / 100)
    return sizes


def _geometry(element: ET.Element, path: str) -> tuple[int | None, int | None, int | None, int | None]:
    transform = element.find(path, NS)
    offset = transform.find("a:off", NS) if transform is not None else None
    extent = transform.find("a:ext", NS) if transform is not None else None
    return (
        _int_attr(offset, "x"), _int_attr(offset, "y"),
        _int_attr(extent, "cx"), _int_attr(extent, "cy"),
    )


def _object_identity(element: ET.Element, path: str) -> tuple[str | None, str | None, bool]:
    non_visual = element.find(path, NS)
    if non_visual is None:
        return None, None, False
    return non_visual.get("id"), non_visual.get("name"), non_visual.get("hidden") in {"1", "true"}


def _intersects_slide(x: int | None, y: int | None, cx: int | None, cy: int | None,
                      width: int, height: int) -> bool:
    return (
        None not in {x, y, cx, cy}
        and cx > 0 and cy > 0
        and x < width and y < height and x + cx > 0 and y + cy > 0
    )


def _transform_bbox(
    bbox: tuple[int | None, int | None, int | None, int | None],
    transform: tuple[float, float, float, float],
) -> tuple[int | None, int | None, int | None, int | None]:
    x, y, cx, cy = bbox
    if None in {x, y, cx, cy}:
        return None, None, None, None
    sx, sy, tx, ty = transform
    return (
        round(tx + sx * x), round(ty + sy * y),
        round(abs(sx) * cx), round(abs(sy) * cy),
    )


def _group_child_transform(
    group: ET.Element,
    parent: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    xfrm = group.find("p:grpSpPr/a:xfrm", NS)
    off = xfrm.find("a:off", NS) if xfrm is not None else None
    ext = xfrm.find("a:ext", NS) if xfrm is not None else None
    child_off = xfrm.find("a:chOff", NS) if xfrm is not None else None
    child_ext = xfrm.find("a:chExt", NS) if xfrm is not None else None
    values = (
        _int_attr(off, "x"), _int_attr(off, "y"),
        _int_attr(ext, "cx"), _int_attr(ext, "cy"),
        _int_attr(child_off, "x"), _int_attr(child_off, "y"),
        _int_attr(child_ext, "cx"), _int_attr(child_ext, "cy"),
    )
    if None in values or values[6] == 0 or values[7] == 0:
        return None
    x, y, cx, cy, chx, chy, chcx, chcy = values
    psx, psy, ptx, pty = parent
    local_sx, local_sy = cx / chcx, cy / chcy
    return (
        psx * local_sx,
        psy * local_sy,
        ptx + psx * (x - chx * local_sx),
        pty + psy * (y - chy * local_sy),
    )


def _collect_visible_objects(
    nodes: list[ET.Element],
    slide_part: str,
    width: int,
    height: int,
    inheritance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return every visible presentation object in slide-space preorder."""
    records: list[dict[str, Any]] = []
    layer = 0
    identity_paths = {
        "sp": "p:nvSpPr/p:cNvPr",
        "pic": "p:nvPicPr/p:cNvPr",
        "graphicFrame": "p:nvGraphicFramePr/p:cNvPr",
        "grpSp": "p:nvGrpSpPr/p:cNvPr",
        "cxnSp": "p:nvCxnSpPr/p:cNvPr",
    }
    geometry_paths = {
        "sp": "p:spPr/a:xfrm",
        "pic": "p:spPr/a:xfrm",
        "graphicFrame": "p:xfrm",
        "grpSp": "p:grpSpPr/a:xfrm",
        "cxnSp": "p:spPr/a:xfrm",
    }

    def visit(node: ET.Element, transform: tuple[float, float, float, float], parent_hidden: bool) -> None:
        nonlocal layer
        kind = node.tag.rsplit("}", 1)[-1]
        if kind not in identity_paths:
            return
        layer += 1
        identity = node.find(identity_paths[kind], NS)
        hidden = parent_hidden or (
            identity is not None and identity.get("hidden") in {"1", "true"}
        )
        local_bbox = _geometry(node, geometry_paths[kind])
        if kind == "sp" and None in local_bbox:
            inherited_bbox = _inherited_placeholder_geometry(node, inheritance or {})
            if None not in inherited_bbox:
                local_bbox = inherited_bbox
        bbox = _transform_bbox(local_bbox, transform)
        object_transform = node.find(geometry_paths[kind], NS)
        rotation_units = _int_attr(object_transform, "rot")
        rotation = 0.0 if rotation_units is None else rotation_units / 60000
        if kind == "sp":
            text_nodes = node.findall(".//a:t", NS)
            text_summary = (
                "".join(item.text or "" for item in text_nodes)
                if text_nodes
                else None
            )
        elif kind == "graphicFrame" and node.find(".//a:tbl", NS) is not None:
            text_summary = "\n".join(
                "".join(item.text or "" for item in cell.findall(".//a:t", NS))
                for cell in node.findall(".//a:tbl/a:tr/a:tc", NS)
                if cell.get("hMerge") not in {"1", "true"}
                and cell.get("vMerge") not in {"1", "true"}
            )
        else:
            text_summary = None
        font_declarations = sorted(_declared_fonts(_font_properties(node)))
        geometry_known = None not in bbox
        visible = False if hidden else (
            _intersects_slide(*bbox, width, height) if geometry_known else None
        )
        record = {
            "slide_part": slide_part,
            "object_type": kind,
            "object_id": identity.get("id") if identity is not None else None,
            "object_name": identity.get("name") if identity is not None else None,
            "layer": layer,
            "hidden": hidden,
            "x": bbox[0], "y": bbox[1], "cx": bbox[2], "cy": bbox[3],
            "bbox": [bbox[0], bbox[1], bbox[2], bbox[3]],
            "geometry_known": geometry_known,
            "visible": visible,
            "has_text": kind == "sp" and bool(node.findall(".//a:t", NS)),
            "rotation": rotation,
            "text_summary": text_summary,
            "font_declarations": font_declarations,
            "media_sha256": None,
            "_element": node,
        }
        records.append(record)
        if kind == "grpSp":
            child_transform = _group_child_transform(node, transform)
            for child in list(node):
                if child.tag.rsplit("}", 1)[-1] in identity_paths:
                    visit(child, child_transform or transform, hidden or child_transform is None)

    for node in nodes:
        visit(node, (1.0, 1.0, 0.0, 0.0), False)
    return records


def _round_rect_adjustment(shape: ET.Element) -> tuple[str | None, int | None]:
    geometry = shape.find("p:spPr/a:prstGeom", NS)
    if geometry is None or geometry.get("prst") != "roundRect":
        return None, None
    adjustment = geometry.find("a:avLst/a:gd[@name='adj']", NS)
    if adjustment is None:
        return "missing", None
    match = re.fullmatch(r"val\s+(-?\d+)", adjustment.get("fmla", "").strip())
    if match is None:
        return "invalid", None
    value = int(match.group(1))
    if not 1 <= value <= 50_000:
        return "invalid", value
    return "valid", value


def _scripts_in_text(text: str) -> list[str]:
    scripts: list[str] = []
    for char in text:
        value = ord(char)
        if (
            0x2E80 <= value <= 0x9FFF
            or 0xAC00 <= value <= 0xD7AF
            or 0xF900 <= value <= 0xFAFF
            or 0x3040 <= value <= 0x30FF
            or 0x3100 <= value <= 0x312F
            or 0xFF00 <= value <= 0xFFEF
        ):
            script = "ea"
        elif (
            0x0590 <= value <= 0x08FF
            or 0x0700 <= value <= 0x074F
            or 0x0780 <= value <= 0x07BF
            or 0xFB1D <= value <= 0xFDFF
            or 0xFE70 <= value <= 0xFEFF
        ):
            script = "cs"
        else:
            script = "latin"
        if script not in scripts and (char.isalnum() or script != "latin"):
            scripts.append(script)
    return scripts or ["latin"]


def _font_from_properties(
    prop: ET.Element | None,
    text: str,
    script: str | None = None,
    *,
    allow_generic: bool = True,
) -> str | None:
    if prop is None:
        return None
    preferred = script or _scripts_in_text(text)[0]
    child = prop.find(f"a:{preferred}", NS)
    if child is not None and child.get("typeface"):
        return child.get("typeface")
    return prop.get("typeface") if allow_generic else None


def _resolve_theme_font(value: str | None, theme_fonts: dict[str, str]) -> str | None:
    if value is None:
        return None
    return theme_fonts.get(value, value)


def _font_from_chain(
    properties: list[ET.Element | None], text: str, theme_fonts: dict[str, str], script: str | None = None
) -> str | None:
    for prop in properties:
        value = _font_from_properties(prop, text, script, allow_generic=False)
        if value:
            return _resolve_theme_font(value, theme_fonts)
    for prop in properties:
        value = prop.get("typeface") if prop is not None else None
        if value:
            return _resolve_theme_font(value, theme_fonts)
    preferred = {"ea": "+mj-ea", "cs": "+mj-cs"}.get(script or _scripts_in_text(text)[0], "+mj-lt")
    return theme_fonts.get(preferred)


def _chain_int(properties: list[ET.Element | None], name: str) -> int | None:
    for prop in properties:
        value = _int_attr(prop, name)
        if value is not None:
            return value
    return None


def _chain_attr(properties: list[ET.Element | None], name: str) -> str | None:
    for prop in properties:
        if prop is not None and prop.get(name) is not None:
            return prop.get(name)
    return None


def _run_color(prop: ET.Element | None) -> str | None:
    if prop is None:
        return None
    color = prop.find("a:solidFill/a:srgbClr", NS)
    return f"#{color.get('val')}" if color is not None and color.get("val") else None


def _paragraph_spacing(
    properties: list[ET.Element | None], tag: str
) -> float | None:
    for ppr in properties:
        if ppr is None:
            continue
        holder = ppr.find(f"a:{tag}", NS)
        pct = holder.find("a:spcPct", NS) if holder is not None else None
        pts = holder.find("a:spcPts", NS) if holder is not None else None
        if pct is not None:
            value = _int_attr(pct, "val")
            return value / 100000 if value is not None else None
        if pts is not None:
            value = _int_attr(pts, "val")
            return value / 100 if value is not None else None
    return None


def _native_bullet_contract(
    properties: list[ET.Element | None],
    level: int,
) -> dict[str, Any]:
    bullet_type = None
    bullet = None
    bullet_relationship_id = None
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buNone", NS) is not None:
            return {"is_list": False, "level": level, "bullet": None}
        char = owner.find("a:buChar", NS)
        auto = owner.find("a:buAutoNum", NS)
        blip = owner.find("a:buBlip", NS)
        if char is not None:
            bullet_type, bullet = "char", char.get("char")
        elif auto is not None:
            bullet_type, bullet = "auto_number", auto.get("type")
        elif blip is not None:
            bullet_type, bullet = "picture", "blip"
            image = blip.find("a:blip", NS)
            bullet_relationship_id = image.get(REMBED) if image is not None else None
        if bullet_type is not None:
            break
    if bullet_type is None:
        return {"is_list": False, "level": level, "bullet": None}

    bullet_font = "follow_text"
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buFontTx", NS) is not None:
            break
        font = owner.find("a:buFont", NS)
        if font is not None and font.get("typeface"):
            bullet_font = font.get("typeface", "")
            break

    bullet_size_mode = "follow_text"
    bullet_size_value: float | None = None
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buSzTx", NS) is not None:
            break
        percent = owner.find("a:buSzPct", NS)
        points = owner.find("a:buSzPts", NS)
        if percent is not None:
            raw = _int_attr(percent, "val")
            if raw is not None:
                bullet_size_mode, bullet_size_value = "percent", raw / 1000
            break
        if points is not None:
            raw = _int_attr(points, "val")
            if raw is not None:
                bullet_size_mode, bullet_size_value = "points", raw / 100
            break

    bullet_color = "follow_text"
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buClrTx", NS) is not None:
            break
        holder = owner.find("a:buClr", NS)
        if holder is None:
            continue
        rgb = holder.find("a:srgbClr", NS)
        scheme = holder.find("a:schemeClr", NS)
        if rgb is not None and rgb.get("val"):
            bullet_color = f"#{rgb.get('val')}"
        elif scheme is not None and scheme.get("val"):
            bullet_color = f"scheme:{scheme.get('val')}"
        break

    result = {
        "is_list": True,
        "level": level,
        "bullet_type": bullet_type,
        "bullet": bullet,
        "bullet_font": bullet_font,
        "bullet_size_mode": bullet_size_mode,
        "bullet_size_value": bullet_size_value,
        "bullet_color": bullet_color,
    }
    if bullet_type == "picture":
        result["bullet_relationship_id"] = bullet_relationship_id
    return result


def _alignment(value: str | None) -> str | None:
    return {
        "l": "left", "ctr": "center", "r": "right",
        "just": "justify", "justLow": "justify", "dist": "distributed",
    }.get(value, value)


def _vertical_alignment(value: str | None) -> str:
    return {None: "top", "t": "top", "ctr": "middle", "b": "bottom"}.get(
        value,
        value,
    )


def _text_object(
    shape: ET.Element,
    slide_part: str,
    layer: int,
    inheritance: dict[str, Any] | None = None,
    theme_fonts: dict[str, str] | None = None,
    slide_bbox: tuple[int | None, int | None, int | None, int | None] | None = None,
    archive: zipfile.ZipFile | None = None,
    names: set[str] | None = None,
    slide_relationships: dict[str, tuple[str, str, bool]] | None = None,
) -> dict[str, Any]:
    theme_fonts = theme_fonts or {}
    inheritance = inheritance or {}
    object_id, name, hidden = _object_identity(shape, "p:nvSpPr/p:cNvPr")
    x, y, cx, cy = slide_bbox or _geometry(shape, "p:spPr/a:xfrm")
    body = shape.find("p:txBody", NS)
    body_pr = body.find("a:bodyPr", NS) if body is not None else None
    body_properties = [body_pr, *_inherited_body_properties(shape, inheritance)]
    paragraphs_out: list[dict[str, Any]] = []
    runs_out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    soft_breaks: list[int] = []
    cursor = 0
    list_style = body.find("a:lstStyle", NS) if body is not None else None
    for paragraph in body.findall("a:p", NS) if body is not None else []:
        p_start = cursor
        ppr = paragraph.find("a:pPr", NS)
        level = _int_attr(ppr, "lvl") or 0
        fallback_pprs = _inherited_paragraph_properties(shape, level, inheritance)
        fallback_rprs = [
            fallback.find("a:defRPr", NS) if fallback is not None else None
            for fallback in fallback_pprs
        ]
        inherited_ppr = (
            list_style.find(f"a:lvl{level + 1}pPr", NS) if list_style is not None else None
        )
        paragraph_properties = [ppr, inherited_ppr, *fallback_pprs]
        default_rprs = [
            ppr.find("a:defRPr", NS) if ppr is not None else None,
            inherited_ppr.find("a:defRPr", NS) if inherited_ppr is not None else None,
        ]
        for child in list(paragraph):
            local = child.tag.rsplit("}", 1)[-1]
            if local == "br":
                soft_breaks.append(cursor)
                continue
            if local not in {"r", "fld"}:
                continue
            text_node = child.find("a:t", NS)
            text = text_node.text if text_node is not None and text_node.text is not None else ""
            prop = child.find("a:rPr", NS)
            properties = [prop, *default_rprs, *fallback_rprs]
            start = cursor
            cursor += len(text)
            text_parts.append(text)
            size = _chain_int(properties, "sz")
            bold = _chain_attr(properties, "b")
            italic = _chain_attr(properties, "i") in {"1", "true"}
            underline = _chain_attr(properties, "u") not in {None, "none"}
            strike_value = _chain_attr(properties, "strike")
            strike = strike_value not in {None, "noStrike"}
            baseline = _chain_int(properties, "baseline") or 0
            spacing = _chain_int(properties, "spc") or 0
            color = next(
                (
                    resolved for candidate in properties
                    if (resolved := _run_color(candidate)) is not None
                ),
                None,
            )
            if isinstance(color, str) and color.startswith("#"):
                color = color.upper()
            fonts_by_script = {
                script: _font_from_chain(properties, text, theme_fonts, script)
                for script in _scripts_in_text(text)
            }
            runs_out.append({
                "start": start, "end": cursor, "text": text,
                "font": fonts_by_script[_scripts_in_text(text)[0]],
                "fonts_by_script": fonts_by_script,
                "font_size": size / 100 if size else None,
                "font_weight": 700 if bold in {"1", "true"} else 400,
                "color": color,
                "italic": italic,
                "underline": underline,
                "strike": strike,
                "baseline": baseline,
                "decoration": "underline" if underline else "none",
                "letter_spacing": spacing / 100,
            })
        list_contract = _native_bullet_contract(paragraph_properties, level)
        if list_contract.get("bullet_type") == "picture":
            relationship_id = list_contract.get("bullet_relationship_id")
            relationship = (
                slide_relationships.get(relationship_id)
                if slide_relationships is not None
                and isinstance(relationship_id, str)
                else None
            )
            relationship_valid = (
                archive is not None
                and names is not None
                and relationship is not None
                and not relationship[2]
                and relationship[1].endswith("/image")
                and relationship[0] in names
            )
            list_contract["bullet_relationship_valid"] = relationship_valid
            list_contract["bullet_media_sha256"] = (
                _archive_sha256(archive, relationship[0])
                if relationship_valid and archive is not None and relationship is not None
                else None
            )
        paragraphs_out.append({
            "start": p_start, "end": cursor,
            "alignment": _alignment(_chain_attr(paragraph_properties, "algn")),
            "line_spacing": _paragraph_spacing(paragraph_properties, "lnSpc"),
            "space_before": _paragraph_spacing(paragraph_properties, "spcBef"),
            "space_after": _paragraph_spacing(paragraph_properties, "spcAft"),
            "margin_left": _chain_int(paragraph_properties, "marL"),
            "indent": _chain_int(paragraph_properties, "indent"),
            "list": list_contract,
        })
    paragraph_breaks = [paragraph["end"] for paragraph in paragraphs_out[:-1]]
    horizontal = paragraphs_out[0]["alignment"] if paragraphs_out else "left"
    margins = {
        "left": _chain_int(body_properties, "lIns"),
        "right": _chain_int(body_properties, "rIns"),
        "top": _chain_int(body_properties, "tIns"),
        "bottom": _chain_int(body_properties, "bIns"),
    }
    anchor = _chain_attr(body_properties, "anchor")
    wrap_value = _chain_attr(body_properties, "wrap")
    vertical_overflow = _chain_attr(body_properties, "vertOverflow")
    horizontal_overflow = _chain_attr(body_properties, "horzOverflow")
    overflow = (
        None
        if vertical_overflow is None and horizontal_overflow is None
        else vertical_overflow == "overflow" and horizontal_overflow == "overflow"
    )
    return {
        "slide_part": slide_part, "object_id": object_id, "object_name": name,
        "layer": layer, "hidden": hidden, "x": x, "y": y, "cx": cx, "cy": cy,
        "text": "".join(text_parts), "paragraphs": paragraphs_out, "runs": runs_out,
        "text_box": {
            "margins": margins,
            "alignment": horizontal,
            "vertical_alignment": _vertical_alignment(anchor),
            "wrap": None if wrap_value is None else wrap_value != "none",
            "overflow": overflow,
            "horizontal_overflow": horizontal_overflow,
            "vertical_overflow": vertical_overflow,
            "soft_breaks": soft_breaks,
            "paragraph_breaks": paragraph_breaks,
        },
    }


def _load_reconstruction_spec(value: dict[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, (Path, str)):
        raise ValidationError(
            "RECONSTRUCTION_SPEC_INVALID", "spec must be an object or path"
        )
    try:
        path = Path(value).expanduser().resolve()
        is_file = path.is_file()
        size = path.stat().st_size if is_file else None
    except (OSError, ValueError, TypeError) as exc:
        raise ValidationError(
            "RECONSTRUCTION_SPEC_INVALID", "cannot resolve or inspect spec path"
        ) from exc
    if not is_file:
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", f"spec not found: {path}")
    if size is not None and size > MAX_XML_BYTES:
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", f"spec too large: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", f"invalid spec: {path}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", "spec root must be an object")
    return payload


def _load_build_report(value: dict[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, (Path, str)):
        raise ValidationError("BUILD_REPORT_INVALID", "build report must be an object or path")
    try:
        path = Path(value).expanduser().resolve()
        is_file = path.is_file()
        size = path.stat().st_size if is_file else None
    except (OSError, ValueError, TypeError) as exc:
        raise ValidationError(
            "BUILD_REPORT_INVALID", "cannot resolve or inspect build report path"
        ) from exc
    if not is_file:
        raise ValidationError("BUILD_REPORT_INVALID", f"build report not found: {path}")
    if size is not None and size > MAX_XML_BYTES:
        raise ValidationError("BUILD_REPORT_INVALID", f"build report too large: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("BUILD_REPORT_INVALID", f"invalid build report: {path}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("BUILD_REPORT_INVALID", "build report root must be an object")
    return payload


def _validate_build_report_shape(report: dict[str, Any]) -> None:
    if set(report) != BUILD_REPORT_FIELDS:
        fixed_runtime_fields = BUILD_REPORT_FIELDS - {"preferred_font"}
        if set(report) == fixed_runtime_fields:
            try:
                report["preferred_font"] = validate_font_runtime(
                    report.get("font_runtime")
                )["family"]
            except ValueError as exc:
                raise ValidationError(
                    "BUILD_REPORT_INVALID",
                    f"legacy build report font runtime is invalid: {exc}",
                ) from exc
        if set(report) != BUILD_REPORT_FIELDS:
            previous_fields = BUILD_REPORT_FIELDS - {
                "preferred_font",
                "runtime_preflight",
                "font_runtime",
            }
            if set(report) == previous_fields:
                raise ValidationError(
                    "BUILD_REPORT_INVALID",
                    "build report is missing preferred_font; rebuild the PPTX",
                )
        legacy_fields = BUILD_REPORT_FIELDS - {
            "content_spec_sha256",
            "input_spec_sha256",
            "background_summary",
            "background_pictures",
        }
        if set(report) != BUILD_REPORT_FIELDS and set(report) == legacy_fields:
            raise ValidationError(
                "BUILD_REPORT_INVALID",
                "legacy build report is missing background and spec identity fields; rebuild the PPTX",
            )
        if set(report) != BUILD_REPORT_FIELDS:
            raise ValidationError(
                "BUILD_REPORT_INVALID", "build report fields do not match schema_version 1"
            )
    if report.get("valid") is not True or report.get("schema_version") != 1:
        raise ValidationError(
            "BUILD_REPORT_INVALID", "build report must declare valid schema_version 1"
        )
    for field in (
        "schema_sha256",
        "content_spec_sha256",
        "input_spec_sha256",
        "compiler_sha256",
        "capability_manifest_sha256",
        "pptx_sha256",
    ):
        value = report.get(field)
        if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
            raise ValidationError(
                "BUILD_REPORT_INVALID", f"build_report.{field} must be lowercase sha256"
            )
    preferred_font = report.get("preferred_font")
    if not isinstance(preferred_font, str) or not preferred_font.strip():
        raise ValidationError(
            "BUILD_REPORT_INVALID",
            "build_report.preferred_font must be a non-empty font family",
        )
    runtime_preflight = report.get("runtime_preflight")
    font_runtime_value = report.get("font_runtime")
    if runtime_preflight is None and font_runtime_value is None:
        pass
    elif (
        not isinstance(runtime_preflight, dict)
        or set(runtime_preflight) != {"path", "sha256"}
        or not isinstance(runtime_preflight.get("path"), str)
        or not Path(runtime_preflight["path"]).is_absolute()
        or not isinstance(runtime_preflight.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(runtime_preflight["sha256"]) is None
    ):
        raise ValidationError(
            "BUILD_REPORT_INVALID",
            "build_report runtime and font identities must be supplied together",
        )
    else:
        try:
            font_runtime = validate_font_runtime(font_runtime_value)
        except ValueError as exc:
            raise ValidationError(
                "BUILD_REPORT_INVALID",
                f"build_report.font_runtime is invalid: {exc}",
            ) from exc
        if font_runtime["family"] != preferred_font:
            raise ValidationError(
                "BUILD_REPORT_INVALID",
                "build_report preferred_font does not match font_runtime.family",
            )
    environment = report.get("environment")
    if (
        not isinstance(environment, dict)
        or set(environment) != {"python", "python-pptx", "Pillow"}
        or not all(
        isinstance(key, str) and key
        and isinstance(value, str) and value
        for key, value in environment.items()
        )
    ):
        raise ValidationError(
            "BUILD_REPORT_INVALID", "build_report.environment must contain string identities"
        )
    if report.get("warnings") != [] or report.get("unsupported") != []:
        raise ValidationError(
            "BUILD_REPORT_INVALID", "passing build report diagnostics must be empty"
        )
    normalization = report.get("normalization")
    valid_normalization = False
    if isinstance(normalization, dict) and type(normalization.get("applied")) is bool:
        if normalization["applied"] is False:
            valid_normalization = set(normalization) == {"applied"}
        else:
            valid_normalization = (
                set(normalization)
                == {"applied", "valid", "paragraphs_checked", "paragraphs_changed"}
                and normalization.get("valid") is True
                and type(normalization.get("paragraphs_checked")) is int
                and normalization["paragraphs_checked"] >= 0
                and type(normalization.get("paragraphs_changed")) is int
                and 0
                <= normalization["paragraphs_changed"]
                <= normalization["paragraphs_checked"]
            )
    if not valid_normalization:
        raise ValidationError(
            "BUILD_REPORT_INVALID", "build_report.normalization is invalid"
        )
    summary = report.get("representation_summary")
    if (
        not isinstance(summary, dict)
        or set(summary) != {"asset", "composite", "native", "not_applicable"}
        or not all(type(value) is int and value >= 0 for value in summary.values())
    ):
        raise ValidationError(
            "BUILD_REPORT_INVALID", "build_report.representation_summary is invalid"
        )
    background_summary = report.get("background_summary")
    if (
        not isinstance(background_summary, dict)
        or set(background_summary) != {"native", "background_picture"}
        or not all(
            type(value) is int and value >= 0
            for value in background_summary.values()
        )
    ):
        raise ValidationError(
            "BUILD_REPORT_INVALID", "build_report.background_summary is invalid"
        )
    background_pictures = report.get("background_pictures")
    if not isinstance(background_pictures, list) or not all(
        isinstance(item, dict) for item in background_pictures
    ):
        raise ValidationError(
            "BUILD_REPORT_INVALID",
            "build_report.background_pictures must be an object array",
        )
    for index, item in enumerate(background_pictures):
        if (
            set(item) != BACKGROUND_ITEM_FIELDS | {"media_sha256"}
            or item.get("selected_mode") != "background_picture"
            or not isinstance(item.get("media_sha256"), str)
            or SHA256_PATTERN.fullmatch(item["media_sha256"]) is None
        ):
            raise ValidationError(
                "BUILD_REPORT_INVALID",
                f"build_report.background_pictures[{index}] is invalid",
            )
    fallbacks = report.get("asset_fallbacks")
    if not isinstance(fallbacks, list) or not all(
        isinstance(item, dict) for item in fallbacks
    ):
        raise ValidationError(
            "BUILD_REPORT_INVALID", "build_report.asset_fallbacks must be an object array"
        )
    for index, fallback in enumerate(fallbacks):
        fallback_path = f"build_report.asset_fallbacks[{index}]"
        source_bbox = fallback.get("source_bbox")
        bindings = fallback.get("bound_element_ids")
        evidence = fallback.get("evidence")
        if (
            set(fallback) != REPRESENTATION_FACT_FIELDS
            or not isinstance(fallback.get("source_fact_id"), str)
            or not fallback["source_fact_id"].strip()
            or not isinstance(fallback.get("semantic_role"), str)
            or not fallback["semantic_role"].strip()
            or not isinstance(source_bbox, list)
            or len(source_bbox) != 4
            or not all(type(value) is int for value in source_bbox)
            or source_bbox[2] <= 0
            or source_bbox[3] <= 0
            or type(fallback.get("required")) is not bool
            or fallback.get("selected_mode") != "asset"
            or fallback.get("required_editability") not in {
                "none",
                "labels_only",
                "labels_and_geometry",
            }
            or fallback.get("fallback_policy") != "allow_minimal_asset"
            or not isinstance(bindings, list)
            or not bindings
            or not all(isinstance(value, str) and value for value in bindings)
            or len(bindings) != len(set(bindings))
            or not isinstance(fallback.get("reason"), str)
            or not fallback["reason"].strip()
            or fallback.get("coverage_status") != "covered"
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(value, str) and value.strip() for value in evidence)
        ):
            raise ValidationError(
                "BUILD_REPORT_INVALID", f"{fallback_path} is invalid"
            )
    elements = report.get("elements")
    if not isinstance(elements, dict) or not elements or not all(
        isinstance(key, str) and key for key in elements
    ):
        raise ValidationError(
            "BUILD_REPORT_INVALID", "build_report.elements must be a non-empty object"
        )
    for element_id, element in elements.items():
        element_path = f"build_report.elements.{element_id}"
        if not isinstance(element, dict) or set(element) != BUILD_REPORT_ELEMENT_FIELDS:
            raise ValidationError(
                "BUILD_REPORT_INVALID", f"{element_path} has invalid fields"
            )
        if not all(
            isinstance(element.get(field), str) and element[field]
            for field in ("semantic_kind", "selected_mode", "object_type")
        ):
            raise ValidationError(
                "BUILD_REPORT_INVALID", f"{element_path} identities must be strings"
            )
        objects = element.get("objects")
        if not isinstance(objects, list) or not objects:
            raise ValidationError(
                "BUILD_REPORT_INVALID", f"{element_path}.objects must be non-empty"
            )
        for index, item in enumerate(objects):
            item_path = f"{element_path}.objects[{index}]"
            if not isinstance(item, dict) or set(item) != BUILD_REPORT_OBJECT_FIELDS:
                raise ValidationError(
                    "BUILD_REPORT_INVALID", f"{item_path} has invalid fields"
                )
            name = item.get("ooxml_name")
            object_type = item.get("object_type")
            bbox = item.get("bbox")
            rotation = item.get("rotation")
            part_id = item.get("part_id")
            media_sha256 = item.get("media_sha256")
            text_summary = item.get("text_summary")
            fonts = item.get("font_declarations")
            if (
                not isinstance(name, str)
                or not name.startswith("ia:")
                or not isinstance(object_type, str)
                or not object_type
                or not isinstance(bbox, list)
                or len(bbox) != 4
                or not all(type(value) is int for value in bbox)
                or bbox[2] <= 0
                or bbox[3] <= 0
                or type(rotation) not in {int, float}
                or not math.isfinite(rotation)
                or not (part_id is None or isinstance(part_id, str) and part_id)
                or not (
                    media_sha256 is None
                    or isinstance(media_sha256, str)
                    and SHA256_PATTERN.fullmatch(media_sha256) is not None
                )
                or not (text_summary is None or isinstance(text_summary, str))
                or not isinstance(fonts, list)
                or not all(isinstance(font, str) and font for font in fonts)
                or fonts != sorted(set(fonts))
                or object_type == "pic"
                and (
                    media_sha256 is None
                    or text_summary is not None
                    or fonts != []
                )
                or object_type != "pic" and media_sha256 is not None
                or object_type == "cxnSp"
                and (text_summary is not None or fonts != [])
            ):
                raise ValidationError(
                    "BUILD_REPORT_INVALID", f"{item_path} contains invalid object data"
                )


def _compiler_sha256() -> str:
    scripts_root = Path(__file__).resolve().parent
    paths = [scripts_root / "build_pptx_from_spec.py"]
    paths.extend((scripts_root / "lib").glob("*.py"))
    paths.extend((scripts_root / "pptx_builder").glob("*.py"))
    identity = {
        path.relative_to(scripts_root).as_posix(): _file_sha256(path)
        for path in sorted(paths)
        if path.is_file()
    }
    return canonical_json_sha256(identity)


def _report_error(result: dict[str, Any], code: str, path: str, detail: str) -> None:
    result["errors"].append(code)
    result["warnings"].append(f"{path}: {detail}")


def _validate_report_identities(
    result: dict[str, Any],
    spec: dict[str, Any] | None,
    report: dict[str, Any],
) -> None:
    if report.get("valid") is not True or report.get("schema_version") != 1:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report",
            "report must be a passing schema_version 1 report",
        )
    if report.get("pptx_sha256") != result.get("pptx_sha256"):
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.pptx_sha256",
            "report does not bind the current PPTX",
        )
    if spec is None:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "reconstruction_spec",
            "a reconstruction spec is required with a build report",
        )
    else:
        representation_issues = validate_representation_plan(spec)
        if representation_issues:
            issue = representation_issues[0]
            raise ValidationError(
                "RECONSTRUCTION_SPEC_INVALID",
                f"{issue.path}: {issue.detail}",
            )
        background_issues = validate_background_prebuild(spec)
        if background_issues:
            issue = background_issues[0]
            raise ValidationError(
                "RECONSTRUCTION_SPEC_INVALID",
                f"{issue.path}: {issue.detail}",
            )
        try:
            schema_digest = canonical_json_sha256(spec)
            content_digest = content_spec_sha256(spec)
            input_digest = input_spec_sha256(spec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(
                "RECONSTRUCTION_SPEC_INVALID", "cannot hash reconstruction spec"
            ) from exc
        if report.get("schema_sha256") != schema_digest:
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                "build_report.schema_sha256",
                "report does not bind the current reconstruction spec",
            )
        if report.get("content_spec_sha256") != content_digest:
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                "build_report.content_spec_sha256",
                "report does not bind the current content specification",
            )
        if report.get("input_spec_sha256") != input_digest:
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                "build_report.input_spec_sha256",
                "report does not bind the exact supplied reconstruction spec",
            )
    try:
        compiler_digest = _compiler_sha256()
        manifest_digest = capability_manifest_sha256()
    except (OSError, TypeError, ValueError) as exc:
        raise ValidationError(
            "BUILD_REPORT_INVALID", "cannot recompute compiler identity"
        ) from exc
    if report.get("compiler_sha256") != compiler_digest:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.compiler_sha256",
            "report compiler identity is stale",
        )
    if report.get("capability_manifest_sha256") != manifest_digest:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.capability_manifest_sha256",
            "report capability manifest identity is stale",
        )


def _expected_report_media_hashes(report: dict[str, Any]) -> set[str]:
    elements = report.get("elements")
    if not isinstance(elements, dict):
        return set()
    hashes: set[str] = set()
    for element in elements.values():
        objects = element.get("objects") if isinstance(element, dict) else None
        if not isinstance(objects, list):
            continue
        for item in objects:
            digest = item.get("media_sha256") if isinstance(item, dict) else None
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                hashes.add(digest)
    return hashes


def _validate_report_media_inventory(
    archive: zipfile.ZipFile,
    names: set[str],
    result: dict[str, Any],
    report: dict[str, Any],
) -> None:
    expected = _expected_report_media_hashes(report)
    if not expected:
        return
    actual = {
        _archive_sha256(archive, name)
        for name in names
        if name.startswith("ppt/media/")
        and not name.endswith("/")
        and archive.getinfo(name).file_size <= MAX_MEDIA_BYTES
    }
    if not expected.issubset(actual):
        _report_error(
            result,
            "ASSET_HASH_MISMATCH",
            "build_report.elements",
            "embedded media does not match the registered asset hash",
        )


def _bound_element_id(name: Any, element_ids: set[str]) -> str | None:
    if not isinstance(name, str):
        return None
    matches = [
        element_id
        for element_id in element_ids
        if name == f"ia:{element_id}" or name.startswith(f"ia:{element_id}:")
    ]
    return max(matches, key=len) if matches else None


def _bbox_matches_element(
    record: dict[str, Any],
    expected: Any,
    width: int,
    height: int,
) -> bool:
    if not isinstance(expected, list) or len(expected) != 4:
        return True
    actual = [record.get(key) for key in ("x", "y", "cx", "cy")]
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in actual + expected
    ):
        return False
    return all(
        abs(left - right) / scale <= 0.01
        for left, right, scale in zip(actual, expected, (width, height, width, height))
    )


def _expected_media_sha256(value: Any, element_id: str) -> str | None:
    if isinstance(value, dict):
        if value.get("element_id") == element_id:
            for key in ("asset_sha256", "source_sha256", "sha256"):
                digest = value.get(key)
                if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                    return digest.lower()
        for child in value.values():
            found = _expected_media_sha256(child, element_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _expected_media_sha256(child, element_id)
            if found:
                return found
    return None


def _chart_points(series: ET.Element, path: str, *, numeric: bool) -> list[Any]:
    cache_path = "c:val/c:numRef/c:numCache" if numeric else "c:cat/c:strRef/c:strCache"
    cache = series.find(cache_path, NS)
    if cache is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} cache is missing")
    points: dict[int, Any] = {}
    for point in cache.findall("c:pt", NS):
        index = _int_attr(point, "idx")
        value = point.find("c:v", NS)
        if index is None or index < 0 or index in points or value is None:
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} cache is invalid")
        raw = value.text or ""
        if numeric:
            try:
                parsed = float(raw)
            except ValueError as exc:
                raise ValidationError(
                    "NATIVE_CHART_CONTRACT_MISMATCH", f"{path} value is not numeric"
                ) from exc
            if not math.isfinite(parsed):
                raise ValidationError(
                    "NATIVE_CHART_CONTRACT_MISMATCH", f"{path} value is not finite"
                )
            points[index] = parsed
        else:
            points[index] = raw or None
    if not points or set(points) != set(range(len(points))):
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} indexes are not contiguous")
    return [points[index] for index in range(len(points))]


def _chart_bool(owner: ET.Element | None, tag: str) -> bool:
    node = owner.find(f"c:{tag}", NS) if owner is not None else None
    return node is not None and node.get("val", "1") not in {"0", "false"}


def _chart_sparse_numeric_points(series: ET.Element, path: str) -> list[float | None]:
    cache = series.find("c:val/c:numRef/c:numCache", NS)
    count = _int_attr(cache.find("c:ptCount", NS) if cache is not None else None, "val")
    if cache is None or count is None or count < 1:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} cache is missing or invalid")
    values: list[float | None] = [None] * count
    seen: set[int] = set()
    for point in cache.findall("c:pt", NS):
        index = _int_attr(point, "idx")
        node = point.find("c:v", NS)
        if index is None or not 0 <= index < count or index in seen or node is None:
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} cache point is invalid")
        try:
            value = float(node.text or "")
        except ValueError as exc:
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} value is not numeric") from exc
        if not math.isfinite(value):
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} value is not finite")
        values[index] = value
        seen.add(index)
    return values


def _chart_series_name(series: ET.Element) -> str | None:
    value = series.find("c:tx/c:strRef/c:strCache/c:pt/c:v", NS)
    text = value.text if value is not None else None
    return text if isinstance(text, str) and text else None


def _chart_rgb(node: ET.Element | None, path: str) -> str:
    value = node.get("val") if node is not None else None
    if not isinstance(value, str) or re.fullmatch(r"[0-9A-Fa-f]{6}", value) is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} RGB color is invalid")
    return f"#{value.upper()}"


def _chart_opacity(color: ET.Element | None) -> float:
    alpha = color.find("a:alpha", NS) if color is not None else None
    value = _int_attr(alpha, "val")
    return 1 if value is None else value / 100000


def _chart_line_contract(line: ET.Element | None, path: str) -> str | dict[str, Any]:
    if line is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} line is missing")
    if line.find("a:noFill", NS) is not None:
        return "noFill"
    color = line.find("a:solidFill/a:srgbClr", NS)
    width = _int_attr(line, "w")
    dash = line.find("a:prstDash", NS)
    dash_value = dash.get("val") if dash is not None else None
    normalized_dash = {
        "solid": "solid",
        "dash": "dash",
        "dot": "dot",
        "dashDot": "dashDot",
    }.get(dash_value)
    if width is None or width < 1 or normalized_dash is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} line width or dash is invalid")
    return {
        "color": _chart_rgb(color, f"{path}.color"),
        "width": width,
        "dash": normalized_dash,
        "opacity": _chart_opacity(color),
    }


def _chart_area_contract(owner: ET.Element, path: str) -> dict[str, Any]:
    properties = owner.find("c:spPr", NS)
    if properties is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} spPr is missing")
    if properties.find("a:noFill", NS) is not None:
        fill: str | dict[str, Any] = "noFill"
    else:
        color = properties.find("a:solidFill/a:srgbClr", NS)
        fill = {
            "type": "solid",
            "color": _chart_rgb(color, f"{path}.fill.color"),
            "opacity": _chart_opacity(color),
        }
    return {
        "fill": fill,
        "line": _chart_line_contract(properties.find("a:ln", NS), f"{path}.line"),
    }


def _chart_font_family(run: ET.Element, path: str) -> str:
    families = {
        tag: (
            run.find(f"a:{tag}", NS).get("typeface")
            if run.find(f"a:{tag}", NS) is not None
            else None
        )
        for tag in ("latin", "ea", "cs")
    }
    if any(not isinstance(value, str) or not value for value in families.values()):
        raise ValidationError(
            "NATIVE_CHART_CONTRACT_MISMATCH",
            f"{path} requires explicit a:latin/a:ea/a:cs fonts",
        )
    if len(set(families.values())) != 1:
        raise ValidationError(
            "NATIVE_CHART_CONTRACT_MISMATCH",
            f"{path} a:latin/a:ea/a:cs fonts must match",
        )
    return families["latin"]  # type: ignore[return-value]


def _chart_text_style(owner: ET.Element | None, path: str) -> dict[str, Any]:
    run = owner.find("c:txPr/a:p/a:pPr/a:defRPr", NS) if owner is not None else None
    if run is None and owner is not None:
        run = owner.find("c:txPr/a:p/a:endParaRPr", NS)
    if run is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} text properties are missing")
    font_name = _chart_font_family(run, path)
    color = run.find("a:solidFill/a:srgbClr", NS)
    size = _int_attr(run, "sz")
    if size is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{path} font name or size is missing")
    return {
        "font_name": font_name,
        "font_size": size / 100,
        "bold": run.get("b", "0") in {"1", "true"},
        "color": _chart_rgb(color, f"{path}.color"),
    }


def _chart_data_labels(plot: ET.Element, *, cartesian: bool) -> dict[str, Any]:
    labels = plot.find("c:dLbls", NS)
    if labels is None and cartesian:
        series_labels = plot.findall("c:ser/c:dLbls", NS)
        if series_labels:
            canonical = ET.tostring(series_labels[0], encoding="unicode")
            if any(
                ET.tostring(item, encoding="unicode") != canonical
                for item in series_labels[1:]
            ):
                raise ValidationError(
                    "NATIVE_CHART_CONTRACT_MISMATCH",
                    "series-level data label contracts differ",
                )
            labels = series_labels[0]
    position = labels.find("c:dLblPos", NS) if labels is not None else None
    number_format = labels.find("c:numFmt", NS) if labels is not None else None
    result = {
        "enabled": labels is not None,
        "show_category": _chart_bool(labels, "showCatName"),
        "show_value": _chart_bool(labels, "showVal"),
        "position": {
            "above": "above",
            "t": "above",
            "below": "below",
            "b": "below",
            "bestFit": "best_fit",
            "ctr": "center",
            "inBase": "inside_base",
            "inEnd": "inside_end",
            "l": "left",
            "outEnd": "outside_end",
            "r": "right",
        }.get(position.get("val") if position is not None else None),
        "number_format": number_format.get("formatCode") if number_format is not None else None,
    }
    if cartesian:
        result["show_series_name"] = _chart_bool(labels, "showSerName")
    else:
        result["show_percentage"] = _chart_bool(labels, "showPercent")
    if labels is not None:
        if cartesian:
            result.update(_chart_text_style(labels, "data_labels"))
        else:
            run = labels.find("c:txPr/a:p/a:pPr/a:defRPr", NS)
            color = run.find("a:solidFill/a:srgbClr", NS) if run is not None else None
            size = _int_attr(run, "sz")
            if run is None or size is None:
                raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", "data_labels size is missing")
            _chart_font_family(run, "data_labels")
            result.update(
                {
                    "font_size": size / 100,
                    "bold": run.get("b", "0") in {"1", "true"},
                    "color": _chart_rgb(color, "data_labels.color"),
                }
            )
    return result


def _chart_axis_contract(
    axis: ET.Element,
    *,
    category: bool,
    path: str,
) -> dict[str, Any]:
    position = axis.find("c:axPos", NS)
    axis_position = {
        "t": "top",
        "b": "bottom",
        "l": "left",
        "r": "right",
    }.get(position.get("val") if position is not None else None)
    result = {
        "visible": not _chart_bool(axis, "delete"),
        "position": axis_position,
        **_chart_text_style(axis, path),
        "line": _chart_line_contract(axis.find("c:spPr/a:ln", NS), f"{path}.line"),
    }
    if category:
        orientation = axis.find("c:scaling/c:orientation", NS)
        label_position = axis.find("c:tickLblPos", NS)
        result.update(
            {
                "reverse_order": orientation is not None and orientation.get("val") == "maxMin",
                "label_position": {
                    "nextTo": "next_to_axis",
                    "low": "low",
                    "high": "high",
                    "none": "none",
                }.get(label_position.get("val") if label_position is not None else None),
            }
        )
        return result
    number_format = axis.find("c:numFmt", NS)
    gridlines = axis.find("c:majorGridlines", NS)
    result.update(
        {
            "minimum": _float_attr(axis.find("c:scaling/c:min", NS), "val"),
            "maximum": _float_attr(axis.find("c:scaling/c:max", NS), "val"),
            "major_unit": _float_attr(axis.find("c:majorUnit", NS), "val"),
            "number_format": number_format.get("formatCode") if number_format is not None else None,
            "major_gridlines": {
                "visible": gridlines is not None,
                "line": (
                    _chart_line_contract(gridlines.find("c:spPr/a:ln", NS), f"{path}.major_gridlines.line")
                    if gridlines is not None
                    else None
                ),
            },
        }
    )
    return result


def _chart_legend_contract(chart_root: ET.Element) -> dict[str, Any]:
    legend = chart_root.find("c:chart/c:legend", NS)
    if legend is None:
        return {"enabled": False}
    position = legend.find("c:legendPos", NS)
    overlay = legend.find("c:overlay", NS)
    return {
        "enabled": True,
        "position": {
            "t": "top",
            "b": "bottom",
            "l": "left",
            "r": "right",
        }.get(position.get("val") if position is not None else None),
        "overlay": overlay is not None and overlay.get("val", "0") in {"1", "true"},
        **_chart_text_style(legend, "legend"),
    }


def _native_chart_contract(
    archive: zipfile.ZipFile,
    names: set[str],
    slide_part: str,
    frame: ET.Element,
    slide_relationships: dict[str, tuple[str, str, bool]],
) -> dict[str, Any]:
    """Extract one supported native chart and its embedded workbook identity."""
    identity = frame.find("p:nvGraphicFramePr/p:cNvPr", NS)
    object_name = identity.get("name") if identity is not None else None
    chart_ref = frame.find("a:graphic/a:graphicData/c:chart", NS)
    relationship_id = chart_ref.get(RID) if chart_ref is not None else None
    relationship = slide_relationships.get(relationship_id or "")
    if (
        relationship is None
        or relationship[2]
        or not relationship[1].endswith("/chart")
        or relationship[0] not in names
    ):
        raise ValidationError(
            "NATIVE_CHART_RELATIONSHIP_INVALID",
            f"{slide_part} chart frame {object_name!r} lacks an internal chart relationship",
        )
    chart_part = relationship[0]
    chart_root = _xml(archive, chart_part)
    plot_candidates = [
        ("pie", chart_root.find("c:chart/c:plotArea/c:pieChart", NS)),
        ("doughnut", chart_root.find("c:chart/c:plotArea/c:doughnutChart", NS)),
        ("bar_plot", chart_root.find("c:chart/c:plotArea/c:barChart", NS)),
        ("line", chart_root.find("c:chart/c:plotArea/c:lineChart", NS)),
    ]
    plots = [(name, node) for name, node in plot_candidates if node is not None]
    if len(plots) != 1:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} must contain exactly one supported plot")
    plot_kind, plot = plots[0]
    assert plot is not None

    chart_rels_part = _slide_rels_part(chart_part)
    chart_relationships = (
        _relationship_map(archive, chart_rels_part) if chart_rels_part in names else {}
    )
    external_data = chart_root.find("c:externalData", NS)
    workbook_id = external_data.get(RID) if external_data is not None else None
    workbook_relation = chart_relationships.get(workbook_id or "")
    workbook_part = (
        workbook_relation[0]
        if workbook_relation is not None
        and not workbook_relation[2]
        and workbook_relation[1].endswith("/package")
        and workbook_relation[0] in names
        else None
    )
    element_id = (
        object_name[3:]
        if isinstance(object_name, str) and object_name.startswith("ia:")
        else None
    )
    common = {
        "element_id": element_id,
        "object_name": object_name,
        "slide_part": slide_part,
        "chart_part": chart_part,
        "embedded_workbook_part": workbook_part,
    }

    if plot_kind in {"pie", "doughnut"}:
        series = plot.findall("c:ser", NS)
        if len(series) != 1:
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} must contain exactly one pie series")
        categories = _chart_points(series[0], f"{chart_part}.categories", numeric=False)
        values = _chart_points(series[0], f"{chart_part}.values", numeric=True)
        if len(categories) != len(values) or len(values) < 2:
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} slice caches are misaligned")
        colors: dict[int, str] = {}
        for point in series[0].findall("c:dPt", NS):
            index = _int_attr(point.find("c:idx", NS), "val")
            if index is None or index in colors:
                raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} slice index is invalid")
            colors[index] = _chart_rgb(point.find("c:spPr/a:solidFill/a:srgbClr", NS), f"{chart_part}.slice.color")
        if set(colors) != set(range(len(values))):
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} requires one explicit color per slice")
        return {
            **common,
            "chart_type": plot_kind,
            "slices": [
                {"category": categories[index], "value": values[index], "color": colors[index]}
                for index in range(len(values))
            ],
            "data_labels": _chart_data_labels(plot, cartesian=False),
            "first_slice_angle": _int_attr(plot.find("c:firstSliceAng", NS), "val"),
            "hole_size": _int_attr(plot.find("c:holeSize", NS), "val"),
        }

    if plot_kind == "bar_plot":
        direction_node = plot.find("c:barDir", NS)
        direction = direction_node.get("val") if direction_node is not None else None
        chart_type = {"col": "column", "bar": "bar"}.get(direction)
        grouping_node = plot.find("c:grouping", NS)
        grouping = {
            "clustered": "clustered",
            "stacked": "stacked",
            "percentStacked": "percent_stacked",
        }.get(grouping_node.get("val") if grouping_node is not None else None)
        if chart_type is None or grouping is None:
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} bar direction or grouping is invalid")
    else:
        chart_type = "line"
        grouping_node = plot.find("c:grouping", NS)
        grouping = grouping_node.get("val") if grouping_node is not None else None
        if grouping != "standard":
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} line grouping must be standard")

    series_nodes = plot.findall("c:ser", NS)
    if not series_nodes:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} requires at least one series")
    categories: list[Any] | None = None
    extracted_series: list[dict[str, Any]] = []
    for index, series in enumerate(series_nodes):
        current_categories = _chart_points(series, f"{chart_part}.series[{index}].categories", numeric=False)
        values = _chart_sparse_numeric_points(series, f"{chart_part}.series[{index}].values")
        if len(current_categories) != len(values):
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} series caches are misaligned")
        if categories is None:
            categories = current_categories
        elif categories != current_categories:
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} series categories differ")
        if chart_type in {"column", "bar"}:
            extracted_series.append(
                {
                    "name": _chart_series_name(series),
                    "values": values,
                    "color": _chart_rgb(series.find("c:spPr/a:solidFill/a:srgbClr", NS), f"{chart_part}.series[{index}].color"),
                }
            )
            continue
        line = series.find("c:spPr/a:ln", NS)
        line_contract = _chart_line_contract(line, f"{chart_part}.series[{index}].line")
        if not isinstance(line_contract, dict):
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} line series cannot use noFill")
        marker = series.find("c:marker", NS)
        marker_symbol = marker.find("c:symbol", NS) if marker is not None else None
        marker_size = _int_attr(marker.find("c:size", NS) if marker is not None else None, "val")
        marker_line = marker.find("c:spPr/a:ln", NS) if marker is not None else None
        marker_line_contract = _chart_line_contract(marker_line, f"{chart_part}.series[{index}].marker.line")
        if not isinstance(marker_line_contract, dict):
            raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} marker line cannot use noFill")
        extracted_series.append(
            {
                "name": _chart_series_name(series),
                "values": values,
                "color": line_contract["color"],
                "line": {"width": line_contract["width"], "dash": line_contract["dash"]},
                "marker": {
                    "style": marker_symbol.get("val") if marker_symbol is not None else None,
                    "size": marker_size,
                    "fill": _chart_rgb(marker.find("c:spPr/a:solidFill/a:srgbClr", NS) if marker is not None else None, f"{chart_part}.series[{index}].marker.fill"),
                    "line_color": marker_line_contract["color"],
                    "line_width": marker_line_contract["width"],
                },
                "smooth": _chart_bool(series, "smooth"),
            }
        )

    category_axes = chart_root.findall("c:chart/c:plotArea/c:catAx", NS)
    value_axes = chart_root.findall("c:chart/c:plotArea/c:valAx", NS)
    if len(category_axes) != 1 or len(value_axes) != 1:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} requires one category axis and one value axis")
    plot_axis_ids = [_int_attr(node, "val") for node in plot.findall("c:axId", NS)]
    category_axis_id = _int_attr(category_axes[0].find("c:axId", NS), "val")
    value_axis_id = _int_attr(value_axes[0].find("c:axId", NS), "val")
    category_cross_id = _int_attr(category_axes[0].find("c:crossAx", NS), "val")
    value_cross_id = _int_attr(value_axes[0].find("c:crossAx", NS), "val")
    if (
        category_axis_id is None
        or value_axis_id is None
        or len(plot_axis_ids) != 2
        or set(plot_axis_ids) != {category_axis_id, value_axis_id}
        or category_cross_id != value_axis_id
        or value_cross_id != category_axis_id
    ):
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} axis IDs or cross-axis bindings are invalid")
    chart_node = chart_root.find("c:chart", NS)
    blanks = chart_node.find("c:dispBlanksAs", NS) if chart_node is not None else None
    plot_area = chart_root.find("c:chart/c:plotArea", NS)
    if plot_area is None:
        raise ValidationError("NATIVE_CHART_CONTRACT_MISMATCH", f"{chart_part} plot area is missing")
    result = {
        **common,
        "chart_type": chart_type,
        "grouping": grouping,
        "categories": categories or [],
        "series": extracted_series,
        "axes": {
            "category": _chart_axis_contract(category_axes[0], category=True, path="category_axis"),
            "value": _chart_axis_contract(value_axes[0], category=False, path="value_axis"),
        },
        "legend": _chart_legend_contract(chart_root),
        "data_labels": _chart_data_labels(plot, cartesian=True),
        "display_blanks_as": blanks.get("val") if blanks is not None else None,
        "chart_area": _chart_area_contract(chart_root, "chart_area"),
        "plot_area": _chart_area_contract(plot_area, "plot_area"),
    }
    if chart_type in {"column", "bar"}:
        result["gap_width"] = _int_attr(plot.find("c:gapWidth", NS), "val")
        result["overlap"] = _int_attr(plot.find("c:overlap", NS), "val")
    return result


def _expected_chart_line(value: Any) -> Any:
    if value == "noFill":
        return value
    if not isinstance(value, dict):
        return value
    return {
        "color": str(value.get("color", "")).upper(),
        "width": value.get("width"),
        "dash": value.get("dash"),
        "opacity": value.get("opacity"),
    }


def _expected_chart_area(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    fill = value.get("fill")
    normalized_fill = (
        fill
        if fill == "noFill" or not isinstance(fill, dict)
        else {
            "type": fill.get("type"),
            "color": str(fill.get("color", "")).upper(),
            "opacity": fill.get("opacity"),
        }
    )
    return {
        "fill": normalized_fill,
        "line": _expected_chart_line(value.get("line")),
    }


def _expected_chart_font(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "font_name": value.get("font_name"),
        "font_size": value.get("font_size"),
        "bold": (
            value.get("bold")
            if "bold" in value
            else value.get("font_weight", 0) >= 600
        ),
        "color": str(value.get("color", "")).upper(),
    }


def _observable_cartesian_labels(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return {"enabled": False}
    return {
        "enabled": True,
        "show_category": value.get("show_category"),
        "show_series_name": value.get("show_series_name"),
        "show_value": value.get("show_value"),
        "position": value.get("position"),
        "number_format": value.get("number_format"),
        **_expected_chart_font(value),
    }


def _observable_chart_legend(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return {"enabled": False}
    return {
        "enabled": True,
        "position": value.get("position"),
        "overlay": value.get("overlay"),
        **_expected_chart_font(value),
    }


def _expected_cartesian_chart(element: dict[str, Any]) -> dict[str, Any]:
    content = element.get("content", {})
    style = element.get("style", {})
    chart_type = content.get("chart_type")
    series: list[dict[str, Any]] = []
    for item in content.get("series", []):
        if not isinstance(item, dict):
            continue
        normalized: dict[str, Any] = {
            "name": item.get("name"),
            "values": [
                None if value is None else float(value)
                for value in item.get("values", [])
            ],
            "color": str(item.get("color", "")).upper(),
        }
        if chart_type == "line":
            line = item.get("line", {})
            marker = item.get("marker", {})
            normalized.update(
                {
                    "line": {
                        "width": line.get("width"),
                        "dash": line.get("dash"),
                    },
                    "marker": {
                        "style": marker.get("style"),
                        "size": marker.get("size"),
                        "fill": str(marker.get("fill", "")).upper(),
                        "line_color": str(marker.get("line_color", "")).upper(),
                        "line_width": marker.get("line_width"),
                    },
                    "smooth": item.get("smooth"),
                }
            )
        series.append(normalized)
    axes = content.get("axes", {})
    category = axes.get("category", {}) if isinstance(axes, dict) else {}
    value = axes.get("value", {}) if isinstance(axes, dict) else {}
    gridlines = value.get("major_gridlines", {}) if isinstance(value, dict) else {}
    expected: dict[str, Any] = {
        "chart_type": chart_type,
        "grouping": content.get("grouping"),
        "categories": content.get("categories"),
        "series": series,
        "axes": {
            "category": {
                "visible": category.get("visible"),
                "position": category.get("position"),
                "reverse_order": category.get("reverse_order"),
                "label_position": category.get("label_position"),
                **_expected_chart_font(category),
                "line": _expected_chart_line(category.get("line")),
            },
            "value": {
                "visible": value.get("visible"),
                "position": value.get("position"),
                "minimum": None if value.get("minimum") is None else float(value["minimum"]),
                "maximum": None if value.get("maximum") is None else float(value["maximum"]),
                "major_unit": None if value.get("major_unit") is None else float(value["major_unit"]),
                "number_format": value.get("number_format"),
                **_expected_chart_font(value),
                "line": _expected_chart_line(value.get("line")),
                "major_gridlines": {
                    "visible": gridlines.get("visible"),
                    "line": (
                        _expected_chart_line(gridlines.get("line"))
                        if gridlines.get("visible") is True
                        else None
                    ),
                },
            },
        },
        "legend": _observable_chart_legend(content.get("legend")),
        "data_labels": _observable_cartesian_labels(content.get("data_labels")),
        "display_blanks_as": content.get("display_blanks_as"),
        "chart_area": _expected_chart_area(style.get("chart_area")),
        "plot_area": _expected_chart_area(style.get("plot_area")),
    }
    if chart_type in {"column", "bar"}:
        expected["gap_width"] = style.get("gap_width")
        expected["overlap"] = style.get("overlap")
    return expected


def _validate_native_chart_contracts(
    result: dict[str, Any], spec: dict[str, Any]
) -> None:
    elements = spec.get("elements")
    if not isinstance(elements, list):
        return
    expected = {
        item.get("element_id"): item
        for item in elements
        if isinstance(item, dict)
        and item.get("kind") == "chart"
        and isinstance(item.get("element_id"), str)
    }
    actual = {
        item.get("element_id"): item
        for item in result.get("native_chart_objects", [])
        if isinstance(item, dict) and isinstance(item.get("element_id"), str)
    }
    for element_id, element in expected.items():
        result["native_chart_contracts_checked"] += 1
        observed = actual.get(element_id)
        if observed is None:
            _report_error(
                result,
                "NATIVE_CHART_CONTRACT_MISMATCH",
                f"elements.{element_id}",
                "named native chart object is missing",
            )
            continue
        if observed.get("embedded_workbook_part") is None:
            _report_error(
                result,
                "NATIVE_CHART_WORKBOOK_MISSING",
                f"elements.{element_id}.content",
                "native chart lacks an internal embedded workbook",
            )
        content = element.get("content", {})
        style = element.get("style", {})
        if content.get("chart_type") in {"column", "bar", "line"}:
            expected_cartesian = _expected_cartesian_chart(element)
            observed_cartesian = {
                key: observed.get(key) for key in expected_cartesian
            }
            observed_cartesian["legend"] = _observable_chart_legend(
                observed.get("legend")
            )
            observed_cartesian["data_labels"] = _observable_cartesian_labels(
                observed.get("data_labels")
            )
            if observed_cartesian != expected_cartesian:
                _report_error(
                    result,
                    "NATIVE_CHART_CONTRACT_MISMATCH",
                    f"elements.{element_id}",
                    "native cartesian chart data, grouping, series, axes, legend, labels, or style differs from the spec",
                )
            continue
        expected_slices = [
            {
                "category": item.get("category"),
                "value": float(item.get("value")),
                "color": str(item.get("color", "")).upper(),
            }
            for item in content.get("slices", [])
            if isinstance(item, dict) and type(item.get("value")) in {int, float}
        ]
        labels = content.get("data_labels", {})
        observed_labels = observed.get("data_labels", {})
        mismatch = (
            observed.get("chart_type") != content.get("chart_type")
            or observed.get("slices") != expected_slices
            or observed.get("first_slice_angle") != style.get("first_slice_angle")
            or observed.get("hole_size") != style.get("hole_size")
            or observed_labels.get("enabled") != labels.get("enabled")
        )
        if labels.get("enabled") is True:
            mismatch = mismatch or any(
                (
                    observed_labels.get("show_category") != labels.get("show_category"),
                    observed_labels.get("show_value") != labels.get("show_value"),
                    observed_labels.get("show_percentage") != labels.get("show_percentage"),
                    observed_labels.get("position") != labels.get("position"),
                    observed_labels.get("number_format") != labels.get("number_format"),
                    observed_labels.get("font_size") != labels.get("font_size"),
                    observed_labels.get("bold") != (labels.get("font_weight", 0) >= 600),
                    observed_labels.get("color") != str(labels.get("color", "")).upper(),
                )
            )
        if mismatch:
            _report_error(
                result,
                "NATIVE_CHART_CONTRACT_MISMATCH",
                f"elements.{element_id}",
                "native chart type, slices, labels, angle, or hole differs from the spec",
            )


def _validate_element_bindings(
    result: dict[str, Any],
    spec: dict[str, Any],
    width: int,
    height: int,
) -> None:
    elements = spec.get("elements")
    if not isinstance(elements, list) or not elements:
        return
    element_map = {
        item.get("element_id"): item
        for item in elements
        if isinstance(item, dict) and isinstance(item.get("element_id"), str)
    }
    element_ids = set(element_map)
    structures = [
        item
        for item in result.get("structure_objects", [])
        if item.get("visible") is True
        and item.get("geometry_known") is True
        and _bound_element_id(item.get("object_name"), element_ids) is not None
    ]
    text_objects = [
        item
        for item in result.get("text_objects", [])
        if item.get("visible") is True
        and _bound_element_id(item.get("object_name"), element_ids) is not None
    ]
    pictures = [
        item
        for item in result.get("picture_objects", [])
        if item.get("visible") is True
        and item.get("geometry_known") is True
        and _bound_element_id(item.get("object_name"), element_ids) is not None
    ]
    type_map = {
        "text": {"sp"},
        "special_text": {"sp"},
        "icon": {"pic"},
        "picture": {"pic"},
        "shape": {"sp"},
        "status": {"sp"},
        "line": {"cxnSp", "sp"},
        "table": {"graphicFrame", "sp"},
        "matrix": {"graphicFrame", "sp"},
        "chart": {"graphicFrame"},
        "diagram": {"graphicFrame", "sp"},
    }
    for element_id, element in element_map.items():
        candidates = [
            item
            for item in structures
            if _bound_element_id(item.get("object_name"), element_ids) == element_id
        ]
        if not candidates:
            result["errors"].append("ELEMENT_OBJECT_MISSING")
            result["warnings"].append(f"{element_id}: no visible object named ia:{element_id}[:part]")
            continue
        kind = element.get("kind")
        allowed_types = type_map.get(kind)
        if allowed_types and not any(item.get("object_type") in allowed_types for item in candidates):
            result["errors"].append("ELEMENT_OBJECT_TYPE_MISMATCH")
            result["warnings"].append(f"{element_id}: bound object type does not match {kind}")
        if kind in {"matrix", "status"}:
            actual_boxes = [
                [item.get("x"), item.get("y"), item.get("cx"), item.get("cy")]
                for item in candidates
            ]
            bbox_matches = (
                bool(actual_boxes)
                and all(all(type(value) is int for value in box) for box in actual_boxes)
                and bbox_union(actual_boxes) == element.get("slide_bbox")
            )
        else:
            bbox_matches = any(
                _bbox_matches_element(item, element.get("slide_bbox"), width, height)
                for item in candidates
            )
        if not bbox_matches:
            result["errors"].append("ELEMENT_BBOX_MISMATCH")
            result["warnings"].append(f"{element_id}: bound object bbox does not match the spec")
        if kind in {"text", "special_text"}:
            expected_text = element.get("content", {}).get("text")
            bound_text = [
                item
                for item in text_objects
                if _bound_element_id(item.get("object_name"), element_ids) == element_id
            ]
            if not bound_text:
                result["errors"].append("ELEMENT_OBJECT_TYPE_MISMATCH")
            elif isinstance(expected_text, str) and not any(
                item.get("text") == expected_text for item in bound_text
            ):
                result["errors"].append("ELEMENT_TEXT_MISMATCH")
                result["warnings"].append(f"{element_id}: editable text differs from the spec")
        if kind in {"icon", "picture"}:
            bound_pictures = [
                item
                for item in pictures
                if _bound_element_id(item.get("object_name"), element_ids) == element_id
            ]
            if not bound_pictures:
                result["errors"].append("ELEMENT_OBJECT_TYPE_MISMATCH")
            asset = element.get("content", {}).get("asset")
            expected_hash = (
                asset.get("asset_sha256") if isinstance(asset, dict) else None
            ) or _expected_media_sha256(spec.get("modules"), element_id)
            if expected_hash and not any(
                item.get("media_sha256") == expected_hash for item in bound_pictures
            ):
                result["errors"].append("ELEMENT_MEDIA_HASH_MISMATCH")
                result["warnings"].append(f"{element_id}: embedded media does not match the declared asset")
        result["element_bindings_checked"] = result.get("element_bindings_checked", 0) + 1


def _validate_report_object_bindings(
    result: dict[str, Any], report: dict[str, Any]
) -> None:
    elements = report.get("elements")
    if not isinstance(elements, dict):
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.elements",
            "elements must be an object",
        )
        return
    actual_by_name: dict[str, list[dict[str, Any]]] = {}
    for record in result.get("structure_objects", []):
        name = record.get("object_name")
        if isinstance(name, str) and name.startswith("ia:"):
            actual_by_name.setdefault(name, []).append(record)
    report_names: set[str] = set()
    for element_id in sorted(elements):
        element = elements[element_id]
        objects = element.get("objects") if isinstance(element, dict) else None
        if not isinstance(objects, list) or not objects:
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                f"build_report.elements.{element_id}.objects",
                "objects must be a non-empty array",
            )
            continue
        for index, expected in enumerate(objects):
            item_path = f"build_report.elements.{element_id}.objects[{index}]"
            if not isinstance(expected, dict):
                _report_error(
                    result, "BUILD_REPORT_MISMATCH", item_path, "object must be a mapping"
                )
                continue
            name = expected.get("ooxml_name")
            if not isinstance(name, str) or not name.startswith("ia:"):
                _report_error(
                    result,
                    "BUILD_REPORT_MISMATCH",
                    f"{item_path}.ooxml_name",
                    "registered object name is invalid",
                )
                continue
            if name in report_names:
                _report_error(
                    result,
                    "BUILD_REPORT_MISMATCH",
                    f"{item_path}.ooxml_name",
                    "registered object name is duplicated",
                )
            report_names.add(name)
            candidates = actual_by_name.get(name, [])
            if len(candidates) != 1:
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    item_path,
                    f"expected exactly one PPTX object named {name}",
                )
                continue
            actual = candidates[0]
            if expected.get("object_type") != actual.get("object_type"):
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"{item_path}.object_type",
                    "PPTX object type differs from build report",
                )
            expected_bbox = expected.get("bbox")
            actual_bbox = [actual.get(key) for key in ("x", "y", "cx", "cy")]
            if expected_bbox != actual_bbox:
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"{item_path}.bbox",
                    "PPTX object bbox differs from build report",
                )
            if not _number_matches(expected.get("rotation"), actual.get("rotation")):
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"{item_path}.rotation",
                    "PPTX object rotation differs from build report",
                )
            if expected.get("text_summary") != actual.get("text_summary"):
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"{item_path}.text_summary",
                    "PPTX object text differs from build report",
                )
            if expected.get("media_sha256") != actual.get("media_sha256"):
                _report_error(
                    result,
                    "ASSET_HASH_MISMATCH",
                    f"{item_path}.media_sha256",
                    "PPTX object media differs from build report",
                )
            expected_fonts = expected.get("font_declarations")
            if (
                isinstance(expected_fonts, list)
                and sorted(set(expected_fonts))
                != sorted(set(actual.get("font_declarations", [])))
            ):
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"{item_path}.font_declarations",
                    "PPTX object fonts differ from build report",
                )
    if set(actual_by_name) != report_names:
        _report_error(
            result,
            "BUILD_OUTPUT_INCOMPLETE",
            "build_report.elements",
            "registered report names and PPTX ia:* names differ",
        )


def _schema_elements(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    elements = spec.get("elements")
    if not isinstance(elements, list):
        return {}
    return {
        item["element_id"]: item
        for item in elements
        if isinstance(item, dict)
        and isinstance(item.get("element_id"), str)
        and item["element_id"]
    }


def _expected_objects_for_element(
    element: dict[str, Any], spec: dict[str, Any]
) -> list[dict[str, Any]]:
    element_id = element["element_id"]
    if element.get("kind") in {"matrix", "status"}:
        parts = expand_multipart_parts(element)
        return [
            {
                "ooxml_name": f"ia:{element_id}:{part.get('part_id')}",
                "part_id": part.get("part_id"),
                "bbox": part.get("slide_bbox"),
                "rotation": part.get("style", {}).get("rotation", 0),
                "text_summary": part.get("content", {}).get("text"),
                "font_declarations": (
                    [part["style"]["text_style"]["font_name"]]
                    if isinstance(part.get("content", {}).get("text"), str)
                    and isinstance(part.get("style"), dict)
                    and isinstance(part["style"].get("text_style"), dict)
                    and isinstance(part["style"]["text_style"].get("font_name"), str)
                    else []
                ),
            }
            for part in parts
        ]
    content = element.get("content")
    text_summary = None
    media_sha256 = None
    font_declarations: list[str] = []
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            text_summary = content["text"]
        if element.get("kind") == "table" and isinstance(content.get("cells"), list):
            text_summary = "\n".join(
                cell.get("text", "")
                for cell in content["cells"]
                if isinstance(cell, dict) and isinstance(cell.get("text"), str)
            )
            font_declarations = sorted(
                {
                    cell["font"]["name"]
                    for cell in content["cells"]
                    if isinstance(cell, dict)
                    and isinstance(cell.get("font"), dict)
                    and isinstance(cell["font"].get("name"), str)
                }
            )
        asset = content.get("asset")
        if isinstance(asset, dict) and isinstance(asset.get("asset_sha256"), str):
            media_sha256 = asset["asset_sha256"]
    if element.get("kind") in {"text", "special_text"}:
        typography = spec.get("modules", {}).get("typography", {})
        typography_items = typography.get("items", []) if isinstance(typography, dict) else []
        contract = next(
            (
                item
                for item in typography_items
                if isinstance(item, dict) and item.get("element_id") == element_id
            ),
            {},
        )
        font_name = contract.get("selected_font") if isinstance(contract, dict) else None
        if isinstance(font_name, str):
            font_declarations = [font_name]
    return [
        {
            "ooxml_name": f"ia:{element_id}",
            "part_id": None,
            "bbox": element.get("slide_bbox"),
            "rotation": element.get("style", {}).get("rotation", 0),
            "text_summary": text_summary,
            "media_sha256": media_sha256,
            "font_declarations": font_declarations,
        }
    ]


def _number_matches(left: Any, right: Any) -> bool:
    return (
        type(left) in {int, float}
        and type(right) in {int, float}
        and abs(float(left) - float(right)) <= 0.01
    )


def _validate_schema_report_contract(
    result: dict[str, Any], spec: dict[str, Any], report: dict[str, Any]
) -> None:
    schema_elements = _schema_elements(spec)
    report_elements = report.get("elements")
    if not isinstance(report_elements, dict):
        return
    if set(report_elements) != set(schema_elements):
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.elements",
            "report element ids differ from the reconstruction spec",
        )
    modes = resolved_element_mode_map(spec)
    actual_by_name: dict[str, list[dict[str, Any]]] = {}
    for record in result.get("structure_objects", []):
        name = record.get("object_name")
        if isinstance(name, str) and name.startswith("ia:"):
            actual_by_name.setdefault(name, []).append(record)
    verified_elements: dict[str, bool] = {}

    for element_id, element in schema_elements.items():
        report_element = report_elements.get(element_id)
        element_path = f"build_report.elements.{element_id}"
        if not isinstance(report_element, dict):
            _report_error(
                result, "BUILD_REPORT_MISMATCH", element_path, "element report is missing"
            )
            verified_elements[element_id] = False
            continue
        element_root_ok = True
        if report_element.get("semantic_kind") != element.get("kind"):
            element_root_ok = False
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                f"{element_path}.semantic_kind",
                "semantic kind differs from schema",
            )
        if report_element.get("selected_mode") != modes.get(element_id):
            element_root_ok = False
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                f"{element_path}.selected_mode",
                "representation mode differs from schema",
            )
        objects = report_element.get("objects")
        if not isinstance(objects, list):
            verified_elements[element_id] = False
            continue
        declared_types = {
            item.get("object_type")
            for item in objects
            if isinstance(item, dict) and isinstance(item.get("object_type"), str)
        }
        summarized_type = (
            next(iter(declared_types)) if len(declared_types) == 1 else "mixed"
        )
        if report_element.get("object_type") != summarized_type:
            element_root_ok = False
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                f"{element_path}.object_type",
                "aggregate object type does not summarize registered objects",
            )
        report_by_name = {
            item.get("ooxml_name"): item
            for item in objects
            if isinstance(item, dict) and isinstance(item.get("ooxml_name"), str)
        }
        try:
            expected_objects = _expected_objects_for_element(element, spec)
        except ToolError as exc:
            _report_error(result, "BUILD_REPORT_MISMATCH", exc.path, exc.detail)
            verified_elements[element_id] = False
            continue
        expected_names = {item["ooxml_name"] for item in expected_objects}
        names_match = (
            set(report_by_name) == expected_names
            and len(objects) == len(expected_objects)
        )
        if not names_match:
            element_root_ok = False
            _report_error(
                result,
                "BUILD_REPORT_MISMATCH",
                f"{element_path}.objects",
                "registered parts differ from schema expansion",
            )
        allowed_types = expected_object_types(element.get("kind"))
        actual_boxes: list[list[int]] = []
        verified_names: set[str] = set()
        for expected in expected_objects:
            name = expected["ooxml_name"]
            item = report_by_name.get(name)
            item_path = f"{element_path}.objects[{name}]"
            if not isinstance(item, dict):
                continue
            object_ok = element_root_ok
            if item.get("part_id") != expected.get("part_id"):
                object_ok = False
                _report_error(
                    result, "BUILD_REPORT_MISMATCH", f"{item_path}.part_id", "part id differs from schema"
                )
            if item.get("bbox") != expected.get("bbox"):
                object_ok = False
                _report_error(
                    result, "BUILD_REPORT_MISMATCH", f"{item_path}.bbox", "bbox differs from schema"
                )
            if item.get("object_type") not in allowed_types:
                object_ok = False
                _report_error(
                    result,
                    "BUILD_REPORT_MISMATCH",
                    f"{item_path}.object_type",
                    "object type is incompatible with schema kind",
                )
            if not _number_matches(item.get("rotation"), expected.get("rotation")):
                object_ok = False
                _report_error(
                    result,
                    "BUILD_REPORT_MISMATCH",
                    f"{item_path}.rotation",
                    "rotation differs from schema",
                )
            for field in ("text_summary", "media_sha256"):
                expected_value = expected.get(field)
                if item.get(field) != expected_value:
                    object_ok = False
                    _report_error(
                        result,
                        "BUILD_REPORT_MISMATCH",
                        f"{item_path}.{field}",
                        f"{field} differs from schema",
                    )
            if sorted(set(item.get("font_declarations", []))) != sorted(
                set(expected.get("font_declarations", []))
            ):
                object_ok = False
                _report_error(
                    result,
                    "BUILD_REPORT_MISMATCH",
                    f"{item_path}.font_declarations",
                    "font declarations differ from schema",
                )
            candidates = actual_by_name.get(name, [])
            if len(candidates) == 1:
                actual = candidates[0]
                box = [actual.get(key) for key in ("x", "y", "cx", "cy")]
                if all(type(value) is int for value in box):
                    actual_boxes.append(box)
                else:
                    object_ok = False
                actual_bbox = [actual.get(key) for key in ("x", "y", "cx", "cy")]
                if (
                    actual.get("object_type") != item.get("object_type")
                    or actual_bbox != item.get("bbox")
                    or not _number_matches(actual.get("rotation"), item.get("rotation"))
                    or actual.get("text_summary") != item.get("text_summary")
                    or actual.get("media_sha256") != item.get("media_sha256")
                    or sorted(set(actual.get("font_declarations", [])))
                    != sorted(set(item.get("font_declarations", [])))
                ):
                    object_ok = False
            else:
                object_ok = False
            if object_ok:
                result["build_report_objects_checked"] += 1
                verified_names.add(name)
        element_verified = (
            element_root_ok
            and verified_names == expected_names
            and len(expected_objects) == len(objects)
        )
        multipart_union_ok = not (
            len(actual_boxes) != len(expected_objects)
            or bbox_union(actual_boxes) != element.get("slide_bbox")
        )
        if element.get("kind") in {"matrix", "status"} and not multipart_union_ok:
            _report_error(
                result,
                "BUILD_OUTPUT_INCOMPLETE",
                element_path,
                "multipart PPTX objects do not union to the parent bbox",
            )
        if element.get("kind") in {"matrix", "status"}:
            if element_verified and multipart_union_ok:
                result["multipart_contracts_checked"] += 1
            else:
                element_verified = False
        verified_elements[element_id] = element_verified

    expected_summary = representation_summary(spec)
    if report.get("representation_summary") != expected_summary:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.representation_summary",
            "representation summary differs from schema",
        )
    background_module = spec.get("modules", {}).get("background", {})
    background_items = (
        background_module.get("items", [])
        if isinstance(background_module, dict)
        else []
    )
    expected_background_summary = {"native": 0, "background_picture": 0}
    expected_background_pictures: list[dict[str, Any]] = []
    for item in background_items if isinstance(background_items, list) else []:
        if not isinstance(item, dict):
            continue
        mode = item.get("selected_mode")
        if mode in expected_background_summary:
            expected_background_summary[mode] += 1
        if mode != "background_picture":
            continue
        element_id = item.get("bound_element_id")
        element = schema_elements.get(element_id)
        asset = (
            element.get("content", {}).get("asset")
            if isinstance(element, dict)
            else None
        )
        asset_sha256 = asset.get("asset_sha256") if isinstance(asset, dict) else None
        if isinstance(asset_sha256, str):
            expected_background_pictures.append(
                {**item, "media_sha256": asset_sha256}
            )
    expected_background_pictures.sort(
        key=lambda value: value.get("background_id", "")
    )
    if report.get("background_summary") != expected_background_summary:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.background_summary",
            "background summary differs from schema",
        )
    if report.get("background_pictures") != expected_background_pictures:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.background_pictures",
            "background picture facts or media hashes differ from schema",
        )
    modules = spec.get("modules")
    plan = modules.get("representation_plan") if isinstance(modules, dict) else None
    facts = plan.get("items") if isinstance(plan, dict) else None
    if not isinstance(facts, list):
        facts = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict) or fact.get("coverage_status") != "covered":
            continue
        fact_path = f"modules.representation_plan.items[{index}]"
        bindings = fact.get("bound_element_ids")
        fact_ok = True
        if not isinstance(bindings, list) or not bindings:
            fact_ok = False
            _report_error(
                result, "BUILD_OUTPUT_INCOMPLETE", f"{fact_path}.bound_element_ids", "covered fact has no bindings"
            )
            continue
        for element_id in bindings:
            prefix = f"ia:{element_id}"
            if not verified_elements.get(element_id, False):
                fact_ok = False
            if not any(
                name == prefix or name.startswith(f"{prefix}:")
                for name in actual_by_name
            ):
                fact_ok = False
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"{fact_path}.bound_element_ids",
                    f"bound PPTX object is missing: {element_id}",
                )
        if fact_ok:
            result["representation_facts_checked"] += 1

    expected_fallbacks = [
        dict(item)
        for item in sorted(facts, key=lambda value: value.get("source_fact_id", ""))
        if isinstance(item, dict) and item.get("selected_mode") == "asset"
    ]
    actual_fallbacks = report.get("asset_fallbacks")
    if actual_fallbacks != expected_fallbacks:
        _report_error(
            result,
            "BUILD_REPORT_MISMATCH",
            "build_report.asset_fallbacks",
            "asset fallback facts differ from schema",
        )
    if isinstance(actual_fallbacks, list):
        actual_fallback_by_fact = {
            item.get("source_fact_id"): item
            for item in actual_fallbacks
            if isinstance(item, dict)
            and isinstance(item.get("source_fact_id"), str)
        }
        for index, fallback in enumerate(expected_fallbacks):
            fallback_ok = (
                len(actual_fallbacks) == len(expected_fallbacks)
                and actual_fallback_by_fact.get(fallback.get("source_fact_id"))
                == fallback
            )
            bindings = fallback.get("bound_element_ids", [])
            bound_elements = [schema_elements.get(value) for value in bindings]
            if not all(
                verified_elements.get(element_id, False)
                for element_id in bindings
            ):
                fallback_ok = False
            picture_elements = [
                item for item in bound_elements
                if isinstance(item, dict) and item.get("kind") in {"picture", "icon"}
            ]
            if not picture_elements:
                fallback_ok = False
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"build_report.asset_fallbacks[{index}]",
                    "asset fallback lacks a picture/icon binding",
                )
                continue
            for element in picture_elements:
                element_id = element["element_id"]
                if not verified_elements.get(element_id, False):
                    fallback_ok = False
                if fallback.get("source_bbox") != element.get("source_bbox"):
                    fallback_ok = False
                    _report_error(
                        result,
                        "BUILD_REPORT_MISMATCH",
                        f"build_report.asset_fallbacks[{index}].source_bbox",
                        "asset fallback local bbox differs from its bound element",
                    )
                records = actual_by_name.get(f"ia:{element_id}", [])
                if len(records) != 1 or records[0].get("object_type") != "pic":
                    fallback_ok = False
                    _report_error(
                        result,
                        "BUILD_OUTPUT_INCOMPLETE",
                        f"build_report.asset_fallbacks[{index}].bound_element_ids",
                        "asset fallback is not bound to one PPTX picture",
                    )
            if fallback.get("required_editability") in {
                "labels_only",
                "labels_and_geometry",
            } and not any(
                isinstance(item, dict) and item.get("kind") in {"text", "special_text"}
                for item in bound_elements
            ):
                fallback_ok = False
                _report_error(
                    result,
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"build_report.asset_fallbacks[{index}].bound_element_ids",
                    "editable fallback labels are not bound",
                )
            if fallback_ok:
                result["asset_fallbacks_checked"] += 1


def _expected_native_list_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    modules = spec.get("modules")
    typography = modules.get("typography") if isinstance(modules, dict) else None
    items = typography.get("items") if isinstance(typography, dict) else None
    if not isinstance(items, list):
        return []
    return [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("paragraphs"), list)
        and any(
            isinstance(paragraph, dict)
            and isinstance(paragraph.get("list"), dict)
            and paragraph["list"].get("is_list") is True
            for paragraph in item["paragraphs"]
        )
    ]


def _expected_text_run_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    modules = spec.get("modules")
    typography = modules.get("typography") if isinstance(modules, dict) else None
    items = typography.get("items") if isinstance(typography, dict) else None
    if not isinstance(items, list):
        return []
    elements = spec.get("elements")
    kinds = {
        element.get("element_id"): element.get("kind")
        for element in elements
        if isinstance(element, dict) and isinstance(element.get("element_id"), str)
    } if isinstance(elements, list) else {}
    return [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("runs"), list)
        and bool(item["runs"])
        and (not kinds or kinds.get(item.get("element_id")) == "text")
    ]


def _text_box_matches(
    actual: dict[str, Any],
    expected: Any,
    width: int,
    height: int,
) -> bool:
    if not isinstance(expected, dict):
        return False
    actual_values = [actual.get(key) for key in ("x", "y", "cx", "cy")]
    expected_values = [expected.get(key) for key in ("x", "y", "w", "h")]
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in actual_values + expected_values):
        return False
    scales = [width, height, width, height]
    return all(abs(left - right) / scale <= 0.01 for left, right, scale in zip(actual_values, expected_values, scales))


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 0.01
    return left == right


def _run_ranges_valid(length: int, runs: Any) -> bool:
    if not isinstance(runs, list) or not runs:
        return False
    covered = [False] * length
    for run in runs:
        if not isinstance(run, dict):
            return False
        start, end = run.get("start"), run.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > length
        ):
            return False
        for offset in range(start, end):
            if covered[offset]:
                return False
            covered[offset] = True
    return bool(covered) and all(covered)


def _normalized_run_value(run: dict[str, Any], property_name: str) -> Any:
    value = run.get(property_name)
    if property_name == "font_size":
        return (
            round(float(value) * 100)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )
    if property_name == "font_weight":
        return (
            float(value) >= 600
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )
    if property_name == "color":
        return value.upper() if isinstance(value, str) else None
    if property_name in {"italic", "underline", "strike"}:
        return value if isinstance(value, bool) else False
    if property_name == "baseline":
        return value if isinstance(value, int) and not isinstance(value, bool) else 0
    if property_name == "letter_spacing":
        return (
            round(float(value) * 100)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else 0
        )
    raise ValueError(f"unsupported run property: {property_name}")


def _style_vector(
    length: int,
    runs: Any,
    property_name: str,
) -> list[Any] | None:
    if not _run_ranges_valid(length, runs):
        return None
    values: list[Any] = [None] * length
    for run in runs:
        normalized = _normalized_run_value(run, property_name)
        for offset in range(run["start"], run["end"]):
            values[offset] = normalized
    return values


def _display_run_value(property_name: str, value: Any) -> Any:
    if property_name in {"font_size", "letter_spacing"} and isinstance(value, int):
        return value / 100
    if property_name == "font_weight" and isinstance(value, bool):
        return 700 if value else 400
    return value


def _append_style_mismatches(
    result: dict[str, Any],
    element_id: Any,
    property_name: str,
    expected: list[Any],
    actual: list[Any],
) -> None:
    offset = 0
    while offset < len(expected):
        if expected[offset] == actual[offset]:
            offset += 1
            continue
        start = offset
        expected_value = expected[offset]
        actual_value = actual[offset]
        offset += 1
        while (
            offset < len(expected)
            and expected[offset] != actual[offset]
            and expected[offset] == expected_value
            and actual[offset] == actual_value
        ):
            offset += 1
        mismatch = {
            "code": "TEXT_RUN_STYLE_MISMATCH",
            "element_id": element_id,
            "start": start,
            "end": offset,
            "property": property_name,
            "expected": _display_run_value(property_name, expected_value),
            "actual": _display_run_value(property_name, actual_value),
        }
        result["text_run_style_mismatches"].append(mismatch)
        result["text_run_style_mismatches_by_property"][property_name] += 1
        result["warnings"].append(
            f"{element_id}[{start}:{offset}] {property_name}: "
            f"expected {mismatch['expected']!r}, got {mismatch['actual']!r}"
        )


def _validate_text_run_contracts(
    result: dict[str, Any],
    spec: dict[str, Any],
    width: int,
    height: int,
) -> None:
    used_objects: set[tuple[Any, Any]] = set()
    for item in _expected_text_run_items(spec):
        element_id = item.get("element_id", "unknown")
        candidates = [
            text_object
            for text_object in result.get("text_objects", [])
            if _text_box_matches(text_object, item.get("text_box"), width, height)
            and (
                text_object.get("object_name") == f"ia:{element_id}"
                or str(text_object.get("object_name", "")).startswith(f"ia:{element_id}:")
            )
        ]
        available = [
            candidate for candidate in candidates
            if (candidate.get("slide_part"), candidate.get("object_id")) not in used_objects
        ]
        if not available:
            result["errors"].append("TYPOGRAPHY_TEXTBOX_MISSING")
            result["warnings"].append(f"{element_id}: expected TextBox for typography run validation")
            continue
        if len(available) > 1:
            result["errors"].append("TYPOGRAPHY_TEXTBOX_AMBIGUOUS")
            result["warnings"].append(f"{element_id}: multiple matching TextBox objects")
            continue
        actual_object = available[0]
        used_objects.add((actual_object.get("slide_part"), actual_object.get("object_id")))
        result["text_run_contracts_checked"] += 1
        expected_text_box = item.get("text_box")
        actual_text_box = actual_object.get("text_box")
        expected_vertical = (
            expected_text_box.get("vertical_alignment")
            if isinstance(expected_text_box, dict)
            else None
        )
        actual_vertical = (
            actual_text_box.get("vertical_alignment")
            if isinstance(actual_text_box, dict)
            else None
        )
        if expected_vertical != actual_vertical:
            result["errors"].append("TEXT_BOX_VERTICAL_ALIGNMENT_MISMATCH")
            result["warnings"].append(
                f"{element_id}: vertical alignment expected {expected_vertical!r}, "
                f"got {actual_vertical!r}"
            )
        expected_overflow = (
            expected_text_box.get("overflow")
            if isinstance(expected_text_box, dict)
            else None
        )
        actual_horizontal_overflow = (
            actual_text_box.get("horizontal_overflow")
            if isinstance(actual_text_box, dict)
            else None
        )
        actual_vertical_overflow = (
            actual_text_box.get("vertical_overflow")
            if isinstance(actual_text_box, dict)
            else None
        )
        if (
            expected_overflow is not True
            or actual_horizontal_overflow != "overflow"
            or actual_vertical_overflow != "overflow"
        ):
            result["errors"].append("TEXT_BOX_OVERFLOW_MISMATCH")
            result["warnings"].append(
                f"{element_id}: expected explicit horizontal and vertical overflow, "
                f"got horzOverflow={actual_horizontal_overflow!r}, "
                f"vertOverflow={actual_vertical_overflow!r}"
            )
        text = item.get("text")
        if not isinstance(text, str):
            continue
        if actual_object.get("text") != text:
            result["errors"].append("TEXT_RUN_TEXT_MISMATCH")
            result["warnings"].append(
                f"{element_id}: TextBox text differs from the typography contract"
            )
            continue
        expected_runs = item.get("runs")
        actual_runs = actual_object.get("runs")
        if not _run_ranges_valid(len(text), expected_runs) or not _run_ranges_valid(
            len(text), actual_runs
        ):
            result["errors"].append("TEXT_RUN_STRUCTURE_INVALID")
            result["warnings"].append(
                f"{element_id}: Text Run ranges overlap, have gaps, or do not cover the text"
            )
            continue
        compared_properties = [
            property_name
            for property_name in TEXT_RUN_STYLE_PROPERTIES
            if all(
                isinstance(run, dict) and property_name in run
                for run in expected_runs
            )
        ]
        for property_name in compared_properties:
            expected_vector = _style_vector(len(text), expected_runs, property_name)
            actual_vector = _style_vector(len(text), actual_runs, property_name)
            if expected_vector is None or actual_vector is None:
                result["errors"].append("TEXT_RUN_STRUCTURE_INVALID")
                continue
            _append_style_mismatches(
                result,
                element_id,
                property_name,
                expected_vector,
                actual_vector,
            )
    result["text_run_style_mismatch_count"] = len(
        result["text_run_style_mismatches"]
    )


def _validate_native_list_contracts(
    result: dict[str, Any],
    spec: dict[str, Any],
    width: int,
    height: int,
) -> None:
    used_objects: set[tuple[Any, Any]] = set()
    for item in _expected_native_list_items(spec):
        result["native_list_contracts_checked"] += 1
        element_id = item.get("element_id", "unknown")
        candidates = [
            text_object
            for text_object in result.get("text_objects", [])
            if text_object.get("text") == item.get("text")
            and _text_box_matches(text_object, item.get("text_box"), width, height)
            and (
                text_object.get("object_name") == f"ia:{element_id}"
                or str(text_object.get("object_name", "")).startswith(f"ia:{element_id}:")
            )
        ]
        available = [
            candidate for candidate in candidates
            if (candidate.get("slide_part"), candidate.get("object_id")) not in used_objects
        ]
        if not available:
            result["errors"].append("NATIVE_LIST_TEXTBOX_MISSING")
            result["warnings"].append(
                f"{element_id}: expected one TextBox containing the complete native list"
            )
            continue
        if len(available) > 1:
            result["errors"].append("NATIVE_LIST_TEXTBOX_AMBIGUOUS")
            result["warnings"].append(f"{element_id}: multiple matching TextBox objects")
            continue
        actual_object = available[0]
        used_objects.add((actual_object.get("slide_part"), actual_object.get("object_id")))
        expected_paragraphs = item.get("paragraphs")
        actual_paragraphs = actual_object.get("paragraphs")
        if not isinstance(expected_paragraphs, list) or not isinstance(actual_paragraphs, list):
            result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
            continue
        if len(expected_paragraphs) != len(actual_paragraphs):
            result["errors"].append("NATIVE_LIST_PARAGRAPH_COUNT_MISMATCH")
            result["warnings"].append(
                f"{element_id}: expected {len(expected_paragraphs)} paragraphs, got {len(actual_paragraphs)}"
            )
            continue
        for index, (expected, actual) in enumerate(zip(expected_paragraphs, actual_paragraphs)):
            if not isinstance(expected, dict) or not isinstance(actual, dict):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                continue
            if (expected.get("start"), expected.get("end")) != (actual.get("start"), actual.get("end")):
                result["errors"].append("NATIVE_LIST_PARAGRAPH_RANGE_MISMATCH")
            expected_list = expected.get("list")
            actual_list = actual.get("list")
            if not isinstance(expected_list, dict) or not isinstance(actual_list, dict):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                continue
            if expected_list.get("is_list") != actual_list.get("is_list"):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                continue
            if expected_list.get("is_list") is not True:
                continue
            if any(
                not _same_value(expected_list.get(key), actual_list.get(key))
                for key in ("level", "bullet_type", "bullet")
            ):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                result["warnings"].append(f"{element_id} paragraph {index}: bullet identity mismatch")
            if any(
                not _same_value(expected.get(key), actual.get(key))
                for key in ("margin_left", "indent")
            ):
                result["errors"].append("NATIVE_LIST_INDENT_MISMATCH")
                result["warnings"].append(f"{element_id} paragraph {index}: list indentation mismatch")
            if any(
                not _same_value(expected_list.get(key), actual_list.get(key))
                for key in (
                    "bullet_font",
                    "bullet_size_mode",
                    "bullet_size_value",
                    "bullet_color",
                )
            ):
                result["errors"].append("NATIVE_LIST_STYLE_MISMATCH")
                result["warnings"].append(f"{element_id} paragraph {index}: bullet style mismatch")
            if expected_list.get("bullet_type") == "picture":
                if (
                    actual_list.get("bullet_relationship_valid") is not True
                    or not isinstance(actual_list.get("bullet_media_sha256"), str)
                ):
                    result["errors"].append(
                        "NATIVE_LIST_PICTURE_RELATIONSHIP_INVALID"
                    )
                    result["warnings"].append(
                        f"{element_id} paragraph {index}: picture bullet relationship is not an internal image"
                    )
                else:
                    asset = expected_list.get("bullet_asset")
                    expected_hash = (
                        asset.get("asset_sha256") if isinstance(asset, dict) else None
                    )
                    if not _same_value(
                        expected_hash, actual_list.get("bullet_media_sha256")
                    ):
                        result["errors"].append(
                            "NATIVE_LIST_PICTURE_MEDIA_HASH_MISMATCH"
                        )
                        result["warnings"].append(
                            f"{element_id} paragraph {index}: picture bullet media hash differs from the spec"
                        )


def _slide_inheritance(
    archive: zipfile.ZipFile,
    names: set[str],
    slide_relationships: dict[str, tuple[str, str, bool]],
) -> tuple[dict[str, Any], dict[str, str]]:
    layout_part = next((target for target, kind, external in slide_relationships.values()
                        if not external and kind.endswith("/slideLayout")), None)
    if not layout_part or layout_part not in names:
        return {}, {}
    layout = _xml(archive, layout_part)
    layout_rels_part = _slide_rels_part(layout_part)
    layout_rels = _relationship_map(archive, layout_rels_part) if layout_rels_part in names else {}
    master_part = next((target for target, kind, external in layout_rels.values()
                        if not external and kind.endswith("/slideMaster")), None)
    if not master_part or master_part not in names:
        return {"layout": layout}, {}
    master = _xml(archive, master_part)
    theme_fonts: dict[str, str] = {}
    master_rels_part = _slide_rels_part(master_part)
    master_rels = _relationship_map(archive, master_rels_part) if master_rels_part in names else {}
    theme_part = next((target for target, kind, external in master_rels.values()
                       if not external and kind.endswith("/theme")), None)
    if theme_part and theme_part in names:
        theme = _xml(archive, theme_part)
        for prefix, path in (("+mj", "a:themeElements/a:fontScheme/a:majorFont"),
                             ("+mn", "a:themeElements/a:fontScheme/a:minorFont")):
            family = theme.find(path, NS)
            if family is None:
                continue
            for suffix, tag in (("lt", "latin"), ("ea", "ea"), ("cs", "cs")):
                node = family.find(f"a:{tag}", NS)
                if node is not None and node.get("typeface"):
                    theme_fonts[f"{prefix}-{suffix}"] = node.get("typeface", "")
    return {"layout": layout, "master": master}, theme_fonts


def _placeholder_identity(shape: ET.Element) -> tuple[str | None, str | None]:
    placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
    return (
        placeholder.get("type") if placeholder is not None else None,
        placeholder.get("idx") if placeholder is not None else None,
    )


def _placeholder_shape(
    root: ET.Element | None, kind: str | None, idx: str | None
) -> ET.Element | None:
    if root is None:
        return None
    candidates: list[tuple[int, ET.Element]] = []
    for shape in root.findall("p:cSld/p:spTree/p:sp", NS):
        other_kind, other_idx = _placeholder_identity(shape)
        if idx is not None and other_idx == idx:
            candidates.append((2, shape))
        elif kind is not None and other_kind == kind:
            candidates.append((1, shape))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _inherited_placeholder_geometry(
    shape: ET.Element, inheritance: dict[str, Any]
) -> tuple[int | None, int | None, int | None, int | None]:
    kind, idx = _placeholder_identity(shape)
    if kind is None and idx is None:
        return None, None, None, None
    for root_name in ("layout", "master"):
        matched = _placeholder_shape(inheritance.get(root_name), kind, idx)
        if matched is None:
            continue
        bbox = _geometry(matched, "p:spPr/a:xfrm")
        if None not in bbox:
            return bbox
    return None, None, None, None


def _inherited_body_properties(
    shape: ET.Element, inheritance: dict[str, Any]
) -> list[ET.Element | None]:
    kind, idx = _placeholder_identity(shape)
    properties: list[ET.Element | None] = []
    for root_name in ("layout", "master"):
        matched = _placeholder_shape(inheritance.get(root_name), kind, idx)
        body = matched.find("p:txBody/a:bodyPr", NS) if matched is not None else None
        properties.append(body)
    return properties


def _placeholder_level(root: ET.Element | None, kind: str | None, idx: str | None, level: int) -> ET.Element | None:
    shape = _placeholder_shape(root, kind, idx)
    return (
        shape.find(f"p:txBody/a:lstStyle/a:lvl{level + 1}pPr", NS)
        if shape is not None else None
    )


def _inherited_paragraph_properties(
    shape: ET.Element, level: int, inheritance: dict[str, Any]
) -> list[ET.Element | None]:
    kind, idx = _placeholder_identity(shape)
    properties: list[ET.Element | None] = []
    for root_name in ("layout", "master"):
        properties.append(
            _placeholder_level(inheritance.get(root_name), kind, idx, level)
        )
    master = inheritance.get("master")
    if master is None:
        return properties
    style = "titleStyle" if kind in {"title", "ctrTitle"} else "bodyStyle" if kind in {"body", "obj", "subTitle"} else "otherStyle"
    properties.append(master.find(f"p:txStyles/p:{style}/a:lvl{level + 1}pPr", NS))
    return properties


def _check_relationship_targets(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    missing: list[str] = []
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        try:
            relationships = _relationship_map(archive, rels_part)
        except ValidationError as exc:
            missing.append(f"{exc.code}:{rels_part}:{exc.detail}")
            continue
        for relationship_id, (target, _kind, external) in relationships.items():
            if not external and target not in names:
                missing.append(f"{rels_part}#{relationship_id}->{target}")
    return missing


def _audit_relationships(
    archive: zipfile.ZipFile, names: set[str], result: dict[str, Any]
) -> None:
    missing: list[str] = []
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        try:
            relationships = _relationship_map(archive, rels_part)
        except ValidationError as exc:
            result["errors"].append(
                "RELATIONSHIPS_XML_INVALID" if exc.code == "XML_INVALID" else exc.code
            )
            result["warnings"].append(f"{rels_part}: {exc.detail}")
            continue
        for relationship_id, (target, kind, external) in relationships.items():
            if external:
                result["external_relationships"].append({
                    "source_rels_part": rels_part,
                    "relationship_id": relationship_id,
                    "relationship_type": kind,
                    "target": target,
                })
                result["errors"].append("EXTERNAL_RELATIONSHIP_FORBIDDEN")
            elif target not in names:
                missing.append(f"{rels_part}#{relationship_id}->{target}")
    if missing:
        result["errors"].append("MISSING_RELATIONSHIP_TARGET")
        result["warnings"].extend(missing)


def _validate_archive_inventory(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValidationError("PPTX_RESOURCE_LIMIT", "ZIP member count exceeds limit")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if name in seen:
            raise ValidationError("DUPLICATE_ZIP_PART", f"Duplicate ZIP part: {name}")
        seen.add(name)
        if (
            not name or "\\" in name or name.startswith("/")
            or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
        ):
            raise ValidationError("ZIP_PART_NAME_INVALID", f"Invalid ZIP part name: {name}")
        if info.file_size > MAX_MEMBER_UNCOMPRESSED:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"ZIP member too large: {name}")
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) and info.file_size > MAX_MEDIA_BYTES:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"Media member too large: {name}")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise ValidationError("PPTX_RESOURCE_LIMIT", "Total expanded package size exceeds limit")
        if info.file_size and info.compress_size == 0:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"Invalid compression size: {name}")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"Compression ratio exceeds limit: {name}")


def _content_type_maps(archive: zipfile.ZipFile):
    root = _xml(archive, "[Content_Types].xml")
    content_ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    content_uri = content_ns["ct"]
    if root.tag != f"{{{content_uri}}}Types":
        raise ValidationError("CONTENT_TYPES_INVALID", "Unexpected Types root QName")
    allowed = {f"{{{content_uri}}}Override", f"{{{content_uri}}}Default"}
    if any(child.tag not in allowed for child in list(root)):
        raise ValidationError("CONTENT_TYPES_INVALID", "Unknown direct child in [Content_Types].xml")
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    mime_pattern = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:\s*;.*)?$")
    for item in root.findall("ct:Override", content_ns):
        raw = item.get("PartName", "")
        content_type = item.get("ContentType", "")
        decoded = unquote(raw)
        part = raw.lstrip("/")
        if (
            not raw.startswith("/") or raw.startswith("//") or "\\" in raw
            or decoded != raw or any(segment in {"", ".", ".."} for segment in PurePosixPath(part).parts)
            or part in overrides or not mime_pattern.match(content_type)
        ):
            raise ValidationError("CONTENT_TYPES_INVALID", f"Invalid content type Override: {raw}")
        overrides[part] = content_type
    for item in root.findall("ct:Default", content_ns):
        extension = item.get("Extension", "").lower()
        content_type = item.get("ContentType", "")
        if (
            not extension or extension.startswith(".") or "/" in extension or "\\" in extension
            or extension in defaults or not mime_pattern.match(content_type)
        ):
            raise ValidationError("CONTENT_TYPES_INVALID", f"Invalid content type Default: {extension}")
        defaults[extension] = content_type
    return overrides, defaults


def _validate_image_payload(archive: zipfile.ZipFile, part: str) -> None:
    info = archive.getinfo(part)
    if info.file_size > MAX_MEDIA_BYTES:
        raise ValidationError("PPTX_RESOURCE_LIMIT", f"Image part exceeds byte budget: {part}")
    try:
        payload = archive.read(info)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                pixels = width * height
                if (
                    width <= 0 or height <= 0
                    or width > MAX_MEDIA_DIMENSION or height > MAX_MEDIA_DIMENSION
                    or pixels > MAX_MEDIA_PIXELS or pixels * 4 > MAX_MEDIA_RGBA_BYTES
                ):
                    raise ValidationError("PPTX_RESOURCE_LIMIT", f"Image pixel budget exceeded: {part}")
                image.verify()
    except ValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError("PPTX_RESOURCE_LIMIT", f"Image decompression bomb: {part}") from exc
    except Exception as exc:
        raise ValidationError("MEDIA_IMAGE_INVALID", f"Invalid image part: {part}") from exc


def _validate_image_parts(
    archive: zipfile.ZipFile,
    names: set[str],
    overrides: dict[str, str],
    defaults: dict[str, str],
) -> None:
    image_parts = {
        part for part in names
        if (overrides.get(part) or defaults.get(PurePosixPath(part).suffix.lstrip(".").lower()) or "")
        .startswith("image/")
    }
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        for target, relation_type, external in _relationship_map(archive, rels_part).values():
            if not external and relation_type.endswith("/image"):
                image_parts.add(target)
    for part in sorted(image_parts):
        if part not in names:
            raise ValidationError("MISSING_RELATIONSHIP_TARGET", f"Missing image part: {part}")
        content_type = overrides.get(part) or defaults.get(
            PurePosixPath(part).suffix.lstrip(".").lower()
        )
        if not (content_type or "").startswith("image/"):
            raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", f"Image part lacks image content type: {part}")
        _validate_image_payload(archive, part)


def _validate_content_type_coverage(
    archive: zipfile.ZipFile,
    names: set[str],
    overrides: dict[str, str],
    defaults: dict[str, str],
) -> None:
    for part in overrides:
        if part not in names:
            raise ValidationError("CONTENT_TYPES_INVALID", f"Override targets missing part: {part}")
    for part in names:
        if part == "[Content_Types].xml" or part.endswith(".rels"):
            continue
        extension = PurePosixPath(part).suffix.lstrip(".").lower()
        if part.startswith("ppt/slides/") and part.endswith(".xml") and part not in overrides:
            raise ValidationError("CONTENT_TYPE_MISSING", f"Slide requires Override: {part}")
        content_type = overrides.get(part) or defaults.get(extension)
        if not content_type:
            raise ValidationError("CONTENT_TYPE_MISSING", f"No content type for part: {part}")
        if part == "ppt/presentation.xml" and "presentation.main+xml" not in content_type:
            raise ValidationError("CONTENT_TYPES_INVALID", "Presentation content type role mismatch")
        if part.startswith("ppt/slides/") and part.endswith(".xml") and content_type != SLIDE_CONTENT_TYPE:
            raise ValidationError("CONTENT_TYPES_INVALID", f"Slide content type role mismatch: {part}")
        if part.startswith("ppt/media/") and not content_type.startswith("image/"):
            raise ValidationError("CONTENT_TYPES_INVALID", f"Media content type role mismatch: {part}")


def _validate_critical_relationship_roles(
    archive: zipfile.ZipFile,
    names: set[str],
    overrides: dict[str, str],
    defaults: dict[str, str],
) -> None:
    root = _relationship_map(archive, "_rels/.rels")
    office = [
        relation for relation in root.values()
        if relation[1].endswith("/officeDocument") and not relation[2]
    ]
    if len(office) != 1 or office[0][0] != "ppt/presentation.xml":
        raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", "Root must target one presentation officeDocument")
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        for target, relation_type, external in _relationship_map(archive, rels_part).values():
            if external:
                continue
            extension = PurePosixPath(target).suffix.lstrip(".").lower()
            content_type = overrides.get(target) or defaults.get(extension)
            expected_fragment = None
            if relation_type.endswith("/slide"):
                expected_fragment = ".slide+xml"
            elif relation_type.endswith("/slideLayout"):
                expected_fragment = ".slideLayout+xml"
            elif relation_type.endswith("/slideMaster"):
                expected_fragment = ".slideMaster+xml"
            elif relation_type.endswith("/theme"):
                expected_fragment = "officedocument.theme+xml"
            elif relation_type.endswith("/image"):
                if not (content_type or "").startswith("image/"):
                    raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", f"Image relationship targets non-image: {target}")
            if expected_fragment and expected_fragment not in (content_type or ""):
                raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", f"Relationship role mismatch: {target}")


def _validate_slide_object_ids(slide: ET.Element) -> None:
    object_paths = (
        (".//p:sp", "p:nvSpPr/p:cNvPr"),
        (".//p:pic", "p:nvPicPr/p:cNvPr"),
        (".//p:graphicFrame", "p:nvGraphicFramePr/p:cNvPr"),
        (".//p:grpSp", "p:nvGrpSpPr/p:cNvPr"),
        (".//p:cxnSp", "p:nvCxnSpPr/p:cNvPr"),
    )
    seen: set[int] = set()
    root_group = slide.find("p:cSld/p:spTree/p:nvGrpSpPr/p:cNvPr", NS)
    nodes: list[ET.Element | None] = [root_group]
    for object_path, identity_path in object_paths:
        for obj in slide.findall(object_path, NS):
            nodes.append(obj.find(identity_path, NS))
    for node in nodes:
        if node is None:
            raise ValidationError("SLIDE_OBJECT_ID_INVALID", "Visible object lacks cNvPr identity")
        raw = node.get("id")
        try:
            value = int(raw or "")
        except ValueError as exc:
            raise ValidationError("SLIDE_OBJECT_ID_INVALID", f"Invalid cNvPr id: {raw!r}") from exc
        if value <= 0 or value in seen:
            raise ValidationError("SLIDE_OBJECT_ID_INVALID", f"Duplicate/invalid cNvPr id: {value}")
        seen.add(value)


def _xml_relationship_ids(root: ET.Element) -> set[str]:
    values: set[str] = set()
    prefix = f"{{{NS['r']}}}"
    for element in root.iter():
        for attribute, value in element.attrib.items():
            if attribute.startswith(prefix) and value:
                values.add(value)
    return values


def validate_pptx(
    path: Path,
    expected_slides: int | None = None,
    reconstruction_spec: dict[str, Any] | Path | str | None = None,
    build_report: dict[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    result = _result(path)
    if not path.is_file():
        result["errors"].append("PPTX_NOT_FOUND")
        return result
    try:
        result["pptx_sha256"] = _file_sha256(path)
    except OSError:
        result["errors"].append("PPTX_ZIP_INVALID")
        return result
    spec = None
    if reconstruction_spec is not None:
        try:
            spec = _load_reconstruction_spec(reconstruction_spec)
        except ValidationError as exc:
            result["errors"].append(exc.code)
            result["warnings"].append(exc.detail)
            return result
    report = None
    if build_report is not None:
        try:
            report = _load_build_report(build_report)
            _validate_build_report_shape(report)
            _validate_report_identities(result, spec, report)
        except ValidationError as exc:
            result["errors"].append(exc.code)
            result["warnings"].append(exc.detail)
            return result

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            try:
                _validate_archive_inventory(archive)
            except ValidationError as exc:
                result["errors"].append(exc.code)
                result["warnings"].append(exc.detail)
                return result
            if report is not None:
                _validate_report_media_inventory(archive, names, result, report)
            bad_member = archive.testzip()
            if bad_member:
                result["errors"].append("PPTX_ZIP_CORRUPT")
                result["warnings"].append(f"Corrupt member: {bad_member}")
                return result
            missing_parts = sorted(REQUIRED_PARTS - names)
            if missing_parts:
                result["errors"].append("PPTX_REQUIRED_PART_MISSING")
                result["warnings"].append("Missing: " + ", ".join(missing_parts))
                return result

            _audit_relationships(archive, names, result)

            try:
                content_overrides, content_defaults = _content_type_maps(archive)
                _validate_content_type_coverage(
                    archive, names, content_overrides, content_defaults
                )
                _validate_critical_relationship_roles(
                    archive, names, content_overrides, content_defaults
                )
                _validate_image_parts(
                    archive, names, content_overrides, content_defaults
                )
            except ValidationError as exc:
                result["errors"].append(exc.code)
                result["warnings"].append(exc.detail)
                return result

            try:
                presentation = _xml(archive, "ppt/presentation.xml")
                presentation_rels = _relationship_map(
                    archive, "ppt/_rels/presentation.xml.rels"
                )
            except ValidationError as exc:
                result["errors"].append(
                    "RELATIONSHIPS_XML_INVALID"
                    if exc.code == "XML_INVALID"
                    else "PRESENTATION_XML_INVALID"
                )
                result["warnings"].append(exc.detail)
                return result

            size = presentation.find("p:sldSz", NS)
            width = _int_attr(size, "cx")
            height = _int_attr(size, "cy")
            result["width_emu"] = width
            result["height_emu"] = height
            if not width or not height:
                result["errors"].append("SLIDE_SIZE_MISSING")
                return result
            ratio = width / height
            result["aspect_ratio"] = ratio
            if abs(ratio - (16 / 9)) > 0.002:
                result["errors"].append("ASPECT_RATIO_NOT_16_9")

            slide_ids = presentation.findall("p:sldIdLst/p:sldId", NS)
            result["slide_count"] = len(slide_ids)
            if expected_slides is not None and len(slide_ids) != expected_slides:
                result["errors"].append("SLIDE_COUNT_MISMATCH")

            any_full_slide_picture = False
            spec_element_ids = {
                item.get("element_id")
                for item in spec.get("elements", [])
                if isinstance(item, dict) and isinstance(item.get("element_id"), str)
            } if isinstance(spec, dict) and isinstance(spec.get("elements"), list) else set()
            fonts: set[str] = set()
            font_sizes: set[float] = set()
            for position, slide_id in enumerate(slide_ids, start=1):
                rid = slide_id.get(RID)
                relationship = presentation_rels.get(rid or "")
                if not relationship or relationship[2] or not relationship[1].endswith("/slide"):
                    result["errors"].append("SLIDE_RELATIONSHIP_INVALID")
                    continue
                slide_part = relationship[0]
                if slide_part not in names:
                    result["errors"].append("SLIDE_PART_MISSING")
                    continue
                if content_overrides.get(slide_part) != SLIDE_CONTENT_TYPE:
                    result["errors"].append("CONTENT_TYPE_MISSING")
                    result["warnings"].append(
                        f"Slide part lacks the required content type override: {slide_part}"
                    )
                try:
                    slide = _xml(archive, slide_part)
                    _validate_slide_object_ids(slide)
                except ValidationError as exc:
                    result["errors"].append(
                        exc.code if exc.code == "SLIDE_OBJECT_ID_INVALID" else "SLIDE_XML_INVALID"
                    )
                    result["warnings"].append(exc.detail)
                    continue

                rels_part = _slide_rels_part(slide_part)
                try:
                    slide_relationships = (
                        _relationship_map(archive, rels_part) if rels_part in names else {}
                    )
                except ValidationError as exc:
                    result["errors"].append(
                        "RELATIONSHIPS_XML_INVALID" if exc.code == "XML_INVALID" else exc.code
                    )
                    result["warnings"].append(f"{rels_part}: {exc.detail}")
                    slide_relationships = {}
                try:
                    inheritance, theme_fonts = _slide_inheritance(
                        archive, names, slide_relationships
                    )
                except ValidationError as exc:
                    result["errors"].append(exc.code)
                    result["warnings"].append(exc.detail)
                    inheritance, theme_fonts = {}, {}
                sp_tree = slide.find("p:cSld/p:spTree", NS)
                object_records = _collect_visible_objects(
                    list(sp_tree) if sp_tree is not None else [], slide_part, width, height,
                    inheritance,
                )
                shapes = [item for item in object_records if item["object_type"] == "sp"]
                pictures = [item for item in object_records if item["object_type"] == "pic"]
                graphic_frames = [item for item in object_records if item["object_type"] == "graphicFrame"]
                for frame_record in graphic_frames:
                    frame = frame_record["_element"]
                    if frame.find("a:graphic/a:graphicData/c:chart", NS) is None:
                        continue
                    try:
                        result["native_chart_objects"].append(
                            _native_chart_contract(
                                archive,
                                names,
                                slide_part,
                                frame,
                                slide_relationships,
                            )
                        )
                    except ValidationError as exc:
                        result["errors"].append(exc.code)
                        result["warnings"].append(exc.detail)
                text_shapes = sum(
                    1 for item in shapes if item["has_text"] and item["visible"] is True
                )
                font_properties = _font_properties(slide)
                fonts.update(_declared_fonts(font_properties))
                font_sizes.update(_declared_font_sizes(font_properties))
                slide_text_runs = len(slide.findall(".//a:r", NS)) + len(
                    slide.findall(".//a:fld", NS)
                )
                meaningful_editable = [
                    item
                    for item in object_records
                    if item["object_type"] in {"sp", "graphicFrame", "cxnSp"}
                    and item["visible"] is True
                    and item["geometry_known"] is True
                ]
                if spec_element_ids:
                    meaningful_editable = [
                        item
                        for item in meaningful_editable
                        if _bound_element_id(item.get("object_name"), spec_element_ids)
                        is not None
                    ]
                editable = len(meaningful_editable)
                full_pictures = sum(
                    1 for picture in pictures
                    if picture["geometry_known"] and picture["visible"] is True
                    and is_near_full_page_bbox(
                        [
                            picture["x"],
                            picture["y"],
                            picture["cx"],
                            picture["cy"],
                        ],
                        [0, 0, width, height],
                    )
                )
                slide_picture_objects: list[dict[str, Any]] = []
                slide_text_objects: list[dict[str, Any]] = []
                for shape_record in shapes:
                    shape = shape_record["_element"]
                    native = {
                        key: shape_record[key] for key in (
                            "slide_part", "object_id", "object_name", "layer", "hidden",
                            "x", "y", "cx", "cy", "has_text", "geometry_known", "visible",
                        )
                    }
                    round_rect_status, round_rect_adjustment = _round_rect_adjustment(shape)
                    if round_rect_status is not None:
                        native["preset_geometry"] = "roundRect"
                        native["corner_adjustment"] = round_rect_adjustment
                    if round_rect_status == "missing":
                        result["errors"].append("ROUND_RECT_ADJUSTMENT_MISSING")
                        result["warnings"].append(
                            f"{slide_part} shape {shape_record['object_id']} uses default roundRect adjustment"
                        )
                    elif round_rect_status == "invalid":
                        result["errors"].append("ROUND_RECT_ADJUSTMENT_INVALID")
                        result["warnings"].append(
                            f"{slide_part} shape {shape_record['object_id']} has invalid roundRect adjustment"
                        )
                    result["native_shape_objects"].append(native)
                    if shape_record["has_text"]:
                        text_object = _text_object(
                            shape, slide_part, shape_record["layer"],
                            inheritance, theme_fonts,
                            (shape_record["x"], shape_record["y"], shape_record["cx"], shape_record["cy"]),
                            archive, names, slide_relationships,
                        )
                        text_object["visible"] = shape_record["visible"]
                        text_object["geometry_known"] = shape_record["geometry_known"]
                        slide_text_objects.append(text_object)
                        result["text_objects"].append(text_object)
                        for paragraph in text_object.get("paragraphs", []):
                            list_contract = paragraph.get("list")
                            if (
                                isinstance(list_contract, dict)
                                and list_contract.get("bullet_type") == "picture"
                                and list_contract.get("bullet_relationship_valid") is not True
                            ):
                                result["errors"].append(
                                    "NATIVE_LIST_PICTURE_RELATIONSHIP_INVALID"
                                )
                slide_list_paragraphs = sum(
                    1
                    for text_object in slide_text_objects
                    for paragraph in text_object.get("paragraphs", [])
                    if isinstance(paragraph.get("list"), dict)
                    and paragraph["list"].get("is_list") is True
                )
                for picture_index, picture_record in enumerate(pictures, start=1):
                    picture = picture_record["_element"]
                    blip = picture.find("p:blipFill/a:blip", NS)
                    picture_rid = blip.get(REMBED) if blip is not None else None
                    relationship = slide_relationships.get(picture_rid or "")
                    media_part = None
                    media_hash = None
                    if (
                        relationship is None
                        or relationship[2]
                        or not relationship[1].endswith("/image")
                    ):
                        result["errors"].append("PICTURE_RELATIONSHIP_INVALID")
                    else:
                        media_part = relationship[0]
                        if media_part in names:
                            media_hash = _archive_sha256(archive, media_part)
                    picture_record["media_sha256"] = media_hash
                    record = {
                        "object_key": f"{slide_part}#picture-{picture_index}",
                        "slide_position": position,
                        "slide_part": slide_part,
                        "object_id": picture_record["object_id"],
                        "object_name": picture_record["object_name"],
                        "object_type": picture_record["object_type"],
                        "layer": picture_record["layer"],
                        "hidden": picture_record["hidden"],
                        "relationship_id": picture_rid,
                        "media_part": media_part,
                        "media_basename": PurePosixPath(media_part).name if media_part else None,
                        "media_sha256": media_hash,
                        "x": picture_record["x"], "y": picture_record["y"],
                        "cx": picture_record["cx"], "cy": picture_record["cy"],
                        "bbox": [
                            picture_record["x"],
                            picture_record["y"],
                            picture_record["cx"],
                            picture_record["cy"],
                        ],
                        "geometry_known": picture_record["geometry_known"],
                        "full_slide": picture_record["geometry_known"]
                        and picture_record["visible"] is True
                        and is_near_full_page_bbox(
                            [
                                picture_record["x"],
                                picture_record["y"],
                                picture_record["cx"],
                                picture_record["cy"],
                            ],
                            [0, 0, width, height],
                        ),
                    }
                    record["visible"] = picture_record["visible"]
                    slide_picture_objects.append(record)
                    result["picture_objects"].append(record)
                result["structure_objects"].extend({
                    key: value for key, value in item.items() if key != "_element"
                } for item in object_records)
                missing_xml_relationships = sorted(
                    _xml_relationship_ids(slide) - set(slide_relationships)
                )
                if missing_xml_relationships:
                    result["errors"].append("MISSING_XML_RELATIONSHIP")
                    result["warnings"].append(
                        f"{slide_part} references missing ids: {', '.join(missing_xml_relationships)}"
                    )
                has_full_slide_picture = bool(full_pictures)
                picture_only = has_full_slide_picture and editable == 0
                any_full_slide_picture = any_full_slide_picture or has_full_slide_picture
                if editable == 0:
                    result["errors"].append("NO_EDITABLE_OBJECTS")
                if picture_only:
                    result["errors"].append("FULL_SLIDE_PICTURE_ONLY")
                elif has_full_slide_picture:
                    result["warnings"].append("FULL_SLIDE_PICTURE_WITH_EDITABLE_OBJECTS")
                result["editable_object_count"] += editable
                result["text_shape_count"] += text_shapes
                visible_graphic_frames = sum(
                    1 for item in graphic_frames if item["visible"] is True
                )
                result["graphic_frame_count"] += visible_graphic_frames
                result["picture_count"] += len(pictures)
                result["text_runs"] += slide_text_runs
                result["native_list_paragraphs"] += slide_list_paragraphs
                result["slides"].append(
                    {
                        "position": position,
                        "part": slide_part,
                        "editable_object_count": editable,
                        "text_shape_count": text_shapes,
                        "graphic_frame_count": visible_graphic_frames,
                        "picture_count": len(pictures),
                        "text_runs": slide_text_runs,
                        "native_list_paragraphs": slide_list_paragraphs,
                        "picture_objects": slide_picture_objects,
                        "full_slide_picture_count": full_pictures,
                        "full_slide_picture_risk": has_full_slide_picture,
                        "missing_xml_relationships": missing_xml_relationships,
                    }
                )

            result["font_declarations"] = sorted(fonts)
            result["font_sizes_pt"] = sorted(font_sizes)
            result["full_slide_picture_risk"] = any_full_slide_picture
            if spec is not None:
                _validate_native_list_contracts(result, spec, width, height)
                _validate_native_chart_contracts(result, spec)
                _validate_text_run_contracts(result, spec, width, height)
                _validate_element_bindings(result, spec, width, height)
            if report is not None:
                _validate_report_object_bindings(result, report)
                if spec is not None:
                    _validate_schema_report_contract(result, spec, report)
            if result["slide_count"] == 0:
                result["errors"].append("NO_SLIDES")

    except ValidationError as exc:
        result["errors"].append(exc.code)
        result["warnings"].append(exc.detail)
        return result
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError):
        result["errors"].append("PPTX_ZIP_INVALID")
        return result

    result["errors"] = list(dict.fromkeys(result["errors"]))
    result["valid"] = not result["errors"]
    return result


def trusted_object_snapshot(path: Path) -> dict[str, Any]:
    """Rebuild the object evidence used by postbuild checks from PPTX bytes only."""
    result = validate_pptx(path, expected_slides=1)
    return {
        "valid": result["valid"],
        "errors": list(result["errors"]),
        "pptx_sha256": result["pptx_sha256"],
        "structure_objects": list(result["structure_objects"]),
        "picture_objects": list(result["picture_objects"]),
        "full_slide_picture_risk": result["full_slide_picture_risk"],
    }


def summary_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return the CLI-friendly validation summary without per-object payloads."""
    verbose_keys = {
        "text_objects",
        "native_shape_objects",
        "picture_objects",
        "structure_objects",
    }
    summary = {key: value for key, value in result.items() if key not in verbose_keys}
    summary["evidence_level"] = "summary"
    summary["usable_as_background_evidence"] = False
    summary["slides"] = [
        {key: value for key, value in slide.items() if key != "picture_objects"}
        for slide in result.get("slides", [])
    ]
    return summary


def _emit_json(
    payload: dict[str, Any],
    output: Path | None,
    *,
    output_payload: dict[str, Any] | None = None,
) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output_text = json.dumps(
            output_payload if output_payload is not None else payload,
            ensure_ascii=False,
            indent=2,
        ) + "\n"
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
                handle.write(output_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, output)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    print(text, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--expected-slides", type=int)
    parser.add_argument(
        "--spec",
        type=Path,
        help="validate native list/TextBox structure against page-reconstruction.json",
    )
    parser.add_argument(
        "--build-report",
        type=Path,
        help="cross-check schema, build report, and PPTX object bindings",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="omit per-object arrays from stdout; --output always saves full evidence",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="atomically save the complete structure evidence JSON",
    )
    args = parser.parse_args(argv)
    result = validate_pptx(
        args.pptx, args.expected_slides, args.spec, args.build_report
    )
    _emit_json(
        summary_result(result) if args.summary else result,
        args.output,
        output_payload=result,
    )
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
