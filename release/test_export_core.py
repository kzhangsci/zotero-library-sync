"""Standalone synthetic control tests, executed against exported runtime modules.

No historical fixtures, real item keys/hashes, model calls, commits or network.
Run: runtime/python.exe -B -m unittest test_export_core
"""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import library_control as control
import library_resume as resume
import library_sync as workflow
import qc_glm

RUN = "20990102_030405_aabbcc"
KEY = "SYNTH001"
CITEKEY = "Example2099SYN-Fixture"


class ExportControlTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="export-control-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root / "runs"
        self.run = self.runs / RUN
        self.run.mkdir(parents=True)
        self.library = self.root / "Library"
        self.folder = self.library / "papers" / CITEKEY
        self.folder.mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(workflow, "RUNS", self.runs))
        self.stack.enter_context(patch.object(workflow, "LIBRARY", self.library))
        self.stack.enter_context(patch.object(workflow, "launch_lock", side_effect=contextlib.nullcontext))
        self.stack.enter_context(patch.object(workflow, "active_controller", return_value={"id": RUN}))
        self.state = {"id": RUN, "status": "running", "control_schema": 1, "requested": [KEY]}
        workflow.save_json(self.run / "state.json", self.state)

    def test_pause_is_durable_idempotent_and_finalization_barrier(self):
        original = (self.run / "state.json").read_bytes()
        first = control.request_pause(RUN)
        self.assertEqual(first["status"], "pause_requested")
        request = (self.run / "pause_request.json").read_bytes()
        self.assertEqual(control.request_pause(RUN)["status"], "pause_requested")
        self.assertEqual((self.run / "pause_request.json").read_bytes(), request)
        self.assertEqual((self.run / "state.json").read_bytes(), original)
        checkpoint = Mock()
        self.assertFalse(control.finalize_barrier(self.state, self.run, checkpoint))
        checkpoint.assert_not_called()
        (self.run / "pause_request.json").unlink()
        self.assertTrue(control.finalize_barrier(self.state, self.run, checkpoint))
        workflow.save_json(self.run / "state.json", self.state)
        with self.assertRaises(RuntimeError):
            control.request_pause(RUN)

    def test_resume_preserves_qc_and_note_and_requests_only_translation(self):
        en = self.folder / (CITEKEY + "_EN.md")
        note = self.folder / (CITEKEY + "_Note.md")
        en.write_text(f"---\ncitekey: {CITEKEY}\nzotero_key: {KEY}\nqc: synthetic-reviewed\n---\nSynthetic original.\n", encoding="utf-8")
        note.write_text("User-authored synthetic note.\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in (en, note)}
        paper = {"key": KEY, "citekey": CITEKEY, "require_translation": True}
        self.assertEqual(control.remaining_steps(paper), ["zh"])
        for p, value in before.items():
            self.assertEqual(p.read_bytes(), value)
        (self.folder / (CITEKEY + "_ZH.md")).write_text("Synthetic translation", encoding="utf-8")
        self.assertEqual(control.remaining_steps(paper), [])
        with self.assertRaises(RuntimeError):
            control.remaining_steps({**paper, "key": "SYNTH002"})

    def test_private_legacy_exception_is_disabled_in_export(self):
        self.assertIsNone(resume.LEGACY_RUN)
        self.assertEqual(resume.LEGACY_PAPERS, {})
        self.state.update(status="blocked", papers=[{"key": KEY, "citekey": CITEKEY, "primary_key": "SYNTHPDF"}])
        workflow.save_json(self.run / "state.json", self.state)
        with self.assertRaisesRegex(RuntimeError, "Missing ownership baseline"):
            resume.source_record(RUN)

    def test_qc_rejects_content_loss_and_missing_formula(self):
        original = ("Synthetic density values preserve their original notation. " * 12) + " $$N^2$$"
        self.assertTrue(qc_glm.judge(original, "Too short."))
        self.assertIn("formula", qc_glm.judge(original, original.replace(" $$N^2$$", "")))
        with self.assertRaises(qc_glm.QCProtocolError):
            qc_glm.parse_changes("[99]\nUnexpected replacement", 1)


if __name__ == "__main__":
    unittest.main()
