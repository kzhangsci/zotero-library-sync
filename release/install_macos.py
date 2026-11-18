"""macOS user installation using CPython 3.13 and offline, hash-pinned wheels."""
import argparse
import json
import platform
from pathlib import Path
import subprocess
import sys
import venv

from install import install, verify


def prerequisites():
    if sys.platform != "darwin":
        raise RuntimeError("This installer requires macOS")
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError("Install CPython 3.13 from python.org, then run setup again")
    if tuple(int(v) for v in platform.mac_ver()[0].split(".")[:1]) < (13,):
        raise RuntimeError("macOS 13 or later is required")
    if platform.machine() not in ("arm64", "x86_64"):
        raise RuntimeError("Unsupported Python architecture")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    p.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--install-dir", type=Path, default=Path.home()/"Library/Application Support/LibrarySync")
    p.add_argument("--library-dir", type=Path, required=True)
    p.add_argument("--baseline-date", required=True)
    p.add_argument("--git-path", type=Path)
    p.add_argument("--init-library-git", action="store_true")
    p.add_argument("--git-user-name", required=True)
    p.add_argument("--git-user-email", required=True)
    args = p.parse_args(argv)
    prerequisites()
    # Check the entire bundle before any application/state installation.
    verify(args.source)
    result = install(args.source, args.install_dir, args.library_dir, args.baseline_date,
                     apply=args.apply, git_path=args.git_path, init_git=args.init_library_git,
                     git_user_name=args.git_user_name, git_user_email=args.git_user_email)
    result.update(platform="macos", runtime="CPython 3.13 private venv", network=False)
    if args.apply:
        app = Path(result["app"])
        receipt = Path(args.install_dir)/"INSTALL_RECEIPT.json"
        result["runtime_ready"] = False
        receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
        venv.EnvBuilder(with_pip=True, symlinks=False).create(app/"runtime")
        python = app/"runtime/bin/python3"
        subprocess.run([str(python), "-m", "pip", "--isolated", "install", "--no-index",
                        "--find-links", str(app/"release/macos-wheels"), "--require-hashes",
                        "--only-binary=:all:", "--disable-pip-version-check", "-r",
                        str(app/"release/requirements-macos.lock")], check=True, timeout=180)
        subprocess.run([str(python), "-c", "import fitz,psutil,requests,yaml; print('Runtime dependencies ready')"],
                       check=True, timeout=30)
        result.update(runtime_ready=True, python=str(python))
        receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
