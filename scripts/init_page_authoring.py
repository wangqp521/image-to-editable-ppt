#!/usr/bin/env python3
"""Copy versioned, self-contained page authoring scripts into a page directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


TEMPLATE_VERSION = "copy-v1-schema-v2"
PAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
PROFILE_VALUES = {"rapid", "reviewed"}
TEMPLATE_SPECS = {
    "prepare_spec.template.py": {
        "destination": "prepare_spec.py",
        "tokens": {
            "@@SOURCE_PATH@@",
            "@@PAGE_ID@@",
            "@@VERIFICATION_PROFILE@@",
            "@@AUTHORING_TEMPLATE_VERSION@@",
        },
    },
    "finalize_spec.template.py": {
        "destination": "finalize_spec.py",
        "tokens": {
            "@@PAGE_ID@@",
            "@@VERIFICATION_PROFILE@@",
            "@@AUTHORING_TEMPLATE_VERSION@@",
        },
    },
}


class PageAuthoringError(RuntimeError):
    def __init__(self, code: str, path: Path, message: str):
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code}: {path}: {message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "path": str(self.path),
            "message": self.message,
        }


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _render_template(
    template_path: Path,
    *,
    required_tokens: set[str],
    replacements: dict[str, str],
) -> str:
    try:
        rendered = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PageAuthoringError(
            "PAGE_AUTHORING_TEMPLATE_INVALID",
            template_path,
            "template cannot be read",
        ) from exc
    for token in required_tokens:
        count = rendered.count(token)
        if count != 1:
            raise PageAuthoringError(
                "PAGE_AUTHORING_TEMPLATE_INVALID",
                template_path,
                f"token {token} must appear exactly once; found {count}",
            )
        rendered = rendered.replace(token, replacements[token])
    if "@@" in rendered:
        raise PageAuthoringError(
            "PAGE_AUTHORING_TEMPLATE_INVALID",
            template_path,
            "template contains an unresolved token",
        )
    try:
        compile(rendered, str(template_path), "exec")
    except SyntaxError as exc:
        raise PageAuthoringError(
            "PAGE_AUTHORING_TEMPLATE_INVALID",
            template_path,
            f"rendered template is not valid Python: {exc.msg}",
        ) from exc
    return rendered


def _publish_no_replace(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            if not content.endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def initialize_page_authoring(
    source_path: Path | str,
    page_dir: Path | str,
    page_id: str,
    profile: str,
    template_root: Path | str | None = None,
) -> dict[str, Any]:
    source = Path(source_path).expanduser().resolve()
    page = Path(page_dir).expanduser().resolve()
    if not source.is_file():
        raise PageAuthoringError(
            "PAGE_AUTHORING_SOURCE_INVALID",
            source,
            "source must be an existing readable file",
        )
    if PAGE_ID_PATTERN.fullmatch(page_id) is None:
        raise PageAuthoringError(
            "PAGE_AUTHORING_PAGE_ID_INVALID",
            page,
            "page_id must contain only letters, digits, hyphens, and underscores",
        )
    if profile not in PROFILE_VALUES:
        raise PageAuthoringError(
            "PAGE_AUTHORING_PROFILE_INVALID",
            page,
            "profile must be rapid or reviewed",
        )

    root = (
        Path(template_root).expanduser().resolve()
        if template_root is not None
        else Path(__file__).resolve().parents[1] / "templates" / "page-authoring"
    )
    destinations = [page / spec["destination"] for spec in TEMPLATE_SPECS.values()]
    for destination in destinations:
        if _path_exists(destination):
            raise PageAuthoringError(
                "PAGE_AUTHORING_TARGET_EXISTS",
                destination,
                "page authoring scripts are never overwritten",
            )

    replacements = {
        "@@SOURCE_PATH@@": json.dumps(str(source), ensure_ascii=False),
        "@@PAGE_ID@@": json.dumps(page_id, ensure_ascii=False),
        "@@VERIFICATION_PROFILE@@": json.dumps(profile, ensure_ascii=False),
        "@@AUTHORING_TEMPLATE_VERSION@@": json.dumps(TEMPLATE_VERSION),
    }
    rendered: list[tuple[Path, str]] = []
    for template_name, spec in TEMPLATE_SPECS.items():
        template_path = root / template_name
        rendered.append(
            (
                page / spec["destination"],
                _render_template(
                    template_path,
                    required_tokens=spec["tokens"],
                    replacements=replacements,
                ),
            )
        )

    page.mkdir(parents=True, exist_ok=True)
    for directory in ("assets", "evidence", "measurements", "work"):
        (page / directory).mkdir(exist_ok=True)
    created: list[Path] = []
    try:
        for destination, content in rendered:
            _publish_no_replace(destination, content)
            created.append(destination)
    except BaseException:
        for destination in reversed(created):
            destination.unlink(missing_ok=True)
        raise

    return {
        "ok": True,
        "page_dir": str(page),
        "page_id": page_id,
        "profile": profile,
        "source": str(source),
        "template_version": TEMPLATE_VERSION,
        "created": [str(path) for path in destinations],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--page-dir", type=Path, required=True)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--profile", choices=sorted(PROFILE_VALUES), required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = initialize_page_authoring(
            args.source,
            args.page_dir,
            args.page_id,
            args.profile,
        )
    except PageAuthoringError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    except OSError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "PAGE_AUTHORING_PUBLICATION_FAILED",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
