"""Offline tests: synthetic keys, temporary stores/locks, forbidden sockets.

Run: py -3 -B -m unittest discover -s tools -p test_model_api.py -v
Never run a probe against a live API as part of this test suite.
"""
from __future__ import annotations

import copy
import io
import json
import multiprocessing
import os
import socket
import tempfile
import threading
import time
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import model_api as api

FAKE_KEYS = {"glm": "fakekey.glm.synthetic-only", "deepseek": "fakekey-deepseek-synthetic-only",
             "tongji": "fakekey-tongji-synthetic-only"}


def profile(provider="glm", **changes):
    value = {"provider": provider, "model": provider + "-test-next"}
    value.update(changes)
    return value


def config():
    return {"schema": 1, "qc": profile(), "zh": profile("deepseek")}


def completion(**changes):
    value = {"model": "server-test-revision", "choices": [{"finish_reason": "stop", "index": 0,
             "message": {"role": "assistant", "content": "Synthetic answer",
                         "reasoning_content": "PRIVATE-THINKING"}}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16,
                       "completion_tokens_details": {"reasoning_tokens": 3}}}
    value.update(changes)
    return value


class FakeTransport:
    def __init__(self, *results):
        self.results = list(results) or [api.TransportResult(200, completion())]
        self.calls = []
        self.lock = threading.Lock()

    def request(self, method, url, **kwargs):
        with self.lock:
            self.calls.append((method, url, kwargs))
            result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def fake_crypto(value, *, decrypt=False):
    # A deterministic TEST codec, not encryption. Never the production default.
    if decrypt:
        if not value.startswith(b"TEST:"):
            raise ValueError("synthetic corrupt document")
        return bytes(c ^ 0xAA for c in value[5:])
    return b"TEST:" + bytes(c ^ 0xAA for c in value)


def save_worker(path, provider, start):
    socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("offline"))
    start.wait(10)
    api.save_config(config(), {provider: FAKE_KEYS[provider]},
                    key_store=api.DPAPIConfigStore(path, crypto=fake_crypto))


def slot_worker(root, active, peak, start):
    socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("offline"))

    class Transport:
        def request(self, *args, **kwargs):
            with active.get_lock():
                active.value += 1
                peak.value = max(peak.value, active.value)
            try:
                time.sleep(0.18)
                return api.TransportResult(200, completion())
            finally:
                with active.get_lock():
                    active.value -= 1

    start.wait(10)
    api.APIBackend(profile(), "qc", transport=Transport(), key_store=FAKE_KEYS,
                   slot_root=Path(root)).ask("synthetic")


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="model-api-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.socket.bind", "socket.socket.listen"):
            guard = patch(target, side_effect=AssertionError("network forbidden in offline tests"))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.object(api, "DEFAULT_CONFIG", self.root / "unused-default.dpapi")
        guard.start()
        self.addCleanup(guard.stop)
        # Model discovery now refreshes anonymous public docs by default. Keep
        # every legacy test fully offline with a fresh, explicitly failed fetch.
        guard = patch.object(api.model_catalog, "fetch_official_markdown",
                             side_effect=api.model_catalog.CatalogError("offline_fixture"))
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch.object(api.model_catalog, "DEFAULT_CATALOG", api.model_catalog.OfficialModelCatalog())
        guard.start()
        self.addCleanup(guard.stop)
        # The local zcode login exists only where the real CLI adapter ships.
        # Default every legacy test to the export-stub behaviour; dedicated
        # ZcodeProviderTests re-enable it explicitly.
        guard = patch.object(api, "zcode_available", return_value=False)
        guard.start()
        self.addCleanup(guard.stop)
        self.store = api.DPAPIConfigStore(self.root / "config.dpapi", crypto=fake_crypto)
        self.transport = FakeTransport()

    def backend(self, value=None, **kwargs):
        options = {"transport": self.transport, "key_store": FAKE_KEYS, "slot_root": self.root / "slots"}
        options.update(kwargs)
        return api.APIBackend(profile() if value is None else value, "qc", **options)

    def probe(self, provider="glm", **kwargs):
        options = {"model": "test-new-model", "transport": self.transport,
                   "key_store": FAKE_KEYS, "slot_root": self.root / "slots"}
        options.update(kwargs)
        return api.test_connection(provider, **options)


