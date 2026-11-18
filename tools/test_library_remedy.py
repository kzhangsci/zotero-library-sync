"""Remedy-engine tests: staged candidates, tokens, evidence-gated actions.

No real API/model/Git: workers are patched at s.run_sync and MUST write into
the staged Library passed via library_root (the harness enforces that a mock
which writes into the live folder fails loudly), credentials are synthetic,
and every file lives in a temporary Library/run cache.
"""
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import library_remedy as remedy
import library_sync as s

KEY = "ABCD2345"
CK = "Example2026JPO-Mixing"
TITLE = "Ocean mixing"
SOURCE = "Observed mixing changes the stratification in the water column."
CONFIG = {"schema": 1, "qc": {"provider": "glm", "model": "glm-5.3", "reasoning_effort": "high",
                              "timeout": 1500, "max_tokens": 32768},
          "zh": {"provider": "glm", "model": "glm-5.3-flash", "reasoning_effort": "high",
                 "timeout": 1500, "max_tokens": 32768}}


def front_matter(values):
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in values.items()) + "\n---\n"


class RemedyFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.lib = self.root / "tools_repo", self.root / "Library"
        self.reports = self.repo / "reports"
        self.runs = self.reports / "_cache" / "library_sync"
        self.reports.mkdir(parents=True)
        (self.lib / "papers").mkdir(parents=True)
        (self.lib / "indexes").mkdir()
        self.primary = self.root / (CK + ".pdf")
        self.primary.write_bytes(b"%PDF-1.4\n% synthetic fixture, never parsed\n")
        self.family_parent = {"key": KEY, "version": 7, "data": {
            "key": KEY, "version": 7, "citationKey": CK, "itemType": "journalArticle",
            "title": TITLE, "DOI": "10.1234/example", "dateAdded": "2026-10-14T01:00:00Z"}}
        self.attachment = {"key": "DEFG5678", "version": 3, "data": {
            "key": "DEFG5678", "version": 3, "itemType": "attachment", "parentItem": KEY,
            "contentType": "application/pdf", "filename": CK + ".pdf"}}
        self.client = Mock()
        self.client.parent_children.return_value = {"parent": copy.deepcopy(self.family_parent),
                                                    "children": [copy.deepcopy(self.attachment)],
                                                    "library_version": 7}
        self.client.file_path.return_value = str(self.primary)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(s, REPO=self.repo, REPORTS=self.reports,
                                                CACHE=self.reports / "_cache", RUNS=self.runs,
                                                LIBRARY=self.lib))
        self.stack.enter_context(patch.object(s, "active_controller", return_value=None))
        self.stack.enter_context(patch.object(s, "assert_idle"))
        self.stack.enter_context(patch.object(s, "workflow_lock",
                                              side_effect=lambda: contextlib.nullcontext()))
        self.worker = self.stack.enter_context(patch.object(s, "run_sync"))
        import model_api
        self.stack.enter_context(patch.object(model_api, "load_config", return_value=copy.deepcopy(CONFIG)))
        self.stack.enter_context(patch.object(model_api, "validate_config",
                                              side_effect=lambda value, **kwargs: copy.deepcopy(value)))
        self.run_id = "20261118_090000_aabbcc"
        self.run_dir = self.runs / self.run_id
        self.run_dir.mkdir(parents=True)
        self.paper = {"key": KEY, "citekey": CK, "title": TITLE, "status": "applied",
                      "primary_pdf": str(self.primary), "primary_key": "DEFG5678",
                      "source_sha256": s.sha256(self.primary), "exit_code": 0,
                      "require_translation": True, "translate": True,
                      "log": str(self.run_dir / (KEY + ".log")),
                      "qa": None, "progress_phase": "failed"}
        (self.run_dir / (KEY + ".log")).write_text(
            "qc done\n!! qc failed " + CK + ": incomplete_completion (finish_reason=length)\n",
            encoding="utf-8")
        self.seed()

    def staged(self, kwargs):
        """The staged Library a mocked worker must write into."""
        root = kwargs.get("library_root")
        assert root is not None, "worker actions must run against a staged Library copy"
        return Path(root) / "papers" / CK

    def zh_body(self):
        return ("# " + TITLE + "\n\n> [!info] 论文信息\n> Zotero: zotero://select/library/items/" + KEY +
                "\n\n## 混合\n\n> [!quote]- en\n> " + SOURCE + "\n\n观测到的混合改变了水柱中的层结。\n")

    def write_zh(self, folder, body=None):
        (folder / f"{CK}_ZH.md").write_text(
            front_matter({"citekey": CK, "zotero_key": KEY, "doi": "10.1234/example",
                          "title": TITLE, "source": "translated by fixture"})
            + (body if body is not None else self.zh_body()), encoding="utf-8")

    def seed(self, *, zh_extra_front=None, zh_body_text=None, en_front=None):
        folder = self.lib / "papers" / CK
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"citekey": CK, "zotero_key": KEY, "doi": "10.1234/example", "title": TITLE}
        en = front_matter({**meta, "source_pdf": CK + ".pdf", "source": "mineru",
                           "qc": "synthetic qc marker", **(en_front or {})}) + "# Mixing\n\n" + SOURCE + "\n"
        (folder / f"{CK}_EN.md").write_text(en, encoding="utf-8")
        zh_meta = {**meta, "source": "translated by fixture", **(zh_extra_front or {})}
        body = zh_body_text if zh_body_text is not None else self.zh_body()
        (folder / f"{CK}_ZH.md").write_text(front_matter(zh_meta) + body, encoding="utf-8")
        (folder / f"{CK}_Note.md").write_text(
            front_matter({**meta, "created": "2026-11-18", "tags": "[paper]"}) +
            f"# {CK}\n\n> [!info] 论文信息\n> Zotero: zotero://select/library/items/{KEY}\n\n"
            "## 要点\n\n\n## 问题与想法\n\n\n## 关联文献\n\n\n", encoding="utf-8")
        return folder

    def freeze_state(self, **paper_overrides):
        paper = copy.deepcopy(self.paper)
        paper.update(paper_overrides)
        paper.pop("qa", None)
        s.validate_completed_paper(paper)
        state = {"id": self.run_id, "status": "blocked", "requested": [KEY], "papers": [paper],
                 "planned_papers": [{"key": KEY, "citekey": CK, "title": TITLE}],
                 "report": str(self.reports / f"library_sync_{self.run_id}.md"), "monitor_done": True}
        s.save_json(self.run_dir / "state.json", state)
        return state, paper

    def live_hashes(self):
        folder = self.lib / "papers" / CK
        return {p.name: s.sha256(p) for p in sorted(folder.glob("*.md"))}


