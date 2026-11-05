"""Explicit source allowlist and reviewed standalone compatibility transforms.

This is not a repository copy with exclusions. Every source input is named.
Transforms touch only exported bytes; source checkout and personal data stay put.
"""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import re

RUNTIME_MODULES = (
    "attachments_audit", "attachments_normalize_prep", "citekey_reformat_prep",
    "claude_client", "codex_client", "common", "fix_latex_spacing", "glm_client",
    "glm_concurrency", "intake_new", "library_control", "library_discovery", "library_intake",
    "library_paths", "library_plugin_bridge", "library_power", "library_progress",
    "library_qa", "library_resume", "library_sync", "library_source", "library_refresh", "mineru_client",
    "mineru_credentials", "model_api", "model_catalog", "qc_glm", "sync", "zotero_local",
)
PLUGIN_FILES = ("bootstrap.js", "bridge-client.js", "manifest.json", "panel.css", "panel.js",
                "panel.xhtml", "picker.js", "prefs.js", "runtime.js")
PLUGIN_TESTS = ("helpers.cjs", "ui.test.cjs", "picker.test.cjs", "protocol.test.cjs", "runtime.test.cjs", "model-parameters.test.cjs", "source-refresh.test.cjs", "model-catalog.test.cjs", "tongji.test.cjs")
RELEASE_FILES = ("README.md", "LICENSE_PENDING.md", "PRIVACY.md", "BUILD.md", "CHANGELOG.md",
                 "setup.ps1", "install.py", "offline_smoke.py", "fetch_dependencies.py",
                 "export_rules.py", "dependencies.lock.json", "requirements.txt",
                 "DATA.gitignore", "LIBRARY.gitignore", "THIRD_PARTY_SOURCE_GUIDE.md")
SOURCE_ALLOWLIST = (tuple("tools/" + x + ".py" for x in RUNTIME_MODULES)
                   + tuple("tools/zotero_library_sync/" + x for x in PLUGIN_FILES)
                   + tuple("tools/zotero_library_sync/tests/" + x for x in PLUGIN_TESTS)
                   + ("tools/notify_library.ps1", "tools/build_zotero_plugin.py",
                      "tools/build_library_release.py", "tools/test_library_release.py",
                      "tools/test_library_progress.py", "tools/test_qc_protocol.py", "tools/test_model_pipeline.py",
                      "tools/test_library_source.py", "tools/test_model_catalog.py", "tools/test_model_api.py", "tools/test_model_profiles.py")
                   + ("release/test_export_core.py", "release/BBT_SETUP.md", "release/journal_acronyms.example.csv")
                   + tuple("release/" + x for x in RELEASE_FILES))

def scan_public(name, data, markers=()):
    text = data.decode("utf-8-sig")
    plain = text.replace("\\\\", "\\")
    if any(value.lower() in plain.lower() for value in markers):
        raise ValueError("Personal path/item marker in allowlisted export: " + name)
    if re.search(r"[A-Za-z]:\\Users\\(?!Synthetic\\|Test\\|Public\\|<user>\\)[^\\\s\"']+\\", plain, re.I):
        raise ValueError("Personal user directory in allowlisted export: " + name)
    if name.startswith("tools/") and Path(name).stem in RUNTIME_MODULES and name.endswith(".py"):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if (re.fullmatch(r"[A-Z0-9]{8}", value) and value not in {"PROGRESS"}) or re.search(r"20\d{6}_\d{6}_[0-9a-f]{6}", value) or re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError("Unreviewed item key/run ID/hash constant in runtime: " + name)
    if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}", text):
        raise ValueError("Credential-like content in allowlisted export: " + name)


def export_sources(repo):
    repo = Path(repo).resolve()
    result, provenance = {}, []
    for name in SOURCE_ALLOWLIST:
        path = repo / name
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("Missing/unsafe allowlisted input: " + name)
        content = path.read_bytes()
        scan_public(name, content)
        result[name] = content
        provenance.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(),
                           "bytes": len(content)})
    result["README.md"] = result["release/README.md"]
    for document in ("BUILD.md", "PRIVACY.md", "LICENSE_PENDING.md", "BBT_SETUP.md", "THIRD_PARTY_SOURCE_GUIDE.md"):
        result["README.md"] = result["README.md"].replace(f"]({document})".encode(), f"](release/{document})".encode())
    result["LICENSE_PENDING"] = b"LicensePending\nNo project open-source license has been selected. Do not infer MIT.\n"
    result["RELEASE.json"] = json.dumps({"version": "0.2.0-beta.8", "status": "public-beta",
        "license": "LicensePending", "platform": "windows-x86_64", "portable_defaults": True,
        "git": "system Git required; explicit installation setup", "real_sync_verified": False}, indent=2).encode() + b"\n"
    result["SOURCE_MANIFEST.json"] = json.dumps({"schema_version": 1, "export": "explicit-allowlist",
        "git_history_included": False, "files": provenance}, indent=2).encode() + b"\n"
    result[".gitignore"] = b"/runtime/\n/release/vendor/\n/release/dist/\n/library-sync.json\n/.secrets/\n/reports/\n__pycache__/\n*.pyc\n"
    return result