class ConfigTests(OfflineCase):
    def test_keys_before_models_and_blank_keys_preserve(self):
        api.save_keys({"glm": FAKE_KEYS["glm"]}, key_store=self.store)
        self.assertIsNone(api.load_config(key_store=self.store))
        public = api.public_config(key_store=self.store)
        self.assertFalse(public["configured"])
        self.assertTrue(public["credentials"]["glm"])
        self.assertNotIn(FAKE_KEYS["glm"], repr(public))
        normalized = api.save_config(config(), key_store=self.store)
        api.save_keys({"glm": "", "deepseek": FAKE_KEYS["deepseek"]}, key_store=self.store)
        self.assertEqual(api.load_config(key_store=self.store), normalized)
        self.assertEqual(self.store.get_key("glm"), FAKE_KEYS["glm"])
        self.assertTrue(api.public_config(key_store=self.store)["configured"])

    def test_source_only_credentials_require_qc_but_never_skip_qc(self):
        api.validate_config(config(), True, key_store={"glm": FAKE_KEYS["glm"]}, required_roles=("qc",))
        with self.assertRaises(api.ConfigError):
            api.validate_config(config(), True, key_store={"glm": FAKE_KEYS["glm"]})
        with self.assertRaises(api.ConfigError):
            api.validate_config(config(), True, key_store={}, required_roles=("qc",))
        with self.assertRaises(api.ConfigError):
            api.validate_config(config(), True, key_store={}, required_roles=())

    def test_defaults_and_pure_validation(self):
        with patch.object(api.DPAPIConfigStore, "load", side_effect=AssertionError("no read")):
            value = api.validate_config(config())
        self.assertEqual(value["qc"], {**profile(), "reasoning_effort": "default",
                                      "timeout": 1500, "max_tokens": 32768})
        self.assertNotIn("keys", value)

    def test_missing_config_is_none(self):
        self.assertIsNone(api.load_config(key_store=self.store))
        self.assertEqual(api.public_config(key_store=self.store), {
            "configured": False, "config": None, "credentials": {"glm": False, "deepseek": False, "tongji": False}})
        self.assertFalse(self.store.path.exists())

    def test_unknown_top_and_profile_fields_rejected_without_echo(self):
        for location in ("top", "qc", "zh"):
            for name in ("api_key", "base_url", "endpoint", "thinking", "headers", "SECRET-FIELD"):
                value = config()
                (value if location == "top" else value[location])[name] = "SECRET-VALUE"
                with self.subTest(location=location, name=name):
                    with self.assertRaises(api.ConfigError) as caught:
                        api.validate_config(value)
                    self.assertNotIn("SECRET", str(caught.exception))

    def test_missing_fields_schema_types(self):
        values = [None, [], {}, {"schema": 1, "qc": profile()}]
        values += [{**config(), "schema": s} for s in (True, 0, 2, "1", 1.0)]
        for value in values:
            with self.subTest(value=value), self.assertRaises(api.ConfigError):
                api.validate_config(value)

    def test_profile_validation_and_bounds(self):
        bad = {"provider": [None, [], "zcode", "bigmodel", "GLM", "https://evil.test"],
               "model": [None, "", "x\ny", "x y", "../x", "https://evil.test", "a" * 257],
               "reasoning_effort": [[], None, "two words", "x" * 65, ""],
               "timeout": [0, -1, True, 1501, float("inf"), float("nan"), "60"],
               "max_tokens": [0, -1, True, 131073, 1.5]}
        for field, values in bad.items():
            for value in values:
                candidate = config()
                candidate["qc"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(api.ConfigError):
                    api.validate_config(candidate)

    def test_new_model_ids_not_allowlisted(self):
        candidate = config()
        candidate["qc"]["model"] = "Future.Model-2099_preview"
        candidate["zh"]["max_tokens"] = 393216
        self.assertEqual(api.validate_config(candidate)["qc"]["model"], "Future.Model-2099_preview")

    def test_require_credentials_is_opt_in(self):
        self.assertEqual(api.validate_config(config(), True, key_store=FAKE_KEYS)["schema"], 1)
        with self.assertRaisesRegex(api.ConfigError, "credential_missing"):
            api.validate_config(config(), True, key_store={"glm": FAKE_KEYS["glm"]})

    def test_save_load_and_public_state(self):
        normalized = api.save_config(config(), FAKE_KEYS, key_store=self.store)
        self.assertEqual(api.load_config(key_store=self.store), normalized)
        self.assertEqual(api.public_config(key_store=self.store), {
            "configured": True, "config": normalized, "credentials": {"glm": True, "deepseek": True, "tongji": True}})
        for key in FAKE_KEYS.values():
            self.assertNotIn(key.encode(), self.store.path.read_bytes())
            self.assertNotIn(key, repr(api.public_config(key_store=self.store)))
        normalized["qc"]["model"] = "changed-only-in-caller"
        self.assertNotEqual(api.load_config(key_store=self.store)["qc"]["model"], normalized["qc"]["model"])

    def test_partial_credentials_not_ready(self):
        api.save_config(config(), {"glm": FAKE_KEYS["glm"]}, key_store=self.store)
        self.assertFalse(api.public_config(key_store=self.store)["configured"])
        self.assertIsNotNone(api.load_config(key_store=self.store))

    def test_empty_keys_preserve_and_one_provider_rotation(self):
        self.store.save(config(), FAKE_KEYS)
        for keys in (None, {}, {"glm": ""}, {"glm": None, "deepseek": ""}):
            api.save_config(config(), keys, key_store=self.store)
            self.assertEqual(self.store.load()["keys"], FAKE_KEYS)
        self.store.save(config(), {"glm": "new.synthetic.key"})
        self.assertEqual(self.store.load()["keys"], {**FAKE_KEYS, "glm": "new.synthetic.key"})

    def test_invalid_keys_never_write_or_echo(self):
        for keys in ([], {"unknown": "SECRET"}, {"glm": "SECRET\r\nheader: x"},
                     {"glm": " space "}, {"glm": "密钥"}, {"glm": 12}, {"glm": "x" * 4097}):
            with self.subTest(keys=type(keys)), self.assertRaises(api.ConfigError) as caught:
                self.store.save(config(), keys)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertFalse(self.store.path.exists())

    def test_corrupt_ciphertext_is_not_unconfigured_or_overwritten(self):
        self.store.path.write_bytes(b"broken-synthetic-document")
        for action in (lambda: api.load_config(key_store=self.store),
                       lambda: api.public_config(key_store=self.store),
                       lambda: self.store.save(config(), FAKE_KEYS)):
            with self.assertRaisesRegex(api.ConfigError, "config_unprotect_failed"):
                action()
        self.assertEqual(self.store.path.read_bytes(), b"broken-synthetic-document")

    def test_unknown_envelope_fields_rejected(self):
        for extra in ({"unexpected": True}, {"schema": True}):
            document = {"schema": 1, "config": config(), "keys": FAKE_KEYS, **extra}
            self.store.path.write_bytes(fake_crypto(json.dumps(document).encode()))
            with self.assertRaises(api.ConfigError):
                self.store.load()

    def test_failed_encryption_leaves_old_document(self):
        self.store.save(config(), FAKE_KEYS)
        before = self.store.path.read_bytes()
        with patch.object(self.store, "_crypto", side_effect=RuntimeError("SECRET")):
            with self.assertRaises(api.ConfigError) as caught:
                self.store.save(config(), FAKE_KEYS)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_atomic_replace_failure_keeps_old_ciphertext_and_cleans_temp(self):
        self.store.save(config(), FAKE_KEYS)
        before = self.store.path.read_bytes()
        with patch("zotero_local.os.replace", side_effect=OSError("SECRET")):
            with self.assertRaisesRegex(api.ConfigError, "config_save_failed"):
                self.store.save(config(), {"glm": "rotated.synthetic"})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"config.dpapi", "config.dpapi.lock"})

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI")
    def test_real_windows_dpapi_roundtrip_synthetic_keys_only(self):
        store = api.DPAPIConfigStore(self.root / "real-dpapi-synthetic.dpapi")
        store.save(config(), FAKE_KEYS)
        self.assertEqual(store.load()["keys"], FAKE_KEYS)
        self.assertEqual(api.load_config(key_store=store), api.validate_config(config()))
        ciphertext = store.path.read_bytes()
        for forbidden in (*FAKE_KEYS.values(), "provider", "model", "schema"):
            self.assertNotIn(forbidden.encode(), ciphertext)
        store.save(config(), {})
        self.assertEqual(store.load()["keys"], FAKE_KEYS)

    def test_concurrent_process_saves_merge_keys(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        processes = [ctx.Process(target=save_worker, args=(str(self.store.path), p, start))
                     for p in FAKE_KEYS]
        for process in processes:
            process.start()
        start.set()
        try:
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(self.store.load()["keys"], FAKE_KEYS)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)


