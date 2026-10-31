from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scidev_cli
from scidev_core import PermanentError


class _FakeAgent:
    instances: list["_FakeAgent"] = []

    def __init__(self, root, ledger, git, **kwargs):
        self.root = Path(root)
        self.kwargs = kwargs
        self.payload = None
        self.__class__.instances.append(self)

    def run(self, payload):
        self.payload = payload
        return {
            "session_id": payload["payload"]["session_id"],
            "text": "implemented",
            "git_result_sha": "abc123",
            "verification_report": {"status": "passed"},
        }

    def restore_last_checkpoint(self):
        return {"restored": ["app.py"], "deleted": [], "failed": {}}


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeAgent.instances.clear()

    def _run(self, argv, fake=_FakeAgent):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            argv = [item.replace("{root}", str(root)) for item in argv]
            with (
                patch("scidev_cli.CodingAgent", fake),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                code = scidev_cli.main(argv)
            return code, output.getvalue(), root

    def test_exec_json_uses_policy_and_reports_result(self) -> None:
        code, output, _root = self._run([
            "exec", "--workspace", "{root}", "--task", "fix it", "--json",
            "--execution-mode", "read-only",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["text"], "implemented")
        self.assertEqual(payload["git_result_sha"], "abc123")
        agent = _FakeAgent.instances[-1]
        self.assertEqual(agent.kwargs["summary_settings"].execution_mode, "read-only")
        self.assertIsNone(agent.kwargs["command_approval"])
        self.assertFalse(agent.kwargs["prefer_environment_provider"])
        self.assertEqual(agent.payload["payload"]["prompt"], "fix it")

    def test_pr_dry_run_prints_a_plan_without_network_calls(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="scidev-cli-pr-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
                cwd=root, check=True,
            )
            subprocess.run(["git", "checkout", "-q", "-b", "feature/task"], cwd=root, check=True)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = scidev_cli.main([
                    "pr", "--workspace", str(root), "--title", "Task", "--draft", "--dry-run", "--json",
                ])
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["repo"], "example/project")
            self.assertEqual(payload["branch"], "feature/task")
            self.assertTrue(payload["draft"])

    def test_exec_can_run_in_an_isolated_worktree(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="scidev-cli-worktree-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            _FakeAgent.instances.clear()
            with (
                patch("scidev_cli.CodingAgent", _FakeAgent),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                code = scidev_cli.main([
                    "exec", "--workspace", str(root), "--task", "fix it",
                    "--worktree", "--worktree-id", "review", "--json",
                ])
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["worktree_branch"], "scidev/agent-review")
            self.assertTrue(Path(payload["worktree"]).is_dir())
            self.assertEqual(_FakeAgent.instances[-1].root, Path(payload["worktree"]).resolve())

    def test_eval_rejects_a_missing_suite(self) -> None:
        code, output, _root = self._run(["eval", "--suite", "{root}/missing.json"])
        self.assertEqual(code, 2)
        self.assertIn("suite not found", output)

    def test_invalid_image_is_rejected_before_starting_the_agent(self) -> None:
        code, output, _root = self._run([
            "exec", "--workspace", "{root}", "--task", "look",
            "--image", "{root}/missing.png",
        ])
        self.assertEqual(code, 2)
        self.assertIn("image rejected", output)

    def test_no_verify_disables_verification_requirement(self) -> None:
        code, _output, _root = self._run([
            "exec", "--workspace", "{root}", "--task", "explain", "--json", "--no-verify",
        ])
        self.assertEqual(code, 0)
        self.assertFalse(_FakeAgent.instances[-1].kwargs["require_verification"])

    def test_permanent_error_becomes_json_failure(self) -> None:
        class FailingAgent(_FakeAgent):
            def run(self, payload):
                raise PermanentError("provider unavailable")

        code, output, _root = self._run([
            "exec", "--workspace", "{root}", "--task", "fix", "--json",
        ], fake=FailingAgent)
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["success"])
        self.assertIn("provider unavailable", payload["error"])

    def test_restore_on_failure_reports_checkpoint_result(self) -> None:
        class FailingAgent(_FakeAgent):
            def run(self, payload):
                raise PermanentError("task failed")

        code, output, _root = self._run([
            "exec", "--workspace", "{root}", "--task", "fix", "--json", "--restore-on-failure",
        ], fake=FailingAgent)
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["checkpoint_restore"]["restored"], ["app.py"])

    def test_restore_on_failure_is_not_called_for_success(self) -> None:
        code, output, _root = self._run([
            "exec", "--workspace", "{root}", "--task", "fix", "--json", "--restore-on-failure",
        ])
        self.assertEqual(code, 0)
        self.assertNotIn("checkpoint_restore", json.loads(output))

    def test_missing_workspace_is_a_usage_error(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = scidev_cli.main([
                "exec", "--workspace", "Z:\\definitely-not-a-workspace", "--task", "x",
            ])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", output.getvalue())

    def test_explicit_provider_flags_set_environment_for_the_run(self) -> None:
        code, _output, _root = self._run([
            "exec", "--workspace", "{root}", "--task", "fix", "--json",
            "--api-base", "http://127.0.0.1:11434/v1",
            "--api-key", "test-key",
            "--model", "test-model",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(os.environ["SCIDEV_API_BASE"], "http://127.0.0.1:11434/v1")
        self.assertEqual(os.environ["SCIDEV_API_KEY"], "test-key")
        self.assertEqual(os.environ["SCIDEV_MODEL"], "test-model")
        self.assertTrue(_FakeAgent.instances[-1].kwargs["prefer_environment_provider"])

    def test_parser_help_without_command(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = scidev_cli.main([])
        self.assertEqual(code, 2)
        self.assertIn("exec", output.getvalue())

    def test_sessions_json_lists_recent_sessions(self) -> None:
        from scidev_core import EventLedger

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            EventLedger(root).create_session(
                "session_cli_resume",
                "修复登录",
                str(root / ".research" / "sessions" / "session_cli_resume.json"),
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = scidev_cli.main([
                    "sessions", "--workspace", str(root), "--json", "--limit", "5",
                ])
            self.assertEqual(code, 0)
            records = json.loads(output.getvalue())
            self.assertEqual(records[0]["session_id"], "session_cli_resume")
            self.assertEqual(records[0]["prompt"], "修复登录")


if __name__ == "__main__":
    unittest.main()
