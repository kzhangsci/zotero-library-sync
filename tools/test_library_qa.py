"""Offline regression tests; real sync.build_zh, fake translation, temp files only.

Run from lit_migration: python -B -m unittest discover -s tools -p test_library_qa.py -v
No Zotero calls, workers, LLMs or Library writes. Temporary fixtures are isolated.
"""
import contextlib
import inspect
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import library_qa as qa
import sync


KEY = "Example2026JPO-Mixing"
ZOTERO_KEY = "ABCD1234"
TITLE = "Mixing and stratification"
DATA = {"title": TITLE, "DOI": "10.1234/example", "date": "2026"}
BODY = r"""# Mixing and stratification

The first source paragraph describes the observed mixing.

![](assets/figure.png)
Fig. 1. Measurements and sampling locations.

$$
F = K N^{2}
$$

## Results

The next paragraph preserves a genuine citation [12].

## References

[12] Smith, J. (2025). Mixing in the ocean. Journal, 1, 1-10.

## References From the Supporting Information

[13] Jones, A. (2024). Observations. Journal, 2, 11-20.

## Appendix A

The appendix also requires a translation.
"""


def fake_translate(paras, glossary, backend):
    """No backend invocation; preserve mathematical/citation material in fixtures."""
    out = []
    for paragraph in paras:
        if paragraph.startswith("#"):
            out.append("## 中文标题")
        else:
            math = "\n".join(m[0] for m in qa.DISPLAY.finditer(paragraph))
            citations = " ".join(re.findall(r"\[\d+\]", paragraph))
            out.append("这是对应的完整中文译文。" + citations + ("\n" + math if math else ""))
    return out


