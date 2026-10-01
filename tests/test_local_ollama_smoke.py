from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from scripts.smoke_local_ollama import (
    BUGGY_FIRST_DUPLICATE_SOURCE,
    CODING_TASKS,
    EXPECTED_FILE_CONTENT,
    _execute_verified_sample,
    _coding_task_prompt,
    _record_local_request,
    _verify_coding_task,
    configure_local_environment,
    validate_local_base_url,
)


class LocalOllamaSmokeTests(unittest.TestCase):
    def test_small_coding_task_examples_execute_in_the_restricted_ast_runner(self) -> None:
        source_by_task = {
            "parity": "def is_even(value):\n    return value % 2 == 0\n",
            "filter-and-accumulate": (
                "def sum_positive(values):\n"
                "    total = 0\n"
                "    for value in values:\n"
                "        if value > 0:\n"
                "            total += value\n"
                "    return total\n"
            ),
            "boundary-conditions": (
                "def clamp(value, lower, upper):\n"
                "    if value < lower:\n"
                "        return lower\n"
                "    if value > upper:\n"
                "        return upper\n"
                "    return value\n"
            ),
            "bug-fix-first-duplicate": (
                "def first_duplicate(values):\n"
                "    seen = []\n"
                "    for value in values:\n"
                "        if value in seen:\n"
                "            return value\n"
                "        seen = seen + [value]\n"
                "    return None\n"
            ),
        }
        with tempfile.TemporaryDirectory(prefix="scidev-coding-cases-") as temporary:
            root = Path(temporary)
            for task in CODING_TASKS:
                with self.subTest(task=task.name):
                    target = root / task.filename
                    target.write_text(source_by_task[task.name], encoding="utf-8")
                    result = _verify_coding_task(target, task)
                    self.assertEqual(result["status"], "passed")
                    self.assertEqual(result["examples_passed"], len(task.examples))

    def test_bug_fix_fixture_fails_before_the_repair(self) -> None:
        task = next(task for task in CODING_TASKS if task.mode == "fix")
        with tempfile.TemporaryDirectory(prefix="scidev-bug-fix-fixture-") as temporary:
            target = Path(temporary) / task.filename
            target.write_text(BUGGY_FIRST_DUPLICATE_SOURCE, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "failed for"):
                _verify_coding_task(target, task)

    def test_bug_fix_prompt_spells_out_authorized_read_and_exact_replacement(self) -> None:
        task = next(task for task in CODING_TASKS if task.mode == "fix")
        prompt = _coding_task_prompt(task)
        self.assertIn("read_file", prompt)
        self.assertIn("replace_in_file", prompt)
        self.assertIn("行号只是显示标记", prompt)
        self.assertIn("不要询问或等待确认", prompt)

    def test_create_prompt_requires_immediate_authorized_tool_use(self) -> None:
        task = CODING_TASKS[0]
        prompt = _coding_task_prompt(task)
        self.assertIn("现在立即调用 write_file", prompt)
        self.assertIn("不要询问确认", prompt)
        self.assertIn("实际调用 git_diff", prompt)

    def test_unsafe_or_incorrect_generated_functions_are_rejected(self) -> None:
        task = next(task for task in CODING_TASKS if task.name == "parity")
        with tempfile.TemporaryDirectory(prefix="scidev-reject-generated-code-") as temporary:
            root = Path(temporary)
            target = root / task.filename
            target.write_text(
                'def is_even(value):\n    return print("AST_GUARD_BYPASSED")\n',
                encoding="utf-8",
            )
            with patch("builtins.exec") as execute:
                with self.assertRaisesRegex(RuntimeError, "unsupported/unsafe syntax"):
                    _verify_coding_task(target, task)
                execute.assert_not_called()

            accumulation = next(task for task in CODING_TASKS if task.name == "filter-and-accumulate")
            target = root / accumulation.filename
            target.write_text(
                "def sum_positive(values):\n"
                "    total = 0\n"
                "    for value in [1, 2, 3]:\n"
                "        total += value\n"
                "    return total\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "provided argument"):
                _verify_coding_task(target, accumulation)

            task = next(task for task in CODING_TASKS if task.name == "parity")
            target = root / task.filename
            target.write_text("def is_even(value):\n    return value % 2 != 0\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "failed for"):
                _verify_coding_task(target, task)

    def test_verified_sample_runs_and_returns_expected_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-verified-sample-") as temporary:
            root = Path(temporary)
            target = root / "hello_qwen.py"
            target.write_text(EXPECTED_FILE_CONTENT, encoding="utf-8")

            output = _execute_verified_sample(target, root)

            self.assertEqual(output, "Qwen2.5-Coder-7B works\n")

    def test_unverified_model_output_is_rejected_without_starting_a_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-unverified-sample-") as temporary:
            root = Path(temporary)
            target = root / "hello_qwen.py"
            target.write_text('raise SystemExit("untrusted output executed")\n', encoding="utf-8")
            with patch("scripts.smoke_local_ollama.subprocess.run") as execute:
                with self.assertRaisesRegex(RuntimeError, "exact requested Python file"):
                    _execute_verified_sample(target, root)

            execute.assert_not_called()

    def test_local_endpoint_accepts_only_loopback_http_urls(self) -> None:
        accepted = (
            "http://127.0.0.1:11434/v1/",
            "http://localhost/v1",
            "http://localhost:11434/v1",
            "http://[::1]:11434/v1",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(validate_local_base_url(url), url.rstrip("/"))

        rejected = (
            "https://127.0.0.1:11434/v1",
            "http://api.example.com/v1",
            "http://user:secret@127.0.0.1:11434/v1",
            "http://127.0.0.1:invalid/v1",
            "http://127.0.0.1:11434/v1?key=secret",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_local_base_url(url)

    def test_local_environment_bypasses_proxies_without_dropping_existing_rules(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": ".example.test,internal"}, clear=True):
            configure_local_environment("http://127.0.0.1:11434/v1", "local-model", "ollama")

            self.assertEqual(os.environ["SCIDEV_API_BASE"], "http://127.0.0.1:11434/v1")
            self.assertEqual(os.environ["SCIDEV_MODEL"], "local-model")
            self.assertEqual(os.environ["SCIDEV_REQUEST_TIMEOUT_SECONDS"], "240")
            self.assertEqual(os.environ["SCIDEV_STREAMING"], "1")
            self.assertEqual(os.environ["SCIDEV_TEXT_TOOL_CALL_FALLBACK"], "1")
            self.assertEqual(os.environ["SCIDEV_SUMMARY_ENABLED"], "1")
            self.assertIn(".example.test", os.environ["NO_PROXY"])
            for host in ("127.0.0.1", "localhost", "::1"):
                self.assertIn(host, os.environ["NO_PROXY"])
                self.assertIn(host, os.environ["no_proxy"])
            self.assertIn("internal", os.environ["no_proxy"])

    def test_local_environment_preserves_explicit_request_timeout(self) -> None:
        with patch.dict(os.environ, {"SCIDEV_REQUEST_TIMEOUT_SECONDS": "360"}, clear=True):
            configure_local_environment("http://127.0.0.1:11434/v1", "local-model", "ollama")
            self.assertEqual(os.environ["SCIDEV_REQUEST_TIMEOUT_SECONDS"], "360")

    def test_qwen_launcher_sets_process_scoped_streaming_and_timeout_defaults(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "start_qwen_local.bat"
        source = launcher.read_text(encoding="utf-8")
        self.assertIn('if not defined SCIDEV_REQUEST_TIMEOUT_SECONDS set "SCIDEV_REQUEST_TIMEOUT_SECONDS=240"', source)
        self.assertIn('if not defined SCIDEV_STREAMING set "SCIDEV_STREAMING=1"', source)
        self.assertIn("setlocal", source.casefold())

    def test_wire_guard_rejects_remote_requests_and_small_token_budgets(self) -> None:
        requests: list[dict] = []
        body = json.dumps({"model": "local-model", "max_tokens": 12_000, "tools": []}).encode()
        local_request = Request("http://127.0.0.1:11434/v1/chat/completions", data=body)
        _record_local_request(local_request, model="local-model", requests=requests)
        self.assertEqual(requests[0]["max_tokens"], 12_000)

        remote_request = Request("https://api.example.com/v1/chat/completions", data=body)
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            _record_local_request(remote_request, model="local-model", requests=requests)

        small_body = json.dumps({"model": "local-model", "max_tokens": 2048}).encode()
        small_request = Request("http://127.0.0.1:11434/v1/chat/completions", data=small_body)
        with self.assertRaisesRegex(RuntimeError, "10000"):
            _record_local_request(small_request, model="local-model", requests=requests)


if __name__ == "__main__":
    unittest.main()