class DiagnoseTests(RemedyFixture):
    def test_diagnosis_is_bounded_and_translates_codes(self):
        _, paper = self.freeze_state()
        paper["qa"] = {"ok": False, "errors": [
            {"code": "quote_source", "message": "英文对照与原文不一致。", "file": CK + "_ZH.md", "unit": 2},
            {"code": "source_pdf", "message": "source_pdf 与指定正本不一致。", "file": CK + "_EN.md"}]}
        paper["qc_log"] = ["!! qc failed x: incomplete_completion (finish_reason=length)"]
        state = {"id": self.run_id, "papers": [paper], "planned_papers": [], "needs_action":
                 [{"key": KEY, "citekey": CK, "reasons": ["产物验收未通过：quote_source"]}],
                 "report": "r", "finished": "2026-11-18T09:30:00"}
        s.save_json(self.run_dir / "state.json", state)
        value = remedy.diagnose(self.runs, self.run_id, KEY)
        self.assertEqual(value["key"], KEY)
        names = {row["category"] for row in value["classification"]}
        self.assertIn("translation", names)
        self.assertIn("legacy_format", names)  # source_pdf = metadata mismatch
        self.assertNotIn("missing_pdf", names)
        self.assertIn("api", names)
        kinds = {r["kind"] for r in value["remedies"]}
        self.assertIn("worker", kinds)
        self.assertIn("ledger", kinds)
        self.assertLessEqual(len(json.dumps(value)), 40000)
        with self.assertRaisesRegex(RuntimeError, "不包含此文献"):
            remedy.diagnose(self.runs, self.run_id, "ZZZZZZZZ")

    def test_unknown_english_quote_difference_is_translation_not_missing_pdf(self):
        _, paper = self.freeze_state()
        paper["qa"] = {"ok": False, "errors": [{"code": "quote_source",
                                                "message": "quote 无法唯一定位到 EN 的剩余应译段落。"}]}
        state = {"id": self.run_id, "papers": [paper], "planned_papers": [], "report": "r"}
        s.save_json(self.run_dir / "state.json", state)
        value = remedy.diagnose(self.runs, self.run_id, KEY)
        names = {row["category"] for row in value["classification"]}
        self.assertEqual(names, {"translation"})
        repair = next(r for r in value["remedies"] if r["id"] == "repair_zh")
        self.assertTrue(repair["available"])
        retranslate = next(r for r in value["remedies"] if r["id"] == "retranslate_zh")
        self.assertTrue(retranslate["available"])

    def test_malformed_legacy_metadata_gets_migration_not_blind_retry(self):
        self.seed(zh_extra_front={"source": "", "doi": "10.9999/wrong"},
                  zh_body_text="# 破损\n\n没有对照结构的旧译文。\n")
        _, paper = self.freeze_state()
        state = {"id": self.run_id, "papers": [paper], "planned_papers": [], "report": "r"}
        s.save_json(self.run_dir / "state.json", state)
        value = remedy.diagnose(self.runs, self.run_id, KEY)
        names = {row["category"] for row in value["classification"]}
        self.assertIn("legacy_format", names)
        migrate = next(r for r in value["remedies"] if r["id"] == "migrate_metadata")
        self.assertTrue(migrate["available"])

    def test_chinese_md_paper_needs_no_zh_and_is_adoptable(self):
        folder = self.seed()
        (folder / f"{CK}_EN.md").unlink()
        (folder / f"{CK}_MD.md").write_text(
            front_matter({"citekey": CK, "zotero_key": KEY, "doi": "10.1234/example", "title": TITLE,
                          "source_pdf": CK + ".pdf", "source": "mineru", "qc": "synthetic qc marker"})
            + "# 混合\n\n这是中文原文，无需 ZH。\n", encoding="utf-8")
        (folder / f"{CK}_ZH.md").unlink()
        _, paper = self.freeze_state(require_translation=False)
        paper.pop("qa", None)
        paper["require_translation"] = False
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "adopt_existing", confirm=True)
        self.assertTrue(result["ok"], result)
        import library_issues
        self.assertEqual(library_issues.load(self.runs)["items"][KEY]["status"], "complete")