class LibraryQATests(unittest.TestCase):
    def test_picture_only_translation_cannot_replace_prose(self):
        errors = []
        qa._translation_checks("![](assets/figure.png)",
            "The observations show a new eddy.\n![](assets/figure.png)", errors, [], "fixture", 1)
        self.assertEqual([e["code"] for e in errors], ["translation_empty"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="library_qa_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "Library"
        self.folder = self.library / "papers" / KEY
        (self.folder / "assets").mkdir(parents=True)
        (self.folder / "assets" / "figure.png").write_bytes(b"fixture-image")
        self.write_paper()

    def path(self, suffix):
        return self.folder / f"{KEY}_{suffix}.md"

    def write_paper(self, body=BODY, translations=None, chinese=False):
        self.body = body
        meta = {"citekey": KEY, "zotero_key": ZOTERO_KEY, "doi": DATA["DOI"], "title": TITLE}
        self.path("MD" if chinese else "EN").write_text(sync.yaml_block(
            meta | {"source": "mineru", "source_pdf": f"{KEY}.pdf", "qc": "bigmodel/glm-5.3-flash 2026-10-14"}
        ) + "\n" + body, encoding="utf-8")
        self.path("Note").write_text(sync.yaml_block(meta | {"tags": ["paper"], "created": "2026-10-14"})
                                     + "\n## 要点\n\n## 问题与想法\n", encoding="utf-8")
        if not chinese:
            translator = translations or fake_translate
            with patch.object(sync, "translate_chunk", side_effect=translator):
                # New sync variants support chunk caching/parallel execution;
                # fixtures must never create production caches or use a model.
                options = {"chunk_cache": False, "parallel": 1} if "chunk_cache" in inspect.signature(sync.build_zh).parameters else {}
                zh = sync.build_zh(body, DATA, KEY, ZOTERO_KEY, None, [], **options)
            self.path("ZH").write_text(sync.yaml_block(meta | {"source": "translated by fake"}) + "\n" + zh,
                                       encoding="utf-8")

    def change(self, suffix, old, new, count=-1):
        path = self.path(suffix)
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, count), encoding="utf-8")

    def validate(self, **kwargs):
        return qa.validate_paper(self.folder, KEY, f"{KEY}.pdf", **kwargs)

    def assert_error(self, code, **kwargs):
        result = self.validate(**kwargs)
        self.assertFalse(result["ok"], result)
        self.assertIn(code, [e["code"] for e in result["errors"]], result)
        return result

    def drop_first_pair(self):
        path = self.path("ZH")
        text = path.read_text(encoding="utf-8")
        text, count = re.subn(r"> \[!quote\]- en\n> [^\n]+\n\n[^\n]+\n\n", "", text, count=1)
        self.assertEqual(count, 1)
        path.write_text(text, encoding="utf-8")

    def test_real_build_zh_passes_with_captions_references_and_appendix(self):
        result = self.validate()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["metrics"]["expected_quote_blocks"], 4)
        self.assertEqual(result["metrics"]["quote_blocks"], 4)
        self.assertEqual(result["metrics"]["quote_coverage"], 1)
        self.assertEqual(result["metrics"]["display_formulas_source"], 1)
        self.assertEqual(result["metrics"]["reference_blocks"], 1)
        self.assertIn("semantic_review_required", [w["code"] for w in result["warnings"]])

    def test_reference_split_matches_sync(self):
        cases = [BODY, "No references", "# Bibliography\n\nA. Book", "## 参考文献\n\n[1] 文献\n\n# 附录\n\n正文",
                 "# References\n\nA\n\n## Notes\n\nB\n\n# Data\n\nC", "# Literature cited\n\nA"]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(qa._reference_segments(body), sync.split_reference_block(body))

    def test_front_matter_matches_actual_generated_scalars(self):
        for suffix in ("EN", "ZH", "Note"):
            text = self.path(suffix).read_text(encoding="utf-8")
            actual, body, problems = qa._front_matter(text)
            expected, _, expected_body = sync.front_matter(text)
            self.assertEqual(actual, expected)
            self.assertEqual(body, expected_body)
            self.assertEqual(problems, [])

    def test_entire_missing_pair_fails_even_when_every_remaining_quote_is_in_en(self):
        self.drop_first_pair()
        result = self.assert_error("quote_coverage")
        self.assertLess(result["metrics"]["quote_coverage"], 1)
        self.assertNotIn("quote_source", [e["code"] for e in result["errors"]])

    def test_missing_translation_with_quote_retained(self):
        self.change("ZH", "这是对应的完整中文译文。\n\n", "", 1)
        self.assert_error("translation_empty")

    def test_placeholder_refusal_and_waiting_fail(self):
        for placeholder in ("*（此段模型拒绝翻译，见上方原文）*", "!! translation failed: missing paragraphs [3]",
                            "正在核对原文，稍后给出译文", "TODO", "…", "<!-- awaiting translation -->"):
            with self.subTest(placeholder=placeholder):
                self.write_paper()
                self.change("ZH", "这是对应的完整中文译文。", placeholder, 1)
                result = self.validate()
                self.assertFalse(result["ok"])
                self.assertTrue({"translation_empty", "translation_placeholder"} & {e["code"] for e in result["errors"]})

    def test_missing_image_file(self):
        # Change links in both documents; do not delete even fixture assets.
        for suffix in ("EN", "ZH"):
            self.change(suffix, "assets/figure.png", "assets/missing.png")
        self.assert_error("image_missing")

    def test_missing_rendered_image_detected_even_if_quote_has_it(self):
        body = "Paragraph with an embedded ![](assets/figure.png) figure."
        self.write_paper(body)
        self.change("ZH", "\n\n![](assets/figure.png)", "")
        self.assert_error("image_coverage")

    def test_image_filename_is_not_mistaken_for_untranslated_english(self):
        image = "ab6c168d0b34801b51606c2fb0c0d30e9bef9b5a052cc648dbb37fbb16d25e16.jpg"
        (self.folder / "assets" / image).write_bytes(b"fixture-image")
        self.write_paper(f"(b)  \n![](assets/{image})", translations=lambda ps, g, b: ps)
        result = self.validate()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("translation_unchanged", [w["code"] for w in result["warnings"]])
        self.assertEqual(result["metrics"]["images_translation_rendered"], 1)

    def test_display_formula_missing(self):
        self.change("ZH", "$$\nF = K N^{2}\n$$", "")
        self.assert_error("formula_mismatch")

    def test_formula_content_changes_not_just_counts(self):
        self.change("ZH", "F = K N^{2}", "F = K N^{3}")
        self.assert_error("formula_mismatch")

    def test_formula_in_quote_does_not_mask_translation_loss(self):
        self.write_paper("The relation is\n$$\nF = K N^{2}\n$$\nand has units of a flux.")
        self.change("ZH", "\n$$\nF = K N^{2}\n$$", "")
        self.assert_error("formula_mismatch")

    def test_display_formula_delimiters_unbalanced(self):
        self.change("ZH", "F = K N^{2}\n$$", "F = K N^{2}")
        self.assert_error("formula_unbalanced")

    def test_internal_number_markers_fail(self):
        for marker in ("[7]\n", "#[7]\n", "【7】\n", "7. ", "[7] "):
            with self.subTest(marker=marker):
                self.write_paper(translations=lambda ps, g, b: [marker + t for t in fake_translate(ps, g, b)])
                self.assert_error("translation_marker")

    def test_genuine_line_initial_numbering_and_decimals_pass(self):
        cases = [("[12] The cited result.", "[12] 引文结果。"),
                 ("1. The first physical regime.", "1. 第一种物理状态。"),
                 ("[2] Waves propagate shoreward.", "[2] 波向岸传播。"),
                 ("25.5°S marks the shelf break export.", "25.5°S 处为颗粒物离岸输送。"),
                 ("A result cites [12].", "该结果与文献[12]一致。")]
        for source, translated in cases:
            with self.subTest(source=source):
                self.write_paper(source, translations=lambda ps, g, b: [translated for _ in ps])
                self.assertTrue(self.validate()["ok"], self.validate())

    def test_numbered_heading_translation_passes(self):
        # ZH "## 1. 结果" carries marker "1."; EN heading "## 1. Results"
        # must count as line-initial numbering (2026-11-09 heading regression).
        self.change("EN", "## Results", "## 1. Results")
        self.change("ZH", "## 中文标题\n\n> [!quote]- en\n> The next paragraph preserves",
                    "## 1. 结果\n\n> [!quote]- en\n> The next paragraph preserves")
        self.assertTrue(self.validate()["ok"], self.validate())

    def test_marker_needs_line_initial_source_numbering(self):
        # Mid-text citations, <sup>N</sup> affiliations and missing list numbers
        # must not justify a leaked counter (2026-11-09 Shroyer/Davis batch).
        cases = [("A result cites [12].", "[12]\n引用的结果。"),
                 ("The result<sup>12</sup> is reproduced.", "[12] 引用的结果。"),
                 ("Davis<sup>1</sup> and Smith<sup>2</sup> observed it.", "[1]\nDavis 与 Smith 观测到它。"),
                 ("1. First regime follows. Then 2. Second regime.", "3. 第三种状态。")]
        for source, translated in cases:
            with self.subTest(source=source):
                self.write_paper(source, translations=lambda ps, g, b: [translated for _ in ps])
                self.assert_error("translation_marker")

    def test_decimal_source_prefix_and_duplicate_markers_fail(self):
        # Main-session acceptance review 2026-11-09: a source decimal is not
        # "1." evidence, and one source number cannot feed two markers.
        cases = [("1.5 m is measured.", "1. 测量得到。"),
                 ("[2] Waves propagate shoreward.", "[2]\n[2] 波向岸传播。")]
        for source, translated in cases:
            with self.subTest(source=source):
                self.write_paper(source, translations=lambda ps, g, b: [translated for _ in ps])
                self.assert_error("translation_marker")

    def test_paren_marker_glued_to_digit(self):
        # Decimal exemption is dot-only: "1)2" is list marker 1) plus content,
        # not a decimal. No source numbering -> flagged; matching source
        # numbering (source also "1)2 ...") is legitimate and passes.
        self.write_paper("Water depth is 2 m.", translations=lambda ps, g, b: ["1)2 米水深。" for _ in ps])
        self.assert_error("translation_marker")
        self.write_paper("1)2 m water depth is measured.", translations=lambda ps, g, b: ["1)2 米水深。" for _ in ps])
        self.assertTrue(self.validate()["ok"], self.validate())

    def test_multiline_legitimate_list_passes(self):
        source = "1. First regime follows.\n2. Second regime follows."
        translated = "1. 第一种状态。\n2. 第二种状态。"
        self.write_paper(source, translations=lambda ps, g, b: [translated for _ in ps])
        self.assertTrue(self.validate()["ok"], self.validate())

    def test_reference_content_must_be_verbatim(self):
        self.change("ZH", "Mixing in the ocean.", "海洋混合。")
        self.assert_error("references_changed")

    def test_appendix_pair_cannot_be_hidden_as_references(self):
        self.change("ZH", "> The appendix also requires a translation.", "> Different text.")
        self.assert_error("quote_source")

    def test_duplicate_and_reordered_quotes_fail(self):
        body = "First paragraph.\n\nSecond paragraph.\n\nFirst paragraph."
        self.write_paper(body)
        self.assertTrue(self.validate()["ok"])
        self.change("ZH", "> Second paragraph.", "> First paragraph.")
        self.assert_error("quote_source")
        self.write_paper(body)
        self.change("ZH", "> First paragraph.", "> TEMP.", 1)
        self.change("ZH", "> Second paragraph.", "> First paragraph.", 1)
        self.change("ZH", "> TEMP.", "> Second paragraph.", 1)
        self.assert_error("quote_coverage")

    def test_missing_heading_fails_without_requiring_heading_quote(self):
        self.change("ZH", "## 中文标题\n\n", "", 1)
        self.assert_error("structure_coverage")

    def test_qc_default_and_explicit_opt_out(self):
        self.change("EN", "qc: bigmodel/glm-5.3-flash 2026-10-14", "qc: false")
        self.assert_error("qc_missing")
        result = self.validate(require_qc=False)
        self.assertTrue(result["ok"], result)
        self.assertIn("qc_requirement_disabled", [w["code"] for w in result["warnings"]])

    def test_metadata_mismatch_and_duplicate_field(self):
        self.change("Note", "doi: 10.1234/example", "doi: 10.1234/wrong")
        self.assert_error("metadata_mismatch")
        self.write_paper()
        self.change("EN", f"citekey: {KEY}", f"citekey: {KEY}\ncitekey: {KEY}")
        self.assert_error("front_matter")

    def test_note_and_zh_do_not_require_source_pdf_or_qc_fields(self):
        self.assertTrue(self.validate()["ok"])
        self.change("ZH", "source: translated by fake", f"source: translated by fake\nsource_pdf: {KEY}_annotated.pdf")
        self.assert_error("source_pdf")

    def test_wrong_primary_is_rejected_even_if_argument_and_metadata_agree(self):
        self.change("EN", f"source_pdf: {KEY}.pdf", f"source_pdf: {KEY}_manuscript.pdf")
        result = qa.validate_paper(self.folder, KEY, f"{KEY}_manuscript.pdf")
        self.assertFalse(result["ok"])
        self.assertIn("primary_pdf", [e["code"] for e in result["errors"]])

    def test_absolute_primary_path_checks_existence(self):
        result = qa.validate_paper(self.folder, KEY, str(self.root / f"{KEY}.pdf"))
        self.assertIn("primary_pdf_missing", [e["code"] for e in result["errors"]])
        pdf = self.root / f"{KEY}.pdf"
        pdf.write_bytes(b"fixture-primary")
        self.assertTrue(qa.validate_paper(self.folder, KEY, str(pdf))["ok"])

    def test_chinese_md_needs_no_zh(self):
        # Use a fresh isolated folder, leaving the original fixture untouched.
        self.folder = self.root / "chinese" / KEY
        self.folder.mkdir(parents=True)
        self.write_paper("# 海洋混合\n\n这是中文论文原文。", chinese=True)
        result = self.validate()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["metrics"]["translation_required"])

    def test_japanese_is_not_mistaken_for_chinese(self):
        self.folder = self.root / "japanese" / KEY
        self.folder.mkdir(parents=True)
        self.write_paper("海洋の混合について観測されたデータを分析します。")
        self.assertTrue(self.validate()["ok"])
        self.assertTrue(self.validate()["metrics"]["translation_required"])

    def test_missing_translation_and_explicit_exception(self):
        self.path("ZH").rename(self.folder / "historical_translation.txt")
        self.assert_error("translation_missing")
        self.assertTrue(self.validate(require_translation=False)["ok"])

    def test_existing_bad_translation_checked_even_when_not_required(self):
        self.drop_first_pair()
        self.assert_error("quote_coverage", require_translation=False)

    def test_missing_note_and_dual_fulltext(self):
        self.path("Note").rename(self.folder / "historical_note.txt")
        self.assert_error("note_missing")
        self.path("MD").write_bytes(self.path("EN").read_bytes())
        self.assert_error("fulltext_count")

    def test_lock_blocks_qa_and_is_preserved(self):
        lock = self.folder / ".sync_zh.lock"
        lock.write_text("worker 2000-01-01", encoding="utf-8")
        before = lock.read_bytes()
        self.assert_error("active_lock")
        self.assertEqual(lock.read_bytes(), before)

    def test_changed_input_is_not_accepted(self):
        original = qa._alignment
        def change_after_read(*args):
            original(*args)
            self.change("EN", "first source", "updated first source")
        with patch.object(qa, "_alignment", side_effect=change_after_read):
            self.assert_error("input_changed")

    def test_invalid_key_folder_and_missing_folder(self):
        self.assertFalse(qa.validate_paper(self.folder, "../invalid", "invalid.pdf")["ok"])
        self.assertFalse(qa.validate_paper(self.folder, "OtherKey", "OtherKey.pdf")["ok"])
        self.assertFalse(qa.validate_paper(self.root / "missing", KEY, f"{KEY}.pdf")["ok"])

    def snapshot(self):
        return {str(p.relative_to(self.library)): p.read_bytes() for p in self.library.rglob("*") if p.is_file()}

    def test_api_and_default_cli_are_read_only_and_limit_is_enforced(self):
        (self.library / "papers" / "ZMissing").mkdir()
        before = self.snapshot()
        self.validate()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = qa.main(["--library", str(self.library), "--limit", "1"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["papers_checked"], 1)
        self.assertNotIn("report_path", report)
        self.assertEqual(self.snapshot(), before)

    def test_apply_only_creates_unique_reports_never_modifies_library(self):
        before = self.snapshot()
        # Patch __file__ as well so the report-boundary check still runs.
        reports = self.root / "tools_repo" / "reports"
        module = self.root / "tools_repo" / "tools" / "library_qa.py"
        paths = []
        with patch.object(qa, "REPORTS", reports), patch.object(qa, "__file__", str(module)):
            for _ in range(2):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(qa.main(["--library", str(self.library), "--only", KEY, "--apply"]), 0)
                path = Path(json.loads(output.getvalue())["report_path"])
                paths.append(path)
                self.assertEqual(path.parent, reports)
                self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["ok"])
        self.assertNotEqual(*paths)
        self.assertEqual(self.snapshot(), before)

    def test_apply_rejects_reports_inside_library(self):
        with patch.object(qa, "REPORTS", self.library / "reports"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(qa.main(["--library", str(self.library), "--only", KEY, "--apply"]), 2)
        self.assertFalse((self.library / "reports").exists())

    def test_cli_invalid_modes_and_limits(self):
        for extra in (["--limit", "-1"], ["--apply", "--dry-run"], ["--only", ""]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    qa.main(["--library", str(self.library)] + extra)
                self.assertEqual(caught.exception.code, 2)

    def test_cli_failure_exit_status(self):
        self.drop_first_pair()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(qa.main(["--library", str(self.library), "--only", KEY, "--dry-run"]), 1)


if __name__ == "__main__":
    unittest.main()
