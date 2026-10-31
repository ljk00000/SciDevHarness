from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scidev_core import CodingAgent, EventLedger, GitManager, SummarySettings


class BudgetSettingsTests(unittest.TestCase):
    def test_budget_round_trip_and_environment_override(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            settings = SummarySettings(enabled=False, max_agent_turns=12, max_task_tokens=5000)
            settings.save(root)
            loaded = SummarySettings.load(root)
            self.assertEqual(loaded.max_agent_turns, 12)
            self.assertEqual(loaded.max_task_tokens, 5000)

            with patch.dict(os.environ, {"SCIDEV_MAX_AGENT_TURNS": "4", "SCIDEV_MAX_TASK_TOKENS": "900"}, clear=False):
                overridden = SummarySettings.load(root)
            self.assertEqual(overridden.max_agent_turns, 4)
            self.assertEqual(overridden.max_task_tokens, 900)

    def test_invalid_budget_values_are_clamped(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            path = SummarySettings.config_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"max_agent_turns": 999, "max_task_tokens": -5}',
                encoding="utf-8",
            )
            loaded = SummarySettings.load(root)
            self.assertEqual(loaded.max_agent_turns, 64)
            self.assertEqual(loaded.max_task_tokens, 0)

    def test_task_token_usage_subtracts_the_task_baseline(self) -> None:
        session = {
            "model_usage": {"total_tokens": 100, "token_reports": 3},
            "task_usage_baseline": {"total_tokens": 40, "token_reports": 1},
        }
        self.assertEqual(CodingAgent._task_token_usage(session), (60, 2))

    def test_configured_turn_limit_is_applied(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            agent = CodingAgent(
                root,
                EventLedger(root),
                GitManager(root),
                summary_settings=SummarySettings(enabled=False, max_agent_turns=2),
                require_verification=False,
            )
            provider = Mock()
            provider.model = "test-model"
            with patch.object(agent, "_build_provider", return_value=provider):
                state = agent._prepare_coding_run(
                    {
                        "session_id": "budget-turn-limit",
                        "prompt": "say done",
                        "planning_mode": "off",
                    },
                    "task-1",
                )
            self.assertEqual(state["turn_limit"], 2)


if __name__ == "__main__":
    unittest.main()