class RevealTests(OfflineCase):
    def setUp(self):
        super().setUp()
        import mineru_credentials as mineru
        self.mineru = mineru
        self.mineru_path = self.root / ".secrets" / "mineru_api.dpapi"
        self.environ = {}
        # Never inspect/copy the process's actual token environment or store.
        for guard in (patch.object(os, "environ", self.environ),
                      patch.object(mineru, "DEFAULT_PATH", self.mineru_path),
                      patch.object(mineru, "_dpapi", side_effect=fake_crypto)):
            guard.start()
            self.addCleanup(guard.stop)

    def assert_safe_failure(self, callback, code="credential_unavailable"):
        try:
            callback()
        except api.ConfigError as error:
            self.assertEqual(error.code, code)
            rendered = str(error) + repr(error) + "".join(traceback.format_exception(error))
            for key in FAKE_KEYS.values():
                self.assertNotIn(key, rendered)
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            return
        self.fail("Expected fixed safe reveal failure")

    def test_model_keys_reveal_only_explicitly_and_never_write_or_log(self):
        self.store.save_keys(FAKE_KEYS)
        before = self.store.path.read_bytes()
        modified = self.store.path.stat().st_mtime_ns
        output = io.StringIO()
        with patch.object(self.store, "_atomic_write", side_effect=AssertionError("no write")), \
                patch.object(api.FileLock, "acquire", side_effect=AssertionError("no file lock")), \
                patch.object(api, "_request", side_effect=AssertionError("no network")), \
                redirect_stdout(output), redirect_stderr(output):
            for provider, key in FAKE_KEYS.items():
                self.assertEqual(api.reveal_credential(provider, key_store=self.store),
                                 {"provider": provider, "key": key, "source": "encrypted"})
                self.assertNotIn(key, repr(api.public_config(key_store=self.store)))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.path.stat().st_mtime_ns, modified)

    def test_reveal_uses_saved_dpapi_path_and_does_not_cache_rotations(self):
        self.store.save_keys(FAKE_KEYS)
        with patch.object(api, "_dpapi", side_effect=fake_crypto):
            first = api.reveal_credential("glm", path=self.store.path)
            self.store.save_keys({"glm": "rotated.synthetic.fixture"})
            second = api.reveal_credential("glm", path=self.store.path)
        self.assertEqual(first["key"], FAKE_KEYS["glm"])
        self.assertEqual(second["key"], "rotated.synthetic.fixture")

    def test_missing_key_does_not_create_store(self):
        self.assert_safe_failure(lambda: api.reveal_credential("glm", key_store=self.store),
                                 "credential_missing")
        self.assertFalse(self.store.path.exists())
        self.assertFalse(self.store.path.with_suffix(".dpapi.lock").exists())

    def test_reveal_resanitizes_backend_and_forged_api_errors(self):
        for error in (RuntimeError(FAKE_KEYS["glm"]), OSError(FAKE_KEYS["glm"]),
                      api.ConfigError(FAKE_KEYS["glm"]), ValueError(FAKE_KEYS["glm"])):
            with patch.object(self.store, "get_key", side_effect=error):
                self.assert_safe_failure(lambda: api.reveal_credential("glm", key_store=self.store))
        for value in (FAKE_KEYS["glm"] + "\n", 123, {}, "x" * 4097):
            self.assert_safe_failure(lambda: api.reveal_credential("glm", key_store={"glm": value}))

    def test_unknown_provider_is_rejected_before_any_store_access(self):
        with patch.object(api, "_store", side_effect=AssertionError("no store read")):
            for provider in (None, "unknown", "GLM", [], {}, FAKE_KEYS["glm"]):
                self.assert_safe_failure(lambda: api.reveal_credential(provider), "invalid_provider")

    def test_mineru_reveals_one_effective_snapshot_with_source_without_writes(self):
        self.mineru_path.parent.mkdir()
        legacy = self.mineru_path.with_name("mineru_token")
        legacy.write_text(FAKE_KEYS["deepseek"] + "\n", encoding="ascii")
        before_legacy = legacy.read_bytes(), legacy.stat().st_mtime_ns
        with patch.object(self.mineru, "save_token", side_effect=AssertionError("no save")), \
                patch.object(self.mineru, "_atomic_bytes", side_effect=AssertionError("no write")), \
                patch.object(self.mineru, "public_status", side_effect=AssertionError("no second read")):
            self.assertEqual(api.reveal_credential("mineru"),
                             {"provider": "mineru", "key": FAKE_KEYS["deepseek"], "source": "legacy_file"})
            self.assertFalse(self.mineru_path.exists())
            self.mineru_path.write_bytes(fake_crypto(FAKE_KEYS["glm"].encode()))
            before_cipher = self.mineru_path.read_bytes(), self.mineru_path.stat().st_mtime_ns
            self.assertEqual(api.reveal_credential("mineru"),
                             {"provider": "mineru", "key": FAKE_KEYS["glm"], "source": "encrypted"})
            self.environ["MINERU_API_TOKEN"] = "synthetic.environment.override"
            with patch.object(self.mineru, "_read", side_effect=AssertionError("env needs no file")):
                self.assertEqual(api.reveal_credential("mineru"),
                                 {"provider": "mineru", "key": "synthetic.environment.override",
                                  "source": "environment"})
        self.assertEqual((legacy.read_bytes(), legacy.stat().st_mtime_ns), before_legacy)
        self.assertEqual((self.mineru_path.read_bytes(), self.mineru_path.stat().st_mtime_ns), before_cipher)
        self.assertEqual({p.name for p in self.mineru_path.parent.iterdir()},
                         {"mineru_token", "mineru_api.dpapi"})

    def test_missing_or_corrupt_mineru_is_safe_and_never_downgrades(self):
        self.assert_safe_failure(lambda: api.reveal_credential("mineru"), "credential_missing")
        self.assertFalse(self.mineru_path.parent.exists())
        self.mineru_path.parent.mkdir()
        self.mineru_path.write_bytes(b"corrupt-synthetic-cipher")
        self.mineru_path.with_name("mineru_token").write_text(FAKE_KEYS["glm"], encoding="ascii")
        self.assert_safe_failure(lambda: api.reveal_credential("mineru"))
        self.assertEqual(self.mineru_path.read_bytes(), b"corrupt-synthetic-cipher")

    def test_mineru_snapshot_failures_cannot_escape_in_exception_or_source(self):
        with patch.object(self.mineru, "_select", side_effect=RuntimeError(FAKE_KEYS["glm"])):
            self.assert_safe_failure(lambda: api.reveal_credential("mineru"))
        with patch.object(self.mineru, "_select", return_value=(FAKE_KEYS["glm"], FAKE_KEYS["deepseek"])):
            self.assert_safe_failure(lambda: api.reveal_credential("mineru"))


