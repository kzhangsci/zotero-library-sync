"""Platform acceptance using synthetic payloads and temporary repositories only."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import library_platform as p
import library_sync as s


class KeychainTests(unittest.TestCase):
    def test_reference_roundtrip_never_stores_payload_in_file(self):
        values = {}
        backend = types.SimpleNamespace(add=lambda account,data: values.update({account:data}), get=lambda account:values[account])
        payload = b'{"fixture_secret":"synthetic-only"}'
        reference = p.keychain_protect(payload, backend=backend)
        self.assertNotIn(payload, reference)
        self.assertEqual(p.keychain_protect(reference, decrypt=True, backend=backend),payload)

    def test_old_windows_ciphertext_is_not_plaintext_fallback(self):
        with self.assertRaisesRegex(RuntimeError, '重新输入'):
            p.keychain_protect(b'windows-ciphertext', decrypt=True, backend=Mock())

    def test_denied_save_does_not_return_reference(self):
        backend=Mock();backend.add.side_effect=RuntimeError('Keychain unavailable')
        with self.assertRaises(RuntimeError):p.keychain_protect(b'synthetic',backend=backend)

    def test_bad_reference_rejected_without_native_access(self):
        backend=Mock()
        with self.assertRaises(ValueError):p.keychain_protect(p.REFERENCE_PREFIX+b'../bad',decrypt=True,backend=backend)
        backend.get.assert_not_called()

    def test_process_identity_pid_reuse_and_access_denial(self):
        api=Mock();api.NoSuchProcess=ProcessLookupError;api.ZombieProcess=ChildProcessError
        process=api.Process.return_value;process.exe.return_value='/Applications/Python/bin/python3'
        process.create_time.side_effect=[100.125,100.125];process.is_running.return_value=True
        self.assertEqual(p.mac_identity(12,api=api)['created'],'100.125000000')
        process.create_time.side_effect=[100.125,101.0]
        self.assertIsNone(p.mac_identity(12,api=api))
        api.Process.side_effect=PermissionError('denied')
        with self.assertRaises(PermissionError):p.mac_identity(12,api=api)


class SnapshotCommitTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.repo=Path(self.temporary.name).resolve()
        self.git('init','--initial-branch=main');self.git('config','user.name','Synthetic Fixture')
        self.git('config','user.email','fixture@example.invalid')
        (self.repo/'owned.md').write_text('before',encoding='utf-8')
        (self.repo/'other.md').write_text('before',encoding='utf-8')
        self.git('add','owned.md','other.md');self.git('commit','-m','fixture')
        self.owned=self.repo/'owned.md';self.owned.write_text('approved',encoding='utf-8')
        self.hashes={str(self.owned):hashlib.sha256(self.owned.read_bytes()).hexdigest()}

    def git(self,*args):
        result=subprocess.run(['git','-C',str(self.repo),*args],capture_output=True,check=True)
        return result.stdout.decode().strip()

    def test_commit_uses_approved_snapshot_preserves_late_edit_and_unrelated_work(self):
        (self.repo/'other.md').write_text('unrelated user work',encoding='utf-8')
        sha=p.posix_commit(self.repo,['owned.md'],'fixture publish',expected_hashes=self.hashes,
                           guard=lambda:self.owned.write_text('late user edit',encoding='utf-8'))
        self.assertTrue(sha)
        self.assertEqual(self.git('show','HEAD:owned.md'),'approved')
        self.assertEqual(self.owned.read_text(),'late user edit')
        self.assertEqual(self.git('show','HEAD:other.md'),'before')
        self.assertEqual(self.git('diff','--cached','--name-only'),'')

    def test_preexisting_staging_unchanged(self):
        self.git('add','owned.md');before=(self.repo/'.git/index').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'pre-existing staged'):
            p.posix_commit(self.repo,['owned.md'],'fixture publish',expected_hashes=self.hashes)
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)

    def test_wrong_hash_and_index_lock_leave_head_and_index_unchanged(self):
        before=self.git('rev-parse','HEAD');index=(self.repo/'.git/index').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'changed concurrently'):
            p.posix_commit(self.repo,['owned.md'],'fixture publish',expected_hashes={str(self.owned):'bad'})
        self.assertEqual(self.git('rev-parse','HEAD'),before)
        self.assertEqual((self.repo/'.git/index').read_bytes(),index)
        (self.repo/'.git/index.lock').write_text('another owner')
        with self.assertRaisesRegex(RuntimeError,'正在使用'):
            p.posix_commit(self.repo,['owned.md'],'fixture publish',expected_hashes=self.hashes)
        self.assertEqual((self.repo/'.git/index.lock').read_text(),'another owner')

    def test_reference_update_rejection_does_not_touch_user_index(self):
        before=self.git('rev-parse','HEAD');index=(self.repo/'.git/index').read_bytes()
        def reject():raise RuntimeError('synthetic guard refusal')
        with self.assertRaisesRegex(RuntimeError,'guard refusal'):
            p.posix_commit(self.repo,['owned.md'],'fixture publish',expected_hashes=self.hashes,guard=reject)
        self.assertEqual(self.git('rev-parse','HEAD'),before)
        self.assertEqual((self.repo/'.git/index').read_bytes(),index)
        self.assertFalse((self.repo/'.git/index.lock').exists())


@unittest.skipUnless(os.name=='posix','Native POSIX only')
class NativePosixTests(unittest.TestCase):
    def test_lock_exclusion_and_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock=Path(temporary)/'workflow.lock'
            with p.posix_lock(lock):
                with self.assertRaises(RuntimeError):
                    with p.posix_lock(lock):pass
            with p.posix_lock(lock):pass


if __name__=='__main__':unittest.main()
