#!/usr/bin/env python3
"""Create one deterministic transparent local picture from an explicit crop."""

from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError

from extract_icon_asset import (
    MAX_TOLERANCE,
    _bytes_sha256,
    _edge_connected_background,
    _file_sha256,
    _touching_edges,
    _validate_bbox,
)


def _validate_output_path(output_path: Path) -> None:
    if output_path.suffix.lower() != ".png":
        raise ValueError("output must be a PNG inside assets/pictures")
    if (
        output_path.parent.name != "pictures"
        or output_path.parent.parent.name != "assets"
    ):
        raise ValueError("output must be inside an assets/pictures directory")


def _validate_source_seeds(
    seeds: Iterable[tuple[int, int]],
    source_bbox: tuple[int, int, int, int],
) -> tuple[tuple[int, int], ...]:
    normalized = tuple(seeds)
    if not normalized:
        raise ValueError("foreground_seeds must contain at least one source-pixel point")
    x, y, width, height = source_bbox
    for index, point in enumerate(normalized):
        if (
            not isinstance(point, tuple)
            or len(point) != 2
            or any(type(value) is not int for value in point)
        ):
            raise ValueError(f"foreground_seeds[{index}] must contain two integers")
        point_x, point_y = point
        if not (x <= point_x < x + width and y <= point_y < y + height):
            raise ValueError(f"foreground_seeds[{index}] must stay inside bbox_xywh")
    return normalized


def _seeded_alpha_isolated_asset(
    crop: Image.Image,
    seeds_local: tuple[tuple[int, int], ...],
    tolerance: int,
) -> tuple[Image.Image, dict[str, Any]]:
    width, height = crop.size
    background = _edge_connected_background(crop, tolerance)
    source_alpha = crop.getchannel("A").tobytes()
    foreground = bytearray(
        1 if source_alpha[offset] > 0 and not background[offset] else 0
        for offset in range(width * height)
    )
    selected = bytearray(width * height)
    selected_component_count = 0

    for seed_x, seed_y in seeds_local:
        seed_offset = seed_y * width + seed_x
        if not foreground[seed_offset]:
            raise ValueError(
                f"foreground seed {seed_x},{seed_y} does not select visible non-background foreground"
            )
        if selected[seed_offset]:
            continue
        selected_component_count += 1
        selected[seed_offset] = 1
        queue: deque[tuple[int, int]] = deque(((seed_x, seed_y),))
        while queue:
            point_x, point_y = queue.popleft()
            for next_y in range(max(0, point_y - 1), min(height, point_y + 2)):
                for next_x in range(max(0, point_x - 1), min(width, point_x + 2)):
                    offset = next_y * width + next_x
                    if foreground[offset] and not selected[offset]:
                        selected[offset] = 1
                        queue.append((next_x, next_y))

    alpha = bytearray(
        source_alpha[offset] if selected[offset] else 0
        for offset in range(width * height)
    )
    alpha_image = Image.frombytes("L", crop.size, bytes(alpha))
    minimum, maximum = alpha_image.getextrema()
    if maximum == 0:
        raise ValueError("alpha_isolation_seeded produced no visible foreground")
    if minimum == 255:
        raise ValueError("alpha_isolation_seeded produced no transparent background")
    foreground_bbox = alpha_image.getbbox()
    if foreground_bbox is None:
        raise ValueError("alpha_isolation_seeded produced no visible foreground")
    touches_edge = _touching_edges(foreground_bbox, crop.size)
    touched = [edge for edge, touching in touches_edge.items() if touching]
    if touched:
        raise ValueError(
            "visible foreground touches crop edge: "
            + ", ".join(touched)
            + "; expand the bbox"
        )

    asset = crop.copy()
    asset.putalpha(alpha_image)
    return asset, {
        "alpha_mask_sha256": _bytes_sha256(bytes(alpha)),
        "visible_pixels": sum(1 for value in alpha if value > 0),
        "discarded_foreground_pixels": sum(foreground) - sum(selected),
        "selected_component_count": selected_component_count,
        "alpha_extrema": [minimum, maximum],
        "foreground_bbox": list(foreground_bbox),
        "touches_edge": touches_edge,
    }


def extract_picture_asset(
    source_path: Path | str,
    output_path: Path | str,
    bbox_xywh: tuple[int, int, int, int],
    *,
    picture_id: str,
    foreground_seeds: Iterable[tuple[int, int]],
    tolerance: int = 24,
) -> dict[str, Any]:
    """Extract one seeded local picture without changing source-crop RGB values."""
    raw_source = Path(source_path).expanduser()
    if raw_source.is_symlink():
        raise ValueError("source_path must not be a symbolic link")
    source = raw_source.resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("source_path must be a readable image file")
    _validate_output_path(output)
    if not isinstance(picture_id, str) or not picture_id.strip():
        raise ValueError("picture_id must be a non-empty string")
    if type(tolerance) is not int or tolerance < 0 or tolerance > MAX_TOLERANCE:
        raise ValueError(f"tolerance must be an integer from 0 to {MAX_TOLERANCE}")

    with Image.open(source) as opened:
        opened.load()
        bbox = _validate_bbox(bbox_xywh, opened.size)
        source_seeds = _validate_source_seeds(foreground_seeds, bbox)
        x, y, width, height = bbox
        crop = opened.convert("RGBA").crop((x, y, x + width, y + height))

    local_seeds = tuple((point_x - x, point_y - y) for point_x, point_y in source_seeds)
    asset, mode_metadata = _seeded_alpha_isolated_asset(
        crop,
        local_seeds,
        tolerance,
    )
    if asset.convert("RGB").tobytes() != crop.convert("RGB").tobytes():
        raise RuntimeError("internal error: picture RGB values changed")
    output.parent.mkdir(parents=True, exist_ok=True)
    asset.save(output, format="PNG")
    return {
        "ok": True,
        "picture_id": picture_id,
        "crop_mode": "alpha_isolation_seeded",
        "source": str(source),
        "source_sha256": _file_sha256(source),
        "bbox_format": "xywh",
        "source_bbox": list(bbox),
        "foreground_seeds": [list(point) for point in source_seeds],
        "output": str(output),
        "asset_sha256": _file_sha256(output),
        "size": [width, height],
        "rgb_preserved": True,
        **mode_metadata,
    }


def _parse_bbox_xywh(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox must be X,Y,W,H integers") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be X,Y,W,H integers")
    return parts


def _parse_point(value: str) -> tuple[int, int]:
    try:
        parts = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("point must be X,Y integers") from exc
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("point must be X,Y integers")
    return parts


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract one seeded transparent local picture from an explicit crop."
    )
    parser.add_argument("source", type=Path, help="Clean visual reference image")
    parser.add_argument("--picture-id", required=True, help="Stable picture identifier")
    parser.add_argument(
        "--bbox-xywh",
        required=True,
        type=_parse_bbox_xywh,
        metavar="X,Y,W,H",
        help="Source-pixel crop including a background margin",
    )
    parser.add_argument(
        "--foreground-seed",
        required=True,
        action="append",
        type=_parse_point,
        metavar="X,Y",
        help="Source-pixel point inside one target foreground component; repeat as needed",
    )
    parser.add_argument("--tolerance", type=int, default=24)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = extract_picture_asset(
            args.source,
            args.output,
            args.bbox_xywh,
            picture_id=args.picture_id,
            foreground_seeds=args.foreground_seed,
            tolerance=args.tolerance,
        )
    except (OSError, ValueError, RuntimeError, UnidentifiedImageError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
