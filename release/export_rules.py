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
    "library_issues", "library_remedy", "library_platform",
    "library_paths", "library_plugin_bridge", "library_power", "library_progress",
    "library_qa", "library_resume", "library_sync", "library_source", "library_refresh", "mineru_client",
    "mineru_credentials", "model_api", "model_catalog", "qc_glm", "report_retention", "sync", "zotero_local",
)
PLUGIN_FILES = ("bootstrap.js", "bridge-client.js", "manifest.json", "panel.css", "panel.js",
                "panel.xhtml", "picker.js", "prefs.js", "runtime.js")
PLUGIN_TESTS = ("helpers.cjs", "ui.test.cjs", "ui-beta15.test.cjs", "picker.test.cjs", "protocol.test.cjs", "runtime.test.cjs", "model-parameters.test.cjs", "source-refresh.test.cjs", "model-catalog.test.cjs", "tongji.test.cjs", "model-switch.test.cjs", "zcode-plan.test.cjs")
RELEASE_FILES = ("README.md", "DISTRIBUTION_LICENSE.md", "AGPL-3.0.txt", "MACOS.md",
                 "install_macos.py", "setup-macos.command", "fetch_macos_dependencies.py",
                 "dependencies.macos.lock.json", "requirements-macos.lock", "PRIVACY.md", "BUILD.md", "CHANGELOG.md",
                 "setup.ps1", "install.py", "offline_smoke.py", "fetch_dependencies.py",
                 "export_rules.py", "dependencies.lock.json", "requirements.txt",
                 "DATA.gitignore", "LIBRARY.gitignore", "THIRD_PARTY_SOURCE_GUIDE.md",
                 "journal_acronyms.base.csv")
SOURCE_ALLOWLIST = (("LICENSE", "tools/test_library_platform.py", "tools/test_macos_install.py", "tools/macos_acceptance.py", ".github/workflows/macos-beta16.yml") + tuple("tools/" + x + ".py" for x in RUNTIME_MODULES)
                   + tuple("tools/zotero_library_sync/" + x for x in PLUGIN_FILES)
                   + tuple("tools/zotero_library_sync/tests/" + x for x in PLUGIN_TESTS)
                   + ("tools/notify_library.ps1", "tools/build_zotero_plugin.py",
                      "tools/build_library_release.py", "tools/test_library_release.py",
                      "tools/test_library_progress.py", "tools/test_library_qa.py", "tools/test_qc_protocol.py", "tools/test_model_pipeline.py",
                      "tools/test_library_source.py", "tools/test_model_catalog.py", "tools/test_model_api.py", "tools/test_model_profiles.py",
                      "tools/test_library_issues.py", "tools/test_library_remedy.py",
                      "tools/test_library_control_beta15.py", "tools/test_plugin_bridge_beta15.py")
                   + ("release/test_export_core.py", "release/BBT_SETUP.md", "release/journal_acronyms.example.csv")
                   + tuple("release/" + x for x in RELEASE_FILES))

# Top-level constants only. Private tables are deliberately replaced, never copied.
CONSTANTS = {
    "library_paths": {"LEGACY_DEFAULTS": "{}"},
    "attachments_normalize_prep": {"EXPLICIT_RENAMES": "{}"},
    "library_resume": {"LEGACY_RUN": "None", "LEGACY_PAPERS": "{}"},
}


class StripNarrative(ast.NodeTransformer):
    def generic_visit(self, node):
        super().generic_visit(node)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                node.body.pop(0)
            if not node.body:
                node.body = [ast.Pass()]
        return node


