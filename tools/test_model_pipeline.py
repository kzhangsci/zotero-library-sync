"""Offline integration: real APIBackend -> real QC/translation/cache functions.

Only the transport, credentials and runtime paths are substituted. Fixtures are
invented scientific prose, never user papers. Run only this group with:
    py -3 -B -m unittest discover -s tools -p test_model_pipeline.py -v
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import traceback
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import glm_concurrency
import model_api
import qc_glm
import sync


KEYS = {"glm": "fakekey.pipeline.glm", "deepseek": "fakekey.pipeline.deepseek", "tongji": "fakekey.pipeline.tongji"}
PRIVATE = "SYNTHETIC-SDK-PRIVATE"
THINKING = "SYNTHETIC-REASONING-NOT-FOR-OUTPUT"
PROSE = (
    "The synthetic density prole is sampled at regular depths to compare adjacent "
    "layers under identical settings. These idealized values describe a numerical "
    "fixture and do not establish a mechanism in observations. A uniform grid keeps "
    "the comparison reproducible while preserving the original measurement notation. "
    "Mixing remains unresolved."
)
CORRECTED = PROSE.replace("prole", "profile")
IMAGE = "![synthetic](assets/layer.svg)"
FORMULA = "$$N^2=10^{-4}\\;\\mathrm{s}^{-2}$$"
REFERENCES = "# References\n\nSynthetic Author (2099). Invented fixture, not a publication."
SOURCE = "\n\n".join((PROSE, IMAGE + "\nFig. 1. Synthetic density layer.", FORMULA, REFERENCES))
ZH = "合成密度剖面用于比较相邻层，数值试验不证明观测中的形成机制，混合过程仍未确定。"


class SyntheticSDKError(RuntimeError):
    """Stand-in for an SDK exception; no SDK client or request is created."""


class SyntheticTransport:
    def __init__(self, content="[1]\n" + ZH, *, status=200, error=None,
                 actual_model=None, finish_reason="stop"):
        self.content, self.status, self.error = content, status, error
        self.actual_model, self.finish_reason = actual_model, finish_reason
        self.calls = []

    def request(self, method, url, *, headers, json, timeout):
        # Record only synthetic prompts; never retain the Authorization header.
        self.calls.append((method, url, copy.deepcopy(json)))
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return model_api.TransportResult(self.status, {"error": PRIVATE, "headers": PRIVATE})
        return model_api.TransportResult(200, {
            "model": self.actual_model or json["model"],
            "choices": [{"index": 0, "finish_reason": self.finish_reason,
                         "message": {"role": "assistant", "content": self.content,
                                     "reasoning_content": THINKING}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        })


class ModelPipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="model-pipeline-offline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs, self.delays, self.forbidden = [], [], []
        # These mocks are tripwires, not replacements for the pipeline/backend.
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "requests.sessions.Session.request",
                       "subprocess.Popen", "model_api.DPAPIConfigStore.load",
                       "model_api.DPAPIConfigStore.save"):
            guard = patch(target, side_effect=AssertionError("outside offline test boundary"))
            self.forbidden.append(guard.start())
            self.addCleanup(guard.stop)
        for module, name, value in (
            (sync, "CACHE", self.root / "cache"),
            (sync, "LIB", self.root / "unused-library"),
            (sync, "QC_TRASH", self.root / "unused-trash"),
            (sync, "FAIL_MEMO", self.root / "unused-failures.json"),
            (sync, "STOP_FILE", self.root / "unused-stop.txt"),
            (model_api, "DEFAULT_CONFIG", self.root / "unused-config.dpapi"),
            (glm_concurrency, "ROOT", self.root / "slots"),
            (qc_glm, "QC_PARALLEL", 1),
        ):
            guard = patch.object(module, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        # Replacing this module's time reference does not alter FileLock timing.
        self.qc_sleep = Mock(side_effect=AssertionError("QC must not wait after API failure"))
        guard = patch.object(qc_glm, "time", types.SimpleNamespace(sleep=self.qc_sleep))
        guard.start()
        self.addCleanup(guard.stop)
        self.source = self.root / "Synthetic2099-Fixture_EN.md"
        self.destination = self.root / "Synthetic2099-Fixture_ZH.md"
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "layer.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">'
            '<path d="M0 2h4" stroke="black"/></svg>', encoding="utf-8")
        self.write_source(SOURCE)

    def tearDown(self):
        for guard in self.forbidden:
            guard.assert_not_called()
        self.qc_sleep.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        # The real translation pipeline writes raw final answers and cache JSON
        # in the temp tree; none may receive keys or provider reasoning/errors.
        for path in self.root.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                for forbidden in (*KEYS.values(), PRIVATE, THINKING):
                    self.assertNotIn(forbidden.encode(), content, path.name)

    def write_source(self, text):
        self.source.write_text(text, encoding="utf-8")
        self.original_bytes = self.source.read_bytes()
        return self.source.read_text(encoding="utf-8")

    def backend(self, role, transport, *, provider="glm", model="fixture-model-a",
                effort="default", max_tokens=32768):
        return model_api.APIBackend(
            {"provider": provider, "model": model, "reasoning_effort": effort,
             "timeout": 60, "max_tokens": max_tokens}, role, self.logs.append,
            transport=transport, key_store=KEYS, slot_root=self.root / "slots",
            sleep=self.delays.append,
        )

    def build(self, body, backend, *, cache=False):
        return sync.build_zh(body, {"title": "Synthetic fixture, not a paper"},
                             "Synthetic2099-Fixture", "TEST0001", backend,
                             [("density", "密度")], parallel=1, chunk_cache=cache)

    def publish(self, backend):
        # Model the caller boundary: a raised build must never reach publication.
        text = self.build(self.source.read_text(encoding="utf-8"), backend,
                          cache=self.root / "chunks")
        self.destination.write_text(text, encoding="utf-8")

    def test_independent_api_roles_qc_then_translation(self):
        for qc_provider, zh_provider in (("glm", "deepseek"), ("deepseek", "glm"), ("tongji", "glm"), ("deepseek", "tongji")):
            with self.subTest(qc=qc_provider, zh=zh_provider):
                qc_transport = SyntheticTransport("[1]\n" + CORRECTED, actual_model="qc-served")
                zh_transport = SyntheticTransport(
                    "[1]\n" + ZH + "\n[2]\n" + IMAGE
                    + "\n[3]\n图 1. 合成密度层。\n[4]\n" + FORMULA,
                    actual_model="zh-served")
                qc = self.backend("qc", qc_transport, provider=qc_provider, model="qc-requested")
                zh = self.backend("zh", zh_transport, provider=zh_provider, model="zh-requested", effort="high")
                body, accepted, rejected = qc_glm.qc_body(self.source.read_text(encoding="utf-8"), qc)
                self.assertEqual(body, SOURCE.replace(PROSE, CORRECTED))
                self.assertEqual(len(accepted), 1)
                self.assertEqual(rejected, [])
                translated = self.build(body, zh)
                self.assertIn(ZH, translated)
                self.assertIn("> " + CORRECTED, translated)
                self.assertEqual(translated.count("> [!quote]- en"), 2)
                self.assertEqual(translated.count(IMAGE), 1)
                self.assertEqual(translated.count(FORMULA), 1)
                self.assertIn(REFERENCES, translated)
                self.assertEqual(qc.used_paper, {qc_provider + "/qc-served"})
                self.assertEqual(zh.used_paper, {zh_provider + "/zh-served"})
                zh.used_paper.clear()
                self.assertEqual(zh.used_run, {zh_provider + "/zh-served"})
                self.assertEqual(qc.used_paper, {qc_provider + "/qc-served"})
                for backend, transport, role, provider in (
                    (qc, qc_transport, "qc", qc_provider), (zh, zh_transport, "zh", zh_provider),
                ):
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(transport.calls[0][1], model_api.ENDPOINTS[provider])
                    self.assertEqual(backend.last_usage["role"], role)
                    self.assertEqual(backend.last_usage["usage"]["total_tokens"], 20)
                    self.assertNotIn(THINKING, json.dumps(backend.usage_history))
                self.assertNotIn(REFERENCES, zh_transport.calls[0][2]["messages"][0]["content"])

    def test_qc_rejects_whole_sentence_deletion_within_length_tolerance(self):
        body = self.write_source(PROSE)
        proposed = CORRECTED.removesuffix(" Mixing remains unresolved.")
        self.assertLess(abs(len(body) - len(proposed)), len(body) * qc_glm.LEN_TOL)
        transport = SyntheticTransport("[1]\n" + proposed)
        result, accepted, notes = qc_glm.qc_body(body, self.backend("qc", transport))
        self.assertEqual(result, body)
        self.assertEqual(accepted, [])
        self.assertTrue(any("drops a sentence" in note for note in notes), notes)
        self.assertEqual(len(transport.calls), 1)

    def test_qc_rejects_embedded_image_deletion(self):
        body = self.write_source(PROSE + " " + IMAGE)
        transport = SyntheticTransport("[1]\n" + CORRECTED)
        result, accepted, notes = qc_glm.qc_body(body, self.backend("qc", transport))
        self.assertEqual(result, body)
        self.assertEqual(accepted, [])
        self.assertTrue(any("drops an image or display formula" in note for note in notes), notes)
        self.assertEqual(len(transport.calls), 1)

    def test_translation_restores_image_omitted_by_api_content(self):
        body = self.write_source(CORRECTED + " " + IMAGE)
        transport = SyntheticTransport("[1]\n" + ZH)  # Deliberately omits image.
        output = self.build(body, self.backend("zh", transport))
        unquoted = "\n".join(line for line in output.splitlines() if not line.startswith(">"))
        self.assertIn(ZH, unquoted)
        self.assertEqual(unquoted.count(IMAGE), 1)
        self.assertEqual(output.count(IMAGE), 2)  # source quote plus restored link
        self.assertEqual(len(transport.calls), 1)

    def test_unparseable_qc_success_stops_before_translation_or_publication(self):
        body = self.write_source(PROSE)
        invalid_answers = (
            "I cannot process this request.",
            "[999]",
            "[999]\n" + CORRECTED,
            "[1]",
            "[0]\n" + CORRECTED,
            "[1]\n" + CORRECTED + "\n\n[999]\nUnsolicited replacement.",
        )
        for index, answer in enumerate(invalid_answers):
            with self.subTest(answer=answer[:60]):
                qc_transport = SyntheticTransport(answer)
                zh_transport = SyntheticTransport()
                qc = self.backend("qc", qc_transport)
                zh = self.backend("zh", zh_transport, provider="deepseek")
                output = self.root / f"invalid-qc-{index}_ZH.md"
                with self.assertRaises((RuntimeError, ValueError)):
                    checked, _, _ = qc_glm.qc_body(body, qc)
                    output.write_text(self.build(checked, zh), encoding="utf-8")
                self.assertEqual(len(qc_transport.calls), 1)
                self.assertEqual(zh_transport.calls, [])
                self.assertFalse(output.exists())
                self.assertEqual(self.delays, [])
                self.qc_sleep.assert_not_called()

    def test_explicit_qc_no_change_remains_valid_and_allows_translation(self):
        body = self.write_source(PROSE)
        for answer in ("无", "无。", " \n无\n "):
            with self.subTest(answer=answer):
                qc_transport = SyntheticTransport(answer)
                checked, accepted, notes = qc_glm.qc_body(body, self.backend("qc", qc_transport))
                self.assertEqual((checked, accepted, notes), (body, [], []))
                zh_transport = SyntheticTransport()
                translated = self.build(checked, self.backend("zh", zh_transport, provider="deepseek"))
                self.assertIn(ZH, translated)
                self.assertEqual(len(qc_transport.calls), 1)
                self.assertEqual(len(zh_transport.calls), 1)

    def test_real_pipeline_cache_isolates_provider_model_effort_role_and_tokens(self):
        body = self.write_source(CORRECTED)
        cache = self.root / "chunks"
        variants = [
            {"role": "zh"},
            {"role": "zh", "provider": "deepseek"},
            {"role": "zh", "model": "fixture-model-b"},
            {"role": "zh", "effort": "high"},
            {"role": "qc"},  # Deliberate role-axis exercise of the shared cache.
            {"role": "zh", "max_tokens": 1024},
        ]
        identities = set()
        for index, variant in enumerate(variants):
            with self.subTest(variant=variant):
                answer = f"合成译文编号{index}：密度分层用于数值比较。"
                transport = SyntheticTransport("[1]\n" + answer)
                backend = self.backend(transport=transport, **variant)
                output = self.build(body, backend, cache=cache)
                self.assertIn(answer, output)
                self.assertEqual(len(transport.calls), 1)
                identities.add(json.dumps(backend.cache_identity, sort_keys=True))
                # A new backend represents a new run, retaining only public config.
                replay_transport = SyntheticTransport(error=AssertionError("cache must avoid request"))
                replay = self.backend(transport=replay_transport, **variant)
                self.assertEqual(self.build(body, replay, cache=cache), output)
                self.assertEqual(replay_transport.calls, [])
                self.assertEqual(replay.used_paper, {replay.model})
                self.assertEqual(replay.usage_history, ())
        self.assertEqual(len(identities), len(variants))
        self.assertEqual(len(list(cache.glob("*.json"))), len(variants))

    def test_cache_replay_restores_actual_model_not_requested_alias_or_prior_marks(self):
        body = self.write_source(CORRECTED)
        cache = self.root / "chunks"
        transport = SyntheticTransport(actual_model="served-revision-a")
        original = self.backend("zh", transport, model="requested-alias")
        original._mark("glm/unrelated-earlier-call")
        output = self.build(body, original, cache=cache)
        self.assertEqual(original.used_paper, {"glm/unrelated-earlier-call", "glm/served-revision-a"})
        replay_transport = SyntheticTransport(error=AssertionError("cache must avoid request"))
        replay = self.backend("zh", replay_transport, model="requested-alias")
        self.assertEqual(self.build(body, replay, cache=cache), output)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(replay_transport.calls, [])
        self.assertEqual(replay.used_paper, {"glm/served-revision-a"})
        self.assertEqual(replay.used_run, {"glm/served-revision-a"})
        self.assertEqual(replay.usage_history, ())  # cache hits are not new usage

    def test_api_cache_without_model_provenance_is_recomputed(self):
        body = self.write_source(CORRECTED)
        cache = self.root / "chunks"
        original = self.backend("zh", SyntheticTransport(actual_model="served-revision-a"),
                                model="requested-alias")
        self.build(body, original, cache=cache)
        cache_path, = cache.glob("*.json")
        entry = json.loads(cache_path.read_text(encoding="utf-8"))
        # Reproduce the old cache envelope: validated text, no actual-model marks.
        cache_path.write_text(json.dumps({k: entry[k] for k in ("key", "result", "sha256")}),
                              encoding="utf-8")
        refreshed = SyntheticTransport("[1]\n重新计算的合成译文。", actual_model="served-revision-b")
        replay = self.backend("zh", refreshed, model="requested-alias")
        self.assertIn("重新计算的合成译文。", self.build(body, replay, cache=cache))
        self.assertEqual(len(refreshed.calls), 1)
        self.assertEqual(replay.used_paper, {"glm/served-revision-b"})
        self.assertEqual(len(replay.usage_history), 1)

    def assert_pipeline_stops(self, role, provider, transport, *, expected_calls, code, status=None):
        self.write_source(PROSE)
        self.delays.clear()
        self.logs.clear()
        backend = self.backend(role, transport, provider=provider)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(model_api.ModelAPIError) as caught:
            if role == "qc":
                qc_glm.qc_body(self.source.read_text(encoding="utf-8"), backend)
            else:
                self.publish(backend)
        self.assertTrue(caught.exception.stop_workflow)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.assertEqual(len(transport.calls), expected_calls)
        self.assertEqual(self.delays, [1, 2] if expected_calls == 3 else [])
        self.assertFalse(self.destination.exists())
        self.assertEqual(list((self.root / "chunks").glob("*.json")), [])
        self.assertEqual(list((self.root / "cache" / "glm_raw").glob("*.txt")), [])
        self.assertEqual(backend.used_run, set())
        self.assertEqual(backend.usage_history, ())
        self.qc_sleep.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(self.logs, [])  # no per-paragraph fallback warnings
        formatted = "".join(traceback.format_exception(caught.exception))
        for secret in (*KEYS.values(), PRIVATE, THINKING):
            self.assertNotIn(secret, formatted)
        # A later chunk on this backend must fail without another transport call.
        with self.assertRaises(model_api.ModelAPIError) as later:
            backend.ask("A different synthetic chunk after the terminal API error.")
        self.assertTrue(later.exception.stop_workflow)
        self.assertEqual((later.exception.code, later.exception.status), (code, status))
        self.assertEqual(len(transport.calls), expected_calls)

    def test_401_stops_both_roles_after_one_request_without_publication(self):
        for provider in KEYS:
            for role in ("qc", "zh"):
                with self.subTest(provider=provider, role=role):
                    self.assert_pipeline_stops(role, provider, SyntheticTransport(status=401),
                                               expected_calls=1, code="http_error", status=401)

    def test_429_exhaustion_stops_both_roles_at_three_requests(self):
        for provider in KEYS:
            for role in ("qc", "zh"):
                with self.subTest(provider=provider, role=role):
                    self.assert_pipeline_stops(role, provider, SyntheticTransport(status=429),
                                               expected_calls=3, code="http_error", status=429)

    def test_sdk_exceptions_are_sanitized_and_stop_without_qc_wait_or_zh_fallback(self):
        for error_type, code in ((SyntheticSDKError, "transport_error"), (requests.Timeout, "request_timeout")):
            for role in ("qc", "zh"):
                with self.subTest(error=error_type.__name__, role=role):
                    error = error_type(PRIVATE + " Authorization: Bearer " + KEYS["glm"])
                    self.assert_pipeline_stops(role, "glm", SyntheticTransport(error=error),
                                               expected_calls=1, code=code)

    def test_truncated_api_content_never_enters_qc_or_published_translation(self):
        for role in ("qc", "zh"):
            with self.subTest(role=role):
                self.assert_pipeline_stops(role, "deepseek", SyntheticTransport(finish_reason="length"),
                                           expected_calls=1, code="incomplete_completion")

    def test_tongji_flash_max_qc_uses_smaller_whole_paragraph_groups(self):
        paras = [str(i) + " density prole" * 100 for i in range(5)]
        body = "\n\n".join(paras)
        for effort, count in (("max", 3), ("default", 1)):
            backend = self.backend("qc", SyntheticTransport(), provider="tongji",
                                   model="DeepSeek-V4.1-Flash", effort=effort)
            with patch.object(qc_glm, "qc_chunk", return_value=({}, [])) as ask:
                checked, _, _ = qc_glm.qc_body(body, backend)
            groups = [call.args[0] for call in ask.call_args_list]
            self.assertEqual([p for group in groups for p in group], paras)
            self.assertEqual((len(groups), checked), (count, body))

    def test_qc_resume_reuses_complete_reply_and_rechecks_guards(self):
        directory = self.root / "qc-chunks"
        def backend(transport, effort="max"):
            return self.backend("qc", transport, provider="tongji", model="DeepSeek-V4.1-Flash", effort=effort)
        first = SyntheticTransport("无")
        be = backend(first)
        qc_glm.qc_chunk([PROSE], be, directory)
        first.finish_reason = "length"
        with self.assertRaisesRegex(model_api.ModelAPIError, "finish_reason=length"):
            qc_glm.qc_chunk([PROSE + " Next."], be, directory)
        self.assertEqual(len(list(directory.glob("*.json"))), 1)
        second = SyntheticTransport("无")
        be = backend(second)
        qc_glm.qc_chunk([PROSE], be, directory)
        self.assertEqual(len(second.calls), 0)
        self.assertEqual(be.used_paper, {"tongji/DeepSeek-V4.1-Flash"})
        qc_glm.qc_chunk([PROSE + " Next."], be, directory)
        qc_glm.qc_chunk([PROSE], backend(second, "high"), directory)
        self.assertEqual(len(second.calls), 2)
        proposed = SyntheticTransport("[1]\n" + CORRECTED)
        qc_glm.qc_chunk([PROSE], backend(proposed), self.root / "judge-cache")
        with patch.object(qc_glm, "judge", return_value="rejected by current guard") as judge:
            ok, notes = qc_glm.qc_chunk([PROSE], backend(proposed), self.root / "judge-cache")
        judge.assert_called_once()
        self.assertFalse(ok)
        self.assertTrue(notes)
        self.assertEqual(len(proposed.calls), 1)


if __name__ == "__main__":
    unittest.main()
