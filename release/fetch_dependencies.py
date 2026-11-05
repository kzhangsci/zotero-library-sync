"""Fetch pinned public Windows dependencies. Dry-run default; --apply downloads.

Only python.org/PyPI publisher assets; no pip config, auth, models, GitHub or
application credentials. --limit caps downloads, never produces a partial build.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
HOSTS = {"www.python.org", "pypi.org", "files.pythonhosted.org"}
PYTHON = {"name": "CPython", "version": "3.13.17", "filename": "python-3.13.17-embed-amd64.zip",
          "url": "https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip",
          "sha256": "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38",
          "license": "PSF-2.0 and bundled third-party notices", "kind": "python",
          "source": "https://www.python.org/downloads/release/python-31317/"}
PACKAGES = {"PyMuPDF": "1.26.7", "PyYAML": "6.0.3", "requests": "2.34.2",
            "urllib3": None, "certifi": None, "charset-normalizer": None, "idna": None}


def approved(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.hostname not in HOSTS or p.username or p.password or p.query or p.fragment:
        raise ValueError("Only unauthenticated official dependency URLs are allowed")
    return url


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        approved(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, maximum=150 * 1024 * 1024):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), OfficialRedirect())
    with op.open(urllib.request.Request(approved(url), headers={"User-Agent": "LibrarySync-ReleaseBuilder/0.2"}), timeout=45) as response:
        value = response.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("Dependency download exceeds limit")
    return value


def wheel_score(filename):
    if not filename.endswith(".whl"):
        return -1
    if filename.endswith("-py3-none-any.whl") or filename.endswith("-py2.py3-none-any.whl"):
        return 1
    if filename.endswith("-cp313-cp313-win_amd64.whl"):
        return 3
    if re.search(r"-cp3(?:9|10|11|12|13)-abi3-win_amd64.whl$", filename):
        return 2
    return -1


def resolve():
    rows = [PYTHON]
    for name, version in PACKAGES.items():
        endpoint = f"https://pypi.org/pypi/{name}/" + (f"{version}/" if version else "") + "json"
        metadata = json.loads(fetch(endpoint, 10 * 1024 * 1024))
        wheels = [x for x in metadata["urls"] if not x.get("yanked") and wheel_score(x["filename"]) >= 0]
        if not wheels:
            raise ValueError(f"No pinned Windows CPython 3.13 wheel for {name}")
        w = max(wheels, key=lambda x: wheel_score(x["filename"]))
        info = metadata["info"]
        rows.append({"name": info["name"], "version": info["version"], "filename": w["filename"],
                     "url": approved(w["url"]), "sha256": w["digests"]["sha256"], "kind": "wheel",
                     "license": info.get("license_expression") or info.get("license") or "ReviewRequired",
                     "requires_dist": info.get("requires_dist") or [], "source": endpoint})
    return {"schema_version": 1, "platform": "windows-x86_64-cp313", "dependencies": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cache", type=Path, default=ROOT / "vendor")
    args = ap.parse_args(argv)
    if args.limit < 0:
        ap.error("--limit must be nonnegative")
    lock = ROOT / "dependencies.lock.json"
    if not args.apply:
        print(json.dumps({"mode": "dry-run", "network": False, "lock_present": lock.is_file(),
                          "python": PYTHON["version"], "packages": PACKAGES, "limit": args.limit}, indent=2))
        return 0
    if args.cache.exists() and args.cache.resolve() != args.cache.absolute():
        raise ValueError("Cache must not be a symlink or junction")
    manifest = json.loads(lock.read_text(encoding="utf-8")) if lock.is_file() else resolve()
    args.cache.mkdir(parents=True, exist_ok=True)
    count = 0
    for row in manifest["dependencies"]:
        if Path(row["filename"]).name != row["filename"]:
            raise ValueError("Unsafe dependency filename")
        dest = args.cache / row["filename"]
        if dest.is_symlink():
            raise ValueError("Unsafe dependency destination")
        if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == row["sha256"]:
            continue
        if args.limit and count >= args.limit:
            break
        content = fetch(row["url"])
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("Dependency SHA256 does not match official metadata")
        dest.write_bytes(content)
        count += 1
        print(f"Verified {row['name']} {row['version']}", flush=True)
    # Public metadata only; never records local machine or credential paths.
    lock.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
