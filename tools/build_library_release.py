"""Build private, allowlisted source/XPI/self-contained Windows ZIPs.

Dry-run default; --apply writes only release/dist (or explicit --output).
--limit 0/1 means one complete release, never truncates files. No network,
Git commands/history, installation, credentials, sync or model calls.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "release"))
from export_rules import export_sources
from build_zotero_plugin import VERSION, build_bytes


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(files):
    return {"schema_version": 1, "version": VERSION, "files": [
        {"path": name, "bytes": len(value), "sha256": digest(value)} for name, value in sorted(files.items())]}


def zip_bytes(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, value in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100755 if name.endswith(".command") else 0o100644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(entry, value)
    return output.getvalue()


def archive_members(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        total = 0
        seen = set()
        for info in z.infolist():
            p = PurePosixPath(info.filename)
            if info.is_dir():
                continue
            if p.is_absolute() or ".." in p.parts or "\\" in info.orig_filename or "\x00" in info.orig_filename or ":" in info.filename or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Unsafe dependency archive member")
            if info.filename.casefold() in seen:
                raise ValueError("Duplicate dependency archive member")
            seen.add(info.filename.casefold())
            total += info.file_size
            if total > 700 * 1024 * 1024:
                raise ValueError("Dependency expansion exceeds bound")
            yield info.filename, z.read(info)


def runtime_files(repo, vendor):
    lock = json.loads((repo / "release/dependencies.lock.json").read_text(encoding="utf-8"))
    result, notices = {}, []
    for row in lock["dependencies"]:
        filename = row["filename"]
        if Path(filename).name != filename:
            raise ValueError("Unsafe dependency filename")
        file = vendor / filename
        if file.is_symlink() or file.resolve() != file.absolute():
            raise ValueError("Unsafe dependency path")
        value = file.read_bytes()
        if digest(value) != row["sha256"]:
            raise ValueError("Dependency SHA256 mismatch: " + row["name"])
        license_files = []
        for name, content in archive_members(value):
            if row["kind"] == "python":
                dest = "runtime/" + name
            else:
                parts = name.split("/")
                if parts[0].endswith(".data"):
                    if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                        raise ValueError("Wheel requires unsupported installation step: " + name)
                    name = "/".join(parts[2:])
                dest = "runtime/Lib/site-packages/" + name
            if dest in result:
                raise ValueError("Overlapping dependency file: " + dest)
            result[dest] = content
            leaf = PurePosixPath(name).name.lower()
            if any(word in leaf for word in ("license", "copying", "notice")):
                notice = "THIRD_PARTY_LICENSES/" + row["name"] + "/" + name
                result[notice] = content
                license_files.append(notice)
        if not license_files:
            raise ValueError("Dependency license text missing: " + row["name"])
        notices.append({**row, "license_files": license_files})
    pth = "runtime/python313._pth"
    if pth not in result:
        raise ValueError("Pinned Python layout changed")
    result[pth] = b"python313.zip\n.\nLib/site-packages\n../tools\n../release\nimport site\n"
    result["runtime/sitecustomize.py"] = (
        b"import sys, os\nsys.dont_write_bytecode = True\n"
        b"if os.environ.get('LIBRARY_SYNC_OFFLINE_CHECK') == '1':\n"
        b"    from offline_smoke import offline_guard\n    offline_guard()\n")
    result["THIRD_PARTY_MANIFEST.json"] = json.dumps({"schema_version": 1, "dependencies": notices,
        "project_license": "MIT", "distribution_license": "AGPL-3.0-only",
        "redistribution": "Corresponding sources and notices required; see THIRD_PARTY_SOURCE_GUIDE.md"}, ensure_ascii=False, indent=2).encode() + b"\n"
    return result


def build(repo=REPO, vendor=None, *, source_only=False, macos=False):
    repo = Path(repo).resolve()
    files = export_sources(repo)
    # The NOT_PUBLISHED tripwire describes local review artifacts; published
    # archives must not carry a statement that contradicts their own release.
    published = {name: value for name, value in files.items() if name != "NOT_PUBLISHED"}
    xpi = build_bytes(repo / "tools/zotero_library_sync")
    published["source.sha256"] = "".join(f"{digest(value)}  {name}\n" for name, value in sorted(published.items())).encode()
    outputs = {f"library-sync-{VERSION}-source.zip": zip_bytes(published), f"library-sync-{VERSION}.xpi": xpi}
    if not source_only:
        app = dict(published)
        app.update(runtime_files(repo, Path(vendor or repo / "release/vendor").resolve()))
        app[f"addon/library-sync-{VERSION}.xpi"] = xpi
        app["FILES.json"] = json.dumps(inventory(app), indent=2).encode() + b"\n"
        bundle = {"app/" + name: data for name, data in app.items()}
        bundle["setup.ps1"] = published["release/setup.ps1"]
        bundle["README.md"] = published["README.md"].replace(b"](release/", b"](app/release/")
        bundle["LICENSE"] = published["LICENSE"]
        bundle["DISTRIBUTION_LICENSE.md"] = published["release/DISTRIBUTION_LICENSE.md"]
        bundle["bundle.sha256"] = "".join(f"{digest(value)}  {name}\n" for name, value in sorted(bundle.items())).encode()
        outputs[f"library-sync-{VERSION}-windows-x64.zip"] = zip_bytes(bundle)
    if macos:
        app = dict(published)
        notices = []
        lock = json.loads((repo / "release/dependencies.macos.lock.json").read_text())
        for package in lock["dependencies"]:
            license_files = []
            for wheel in package["wheels"]:
                data = (Path(vendor or repo / "release/vendor") / "macos" / wheel["filename"]).read_bytes()
                if digest(data) != wheel["sha256"]:
                    raise ValueError("macOS dependency checksum failed")
                app["release/macos-wheels/" + wheel["filename"]] = data
                for name, content in archive_members(data):
                    if any(word in PurePosixPath(name).name.lower() for word in ("license", "copying", "notice")):
                        dest = "THIRD_PARTY_LICENSES/" + package["name"] + "/" + name
                        if dest in app and app[dest] != content:
                            raise ValueError("Different wheel notices for same dependency path")
                        app[dest] = content
                        if dest not in license_files:license_files.append(dest)
            if not license_files:raise ValueError("Dependency license text missing")
            notices.append({**package, "license_files": license_files})
        app["THIRD_PARTY_MANIFEST.json"] = json.dumps({"project_license": "MIT", "distribution_license": "AGPL-3.0-only", "dependencies": notices}, indent=2).encode()
        app[f"addon/library-sync-{VERSION}.xpi"] = xpi
        app["FILES.json"] = json.dumps(inventory(app), indent=2).encode() + b"\n"
        bundle = {"app/" + name: data for name,data in app.items()}
        bundle["setup-macos.command"] = published["release/setup-macos.command"]
        bundle["README.md"] = published["release/MACOS.md"]
        bundle["LICENSE"] = published["LICENSE"]
        bundle["DISTRIBUTION_LICENSE.md"] = published["release/DISTRIBUTION_LICENSE.md"]
        bundle["bundle.sha256"] = "".join(f"{digest(value)}  {name}\n" for name,value in sorted(bundle.items())).encode()
        outputs[f"library-sync-{VERSION}-macos.zip"] = zip_bytes(bundle)
    outputs["SHA256SUMS.txt"] = "".join(f"{digest(value)}  {name}\n" for name, value in sorted(outputs.items())).encode()
    return outputs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, choices=(0, 1), default=1)
    ap.add_argument("--source-only", action="store_true")
    ap.add_argument("--macos", action="store_true", help="Also build macOS offline wheels/bootstrap package")
    ap.add_argument("--vendor", type=Path, default=REPO / "release/vendor")
    ap.add_argument("--output", type=Path, default=REPO / "release/dist")
    args = ap.parse_args(argv)
    outputs = build(vendor=args.vendor, source_only=args.source_only, macos=args.macos)
    if args.apply:
        target = args.output.absolute()
        if target.resolve() != target or target == Path(target.anchor):
            raise ValueError("Unsafe release output path")
        target.mkdir(parents=True, exist_ok=True)
        for name, data in outputs.items():
            file = target / name
            if file.is_symlink() or (file.exists() and not file.is_file()):
                raise ValueError("Unsafe release output")
            file.write_bytes(data)
            if digest(file.read_bytes()) != digest(data):
                raise ValueError("Written release hash mismatch")
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "status": "NOT_PUBLISHED",
        "license": "MIT", "distribution_license": "AGPL-3.0-only", "outputs": [{"name": n, "bytes": len(v), "sha256": digest(v)}
        for n, v in sorted(outputs.items())]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
