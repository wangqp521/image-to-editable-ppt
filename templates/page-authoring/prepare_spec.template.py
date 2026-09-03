#!/usr/bin/env python3
"""Self-contained schema-v2 authoring template copied once per page."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image


# ===== STABLE PRELUDE: BEGIN =====
AUTHORING_TEMPLATE_VERSION = @@AUTHORING_TEMPLATE_VERSION@@
PAGE_ID = @@PAGE_ID@@
VERIFICATION_PROFILE = @@VERIFICATION_PROFILE@@
PAGE_DIR = Path(__file__).resolve().parent
SOURCE = Path(@@SOURCE_PATH@@).resolve()
WORK_DIR = PAGE_DIR / "work"
OUTPUT = WORK_DIR / "page-reconstruction.json"


class AuthoringError(RuntimeError):
    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code}: {path}: {message}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def alpha_sha256(path: Path) -> str:
    with Image.open(path) as opened:
        alpha = opened.convert("RGBA").getchannel("A")
    return hashlib.sha256(alpha.tobytes()).hexdigest()


def coordinate_overlay_manifest_sha256(path: Path) -> str:
    with Image.open(path) as opened:
        value = opened.info.get("coordinate_overlay_manifest_sha256")
    if not isinstance(value, str) or len(value) != 64:
        raise AuthoringError(
            "AUTHORING_OVERLAY_EVIDENCE_INVALID",
            str(path),
            "coordinate overlay PNG metadata is missing",
        )
    return value


def solid(color: str, opacity: float = 1.0) -> dict[str, Any]:
    return {"type": "solid", "color": color, "opacity": opacity}


def linear_gradient(
    stops: list[dict[str, Any]],
    *,
    angle: float,
) -> dict[str, Any]:
    return {"type": "linear_gradient", "angle": angle, "stops": stops}


def stroke(
    color: str,
    *,
    width_pt: float,
    dash: str,
    opacity: float,
) -> dict[str, Any]:
    return {
        "color": color,
        "width": round(width_pt * 12700),
        "dash": dash,
        "opacity": opacity,
    }


def representation(
    *,
    source_fact_id: str,
    semantic_role: str,
    source_bbox: list[int],
    required: bool,
    selected_mode: str,
    required_editability: str,
    fallback_policy: str,
    reason: str,
    evidence: list[str],
) -> dict[str, Any]:
    if not source_fact_id or not semantic_role or not reason or not evidence:
        raise AuthoringError(
            "AUTHORING_REQUIRED_FIELD_MISSING",
            source_fact_id or "representation",
            "representation requires fact id, semantic role, reason, and evidence",
        )
    return {
        "source_fact_id": source_fact_id,
        "semantic_role": semantic_role,
        "source_bbox": list(source_bbox),
        "required": required,
        "selected_mode": selected_mode,
        "required_editability": required_editability,
        "fallback_policy": fallback_policy,
        "bound_element_ids": [],
        "reason": reason,
        "coverage_status": "covered",
        "evidence": list(evidence),
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


class MeasurementIndex:
    def __init__(self, report_path: Path, source_path: Path):
        self.report_path = report_path.resolve()
        self.source_path = source_path.resolve()
        try:
            self.report = json.loads(self.report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthoringError(
                "MEASUREMENT_SOURCE_MISMATCH",
                str(self.report_path),
                "measurement report is missing or invalid",
            ) from exc
        with Image.open(self.source_path) as opened:
            current_size = list(opened.size)
        source = self.report.get("source", {})
        if source.get("sha256") != sha256(self.source_path) or source.get("pixel_size") != current_size:
            raise AuthoringError(
                "MEASUREMENT_SOURCE_MISMATCH",
                str(self.report_path),
                "measurement report does not belong to the current source image",
            )
        self.source_size = current_size

    def bbox(self, measurement_id: str) -> list[int]:
        item = self.report.get("regions_by_id", {}).get(measurement_id)
        if not isinstance(item, dict) or not isinstance(item.get("source_bbox"), list):
            raise AuthoringError(
                "MEASUREMENT_ID_NOT_FOUND",
                measurement_id,
                "named bbox is missing from measurements.json",
            )
        bbox = item["source_bbox"]
        if len(bbox) != 4 or any(not isinstance(value, int) for value in bbox):
            raise AuthoringError("MEASUREMENT_ID_NOT_FOUND", measurement_id, "named bbox is invalid")
        return list(bbox)

    def point(self, measurement_id: str) -> list[int]:
        item = self.report.get("points_by_id", {}).get(measurement_id)
        if not isinstance(item, dict) or not isinstance(item.get("point"), list):
            raise AuthoringError(
                "MEASUREMENT_ID_NOT_FOUND",
                measurement_id,
                "named point is missing from measurements.json",
            )
        point = item["point"]
        if len(point) != 2 or any(not isinstance(value, int) for value in point):
            raise AuthoringError("MEASUREMENT_ID_NOT_FOUND", measurement_id, "named point is invalid")
        return list(point)


def _build_runs(
    text: str,
    default_style: dict[str, Any],
    spans: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not spans:
        return [{"start": 0, "end": len(text), **default_style}]
    normalized: list[dict[str, Any]] = []
    for raw_span in spans:
        item = dict(raw_span)
        if "text" in item:
            needle = item.pop("text")
            start = text.index(needle)
            item["start"] = start
            item["end"] = start + len(needle)
        if not isinstance(item.get("start"), int) or not isinstance(item.get("end"), int):
            raise AuthoringError("AUTHORING_REQUIRED_FIELD_MISSING", "spans", "span needs start/end or text")
        normalized.append(item)
    boundaries = {0, len(text)}
    for item in normalized:
        boundaries.add(item["start"])
        boundaries.add(item["end"])
    runs: list[dict[str, Any]] = []
    ordered = sorted(boundaries)
    for left, right in zip(ordered, ordered[1:]):
        style = dict(default_style)
        for item in normalized:
            if item["start"] <= left and right <= item["end"]:
                style.update({key: value for key, value in item.items() if key not in {"start", "end"}})
        runs.append({"start": left, "end": right, **style})
    return runs


class AuthoringPage:
    def __init__(
        self,
        *,
        page_id: str,
        verification_profile: str,
        source_path: Path,
        clean_visual_path: Path,
        slide_size_emu: list[int],
        page_frame_bbox: list[int],
        mapping_mode: str,
        background_color: str,
        session_reuse_reason: str,
    ):
        self.page_id = page_id
        self.verification_profile = verification_profile
        self.source_path = source_path.resolve()
        self.clean_visual_path = clean_visual_path.resolve()
        with Image.open(self.source_path) as opened:
            self.source_size = list(opened.size)
        with Image.open(self.clean_visual_path) as opened:
            self.visual_size = list(opened.size)
        self.source_sha256 = sha256(self.source_path)
        self.clean_visual_sha256 = sha256(self.clean_visual_path)
        self.slide_size_emu = list(slide_size_emu)
        self.page_frame_bbox = list(page_frame_bbox)
        self.mapping_mode = mapping_mode
        self.background_color = background_color
        self.session_reuse_reason = session_reuse_reason
        self.regions: list[dict[str, Any]] = []
        self.region_elements: dict[str, list[str]] = {}
        self.elements: list[dict[str, Any]] = []
        self.element_ids: set[str] = set()
        self.representations: list[dict[str, Any]] = []
        self.representation_element_ids: set[str] = set()
        self.typography_items: list[dict[str, Any]] = []
        self.graphics_items: list[dict[str, Any]] = []
        self.icon_items: list[dict[str, Any]] = []
        self.extra_modules: dict[str, Any] = {}
        self.background_items: list[dict[str, Any]] | None = None
        self.page_layout: dict[str, Any] | None = None
        self.reading_order: list[str] | None = None

    def slide_bbox(self, source_bbox: list[int]) -> list[int]:
        if len(source_bbox) != 4:
            raise AuthoringError("AUTHORING_REQUIRED_FIELD_MISSING", "source_bbox", "bbox must be xywh")
        frame_x, frame_y, frame_w, frame_h = self.page_frame_bbox
        slide_w, slide_h = self.slide_size_emu
        x, y, width, height = source_bbox
        return [
            round((x - frame_x) / frame_w * slide_w),
            round((y - frame_y) / frame_h * slide_h),
            round(width / frame_w * slide_w),
            round(height / frame_h * slide_h),
        ]

    def slide_point(self, source_point: list[int]) -> list[int]:
        return self.slide_bbox([source_point[0], source_point[1], 1, 1])[:2]

    def region(
        self,
        region_id: str,
        source_bbox: list[int],
        *,
        layer: int,
        padding: dict[str, int],
    ) -> None:
        if region_id in self.region_elements:
            raise AuthoringError("AUTHORING_DUPLICATE_ID", region_id, "region id is duplicated")
        self.region_elements[region_id] = []
        self.regions.append(
            {
                "region_id": region_id,
                "source_bbox": list(source_bbox),
                "slide_bbox": self.slide_bbox(source_bbox),
                "layer": layer,
                "padding": dict(padding),
                "element_ids": self.region_elements[region_id],
            }
        )

    def _register(
        self,
        element: dict[str, Any],
        *,
        region: str,
        representation_item: dict[str, Any] | None,
    ) -> None:
        element_id = element.get("element_id")
        if not isinstance(element_id, str) or not element_id:
            raise AuthoringError("AUTHORING_REQUIRED_FIELD_MISSING", "element_id", "element id is required")
        if element_id in self.element_ids:
            raise AuthoringError("AUTHORING_DUPLICATE_ID", element_id, "element id is duplicated")
        if region not in self.region_elements:
            raise AuthoringError("AUTHORING_REGION_NOT_FOUND", region, f"unknown region for {element_id}")
        if not isinstance(element.get("layer"), int):
            raise AuthoringError("AUTHORING_REQUIRED_FIELD_MISSING", element_id, "layer is required")
        self.element_ids.add(element_id)
        self.elements.append(element)
        self.region_elements[region].append(element_id)
        if representation_item is not None:
            bound = dict(representation_item)
            bound["bound_element_ids"] = [element_id]
            if bound.get("source_bbox") != element.get("source_bbox"):
                raise AuthoringError(
                    "AUTHORING_REPRESENTATION_BBOX_MISMATCH",
                    element_id,
                    "representation bbox must equal the bound element bbox",
                )
            self.representations.append(bound)
            self.representation_element_ids.add(element_id)

    def shape(
        self,
        element_id: str,
        source_bbox: list[int],
        *,
        region: str,
        layer: int,
        shape_type: str,
        fill: dict[str, Any] | str,
        line: dict[str, Any] | None,
        effects: dict[str, Any] | str,
        rotation: float,
        representation: dict[str, Any] | None,
    ) -> None:
        style: dict[str, Any] = {
            "shape_type": shape_type,
            "fill": fill,
            "effects": effects,
            "rotation": rotation,
        }
        if line is not None:
            style["line"] = line
        self._register(
            {
                "element_id": element_id,
                "kind": "shape",
                "source_bbox": list(source_bbox),
                "slide_bbox": self.slide_bbox(source_bbox),
                "layer": layer,
                "editable": True,
                "confidence": "high",
                "style": style,
                "content": {},
            },
            region=region,
            representation_item=representation,
        )
        self.graphics_items.append({"element_id": element_id, "kind": "shape"})

    def line(
        self,
        element_id: str,
        source_bbox: list[int],
        *,
        region: str,
        layer: int,
        line: dict[str, Any],
        head_arrow: str,
        tail_arrow: str,
        rotation: float,
        representation: dict[str, Any],
    ) -> None:
        self._register(
            {
                "element_id": element_id,
                "kind": "line",
                "source_bbox": list(source_bbox),
                "slide_bbox": self.slide_bbox(source_bbox),
                "layer": layer,
                "editable": True,
                "confidence": "high",
                "style": {
                    "line": dict(line),
                    "head_arrow": head_arrow,
                    "tail_arrow": tail_arrow,
                    "rotation": rotation,
                },
                "content": {},
            },
            region=region,
            representation_item=representation,
        )
        self.graphics_items.append({"element_id": element_id, "kind": "line"})

    def text(
        self,
        element_id: str,
        source_bbox: list[int],
        *,
        region: str,
        layer: int,
        paragraphs_text: list[str],
        font_name: str,
        font_size: float,
        color: str,
        font_weight: int,
        alignment: str,
        vertical_alignment: str,
        wrap: bool,
        margins: dict[str, int],
        representation: dict[str, Any],
        spans: list[dict[str, Any]] | None,
        source_line_distances_pt: list[float] | None = None,
        source_center_offset_y_pt: float | None = None,
        paragraph_styles: list[dict[str, Any]] | None = None,
    ) -> None:
        if not paragraphs_text or any(not isinstance(value, str) for value in paragraphs_text):
            raise AuthoringError("AUTHORING_REQUIRED_FIELD_MISSING", element_id, "paragraphs_text is required")
        if (len(paragraphs_text) > 1 or vertical_alignment != "top" or wrap) and (
            source_line_distances_pt is None or source_center_offset_y_pt is None
        ):
            raise AuthoringError(
                "AUTHORING_REQUIRED_FIELD_MISSING",
                element_id,
                "source layout measurements are required for multiline, wrapped, or non-top text",
            )
        text = "".join(paragraphs_text)
        mapped = self.slide_bbox(source_bbox)
        default_run = {
            "font_size": font_size,
            "font_weight": font_weight,
            "color": color,
            "italic": False,
            "underline": False,
            "strike": False,
            "baseline": 0,
            "letter_spacing": 0,
        }
        paragraphs: list[dict[str, Any]] = []
        cursor = 0
        for index, paragraph_text in enumerate(paragraphs_text):
            end = cursor + len(paragraph_text)
            authored_style = (paragraph_styles or [{}] * len(paragraphs_text))[index]
            paragraph = {
                "start": cursor,
                "end": end,
                "alignment": authored_style.get("alignment", alignment),
                "line_spacing": authored_style.get("line_spacing", 1.0),
                "space_before": authored_style.get("space_before", 0),
                "space_after": authored_style.get("space_after", 0),
                "indent": authored_style.get("indent", 0),
                "list": authored_style.get("list", {"is_list": False, "level": 0, "bullet": None}),
            }
            if "margin_left" in authored_style:
                paragraph["margin_left"] = authored_style["margin_left"]
            paragraphs.append(paragraph)
            cursor = end
        typography = {
            "element_id": element_id,
            "text": text,
            "selected_font": font_name,
            "internal_font_declaration": font_name,
            "source_font_guess": "Chinese sans-serif",
            "fallback_reason": "source_font_uncertain",
            "fallback_trace": None,
            "font_declaration_verified": False,
            "runs": _build_runs(text, default_run, spans),
            "paragraphs": paragraphs,
            "text_box": {
                "x": mapped[0],
                "y": mapped[1],
                "w": mapped[2],
                "h": mapped[3],
                "margins": dict(margins),
                "alignment": alignment,
                "vertical_alignment": vertical_alignment,
                "wrap": wrap,
                "overflow": False,
                "soft_breaks": [],
                "paragraph_breaks": [paragraph["end"] for paragraph in paragraphs[:-1]],
            },
        }
        if source_line_distances_pt is not None:
            typography["source_layout"] = {
                "line_center_distances_pt": list(source_line_distances_pt),
                "text_block_center_offset_y_pt": source_center_offset_y_pt,
            }
        self.typography_items.append(typography)
        self._register(
            {
                "element_id": element_id,
                "kind": "text",
                "source_bbox": list(source_bbox),
                "slide_bbox": mapped,
                "layer": layer,
                "editable": True,
                "confidence": "high",
                "style": {
                    "fill": "noFill",
                    "effects": "none",
                    "rotation": 0,
                    "vertical_alignment": vertical_alignment,
                    "wrap": wrap,
                },
                "content": {"text": text},
            },
            region=region,
            representation_item=representation,
        )

    def icon(
        self,
        element_id: str,
        source_bbox: list[int],
        *,
        region: str,
        layer: int,
        icon_id: str,
        category: str,
        asset_path: Path,
        representation: dict[str, Any],
        repeat_group: str | None = None,
        selectable_picture_verified: bool = True,
    ) -> None:
        path = asset_path.resolve()
        with Image.open(path) as opened:
            pixel_size = list(opened.size)
        if pixel_size != source_bbox[2:]:
            raise AuthoringError("AUTHORING_ASSET_SIZE_MISMATCH", element_id, "icon pixels must equal source bbox")
        asset_hash = sha256(path)
        mapped = self.slide_bbox(source_bbox)
        self.icon_items.append(
            {
                "icon_id": icon_id,
                "element_id": element_id,
                "category": category,
                "instance_count": 1,
                "repeat_group": repeat_group,
                "semantic_scope": "icon_only",
                "source_bbox": list(source_bbox),
                "slide_bbox": mapped,
                "layer": layer,
                "source_path": str(self.clean_visual_path),
                "source_sha256": self.clean_visual_sha256,
                "crop_mode": "alpha_isolation",
                "padding": 0,
                "background_handling": "transparent",
                "asset_path": str(path),
                "asset_sha256": asset_hash,
                "alpha_mask_sha256": alpha_sha256(path),
                "final_width": pixel_size[0],
                "final_height": pixel_size[1],
                "sharpness": "source_pixels",
                "validation": "passed",
                "native_redraw": False,
                "selectable_picture_verified": selectable_picture_verified,
                "object_type": "picture",
            }
        )
        self._register(
            {
                "element_id": element_id,
                "kind": "icon",
                "source_bbox": list(source_bbox),
                "slide_bbox": mapped,
                "layer": layer,
                "editable": False,
                "confidence": "high",
                "style": {"rotation": 0, "opacity": 1},
                "content": {
                    "asset": {"path": str(path), "asset_sha256": asset_hash, "pixel_size": pixel_size},
                    "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                    "mode": "none",
                },
            },
            region=region,
            representation_item=representation,
        )

    def picture(
        self,
        element_id: str,
        source_bbox: list[int],
        *,
        region: str,
        layer: int,
        asset_path: Path,
        crop: dict[str, float],
        mode: str,
        representation: dict[str, Any],
    ) -> None:
        path = asset_path.resolve()
        with Image.open(path) as opened:
            pixel_size = list(opened.size)
        asset_hash = sha256(path)
        self._register(
            {
                "element_id": element_id,
                "kind": "picture",
                "source_bbox": list(source_bbox),
                "slide_bbox": self.slide_bbox(source_bbox),
                "layer": layer,
                "editable": False,
                "confidence": "high",
                "style": {"rotation": 0, "opacity": 1},
                "content": {
                    "asset": {"path": str(path), "asset_sha256": asset_hash, "pixel_size": pixel_size},
                    "crop": dict(crop),
                    "mode": mode,
                },
            },
            region=region,
            representation_item=representation,
        )

    def raw_element(
        self,
        element: dict[str, Any],
        *,
        region: str,
        representation: dict[str, Any] | None,
        module_items: dict[str, Any] | None = None,
    ) -> None:
        self._register(dict(element), region=region, representation_item=representation)
        for module_name, payload in (module_items or {}).items():
            self.extra_modules[module_name] = payload

    def set_background_items(self, items: list[dict[str, Any]]) -> None:
        self.background_items = [dict(item) for item in items]

    def set_page_layout(
        self,
        *,
        anchors: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        layout_invariants: list[str],
        density_targets: dict[str, int],
        coordinate_overlay_evidence: dict[str, Any],
    ) -> None:
        overlay_path = Path(coordinate_overlay_evidence.get("path", ""))
        if not overlay_path.is_file() or coordinate_overlay_evidence.get("sha256") != sha256(overlay_path):
            raise AuthoringError(
                "AUTHORING_OVERLAY_EVIDENCE_INVALID",
                str(overlay_path),
                "coordinate overlay path/hash is invalid",
            )
        if coordinate_overlay_evidence.get("source_sha256") != self.source_sha256:
            raise AuthoringError(
                "AUTHORING_OVERLAY_EVIDENCE_INVALID",
                str(overlay_path),
                "coordinate overlay source hash is stale",
            )
        self.page_layout = {
            "anchors": [dict(item) for item in anchors],
            "relationships": [dict(item) for item in relationships],
            "layout_invariants": list(layout_invariants),
            "density_targets": dict(density_targets),
            "coordinate_overlay_evidence": dict(coordinate_overlay_evidence),
        }

    def set_reading_order(self, element_ids: list[str]) -> None:
        self.reading_order = list(element_ids)

    def build(self) -> dict[str, Any]:
        if self.background_items is None or self.page_layout is None or self.reading_order is None:
            raise AuthoringError(
                "AUTHORING_REQUIRED_FIELD_MISSING",
                self.page_id,
                "background, page layout, and reading order must be authored",
            )
        background_ids = {item.get("bound_element_id") for item in self.background_items}
        if None in background_ids or not background_ids.issubset(self.element_ids):
            raise AuthoringError("AUTHORING_BACKGROUND_BINDING_INVALID", self.page_id, "background binding is invalid")
        conflicts = background_ids & self.representation_element_ids
        if conflicts:
            raise AuthoringError(
                "AUTHORING_BACKGROUND_REPRESENTATION_CONFLICT",
                sorted(conflicts)[0],
                "background elements cannot appear in representation_plan",
            )
        missing_representations = self.element_ids - background_ids - self.representation_element_ids
        if missing_representations:
            raise AuthoringError(
                "AUTHORING_REQUIRED_FIELD_MISSING",
                sorted(missing_representations)[0],
                "every non-background element requires representation",
            )
        if len(self.reading_order) != len(set(self.reading_order)) or set(self.reading_order) != self.element_ids:
            raise AuthoringError(
                "AUTHORING_READING_ORDER_INVALID",
                self.page_id,
                "reading order must contain every element id exactly once",
            )

        modules: dict[str, Any] = {
            "background": {"items": self.background_items},
            "page_layout": self.page_layout,
            "representation_plan": {"items": self.representations},
        }
        activated_modules = ["background", "page_layout", "representation_plan"]
        if self.typography_items:
            modules["typography"] = {"slide_coordinate_unit": "EMU", "items": self.typography_items}
            activated_modules.append("typography")
        non_background_graphics = [
            item for item in self.graphics_items if item["element_id"] not in background_ids
        ]
        if non_background_graphics:
            modules["graphics"] = {"items": non_background_graphics}
            activated_modules.append("graphics")
        if self.icon_items:
            modules["icons"] = {
                "schema_version": 2,
                "page_id": self.page_id,
                "slide_coordinate_unit": "EMU",
                "clean_visual_reference": str(self.clean_visual_path),
                "clean_visual_sha256": self.clean_visual_sha256,
                "icons": self.icon_items,
            }
            activated_modules.append("icons")
        for module_name, payload in self.extra_modules.items():
            modules[module_name] = payload
            if module_name not in activated_modules:
                activated_modules.append(module_name)

        return {
            "schema_version": 2,
            "page_id": self.page_id,
            "verification_profile": self.verification_profile,
            "delivery_status": "pending",
            "session_reuse": {
                "mode": "fresh_reconstruction",
                "reason": self.session_reuse_reason,
                "artifacts": [],
            },
            "content_reference": {"path": str(self.source_path), "sha256": self.source_sha256},
            "clean_visual_reference": {
                "path": str(self.clean_visual_path),
                "sha256": self.clean_visual_sha256,
            },
            "canvas": {
                "source_size": self.source_size,
                "visual_size": self.visual_size,
                "page_frame_bbox": self.page_frame_bbox,
                "slide_size_emu": self.slide_size_emu,
                "mapping_mode": self.mapping_mode,
                "background": self.background_color,
            },
            "activated_modules": activated_modules,
            "modules": modules,
            "regions": self.regions,
            "elements": self.elements,
            "reading_order": self.reading_order,
            "visual_gate": {"status": "pending", "evidence": [], "tripwire": None, "review": None},
            "editability_gate": {
                "status": "pending",
                "evidence": [
                    "page facts explicitly bind every non-background source fact to native, composite, or asset representation"
                ],
            },
        }


with Image.open(SOURCE) as _source_image:
    SOURCE_WIDTH, SOURCE_HEIGHT = _source_image.size
SOURCE_SHA256 = sha256(SOURCE)
# ===== STABLE PRELUDE: END =====


# ===== PAGE FACTS: BEGIN =====
raise AuthoringError(
    "AUTHORING_PAGE_FACTS_REQUIRED",
    PAGE_ID,
    "replace the PAGE FACTS block with explicit regions, elements, representations, and reading order",
)
# ===== PAGE FACTS: END =====


# ===== STABLE ASSEMBLY: BEGIN =====
if "PAGE" not in globals() or not isinstance(PAGE, AuthoringPage):
    raise AuthoringError("AUTHORING_PAGE_FACTS_REQUIRED", PAGE_ID, "PAGE must be an AuthoringPage")
_SPEC = PAGE.build()
_write_json_atomic(OUTPUT, _SPEC)
print(
    json.dumps(
        {
            "ok": True,
            "output": str(OUTPUT),
            "page_id": PAGE_ID,
            "elements": len(_SPEC["elements"]),
            "text_boxes": len(_SPEC["modules"].get("typography", {}).get("items", [])),
            "icons": len(_SPEC["modules"].get("icons", {}).get("icons", [])),
            "authoring_template_version": AUTHORING_TEMPLATE_VERSION,
        },
        ensure_ascii=False,
    )
)
# ===== STABLE ASSEMBLY: END =====
