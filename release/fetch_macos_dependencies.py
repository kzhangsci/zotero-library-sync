"""Fetch only the pinned official macOS wheels. Dry-run unless --apply."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request


def fetch(root, apply=False):
    root = Path(root)
    lock = json.loads((root / "dependencies.macos.lock.json").read_text())
    target = root / "vendor" / "macos"
    count = 0
    for package in lock["dependencies"]:
        for wheel in package["wheels"]:
            url = urllib.parse.urlsplit(wheel["url"])
            name = wheel["filename"]
            if url.scheme != "https" or url.hostname != "files.pythonhosted.org" or Path(name).name != name:
                raise ValueError("Unexpected dependency URL/path")
            if apply:
                target.mkdir(parents=True, exist_ok=True)
                file = target / name
                data = file.read_bytes() if file.is_file() else urllib.request.urlopen(wheel["url"], timeout=45).read()
                if hashlib.sha256(data).hexdigest() != wheel["sha256"]:
                    raise ValueError("Dependency checksum failed: " + name)
                if not file.exists():
                    with file.open("xb") as out:
                        out.write(data)
            count += 1
    return {"mode": "apply" if apply else "dry-run", "wheels": count, "target": str(target)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(fetch(Path(__file__).resolve().parent, args.apply)))
