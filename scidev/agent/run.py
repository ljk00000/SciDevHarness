"""Prepare and advance a bounded coding run.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import copy
import json
import os
import subprocess
from typing import Any
from scidev.integrations.visual import VisualArtifactReviewer
from scidev.runtime.errors import PermanentError
from scidev.runtime.errors import TaskAborted
from scidev.runtime.errors import TaskCancelled
from scidev.integrations.model import OpenAICompatibleProvider as OpenAICompatibleProvider
from scidev.runtime.helpers import new_id
from scidev.runtime.helpers import now_iso
from scidev.agent.attachments import AttachmentError
from scidev.agent.attachments import normalize_images
from scidev.agent.multi_agent import DELEGATE_PARALLEL_RESEARCH_TOOL
from scidev.agent.multi_agent import DELEGATE_RESEARCH_TOOL
from scidev.agent.planning import PLAN_TOOL
from scidev.agent.planning import TaskPlan
from scidev.agent.planning import planning_mode
from scidev.integrations.provider import ProviderConfigError
from scidev.integrations.provider import exists as provider_config_exists
from scidev.integrations.provider import load as load_provider_config
from scidev.agent.usage import ModelRequestUsage
from scidev.integrations.svg import SvgArtifactAdapter as SvgArtifactAdapter


@dataclass
class CodingRunState:
    """Mutable state owned by one task and shared only by its execution phases."""

    allowed_tool_names: set[str]
    context_dirty: bool
    diff_verified_revision: int
    focus_paths: list[str]
    identical_svg_preflight_retries: int
    last_svg_preflight_fingerprint: str
    messages: list[dict[str, Any]]
    payload: dict[str, Any]
    prompt: str
    provider: Any
    recovered_artifact_path: str | None
    required_initial_plan: bool
    session: dict[str, Any]
    session_id: str
    svg_creation_written: bool
    svg_mutation_seen: bool
    svg_preflight_pending: bool
    svg_repair_noop_retries: int
    svg_repair_read_paths: dict[str, str]
    svg_task: bool
    task_id: str
    task_tool_definitions: list[dict[str, Any]]
    tool_definitions: list[dict[str, Any]]
    tool_format_retries: int
    tool_scope_prompt: str
    turn_limit: int
    workspace_revision: int
    empty_response_retries: int = 0
    internal_echo_retries: int = 0
    tool_error_echo_retries: int = 0
    turn: int = 0
    message: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_parse_error: Any = None
    recovered_tool_ids: set[str] = field(default_factory=set)


class AgentRunMixin:
    """Coordinate one task while the Agent retains services and lifecycle ownership."""

    @staticmethod
    def _task_token_usage(session: dict[str, Any]) -> tuple[int, int]:
        current = ModelRequestUsage.snapshot(session)
        baseline = session.get("task_usage_baseline")
        baseline = baseline if isinstance(baseline, dict) else {}
        used = max(0, current["total_tokens"] - ModelRequestUsage._count(baseline.get("total_tokens")))
        reports = max(0, current["token_reports"] - ModelRequestUsage._count(baseline.get("token_reports")))
        return used, reports


    @staticmethod
    def _task_estimated_tokens(session: dict[str, Any]) -> int:
        """Estimate tokens for providers (for example Ollama) that omit usage."""
        current = ModelRequestUsage.snapshot(session)
        baseline = session.get("task_usage_baseline")
        baseline = baseline if isinstance(baseline, dict) else {}
        request_bytes = max(
            0,
            current["request_body_bytes"] - ModelRequestUsage._count(baseline.get("request_body_bytes")),
        )
        response_bytes = max(
            0,
            current["response_body_bytes"] - ModelRequestUsage._count(baseline.get("response_body_bytes")),
        )
        # Four UTF-8 bytes per token is a deliberate approximation; this is
        # only a safety budget, never a billing figure.
        return (request_bytes + response_bytes) // 4


    @staticmethod
    def _should_fallback_provider(exc: Exception) -> bool:
        text = str(exc).casefold()
        if "http 404" in text or "model_not_found" in text or "model not found" in text:
            return True
        return "http 400" in text and "model" in text


    def _build_provider(self) -> OpenAICompatibleProvider:
        if not self.prefer_environment_provider and provider_config_exists(self.project_root):
            try:
                config = load_provider_config(self.project_root, require_key=True)
            except ProviderConfigError as exc:
                raise PermanentError(str(exc)) from exc
            self.provider_reasoning_effort = config.reasoning_effort
            self.provider_fallback_model = config.fallback_model
            return OpenAICompatibleProvider(
                config.base_url,
                config.api_key,
                config.model,
                timeout=config.request_timeout,
                text_tool_call_fallback=config.text_tool_call_fallback,
                streaming=config.streaming,
                presence_penalty=config.presence_penalty,
                temperature=config.temperature,
            )
        self.provider_reasoning_effort = (os.getenv("SCIDEV_REASONING_EFFORT") or "").strip()
        self.provider_fallback_model = (os.getenv("SCIDEV_FALLBACK_MODEL") or "").strip()
        return OpenAICompatibleProvider.from_env()


    def _prepare_coding_run(
        self,
        payload: dict[str, Any],
        task_id: str,
    ) -> dict[str, Any]:
        """Resolve session, provider, tools and per-run flags for one task."""
        session_id = payload["session_id"]
        prompt = payload["prompt"]
        try:
            images = normalize_images(payload.get("images"))
        except AttachmentError as exc:
            raise PermanentError(str(exc)) from exc
        try:
            requested_planning_mode = planning_mode(
                payload.get("planning_mode", "auto")
            )
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
        session = self._load_session(session_id, prompt, images)
        self._recover_interrupted_tool_calls(session)
        retrying_acceptance = (
            session.get("status") == "acceptance_required"
            and session.get("last_prompt") == prompt
        )
        if retrying_acceptance:
            previous_acceptance = session.get("semantic_acceptance_report")
            if previous_acceptance:
                history = list(session.get("semantic_acceptance_history") or [])
                history.append({
                    "task_id": str(session.get("last_task_id") or ""),
                    "prompt": str(prompt)[:1000],
                    "report": previous_acceptance,
                    "archived_at": now_iso(),
                })
                session["semantic_acceptance_history"] = history[-20:]
                session.pop("semantic_acceptance_report", None)
            session["semantic_acceptance_required"] = False
        if session.get("status") in {"failed", "completed", "verification_required", "acceptance_required"}:
            # A user-triggered retry after a terminal model failure gets a fresh,
            # still-bounded completion budget; transient queue retries retain it.
            session["plan_completion_retries"] = 0
            session["verification_required_retries"] = 0
            session["semantic_acceptance_retries"] = 0
        session.setdefault("git_preexisting_paths", sorted(self.git.status_paths()))
        # Baseline for detecting commits made outside the Harness mid-run; the
        # auto-commit step must report such a commit instead of "无新提交".
        session["git_baseline_sha"] = self.git.head_sha()
        session.setdefault("agent_changed_paths", [])
        session.setdefault("verification_runs", [])
        session.setdefault("verification_history", [])
        session.setdefault("verification_required_retries", 0)
        session.setdefault("semantic_acceptance_retries", 0)
        session.setdefault("verification_code_changed", False)
        session.setdefault("verification_edit_revision", 0)
        session.setdefault("verification_paths", sorted(self.git.status_paths()))
        session.setdefault("plan_history", [])
        session.setdefault("specialist_calls", [])
        session.setdefault("specialist_history", [])
        is_new_task = session.get("last_prompt") != prompt
        previous_task_id = str(session.get("last_task_id") or "")
        if task_id and previous_task_id and previous_task_id != task_id and not retrying_acceptance:
            is_new_task = True
        if is_new_task:
            session["multi_agent_config"] = {
                "enabled": bool(self.summary_settings.multi_agent_enabled),
                "model": str(self.summary_settings.multi_agent_model or "").strip()[:160],
            }
        else:
            session.setdefault("multi_agent_config", {
                "enabled": bool(self.summary_settings.multi_agent_enabled),
                "model": str(self.summary_settings.multi_agent_model or "").strip()[:160],
            })
        if is_new_task:
            previous_verification = session.get("verification_report")
            previous_runs = session.get("verification_runs")
            if previous_verification or previous_runs:
                verification_history = list(session.get("verification_history") or [])
                verification_history.append({
                    "task_id": previous_task_id,
                    "prompt": str(session.get("last_prompt") or "")[:1000],
                    "report": previous_verification,
                    "runs": list(previous_runs or [])[-100:],
                    "archived_at": now_iso(),
                })
                session["verification_history"] = verification_history[-20:]
            previous_plan = session.get("task_plan")
            if previous_plan:
                history = list(session.get("plan_history") or [])
                history.append(
                    {
                        "task_id": session.get("plan_task_id", previous_task_id),
                        "plan": previous_plan,
                        "archived_at": now_iso(),
                    }
                )
                session["plan_history"] = history[-20:]
            previous_specialists = session.get("specialist_calls")
            if previous_specialists:
                specialist_history = list(session.get("specialist_history") or [])
                specialist_history.append({
                    "task_id": previous_task_id,
                    "prompt": str(session.get("last_prompt") or "")[:1000],
                    "calls": list(previous_specialists)[-self.MAX_SPECIALIST_CALLS_PER_TASK:],
                    "archived_at": now_iso(),
                })
                session["specialist_history"] = specialist_history[-20:]
            session["specialist_calls"] = []
            session.pop("task_plan", None)
            session["plan_completion_retries"] = 0
            session["planning_mode"] = requested_planning_mode
            session["plan_task_id"] = task_id
            session["git_preexisting_paths"] = sorted(self.git.status_paths())
            session["agent_changed_paths"] = []
            session["verification_runs"] = []
            session["verification_required_retries"] = 0
            session["verification_code_changed"] = False
            session["verification_edit_revision"] = 0
            session["verification_paths"] = sorted(self.git.status_paths())
            session.pop("verification_report", None)
            previous_acceptance = session.get("semantic_acceptance_report")
            if previous_acceptance:
                history = list(session.get("semantic_acceptance_history") or [])
                history.append({
                    "task_id": previous_task_id,
                    "prompt": str(session.get("last_prompt") or "")[:1000],
                    "report": previous_acceptance,
                    "archived_at": now_iso(),
                })
                session["semantic_acceptance_history"] = history[-20:]
            session.pop("semantic_acceptance_report", None)
            session["semantic_acceptance_retries"] = 0
            session["svg_creation_retries"] = 0
            session["svg_repair_noop_retries"] = 0
            if session.get("status") == "cancelled":
                # A stopped task's unfinished tool transcript must not steer
                # the next prompt (weak models otherwise finish the old task).
                session["messages"] = [
                    {"role": "system", "content": self._system_prompt_for_task(prompt)},
                    {"role": "user", "content": f"用户任务：\n{prompt}"},
                ]
            else:
                self._append_followup_prompt(
                    session["messages"],
                    session,
                    session_id,
                    0,
                    f"继续当前编码会话。\n新任务：\n{prompt}",
                    reason="a second user task was supplied to a single-prompt run",
                )
            session["last_prompt"] = prompt
            session["prompt"] = prompt
        else:
            session["planning_mode"] = requested_planning_mode
            if task_id:
                session["plan_task_id"] = task_id
        if task_id:
            if (
                session.get("last_task_id") != task_id
                or session.get("last_prompt") != prompt
            ):
                session["task_usage_baseline"] = ModelRequestUsage.snapshot(session)
            session["last_task_id"] = task_id
        elif session.get("last_prompt") != prompt or "task_usage_baseline" not in session:
            session["task_usage_baseline"] = ModelRequestUsage.snapshot(session)
        # Fail closed on a malformed saved plan: do not silently discard history or
        # let the model continue under an invalid completion gate.
        try:
            saved_plan = TaskPlan.from_record(session.get("task_plan"))
        except ValueError as exc:
            raise PermanentError(f"无法恢复任务计划：{exc}") from exc
        session["status"] = "active"
        if images:
            session["attachments"] = images
            self._attach_images_to_last_user_message(session["messages"], images)
        session["updated_at"] = now_iso()
        self._save_session(session)
        focus_paths = payload.get("context_files", [])
        if not isinstance(focus_paths, list) or any(not isinstance(path, str) for path in focus_paths):
            raise PermanentError("context_files 必须是项目相对路径的字符串列表")
        self._refresh_project_memory(session, prompt, task_id)
        self._refresh_project_context(session, prompt, focus_paths)
        if ModelRequestUsage.snapshot(session)["request_attempts"]:
            self._emit(
                "model_usage_updated",
                {"session_id": session_id, "usage": ModelRequestUsage.snapshot(session), "record": None},
            )
        self._emit(
            "plan_restored",
            {
                "session_id": session_id,
                "task_id": task_id,
                "mode": session["planning_mode"],
                "plan": saved_plan.to_record() if saved_plan else None,
            },
        )
        provider = self._build_provider()
        vision_model = (os.getenv("SCIDEV_VISION_MODEL") or "").strip()
        vision_reasoning_effort = (os.getenv("SCIDEV_VISION_REASONING_EFFORT") or "").strip()
        self.toolbox.visual_reviewer = (
            VisualArtifactReviewer(
                self.project_root,
                provider.with_model(vision_model, streaming=False, temperature=0.0),
                reasoning_effort=vision_reasoning_effort or None,
            )
            if vision_model and isinstance(provider, OpenAICompatibleProvider)
            else None
        )
        messages = session["messages"]
        tool_definitions = self._tool_definitions_for_prompt(prompt)
        svg_task = SvgArtifactAdapter.is_svg_artifact_request(prompt)
        collaboration = session.get("multi_agent_config")
        remaining_specialist_calls = self.MAX_SPECIALIST_CALLS_PER_TASK - len(session.get("specialist_calls") or [])
        if (
            isinstance(collaboration, dict)
            and collaboration.get("enabled") is True
            and not svg_task
            and remaining_specialist_calls > 0
        ):
            tool_definitions.append(copy.deepcopy(DELEGATE_RESEARCH_TOOL))
            if remaining_specialist_calls >= 2:
                tool_definitions.append(copy.deepcopy(DELEGATE_PARALLEL_RESEARCH_TOOL))
        if session["planning_mode"] != "off" and not svg_task:
            tool_definitions.append(copy.deepcopy(PLAN_TOOL))
        task_tool_definitions = list(tool_definitions)
        required_initial_plan = session["planning_mode"] == "required" and saved_plan is None
        if required_initial_plan:
            tool_definitions = [copy.deepcopy(PLAN_TOOL)]
        tool_scope_prompt = prompt
        allowed_tool_names = self._session_tool_names(tool_definitions)
        workspace_revision = 0
        context_dirty = False
        diff_verified_revision = -1
        recovered_artifact_path = ""
        svg_mutation_seen = False
        svg_creation_written = False
        # Standalone targeted repair sessions must follow the same read-then-edit
        # tool sequence as repairs triggered inside an SVG creation session.
        # Otherwise a model can keep choosing read_file/git_diff and never edit.
        svg_preflight_pending = SvgArtifactAdapter.is_local_repair_request(prompt)
        # Keep the original spelling for safe inference on case-sensitive filesystems.
        svg_repair_read_paths: dict[str, str] = {}
        last_svg_preflight_fingerprint: str | None = None
        identical_svg_preflight_retries = 0
        tool_format_retries = 0
        turn_limit = min(self.MAX_TURNS, max(1, int(self.summary_settings.max_agent_turns or self.MAX_TURNS)))
        if svg_task:
            turn_limit = min(turn_limit, self.MAX_SVG_ARTIFACT_TURNS)
        svg_repair_noop_retries = int(session.get("svg_repair_noop_retries", 0) or 0)
        return {
            "allowed_tool_names": allowed_tool_names,
            "context_dirty": context_dirty,
            "diff_verified_revision": diff_verified_revision,
            "focus_paths": focus_paths,
            "identical_svg_preflight_retries": identical_svg_preflight_retries,
            "last_svg_preflight_fingerprint": last_svg_preflight_fingerprint,
            "messages": messages,
            "payload": payload,
            "prompt": prompt,
            "provider": provider,
            "recovered_artifact_path": recovered_artifact_path,
            "required_initial_plan": required_initial_plan,
            "session": session,
            "session_id": session_id,
            "svg_creation_written": svg_creation_written,
            "svg_mutation_seen": svg_mutation_seen,
            "svg_preflight_pending": svg_preflight_pending,
            "svg_repair_noop_retries": svg_repair_noop_retries,
            "svg_repair_read_paths": svg_repair_read_paths,
            "svg_task": svg_task,
            "task_id": task_id,
            "task_tool_definitions": task_tool_definitions,
            "tool_definitions": tool_definitions,
            "tool_format_retries": tool_format_retries,
            "tool_scope_prompt": tool_scope_prompt,
            "turn_limit": turn_limit,
            "workspace_revision": workspace_revision,
        }


    def _request_coding_message(self, coding_run: CodingRunState) -> None:
        self._raise_if_cancelled(coding_run.session, coding_run.session_id)
        max_task_tokens = int(self.summary_settings.max_task_tokens or 0)
        if max_task_tokens:
            used_tokens, token_reports = self._task_token_usage(coding_run.session)
            estimated = False
            if not token_reports:
                estimated_tokens = self._task_estimated_tokens(coding_run.session)
                if estimated_tokens > used_tokens:
                    used_tokens = estimated_tokens
                    estimated = True
            if (token_reports or estimated) and used_tokens >= max_task_tokens:
                coding_run.message = (
                    f"任务 token 预算已用尽（{used_tokens}/{max_task_tokens}）；"
                    "未完成改动保留在工作区，不会自动提交。"
                )
                event = {
                    "session_id": coding_run.session_id,
                    "turn": coding_run.turn,
                    "used_tokens": used_tokens,
                    "limit": max_task_tokens,
                    "estimated": estimated,
                }
                self.ledger.append("task_token_budget_exhausted", event)
                self._emit("task_token_budget_exhausted", event)
                raise PermanentError(coding_run.message)
        if coding_run.context_dirty:
            self._refresh_project_context(coding_run.session, coding_run.prompt, coding_run.focus_paths)
            coding_run.context_dirty = False
        if (
            coding_run.svg_preflight_pending
            and not coding_run.required_initial_plan
            and self.EXPLICIT_COMMAND_INTENT.search(coding_run.prompt) is None
        ):
            inferred_path = (
                next(iter(coding_run.svg_repair_read_paths.values()))
                if len(coding_run.svg_repair_read_paths) == 1
                else ""
            )
            coding_run.tool_definitions = self._svg_preflight_repair_tool_definitions(
                file_was_read=bool(coding_run.svg_repair_read_paths),
                inferred_path=inferred_path,
            )
            coding_run.allowed_tool_names = {
                definition["function"]["name"]
                for definition in coding_run.tool_definitions
                if isinstance(definition, dict)
                and isinstance(definition.get("function"), dict)
                and isinstance(definition["function"].get("name"), str)
            }
        if coding_run.recovered_artifact_path:
            coding_run.message = {
                "role": "assistant",
                "content": f"Saved SVG artifact to {coding_run.recovered_artifact_path}.",
                "tool_calls": [],
            }
        else:
            self.ledger.append("model_call_started", {"session_id": coding_run.session_id, "turn": coding_run.turn})
            self._emit("model_call_started", {"session_id": coding_run.session_id, "turn": coding_run.turn})
            stream_options: dict[str, Any] = {}
            request_scope = self._request_scope_id(
                coding_run.session_id, coding_run.task_id or str(coding_run.session.get("last_task_id") or coding_run.session_id),
            )
            request_id = f"coding-{request_scope}-turn-{coding_run.turn}"
            if isinstance(coding_run.provider, OpenAICompatibleProvider):
                stream_options["on_delta"] = lambda text: self._emit(
                    "assistant_delta",
                    {"session_id": coding_run.session_id, "turn": coding_run.turn, "text": text},
                )
                stream_options["should_cancel"] = self._cancel.is_set
                reasoning_effort = (
                    self.provider_reasoning_effort
                    or (os.getenv("SCIDEV_REASONING_EFFORT") or "").strip()
                )
                if reasoning_effort:
                    stream_options["reasoning_effort"] = reasoning_effort
                if coding_run.required_initial_plan or (
                    coding_run.svg_task and (not coding_run.svg_mutation_seen or coding_run.svg_preflight_pending)
                ):
                    stream_options["tool_choice"] = "required"
                elif coding_run.svg_task:
                    stream_options["tool_choice"] = "auto"
                else:
                    stream_options["tool_choice"] = self._tool_choice_for_tools(coding_run.allowed_tool_names)
                logical_key = f"{coding_run.task_id or coding_run.session_id}|coding:turn:{coding_run.turn}"
                stream_options["on_metrics"] = lambda metrics, key=logical_key: self._record_model_request(
                    coding_run.session, key, "coding", metrics
                )
            request_messages = self._messages_for_model(coding_run.session)
            self._emit(
                "context_usage_updated",
                {
                    "session_id": coding_run.session_id,
                    "estimated_tokens": ModelRequestUsage.estimate_context_tokens(
                        request_messages,
                        coding_run.tool_definitions,
                    ),
                    "context_window_tokens": self.summary_settings.context_window_tokens,
                    "message_count": len(request_messages),
                    "tool_count": len(coding_run.tool_definitions),
                },
            )
            try:
                coding_run.message = coding_run.provider.chat(
                    request_messages,
                    tools=coding_run.tool_definitions,
                    max_tokens=12000,
                    request_id=request_id,
                    **stream_options,
                )
            except TaskCancelled:
                # Turn the stream abort into the normal cancelled session
                # terminal state instead of a generic failure.
                self._raise_if_cancelled(coding_run.session, coding_run.session_id)
                raise
            except TaskAborted:
                raise
            except PermanentError as exc:
                fallback_model = self.provider_fallback_model.strip()
                can_fallback = (
                    fallback_model
                    and fallback_model.casefold() != str(getattr(coding_run.provider, "model", "")).casefold()
                    and not coding_run.session.get("provider_fallback_used")
                    and self._should_fallback_provider(exc)
                    and callable(getattr(coding_run.provider, "with_model", None))
                )
                if not can_fallback:
                    raise
                coding_run.session["provider_fallback_used"] = {
                    "from": str(getattr(coding_run.provider, "model", "")),
                    "to": fallback_model,
                    "error": str(exc)[:300],
                }
                event = {
                    "session_id": coding_run.session_id,
                    "turn": coding_run.turn,
                    "from": str(getattr(coding_run.provider, "model", "")),
                    "to": fallback_model,
                    "error": str(exc)[:300],
                }
                self.ledger.append("provider_fallback_started", event)
                self._emit("provider_fallback_started", event)
                coding_run.provider = coding_run.provider.with_model(fallback_model)
                coding_run.message = coding_run.provider.chat(
                    request_messages,
                    tools=coding_run.tool_definitions,
                    max_tokens=12000,
                    request_id=f"{request_id}-fallback",
                    **stream_options,
                )
                completed_event = {
                    "session_id": coding_run.session_id,
                    "turn": coding_run.turn,
                    "model": fallback_model,
                }
                self.ledger.append("provider_fallback_completed", completed_event)
                self._emit("provider_fallback_completed", completed_event)

    def _process_coding_response(self, coding_run: CodingRunState) -> bool:
        coding_run.content = self._text_content(coding_run.message.get("content"))
        coding_run.tool_calls = coding_run.message.get("tool_calls") or []
        coding_run.tool_parse_error = coding_run.message.get("tool_parse_error")
        self._raise_if_cancelled(coding_run.session, coding_run.session_id)
        if not coding_run.content and not coding_run.tool_calls and not isinstance(coding_run.tool_parse_error, dict):
            # A weak/local model can return an empty message (for example
            # when the prompt exceeds its context). Never report that as a
            # finished task; ask once or twice, then fail explicitly.
            coding_run.empty_response_retries += 1
            if self.single_user_prompt_only or coding_run.empty_response_retries > self.MAX_EMPTY_RESPONSE_RETRIES:
                if self.single_user_prompt_only:
                    error = "模型返回空回复；单用户提示模式不能追加提示，任务已停止。"
                else:
                    error = "模型连续返回空回复；请检查模型上下文窗口、提示词长度或服务状态。"
                coding_run.session["status"] = "failed"
                coding_run.session["error"] = error
                coding_run.session["updated_at"] = now_iso()
                self.ledger.append(
                    "model_empty_response_exhausted",
                    {"session_id": coding_run.session_id, "turn": coding_run.turn, "retries": coding_run.empty_response_retries - 1},
                )
                self._save_session(coding_run.session)
                self._emit("failed", {"session_id": coding_run.session_id, "turn": coding_run.turn, "error": error})
                raise PermanentError(error)
            self.ledger.append(
                "model_empty_response",
                {"session_id": coding_run.session_id, "turn": coding_run.turn, "retry": coding_run.empty_response_retries},
            )
            self._emit(
                "model_empty_response",
                {"session_id": coding_run.session_id, "turn": coding_run.turn, "retry": coding_run.empty_response_retries},
            )
            coding_run.messages.append({
                "role": "user",
                "content": (
                    "你的上一条回复为空。请立即给出下一步："
                    "要么调用一个可用工具，要么给出最终答复。"
                ),
            })
            return False
        if isinstance(coding_run.tool_parse_error, dict) and not coding_run.tool_calls:
            tool_name = str(coding_run.tool_parse_error.get("name", ""))[:80]
            parse_reason = str(coding_run.tool_parse_error.get("reason", "invalid_tool_envelope"))[:80]
            parse_detail = str(coding_run.tool_parse_error.get("detail", ""))[:180]
            coding_run.tool_format_retries += 1
            rejection = {
                "session_id": coding_run.session_id,
                "turn": coding_run.turn,
                "tool_name": tool_name,
                "reason": parse_reason,
                "retry": coding_run.tool_format_retries,
                "retry_limit": self.MAX_TOOL_FORMAT_RETRIES,
            }
            if parse_detail:
                rejection["detail"] = parse_detail
            self.ledger.append("model_tool_call_rejected", rejection)
            self._emit("tool_call_rejected", rejection)
            if coding_run.tool_format_retries > self.MAX_TOOL_FORMAT_RETRIES:
                error = (
                    f"Model repeatedly returned an invalid {tool_name or 'declared tool'} call; "
                    "the Harness rejected it without executing it."
                )
                coding_run.session["status"] = "failed"
                coding_run.session["error"] = error
                coding_run.session["updated_at"] = now_iso()
                self.ledger.append("coding_session_failed", {"session_id": coding_run.session_id, "error": error})
                self._save_session(coding_run.session)
                raise PermanentError(error)
            declared_tools = ", ".join(sorted(coding_run.allowed_tool_names))
            retry_feedback = (
                f"The previous {tool_name or 'tool'} call was rejected before execution ({parse_reason}); "
                f"{parse_detail + '; ' if parse_detail else ''}"
                "no workspace action was executed. Retry using a proper declared function call, "
                f"choosing only from: {declared_tools}. Follow that tool's exact schema and include "
                "only its documented arguments. Do not print a JSON object or claim success as text."
            )
            if self.single_user_prompt_only:
                # Keep the sole user task immutable while feeding the parser's
                # actionable rejection back through the existing conversation.
                coding_run.messages.append({"role": "assistant", "content": retry_feedback})
                self.ledger.append(
                    "harness_followup_suppressed",
                    {
                        "session_id": coding_run.session_id,
                        "turn": coding_run.turn,
                        "reason": "invalid tool-call response",
                        "feedback_source": "tool_protocol",
                    },
                )
            else:
                # Avoid retaining malformed arguments in provider context.
                coding_run.messages.append(
                    {
                        "role": "assistant",
                        "content": "The Harness rejected an invalid tool-call response before execution.",
                    }
                )
                self._append_followup_prompt(
                    coding_run.messages,
                    coding_run.session,
                    coding_run.session_id,
                    coding_run.turn,
                    retry_feedback,
                    reason="invalid tool-call response",
                )
            coding_run.session["updated_at"] = now_iso()
            self._save_session(coding_run.session)
            return False
        if (
            not coding_run.tool_calls
            and coding_run.content.strip().startswith(self.INTERNAL_REJECTION_ECHO)
        ):
            # The model repeated the Harness placeholder instead of doing
            # work and instead of answering. Never present that as a
            # completed task; ask once for a real result, then stop.
            coding_run.internal_echo_retries += 1
            echo_event = {
                "session_id": coding_run.session_id,
                "turn": coding_run.turn,
                "retry": coding_run.internal_echo_retries,
            }
            self.ledger.append("model_internal_prompt_echo", echo_event)
            self._emit("model_internal_prompt_echo", echo_event)
            if coding_run.internal_echo_retries > self.MAX_INTERNAL_ECHO_RETRIES:
                error = (
                    "模型复述了 Harness 的内部提示，没有给出任务结果或调用工具；"
                    "为避免把内部提示当成完成答复，本次任务已停止。请重试或更换模型。"
                )
                coding_run.session["status"] = "failed"
                coding_run.session["error"] = error
                coding_run.session["updated_at"] = now_iso()
                self.ledger.append(
                    "coding_session_failed",
                    {"session_id": coding_run.session_id, "error": error},
                )
                self._save_session(coding_run.session)
                self._emit("failed", {"session_id": coding_run.session_id, "error": error})
                raise PermanentError(error)
            self._append_followup_prompt(
                coding_run.messages,
                coding_run.session,
                coding_run.session_id,
                coding_run.turn,
                "上一条回复复述了 Harness 的内部提示，没有给出任务结果。"
                "请直接给出最终答复，或调用一个可用工具；不要复述内部提示。",
                reason="internal_prompt_echo",
            )
            coding_run.session["updated_at"] = now_iso()
            self._save_session(coding_run.session)
            return False
        last_tool_error = ""
        if not coding_run.tool_calls:
            for coding_run.message in reversed(coding_run.messages):
                if coding_run.message.get("role") == "tool":
                    candidate = str(coding_run.message.get("content") or "").strip()
                    if candidate.startswith(self.TOOL_ERROR_ECHO_PREFIXES):
                        last_tool_error = candidate
                    break
        if (
            not coding_run.tool_calls
            and coding_run.content
            and last_tool_error
            and coding_run.content.strip() == last_tool_error
        ):
            # Some weak models copy the failed tool result verbatim and stop.
            # Do not present that as a completed task.
            coding_run.tool_error_echo_retries += 1
            echo_event = {
                "session_id": coding_run.session_id,
                "turn": coding_run.turn,
                "retry": coding_run.tool_error_echo_retries,
                "tool_error": last_tool_error[:300],
            }
            self.ledger.append("model_tool_error_echo", echo_event)
            self._emit("model_tool_error_echo", echo_event)
            if coding_run.tool_error_echo_retries > self.MAX_TOOL_ERROR_ECHO_RETRIES:
                error = (
                    "模型复述了工具执行失败信息，没有修正问题或给出任务结果；"
                    "为避免把错误当成完成答复，本次任务已停止。"
                )
                coding_run.session["status"] = "failed"
                coding_run.session["error"] = error
                coding_run.session["updated_at"] = now_iso()
                self.ledger.append(
                    "coding_session_failed",
                    {"session_id": coding_run.session_id, "error": error},
                )
                self._save_session(coding_run.session)
                self._emit("failed", {"session_id": coding_run.session_id, "error": error})
                raise PermanentError(error)
            self._append_followup_prompt(
                coding_run.messages,
                coding_run.session,
                coding_run.session_id,
                coding_run.turn,
                "上一条回复原样复述了工具执行失败信息，没有修正问题或给出最终结果。"
                "请根据错误提示修正后再试一次；如果无法完成，请明确说明原因。",
                reason="tool_error_echo",
            )
            coding_run.session["updated_at"] = now_iso()
            self._save_session(coding_run.session)
            return False
        coding_run.recovered_tool_ids: set[str] = set()
        if not coding_run.tool_calls and coding_run.content:
            recovered = SvgArtifactAdapter.create_tool_call(coding_run.prompt, coding_run.content, self.project_root)
            if recovered:
                recovered_name = str(recovered.get("function", {}).get("name", ""))
                if recovered_name not in coding_run.allowed_tool_names:
                    coding_run.tool_format_retries += 1
                    rejection = {
                        "session_id": coding_run.session_id,
                        "turn": coding_run.turn,
                        "tool_name": recovered_name,
                        "reason": "recovered_action_not_allowed_in_current_phase",
                        "retry": coding_run.tool_format_retries,
                        "retry_limit": self.MAX_TOOL_FORMAT_RETRIES,
                    }
                    self.ledger.append("artifact_response_recovery_rejected", rejection)
                    self._emit("artifact_response_recovery_rejected", rejection)
                    if coding_run.tool_format_retries > self.MAX_TOOL_FORMAT_RETRIES:
                        error = (
                            f"The model returned a complete SVG that requires disallowed "
                            f"{recovered_name or 'file-write'} in the current repair phase; "
                            "no workspace action was applied."
                        )
                        coding_run.session["status"] = "failed"
                        coding_run.session["error"] = error
                        coding_run.session["updated_at"] = now_iso()
                        self.ledger.append(
                            "coding_session_failed",
                            {"session_id": coding_run.session_id, "error": error},
                        )
                        self._save_session(coding_run.session)
                        raise PermanentError(error)
                    coding_run.messages.append(
                        {
                            "role": "assistant",
                            "content": "The complete-artifact response was not applied; no edit was made.",
                        }
                    )
                    available_tools = ", ".join(sorted(coding_run.allowed_tool_names))
                    if self.single_user_prompt_only:
                        self.ledger.append(
                            "harness_followup_suppressed",
                            {
                                "session_id": coding_run.session_id,
                                "turn": coding_run.turn,
                                "reason": "artifact response required a disallowed tool",
                                "available_tools": sorted(coding_run.allowed_tool_names),
                                "feedback_source": "tool_protocol",
                            },
                        )
                    else:
                        self._append_followup_prompt(
                            coding_run.messages,
                            coding_run.session,
                            coding_run.session_id,
                            coding_run.turn,
                            (
                                "A complete SVG response is not an allowed action in this repair phase. "
                                f"Use only the currently declared tool(s): {available_tools}. "
                                "For a localized repair, use replace_in_file with exact old_text/new_text "
                                "from the latest read; preserve unrelated artwork and do not resend the file."
                            ),
                            reason="artifact response required a disallowed tool",
                        )
                    coding_run.session["updated_at"] = now_iso()
                    self._save_session(coding_run.session)
                    return False
                coding_run.tool_calls = [recovered]
                coding_run.recovered_tool_ids.add(str(recovered["id"]))
                self.ledger.append(
                    "artifact_response_recovered",
                    {
                        "session_id": coding_run.session_id,
                        "format": "svg",
                        "path": json.loads(recovered["function"]["arguments"])["path"],
                    },
                )
        return True

    def _complete_coding_turn(self, coding_run: CodingRunState) -> tuple[bool, dict[str, Any] | None]:
        current_plan = TaskPlan.from_record(coding_run.session.get("task_plan"))
        plan_incomplete = (
            (coding_run.session["planning_mode"] == "required" and current_plan is None)
            or (current_plan is not None and not current_plan.finished)
        )
        if plan_incomplete:
            retries = int(coding_run.session.get("plan_completion_retries", 0) or 0)
            if retries < self.MAX_PLAN_COMPLETION_RETRIES:
                retries += 1
                coding_run.session["plan_completion_retries"] = retries
                reason = (
                    "必须先创建任务计划"
                    if current_plan is None
                    else "仍有步骤未完成或被阻塞"
                )
                feedback = (
                    f"任务尚未完成：{reason}。请继续实际执行并使用 update_plan 更新步骤；"
                    "只有所有步骤均为 completed 或有说明的 skipped 后才能结束。"
                )
                coding_run.messages.append({"role": "assistant", "content": feedback})
                state = {
                    "session_id": coding_run.session_id,
                    "attempt": retries,
                    "retry_limit": self.MAX_PLAN_COMPLETION_RETRIES,
                    "reason": reason,
                }
                self.ledger.append("task_plan_incomplete", state)
                self._emit("plan_incomplete", state)
                coding_run.session["updated_at"] = now_iso()
                self._save_session(coding_run.session)
                return True, None
            error = "任务计划仍有未完成步骤；已停止自动结束，计划已保留，可重试继续。"
            coding_run.session["status"] = "failed"
            coding_run.session["error"] = error
            coding_run.session["updated_at"] = now_iso()
            failure = {"session_id": coding_run.session_id, "error": error}
            self.ledger.append("coding_session_failed", failure)
            self._emit("plan_incomplete", failure | {"exhausted": True})
            self._save_session(coding_run.session)
            raise PermanentError(error)
        preexisting_paths = set(coding_run.session.get("git_preexisting_paths") or [])
        changed_paths = (self.git.status_paths() - preexisting_paths) | (
            set(coding_run.session.get("agent_changed_paths") or []) - preexisting_paths
        )
        has_svg_output = any(path.casefold().endswith(".svg") for path in changed_paths)
        svg_retries = int(coding_run.session.get("svg_creation_retries", 0) or 0)
        missing_svg = SvgArtifactAdapter.is_creation_request(coding_run.prompt) and not has_svg_output
        if missing_svg and svg_retries < self.MAX_SVG_CREATION_RETRIES:
            if coding_run.content:
                self._emit("assistant", {"session_id": coding_run.session_id, "turn": coding_run.turn, "text": coding_run.content})
            # A malformed, very long artifact response can dominate the
            # next prompt and make a local model repeat the same failure.
            # Keep the audit event, but replace that non-actionable turn
            # in session context with a concise marker before retrying.
            if coding_run.messages and coding_run.messages[-1].get("role") == "assistant" and not coding_run.messages[-1].get("tool_calls"):
                coding_run.messages[-1] = {
                    "role": "assistant",
                    "content": "The previous response did not save an SVG file.",
                }
            svg_retries += 1
            coding_run.session["svg_creation_retries"] = svg_retries
            self._append_followup_prompt(
                coding_run.messages,
                coding_run.session,
                coding_run.session_id,
                coding_run.turn,
                (
                    "Your previous reply did not create an SVG file, so this task is not complete. "
                    "Immediately call the declared write_file tool exactly once with a safe .svg "
                    "filename and complete SVG markup. Do not return a JSON tool-call object or "
                    "wrap the tool call in XML. If a tool call cannot be emitted, return only one "
                    "complete fenced svg block. Give each requested key part a unique semantic `id` "
                    "on its visible SVG element, show requested actions and relationships through "
                    "actual shape contact, and keep all geometry inside the viewBox."
                ),
                reason="SVG creation returned without an artifact",
            )
            retry_event = {
                "session_id": coding_run.session_id,
                "attempt": svg_retries,
                "reason": "explicit SVG creation returned without creating an SVG file",
            }
            self.ledger.append("svg_creation_retry_scheduled", retry_event)
            self._emit("svg_creation_retry_scheduled", retry_event)
            coding_run.session["updated_at"] = now_iso()
            self._save_session(coding_run.session)
            return True, None
        if missing_svg:
            coding_run.session["status"] = "failed"
            coding_run.session["error"] = "模型在一次自动补救后仍未创建 SVG 文件；任务未标记为完成。"
            coding_run.session["updated_at"] = now_iso()
            failure = {
                "session_id": coding_run.session_id,
                "error": coding_run.session["error"],
                "svg_creation_retries": svg_retries,
            }
            self.ledger.append("coding_session_failed", failure)
            self._save_session(coding_run.session)
            raise PermanentError(coding_run.session["error"])
        if SvgArtifactAdapter.is_repair_request(coding_run.prompt) and not changed_paths:
            if coding_run.svg_repair_noop_retries < self.MAX_SVG_REPAIR_NOOP_RETRIES and coding_run.turn < coding_run.turn_limit:
                coding_run.svg_repair_noop_retries += 1
                coding_run.session["svg_repair_noop_retries"] = coding_run.svg_repair_noop_retries
                if coding_run.messages and coding_run.messages[-1].get("role") == "assistant" and not coding_run.messages[-1].get("tool_calls"):
                    coding_run.messages[-1] = {
                        "role": "assistant",
                        "content": "The previous response did not modify the requested SVG.",
                    }
                self._append_followup_prompt(
                    coding_run.messages,
                    coding_run.session,
                    coding_run.session_id,
                    coding_run.turn,
                    (
                        "This is a targeted SVG repair task, but no file edit has been applied. "
                        "The task is not complete. Re-read the current file if needed, then use "
                        "replace_in_file for the exact reported issue. Preserve unrelated artwork; "
                        "do not claim success without an actual edit."
                    ),
                    reason="SVG repair returned without an edit",
                )
                retry_event = {
                    "session_id": coding_run.session_id,
                    "turn": coding_run.turn,
                    "attempt": coding_run.svg_repair_noop_retries,
                    "reason": "SVG repair response returned without a workspace edit",
                }
                self.ledger.append("svg_repair_retry_scheduled", retry_event)
                self._emit("svg_repair_retry_scheduled", retry_event)
                coding_run.session["updated_at"] = now_iso()
                self._save_session(coding_run.session)
                return True, None
            error = (
                "The targeted SVG repair returned without changing the file after its bounded retry; "
                "the session was marked failed and no commit was created."
            )
            coding_run.session["status"] = "failed"
            coding_run.session["error"] = error
            coding_run.session["updated_at"] = now_iso()
            self.ledger.append("coding_session_failed", {"session_id": coding_run.session_id, "error": error})
            self._save_session(coding_run.session)
            raise PermanentError(error)
        svg_preflight_failures: list[tuple[str, str]] = []
        if coding_run.svg_task:
            for artifact_path in sorted(path for path in changed_paths if path.casefold().endswith(".svg")):
                try:
                    artifact_issues = self.toolbox.svg_structure_issues_for_file(artifact_path)
                except Exception as exc:
                    artifact_issues = [f"无法完成结构预检（{type(exc).__name__}）"]
                svg_preflight_failures.extend(
                    (artifact_path, issue) for issue in artifact_issues
                )
        if svg_preflight_failures:
            compact_issues = "\n".join(
                f"- {path}: {issue}" for path, issue in svg_preflight_failures[:8]
            )
            failure_paths = {
                self._normalized_tool_path(path) for path, _issue in svg_preflight_failures
            }
            already_read_current = bool(failure_paths) and failure_paths.issubset(
                coding_run.svg_repair_read_paths
            )
            fingerprint = self._svg_preflight_fingerprint(svg_preflight_failures)
            if fingerprint == coding_run.last_svg_preflight_fingerprint:
                coding_run.identical_svg_preflight_retries += 1
            else:
                coding_run.identical_svg_preflight_retries = 0
            coding_run.last_svg_preflight_fingerprint = fingerprint
            if coding_run.identical_svg_preflight_retries >= self.MAX_IDENTICAL_SVG_PREFLIGHT_RETRIES:
                error = (
                    f"SVG 结构预检连续 {self.MAX_IDENTICAL_SVG_PREFLIGHT_RETRIES} 次重试后，问题和文件均未变化；"
                    "已停止空转，文件保留在工作区，未自动提交。\n" + compact_issues
                )
                self._fail_svg_preflight(
                    coding_run.session,
                    coding_run.session_id,
                    coding_run.turn,
                    compact_issues,
                    error,
                    stalled=True,
                )
            if coding_run.turn >= coding_run.turn_limit:
                error = (
                    f"SVG 结构预检在 {coding_run.turn_limit} 个 Agent 回合后仍未通过；"
                    "文件保留在工作区，未自动提交。\n" + compact_issues
                )
                self._fail_svg_preflight(coding_run.session, coding_run.session_id, coding_run.turn, compact_issues, error)
            if coding_run.messages and coding_run.messages[-1].get("role") == "assistant" and not coding_run.messages[-1].get("tool_calls"):
                coding_run.messages.pop()
            if already_read_current:
                repair_step = (
                    "The current file was already read and has not changed. Do not call read_file again. "
                    "Immediately use replace_in_file for the reported issue, copying exact old_text from "
                    "the latest read; do not ask the user to provide the file or clarify the repair."
                )
            else:
                repair_step = (
                    "Call read_file on the current SVG now. Then make focused exact edits with "
                    "replace_in_file; preserve valid artwork and avoid broad replacement."
                )
            if self.single_user_prompt_only:
                # The validation result is already present in the preceding
                # write/replace tool response. Let the model continue from
                # that evidence without appending another user instruction.
                self.ledger.append(
                    "harness_followup_suppressed",
                    {
                        "session_id": coding_run.session_id,
                        "turn": coding_run.turn,
                        "reason": "SVG structural preflight failed",
                        "feedback_source": "tool_result",
                    },
                )
            else:
                self._append_followup_prompt(
                    coding_run.messages,
                    coding_run.session,
                    coding_run.session_id,
                    coding_run.turn,
                    (
                        "The SVG was saved, but the Harness structural preflight found unresolved issues:\n"
                        f"{compact_issues}\n"
                        f"{repair_step} Re-read after any edit attempt that changes the file or returns "
                        "an error; never reuse stale old_text. The user's original request remains "
                        "unchanged. After edits, wait for the structural preflight result. This preflight "
                        "does not prove visual rendering or semantic correctness."
                    ),
                    reason="SVG structural preflight failed",
                )
            coding_run.svg_preflight_pending = True
            if not already_read_current:
                coding_run.svg_repair_read_paths.clear()
            if self.EXPLICIT_COMMAND_INTENT.search(coding_run.prompt) is None:
                coding_run.tool_scope_prompt = (
                    "Repair only these localized SVG issues in the SVG just created; preserve valid artwork. "
                    "Use exact replace_in_file edits and inspect git_diff."
                )
                coding_run.tool_definitions = self._svg_artifact_repair_tool_definitions()
                coding_run.allowed_tool_names = {item["function"]["name"] for item in coding_run.tool_definitions}
            retry_event = {
                "session_id": coding_run.session_id,
                "turn": coding_run.turn,
                "issues": compact_issues[:1800],
            }
            self.ledger.append("svg_preflight_retry_scheduled", retry_event)
            self._emit("svg_preflight_retry_scheduled", retry_event)
            coding_run.recovered_artifact_path = ""
            coding_run.session["updated_at"] = now_iso()
            self._save_session(coding_run.session)
            return True, None
        if changed_paths and coding_run.diff_verified_revision != coding_run.workspace_revision:
            # The model's premature final text is not shown or retained;
            # it must review the actual diff before producing a final answer.
            coding_run.messages.pop()
            call_id = new_id("call")
            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": "git_diff", "arguments": "{}"},
            }
            coding_run.messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
            self.ledger.append(
                "tool_call_started",
                {
                    "session_id": coding_run.session_id,
                    "name": "git_diff",
                    "source": "harness_precommit",
                    "reason": "unverified_agent_changes",
                },
            )
            self._emit(
                "tool_started",
                {
                    "session_id": coding_run.session_id,
                    "name": "git_diff",
                    "arguments": {},
                    "source": "harness_precommit",
                },
            )
            try:
                diff_result = str(self.toolbox.execute("git_diff", {}))[-30000:]
            except Exception as exc:
                if isinstance(exc, subprocess.CalledProcessError):
                    command = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]
                    command_summary = " ".join(str(part) for part in command[:3])
                    if len(command) > 3:
                        command_summary += " …"
                    detail = f"{command_summary}（退出码 {exc.returncode}）"
                    stderr = str(exc.stderr or exc.output or "").strip()
                    if stderr:
                        detail += f"：{stderr[-1200:]}"
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                error = f"提交前 Git diff 检查失败：{detail}"
                self.ledger.append(
                    "tool_result",
                    {
                        "session_id": coding_run.session_id,
                        "name": "git_diff",
                        "source": "harness_precommit",
                        "error": error[:1000],
                    },
                )
                self._emit(
                    "tool_result",
                    {
                        "session_id": coding_run.session_id,
                        "name": "git_diff",
                        "source": "harness_precommit",
                        "result": error,
                    },
                )
                coding_run.session["status"] = "failed"
                coding_run.session["error"] = error
                coding_run.session["updated_at"] = now_iso()
                self._save_session(coding_run.session)
                raise PermanentError(error) from exc
            coding_run.messages.append({"role": "tool", "tool_call_id": call_id, "content": diff_result})
            self.ledger.append(
                "tool_result",
                {
                    "session_id": coding_run.session_id,
                    "name": "git_diff",
                    "source": "harness_precommit",
                    "result": diff_result[:3000],
                },
            )
            self._emit(
                "tool_result",
                {
                    "session_id": coding_run.session_id,
                    "name": "git_diff",
                    "result": diff_result,
                    "source": "harness_precommit",
                },
            )
            coding_run.diff_verified_revision = coding_run.workspace_revision
            coding_run.session["updated_at"] = now_iso()
            self._save_session(coding_run.session)
            return True, None

        retry_scheduled, verification_report = self._enforce_verification_gate(
            coding_run.session, coding_run.session_id, coding_run.prompt, coding_run.messages, coding_run.turn, coding_run.content,
        )
        if retry_scheduled:
            return True, None

        accept_retry, acceptance_report, coding_run.content = self._run_acceptance_stage(
            coding_run.provider, coding_run.session, coding_run.session_id, coding_run.prompt, coding_run.content, changed_paths, coding_run.turn, coding_run.messages,
        )
        if accept_retry:
            return True, None

        self._raise_if_cancelled(coding_run.session, coding_run.session_id)
        return False, self._finish_coding_session(
            coding_run.provider, coding_run.session, coding_run.session_id, coding_run.prompt, coding_run.content, changed_paths, coding_run.turn,
            acceptance_report, verification_report,
        )

    def _run_with_editor_context(self, task: dict[str, Any]) -> dict[str, Any]:
        coding_run = CodingRunState(**self._prepare_coding_run(
            task["payload"], str(task.get("task_id") or ""),
        ))

        for coding_run.turn in range(1, coding_run.turn_limit + 1):
            self._request_coding_message(coding_run)
            if not self._process_coding_response(coding_run):
                continue
            # Ollama templates commonly serialize assistant content *instead of*
            # tool calls when both fields are present. Keep narration in the UI
            # event below, but give the next model turn an unambiguous call record.
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": "" if coding_run.tool_calls else coding_run.content,
            }
            if coding_run.tool_calls:
                for call in coding_run.tool_calls:
                    if isinstance(call, dict) and not call.get("id"):
                        call["id"] = new_id("call")
                assistant_message["tool_calls"] = coding_run.tool_calls
            assistant_message_index = len(coding_run.messages)
            coding_run.messages.append(assistant_message)
            if coding_run.content and coding_run.tool_calls:
                display_text = coding_run.content
                if coding_run.recovered_tool_ids and len(coding_run.recovered_tool_ids) == len(coding_run.tool_calls):
                    display_text = "Harness 识别到 SVG 文件内容，正在安全保存。"
                self._emit("assistant", {"session_id": coding_run.session_id, "turn": coding_run.turn, "text": display_text})
            if coding_run.tool_calls:
                self.ledger.prepare_tool_calls(coding_run.session_id, assistant_message_index, coding_run.tool_calls)
                self._save_session(coding_run.session)

            if not coding_run.tool_calls:
                retry_completion, result = self._complete_coding_turn(coding_run)
                if retry_completion:
                    continue
                return result

            tool_state = self._execute_tool_calls(
                session=coding_run.session, session_id=coding_run.session_id, prompt=coding_run.prompt, task_id=coding_run.task_id,
                provider=coding_run.provider, messages=coding_run.messages, tool_calls=coding_run.tool_calls, turn=coding_run.turn,
                assistant_message_index=assistant_message_index,
                recovered_tool_ids=coding_run.recovered_tool_ids, svg_task=coding_run.svg_task,
                svg_repair_read_paths=coding_run.svg_repair_read_paths,
                tool_definitions=coding_run.tool_definitions,
                recovered_artifact_path=coding_run.recovered_artifact_path,
                svg_creation_written=coding_run.svg_creation_written,
                svg_preflight_pending=coding_run.svg_preflight_pending,
                workspace_revision=coding_run.workspace_revision,
                required_initial_plan=coding_run.required_initial_plan,
                task_tool_definitions=coding_run.task_tool_definitions,
                tool_scope_prompt=coding_run.tool_scope_prompt,
                allowed_tool_names=coding_run.allowed_tool_names,
                context_dirty=coding_run.context_dirty,
                diff_verified_revision=coding_run.diff_verified_revision,
                svg_mutation_seen=coding_run.svg_mutation_seen,
            )
            coding_run.tool_definitions = tool_state["tool_definitions"]
            coding_run.recovered_artifact_path = tool_state["recovered_artifact_path"]
            coding_run.svg_creation_written = tool_state["svg_creation_written"]
            coding_run.svg_preflight_pending = tool_state["svg_preflight_pending"]
            coding_run.svg_mutation_seen = tool_state["svg_mutation_seen"]
            coding_run.context_dirty = tool_state["context_dirty"]
            coding_run.diff_verified_revision = tool_state["diff_verified_revision"]
            coding_run.allowed_tool_names = tool_state["allowed_tool_names"]
            coding_run.task_tool_definitions = tool_state["task_tool_definitions"]
            coding_run.tool_scope_prompt = tool_state["tool_scope_prompt"]
            coding_run.workspace_revision = tool_state["workspace_revision"]
            coding_run.required_initial_plan = tool_state["required_initial_plan"]
            if (
                coding_run.svg_creation_written
                and self.EXPLICIT_COMMAND_INTENT.search(coding_run.prompt) is None
            ):
                coding_run.tool_scope_prompt = (
                    "Repair only these localized SVG issues in the SVG just created; preserve valid artwork. "
                    "Use exact replace_in_file edits and inspect git_diff."
                )
                coding_run.tool_definitions = self._svg_artifact_repair_tool_definitions()
                coding_run.allowed_tool_names = {
                    definition["function"]["name"]
                    for definition in coding_run.tool_definitions
                    if isinstance(definition, dict)
                    and isinstance(definition.get("function"), dict)
                    and isinstance(definition["function"].get("name"), str)
                }

            interval = self.summary_settings.interval_turns
            if (
                not coding_run.recovered_artifact_path
                and self.summary_settings.enabled
                and interval > 0
                and coding_run.turn % interval == 0
            ):
                coding_run.session["updated_at"] = now_iso()
                self._summarize_session(coding_run.provider, coding_run.session, phase="checkpoint", turn=coding_run.turn)
                self._save_session(coding_run.session)

        coding_run.session["status"] = "failed"
        coding_run.session["error"] = f"超过最大 Agent 步数：{coding_run.turn_limit}"
        self._save_session(coding_run.session)
        raise PermanentError(coding_run.session["error"])
