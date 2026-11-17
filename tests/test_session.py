from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from scidev.agent.core import CodingAgent
from scidev.runtime.errors import PermanentError


class SessionModuleBoundaryTests(unittest.TestCase):
    @staticmethod
    def _agent(root: Path) -> CodingAgent:
        agent = CodingAgent.__new__(CodingAgent)
        agent.project_root = root
        agent.ledger = Mock()
        agent.git = Mock()
        agent.git.head_sha.return_value = "base-sha"
        agent.git.status_paths.return_value = {"z.py", "a.py"}
        agent.event_callback = Mock()
        return agent

    @staticmethod
    def _interrupted_session() -> dict:
        return {
            "session_id": "recovery",
            "prompt": "Create once.py",
            "status": "active",
            "verification_edit_revision": 2,
            "messages": [
                {"role": "user", "content": "Create once.py"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "write-once",
                    "function": {"name": "write_file", "arguments": "{}"},
                }]},
            ],
        }

    def test_session_import_does_not_load_agent_model_or_ui(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-c", """
import json
import sys
import scidev.agent.session
print(json.dumps([
    name for name in sys.modules
    if name in {"scidev.agent.core", "scidev.desktop.client", "scidev.integrations.model", "scidev.workspace.git", "scidev.runtime.ledger"}
    or name.startswith("PySide6")
]))
"""],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [])

    def test_existing_session_load_preserves_state_without_new_git_or_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = self._agent(root)
            path = root / "custom-session.json"
            original = {
                "session_id": "existing",
                "messages": [{"role": "user", "content": "old prompt"}],
                "verification_runs": [{"command": "pytest -q", "check_status": "failed"}],
                "custom_state": {"keep": True},
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            agent._session_path = Mock(return_value=path)
            agent._save_session = Mock()
            agent._system_prompt_for_task = Mock()

            restored = agent._load_session("existing", "new prompt")

            self.assertEqual(restored, original)
            agent._session_path.assert_called_once_with("existing")
            self.assertEqual(agent.git.mock_calls, [])
            self.assertEqual(agent.ledger.mock_calls, [])
            agent._system_prompt_for_task.assert_not_called()
            agent._save_session.assert_not_called()

    def test_new_session_uses_current_save_path_and_prompt_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = self._agent(root)
            agent._session_path = Mock(return_value=root / "custom.json")
            agent._save_session = Mock()
            agent._system_prompt_for_task = Mock(return_value="custom system prompt")

            session = agent._load_session("new", "inspect the project")

            agent._save_session.assert_called_once_with(session)
            agent._system_prompt_for_task.assert_called_once_with("inspect the project")
            agent.ledger.create_session.assert_called_once_with("new", "inspect the project", "custom.json")
            self.assertEqual(session["git_base_sha"], "base-sha")
            self.assertEqual(session["git_preexisting_paths"], ["a.py", "z.py"])
            self.assertEqual(session["messages"][0]["content"], "custom system prompt")

    def test_invalid_saved_session_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = self._agent(root)
            path = root / "invalid.json"
            path.write_text("{", encoding="utf-8")
            agent._session_path = Mock(return_value=path)
            agent._save_session = Mock()

            with self.assertRaisesRegex(PermanentError, "无法恢复编码会话"):
                agent._load_session("invalid", "inspect the project")

            self.assertEqual(path.read_text(encoding="utf-8"), "{")
            agent._save_session.assert_not_called()
            self.assertEqual(agent.git.mock_calls, [])
            self.assertEqual(agent.ledger.mock_calls, [])

    def test_recovery_saves_before_journal_cleanup_and_events(self) -> None:
        agent = self._agent(Path.cwd())
        session = self._interrupted_session()
        agent.ledger.tool_calls_for_session.return_value = {
            (1, 0): {"call_id": "write-once", "name": "write_file", "status": "completed", "result": "saved result"},
        }
        operations = []
        saved = []
        agent._save_session = Mock(side_effect=lambda record: (operations.append("save"), saved.append(copy.deepcopy(record))))
        agent.ledger.clear_tool_call.side_effect = lambda *_args: operations.append("clear")
        agent.ledger.append.side_effect = lambda *_args: operations.append("event")
        agent.event_callback.side_effect = lambda *_args: operations.append("emit")

        agent._recover_interrupted_tool_calls(session)

        self.assertEqual(operations, ["save", "clear", "event", "emit"])
        self.assertEqual(saved[0]["messages"][-1], {
            "role": "tool", "tool_call_id": "write-once", "content": "saved result",
        })
        self.assertTrue(saved[0]["verification_code_changed"])
        self.assertEqual(saved[0]["verification_edit_revision"], 3)
        agent.ledger.clear_tool_call.assert_called_once_with("recovery", 1, 0)
        agent.event_callback.assert_called_once()
        self.assertEqual(agent.event_callback.call_args.args[0], "tool_call_recovered")

    def test_failed_recovery_save_leaves_journal_and_events_intact(self) -> None:
        agent = self._agent(Path.cwd())
        agent.ledger.tool_calls_for_session.return_value = {
            (1, 0): {"call_id": "write-once", "name": "write_file", "status": "started"},
        }
        agent._save_session = Mock(side_effect=OSError("disk full"))

        with self.assertRaisesRegex(OSError, "disk full"):
            agent._recover_interrupted_tool_calls(self._interrupted_session())

        agent.ledger.clear_tool_call.assert_not_called()
        agent.ledger.append.assert_not_called()
        agent.event_callback.assert_not_called()

    def test_existing_tool_result_is_not_rewritten_or_duplicated(self) -> None:
        agent = self._agent(Path.cwd())
        session = self._interrupted_session()
        session["messages"].append({"role": "tool", "tool_call_id": "write-once", "content": "existing result"})
        original = copy.deepcopy(session)
        agent.ledger.tool_calls_for_session.return_value = {
            (1, 0): {"call_id": "write-once", "name": "write_file", "status": "completed", "result": "journal result"},
        }
        agent._save_session = Mock()

        agent._recover_interrupted_tool_calls(session)

        self.assertEqual(session, original)
        agent._save_session.assert_not_called()
        agent.ledger.clear_tool_call.assert_called_once_with("recovery", 1, 0)
        agent.ledger.append.assert_not_called()
        agent.event_callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
