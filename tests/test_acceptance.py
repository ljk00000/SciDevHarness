from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_acceptance import SemanticAcceptance
from scidev_core import CodingAgent, EventLedger, GitManager, PermanentError, SummarySettings


def review_response(verdict: str = "pass", criterion_status: str = "met") -> dict:
    return {
        "role": "assistant",
        "content": json.dumps({
            "verdict": verdict,
            "summary": "对照任务要求检查了变更。",
            "criteria": [{
                "criterion": "修改满足用户指定行为",
                "status": criterion_status,
                "evidence": "main.py:1 的变更实现了目标。",
            }],
        }, ensure_ascii=False),
        "tool_calls": [],
    }


def tool_reply(name: str, arguments: dict, call_id: str | None = None) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id or f"call_{name}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }],
    }


def reviewed_edit(old_line: str, new_line: str, call_id: str = "call_reviewed_edit") -> dict:
    patch_text = (
        "*** Begin Patch\n*** Update File: main.py\n@@\n"
        f"-{old_line}\n+{new_line}\n*** End Patch"
    )
    return tool_reply("apply_patch", {"patch": patch_text}, call_id)


class SemanticAcceptanceValidationTests(unittest.TestCase):
    def test_modes_are_allowlisted_and_invalid_values_fail_closed_to_off(self) -> None:
        self.assertEqual(SemanticAcceptance.mode("required"), "required")
        self.assertEqual(SemanticAcceptance.mode("ADVISORY"), "advisory")
        self.assertEqual(SemanticAcceptance.mode("execute"), "off")

    def test_complete_cited_review_can_pass(self) -> None:
        report = SemanticAcceptance.evaluate(
            review_response(), reviewed_paths=["main.py"], scope_complete=True, reviewer_model="review-v2",
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["reviewed_paths"], ["main.py"])
        self.assertTrue(report["scope_complete"])

    def test_pass_verdict_with_unmet_criterion_is_downgraded_to_revise(self) -> None:
        report = SemanticAcceptance.evaluate(
            review_response("pass", "unmet"), reviewed_paths=["main.py"],
            scope_complete=True, reviewer_model="review-v2",
        )
        self.assertEqual(report["status"], "revise")

    def test_uncited_pass_is_not_accepted(self) -> None:
        response = review_response()
        payload = json.loads(response["content"])
        payload["criteria"][0]["evidence"] = "看起来实现正确。"
        response["content"] = json.dumps(payload)
        report = SemanticAcceptance.evaluate(
            response, reviewed_paths=["main.py"], scope_complete=True, reviewer_model="review-v2",
        )
        self.assertEqual(report["status"], "uncertain")

    def test_malformed_tool_call_oversized_or_incomplete_review_cannot_pass(self) -> None:
        valid = review_response()
        cases = (
            ("not JSON", True, "unavailable"),
            ({**valid, "tool_calls": [{"id": "tool"}]}, True, "unavailable"),
            ({"role": "assistant", "content": "x" * (SemanticAcceptance.MAX_RESPONSE_CHARS + 1)}, True, "unavailable"),
            (valid, False, "scope_incomplete"),
        )
        for response, complete, expected in cases:
            with self.subTest(expected=expected, complete=complete):
                report = SemanticAcceptance.evaluate(
                    response, reviewed_paths=["main.py"], scope_complete=complete, reviewer_model="review-v2",
                )
                self.assertEqual(report["status"], expected)

    def test_reviewer_prompt_separates_untrusted_diff_and_verification_evidence(self) -> None:
        messages = SemanticAcceptance.messages(
            task="Implement requested change",
            final_answer="I believe it works",
            paths=["main.py"],
            diff="+value = 2",
            verification={"status": "passed"},
        )
        self.assertIn("untrusted data", messages[0]["content"])
        self.assertIn('"complete_changed_file_diff_untrusted": "+value = 2"', messages[1]["content"])
        self.assertIn("verification_evidence", messages[1]["content"])

    def test_settings_round_trip_and_invalid_mode_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SummarySettings(acceptance_mode="required", acceptance_model="review-v2")
            settings.save(root)
            loaded = SummarySettings.load(root)
            self.assertEqual(loaded.acceptance_mode, "required")
            self.assertEqual(loaded.acceptance_model, "review-v2")
            settings.config_path(root).write_text(
                json.dumps({"semantic_acceptance_mode": "run", "semantic_acceptance_model": " x "}),
                encoding="utf-8",
            )
            invalid = SummarySettings.load(root)
            self.assertEqual(invalid.acceptance_mode, "off")
            self.assertEqual(invalid.acceptance_model, "x")


