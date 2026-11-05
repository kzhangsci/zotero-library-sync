"""Strict QC protocol regression tests; only synthetic text, temp files and mocks."""
from __future__ import annotations

import contextlib
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import qc_glm as qc
import sync


PROSE = (
    "The synthetic density prole is sampled at regular depths to compare adjacent "
    "layers under identical settings. These idealized values describe a numerical "
    "fixture and do not establish a mechanism in observations. A uniform grid keeps "
    "the comparison reproducible while preserving the original measurement notation. "
    "Mixing remains unresolved."
)
CORRECTED = PROSE.replace("prole", "profile")
INVALID = (
    None, {}, "", " \r\n\t", "无。解释", "None", "无需修改", "I cannot process this request.",
    "抱歉，我无法处理此请求。", "[999]", "[999]\n" + CORRECTED, "[0]\n" + CORRECTED,
    "[-1]\n" + CORRECTED, "[abc]\n" + CORRECTED, "[1]", "[1]\n \t\n",
    "[1]\n\n[2]\n" + CORRECTED,
    "[1]\n" + CORRECTED + "\n[1]\n" + CORRECTED,
    "[01]\n" + CORRECTED + "\n【1】\n" + CORRECTED,
    "[1]\n" + CORRECTED + "\n[999]\nUnsolicited replacement.",
    "[1]\n" + CORRECTED + "\n[-1]\nUnsolicited replacement.",
    "Here are the corrections:\n[1]\n" + CORRECTED,
    "```text\n[1]\n" + CORRECTED + "\n```",
    "无\n[1]\n" + CORRECTED, "[1]\n无", "[1]\n无。", "[1]\nNone",
    "[1]\nI cannot process this request.", "[1]\n抱歉，我无法处理此请求。",
)


