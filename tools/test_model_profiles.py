"""Offline v2 profile contract: fixture credentials/HTTP/DNS, never live services."""
import copy
import importlib.util
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import model_api as api
from test_model_api import OfflineCase, FakeTransport, FAKE_KEYS, completion, config, profile


def extended(provider="glm", **changes):
    value = {**api.profile_defaults()[provider], "model": "namespace/model:synthetic-v2"}
    value.update(changes)
    return value


class ProfileTests(OfflineCase):
    def test_defaults_and_validation_are_pure_and_fresh(self):
        with patch.object(api, "_store", side_effect=AssertionError("no credentials")), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("no DNS")):
            templates = api.profile_defaults()
            for provider, default in templates.items():
                if provider == api.ZCODE_PROVIDER:
                    # The local-login template has no endpoint fields at all;
                    # its subscription defaults to the coding route explicitly.
                    self.assertEqual(default, {"provider": provider, "plan": "coding", "model": "",
                                               "reasoning_effort": "high", "timeout": 1500, "max_tokens": 128000})
                    self.assertEqual(api.validate_profile(default, for_models=True), default)
                    with self.assertRaisesRegex(api.ConfigError, "invalid_model_id"):
                        api.validate_profile(default)
                    continue
                base_url = api.GLM_CODING_BASE_URL if provider == "glm" else api.BASE_URLS[provider]
                models_url = "" if provider == "glm" else api.MODEL_ENDPOINTS.get(provider, "")
                self.assertEqual(default, {
                    "provider": provider, "model": "",
                    # User preference: every supported provider defaults to
                    # thinking ON + effort high; Tongji expresses thinking via
                    # reasoning_effort only, so its thinking stays "default".
                    "reasoning_effort": "high",
                    "timeout": 1500, "max_tokens": {"glm": 131072, "deepseek": 393216, "tongji": 32768}[provider],
                    "base_url": base_url,
                    "protocol": "openai_chat_completions", "models_url": models_url,
                    "thinking": "default" if provider == "tongji" else "enabled", "extra_body": {}, "endpoint_confirmed": False,
                })
                self.assertEqual(api.validate_profile(default, for_models=True), default)
                with self.assertRaisesRegex(api.ConfigError, "invalid_model_id"):
                    api.validate_profile(default)
            api.validate_profile(extended(base_url="https://user-selected.example/v1", endpoint_confirmed=True))
            # The trusted Coding Plan draft endpoint needs no extra confirmation.
            api.validate_profile(extended())
        templates["glm"]["extra_body"]["temperature"] = 0.4
        self.assertEqual(api.profile_defaults()["glm"]["extra_body"], {})

    def test_fresh_defaults_prefer_high_and_never_touch_saved_selections(self):
        # "high" is the USER preference, distinct from official defaults and
        # maxima: GLM-5.3's official default is max, DeepSeek's is high. Fresh
        # templates pick high; already-saved values (default/max/custom) are
        # validated back unchanged and never silently migrated.
        templates = api.profile_defaults()
        for provider in (*api.ENDPOINTS, *([api.ZCODE_PROVIDER] if api.ZCODE_PROVIDER in templates else [])):
            self.assertEqual(templates[provider]["reasoning_effort"], "high")
        self.assertEqual(templates["glm"]["thinking"], "enabled")
        self.assertEqual(templates["deepseek"]["thinking"], "enabled")
        self.assertEqual(templates["tongji"]["thinking"], "default")
        for effort in ("default", "max", "none", "custom-v1"):
            self.assertEqual(api.validate_profile(profile("glm", reasoning_effort=effort))["reasoning_effort"], effort)
            self.assertEqual(api.validate_profile(extended("deepseek", reasoning_effort=effort))["reasoning_effort"], effort)

    def test_legacy_normalization_stays_exactly_five_fields(self):
        legacy = api.validate_profile(profile(reasoning_effort="none"))
        self.assertEqual(set(legacy), {"provider", "model", "reasoning_effort", "timeout", "max_tokens"})
        backend = self.backend(legacy)
        self.assertEqual(backend.cache_identity["schema"], 1)
        backend.ask("fixture")
        body = self.transport.calls[-1][2]["json"]
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)

    def test_each_extension_activates_complete_v2_defaults(self):
        # "plan" is the zcode subscription field: API-key providers never carry it.
        expected = set(api._PROFILE_FIELDS) - {"plan"}
        for field in api._V2_FIELDS:
            candidate = {**profile(), field: api.profile_defaults()["glm"][field]}
            with self.subTest(field=field):
                self.assertEqual(set(api.validate_profile(candidate)), expected)

    def test_model_namespaces_and_custom_effort_are_allowed_not_remapped(self):
        for model in ("org/model:revision", "vendor/model@v2+q4", "A" * 256):
            self.assertEqual(api.validate_profile(extended(model=model))["model"], model)
        for effort in ("minimal", "medium", "xhigh", "ultra", "custom-v3.1", "a" * 64):
            backend = self.backend(extended("deepseek", reasoning_effort=effort, thinking="default"))
            backend.ask("fixture")
            body = self.transport.calls[-1][2]["json"]
            self.assertEqual(body["reasoning_effort"], effort)
            self.assertNotIn("thinking", body)

    def test_thinking_is_independent_of_effort_on_new_profiles(self):
        for provider in ("glm", "deepseek"):
            for thinking in ("default", "enabled", "disabled"):
                for effort in ("default", "none", "max"):
                    self.backend(extended(provider, thinking=thinking, reasoning_effort=effort)).ask("fixture")
                    body = self.transport.calls[-1][2]["json"]
                    if thinking == "default":
                        self.assertNotIn("thinking", body)
                    else:
                        self.assertEqual(body["thinking"], {"type": thinking})
                    if effort == "default":
                        self.assertNotIn("reasoning_effort", body)
                    else:
                        self.assertEqual(body["reasoning_effort"], effort)

    def test_unsupported_protocol_is_explicit_before_any_io(self):
        with patch.object(api, "_store", side_effect=AssertionError("no credentials")):
            for protocol in ("responses", "anthropic_messages", "", None, []):
                candidate = extended(protocol=protocol)
                for action in (lambda: api.validate_profile(candidate),
                               lambda: api.create_backend(candidate, "qc"),
                               lambda: api.test_connection(profile=candidate),
                               lambda: api.model_choices(profile=candidate)):
                    with self.assertRaisesRegex(api.UnsupportedOperation, "^unsupported_protocol$"):
                        action()

    def test_invalid_thinking_and_confirmation_types(self):
        for field, values in {"thinking": [None, [], "auto", "off", True],
                              "endpoint_confirmed": [None, 1, 0, "true", []]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(api.ConfigError):
                    api.validate_profile(extended(**{field: value}))

    def test_urls_require_https_public_literal_hosts_without_secrets_or_suffix(self):
        bad = ["http://example.com/v1", "https://u:p@example.com/v1", "https://u@example.com/v1",
               "https://example.com/v1?key=SECRET", "https://example.com/v1?", "https://example.com/#",
               "https://example.com/%2e%2e", "https://example.com\\@evil.test/v1",
               "https://localhost/v1", "https://x.localhost/v1", "https://localhost./v1",
               "https://foo.local/v1", "https://foo.internal/v1", "https://foo.lan/v1",
               "https://127.0.0.1/v1", "https://10.0.0.1/v1", "https://169.254.169.254/v1",
               "https://172.16.0.1/v1", "https://192.168.1.1/v1", "https://100.64.0.1/v1",
               "https://[::1]/v1", "https://[fc00::1]/v1", "https://[::ffff:127.0.0.1]/v1",
               "https://2130706433/v1", "https://127.1/v1", "https://0x7f000001/v1",
               "https://example.com:0/v1", "https://example.com:65536/v1", "https://example.com:a/v1",
               "https://example.com/a/../v1", "https://example.com/./v1", "https://example.com/a b",
               "https://exa_mple.com/v1", "https://example.com/\nSECRET", None, []]
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("validation cannot use DNS")):
            for field in ("base_url", "models_url"):
                for value in bad:
                    with self.subTest(field=field, value=value), self.assertRaisesRegex(
                            api.ConfigError, "^invalid_endpoint_url$"):
                        api.validate_profile(extended(endpoint_confirmed=True, **{field: value}))
            with self.assertRaises(api.ConfigError):
                api.validate_profile(extended(base_url="https://example.com/v1/chat/completions/", endpoint_confirmed=True))

    def test_custom_endpoint_confirmation_and_normalization(self):
        for field in ("base_url", "models_url"):
            for provider in api.ENDPOINTS:
                candidate = extended(provider, **{field: "https://user-selected.example/v1"})
                with self.assertRaisesRegex(api.ConfigError, "endpoint_confirmation_required"):
                    api.validate_profile(candidate)
                candidate["endpoint_confirmed"] = True
                self.assertEqual(api.validate_profile(candidate)[field], "https://user-selected.example/v1")
        value = api.validate_profile(extended(base_url="https://OPEN.BIGMODEL.CN:443/api/paas/v4/"))
        self.assertFalse(value["endpoint_confirmed"])
        self.assertEqual(value["base_url"], api.BASE_URLS["glm"])
        self.assertEqual(api.validate_profile(extended(base_url="https://8.8.8.8/v1", endpoint_confirmed=True))["base_url"],
                         "https://8.8.8.8/v1")

    def test_extra_body_is_bounded_json_and_blocks_sensitive_or_tool_fields_recursively(self):
        valid = {"temperature": 0.5, "top_p": 0.95, "stop": ["END"],
                 "response_format": {"type": "json_object"}, "other": [True, None, 2]}
        self.assertEqual(api.validate_profile(extended(extra_body=valid))["extra_body"], valid)
        for name in ("model", "messages", "stream", "max_tokens", "reasoning_effort", "thinking",
                     "headers", "api_key", "Authorization", "extra_headers", "tools", "tool_choice",
                     "tool_calls", "functions", "function_call", "parallel_tool_calls", "tool_stream",
                     "proxy", "verify", "timeout", "api-key", "API.KEY", "extra_body"):
            for value in ({name: "SECRET"}, {"nested": [{name: "SECRET"}]}):
                with self.subTest(name=name), self.assertRaisesRegex(api.ConfigError, "^invalid_extra_body$"):
                    api.validate_profile(extended(extra_body=value))
        cyclic = {}; cyclic["a"] = cyclic
        for value in (None, [], {"a": float("nan")}, {"a": float("inf")}, {"a": 2**54},
                      {"a": {1, 2}}, {1: "bad"}, {"a": "x" * 4097}, {"a": [0] * 256},
                      {str(i): i for i in range(65)}, {"a": "汉" * 3000}, cyclic):
            with self.assertRaisesRegex(api.ConfigError, "^invalid_extra_body$"):
                api.validate_profile(extended(extra_body=value))

    def test_configuration_roundtrip_and_rotation_keep_all_extended_fields(self):
        value = config()
        value["qc"] = extended(base_url="https://user-selected.example/v1", endpoint_confirmed=True,
                               reasoning_effort="ultra", thinking="enabled", extra_body={"temperature": .3})
        value["zh"] = extended("deepseek", models_url="", thinking="disabled")
        normalized = api.save_config(value, FAKE_KEYS, key_store=self.store)
        self.assertEqual(api.load_config(key_store=self.store), normalized)
        self.assertEqual(normalized["schema"], 1)
        api.save_keys({"glm": "fixture-rotated-key"}, key_store=self.store)
        self.assertEqual(api.load_config(key_store=self.store), normalized)
        self.assertEqual(self.store.get_key("glm"), "fixture-rotated-key")
        self.assertNotIn("fixture-rotated-key", repr(api.public_config(key_store=self.store)))

    def test_cache_identity_covers_fields_and_canonical_extra_body(self):
        initial = extended(endpoint_confirmed=True, extra_body={"top_p": .95, "temperature": .5})
        baseline = self.backend(initial).cache_identity
        self.assertEqual(baseline["schema"], 2)
        self.assertEqual(baseline["extra_body"], '{"temperature":0.5,"top_p":0.95}')
        self.assertEqual(baseline, self.backend({**initial, "extra_body": {"temperature": .5, "top_p": .95}}).cache_identity)
        for changes in ({"base_url": "https://user-selected.example/v1"},
                        {"models_url": "https://user-selected.example/models"},
                        {"thinking": "disabled"}, {"extra_body": {"temperature": .4}},
                        {"endpoint_confirmed": False}, {"reasoning_effort": "ultra"},
                        {"model": "other/model:revision"}, {"timeout": 100}, {"max_tokens": 1234}):
            self.assertNotEqual(baseline, self.backend({**initial, **changes}).cache_identity)
        self.assertEqual(baseline["protocol"], api.PROTOCOL)
        self.assertNotEqual(baseline, self.backend(profile()).cache_identity)
        for secret in FAKE_KEYS.values():
            self.assertNotIn(secret, json.dumps(baseline))

    def test_nested_profile_and_payload_mutations_cannot_change_frozen_backend(self):
        source = extended(extra_body={"temperature": .4, "response_format": {"type": "json_object"}})
        backend = self.backend(source)
        original = dict(backend.cache_identity)
        source["extra_body"]["temperature"] = .9
        backend.profile["extra_body"]["response_format"]["type"] = "changed"
        backend.ask("fixture")
        self.transport.calls[-1][2]["json"]["response_format"]["type"] = "changed-again"
        backend.ask("fixture")
        self.assertEqual(self.transport.calls[-1][2]["json"]["temperature"], .4)
        self.assertEqual(self.transport.calls[-1][2]["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(backend.cache_identity, original)

    def test_import_does_no_credentials_or_network_io(self):
        spec = importlib.util.spec_from_file_location("model_api_fixture_import", api.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {spec.name: module}), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no read")), \
                patch.object(Path, "write_bytes", side_effect=AssertionError("no write")), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("no DNS")), \
                patch.object(api.requests, "Session", side_effect=AssertionError("no request")):
            spec.loader.exec_module(module)


class ExtendedRequestTests(OfflineCase):
    def test_response_model_key_canary_never_reaches_metrics_or_usage(self):
        for provider in api.ENDPOINTS:
            for model in (FAKE_KEYS[provider], "prefix-" + FAKE_KEYS[provider] + "-suffix"):
                transport = FakeTransport(api.TransportResult(200, completion(model=model)))
                logs = []
                backend = self.backend(extended(provider), transport=transport, log=logs.append)
                with self.assertRaisesRegex(api.ModelAPIError, "^credential_in_response_metadata$") as caught:
                    backend.ask("synthetic canary")
                self.assertNotIn(FAKE_KEYS[provider], repr(caught.exception))
                self.assertEqual(backend.usage_history, ())
                self.assertIsNone(backend.last_usage)
                self.assertIsNone(backend.last_response_model)
                self.assertFalse(backend.used_run)
                self.assertFalse(backend.used_paper)
                self.assertEqual(logs, [])
                self.assertEqual(len(transport.calls), 1)

    def test_canary_uses_request_key_snapshot_not_rotated_store_and_ignores_content(self):
        key = FAKE_KEYS["glm"]
        store = MagicMock()
        store.get_key.side_effect = [key, "different-rotated-fixture-key"]
        transport = FakeTransport(api.TransportResult(200, completion(model="prefix-" + key)))
        backend = self.backend(extended(), transport=transport, key_store=store)
        with self.assertRaisesRegex(api.ModelAPIError, "^credential_in_response_metadata$"):
            backend.ask("synthetic canary")
        store.get_key.assert_called_once_with("glm")
        data = completion()
        data["choices"][0]["message"]["content"] = "synthetic body: " + key
        backend = self.backend(extended(), transport=FakeTransport(api.TransportResult(200, data)))
        self.assertEqual(backend.ask("synthetic"), "synthetic body: " + key)
        self.assertNotIn(key, repr(backend.usage_history))

    def test_custom_url_uses_selected_provider_key_and_profile_fields(self):
        for provider in api.ENDPOINTS:
            candidate = extended(provider, base_url="https://user-selected.example/v1", endpoint_confirmed=True,
                                 reasoning_effort="ultra", thinking="default" if provider == "tongji" else "enabled",
                                 extra_body={"temperature": .4})
            self.backend(candidate).ask("fixture")
            method, url, args = self.transport.calls[-1]
            self.assertEqual((method, url), ("POST", "https://user-selected.example/v1/chat/completions"))
            self.assertEqual(args["headers"]["Authorization"], "Bearer " + FAKE_KEYS[provider])
            self.assertEqual(args["json"]["reasoning_effort"], "ultra")
            if provider == "tongji":
                self.assertNotIn("thinking", args["json"])
            else:
                self.assertEqual(args["json"]["thinking"], {"type": "enabled"})
            self.assertEqual(args["json"]["temperature"], .4)

    def test_probe_full_profile_and_saved_role_use_same_endpoint_and_body(self):
        candidate = extended(base_url="https://user-selected.example/v1", endpoint_confirmed=True,
                             thinking="disabled", reasoning_effort="none", extra_body={"temperature": .3})
        result = api.test_connection(profile=candidate, key_store=FAKE_KEYS, transport=self.transport,
                                     slot_root=self.root / "slots")
        first = copy.deepcopy(self.transport.calls[-1])
        self.assertTrue(result["ok"])
        self.assertEqual(first[2]["timeout"], 60)
        self.assertEqual(first[2]["json"]["max_tokens"], 1024)
        self.assertEqual(first[2]["json"]["thinking"], {"type": "disabled"})
        self.assertEqual(first[2]["json"]["reasoning_effort"], "none")
        self.assertEqual(first[2]["json"]["temperature"], .3)
        self.store.save({**config(), "qc": candidate}, FAKE_KEYS)
        api.test_connection("glm", key_store=self.store, transport=self.transport, slot_root=self.root / "slots")
        self.assertEqual(self.transport.calls[-1], first)
        self.assertEqual(candidate["max_tokens"], 131072)

    def test_probe_respects_smaller_limits_and_rejects_ambiguous_profiles(self):
        api.test_connection(profile=extended(timeout=10, max_tokens=32), key_store=FAKE_KEYS,
                            transport=self.transport, slot_root=self.root / "slots")
        self.assertEqual(self.transport.calls[0][2]["timeout"], 10)
        self.assertEqual(self.transport.calls[0][2]["json"]["max_tokens"], 32)
        with patch.object(api, "_store", side_effect=AssertionError("no read")):
            for args in ({"provider": "glm"}, {"model": "test"}, {"reasoning_effort": "high"}):
                with self.assertRaisesRegex(api.ConfigError, "ambiguous_profile"):
                    api.test_connection(profile=extended(), **args)

    def test_custom_transport_no_redirect_proxy_tls_bypass_or_other_destination(self):
        candidate = extended(base_url="https://user-selected.example/v1", endpoint_confirmed=True)
        session, response = MagicMock(), MagicMock()
        session.__enter__.return_value = session
        session.request.return_value.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [b'{"data":[]}']
        transport = api.RequestsTransport(profile=candidate)
        with patch.object(api.requests, "Session", return_value=session):
            transport.request("POST", candidate["base_url"] + "/chat/completions", headers={}, json={}, timeout=30)
            for method, url in (("GET", candidate["base_url"] + "/models"),
                                ("POST", api.ENDPOINTS["glm"]), ("POST", "https://other.example/v1/chat/completions")):
                with self.assertRaisesRegex(api.ModelAPIError, "endpoint_not_allowed"):
                    transport.request(method, url, headers={}, json={}, timeout=30)
        self.assertFalse(session.trust_env)
        self.assertIs(session.verify, True)
        kwargs = session.request.call_args.kwargs
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["allow_redirects"], False)
        adapter = session.mount.call_args.args[1]
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertIs(adapter.poolmanager.pool_classes_by_scheme["https"], api._PublicHTTPSPool)
        self.assertEqual(session.request.call_count, 1)

    def test_dns_checks_all_answers_before_connect_and_pins_public_address(self):
        connection = api._PublicHTTPSConnection("user-selected.example", port=443, timeout=5)
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        sock = MagicMock()
        with patch.object(socket, "getaddrinfo", return_value=[public]) as dns, \
                patch.object(socket, "socket", return_value=sock):
            self.assertIs(connection._new_conn(), sock)
        dns.assert_called_once_with("user-selected.example", 443, type=socket.SOCK_STREAM)
        sock.connect.assert_called_once_with(("8.8.8.8", 443))
        self.assertEqual(connection.host, "user-selected.example")
        for address in ("127.0.0.1", "10.0.0.1", "::1", "::ffff:192.168.1.2", "224.0.0.1"):
            private = (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            with patch.object(socket, "getaddrinfo", return_value=[public, private]), \
                    patch.object(socket, "socket") as create, \
                    self.assertRaisesRegex(api.ModelAPIError, "^endpoint_not_allowed$"):
                connection._new_conn()
            create.assert_not_called()

    def test_dns_and_socket_failures_are_fixed_safe_errors(self):
        connection = api._PublicHTTPSConnection("user-selected.example", port=443, timeout=5)
        with patch.object(socket, "getaddrinfo", side_effect=OSError("SECRET DNS")), \
                self.assertRaisesRegex(api.ModelAPIError, "^endpoint_resolution_failed$"):
            connection._new_conn()
        for error, code in ((OSError("SECRET socket"), "transport_error"),
                            (TimeoutError("SECRET timeout"), "request_timeout")):
            sock = MagicMock(); sock.connect.side_effect = error
            with patch.object(socket, "getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), \
                    patch.object(socket, "socket", return_value=sock), self.assertRaisesRegex(api.ModelAPIError, "^" + code + "$"):
                connection._new_conn()
            sock.close.assert_called_once()

    def test_custom_requests_keep_bounded_retries_and_reject_truncation_without_fallback(self):
        candidate = extended(base_url="https://user-selected.example/v1", endpoint_confirmed=True)
        transport = FakeTransport(api.TransportResult(429), api.TransportResult(503), api.TransportResult(200, completion()))
        sleeps = []
        self.backend(candidate, transport=transport, sleep=sleeps.append).ask("fixture")
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(transport.calls, [transport.calls[0]] * 3)
        truncated = completion()
        truncated["choices"][0]["finish_reason"] = "length"
        transport = FakeTransport(api.TransportResult(200, truncated))
        backend = self.backend(candidate, transport=transport)
        for _ in range(2):
            with self.assertRaisesRegex(api.ModelAPIError, "incomplete_completion"):
                backend.ask("fixture")
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(backend.used_run)


class ModelDetailsTests(OfflineCase):
    def test_model_id_and_effort_key_canaries_fail_without_partial_results(self):
        key = FAKE_KEYS["deepseek"]
        for reflected in (key, "prefix-" + key + "-suffix"):
            entries = [
                {"id": reflected},
                {"id": "safe-model", "name": reflected},
                {"id": "safe-model", "effort": {"supported_levels": ["high", reflected]}},
                {"id": "safe-model", "effort": {"supported_levels": ["high"], "default_level": reflected}},
            ]
            for entry in entries:
                for function in (api.model_choices, api.list_models):
                    transport = FakeTransport(api.TransportResult(200, {"data": [{"id": "first-safe"}, entry]}))
                    with self.assertRaisesRegex(api.ModelAPIError, "^credential_in_response_metadata$") as caught:
                        function(profile=extended("deepseek", model=""), key_store=FAKE_KEYS, transport=transport,
                                 slot_root=self.root / "slots")
                    self.assertNotIn(key, repr(caught.exception))
                    self.assertEqual(len(transport.calls), 1)

    def choices(self, entries, **kwargs):
        transport = FakeTransport(api.TransportResult(200, {"data": entries}))
        result = api.model_choices(profile=extended("deepseek", model=""), key_store=FAKE_KEYS,
                                   transport=transport, slot_root=self.root / "slots", **kwargs)
        self.assertEqual(len(transport.calls), 1)
        return result

    def test_deepseek_details_same_request_whitelist_and_legacy_list_return(self):
        entries = [{"id": "org/model:next", "effort": {"supported_levels": ["low", "high", "max"],
                    "default_level": "high", "secret": "PRIVATE"}, "max_output_tokens": 32768,
                    "api_capabilities": {"secret": "PRIVATE"}, "headers": "PRIVATE"}]
        result = self.choices(entries)
        self.assertEqual(result["source"], "provider_api")
        self.assertEqual(result["models"], ["org/model:next"])
        self.assertEqual(result["details"], {"org/model:next": {
            "effort_levels": ["low", "high", "max"], "default_effort": "high",
            "highest_effort": "max", "max_output_tokens": 32768}})
        self.assertNotIn("PRIVATE", repr(result))
        transport = FakeTransport(api.TransportResult(200, {"data": entries}))
        self.assertEqual(api.list_models("deepseek", key_store=FAKE_KEYS, transport=transport,
                                         slot_root=self.root / "slots"), ["org/model:next"])
        self.assertEqual(len(transport.calls), 1)

    def test_details_effort_aliases_are_kept_verbatim(self):
        levels = ["minimal", "medium", "xhigh", "ultra", "custom-v2"]
        result = self.choices([{"id": "fixture", "effort": {"supported_levels": levels, "default_level": "xhigh"}}])
        self.assertEqual(result["details"]["fixture"], {"effort_levels": levels, "default_effort": "xhigh"})

    def test_optional_metadata_is_bounded_and_invalid_fields_are_omitted(self):
        for levels in (None, "high", [], ["bad word"], ["x" * 65], [None], ["high"] * 17):
            result = self.choices([{"id": "fixture", "effort": {"supported_levels": levels, "default_level": "high"}}])
            self.assertNotIn("details", result)
        for maximum in (None, True, -1, 0, 1.5, "123", 2**31):
            result = self.choices([{"id": "fixture", "max_output_tokens": maximum}])
            self.assertNotIn("details", result)
        result = self.choices([{"id": "fixture", "effort": {"supported_levels": ["high", "high"],
                                                              "default_level": "PRIVATE BAD"}, "max_output_tokens": 1}])
        self.assertEqual(result["details"]["fixture"], {"effort_levels": ["high"], "highest_effort": "high",
                                                        "max_output_tokens": 1})
        self.assertNotIn("PRIVATE", repr(result))
        levels = [f"level-{i}" for i in range(16)]
        self.assertEqual(len(self.choices([{"id": "fixture", "effort": {"supported_levels": levels}}])[
            "details"]["fixture"]["effort_levels"]), 16)

    def test_model_count_bound_and_safe_model_id_validation(self):
        self.assertEqual(len(self.choices([{"id": f"model-{i}"} for i in range(512)])["models"]), 512)
        for entries in ([{"id": f"model-{i}"} for i in range(513)], [{"id": "x" * 257}],
                        [{"id": "https://SECRET.test"}], [{"id": "has space"}]):
            with self.assertRaisesRegex(api.ModelAPIError, "^invalid_model_list$"):
                self.choices(entries)

    def test_glm_catalog_exact_ids_and_only_verified_per_model_effort(self):
        expected = ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal",
                    "glm-5.2", "glm-5.1", "glm-5-turbo", "glm-5", "glm-4.7", "glm-4.7-flash",
                    "glm-4.7-flashx", "glm-4.6", "glm-4.5-air", "glm-4.5-airx", "glm-4.5-flash",
                    "glm-4-flash-250414", "glm-4-flashx-250414"]
        with patch.object(api, "_store", side_effect=AssertionError("no credentials")), \
                patch.object(api, "_request", side_effect=AssertionError("no HTTP")), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("no DNS")):
            for blank in ("", "   ", "\t"):
                # The general GLM endpoint keeps its historical bundled catalog;
                # new drafts default to the Coding Plan endpoint instead.
                general = extended(model=blank, base_url=api.BASE_URLS["glm"])
                result = api.model_choices(profile=general)
                self.assertEqual(result["models"], expected)
                self.assertEqual(result["source"], "bundled_catalog")
                self.assertIn("非账户可用清单", result["notice"])
                self.assertIn("thinking 开启", result["notice"])
                self.assertEqual(result["details"], {**{name: {"effort_levels": ["low", "high", "max"],
                    "default_effort": "max", "highest_effort": "max", "max_output_tokens": 131072,
                    "context_window": 1048576}
                    for name in ("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal")},
                    "glm-5.2": {"effort_levels": ["max", "xhigh", "high", "medium", "low", "minimal", "none"],
                                "default_effort": "max", "highest_effort": "max", "max_output_tokens": 131072}})
                self.assertNotIn("glm-5.1", result["details"])
            result["details"]["glm-5.3"]["effort_levels"].append("changed")
            self.assertEqual(api.model_choices("glm")["details"]["glm-5.3"]["effort_levels"], ["low", "high", "max"])

    def test_models_url_custom_and_empty_never_guesses_or_drops_profile(self):
        candidate = extended("glm", model="", models_url="https://user-selected.example/catalog", endpoint_confirmed=True,
                             base_url="https://user-selected.example/v1", timeout=12, thinking="enabled",
                             extra_body={"temperature": .1})
        transport = FakeTransport(api.TransportResult(200, {"data": [{"id": "org/fixture:1"}]}))
        result = api.model_choices(profile=candidate, key_store=FAKE_KEYS, transport=transport, slot_root=self.root / "slots")
        self.assertEqual(result["source"], "provider_api")
        self.assertEqual(transport.calls[0][:2], ("GET", candidate["models_url"]))
        self.assertEqual(transport.calls[0][2]["timeout"], 12)
        self.assertIsNone(transport.calls[0][2]["json"])
        self.assertEqual(transport.calls[0][2]["headers"]["Authorization"], "Bearer " + FAKE_KEYS["glm"])
        with patch.object(api, "_store", side_effect=AssertionError("no credentials")):
            result = api.model_choices(profile=extended("deepseek", model="", models_url=""))
            self.assertEqual(result["models"], [])
            self.assertEqual(result["source"], "bundled_catalog")
            self.assertIn("未配置", result["notice"])

    def test_custom_listing_failures_do_not_fallback_and_protocol_is_still_validated(self):
        candidate = extended(model="", models_url="https://user-selected.example/catalog", endpoint_confirmed=True)
        transport = FakeTransport(api.TransportResult(401, "SECRET"))
        with self.assertRaises(api.ModelAPIError) as caught:
            api.model_choices(profile=candidate, key_store=FAKE_KEYS, transport=transport, slot_root=self.root / "slots")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)
        with self.assertRaisesRegex(api.UnsupportedOperation, "unsupported_protocol"):
            api.model_choices(profile=extended(model="", protocol="responses"))
        with self.assertRaisesRegex(api.ConfigError, "ambiguous_profile"):
            api.model_choices("glm", profile=extended(model=""))


