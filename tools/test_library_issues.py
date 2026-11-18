"""Durable issues-ledger tests: classification, transitions, retention safety."""
import json
from pathlib import Path
import tempfile
import unittest

import library_issues as issues


class ClassificationTests(unittest.TestCase):
    def test_qa_codes_map_to_multiple_categories_preserving_all_causes(self):
        qa = {"ok": False, "errors": [
            {"code": "quote_coverage", "message": "覆盖不一致"},
            {"code": "translation_marker", "message": "内部编号", "file": "X_ZH.md", "unit": 3},
            {"code": "source_pdf", "message": "source_pdf 与指定正本不一致。", "file": "X_EN.md"},
            {"code": "qc_missing", "message": "缺少 QC 标记", "file": "X_EN.md"},
        ]}
        categories = {row["category"]: row for row in issues.classify_qa(qa)}
        self.assertEqual(set(categories), {"translation", "legacy_format", "english_qc"})
        # source_pdf is a METADATA mismatch, never auto-classified missing PDF.
        self.assertNotIn("missing_pdf", categories)
        evidence = {e["code"] for e in categories["translation"]["evidence"]}
        self.assertEqual(evidence, {"quote_coverage", "translation_marker"})
        self.assertEqual(categories["translation"]["evidence"][1]["file"], "X_ZH.md")

    def test_missing_pdf_classification_uses_actual_filesystem_existence(self):
        with tempfile.TemporaryDirectory() as temp:
            present = Path(temp) / "real.pdf"
            present.write_bytes(b"%PDF-1.4 fixture")
            broken = "missing attachment file: " + str(Path(temp) / "gone.pdf")
            rows = issues.classify_reasons([broken], library=Path(temp))
            self.assertEqual(rows[0]["category"], "missing_pdf")
            fixed = "missing attachment file: " + str(present)
            rows = issues.classify_reasons([fixed], library=Path(temp))
            # The file exists on disk: not a missing-PDF case any more.
            self.assertNotEqual(rows[0]["category"], "missing_pdf")

    def test_identity_api_and_user_modified_reasons(self):
        rows = {row["category"] for row in issues.classify_reasons(
            ["IntakeBlocked: ambiguous attachment role: ABCD1234 x.pdf",
             "IntakeBlocked: X_ZH.md 未质检原文有用户修改，不自动覆盖",
             "!! qc failed X: incomplete_completion (finish_reason=length)"])}
        self.assertIn("identity", rows)
        self.assertIn("user_modified", rows)
        api = issues.classify_log(["!! qc failed X: incomplete_completion (finish_reason=length)",
                                   "!! translation failed X: timeout after 1500s [backend error]",
                                   "GLM 429 rate limit exceeded"])
        self.assertEqual(api[0]["category"], "api")
        self.assertEqual(len(api[0]["evidence"]), 3)

    def test_unknown_codes_fall_to_other_not_dropped(self):
        rows = issues.classify_qa({"ok": False, "errors": [{"code": "brand_new_code", "message": "x"}]})
        self.assertEqual(rows[0]["category"], "other")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = Path(self.temp.name)

    def test_record_result_complete_failed_and_unchanged_failure(self):
        ledger = issues.empty()
        state = {"status": "blocked", "id": "20261118_102159_cd926e", "requested": ["ABCDEFG2", "ZZZZ9999", "YYYY8888"],
                 "papers": [
                     {"key": "ABCDEFG2", "citekey": "Ok2026-Paper", "title": "ok", "exit_code": 0,
                      "qa": {"ok": True, "metrics": {"input_sha256": {"X_EN.md": "a" * 64}}}, "index": {"pdf": "True"}},
                     {"key": "ZZZZ9999", "citekey": "Bad2026-Paper", "title": "bad", "exit_code": 0,
                      "source_sha256": "b" * 64,
                      "qa": {"ok": False, "errors": [{"code": "quote_coverage", "message": "m"}]},
                      "qc_log": []},
                 ],
                 "deferred": [{"key": "YYYY8888", "citekey": "No2026-Pdf", "blocked": ["IntakeBlocked: no PDF; a clean primary is required"]}]}
        ledger = issues.record_result(ledger, state)
        self.assertEqual(ledger["items"]["ABCDEFG2"]["status"], "complete")
        self.assertEqual(ledger["items"]["ABCDEFG2"]["evidence"]["source_sha256"] if "source_sha256" in
                         ledger["items"]["ABCDEFG2"].get("evidence", {}) else
                         ledger["items"]["ABCDEFG2"]["evidence"].get("content_sha256"), {"X_EN.md": "a" * 64})
        failed = ledger["items"]["ZZZZ9999"]
        self.assertEqual(failed["status"], "needs_repair")
        self.assertIn("quote_coverage", failed["reasons"][0])
        self.assertEqual(ledger["items"]["YYYY8888"]["status"], "missing_pdf")
        # An unchanged failed item is not reset by a later paused/incomplete run.
        later = {"status": "paused", "id": "later", "requested": ["ZZZZ9999"], "papers": []}
        ledger = issues.record_result(ledger, later)
        self.assertEqual(ledger["items"]["ZZZZ9999"]["status"], "needs_repair")
        # A successful sibling never silently completes the failed item.
        other = {"status": "completed", "id": "other", "requested": ["ABCDEFG2"], "papers": []}
        ledger = issues.record_result(ledger, other)
        self.assertEqual(ledger["items"]["ZZZZ9999"]["status"], "needs_repair")

    def test_interrupted_and_paused_runs_record_nothing(self):
        ledger = issues.record_result(issues.empty(), {"status": "interrupted", "requested": ["ABCDEFG2"], "papers": []})
        self.assertEqual(ledger["items"], {})
        ledger = issues.record_result(issues.empty(), {"status": "paused", "requested": ["ABCDEFG2"], "papers": []})
        self.assertEqual(ledger["items"], {})

    def test_dispositions_and_undo_release_hold(self):
        ledger = issues.record_result(issues.empty(), {
            "status": "blocked", "id": "r1", "requested": ["ABCDEFG2"],
            "papers": [{"key": "ABCDEFG2", "citekey": "X", "exit_code": 1,
                        "qa": {"ok": False, "errors": []}}], "deferred": []})
        ledger = issues.record_disposition(ledger, "ABCDEFG2", action="defer", resulting_status="incomplete",
                                           reasons=["用户暂缓"], release_hold=True)
        self.assertTrue(ledger["items"]["ABCDEFG2"]["hold_released"])
        ledger = issues.undo_disposition(ledger, "ABCDEFG2")
        self.assertEqual(ledger["items"]["ABCDEFG2"]["status"], "needs_repair")
        self.assertNotIn("hold_released", ledger["items"]["ABCDEFG2"])
        with self.assertRaises(RuntimeError):
            issues.undo_disposition(ledger, "ABCDEFG2")

    def test_save_load_roundtrip_and_corruption_fails_closed(self):
        ledger = issues.record_result(issues.empty(), {"status": "blocked", "id": "r1", "requested": ["ABCDEFG2"],
                                                       "papers": [{"key": "ABCDEFG2", "citekey": "X", "exit_code": 0,
                                                                   "qa": {"ok": False, "errors": []}}], "deferred": []})
        issues.save(self.runs, ledger)
        self.assertEqual(issues.load(self.runs), ledger)
        (self.runs / "issues.json").write_text('{"schema": 1, "items": {"ABCDEFG2": {"status": "weird"}}}',
                                               encoding="utf-8")
        with self.assertRaises(RuntimeError):
            issues.load(self.runs)


if __name__ == "__main__":
    unittest.main()
