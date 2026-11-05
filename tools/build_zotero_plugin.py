"""Build the Library Sync XPI from an explicit allowlist. Dry-run by default.

No dependency installation, plugin installation, bridge launch or library access.
--limit is accepted for compatibility with repository command conventions. This
builder always creates exactly one complete package (never a truncated plugin).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "tools" / "zotero_library_sync"
VERSION = "0.2.0-beta.8"
OUTPUT = REPO / "release" / "dist" / f"library-sync-{VERSION}.xpi"
# Reserved, deliberately unresolvable hostname: this private build is updated
# manually. Never point at an invented public repository or a third-party feed.
# Zotero requires a nonempty update_url AND HTTPS; data:/file: fail its security gate.
MANUAL_UPDATE_URL = "https://library-sync.invalid/updates.json"
ALLOWLIST = (
    "bootstrap.js", "bridge-client.js", "manifest.json", "panel.css",
    "panel.js", "panel.xhtml", "picker.js", "prefs.js", "runtime.js",
)
STAMP = (2020, 1, 1, 0, 0, 0)


def validate_manifest(manifest: dict) -> None:
    target = manifest.get("applications", {}).get("zotero", {})
    # Zotero 10 Extension.sys.mjs requires all three even though the generic
    # WebExtension JSON schema does not require the Zotero-specific properties.
    for name in ("id", "update_url", "strict_max_version"):
        if not isinstance(target.get(name), str) or not target[name].strip():
            raise ValueError(f"applications.zotero.{name} not provided")
    if target["update_url"] != MANUAL_UPDATE_URL:
        raise ValueError("Private build requires the reserved HTTPS manual-update URL")
    if (manifest.get("manifest_version") != 2 or manifest.get("version") != VERSION
            or target["id"] != "library-sync@local.invalid"
            or target.get("strict_min_version") != "10.0"
            or target["strict_max_version"] != "10.0.*"):
        raise ValueError("Manifest does not match the pinned Zotero 10.0 package contract")


def build_bytes(source: Path = SOURCE) -> bytes:
    source = Path(source).resolve(strict=True)
    contents = {}
    for name in ALLOWLIST:
        path = source / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != source:
            raise ValueError(f"Missing or unsafe allowlisted source: {name}")
        contents[name] = path.read_bytes()
        contents[name].decode("utf-8")
    manifest = json.loads(contents["manifest.json"])
    validate_manifest(manifest)
    buffer = io.BytesIO()
    # ZIP_STORED avoids platform/zlib-version variation; these sources are small.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(ALLOWLIST):
            entry = zipfile.ZipInfo(name, date_time=STAMP)
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_STORED
            entry.extra = b""
            entry.comment = b""
            archive.writestr(entry, contents[name])
    return buffer.getvalue()


def nonnegative(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("--limit must be nonnegative")
    return n


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write the deterministic XPI")
    mode.add_argument("--dry-run", action="store_true", help="Validate and describe without writing (default)")
    parser.add_argument("--limit", type=nonnegative, default=0, help="Accepted for compatibility; one full XPI")
    args = parser.parse_args(argv)
    payload = build_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    result = {"mode": "apply" if args.apply else "dry-run", "output": str(OUTPUT),
              "files": list(sorted(ALLOWLIST)), "bytes": len(payload), "sha256": digest,
              "limit": args.limit, "installed": False}
    if args.apply:
        # Never follow a pre-existing junction/symlink outside this exact output folder.
        if OUTPUT.parent.resolve() != REPO / "release" / "dist" or OUTPUT.is_symlink():
            raise ValueError("Unsafe build output path")
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=OUTPUT.parent, prefix=".library-sync-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, OUTPUT)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        with zipfile.ZipFile(OUTPUT) as archive:
            if archive.testzip() or tuple(archive.namelist()) != tuple(sorted(ALLOWLIST)):
                raise ValueError("Written XPI verification failed")
        if hashlib.sha256(OUTPUT.read_bytes()).hexdigest() != digest:
            raise ValueError("Written XPI digest mismatch")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
