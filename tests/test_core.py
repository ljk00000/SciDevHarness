from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from scidev_core import (
    CodingAgent,
    CodingToolbox,
    EventLedger,
    GitManager,
    OpenAICompatibleProvider,
    PermanentError,
    RetryQueue,
    RetryableError,
    SummarySettings,
)


class FakeCodingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.message_histories = []

    def chat(self, _messages, tools=None, max_tokens=12000, request_id=""):
        self.calls += 1
        self.message_histories.append([dict(message) for message in _messages])
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

    def test_retry_queue_start_is_idempotent_and_restartable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            queue = RetryQueue(EventLedger(Path(temp)), {})
            threads = []
            try:
                queue.start()
                first_thread = queue._thread
                if first_thread:
                    threads.append(first_thread)
                queue.start()
                self.assertIs(queue._thread, first_thread)
                self.assertTrue(first_thread and first_thread.is_alive())

                queue.stop()
                self.assertFalse(first_thread and first_thread.is_alive())

                queue.start()
                restarted_thread = queue._thread
                if restarted_thread and restarted_thread not in threads:
                    threads.append(restarted_thread)
                self.assertIsNot(restarted_thread, first_thread)
                self.assertTrue(restarted_thread and restarted_thread.is_alive())
                queue.stop()
                self.assertFalse(restarted_thread and restarted_thread.is_alive())
            finally:
                queue.stop()
                for thread in threads:
                    thread.join(timeout=2)

    def test_retry_queue_reports_restart_while_previous_worker_is_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = EventLedger(Path(temp))
            handler_entered = threading.Event()
            release_handler = threading.Event()
            calls = []

            def handler(_task):
                calls.append(True)
                if len(calls) == 1:
                    handler_entered.set()
                    if not release_handler.wait(timeout=3):
                        raise RuntimeError("test handler timed out")

            queue = RetryQueue(ledger, {"test": handler}, retry_base_seconds=0.01)
            queue.start()
            first_task = queue.submit("test", {}, max_attempts=1)
            self.assertTrue(handler_entered.wait(timeout=2))
            old_worker = queue._thread
            self.assertIsNotNone(old_worker)

            try:
                with patch.object(old_worker, "join"):
                    queue.stop()
                with self.assertRaisesRegex(RuntimeError, "仍在停止"):
                    queue.start()
            finally:
                release_handler.set()
                old_worker.join(timeout=2)

            self.assertFalse(old_worker.is_alive())
            queue.start()
            second_task = queue.submit("test", {}, max_attempts=1)
            deadline = time.time() + 2
            rows = {}
            while time.time() < deadline:
                rows = {row["task_id"]: row for row in ledger.list_tasks()}
                if rows.get(second_task, {}).get("status") == "succeeded":
                    break
                time.sleep(0.02)
            queue.stop()
            self.assertEqual(rows[first_task]["status"], "succeeded")
            self.assertEqual(rows[second_task]["status"], "succeeded")
            self.assertEqual(len(calls), 2)

    def test_retry_queue_recovers_interrupted_coding_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = EventLedger(Path(temp))
            task_id = ledger.create_task("coding", {"session_id": "interrupted"}, max_attempts=2)
            claimed = ledger.claim_due_task()
            self.assertEqual(claimed and claimed["task_id"], task_id)
            self.assertEqual(ledger.list_tasks()[0]["status"], "running")
            attempts = []
            queue = RetryQueue(ledger, {"coding": lambda task: attempts.append(task["attempts"])})
            try:
                queue.start()
                deadline = time.time() + 2
                row = None
                while time.time() < deadline:
                    row = next((item for item in ledger.list_tasks() if item["task_id"] == task_id), None)
                    if row and row["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.02)
                self.assertEqual(row and row["status"], "succeeded")
                self.assertEqual(attempts, [2])
            finally:
                queue.stop()

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
            prior_tool_messages = [
                message
                for message in fake_provider.message_histories[1]
                if message.get("role") == "assistant" and message.get("tool_calls")
            ]
            self.assertEqual(len(prior_tool_messages), 1)
            self.assertEqual(prior_tool_messages[0]["content"], "")
            self.assertEqual(result["session_id"], "session_smoke")
            self.assertTrue((root / "hello.py").exists())
            self.assertTrue(result["git_result_sha"])
            self.assertTrue(result["summary"])
            self.assertTrue(result["summary_path"])
            summary_files = list((root / ".research" / "summaries").glob("*.json"))
            self.assertEqual(len(summary_files), 1)
            self.assertTrue(git.is_repo())
            events = [
                json.loads(line)
                for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            event_types = [event["event_type"] for event in events]
            self.assertIn("file_changed", event_types)
            self.assertIn("git_commit_created", event_types)
            self.assertIn("coding_session_completed", event_types)
            diff_results = [
                index
                for index, event in enumerate(events)
                if event["event_type"] == "tool_result"
                and event["payload"].get("name") == "git_diff"
                and event["payload"].get("source") == "harness_precommit"
            ]
            self.assertEqual(len(diff_results), 1)
            self.assertLess(diff_results[0], event_types.index("git_commit_created"))

    def test_precommit_diff_is_rechecked_after_a_later_agent_edit(self) -> None:
        class EditAfterDiffProvider:
            def __init__(self) -> None:
                self.calls = 0

            @staticmethod
            def _tool_call(name: str, arguments: dict[str, str]) -> dict:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{name}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }

            def chat(self, _messages, tools=None, max_tokens=12000, request_id=""):
                self.calls += 1
                if self.calls == 1:
                    return self._tool_call("write_file", {"path": "hello.py", "content": "value = 1\n"})
                if self.calls == 2:
                    return self._tool_call("git_diff", {})
                if self.calls == 3:
                    return self._tool_call("write_file", {"path": "hello.py", "content": "value = 2\n"})
                return {"role": "assistant", "content": "已根据最终差异完成。"}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            git.commit_changes("baseline")
            provider = EditAfterDiffProvider()
            agent = CodingAgent(root, ledger, git, summary_settings=SummarySettings(enabled=False))
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=provider):
                result = agent.run({"payload": {"session_id": "session_stale_diff", "prompt": "修改 hello.py"}})

            self.assertEqual((root / "hello.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertTrue(result["git_result_sha"])
            self.assertEqual(provider.calls, 5)
            events = [
                json.loads(line)
                for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            diff_results = [
                (index, event["payload"].get("source"))
                for index, event in enumerate(events)
                if event["event_type"] == "tool_result" and event["payload"].get("name") == "git_diff"
            ]
            self.assertEqual(len(diff_results), 2)
            self.assertEqual(diff_results[-1][1], "harness_precommit")
            self.assertLess(diff_results[-1][0], [event["event_type"] for event in events].index("git_commit_created"))

    def test_coding_agent_does_not_commit_when_precommit_diff_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            git.commit_changes("baseline")
            baseline_sha = git.head_sha()
            agent_events = []
            agent = CodingAgent(
                root,
                ledger,
                git,
                event_callback=lambda name, payload: agent_events.append((name, payload)),
                summary_settings=SummarySettings(enabled=False),
            )
            provider = FakeCodingProvider()
            execute = agent.toolbox.execute

            def fail_final_diff(name: str, arguments: dict[str, str]) -> str:
                if name == "git_diff":
                    raise OSError("simulated diff failure")
                return execute(name, arguments)

            task = {"payload": {"session_id": "session_diff_failure", "prompt": "创建 hello.py"}}
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=provider):
                with patch.object(agent.toolbox, "execute", side_effect=fail_final_diff):
                    with self.assertRaisesRegex(PermanentError, "提交前 Git diff 检查失败"):
                        agent.run(task)

            self.assertTrue((root / "hello.py").is_file())
            self.assertEqual(git.head_sha(), baseline_sha)
            session = json.loads(
                (root / ".research" / "sessions" / "session_diff_failure.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session["status"], "failed")
            ledger_events = [
                json.loads(line)
                for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertNotIn("git_commit_created", [event["event_type"] for event in ledger_events])
            self.assertTrue(
                any(
                    event["event_type"] == "tool_result"
                    and event["payload"].get("source") == "harness_precommit"
                    and "simulated diff failure" in event["payload"].get("error", "")
                    for event in ledger_events
                )
            )
            self.assertTrue(
                any(
                    name == "tool_result"
                    and payload.get("source") == "harness_precommit"
                    and "simulated diff failure" in payload.get("result", "")
                    for name, payload in agent_events
                )
            )

    def test_agent_auto_commit_preserves_preexisting_staged_and_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            dirty = root / "user_dirty.py"
            staged = root / "user_staged.py"
            dirty.write_text("value = 1\n", encoding="utf-8")
            staged.write_text("value = 1\n", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            git.commit_changes("baseline")

            dirty.write_text("value = 2\n", encoding="utf-8")
            staged.write_text("value = 2\n", encoding="utf-8")
            git.stage_path(staged)
            staged.write_text("value = 3\n", encoding="utf-8")
            agent = CodingAgent(root, ledger, git)
            task = {"payload": {"session_id": "session_isolated_commit", "prompt": "创建 hello.py"}}
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=FakeCodingProvider()):
                result = agent.run(task)

            committed_paths = set(
                git._run(["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]).stdout.splitlines()
            )
            self.assertEqual(committed_paths, {"hello.py"})
            status = {entry["path"]: entry["code"] for entry in git.status_entries()}
            self.assertEqual(status["user_dirty.py"], " M")
            self.assertEqual(status["user_staged.py"], "MM")
            self.assertEqual((root / "user_dirty.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((root / "user_staged.py").read_text(encoding="utf-8"), "value = 3\n")
            self.assertEqual(result["git_result_sha"], git.head_sha())

    def test_git_scoped_commit_handles_unicode_and_spaced_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git = GitManager(root)
            target = root / "科研代码" / "analysis draft.py"
            target.parent.mkdir()
            target.write_text("value = 1\n", encoding="utf-8")
            git.commit_changes("baseline")
            target.write_text("value = 2\n", encoding="utf-8")
            relative = target.relative_to(root).as_posix()

            self.assertEqual(git.status_paths(), {relative})
            commit = git.commit_paths("commit unicode path", {relative})

            self.assertTrue(commit)
            changed = set(
                filter(
                    None,
                    git._run(["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD"]).stdout.split("\x00"),
                )
            )
            self.assertEqual(changed, {relative})
            self.assertFalse(git.status_paths())

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

    def test_agent_prompt_requires_explicit_checks_to_be_run_before_completion(self) -> None:
        self.assertIn("用户明确要求的工具调用或检查", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("依据工具结果汇报", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("工具 JSON 文本当作已发生的结果", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("文件读取和编辑可直接执行", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("不要只提出方案或在执行前二次询问", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("立即选用清楚的文件名并在工作区根目录调用 write_file", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("只有任务依赖现有文件时才调用 list_files", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("Do not use shell commands or downloads for a simple SVG/artwork", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("two well-separated wheels joined by a clear frame", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("a pelican has a long bill and throat pouch", CodingAgent.SYSTEM_PROMPT)
        self.assertIn("distinct body, head, visible eye, wing, and beak", CodingAgent.SYSTEM_PROMPT)

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

    def test_agent_git_diff_hides_sensitive_tracked_and_untracked_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            (root / "config").mkdir()
            env_file = root / ".env"
            env_file.write_text("TOKEN=UNTRACKED_SECRET_SENTINEL\n", encoding="utf-8")
            production_env = root / "config" / ".env.production"
            production_env.write_text("TOKEN=UNTRACKED_PRODUCTION_SENTINEL\n", encoding="utf-8")
            credentials = root / "credentials.json"
            credentials.write_text('{"token":"UNTRACKED_CREDENTIAL_SENTINEL"}\n', encoding="utf-8")
            template = root / ".env.example"
            template.write_text("TOKEN=replace-me\n", encoding="utf-8")

            ledger = EventLedger(root)
            git = GitManager(root)
            git.init()
            toolbox = CodingToolbox(root, ledger)
            untracked_diff = toolbox.git_diff()
            for sentinel in (
                "UNTRACKED_SECRET_SENTINEL",
                "UNTRACKED_PRODUCTION_SENTINEL",
                "UNTRACKED_CREDENTIAL_SENTINEL",
            ):
                self.assertNotIn(sentinel, untracked_diff)
            self.assertIn("TOKEN=replace-me", untracked_diff)

            git.commit_changes("test-only baseline")
            env_file.write_text("TOKEN=TRACKED_SECRET_SENTINEL\n", encoding="utf-8")
            production_env.write_text("TOKEN=TRACKED_PRODUCTION_SENTINEL\n", encoding="utf-8")
            credentials.write_text('{"token":"TRACKED_CREDENTIAL_SENTINEL"}\n', encoding="utf-8")
            template.write_text("TOKEN=replace-me-later\n", encoding="utf-8")

            tracked_diff = toolbox.git_diff()
            for sentinel in (
                "TRACKED_SECRET_SENTINEL",
                "TRACKED_PRODUCTION_SENTINEL",
                "TRACKED_CREDENTIAL_SENTINEL",
            ):
                self.assertNotIn(sentinel, tracked_diff)
            self.assertIn("TOKEN=replace-me-later", tracked_diff)
            self.assertIn("UNTRACKED_SECRET_SENTINEL", git.diff())

    def test_agent_git_diff_and_auto_commit_hide_sensitive_renames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            secret = root / ".env"
            secret.write_text("TOKEN=RENAMED_SECRET_SENTINEL\n", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            git.init()
            git.commit_changes("test-only baseline")

            secret.rename(root / "config.txt")
            status_paths = git.status_paths()
            self.assertEqual(status_paths, {".env", "config.txt"})
            safe_diff = CodingToolbox(root, ledger).git_diff()
            self.assertNotIn("RENAMED_SECRET_SENTINEL", safe_diff)
            self.assertIn("敏感/内部路径的差异已隐藏", safe_diff)

            safe_paths, skipped_paths = git.filter_auto_commit_paths(status_paths)
            self.assertEqual(safe_paths, set())
            self.assertEqual(set(skipped_paths), {".env", "config.txt"})

    def test_auto_commit_path_filter_blocks_secrets_internal_paths_and_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "hello.py").write_text("print('hello')\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=test-only\n", encoding="utf-8")
            (root / ".env.example").write_text("TOKEN=replace-me\n", encoding="utf-8")
            (root / "credentials.json").write_text("{}\n", encoding="utf-8")
            internal = root / ".research" / "sessions"
            internal.mkdir(parents=True)
            (internal / "session.json").write_text("{}\n", encoding="utf-8")
            artifact = root / "outputs" / "model.bin"
            artifact.parent.mkdir()
            artifact.write_bytes(b"x" * 33)
            binary = root / "outputs" / "tiny.bin"
            binary.write_bytes(b"\x00\x01\x02")
            git = GitManager(root)
            with patch.object(GitManager, "MAX_AUTO_COMMIT_FILE_BYTES", 32):
                safe_paths, skipped_paths = git.filter_auto_commit_paths(
                    {"hello.py", ".env", ".env.example", "credentials.json", ".research/sessions/session.json", "outputs/model.bin", "outputs/tiny.bin"}
                )

            self.assertEqual(safe_paths, {"hello.py", ".env.example"})
            self.assertEqual(skipped_paths[".env"]["reason"], "protected_path")
            self.assertEqual(skipped_paths["credentials.json"]["reason"], "protected_path")
            self.assertEqual(skipped_paths[".research/sessions/session.json"]["reason"], "protected_path")
            self.assertEqual(skipped_paths["outputs/model.bin"], {"reason": "file_too_large", "size_bytes": 33})
            self.assertEqual(skipped_paths["outputs/tiny.bin"]["reason"], "non_text_file")

    def test_agent_auto_commit_leaves_large_artifacts_uncommitted_and_reports_them(self) -> None:
        class ArtifactProvider:
            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, tools=None, max_tokens=12000, request_id=""):
                self.calls += 1
                if self.calls == 1:
                    calls = []
                    for call_id, path, content in (
                        ("call_code", "hello.py", "print('hello')\\n"),
                        ("call_artifact", "outputs/model.bin", "x" * 17),
                    ):
                        calls.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps({"path": path, "content": content}),
                                },
                            }
                        )
                    return {"role": "assistant", "content": "我已创建代码和输出文件。", "tool_calls": calls}
                return {"role": "assistant", "content": "代码已完成并检查了差异。", "tool_calls": []}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            ui_events = []
            agent = CodingAgent(
                root,
                ledger,
                git,
                event_callback=lambda event, data: ui_events.append((event, data)),
                summary_settings=SummarySettings(enabled=False),
            )
            provider = ArtifactProvider()
            with patch.object(GitManager, "MAX_AUTO_COMMIT_FILE_BYTES", 16):
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=provider):
                    result = agent.run({"payload": {"session_id": "large_artifact", "prompt": "创建代码"}})

            committed = set(filter(None, git._run(["show", "--pretty=format:", "--name-only", "HEAD"]).stdout.splitlines()))
            self.assertEqual(committed, {"hello.py"})
            self.assertIn("outputs/model.bin", git.status_paths())
            self.assertEqual(result["git_auto_commit_skipped_paths"]["outputs/model.bin"]["size_bytes"], 17)
            self.assertTrue(any(event == "git_auto_commit_skipped_paths" for event, _data in ui_events))
            events = [json.loads(line) for line in ledger.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(event["event_type"] == "git_auto_commit_skipped_paths" for event in events))

    def test_toolbox_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            toolbox = CodingToolbox(root, EventLedger(root))
            with self.assertRaises(Exception):
                toolbox.read_file("../outside.txt")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior is platform-specific")
    def test_toolbox_hides_and_rejects_external_windows_junctions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-junction-") as temp:
            root = Path(temp) / "project"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            secret = outside / "private_sentinel.txt"
            secret.write_text("outside workspace\n", encoding="utf-8")
            junction = root / "linked_data"
            result = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue(junction.is_junction())
            toolbox = CodingToolbox(root, EventLedger(root))
            try:
                listing = toolbox.list_files(".", depth=4)
                self.assertNotIn("linked_data", listing)
                self.assertNotIn("private_sentinel.txt", listing)
                with self.assertRaises(PermanentError):
                    toolbox.read_file("linked_data/private_sentinel.txt")
                with self.assertRaises(PermanentError):
                    toolbox.write_file("linked_data/created.py", "print('outside')\n")
                self.assertFalse((outside / "created.py").exists())
            finally:
                junction.rmdir()

    def test_toolbox_hides_sensitive_paths_and_allows_env_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("TOKEN=private\n", encoding="utf-8")
            (root / ".env.example").write_text("TOKEN=replace-me\n", encoding="utf-8")
            (root / ".npmrc").write_text("//registry.example/:_authToken=private\n", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / ".env.production").write_text("TOKEN=private\n", encoding="utf-8")
            (root / "secrets").mkdir()
            (root / "secrets" / "credentials.json").write_text("{}\n", encoding="utf-8")
            toolbox = CodingToolbox(root, EventLedger(root))

            listing = toolbox.list_files(".", depth=4)
            listed_names = {line.strip().split("  [", 1)[0].rstrip("/") for line in listing.splitlines()}
            self.assertIn(".env.example", listed_names)
            for secret_name in (".env", ".npmrc", ".env.production", "secrets"):
                self.assertNotIn(secret_name, listed_names)
            self.assertIn("replace-me", toolbox.read_file(".env.example"))
            for secret_path in (".env", ".npmrc", "config/.env.production", "secrets/credentials.json"):
                with self.subTest(path=secret_path), self.assertRaises(PermanentError):
                    toolbox.read_file(secret_path)
            with self.assertRaises(PermanentError):
                toolbox.write_file(".env.production", "TOKEN=overwrite\n")

    def test_replace_in_file_rejects_empty_needle_and_binary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            text_file = root / "source.py"
            text_file.write_text("value = 1\n", encoding="utf-8")
            binary_file = root / "image.png"
            binary_content = b"\x89PNG\r\n\x1a\n\x00binary"
            binary_file.write_bytes(binary_content)
            invalid_utf8_file = root / "legacy.dat"
            invalid_utf8_content = b"\xfflegacy bytes"
            invalid_utf8_file.write_bytes(invalid_utf8_content)
            toolbox = CodingToolbox(root, EventLedger(root))

            with self.assertRaises(PermanentError):
                toolbox.replace_in_file("source.py", "", "unexpected")
            self.assertEqual(text_file.read_text(encoding="utf-8"), "value = 1\n")

            for replacement in ("contains\x00nul", "x" * (toolbox.MAX_WRITE_BYTES + 1)):
                with self.subTest(replacement="NUL" if "\x00" in replacement else "oversized"):
                    with self.assertRaises(PermanentError):
                        toolbox.replace_in_file("source.py", "value = 1", replacement)
                    self.assertEqual(text_file.read_text(encoding="utf-8"), "value = 1\n")

            with self.assertRaises(PermanentError):
                toolbox.replace_in_file("image.png", "\ufffd", "corrupted")
            self.assertEqual(binary_file.read_bytes(), binary_content)

            with self.assertRaises(PermanentError):
                toolbox.replace_in_file("legacy.dat", "\ufffd", "corrupted")
            self.assertEqual(invalid_utf8_file.read_bytes(), invalid_utf8_content)

            for binary_path, original in (
                ("image.png", binary_content),
                ("legacy.dat", invalid_utf8_content),
            ):
                with self.subTest(path=binary_path), self.assertRaises(PermanentError):
                    toolbox.write_file(binary_path, "replacement text\n")
                self.assertEqual((root / binary_path).read_bytes(), original)

            with self.assertRaises(PermanentError):
                toolbox.write_file("contains_nul.txt", "text\x00not-safe")
            self.assertFalse((root / "contains_nul.txt").exists())

    def test_replace_in_file_accepts_lf_snippet_in_crlf_file_and_preserves_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "source.py"
            target.write_bytes(b"def answer():\r\n    return 0\r\n")
            toolbox = CodingToolbox(root, EventLedger(root))

            toolbox.replace_in_file(
                "source.py",
                "    return 0\n",
                "    return None\n",
            )

            self.assertEqual(target.read_bytes(), b"def answer():\r\n    return None\r\n")

    def test_run_command_requires_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = EventLedger(root)
            toolbox = CodingToolbox(root, ledger)
            with patch("scidev_core.subprocess.Popen") as process:
                with self.assertRaises(PermanentError):
                    toolbox.run_command("python -c \"print('must not run')\"")
            process.assert_not_called()
            events = [json.loads(line) for line in ledger.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertIn("command_approval_requested", [event["event_type"] for event in events])
            self.assertIn("command_approval_denied", [event["event_type"] for event in events])

    def test_run_command_executes_only_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = EventLedger(root)
            approvals = []
            toolbox = CodingToolbox(
                root,
                ledger,
                command_approval=lambda command, timeout: approvals.append((command, timeout)) or True,
            )
            process_result = type("ProcessResult", (), {"returncode": 0, "communicate": lambda self, timeout: ("ok", None)})()
            with patch("scidev_core.subprocess.Popen", return_value=process_result) as process:
                result = toolbox.run_command("python -V", timeout_seconds=400)
            process.assert_called_once()
            self.assertEqual(approvals, [("python -V", 300)])
            self.assertIn("退出码：0", result)
            events = [json.loads(line) for line in ledger.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertIn("command_approval_granted", [event["event_type"] for event in events])

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

        redundant_brace_call = {
            "role": "assistant",
            "content": (
                '```xml\n{"name":"write_file","arguments":{"path":"hello.py",'
                '"content":"print(2)"}}}\n```'
            ),
        }
        normalized_redundant_brace = provider._coerce_text_tool_calls(redundant_brace_call, tools)
        self.assertEqual(normalized_redundant_brace["tool_calls"][0]["function"]["name"], "write_file")

        multiple_extra_braces = {
            "role": "assistant",
            "content": (
                '```json\n{"name":"write_file","arguments":{"path":"hello.py",'
                '"content":"print(2)"}}}}\n```'
            ),
        }
        self.assertNotIn("tool_calls", provider._coerce_text_tool_calls(multiple_extra_braces, tools))

        tagged_request = {
            "role": "assistant",
            "content": '<tool_request>{"name":"write_file","arguments":{"path":"hello.py","content":"print(2)"}}</tool_request>',
        }
        normalized_request = provider._coerce_text_tool_calls(tagged_request, tools)
        self.assertEqual(normalized_request["tool_calls"][0]["function"]["name"], "write_file")
        self.assertEqual(normalized_request["content"], "")

        unwrapped_call = {
            "role": "assistant",
            "content": json.dumps(
                {"name": "write_file", "arguments": {"path": "hello.py", "content": "print(3)"}},
            ),
        }
        normalized_unwrapped = provider._coerce_text_tool_calls(unwrapped_call, tools)
        self.assertEqual(normalized_unwrapped["tool_calls"][0]["function"]["name"], "write_file")
        self.assertEqual(
            json.loads(normalized_unwrapped["tool_calls"][0]["function"]["arguments"]),
            {"path": "hello.py", "content": "print(3)"},
        )
        self.assertEqual(normalized_unwrapped["content"], "")

        prose_json = {
            "role": "assistant",
            "content": 'Example: {"name":"write_file","arguments":{"path":"hello.py","content":"print(4)"}}',
        }
        self.assertNotIn("tool_calls", provider._coerce_text_tool_calls(prose_json, tools))

        extra_envelope_field = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "name": "write_file",
                    "arguments": {"path": "hello.py", "content": "print(5)"},
                    "execute_immediately": True,
                },
            ),
        }
        self.assertNotIn("tool_calls", provider._coerce_text_tool_calls(extra_envelope_field, tools))

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

    def test_bare_json_tool_call_is_parsed_only_when_opted_in(self) -> None:
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
        raw_call = json.dumps({"name": "write_file", "arguments": {"path": "hello.py", "content": "print(1)"}})
        response_body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": raw_call}}]},
        ).encode("utf-8")
        messages = [{"role": "user", "content": "Create a file"}]
        enabled = OpenAICompatibleProvider(
            "http://localhost:11434/v1", "ollama", "qwen-test", text_tool_call_fallback=True
        )
        with patch("scidev_core.urlopen", return_value=BytesIO(response_body)) as opener:
            parsed = enabled.chat(messages, tools=tools, max_tokens=12000)
        self.assertEqual(parsed["tool_calls"][0]["function"]["name"], "write_file")
        request_body = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(request_body["max_tokens"], 12000)
        self.assertTrue(request_body["messages"][0]["content"].startswith("Local tool compatibility mode:"))

        disabled = OpenAICompatibleProvider("http://localhost:11434/v1", "ollama", "qwen-test")
        with patch("scidev_core.urlopen", return_value=BytesIO(response_body)):
            untouched = disabled.chat(messages, tools=tools, max_tokens=12000)
        self.assertEqual(untouched["content"], raw_call)
        self.assertNotIn("tool_calls", untouched)

    def test_git_probe_is_safe_when_git_cli_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            git = GitManager(Path(temp))
            with patch("scidev_core.subprocess.run", side_effect=FileNotFoundError("git not found")):
                self.assertFalse(git.is_repo())
                self.assertEqual(git.status(), "Git 不可用：请安装 Git CLI")


if __name__ == "__main__":
    unittest.main()