class BackendTests(OfflineCase):
    def test_profile_snapshot_and_required_compatibility(self):
        p = profile()
        backend = self.backend(p)
        p["model"] = "caller-changed"
        self.assertEqual(backend.model, "glm/glm-test-next")
        self.assertFalse(backend.is_codex)
        self.assertFalse(backend.is_claude)
        self.assertIsInstance(backend.used_paper, set)
        with self.assertRaises(TypeError):
            backend.profile["model"] = "mutated"
        with self.assertRaises(AttributeError):
            backend.model = "mutated"

    def test_no_config_never_falls_back(self):
        with self.assertRaises(api.ConfigError):
            api.create_backend(None, "qc")
        backend = self.backend(key_store={})
        with self.assertRaisesRegex(api.ConfigError, "credential_missing"):
            backend.ask("synthetic")
        self.assertEqual(self.transport.calls, [])

    def test_default_effort_omitted_and_exact_official_endpoint(self):
        for provider in FAKE_KEYS:
            backend = self.backend(profile(provider))
            self.assertEqual(backend.ask("synthetic prompt"), "Synthetic answer")
            method, url, kwargs = self.transport.calls[-1]
            self.assertEqual((method, url), ("POST", api.ENDPOINTS[provider]))
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + FAKE_KEYS[provider])
            self.assertEqual(kwargs["json"]["messages"], [{"role": "user", "content": "synthetic prompt"}])
            self.assertEqual(kwargs["timeout"], 1500)
            self.assertFalse(kwargs["json"]["stream"])
            self.assertNotIn("reasoning_effort", kwargs["json"])
            self.assertNotIn("thinking", kwargs["json"])

    def test_explicit_efforts(self):
        for provider in FAKE_KEYS:
            for effort in ("none", "low", "high", "max"):
                self.backend(profile(provider, reasoning_effort=effort)).ask("synthetic")
                payload = self.transport.calls[-1][2]["json"]
                if provider == "glm" and effort == "none":
                    self.assertEqual(payload["thinking"], {"type": "disabled"})
                    self.assertNotIn("reasoning_effort", payload)
                else:
                    self.assertEqual(payload["reasoning_effort"], effort)
                    if provider == "glm":
                        self.assertEqual(payload["thinking"], {"type": "enabled"})

    def test_cache_identity_immutable_stable_and_separated(self):
        baseline = self.backend().cache_identity
        self.assertEqual(dict(baseline), dict(self.backend().cache_identity))
        self.assertEqual(set(baseline), {"adapter", "schema", "provider", "endpoint", "model",
                                         "reasoning_effort", "max_tokens", "timeout", "role"})
        self.assertEqual(json.loads(json.dumps(baseline)), dict(baseline))
        with self.assertRaises(TypeError):
            baseline["model"] = "mutated"
        for changes in ({"model": "another-model"}, {"provider": "deepseek"},
                        {"reasoning_effort": "high"}, {"max_tokens": 100}, {"timeout": 60}):
            self.assertNotEqual(baseline, self.backend(profile(**changes)).cache_identity)
        self.assertNotEqual(baseline, api.APIBackend(profile(), "zh", key_store={}).cache_identity)

    def test_cache_identity_all_normal_mutations_blocked(self):
        identity = self.backend().cache_identity
        before = dict(identity)
        for action in (lambda: identity.clear(), lambda: identity.pop("model"),
                       lambda: identity.popitem(), lambda: identity.update({"model": "mutated"}),
                       lambda: identity.setdefault("new", 1),
                       lambda: identity.__delitem__("model"), lambda: identity.__ior__({"role": "zh"})):
            with self.assertRaises(TypeError):
                action()
        identity.__init__({"model": "mutated"})
        self.assertEqual(identity, before)
        self.assertEqual(copy.deepcopy(identity), before)

    def test_actual_model_usage_and_no_response_leak(self):
        logs = []
        backend = self.backend(log=logs.append)
        backend.ask("PRIVATE-PROMPT")
        self.assertEqual(backend.used_run, {"glm/server-test-revision"})
        self.assertEqual(backend.used_paper, backend.used_run)
        self.assertEqual(backend.last_usage["model"], "server-test-revision")
        self.assertEqual(backend.last_usage["usage"]["total_tokens"], 16)
        self.assertEqual(logs, [])
        serialized = json.dumps(backend.usage_history)
        for secret in ("PRIVATE-THINKING", "PRIVATE-PROMPT", "Synthetic answer", *FAKE_KEYS.values()):
            self.assertNotIn(secret, serialized)
        backend.last_usage["usage"]["total_tokens"] = 0
        self.assertEqual(backend.last_usage["usage"]["total_tokens"], 16)
        backend.used_paper.clear()
        self.assertEqual(backend.used_paper, set())
        self.assertEqual(backend.used_run, {"glm/server-test-revision"})
        backend._mark("glm/cache-actual-model")
        self.assertEqual(backend.used_paper, {"glm/cache-actual-model"})

    def test_mark_rejects_foreign_provider_and_invalid_model(self):
        backend = self.backend()
        for value in ("deepseek/test", "glm/../bad", "glm/a b", None, "missing-prefix"):
            with self.subTest(value=value), self.assertRaises(api.ConfigError):
                backend._mark(value)

    def test_concurrent_asks_marks_and_paper_reset(self):
        backend = self.backend()
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(backend.ask, ["synthetic"] * 18)), ["Synthetic answer"] * 18)
        self.assertEqual(len(backend.usage_history), 18)
        self.assertEqual(backend.used_paper, {"glm/server-test-revision"})
        backend.used_paper.clear()
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(backend._mark, [f"glm/test-{i}" for i in range(50)]))
        self.assertEqual(len(backend.used_paper), 50)
        self.assertEqual(len(backend.used_run), 51)

    def test_rejects_invalid_prompt_and_role(self):
        backend = self.backend()
        for value in (None, [], "", " \n"):
            with self.assertRaises(api.ConfigError):
                backend.ask(value)
        with self.assertRaises(api.ConfigError):
            api.APIBackend(profile(), "other")
        self.assertFalse(self.transport.calls)

    def test_rejects_incomplete_finishes_without_retry_or_marks(self):
        for reason in (None, "length", "tool_calls", "content_filter", "aborted",
                       "insufficient_system_resource", "unknown-SECRET"):
            data = completion()
            data["choices"][0]["finish_reason"] = reason
            transport = FakeTransport(api.TransportResult(200, data))
            backend = self.backend(transport=transport)
            with self.subTest(reason=reason), self.assertRaisesRegex(api.ModelAPIError, "incomplete_completion") as caught:
                backend.ask("synthetic")
            expected = "missing" if reason is None else ("unknown" if reason == "unknown-SECRET" else reason)
            self.assertIn("finish_reason=" + expected, str(caught.exception))
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(len(transport.calls), 1)
            self.assertFalse(backend.used_run)

    def test_empty_content_and_tools_are_rejected(self):
        changes = [{"content": x} for x in (None, "", " \n", [], 42)]
        changes += [{"tool_calls": [{"function": {"name": "SECRET"}}]},
                    {"function_call": {"name": "SECRET"}}, {"role": "tool"}]
        for change in changes:
            data = completion()
            data["choices"][0]["message"].update(change)
            with self.subTest(change=change), self.assertRaises(api.ModelAPIError) as caught:
                self.backend(transport=FakeTransport(api.TransportResult(200, data))).ask("synthetic")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_malformed_response_and_usage(self):
        bad = [None, [], {}, completion(choices=[]), completion(choices=[{}, {}]),
               completion(choices=[None]), completion(model=None), completion(model="bad\nmodel"),
               completion(error={"message": "SECRET"}), completion(usage=[]),
               completion(usage={"total_tokens": True}), completion(usage={"total_tokens": -1}),
               completion(usage={"prompt_tokens_details": []}),
               completion(usage={"completion_tokens_details": {"reasoning_tokens": "SECRET"}})]
        for data in bad:
            with self.subTest(data=type(data)), self.assertRaises(api.ModelAPIError) as caught:
                self.backend(transport=FakeTransport(api.TransportResult(200, data))).ask("synthetic")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_unknown_usage_fields_are_dropped(self):
        data = completion(usage={"prompt_tokens": 2, "reasoning_content": "SECRET",
                                 "prompt_tokens_details": {"cached_tokens": 1, "headers": "SECRET"}})
        backend = self.backend(transport=FakeTransport(api.TransportResult(200, data)))
        backend.ask("synthetic")
        self.assertEqual(backend.last_usage["usage"], {"prompt_tokens": 2, "prompt_tokens_details": {"cached_tokens": 1}})

    def test_no_auto_read_of_real_store_when_constructing_backend(self):
        with patch.object(api.DPAPIConfigStore, "load", side_effect=AssertionError("no IO")):
            backend = api.create_backend(profile(), "qc")
            self.assertIsNone(backend.last_usage)


