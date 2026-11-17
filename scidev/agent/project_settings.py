"""Project-local settings for agent policies, summaries and collaboration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scidev.agent.acceptance import SemanticAcceptance
from scidev.runtime.policy import ExecutionMode, NetworkPolicy, normalize_command_prefixes


@dataclass
class SummarySettings:
    """Project-local policy for summary, review, memory and collaboration features."""

    enabled: bool = True
    model: str = ""
    max_tokens: int = 12000
    context_chars: int = 18000
    retries: int = 2
    interval_turns: int = 4
    instruction: str = "请用中文总结本次编码对话，包含：目标、实际修改、验证结果、未完成事项和下一步建议。不要编造没有发生的事实。"
    memory_enabled: bool = True
    memory_results: int = 3
    memory_context_chars: int = 6000
    acceptance_mode: str = "off"
    acceptance_model: str = ""
    multi_agent_enabled: bool = False
    multi_agent_model: str = ""
    context_window_tokens: int = 32768
    execution_mode: str = "workspace-write"
    max_agent_turns: int = 32
    max_task_tokens: int = 0
    network_policy: str = "ask"
    allowed_commands: tuple[str, ...] = ()
    sandbox_backend: str = "local"
    sandbox_image: str = ""

    @classmethod
    def config_path(cls, project_root: Path) -> Path:
        return Path(project_root).resolve() / ".research" / "settings.json"

    @classmethod
    def load(cls, project_root: Path) -> "SummarySettings":
        path = cls.config_path(project_root)
        data: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, json.JSONDecodeError):
            pass

        enabled = data.get("summary_enabled", cls.enabled)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "no", "off", ""}
        if os.getenv("SCIDEV_SUMMARY_ENABLED") is not None:
            enabled = os.getenv("SCIDEV_SUMMARY_ENABLED", "").strip().lower() not in {"", "0", "false", "no", "off"}
        model = str(os.getenv("SCIDEV_SUMMARY_MODEL") or data.get("summary_model") or "").strip()
        max_tokens = data.get("summary_max_tokens", cls.max_tokens)
        context_chars = data.get("summary_context_chars", cls.context_chars)
        retries = data.get("summary_retries", cls.retries)
        interval_turns = data.get("summary_interval_turns", cls.interval_turns)
        try:
            max_tokens = max(200, min(32000, int(max_tokens)))
        except (TypeError, ValueError):
            max_tokens = cls.max_tokens
        try:
            context_chars = max(4000, min(50000, int(context_chars)))
        except (TypeError, ValueError):
            context_chars = cls.context_chars
        try:
            retries = max(0, min(3, int(retries)))
        except (TypeError, ValueError):
            retries = cls.retries
        if os.getenv("SCIDEV_SUMMARY_INTERVAL_TURNS") is not None:
            interval_turns = os.getenv("SCIDEV_SUMMARY_INTERVAL_TURNS", "").strip()
        try:
            interval_turns = max(0, min(32, int(interval_turns)))
        except (TypeError, ValueError):
            interval_turns = cls.interval_turns
        instruction = str(data.get("summary_instruction") or cls.instruction).strip() or cls.instruction
        memory_enabled = data.get("project_memory_enabled", cls.memory_enabled)
        if isinstance(memory_enabled, str):
            memory_enabled = memory_enabled.strip().lower() not in {"0", "false", "no", "off", ""}
        if os.getenv("SCIDEV_MEMORY_ENABLED") is not None:
            memory_enabled = os.getenv("SCIDEV_MEMORY_ENABLED", "").strip().lower() not in {"", "0", "false", "no", "off"}
        memory_results = data.get("project_memory_results", cls.memory_results)
        memory_context_chars = data.get("project_memory_context_chars", cls.memory_context_chars)
        try:
            memory_results = max(1, min(8, int(memory_results)))
        except (TypeError, ValueError):
            memory_results = cls.memory_results
        try:
            memory_context_chars = max(500, min(16000, int(memory_context_chars)))
        except (TypeError, ValueError):
            memory_context_chars = cls.memory_context_chars
        acceptance_mode = SemanticAcceptance.mode(data.get("semantic_acceptance_mode", cls.acceptance_mode))
        acceptance_model = str(data.get("semantic_acceptance_model") or "").strip()[:160]
        raw_multi_agent_enabled = data.get("multi_agent_enabled", cls.multi_agent_enabled)
        if isinstance(raw_multi_agent_enabled, str):
            multi_agent_enabled = raw_multi_agent_enabled.strip().lower() in {"1", "true", "yes", "on"}
        else:
            multi_agent_enabled = raw_multi_agent_enabled is True
        multi_agent_model = str(data.get("multi_agent_model") or "").strip()[:160]
        context_window_tokens = data.get("model_context_window_tokens", cls.context_window_tokens)
        try:
            context_window_tokens = max(1024, min(1_048_576, int(context_window_tokens)))
        except (TypeError, ValueError, OverflowError):
            context_window_tokens = cls.context_window_tokens
        execution_mode = ExecutionMode.parse(
            os.getenv("SCIDEV_EXECUTION_MODE") or data.get("execution_mode", cls.execution_mode)
        ).value
        network_policy = NetworkPolicy.parse(
            os.getenv("SCIDEV_NETWORK_POLICY") or data.get("network_policy", cls.network_policy)
        ).value
        allowed_commands = tuple(normalize_command_prefixes(
            os.getenv("SCIDEV_ALLOWED_COMMANDS") or data.get("allowed_commands", list(cls.allowed_commands))
        ))
        max_agent_turns = data.get("max_agent_turns", cls.max_agent_turns)
        if os.getenv("SCIDEV_MAX_AGENT_TURNS") is not None:
            max_agent_turns = os.getenv("SCIDEV_MAX_AGENT_TURNS", "").strip()
        try:
            max_agent_turns = max(1, min(64, int(max_agent_turns)))
        except (TypeError, ValueError, OverflowError):
            max_agent_turns = cls.max_agent_turns
        max_task_tokens = data.get("max_task_tokens", cls.max_task_tokens)
        if os.getenv("SCIDEV_MAX_TASK_TOKENS") is not None:
            max_task_tokens = os.getenv("SCIDEV_MAX_TASK_TOKENS", "").strip()
        try:
            max_task_tokens = max(0, min(10_000_000, int(max_task_tokens)))
        except (TypeError, ValueError, OverflowError):
            max_task_tokens = cls.max_task_tokens
        sandbox_backend = str(
            os.getenv("SCIDEV_SANDBOX_BACKEND") or data.get("sandbox_backend", cls.sandbox_backend)
        ).strip().lower()
        if sandbox_backend not in {"local", "docker"}:
            sandbox_backend = "local"
        sandbox_image = str(
            os.getenv("SCIDEV_SANDBOX_IMAGE") or data.get("sandbox_image", cls.sandbox_image)
        ).strip()[:200]
        return cls(bool(enabled), model, max_tokens, context_chars, retries, interval_turns, instruction,
                   bool(memory_enabled), memory_results, memory_context_chars,
                   acceptance_mode, acceptance_model, bool(multi_agent_enabled), multi_agent_model,
                   context_window_tokens, execution_mode, max_agent_turns, max_task_tokens,
                   network_policy, allowed_commands, sandbox_backend, sandbox_image)

    def save(self, project_root: Path) -> None:
        path = self.config_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "summary_enabled": self.enabled,
                    "summary_model": self.model,
                    "summary_max_tokens": self.max_tokens,
                    "summary_context_chars": self.context_chars,
                    "summary_retries": self.retries,
                    "summary_interval_turns": self.interval_turns,
                    "summary_instruction": self.instruction,
                    "project_memory_enabled": self.memory_enabled,
                    "project_memory_results": self.memory_results,
                    "project_memory_context_chars": self.memory_context_chars,
                    "semantic_acceptance_mode": SemanticAcceptance.mode(self.acceptance_mode),
                    "semantic_acceptance_model": self.acceptance_model.strip()[:160],
                    "multi_agent_enabled": bool(self.multi_agent_enabled),
                    "multi_agent_model": self.multi_agent_model.strip()[:160],
                    "model_context_window_tokens": max(1024, min(1_048_576, int(self.context_window_tokens))),
                    "execution_mode": ExecutionMode.parse(self.execution_mode).value,
                    "network_policy": NetworkPolicy.parse(self.network_policy).value,
                    "allowed_commands": list(self.allowed_commands),
                    "max_agent_turns": max(1, min(64, int(self.max_agent_turns))),
                    "max_task_tokens": max(0, min(10_000_000, int(self.max_task_tokens))),
                    "sandbox_backend": "docker" if self.sandbox_backend == "docker" else "local",
                    "sandbox_image": self.sandbox_image.strip()[:200],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
