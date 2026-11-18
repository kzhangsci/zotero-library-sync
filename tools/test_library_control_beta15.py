"""beta.15 pause/resume recovery tests: mixed failures, needs-action, model choice.

Synthetic reproduction of the 20261118_102159_cd926e defect (pause requested,
exit-0 workers with failed QA, exit-75 workers, blocked state, no receipt) plus
the evidence-gated legacy recovery path. No real Zotero writes or model calls.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import library_control as c
import library_sync as s
import library_progress as progress
from test_library_sync import ControllerFixture


class MixedFailurePauseTests(ControllerFixture):
    KEY_OK, KEY_BAD, KEY_PAUSE = "ABCD2345", "EFGH6789", "IJKL1234"
    CK_OK, CK_BAD, CK_PAUSE = "Ok2026JPO-Mixing", "Bad2026GRL-Waves", "Pause2026DSR-Deep"

    def setUp(self):
        super().setUp()
        # Drop the fixture's default single item: this suite builds its own
        # three-paper batch with distinct keys/citekeys.
        self.items[:] = [x for x in self.items if x.get("key") != "ABCD2345"]
        self.results.pop("ABCD2345", None)
        self.plan["id"] = "20261118_102159_cd926e"
        self.plan["items"] = []
        for key, ck in ((self.KEY_OK, self.CK_OK), (self.KEY_BAD, self.CK_BAD),
                        (self.KEY_PAUSE, self.CK_PAUSE)):
            self.plan["items"].append({"key": key, "citekey": ck, "title": "t", "blocked": []})
            self.items.append({"key": key, "data": {"key": key, "itemType": "journalArticle",
                                                    "citationKey": ck, "title": "t",
                                                    "dateAdded": "2026-11-18T00:00:00Z", "language": "en"}})
            primary = self.root / (ck + ".pdf")
            primary.write_bytes(b"%PDF-1.4 fixture\n")
            self.results[key] = {"key": key, "citekey": ck, "status": "applied", "blocked": [],
                                 "redo_required": False, "require_translation": True,
                                 "primary_key": "PDF" + key[-4:], "primary_pdf": str(primary),
                                 "source_sha256": s.sha256(primary)}
            self.items.append({"key": "PDF" + key[-4:], "data": {
                "key": "PDF" + key[-4:], "itemType": "attachment", "parentItem": key,
                "contentType": "application/pdf", "filename": ck + ".pdf"}})
        self.client.file_path.side_effect = lambda key: self.results[
            next(k for k, v in self.results.items() if v["primary_key"] == key)]["primary_pdf"]
        self.stack.enter_context(patch.object(progress, "owner_matches", return_value=False))

    def family(self, key):
        parent = next(x for x in self.items if x["key"] == key)
        return {"parent": copy.deepcopy(parent),
                "children": copy.deepcopy([x for x in self.items if x["data"].get("parentItem") == key]),
                "library_version": 7}

    def mixed_pause(self):
        outcomes = {self.CK_OK: 0, self.CK_BAD: 0, self.CK_PAUSE: 75}

        def worker(args, log, env, **kwargs):
            ck = args[args.index("--only") + 1]
            self.worker_calls.append((args, Path(log), dict(env)))
            key = next(k for k, v in self.results.items() if v["citekey"] == ck)
            self.seed_paper(key, translation=outcomes[ck] == 0 and ck == self.CK_OK)
            Path(log).write_text("qc done: accepted\n", encoding="utf-8")
            if outcomes[ck] == 75:
                s.save_json(Path(env["LIBRARY_SYNC_PAUSE_FILE"]),
                            {"schema": 1, "run_id": self.plan["id"]})
            return outcomes[ck]
        self.worker.side_effect = worker
        return self.execute()

    def test_pause_seals_mixed_batch_and_preserves_failures(self):
        code, state = self.mixed_pause()
        self.assertEqual(code, 2)
        self.assertEqual(state["status"], "paused")
        self.assertIsNone(state.get("finalize_phase"))
        receipt = json.loads((self.runs / state["id"] / "pause_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], 2)
        self.assertEqual(receipt["papers"][self.KEY_OK]["status"], "completed")
        self.assertEqual(receipt["papers"][self.KEY_BAD]["status"], "failed")
        self.assertEqual(receipt["papers"][self.KEY_PAUSE]["status"], "paused")
        failed = {row["key"]: row for row in state["pause_failed_papers"]}
        self.assertEqual(set(failed), {self.KEY_BAD})
        self.assertTrue(any("验收" in r for r in failed[self.KEY_BAD]["reasons"]))

    def test_resume_completes_healthy_subset_and_quarantines_failed(self):
        _, state = self.mixed_pause()
        root = self.runs / state["id"]
        attempt = "20261118_110000_ddeeff"
        s.save_json(root / "resume_dispatch.json", {"schema": 1, "run_id": attempt,
                      "source_sha256": s.sha256(root / "state.json")})
        self.intake.reset_mock()
        self.intake.side_effect = AssertionError("intake forbidden during resume")
        self.worker.side_effect = self.run_worker
        with contextlib.redirect_stdout(io.StringIO()):
            code = c.resume_run(self.client, state["id"], apply=True, attempt=attempt)
        result = json.loads((self.runs / attempt / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 2)  # partial: the failed paper still needs action
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["workflow_complete"])
        self.assertEqual({row["key"] for row in result["needs_action"]}, {self.KEY_BAD})
        self.assertIn(self.KEY_BAD, {row["key"] for row in result["deferred"]})
        by_key = {p["key"]: p for p in result["papers"]}
        self.assertEqual({self.KEY_OK, self.KEY_PAUSE}, set(by_key))
        self.assertTrue(by_key[self.KEY_OK]["qa"]["ok"])
        self.assertTrue(by_key[self.KEY_PAUSE]["qa"]["ok"])
        # The failed paper was never re-sent to a worker during resume.
        resumed = [args for args, _, _ in self.worker_calls if "resume_" in args[-1]]
        self.assertEqual([a[a.index("--only") + 1] for a in resumed], [self.CK_PAUSE])

    def test_duplicate_resume_dispatch_is_idempotent(self):
        _, state = self.mixed_pause()
        root = self.runs / state["id"]
        s.save_json(root / "resume_dispatch.json", {"schema": 1, "run_id": "20261118_110001_abcabc",
                      "source_sha256": s.sha256(root / "state.json")})
        with self.assertRaisesRegex(RuntimeError, "已提交恢复"):
            c.prepare_resume(self.client, state["id"])

    def selected_config(self):
        return {"schema": 1,
                "qc": {"provider": "glm", "model": "glm-5.3", "reasoning_effort": "high",
                       "timeout": 1500, "max_tokens": 32768},
                "zh": {"provider": "glm", "model": "glm-5.3", "reasoning_effort": "high",
                       "timeout": 1500, "max_tokens": 32768}}

    def dispatch_with_choice(self, attempt):
        _, state = self.mixed_pause()
        root = self.runs / state["id"]
        s.save_json(root / "resume_dispatch.json", {"schema": 1, "run_id": attempt,
                      "source_sha256": s.sha256(root / "state.json")})
        destination = self.runs / attempt
        destination.mkdir(parents=True, exist_ok=True)
        s.save_json(destination / "model_choice.json",
                    {"schema": 1, "config": self.selected_config()})
        self.worker.side_effect = self.run_worker
        return state, destination

    def test_resume_model_choice_uses_saved_config_after_preview(self):
        import model_api
        state, destination = self.dispatch_with_choice("20261118_110002_a11ce1")
        selected = self.selected_config()
        with patch.object(model_api, "load_config", return_value=copy.deepcopy(selected)), \
                patch.object(model_api, "validate_config", side_effect=lambda value, **kw: copy.deepcopy(value)), \
                contextlib.redirect_stdout(io.StringIO()):
            c.resume_run(self.client, state["id"], apply=True, attempt=destination.name)
        result = json.loads((destination / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(result["model_config_source"], "selected_saved")
        saved = json.loads((destination / "model_config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["qc"]["model"], "glm-5.3")

    def test_resume_model_choice_fails_closed_when_saved_config_drifts(self):
        import model_api
        state, destination = self.dispatch_with_choice("20261118_110003_a11ce2")
        drifted = self.selected_config()
        drifted["qc"]["model"] = "glm-5.3-flash"
        with patch.object(model_api, "load_config", return_value=drifted), \
                patch.object(model_api, "validate_config", side_effect=lambda value, **kw: copy.deepcopy(value)), \
                contextlib.redirect_stdout(io.StringIO()):
            c.resume_run(self.client, state["id"], apply=True, attempt=destination.name)
        result = json.loads((destination / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("已变化", result["error"])

    def test_default_resume_keeps_frozen_model(self):
        _, state = self.mixed_pause()
        root = self.runs / state["id"]
        attempt = "20261118_110004_ff0204"
        s.save_json(root / "resume_dispatch.json", {"schema": 1, "run_id": attempt,
                      "source_sha256": s.sha256(root / "state.json")})
        self.worker.side_effect = self.run_worker
        with contextlib.redirect_stdout(io.StringIO()):
            c.resume_run(self.client, state["id"], apply=True, attempt=attempt)
        result = json.loads((self.runs / attempt / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(result["model_config_source"], "frozen_original")
        self.assertFalse((self.runs / attempt / "model_choice.json").exists())


class LegacyRecoveryTests(ControllerFixture):
    KEY_OK, KEY_BAD, KEY_PAUSE = "ABCD2345", "EFGH6789", "IJKL1234"
    CK_OK, CK_BAD, CK_PAUSE = "Ok2026JPO-Mixing", "Bad2026GRL-Waves", "Pause2026DSR-Deep"
    RUN = "20261118_102159_cd926e"

    def setUp(self):
        super().setUp()
        self.items[:] = [x for x in self.items if x.get("key") != "ABCD2345"]
        self.results.pop("ABCD2345", None)
        self.stack.enter_context(patch.object(progress, "owner_matches", return_value=False))
        self.run_dir = self.runs / self.RUN
        self.run_dir.mkdir(parents=True)
        self.papers = []
        for key, ck in ((self.KEY_OK, self.CK_OK), (self.KEY_BAD, self.CK_BAD),
                        (self.KEY_PAUSE, self.CK_PAUSE)):
            self.items.append({"key": key, "data": {"key": key, "itemType": "journalArticle",
                                                    "citationKey": ck, "title": "t",
                                                    "dateAdded": "2026-11-18T00:00:00Z", "language": "en"}})
            primary = self.root / (ck + ".pdf")
            primary.write_bytes(b"%PDF-1.4 fixture\n")
            paper = {"key": key, "citekey": ck, "title": "t", "status": "applied", "blocked": [],
                     "redo_required": False, "require_translation": True,
                     "primary_key": "PDF" + key[-4:], "primary_pdf": str(primary),
                     "source_sha256": s.sha256(primary), "log": str(self.run_dir / (key + ".log")),
                     "progress_phase": "paused", "translate": True}
            (self.run_dir / (key + ".log")).write_text("PAUSED at boundary\n", encoding="utf-8")
            self.items.append({"key": "PDF" + key[-4:], "data": {
                "key": "PDF" + key[-4:], "itemType": "attachment", "parentItem": key,
                "contentType": "application/pdf", "filename": ck + ".pdf"}})
            self.results[key] = paper
            self.papers.append(paper)
        self.client.file_path.side_effect = lambda key: next(
            p["primary_pdf"] for p in self.papers if p["primary_key"] == key)

    def family(self, key):
        parent = next(x for x in self.items if x["key"] == key)
        return {"parent": copy.deepcopy(parent),
                "children": copy.deepcopy([x for x in self.items if x["data"].get("parentItem") == key]),
                "library_version": 7}

    def seed_legacy_state(self):
        # OK paper: complete products recorded in qa_frozen (validated in-run).
        self.seed_paper(self.KEY_OK)
        ok = self.papers[0]
        ok["exit_code"] = 0
        ok["progress_phase"] = "validated"
        s.validate_completed_paper(ok)
        frozen = dict(s.fingerprint([self.lib / "papers" / self.CK_OK / (self.CK_OK + f"_{x}.md")
                                     for x in ("EN", "ZH", "Note")]))
        # BAD paper: exit 0 with failed QA (EN only) — the historical defect shape.
        self.seed_paper(self.KEY_BAD, translation=False)
        bad = self.papers[1]
        bad["exit_code"] = 0
        bad["progress_phase"] = "failed"
        bad["qa"] = {"ok": False, "errors": [{"code": "translation_missing", "message": "外文论文缺少 ZH 翻译。"}]}
        # PAUSE paper: exit 75 with an un-QC'd fulltext on disk.
        self.seed_paper(self.KEY_PAUSE, translation=False)
        en = self.lib / "papers" / self.CK_PAUSE / (self.CK_PAUSE + "_EN.md")
        text = en.read_text(encoding="utf-8")
        marker = next(line for line in text.splitlines() if line.startswith("qc:"))
        en.write_text(text.replace(marker + "\n", ""), encoding="utf-8")
        paused = self.papers[2]
        paused["exit_code"] = 75
        snapshot = s.live_snapshot({"time": "2026-11-18T10:00:00", "items": copy.deepcopy(self.items),
                                    "collections": []})
        s.save_json(self.run_dir / "snapshot.json", snapshot)
        owned = [self.lib / "papers" / p["citekey"] / f"{p['citekey']}_{x}.md"
                 for p in self.papers for x in ("EN", "MD", "ZH", "Note")]
        before = s.fingerprint([self.lib / "papers" / self.CK_OK / (self.CK_OK + f"_{x}.md")
                                for x in ("EN", "ZH", "Note")])
        state = {"id": self.RUN, "control_schema": 1, "status": "blocked",
                 "error": "RuntimeError: 产物验收未通过；未重建索引/提交；详见各篇 qa 和日志",
                 "started": "2026-11-18T10:21:59", "finished": "2026-11-18T10:40:00",
                 "monitor_done": True, "requested": [p["key"] for p in self.papers],
                 "papers": self.papers, "planned_papers": [{"key": p["key"], "citekey": p["citekey"],
                                                            "title": "t"} for p in self.papers],
                 "deferred": [], "warnings": [], "translate": True, "commit": True,
                 "workers": 2, "chunk_parallel": 1,
                 "controller": progress.process_identity(os.getpid()),
                 "report": str(self.reports / f"library_sync_{self.RUN}.md"),
                 "baseline": {"schema": 1, "before": before,
                              "owned": [str(p) for p in owned],
                              "library_dirty": [], "tools_dirty": [],
                              "before_index": s.index_fingerprint()},
                 "qa_frozen": frozen, "phase": "qa"}
        s.save_json(self.run_dir / "state.json", state)
        s.save_json(self.run_dir / "pause_request.json",
                    {"schema": 1, "run_id": self.RUN, "requested": "2026-11-18T10:30:00"})
        return state

    def test_recovery_preview_is_read_only_and_classifies(self):
        self.seed_legacy_state()
        before = (self.run_dir / "state.json").read_bytes()
        prepared = c.prepare_recovery(self.client, self.RUN)
        self.assertEqual((self.run_dir / "state.json").read_bytes(), before)
        self.assertFalse((self.run_dir / "pause_receipt.json").exists())
        self.assertIn(self.KEY_BAD, prepared["needs_action"])
        self.assertEqual(prepared["steps"][self.KEY_OK], [])
        self.assertEqual(prepared["steps"][self.KEY_PAUSE], ["qc", "zh"])
        self.assertTrue(prepared["recovery"])
        self.assertIn(self.KEY_PAUSE, prepared["evidence"])

    def test_recovery_refuses_when_receipt_or_dispatch_exists(self):
        self.seed_legacy_state()
        s.save_json(self.run_dir / "pause_receipt.json", {"schema": 2, "run_id": self.RUN, "papers": {}})
        with self.assertRaisesRegex(RuntimeError, "已有暂停凭据"):
            c.prepare_recovery(self.client, self.RUN)
        (self.run_dir / "pause_receipt.json").unlink()
        s.save_json(self.run_dir / "resume_dispatch.json", {"schema": 1, "run_id": "20261118_120000_00ba01"})
        with self.assertRaisesRegex(RuntimeError, "已派发过恢复"):
            c.prepare_recovery(self.client, self.RUN)

    def test_recovery_child_completes_incomplete_and_preserves_history(self):
        self.seed_legacy_state()
        original_bytes = (self.run_dir / "state.json").read_bytes()
        attempt = "20261118_130000_0ddba5"
        s.save_json(self.run_dir / "resume_dispatch.json",
                    {"schema": 1, "run_id": attempt, "source": self.RUN,
                     "source_sha256": s.sha256(self.run_dir / "state.json"), "recovery": True})
        real_worker = self.run_worker

        def recovering_worker(args, log, env, **kwargs):
            # The mocked qc step writes the missing marker, then the shared
            # fixture seeds the remaining artifacts for the zh step.
            if "--only" in args:
                ck = args[args.index("--only") + 1]
                en = self.lib / "papers" / ck / (ck + "_EN.md")
                if en.is_file() and "qc:" not in en.read_text(encoding="utf-8"):
                    lines = en.read_text(encoding="utf-8").splitlines(keepends=True)
                    closing = lines.index("---\n", 1)
                    lines.insert(closing, "qc: synthetic qc marker 2026-11-18\n")
                    en.write_text("".join(lines), encoding="utf-8")
            return real_worker(args, log, env, **kwargs)
        self.worker.side_effect = recovering_worker
        with contextlib.redirect_stdout(io.StringIO()):
            code = c.resume_run(self.client, self.RUN, apply=True, attempt=attempt, recover_legacy=True)
        result = json.loads((self.runs / attempt / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(result["recovered_from"], self.RUN)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["workflow_complete"])
        by_key = {p["key"]: p for p in result["papers"]}
        self.assertEqual(set(by_key), {self.KEY_OK, self.KEY_PAUSE})
        self.assertTrue(by_key[self.KEY_PAUSE]["qa"]["ok"])
        self.assertEqual({row["key"] for row in result["needs_action"]}, {self.KEY_BAD})
        # Historical record untouched: bytes identical, no fabricated receipt.
        self.assertEqual((self.run_dir / "state.json").read_bytes(), original_bytes)
        self.assertFalse((self.run_dir / "pause_receipt.json").exists())
        # Only the incomplete paper ran a worker in the recovery child.
        recovered = [args for args, _, _ in self.worker_calls if "resume_" in args[-1]]
        self.assertEqual([a[a.index("--only") + 1] for a in recovered], [self.CK_PAUSE])


if __name__ == "__main__":
    unittest.main()
