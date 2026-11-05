"""Offline release acceptance. Only temporary synthetic fixtures are written.

Run: py -3 -B -m unittest discover -s tools -p test_library_release.py
Full embedded-runtime installation test runs when release/vendor is present.
No production APIs, credentials, Git operations or model calls.
"""
from __future__ import annotations
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "release"))
import library_paths
import build_library_release as builder
import export_rules
import install

GIT_SETUP = {"git_path": shutil.which("git"), "init_git": True,
             "git_user_name": "Synthetic Test", "git_user_email": "synthetic@example.invalid"}


class PathsTests(unittest.TestCase):
    def test_legacy_and_separate_config_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            legacy = library_paths.get_paths(repo=root, environ={})
            self.assertTrue(legacy.portable)
            self.assertNotEqual(legacy.data, root)
            self.assertIsNone(legacy.baseline_date)
            (root / "library-sync.json").write_text(json.dumps({"schema_version": 1,
                "data_dir": "state", "library_dir": "papers-vault", "baseline_date": "2026-01-02"}))
            configured = library_paths.get_paths(repo=root, environ={})
            self.assertEqual(configured.repo, root)
            self.assertEqual(configured.cache, root / "state/reports/_cache")
            self.assertEqual(configured.library, root / "papers-vault")
            self.assertFalse(configured.data.exists())
            self.assertFalse(configured.library.exists())

    def test_explicit_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for text in ('{"schema_version":1,"baseline_date":"2026-02-30"}',
                         '{"schema_version":1,"schema_version":1}',
                         '{"schema_version":1,"token":"synthetic"}', '[]'):
                (root / "bad.json").write_text(text)
                with self.assertRaises(ValueError):
                    library_paths.get_paths(repo=root, environ={"LIBRARY_SYNC_CONFIG": "bad.json"})
            with self.assertRaises(ValueError):
                library_paths.get_paths(repo=root, environ={"LIBRARY_SYNC_CONFIG": "absent.json"})

    def test_environment_and_portable_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "RELEASE.json").write_text('{}')
            cfg = root / "public.json"
            cfg.write_text(json.dumps({"schema_version": 1, "data_dir": "cfg-data", "library_dir": "cfg-lib"}))
            p = library_paths.get_paths(repo=root, environ={"LIBRARY_SYNC_CONFIG": str(cfg),
                "LIBRARY_SYNC_HOME": str(root / "env-data"), "LIBRARY_SYNC_LIBRARY": str(root / "env-lib")})
            self.assertTrue(p.portable)
            self.assertEqual(p.data, root / "env-data")
            self.assertEqual(p.library, root / "env-lib")


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = export_rules.export_sources(REPO)

    def test_explicit_allowlist_and_no_private_tables(self):
        self.assertIn("tools/library_control.py", self.sources)
        self.assertFalse(any(x.startswith((".git/", "reports/", ".secrets/", "output/")) for x in self.sources))
        for name, value in self.sources.items():
            export_rules.scan_public(name, value)
        for module, symbol in (("citekey_reformat_prep", "PINS"), ("attachments_normalize_prep", "EXPLICIT_RENAMES"), ("library_paths", "LEGACY_DEFAULTS"), ("library_resume", "LEGACY_PAPERS")):
            tree = ast.parse(self.sources[f"tools/{module}.py"])
            targets = [x for x in tree.body if isinstance(x, (ast.Assign, ast.AnnAssign))
                       and any(isinstance(t, ast.Name) and t.id == symbol for t in (x.targets if isinstance(x, ast.Assign) else [x.target]))]
            self.assertEqual(len(targets), 1)
            self.assertEqual(ast.literal_eval(targets[0].value), {})
        tree = ast.parse(self.sources["tools/library_resume.py"])
        run = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LEGACY_RUN" for t in n.targets))
        self.assertIsNone(ast.literal_eval(run.value))

    def test_import_closure_and_compile(self):
        exported = {Path(x).stem for x in self.sources if x.startswith("tools/") and x.endswith(".py")}
        external = {"fitz", "requests", "urllib3", "yaml"}
        for module in export_rules.RUNTIME_MODULES:
            value = self.sources[f"tools/{module}.py"]
            compile(value, module + ".py", "exec")
            for node in ast.walk(ast.parse(value)):
                names = [x.name.split('.')[0] for x in node.names] if isinstance(node, ast.Import) else [node.module.split('.')[0]] if isinstance(node, ast.ImportFrom) and node.module else []
                for name in names:
                    self.assertIn(name, exported | external | sys.stdlib_module_names, (module, name))

    def test_zip_is_deterministic(self):
        first = builder.zip_bytes(self.sources)
        self.assertEqual(first, builder.zip_bytes(dict(reversed(list(self.sources.items())))))
        with zipfile.ZipFile(io.BytesIO(first)) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(sorted(z.namelist()), sorted(self.sources))

    def test_dependency_zip_traversal_rejected(self):
        for name in ("../outside", "/absolute", "a\\b", "C:/outside"):
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as z:
                info = zipfile.ZipInfo("placeholder")
                info.filename = name  # preserve hostile separators on Windows
                z.writestr(info, b'fixture')
            with self.assertRaises(ValueError):
                list(builder.archive_members(data.getvalue()))

    def test_source_manifest_hashes(self):
        manifest = json.loads(self.sources["SOURCE_MANIFEST.json"])
        self.assertFalse(manifest["git_history_included"])
        for row in manifest["files"]:
            self.assertEqual(hashlib.sha256(self.sources[row["path"]]).hexdigest(), row["sha256"])

    def test_installer_dryrun_baseline_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source"
            source.mkdir()
            content = {"RELEASE.json": b'{"status":"NOT_PUBLISHED"}', "README.md": b'synthetic'}
            for name, value in content.items():
                (source / name).write_bytes(value)
            (source / "FILES.json").write_text(json.dumps(builder.inventory(content)))
            for date in ("", "2026-02-30", "yesterday"):
                with self.assertRaises(ValueError):
                    install.install(source, root / "target", root / "Library", date, apply=True)
            self.assertFalse((root / "target").exists())
            with self.assertRaisesRegex(ValueError, "init-library-git"):
                install.install(source, root / "target", root / "Library", "2026-01-02")
            preview = install.install(source, root / "target", root / "Library", "2026-01-02", **GIT_SETUP)
            self.assertEqual(preview["mode"], "dry-run")
            self.assertFalse((root / "target").exists())
            self.assertFalse((root / "Library").exists())
            install.install(source, root / "target", root / "Library", "2026-01-02", apply=True, **GIT_SETUP)
            self.assertTrue((root / "Library/.git").is_dir())
            with self.assertRaises(ValueError):
                install.install(source, root / "target", root / "Library", "2026-01-02", apply=True)
            ledger = json.loads((root / "target/data/reports/_cache/library_sync/discovery.json").read_text())
            self.assertEqual(ledger, {"schema": 1, "since": "2026-01-02T00:00:00+00:00", "entries": {}})
            (source / "README.md").write_text("tampered")
            with self.assertRaises(ValueError):
                install.install(source, root / "target2", root / "Library", "2026-01-02", apply=True, **GIT_SETUP)
            self.assertFalse((root / "target2").exists())

    @unittest.skipUnless(os.name == 'nt' and (REPO / 'release/vendor').is_dir(), 'Bundled Windows artifacts required')
    def test_embedded_fresh_install(self):
        with tempfile.TemporaryDirectory(prefix="Library Sync 中文 ") as d:
            root = Path(d)
            outputs = builder.build()
            payload = outputs[f"library-sync-{builder.VERSION}-windows-x64.zip"]
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                self.assertIsNone(z.testzip())
                z.extractall(root / "unpacked")
            package = root / "unpacked/app"
            env = {k: v for k, v in os.environ.items() if not k.startswith(("LIBRARY_SYNC_", "PYTHON", "GLM_", "DEEPSEEK_", "MINERU_"))}
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["GIT_CONFIG_GLOBAL"] = os.devnull
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            setup = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                     str(root / "unpacked/setup.ps1"), "-InstallDir", str(root / "fresh"),
                     "-LibraryDir", str(root / "Chosen Library"), "-BaselineDate", "2026-01-02",
                     "-InitLibraryGit", "-GitPath", GIT_SETUP["git_path"],
                     "-GitUserName", GIT_SETUP["git_user_name"], "-GitUserEmail", GIT_SETUP["git_user_email"]]
            for apply in (False, True):
                setup_result = subprocess.run(setup + (["-Apply"] if apply else []), cwd=root, env=env,
                    capture_output=True, text=True, encoding="utf-8", timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(setup_result.returncode, 0, setup_result.stderr)
                self.assertEqual(json.loads(setup_result.stdout)["mode"], "apply" if apply else "dry-run")
                if not apply:
                    self.assertFalse((root / "fresh").exists())
                    self.assertFalse((root / "Chosen Library").exists())
            app = root / "fresh/app"
            env["LIBRARY_SYNC_OFFLINE_CHECK"] = "1"
            result = subprocess.run([str(app / "runtime/python.exe"), "-B", "-X", "utf8",
                str(app / "release/offline_smoke.py"), "--smoke"], cwd=root, env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["result"], "PASS")
            self.assertFalse(data["network"])
            self.assertFalse(data["credentials_read"])
            self.assertFalse((app / ".git").exists())
            self.assertTrue((root / "fresh/data/.git").is_dir())
            for sensitive in (".secrets/", "reports/_cache/", "mineru_out/", "output/"):
                self.assertIn(sensitive, (root / "fresh/data/.gitignore").read_text())
            self.assertFalse((root / "fresh/data/.secrets").exists())
            for entry in ("library_sync.py", "sync.py", "library_plugin_bridge.py", "zotero_local.py", "library_qa.py", "mineru_client.py"):
                help_result = subprocess.run([str(app / "runtime/python.exe"), "-B", "-X", "utf8",
                    str(app / "tools" / entry), "--help"], cwd=root, env=env,
                    capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(help_result.returncode, 0, (entry, help_result.stderr))
                self.assertIn("usage:", help_result.stdout)
            config_result = subprocess.run([str(app / "runtime/python.exe"), "-B", "-X", "utf8",
                str(app / "release/offline_smoke.py"), "--config"], cwd=root, env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(config_result.returncode, 0, config_result.stderr)
            public = json.loads(config_result.stdout)
            self.assertEqual(public["library"], str(root / "Chosen Library"))
            self.assertEqual(Path(public["git_path"]), Path(GIT_SETUP["git_path"]).resolve())
            fixture_result = subprocess.run([str(app / "runtime/python.exe"), "-B", "-X", "utf8", "-m", "unittest",
                "test_export_core", "test_library_progress", "test_qc_protocol", "test_model_pipeline", "test_model_catalog", "test_model_profiles"], cwd=app,
                env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
            self.assertEqual(fixture_result.returncode, 0, fixture_result.stderr)
            self.assertIn("OK", fixture_result.stderr)
            print(json.dumps({"embedded_smoke": data, "setup_ps1_dryrun_apply": True,
                              "cli_help_checks": 6, "readonly_config": True,
                              "exported_core_tests": fixture_result.stderr.strip().splitlines()[-4:]}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
