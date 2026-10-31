from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

import scidev_credentials as credentials
import scidev_provider as provider
from scidev_core import CodingAgent, EventLedger, GitManager


class ProviderSettingsTests(unittest.TestCase):
    def test_save_and_load_round_trip_keeps_key_out_of_json(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            with patch.object(credentials, "secure_storage_available", return_value=True), \
                    patch.object(credentials, "set_api_key") as set_key, \
                    patch.object(credentials, "get_api_key", return_value="secret-value"):
                config = provider.save(
                    root,
                    base_url="https://example.test/v1/",
                    model="model-a",
                    api_key="secret-value",
                    request_timeout=30,
                    reasoning_effort="high",
                )
                loaded = provider.load(root, require_key=True)

            self.assertEqual(config.base_url, "https://example.test/v1")
            self.assertEqual(loaded.model, "model-a")
            self.assertEqual(loaded.api_key, "secret-value")
            self.assertEqual(loaded.reasoning_effort, "high")
            self.assertEqual(loaded.source, "saved")
            set_key.assert_called_once_with("secret-value", "default")
            raw = (root / ".research" / "provider.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-value", raw)
            self.assertEqual(json.loads(raw)["base_url"], "https://example.test/v1")

    def test_environment_is_used_when_no_saved_file_exists(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            with patch.dict(os.environ, {
                "SCIDEV_API_BASE": "http://127.0.0.1:11434/v1",
                "SCIDEV_MODEL": "local-model",
                "SCIDEV_API_KEY": "env-key",
            }, clear=False), patch.object(credentials, "secure_storage_available", return_value=False):
                config = provider.load(root, require_key=True)

            self.assertEqual(config.source, "environment")
            self.assertEqual(config.base_url, "http://127.0.0.1:11434/v1")
            self.assertEqual(config.model, "local-model")
            self.assertEqual(config.api_key, "env-key")

    def test_saved_config_falls_back_to_environment_key(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            with patch.object(credentials, "secure_storage_available", return_value=False), \
                    patch.dict(os.environ, {"SCIDEV_API_KEY": "env-key"}, clear=False):
                provider.save(root, base_url="https://example.test/v1", model="m")
                config = provider.load(root, require_key=True)
            self.assertEqual(config.api_key, "env-key")

    def test_missing_key_fails_closed_when_required(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            with patch.object(credentials, "secure_storage_available", return_value=False), \
                    patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(provider.ProviderConfigError):
                    provider.load(root, require_key=True)

    def test_validation_rejects_unsafe_values(self) -> None:
        with self.assertRaises(provider.ProviderConfigError):
            provider.validate_config("file:///tmp/x", "model")
        with self.assertRaises(provider.ProviderConfigError):
            provider.validate_config("https://example.test/v1", "")
        with self.assertRaises(provider.ProviderConfigError):
            provider.validate_config("https://example.test/v1", "m", request_timeout=1)
        with self.assertRaises(provider.ProviderConfigError):
            provider.validate_config("https://example.test/v1", "m", temperature=3)
        with self.assertRaises(provider.ProviderConfigError):
            provider.save(
                Path(tempfile.gettempdir()),
                base_url="https://example.test/v1",
                model="m",
                reasoning_effort="extreme",
            )

    def test_key_status_distinguishes_credential_environment_and_missing(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            with patch.object(credentials, "secure_storage_available", return_value=True), \
                    patch.object(credentials, "get_api_key", return_value="saved"):
                self.assertEqual(provider.key_status(root), "credential")
            with patch.object(credentials, "secure_storage_available", return_value=False), \
                    patch.dict(os.environ, {"SCIDEV_API_KEY": "env"}, clear=False):
                self.assertEqual(provider.key_status(root), "environment")
            with patch.object(credentials, "secure_storage_available", return_value=False), \
                    patch.dict(os.environ, {}, clear=True):
                self.assertEqual(provider.key_status(root), "missing")

    def test_connection_probe_reports_models_and_http_errors(self) -> None:
        config = provider.ProviderConfig(
            base_url="https://example.test/v1",
            model="m",
            api_key="secret",
        )

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"data":[{"id":"a"},{"id":"b"}]}'

        with patch.object(provider.urllib_request, "urlopen", return_value=Response()) as urlopen:
            ok, message = provider.test_connection(config)
        self.assertTrue(ok)
        self.assertIn("2", message)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

        http_error = provider.urllib_error.HTTPError(
            "https://example.test/v1/models", 401, "Unauthorized", {}, None,
        )
        try:
            with patch.object(provider.urllib_request, "urlopen", side_effect=http_error):
                ok, message = provider.test_connection(config)
        finally:
            http_error.close()
        self.assertFalse(ok)
        self.assertIn("401", message)

    def test_capability_probe_reports_tools_context_and_vision(self) -> None:
        config = provider.ProviderConfig(
            base_url="https://example.test/v1",
            model="m",
            api_key="secret",
        )

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return self.payload

        responses = [
            Response(b'{"data":[{"id":"m","context_length":128000,"capabilities":["vision"]}]}'),
            Response(b'{"choices":[{"message":{"tool_calls":[{"id":"call_1"}]}}]}'),
        ]
        with patch.object(provider.urllib_request, "urlopen", side_effect=responses):
            result = provider.probe_capabilities(config)
        self.assertTrue(result["tools_supported"])
        self.assertEqual(result["context_length"], 128000)
        self.assertTrue(result["vision"])
        self.assertIn("工具调用：支持", provider.format_capabilities(result))
        self.assertIn("128,000", provider.format_capabilities(result))

    def test_agent_builds_provider_from_saved_config(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            with patch.object(credentials, "secure_storage_available", return_value=False):
                provider.save(
                    root,
                    base_url="https://example.test/v1",
                    model="saved-model",
                    reasoning_effort="high",
                )
            agent = CodingAgent(
                root, EventLedger(root), GitManager(root),
                require_verification=False,
            )
            with patch.dict(os.environ, {"SCIDEV_API_KEY": "env-key"}, clear=False), \
                    patch("scidev_core.OpenAICompatibleProvider.from_env", side_effect=AssertionError("env path used")):
                built = agent._build_provider()
            self.assertEqual(built.base_url, "https://example.test/v1")
            self.assertEqual(built.model, "saved-model")
            self.assertEqual(built.api_key, "env-key")
            self.assertEqual(agent.provider_reasoning_effort, "high")


class CredentialManagerTests(unittest.TestCase):
    def test_target_name_is_bounded_and_sanitized(self) -> None:
        self.assertEqual(credentials.credential_target("a/b c"), "SciDevHarness/provider/abc")
        self.assertTrue(credentials.credential_target("x" * 200).startswith("SciDevHarness/provider/"))

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager only")
    def test_round_trip_and_delete(self) -> None:
        profile = "test-" + uuid.uuid4().hex
        try:
            try:
                credentials.set_api_key("round-trip-secret", profile)
            except credentials.CredentialError as exc:
                self.skipTest(f"Credential Manager unavailable: {exc}")
            self.assertEqual(credentials.get_api_key(profile), "round-trip-secret")
        finally:
            try:
                credentials.delete_api_key(profile)
            except credentials.CredentialError:
                pass


if __name__ == "__main__":
    unittest.main()
