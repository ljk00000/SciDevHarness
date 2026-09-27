from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_core import (
    CodingAgent,
    CodingToolbox,
    EventLedger,
    GitManager,
    OpenAICompatibleProvider,
    RetryQueue,
    RetryableError,
    SummarySettings,
)


class FakeCodingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, _messages, tools=None, max_tokens=12000, request_id=""):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "我先创建代码文件。",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "hello.py", "content": "print('hello')\n"}),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "代码已完成并检查了修改。", "tool_calls": []}


class CoreTests(unittest.TestCase):
    def test_ledger_writes_event_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = EventLedger(root)
            ledger.append("test_event", {"value": 1})
            ledger.create_session("session_test", "写一个 hello 文件", ".research/sessions/session_test.json")
            self.assertIn("test_event", (root / ".research" / "events.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(ledger.list_sessions()[0]["session_id"], "session_test")

    def test_retry_queue_retries_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = EventLedger(Path(temp))
            attempts = []

            def handler(_task):
                attempts.append(1)
                if len(attempts) == 1:
                    raise RetryableError("temporary")
                return {"ok": True}

            queue = RetryQueue(ledger, {"test": handler}, retry_base_seconds=0.01)
            queue.start()
            task_id = queue.submit("test", {}, max_attempts=2)
            deadline = time.time() + 2
            rows = {}
            while time.time() < deadline:
                rows = {row["task_id"]: row for row in ledger.list_tasks()}
                if rows.get(task_id, {}).get("status") == "succeeded":
                    break
                time.sleep(0.02)
            queue.stop()
            self.assertEqual(len(attempts), 2)
            self.assertEqual(rows[task_id]["status"], "succeeded")

    def test_coding_agent_uses_tools_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            agent = CodingAgent(root, ledger, git)
            fake_provider = FakeCodingProvider()
            task = {"payload": {"session_id": "session_smoke", "prompt": "创建 hello.py"}}
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=fake_provider):
                result = agent.run(task)
            self.assertEqual(result["session_id"], "session_smoke")
            self.assertTrue((root / "hello.py").exists())
            self.assertTrue(result["git_result_sha"])
            self.assertTrue(result["summary"])
            self.assertTrue(result["summary_path"])
            summary_files = list((root / ".research" / "summaries").glob("*.json"))
            self.assertEqual(len(summary_files), 1)
            self.assertTrue(git.is_repo())
            events = (root / ".research" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("file_changed", events)
            self.assertIn("git_commit_created", events)
            self.assertIn("coding_session_completed", events)

    def test_summary_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = SummarySettings(
                enabled=False,
                model="summary-model",
                max_tokens=1200,
                context_chars=8000,
                retries=1,
                instruction="只保留实验结论",
            )
            settings.save(root)
            loaded = SummarySettings.load(root)
            self.assertFalse(loaded.enabled)
            self.assertEqual(loaded.model, "summary-model")
            self.assertEqual(loaded.max_tokens, 1200)
            self.assertEqual(loaded.context_chars, 8000)
            self.assertEqual(loaded.interval_turns, 4)
            self.assertEqual(loaded.instruction, "只保留实验结论")
            settings.config_path(root).write_text(json.dumps({"summary_enabled": "false"}), encoding="utf-8")
            self.assertFalse(SummarySettings.load(root).enabled)
            with patch.dict(os.environ, {"SCIDEV_SUMMARY_ENABLED": ""}, clear=False):
                self.assertFalse(SummarySettings.load(root).enabled)

    def test_summary_checkpoint_runs_every_configured_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            settings = SummarySettings(interval_turns=1)
            agent = CodingAgent(root, ledger, git, summary_settings=settings)
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=FakeCodingProvider()):
                agent.run({"payload": {"session_id": "session_checkpoint", "prompt": "创建 hello.py"}})
            records = [json.loads(path.read_text(encoding="utf-8")) for path in (root / ".research" / "summaries").glob("*.json")]
            self.assertEqual(len(records), 2)
            self.assertEqual({record["phase"] for record in records}, {"checkpoint", "final"})

    def test_git_diff_includes_tracked_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git = GitManager(root)
            tracked = root / "tracked.py"
            tracked.write_text("value = 1\n", encoding="utf-8")
            git.commit_changes("initial")
            tracked.write_text("value = 2\n", encoding="utf-8")
            self.assertIn("tracked.py", git.diff(tracked))
            git._run(["add", "tracked.py"], check=True)
            self.assertIn("tracked.py", git.diff(tracked))
            untracked = root / "new.py"
            untracked.write_text("print('new')\n", encoding="utf-8")
            self.assertIn("new.py", git.diff(untracked))
            self.assertTrue(any(entry["path"] == "new.py" for entry in git.status_entries()))
            git.stage_path(untracked)
            self.assertTrue(any(entry["path"] == "new.py" and entry["code"].startswith("A") for entry in git.status_entries()))
            git.unstage_path(untracked)
            self.assertTrue(any(entry["path"] == "new.py" and entry["code"] == "??" for entry in git.status_entries()))
            toolbox = CodingToolbox(root, EventLedger(root))
            whole_diff = toolbox.git_diff()
            self.assertIn("tracked.py", whole_diff)
            self.assertIn("new.py", whole_diff)

    def test_toolbox_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            toolbox = CodingToolbox(root, EventLedger(root))
            with self.assertRaises(Exception):
                toolbox.read_file("../outside.txt")

    def test_text_tool_call_fallback_only_accepts_declared_tools(self) -> None:
        provider = OpenAICompatibleProvider(
            "http://localhost:11434/v1",
            "ollama",
            "qwen-test",
            text_tool_call_fallback=True,
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                    },
                },
            }
        ]
        message = {
            "role": "assistant",
            "content": (
                "我会调用工具。\n"
                '```json\n{"name":"write_file","arguments":{"path":"hello.py","content":"print(1)"}}\n```\n'
                "```python\nprint(1)\n```"
            ),
        }
        normalized = provider._coerce_text_tool_calls(message, tools)
        self.assertEqual(normalized["tool_calls"][0]["function"]["name"], "write_file")
        self.assertEqual(
            json.loads(normalized["tool_calls"][0]["function"]["arguments"]),
            {"path": "hello.py", "content": "print(1)"},
        )
        self.assertNotIn('"name":"write_file"', normalized["content"])

        tagged_call = {
            "role": "assistant",
            "content": '<tool_call>{"name":"write_file","arguments":{"path":"hello.py","content":"print(2)"}}</tool_call>',
        }
        normalized_tagged = provider._coerce_text_tool_calls(tagged_call, tools)
        self.assertEqual(normalized_tagged["tool_calls"][0]["function"]["name"], "write_file")

        unknown_tool = {
            "role": "assistant",
            "content": '```json\n{"name":"delete_everything","arguments":{}}\n```',
        }
        self.assertNotIn("tool_calls", provider._coerce_text_tool_calls(unknown_tool, tools))

        missing_argument = {
            "role": "assistant",
            "content": '```json\n{"name":"write_file","arguments":{"path":"hello.py"}}\n```',
        }
        self.assertNotIn("tool_calls", provider._coerce_text_tool_calls(missing_argument, tools))

        no_arg_tools = [
            {
                "type": "function",
                "function": {
                    "name": "git_diff",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
        ]
        unexpected_argument = {
            "role": "assistant",
            "content": '```json\n{"name":"git_diff","arguments":{"command":"del *"}}\n```',
        }
        self.assertNotIn("tool_calls", provider._coerce_text_tool_calls(unexpected_argument, no_arg_tools))

    def test_git_probe_is_safe_when_git_cli_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            git = GitManager(Path(temp))
            with patch("scidev_core.subprocess.run", side_effect=FileNotFoundError("git not found")):
                self.assertFalse(git.is_repo())
                self.assertEqual(git.status(), "Git 不可用：请安装 Git CLI")


if __name__ == "__main__":
    unittest.main()
