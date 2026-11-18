"""Idle upgrade gates and immutable startup identity; no model or user data."""
import concurrent.futures
from pathlib import Path
import unittest
from unittest.mock import patch
import library_backend as backend
import library_plugin_bridge as bridge


class BackendUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.service=bridge.Service()
        self.addCleanup(self.service.pool.shutdown,False)
        self.addCleanup(self.service.catalog_pool.shutdown,False)
        self.owner=patch.object(bridge.workflow,'active_controller',return_value=None).start()
        self.idle=patch.object(bridge.workflow,'assert_idle').start()
        self.addCleanup(patch.stopall)

    def test_running_task_or_catalog_refuses_restart(self):
        self.owner.return_value={'id':'running'}
        with self.assertRaises(bridge.RequestError):self.service.dispatch('POST','/v1/service/stop',{'confirm':True})
        self.owner.return_value=None
        future=concurrent.futures.Future();self.service.operations['pending']=future
        with self.assertRaises(bridge.RequestError):self.service.dispatch('POST','/v1/service/stop',{'confirm':True})
        self.assertFalse(self.service.stopping)
        future.set_result({})
        self.assertTrue(self.service.dispatch('POST','/v1/service/stop',{'confirm':True})['stopping'])
        with self.assertRaises(bridge.RequestError):self.service.dispatch('POST','/v1/start',{})

    def test_unconfirmed_request_and_concurrent_request_refused(self):
        with self.assertRaises(bridge.RequestError):self.service.dispatch('POST','/v1/service/stop',{})
        self.service.requests=1
        with self.assertRaises(bridge.RequestError):self.service.dispatch('POST','/v1/service/stop',{'confirm':True})
        self.assertFalse(self.service.stopping)

    def test_version_captured_at_start_and_detects_disk_change(self):
        identity=backend.Identity(Path(__file__).resolve().parents[1])
        self.assertEqual(identity.public()['version'],'0.2.0-beta.16')
        self.assertFalse(identity.public()['disk_changed'])
        with patch.object(backend,'fingerprint',return_value='changed'):
            self.assertTrue(identity.public()['disk_changed'])
            self.assertNotEqual(identity.public()['build'],'changed')