class TransportTests(OfflineCase):
    def test_retry_429_5xx_at_most_three_attempts(self):
        for status in (429, 500, 503, 599):
            transport = FakeTransport(api.TransportResult(status, {"error": "SECRET"}))
            sleeps = []
            with self.subTest(status=status), self.assertRaises(api.ModelAPIError) as caught:
                self.backend(transport=transport, sleep=sleeps.append).ask("SECRET-PROMPT")
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(len(transport.calls), 3)
            self.assertEqual(sleeps, [1, 2])
            self.assertNotIn("SECRET", str(caught.exception))

    def test_success_after_retry_preserves_request(self):
        transport = FakeTransport(api.TransportResult(429), api.TransportResult(503),
                                  api.TransportResult(200, completion()))
        backend = self.backend(transport=transport, sleep=lambda _: None)
        backend.ask("synthetic")
        self.assertEqual(backend.last_usage["attempts"], 3)
        self.assertEqual(transport.calls[0], transport.calls[1])
        self.assertEqual(transport.calls[1], transport.calls[2])

    def test_no_retry_client_errors_or_redirects(self):
        for status in (201, 301, 302, 307, 308, 400, 401, 403, 404, 408, 422):
            transport = FakeTransport(api.TransportResult(status, "SECRET"))
            with self.subTest(status=status), self.assertRaises(api.ModelAPIError) as caught:
                self.backend(transport=transport).ask("synthetic")
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(len(transport.calls), 1)

    def test_transport_errors_are_safe_and_not_retried(self):
        for error, expected in ((requests.Timeout("SECRET"), "request_timeout"),
                                (requests.exceptions.SSLError("SECRET"), "tls_error"),
                                (requests.ConnectionError("SECRET"), "transport_error"),
                                (RuntimeError("SECRET"), "transport_error"),
                                (api.ModelAPIError("SECRET"), "transport_error")):
            transport = FakeTransport(error)
            with self.subTest(error=type(error)), self.assertRaises(api.ModelAPIError) as caught:
                self.backend(transport=transport).ask("SECRET-PROMPT")
            formatted = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn("SECRET", formatted)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(len(transport.calls), 1)

    def test_invalid_transport_result(self):
        for result in ({}, api.TransportResult(True), api.TransportResult(999)):
            with self.assertRaises(api.ModelAPIError):
                self.backend(transport=FakeTransport(result)).ask("synthetic")

    def test_default_http_security_and_ignores_proxy_env(self):
        session, response = MagicMock(), MagicMock()
        session.__enter__.return_value = session
        session.request.return_value.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [json.dumps(completion()).encode()]
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://invalid-proxy.test:9", "REQUESTS_CA_BUNDLE": "wrong"}), \
             patch.object(api.requests, "Session", return_value=session):
            self.backend(transport=api.RequestsTransport()).ask("synthetic")
        self.assertIs(session.trust_env, False)
        self.assertIs(session.verify, True)
        kwargs = session.request.call_args.kwargs
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["stream"], True)
        self.assertEqual(kwargs["timeout"], (30, 1500))

    def test_default_transport_does_not_read_error_body(self):
        session, response = MagicMock(), MagicMock()
        session.__enter__.return_value = session
        session.request.return_value.__enter__.return_value = response
        response.status_code = 401
        with patch.object(api.requests, "Session", return_value=session):
            with self.assertRaises(api.ModelAPIError):
                self.backend(transport=api.RequestsTransport()).ask("synthetic")
        response.iter_content.assert_not_called()
        response.json.assert_not_called()

    def test_default_transport_rejects_custom_or_coding_urls(self):
        transport = api.RequestsTransport()
        for url in ("http://api.deepseek.com/chat/completions", "https://evil.test",
                    "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"):
            with self.assertRaisesRegex(api.ModelAPIError, "endpoint_not_allowed"):
                transport.request("POST", url, headers={}, json={}, timeout=1)

    def test_default_transport_bad_json_and_size_guard(self):
        for body, limit, code in ((b"SECRET-not-json", 1024, "invalid_response_json"),
                                  (b"SECRET" * 100, 10, "response_too_large")):
            session, response = MagicMock(), MagicMock()
            session.__enter__.return_value = session
            session.request.return_value.__enter__.return_value = response
            response.status_code = 200
            response.iter_content.return_value = [body]
            with patch.object(api.requests, "Session", return_value=session), \
                 patch.object(api, "MAX_RESPONSE_BYTES", limit):
                with self.assertRaisesRegex(api.ModelAPIError, code) as caught:
                    self.backend(transport=api.RequestsTransport()).ask("synthetic")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_slot_limit_fixed_two_and_backoff_outside_slot(self):
        occupied, calls = [], []

        @contextmanager
        def slot(**kwargs):
            calls.append(kwargs)
            occupied.append(True)
            try:
                yield
            finally:
                occupied.pop()

        def sleep(_):
            self.assertFalse(occupied)

        with patch.object(api, "glm_slot", slot), patch.dict(os.environ, {"GLM_MAX_CONCURRENCY": "10"}):
            transport = FakeTransport(api.TransportResult(429), api.TransportResult(200, completion()))
            self.backend(transport=transport, sleep=sleep).ask("synthetic")
        self.assertEqual([c["limit"] for c in calls], [2, 2])
        self.assertEqual([c["timeout"] for c in calls], [1500, 1500])

    def test_response_elapsed_timeout_is_safe(self):
        session, response = MagicMock(), MagicMock()
        session.__enter__.return_value = session
        session.request.return_value.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [b"SECRET"]
        with patch.object(api.requests, "Session", return_value=session), \
             patch.object(api.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaisesRegex(api.ModelAPIError, "request_timeout"):
                api.RequestsTransport().request("POST", api.ENDPOINTS["glm"],
                                                headers={}, json={}, timeout=60)

    def test_shared_default_root_used(self):
        @contextmanager
        def slot(**kwargs):
            self.assertIsNone(kwargs["root"])
            self.assertEqual(kwargs["limit"], 2)
            yield

        with patch.object(api, "glm_slot", slot):
            self.backend(slot_root=None).ask("synthetic")

    def test_process_requests_max_two(self):
        ctx = multiprocessing.get_context("spawn")
        active, peak, start = ctx.Value("i", 0), ctx.Value("i", 0), ctx.Event()
        processes = [ctx.Process(target=slot_worker, args=(str(self.root / "slots"), active, peak, start))
                     for _ in range(5)]
        for process in processes:
            process.start()
        start.set()
        try:
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(peak.value, 2)
            self.assertEqual(active.value, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)


class ProbeTests(OfflineCase):
    def setUp(self):
        super().setUp()
        original_store = api._store

        def fixture_store(key_store=None, path=None):
            if key_store is None or (isinstance(key_store, api.DPAPIConfigStore)
                    and not key_store.path.resolve().is_relative_to(self.root.resolve())):
                raise AssertionError("probe tests require an explicit fixture store")
            return original_store(key_store, path)

        guard = patch.object(api, "_store", side_effect=fixture_store)
        guard.start()
        self.addCleanup(guard.stop)

    def test_probe_isolation_guards_are_active(self):
        guarded = (api._store, socket.getaddrinfo, socket.create_connection, socket.socket.connect,
                   socket.socket.connect_ex, socket.socket.bind, socket.socket.listen)
        for function in guarded:
            self.assertIsInstance(function, MagicMock)
            with self.assertRaises(AssertionError):
                function()

    def test_synthetic_probe_returns_only_safe_metrics(self):
        result = self.probe()
        self.assertIs(result["ok"], True)
        self.assertEqual(result["role"], "qc")
        self.assertEqual(result["model"], "server-test-revision")
        payload = self.transport.calls[0][2]["json"]
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Reply with exactly OK."}])
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertNotIn("Synthetic answer", json.dumps(result))
        self.assertNotIn("PRIVATE-THINKING", json.dumps(result))

    def test_probe_role_reaches_metrics_without_provider_payload_or_config_change(self):
        for provider in ("glm", "deepseek"):
            candidate = {**api.profile_defaults()[provider], "model": "synthetic-model"}
            original = copy.deepcopy(candidate)
            for role in ("qc", "zh"):
                for explicit_profile in (False, True):
                    arguments = {"profile": candidate} if explicit_profile else {
                        "provider": provider, "model": candidate["model"]}
                    result = api.test_connection(**arguments, role=role, key_store=FAKE_KEYS,
                                                 transport=self.transport, slot_root=self.root / "slots")
                    self.assertEqual(result["role"], role)
                    self.assertNotIn("role", self.transport.calls[-1][2]["json"])
            self.assertEqual(candidate, original)
            with self.assertRaisesRegex(api.ConfigError, "invalid_profile_fields"):
                api.validate_profile({**candidate, "role": "zh"})

    def test_probe_invalid_role_rejected_before_credentials_or_transport(self):
        with patch.object(api, "_store", side_effect=AssertionError("no credential access")) as store:
            for role in (None, True, 0, [], {}, "", "ZH", "unknown", "zh "):
                with self.subTest(role=role), self.assertRaisesRegex(api.ConfigError, "^invalid_role$"):
                    api.test_connection(profile=profile(), role=role, transport=self.transport)
            store.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    def test_provider_only_probe_prefers_selected_role_with_legacy_qc_default(self):
        configured = config()
        configured["zh"] = profile("glm", model="synthetic-zh-model")
        self.store.save(configured, FAKE_KEYS)
        before = self.store.path.read_bytes()
        for role, model in (("qc", "glm-test-next"), ("zh", "synthetic-zh-model")):
            result = self.probe(model=None, key_store=self.store, role=role)
            self.assertEqual(result["role"], role)
            self.assertEqual(self.transport.calls[-1][2]["json"]["model"], model)
        self.assertEqual(self.probe(model=None, key_store=self.store)["role"], "qc")
        self.assertEqual(self.transport.calls[-1][2]["json"]["model"], "glm-test-next")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_probe_model_from_saved_profile_no_default_model(self):
        self.store.save(config(), FAKE_KEYS)
        self.probe(model=None, key_store=self.store)
        self.assertEqual(self.transport.calls[0][2]["json"]["model"], "glm-test-next")
        empty = api.DPAPIConfigStore(self.root / "missing.dpapi", crypto=fake_crypto)
        with self.assertRaisesRegex(api.ConfigError, "model_required"):
            self.probe(model=None, key_store=empty)

    def test_deepseek_model_discovery_uses_official_get(self):
        transport = FakeTransport(api.TransportResult(200, {"data": [
            {"id": "new-future-model", "extra": "SECRET"}, {"id": "new-future-model"}, {"id": "next-model"}]}))
        result = api.list_models("deepseek", transport=transport, key_store=FAKE_KEYS, slot_root=self.root / "slots")
        self.assertEqual(result, ["new-future-model", "next-model"])
        self.assertEqual(transport.calls[0][0:2], ("GET", "https://api.deepseek.com/models"))
        self.assertIsNone(transport.calls[0][2]["json"])

    def test_glm_model_discovery_explicit_unsupported_without_io(self):
        with patch.object(api.DPAPIConfigStore, "load", side_effect=AssertionError("no keys read")):
            with self.assertRaisesRegex(api.UnsupportedOperation, "model_listing_not_supported"):
                api.list_models("glm", transport=self.transport)
        self.assertFalse(self.transport.calls)

    def test_model_discovery_malformed_or_auth_failure(self):
        for data in ({}, {"data": [None]}, {"data": [{"id": "bad\nmodel"}]},
                     {"data": [], "error": {"message": "SECRET"}}):
            with self.assertRaises(api.ModelAPIError):
                api.list_models("deepseek", transport=FakeTransport(api.TransportResult(200, data)),
                                key_store=FAKE_KEYS, slot_root=self.root / "slots")
        transport = FakeTransport(api.TransportResult(403, "SECRET"))
        with self.assertRaises(api.ModelAPIError):
            api.list_models("deepseek", transport=transport, key_store=FAKE_KEYS, slot_root=self.root / "slots")
        self.assertEqual(len(transport.calls), 1)

    def test_glm_choices_are_exact_document_candidates_without_credential_or_network_io(self):
        with patch.object(api, "_store", side_effect=AssertionError("no credentials")), \
                patch.object(api, "list_models", side_effect=AssertionError("no listing endpoint")), \
                patch.object(api, "_request", side_effect=AssertionError("no network")):
            result = api.model_choices("glm", transport=self.transport)
        self.assertEqual(set(result), {"provider", "models", "source", "notice", "reference", "details",
                                       "fetched_at", "endpoint_kind"})
        self.assertEqual(result["provider"], "glm")
        self.assertEqual(result["models"], list(api.GLM_MODEL_CANDIDATES))
        self.assertEqual(result["source"], "bundled_catalog")
        for wording in ("内置官方文档候选", "非账户可用清单", "需测试"):
            self.assertIn(wording, result["notice"])
        self.assertEqual(result["reference"], api.GLM_MODEL_REFERENCE)
        self.assertFalse(self.transport.calls)
        result["models"].append("caller-only")
        self.assertEqual(api.model_choices("glm")["models"], list(api.GLM_MODEL_CANDIDATES))

    def test_deepseek_choices_preserve_validated_provider_list_contract(self):
        transport = FakeTransport(api.TransportResult(200, {"data": [
            {"id": "model-beta", "extra": "PRIVATE"}, {"id": "model-alpha"}, {"id": "model-beta"}]}))
        result = api.model_choices("deepseek", transport=transport, key_store=FAKE_KEYS,
                                   slot_root=self.root / "slots")
        self.assertEqual(set(result), {"provider", "models", "source", "notice", "reference",
                                       "fetched_at", "endpoint_kind"})
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["source"], "provider_api")
        self.assertEqual(result["models"], ["model-beta", "model-alpha"])
        self.assertIn("需测试", result["notice"])
        self.assertNotIn("PRIVATE", repr(result))
        self.assertEqual(transport.calls[0][:2], ("GET", "https://api.deepseek.com/models"))
        self.assertEqual(len(transport.calls), 1)

    def test_deepseek_choices_never_fall_back_on_auth_or_invalid_model_list(self):
        for response in (api.TransportResult(401, FAKE_KEYS["deepseek"]),
                         api.TransportResult(200, {"data": [{"id": "bad model"}]})):
            transport = FakeTransport(response)
            with self.assertRaises(api.ModelAPIError) as caught:
                api.model_choices("deepseek", transport=transport, key_store=FAKE_KEYS,
                                  slot_root=self.root / "slots")
            self.assertNotIn(FAKE_KEYS["deepseek"], repr(caught.exception))
            self.assertEqual(len(transport.calls), 1)


