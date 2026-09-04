#!/usr/bin/env python3
"""Render one immutable PPTX through stable LibreOffice into traceable PDF/PNG evidence."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from lib.font_runtime import pdf_font_name_matches, validate_font_runtime


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRERELEASE_PATTERN = re.compile(
    r"(?:libreofficedev|\b(?:alpha|beta|rc)\d*\b)",
    re.IGNORECASE,
)
INVALID_VERSION_PATTERN = re.compile(r"^(?:unavailable\b|exit\s*=)", re.IGNORECASE)
PAGE_SIZE = (960.0, 540.0)
PREVIEW_SIZE = (1920, 1080)
REQUIRED_EXECUTABLES = ("soffice", "pdftoppm", "pdffonts", "pdftotext")


class RenderError(RuntimeError):
    def __init__(self, code: str, detail: str, *, returncode: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.returncode = returncode


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_valid_executable_version(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and INVALID_VERSION_PATTERN.search(value.strip()) is None
    )


def _resolve_executable(requested: str) -> Path:
    candidate = Path(requested).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate.resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    else:
        found = shutil.which(requested)
        if found:
            return Path(found).resolve()
    raise RenderError("RENDER_RUNTIME_INVALID", f"executable unavailable: {requested}")


def _executable_version(path: Path) -> str:
    for flag in ("--version", "-v"):
        try:
            completed = _run([str(path), flag])
        except RenderError:
            continue
        value = (completed.stdout or completed.stderr).strip().splitlines()
        if value and _is_valid_executable_version(value[0]):
            return value[0]
    raise RenderError("RENDER_RUNTIME_INVALID", f"version unavailable: {path}")


def _draft_runtime(
    *,
    preferred_font: str,
    soffice: str,
    pdftoppm: str,
    pdffonts: str,
    pdftotext: str,
) -> dict[str, Any]:
    """Resolve preview tools at point of use without a batch preflight report."""
    if not isinstance(preferred_font, str) or not preferred_font.strip():
        raise RenderError(
            "RENDER_RUNTIME_INVALID",
            "--preferred-font is required when --runtime is omitted",
        )
    requested = {
        "soffice": soffice,
        "pdftoppm": pdftoppm,
        "pdffonts": pdffonts,
        "pdftotext": pdftotext,
    }
    executables: dict[str, dict[str, str]] = {}
    for name, value in requested.items():
        path = _resolve_executable(value)
        version = _executable_version(path)
        executables[name] = {
            "path": str(path),
            "version": version,
            "sha256": _sha256(path),
        }
    if PRERELEASE_PATTERN.search(executables["soffice"]["version"]):
        raise RenderError(
            "RENDER_RUNTIME_INVALID",
            f"unstable LibreOffice: {executables['soffice']['version']}",
        )
    return {
        "mode": "draft_preview",
        "preferred_font": preferred_font.strip(),
        "executables": executables,
    }


def render_environment(
    runtime: dict[str, Any],
    base: dict[str, str],
) -> dict[str, str]:
    """Return a provider-specific render environment without selecting fonts."""
    env = dict(base)
    try:
        font_runtime = validate_font_runtime(runtime.get("font_runtime"))
    except ValueError as exc:
        raise RenderError("RENDER_RUNTIME_INVALID", str(exc)) from exc
    if font_runtime["provider"] == "fontconfig":
        fontconfig = runtime.get("fontconfig")
        if not isinstance(fontconfig, dict) or not isinstance(
            fontconfig.get("path"), str
        ):
            raise RenderError("RENDER_RUNTIME_INVALID", "missing fontconfig")
        env["FONTCONFIG_FILE"] = fontconfig["path"]
    else:
        env.pop("FONTCONFIG_FILE", None)
    return env


def validate_resolved_fonts(family: str, resolved_fonts: list[str]) -> bool:
    """Report whether PDF fonts match the preferred family."""
    mismatches = [
        name for name in resolved_fonts if not pdf_font_name_matches(family, name)
    ]
    return not mismatches


def _load_runtime(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenderError("RENDER_RUNTIME_INVALID", str(exc)) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("valid") is not True
        or payload.get("renderer_backend") != "libreoffice"
        or payload.get("preview_size") != list(PREVIEW_SIZE)
    ):
        raise RenderError("RENDER_RUNTIME_INVALID", str(path))
    executables = payload.get("executables")
    if not isinstance(executables, dict):
        raise RenderError("RENDER_RUNTIME_INVALID", "invalid executables")
    soffice_entry = executables.get("soffice")
    if not isinstance(soffice_entry, dict):
        raise RenderError("RENDER_RUNTIME_INVALID", "missing soffice")
    version = soffice_entry.get("version")
    if not _is_valid_executable_version(version) or PRERELEASE_PATTERN.search(version):
        raise RenderError("RENDER_RUNTIME_INVALID", f"unstable LibreOffice: {version}")
    for name in REQUIRED_EXECUTABLES:
        entry = executables.get(name)
        if not isinstance(entry, dict):
            raise RenderError("RENDER_RUNTIME_INVALID", f"missing {name}")
        executable = Path(str(entry.get("path", ""))).expanduser().resolve()
        if (
            not executable.is_file()
            or not os.access(executable, os.X_OK)
            or not _is_valid_executable_version(entry.get("version"))
            or not isinstance(entry.get("sha256"), str)
            or _sha256(executable) != entry["sha256"]
        ):
            raise RenderError("RENDER_RUNTIME_INVALID", f"invalid {name}")
    try:
        font_runtime = validate_font_runtime(payload.get("font_runtime"))
    except ValueError as exc:
        raise RenderError("RENDER_RUNTIME_INVALID", str(exc)) from exc
    fontconfig = payload.get("fontconfig")
    if font_runtime["provider"] == "fontconfig":
        if not isinstance(fontconfig, dict):
            raise RenderError("RENDER_RUNTIME_INVALID", "missing fontconfig")
        fontconfig_path = Path(str(fontconfig.get("path", ""))).expanduser().resolve()
        if (
            not fontconfig_path.is_file()
            or not isinstance(fontconfig.get("sha256"), str)
            or _sha256(fontconfig_path) != fontconfig["sha256"]
        ):
            raise RenderError("RENDER_RUNTIME_INVALID", "invalid fontconfig")
    elif fontconfig is not None:
        raise RenderError(
            "RENDER_RUNTIME_INVALID",
            "windows-system runtime must not carry fontconfig",
        )
    return payload


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError("RENDER_COMMAND_FAILED", str(exc)) from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RenderError(
            "RENDER_COMMAND_FAILED",
            f"exit={completed.returncode}: {detail}",
            returncode=completed.returncode,
        )
    return completed


def _parse_pdffonts(text: str) -> list[str]:
    fonts: set[str] = set()
    for line in text.splitlines()[2:]:
        fields = line.split()
        if fields:
            fonts.add(fields[0].split("+", 1)[-1])
    return sorted(fonts)


def _pdfinfo_executable(pdftoppm: Path) -> Path:
    sibling = pdftoppm.with_name("pdfinfo")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling
    found = shutil.which("pdfinfo")
    if found:
        return Path(found).resolve()
    raise RenderError("RENDER_RUNTIME_INVALID", "pdfinfo is unavailable")


def _inspect_pdf(pdf: Path, pdftoppm: Path) -> tuple[int, list[float]]:
    completed = _run([str(_pdfinfo_executable(pdftoppm)), str(pdf)])
    pages_match = re.search(r"^Pages:\s+(\d+)\s*$", completed.stdout, re.MULTILINE)
    size_match = re.search(
        r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts\s*$",
        completed.stdout,
        re.MULTILINE,
    )
    if not pages_match or not size_match:
        raise RenderError("RENDER_PDF_INVALID", "pdfinfo did not report pages and size")
    return int(pages_match.group(1)), [
        float(size_match.group(1)),
        float(size_match.group(2)),
    ]


def _validate_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise RenderError("RENDER_OUTPUT_NOT_EMPTY", str(output_dir))


@contextlib.contextmanager
def _macos_soffice_lock():
    if sys.platform != "darwin":
        yield
        return
    import fcntl

    lock_path = Path(tempfile.gettempdir()) / "ia-image-to-editable-ppt-soffice.lock"
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _new_render_temp(output_dir: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))


def render_preview(
    pptx: Path,
    output_dir: Path,
    runtime_path: Path | None,
    expected_slides: int = 1,
    *,
    preferred_font: str | None = None,
    soffice_command: str | None = None,
    pdftoppm_command: str = "pdftoppm",
    pdffonts_command: str = "pdffonts",
    pdftotext_command: str = "pdftotext",
) -> dict[str, Any]:
    pptx = pptx.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not pptx.is_file():
        raise RenderError("RENDER_INPUT_INVALID", str(pptx))
    if expected_slides < 1:
        raise RenderError("RENDER_INPUT_INVALID", "expected_slides must be positive")
    _validate_output_dir(output_dir)
    strict_runtime = runtime_path is not None
    if strict_runtime:
        runtime = _load_runtime(runtime_path)
    else:
        default_soffice = (
            "/Applications/LibreOffice.app/Contents/MacOS/soffice"
            if sys.platform == "darwin"
            else "soffice"
        )
        runtime = _draft_runtime(
            preferred_font=preferred_font or "",
            soffice=soffice_command or default_soffice,
            pdftoppm=pdftoppm_command,
            pdffonts=pdffonts_command,
            pdftotext=pdftotext_command,
        )
    initial_pptx_hash = _sha256(pptx)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    soffice = Path(runtime["executables"]["soffice"]["path"]).resolve()
    pdftoppm = Path(runtime["executables"]["pdftoppm"]["path"]).resolve()
    pdffonts = Path(runtime["executables"]["pdffonts"]["path"]).resolve()
    pdftotext = Path(runtime["executables"]["pdftotext"]["path"]).resolve()
    font_runtime = (
        validate_font_runtime(runtime["font_runtime"])
        if strict_runtime
        else None
    )
    preferred_family = (
        font_runtime["family"]
        if font_runtime is not None
        else runtime["preferred_font"]
    )
    env = (
        render_environment(runtime, os.environ.copy())
        if strict_runtime
        else os.environ.copy()
    )
    if not strict_runtime:
        env.pop("FONTCONFIG_FILE", None)
    temp_dir: Path | None = None
    attempt_count = 0
    recovered_from: str | None = None
    try:
        while True:
            attempt_count += 1
            temp_dir = _new_render_temp(output_dir)
            profile = temp_dir / "lo-profile"
            profile.mkdir()
            pdf_path = temp_dir / f"{pptx.stem}.pdf"
            try:
                with _macos_soffice_lock():
                    _run(
                        [
                            str(soffice),
                            f"-env:UserInstallation={profile.resolve().as_uri()}",
                            "--headless",
                            "--convert-to",
                            "pdf",
                            "--outdir",
                            str(temp_dir),
                            str(pptx),
                        ],
                        env=env,
                    )
            except RenderError as exc:
                macos_sigabrt_without_pdf = (
                    sys.platform == "darwin"
                    and exc.returncode == -signal.SIGABRT
                    and (
                        not pdf_path.is_file()
                        or pdf_path.stat().st_size == 0
                    )
                )
                if not strict_runtime and macos_sigabrt_without_pdf:
                    raise RenderError(
                        "RENDER_MACOS_APPLICATION_REGISTRATION_FAILED",
                        (
                            "LibreOffice aborted before producing a PDF; "
                            "run the first preview outside the macOS sandbox"
                        ),
                        returncode=exc.returncode,
                    ) from exc
                if strict_runtime and macos_sigabrt_without_pdf and attempt_count == 1:
                    if _sha256(pptx) != initial_pptx_hash:
                        raise RenderError("RENDER_INPUT_CHANGED", str(pptx)) from exc
                    shutil.rmtree(temp_dir)
                    temp_dir = None
                    recovered_from = "SIGABRT"
                    continue
                if strict_runtime and macos_sigabrt_without_pdf:
                    raise RenderError(
                        "RENDER_MACOS_APPLICATION_REGISTRATION_FAILED",
                        (
                            "LibreOffice aborted twice before producing a PDF; "
                            "run the render outside the macOS sandbox"
                        ),
                        returncode=exc.returncode,
                    ) from exc
                raise
            break

        pdf_path = temp_dir / f"{pptx.stem}.pdf"
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RenderError("RENDER_PDF_INVALID", str(pdf_path))
        pages, page_size = _inspect_pdf(pdf_path, pdftoppm)
        if pages != expected_slides:
            raise RenderError(
                "RENDER_PDF_PAGE_MISMATCH",
                f"expected {expected_slides}, got {pages}",
            )
        if any(abs(actual - expected) > 1.0 for actual, expected in zip(page_size, PAGE_SIZE)):
            raise RenderError(
                "RENDER_PDF_INVALID",
                f"unexpected page size: {page_size}",
            )

        fonts_completed = _run([str(pdffonts), str(pdf_path)])
        fonts_text_path = temp_dir / "pdffonts.txt"
        fonts_text_path.write_text(fonts_completed.stdout, encoding="utf-8")
        fonts_json_path = temp_dir / "pdffonts.json"
        resolved_fonts = _parse_pdffonts(fonts_completed.stdout)
        matched = validate_resolved_fonts(preferred_family, resolved_fonts)
        mismatches = [
            name
            for name in resolved_fonts
            if not pdf_font_name_matches(preferred_family, name)
        ]
        font_payload = {
            "expected_family": preferred_family,
            "resolved_fonts": resolved_fonts,
            "matched": matched,
            "mismatches": mismatches,
        }
        fonts_json_path.write_text(
            json.dumps(
                font_payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        preview_prefix = temp_dir / "current-preview"
        _run(
            [
                str(pdftoppm),
                "-png",
                "-singlefile",
                "-f",
                "1",
                "-l",
                "1",
                "-scale-to-x",
                str(PREVIEW_SIZE[0]),
                "-scale-to-y",
                str(PREVIEW_SIZE[1]),
                str(pdf_path),
                str(preview_prefix),
            ]
        )
        preview_path = temp_dir / "current-preview.png"
        try:
            with Image.open(preview_path) as image:
                image.load()
                if image.size != PREVIEW_SIZE:
                    raise RenderError(
                        "RENDER_PREVIEW_SIZE_MISMATCH",
                        f"expected {PREVIEW_SIZE}, got {image.size}",
                    )
                if image.convert("L").getextrema()[0] >= 245:
                    raise RenderError("RENDER_PREVIEW_INVALID", "preview is blank")
        except RenderError:
            raise
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            raise RenderError("RENDER_PREVIEW_INVALID", str(exc)) from exc

        if _sha256(pptx) != initial_pptx_hash:
            raise RenderError("RENDER_INPUT_CHANGED", str(pptx))

        final_pdf = output_dir / pdf_path.name
        final_fonts_text = output_dir / fonts_text_path.name
        final_fonts_json = output_dir / fonts_json_path.name
        final_preview = output_dir / preview_path.name
        renderer = {
            "backend": "libreoffice",
            "path": str(soffice),
            "version": runtime["executables"]["soffice"]["version"],
            "executable_sha256": runtime["executables"]["soffice"]["sha256"],
            "font_policy": "fixed" if strict_runtime else "preferred",
            "preferred_font": preferred_family,
            "isolated_profile": True,
            "attempt_count": attempt_count,
        }
        if font_runtime is not None:
            renderer["font_runtime"] = font_runtime
        if font_runtime is not None and font_runtime["provider"] == "fontconfig":
            renderer["fontconfig_path"] = runtime["fontconfig"]["path"]
            renderer["fontconfig_sha256"] = runtime["fontconfig"]["sha256"]
        if recovered_from is not None:
            renderer["recovered_from"] = recovered_from
        report = {
            "schema_version": 1,
            "pptx": {"path": str(pptx), "sha256": initial_pptx_hash},
            "renderer": renderer,
            "pdf": {
                "path": str(final_pdf),
                "sha256": _sha256(pdf_path),
                "pages": pages,
                "page_size_pt": page_size,
            },
            "font_report": {
                "path": str(final_fonts_json),
                "sha256": _sha256(fonts_json_path),
                "raw_path": str(final_fonts_text),
                "raw_sha256": _sha256(fonts_text_path),
                "expected_family": preferred_family,
                "resolved_fonts": resolved_fonts,
                "matched": matched,
                "mismatches": mismatches,
            },
            "text_extractor": {
                "path": str(pdftotext),
                "version": runtime["executables"]["pdftotext"]["version"],
                "executable_sha256": runtime["executables"]["pdftotext"]["sha256"],
            },
            "rasterizer": {
                "path": str(pdftoppm),
                "version": runtime["executables"]["pdftoppm"]["version"],
                "executable_sha256": runtime["executables"]["pdftoppm"]["sha256"],
                "output_size": list(PREVIEW_SIZE),
            },
            "preview": {
                "path": str(final_preview),
                "sha256": _sha256(preview_path),
                "size": list(PREVIEW_SIZE),
            },
        }
        report_path = temp_dir / "render-report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if output_dir.exists():
            output_dir.rmdir()
        os.replace(temp_dir, output_dir)
        return report
    except BaseException:
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--preferred-font")
    parser.add_argument("--soffice")
    parser.add_argument("--pdftoppm", default="pdftoppm")
    parser.add_argument("--pdffonts", default="pdffonts")
    parser.add_argument("--pdftotext", default="pdftotext")
    parser.add_argument("--expected-slides", type=int, default=1)
    args = parser.parse_args(argv)
    if args.runtime is None and not args.preferred_font:
        parser.error("--preferred-font is required when --runtime is omitted")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = render_preview(
            args.pptx,
            args.output_dir,
            args.runtime,
            expected_slides=args.expected_slides,
            preferred_font=args.preferred_font,
            soffice_command=args.soffice,
            pdftoppm_command=args.pdftoppm,
            pdffonts_command=args.pdffonts,
            pdftotext_command=args.pdftotext,
        )
    except RenderError as exc:
        print(
            json.dumps(
                {
                    "valid": False,
                    "error": {"code": exc.code, "detail": exc.detail},
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
