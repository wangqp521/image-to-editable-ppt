#!/usr/bin/env python3
"""Move one copied page spec from pending to a profile-specific final state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


AUTHORING_TEMPLATE_VERSION = @@AUTHORING_TEMPLATE_VERSION@@
PAGE_ID = @@PAGE_ID@@
VERIFICATION_PROFILE = @@VERIFICATION_PROFILE@@
PAGE_DIR = Path(__file__).resolve().parent
SPEC_PATH = PAGE_DIR / "work" / "page-reconstruction.json"
ALLOWED_BY_PROFILE = {
    "rapid": {"rapid_validated", "rapid_validation_failed"},
    "reviewed": {"reviewed_passed", "reviewed_failed"},
}


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "status",
        choices=sorted(set().union(*ALLOWED_BY_PROFILE.values())),
    )
    args = parser.parse_args(argv)

    payload = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    if payload.get("page_id") != PAGE_ID or payload.get("verification_profile") != VERIFICATION_PROFILE:
        raise SystemExit("AUTHORING_FINAL_STATE_INVALID: page/profile does not match copied finalizer")
    if payload.get("delivery_status") != "pending":
        raise SystemExit("AUTHORING_FINAL_STATE_INVALID: finalizer only accepts pending specs")
    if args.status not in ALLOWED_BY_PROFILE[VERIFICATION_PROFILE]:
        raise SystemExit("AUTHORING_FINAL_STATE_INVALID: status does not match verification profile")

    before_layout = {key: value for key, value in payload.items() if key != "delivery_status"}
    before_hash = _canonical_hash(before_layout)
    payload["delivery_status"] = args.status
    after_layout = {key: value for key, value in payload.items() if key != "delivery_status"}
    after_hash = _canonical_hash(after_layout)
    if before_hash != after_hash:
        raise SystemExit("AUTHORING_FINAL_STATE_INVALID: layout changed while finalizing")
    _write_json_atomic(SPEC_PATH, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(SPEC_PATH),
                "page_id": PAGE_ID,
                "delivery_status": args.status,
                "layout_sha256": after_hash,
                "authoring_template_version": AUTHORING_TEMPLATE_VERSION,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
