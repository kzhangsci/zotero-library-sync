"""Offline public-document fixtures; never read keys or call a paid API."""
import copy
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import model_api as api
import model_catalog as catalog
from test_model_api import OfflineCase, FakeTransport, FAKE_KEYS, profile, config


# Minimal structure-preserving excerpts, with unrelated enums/mentions as traps.
CHAT = '''# Chat completion
Incidental glm-fake-prose is not a model declaration.
````yaml /openapi/openapi.json post /paas/v4/chat/completions
openapi: 3.0.1
components:
  schemas:
    ChatCompletionTextRequest:
      type: object
      properties:
        model:
          type: string
          description: glm-fake-example
          enum:
            - glm-5.3
            - glm-5.2
            - glm-fixture-future
            - glm-5.3
        messages:
          items:
            properties:
              role:
                enum:
                  - glm-fake-role
    ChatCompletionVisionRequest:
      properties:
        model:
          enum:
            - glm-fake-vision
    ChatCompletionAudioRequest:
      properties:
        model:
          enum:
            - glm-fake-audio
````
'''
FLASH = '''# GLM Flash
> **上下文窗口**
> 1M
> **最大输出**
> 128K
**Model Code**：`glm-5.3-flash`/`glm-5.3-flashx`/`glm-5.3-flashx-legal`
**推荐设置**：`thinking.type` 仅支持 `enabled`，`reasoning_effort: max`。
套餐当前暂未开放 GLM-5.3-FlashX。
Unrelated: `glm-fake-mention`
'''
CODING = '''# 套餐概览
## 可用模型
* 所有套餐均支持 **GLM-5.3**、GLM-5.3-Flash。
* 调用历史模型 GLM-5.2、GLM-5.1 都将自动切换至 GLM-5.3，调用 GLM-5-Turbo、GLM-4.7 将自动切换至 GLM-5.3-Flash。
## 适用工具
* 支持 GLM-fake-outside。
'''
DOCUMENTS = {catalog.GLM_CHAT_DOCUMENT: CHAT, catalog.GLM_FLASH_DOCUMENT: FLASH,
             catalog.GLM_CODING_DOCUMENT: CODING}
STAMP = "2026-10-26T01:02:03+00:00"