class WorkerActionTests(RemedyFixture):
    def test_preview_discloses_staging_and_backups_without_dispatch(self):
        _, paper = self.freeze_state()
        preview = remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh")
        self.assertEqual(preview["steps"], ["zh"])
        self.assertIn("隔离候选目录", preview["will_run_in"])
        self.assertIn(f"{CK}_ZH.md", preview["will_backup"])
        self.assertIn("glm-5.3-flash", preview["model_settings"])
        self.worker.assert_not_called()

    def test_crashed_worker_writes_bad_candidate_live_bytes_proven_unchanged(self):
        """Root counterexample: a worker that writes a bad ZH and exits 1."""
        _, paper = self.freeze_state()
        before = self.live_hashes()

        def hostile(args, logfile, env, **kwargs):
            staged = self.staged(kwargs)
            self.write_zh(staged, body="# 完全损坏的候选\n\n没有对照。\n")
            Path(logfile).write_text("!! zh failed: boom\n", encoding="utf-8")
            return 1
        self.worker.side_effect = hostile
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh", confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["published"], [])
        self.assertIn("未变", result["message"])
        # The hostile write only reached the staged copy.
        self.assertEqual(self.live_hashes(), before)
        self.assertIn("观测到的混合", (self.lib / "papers" / CK / f"{CK}_ZH.md").read_text(encoding="utf-8"))
        import library_issues
        self.assertEqual(library_issues.load(self.runs)["items"][KEY]["status"], "needs_repair")

    def test_failing_candidate_qa_never_publishes(self):
        _, paper = self.freeze_state()
        before = self.live_hashes()

        def bad_candidate(args, logfile, env, **kwargs):
            staged = self.staged(kwargs)
            self.write_zh(staged, body="# 又一个损坏候选\n\n孤儿段落。\n")
            Path(logfile).write_text("zh done\n", encoding="utf-8")
            return 0
        self.worker.side_effect = bad_candidate
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh", confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("未发布任何字节", result["message"])
        self.assertEqual(self.live_hashes(), before)

    def test_repair_zh_publishes_only_zh_after_staged_qa(self):
        _, paper = self.freeze_state()
        en_before = (self.lib / "papers" / CK / f"{CK}_EN.md").read_bytes()
        note_before = (self.lib / "papers" / CK / f"{CK}_Note.md").read_bytes()

        def repaired(args, logfile, env, **kwargs):
            staged = self.staged(kwargs)
            self.write_zh(staged, body=self.zh_body().replace("观测到的混合", "观测到的混合（修订）"))
            # The worker also touches the staged EN front matter (an extra
            # harmless field). Staging QA still passes, but the publish scope
            # of repair_zh must drop this: only ZH reaches the live folder.
            en = staged / f"{CK}_EN.md"
            en.write_text(en.read_text(encoding="utf-8").replace(
                "---\n", "---\ngenerated: fixture-worker\n", 1), encoding="utf-8")
            Path(logfile).write_text("zh repair: done\n", encoding="utf-8")
            return 0
        self.worker.side_effect = repaired
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh", confirm=True)
        self.assertTrue(result["ok"], result)
        live = self.lib / "papers" / CK
        self.assertIn("（修订）", (live / f"{CK}_ZH.md").read_text(encoding="utf-8"))
        self.assertEqual((live / f"{CK}_EN.md").read_bytes(), en_before)  # scope: ZH only
        self.assertEqual((live / f"{CK}_Note.md").read_bytes(), note_before)
        self.assertTrue(Path(result["backup"]).is_dir())
        import library_issues
        self.assertEqual(library_issues.load(self.runs)["items"][KEY]["status"], "complete")

    def test_retranslate_zh_generates_fresh_candidate_from_current_en(self):
        _, paper = self.freeze_state()
        en_before = (self.lib / "papers" / CK / f"{CK}_EN.md").read_bytes()
        seen_staged_zh = []

        def fresh(args, logfile, env, **kwargs):
            staged = self.staged(kwargs)
            seen_staged_zh.append((staged / f"{CK}_ZH.md").is_file())
            self.write_zh(staged, body=self.zh_body().replace("层结。", "层结。（全新重译）"))
            Path(logfile).write_text("zh done (fresh)\n", encoding="utf-8")
            return 0
        self.worker.side_effect = fresh
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "retranslate_zh", confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen_staged_zh, [False], "staged ZH must be removed before retranslation")
        self.assertIn("全新重译", (self.lib / "papers" / CK / f"{CK}_ZH.md").read_text(encoding="utf-8"))
        self.assertEqual((self.lib / "papers" / CK / f"{CK}_EN.md").read_bytes(), en_before)
        args = self.worker.call_args.args[0]
        self.assertNotIn("--fresh-source", args)

    def test_retry_qc_scope_is_fulltext_only(self):
        folder = self.seed()
        (folder / f"{CK}_EN.md").write_text(
            (folder / f"{CK}_EN.md").read_text(encoding="utf-8").replace("qc: synthetic qc marker\n", ""),
            encoding="utf-8")
        _, paper = self.freeze_state()

        def qc(args, logfile, env, **kwargs):
            staged = self.staged(kwargs)
            text = (staged / f"{CK}_EN.md").read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            lines.insert(lines.index("---\n", 1), "qc: synthetic qc marker\n")
            (staged / f"{CK}_EN.md").write_text("".join(lines), encoding="utf-8")
            Path(logfile).write_text("qc done\n", encoding="utf-8")
            return 0
        self.worker.side_effect = qc
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "retry_qc", confirm=True)
        self.assertTrue(result["ok"], result)
        args = self.worker.call_args.args[0]
        self.assertEqual(args[args.index("--steps") + 1], "qc")

    def test_recover_missing_qc_and_zh_chains_only_one_paper(self):
        folder = self.seed()
        zh = (folder/f'{CK}_ZH.md').read_bytes()
        (folder/f'{CK}_ZH.md').unlink()
        en = folder/f'{CK}_EN.md'
        en.write_text(en.read_text(encoding='utf-8').replace('qc: synthetic qc marker\n',''),encoding='utf-8')
        self.freeze_state()
        before = self.live_hashes()
        note_before = (folder/f'{CK}_Note.md').read_bytes()
        other = self.lib/'papers'/'AlreadyAccepted';other.mkdir()
        kept = other/'AlreadyAccepted_EN.md';kept.write_bytes(b'accepted user content')
        def recover(args, logfile, env, **kwargs):
            self.assertEqual(args[args.index('--only')+1], CK)
            self.assertEqual(args[args.index('--steps')+1], 'qc,zh')
            self.assertNotIn('--fresh-source',args)
            self.assertEqual(self.live_hashes(), before)
            staged = self.staged(kwargs)
            text = (staged/en.name).read_text(encoding='utf-8')
            (staged/en.name).write_text(text.replace('\n---\n','\nqc: synthetic qc marker\n---\n',1),encoding='utf-8')
            (staged/f'{CK}_ZH.md').write_bytes(zh)
            return 0
        self.worker.side_effect = recover
        result = remedy.remedy(self.client,self.runs,self.run_id,KEY,'recover_paper',confirm=True)
        self.assertTrue(result['ok'],result)
        self.assertEqual(kept.read_bytes(),b'accepted user content')
        self.assertEqual((folder/f'{CK}_Note.md').read_bytes(),note_before)
        self.assertTrue(remedy.diagnose(self.runs,self.run_id,KEY)['accepted'])
        self.worker.reset_mock()
        with self.assertRaises(RuntimeError):
            remedy.remedy(self.client,self.runs,self.run_id,KEY,'recover_paper',confirm=True)
        self.worker.assert_not_called()

    def test_recovery_failure_preserves_live_bytes_and_existing_qc_skips_qc(self):
        folder = self.seed();(folder/f'{CK}_ZH.md').unlink()
        self.freeze_state();before=self.live_hashes()
        self.worker.return_value=1
        result=remedy.remedy(self.client,self.runs,self.run_id,KEY,'recover_paper',confirm=True)
        self.assertFalse(result['ok'])
        self.assertEqual(self.live_hashes(),before)
        args=self.worker.call_args.args[0]
        self.assertEqual(args[args.index('--steps')+1],'zh')

    def test_reparse_requires_explicit_fresh_selection(self):
        _, paper = self.freeze_state()
        with self.assertRaisesRegex(RuntimeError, "显式"):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, "reparse_source", confirm=True)
        preview = remedy.remedy(self.client, self.runs, self.run_id, KEY, "reparse_source")
        self.assertEqual(preview["steps"], ["folders", "md", "qc"])

    def test_user_edited_outputs_require_explicit_overwrite_choice(self):
        _, paper = self.freeze_state()
        zh = self.lib / "papers" / CK / f"{CK}_ZH.md"
        zh.write_text(zh.read_text(encoding="utf-8") + "\n用户补充的一段笔记。\n", encoding="utf-8")
        preview = remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh")
        self.assertTrue(preview["user_edit_conflicts"])
        self.assertIn("默认保留", preview["user_edit_conflicts"][0])
        with self.assertRaisesRegex(RuntimeError, "疑似你的修改"):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh", confirm=True)
        # Explicit acknowledgement proceeds; the mocked repair restores the
        # passing bytes (a real repair writes a QA-validated candidate).
        def ok(args, logfile, env, **kwargs):
            self.write_zh(self.staged(kwargs))
            Path(logfile).write_text("zh repair: restored aligned translation\n", encoding="utf-8")
            return 0
        self.worker.side_effect = ok
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh",
                               {"overwrite": "user_edited_confirmed"}, confirm=True)
        self.assertTrue(result["ok"], result)

    def test_worker_actions_refuse_without_primary_record(self):
        state = {"id": self.run_id, "papers": [], "planned_papers": [{"key": KEY, "citekey": CK, "title": TITLE}]}
        s.save_json(self.run_dir / "state.json", state)
        with self.assertRaisesRegex(RuntimeError, "正本附件记录"):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, "repair_zh", confirm=True)