class ZcodeProviderTests(OfflineCase):
    """The local-login provider: no key, no endpoint, and honestly no route."""

    def setUp(self):
        super().setUp()
        enable = patch.object(api, "zcode_available", return_value=True)
        enable.start(); self.addCleanup(enable.stop)
        # Hermetic routing facts: never read the developer's real CLI config
        # from tests. Both plans are unsupported exactly like production.
        self.report = patch.object(api, "zcode_route_report", return_value={
            "coding": {"supported": False, "reason": "login_route_unavailable",
                       "provider_id": api.ZCODE_CODING_PROVIDER_ID,
                       "models": {model: {"max_output_tokens": 128000,
                                          "effort_levels": ["low", "high", "max"], "default_effort": "max"}
                                  for model in api.ZCODE_MODELS}},
            "start": {"supported": False, "reason": "start_route_not_implemented",
                      "provider_id": api.ZCODE_START_PROVIDER_KEY, "models": {}}})
        self.report.start(); self.addCleanup(self.report.stop)

    def test_profile_shape_and_rejections(self):
        base = {"provider": "zcode", "model": "glm-5.3-flash", "reasoning_effort": "max"}
        self.assertEqual(api.validate_profile(base),
                         {**base, "plan": "coding", "timeout": 1500, "max_tokens": 128000})
        for bad, code in [
            ({**base, "model": "glm-4.6"}, "zcode_unknown_model"),
            ({**base, "reasoning_effort": "xhigh"}, "zcode_effort_level"),
            ({**base, "base_url": "https://user.example"}, "zcode_no_endpoint_fields"),
            ({**base, "thinking": "enabled"}, "zcode_no_endpoint_fields"),
        ]:
            with self.assertRaisesRegex(api.ConfigError, code):
                api.validate_profile(bad)
        for effort in ("default", "low", "high", "max"):
            self.assertEqual(api.validate_profile({**base, "reasoning_effort": effort})["reasoning_effort"], effort)
        # A start-plan profile keeps parsing so legacy configs load and display.
        start = api.validate_profile({"provider": "zcode", "plan": "start", "model": "glm-5.3-flash",
                                      "reasoning_effort": "low"})
        self.assertEqual(start["plan"], "start")
        defaults = api.profile_defaults()
        self.assertEqual(defaults["zcode"]["max_tokens"], 128000)
        self.assertEqual(defaults["zcode"]["plan"], "coding")
        # Fresh templates default to the user preference high, not the CLI's max.
        self.assertEqual(defaults["zcode"]["reasoning_effort"], "high")
        self.assertNotIn("base_url", defaults["zcode"])

    def test_credentials_are_login_presence_not_stored_keys(self):
        value = {"schema": 1, "qc": {"provider": "zcode", "model": "glm-5.3"},
                 "zh": {"provider": "zcode", "model": "glm-5.3"}}
        # beta.14 fail-closed: every zcode route is unroutable, so a REQUIRED
        # saved zcode role is rejected with the fixed route-unavailable code
        # whether or not the local login exists — login presence is a
        # display fact, never a usable credential.
        for logged_in in (False, True):
            with patch.object(api, "zcode_logged_in", return_value=logged_in):
                with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
                    api.validate_config(value, require_credentials=True)
                with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
                    api.validate_config(value, require_credentials=True, required_roles=("qc",))
        # Reading/migrating the legacy config stays permissive, and an UNUSED
        # zh zcode role (translation off, qc on an API provider) must not
        # block the QC path.
        logged = patch.object(api, "zcode_logged_in", return_value=True)
        logged.start(); self.addCleanup(logged.stop)
        fixed = {"schema": 1, "qc": {"provider": "zcode", "model": "glm-5.3", "reasoning_effort": "max"},
                 "zh": {"provider": "zcode", "model": "glm-5.3", "reasoning_effort": "max"}}
        normalized = api.validate_config(fixed)
        self.assertEqual(normalized,
                         {"schema": 1, "qc": {**fixed["qc"], "plan": "coding", "timeout": 1500, "max_tokens": 128000},
                          "zh": {**fixed["zh"], "plan": "coding", "timeout": 1500, "max_tokens": 128000}})
        mixed = {"schema": 1, "qc": {"provider": "glm", "model": "glm-5.3"},
                 "zh": {"provider": "zcode", "model": "glm-5.3"}}
        self.assertEqual(api.validate_config(mixed, require_credentials=True, key_store=FAKE_KEYS,
                                             required_roles=("qc",))["qc"]["provider"], "glm")
        with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
            api.validate_config(mixed, require_credentials=True, key_store=FAKE_KEYS)
        with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
            api.assert_callable_routes(normalized, ("qc",))
        public = api.public_config(key_store=self.store)
        self.assertTrue(public["credentials"]["zcode"])

    def test_model_choices_and_backend_never_touch_the_cli(self):
        # glm_client (with its private-home v2 credential copies) must never be
        # routed through this provider; the honest answer is a fixed failure.
        base = {"provider": "zcode", "model": "glm-5.3-flash", "reasoning_effort": "max"}
        choices = api.model_choices(profile=base)
        self.assertEqual(choices["source"], "bundled_catalog")
        self.assertEqual(choices["endpoint_kind"], "zcode")
        self.assertEqual(choices["plan"], "coding")
        self.assertEqual(choices["models"], [])
        self.assertIn("已停用", choices["notice"])
        self.assertIn("改选 API Key 服务商", choices["notice"])
        logged = patch.object(api, "zcode_logged_in", return_value=True)
        logged.start(); self.addCleanup(logged.stop)
        import glm_client
        with patch.object(glm_client, "ask", side_effect=AssertionError("no CLI call")), \
                patch.object(glm_client, "ensure_home", side_effect=AssertionError("no home copy")):
            backend = api.create_backend(base, "qc", log=None)
            self.assertEqual(backend.cache_identity["endpoint"], "zcode-cli")
            self.assertEqual(backend.cache_identity["plan"], "coding")
            self.assertEqual(backend.cache_identity["reasoning_level"], "max")
            with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
                backend.ask("Reply with exactly OK.")
            with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
                api.create_backend({**base, "plan": "start"}, "qc", log=None).ask("start plan")
            with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
                api.test_connection(profile=base, key_store={})
        self.assertEqual(backend.last_usage, None)