class CatalogTests(OfflineCase):
    def fixture_catalog(self):
        self.fetch = MagicMock(side_effect=DOCUMENTS.__getitem__)
        return catalog.OfficialModelCatalog(fetcher=self.fetch, now=lambda: STAMP)

    def test_structure_scoped_parsers_and_dynamic_new_ids(self):
        self.assertEqual(catalog.parse_chat_models(CHAT), ["glm-5.3", "glm-5.2", "glm-fixture-future"])
        self.assertEqual(catalog.parse_flash_models(FLASH), list(catalog.GLM_MODEL_CANDIDATES[1:4]))
        self.assertEqual(catalog.parse_coding_models(CODING), ["glm-5.3", "glm-5.3-flash"])
        updated = CODING.replace("GLM-5.3-Flash。", "GLM-5.3-Flash、GLM-fixture-new。", 1)
        self.assertIn("glm-fixture-new", catalog.parse_coding_models(updated))
        models = self.fixture_catalog().get()["models"]
        self.assertEqual(models, ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal",
                                  "glm-5.2", "glm-fixture-future"])

    def test_missing_or_malformed_sections_fail_closed(self):
        cases = [(catalog.parse_chat_models, CHAT.replace("ChatCompletionTextRequest:", "UnknownSchema:")),
                 (catalog.parse_chat_models, CHAT.replace("glm-5.2\n", "https://malicious.invalid\n")),
                 (catalog.parse_chat_models, CHAT.replace("openapi: 3.0.1", "openapi: 0")),
                 (catalog.parse_chat_models, "model:\n  enum:\n    - glm-fake"),
                 (catalog.parse_flash_models, FLASH.replace("Model Code", "Example")),
                 (catalog.parse_coding_models, CODING.replace("可用模型", "历史模型")),
                 (catalog.parse_coding_models, "## 可用模型\n* 调用历史模型 GLM-5.2 映射至 GLM-5.3。"),
                 (catalog.parse_coding_models, "## 可用模型\n* 套餐不支持 GLM-fake。")]
        for parser, document in cases:
            with self.subTest(parser=parser.__name__, document=document[:30]), self.assertRaises(catalog.CatalogError):
                parser(document)

    def test_bounds_apply_to_injected_documents_and_models(self):
        for bad in (None, b"\xff", "\x00", "x" * (catalog.MAX_DOCUMENT_BYTES + 1)):
            with self.subTest(value=repr(bad)[:20]), self.assertRaises(catalog.CatalogError):
                catalog.parse_chat_models(bad)
        many = CHAT.replace("            - glm-5.2", "\n".join(f"            - glm-fixture-{i}" for i in range(513)))
        with self.assertRaises(catalog.CatalogError):
            catalog.parse_chat_models(many)

    def test_default_refresh_provenance_and_no_credentials(self):
        instance = self.fixture_catalog()
        with patch.object(api, "_store", side_effect=AssertionError("no credentials")), \
                patch.object(api, "_request", side_effect=AssertionError("no API request")):
            first = api.model_choices("glm", catalog=instance)
            second = api.model_choices("glm", catalog=instance)
        self.assertEqual(self.fetch.call_count, 4)
        self.assertEqual(first, second)
        self.assertEqual(first["source"], "official_docs")
        self.assertEqual(first["fetched_at"], STAMP)
        self.assertEqual(first["endpoint_kind"], "standard")
        self.assertEqual(first["reference"], catalog.GLM_CHAT_DOCUMENT)
        self.assertIn("非账户可用清单", first["notice"])
        self.assertNotIn("glm-fixture-future", first["details"])

    def test_cache_only_after_complete_refresh_and_separate_endpoint_kinds(self):
        instance = self.fixture_catalog()
        initial = instance.get()
        initial["models"].append("caller-mutated")
        initial["details"]["glm-5.3"]["effort_levels"].clear()
        self.fetch.side_effect = TimeoutError("secret transport text")
        cached = instance.get()
        self.assertEqual(cached["source"], "official_cache")
        self.assertEqual(cached["fetched_at"], STAMP)
        self.assertIn("非实时", cached["notice"])
        self.assertNotIn("caller-mutated", cached["models"])
        self.assertEqual(cached["details"]["glm-5.3"]["highest_effort"], "max")
        coding = instance.get("coding_plan")
        self.assertEqual(coding["source"], "bundled_catalog")
        self.assertEqual(coding["models"], ["glm-5.3", "glm-5.3-flash"])
        self.assertIsNone(coding["fetched_at"])
        self.assertIn("非实时", coding["notice"])
        self.assertNotIn("secret", repr(cached) + repr(coding))
        self.fetch.reset_mock()
        self.assertEqual(instance.get(refresh=False), cached)
        self.fetch.assert_not_called()

    def test_partial_or_changed_doc_refresh_does_not_poison_cache(self):
        instance = self.fixture_catalog()
        original = instance.get()
        self.fetch.side_effect = lambda url: CHAT.replace("glm-fixture-future", "glm-changed") if url == catalog.GLM_CHAT_DOCUMENT else "broken"
        cached = instance.get()
        self.assertEqual(cached["source"], "official_cache")
        self.assertEqual(cached["models"], original["models"])
        self.assertEqual(self.fixture_catalog().get(refresh=False)["source"], "bundled_catalog")

    def test_coding_only_fetches_coding_page_and_never_changes_profile(self):
        candidate = {**api.profile_defaults()["glm"], "model": "glm-5.2", "endpoint_confirmed": True,
                     "base_url": "https://open.bigmodel.cn/api/coding/paas/v4"}
        before = copy.deepcopy(candidate)
        instance = self.fixture_catalog()
        result = api.model_choices(profile=candidate, catalog=instance)
        self.fetch.assert_called_once_with(catalog.GLM_CODING_DOCUMENT)
        self.assertEqual(result["source"], "official_docs")
        self.assertEqual(result["endpoint_kind"], "coding_plan")
        self.assertEqual(result["models"], ["glm-5.3", "glm-5.3-flash"])
        self.assertEqual(candidate, before)

    def test_unconfirmed_coding_profile_is_a_trusted_v2_endpoint(self):
        candidate = {**api.profile_defaults()["glm"], "model": "glm-5.3", "endpoint_confirmed": False,
                     "base_url": "https://open.bigmodel.cn/api/coding/paas/v4", "models_url": ""}
        before = copy.deepcopy(candidate)
        instance = self.fixture_catalog()
        with patch.object(api, "_store", side_effect=AssertionError("no credentials")), \
                patch.object(api, "_request", side_effect=AssertionError("no credentialed HTTP")):
            validated = api.validate_profile(candidate, for_models=True)
            self.assertEqual(validated, candidate)
            self.assertEqual(api.validate_profile(candidate), validated)
            result = api.model_choices(profile=validated, catalog=instance)
            self.assertEqual(result["endpoint_kind"], "coding_plan")
            self.assertEqual(result["models"], ["glm-5.3", "glm-5.3-flash"])
            self.fetch.assert_called_once_with(catalog.GLM_CODING_DOCUMENT)
            self.assertEqual(api.highest_parameters(candidate)["max_tokens"], 131072)
            api.RequestsTransport(profile=candidate)  # no confirmation gate before a credential read
            with self.assertRaisesRegex(api.UnsupportedOperation, "^model_listing_not_supported$"):
                api.list_models(profile=candidate)
        configured = {**config(), "qc": candidate}
        self.assertEqual(api.validate_config(configured)["qc"], validated)
        self.assertEqual(api.save_config(configured, key_store=self.store)["qc"], validated)
        api.create_backend(candidate, "qc", key_store=self.store)
        self.assertEqual(candidate, before)

    def test_anonymous_coding_exception_is_exact_and_cannot_enable_custom_listing(self):
        candidate = {**api.profile_defaults()["glm"], "base_url": "https://open.bigmodel.cn/api/coding/paas/v4"}
        variants = ({"models_url": "https://open.bigmodel.cn/api/coding/paas/v4/models"},
                    {"models_url": "https://custom.example/models"},
                    {"base_url": "https://open.bigmodel.cn/api/coding/paas/v4-other"},
                    {"base_url": "https://open.bigmodel.cn:444/api/coding/paas/v4"},
                    {"base_url": "https://custom.example/api/coding/paas/v4"},
                    {"provider": "deepseek"})
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaisesRegex(api.ConfigError, "^endpoint_confirmation_required$"):
                api.validate_profile({**candidate, **changes}, for_models=True)

    def test_custom_without_listing_does_not_fetch_or_receive_glm_candidates(self):
        for base in ("https://custom.example/v1", "https://open.bigmodel.cn/custom/v4",
                     "https://open.bigmodel.cn:444/api/paas/v4", "https://evil.example/api/coding/paas/v4"):
            instance = self.fixture_catalog()
            candidate = {**api.profile_defaults()["glm"], "base_url": base, "endpoint_confirmed": True}
            with patch.object(api, "_store", side_effect=AssertionError("no credentials")):
                result = api.model_choices(profile=candidate, catalog=instance)
            self.assertEqual(result["endpoint_kind"], "custom")
            self.assertEqual(result["models"], [])
            self.assertIn("非实时", result["notice"])
            self.fetch.assert_not_called()

    def test_custom_explicit_listing_takes_priority_over_docs(self):
        candidate = {**api.profile_defaults()["glm"], "models_url": "https://custom.example/catalog",
                     "endpoint_confirmed": True}
        instance = self.fixture_catalog()
        transport = FakeTransport(api.TransportResult(200, {"data": [{"id": "custom-id"}]}))
        result = api.model_choices(profile=candidate, catalog=instance, transport=transport,
                                   key_store=FAKE_KEYS, slot_root=self.root / "slots")
        self.assertEqual(result["source"], "provider_api")
        self.assertEqual(result["models"], ["custom-id"])
        self.assertEqual(transport.calls[0][1], candidate["models_url"])
        self.fetch.assert_not_called()

    def test_catalog_import_has_no_io(self):
        spec = importlib.util.spec_from_file_location("catalog_import_fixture", catalog.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("no files")), \
                patch.object(catalog.requests, "Session", side_effect=AssertionError("no HTTP")):
            spec.loader.exec_module(module)


