from __future__ import annotations

import unittest

from scidev_planning import PLAN_TOOL, TaskPlan, planning_mode


class TaskPlanTests(unittest.TestCase):
    def test_modes_are_restricted_and_default_vocabulary_is_stable(self) -> None:
        for mode in ("auto", "required", "off"):
            with self.subTest(mode=mode):
                self.assertEqual(planning_mode(mode), mode)
        for invalid in (None, "always", "AUTO", 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    planning_mode(invalid)

    def test_plan_round_trip_counts_skipped_steps_as_resolved(self) -> None:
        plan = TaskPlan.update(
            None,
            [
                {"step": "inspect code", "status": "completed"},
                {"step": "optional check", "status": "skipped"},
            ],
            "The optional check does not apply.",
        )

        restored = TaskPlan.from_record(plan.to_record())
        self.assertEqual(restored, plan)
        self.assertTrue(restored.finished)
        self.assertEqual(restored.progress, (2, 2))
        self.assertIn("2/2", restored.context())

    def test_updates_require_a_reason_for_skipping_removing_or_reopening(self) -> None:
        previous = TaskPlan.update(
            None,
            [
                {"step": "implement", "status": "in_progress"},
                {"step": "verify", "status": "pending"},
            ],
        )
        with self.assertRaisesRegex(ValueError, "explanation"):
            TaskPlan.update(previous, [{"step": "implement", "status": "skipped"}])
        with self.assertRaisesRegex(ValueError, "explanation"):
            TaskPlan.update(previous, [{"step": "implement", "status": "in_progress"}])
        completed = TaskPlan.update(previous, [
            {"step": "implement", "status": "completed"},
            {"step": "verify", "status": "completed"},
        ])
        with self.assertRaisesRegex(ValueError, "explanation"):
            TaskPlan.update(completed, [{"step": "implement", "status": "pending"}])
        skipped = TaskPlan.update(
            previous,
            [
                {"step": "implement", "status": "skipped"},
                {"step": "verify", "status": "pending"},
            ],
            "Implementation is out of scope.",
        )
        with self.assertRaisesRegex(ValueError, "explanation"):
            TaskPlan.update(
                skipped,
                [
                    {"step": "implement", "status": "pending"},
                    {"step": "verify", "status": "pending"},
                ],
            )

    def test_plan_rejects_ambiguous_or_malformed_steps(self) -> None:
        cases = (
            [],
            [{"step": "same", "status": "pending"}, {"step": "same", "status": "pending"}],
            [{"step": "one", "status": "in_progress"}, {"step": "two", "status": "in_progress"}],
            [{"step": "   ", "status": "pending"}],
            [{"step": "one", "status": "unknown"}],
            [{"step": "one", "status": "pending", "extra": True}],
        )
        for steps in cases:
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError):
                    TaskPlan.update(None, steps)

    def test_tool_schema_caps_plan_size_and_declares_statuses(self) -> None:
        schema = PLAN_TOOL["function"]["parameters"]
        self.assertEqual(schema["properties"]["plan"]["maxItems"], 12)
        self.assertEqual(
            schema["properties"]["plan"]["items"]["properties"]["status"]["enum"],
            ["pending", "in_progress", "completed", "blocked", "skipped"],
        )


if __name__ == "__main__":
    unittest.main()