class TongjiTests(OfflineCase):
    def test_preflight_requires_exact_campus_id_only_for_selected_roles(self):
        profiles = {role: extended("tongji", model="GLM-5.3") for role in ("qc", "zh")}
        with patch.object(api, "list_models", return_value=["GLM-5.3"]) as listing:
            api.check_tongji_model_ids(profiles, ("qc", "zh"))
            listing.assert_called_once()
            profiles["zh"]["model"] = "glm-5.3"
            api.check_tongji_model_ids(profiles, ("qc",))
            with self.assertRaisesRegex(api.ConfigError, "翻译.*大小写.*GLM-5.3"):
                api.check_tongji_model_ids(profiles, ("qc", "zh"))
            profiles["zh"]["model"] = "missing"
            with self.assertRaisesRegex(api.ConfigError, "找不到所选翻译模型"):
                api.check_tongji_model_ids(profiles, ("zh",))
            listing.reset_mock()
            profiles["qc"] = extended("glm")
            profiles["zh"] = extended("tongji", base_url="https://alternate.example/v1",
                                       endpoint_confirmed=True)
            api.check_tongji_model_ids(profiles, ("qc", "zh"))
            listing.assert_not_called()

    def test_key_is_independent_and_existing_config_is_preserved(self):
        old_keys = {name: FAKE_KEYS[name] for name in ("glm", "deepseek")}
        api.save_config(config(), old_keys, key_store=self.store)
        before = api.load_config(key_store=self.store)
        api.save_keys({"tongji": FAKE_KEYS["tongji"]}, key_store=self.store)
        self.assertEqual(api.load_config(key_store=self.store), before)
        self.assertEqual(self.store.load()["keys"], FAKE_KEYS)
        self.assertTrue(api.public_config(key_store=self.store)["credentials"]["tongji"])
        self.assertNotIn(FAKE_KEYS["tongji"], repr(api.public_config(key_store=self.store)))
        self.assertEqual(api.reveal_credential("tongji", key_store=self.store)["key"], FAKE_KEYS["tongji"])
        selected = {**before, "qc": extended("tongji", model="DeepSeek-V4.1-Flash")}
        api.save_config(selected, key_store=self.store)
        self.assertEqual(api.load_config(key_store=self.store)["zh"], before["zh"])
        self.assertEqual(self.store.load()["keys"], FAKE_KEYS)

    def test_exact_model_and_reasoning_effort_are_sent_without_thinking_aliases(self):
        response = completion(model="DeepSeek-V4.1-Flash")
        response["choices"][0]["message"]["reasoning"] = "PRIVATE-CAMPUS-THINKING"
        for effort in ("default", "none", "low", "high", "xhigh", "max"):
            transport = FakeTransport(api.TransportResult(200, response))
            backend = self.backend(extended("tongji", model="DeepSeek-V4.1-Flash", reasoning_effort=effort),
                                   transport=transport)
            self.assertEqual(backend.ask("synthetic"), "Synthetic answer")
            method, url, args = transport.calls[0]
            self.assertEqual((method, url), ("POST", "https://agi.tongji.edu.cn/v1/chat/completions"))
            self.assertEqual(args["headers"]["Authorization"], "Bearer " + FAKE_KEYS["tongji"])
            body = args["json"]
            self.assertEqual(body["model"], "DeepSeek-V4.1-Flash")
            self.assertEqual(body["max_tokens"], 32768)
            self.assertIs(body["stream"], False)
            self.assertEqual(body.get("reasoning_effort"), None if effort == "default" else effort)
            for name in ("thinking", "enable_thinking", "chat_template_kwargs"):
                self.assertNotIn(name, body)
            self.assertEqual(backend.used_paper, {"tongji/DeepSeek-V4.1-Flash"})
            self.assertNotIn("PRIVATE", repr(backend.usage_history))
            self.assertNotIn("Synthetic answer", repr(backend.usage_history))

    def test_incompatible_thinking_fields_rejected_before_http(self):
        for patch_value in ({"thinking": "enabled"}, {"thinking": "disabled"},
                            {"extra_body": {"enable_thinking": True}},
                            {"extra_body": {"nested": [{"chat_template_kwargs": {"enable_thinking": False}}]}},
                            {"extra_body": {"Enable.Thinking": False}}):
            with self.subTest(value=patch_value), self.assertRaisesRegex(api.ConfigError, "tongji_use_reasoning_effort"):
                self.backend(extended("tongji", **patch_value))
        self.assertEqual(self.transport.calls, [])

    def test_campus_listing_is_live_and_never_inherits_vendor_capabilities(self):
        entries = [{"id": "DeepSeek-V4.1-Flash"}, {"id": "Qwen3.5-Embedding"}, {"id": "deepseek-flash"}]
        transport = FakeTransport(api.TransportResult(200, {"data": entries}))
        result = api.model_choices("tongji", transport=transport, key_store=FAKE_KEYS, slot_root=self.root / "slots")
        self.assertEqual(transport.calls[0][:2], ("GET", "https://agi.tongji.edu.cn/v1/models"))
        self.assertEqual(result["models"], [entry["id"] for entry in entries])
        self.assertEqual(result["source"], "provider_api")
        self.assertEqual(result["endpoint_kind"], "tongji")
        self.assertIn("非对话", result["notice"])
        for model in result["models"] + ["glm-5.3"]:
            self.assertEqual(api.highest_parameters(extended("tongji", model=model), result), {})
        details = {**result, "details": {"DeepSeek-V4.1-Flash": {
            "max_output_tokens": 4567, "effort_levels": ["none", "high"], "default_effort": "none"}}}
        self.assertEqual(api.highest_parameters(extended("tongji", model="DeepSeek-V4.1-Flash"), details),
                         {"max_tokens": 4567, "reasoning_effort": "high"})

    def test_campus_failures_and_missing_key_never_use_other_providers(self):
        for method in ("completion", "models"):
            transport = FakeTransport(api.TransportResult(401, "SECRET-CAMPUS-ERROR"))
            with self.assertRaises(api.ModelAPIError) as caught:
                if method == "completion":
                    self.backend(extended("tongji"), transport=transport).ask("synthetic")
                else:
                    api.model_choices("tongji", transport=transport, key_store=FAKE_KEYS, slot_root=self.root / "slots")
            self.assertNotIn("SECRET", repr(caught.exception))
            self.assertEqual(len(transport.calls), 1)
            self.assertIn("agi.tongji.edu.cn", transport.calls[0][1])
        missing = {name: FAKE_KEYS[name] for name in ("glm", "deepseek")}
        with self.assertRaisesRegex(api.ConfigError, "credential"):
            self.backend(extended("tongji"), key_store=missing).ask("synthetic")
        self.assertEqual(self.transport.calls, [])

    def test_campus_private_exception_is_exact_and_dns_is_pinned(self):
        for host in api.TONGJI_HOSTS:
            connection = api._TongjiHTTPSConnection(host, port=443, timeout=5)
            answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", 443))
            sock = MagicMock()
            with patch.object(socket, "getaddrinfo", return_value=[answer]) as dns, \
                    patch.object(socket, "socket", return_value=sock):
                self.assertIs(connection._new_conn(), sock)
            dns.assert_called_once_with(host, 443, type=socket.SOCK_STREAM)
            sock.connect.assert_called_once_with(("10.20.30.40", 443))
            self.assertEqual(connection.host, host)
            for address in ("127.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1", "::ffff:10.0.0.1"):
                denied = (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                with patch.object(socket, "getaddrinfo", return_value=[answer, denied]), \
                        patch.object(socket, "socket") as create, self.assertRaisesRegex(api.ModelAPIError, "endpoint_not_allowed"):
                    connection._new_conn()
                create.assert_not_called()
        for host, port in (("other.example", 443), ("agi.tongji.edu.cn.evil.example", 443), ("agi.tongji.edu.cn", 444)):
            self.assertFalse(api._TongjiHTTPSConnection(host, port=port)._address_allowed("10.1.2.3"))
        for provider, url, expected in (("tongji", api.ENDPOINTS["tongji"], True),
                                        ("glm", api.ENDPOINTS["tongji"], False),
                                        ("tongji", "https://agi.tongji.edu.cn/admin", False),
                                        ("tongji", "https://agi.tongji.edu.cn:444/v1/models", False),
                                        ("tongji", "https://llmapi.tongji.edu.cn/v1/models", True)):
            self.assertEqual(api._tongji_campus_url(provider, url), expected)
        with self.assertRaisesRegex(api.ConfigError, "endpoint_confirmation_required"):
            api.validate_profile(extended("tongji", base_url="https://llmapi.tongji.edu.cn/v1"))

    def test_transport_keeps_tls_no_redirect_and_private_exception_only_for_campus(self):
        for candidate, expected in ((extended("tongji"), api._TongjiHTTPSPool),
                                     (extended("tongji", base_url="https://other.example/v1", endpoint_confirmed=True), api._PublicHTTPSPool),
                                     (extended("glm", base_url="https://agi.tongji.edu.cn/v1", endpoint_confirmed=True), api._PublicHTTPSPool)):
            session, response = MagicMock(), MagicMock()
            session.__enter__.return_value = session
            session.request.return_value.__enter__.return_value = response
            response.status_code = 200
            response.iter_content.return_value = [b'{"data":[]}']
            transport = api.RequestsTransport(profile=candidate)
            with patch.object(api.requests, "Session", return_value=session):
                transport.request("POST", candidate["base_url"] + "/chat/completions", headers={}, json={}, timeout=30)
            adapter = session.mount.call_args.args[1]
            self.assertIs(adapter.poolmanager.pool_classes_by_scheme["https"], expected)
            self.assertFalse(session.trust_env)
            self.assertIs(session.request.call_args.kwargs["verify"], True)
            self.assertIs(session.request.call_args.kwargs["allow_redirects"], False)
            self.assertEqual(adapter.max_retries.total, 0)


if __name__ == "__main__":
    unittest.main()