class DocumentFetchTests(OfflineCase):
    def fake_session(self, status=200, chunks=None, headers=None):
        session, response = MagicMock(), MagicMock()
        session.__enter__.return_value = session
        session.get.return_value.__enter__.return_value = response
        response.status_code = status
        response.headers = {} if headers is None else headers
        response.iter_content.return_value = [CHAT.encode()] if chunks is None else chunks
        return session, response

    def fetch(self, *args, **kwargs):
        # Bypass only the OfflineCase network-fetch mock, keeping its real socket
        # guards. Session itself is mocked in each test below.
        return self.original_fetch(*args, **kwargs)

    def setUp(self):
        self.original_fetch = catalog.fetch_official_markdown
        super().setUp()

    def test_whitelist_anonymous_short_timeout_tls_no_redirect(self):
        session, response = self.fake_session()
        with patch.object(catalog.requests, "Session", return_value=session):
            self.assertEqual(self.fetch(catalog.GLM_CHAT_DOCUMENT), CHAT)
        self.assertFalse(session.trust_env)
        self.assertIsNone(session.auth)
        session.cookies.clear.assert_called_once()
        args = session.get.call_args.kwargs
        self.assertEqual(args["timeout"], (3, 5))
        self.assertFalse(args["allow_redirects"])
        self.assertTrue(args["verify"])
        self.assertEqual(args["headers"], {"Accept": "text/markdown"})
        session.get.assert_called_once()
        for url in ("http://docs.bigmodel.cn/", catalog.GLM_CHAT_DOCUMENT + "?key=secret",
                    "https://evil.example/catalog", "https://docs.bigmodel.cn/llms.txt"):
            with patch.object(catalog.requests, "Session") as factory, self.assertRaises(catalog.CatalogError):
                self.fetch(url)
            factory.assert_not_called()

    def test_http_errors_and_redirects_do_not_follow_or_echo_body(self):
        for status in (301, 302, 307, 308, 401, 403, 429, 500):
            session, response = self.fake_session(status=status, headers={"Location": "https://secret.example"})
            with patch.object(catalog.requests, "Session", return_value=session), self.assertRaisesRegex(
                    catalog.CatalogError, "^document_http_error$"):
                self.fetch(catalog.GLM_CHAT_DOCUMENT)
            session.get.assert_called_once()
            response.iter_content.assert_not_called()

    def test_size_encoding_and_elapsed_time_bounds(self):
        cases = [({"chunks": [b"x" * catalog.MAX_DOCUMENT_BYTES, b"x"]}, "document_too_large"),
                 ({"headers": {"Content-Length": str(catalog.MAX_DOCUMENT_BYTES + 1)}}, "document_too_large"),
                 ({"chunks": [b"\xff"]}, "invalid_document_encoding")]
        for options, error in cases:
            session, _ = self.fake_session(**options)
            with patch.object(catalog.requests, "Session", return_value=session), self.assertRaisesRegex(catalog.CatalogError, error):
                self.fetch(catalog.GLM_CHAT_DOCUMENT)
        session, _ = self.fake_session()
        with patch.object(catalog.requests, "Session", return_value=session), \
                patch.object(catalog.time, "monotonic", side_effect=[0, 9]), \
                self.assertRaisesRegex(catalog.CatalogError, "^document_timeout$"):
            self.fetch(catalog.GLM_CHAT_DOCUMENT)
        with patch.object(catalog.requests, "Session", side_effect=TimeoutError("secret")), \
                self.assertRaisesRegex(catalog.CatalogError, "^document_fetch_failed$"):
            self.fetch(catalog.GLM_CHAT_DOCUMENT)


