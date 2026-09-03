#!/usr/bin/env python3
"""Measure explicit image points and bounding boxes without object detection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


MEASUREMENT_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rgba_hex(pixel: tuple[int, int, int, int]) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in pixel)


def _validated_point(point: tuple[int, int], size: tuple[int, int]) -> tuple[int, int]:
    x, y = point
    if x < 0 or y < 0 or x >= size[0] or y >= size[1]:
        raise ValueError("point must stay inside source image")
    return x, y


def _validated_bbox(
    bbox: tuple[int, int, int, int],
    size: tuple[int, int],
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    if left < 0 or top < 0 or right > size[0] or bottom > size[1] or right <= left or bottom <= top:
        raise ValueError("bbox must stay inside source image")
    return left, top, right, bottom


def _sample_point(image: Image.Image, x: int, y: int) -> dict[str, Any]:
    x, y = _validated_point((x, y), image.size)
    pixel = image.getpixel((x, y))
    return {"point": [x, y], "rgba": _rgba_hex(pixel)}


def _dominant_colors(crop: Image.Image, count: int = 5) -> list[dict[str, Any]]:
    quantized = crop.convert("RGB").quantize(colors=count)
    palette = quantized.getpalette() or []
    colors = quantized.getcolors(maxcolors=crop.width * crop.height) or []
    result: list[dict[str, Any]] = []
    for pixels, index in sorted(colors, reverse=True)[:count]:
        offset = index * 3
        rgb = tuple(palette[offset : offset + 3])
        result.append({"rgb": "#" + "".join(f"{channel:02X}" for channel in rgb), "pixels": pixels})
    return result


def _foreground_edges(crop: Image.Image) -> dict[str, bool]:
    corners = [
        crop.getpixel((0, 0))[:3],
        crop.getpixel((crop.width - 1, 0))[:3],
        crop.getpixel((0, crop.height - 1))[:3],
        crop.getpixel((crop.width - 1, crop.height - 1))[:3],
    ]
    background = tuple(int(statistics.median(channel)) for channel in zip(*corners))

    def foreground(pixel: tuple[int, int, int, int]) -> bool:
        return pixel[3] > 0 and any(abs(pixel[channel] - background[channel]) > 12 for channel in range(3))

    return {
        "top": any(foreground(crop.getpixel((x, 0))) for x in range(crop.width)),
        "right": any(foreground(crop.getpixel((crop.width - 1, y))) for y in range(crop.height)),
        "bottom": any(foreground(crop.getpixel((x, crop.height - 1))) for x in range(crop.width)),
        "left": any(foreground(crop.getpixel((0, y))) for y in range(crop.height)),
    }


def _measure_crop(
    crop: Image.Image,
    bbox: tuple[int, int, int, int],
    source_size: tuple[int, int],
    crop_path: Path,
    magnified_path: Path,
) -> dict[str, Any]:
    left, top, right, bottom = bbox
    center = (crop.width // 2, crop.height // 2)
    samples = {
        "top_left": _rgba_hex(crop.getpixel((0, 0))),
        "top_right": _rgba_hex(crop.getpixel((crop.width - 1, 0))),
        "center": _rgba_hex(crop.getpixel(center)),
        "bottom_left": _rgba_hex(crop.getpixel((0, crop.height - 1))),
        "bottom_right": _rgba_hex(crop.getpixel((crop.width - 1, crop.height - 1))),
    }
    alpha = crop.getchannel("A")
    alpha_histogram = alpha.histogram()
    total = crop.width * crop.height
    transparent = sum(alpha_histogram[:255])
    extrema = alpha.getextrema()
    return {
        "bbox_format": "xywh",
        "source_bbox": [left, top, right - left, bottom - top],
        "measured_bbox_ltrb": [left, top, right, bottom],
        "normalized_bbox": [
            round(left / source_size[0], 6),
            round(top / source_size[1], 6),
            round(right / source_size[0], 6),
            round(bottom / source_size[1], 6),
        ],
        "crop_size": list(crop.size),
        "crop_path": str(crop_path.resolve()),
        "magnified_path": str(magnified_path.resolve()),
        "samples": samples,
        "dominant_colors": _dominant_colors(crop),
        "alpha": {
            "min": extrema[0],
            "max": extrema[1],
            "transparent_pixel_ratio": round(transparent / total if total else 0.0, 6),
        },
        "foreground_touches_edge": _foreground_edges(crop),
    }


def _validated_named_items(
    named_points: list[tuple[str, tuple[int, int]]],
    named_bboxes: list[tuple[str, tuple[int, int, int, int]]],
) -> None:
    seen: set[str] = set()
    for measurement_id, _coordinates in [*named_points, *named_bboxes]:
        if MEASUREMENT_ID_PATTERN.fullmatch(measurement_id) is None:
            raise ValueError(f"MEASUREMENT_ID_INVALID: {measurement_id}")
        if measurement_id in seen:
            raise ValueError(f"MEASUREMENT_ID_DUPLICATE: {measurement_id}")
        seen.add(measurement_id)


def _write_report_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, allow_nan=False, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def inspect_image_region(
    source_path: Path | str,
    output_dir: Path | str,
    *,
    points: list[tuple[int, int]] | None = None,
    bboxes: list[tuple[int, int, int, int]] | None = None,
    named_points: list[tuple[str, tuple[int, int]]] | None = None,
    named_bboxes: list[tuple[str, tuple[int, int, int, int]]] | None = None,
    scale: int = 2,
) -> dict[str, Any]:
    """Measure only the points and boxes explicitly supplied by the caller."""
    if scale < 1:
        raise ValueError("scale must be at least 1")
    actual_named_points = named_points or []
    actual_named_bboxes = named_bboxes or []
    _validated_named_items(actual_named_points, actual_named_bboxes)
    source_path = Path(source_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    with Image.open(source_path) as opened:
        image = opened.convert("RGBA")
    output_dir.mkdir(parents=True, exist_ok=True)

    point_results = [_sample_point(image, x, y) for x, y in points or []]
    points_by_id: dict[str, dict[str, Any]] = {}
    for measurement_id, raw_point in actual_named_points:
        result = {"id": measurement_id, **_sample_point(image, *raw_point)}
        point_results.append(result)
        points_by_id[measurement_id] = result

    regions: list[dict[str, Any]] = []
    for index, raw_bbox in enumerate(bboxes or [], start=1):
        bbox = _validated_bbox(raw_bbox, image.size)
        crop = image.crop(bbox)
        region_dir = output_dir / f"region-{index:03d}"
        region_dir.mkdir(parents=True, exist_ok=True)
        crop_path = region_dir / "crop.png"
        magnified_path = region_dir / f"crop-{scale * 100}pct.png"
        crop.save(crop_path)
        crop.resize(
            (crop.width * scale, crop.height * scale),
            Image.Resampling.NEAREST,
        ).save(magnified_path)
        regions.append(_measure_crop(crop, bbox, image.size, crop_path, magnified_path))

    regions_by_id: dict[str, dict[str, Any]] = {}
    for measurement_id, raw_bbox in actual_named_bboxes:
        bbox = _validated_bbox(raw_bbox, image.size)
        crop = image.crop(bbox)
        region_dir = output_dir / "regions" / measurement_id
        region_dir.mkdir(parents=True, exist_ok=True)
        crop_path = region_dir / "crop.png"
        magnified_path = region_dir / f"crop-{scale * 100}pct.png"
        crop.save(crop_path)
        crop.resize(
            (crop.width * scale, crop.height * scale),
            Image.Resampling.NEAREST,
        ).save(magnified_path)
        result = {
            "id": measurement_id,
            **_measure_crop(crop, bbox, image.size, crop_path, magnified_path),
        }
        regions.append(result)
        regions_by_id[measurement_id] = result

    report = {
        "source": {
            "path": str(source_path),
            "sha256": _sha256(source_path),
            "pixel_size": list(image.size),
        },
        "points": point_results,
        "regions": regions,
        "points_by_id": points_by_id,
        "regions_by_id": regions_by_id,
    }
    _write_report_atomic(output_dir / "measurements.json", report)
    return report


def _parse_csv(value: str, expected: int, label: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must contain integers") from exc
    if len(parsed) != expected:
        raise argparse.ArgumentTypeError(f"{label} must contain {expected} integers")
    return parsed


def _parse_named_csv(
    value: str,
    expected: int,
    label: str,
) -> tuple[str, tuple[int, ...]]:
    measurement_id, separator, csv_value = value.partition("=")
    if not separator or MEASUREMENT_ID_PATTERN.fullmatch(measurement_id) is None:
        raise argparse.ArgumentTypeError(f"{label} must use ID=integers")
    return measurement_id, _parse_csv(csv_value, expected, label)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("source", type=Path)
    parser.add_argument("--point", action="append", default=[], type=lambda value: _parse_csv(value, 2, "point"))
    parser.add_argument(
        "--point-id",
        action="append",
        default=[],
        metavar="ID=X,Y",
        type=lambda value: _parse_named_csv(value, 2, "point-id"),
    )
    parser.add_argument(
        "--bbox",
        action="append",
        default=[],
        metavar="LEFT,TOP,RIGHT,BOTTOM",
        help="crop bounds as edge coordinates; RIGHT and BOTTOM are exclusive",
        type=lambda value: _parse_csv(value, 4, "bbox"),
    )
    parser.add_argument(
        "--bbox-id",
        action="append",
        default=[],
        metavar="ID=LEFT,TOP,RIGHT,BOTTOM",
        help="named crop bounds as edge coordinates; RIGHT and BOTTOM are exclusive",
        type=lambda value: _parse_named_csv(value, 4, "bbox-id"),
    )
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = inspect_image_region(
            args.source,
            args.output_dir,
            points=args.point,
            bboxes=args.bbox,
            named_points=args.point_id,
            named_bboxes=args.bbox_id,
            scale=args.scale,
        )
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
