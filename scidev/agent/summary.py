"""Summarize coding sessions and compact conversation context.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any
from scidev.runtime.errors import PermanentError
from scidev.runtime.errors import RetryableError as RetryableError
from scidev.integrations.model import OpenAICompatibleProvider as OpenAICompatibleProvider
from scidev.runtime.helpers import MAX_SERVER_RETRY_AFTER_SECONDS
from scidev.runtime.helpers import new_id
from scidev.runtime.helpers import now_iso
from scidev.agent.verification import VerificationTracker

MAX_SUMMARY_INLINE_RETRY_WAIT_SECONDS = 5.0


class AgentSummaryMixin:
    """Operate on host state; introduce no initialization or additional state."""

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                    chunks.append(str(item.get("text", "")))
            return "\n".join(chunks)
        return str(content or "")


    def _summary_source(self, session: dict[str, Any]) -> str:
        """Build a compact, role-labelled transcript for the summary request."""
        chunks: list[str] = []
        prior_summary = session.get("conversation_context_summary")
        has_prior_summary = isinstance(prior_summary, str) and bool(prior_summary.strip())
        if has_prior_summary:
            chunks.append(f"[previous_context_summary]\n{prior_summary.strip()}")
        messages = session.get("messages")
        messages = messages if isinstance(messages, list) else []
        start_index = 0
        if has_prior_summary:
            try:
                cutoff = int(session.get("context_compacted_through", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                cutoff = 0
            if 2 <= cutoff <= len(messages):
                start_index = cutoff
        for message in messages[start_index:]:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "message")
            if role == "system":
                continue
            content = self._text_content(message.get("content")).strip()
            if content:
                chunks.append(f"[{role}]\n{content}")
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    function = call.get("function") or {}
                    if isinstance(function, dict):
                        name = str(function.get("name") or "unknown")
                        chunks.append(f"[tool_call] {name}")
        commit_warning = str(session.get("git_auto_commit_warning") or "").strip()
        if commit_warning:
            chunks.append(f"[harness_git_safety]\n{commit_warning}")
        verification = session.get("verification_report")
        if isinstance(verification, dict):
            latest = verification.get("latest") if isinstance(verification.get("latest"), dict) else {}
            exit_code = latest.get("exit_code")
            exit_text = f"；退出码 {exit_code}" if exit_code is not None else ""
            chunks.append(
                "[harness_verification]\n"
                f"{VerificationTracker.label(str(verification.get('status') or 'not_run'))}"
                f"；检查数 {verification.get('check_count', 0)}{exit_text}"
            )
        acceptance = session.get("semantic_acceptance_report")
        if isinstance(acceptance, dict):
            chunks.append(
                "[harness_semantic_acceptance]\n"
                f"模式 {acceptance.get('mode', 'off')}；状态 {acceptance.get('status', 'uncertain')}；"
                f"复核模型 {acceptance.get('reviewer_model') or '未配置'}；"
                "此项是模型审查记录，不是形式化正确性证明。"
            )
        source = "\n\n".join(chunks)
        limit = self.summary_settings.context_chars
        if len(source) <= limit:
            return source
        marker = "\n\n[中间内容已折叠]\n\n"
        content_budget = max(0, limit - len(marker))
        head = content_budget // 3
        tail = content_budget - head
        head_text = source[:head] if head else ""
        tail_text = source[-tail:] if tail else ""
        return head_text + marker + tail_text


    def _summarize_session(
        self,
        provider: Any,
        session: dict[str, Any],
        phase: str = "final",
        turn: int | None = None,
    ) -> str:
        """Create and persist a checkpoint/final summary without blocking completion."""
        settings = self.summary_settings
        session_id = str(session["session_id"])
        if not settings.enabled:
            self.ledger.append(
                "conversation_summary_skipped",
                {"session_id": session_id, "reason": "disabled", "phase": phase, "turn": turn},
            )
            return ""

        self.ledger.append(
            "conversation_summary_started",
            {
                "session_id": session_id,
                "model": settings.model or getattr(provider, "model", "default"),
                "phase": phase,
                "turn": turn,
            },
        )
        self._emit("summary_started", {"session_id": session_id, "phase": phase, "turn": turn})
        summary_provider = provider
        if settings.model and hasattr(provider, "with_model"):
            summary_provider = provider.with_model(settings.model)
        summary_messages = [
            {
                "role": "system",
                "content": "你是科研编码项目的会话记录器。只根据给定对话总结已经发生的事实，禁止臆测。输出简洁、可检索的中文 Markdown。",
            },
            {
                "role": "user",
                "content": (
                    f"{settings.instruction}\n\n"
                    f"会话 ID：{session_id}\n"
                    f"Git 结果提交：{session.get('git_result_sha') or '无'}\n\n"
                    f"对话记录：\n{self._summary_source(session)}"
                ),
            },
        ]
        attempts = settings.retries + 1
        attempts_made = 0
        retry_deferred = False
        last_error: Exception | None = None
        request_scope = self._request_scope_id(session_id, str(session.get("last_task_id") or session_id))
        request_id = f"summary-{request_scope}-{phase}-turn-{turn if turn is not None else 'final'}"
        for attempt in range(attempts):
            try:
                attempts_made += 1
                request_options: dict[str, Any] = {}
                if isinstance(summary_provider, OpenAICompatibleProvider):
                    logical_key = (
                        f"{session.get('last_task_id') or session_id}|summary:{phase}:{turn if turn is not None else 'final'}"
                    )
                    request_options["on_metrics"] = lambda metrics, key=logical_key: self._record_model_request(
                        session, key, f"summary_{phase}", metrics
                    )
                message = summary_provider.chat(
                    summary_messages,
                    max_tokens=settings.max_tokens,
                    # Keep retries of one summary idempotent, but never reuse
                    # that key for a different checkpoint/final payload.
                    request_id=request_id,
                    **request_options,
                )
                text = self._text_content(message.get("content")).strip()
                if not text:
                    raise PermanentError("总结模型返回了空内容")
                summary_id = new_id("summary")
                summary_path = self.project_root / ".research" / "summaries" / f"{session_id}_{summary_id}.json"
                record = {
                    "summary_id": summary_id,
                    "session_id": session_id,
                    "created_at": now_iso(),
                    "phase": phase,
                    "turn": turn,
                    "model": getattr(summary_provider, "model", settings.model or "default"),
                    "text": text,
                    "config": {
                        "max_tokens": settings.max_tokens,
                        "context_chars": settings.context_chars,
                        "instruction": settings.instruction,
                    },
                }
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                try:
                    self.project_memory.index_summary(record)
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self.ledger.append("project_memory_index_failed", {
                        "session_id": session_id, "summary_id": summary_id, "error": str(exc)[:500],
                    })
                relative_path = summary_path.relative_to(self.project_root).as_posix()
                session.setdefault("summaries", []).append(
                    {"summary_id": summary_id, "created_at": record["created_at"], "path": relative_path, "text": text}
                )
                session["summary"] = text
                session["summary_path"] = relative_path
                session["summary_created_at"] = record["created_at"]
                self.ledger.append(
                    "conversation_summary_created",
                    {
                        "session_id": session_id,
                        "summary_id": summary_id,
                        "path": relative_path,
                        "phase": phase,
                        "turn": turn,
                        "text": text[:4000],
                    },
                )
                self._emit(
                    "summary_completed",
                    {
                        "session_id": session_id,
                        "summary_id": summary_id,
                        "path": relative_path,
                        "phase": phase,
                        "turn": turn,
                        "model": record["model"],
                        "text": text,
                    },
                )
                summary_for_context = text.strip()
                if len(summary_for_context) > self.MAX_CONVERSATION_CONTEXT_SUMMARY_CHARS:
                    notice = "\n[中间摘要已省略；如需细节请检查本地会话记录。]\n"
                    side = (self.MAX_CONVERSATION_CONTEXT_SUMMARY_CHARS - len(notice)) // 2
                    summary_for_context = summary_for_context[:side] + notice + summary_for_context[-side:]
                message_count = len(session.get("messages", []))
                try:
                    old_cutoff = max(0, int(session.get("context_compacted_through", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    old_cutoff = 0
                try:
                    prior_compactions = max(0, int(session.get("context_compactions", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    prior_compactions = 0
                session["conversation_context_summary"] = summary_for_context
                session["context_compacted_through"] = message_count
                session["context_compactions"] = prior_compactions + 1
                active_prompt = str(session.get("last_prompt") or session.get("prompt") or "")
                self._sync_task_plan_prompt(session, active_prompt)
                compact_event = {
                    "session_id": session_id,
                    "phase": phase,
                    "turn": turn,
                    "omitted_message_count": max(0, message_count - max(2, old_cutoff)),
                    "retained_summary_chars": len(summary_for_context),
                }
                self.ledger.append("conversation_context_compacted", compact_event)
                self._emit("conversation_context_compacted", compact_event)
                self._save_session(session)
                return text
            except RetryableError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    fallback_delay = min(2.0, 0.5 * (attempt + 1))
                    if exc.retry_after_seconds is None:
                        delay = fallback_delay
                    else:
                        try:
                            server_delay = float(exc.retry_after_seconds)
                        except (TypeError, ValueError, OverflowError):
                            server_delay = 0.0
                        if not math.isfinite(server_delay):
                            server_delay = 0.0
                        server_delay = min(MAX_SERVER_RETRY_AFTER_SECONDS, max(0.0, server_delay))
                        if server_delay > MAX_SUMMARY_INLINE_RETRY_WAIT_SECONDS:
                            retry_deferred = True
                            break
                        delay = max(fallback_delay, server_delay)
                    time.sleep(delay)
            except Exception as exc:  # Summary is auxiliary; a failed summary must not erase completed code work.
                last_error = exc
                break

        detail = str(last_error or "未知错误")
        self.ledger.append(
            "conversation_summary_failed",
            {
                "session_id": session_id,
                "error": detail[:1000],
                "attempts": attempts_made,
                "retry_deferred": retry_deferred,
                "phase": phase,
                "turn": turn,
            },
        )
        self._emit(
            "summary_failed",
            {"session_id": session_id, "error": detail[:1000], "phase": phase, "turn": turn},
        )
        return ""
