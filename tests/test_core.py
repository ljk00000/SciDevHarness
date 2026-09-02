from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_core import CodingAgent, CodingToolbox, EventLedger, GitManager, RetryQueue, RetryableError


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
            self.assertTrue(git.is_repo())
            events = (root / ".research" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("file_changed", events)
            self.assertIn("git_commit_created", events)
            self.assertIn("coding_session_completed", events)

    def test_toolbox_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            toolbox = CodingToolbox(root, EventLedger(root))
            with self.assertRaises(Exception):
                toolbox.read_file("../outside.txt")


if __name__ == "__main__":
    unittest.main()
