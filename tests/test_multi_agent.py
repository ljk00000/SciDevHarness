from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_core import (
    CodingAgent,
    CodingToolbox,
    EventLedger,
    GitManager,
    OpenAICompatibleProvider,
    SummarySettings,
)
from scidev_multi_agent import (
    DELEGATE_PARALLEL_RESEARCH_TOOL,
    DELEGATE_RESEARCH_TOOL,
    MAX_SPECIALIST_TURNS,
    MAX_SPECIALIST_TOOL_CALLS,
    READ_ONLY_TOOL_NAMES,
    SPECIALIST_MAX_TOKENS,
    run_specialist_agent,
    validate_delegation,
    validate_parallel_delegations,
)


def tool_reply(name: str, arguments: dict, call_id: str = "call-specialist") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }],
    }


class ScriptedSpecialistProvider:
    model = "specialist-test"

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.histories: list[list[dict]] = []

    def chat(self, messages, tools=None, max_tokens=12000, request_id="", on_metrics=None, **kwargs):
        self.calls.append({
            "tools": tools,
            "max_tokens": max_tokens,
            "request_id": request_id,
            "tool_choice": kwargs.get("tool_choice", "auto"),
        })
        self.histories.append(json.loads(json.dumps(messages)))
        if not self.responses:
            raise AssertionError("unexpected specialist model call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ScriptedCoordinatorProvider:
    model = "coder-test"

    def __init__(self, responses: list[dict], specialist: ScriptedSpecialistProvider | None = None):
        self.responses = list(responses)
        self.specialist = specialist
        self.calls: list[dict] = []
        self.selected_models: list[str] = []

    def with_model(self, model, *, streaming=None, temperature=None):
        self.selected_models.append(model)
        return self.specialist

    def chat(self, messages, tools=None, max_tokens=12000, request_id="", **kwargs):
        self.calls.append({"tools": tools, "max_tokens": max_tokens, "request_id": request_id})
        if not self.responses:
            raise AssertionError("unexpected coordinator model call")
        return self.responses.pop(0)


class BarrierSpecialistProvider:
    model = "parallel-specialist-test"

    def __init__(self, on_parallel_start=None):
        self.barrier = threading.Barrier(2)
        self.lock = threading.Lock()
        self.on_parallel_start = on_parallel_start
        self.did_parallel_action = False
        self.calls = 0
        self.max_active = 0
        self.active = 0

    def chat(self, messages, tools=None, max_tokens=12000, request_id="", **kwargs):
        with self.lock:
            self.calls += 1
            call_number = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if call_number <= 2:
                self.barrier.wait(timeout=5)
                with self.lock:
                    run_action = self.on_parallel_start is not None and not self.did_parallel_action
                    if run_action:
                        self.did_parallel_action = True
                if run_action:
                    self.on_parallel_start()
            question = str(messages[-1].get("content") or "")
            if "architecture probe" in question:
                result = "architecture-result"
            elif "tests probe" in question:
                result = "tests-result"
            else:
                result = "reviewer-result"
            return {"role": "assistant", "content": result, "tool_calls": []}
        finally:
            with self.lock:
                self.active -= 1


class MultiAgentTests(unittest.TestCase):
    def _toolbox(self, root: Path):
        ledger = EventLedger(root)
        return ledger, CodingToolbox(root, ledger)

    def test_delegation_schema_is_bounded_and_rejects_unknown_roles_or_fields(self) -> None:
        request = validate_delegation({
            "role": "tests",
            "question": "Which tests cover this parser?",
            "focus_paths": ["src", "src"],
        })
        self.assertEqual(request["focus_paths"], ["src"])
        self.assertEqual(DELEGATE_RESEARCH_TOOL["function"]["name"], "delegate_research")
        self.assertEqual(DELEGATE_RESEARCH_TOOL["function"]["parameters"]["additionalProperties"], False)
        self.assertEqual(MAX_SPECIALIST_TURNS, 5)
        for invalid in (
            {"role": "unknown", "question": "inspect"},
            {"role": "tests", "question": " "},
            {"role": "tests", "question": "inspect", "unknown": True},
            {"role": "tests", "question": "inspect", "focus_paths": [str(index) for index in range(6)]},
        ):
            with self.subTest(arguments=invalid), self.assertRaises(ValueError):
                validate_delegation(invalid)

    def test_parallel_delegations_are_bounded_read_only_and_unique(self) -> None:
        requests = validate_parallel_delegations({"delegations": [
            {"role": "architecture", "question": "architecture probe", "focus_paths": ["src"]},
            {"role": "tests", "question": "tests probe", "focus_paths": ["tests"]},
        ]})
        self.assertEqual([item["role"] for item in requests], ["architecture", "tests"])
        self.assertEqual(DELEGATE_PARALLEL_RESEARCH_TOOL["function"]["name"], "delegate_parallel_research")
        self.assertEqual(DELEGATE_PARALLEL_RESEARCH_TOOL["function"]["parameters"]["additionalProperties"], False)
        for invalid in (
            {"delegations": [{"role": "tests", "question": "one"}]},
            {"delegations": [{"role": "tests", "question": "one"}] * 4},
            {"delegations": [
                {"role": "coder", "question": "write code"},
                {"role": "tests", "question": "inspect tests"},
            ]},
            {"delegations": [
                {"role": "tests", "question": "same"},
                {"role": "tests", "question": "same"},
            ]},
            {"delegations": [
                {"role": "tests", "question": "inspect", "extra": True},
                {"role": "architecture", "question": "inspect"},
            ]},
        ):
            with self.subTest(arguments=invalid), self.assertRaises(ValueError):
                validate_parallel_delegations(invalid)

    def test_specialist_can_only_search_read_and_list_and_uses_large_token_budget(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-readonly-") as temporary:
            root = Path(temporary)
            source = root / "main.py"
            source.write_text("target_value = 7\n", encoding="utf-8")
            ledger, toolbox = self._toolbox(root)
            child = ScriptedSpecialistProvider([
                tool_reply("search_code", {"query": "target_value", "path": "main.py"}),
                {"role": "assistant", "content": "main.py:1 定义 target_value；这是本次调查的相关实现。", "tool_calls": []},
            ])
            coordinator = ScriptedCoordinatorProvider([], child)
            events: list[tuple[str, dict]] = []

            result = run_specialist_agent(
                coordinator,
                toolbox,
                role="architecture",
                task="Find the implementation",
                question="Where is target_value defined?",
                focus_paths=["main.py"],
                model="specialist-test",
                request_id="specialist-test-request",
                on_event=lambda name, payload: events.append((name, payload)),
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["tool_calls"], 1)
            self.assertIn("main.py:1", result["summary"])
            self.assertEqual(coordinator.selected_models, ["specialist-test"])
            self.assertEqual(set(child.calls[0]["tools"][i]["function"]["name"] for i in range(4)), READ_ONLY_TOOL_NAMES)
            self.assertNotIn("write_file", {tool["function"]["name"] for tool in child.calls[0]["tools"]})
            self.assertEqual(child.calls[0]["max_tokens"], SPECIALIST_MAX_TOKENS)
            self.assertGreater(SPECIALIST_MAX_TOKENS, 10000)
            self.assertTrue(any(name == "specialist_tool_used" for name, _ in events))
            self.assertEqual(source.read_text(encoding="utf-8"), "target_value = 7\n")
            toolbox.close()

    def test_coder_specialist_submits_one_scoped_patch_through_user_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-coder-") as temporary:
            root = Path(temporary)
            source = root / "src" / "main.py"
            source.parent.mkdir()
            source.write_text("value = 1\n", encoding="utf-8")
            ledger = EventLedger(root)
            reviews = []
            toolbox = CodingToolbox(
                root,
                ledger,
                patch_review=lambda preview: (
                    reviews.append(preview) or {int(item["id"]) for item in preview["hunks"]}
                ),
            )
            patch_text = "*** Begin Patch\n*** Update File: src/main.py\n@@\n-value = 1\n+value = 2\n*** End Patch"
            child = ScriptedSpecialistProvider([
                tool_reply("apply_patch", {"patch": patch_text}, "coder-patch"),
                {"role": "assistant", "content": "src/main.py:1 updated value to 2.", "tool_calls": []},
            ])
            coordinator = ScriptedCoordinatorProvider([], child)
            events: list[tuple[str, dict]] = []

            result = run_specialist_agent(
                coordinator,
                toolbox,
                role="coder",
                task="Change value from 1 to 2 in src/main.py",
                question="Make the requested focused change.",
                focus_paths=["src"],
                request_id="coder-reviewed-patch",
                on_event=lambda name, payload: events.append((name, payload)),
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["modified_paths"], ["src/main.py"])
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(len(reviews), 1)
            first_names = {item["function"]["name"] for item in child.calls[0]["tools"]}
            self.assertEqual(first_names, {"apply_patch"})
            self.assertEqual(child.calls[0]["tool_choice"], "required")
            self.assertNotIn("write_file", first_names)
            self.assertNotIn("replace_in_file", first_names)
            self.assertNotIn("run_command", first_names)
            self.assertIn("value = 1", child.histories[0][1]["content"])
            self.assertIn("Harness 预取的项目上下文", child.histories[0][1]["content"])
            self.assertEqual(len(child.calls), 2)
            self.assertTrue(any(name == "specialist_tool_used" for name, _ in events))
            toolbox.close()

    def test_coder_text_diff_without_reviewed_patch_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-coder-text-only-") as temporary:
            root = Path(temporary)
            source = root / "src" / "main.py"
            source.parent.mkdir()
            source.write_text("value = 1\n", encoding="utf-8")
            ledger, toolbox = self._toolbox(root)
            child = ScriptedSpecialistProvider([
                {
                    "role": "assistant",
                    "content": "Proposed diff: value = 1 -> value = 2. Please approve.",
                    "tool_calls": [],
                },
                {
                    "role": "assistant",
                    "content": "I cannot provide a patch tool call.",
                    "tool_calls": [],
                },
            ])
            coordinator = ScriptedCoordinatorProvider([], child)

            result = run_specialist_agent(
                coordinator,
                toolbox,
                role="coder",
                task="Change the value",
                question="Change value from 1 to 2.",
                focus_paths=["src/main.py"],
                request_id="coder-text-only",
            )

            self.assertEqual(result["status"], "incomplete")
            self.assertIn("没有通过 apply_patch", result["summary"])
            self.assertEqual(result["modified_paths"], [])
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(
                [{tool["function"]["name"] for tool in call["tools"]} for call in child.calls],
                [{"apply_patch"}, {"apply_patch"}],
            )
            self.assertEqual([call["tool_choice"] for call in child.calls], ["required"] * 2)
            toolbox.close()

    def test_coder_declares_only_patch_tool_after_context_is_prefetched(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-coder-stages-") as temporary:
            root = Path(temporary)
            source = root / "src" / "main.py"
            source.parent.mkdir()
            source.write_text("value = 1\n", encoding="utf-8")
            ledger, toolbox = self._toolbox(root)
            reviews = []
            toolbox.patch_review = lambda preview: reviews.append(preview) or {1}
            read_attempt = ScriptedSpecialistProvider([
                tool_reply("read_file", {"path": "src/main.py"}, "model-read-attempt"),
            ])
            result = run_specialist_agent(
                ScriptedCoordinatorProvider([], read_attempt), toolbox,
                role="coder", task="Change the value", question="Change value to 2.",
                focus_paths=["src/main.py"], request_id="coder-read-attempt",
            )
            self.assertEqual(result["status"], "blocked")
            self.assertIn("当前阶段未声明", result["summary"])
            self.assertEqual(reviews, [])
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(
                {tool["function"]["name"] for tool in read_attempt.calls[0]["tools"]},
                {"apply_patch"},
            )
            toolbox.close()

    def test_coder_patch_outside_focus_is_blocked_before_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-coder-scope-") as temporary:
            root = Path(temporary)
            focus = root / "src"
            focus.mkdir()
            (focus / "main.py").write_text("value = 1\n", encoding="utf-8")
            other = root / "other.py"
            other.write_text("secretly_unchanged = True\n", encoding="utf-8")
            ledger = EventLedger(root)
            reviews = []
            toolbox = CodingToolbox(root, ledger, patch_review=lambda preview: reviews.append(preview) or {1})
            patch_text = "*** Begin Patch\n*** Update File: other.py\n@@\n-secretly_unchanged = True\n+secretly_unchanged = False\n*** End Patch"
            child = ScriptedSpecialistProvider([
                tool_reply("apply_patch", {"patch": patch_text}, "out-of-focus-patch"),
            ])
            coordinator = ScriptedCoordinatorProvider([], child)

            result = run_specialist_agent(
                coordinator,
                toolbox,
                role="coder",
                task="Only modify src/main.py",
                question="Change the focused code.",
                focus_paths=["src"],
                request_id="coder-scope-check",
            )

            self.assertEqual(result["status"], "blocked")
            self.assertIn("超出", result["summary"])
            self.assertEqual(reviews, [])
            self.assertEqual(other.read_text(encoding="utf-8"), "secretly_unchanged = True\n")
            self.assertIsNone(toolbox.last_patch_result)
            toolbox.close()

    def test_specialist_blocks_write_tool_and_never_touches_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-block-") as temporary:
            root = Path(temporary)
            source = root / "main.py"
            source.write_text("value = 1\n", encoding="utf-8")
            ledger, toolbox = self._toolbox(root)
            child = ScriptedSpecialistProvider([
                {"role": "assistant", "content": "", "tool_calls": [
                    tool_reply("read_file", {"path": "main.py"}, "read-before-write")["tool_calls"][0],
                    tool_reply("write_file", {"path": "main.py", "content": "value = 2\n"}, "blocked-write")["tool_calls"][0],
                ]},
            ])
            coordinator = ScriptedCoordinatorProvider([], child)
            events: list[tuple[str, dict]] = []

            result = run_specialist_agent(
                coordinator,
                toolbox,
                role="security",
                task="Inspect safely",
                question="Check whether editing is possible",
                focus_paths=["main.py"],
                request_id="blocked-write",
                on_event=lambda name, payload: events.append((name, payload)),
            )

            self.assertEqual(result["status"], "blocked")
            self.assertIn("未执行", result["summary"])
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
            self.assertFalse(any(name == "specialist_tool_used" for name, _ in events))
            self.assertEqual(len(child.calls), 1)
            toolbox.close()

    def test_specialist_enforces_a_bounded_read_tool_call_budget(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-tool-budget-") as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("value = 1\n", encoding="utf-8")
            ledger, toolbox = self._toolbox(root)
            batched_calls = [
                tool_reply("read_file", {"path": "main.py"}, f"read-{index}")["tool_calls"][0]
                for index in range(MAX_SPECIALIST_TOOL_CALLS + 1)
            ]
            child = ScriptedSpecialistProvider([
                {"role": "assistant", "content": "", "tool_calls": batched_calls},
                {"role": "assistant", "content": "已基于有限读取完成调查。", "tool_calls": []},
            ])
            coordinator = ScriptedCoordinatorProvider([], child)

            result = run_specialist_agent(
                coordinator,
                toolbox,
                role="tests",
                task="Inspect a bounded project",
                question="Read this file",
                focus_paths=["main.py"],
                request_id="bounded-tools",
            )

            self.assertEqual(result["tool_calls"], MAX_SPECIALIST_TOOL_CALLS)
            tool_messages = [message for message in child.histories[1] if message.get("role") == "tool"]
            self.assertEqual(len(tool_messages), MAX_SPECIALIST_TOOL_CALLS + 1)
            self.assertIn("调用上限", tool_messages[-1]["content"])
            self.assertEqual(result["status"], "completed")
            toolbox.close()

    def test_specialist_rejects_out_of_project_focus_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-scope-") as temporary:
            root = Path(temporary)
            ledger, toolbox = self._toolbox(root)
            child = ScriptedSpecialistProvider([])
            coordinator = ScriptedCoordinatorProvider([], child)

            with self.assertRaises(ValueError):
                run_specialist_agent(
                    coordinator,
                    toolbox,
                    role="architecture",
                    task="Inspect",
                    question="Read something external",
                    focus_paths=["../outside"],
                    request_id="out-of-scope",
                )

            self.assertEqual(child.calls, [])
            toolbox.close()

    def test_summary_settings_round_trip_collaboration_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-settings-") as temporary:
            root = Path(temporary)
            self.assertFalse(SummarySettings.load(root).multi_agent_enabled)
            settings = SummarySettings(multi_agent_enabled=True, multi_agent_model="research-v2")
            settings.save(root)
            loaded = SummarySettings.load(root)
            self.assertTrue(loaded.multi_agent_enabled)
            self.assertEqual(loaded.multi_agent_model, "research-v2")
            settings.config_path(root).write_text(
                json.dumps({"multi_agent_enabled": {"unexpected": "truthy"}}),
                encoding="utf-8",
            )
            self.assertFalse(SummarySettings.load(root).multi_agent_enabled)

    def test_coordinator_delegates_once_reuses_same_revision_and_persists_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-coordinator-") as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("needle = 9\n", encoding="utf-8")
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([
                tool_reply("search_code", {"query": "needle", "path": "main.py"}),
                {"role": "assistant", "content": "main.py:1 定义 needle。", "tool_calls": []},
            ])
            request = {"role": "tests", "question": "Locate needle", "focus_paths": ["main.py"]}
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_research", request, "delegate-one"),
                tool_reply("delegate_research", request, "delegate-repeat"),
                {"role": "assistant", "content": "已根据只读调查核对目标位置。", "tool_calls": []},
            ], child)
            settings = SummarySettings(enabled=False, multi_agent_enabled=True, multi_agent_model="specialist-v2")
            events: list[tuple[str, dict]] = []
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                event_callback=lambda name, payload: events.append((name, payload)),
                summary_settings=settings,
                require_verification=False,
            )
            task = {"task_id": "team-task", "payload": {
                "session_id": "team-session",
                "prompt": "Inspect the code around needle",
                "planning_mode": "off",
                "context_files": [],
                "context_overrides": {},
                "context_readonly_paths": [],
            }}

            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    outcome = agent.run(task)
                self.assertEqual(outcome["text"], "已根据只读调查核对目标位置。")
                saved = json.loads((root / ".research" / "sessions" / "team-session.json").read_text(encoding="utf-8"))
                self.assertEqual(len(child.calls), 2, saved["specialist_calls"])
                self.assertEqual(main.selected_models, ["specialist-v2"])
                self.assertTrue(any(
                    tool["function"]["name"] == "delegate_research"
                    for tool in main.calls[0]["tools"]
                ))
                self.assertEqual(len(saved["specialist_calls"]), 1)
                self.assertEqual(saved["specialist_calls"][0]["status"], "completed")
                self.assertEqual(saved["specialist_calls"][0]["tool_calls"], 1)
                self.assertTrue(any(name == "specialist_result_reused" for name, _ in events))
                self.assertTrue(any(name == "specialist_completed" for name, _ in events))
                self.assertTrue(any(name == "specialist_tool_used" for name, _ in events))
            finally:
                agent.toolbox.close()

    def test_coordinator_runs_read_only_specialists_concurrently_and_persists_results(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-parallel-") as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("needle = 9\n", encoding="utf-8")
            ledger = EventLedger(root)
            child = BarrierSpecialistProvider()
            request = {"delegations": [
                {"role": "architecture", "question": "architecture probe", "focus_paths": ["main.py"]},
                {"role": "tests", "question": "tests probe", "focus_paths": ["main.py"]},
                {"role": "reviewer", "question": "reviewer probe", "focus_paths": ["main.py"]},
            ]}
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_parallel_research", request, "delegate-parallel"),
                {"role": "assistant", "content": "并行调查已完成并待核对。", "tool_calls": []},
            ], child)
            events: list[tuple[str, dict]] = []
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                event_callback=lambda name, payload: events.append((name, payload)),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
            )
            task = {"task_id": "parallel-task", "payload": {
                "session_id": "parallel-session",
                "prompt": "Inspect this project from two independent angles",
                "planning_mode": "off",
                "context_files": [],
                "context_overrides": {},
                "context_readonly_paths": [],
            }}

            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    outcome = agent.run(task)
                saved = json.loads(
                    (root / ".research" / "sessions" / "parallel-session.json").read_text(encoding="utf-8")
                )
                records = saved["specialist_calls"]
                self.assertEqual(outcome["text"], "并行调查已完成并待核对。")
                self.assertEqual(child.calls, 3)
                self.assertEqual(child.max_active, 2)
                self.assertTrue(any(
                    tool["function"]["name"] == "delegate_parallel_research"
                    for tool in main.calls[0]["tools"]
                ))
                self.assertEqual([record["role"] for record in records], ["architecture", "tests", "reviewer"])
                self.assertEqual([record["status"] for record in records], ["completed"] * 3)
                self.assertEqual([record["summary"] for record in records], [
                    "architecture-result", "tests-result", "reviewer-result",
                ])
                self.assertEqual([record["call_index"] for record in records], [1, 2, 3])
                self.assertTrue(all(record["modified_paths"] == [] for record in records))
                self.assertEqual((root / "main.py").read_text(encoding="utf-8"), "needle = 9\n")
                self.assertEqual(sum(name == "specialist_started" for name, _ in events), 3)
                self.assertEqual(sum(name == "specialist_completed" for name, _ in events), 3)
            finally:
                agent.toolbox.close()

    def test_parallel_report_is_marked_stale_if_workspace_changes_during_investigation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-parallel-stale-") as temporary:
            root = Path(temporary)
            source = root / "main.py"
            source.write_text("needle = 9\n", encoding="utf-8")
            ledger = EventLedger(root)
            child = BarrierSpecialistProvider(lambda: source.write_text("needle = 10\n", encoding="utf-8"))
            request = {"delegations": [
                {"role": "architecture", "question": "architecture probe", "focus_paths": ["main.py"]},
                {"role": "tests", "question": "tests probe", "focus_paths": ["main.py"]},
            ]}
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_parallel_research", request, "delegate-parallel-stale"),
                {"role": "assistant", "content": "已收到并行调查报告。", "tool_calls": []},
            ], child)
            events: list[tuple[str, dict]] = []
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                event_callback=lambda name, payload: events.append((name, payload)),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
            )
            task = {"task_id": "parallel-stale-task", "payload": {
                "session_id": "parallel-stale-session",
                "prompt": "Inspect this project from two independent angles",
                "planning_mode": "off",
                "context_files": [],
                "context_overrides": {},
                "context_readonly_paths": [],
            }}

            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    agent.run(task)
                saved = json.loads(
                    (root / ".research" / "sessions" / "parallel-stale-session.json").read_text(encoding="utf-8")
                )
                records = saved["specialist_calls"]
                self.assertEqual(source.read_text(encoding="utf-8"), "needle = 10\n")
                self.assertEqual([record["status"] for record in records], ["stale", "stale"])
                self.assertTrue(all(record["workspace_changed"] is True for record in records))
                self.assertTrue(all("不会缓存" in record["summary"] for record in records))
                self.assertTrue(any(
                    name == "specialist_failed" and payload.get("status") == "stale"
                    for name, payload in events
                ))
            finally:
                agent.toolbox.close()

    def test_parallel_batch_does_not_partially_start_when_task_call_capacity_is_insufficient(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-parallel-cap-") as temporary:
            root = Path(temporary)
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([])
            provider = ScriptedCoordinatorProvider([], child)
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
            )
            session = {
                "session_id": "parallel-cap-session",
                "multi_agent_config": {"enabled": True, "model": "specialist-test"},
                "specialist_calls": [
                    {"call_index": 1, "fingerprint": "prior-1", "status": "completed"},
                    {"call_index": 2, "fingerprint": "prior-2", "status": "unavailable"},
                ],
            }
            try:
                result = agent._delegate_parallel_research(
                    provider,
                    session,
                    "Inspect the project",
                    {"delegations": [
                        {"role": "architecture", "question": "inspect architecture"},
                        {"role": "tests", "question": "inspect tests"},
                    ]},
                    turn=1,
                    workspace_revision=0,
                    task_id="parallel-cap-task",
                )
                self.assertIn("没有部分启动", result)
                self.assertEqual(len(session["specialist_calls"]), 2)
                self.assertEqual(child.calls, [])
                event_rows = [
                    json.loads(line)
                    for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertFalse(any(
                    row["event_type"] == "specialist_started"
                    for row in event_rows
                ))
            finally:
                agent.toolbox.close()

    def test_transient_specialist_failure_does_not_cache_failure_as_a_completed_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-retry-") as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("needle = 9\n", encoding="utf-8")
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([
                RuntimeError("temporary provider outage"),
                {"role": "assistant", "content": "main.py:1 defines needle.", "tool_calls": []},
            ])
            request = {"role": "architecture", "question": "Inspect needle", "focus_paths": ["main.py"]}
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_research", request, "delegate-failed"),
                tool_reply("delegate_research", request, "delegate-retry"),
                {"role": "assistant", "content": "已依据第二次只读调查确认。", "tool_calls": []},
            ], child)
            events: list[tuple[str, dict]] = []
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                event_callback=lambda name, payload: events.append((name, payload)),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
            )
            task = {"task_id": "retry-task", "payload": {
                "session_id": "retry-session",
                "prompt": "Inspect needle in this project",
                "planning_mode": "off",
                "context_files": ["main.py"],
            }}
            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    agent.run(task)
                saved = json.loads((root / ".research" / "sessions" / "retry-session.json").read_text(encoding="utf-8"))
                records = saved["specialist_calls"]
                self.assertEqual(len(child.calls), 2)
                self.assertEqual(len(records), 2)
                self.assertEqual([record["status"] for record in records], ["unavailable", "completed"])
                self.assertEqual(records[0]["fingerprint"], records[1]["fingerprint"])
                self.assertEqual(records[1]["summary"], "main.py:1 defines needle.")
                self.assertTrue(any(name == "specialist_failed" for name, _ in events))
                self.assertTrue(any(name == "specialist_completed" for name, _ in events))
                self.assertFalse(any(name == "specialist_result_reused" for name, _ in events))
            finally:
                agent.toolbox.close()

    def test_manager_tracks_coder_patch_for_context_verification_and_precommit_diff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-manager-coder-") as temporary:
            root = Path(temporary)
            source = root / "src" / "main.py"
            source.parent.mkdir()
            source.write_text("value = 1\n", encoding="utf-8")
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([
                tool_reply("apply_patch", {"patch": (
                    "*** Begin Patch\n*** Update File: src/main.py\n@@\n-value = 1\n+value = 2\n*** End Patch"
                )}, "manager-coder-patch"),
                {"role": "assistant", "content": "src/main.py:1 changed value to 2.", "tool_calls": []},
            ])
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_research", {
                    "role": "coder",
                    "question": "Change value from 1 to 2.",
                    "focus_paths": ["src"],
                }, "manager-delegate-coder"),
                {"role": "assistant", "content": "The requested change is implemented.", "tool_calls": []},
                {"role": "assistant", "content": "The reviewed diff confirms the requested change.", "tool_calls": []},
            ], child)
            events: list[tuple[str, dict]] = []
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                event_callback=lambda name, payload: events.append((name, payload)),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
                patch_review=lambda preview: {int(item["id"]) for item in preview["hunks"]},
            )
            task = {"task_id": "manager-coder-task", "payload": {
                "session_id": "manager-coder-session",
                "prompt": "Change value from 1 to 2 in src/main.py",
                "planning_mode": "off",
                "context_files": ["src/main.py"],
            }}
            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    outcome = agent.run(task)
                saved = json.loads((root / ".research" / "sessions" / "manager-coder-session.json").read_text(encoding="utf-8"))
                self.assertIn("reviewed diff", outcome["text"])
                self.assertEqual(source.read_text(encoding="utf-8"), "value = 2\n")
                self.assertEqual(saved["agent_changed_paths"], ["src/main.py"])
                self.assertTrue(saved["verification_code_changed"])
                self.assertEqual(saved["verification_edit_revision"], 1)
                self.assertEqual(saved["specialist_calls"][0]["modified_paths"], ["src/main.py"])
                self.assertTrue(any(name == "tool_started" and payload.get("source") == "harness_precommit" for name, payload in events))
                self.assertEqual(len(child.calls), 2)
            finally:
                agent.toolbox.close()

    def test_declined_coder_patch_stops_manager_without_fallback_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-coder-denied-") as temporary:
            root = Path(temporary)
            source = root / "src" / "main.py"
            source.parent.mkdir()
            source.write_text("value = 1\n", encoding="utf-8")
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([
                tool_reply("apply_patch", {"patch": (
                    "*** Begin Patch\n*** Update File: src/main.py\n@@\n-value = 1\n+value = 2\n*** End Patch"
                )}, "denied-coder-patch"),
            ])
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_research", {
                    "role": "coder",
                    "question": "Change value from 1 to 2.",
                    "focus_paths": ["src"],
                }, "denied-manager-delegate"),
            ], child)
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
                patch_review=lambda _preview: None,
            )
            task = {"task_id": "denied-coder-task", "payload": {
                "session_id": "denied-coder-session",
                "prompt": "Change value from 1 to 2 in src/main.py",
                "planning_mode": "off",
                "context_files": ["src/main.py"],
            }}
            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    with self.assertRaisesRegex(Exception, "拒绝了补丁"):
                        agent.run(task)
                saved = json.loads((root / ".research" / "sessions" / "denied-coder-session.json").read_text(encoding="utf-8"))
                self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["specialist_calls"][0]["status"], "review_rejected")
                self.assertEqual(len(main.calls), 1)
                self.assertEqual(len(child.calls), 1)
            finally:
                agent.toolbox.close()

    def test_partial_coder_patch_keeps_only_reviewed_hunks_and_stops_without_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-coder-partial-") as temporary:
            root = Path(temporary)
            source = root / "src" / "main.py"
            source.parent.mkdir()
            source.write_text("alpha = 1\n\nbeta = 2\n", encoding="utf-8")
            ledger = EventLedger(root)
            reviews = []
            patch_text = (
                "*** Begin Patch\n*** Update File: src/main.py\n"
                "@@\n-alpha = 1\n+alpha = 10\n"
                "@@\n-beta = 2\n+beta = 20\n*** End Patch"
            )
            child = ScriptedSpecialistProvider([
                tool_reply("apply_patch", {"patch": patch_text}, "partial-coder-patch"),
            ])
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_research", {
                    "role": "coder",
                    "question": "Update both constants.",
                    "focus_paths": ["src"],
                }, "partial-manager-delegate"),
            ], child)
            events: list[tuple[str, dict]] = []
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                event_callback=lambda name, payload: events.append((name, payload)),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
                patch_review=lambda preview: (
                    reviews.append(preview) or {int(preview["hunks"][0]["id"])}
                ),
            )
            task = {"task_id": "partial-coder-task", "payload": {
                "session_id": "partial-coder-session",
                "prompt": "Update both constants in src/main.py",
                "planning_mode": "off",
                "context_files": ["src/main.py"],
            }}
            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    with self.assertRaisesRegex(Exception, "其余补丁块未获批准"):
                        agent.run(task)
                saved = json.loads((root / ".research" / "sessions" / "partial-coder-session.json").read_text(encoding="utf-8"))
                self.assertEqual(len(reviews), 1)
                self.assertEqual(reviews[0]["hunk_count"], 2)
                self.assertEqual(source.read_text(encoding="utf-8"), "alpha = 10\n\nbeta = 2\n")
                self.assertEqual(saved["status"], "failed")
                self.assertTrue(saved["specialist_calls"][0]["patch_partial"])
                self.assertEqual(saved["agent_changed_paths"], ["src/main.py"])
                self.assertEqual(len(child.calls), 1)
                self.assertEqual(len(main.calls), 1)
                self.assertFalse(any(
                    name == "tool_started" and payload.get("source") == "harness_precommit"
                    for name, payload in events
                ))
            finally:
                agent.toolbox.close()

    def test_disabled_collaboration_does_not_expose_tool_or_create_child_agent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-off-") as temporary:
            root = Path(temporary)
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([])
            main = ScriptedCoordinatorProvider([
                {"role": "assistant", "content": "No delegation used.", "tool_calls": []},
            ], child)
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=False),
                require_verification=False,
            )
            task = {"task_id": "no-team-task", "payload": {
                "session_id": "no-team-session",
                "prompt": "Answer a simple question",
                "planning_mode": "off",
                "context_files": [],
            }}
            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    agent.run(task)
                self.assertNotIn("delegate_research", {
                    item["function"]["name"] for item in main.calls[0]["tools"]
                })
                self.assertEqual(main.selected_models, [])
                self.assertEqual(child.calls, [])
            finally:
                agent.toolbox.close()

    def test_coordinator_enforces_three_delegations_even_for_batched_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-cap-") as temporary:
            root = Path(temporary)
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([
                {"role": "assistant", "content": f"调查结果 {index}。", "tool_calls": []}
                for index in range(1, 4)
            ])
            delegated_calls = [
                tool_reply("delegate_research", {
                    "role": "architecture",
                    "question": f"Investigate subsystem {index}",
                    "focus_paths": [],
                }, f"delegate-{index}")["tool_calls"][0]
                for index in range(1, 5)
            ]
            main = ScriptedCoordinatorProvider([
                {"role": "assistant", "content": "", "tool_calls": delegated_calls},
                {"role": "assistant", "content": "已完成有限协作调查。", "tool_calls": []},
            ], child)
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
            )
            task = {"task_id": "cap-task", "payload": {
                "session_id": "cap-session",
                "prompt": "Investigate a code change",
                "planning_mode": "off",
                "context_files": [],
            }}
            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    agent.run(task)
                self.assertEqual(len(child.calls), 3)
                self.assertEqual(len(json.loads(
                    (root / ".research" / "sessions" / "cap-session.json").read_text(encoding="utf-8")
                )["specialist_calls"]), 3)
                next_tools = {item["function"]["name"] for item in main.calls[1]["tools"]}
                self.assertNotIn("delegate_research", next_tools)
            finally:
                agent.toolbox.close()

    def test_workspace_change_invalidates_a_previously_cached_specialist_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-specialist-stale-") as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("needle = 9\n", encoding="utf-8")
            ledger = EventLedger(root)
            child = ScriptedSpecialistProvider([
                {"role": "assistant", "content": "版本 0 的调查。", "tool_calls": []},
                {"role": "assistant", "content": "新增文件后的调查。", "tool_calls": []},
            ])
            request = {"role": "architecture", "question": "Inspect needle", "focus_paths": ["main.py"]}
            main = ScriptedCoordinatorProvider([
                tool_reply("delegate_research", request, "delegate-before-edit"),
                tool_reply("write_file", {"path": "notes.py", "content": "note = True\n"}, "create-note"),
                tool_reply("delegate_research", request, "delegate-after-edit"),
                {"role": "assistant", "content": "两版代码均已分别调查。", "tool_calls": []},
                {"role": "assistant", "content": "已检查最终 Git diff。", "tool_calls": []},
            ], child)
            agent = CodingAgent(
                root,
                ledger,
                GitManager(root),
                summary_settings=SummarySettings(enabled=False, multi_agent_enabled=True),
                require_verification=False,
            )
            task = {"task_id": "stale-task", "payload": {
                "session_id": "stale-session",
                "prompt": "Inspect needle in the project",
                "planning_mode": "off",
                "context_files": [],
            }}
            try:
                with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=main):
                    agent.run(task)
                self.assertEqual(len(child.calls), 2)
                saved = json.loads((root / ".research" / "sessions" / "stale-session.json").read_text(encoding="utf-8"))
                self.assertEqual(len(saved["specialist_calls"]), 2)
                self.assertNotEqual(
                    saved["specialist_calls"][0]["fingerprint"],
                    saved["specialist_calls"][1]["fingerprint"],
                )
            finally:
                agent.toolbox.close()