class LocalActionTests(RemedyFixture):
    def test_adopt_existing_makes_no_model_call_and_updates_ledgers(self):
        state, paper = self.freeze_state()
        s.save_json(self.runs / "discovery.json", {"schema": 1, "since": "2026-01-01T00:00:00+00:00",
                                                   "entries": {KEY: {"status": "pending", "citekey": CK}}})
        preview = remedy.remedy(self.client, self.runs, self.run_id, KEY, "adopt_existing")
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["model_calls"], 0)
        self.worker.assert_not_called()
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "adopt_existing", confirm=True)
        self.assertTrue(result["ok"])
        self.worker.assert_not_called()  # adoption never starts a model call
        import library_issues
        ledger = library_issues.load(self.runs)
        self.assertEqual(ledger["items"][KEY]["status"], "complete")
        self.assertTrue(ledger["items"][KEY]["hold_released"])
        discovery = json.loads((self.runs / "discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(discovery["entries"][KEY]["status"], "completed")

    def test_adopt_refuses_when_identity_or_qa_fails(self):
        self.seed(zh_body_text="# 只有标题\n\n没有译文的破损产物。\n")
        _, paper = self.freeze_state()
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "adopt_existing")
        self.assertFalse(result["qa_ok"])
        self.assertTrue(result["errors"])
        with self.assertRaisesRegex(RuntimeError, "未通过完整验收"):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, "adopt_existing", confirm=True)
        self.client.file_path.return_value = str(self.root / "moved.pdf")
        with self.assertRaisesRegex(RuntimeError, "正本路径已变化"):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, "adopt_existing", confirm=True)

    def test_migrate_metadata_preview_diff_and_atomic_apply(self):
        folder = self.seed(zh_extra_front={"source": "", "doi": "10.9999/wrong"})
        en_before = (folder / f"{CK}_EN.md").read_bytes()
        _, paper = self.freeze_state()
        preview = remedy.remedy(self.client, self.runs, self.run_id, KEY, "migrate_metadata")
        self.assertTrue(preview["preview"])
        self.assertEqual([row["file"] for row in preview["changes"]], [f"{CK}_ZH.md"])
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "migrate_metadata", confirm=True)
        self.assertTrue(result["ok"], result)
        zh_after = (folder / f"{CK}_ZH.md").read_text(encoding="utf-8")
        self.assertIn("doi: 10.1234/example", zh_after)
        self.assertIn("source: legacy-migrated", zh_after)
        # The fulltext is deliberately untouched: a correct current PDF name
        # must never be used to "prove" an old fulltext's provenance.
        self.assertEqual((folder / f"{CK}_EN.md").read_bytes(), en_before)
        self.assertIn(SOURCE, (folder / f"{CK}_EN.md").read_text(encoding="utf-8"))
        import library_issues
        self.assertEqual(library_issues.load(self.runs)["items"][KEY]["status"], "complete")
        self.assertTrue(Path(result["backup"]).is_dir())

    def test_failed_migration_candidate_never_overwrites_bytes(self):
        # Legacy metadata AND an unfixable body defect: the staged candidate
        # still fails QA, so nothing is written and originals stay identical.
        folder = self.seed(zh_extra_front={"source": ""},
                           zh_body_text="# 破损旧译文\n\n孤儿段落，没有 quote 对照。\n\n参考文献没有。\n")
        _, paper = self.freeze_state()
        en_before = (folder / f"{CK}_EN.md").read_bytes()
        zh_before = (folder / f"{CK}_ZH.md").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "未通过完整验收|人工审阅"):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, "migrate_metadata", confirm=True)
        self.assertEqual((folder / f"{CK}_EN.md").read_bytes(), en_before)
        self.assertEqual((folder / f"{CK}_ZH.md").read_bytes(), zh_before)

    def test_ledger_actions_and_honest_recheck(self):
        _, paper = self.freeze_state()
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, "exclude", confirm=True)
        self.assertEqual(result["status"], "excluded")
        undone = remedy.remedy(self.client, self.runs, self.run_id, KEY, "undo", confirm=True)
        self.assertEqual(undone["status"], "needs_repair")
        self.worker.assert_not_called()
        # Intake passes but the artifacts fail content QA: recheck must NOT
        # claim the item is fixed.
        zh = self.lib / "papers" / CK / f"{CK}_ZH.md"
        zh.write_text(zh.read_text(encoding="utf-8").replace("观测到的混合改变了水柱中的层结。", ""),
                      encoding="utf-8")
        with patch("library_intake.plan_item", return_value={
                "key": KEY, "citekey": CK, "blocked": [], "title": TITLE}):
            recheck = remedy.remedy(self.client, self.runs, self.run_id, KEY, "recheck", confirm=True)
        self.assertFalse(recheck["ok"])
        self.assertTrue(recheck["qa_errors"])
        self.assertIn("不能据此宣称已修好", recheck["message"])
        import library_issues
        self.assertEqual(library_issues.load(self.runs)["items"][KEY]["status"], "needs_repair")
        # A genuinely clean item rechecks to "available for preview" only.
        with patch("library_intake.plan_item", return_value={
                "key": KEY, "citekey": CK, "blocked": [], "title": TITLE}), \
             patch("library_qa.validate_paper", return_value={"ok": True, "errors": [], "metrics": {}}):
            clean = remedy.remedy(self.client, self.runs, self.run_id, KEY, "recheck", confirm=True)
        self.assertTrue(clean["ok"])
        self.assertIn("可重新预演", clean["message"])
        self.assertEqual(library_issues.load(self.runs)["items"][KEY]["status"], "new")

    def test_recheck_classifies_missing_pdf(self):
        _, paper = self.freeze_state()
        with patch("library_intake.plan_item", return_value={
                "key": KEY, "citekey": CK, "blocked": ["IntakeBlocked: no PDF; a clean primary is required"],
                "title": TITLE}):
            recheck = remedy.remedy(self.client, self.runs, self.run_id, KEY, "recheck")
            confirmed = remedy.remedy(self.client, self.runs, self.run_id, KEY, "recheck", confirm=True)
        self.assertFalse(recheck["ok"])
        self.assertEqual(recheck["classification"][0]["category"], "missing_pdf")
        import library_issues
        self.assertEqual(library_issues.load(self.runs)["items"][KEY]["status"], "missing_pdf")



