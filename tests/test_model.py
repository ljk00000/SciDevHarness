from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

import scidev.agent.core
import scidev.runtime.errors
import scidev.integrations.model


class ModelModuleBoundaryTests(unittest.TestCase):
    def test_legacy_imports_preserve_provider_and_exception_identity(self) -> None:
        self.assertIs(scidev.agent.core.OpenAICompatibleProvider, scidev.integrations.model.OpenAICompatibleProvider)
        self.assertIs(scidev.agent.core.RetryableError, scidev.runtime.errors.RetryableError)
        self.assertIs(scidev.integrations.model.RetryableError, scidev.runtime.errors.RetryableError)
        error = scidev.agent.core.RetryableError("rate limited", retry_after_seconds=3.0)
        self.assertIsInstance(error, scidev.integrations.model.RetryableError)
        self.assertEqual(error.retry_after_seconds, 3.0)

    def test_model_can_be_imported_without_agent_or_ui(self) -> None:
        script = """
import json
import sys
import scidev.integrations.model

provider = scidev.integrations.model.OpenAICompatibleProvider("https://provider.invalid/v1", "test", "test")
print(json.dumps({
    "endpoint": provider._endpoint(),
    "unexpected_modules": [
        name for name in sys.modules
        if name in {"scidev.agent.core", "scidev.desktop.client"} or name.startswith("PySide6")
    ],
}))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["endpoint"], "https://provider.invalid/v1/chat/completions")
        self.assertEqual(report["unexpected_modules"], [])


if __name__ == "__main__":
    unittest.main()