class ProtocolTests(unittest.TestCase):
    def test_only_exact_nochange_with_outer_whitespace(self):
        for answer in ("无", "无。", " \n无\r\n\t", " \n无。\r\n\t"):
            with self.subTest(answer=answer):
                self.assertEqual(qc.parse_changes(answer, 2), {})
                self.assertEqual(qc.qc_body(PROSE, Mock(ask=Mock(return_value=answer))),
                                 (PROSE, [], []))

    def test_invalid_answers_raise_dedicated_nonmemoized_error(self):
        for answer in INVALID:
            with self.subTest(answer=str(answer)[:60]):
                with self.assertRaises(qc.QCProtocolError) as caught:
                    qc.parse_changes(answer, 2)
                self.assertIs(caught.exception.stop_workflow, True)
                self.assertNotIn(CORRECTED, str(caught.exception))

    def test_legacy_sparse_unordered_multiline_replacements(self):
        answer = " \r\n【3】\r\nThird paragraph\r\nsecond line\r\n\r\n[01]\r\nFirst [12] citation.\r\n"
        self.assertEqual(qc.parse_changes(answer, 3),
                         {3: "Third paragraph\r\nsecond line", 1: "First [12] citation."})

    def test_invalid_answer_never_reaches_judge_or_retry(self):
        for answer in INVALID:
            with self.subTest(answer=str(answer)[:60]):
                backend = Mock(ask=Mock(return_value=answer))
                with patch.object(qc, "time", types.SimpleNamespace(sleep=Mock())) as clock, \
                     patch.object(qc, "judge") as judge:
                    with self.assertRaises(qc.QCProtocolError):
                        qc.qc_body(PROSE, backend)
                backend.ask.assert_called_once()
                clock.sleep.assert_not_called()
                judge.assert_not_called()

    def test_later_invalid_chunk_prevents_partial_return_serial_and_parallel(self):
        for workers in (1, 2):
            with self.subTest(workers=workers):
                def answer(prompt):
                    return "[1]\n" + CORRECTED if "first fixture" in prompt else "[999]\nbad"
                backend = Mock(ask=Mock(side_effect=answer))
                with patch.object(qc, "QC_PARALLEL", workers), \
                     patch.object(qc, "chunks", return_value=iter([[PROSE + " first fixture"], [PROSE]])), \
                     patch.object(qc, "time", types.SimpleNamespace(sleep=Mock())) as clock:
                    with self.assertRaises(qc.QCProtocolError):
                        qc.qc_body(PROSE + " first fixture\n\n" + PROSE, backend)
                self.assertEqual(backend.ask.call_count, 2)
                clock.sleep.assert_not_called()

    def test_wellformed_fix_still_passes(self):
        result, accepted, notes = qc.qc_body(PROSE, Mock(ask=Mock(return_value="[1]\n" + CORRECTED)))
        self.assertEqual(result, CORRECTED)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(notes, [])

    def test_content_guards_still_reject_without_changing_protocol(self):
        cases = (
            (PROSE, "这是中文段落。", "adds Chinese"),
            (PROSE, "Too short.", "length"),
            (PROSE, CORRECTED.removesuffix(" Mixing remains unresolved."), "drops a sentence"),
            (PROSE + " ![x](a.png)", CORRECTED, "drops an image"),
            (PROSE + " $$N^2$$", CORRECTED, "drops an image or display formula"),
        )
        for old, new, reason in cases:
            with self.subTest(reason=reason):
                result, accepted, notes = qc.qc_body(old, Mock(ask=Mock(return_value="[1]\n" + new)))
                self.assertEqual((result, accepted), (old, []))
                self.assertTrue(any(reason in note for note in notes), notes)

    def test_pdf_guard_still_checks_valid_proposals(self):
        with patch.object(qc, "pdf_check", return_value=[(3, "old", "new")]) as guard:
            result, accepted, notes = qc.qc_body(
                PROSE, Mock(ask=Mock(return_value="[1]\n" + CORRECTED)), "synthetic PDF text")
        guard.assert_called_once()
        self.assertEqual((result, accepted), (PROSE, []))
        self.assertTrue(notes[0].startswith("rejected [pdf]"))

    def test_sync_invalid_qc_writes_no_stamp_translation_backup_or_failure_memo(self):
        for answer in INVALID:
            with self.subTest(answer=str(answer)[:60]), \
                 tempfile.TemporaryDirectory(prefix="qc-protocol-offline-") as temp, \
                 contextlib.ExitStack() as stack:
                root = Path(temp)
                ck, key = "Synthetic2099-Fixture", "TEST0001"
                folder = root / "library" / "papers" / ck
                folder.mkdir(parents=True)
                reports = root / "reports"
                reports.mkdir()
                source = folder / f"{ck}_EN.md"
                source.write_text(f"---\ncitekey: {ck}\nzotero_key: {key}\nsource: mineru\n---\n\n{PROSE}",
                                  encoding="utf-8")
                original = source.read_bytes()
                memo = root / "failures.json"
                memo.write_text("{}", encoding="utf-8")
                qc_backend = types.SimpleNamespace(ask=Mock(return_value=answer), used_paper=set(),
                                                  used_run=set(), model="synthetic/qc",
                                                  is_codex=False, is_claude=False)
                zh_backend = types.SimpleNamespace(used_run=set())
                for name, value in {
                    "LIB": root / "library", "CACHE": root / "cache", "REPORTS": reports,
                    "QC_TRASH": root / "trash", "FAIL_MEMO": memo,
                    "STOP_FILE": root / "stop", "NO_TRANSLATE_LIST": root / "no-translate",
                }.items():
                    stack.enter_context(patch.object(sync, name, value))
                stack.enter_context(patch.object(sync, "load_zotero", return_value=({key: {
                    "citationKey": ck, "title": "Synthetic fixture", "language": "en",
                    "tags": [], "collections": [],
                }}, {}, {})))
                stack.enter_context(patch.object(sync, "scan_library", return_value={key: folder}))
                stack.enter_context(patch.object(sync, "load_glossary", return_value=[]))
                stack.enter_context(patch.object(sync, "primary_pdf", return_value=None))
                stack.enter_context(patch.object(sync, "select_backends", return_value=(qc_backend, zh_backend)))
                stack.enter_context(patch("sys.argv", ["sync.py", "--only", ck, "--steps", "qc,zh", "--apply"]))
                translation = stack.enter_context(patch.object(sync, "build_zh", side_effect=AssertionError("no ZH")))
                failure = stack.enter_context(patch.object(sync, "record_fail", side_effect=AssertionError("no memo")))
                clock = stack.enter_context(patch.object(qc, "time", types.SimpleNamespace(sleep=Mock())))
                tripwires = [stack.enter_context(patch(target, side_effect=AssertionError("offline only")))
                             for target in ("socket.create_connection", "socket.socket.connect",
                                            "socket.socket.connect_ex", "requests.sessions.Session.request",
                                            "subprocess.Popen", "model_api.DPAPIConfigStore.load",
                                            "model_api.DPAPIConfigStore.save")]
                log = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                sync.main()
                self.assertIn('"qc_failed": 1', log.getvalue())
                self.assertNotIn("qc done:", log.getvalue())
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(memo.read_text(encoding="utf-8"), "{}")
                self.assertFalse((folder / f"{ck}_ZH.md").exists())
                self.assertFalse((root / "trash").exists())
                qc_backend.ask.assert_called_once()
                for mock in (translation, failure, clock.sleep, *tripwires):
                    mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
