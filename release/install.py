"""Offline per-user installer. Dry-run default; --apply installs; --limit=0/1.

No elevation, model/API calls, registry, scheduled tasks or credential reads.
Git init is explicit; no commits, global settings, remotes or network operations.
Never replaces an existing app or an existing installation's state.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess


def safe_member(root, name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name or not name:
        raise ValueError("Unsafe manifest member")
    root = Path(root).resolve()
    dest = root.joinpath(*p.parts)
    if not dest.resolve().is_relative_to(root) or dest.is_symlink():
        raise ValueError("Manifest member escapes installation root")
    cursor = dest
    while cursor != root:
        if cursor.exists() and (cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction())):
            raise ValueError("Reparse points are not accepted")
        cursor = cursor.parent
    return dest


def verify(root):
    manifest = json.loads((root / "FILES.json").read_text(encoding="utf-8"))
    names = set()
    for row in manifest["files"]:
        if row["path"].casefold() in names:
            raise ValueError("Duplicate manifest member")
        names.add(row["path"].casefold())
        path = safe_member(root, row["path"])
        if not path.is_file() or path.stat().st_size != row["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Installation integrity check failed: " + row["path"])
    return manifest


def _clean_target(path):
    path = Path(path).absolute()
    cursor = path
    while True:
        if cursor.exists() and (cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction())):
            raise ValueError("Installation paths must not use symlinks/junctions")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if path == Path(path.anchor):
        raise ValueError("A drive root is not an installation directory")
    return path.resolve()


def git_command(path, *args, cwd=None):
    result = subprocess.run([str(path), *args], cwd=cwd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("Git prerequisite/setup command failed; no sync started")
    return result.stdout.strip()


def git_prerequisite(library, git_path=None, init_git=False, user_name=None, user_email=None):
    candidate = str(git_path) if git_path else shutil.which("git")
    if not candidate:
        raise ValueError("System Git is required. Install system Git or pass --git-path.")
    git = Path(candidate).resolve()
    if not git.is_file() or git.name.lower() not in {"git.exe", "git"}:
        raise ValueError("--git-path must point to the Git executable, not a command")
    version = git_command(git, "--version")
    if not version.startswith("git version "):
        raise ValueError("Git executable did not identify itself")
    existing = (library / ".git").exists()
    if existing:
        root = Path(git_command(git, "-C", str(library), "rev-parse", "--show-toplevel")).resolve()
        if root != library:
            raise ValueError("Selected Library is not the root of its Git repository")
    elif not init_git:
        raise ValueError("Library has no Git repository. Explicit --init-library-git is required; existing data will not be staged or committed.")
    if not user_name or not user_email:
        raise ValueError("New DATA Git requires explicit --git-user-name and --git-user-email (local repositories only).")
    if any(c in (user_name or "") + (user_email or "") for c in "\r\n\x00"):
        raise ValueError("Git identity must be single-line text")
    return git, existing, version


def install(source, target, library, baseline, *, apply=False, git_path=None,
            init_git=False, git_user_name=None, git_user_email=None):
    source, target, library = Path(source).resolve(), _clean_target(target), _clean_target(library)
    try:
        if dt.date.fromisoformat(baseline).isoformat() != baseline:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("An explicit baseline date YYYY-MM-DD is required") from exc
    app, data = target / "app", target / "data"
    for protected in (source, app, data):
        if library.is_relative_to(protected) or protected.is_relative_to(library):
            raise ValueError("Library must be separate from source, application and state directories")
    if app.exists() or data.exists():
        raise ValueError("Existing app/data found; use a fresh install directory. No files overwritten.")
    for name in ("papers", "indexes", "resources"):
        _clean_target(library / name)
    git, existing_git, git_version = git_prerequisite(library, git_path, init_git, git_user_name, git_user_email)
    manifest = verify(source)
    plan = {"mode": "apply" if apply else "dry-run", "version": manifest["version"],
            "app": str(app), "data": str(data), "library": str(library), "baseline_date": baseline,
            "files": len(manifest["files"]), "state": "NOT_PUBLISHED", "license": "MIT", "distribution_license": "AGPL-3.0-or-later",
            "sync_started": False, "network": False, "git_path": str(git), "git_version": git_version,
            "git_init_requested": bool(init_git), "git_initialized": False}
    if not apply:
        return plan
    # mkdir(exist_ok=False) protects against accidental replacement and races.
    target.mkdir(parents=True, exist_ok=True)
    app.mkdir(exist_ok=False)
    for row in manifest["files"]:
        src, dest = safe_member(source, row["path"]), safe_member(app, row["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as output:
            output.write(src.read_bytes())
    shutil.copyfile(source / "FILES.json", app / "FILES.json")
    verify(app)
    data.mkdir(exist_ok=False)
    # Private report history is separate from the immutable source installation.
    (data / ".gitignore").write_bytes(Path(__file__).with_name("DATA.gitignore").read_bytes())
    git_command(git, "-c", "init.templateDir=", "init", "--initial-branch=main", str(data))
    git_command(git, "-C", str(data), "config", "--local", "user.name", git_user_name)
    git_command(git, "-C", str(data), "config", "--local", "user.email", git_user_email)
    plan["data_git_initialized"] = True
    reports = data / "reports"
    runs = reports / "_cache" / "library_sync"
    runs.mkdir(parents=True)
    (reports / "journal_acronyms.csv").write_text("title,proposed\n", encoding="utf-8")
    ledger = {"schema": 1, "since": baseline + "T00:00:00+00:00", "entries": {}}
    (runs / "discovery.json").write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    for name in ("papers", "indexes", "resources"):
        (library / name).mkdir(parents=True, exist_ok=True)
    if not existing_git:
        ignore = library / ".gitignore"
        if not ignore.exists():
            with ignore.open("xb") as f:
                f.write(Path(__file__).with_name("LIBRARY.gitignore").read_bytes())
            plan["library_ignore_created"] = True
        else:
            plan["library_ignore_created"] = False
        git_command(git, "-c", "init.templateDir=", "init", "--initial-branch=main", str(library))
        git_command(git, "-C", str(library), "config", "--local", "user.name", git_user_name)
        git_command(git, "-C", str(library), "config", "--local", "user.email", git_user_email)
        plan["git_initialized"] = True
    glossary = library / "resources" / "glossary.md"
    if not glossary.exists():
        glossary.write_text("# Translation glossary\n\nAdd your own terminology here.\n", encoding="utf-8")
    config = {"schema_version": 1, "data_dir": str(data), "library_dir": str(library),
              "baseline_date": baseline, "git_path": str(git)}
    with (app / "library-sync.json").open("x", encoding="utf-8") as f:
        f.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    (target / "INSTALL_RECEIPT.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, choices=(0, 1), default=1)
    ap.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--install-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LibrarySync")
    ap.add_argument("--library-dir", type=Path, required=True)
    ap.add_argument("--baseline-date", required=True)
    ap.add_argument("--git-path", type=Path)
    ap.add_argument("--init-library-git", action="store_true")
    ap.add_argument("--git-user-name")
    ap.add_argument("--git-user-email")
    args = ap.parse_args(argv)
    print(json.dumps(install(args.source, args.install_dir, args.library_dir, args.baseline_date, apply=args.apply,
        git_path=args.git_path, init_git=args.init_library_git, git_user_name=args.git_user_name,
        git_user_email=args.git_user_email), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
