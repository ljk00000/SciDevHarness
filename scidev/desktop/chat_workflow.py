"""Agent progress rendering, prompt submission, context and attachments.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from PySide6.QtCore import QBuffer
from PySide6.QtCore import QIODevice
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QLabel
from scidev.agent.planning import TaskPlan
from scidev.runtime.helpers import new_id
from scidev.desktop.chat_ui import ExecutionGroup
from scidev.desktop.chat_ui import MessageBubble
from scidev.desktop.editor import CodeEditor as CodeEditor
from scidev.agent.attachments import AttachmentError
from scidev.agent.attachments import image_from_bytes
from scidev.agent.attachments import load_image_file
from scidev.workspace.environment import APPROVAL_EXPLANATION
from scidev.workspace.environment import APPROVAL_PURPOSE
from scidev.workspace.environment import ProjectEnvironmentInspector
from scidev.agent.usage import ModelRequestUsage
from scidev.agent.verification import VerificationTracker


class ChatWorkflowMixin:
    """Operate on host state; introduce no initialization or additional state."""

    @staticmethod
    def _set_state(widget: QLabel, text: str, name: str) -> None:
        widget.setText(text)
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


    @staticmethod
    def _tool_event_speaker(payload: dict[str, Any]) -> str:
        if payload.get("source") == "harness_precommit":
            return "Harness · 提交前 diff"
        return f"工具 · {payload.get('name', '')}"


    def _handle_agent_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "context_usage_updated":
            if not payload.get("session_id") or str(payload.get("session_id")) == str(self.current_session_id or ""):
                try:
                    self._context_usage_estimated_tokens = max(0, int(payload.get("estimated_tokens", 0) or 0))
                    self._context_usage_session_id = str(payload.get("session_id") or "")
                except (TypeError, ValueError, OverflowError):
                    self._context_usage_estimated_tokens = 0
                    self._context_usage_session_id = str(payload.get("session_id") or "")
                self._context_usage_actual_tokens = None
                self._render_context_meter()
        elif event == "model_usage_updated":
            self._display_model_usage(payload)
        elif event == "project_context_updated":
            self._context_generation += 1
            self._display_project_context(payload)
        elif event == "project_memory_retrieved":
            count = int(payload.get("count", 0) or 0)
            if count:
                self._append_execution_chat(
                    "Harness · 项目记忆",
                    f"已从本项目历史会话中检索并引用 {count} 条相关总结。",
                    "meta",
                    session_id=payload.get("session_id"),
                )
            elif payload.get("error"):
                self._append_log(f"项目记忆不可用，不影响当前任务：{payload['error']}")
        elif event == "specialist_started":
            labels = {
                "architecture": "架构调查", "tests": "测试专员", "security": "安全调查",
                "coder": "编码专员", "reviewer": "代码审阅",
            }
            role = str(payload.get("role") or "协作")
            boundary = (
                "只能对委派范围提交一次逐块审批补丁，不可直接写文件或运行命令。"
                if role == "coder"
                else "只读调查，不会修改文件或运行命令。"
            )
            self._append_execution_chat(
                f"协作 Agent · {labels.get(role, role)}",
                f"开始协作任务 · 模型 {payload.get('model') or '跟随主模型'} · "
                f"范围：{', '.join(payload.get('focus_paths') or []) or '整个项目'}。{boundary}",
                "meta",
                session_id=payload.get("session_id"),
            )
            self._append_log(f"协作 Agent 开始 · {labels.get(role, role)}")
        elif event == "specialist_tool_used":
            self._append_log(
                f"协作 Agent 工具 · {payload.get('tool', '')} · "
                f"第 {payload.get('tool_calls', '?')} 次"
            )
        elif event == "specialist_completed":
            labels = {
                "architecture": "架构调查", "tests": "测试专员", "security": "安全调查",
                "coder": "编码专员", "reviewer": "代码审阅",
            }
            role = str(payload.get("role") or "协作")
            self._append_execution_chat(
                f"协作 Agent · {labels.get(role, role)}",
                str(payload.get("summary") or "调查完成，没有返回文字发现。"),
                "meta",
                session_id=payload.get("session_id"),
            )
            self._append_log(
                f"协作 Agent 完成 · {labels.get(role, role)} · "
                f"{payload.get('tool_calls', 0)} 次工具调用"
            )
        elif event == "specialist_failed":
            failed_status = str(payload.get("status", "unknown"))
            failed_message = str(payload.get("summary") or "子 Agent 未能完成调查。")
            if failed_status == "review_rejected":
                failed_message = f"用户未批准编码专员补丁，任务已停止；没有使用其他写入工具绕过。{failed_message}"
            self._append_execution_chat(
                "协作 Agent · 调查未完成",
                failed_message,
                "meta",
                session_id=payload.get("session_id"),
            )
            self._append_log(f"协作 Agent 未完成 · {failed_status}")
        elif event == "specialist_result_reused":
            self._append_log(
                f"复用同一代码版本的协作调查 · {payload.get('role', '')}"
            )
        elif event == "specialist_limit_reached":
            self._append_log("协作 Agent 达到每任务 3 次上限，本次委派未启动")
        elif event == "project_context_preview":
            self._context_preview_pending = False
            self.project_context_refresh.setEnabled(True)
            if payload.get("generation") == self._context_generation:
                if payload.get("error"):
                    self.project_context_meta.setText("上下文刷新失败，请重试")
                    self._append_log(str(payload["error"]))
                else:
                    self._display_project_context(payload)
        elif event == "project_environment_check_result":
            self._environment_check_running = False
            self.project_environment_check.setEnabled(True)
            self._project_environment_report = str(payload.get("report") or "环境核验未返回报告")
            status = str(payload.get("status") or "unknown")
            if self.project_context_snapshot.get("text"):
                self._display_project_context(self.project_context_snapshot)
            else:
                self.project_context_output.setPlainText(self._project_environment_report)
                self.project_context_meta.setText(f"项目环境核验 · {status}")
            checks = payload.get("checks") or []
            checked_summary = "、".join(str(item) for item in checks[:8]) if checks else "未执行诊断命令"
            self._append_log(
                f"项目环境核验：{status} · {checked_summary}"
            )
        elif event == "plan_restored":
            self.task_plan_panel.set_plan(
                payload.get("plan"),
                run_state="running",
            )
        elif event == "plan_updated":
            self.task_plan_panel.set_plan(
                payload.get("plan"),
                run_state="running",
            )
            plan = TaskPlan.from_record(payload.get("plan"))
            if plan is not None:
                self._append_log(f"任务计划已更新 · {plan.progress[0]}/{plan.progress[1]}")
        elif event == "plan_incomplete":
            self.task_plan_panel.set_run_state("incomplete")
            self._append_log(str(payload.get("reason") or payload.get("error") or "计划尚未完成"))
        elif event == "verification_reported":
            label = VerificationTracker.label(str(payload.get("status") or "not_run"))
            self._append_log(f"验证状态：{label}")
        elif event == "semantic_acceptance_started":
            self._append_execution_chat(
                "Harness · 独立语义验收",
                f"正在把任务与 {payload.get('changed_file_count', 0)} 个变更文件的 diff 发送给复核模型 "
                f"{payload.get('reviewer_model') or '（未配置）'}。这是额外模型调用，不是形式化证明。",
                "meta",
                session_id=payload.get("session_id"),
            )
        elif event == "semantic_acceptance_reviewed":
            self._append_log(
                f"语义验收：{payload.get('status', 'uncertain')} · "
                f"{payload.get('reviewer_model') or '未配置复核模型'}"
            )
        elif event == "semantic_acceptance_retry_scheduled":
            self._append_execution_chat(
                "Harness · 语义验收发现待修项",
                "复核模型报告了未满足的需求，Agent 将进行一次有限修正；之后需要重新检查并复核。",
                "meta",
                session_id=payload.get("session_id"),
            )
        elif event == "verification_retry_scheduled":
            self._append_execution_chat(
                "Harness · 需要验证",
                "代码有改动但尚无当前代码的通过检查；Agent 正在有限补验。命令仍需逐条批准。",
                "meta",
                session_id=payload.get("session_id"),
            )
        elif event == "verification_blocked":
            if payload.get("text"):
                self._append_chat("Agent · 未完成验证", str(payload["text"]), "agent")
            self._append_chat("Harness · 未自动提交", str(payload.get("error") or "代码未通过验证门槛。"), "meta")
            self._append_log("代码未完成验证，本次改动保留且没有自动提交")
        elif event == "semantic_acceptance_blocked":
            if payload.get("text"):
                self._append_chat("Agent · 语义验收未完成", str(payload["text"]), "agent")
            self._append_chat(
                "Harness · 未自动提交",
                str(payload.get("error") or "独立语义验收未通过，改动保留在工作区。"),
                "meta",
            )
            self._append_log("独立语义验收未通过，本次改动保留且没有自动提交")
        elif event == "patch_review_stopped":
            partial = bool(payload.get("patch_partial"))
            detail = (
                "只保留了你批准的补丁块；其余修改未应用。任务已停止且没有自动提交。"
                if partial else "补丁被拒绝或未完成审核；任务已停止，没有改用其他工具写入。"
            )
            self._append_chat("Harness · 补丁审核", detail, "meta")
            self._append_log("补丁审核结束，任务停止且未自动提交")
        if event == "summary_started":
            turn = payload.get("turn")
            label = f"第 {turn} 轮阶段总结" if payload.get("phase") == "checkpoint" and turn else "会话总结"
            self._append_execution_chat(
                label, "正在整理本次对话、修改和验证结果…", "meta", session_id=payload.get("session_id")
            )
            self._append_log(f"正在生成{label}")
        elif event == "summary_completed":
            turn = payload.get("turn")
            label = f"第 {turn} 轮阶段总结" if payload.get("phase") == "checkpoint" and turn else "会话总结"
            self._append_execution_chat(
                label, str(payload.get("text", "")), "summary", session_id=payload.get("session_id")
            )
            self._append_log(f"{label}已保存: {payload.get('path', '')}")
        elif event == "summary_failed":
            turn = payload.get("turn")
            label = f"第 {turn} 轮阶段总结" if payload.get("phase") == "checkpoint" and turn else "会话总结"
            self._append_execution_chat(
                label,
                f"总结生成失败，但代码任务仍会继续：{payload.get('error', '')}",
                "meta",
                session_id=payload.get("session_id"),
            )
            self._append_log(f"{label}失败，未影响代码任务")
        elif event == "verification_recorded":
            if payload.get("kind") != "command":
                category = {
                    "test": "测试", "lint": "代码风格", "typecheck": "类型检查",
                    "compile": "编译", "build": "构建", "check": "检查",
                }.get(str(payload.get("kind") or ""), "项目检查")
                label = VerificationTracker.label(str(payload.get("check_status") or "not_run"))
                exit_code = payload.get("exit_code")
                exit_text = f" · 退出码 {exit_code}" if exit_code is not None else ""
                command = str(payload.get("command") or "")[:180]
                detail = f"{category} · {label}{exit_text}"
                if command:
                    detail += f"\n{command}"
                self._append_execution_chat(
                    "Harness · 执行验证", detail, "meta", session_id=payload.get("session_id")
                )
                self._append_log(f"{category}：{label}")
        if event == "model_call_started":
            group = self._ensure_execution_group(payload.get("session_id"))
            if group is not None:
                group.set_state("running")
                group.record_model_call(payload.get("turn"))
            self._append_log(f"正在请求模型 · 第 {payload['turn']} 轮")
        elif event == "svg_creation_retry_scheduled":
            self._append_execution_chat(
                "Harness · SVG 生成补救",
                "模型上一轮没有写出 SVG 文件，Harness 正在自动补问一次。",
                "meta",
                session_id=payload.get("session_id"),
            )
            self._append_log("模型未生成 SVG 文件，已安排一次自动补救")
        elif event == "assistant_delta":
            self._append_assistant_delta(payload)
        elif event == "assistant":
            self._finish_streamed_assistant(payload)
        elif event == "tool_started":
            group = self._ensure_execution_group(payload.get("session_id"))
            if group is not None:
                self._move_latest_stream_to_execution_group(payload.get("session_id"))
                group.record_tool_call(str(payload.get("name") or "工具"))
            if payload.get("name") == "update_plan":
                self._append_execution_chat(
                    "工具 · update_plan", "更新任务计划", "tool", session_id=payload.get("session_id")
                )
                self._append_log("Agent 正在更新任务计划")
                return
            if payload.get("name") == "apply_patch":
                self._append_execution_chat(
                    "工具 · apply_patch", "准备补丁，等待逐块审核", "tool", session_id=payload.get("session_id")
                )
                self._append_log("补丁已准备，等待逐块审核")
                return
            arguments = payload.get("arguments")
            args = json.dumps(arguments, ensure_ascii=False) if arguments else "无需参数"
            self._append_execution_chat(
                self._tool_event_speaker(payload), args, "tool", session_id=payload.get("session_id")
            )
        elif event == "tool_result":
            group = self._ensure_execution_group(payload.get("session_id"))
            if group is not None:
                group.set_activity(f"已完成 {payload.get('name') or '工具'}")
            if payload.get("name") == "update_plan":
                result = str(payload.get("result", ""))
                if result.startswith("工具执行失败："):
                    self._append_log(result)
                self._append_execution_chat(
                    "工具 · update_plan",
                    result[-1400:] or "任务计划已更新",
                    "tool",
                    session_id=payload.get("session_id"),
                )
                return
            result = str(payload.get("result", ""))
            self._append_execution_chat(
                self._tool_event_speaker(payload), result[-1400:], "tool", session_id=payload.get("session_id")
            )
        elif event == "git_auto_commit_skipped_paths":
            self._append_chat(
                "Harness · Git 自动提交保护",
                str(payload.get("message", "部分文件未自动提交，请在 Git 面板审核。")),
                "meta",
            )
            self._append_log("部分文件受 Git 自动提交安全策略保护，仍保留在工作区")
        elif event == "no_changes_detected":
            self._append_chat(
                "Harness · 未检测到文件改动",
                str(payload.get("message") or "本次没有写入任何文件。"),
                "meta",
            )
            self._append_log("任务结束但未检测到文件改动")
        elif event == "completed":
            sha = str(payload.get("git_result_sha") or "")
            if not sha:
                sha = "未自动提交" if payload.get("git_auto_commit_warning") else "无新提交"
            report = payload.get("verification_report")
            status = report.get("status") if isinstance(report, dict) else "not_run"
            label = VerificationTracker.label(str(status or "not_run"))
            self._append_execution_chat(
                "系统",
                f"任务完成 · Git commit: {sha[:12]}\n验证：{label}",
                "meta",
                session_id=payload.get("session_id"),
            )
            self._finish_execution_group("completed", payload.get("session_id"))
            self._refresh_open_file_after_task()
            self.refresh_git_status()
            self._refresh_development_tree()


    def _append_assistant_delta(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", ""))
        if not text:
            return
        key = (str(payload.get("session_id", "")), int(payload.get("turn", 0)))
        bubble = self._streaming_bubbles.get(key)
        if bubble is None:
            bubble = MessageBubble("Agent · 正在生成", "", "agent")
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
            self._streaming_bubbles[key] = bubble
        bubble.append_message(text)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)


    def _finish_streamed_assistant(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id", ""))
        try:
            turn = int(payload.get("turn", 0))
        except (TypeError, ValueError):
            turn = 0
        bubble = self._streaming_bubbles.pop((session_id, turn), None)
        text = str(payload.get("text", ""))
        if bubble is None:
            self._append_chat("Agent", text, "agent")
            return
        bubble.set_speaker("Agent")
        bubble.set_message(text)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)


    def _interrupt_streaming_bubbles(self, speaker: str) -> None:
        for bubble in self._streaming_bubbles.values():
            bubble.set_speaker(speaker)
        self._streaming_bubbles.clear()


    def _ensure_execution_group(self, session_id: object = None) -> ExecutionGroup | None:
        if not self._execution_group_active:
            return None
        requested_session = str(session_id or self._execution_group_session_id)
        if requested_session != self._execution_group_session_id:
            return None
        if self._active_execution_group is None:
            group = ExecutionGroup(requested_session)
            self._active_execution_group = group
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, group)
        return self._active_execution_group


    def _move_latest_stream_to_execution_group(self, session_id: object = None) -> None:
        requested_session = str(session_id or self._execution_group_session_id)
        candidates = [
            (turn, bubble)
            for (stream_session, turn), bubble in self._streaming_bubbles.items()
            if stream_session == requested_session
        ]
        if not candidates:
            return
        turn, bubble = max(candidates, key=lambda item: item[0])
        group = self._ensure_execution_group(requested_session)
        if group is None:
            return
        self._streaming_bubbles.pop((requested_session, turn), None)
        self.chat_layout.removeWidget(bubble)
        group.add_item(bubble)


    def _move_streams_to_execution_group(
        self,
        session_id: object = None,
        *,
        speaker: str = "",
    ) -> None:
        requested_session = str(session_id or self._execution_group_session_id)
        candidates = sorted(
            (
                (turn, bubble)
                for (stream_session, turn), bubble in self._streaming_bubbles.items()
                if stream_session == requested_session
            ),
            key=lambda item: item[0],
        )
        if not candidates:
            return
        group = self._ensure_execution_group(requested_session)
        if group is None:
            return
        for turn, bubble in candidates:
            self._streaming_bubbles.pop((requested_session, turn), None)
            self.chat_layout.removeWidget(bubble)
            if speaker:
                bubble.set_speaker(speaker)
            group.add_item(bubble)


    def _append_execution_chat(
        self,
        speaker: str,
        message: str,
        kind: str,
        session_id: object = None,
    ) -> None:
        self._append_chat(speaker, message, kind, process=True, session_id=session_id)


    def _finish_execution_group(self, state: str, session_id: object = None) -> None:
        requested_session = str(session_id or self._execution_group_session_id)
        if self._execution_group_active and requested_session == self._execution_group_session_id:
            self._move_streams_to_execution_group(requested_session)
            if self._active_execution_group is not None:
                self._active_execution_group.set_state(state)
            self._execution_group_active = False
            self._execution_group_session_id = ""
            self._active_execution_group = None


    @staticmethod
    def _worker_message(event: str, payload: dict[str, Any]) -> str:
        task_id = payload.get("task_id", "")
        if event == "task_retry":
            return f"{task_id} · 网络暂时不可用，已安排定时重试"
        if event == "task_failed":
            return f"{task_id} · 任务失败：{payload.get('error', '')}"
        if event == "task_completed":
            return f"{task_id} · 任务完成"
        if event == "task_started":
            return f"{task_id} · Agent 开始工作（第 {payload.get('attempts', 1)} 次尝试）"
        return f"{task_id} · {event}"


    def _append_log(self, message: str) -> None:
        self.statusBar().showMessage(message[:220])


    def _append_chat(
        self,
        speaker: str,
        message: str,
        kind: str,
        *,
        process: bool = False,
        session_id: object = None,
    ) -> None:
        bubble = MessageBubble(speaker, message, kind)
        group = self._ensure_execution_group(session_id) if process else None
        if group is not None:
            group.add_item(bubble)
        else:
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)


    def _scroll_chat_to_bottom(self) -> None:
        try:
            scrollbar = self.chat_scroll.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except RuntimeError:
            # A queued scroll may run after the window has already closed.
            return


    def _clear_chat_history(self) -> None:
        for request in list(self._pending_patch_reviews.values()):
            self._resolve_patch_review(request, None)
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


    def _insert_current_file_context(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            self._append_log("请先在编辑器中打开一个文件")
            return
        relative = path.relative_to(self.project_root).as_posix()
        if relative not in self._context_file_paths():
            self._append_log("该文件属于 Agent 不可访问的内部或敏感路径")
            return
        selected = editor.textCursor().selectedText().replace("\u2029", "\n")[:4000]
        context = f"\n\n请参考当前文件：`{relative}`"
        if selected:
            context += f"\n当前选中代码：\n```\n{selected}\n```"
        self.chat_input.insertPlainText(context)
        self.chat_input.setFocus()


    def _prompt_with_editor_context(
        self,
        prompt: str,
        content_overrides: dict[str, str] | None = None,
        readonly_paths: list[str] | None = None,
    ) -> str:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            return prompt
        relative = path.relative_to(self.project_root).as_posix()
        if relative not in self._context_file_paths():
            return prompt
        selected = editor.textCursor().selectedText().replace("\u2029", "\n")[:2400]
        context = f"\n\n当前编辑器文件：{relative}（如需修改，请先读取该文件）"
        if editor.document().isModified():
            overrides = content_overrides or {}
            readonly = {item.casefold() for item in (readonly_paths or [])}
            if relative in overrides:
                context = (
                    f"\n\n当前编辑器文件：{relative}（有未保存修改；Harness 已将本次编辑器缓冲作为只读上下文提供。"
                    "请勿尝试修改此文件；如确需修改，请提醒我保存后重新发起任务。）"
                )
            elif relative.casefold() in readonly:
                context = (
                    f"\n\n当前编辑器文件：{relative}（有未保存修改，但文件因上下文预算限制未提供。"
                    "请勿按磁盘旧版本修改此文件，先提醒我保存后重试。）"
                )
        if selected:
            context += f"\n当前选中代码：\n```\n{selected}\n```"
        return prompt + context


    def _context_file_paths(self) -> list[str]:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if path is None or not path.is_relative_to(self.project_root):
            return []
        relative = path.relative_to(self.project_root).as_posix()
        try:
            self.agent.toolbox.project_context.resolve_scope(relative)
        except ValueError:
            return []
        return [relative]


    def _editor_context_state(self) -> tuple[dict[str, str], list[str], int]:
        """Capture bounded dirty buffers; they remain read-only for this task."""
        active = self._active_editor()
        entries = list(self._editor_paths.items())
        entries.sort(key=lambda item: item[0] is not active)
        overrides: dict[str, str] = {}
        readonly: list[str] = []
        seen: set[str] = set()
        total_bytes = 0
        skipped = 0
        for editor, path in entries:
            if not isinstance(editor, CodeEditor) or not editor.document().isModified():
                continue
            try:
                relative = path.resolve().relative_to(self.project_root).as_posix()
                self.agent.toolbox.project_context.resolve_scope(relative)
            except (OSError, RuntimeError, ValueError):
                continue
            if relative.casefold() in seen:
                continue
            seen.add(relative.casefold())
            if len(readonly) >= 128:
                skipped += 1
                continue
            readonly.append(relative)
            if len(overrides) >= self.agent.toolbox.project_context.MAX_CONTENT_OVERRIDES:
                skipped += 1
                continue
            text = editor.toPlainText()
            encoded = text.encode("utf-8")
            if (
                b"\x00" in encoded
                or len(encoded) > self.agent.toolbox.MAX_CONTEXT_FILE_BYTES
                or total_bytes + len(encoded) > self.agent.toolbox.project_context.MAX_OVERRIDE_BYTES
            ):
                skipped += 1
                continue
            overrides[relative] = text
            total_bytes += len(encoded)
        return overrides, readonly, skipped


    def _display_project_context(self, snapshot: dict[str, Any]) -> None:
        self.project_context_snapshot = dict(snapshot)
        context_text = str(snapshot.get("text", ""))
        if self._project_environment_report:
            context_text += "\n\n──────── 项目环境核验 ────────\n" + self._project_environment_report
        self.project_context_output.setPlainText(context_text)
        counts = f"{snapshot.get('file_count', 0)} 个文件 · {snapshot.get('indexed_count', 0)} 个文本"
        suffix = " · 范围已截断" if snapshot.get("limited") else ""
        override_paths = snapshot.get("editor_overrides") or []
        excluded_paths = snapshot.get("editor_excluded") or []
        if override_paths:
            suffix += f" · {len(override_paths)} 个未保存缓冲只读接入"
        if excluded_paths or snapshot.get("editor_context_skipped"):
            suffix += " · 有未保存文件因预算未纳入"
        self.project_context_meta.setText(counts + suffix)
        tooltip = "相关文件：\n" + "\n".join(snapshot.get("sources", []))
        if override_paths:
            tooltip += "\n\n未保存只读缓冲：\n" + "\n".join(override_paths)
        if excluded_paths:
            tooltip += "\n\n因预算排除：\n" + "\n".join(excluded_paths)
        self.project_context_meta.setToolTip(tooltip)
        self.chat_mode_chip.setToolTip("项目上下文已更新 · " + counts + suffix + "\n" + tooltip)


    def show_project_context(self) -> None:
        self.show_workspace()
        self.bottom_tabs.setVisible(True)
        self.bottom_tabs.setCurrentIndex(self.project_context_tab_index)
        if not self.project_context_snapshot:
            self.refresh_project_context()


    def refresh_project_context(self) -> None:
        if self._context_preview_pending:
            return
        self._context_preview_pending = True
        self.project_context_refresh.setEnabled(False)
        self.project_context_meta.setText("正在检索项目上下文…")
        query = self.chat_input.toPlainText().strip() or self._last_context_query
        paths = self._context_file_paths()
        overrides, readonly_paths, skipped = self._editor_context_state()
        excluded = [path for path in readonly_paths if path.casefold() not in {item.casefold() for item in overrides}]
        generation = self._context_generation

        def build_preview() -> None:
            try:
                snapshot = self.agent.context_snapshot(query, paths, overrides, excluded)
                snapshot["editor_context_skipped"] = skipped
            except Exception as exc:  # UI background failures stay non-modal.
                snapshot = {"error": f"项目上下文刷新失败：{exc}"}
            if not self._closing:
                self.signals.agent_event.emit("project_context_preview", {**snapshot, "generation": generation})

        threading.Thread(target=build_preview, name="project-context-preview", daemon=True).start()


    def check_project_environment(self) -> None:
        if self._environment_check_running:
            return
        if self.active_task_id:
            self._append_log("项目环境核验暂不可用：请等待当前编码任务结束。")
            return
        self._environment_check_running = True
        self.project_environment_check.setEnabled(False)
        self.project_context_meta.setText("正在准备项目环境核验…")

        def inspect_environment() -> None:
            try:
                inspector = ProjectEnvironmentInspector(
                    self.project_root,
                    lambda command, timeout: self._request_command_approval(
                        command,
                        timeout,
                        purpose=APPROVAL_PURPOSE,
                        explanation=APPROVAL_EXPLANATION,
                    ),
                    event_recorder=self.ledger.append,
                )
                result = inspector.inspect()
            except Exception as exc:  # Keep an optional local diagnostic from affecting the editor.
                result = {
                    "status": "failed",
                    "report": f"项目环境核验失败：{type(exc).__name__}",
                }
            if not self._closing:
                self.signals.agent_event.emit("project_environment_check_result", result)

        threading.Thread(
            target=inspect_environment,
            name="project-environment-check",
            daemon=True,
        ).start()


    def _sync_stop_button(self) -> None:
        button = getattr(self, "stop_task_button", None)
        if button is None:
            return
        running = self.active_task_id is not None
        button.setVisible(running)
        button.setEnabled(running)


    def cancel_active_task(self) -> None:
        """Stop the running task before its next model call, tool or commit."""
        if self.active_task_id is None:
            self._append_log("当前没有正在运行的任务")
            return
        self.agent.request_cancel(self.active_task_id)
        self._cancel_requested = True
        self._append_chat(
            "系统",
            "已请求停止；当前模型调用返回后不再执行工具或自动提交，改动保留在工作区。",
            "meta",
        )
        self._append_log("已请求停止当前任务")
        if hasattr(self, "stop_task_button"):
            self.stop_task_button.setEnabled(False)


    def _attach_image_path(self, raw_path: str) -> None:
        if len(self._pending_images) >= 4:
            self._append_log("最多附加 4 张图片")
            return
        try:
            attachment = load_image_file(Path(raw_path))
        except AttachmentError as exc:
            self._append_log(f"图片未附加：{exc}")
            return
        self._pending_images.append(attachment)
        self._update_image_attachment_ui()
        self._append_log(f"已附加图片：{attachment['name']}")


    def _attach_pasted_image(self, image: QImage) -> None:
        if len(self._pending_images) >= 4:
            self._append_log("最多附加 4 张图片")
            return
        if not isinstance(image, QImage) or image.isNull():
            self._append_log("剪贴板中没有可用的图片")
            return
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            self._append_log("无法读取剪贴板图片")
            return
        try:
            if not image.save(buffer, "PNG"):
                self._append_log("无法编码剪贴板图片")
                return
            data = bytes(buffer.data())
        finally:
            buffer.close()
        try:
            attachment = image_from_bytes(data, "clipboard.png")
        except AttachmentError as exc:
            self._append_log(f"图片未附加：{exc}")
            return
        self._pending_images.append(attachment)
        self._update_image_attachment_ui()
        self._append_log(f"已附加剪贴板图片：{attachment['name']}")


    def _choose_image_attachment(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(
            self,
            "选择图片附件",
            str(self.project_root),
            "图片 (*.png *.jpg *.jpeg *.gif *.webp)",
        )
        added = 0
        for raw_path in paths:
            if len(self._pending_images) >= 4:
                self._append_log("最多附加 4 张图片")
                break
            try:
                self._pending_images.append(load_image_file(Path(raw_path)))
                added += 1
            except AttachmentError as exc:
                self._append_log(f"图片未附加：{exc}")
        if added:
            self._update_image_attachment_ui()
            self._append_log(f"已附加 {added} 张图片")


    def _clear_image_attachments(self) -> None:
        self._pending_images.clear()
        self._update_image_attachment_ui()


    def _update_image_attachment_ui(self) -> None:
        count = len(self._pending_images)
        self.image_attachment_label.setVisible(count > 0)
        self.clear_image_button.setVisible(count > 0)
        if count:
            names = "、".join(str(item.get("name") or "image") for item in self._pending_images)
            self.image_attachment_label.setText(f"图片 {count} 张：{names}")
            self.image_attachment_label.setToolTip(names)


    def submit_chat(self) -> None:
        prompt = self.chat_input.toPlainText().strip()
        if not prompt:
            self.chat_input.setFocus()
            return
        if self._submit_prompt(prompt):
            self.chat_input.clear()


    def _submit_prompt(self, prompt: str) -> bool:
        if self._environment_check_running:
            self._append_chat("系统", "项目环境核验进行中，请等待核验结束后再启动编码任务。", "meta")
            return False
        if self.active_task_id:
            self._append_chat("系统", "当前任务仍在执行，请等待 Agent 完成后继续。", "meta")
            return False
        if self.current_session_id is None:
            self.current_session_id = new_id("session")
            self._append_chat("系统", f"已创建会话 {self.current_session_id}", "meta")
        self._cancel_requested = False
        self._execution_group_active = True
        self._execution_group_session_id = str(self.current_session_id)
        self._active_execution_group = None
        self._append_chat("你", prompt, "user")
        editor_overrides, readonly_paths, skipped_overrides = self._editor_context_state()
        task_prompt = self._prompt_with_editor_context(prompt, editor_overrides, readonly_paths)
        self._last_context_query = task_prompt
        self.active_task_id = self.worker.submit(
            "coding",
            {
                "session_id": self.current_session_id,
                "prompt": task_prompt,
                "context_files": self._context_file_paths(),
                "context_overrides": editor_overrides,
                "context_readonly_paths": readonly_paths,
                "planning_mode": self.planning_mode,
                "images": list(self._pending_images),
            },
            max_attempts=6,
        )
        self._clear_image_attachments()
        self._sync_stop_button()
        self.task_plan_panel.set_plan(None, run_state="running")
        self._set_state(self.chat_status, "排队中 · 等待 Agent", "StateWorking")
        self._set_state(self.workspace_state, "● 排队中", "StateWorking")
        self._append_log(f"编码任务已加入队列：{self.active_task_id}")
        if skipped_overrides:
            self._append_log(
                f"有 {skipped_overrides} 个未保存文件因数量或大小预算未载入上下文；Agent 将跳过这些文件的磁盘旧版本和修改。"
            )
        return True


    def new_coding_task(self) -> None:
        if self._environment_check_running:
            self._append_chat("系统", "项目环境核验进行中，暂时不能新建会话。", "meta")
            return
        if self.active_task_id:
            self._append_chat("系统", "当前任务仍在执行，暂时不能新建会话。", "meta")
            return
        self.show_workspace()
        self.current_session_id = None
        self._clear_image_attachments()
        self._execution_group_active = False
        self._execution_group_session_id = ""
        self._active_execution_group = None
        self._context_usage_estimated_tokens = 0
        self._context_usage_actual_tokens = None
        self._context_usage_session_id = ""
        self._render_context_meter()
        self._display_model_usage({"usage": ModelRequestUsage.empty(), "record": None})
        self._last_context_query = ""
        self.project_context_snapshot = {}
        self._context_generation += 1
        self.project_context_output.setPlainText(self._project_environment_report)
        self.project_context_meta.setText("发送任务时自动检索相关代码与项目指令")
        self.task_plan_panel.set_plan(None, run_state="idle")
        self._show_welcome()
        self._clear_chat_history()
        self._append_chat("系统", "已开始新会话。", "meta")
        self.chat_input.setFocus()
