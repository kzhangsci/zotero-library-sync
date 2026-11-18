"""Read-only installed-runtime checks. --smoke uses synthetic temporary files.

No API, credential reads, Git, sync, background service, or real model calls.
--apply is deliberately unsupported; --limit=0/1 bounds one acceptance run.
"""
from __future__ import annotations
import argparse
import importlib
import json
from pathlib import Path
import sys
import tempfile


def offline_guard():
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            raise RuntimeError("Offline smoke forbids network and subprocesses")
        if event == "open" and args and isinstance(args[0], (str, bytes)):
            value = str(args[0]).replace("\\", "/").lower()
            if "/.secrets/" in value or value.endswith(("/prefs.js", "/zotero.sqlite")):
                raise RuntimeError("Offline smoke forbids credential/profile/database reads")
    sys.addaudithook(audit)


def smoke():
    offline_guard()
    import library_paths as paths
    import common
    import library_sync
    import sync
    import model_api
    import mineru_credentials
    import glm_concurrency
    import library_qa
    import library_discovery
    import intake_new
    import citekey_reformat_prep
    import attachments_normalize_prep
    import fitz
    import requests
    import yaml
    assert paths.REPO != paths.DATA, "Code and data must be separate"
    assert common.REPORTS == paths.REPORTS
    assert library_sync.LIBRARY == paths.LIBRARY == sync.LIB == library_qa.LIBRARY == intake_new.LIB
    assert model_api.DEFAULT_CONFIG == paths.SECRETS / "model_api.dpapi"
    assert mineru_credentials.DEFAULT_PATH == paths.SECRETS / "mineru_api.dpapi"
    assert glm_concurrency.ROOT.is_relative_to(paths.DATA)
    assert not citekey_reformat_prep.PINS and not attachments_normalize_prep.EXPLICIT_RENAMES
    # beta.15: the distribution ships a PUBLIC base acronym table next to the
    # code (release/journal_acronyms.base.csv) with curated filename/citekey
    # short codes for common oceanography/geoscience journals — explicitly NOT
    # ISO 4 abbreviations. A fresh install's user override table starts
    # header-only, so the effective table equals the base table; the synthetic
    # example row must never leak into it.
    assert intake_new.BASE_TABLE.is_file(), "base acronym table missing from install"
    effective = intake_new.acronym_table()
    assert effective.get("geoscientific model development") == "GMD"
    assert effective.get("journal of physical oceanography") == "JPO"
    assert effective.get("journal of geophysical research: oceans") == "JGR"
    assert effective.get("scientific data") == "SciData"
    assert "synthetic journal of example studies" not in effective, "example row leaked into effective table"
    ledger = library_discovery.load(library_sync.RUNS)
    assert ledger["since"].startswith(paths.BASELINE_DATE) and ledger["entries"] == {}
    class SyntheticClient:
        def list_all(self, path):
            assert path == "items/top"
            return [{"key": "OLDTEST1", "data": {"itemType": "journalArticle", "dateAdded": "1900-01-01T00:00:00Z"}},
                    {"key": "NEWTEST1", "data": {"itemType": "journalArticle", "dateAdded": paths.BASELINE_DATE + "T12:00:00Z"}}]
    discovered = library_discovery.discover(SyntheticClient(), library_sync.RUNS, limit=1)
    assert discovered["selected"] == ["NEWTEST1"], "Old library catch-up must remain excluded"
    with tempfile.TemporaryDirectory(prefix="library-sync-synthetic-") as folder:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Library Sync synthetic PDF")
        file = Path(folder) / "synthetic.pdf"
        doc.save(file)
        doc.close()
        with fitz.open(file) as check:
            assert "synthetic PDF" in check[0].get_text()
        assert yaml.safe_load("synthetic: true")["synthetic"] is True
    modules = ["library_plugin_bridge", "library_control", "library_intake", "library_resume", "qc_glm", "mineru_client", "zotero_local"]
    for name in modules:
        importlib.import_module(name)
    return {"result": "PASS", "python": sys.version.split()[0], "pdf": fitz.VersionBind, "mupdf": fitz.VersionFitz,
            "requests": requests.__version__, "yaml": yaml.__version__, "baseline_explicit": True,
            "old_library_catchup_excluded": True,
            "code_data_separate": True, "credentials_read": False, "network": False,
            "sync_started": False, "models_called": False, "modules_imported": len(modules) + 12}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--config", action="store_true", help="Read public installation paths only; never model credentials")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", choices=(0, 1), type=int, default=1)
    args = ap.parse_args(argv)
    if args.config:
        offline_guard()
        from library_paths import get_paths
        p = get_paths()
        value = {"repo": str(p.repo), "data": str(p.data), "library": str(p.library),
                 "baseline_date": p.baseline_date, "git_path": str(p.git_path) if p.git_path else None,
                 "credentials_read": False, "network": False}
    else:
        value = smoke() if args.smoke and not args.dry_run else {"mode": "dry-run", "network": False, "credentials_read": False}
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
