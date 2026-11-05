"""Read-only viewer regressions. All fixtures are temporary; no API or workers.

Run from tools: py -3 -B -m unittest test_library_progress -v
"""
import contextlib
import ctypes
from ctypes import wintypes
import datetime as dt
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import library_progress as p


RUN = "20261018_120000_a1b2c3"
OWNER = {"pid": 1234, "created": "134052624001234567", "exe": r"C:\Python\python.exe"}
PAPER = {"key": "ABCD2345", "citekey": "Example2026JPO-Mixing", "title": "Mixing"}
SECOND = {"key": "EFGH6789", "citekey": "Other2026JPO-Flow", "title": "Flow"}
START = dt.datetime(2026, 10, 18, 12).timestamp()
NOW = START + 300


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="library_progress_test_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs, self.library = self.root / "runs", self.root / "Library"
        self.run = self.runs / RUN
        self.run.mkdir(parents=True)
        self.state = {"id": RUN, "status": "running", "started": START,
                      "controller": dict(OWNER), "monitor_done": False,
                      "phase": "workers", "phase_started": START + 10,
                      "planned_papers": [dict(PAPER)], "papers": [],
                      "report": str(self.root / "report.md")}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.identity = self.stack.enter_context(patch.object(p, "process_identity", return_value=dict(OWNER)))
        self.stack.enter_context(patch.object(p.time, "sleep"))
        self.put(self.runs / "active.json", dict(OWNER, id=RUN))

    def put(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def view(self, **updates):
        self.state.update(updates)
        self.put(self.run / "state.json", self.state)
        return p.snapshot(self.runs, RUN, self.library, now=NOW)

    def lock(self, paper=PAPER, phase="qc", age=80):
        folder = self.library / "papers" / paper["citekey"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f".sync_{phase}.lock"
        path.write_text(f"oneclick_{paper['key']} 2026-10-18T12:03:40", encoding="utf-8")
        os.utime(path, (NOW - age, NOW - age))
        return path

    def receipt(self, paper=PAPER, **updates):
        return dict(paper, exit_code=0, qa={"ok": True}, **updates)

    def watch(self, **kwargs):
        self.output = io.StringIO()
        return p.watch(self.runs, RUN, self.library, stream=self.output,
                       clock=lambda: NOW, sleep=Mock(), **kwargs)


class ActiveTests(Fixture):
    def test_live_record_and_monitor_done(self):
        self.assertEqual(p.active_run(self.runs)["id"], RUN)
        self.view(status="completed", monitor_done=True)
        self.assertIsNone(p.active_run(self.runs))

    def test_missing_or_dead_or_reused_pid(self):
        for identity in (None, dict(OWNER, created="different"), dict(OWNER, exe=r"C:\Other\python.exe")):
            with self.subTest(identity=identity):
                self.identity.return_value = identity
                self.assertIsNone(p.active_run(self.runs))
        self.assertIsNone(p.active_run(self.root / "absent"))

    def test_access_denied_is_not_absence(self):
        self.identity.side_effect = PermissionError("secret token=private")
        with self.assertRaises(PermissionError):
            p.active_run(self.runs)

    def test_owner_path_case_and_exact_creation_time(self):
        self.assertTrue(p.owner_matches(dict(OWNER, exe=r"c:\PYTHON\PYTHON.EXE")))
        self.assertFalse(p.owner_matches(dict(OWNER, created=OWNER["created"][:-1] + "8")))

    def test_corrupt_active_fails_closed(self):
        for text in ('{', '[]', '{"id":"a","id":"b"}', json.dumps(dict(OWNER, id="../escape")),
                     json.dumps({"id": RUN, "pid": 1234})):
            with self.subTest(text=text):
                (self.runs / "active.json").write_text(text, encoding="utf-8")
                with self.assertRaises(p.ProgressError):
                    p.active_run(self.runs)


class SnapshotTests(Fixture):
    def test_paper_reports_only_known_completion_reason(self):
        paper = dict(PAPER, exit_code=0, qa={"ok": False}, qc_log=[
            "!! qc failed Example: incomplete_completion (finish_reason=length)"])
        view = self.view(status="blocked", monitor_done=True, papers=[paper])
        self.assertIn("质检回复未正常结束（finish_reason=length）", view["papers"][0]["note"])
        paper["qc_log"] = ["!! qc failed Example: incomplete_completion (finish_reason=private-response-body)"]
        view = self.view(status="blocked", monitor_done=True, papers=[paper])
        self.assertNotIn("private-response-body", str(view))

    def test_pending_plan_is_visible_without_worker(self):
        view = self.view()
        self.assertFalse(view["terminal"])
        self.assertEqual((view["accepted"], view["total"]), (0, 1))
        self.assertEqual(view["papers"][0]["phase"], "pending")
        self.assertIsNone(view["papers"][0]["elapsed_seconds"])

    def test_controller_json_covers_startup_gap(self):
        (self.runs / "active.json").unlink()
        self.put(self.run / "controller.json", dict(OWNER, id=RUN))
        view = p.snapshot(self.runs, RUN, self.library, now=NOW)
        self.assertEqual(view["status"], "pending")
        self.assertFalse(view["terminal"])

    def test_missing_all_records_is_not_running_forever(self):
        (self.runs / "active.json").unlink()
        view = p.snapshot(self.runs, RUN, self.library, now=NOW)
        self.assertEqual(view["status"], "attention")
        self.assertTrue(view["terminal"])

    def test_partial_write_and_atomic_replace_gap_retry(self):
        for first in ('{"id":', FileNotFoundError()):
            self.view()
            original = Path.read_text
            reads = []
            def read(path, *args, **kwargs):
                if path == self.run / "state.json" and not reads:
                    reads.append(True)
                    if isinstance(first, Exception):
                        raise first
                    return first
                return original(path, *args, **kwargs)
            with self.subTest(first=first), patch.object(Path, "read_text", read):
                self.assertEqual(p.snapshot(self.runs, RUN, self.library, now=NOW)["total"], 1)

    def test_persistent_corruption_and_invalid_id_fail_closed(self):
        (self.run / "state.json").write_text('{"token":"private"', encoding="utf-8")
        with self.assertRaises(p.ProgressError) as error:
            p.snapshot(self.runs, RUN, self.library, now=NOW)
        self.assertNotIn("private", str(error.exception))
        for value in ("mock_run", "../state", RUN + "\n", RUN + "/", "20261018_120000_A1B2C3"):
            with self.subTest(value=value), self.assertRaises(p.ProgressError):
                p.snapshot(self.runs, value, self.library, now=NOW)

    def test_two_papers_merge_by_key_and_independent_locks(self):
        self.lock(phase="qc", age=80)
        self.lock(SECOND, "zh", 25)
        view = self.view(planned_papers=[PAPER, SECOND], papers=[dict(PAPER)])
        self.assertEqual(view["total"], 2)
        self.assertEqual([x["phase"] for x in view["papers"]], ["qc", "zh"])
        self.assertEqual([x["elapsed_seconds"] for x in view["papers"]], [80, 25])
        self.assertEqual(view["elapsed_seconds"], 300)

    def test_md_lock_and_stage_clock(self):
        self.lock(phase="md", age=65)
        self.assertEqual(self.view()["papers"][0]["phase"], "md")
        self.assertEqual(self.view()["phase_elapsed_seconds"], 290)

    def test_old_future_and_ambiguous_locks_are_not_evidence(self):
        path = self.lock(age=400)
        self.assertEqual(self.view()["papers"][0]["phase"], "pending")
        os.utime(path, (NOW + 10, NOW + 10))
        self.assertEqual(self.view()["papers"][0]["phase"], "pending")
        os.utime(path, (NOW - 30, NOW - 30))
        self.lock(phase="zh")
        view = self.view()["papers"][0]
        self.assertEqual(view["phase"], "pending")
        self.assertIn("多个", view["note"])

    def test_no_locks_and_old_products_never_mean_complete(self):
        folder = self.library / "papers" / PAPER["citekey"]
        folder.mkdir(parents=True)
        for suffix in ("EN", "ZH", "Note"):
            (folder / f"{PAPER['citekey']}_{suffix}.md").write_text("old", encoding="utf-8")
        self.assertEqual(self.view()["accepted"], 0)
        self.assertEqual(self.view()["papers"][0]["phase"], "pending")

    def test_only_strict_qa_and_exit_zero_accept(self):
        for qa, code, accepted in ((True, 0, True), (True, None, False), (False, 0, False),
                                    (True, 1, False), (1, 0, False), (True, False, False)):
            with self.subTest(qa=qa, code=code):
                view = self.view(papers=[dict(PAPER, qa={"ok": qa}, exit_code=code)])
                self.assertEqual(view["accepted"], int(accepted))

    def test_current_intake_and_qa_and_failed_phases(self):
        view = self.view(phase="intake", current_key=PAPER["key"])
        self.assertEqual(view["papers"][0]["phase"], "intake")
        for phase in ("qa", "failed"):
            view = self.view(papers=[dict(PAPER, progress_phase=phase)])
            self.assertEqual(view["papers"][0]["phase"], phase)

    def test_qa_success_still_waits_for_indexes_git_and_notification(self):
        for phase in ("snapshot", "index_stage", "publishing_indexes", "indexes_published",
                      "library_commit_started", "finalized"):
            with self.subTest(phase=phase):
                view = self.view(papers=[self.receipt()], finalize_phase=phase)
                self.assertEqual(view["accepted"], 1)
                self.assertEqual(view["phase"], phase)  # phase=workers cannot mask finalize.
                self.assertFalse(view["terminal"])
        view = self.view(status="completed")
        self.assertEqual(view["status"], "finalizing")
        self.assertFalse(view["terminal"])

    def test_all_terminal_statuses_wait_for_monitor_done(self):
        for status in ("completed", "partial", "blocked", "attention", "failed"):
            with self.subTest(status=status):
                view = self.view(status=status, monitor_done=False, papers=[self.receipt()])
                self.assertFalse(view["terminal"])
                view = self.view(monitor_done=True)
                self.assertTrue(view["terminal"])
                self.assertEqual(view["status"], status)

    def test_finalizing_teardown_names_failure_and_promises_no_git_or_popup(self):
        # 0.2.0-beta.6: the teardown only persists report/records; a failure is
        # stated as confirmed and must not promise Git work or a notification.
        view = self.view(status="blocked", error="RuntimeError: 未质检原文有用户修改", papers=[])
        self.assertEqual(view["status"], "finalizing")
        self.assertFalse(view["terminal"])
        notices = " ".join(view["notices"])
        self.assertIn("失败已确认，正在保存报告与收尾记录", notices)
        self.assertIn("不可恢复或重新启动", notices)
        self.assertNotIn("通知", notices)
        self.assertNotIn("Git", notices)
        # The owner is still alive: no pause of a finished run, no resume, and
        # index/Git must show failure evidence instead of "等待" promises.
        self.assertFalse(view["can_pause"])
        self.assertFalse(view["can_resume"])
        self.assertFalse(view["can_resume_finalize"])
        self.assertNotIn("等待", view["index_status"])
        self.assertNotIn("等待", view["git_status"])
        self.assertIn("未确认完成", view["index_status"])
        self.assertIn("未确认完成", view["git_status"])
        # This second scenario must not inherit the prior blocked-case error.
        view = self.view(status="completed", error=None, papers=[self.receipt()])
        self.assertIn("终态已确认", " ".join(view["notices"]))
        self.assertNotIn("通知", " ".join(view["notices"]))

    def test_finalizing_preserves_published_index_and_commit_evidence(self):
        self.state.update(finalize_phase="indexes_published", library_commit="abc1234")
        view = self.view(status="blocked", error="RuntimeError: 报告前失败", papers=[])
        self.assertEqual(view["status"], "finalizing")
        self.assertEqual(view["index_status"], "已发布")
        self.assertIn("已提交", view["git_status"])
        self.assertFalse(view["can_pause"])

    def test_orphan_interrupted_run_shows_failure_evidence_not_waiting(self):
        # A raw "running" record whose owner died derives "interrupted"; its
        # index/Git must state the unconfirmed outcome, never "等待".
        self.identity.return_value = None
        view = self.view(status="running", papers=[])
        self.assertEqual(view["status"], "interrupted")
        self.assertTrue(view["terminal"])
        self.assertIn("未确认完成", view["index_status"])
        self.assertIn("未确认完成", view["git_status"])
        self.assertNotIn("等待", view["index_status"])
        self.assertNotIn("等待", view["git_status"])
        self.assertFalse(view["can_pause"])

    def test_unknown_raw_status_with_live_owner_cannot_pause(self):
        view = self.view(status="garbage-status", papers=[])
        self.assertEqual(view["status"], "attention")
        self.assertTrue(view["terminal"])
        self.assertIn("无法识别任务状态", " ".join(view["notices"]))
        self.assertIn("未确认完成", view["index_status"])
        self.assertFalse(view["can_pause"])
        self.assertFalse(view["safe_to_shutdown"])

    def test_completed_and_partial_finalized_display_done(self):
        for status in ("completed", "partial"):
            view = self.view(status=status, monitor_done=True, workflow_complete=True,
                             finalize_phase="finalized", papers=[self.receipt()], finished=START + 240)
            self.assertEqual(view["phase"], "done")
            self.assertEqual(view["phase_label"], "收尾完成")
            self.assertEqual(view["elapsed_seconds"], 240)
            self.assertNotIn("等待报告", p.render(view))

    def test_dead_or_reused_owner_never_stays_running_or_completed(self):
        for identity in (None, dict(OWNER, created="reused")):
            self.identity.return_value = identity
            for status in ("running", "completed"):
                view = self.view(status=status)
                self.assertEqual(view["status"], "interrupted")
                self.assertIn("收尾未确认", " ".join(view["notices"]))
        self.state.pop("monitor_done")
        self.assertEqual(self.view(status="completed")["status"], "interrupted")

    def test_legacy_completed_without_owner_remains_compatible(self):
        (self.runs / "active.json").unlink()
        self.state.pop("controller")
        self.state.pop("monitor_done")
        view = self.view(status="completed", papers=[self.receipt()], finalize_phase="finalized")
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["phase"], "done")

    def test_corrupt_completion_cannot_bypass_qa(self):
        self.assertEqual(self.view(status="completed", monitor_done=True)["status"], "attention")

    def test_render_has_required_fields_no_secrets_logs_eta_or_percent(self):
        view = self.view(error="Authorization: Bearer private", warnings=["token=private"],
                         papers=[dict(PAPER, qc_log=["private"], log="secret.log")])
        text = p.render(view)
        for required in (RUN, "状态：", "总耗时：", "阶段耗时：", "验收：0/1", "索引：", "Git：", "报告路径：", "不影响后台"):
            self.assertIn(required, text)
        for forbidden in ("private", "secret.log", "ETA", "%", "\x1b"):
            self.assertNotIn(forbidden, text)

    def test_path_traversal_and_bad_state_identity_rejected(self):
        view = self.view(planned_papers=[dict(PAPER, citekey="../escape")])
        self.assertEqual(view["papers"][0]["source"], "none")
        self.assertIn("暂不读取", view["papers"][0]["note"])
        with self.assertRaises(p.ProgressError):
            self.view(id="20261018_120001_a1b2c3")

    def test_provisional_colon_key_then_final_key_keeps_one_item(self):
        old = dict(PAPER, citekey="Demo2026JournalofGeophysicalResearch:Oceans-Title")
        before = self.view(phase="intake", current_key=PAPER["key"], planned_papers=[old], papers=[])
        self.assertEqual(before["total"], 1)
        self.assertEqual(before["papers"][0]["phase"], "intake")
        self.assertIn("最终 citekey", before["papers"][0]["note"])
        self.lock(phase="qc")
        after = self.view(planned_papers=[old], papers=[PAPER])
        self.assertEqual(after["total"], 1)
        self.assertEqual(after["papers"][0]["phase"], "qc")

    def test_different_item_keys_with_same_provisional_citekey_remain_separate(self):
        provisional = "Demo2026Journal:Oceans-Title"
        view = self.view(planned_papers=[dict(PAPER, citekey=provisional), dict(SECOND, citekey=provisional)],
                         papers=[PAPER, SECOND])
        self.assertEqual(view["total"], 2)
        self.assertEqual({x["key"] for x in view["papers"]}, {PAPER["key"], SECOND["key"]})

    def test_bad_one_paper_does_not_hide_second(self):
        view = self.view(papers=[dict(PAPER, qa="bad"), SECOND])
        self.assertEqual(view["total"], 2)
        self.assertEqual(view["papers"][0]["source"], "state_error")
        self.assertEqual(view["papers"][1]["key"], SECOND["key"])

    def test_terminal_footer_does_not_claim_background_is_still_running(self):
        view = self.view(status="blocked", monitor_done=True)
        text = p.render(view)
        self.assertIn("本次任务已结束", text)
        self.assertIn("-Watch", text)
        self.assertNotIn("不影响后台任务", text)


class WatchTests(Fixture):
    def test_terminal_exit_codes(self):
        for status, code in (("completed", 0), ("partial", 2), ("blocked", 2), ("attention", 2)):
            self.view(status=status, monitor_done=True, papers=[self.receipt()])
            self.assertEqual(self.watch(), code)

    def test_max_updates_does_not_claim_success_or_ansi(self):
        self.view()
        self.assertEqual(self.watch(max_updates=2), 2)
        self.assertEqual(self.output.getvalue().count("文献同步进度"), 2)
        self.assertNotIn("\x1b", self.output.getvalue())

    def test_windows_tty_never_emits_unsupported_ansi(self):
        self.view(status="completed", monitor_done=True, papers=[self.receipt()])
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch.object(p.os, "name", "nt"):
            self.assertEqual(p.watch(self.runs, RUN, self.library, stream=stream, clock=lambda: NOW), 0)
        self.assertNotIn("\x1b", stream.getvalue())

    def test_ctrl_c_only_closes_viewer_and_never_signals_owner(self):
        self.view()
        before = (self.run / "state.json").read_bytes()
        with patch.object(p.os, "kill", side_effect=AssertionError("no signals")) as kill:
            self.assertEqual(p.watch(self.runs, RUN, self.library, stream=io.StringIO(),
                                    clock=lambda: NOW, sleep=Mock(side_effect=KeyboardInterrupt)), 0)
        kill.assert_not_called()
        self.assertEqual(before, (self.run / "state.json").read_bytes())

    def test_read_errors_are_short_safe_and_not_success(self):
        self.view()
        self.identity.side_effect = PermissionError("token=private")
        self.assertEqual(self.watch(max_updates=1), 2)
        self.assertIn("未确认成功", self.output.getvalue())
        self.assertNotIn("private", self.output.getvalue())

    def test_stalled_evidence_is_explained(self):
        view = self.view()
        output = io.StringIO()
        code = p.watch(self.runs, RUN, self.library, stream=output, max_updates=2,
                       clock=Mock(side_effect=[NOW, NOW + 1000]), sleep=Mock(),
                       snapshot_fn=lambda *a, **k: view)
        self.assertEqual(code, 2)
        self.assertIn("长时间未变化", output.getvalue())

    def test_reads_are_only_json_and_lock_stat_no_writes_api_or_processes(self):
        self.lock()
        self.view()
        before = {str(f): (f.read_bytes(), f.stat().st_mtime_ns)
                  for f in self.root.rglob("*") if f.is_file()}
        original = Path.open
        def read_only(path, mode="r", *args, **kwargs):
            self.assertFalse(set(mode) & set("wax+"))
            self.assertIn(path.name, ("state.json", "active.json", "controller.json"))
            return original(path, mode, *args, **kwargs)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(Path, "open", read_only))
            for module, name in ((subprocess, "Popen"), (subprocess, "run"), (os, "kill"),
                                 (os, "replace"), (os, "remove"), (socket, "socket"),
                                 (Path, "mkdir"), (Path, "unlink"), (Path, "touch")):
                stack.enter_context(patch.object(module, name, side_effect=AssertionError("forbidden mutation/API")))
            self.assertIsNotNone(p.active_run(self.runs))
            self.assertEqual(self.watch(max_updates=2), 2)
        after = {str(f): (f.read_bytes(), f.stat().st_mtime_ns)
                 for f in self.root.rglob("*") if f.is_file()}
        self.assertEqual(before, after)


