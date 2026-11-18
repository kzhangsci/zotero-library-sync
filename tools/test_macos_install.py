"""Installer contracts with temporary bundles; no network or real application state."""
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

RELEASE=Path(__file__).resolve().parents[1]/'release'
sys.path.insert(0,str(RELEASE))
import install_macos as installer


class MacInstallTests(unittest.TestCase):
    def test_prerequisites_reject_other_platform_and_python(self):
        with patch.object(installer.sys,'platform','win32'):
            with self.assertRaisesRegex(RuntimeError,'macOS'):installer.prerequisites()
        with patch.object(installer.sys,'platform','darwin'),patch.object(installer.sys,'version_info',(3,12)):
            with self.assertRaisesRegex(RuntimeError,'3.13'):installer.prerequisites()

    def bundle(self,root):
        app=root/'source';app.mkdir()
        (app/'fixture').write_bytes(b'synthetic')
        (app/'FILES.json').write_text(json.dumps({'version':'0.2.0-beta.16','files':[
            {'path':'fixture','bytes':9,'sha256':hashlib.sha256(b'synthetic').hexdigest()}]}))
        return app

    def args(self,source,target):
        return ['--source',str(source),'--install-dir',str(target),'--library-dir',str(target.parent/'Library'),
                '--baseline-date','2026-01-01','--git-user-name','Fixture','--git-user-email','fixture@example.invalid']

    def test_dry_run_does_not_create_runtime_or_start_pip(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=self.bundle(root);target=root/'install'
            with patch.object(installer,'prerequisites'),patch.object(installer,'install',return_value={'app':str(target/'app')}) as core,patch.object(installer.venv,'EnvBuilder') as env,patch.object(installer.subprocess,'run') as run:
                installer.main(self.args(source,target))
                self.assertFalse(core.call_args.kwargs['apply']);env.assert_not_called();run.assert_not_called()

    def test_tampered_bundle_refused_before_install(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=self.bundle(root);(source/'fixture').write_bytes(b'changed')
            with patch.object(installer,'prerequisites'),patch.object(installer,'install') as core:
                with self.assertRaisesRegex(ValueError,'integrity'):installer.main(self.args(source,root/'install'))
                core.assert_not_called()

    def test_apply_uses_offline_hashed_wheels_and_records_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=self.bundle(root);target=root/'install';app=target/'app';app.mkdir(parents=True)
            with patch.object(installer,'prerequisites'),patch.object(installer,'install',return_value={'app':str(app)}),patch.object(installer.venv,'EnvBuilder') as env,patch.object(installer.subprocess,'run') as run:
                installer.main(self.args(source,target)+['--apply'])
                self.assertTrue(env.call_args.kwargs['copies'])
                command=run.call_args_list[0].args[0]
                self.assertIn('--no-index',command);self.assertIn('--require-hashes',command)
                self.assertIn('--only-binary=:all:',command)
                receipt=json.loads((target/'INSTALL_RECEIPT.json').read_text())
                self.assertTrue(receipt['runtime_ready']);self.assertFalse(receipt['network'])


if __name__=='__main__':unittest.main()