@unittest.skipUnless(
    os.getenv("SCIDEV_QWEN_CODER_E2E", "").strip() == "1",
    "set SCIDEV_QWEN_CODER_E2E=1 to run against the already-installed local Ollama model",
)
class LocalQwenCoderE2ETests(unittest.TestCase):
    def test_qwen_reads_then_submits_a_reviewed_coder_patch(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="scidev-qwen-coder-e2e-")
        root = Path(temporary.name)
        source = root / "main.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        reviews: list[dict] = []
        events: list[tuple[str, dict]] = []
        ledger = EventLedger(root)
        provider = OpenAICompatibleProvider(
            os.getenv("SCIDEV_API_BASE", "http://127.0.0.1:11434/v1"),
            os.getenv("SCIDEV_API_KEY", "ollama"),
            os.getenv("SCIDEV_MODEL", "scidev-qwen2.5-coder-7b:q4_k_m"),
            timeout=240,
            text_tool_call_fallback=True,
            streaming=False,
            temperature=0.0,
        )
        model_responses: list[dict[str, object]] = []
        original_chat = provider.chat

        def observe_chat(messages: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
            response = original_chat(messages, **kwargs)
            model_responses.append({
                "tool_choice": kwargs.get("tool_choice", "auto"),
                "has_prefetched_value": any(
                    message.get("role") == "user" and "VALUE = 1" in str(message.get("content") or "")
                    for message in messages
                ),
                "declared_tools": [
                    item["function"]["name"]
                    for item in kwargs.get("tools", [])
                    if isinstance(item, dict) and isinstance(item.get("function"), dict)
                ],
                "content": str(response.get("content") or "")[:2000],
                "tool_calls": response.get("tool_calls"),
            })
            return response

        provider.with_model = lambda _model, *, streaming=None, temperature=None: provider
        provider.chat = observe_chat
        toolbox = CodingToolbox(
            root,
            ledger,
            patch_review=lambda preview: (
                reviews.append(preview) or {int(hunk["id"]) for hunk in preview["hunks"]}
            ),
        )
        tool_observations: list[dict[str, object]] = []
        execute_tool = toolbox.execute

        def observe_tool(name: str, arguments: dict[str, object]) -> str:
            result = execute_tool(name, arguments)
            tool_observations.append({"name": name, "arguments": arguments, "result": result})
            return result

        toolbox.execute = observe_tool
        try:
            result = run_specialist_agent(
                provider,
                toolbox,
                role="coder",
                task="In main.py, change the VALUE constant from 1 to 2. Make only this change.",
                question="Implement the requested one-line change in main.py.",
                focus_paths=["main.py"],
                model=provider.model,
                request_id=f"local-qwen-coder-e2e-{os.getpid()}",
                on_event=lambda name, payload: events.append((name, payload)),
            )
            self.assertEqual(
                result["status"],
                "completed",
                f"{result['summary']}\nObserved tools: "
                f"{[{key: item[key] for key in ('name', 'arguments', 'result')} for item in tool_observations]}\n"
                f"Model responses: {model_responses}",
            )
            self.assertTrue(model_responses[0]["has_prefetched_value"])
            self.assertEqual(model_responses[0]["declared_tools"], ["apply_patch"])
            self.assertEqual(result["tool_calls"], 1)
            self.assertEqual(result["modified_paths"], ["main.py"])
            self.assertEqual(source.read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertEqual(len(reviews), 1)
            used_tools = [
                payload["tool"]
                for name, payload in events
                if name == "specialist_tool_used" and payload.get("role") == "coder"
            ]
            self.assertEqual(used_tools[-1], "apply_patch")
            self.assertEqual(used_tools, ["apply_patch"])
        finally:
            toolbox.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