@unittest.skipUnless(os.name == "nt", "Windows process identity API")
class ProcessTests(unittest.TestCase):
    def test_real_current_process_identity_is_stable(self):
        owner = p.process_identity(os.getpid())
        self.assertEqual(owner["pid"], os.getpid())
        self.assertTrue(Path(owner["exe"]).is_absolute())
        self.assertTrue(owner["created"].isdigit())
        self.assertTrue(p.owner_matches(owner))

    def kernel(self):
        def times(handle, created, *rest):
            value = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
            ticks = int(OWNER["created"])
            value.dwLowDateTime, value.dwHighDateTime = ticks & 0xFFFFFFFF, ticks >> 32
            return True
        def name(handle, flags, output, size):
            output.value = OWNER["exe"]
            return True
        return types.SimpleNamespace(OpenProcess=Mock(return_value=10), WaitForSingleObject=Mock(return_value=258),
                                     GetProcessTimes=Mock(side_effect=times),
                                     QueryFullProcessImageNameW=Mock(side_effect=name), CloseHandle=Mock())

    def test_filetime_keeps_all_bits_and_closes_read_handle(self):
        kernel = self.kernel()
        with patch.object(p, "_kernel32", return_value=kernel):
            self.assertEqual(p.process_identity(1234), OWNER)
        kernel.CloseHandle.assert_called_once_with(10)
        self.assertEqual(kernel.OpenProcess.call_args.args[0], 0x101000)

    def test_absent_access_denied_and_exited_are_distinguished(self):
        kernel = self.kernel()
        kernel.OpenProcess.return_value = None
        with patch.object(p, "_kernel32", return_value=kernel), patch.object(ctypes, "get_last_error", return_value=87):
            self.assertIsNone(p.process_identity(1234))
        with patch.object(p, "_kernel32", return_value=kernel), patch.object(ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError):
                p.process_identity(1234)
        kernel.OpenProcess.return_value = 10
        kernel.WaitForSingleObject.return_value = 0
        with patch.object(p, "_kernel32", return_value=kernel):
            self.assertIsNone(p.process_identity(1234))
        kernel.CloseHandle.assert_called_once_with(10)

    def test_inspection_failure_closes_handle_and_raises(self):
        kernel = self.kernel()
        kernel.QueryFullProcessImageNameW.side_effect = None
        kernel.QueryFullProcessImageNameW.return_value = False
        with patch.object(p, "_kernel32", return_value=kernel), patch.object(ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError):
                p.process_identity(1234)
        kernel.CloseHandle.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