class HighestParametersTests(OfflineCase):
    def test_capabilities_are_scoped_to_exact_known_endpoints(self):
        for provider, model in (("glm", "glm-5.3"), ("deepseek", "deepseek-flash")):
            for changes in ({"base_url": "https://custom.example/v1", "models_url": ""},
                            {"models_url": "https://custom.example/models"}):
                candidate = {**api.profile_defaults()[provider], "model": model, "endpoint_confirmed": True, **changes}
                self.assertEqual(api.highest_parameters(candidate), {})
                self.assertEqual(api.highest_parameters(candidate, {"max_output_tokens": 4567}), {"max_tokens": 4567})
                self.assertEqual(api.highest_parameters(candidate, {"effort_levels": ["high", "low"]}),
                                 {"reasoning_effort": "high", "thinking": "enabled"})
            self.assertEqual(catalog.known_details(provider, model, kind="custom"), {})
        candidate = {**api.profile_defaults()["glm"], "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                     "endpoint_confirmed": True}
        for model in ("glm-5.2", "glm-5.3-flashx", "glm-5.3-flashx-legal"):
            candidate["model"] = model
            self.assertEqual(api.highest_parameters(candidate), {})
            self.assertEqual(api.highest_parameters(candidate, catalog.known_details("glm", model)), {})
            self.assertEqual(catalog.known_details("glm", model, kind="coding_plan"), {})
        for model in catalog.GLM_CODING_CANDIDATES:
            candidate["model"] = model
            self.assertEqual(api.highest_parameters(candidate)["max_tokens"], 131072)
            self.assertEqual(catalog.known_details("glm", model, kind="coding_plan")["context_window"], 1048576)

    def test_custom_listing_cannot_gain_official_limits_from_same_model_id(self):
        for provider, model in (("glm", "glm-5.3"), ("deepseek", "deepseek-flash")):
            candidate = {**api.profile_defaults()[provider], "model": model, "endpoint_confirmed": True,
                         "models_url": "https://custom.example/catalog"}
            transport = FakeTransport(api.TransportResult(200, {"data": [{"id": model, "max_output_tokens": 4567}]}))
            result = api.model_choices(profile=candidate, key_store=FAKE_KEYS, transport=transport,
                                       slot_root=self.root / "slots")
            self.assertEqual(result["endpoint_kind"], "custom")
            self.assertEqual(result["details"][model], {"max_output_tokens": 4567})
            self.assertEqual(api.highest_parameters(candidate, result), {"max_tokens": 4567})
            result["reference"] = "https://other.example/catalog"
            self.assertEqual(api.highest_parameters(candidate, result), {})

    def test_official_metadata_cannot_be_reused_for_custom_chat_endpoint(self):
        candidate = {**api.profile_defaults()["deepseek"], "model": "deepseek-flash", "endpoint_confirmed": True,
                     "base_url": "https://custom.example/v1"}
        transport = FakeTransport(api.TransportResult(200, {"data": [{"id": "deepseek-flash", "max_output_tokens": 393216}]}))
        result = api.model_choices(profile=candidate, key_store=FAKE_KEYS, transport=transport, slot_root=self.root / "slots")
        self.assertEqual(api.highest_parameters(candidate, result), {})
        for source in ("official_docs", "official_cache", "bundled_catalog"):
            self.assertEqual(api.highest_parameters(candidate, {"source": source,
                             "details": {"deepseek-flash": catalog.known_details("deepseek", "deepseek-flash")}}), {})

    def test_effort_display_order_is_preserved_but_never_used_as_rank(self):
        levels = ["max", "low", "high"]
        self.assertEqual(api._model_details({"effort": {"supported_levels": levels}}),
                         {"effort_levels": levels, "highest_effort": "max"})
        self.assertEqual(api.highest_parameters(profile(model="unknown"), {"effort_levels": levels})["reasoning_effort"], "max")

    def test_exact_known_models_and_defaults_without_io_or_mutation(self):
        for provider, models, maximum in (("glm", catalog.GLM_MODEL_CANDIDATES[:4], 131072),
                                         ("deepseek", ("deepseek-flash", "deepseek-v4-pro"), 393216)):
            for model in models:
                candidate = profile(provider, model=model, reasoning_effort="low", max_tokens=1024)
                before = copy.deepcopy(candidate)
                with patch.object(api, "_store", side_effect=AssertionError("no credentials")), \
                        patch.object(catalog.requests, "Session", side_effect=AssertionError("no HTTP")):
                    result = api.highest_parameters(candidate)
                self.assertEqual(result, {"max_tokens": maximum, "reasoning_effort": "max", "thinking": "enabled"})
                self.assertEqual(candidate, before)
                self.assertEqual(set(candidate), set(before))

    def test_unknown_ids_never_inherit_provider_maxima(self):
        for provider in api.ENDPOINTS:
            for model in ("glm-future", "deepseek-future", "deepseek-chat", "glm-5.3-fake", "DeepSeek-V4.1-Flash-1017"):
                self.assertEqual(api.highest_parameters(profile(provider, model=model)), {})
        self.assertEqual(api.highest_parameters(api.profile_defaults()["glm"]), {})

    def test_live_metadata_overrides_snapshot_and_server_default_is_not_maximum(self):
        candidate = profile("deepseek", model="deepseek-flash")
        metadata = {"max_output_tokens": 12345, "effort_levels": ["low", "high"], "default_effort": "low"}
        for details in (metadata, {"deepseek-flash": metadata}, {"details": {"deepseek-flash": metadata}}):
            before = copy.deepcopy(details)
            self.assertEqual(api.highest_parameters(candidate, details),
                             {"max_tokens": 12345, "reasoning_effort": "high", "thinking": "enabled"})
            self.assertEqual(details, before)
        candidate["model"] = "unknown-verified-by-api"
        self.assertEqual(api.highest_parameters(candidate, metadata)["max_tokens"], 12345)
        self.assertEqual(api.highest_parameters(candidate, {"max_output_tokens": True}), {})
        self.assertEqual(api.highest_parameters(candidate, {"effort_levels": ["custom-future"]}), {})

    def test_recommended_defaults_are_high_with_documented_fallback_only(self):
        # User preference: thinking ON + effort high, strictly from each
        # model's documented set. Official default (GLM-5.3: max) and maximum
        # stay separate; nothing is invented and options are never removed.
        self.assertEqual(catalog.recommended_effort(["low", "high", "max"]), "high")
        self.assertEqual(catalog.recommended_effort(["low", "medium"]), "medium")
        self.assertEqual(catalog.recommended_effort(["none", "minimal", "max"]), "max")
        self.assertIsNone(catalog.recommended_effort(["none", "minimal"]))
        self.assertIsNone(catalog.recommended_effort(["low", "future-effort"]))
        for provider, model, base in (("glm", "glm-5.3", api.BASE_URLS["glm"]),
                                       ("glm", "glm-5.2", api.BASE_URLS["glm"]),
                                       ("glm", "glm-5.3", api.GLM_CODING_BASE_URL),
                                       ("deepseek", "deepseek-flash", None)):
            candidate = {**api.profile_defaults()[provider], "model": model}
            if base:
                candidate["base_url"] = base
            with patch.object(api, "_store", side_effect=AssertionError("no credentials")):
                recommended = api.recommended_parameters(candidate)
                highest = api.highest_parameters(candidate)
            self.assertEqual(recommended, {"max_tokens": 393216 if provider == "deepseek" else 131072,
                                           "reasoning_effort": "high", "thinking": "enabled"})
            self.assertEqual(highest["reasoning_effort"], "max")  # maximum stays distinct
            self.assertEqual(candidate["reasoning_effort"], "high")  # pure patch, nothing mutated
        campus = {**api.profile_defaults()["tongji"], "model": "DeepSeek-V4.1-Flash"}
        self.assertEqual(api.recommended_parameters(campus, {"effort_levels": ["none", "high"]}),
                         {"reasoning_effort": "high"})  # Tongji sends no thinking field
        self.assertEqual(api.recommended_parameters(campus, {"effort_levels": ["low", "medium"]}),
                         {"reasoning_effort": "medium"})
        self.assertEqual(api.recommended_parameters(campus, {"effort_levels": ["none"]}), {})
        # Custom endpoints and unknown models never gain official recommendations.
        for changes in ({"base_url": "https://custom.example/v1", "endpoint_confirmed": True},
                        {"model": "glm-future"}):
            self.assertEqual(api.recommended_parameters({**api.profile_defaults()["glm"], **changes}), {})
        custom = {**api.profile_defaults()["glm"], "model": "glm-5.3",
                  "base_url": "https://custom.example/v1", "endpoint_confirmed": True}
        self.assertEqual(api.recommended_parameters(custom, {"max_output_tokens": 8192}), {"max_tokens": 8192})

    def test_legacy_profile_load_defaults_and_cache_identity_stay_unchanged(self):
        for provider in api.ENDPOINTS:
            candidate = {"provider": provider, "model": "glm-5.3" if provider == "glm" else "deepseek-flash"}
            normalized = api.validate_profile(candidate)
            self.assertEqual(normalized, {**candidate, "reasoning_effort": "default", "timeout": 1500, "max_tokens": 32768})
            identity = self.backend(normalized).cache_identity
            self.assertEqual(identity["schema"], 1)
            api.highest_parameters(normalized)
            self.assertEqual(self.backend(normalized).cache_identity, identity)
            self.assertNotIn("thinking", normalized)

    def test_deepseek_metadata_keeps_api_id_separate_from_display_name(self):
        entry = {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash-1017", "context_window": 1048576,
                 "max_output_tokens": 393216, "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"}}
        transport = FakeTransport(api.TransportResult(200, {"data": [entry]}))
        result = api.model_choices("deepseek", transport=transport, key_store=FAKE_KEYS, slot_root=self.root / "slots")
        self.assertEqual(result["models"], ["deepseek-flash"])
        self.assertEqual(result["details"], {"deepseek-flash": {"display_name": "DeepSeek-V4.1-Flash-1017",
            "context_window": 1048576, "max_output_tokens": 393216, "effort_levels": ["low", "high", "max"],
            "default_effort": "high", "highest_effort": "max"}})
        self.assertNotIn("快照补充", result["notice"])
        self.assertEqual(len(transport.calls), 1)
        candidate = {**api.profile_defaults()["deepseek"], "model": result["models"][0]}
        self.backend(candidate).ask("offline fixture")
        self.assertEqual(self.transport.calls[0][2]["json"]["model"], "deepseek-flash")

    def test_known_api_ids_never_fill_missing_metadata_and_keep_reduced_live_caps(self):
        cases = (({}, {}),
                 ({"max_output_tokens": 65536}, {"max_output_tokens": 65536}),
                 ({"name": "Fixture display"}, {"display_name": "Fixture display"}),
                 ({"effort": {"supported_levels": ["high", "low"]}},
                  {"effort_levels": ["high", "low"], "highest_effort": "high"}))
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            for metadata, expected in cases:
                with self.subTest(model=model, metadata=metadata):
                    transport = FakeTransport(api.TransportResult(200, {"data": [{"id": model, **metadata}]}))
                    with patch.object(catalog, "enrich_details", side_effect=AssertionError("no static supplement")):
                        result = api.model_choices("deepseek", key_store=FAKE_KEYS, transport=transport,
                                                   slot_root=self.root / "slots")
                    self.assertEqual(result["source"], "provider_api")
                    self.assertEqual(result["models"], [model])
                    self.assertNotIn("快照补充", result["notice"])
                    if expected:
                        self.assertEqual(result["details"], {model: expected})
                    else:
                        self.assertNotIn("details", result)

    def test_display_and_context_metadata_are_typed_bounded_and_omitted_if_invalid(self):
        for name in (None, 123, "", "   ", "x" * 257, "bad\nname", "bad\x00name"):
            self.assertNotIn("display_name", api._model_details({"name": name}))
        for size in (None, True, 0, -1, "1048576", 1.1, 2**31):
            self.assertNotIn("context_window", api._model_details({"context_window": size}))


if __name__ == "__main__":
    unittest.main()