class FinalAcceptanceTests(RemedyFixture):
    def test_publish_second_replace_failure_restores_all_bytes(self):
        import os
        staged = self.root / 'candidate'; staged.mkdir()
        live = self.lib / 'papers' / CK
        for suffix in ('EN', 'MD'):
            (live / f'{CK}_{suffix}.md').write_text('old', encoding='utf-8')
            (staged / f'{CK}_{suffix}.md').write_text('new', encoding='utf-8')
        original = os.replace
        failed = False
        def fail_once(src, dst):
            nonlocal failed
            if Path(dst).name == f'{CK}_MD.md' and not failed:
                failed = True
                raise PermissionError('synthetic locked second output')
            return original(src, dst)
        with patch.object(remedy.os, 'replace', side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, '已回滚'):
                remedy._publish(staged, live, CK, 'retry_qc')
        for suffix in ('EN', 'MD'):
            self.assertEqual((live / f'{CK}_{suffix}.md').read_text(), 'old')

    def test_rollback_removes_absent_before_output_and_keeps_backup(self):
        staged = self.root / 'candidate'; staged.mkdir()
        live = self.lib / 'papers' / CK
        target = live / f'{CK}_ZH.md'; target.unlink()
        (staged / target.name).write_text('new', encoding='utf-8')
        result = remedy._publish(staged, live, CK, 'repair_zh')
        self.assertTrue(remedy._rollback(result))
        self.assertFalse(target.exists())
        self.assertEqual(json.loads((Path(result['backup']) / 'transaction.json').read_text())['status'], 'rolled_back')

    def test_rollback_failure_is_honest_and_recovery_evidence_survives(self):
        staged = self.root / 'candidate'; staged.mkdir()
        live = self.lib / 'papers' / CK
        target = live / f'{CK}_ZH.md'
        original = target.read_bytes()
        (staged / target.name).write_text('new', encoding='utf-8')
        result = remedy._publish(staged, live, CK, 'repair_zh')
        original_replace = remedy.os.replace
        def locked(src, dst):
            if Path(dst) == target:
                raise PermissionError('locked')
            return original_replace(src, dst)
        with patch.object(remedy.os, 'replace', side_effect=locked):
            self.assertFalse(remedy._rollback(result))
        self.assertEqual(Path(result['backup_pairs'][0][0]).read_bytes(), original)
        self.assertEqual(json.loads((Path(result['backup']) / 'transaction.json').read_text())['status'], 'recovery_required')
        self.assertTrue(remedy._rollback(result))
        self.assertEqual(target.read_bytes(), original)

    def test_success_refreshes_diagnosis_baseline_and_progress_without_history_rewrite(self):
        folder = self.lib / 'papers' / CK
        self.write_zh(folder, body='broken translation')
        self.freeze_state()
        original_state = (self.run_dir / 'state.json').read_bytes()
        def repaired(args, logfile, env, **kwargs):
            self.write_zh(self.staged(kwargs))
            Path(logfile).write_text('done')
            return 0
        self.worker.side_effect = repaired
        outcome = remedy.remedy(self.client, self.runs, self.run_id, KEY, 'repair_zh', confirm=True)
        self.assertTrue(outcome['ok'])
        diagnosis = remedy.diagnose(self.runs, self.run_id, KEY)
        self.assertTrue(diagnosis['accepted'])
        self.assertTrue(diagnosis['qa']['ok'])
        self.assertEqual(diagnosis['classification'], [])
        self.assertNotEqual(diagnosis['latest_attempt']['run_id'], self.run_id)
        preview = remedy.remedy(self.client, self.runs, self.run_id, KEY, 'repair_zh')
        self.assertEqual(preview['user_edit_conflicts'], [])
        self.assertEqual((self.run_dir / 'state.json').read_bytes(), original_state)
        import library_progress
        view = library_progress.snapshot(self.runs, self.run_id, self.lib)
        self.assertTrue(view['papers'][0]['accepted'])
        with (folder / f'{CK}_ZH.md').open('a', encoding='utf-8') as stream:
            stream.write('\nuser edit')
        self.assertFalse(remedy.diagnose(self.runs, self.run_id, KEY)['accepted'])
        self.assertTrue(remedy.remedy(self.client, self.runs, self.run_id, KEY, 'repair_zh')['user_edit_conflicts'])

    def test_migration_preview_drift_rejected_under_lock(self):
        folder = self.seed(zh_extra_front={'source': ''})
        self.freeze_state()
        bound = remedy.binding(self.client, self.runs, self.run_id, KEY, 'migrate_metadata')
        target = folder / f'{CK}_ZH.md'
        target.write_text(target.read_text(encoding='utf-8')+'\nmy edit', encoding='utf-8')
        before = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '重新预览'):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, 'migrate_metadata', confirm=True, expected_binding=bound)
        self.assertEqual(target.read_bytes(), before)

    def test_ledger_preview_drift_rejected(self):
        self.freeze_state()
        bound = remedy.binding(self.client, self.runs, self.run_id, KEY, 'exclude')
        remedy.remedy(self.client, self.runs, self.run_id, KEY, 'defer', confirm=True)
        with self.assertRaisesRegex(RuntimeError, '重新预览'):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, 'exclude', confirm=True, expected_binding=bound)

    def test_worker_failure_is_durable(self):
        self.freeze_state()
        self.worker.return_value = 1
        result = remedy.remedy(self.client, self.runs, self.run_id, KEY, 'repair_zh', confirm=True)
        self.assertFalse(result['ok'])
        record = remedy.diagnose(self.runs, self.run_id, KEY)['latest_attempt']
        self.assertEqual(record['status'], 'failed')
        self.assertTrue(record['finished'])

    def test_new_live_file_during_worker_is_preserved(self):
        folder = self.lib / 'papers' / CK
        self.freeze_state()
        def worker(args, logfile, env, **kwargs):
            (folder / 'my_new_note.md').write_text('mine', encoding='utf-8')
            self.write_zh(self.staged(kwargs))
            return 0
        self.worker.side_effect = worker
        with self.assertRaisesRegex(RuntimeError, '外部修改'):
            remedy.remedy(self.client, self.runs, self.run_id, KEY, 'repair_zh', confirm=True)
        self.assertEqual((folder / 'my_new_note.md').read_text(), 'mine')

    def test_postpublish_qa_failure_removes_new_zh(self):
        folder = self.lib / 'papers' / CK
        (folder / f'{CK}_ZH.md').unlink()
        self.freeze_state()
        def worker(args, logfile, env, **kwargs):
            self.write_zh(self.staged(kwargs))
            return 0
        self.worker.side_effect = worker
        def fail_qa(paper):
            paper['qa'] = {'ok': False, 'errors': [{'code': 'synthetic'}]}
        with patch.object(s, 'validate_completed_paper', side_effect=fail_qa):
            outcome = remedy.remedy(self.client, self.runs, self.run_id, KEY, 'repair_zh', confirm=True)
        self.assertFalse(outcome['ok'])
        self.assertFalse((folder / f'{CK}_ZH.md').exists())

    def test_migration_postpublish_qa_failure_restores_original(self):
        folder = self.seed(zh_extra_front={'source': ''})
        self.freeze_state()
        before = self.live_hashes()
        def fail_qa(paper):
            paper['qa'] = {'ok': False, 'errors': [{'code': 'synthetic'}]}
        with patch.object(s, 'validate_completed_paper', side_effect=fail_qa):
            with self.assertRaisesRegex(RuntimeError, '已回滚'):
                remedy.remedy(self.client, self.runs, self.run_id, KEY, 'migrate_metadata', confirm=True)
        self.assertEqual(self.live_hashes(), before)


if __name__ == "__main__":
    unittest.main()
