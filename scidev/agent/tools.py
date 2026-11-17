"""Execute model tool batches with approval and SVG edit restrictions.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from scidev.integrations.browser import BrowserApprovalDenied
from scidev.runtime.errors import PermanentError
from scidev.runtime.errors import TaskAborted
from scidev.runtime.errors import TaskCancelled
from scidev.runtime.helpers import new_id
from scidev.runtime.helpers import now_iso
from scidev.workspace.patching import PatchReviewRejected
from scidev.agent.verification import VerificationTracker
from scidev.integrations.svg import SvgArtifactAdapter as SvgArtifactAdapter


class AgentToolsMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _svg_artifact_repair_tool_definitions(self) -> list[dict[str, Any]]:
        """Return local-only tools for post-write SVG repair; never offer a broad rewrite."""
        selected = [
            definition
            for definition in self.toolbox.all_definitions()
            if definition.get("function", {}).get("name") in self.SVG_LOCAL_REPAIR_TOOLS
        ]
        for definition in selected:
            if definition.get("function", {}).get("name") != "replace_in_file":
                continue
            properties = definition.get("function", {}).get("parameters", {}).get("properties", {})
            properties.pop("replace_all", None)
            edit_properties = properties.get("edits", {}).get("items", {}).get("properties", {})
            edit_properties.pop("replace_all", None)
        return selected


    def _svg_preflight_repair_tool_definitions(
        self,
        *,
        file_was_read: bool,
        inferred_path: str = "",
    ) -> list[dict[str, Any]]:
        """Require read-then-edit; only relax path when one target was just read."""
        required_name = "replace_in_file" if file_was_read else "read_file"
        required_names = {"replace_in_file", "apply_patch"} if file_was_read else {"read_file"}
        selected = [
            definition
            for definition in self._svg_artifact_repair_tool_definitions()
            if definition.get("function", {}).get("name") in required_names
        ]
        if required_name == "replace_in_file" and inferred_path:
            for definition in selected:
                parameters = definition.get("function", {}).get("parameters", {})
                path_schema = parameters.get("properties", {}).get("path")
                if not isinstance(path_schema, dict):
                    continue
                path_schema["enum"] = [inferred_path]
                path_schema["description"] = (
                    "Omit only when editing the sole file just read; the Harness will bind this exact path. "
                    "If supplied, it must match this path."
                )
                parameters["required"] = [
                    name for name in parameters.get("required", []) if name != "path"
                ]
        return selected


    def _normalized_tool_path(self, raw_path: Any) -> str:
        raw = str(raw_path or "")
        try:
            target = self.toolbox._resolve(raw, allow_root=False)
            return target.relative_to(self.project_root).as_posix().casefold()
        except (OSError, ValueError, RuntimeError):
            return raw.replace("\\", "/").casefold()


    @classmethod
    def _tool_choice_for_tools(cls, tool_names: set[str]) -> str:
        """Require a tool for scoped SVG artifact creation and repair operations."""
        required_tool_sets = {
            cls.SVG_CREATION_TOOLS,
            cls.SVG_LOCAL_REPAIR_TOOLS,
            cls.SVG_ARTIFACT_TOOLS,
        }
        return "required" if any(tool_names == allowed for allowed in required_tool_sets) else "auto"


    @staticmethod
    def _validate_tool_edit_scope(prompt: str, name: str, arguments: dict[str, Any]) -> None:
        """Prevent broad replacements during a targeted SVG repair."""
        if (
            name != "replace_in_file"
            or not SvgArtifactAdapter.is_local_repair_request(prompt)
            or not str(arguments.get("path", "")).casefold().endswith(".svg")
        ):
            return
        edits = arguments.get("edits")
        bulk_requested = arguments.get("replace_all") is True
        if isinstance(edits, list):
            bulk_requested = bulk_requested or any(
                isinstance(edit, dict) and edit.get("replace_all") is True for edit in edits
            )
        if bulk_requested:
            raise PermanentError(
                "replace_all is disabled for a targeted SVG repair; use exact unique old_text for each shape"
            )


    def _svg_preflight_fingerprint(self, failures: list[tuple[str, str]]) -> str:
        digest = hashlib.sha256()
        paths = sorted({path for path, _issue in failures})
        for path, issue in sorted(failures):
            digest.update(path.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(issue.encode("utf-8", errors="replace"))
            digest.update(b"\0")
        for path in paths:
            digest.update(path.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            try:
                target = self.toolbox._resolve(path, allow_root=False)
                digest.update(target.read_bytes())
            except OSError as exc:
                digest.update(type(exc).__name__.encode("ascii", errors="ignore"))
            digest.update(b"\0")
        return digest.hexdigest()


    def _fail_svg_preflight(
        self,
        session: dict[str, Any],
        session_id: str,
        turn: int,
        issues: str,
        error: str,
        *,
        stalled: bool = False,
    ) -> None:
        session["status"] = "failed"
        session["error"] = error[:3000]
        session["updated_at"] = now_iso()
        failure = {"session_id": session_id, "turn": turn, "issues": issues[:1800]}
        self.ledger.append("svg_preflight_failed", failure)
        if stalled:
            self.ledger.append("svg_preflight_stalled", failure)
        self.ledger.append("coding_session_failed", failure | {"error": error[:1000]})
        self._save_session(session)
        raise PermanentError(error)


    def _execute_tool_calls(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
        prompt: str,
        task_id: str,
        provider: Any,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        turn: int,
        assistant_message_index: int,
        recovered_tool_ids: set[str],
        svg_task: bool,
        svg_repair_read_paths: dict[str, str],
        tool_definitions: list[dict[str, Any]],
        recovered_artifact_path: str | None,
        svg_creation_written: bool,
        svg_preflight_pending: bool,
        workspace_revision: int,
        required_initial_plan: bool,
        task_tool_definitions: list[dict[str, Any]],
        tool_scope_prompt: str,
        allowed_tool_names: set[str],
        context_dirty: bool,
        diff_verified_revision: int,
        svg_mutation_seen: bool,
    ) -> dict[str, Any]:
        """Execute this turn's tool calls and report the state they changed."""
        for call_index, call in enumerate(tool_calls):
            self._raise_if_cancelled(session, session_id)
            function = call.get("function", {}) if isinstance(call, dict) else {}
            name = str(function.get("name", ""))
            call_id = str(call.get("id", new_id("tool")))
            raw_arguments = function.get("arguments", "{}")
            tool_succeeded = False
            tool_journal_started = False
            normalized_tool_path = ""
            command_for_record = ""
            patch_stop_reason: str | None = None
            browser_action_denied = False
            patch_partial = False
            patch_applied_paths: list[str] = []
            # Weak models routinely emit malformed native tool arguments. Keep a
            # placeholder so the error can be reported back to the model instead
            # of crashing the turn with UnboundLocalError.
            arguments: dict[str, Any] = {}
            verification_revision = int(session.get("verification_edit_revision", 0) or 0)
            verification_before = (
                self._verification_snapshot(session) if name == "run_command" else None
            )
            if name == "run_command":
                self.toolbox.last_command_execution = None
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                self.hooks.run("before_tool", {
                    "session_id": session_id,
                    "turn": turn,
                    "tool": name,
                    "arguments": arguments,
                }, blocking=True)
                if name == "run_command":
                    command_for_record = str(arguments.get("command") or "")
                if name not in allowed_tool_names:
                    raise PermanentError(f"Tool {name!r} is not available for this task.")
                if (
                    name in self.MUTATING_TOOL_NAMES
                    and self.prompt_forbids_mutating_tools(prompt)
                ):
                    attempts = int(session.get("forbidden_mutation_attempts", 0) or 0) + 1
                    session["forbidden_mutation_attempts"] = attempts
                    if attempts >= 2:
                        message = (
                            "用户明确要求只给示例或不要调用工具，但模型仍重复调用写操作；"
                            "Harness 已拒绝并停止任务，未修改任何文件。"
                        )
                        self.ledger.append(
                            "forbidden_mutation_repeated",
                            {
                                "session_id": session_id,
                                "tool": name,
                                "attempts": attempts,
                            },
                        )
                        raise TaskAborted(message)
                    raise PermanentError(
                        "用户明确要求只给示例或不要实际执行/调用工具；Harness 已拒绝该写操作，"
                        "请直接用文字回复，不要再次调用工具。"
                    )
                if (
                    svg_preflight_pending
                    and name == "replace_in_file"
                    and "path" not in arguments
                    and len(svg_repair_read_paths) == 1
                ):
                    arguments["path"] = next(iter(svg_repair_read_paths.values()))
                    inference_event = {
                        "session_id": session_id,
                        "turn": turn,
                        "tool_name": name,
                        "argument": "path",
                        "reason": "single_recently_read_target",
                    }
                    self.ledger.append("tool_argument_inferred", inference_event)
                    self._emit("tool_argument_inferred", inference_event)
                normalized_tool_path = self._normalized_tool_path(arguments.get("path", ""))
                if (
                    svg_preflight_pending
                    and name == "replace_in_file"
                    and normalized_tool_path not in svg_repair_read_paths
                ):
                    raise PermanentError(
                        "Read the saved SVG with read_file immediately before editing it; "
                        "then use an exact replace_in_file edit."
                    )
                if svg_preflight_pending and name == "apply_patch":
                    patch_plan = self.toolbox.patch_engine.prepare(str(arguments.get("patch") or ""))
                    read_paths = set(svg_repair_read_paths)
                    if (
                        len(patch_plan.files) != 1
                        or patch_plan.files[0].path.casefold() not in read_paths
                        or not patch_plan.files[0].path.casefold().endswith(".svg")
                        or patch_plan.files[0].operation != "update"
                    ):
                        raise PermanentError(
                            "SVG 局部修复补丁必须只修改刚由 read_file 读取的现有 SVG；"
                            "不能新增、删除或修改其他文件。"
                        )
                if (
                    SvgArtifactAdapter.is_creation_request(prompt)
                    and svg_creation_written
                    and name == "write_file"
                ):
                    raise PermanentError("Initial SVG creation already wrote a file; use focused edit tools to repair it.")
                self._validate_tool_edit_scope(tool_scope_prompt, name, arguments)
                journal_record = self.ledger.start_tool_call(
                    session_id, assistant_message_index, call_index, call_id, name,
                )
                tool_journal_started = bool(journal_record and journal_record.get("should_execute"))
                if tool_journal_started:
                    start_event = {
                        "session_id": session_id,
                        "assistant_message_index": assistant_message_index,
                        "call_index": call_index,
                        "call_id": call_id,
                        "name": name,
                    }
                    self.ledger.append("tool_call_started", start_event)
                    tool_event = {"session_id": session_id, "name": name, "arguments": arguments}
                    if call_id in recovered_tool_ids:
                        tool_event["source"] = "harness_svg_artifact_recovery"
                    self._emit("tool_started", tool_event)
                    if name == "update_plan":
                        result = self._apply_task_plan(session, session_id, prompt, arguments)
                    elif name == "delegate_research":
                        self.toolbox.last_patch_result = None
                        result = self._delegate_research(
                            provider,
                            session,
                            prompt,
                            arguments,
                            turn=turn,
                            workspace_revision=workspace_revision,
                            task_id=task_id or session_id,
                        )
                        delegated_patch = self.toolbox.last_patch_result or {}
                        patch_applied_paths = [
                            str(path) for path in delegated_patch.get("applied_paths", []) if path
                        ]
                        patch_partial = bool(delegated_patch.get("declined_hunks"))
                    elif name == "delegate_parallel_research":
                        result = self._delegate_parallel_research(
                            provider,
                            session,
                            prompt,
                            arguments,
                            turn=turn,
                            workspace_revision=workspace_revision,
                            task_id=task_id or session_id,
                        )
                    else:
                        result = self.toolbox.execute(name, arguments)
                    tool_succeeded = True
                else:
                    status = str(journal_record.get("status")) if journal_record else "legacy_unknown"
                    if journal_record and not journal_record.get("identity_matches"):
                        result = "工具执行记录与当前调用不匹配；为避免重放潜在副作用，本次没有运行。"
                    elif status == "completed":
                        result = str(journal_record.get("result") or "")
                    elif status == "started":
                        result = (
                            "工具调用已启动但没有可恢复结果；可能已经产生副作用。"
                            "为避免重复执行，本次没有再次运行。请先检查当前状态。"
                        )
                    else:
                        result = "工具调用缺少安全执行记录；是否执行过无法确定，本次没有运行。"
            except TaskCancelled:
                self._raise_if_cancelled(session, session_id)
                raise
            except TaskAborted:
                raise
            except PatchReviewRejected as exc:
                patch_stop_reason = str(exc)
                result = f"工具执行失败：PatchReviewRejected: {exc}"
            except BrowserApprovalDenied as exc:
                browser_action_denied = True
                result = f"浏览器操作已由用户拒绝：{exc}"
            except Exception as exc:  # Other tool errors go back to the model for correction.
                result = f"工具执行失败：{type(exc).__name__}: {exc}"
            try:
                self.hooks.run("after_tool", {
                    "session_id": session_id,
                    "turn": turn,
                    "tool": name,
                    "success": bool(tool_succeeded),
                    "result": str(result)[:8000],
                }, blocking=False)
            except Exception:  # noqa: BLE001 - after_tool is advisory only
                pass
            signature = (
                name,
                json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str),
                str(result)[:200],
            )
            if signature == self._tool_loop_signature:
                self._tool_loop_repeats += 1
            else:
                self._tool_loop_signature = signature
                self._tool_loop_repeats = 1
            if self._tool_loop_repeats >= self.MAX_IDENTICAL_TOOL_RESULTS:
                loop_message = (
                    f"模型连续 {self._tool_loop_repeats} 次重复同一工具调用且结果不变"
                    f"（{name}）；为避免继续消耗，本次任务已停止。"
                )
                session["status"] = "failed"
                session["error"] = loop_message
                session["updated_at"] = now_iso()
                self.ledger.append(
                    "tool_loop_detected",
                    {
                        "session_id": session_id,
                        "tool": name,
                        "repeats": self._tool_loop_repeats,
                    },
                )
                self._save_session(session)
                self._emit("failed", {"session_id": session_id, "error": loop_message})
                raise PermanentError(loop_message)
            if name == "run_command" and verification_before is not None and not self._cancel.is_set():
                verification_after = self._verification_snapshot(session)
                execution = dict(self.toolbox.last_command_execution or {
                    "command_id": new_id("cmd"),
                    "command": command_for_record[:1000],
                    "status": "not_run",
                    "exit_code": None,
                    "timeout_seconds": None,
                    "elapsed_ms": 0,
                    "started_at": "",
                    "finished_at": now_iso(),
                })
                self._record_command_verification(
                    session,
                    session_id,
                    turn,
                    execution,
                    verification_before,
                    verification_after,
                    verification_revision,
                )
            if (
                svg_preflight_pending
                and name in {"replace_in_file", "apply_patch"}
                and name in allowed_tool_names
                and tool_succeeded
            ):
                svg_repair_read_paths.clear()
            if tool_succeeded:
                if name == "update_plan" and required_initial_plan:
                    required_initial_plan = False
                    if not svg_task:
                        tool_definitions = list(task_tool_definitions)
                        allowed_tool_names = self._session_tool_names(tool_definitions)
                if name in {"delegate_research", "delegate_parallel_research"}:
                    specialist_count = len(session.get("specialist_calls") or [])
                    excluded_specialist_tools: set[str] = set()
                    if specialist_count >= self.MAX_SPECIALIST_CALLS_PER_TASK:
                        excluded_specialist_tools = {"delegate_research", "delegate_parallel_research"}
                    elif self.MAX_SPECIALIST_CALLS_PER_TASK - specialist_count < 2:
                        excluded_specialist_tools = {"delegate_parallel_research"}
                    if excluded_specialist_tools:
                        tool_definitions = [
                            definition for definition in tool_definitions
                            if definition.get("function", {}).get("name") not in excluded_specialist_tools
                        ]
                        task_tool_definitions = [
                            definition for definition in task_tool_definitions
                            if definition.get("function", {}).get("name") not in excluded_specialist_tools
                        ]
                    allowed_tool_names = self._session_tool_names(tool_definitions)
                if call_id in recovered_tool_ids and name == "write_file":
                    recovered_artifact_path = str(arguments.get("path", ""))
                if name in {"write_file", "replace_in_file"}:
                    context_dirty = True
                    changed = set(session.get("agent_changed_paths") or [])
                    changed.add(str(arguments.get("path", "")))
                    session["agent_changed_paths"] = sorted(path for path in changed if path)
                    if VerificationTracker.requires_check([str(arguments.get("path", ""))]):
                        session["verification_code_changed"] = True
                    workspace_revision += 1
                    session["verification_edit_revision"] = int(
                        session.get("verification_edit_revision", 0) or 0
                    ) + 1
                    if svg_task:
                        svg_mutation_seen = True
                elif name in {"apply_patch", "delegate_research"}:
                    patch_result = (
                        self.toolbox.last_patch_result or {}
                        if name == "apply_patch"
                        else delegated_patch
                    )
                    patch_applied_paths = [
                        str(path) for path in patch_result.get("applied_paths", []) if path
                    ]
                    if patch_applied_paths:
                        context_dirty = True
                        if svg_task:
                            svg_mutation_seen = True
                        changed = set(session.get("agent_changed_paths") or [])
                        changed.update(patch_applied_paths)
                        session["agent_changed_paths"] = sorted(changed)
                        if VerificationTracker.requires_check(patch_applied_paths):
                            session["verification_code_changed"] = True
                        workspace_revision += 1
                        session["verification_edit_revision"] = int(
                            session.get("verification_edit_revision", 0) or 0
                        ) + 1
                    if patch_result.get("declined_hunks"):
                        patch_partial = True
                        patch_stop_reason = (
                            "已应用你批准的补丁块；其余补丁块未获批准，任务已停止且不会自动提交。"
                        )
                if svg_preflight_pending and name == "read_file":
                    svg_repair_read_paths[normalized_tool_path] = str(arguments.get("path", ""))
                if SvgArtifactAdapter.is_creation_request(prompt) and name == "write_file":
                    svg_creation_written = True
                if svg_task and name in {"write_file", "replace_in_file", "apply_patch"}:
                    svg_preflight_pending = "SVG 结构预检提示" in str(result)
                    if svg_preflight_pending and self.EXPLICIT_COMMAND_INTENT.search(prompt) is None:
                        tool_scope_prompt = (
                            "Repair only these localized SVG issues in the SVG just created; preserve valid artwork. "
                            "Use exact replace_in_file edits and inspect git_diff."
                        )
                        svg_repair_read_paths.clear()
                    elif self.EXPLICIT_COMMAND_INTENT.search(prompt) is None:
                        tool_definitions = self._svg_artifact_repair_tool_definitions()
                        allowed_tool_names = {
                            definition["function"]["name"]
                            for definition in tool_definitions
                            if isinstance(definition, dict)
                            and isinstance(definition.get("function"), dict)
                            and isinstance(definition["function"].get("name"), str)
                        }
                elif name == "run_command":
                    context_dirty = True
                    workspace_revision += 1
                elif name == "git_diff":
                    diff_verified_revision = workspace_revision
            result = str(result)[-30000:]
            if tool_journal_started:
                self.ledger.complete_tool_call(
                    session_id, assistant_message_index, call_index, result,
                )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
            self.ledger.append(
                "tool_result",
                {
                    "session_id": session_id,
                    "name": name,
                    "call_id": call_id,
                    "assistant_message_index": assistant_message_index,
                    "call_index": call_index,
                    "result": result[:3000],
                },
            )
            self._emit("tool_result", {
                "session_id": session_id,
                "name": name,
                "call_id": call_id,
                "result": result,
            })
            if patch_stop_reason:
                session["status"] = "failed"
                session["error"] = patch_stop_reason
                session["updated_at"] = now_iso()
                failure = {
                    "session_id": session_id,
                    "error": patch_stop_reason,
                    "patch_partial": patch_partial,
                    "applied_paths": patch_applied_paths,
                }
                self.ledger.append("coding_session_failed", failure)
                self._save_session(session)
                self.ledger.clear_tool_call(session_id, assistant_message_index, call_index)
                self._emit("patch_review_stopped", failure)
                raise PermanentError(patch_stop_reason)
            if browser_action_denied:
                tool_definitions = [
                    definition
                    for definition in tool_definitions
                    if definition.get("function", {}).get("name") not in self.BROWSER_TOOL_NAMES
                ]
                task_tool_definitions = [
                    definition
                    for definition in task_tool_definitions
                    if definition.get("function", {}).get("name") not in self.BROWSER_TOOL_NAMES
                ]
                allowed_tool_names = self._session_tool_names(tool_definitions)
                self.ledger.append(
                    "browser_tools_disabled_after_denial",
                    {"session_id": session_id, "turn": turn},
                )
            self._save_session(session)
            self.ledger.clear_tool_call(session_id, assistant_message_index, call_index)
            self._raise_if_cancelled(session, session_id)
        return {
            "tool_definitions": tool_definitions,
            "recovered_artifact_path": recovered_artifact_path,
            "svg_creation_written": svg_creation_written,
            "svg_preflight_pending": svg_preflight_pending,
            "svg_mutation_seen": svg_mutation_seen,
            "context_dirty": context_dirty,
            "diff_verified_revision": diff_verified_revision,
            "allowed_tool_names": allowed_tool_names,
            "task_tool_definitions": task_tool_definitions,
            "tool_scope_prompt": tool_scope_prompt,
            "workspace_revision": workspace_revision,
            "required_initial_plan": required_initial_plan,
        }
