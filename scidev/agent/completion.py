"""Verification, semantic acceptance and completion/commit gates.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from scidev.agent.acceptance import SemanticAcceptance
from scidev.runtime.hooks import HookError
from scidev.runtime.errors import PermanentError
from scidev.integrations.model import OpenAICompatibleProvider as OpenAICompatibleProvider
from scidev.runtime.helpers import now_iso
from scidev.runtime.policy import assess_command
from scidev.runtime.policy import format_verification_diagnostics
from scidev.agent.usage import ModelRequestUsage
from scidev.agent.verification import VerificationTracker
from scidev.agent.toolbox import CodingToolbox as CodingToolbox


class AgentCompletionMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _verification_snapshot(self, session: dict[str, Any]) -> dict[str, Any]:
        paths = set(session.get("verification_paths") or [])
        paths.update(self.git.status_paths())
        paths.update(session.get("agent_changed_paths") or [])
        try:
            git_repository = self.git.is_repo()
            head_sha = self.git.head_sha() if git_repository else ""
        except Exception:
            git_repository = False
            head_sha = ""
        scope_complete = git_repository and bool(head_sha)
        if not scope_complete:
            try:
                inventory = self.toolbox.project_context.list_paths(VerificationTracker.CODE_SUFFIXES)
                paths.update(inventory["paths"])
                scope_complete = bool(inventory["complete"])
            except (OSError, RuntimeError, ValueError):
                scope_complete = False
        session["verification_paths"] = sorted(path for path in paths if isinstance(path, str) and path)
        snapshot = VerificationTracker.fingerprint(
            self.project_root,
            session["verification_paths"],
            CodingToolbox.is_protected_path,
            scope_complete=scope_complete,
        )
        snapshot["fingerprint"] = hashlib.sha256(
            f"{head_sha}\0{snapshot.get('fingerprint', '')}".encode("utf-8")
        ).hexdigest()
        snapshot["git_head_sha"] = head_sha
        snapshot["git_repository"] = git_repository
        return snapshot


    def _record_command_verification(
        self,
        session: dict[str, Any],
        session_id: str,
        turn: int,
        execution: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
        edit_revision: int,
    ) -> dict[str, Any]:
        record = VerificationTracker.record(
            execution,
            before,
            after,
            edit_revision=edit_revision,
            turn=turn,
        )
        if before.get("fingerprint") != after.get("fingerprint"):
            session["verification_edit_revision"] = edit_revision + 1
            before_paths = before.get("path_fingerprints") or {}
            after_paths = after.get("path_fingerprints") or {}
            changed_paths = {
                path for path in set(before_paths) | set(after_paths)
                if before_paths.get(path) != after_paths.get(path)
            }
            if VerificationTracker.requires_check(changed_paths) or (
                before.get("git_head_sha") != after.get("git_head_sha")
            ):
                session["verification_code_changed"] = True
        runs = list(session.get("verification_runs") or [])
        runs.append(record)
        session["verification_runs"] = VerificationTracker.retain_history(runs)
        report = VerificationTracker.summarize(
            session["verification_runs"],
            after,
            edit_revision=int(session.get("verification_edit_revision", 0) or 0),
        )
        session["verification_report"] = report
        session["updated_at"] = now_iso()
        self._save_session(session)
        self.ledger.append("verification_recorded", {"session_id": session_id, **record, "report": report})
        self._emit("verification_recorded", {"session_id": session_id, **record, "report": report})
        return record


    def _auto_verification_candidates(self, prompt: str) -> list[tuple[str, str, str]]:
        """Return at most one safe discovered check per category.

        Tests come first because they catch behavior errors; lint and typecheck
        then add diagnostics a failing test may not show.  Only commands the
        execution policy can auto-approve are returned.
        """
        try:
            context = self.toolbox.project_context.build(prompt[:4000])
        except (OSError, RuntimeError, ValueError):
            return []
        discovery = context.get("test_discovery")
        if not isinstance(discovery, dict):
            return []
        priority = {"test": 0, "lint": 1, "typecheck": 2}
        best: dict[str, tuple[int, str, str]] = {}
        for candidate in discovery.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            command = str(candidate.get("command") or "").strip()
            if not command:
                continue
            assessment = assess_command(command)
            rank = priority.get(assessment.category)
            if rank is None or not assessment.auto_approvable:
                continue
            current = best.get(assessment.category)
            if current is None or rank < current[0]:
                best[assessment.category] = (rank, command, str(candidate.get("name") or ""))
        return [
            (category, value[1], value[2])
            for category, value in sorted(best.items(), key=lambda item: item[1][0])
        ]


    def _auto_verification_candidate(self, prompt: str) -> tuple[str, str]:
        """Return the first safe discovered check, preferring tests over lint."""
        candidates = self._auto_verification_candidates(prompt)
        if not candidates:
            return "", ""
        return candidates[0][1], candidates[0][2]


    def _run_auto_verification(
        self,
        session: dict[str, Any],
        session_id: str,
        turn: int,
        prompt: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Run up to one Harness-selected check per category under the policy.

        The model gets structured diagnostics from every check, not just the
        raw tail of one command.  A failed test does not stop lint/typecheck:
        those often name the exact line the test failure only implies.
        """
        candidates = self._auto_verification_candidates(prompt)
        if not candidates:
            return False, "", {}
        revision = int(session.get("verification_edit_revision", 0) or 0)
        auto_runs = session.get("verification_auto_runs")
        if not isinstance(auto_runs, list):
            auto_runs = []
        if len(auto_runs) >= 3:
            return False, "", {}
        ran_any = False
        notes: list[str] = []
        for category, command, label in candidates:
            if len(auto_runs) >= 3:
                break
            assessment = assess_command(command)
            if assessment.category != category:
                continue
            if not self.toolbox.execution_policy.auto_approves(assessment, command):
                continue
            if any(
                isinstance(item, dict)
                and item.get("command") == command
                and int(item.get("edit_revision", -1) or -1) == revision
                for item in auto_runs
            ):
                continue
            before = self._verification_snapshot(session)
            self._emit("verification_auto_started", {
                "session_id": session_id,
                "turn": turn,
                "command": command,
                "label": label,
                "category": category,
                "edit_revision": revision,
            })
            try:
                output = self.toolbox.run_command(command, timeout_seconds=240)
            except PermanentError as exc:
                notes.append(f"Harness 自动验证未能启动 `{command}`：{exc}")
                self._emit("verification_auto_failed", {
                    "session_id": session_id, "turn": turn, "command": command, "error": str(exc),
                })
                continue
            after = self._verification_snapshot(session)
            execution = dict(self.toolbox.last_command_execution or {})
            if not execution:
                continue
            self._record_command_verification(
                session, session_id, turn, execution, before, after, revision,
            )
            auto_runs.append({
                "command": command,
                "label": label,
                "category": category,
                "edit_revision": revision,
                "turn": turn,
                "status": execution.get("status"),
                "exit_code": execution.get("exit_code"),
            })
            ran_any = True
            diagnostics = format_verification_diagnostics(category, output)
            detail = output[-3000:]
            if diagnostics:
                detail = f"{diagnostics}\n\n原始输出（末尾）：\n{detail[-2000:]}"
            notes.append(
                f"Harness 已自动运行{category}验证 `{command}`（{label or 'discovered check'}）："
                f"状态 {execution.get('status')}，退出码 {execution.get('exit_code')}。\n{detail}"
            )
            self._emit("verification_auto_completed", {
                "session_id": session_id,
                "turn": turn,
                "command": command,
                "category": category,
                "status": execution.get("status"),
                "exit_code": execution.get("exit_code"),
                "report": session.get("verification_report") or {},
            })
        if not ran_any:
            return False, "\n\n".join(notes), {}
        session["verification_auto_runs"] = auto_runs[-20:]
        session["updated_at"] = now_iso()
        self._save_session(session)
        return True, "\n\n".join(notes), session.get("verification_report") or {}


    def _semantic_acceptance_review(
        self,
        provider: Any,
        session: dict[str, Any],
        prompt: str,
        final_answer: str,
        changed_paths: set[str],
        turn: int,
    ) -> dict[str, Any] | None:
        settings = self.summary_settings
        mode = SemanticAcceptance.mode(settings.acceptance_mode)
        if mode == "off" or not changed_paths:
            return None

        reviewer_model = settings.acceptance_model.strip()[:160]
        coding_model = str(getattr(provider, "model", "") or "").strip()
        if not reviewer_model:
            report = SemanticAcceptance.unavailable("未配置独立验收模型")
        elif reviewer_model.casefold() == coding_model.casefold():
            report = SemanticAcceptance.unavailable(
                "验收模型必须与编码模型使用不同的模型 ID", model=reviewer_model,
            )
        elif not callable(getattr(provider, "with_model", None)):
            report = SemanticAcceptance.unavailable(
                "当前模型服务不支持配置独立验收模型", model=reviewer_model,
            )
        else:
            safe_paths, skipped_paths = self.git.filter_auto_commit_paths(changed_paths)
            paths = sorted(safe_paths)
            if skipped_paths or len(paths) != len(changed_paths) or len(paths) > SemanticAcceptance.MAX_REVIEW_FILES:
                report = SemanticAcceptance.scope_incomplete(
                    f"变更范围含不可安全读取/审查的文件（{len(skipped_paths)} 个）或超过文件数上限；未调用验收模型",
                    model=reviewer_model,
                    paths=paths,
                )
            else:
                def collect_diff() -> tuple[str, str]:
                    current_paths, current_skipped = self.git.filter_auto_commit_paths(changed_paths)
                    if current_skipped or current_paths != safe_paths:
                        return "", "变更文件范围在审查期间发生变化或无法安全读取"
                    parts: list[str] = []
                    total_chars = 0
                    for relative in paths:
                        try:
                            diff = self.git.diff(self.project_root / Path(relative), protect_sensitive=True)
                        except Exception:
                            diff = ""
                        if not diff.strip() or diff.startswith((
                            "Git 尚未初始化", "敏感/内部路径的差异已隐藏", "拒绝读取项目目录之外的文件",
                        )):
                            return "", "至少一个变更文件没有可安全读取的完整文本 diff"
                        if total_chars + len(diff) > SemanticAcceptance.MAX_DIFF_CHARS:
                            return "", "完整 diff 超过语义验收上下文上限"
                        parts.append(f"--- {relative} ---\n{diff}")
                        total_chars += len(diff)
                    return "\n\n".join(parts), ""

                diff_text, incomplete_reason = collect_diff()

                if incomplete_reason:
                    report = SemanticAcceptance.scope_incomplete(
                        incomplete_reason, model=reviewer_model, paths=paths,
                    )
                else:
                    verification = session.get("verification_report")
                    verification = verification if isinstance(verification, dict) else {}
                    latest = verification.get("latest") if isinstance(verification.get("latest"), dict) else {}
                    compact_verification = {
                        "status": str(verification.get("status") or "not_run"),
                        "scope_complete": bool(verification.get("scope_complete")),
                        "check_count": int(verification.get("check_count", 0) or 0),
                        "latest": {
                            key: latest.get(key)
                            for key in ("kind", "command", "check_status", "exit_code", "elapsed_ms")
                            if key in latest
                        },
                    }
                    plan = session.get("task_plan")
                    if isinstance(plan, dict):
                        plan = json.dumps(plan, ensure_ascii=False)[:6000]
                    else:
                        plan = None
                    messages = SemanticAcceptance.messages(
                        task=prompt,
                        final_answer=final_answer,
                        paths=paths,
                        diff=diff_text,
                        verification=compact_verification,
                        plan={"snapshot": plan} if plan else None,
                    )
                    request_key = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()[:16]
                    full_diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
                    request_id = (
                        f"{session.get('last_task_id') or session['session_id']}"
                        f"-acceptance-turn-{turn}-{request_key}"
                    )
                    try:
                        reviewer = provider.with_model(reviewer_model, streaming=False, temperature=0.0)
                        if not callable(getattr(reviewer, "chat", None)):
                            raise TypeError("reviewer provider has no chat method")
                        started = {
                            "session_id": session["session_id"],
                            "mode": mode,
                            "reviewer_model": reviewer_model,
                            "changed_file_count": len(paths),
                            "turn": turn,
                        }
                        self.ledger.append("semantic_acceptance_started", started)
                        self._emit("semantic_acceptance_started", started)
                        metrics = None
                        if isinstance(reviewer, OpenAICompatibleProvider):
                            def metrics(result: dict[str, Any]) -> None:
                                self._record_model_request(
                                    session,
                                    f"{session.get('last_task_id') or session['session_id']}|semantic_acceptance:{request_key}",
                                    "semantic_acceptance",
                                    result,
                                )
                        response = reviewer.chat(
                            messages,
                            max_tokens=SemanticAcceptance.REVIEW_MAX_TOKENS,
                            request_id=request_id,
                            on_metrics=metrics,
                        )
                        current_diff, changed_during_review = collect_diff()
                        if changed_during_review or current_diff != diff_text:
                            report = SemanticAcceptance.scope_incomplete(
                                changed_during_review or "变更 diff 在复核期间发生变化；审查结论不适用于当前代码",
                                model=reviewer_model,
                                paths=paths,
                            )
                            report["reviewed_diff_sha256"] = full_diff_hash
                            report["revalidated"] = False
                        else:
                            report = SemanticAcceptance.evaluate(
                                response,
                                reviewed_paths=paths,
                                scope_complete=True,
                                reviewer_model=reviewer_model,
                            )
                            report["reviewed_diff_sha256"] = full_diff_hash
                            report["revalidated"] = True
                    except Exception as exc:
                        # Do not persist provider error bodies; they may contain endpoint details.
                        report = SemanticAcceptance.unavailable(
                            f"验收模型请求失败（{type(exc).__name__}）", model=reviewer_model, paths=paths,
                        )

        report["mode"] = mode
        report["turn"] = turn
        session["semantic_acceptance_report"] = report
        session["updated_at"] = now_iso()
        event = {"session_id": session["session_id"], **report}
        self.ledger.append("semantic_acceptance_reviewed", event)
        self._save_session(session)
        self._emit("semantic_acceptance_reviewed", event)
        return report


    def _auto_commit_warning(
        self,
        skipped_paths: dict[str, dict[str, str | int]],
        commit_sha: str,
    ) -> str:
        if not skipped_paths:
            return ""
        reason_labels = {
            "protected_path": "敏感/内部路径",
            "outside_workspace": "越出工作区的路径",
            "linked_path": "符号链接或目录联接",
            "path_unavailable": "无法安全核验的路径",
            "file_too_large": f"超过 {self.git.MAX_AUTO_COMMIT_FILE_BYTES:,} bytes 自动提交上限",
            "non_text_file": "二进制或非 UTF-8 文件",
        }
        lines = ["Git 自动提交保护：以下变更未自动提交，仍保留在工作区，可在 Git 面板审核后手动提交："]
        for relative, detail in sorted(skipped_paths.items())[:12]:
            label = json.dumps(relative, ensure_ascii=False)
            reason = reason_labels.get(str(detail.get("reason")), "安全策略拦截")
            size = detail.get("size_bytes")
            if isinstance(size, int):
                reason += f"（{size:,} bytes）"
            lines.append(f"- {label}：{reason}")
        remaining = len(skipped_paths) - min(12, len(skipped_paths))
        if remaining:
            lines.append(f"- 另有 {remaining} 个文件因同一安全策略保留未提交。")
        lines.append(
            f"其余安全文件已提交 {commit_sha[:12]}。"
            if commit_sha
            else "本次没有安全文件产生自动提交。"
        )
        return "\n".join(lines)


    def _enforce_verification_gate(
        self,
        session: dict[str, Any],
        session_id: str,
        prompt: str,
        messages: list[dict[str, Any]],
        turn: int,
        content: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Record verification evidence and enforce the completion gate."""
        verification_snapshot = self._verification_snapshot(session)
        verification_report = VerificationTracker.summarize(
            session.get("verification_runs", []),
            verification_snapshot,
            edit_revision=int(session.get("verification_edit_revision", 0) or 0),
        )
        session["verification_report"] = verification_report
        verification_event = {"session_id": session_id, **verification_report}
        self.ledger.append("verification_report_recorded", verification_event)
        self._emit("verification_reported", verification_event)
        if (
            self.require_verification
            and session.get("verification_code_changed")
            and verification_report.get("status") != "passed"
        ):
            auto_note = ""
            if not self.single_user_prompt_only:
                latest = (session.get("verification_runs") or [])
                latest = latest[-1] if latest and isinstance(latest[-1], dict) else {}
                prior_denied = (
                    latest.get("execution_status") == "not_run"
                    and latest.get("approval_status") in {"denied", "rejected", "error"}
                )
                if not prior_denied:
                    auto_ran, auto_note, auto_report = self._run_auto_verification(
                        session, session_id, turn, prompt,
                    )
                    if auto_ran:
                        verification_report = auto_report or session.get("verification_report") or verification_report
                        session["verification_report"] = verification_report
                        if verification_report.get("status") == "passed":
                            self._emit("verification_auto_passed", {
                                "session_id": session_id,
                                "turn": turn,
                                "report": verification_report,
                            })
                            return False, verification_report
            if (
                self.toolbox.command_approval is None
                and not self.toolbox.execution_policy.auto_approves(assess_command("pytest -q"), "pytest -q")
            ):
                # Without an approval channel and without a policy that can
                # auto-approve verification, run_command can never execute.
                # Asking the model for checks only burns turns; fail closed.
                blocked_message = (
                    "代码改动尚未验证：当前运行没有命令审批通道，且执行策略不允许自动验证。"
                    "改动保留在工作区，本次不会自动提交；"
                    "请在桌面客户端中运行，或切换为“工作区可写”/提供 command_approval 后重试。"
                )
                session["status"] = "verification_required"
                session["verification_required"] = True
                session["error"] = blocked_message
                session["final_message"] = f"{content.rstrip()}\n\n{blocked_message}".strip()
                session["updated_at"] = now_iso()
                failure = {
                    "session_id": session_id,
                    "error": blocked_message,
                    "verification_report": verification_report,
                }
                self.ledger.append("coding_session_verification_required", failure)
                self.ledger.append("coding_session_failed", failure)
                self._save_session(session)
                self._emit("verification_blocked", failure | {"text": content})
                raise PermanentError(blocked_message)
            attempts = int(session.get("verification_required_retries", 0) or 0)
            attempts_list = session.get("verification_runs") or []
            latest_attempt = attempts_list[-1] if attempts_list and isinstance(attempts_list[-1], dict) else {}
            approval_denied = (
                latest_attempt.get("execution_status") == "not_run"
                and latest_attempt.get("approval_status") in {"denied", "rejected", "error"}
            )
            if (
                not self.single_user_prompt_only
                and not approval_denied
                and attempts < self.MAX_VERIFICATION_FOLLOWUPS
            ):
                if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
                    messages.pop()
                attempts += 1
                session["verification_required_retries"] = attempts
                session["status"] = "active"
                session["updated_at"] = now_iso()
                self._sync_task_plan_prompt(session, prompt)
                status_label = VerificationTracker.label(str(verification_report.get("status") or "not_run"))
                followup_text = (
                    "Harness 验证门槛：本任务改动了代码，但尚无针对当前最终代码且完整通过的检查。"
                    f"当前证据：{status_label}。请检查项目已有测试/构建说明，选择最小相关检查；"
                    "若检查失败，先分析错误、修复并重新运行。命令按当前执行策略处理："
                    "工作区可写会自动批准可识别的验证命令，手动审批模式仍需逐条批准；"
                    "不要重复用户已拒绝的命令。只有检查成功且期间代码未变后才能结束。"
                )
                if auto_note:
                    followup_text = f"{auto_note}\n\n{followup_text}"
                self._append_followup_prompt(
                    messages,
                    session,
                    session_id,
                    turn,
                    followup_text,
                    reason="changed code lacks a passing current-code check",
                )
                retry_event = {
                    "session_id": session_id,
                    "turn": turn,
                    "attempt": attempts,
                    "retry_limit": self.MAX_VERIFICATION_FOLLOWUPS,
                    "status": verification_report.get("status"),
                }
                self.ledger.append("verification_retry_scheduled", retry_event)
                self._emit("verification_retry_scheduled", retry_event)
                self._save_session(session)
                return True, verification_report

            status_label = VerificationTracker.label(str(verification_report.get("status") or "not_run"))
            if approval_denied:
                reason = "用户未批准检查命令，命令没有运行。"
            elif self.single_user_prompt_only:
                reason = "单轮提示模式不能继续发起补验请求。"
            else:
                reason = f"已达到 {self.MAX_VERIFICATION_FOLLOWUPS} 次补验上限。"
            blocked_message = (
                f"代码改动尚未验证：{status_label}。{reason}"
                "改动保留在工作区，本次不会自动提交；可修复/批准检查后重试。"
            )
            if auto_note:
                blocked_message = f"{auto_note}\n\n{blocked_message}"
            session["status"] = "verification_required"
            session["verification_required"] = True
            session["error"] = blocked_message
            session["final_message"] = f"{content.rstrip()}\n\n{blocked_message}".strip()
            session["updated_at"] = now_iso()
            failure = {"session_id": session_id, "error": blocked_message, "verification_report": verification_report}
            self.ledger.append("coding_session_verification_required", failure)
            self.ledger.append("coding_session_failed", failure)
            self._save_session(session)
            self._emit("verification_blocked", failure | {"text": content})
            raise PermanentError(blocked_message)
        return False, verification_report


    def _run_acceptance_stage(
        self,
        provider: Any,
        session: dict[str, Any],
        session_id: str,
        prompt: str,
        content: str,
        changed_paths: set[str],
        turn: int,
        messages: list[dict[str, Any]],
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """Run the semantic acceptance review and its bounded follow-up."""
        acceptance_report = self._semantic_acceptance_review(
            provider, session, prompt, content, changed_paths, turn,
        )
        if acceptance_report is not None:
            acceptance_status = str(acceptance_report.get("status") or "uncertain")
            acceptance_mode = str(acceptance_report.get("mode") or "off")
            if acceptance_mode == "required" and acceptance_status != "passed":
                attempts = int(session.get("semantic_acceptance_retries", 0) or 0)
                if (
                    acceptance_status == "revise"
                    and not self.single_user_prompt_only
                    and attempts < self.MAX_ACCEPTANCE_FOLLOWUPS
                ):
                    if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
                        messages.pop()
                    attempts += 1
                    session["semantic_acceptance_retries"] = attempts
                    session["status"] = "active"
                    session["updated_at"] = now_iso()
                    self._sync_task_plan_prompt(session, prompt)
                    self._append_followup_prompt(
                        messages,
                        session,
                        session_id,
                        turn,
                        SemanticAcceptance.followup(acceptance_report),
                        reason="independent semantic review found an unmet requirement",
                    )
                    retry_event = {
                        "session_id": session_id,
                        "turn": turn,
                        "attempt": attempts,
                        "retry_limit": self.MAX_ACCEPTANCE_FOLLOWUPS,
                        "review_status": acceptance_status,
                    }
                    self.ledger.append("semantic_acceptance_retry_scheduled", retry_event)
                    self._emit("semantic_acceptance_retry_scheduled", retry_event)
                    self._save_session(session)
                    return True, acceptance_report, content

                if acceptance_status == "revise":
                    reason = (
                        "单轮提示模式不允许 Harness 自动追加修复任务。"
                        if self.single_user_prompt_only
                        else f"已达到 {self.MAX_ACCEPTANCE_FOLLOWUPS} 次语义修复上限。"
                    )
                else:
                    reason = "审查缺失、范围不完整或结论不确定，不能据此通过。"
                blocked_message = (
                    f"独立语义验收未通过：{SemanticAcceptance.display(acceptance_report)}\n"
                    f"{reason}改动保留在工作区，本次不会自动提交；可配置独立模型、补全证据后重试。"
                )
                session["status"] = "acceptance_required"
                session["semantic_acceptance_required"] = True
                session["error"] = blocked_message
                session["final_message"] = f"{content.rstrip()}\n\n{blocked_message}".strip()
                session["updated_at"] = now_iso()
                failure = {
                    "session_id": session_id,
                    "error": blocked_message,
                    "semantic_acceptance_report": acceptance_report,
                }
                self.ledger.append("coding_session_acceptance_required", failure)
                self.ledger.append("coding_session_failed", failure)
                self._save_session(session)
                self._emit("semantic_acceptance_blocked", failure | {"text": content})
                raise PermanentError(blocked_message)

            session["semantic_acceptance_required"] = False
            content = f"{content.rstrip()}\n\n{SemanticAcceptance.display(acceptance_report)}".strip()
        return False, acceptance_report, content


    def _finish_coding_session(
        self,
        provider: Any,
        session: dict[str, Any],
        session_id: str,
        prompt: str,
        content: str,
        changed_paths: set[str],
        turn: int,
        acceptance_report: dict[str, Any] | None,
        verification_report: dict[str, Any],
    ) -> dict[str, Any]:
        """Auto-commit the verified change and build the completion payload."""
        no_change_warning = ""

        def flag_no_changes(message: str) -> str:
            session["no_changes_detected"] = True
            payload = {
                "session_id": session_id,
                "message": message,
                "prompt": " ".join(prompt.split())[:500],
            }
            self.ledger.append("no_changes_detected", payload)
            self._emit("no_changes_detected", payload)
            return message

        if (
            not changed_paths
            and self._prompt_requests_file_changes(prompt)
            and not self.prompt_forbids_mutating_tools(prompt)
        ):
            no_change_warning = flag_no_changes(
                "未检测到文件改动：任务描述看起来要求修改项目，但本次没有写入任何文件。"
                "如果确实无需改动，请忽略此提醒。"
            )
        if content:
            self._emit("assistant", {"session_id": session_id, "turn": turn, "text": content})
        self._save_session(session)
        summary = " ".join(prompt.split())[:72] or session_id
        session["status"] = "completed"
        session["verification_required"] = False
        session["final_message"] = content
        commit_paths, skipped_paths = self.git.filter_auto_commit_paths(changed_paths)
        suppressed_note = ""
        if self.prompt_forbids_auto_commit(prompt):
            session["git_result_sha"] = ""
            session["git_auto_commit_suppressed"] = bool(commit_paths)
            if commit_paths:
                note = "按你的要求未自动提交；改动保留在工作区，请在 Git 面板审核后手动提交。"
                suppressed_note = note
                content = f"{content.rstrip()}\n\n{note}".strip()
                session["final_message"] = content
                self.ledger.append(
                    "git_auto_commit_suppressed_by_user",
                    {"session_id": session_id, "paths": sorted(commit_paths)},
                )
                self._emit(
                    "git_auto_commit_suppressed_by_user",
                    {"session_id": session_id, "paths": sorted(commit_paths), "message": note},
                )
        else:
            try:
                self.hooks.run("before_commit", {
                    "session_id": session_id,
                    "paths": sorted(commit_paths),
                    "prompt": prompt[:8000],
                }, blocking=True)
            except HookError as exc:
                raise PermanentError(f"before_commit hook 阻止提交：{exc}") from exc
            session["git_result_sha"] = self.git.commit_paths(f"[codex] {summary}", commit_paths)
        session["git_auto_commit_skipped_paths"] = skipped_paths
        external_commit = ""
        if not session["git_result_sha"]:
            baseline = str(session.get("git_baseline_sha") or "")
            head = self.git.head_sha()
            if head and baseline and head != baseline:
                # The model ran a commit some other way (or the user did while
                # the task ran). Report the real SHA instead of claiming
                # "无新提交", and keep the warning visible in the transcript.
                external_commit = head
                session["git_result_sha"] = head
                session["git_commit_source"] = "detected_external"
                self.ledger.append(
                    "git_commit_detected_outside_harness",
                    {"session_id": session_id, "git_result_sha": head, "baseline_sha": baseline},
                )
        if (
            not session["git_result_sha"]
            and not skipped_paths
            and not commit_paths
            and not no_change_warning
            and self._prompt_requests_file_changes(prompt)
            and not self.prompt_forbids_mutating_tools(prompt)
        ):
            # The model "edited" a file but nothing actually differs, so the
            # auto-commit was a no-op; say so instead of ending silently.
            no_change_warning = flag_no_changes(
                "未检测到文件改动：任务描述看起来要求修改项目，但本次没有产生可提交的改动。"
                "如果确实无需改动，请忽略此提醒。"
            )
        commit_warning = self._auto_commit_warning(skipped_paths, session["git_result_sha"])
        if external_commit:
            external_warning = (
                "检测到本次运行期间出现了新提交（可能是 Agent 手动执行了 git commit）；"
                "Harness 未重复提交，请核对提交内容是否符合预期。"
            )
            commit_warning = f"{commit_warning}\n{external_warning}".strip()
        if suppressed_note:
            commit_warning = f"{suppressed_note}\n{commit_warning}".strip() if commit_warning else suppressed_note
            # The final message already carries the note; only expose the
            # warning to the UI so it does not report "无新提交".
            session["git_auto_commit_warning"] = commit_warning
        if commit_warning and not suppressed_note:
            session["git_auto_commit_warning"] = commit_warning
            session["final_message"] = f"{content.rstrip()}\n\n{commit_warning}".strip()
            payload = {
                "session_id": session_id,
                "skipped_paths": skipped_paths,
                "limit_bytes": self.git.MAX_AUTO_COMMIT_FILE_BYTES,
                "message": commit_warning,
            }
            self.ledger.append("git_auto_commit_skipped_paths", payload)
            self._emit("git_auto_commit_skipped_paths", payload)
        if no_change_warning:
            session["final_message"] = (
                f"{session.get('final_message') or content}\n\n{no_change_warning}"
            ).strip()
        session["updated_at"] = now_iso()
        conversation_summary = self._summarize_session(provider, session, phase="final", turn=turn)
        self._save_session(session)
        if session["git_result_sha"]:
            self.ledger.append(
                "git_commit_created",
                {"session_id": session_id, "git_result_sha": session["git_result_sha"]},
            )
        self.ledger.append(
            "coding_session_completed",
            {
                "session_id": session_id,
                "git_result_sha": session["git_result_sha"],
                "verification_report": verification_report,
                "semantic_acceptance_report": acceptance_report,
            },
        )
        self._emit(
            "completed",
            {
                "session_id": session_id,
                "text": content,
                "git_result_sha": session["git_result_sha"],
                "summary": conversation_summary,
                "summary_path": session.get("summary_path", ""),
                "git_auto_commit_skipped_paths": skipped_paths,
                "git_auto_commit_warning": commit_warning,
                "no_changes_detected": bool(no_change_warning),
                "verification_report": verification_report,
                "semantic_acceptance_report": acceptance_report,
            },
        )
        return {
            "session_id": session_id,
            "git_result_sha": session["git_result_sha"],
            "text": content,
            "summary": conversation_summary,
            "summary_path": session.get("summary_path", ""),
            "git_auto_commit_skipped_paths": skipped_paths,
            "git_auto_commit_warning": commit_warning,
            "no_changes_detected": bool(no_change_warning),
            "verification_report": verification_report,
            "semantic_acceptance_report": acceptance_report,
            "checkpoint_id": self.last_checkpoint.checkpoint_id if self.last_checkpoint else "",
            "turn_count": int(turn),
            "usage": ModelRequestUsage.snapshot(session),
        }