def portable_python(name, text):
    if name == "citekey_reformat_prep":
        return '"""No private Zotero item pins are distributed."""\nPINS = {}\n'
    if name in {"glm_client", "codex_client", "claude_client"}:
        # The standalone UI uses the explicit API profiles. Legacy paid CLI
        # adapters are not dependencies and must not read unrelated app homes.
        return ('"""Standalone distributions use explicitly configured model APIs."""\n'
                'REASONING_LEVEL = "max"  # cache compatibility; API profiles keep their own reasoning identity\n'
                'class ClaudeError(RuntimeError): pass\nclass CodexError(RuntimeError): pass\n'
                'class CodexQuotaError(CodexError): pass\n'
                'def ask(*args, **kwargs):\n    raise RuntimeError("Standalone Library Sync requires an API profile; CLI adapters are not bundled.")\n')
    tree = ast.parse(text)
    if name == "library_resume":
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("Missing ownership baseline;"):
                node.value = "Missing ownership baseline; legacy recovery is not supported in this distribution"
    if name == "intake_new":
        keep = {"CK_V2", "JOURNAL_TYPES", "MEETING", "BASE_TABLE", "ACRONYM"}
        body = ast.parse("import csv\nimport re\nfrom pathlib import Path\n"
                         "from library_paths import LIBRARY as LIB, REPORTS\n").body
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in keep for t in node.targets):
                body.append(node)
            if isinstance(node, ast.FunctionDef) and node.name in {"norm", "acronym_table", "_acronym_rows"}:
                body.append(node)
        tree.body = body
    changed = False
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id in CONSTANTS.get(name, {}):
                expr = CONSTANTS[name][target.id]
                node.value = ast.parse(expr, mode="eval").body
                changed |= "_release_paths" in expr
    if changed:
        index = 0
        for i, node in enumerate(tree.body):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                index = i + 1
        tree.body.insert(index, ast.parse("import library_paths as _release_paths").body[0])
    tree = StripNarrative().visit(tree)
    ast.fix_missing_locations(tree)
    return '# Library Sync standalone export; Original source MIT; bundled distribution AGPL. See release/DISTRIBUTION_LICENSE.md.\n' + ast.unparse(tree) + '\n'


def private_markers(repo):
    """Identify private table values from the input, without publishing them."""
    wanted = {"library_paths": {"LEGACY_DEFAULTS"}, "citekey_reformat_prep": {"PINS"},
              "attachments_normalize_prep": {"EXPLICIT_RENAMES"}, "library_resume": {"LEGACY_RUN", "LEGACY_PAPERS"}}
    result = set()
    for module, symbols in wanted.items():
        tree = ast.parse((Path(repo) / "tools" / (module + ".py")).read_text(encoding="utf-8-sig"))
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            if any(isinstance(t, ast.Name) and t.id in symbols for t in targets):
                nodes = node.value.values if module == "library_paths" and isinstance(node.value, ast.Dict) else ast.walk(node.value)
                result.update(n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) >= 8)
    return {s for s in result if "{ck}" not in s} - {"annotated", "supplement", "manuscript", "translation", "preprint", "primary"}


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
    markers = private_markers(repo)
    # The plugin manifest is the one canonical version source: the XPI builder
    # derives its version from it and refuses drift, so the release metadata
    # reads the same value instead of keeping a second hand-updated copy.
    plugin_version = json.loads((repo / "tools" / "zotero_library_sync" / "manifest.json")
                                 .read_text(encoding="utf-8"))["version"]
    result, provenance = {}, []
    for name in SOURCE_ALLOWLIST:
        path = repo / name
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("Missing/unsafe allowlisted input: " + name)
        raw = path.read_bytes()
        content = raw
        if name.startswith("tools/") and Path(name).stem in RUNTIME_MODULES and name.endswith(".py"):
            content = portable_python(Path(name).stem, raw.decode("utf-8-sig")).encode("utf-8")
        scan_public(name, content, markers)
        result[name] = content
        provenance.append({"path": name, "input_sha256": hashlib.sha256(raw).hexdigest(),
                           "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                           "transformed": raw != content})
    result["README.md"] = result["release/README.md"]
    for document in ("BUILD.md", "PRIVACY.md", "DISTRIBUTION_LICENSE.md", "MACOS.md", "BBT_SETUP.md", "THIRD_PARTY_SOURCE_GUIDE.md"):
        result["README.md"] = result["README.md"].replace(f"]({document})".encode(), f"](release/{document})".encode())
    result["NOT_PUBLISHED"] = b"NOT_PUBLISHED\nLocal review artifacts only. No remote or public repository has been created.\n"
    result["RELEASE.json"] = json.dumps({"version": plugin_version, "status": "public-beta",
        "license": "MIT", "distribution_license": "AGPL-3.0-only",
        "platform": "windows-x86_64; macos-arm64/x86_64-experimental", "portable_defaults": True,
        "git": "system Git required; explicit installation setup", "real_sync_verified": False}, indent=2).encode() + b"\n"
    result["SOURCE_MANIFEST.json"] = json.dumps({"schema_version": 1, "export": "explicit-allowlist",
        "git_history_included": False, "files": provenance}, indent=2).encode() + b"\n"
    result[".gitignore"] = b"/runtime/\n/release/vendor/\n/release/dist/\n/library-sync.json\n/.secrets/\n/reports/\n__pycache__/\n*.pyc\n"
    return result
