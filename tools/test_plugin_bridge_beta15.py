"""beta.15 bridge tests: remedy preview tokens and CSV-safe acronym saves."""
import csv as csv_module
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import library_plugin_bridge as bridge
import library_sync as workflow

RUN = "20261118_123000_abcdef"
KEY = "ABCD2345"


class RemedyTokenTests(unittest.TestCase):
    """/v1/remedy confirmations are bound to a one-time preview."""

    def setUp(self):
        import model_api
        guard = patch.object(model_api, "_store", side_effect=AssertionError("No real credential store"))
        guard.start()
        self.addCleanup(guard.stop)
        self.service = bridge.Service()
        import library_remedy
        import zotero_local
        self.remedy = patch.object(library_remedy, "remedy")
        self.mock_remedy = self.remedy.start()
        self.addCleanup(self.remedy.stop)
        self.binding = patch.object(library_remedy, "binding")
        self.mock_binding = self.binding.start()
        self.addCleanup(self.binding.stop)
        self.client = patch.object(zotero_local, "Client", return_value=Mock())
        self.client.start()
        self.addCleanup(self.client.stop)
        self.mock_binding.return_value = {"key": KEY, "files": {"X_ZH.md": "a" * 64}, "model": {"qc": 1}}
        self.mock_remedy.return_value = {"preview": True, "action": "repair_zh", "ok": True, "message": "p"}
        patcher = patch.object(workflow, "active_controller", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.service.pool.shutdown(wait=True)
        self.service.catalog_pool.shutdown(wait=False)

    def confirm(self, token=None, action="repair_zh"):
        payload = {"run_id": RUN, "key": KEY, "action": action, "confirm": True}
        if token is not None:
            payload["token"] = token
        return self.service.dispatch("POST", "/v1/remedy", payload)

    def wait_operation(self, operation_id, timeout=5.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.service.get_operation(operation_id)
            if result["status"] != "running":
                return result
            time.sleep(0.01)
        return result

    def preview(self, action="repair_zh"):
        operation = self.service.dispatch("POST", "/v1/remedy",
                                          {"run_id": RUN, "key": KEY, "action": action, "preview": True})
        result = self.wait_operation(operation["operation"])
        self.assertEqual(result["status"], "completed", result)
        return result["result"]

    def test_confirmation_requires_a_live_preview_token(self):
        with self.assertRaisesRegex(bridge.RequestError, "一次性令牌"):
            self.confirm()
        with self.assertRaisesRegex(bridge.RequestError, "不存在或已使用"):
            self.confirm(token="forged-token-value")

    def test_token_is_one_time(self):
        previewed = self.preview()
        self.assertTrue(previewed["token"])
        self.assertIn("binding", previewed)
        operation = self.confirm(token=previewed["token"])
        result = self.wait_operation(operation["operation"])
        self.assertEqual(result["status"], "completed")
        # Replay of the same token is refused even with identical inputs.
        with self.assertRaisesRegex(bridge.RequestError, "不存在或已使用"):
            self.confirm(token=previewed["token"])

    def test_stale_preview_inputs_require_a_new_preview(self):
        previewed = self.preview()
        self.mock_remedy.reset_mock()  # the preview itself ran remedy(confirm=False)
        self.mock_binding.return_value = {"key": KEY, "files": {"X_ZH.md": "b" * 64}, "model": {"qc": 1}}
        with self.assertRaisesRegex(bridge.RequestError, "重新预览"):
            self.confirm(token=previewed["token"])
        self.mock_remedy.assert_not_called()

    def test_model_config_drift_invalidates_preview(self):
        previewed = self.preview()
        self.mock_remedy.reset_mock()
        self.mock_binding.return_value = {"key": KEY, "files": {"X_ZH.md": "a" * 64}, "model": {"qc": 2}}
        with self.assertRaisesRegex(bridge.RequestError, "重新预览"):
            self.confirm(token=previewed["token"])

    def test_unknown_action_and_bad_keys_are_rejected(self):
        for payload in ({"run_id": RUN, "key": KEY, "action": "evil-action;", "preview": True},
                        {"run_id": RUN, "key": "bad", "action": "repair_zh", "preview": True},
                        {"run_id": "../x", "key": KEY, "action": "repair_zh", "preview": True},
                        {"run_id": RUN, "key": KEY, "action": "repair_zh", "preview": True, "confirm": True}):
            with self.assertRaises((bridge.RequestError, bridge.progress.ProgressError)):
                self.service.dispatch("POST", "/v1/remedy", payload)
        self.mock_remedy.assert_not_called()

    def test_active_controller_blocks_remedy_dispatch(self):
        with patch.object(workflow, "active_controller", return_value={"id": RUN}):
            with self.assertRaisesRegex(bridge.RequestError, "已有同步任务"):
                self.service.dispatch("POST", "/v1/remedy",
                                      {"run_id": RUN, "key": KEY, "action": "repair_zh", "preview": True})


class AcronymSaveTests(unittest.TestCase):
    """CSV-escaped, atomic, conflict-aware user acronym overrides."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reports = Path(self.temp.name) / "reports"
        self.reports.mkdir(parents=True)
        patcher = patch.object(workflow, "REPORTS", self.reports)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = bridge.Service()

    def tearDown(self):
        self.service.pool.shutdown(wait=True)
        self.service.catalog_pool.shutdown(wait=False)

    def save(self, title, proposed):
        return self.service.save_acronym({"title": title, "proposed": proposed, "confirm": True})

    def read_table(self):
        import intake_new
        return intake_new._acronym_rows(self.reports / "journal_acronyms.csv", "user")

    def effective_table(self):
        import intake_new
        saved = intake_new.REPORTS
        intake_new.REPORTS = self.reports
        try:
            return intake_new.acronym_table()
        finally:
            intake_new.REPORTS = saved

    def test_comma_quote_and_unicode_titles_roundtrip(self):
        result = self.save("Synthetic Journal, Ocean Research", "SJOR")
        self.assertTrue(result["changed"])
        result = self.save('Weird "Quotes" 地球物理学报', "WQG")
        self.assertTrue(result["changed"])
        rows = {(t, p) for t, p, _ in self.read_table()}
        self.assertIn(("Synthetic Journal, Ocean Research", "SJOR"), rows)
        self.assertIn(('Weird "Quotes" 地球物理学报', "WQG"), rows)
        table = self.effective_table()
        self.assertEqual(table.get("synthetic journal, ocean research"), "SJOR")
        self.assertEqual(table.get('weird "quotes" 地球物理学报'), "WQG")

    def test_conflict_and_idempotence(self):
        self.save("Deep Sea Research", "DSRX")
        with self.assertRaisesRegex(bridge.RequestError, "已映射"):
            self.save("Deep Sea Research", "OTHER")
        again = self.save("Deep Sea Research", "DSRX")
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.read_table()), 1)

    def test_injection_and_invalid_inputs_rejected(self):
        for title in ("=cmd()", "+1", "-x", "@ref"):
            with self.assertRaisesRegex(bridge.RequestError, "刊名无效"):
                self.save(title, "OK")
        for proposed in ("has space", "ISO4.2", "waytoolongacronym", ""):
            with self.assertRaisesRegex(bridge.RequestError, "缩写"):
                self.save("Valid Title", proposed)
        # Nothing was written by any rejected save.
        target = self.reports / "journal_acronyms.csv"
        if target.exists():
            self.assertEqual(self.read_table(), [])

    def test_existing_full_format_columns_are_preserved(self):
        target = self.reports / "journal_acronyms.csv"
        target.write_text(
            "title,n_items,item_type,existing_abbrevs,proposed,source,sample_citekey\n"
            "Geoscientific Model Development,1,journal,,GMD,curated,X1\n", encoding="utf-8")
        self.save("Journal of Physical Oceanography", "JPO")
        with target.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv_module.reader(stream))
        self.assertEqual(rows[0][:3], ["title", "n_items", "item_type"])
        self.assertEqual([r[0] for r in rows[1:]],
                         ["Geoscientific Model Development", "Journal of Physical Oceanography"])
        self.assertEqual(rows[2][4], "JPO")  # proposed column preserved by position

    def test_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(bridge.RequestError, "明确确认"):
            self.service.save_acronym({"title": "T", "proposed": "T"})


if __name__ == "__main__":
    unittest.main()
