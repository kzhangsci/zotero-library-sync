"""Offline tests: py -3 -B tools/test_library_source.py -v.

Only TemporaryDirectory fixtures, synthetic fitz PDFs and mocks are used.
Never reads production Library, Zotero, credentials or runs synchronization/Git.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import fitz

import library_source as source


KEY, OLD_KEY, OTHER_KEY = "PARENT01", "OLDKEY01", "OTHER001"
CK, OLD_CK = "Li2026JPO-ShortPaper", "Li2025JPO-OldPaper"
SHA_A, SHA_B = "a" * 64, "b" * 64
DOI = "10.1234/Article.01"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    return {str(p): digest(p) for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("process/Git/sync forbidden")))

    def pdf(self, texts, name="paper.pdf", encrypted=False):
        path = self.root / name
        with fitz.open() as doc:
            for text in texts:
                page = doc.new_page()
                if text:
                    page.insert_text((50, 60), text, fontsize=10)
            if encrypted:
                doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader")
            else:
                doc.save(path)
        return path


class PDFTests(Fixture):
    def test_genuine_two_page_paper_is_not_blocked(self):
        path = self.pdf(["Short communication\n1. Introduction\nObservations.",
                         "2. Conclusions\nThis completes the study.\nReferences\nAuthor (2026)."])
        before = tree(self.root)
        result = source.inspect_pdf(path, {"pages": "101-102"})
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["sha256"], digest(path))
        self.assertFalse(result["likely_incomplete"])
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["metrics"]["lastpage"]["page"], 2)
        self.assertEqual({h["kind"] for h in result["metrics"]["lastpage"]["headings"]},
                         {"conclusions", "references"})
        self.assertFalse(result["metrics"]["fulltext_verified"])
        self.assertEqual(before, tree(self.root))

    def test_two_pages_without_metadata_or_endings_only_warn(self):
        result = source.inspect_pdf(self.pdf(["Brief report", "Last paragraph."]))
        self.assertFalse(result["likely_incomplete"])
        self.assertEqual(result["blocked"], [])
        self.assertIn("ending_not_detected", {i["code"] for i in result["issues"]})

    def test_article_id_two_page_journal_pdf_has_specific_preview_warning(self):
        result = source.inspect_pdf(self.pdf(["Introduction\nPaper text", "Methods\nContinued text"]),
                                    {"itemType": "journalArticle", "pages": "e12345", "title": "Ocean mixing"})
        self.assertTrue(any(w.startswith("short_pdf_possible_preview:") for w in result["warnings"]))
        self.assertEqual(result["blocked"], [])
        self.assertFalse(result["likely_incomplete"])
        self.assertIn("does not prove", " ".join(result["warnings"]))

    def test_editorial_erratum_and_genuine_short_endings_avoid_preview_warning(self):
        no_endings = self.pdf(["Brief text", "The end"], name="short.pdf")
        has_endings = self.pdf(["Research", "Conclusions\nReferences"], name="complete.pdf")
        for path, metadata in ((no_endings, {"itemType": "journalArticle", "title": "Editorial: Mixing"}),
                               (no_endings, {"itemType": "journalArticle", "title": "Erratum"}),
                               (no_endings, {"itemType": "journalArticle", "articleType": "corrigendum"}),
                               (no_endings, {"itemType": "bookSection"}),
                               (has_endings, {"itemType": "journalArticle"})):
            with self.subTest(metadata=metadata):
                result = source.inspect_pdf(path, metadata)
                self.assertNotIn("short_pdf_possible_preview", {i["code"] for i in result["issues"]})
                self.assertEqual(result["blocked"], [])

    def test_short_preview_warning_does_not_override_strong_range_block(self):
        result = source.inspect_pdf(self.pdf(["Introduction", "Methods"]),
                                    {"itemType": "journalArticle", "pages": "277-295"})
        self.assertTrue(result["likely_incomplete"])
        self.assertTrue(any(w.startswith("likely_incomplete:") for w in result["blocked"]))
        self.assertTrue(any(w.startswith("short_pdf_possible_preview:") for w in result["warnings"]))

    def test_nineteen_pages_277_295_timeout_is_not_incompleteness(self):
        texts = ["Research text for page " + str(i) for i in range(18)] + ["Conclusions\nReferences\nEnd."]
        result = source.inspect_pdf(self.pdf(texts), {"key": "JTNZHFYQ", "data": {
            "pages": "277-295", "DOI": DOI}, "qc_error": "request_timeout"})
        self.assertEqual(result["page_count"], 19)
        self.assertEqual(result["metrics"]["pages_metadata"]["expected_count"], 19)
        self.assertFalse(result["likely_incomplete"])
        self.assertEqual(result["blocked"], [])

    def test_explicit_range_and_missing_ending_block_likely_incomplete(self):
        result = source.inspect_pdf(self.pdf(["1. Introduction\nStart.", "2. Methods\nOngoing work."]),
                                    {"pages": "277-295"})
        self.assertTrue(result["likely_incomplete"])
        self.assertFalse(result["ok"])
        self.assertTrue(any(s.startswith("likely_incomplete:") for s in result["blocked"]))
        self.assertTrue(all(isinstance(s, str) for s in result["warnings"] + result["blocked"]))

    def test_range_conflict_with_endings_warns_without_claiming_complete(self):
        result = source.inspect_pdf(self.pdf(["Body", "References\nEnd"]), {"pages": "1-12"})
        self.assertTrue(result["likely_incomplete"])
        self.assertEqual(result["blocked"], [])
        self.assertTrue(any(w.startswith("likely_incomplete:") for w in result["warnings"]))

    def test_article_ids_and_single_numbers_are_not_page_counts(self):
        path = self.pdf(["Short article", "Conclusions\nEnd."])
        for pages in ("e123456", "123456", "2", 123456, "Article 123456"):
            with self.subTest(pages=pages):
                result = source.inspect_pdf(path, {"pages": pages})
                self.assertIsNone(result["metrics"]["pages_metadata"]["expected_count"])
                self.assertFalse(result["likely_incomplete"])
                self.assertEqual(result["blocked"], [])

    def test_explicit_article_id_overrides_range_shaped_id(self):
        result = source.inspect_pdf(self.pdf(["Short", "End"]),
                                    {"pages": "100-200", "articleNumber": "100-200"})
        self.assertEqual(result["metrics"]["pages_metadata"]["kind"], "article_id")
        self.assertFalse(result["likely_incomplete"])

    def test_supported_page_ranges(self):
        path = self.pdf(["Body", "Conclusions\nReferences"])
        for pages, expected in (("pp. 101–102", 2), ("pages 101—102", 2),
                                ("S101-S102", 2), ("101-2", 2), ("277-95", 19),
                                ("e101-e102", 2), ("1001−1002", 2)):
            with self.subTest(pages=pages):
                info = source.inspect_pdf(path, {"pages": pages})["metrics"]["pages_metadata"]
                self.assertEqual(info["expected_count"], expected)

    def test_unsupported_ranges_are_not_invented(self):
        path = self.pdf(["Body"])
        for pages in ("1-3,7-10", "S3-T5", "300-100", "xv-xx"):
            with self.subTest(pages=pages):
                result = source.inspect_pdf(path, {"pages": pages})
                self.assertIsNone(result["metrics"]["pages_metadata"]["expected_count"])
                self.assertEqual(result["blocked"], [])

    def test_unsearchable_pages_do_not_prove_truncation(self):
        result = source.inspect_pdf(self.pdf(["", ""]), {"pages": "1-12"})
        self.assertTrue(result["likely_incomplete"])
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["metrics"]["unsearchable_pages"], [1, 2])
        self.assertEqual(result["metrics"]["lastpage"]["text_chars"], 0)

    def test_heading_requires_whole_line_not_inline_word(self):
        result = source.inspect_pdf(self.pdf(["The references and conclusions are discussed here."]))
        self.assertEqual(result["metrics"]["ending_headings"], [])

    def test_references_can_span_multiple_final_pages(self):
        result = source.inspect_pdf(self.pdf(["Body", "References", "Author one", "Author two"]),
                                    {"pages": "1-4"})
        self.assertEqual(result["metrics"]["ending_headings"][0]["page"], 2)
        self.assertEqual(result["warnings"], [])

    def test_empty_missing_and_invalid_pdf(self):
        empty, bad = self.root / "empty.pdf", self.root / "bad.pdf"
        empty.write_bytes(b"")
        bad.write_bytes(b"not a PDF")
        for path in (empty, bad, self.root / "missing.pdf"):
            with self.subTest(path=path):
                result = source.inspect_pdf(path)
                self.assertTrue(result["blocked"])
                self.assertIsNone(result["page_count"])
                self.assertFalse(result["metrics"]["fulltext_verified"])

    def test_password_protected_pdf_is_blocked(self):
        path = self.pdf(["Private test content"], encrypted=True)
        result = source.inspect_pdf(path)
        self.assertTrue(result["metrics"]["encrypted"])
        self.assertTrue(any(w.startswith("pdf_encrypted:") for w in result["blocked"]))
        self.assertEqual(result["metrics"]["pages_checked"], 0)

    def test_page_extraction_failure_is_not_silently_passed(self):
        path = self.pdf(["Body"])
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.needs_pass = fake.is_repaired = False
        fake.page_count = 1
        fake.load_page.side_effect = RuntimeError("secret should not be exposed")
        with patch.object(fitz, "open", return_value=fake):
            result = source.inspect_pdf(path)
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["metrics"]["lastpage"])
        self.assertNotIn("secret should not be exposed", json.dumps(result))

    def test_same_mtime_different_bytes_produce_different_hashes(self):
        first = self.pdf(["First text"], name="first.pdf")
        second = self.pdf(["Other text"], name="second.pdf")
        for path in (first, second):
            os.utime(path, (123456789, 123456789))
        self.assertNotEqual(source.inspect_pdf(first)["sha256"], source.inspect_pdf(second)["sha256"])

    def test_metrics_and_hash_use_same_byte_snapshot(self):
        path = self.pdf(["Original", "Conclusions"])
        real_open, before = fitz.open, path.read_bytes()
        def replace_after_read(*args, **kwargs):
            path.write_bytes(b"replaced after snapshot")
            return real_open(*args, **kwargs)
        with patch.object(fitz, "open", side_effect=replace_after_read):
            result = source.inspect_pdf(path)
        self.assertEqual(result["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["page_count"], 2)


class SourceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.library, self.runs = self.root / "Library", self.root / "runs"
        self.library.mkdir()
        self.runs.mkdir()
        self.parent = {"key": KEY, "data": {"key": KEY, "citationKey": CK, "DOI": DOI, "title": "Shared title"}}

    def paper(self, key=KEY, ck=CK, doi=DOI, suffix="EN", **fields):
        folder = self.library / "papers" / ck
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{ck}_{suffix}.md"
        metadata = {"zotero_key": key, "citekey": ck, "doi": doi, "title": "Shared title", **fields}
        text = "---\n" + "\n".join(k + ": " + json.dumps(v) for k, v in metadata.items()) + "\n---\n\nBody.\n"
        path.write_text(text, encoding="utf-8")
        return path

    def receipt(self, key=KEY, ck=CK, sha=SHA_A, status="completed", run_id="20261020_120000_aaaaaa",
                snapshot_doi=DOI, qa_path=None, **extra):
        folder = self.runs / run_id
        folder.mkdir(parents=True, exist_ok=True)
        paper = {"key": key, "citekey": ck, "source_sha256": sha, "primary_key": "PDF00001",
                 "primary_pdf": str(self.root / "never_read_old.pdf"), "status": "applied",
                 "exit_code": 0, "qa": {"ok": True, "metrics": {"input_sha256": {}}}, **extra}
        if qa_path:
            paper["qa"]["metrics"]["input_sha256"] = {qa_path.name: digest(qa_path)}
        state = {"id": run_id, "status": status, "papers": [paper], "resume_of": "",
                 "model_config": {"api_key": "DO_NOT_RETURN_THIS_SECRET"}}
        (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
        if snapshot_doi is not None:
            snapshot = {"items": [{"key": key, "data": {"key": key, "citationKey": ck, "DOI": snapshot_doi}}],
                        "collections": []}
            (folder / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        return folder / "state.json"

    def find(self, parent=None):
        return source.find_previous_source(self.library, self.runs, parent or self.parent)

    def test_completed_receipt_and_current_file_hashes(self):
        path = self.paper()
        self.receipt(qa_path=path)
        result = self.find()
        self.assertEqual(result["item_key"], KEY)
        self.assertEqual(result["citekey"], CK)
        self.assertEqual(result["doi"], DOI.lower())
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertEqual(result["files"], {str(path): digest(path)})
        self.assertTrue(result["library_hash_verified"])
        self.assertFalse(result["review_required"])
        self.assertEqual(result["blocked"], [])

    def test_failure_and_blocked_runs_are_source_evidence(self):
        path = self.paper()
        for status in ("failed", "blocked", "attention", "paused"):
            with self.subTest(status=status):
                self.receipt(status=status, exit_code=1, qa_path=path, error="request_timeout")
                result = self.find()
                self.assertEqual(result["source_sha256"], SHA_A)
                self.assertEqual(next(r for r in result["records"] if r["kind"] == "run")["run_status"], status)
                self.assertNotIn("request_timeout", json.dumps(result))

    def test_snapshot_supplies_doi_for_deleted_readded_item(self):
        self.receipt(key=OLD_KEY, ck=OLD_CK)
        result = self.find({**self.parent, "live_item_keys": [KEY]})
        self.assertEqual(result["item_key"], OLD_KEY)
        self.assertEqual(result["match"], "doi")
        self.assertFalse(result["old_item_live"])
        self.assertTrue(result["review_required"])

    def test_unknown_live_inventory_never_means_deleted(self):
        self.paper(key=OLD_KEY, ck=OLD_CK)
        result = self.find()
        self.assertIsNone(result["old_item_live"])
        self.assertTrue(result["review_required"])

    def test_old_item_still_live_blocks_cross_key_replacement(self):
        self.paper(key=OLD_KEY, ck=OLD_CK)
        result = self.find({**self.parent, "live_item_keys": [KEY, OLD_KEY]})
        self.assertIn("previous_item_still_live", result["blocked"])

    def test_title_or_citekey_alone_does_not_merge(self):
        self.paper(key=OLD_KEY, ck=CK, doi="10.9999/different")
        self.assertIsNone(self.find())
        self.paper(key="", ck=CK, doi="")
        self.assertIsNone(self.find())

    def test_same_citekey_with_exact_doi_is_reviewable(self):
        self.paper(key=OLD_KEY, ck=CK, doi="https://doi.org/10.1234/ARTICLE.01")
        result = self.find()
        self.assertEqual(result["item_key"], OLD_KEY)
        self.assertEqual(result["match"], "doi")
        self.assertTrue(result["review_required"])

    def test_nested_source_fields_and_unknown_old_hash(self):
        path = self.paper(key=OLD_KEY, ck=OLD_CK, source={"sha256": SHA_A, "primary_key": "PDFOLD01"})
        result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertEqual(result["primary_key"], "PDFOLD01")
        self.assertIn(str(path), result["files"])
        self.paper(key=OLD_KEY, ck=OLD_CK, source="mineru", source_pdf="historical.pdf")
        result = self.find()
        self.assertEqual(result["source_sha256"], "")
        self.assertTrue(result["review_required"])

    def test_source_paths_are_not_dereferenced_to_invent_old_hash(self):
        secret = self.root / "must_not_read.pdf"
        secret.write_bytes(b"current replacement does not prove old content")
        self.paper(source_pdf=str(secret), source_path=str(secret))
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path == secret:
                raise AssertionError("historical PDF path was dereferenced")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            result = self.find()
        self.assertEqual(result["source_sha256"], "")

    def test_exact_item_key_wins_over_other_same_doi(self):
        self.paper()
        self.paper(key=OLD_KEY, ck=OLD_CK)
        result = self.find()
        self.assertEqual(result["item_key"], KEY)
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["candidates"], [])  # Verified current folder needs no unrelated DOI scan.

    def test_multiple_old_doi_keys_require_explicit_choice(self):
        self.paper(key=OLD_KEY, ck=OLD_CK)
        self.paper(key=OTHER_KEY, ck="Other2026-Paper")
        result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["files"], {})
        self.assertEqual(result["item_key"], "")
        self.assertIn("doi", result)
        self.assertEqual(len(result["candidates"]), 2)

    def test_same_item_doi_conflict_is_not_silently_accepted(self):
        self.paper(doi="10.9999/different")
        self.assertIn("historical_doi_conflict", self.find()["blocked"])

    def test_mixed_identity_in_same_folder_blocks(self):
        self.paper()
        self.paper(key=OTHER_KEY, doi="10.9999/different", suffix="ZH")
        result = self.find()
        self.assertIn("library_folder_identity_conflict", result["blocked"])

    def test_anchored_complete_source_beats_later_failed_new_source(self):
        path = self.paper()
        self.receipt(qa_path=path)
        self.receipt(sha=SHA_B, status="failed", run_id="20261026_120000_bbbbbb", exit_code=1, qa={"ok": False})
        result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertTrue(result["library_hash_verified"])
        self.assertIn(SHA_B, {r["source_sha256"] for r in result["records"]})

    def test_neither_recorded_chronology_nor_mtime_proves_source_hash(self):
        self.paper()
        older = self.receipt()
        newer = self.receipt(sha=SHA_B, run_id="20261026_120000_bbbbbb")
        os.utime(older, (1999999999, 1999999999))
        os.utime(newer, (123456789, 123456789))
        result = self.find()
        self.assertEqual(result["source_sha256"], "")
        self.assertTrue(result["review_required"])
        self.assertIn(SHA_B, {r["source_sha256"] for r in result["records"]})

    def test_equal_rank_conflicting_hashes_are_ambiguous(self):
        path = self.paper()
        self.receipt(run_id="undated_a", qa_path=path)
        self.receipt(sha=SHA_B, run_id="undated_b", qa_path=path)
        result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["source_sha256"], "")

    def test_new_intake_hash_cannot_masquerade_as_old_fulltext_source(self):
        self.paper()
        self.receipt(sha=SHA_B, status="failed", qa={"ok": False},
                     error="request_timeout", run_id="20261026_120000_bbbbbb")
        result = self.find()
        self.assertEqual(result["source_sha256"], "")
        comparison = source.compare_sources(result, {"item_key": KEY, "source_sha256": SHA_B})
        self.assertIsNone(comparison["content_changed"])
        self.assertTrue(comparison["review_required"])

    def test_mismatching_qa_hash_cannot_bind_current_en(self):
        path = self.paper()
        self.receipt(qa_path=path)
        path.write_text(path.read_text(encoding="utf-8") + "Changed text.\n", encoding="utf-8")
        result = self.find()
        self.assertEqual(result["source_sha256"], "")
        self.assertFalse(result["library_hash_verified"])

    def test_current_en_frontmatter_wins_over_old_or_new_intake(self):
        self.paper(source_sha256=SHA_A)
        self.receipt(sha=SHA_B, status="failed", qa={"ok": False})
        result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertFalse(result["ambiguous"])

    def test_note_and_zh_source_hashes_do_not_prove_en_provenance(self):
        self.paper()
        self.paper(suffix="Note", source_sha256=SHA_B)
        self.paper(suffix="ZH", source_sha256=SHA_B)
        result = self.find()
        self.assertEqual(result["source_sha256"], "")

    def test_qa_frozen_binds_a_failed_or_resumed_receipt(self):
        path = self.paper()
        state_path = self.receipt(status="blocked", qa={"ok": False})
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["qa_frozen"] = {str(path): digest(path)}
        state["resume_of"] = "20261018_120000_aaaaaa"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertTrue(result["library_hash_verified"])

    def test_old_library_doi_bridges_to_old_key_run_without_snapshot(self):
        path = self.paper(key=OLD_KEY, ck=OLD_CK)
        self.receipt(key=OLD_KEY, ck=OLD_CK, snapshot_doi=None, qa_path=path)
        result = self.find({**self.parent, "live_item_keys": [KEY]})
        self.assertEqual(result["item_key"], OLD_KEY)
        self.assertEqual(result["doi"], DOI.lower())
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertFalse(result["old_item_live"])

    def test_conflicting_key_aliases_are_ambiguous(self):
        self.paper(source_sha256=SHA_A, item_key=OLD_KEY)
        result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["source_sha256"], "")

    def test_conflicting_current_fulltexts_do_not_choose_arbitrary_hash(self):
        self.paper(source_sha256=SHA_A)
        self.paper(suffix="MD", source_sha256=SHA_B)
        result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["source_sha256"], "")

    def test_metadata_and_later_file_hash_change_is_reported(self):
        path = self.paper(source_sha256=SHA_A)
        real_files = source._files
        def change_before_hash(folder, library, warnings):
            path.write_text(path.read_text(encoding="utf-8") + "Concurrent edit.\n", encoding="utf-8")
            return real_files(folder, library, warnings)
        with patch.object(source, "_files", side_effect=change_before_hash):
            result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertIn("library_changed_during_lookup", result["blocked"])
        self.assertEqual(result["source_sha256"], "")

    def test_resources_are_fingerprinted_and_inputs_preserved(self):
        path = self.paper()
        image = path.parent / "images" / "figure.png"
        image.parent.mkdir()
        image.write_bytes(b"temporary image fixture")
        (path.parent / ".sync_test.lock").write_text("preserve lock", encoding="utf-8")
        self.receipt(qa_path=path)
        before = tree(self.root)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.find()
        self.assertEqual(before, tree(self.root))
        self.assertIn(str(image), result["files"])
        self.assertNotIn("DO_NOT_RETURN_THIS_SECRET", json.dumps(result))
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_malformed_records_report_ambiguity_not_absence(self):
        path = self.receipt()
        path.write_text('{"papers": [], "papers": []}', encoding="utf-8")
        result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertIn("source_lookup_incomplete", result["blocked"])

    def test_duplicate_yaml_identity_is_rejected(self):
        path = self.paper()
        text = path.read_text(encoding="utf-8").replace("---\n", '---\nzotero_key: "OTHER001"\n', 1)
        path.write_text(text, encoding="utf-8")
        result = self.find()
        self.assertTrue(result["ambiguous"])

    def test_no_match_returns_none(self):
        self.assertIsNone(self.find())

    def test_missing_runs_does_not_hide_frontmatter(self):
        self.paper()
        result = source.find_previous_source(self.library, self.root / "absent_runs", self.parent)
        self.assertEqual(result["item_key"], KEY)

    def test_missing_library_is_not_claimed_uncollected(self):
        result = source.find_previous_source(self.root / "missing", self.runs, self.parent)
        self.assertTrue(result["ambiguous"])

    def test_invalid_live_inventory_reports_unresolved(self):
        self.assertTrue(self.find({**self.parent, "live_item_keys": "not a list"})["ambiguous"])
        self.assertTrue(self.find({**self.parent, "live_item_keys": [OLD_KEY]})["ambiguous"])

    def test_parent_is_not_modified(self):
        self.paper()
        before = copy.deepcopy(self.parent)
        self.find()
        self.assertEqual(before, self.parent)

    def test_authoritative_current_en_ignores_unrelated_malformed_library_and_history(self):
        self.paper(source_sha256=SHA_A)
        unrelated = self.paper(key=OTHER_KEY, ck="Broken2020-Other", suffix="Note", doi="10.9999/other")
        unrelated.write_text('---\ncitekey: "unclosed\n---\n', encoding="utf-8")
        state = self.receipt(key=OTHER_KEY, ck="Broken2020-Other")
        state.write_text('{"bad":', encoding="utf-8")
        with patch.object(source, "_json", side_effect=AssertionError("authoritative fulltext needs no history")):
            result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertEqual(result["blocked"], [])
        self.assertFalse(result["ambiguous"])

    def test_bound_current_en_survives_unrelated_malformed_state(self):
        path = self.paper()
        self.receipt(qa_path=path)
        bad = self.receipt(key=OTHER_KEY, run_id="unrelated_broken")
        bad.write_text('{"key":"OTHER001","credential":"DO_NOT_PRINT_THIS",', encoding="utf-8")
        result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertTrue(result["library_hash_verified"])
        self.assertEqual(result["blocked"], [])
        self.assertFalse(result["ambiguous"])
        self.assertTrue(any(w.startswith("run_record_unreadable:") for w in result["warnings"]))
        self.assertNotIn("DO_NOT_PRINT_THIS", json.dumps(result))

    def test_unbound_intake_stays_unknown_despite_ignoring_unrelated_errors(self):
        self.paper()
        self.receipt(sha=SHA_B, status="failed", qa={"ok": False})
        unrelated = self.receipt(key=OTHER_KEY, run_id="unrelated_broken")
        unrelated.write_text("{", encoding="utf-8")
        result = self.find()
        self.assertEqual(result["source_sha256"], "")
        self.assertTrue(result["review_required"])
        self.assertEqual(result["blocked"], [])
        self.assertIsNone(source.compare_sources(result, {"item_key": KEY, "source_sha256": SHA_B})["content_changed"])

    def test_irrelevant_and_identity_redundant_snapshots_are_not_read(self):
        path = self.paper()
        own = self.receipt(qa_path=path)
        other = self.receipt(key=OTHER_KEY, run_id="unrelated_run")
        own.with_name("snapshot.json").write_text("{malformed", encoding="utf-8")
        other.with_name("snapshot.json").write_bytes(b" " * 100000)
        real = source._json
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path.name, "snapshot.json", "exact key plus fulltext needs no snapshot scan")
            return real(path, *args, **kwargs)
        with patch.object(source, "_json", side_effect=guarded):
            result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["warnings"], [])

    def test_malformed_selected_folder_still_blocks(self):
        self.paper(source_sha256=SHA_A)
        bad = self.paper(suffix="Note")
        bad.write_text('---\nzotero_key: "' + KEY + '"\ntitle: "bad\n---\n', encoding="utf-8")
        result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertIn("source_lookup_incomplete", result["blocked"])
        self.assertEqual(result["source_sha256"], "")

    def test_fallback_reads_unrelated_headers_not_bodies_and_finds_old_key(self):
        self.paper(key=OLD_KEY, ck=OLD_CK, source_sha256=SHA_A)
        other = self.paper(key=OTHER_KEY, ck="Unrelated2020-Paper", doi="10.9999/other")
        other.write_text(other.read_text(encoding="utf-8") + "large irrelevant body\n" * 50000, encoding="utf-8")
        real_open = Path.open
        seen = []
        class HeaderOnly:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def read(self, *args):
                raise AssertionError("unrelated body must not be read/hashed")
            def readline(self, limit=-1):
                seen.append(self.stream.tell())
                if self.stream.tell() > 1000:
                    raise AssertionError("header read entered unrelated body")
                return self.stream.readline(limit)
        def guarded(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return HeaderOnly(stream) if path == other else stream
        with patch.object(Path, "open", guarded):
            result = self.find()
        self.assertTrue(seen)
        self.assertEqual(result["item_key"], OLD_KEY)
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertEqual(result["blocked"], [])

    def test_fallback_unrelated_bad_note_does_not_block_bound_identity(self):
        path = self.paper(key=OLD_KEY, ck=OLD_CK)
        self.receipt(key=OLD_KEY, ck=OLD_CK, qa_path=path)
        unrelated = self.paper(key=OTHER_KEY, ck="Bad2010-Note", suffix="Note", doi="10.9999/other")
        unrelated.write_text('---\nzotero_key: OTHER001\ntitle: "invalid\n---\n', encoding="utf-8")
        result = self.find()
        self.assertEqual(result["source_sha256"], SHA_A)
        self.assertEqual(result["blocked"], [])
        self.assertFalse(result["ambiguous"])

    def test_no_mtime_cache_across_successive_authoritative_lookups(self):
        path = self.paper(source_sha256=SHA_A)
        os.utime(path, (123456789, 123456789))
        first = self.find()
        self.paper(source_sha256=SHA_B)
        os.utime(path, (123456789, 123456789))
        second = self.find()
        self.assertEqual(first["source_sha256"], SHA_A)
        self.assertEqual(second["source_sha256"], SHA_B)

    def test_history_byte_limit_returns_unknown_not_a_partial_source_claim(self):
        self.paper()
        self.receipt()
        with patch.object(source, "_HISTORY_BUDGET", 64):
            result = self.find()
        self.assertEqual(result["source_sha256"], "")
        self.assertTrue(result["review_required"])
        self.assertTrue(any(w.startswith("source_lookup_limit:") for w in result["warnings"]))

    def test_library_fallback_limit_is_explicit_ambiguity(self):
        self.paper(key=OLD_KEY, ck=OLD_CK)
        with patch.object(source, "_LIBRARY_FILE_LIMIT", 0):
            result = self.find()
        self.assertTrue(result["ambiguous"])
        self.assertTrue(any(w.startswith("source_lookup_limit:") for w in result["warnings"]))

    def test_partial_current_fulltext_provenance_does_not_bind_other_fulltext(self):
        self.paper(source_sha256=SHA_A)
        self.paper(suffix="MD")
        result = self.find()
        self.assertEqual(result["source_sha256"], "")
        self.assertTrue(result["review_required"])


class ComparisonTests(unittest.TestCase):
    def test_doi_normalization_is_exact(self):
        self.assertEqual(source.normalize_doi(" DOI:10.1234/Article.01 "), DOI.lower())
        self.assertEqual(source.normalize_doi("https://dx.doi.org/10.1234/Article%2E01?utm=x"), DOI.lower())
        self.assertNotEqual(source.normalize_doi(DOI + "."), source.normalize_doi(DOI))
        self.assertEqual(source.normalize_doi("title with 10.1234/example"), "")

    def test_content_changed_on_same_item_and_attachment(self):
        previous = {"item_key": KEY, "primary_key": "PDF00001", "source_sha256": SHA_A}
        result = source.compare_sources(previous, {**previous, "source_sha256": SHA_B})
        self.assertTrue(result["changed"])
        self.assertTrue(result["content_changed"])
        self.assertFalse(result["identity_changed"])

    def test_identity_changed_even_with_equal_bytes(self):
        previous = {"item_key": OLD_KEY, "primary_key": "PDF00001", "source_sha256": SHA_A}
        result = source.compare_sources(previous, {**previous, "item_key": KEY, "primary_key": "PDF00002"})
        self.assertTrue(result["changed"])
        self.assertFalse(result["content_changed"])
        self.assertTrue(result["identity_changed"])

    def test_missing_or_invalid_hash_is_unknown_never_unchanged(self):
        for value in (None, "", "old", "x" * 64):
            previous = {"item_key": KEY, "primary_key": "PDF00001", "source_sha256": value}
            result = source.compare_sources(previous, {**previous, "source_sha256": SHA_A})
            self.assertIsNone(result["content_changed"])
            self.assertIsNone(result["changed"])
            self.assertTrue(result["review_required"])

    def test_mtime_and_filename_changes_alone_are_not_change_evidence(self):
        previous = {"item_key": KEY, "primary_key": "PDF00001", "source_sha256": SHA_A,
                    "primary_pdf": "old.pdf", "mtime": 1}
        result = source.compare_sources(previous, {**previous, "primary_pdf": "renamed.pdf", "mtime": 999})
        self.assertFalse(result["changed"])
        self.assertFalse(result["review_required"])


if __name__ == "__main__":
    unittest.main()