class ReviewerProvider:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.histories: list[list[dict]] = []
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, max_tokens=12000, request_id="", on_metrics=None):
        self.histories.append(json.loads(json.dumps(messages)))
        self.calls.append({"tools": tools, "max_tokens": max_tokens, "request_id": request_id})
        if not self.responses:
            raise AssertionError("unexpected reviewer call")
        return self.responses.pop(0)


class CodingProvider:
    model = "coder-v1"

    def __init__(self, responses: list[dict], reviewer: ReviewerProvider | None = None):
        self.responses = list(responses)
        self.reviewer = reviewer
        self.histories: list[list[dict]] = []
        self.reviewer_models: list[str] = []

    def with_model(self, model, *, streaming=None, temperature=None):
        self.reviewer_models.append(model)
        return self.reviewer

    def chat(self, messages, tools=None, max_tokens=12000, request_id="", **_kwargs):
        self.histories.append(json.loads(json.dumps(messages)))
        if not self.responses:
            raise AssertionError("unexpected coding-model call")
        return self.responses.pop(0)


class SemanticAcceptanceAgentTests(unittest.TestCase):
    def _run_agent(
        self,
        mode: str,
        reviewer_model: str,
        reviewer: ReviewerProvider | None,
        coding_responses: list[dict] | None = None,
    ):
        temporary = tempfile.TemporaryDirectory(prefix="scidev-semantic-acceptance-")
        root = Path(temporary.name)
        (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
        source = root / "main.py"
        source.write_text("value = 1\n", encoding="utf-8")
        git = GitManager(root)
        git.init()
        git.commit_changes("baseline")
        ledger = EventLedger(root)
        agent = CodingAgent(
            root,
            ledger,
            git,
            summary_settings=SummarySettings(
                enabled=False, acceptance_mode=mode, acceptance_model=reviewer_model,
            ),
            require_verification=False,
            patch_review=lambda preview: {int(hunk["id"]) for hunk in preview["hunks"]},
        )
        default_responses = [
            tool_reply("read_file", {"path": "main.py"}),
            reviewed_edit("value = 1", "value = 2"),
            tool_reply("git_diff", {}),
            {"role": "assistant", "content": "Set value to 2.", "tool_calls": []},
        ]
        coding = CodingProvider(coding_responses or default_responses, reviewer)
        task = {"task_id": "acceptance-task", "payload": {
            "session_id": "acceptance-session", "prompt": "Set value to 2", "planning_mode": "off",
        }}
        return temporary, root, source, git, ledger, agent, coding, task

    def test_required_review_receives_bounded_diff_and_allows_commit_when_passed(self) -> None:
        reviewer = ReviewerProvider([review_response()])
        temporary, root, source, git, ledger, agent, coding, task = self._run_agent("required", "review-v2", reviewer)
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=coding):
                result = agent.run(task)
            self.assertEqual(result["semantic_acceptance_report"]["status"], "passed")
            self.assertTrue(result["git_result_sha"])
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(coding.reviewer_models, ["review-v2"])
            self.assertEqual(reviewer.calls[0]["max_tokens"], 12000)
            self.assertIsNone(reviewer.calls[0]["tools"])
            self.assertIn("main.py", reviewer.histories[0][1]["content"])
            self.assertIn("+value = 2", reviewer.histories[0][1]["content"])
            ledger_text = ledger.events_path.read_text(encoding="utf-8")
            self.assertIn("semantic_acceptance_reviewed", ledger_text)
            self.assertNotIn("complete_changed_file_diff_untrusted", ledger_text)
        finally:
            temporary.cleanup()

    def test_default_off_does_not_make_extra_model_call(self) -> None:
        temporary, _root, _source, git, _ledger, agent, coding, task = self._run_agent("off", "review-v2", None)
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=coding):
                result = agent.run(task)
            self.assertTrue(result["git_result_sha"])
            self.assertEqual(coding.reviewer_models, [])
            self.assertIsNone(result["semantic_acceptance_report"])
        finally:
            temporary.cleanup()

    def test_required_mode_fails_closed_when_reviewer_is_not_configured(self) -> None:
        temporary, _root, source, git, _ledger, agent, coding, task = self._run_agent("required", "", None)
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=coding):
                with self.assertRaises(PermanentError):
                    agent.run(task)
            session = json.loads((source.parent / ".research/sessions/acceptance-session.json").read_text(encoding="utf-8"))
            self.assertEqual(session["status"], "acceptance_required")
            self.assertFalse(git.head_sha() == session.get("git_result_sha"))
            self.assertEqual(git.status_paths(), {"main.py"})
        finally:
            temporary.cleanup()

    def test_manual_retry_after_acceptance_block_rechecks_the_pending_diff(self) -> None:
        temporary, root, _source, git, _ledger, agent, first_provider, task = self._run_agent(
            "required", "", None,
        )
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=first_provider):
                with self.assertRaises(PermanentError):
                    agent.run(task)

            agent.summary_settings.acceptance_model = "review-v2"
            reviewer = ReviewerProvider([review_response()])
            retry_provider = CodingProvider([
                tool_reply("git_diff", {}, "retry_diff"),
                {"role": "assistant", "content": "The pending change is now independently reviewed.", "tool_calls": []},
            ], reviewer)
            retry_task = {"task_id": "acceptance-manual-retry", "payload": {
                "session_id": "acceptance-session", "prompt": "Set value to 2", "planning_mode": "off",
            }}
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=retry_provider):
                result = agent.run(retry_task)

            self.assertEqual(result["semantic_acceptance_report"]["status"], "passed")
            self.assertTrue(result["git_result_sha"])
            self.assertEqual(git.status_paths(), set())
            self.assertEqual(len(reviewer.calls), 1)
        finally:
            temporary.cleanup()

    def test_advisory_uncertain_review_is_reported_but_does_not_block_commit(self) -> None:
        reviewer = ReviewerProvider([{"role": "assistant", "content": "not json", "tool_calls": []}])
        temporary, _root, _source, _git, _ledger, agent, coding, task = self._run_agent("advisory", "review-v2", reviewer)
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=coding):
                result = agent.run(task)
            self.assertEqual(result["semantic_acceptance_report"]["status"], "unavailable")
            self.assertTrue(result["git_result_sha"])
            self.assertIn("语义验收", result["text"])
        finally:
            temporary.cleanup()

    def test_required_mode_rejects_same_model_id_without_making_review_call(self) -> None:
        reviewer = ReviewerProvider([review_response()])
        temporary, _root, _source, git, _ledger, agent, coding, task = self._run_agent(
            "required", "coder-v1", reviewer,
        )
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=coding):
                with self.assertRaisesRegex(PermanentError, "不同的模型 ID"):
                    agent.run(task)
            self.assertEqual(reviewer.calls, [])
            self.assertEqual(git.status_paths(), {"main.py"})
        finally:
            temporary.cleanup()

    def test_required_review_finding_gets_one_fix_and_is_rechecked(self) -> None:
        reviewer = ReviewerProvider([review_response("revise", "unmet"), review_response()])
        responses = [
            tool_reply("read_file", {"path": "main.py"}, "read_first"),
            reviewed_edit("value = 1", "value = 2", "edit_first"),
            tool_reply("git_diff", {}, "diff_first"),
            {"role": "assistant", "content": "Set value to 2.", "tool_calls": []},
            tool_reply("read_file", {"path": "main.py"}, "read_again"),
            reviewed_edit("value = 2", "value = 3", "edit_again"),
            tool_reply("git_diff", {}, "diff_again"),
            {"role": "assistant", "content": "Updated the value to meet the review finding.", "tool_calls": []},
        ]
        temporary, _root, source, _git, _ledger, agent, coding, task = self._run_agent(
            "required", "review-v2", reviewer, responses,
        )
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=coding):
                result = agent.run(task)
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 3\n")
            self.assertEqual(result["semantic_acceptance_report"]["status"], "passed")
            self.assertGreater(result["semantic_acceptance_report"]["turn"], 4)
            self.assertEqual(len(reviewer.calls), 2)
            self.assertTrue(result["git_result_sha"])
        finally:
            temporary.cleanup()

    def test_workspace_change_while_review_is_in_flight_invalidates_verdict(self) -> None:
        temporary, _root, source, git, _ledger, agent, coding, task = self._run_agent(
            "required", "review-v2", None,
        )

        class ConcurrentEditReviewer(ReviewerProvider):
            def chat(self, messages, tools=None, max_tokens=12000, request_id="", on_metrics=None):
                result = super().chat(messages, tools, max_tokens, request_id, on_metrics)
                source.write_text("value = 99\n", encoding="utf-8")
                return result

        reviewer = ConcurrentEditReviewer([review_response()])
        coding.reviewer = reviewer
        try:
            with patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=coding):
                with self.assertRaisesRegex(PermanentError, "变更证据不完整"):
                    agent.run(task)
            session = json.loads((source.parent / ".research/sessions/acceptance-session.json").read_text(encoding="utf-8"))
            self.assertEqual(session["semantic_acceptance_report"]["status"], "scope_incomplete")
            self.assertFalse(session["semantic_acceptance_report"]["revalidated"])
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 99\n")
            self.assertEqual(git.status_paths(), {"main.py"})
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