class ZcodePlanTests(OfflineCase):
    """Both subscription plans are truthfully unsupported; fixed reason codes."""

    def setUp(self):
        super().setUp()
        enable = patch.object(api, "zcode_available", return_value=True)
        enable.start(); self.addCleanup(enable.stop)

    def report(self, start_supported=False, start_models=("glm-5.3-flash",)):
        return {
            "coding": {"supported": False, "reason": "login_route_unavailable",
                       "provider_id": api.ZCODE_CODING_PROVIDER_ID,
                       "models": {model: {"max_output_tokens": 128000,
                                          "effort_levels": ["low", "high", "max"], "default_effort": "max"}
                                  for model in api.ZCODE_MODELS}},
            "start": {"supported": start_supported, "reason": "" if start_supported else "start_route_not_implemented",
                      "provider_id": api.ZCODE_START_PROVIDER_KEY,
                      "models": {model: {"max_output_tokens": 128000} for model in start_models} if start_supported else {}},
        }

    def test_start_plan_unsupported_before_any_cli_call(self):
        import glm_client
        base = {"provider": "zcode", "plan": "start", "model": "glm-5.3-flash", "reasoning_effort": "max"}
        with patch.object(api, "zcode_route_report", return_value=self.report(False)), \
                patch.object(glm_client, "ask", side_effect=AssertionError("no CLI call")):
            # Parsing stays possible (legacy display); runs fail closed with a
            # fixed code before any CLI invocation or credential read.
            self.assertEqual(api.validate_profile(base)["plan"], "start")
            with self.assertRaisesRegex(api.ConfigError, "zcode_route_unavailable"):
                api.create_backend(base, "qc", log=None).ask("no start route")
            # The listing endpoint answers with an honest empty catalog instead
            # of an error or — worse — a silently rerouted Coding Plan list.
            choices = api.model_choices(profile=base)
            self.assertEqual(choices, {"provider": "zcode", "plan": "start", "models": [], "source": "bundled_catalog",
                                       "endpoint_kind": "zcode", "fetched_at": None,
                                       "notice": "Start Plan 没有可调用的本机 CLI 路由（未实现官方 Start 调用，也不自建私有 OAuth 适配）"
                                                 "（start_route_not_implemented）。不会改走其它订阅或计费服务商；请改选 API Key 服务商"
                                                 "（GLM / DeepSeek / 同济）。已保存的 ZCode 配置仍可读取显示，便于迁移。"})

    def test_both_plans_list_nothing_and_never_reroute(self):
        with patch.object(api, "zcode_route_report", return_value=self.report(False)):
            for plan in ("start", "coding"):
                choices = api.model_choices(profile={"provider": "zcode", "plan": plan,
                                                     "model": "glm-5.3-flash", "reasoning_effort": "max"})
                self.assertEqual(choices["models"], [])
                self.assertNotIn("details", choices)
                self.assertIn("改选 API Key 服务商", choices["notice"])
        self.assertEqual(api.zcode_plan_models("start", self.report(False)), ())

    def test_legacy_zcode_defaults_to_coding_and_plan_is_zcode_only(self):
        with patch.object(api, "zcode_route_report", return_value=self.report()):
            legacy = api.validate_profile({"provider": "zcode", "model": "glm-5.3", "reasoning_effort": "max"})
            self.assertEqual(legacy["plan"], "coding")
            with self.assertRaisesRegex(api.ConfigError, "zcode_unknown_plan"):
                api.validate_profile({"provider": "zcode", "plan": "pro", "model": "glm-5.3", "reasoning_effort": "max"})
        with self.assertRaisesRegex(api.ConfigError, "plan_zcode_only"):
            api.validate_profile({**profile(), "plan": "coding"})

    def test_cache_identities_reflect_plan_and_effective_level(self):
        # The route cannot run, but a cached identity still names the parsed
        # subscription and the effective effort: a plan or level change must
        # never reuse another route's entry.
        identities = {effort: api.create_backend({"provider": "zcode", "plan": "coding", "model": "glm-5.3",
                                                  "reasoning_effort": effort}, "qc", log=None).cache_identity
                      for effort in ("default", "low", "high", "max")}
        self.assertEqual([identities[e]["reasoning_level"] for e in ("default", "low", "high", "max")],
                         ["", "low", "high", "max"])
        self.assertEqual(len({json.dumps(i, sort_keys=True) for i in identities.values()}), 4)
        start = api.create_backend({"provider": "zcode", "plan": "start", "model": "glm-5.3-flash",
                                    "reasoning_effort": "high"}, "qc", log=None).cache_identity
        self.assertEqual(start["plan"], "start")
        self.assertNotEqual(start, identities["high"])
        fresh = api.create_backend({**api.profile_defaults()["zcode"], "model": "glm-5.3"}, "qc", log=None)
        self.assertEqual(fresh.cache_identity["reasoning_level"], "high")

    def test_route_report_is_sanitized_and_never_leaks_option_values(self):
        official = {"kind": "anthropic", "options": {"baseURL": api.ZCODE_CLI_CODING_ENDPOINT,
                                                     "apiKeyRequired": True, "apiKey": ""},
                    "headers": {}, "models": {"glm-5.3": {"limit": {"output": 128000},
                                                          "reasoning": {"levels": ["low", "high", "max"],
                                                                        "defaultLevel": "max"}}}}
        cli = {"provider": {"bigmodel": {"options": {"apiKey": "CANARY-CLI-KEY", "baseURL": "https://canary"},
                                          "models": {"glm-5.3": {"limit": {"output": 128000}}}}}}
        v2 = {"provider": {"builtin:bigmodel-start-plan": {
            "options": {"apiKey": "CANARY-V2-KEY"},
            "enabled": True, "systemDisabledReason": "arbitrary provider free text CANARY-REASON",
            "models": {"GLM-5.3-Flash": {"reasoning": {"variants": ["low", "high", "max"], "defaultVariant": "max"},
                                          "limit": {"output": 128000}}}}}}
        executable = patch.object(api, "zcode_executable", return_value="C:\\fake\\zcode.cmd")
        executable.start(); self.addCleanup(executable.stop)
        with tempfile.TemporaryDirectory(prefix="zcode-report-") as directory:
            def paths(provider_document):
                cli_path = Path(directory) / "cli.json"
                v2_path = Path(directory) / "v2.json"
                cli_path.write_text(json.dumps(provider_document), encoding="utf-8")
                v2_path.write_text(json.dumps(v2), encoding="utf-8")
                return cli_path, v2_path
            report = api.zcode_route_report(*paths(cli))
            summary = api.zcode_plan_summary(*paths(cli))
            official_report = api.zcode_route_report(*paths({"provider": {"bigmodel": official}}))
        # An arbitrary baseURL / saved API-key profile is NOT the Coding Plan.
        self.assertFalse(report["coding"]["supported"])
        self.assertEqual(report["coding"]["reason"], "provider_config_unsupported")
        self.assertEqual(report["coding"]["models"], {})
        # The official login-managed shape still never claims support: our
        # adapter simply has no callable route.
        self.assertFalse(official_report["coding"]["supported"])
        self.assertEqual(official_report["coding"]["reason"], "login_route_unavailable")
        self.assertEqual(list(official_report["coding"]["models"]), ["glm-5.3"])
        self.assertEqual(official_report["coding"]["models"]["glm-5.3"]["max_output_tokens"], 128000)
        # Start is unsupported regardless of the App entry being "enabled"; its
        # model ids surface separately as display-only facts.
        for row in (report["start"], official_report["start"]):
            self.assertFalse(row["supported"])
            self.assertEqual(row["reason"], "start_route_not_implemented")
        self.assertEqual(report["start"]["app_models"], ["glm-5.3-flash"])
        self.assertEqual(api.zcode_plan_models("start", report), ())
        self.assertEqual(summary["start"]["models"], [])
        for rendered in (json.dumps(report), json.dumps(summary)):
            for canary in ("CANARY-CLI-KEY", "CANARY-V2-KEY", "CANARY-REASON", "apiKey", "baseURL"):
                self.assertNotIn(canary, rendered)
        # Absent configs stay truthful, with fixed reason codes only.
        empty = api.zcode_route_report(Path(self.temp.name) / "missing-cli.json",
                                       Path(self.temp.name) / "missing-v2.json")
        self.assertEqual((empty["coding"]["reason"], empty["start"]["reason"]),
                         ("cli_config_unreadable", "start_route_not_implemented"))
        self.assertEqual(api.zcode_plan_models("start", empty), ())

    def test_route_report_requires_the_cli_executable(self):
        executable = patch.object(api, "zcode_executable", return_value="")
        executable.start(); self.addCleanup(executable.stop)
        report = api.zcode_route_report(Path(self.temp.name) / "missing.json", Path(self.temp.name) / "missing.json")
        self.assertEqual(report["coding"]["reason"], "cli_executable_unavailable")
        self.assertEqual(report["start"]["reason"], "cli_executable_unavailable")
        self.assertFalse(any(row["supported"] for row in report.values()))
        stub = patch.object(api, "zcode_available", return_value=False)
        stub.start(); self.addCleanup(stub.stop)
        report = api.zcode_route_report(Path(self.temp.name) / "missing.json", Path(self.temp.name) / "missing.json")
        self.assertEqual(report["coding"]["reason"], "cli_adapter_unavailable")


if __name__ == "__main__":
    unittest.main()
