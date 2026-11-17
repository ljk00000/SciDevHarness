"""Model configuration, connection probes and Agent settings persistence.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import os
from PySide6.QtCore import QObject
from PySide6.QtCore import QThread
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMessageBox
from scidev.integrations.provider import PROVIDER_PRESETS
from scidev.integrations.provider import detect_local_providers
from scidev.integrations.provider import is_local_provider
from scidev.integrations.provider import preset_for
from scidev.integrations.provider import format_capabilities
from scidev.integrations.provider import load as load_provider_config
from scidev.integrations.provider import probe_capabilities
from scidev.integrations.provider import save as save_provider_config
from scidev.integrations.provider import test_connection as test_provider_connection
from scidev.runtime.policy import ExecutionMode
from scidev.runtime.policy import NetworkPolicy
from scidev.runtime.policy import normalize_command_prefixes
from scidev.agent.acceptance import SemanticAcceptance
from scidev.agent.project_settings import SummarySettings
from scidev.runtime.sandbox import SandboxError


class LocalProviderCheckThread(QThread):
    checked = Signal(object)

    def run(self) -> None:
        try:
            found = detect_local_providers()
        except Exception:  # noqa: BLE001 - local probe failure is non-fatal
            found = []
        self.checked.emit(found)



class ProviderCheckThread(QThread):
    checked = Signal(bool, str)

    def __init__(self, config, parent: QObject | None = None, *, probe: bool = False):  # noqa: ANN001
        super().__init__(parent)
        self.config = config
        self.probe = bool(probe)

    def run(self) -> None:
        try:
            if self.probe:
                result = probe_capabilities(self.config, timeout=20.0)
                ok = bool(result.get("models_count") or result.get("tools_supported"))
                message = format_capabilities(result)
            else:
                ok, message = test_provider_connection(self.config, timeout=12.0)
        except Exception as exc:  # noqa: BLE001 - a connectivity probe must not crash the UI
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self.checked.emit(ok, message)



class AgentSettingsMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _sync_provider_preset_selection(self) -> None:
        combo = getattr(self, "provider_preset_combo", None)
        if combo is None:
            return
        config = getattr(self, "provider_config", None)
        base_url = str(getattr(config, "base_url", "") or "").rstrip("/")
        combo.blockSignals(True)
        try:
            index = -1
            for preset in PROVIDER_PRESETS:
                if preset.base_url.rstrip("/") == base_url and base_url:
                    index = combo.findData(preset.key)
                    break
            if index < 0:
                index = combo.findData("custom")
            combo.setCurrentIndex(max(0, index))
        finally:
            combo.blockSignals(False)


    def _apply_provider_preset(self, _index: int = -1) -> None:
        combo = getattr(self, "provider_preset_combo", None)
        if combo is None:
            return
        key = str(combo.currentData() or "")
        if key.startswith("local-"):
            for item in self._detected_local_providers:
                if item.get("key") != key:
                    continue
                self.provider_base_edit.setText(str(item.get("base_url") or ""))
                self.provider_model_edit.setText(str(item.get("model") or ""))
                models = item.get("models")
                if isinstance(models, list):
                    self._available_models = [str(model) for model in models if str(model).strip()]
                self.provider_status_label.setText(
                    f"已填入 {item.get('label') or '本地模型'}；保存后下一次任务生效。"
                )
                break
            return
        preset = preset_for(key)
        if preset.key == "custom":
            self.provider_status_label.setText("自定义 OpenAI 兼容接口：填写地址、模型和 Key 后保存。")
            return
        self.provider_base_edit.setText(preset.base_url)
        self.provider_model_edit.setText(preset.model)
        if preset.local:
            self.provider_key_edit.setPlaceholderText("本地服务通常无需 Key；如需可填写")
        if preset.models:
            self._available_models = list(preset.models)
        self._pending_model_choice = preset.model
        note = preset.note or f"已填入 {preset.label}；填写 Key 后保存。"
        self.provider_status_label.setText(f"{preset.label}：{note}")
        if preset.local:
            self._detect_local_providers()


    def _detect_local_providers(self) -> None:
        thread = self._local_provider_thread
        if thread is not None and thread.isRunning():
            return
        self.provider_local_button.setEnabled(False)
        self.provider_status_label.setText("正在探测本机 Ollama / LM Studio / llama.cpp…")
        thread = LocalProviderCheckThread(self)
        thread.checked.connect(self._on_local_providers_detected)
        thread.finished.connect(self._on_local_provider_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._local_provider_thread = thread
        thread.start()


    def _on_local_provider_thread_finished(self) -> None:
        self._local_provider_thread = None
        if getattr(self, "_closing", False):
            return
        self.provider_local_button.setEnabled(True)


    def _on_local_providers_detected(self, found: object) -> None:
        if getattr(self, "_closing", False):
            return
        items = found if isinstance(found, list) else []
        self._detected_local_providers = [item for item in items if isinstance(item, dict)]
        combo = getattr(self, "provider_preset_combo", None)
        if combo is not None:
            for item in self._detected_local_providers:
                key = str(item.get("key") or "")
                if key and combo.findData(key) < 0:
                    combo.addItem(str(item.get("label") or key), key)
        if not self._detected_local_providers:
            self.provider_status_label.setText(
                "未检测到本地模型服务；可启动 Ollama (11434)、LM Studio (1234) 或 llama.cpp (8080) 后重试。"
            )
            return
        first = self._detected_local_providers[0]
        self.provider_base_edit.setText(str(first.get("base_url") or ""))
        self.provider_model_edit.setText(str(first.get("model") or ""))
        self._pending_model_choice = str(first.get("model") or "")
        self.provider_key_edit.setPlaceholderText("本地服务通常无需 Key；如需可填写")
        models = first.get("models")
        if isinstance(models, list):
            self._available_models = [str(model) for model in models if str(model).strip()]
        if combo is not None:
            index = combo.findData(str(first.get("key") or ""))
            if index >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)
        labels = "、".join(str(item.get("label") or "本地") for item in self._detected_local_providers)
        self.provider_status_label.setText(f"检测到本地模型：{labels}。已填入第一项，保存后生效。")
        self._refresh_model_selector()


    def _refresh_model_selector(self) -> None:
        selector = getattr(self, "model_selector", None)
        if selector is None:
            return
        selector.blockSignals(True)
        try:
            selector.clear()
            config = getattr(self, "provider_config", None)
            configured = self._provider_configured()
            current = str(getattr(config, "model", "") or "") if config is not None else ""
            if configured and current:
                selector.addItem(current, current)
            elif self._pending_model_choice:
                selector.addItem(f"{self._pending_model_choice}（未保存）", "__configure__")
            else:
                selector.addItem("未配置模型", "__configure__")
            for model in self._available_models:
                if model and model != current and selector.findData(model) < 0:
                    selector.addItem(model, model)
            selector.insertSeparator(selector.count())
            selector.addItem("配置模型…", "__configure__")
            selector.setToolTip(
                f"当前模型：{current}\n点击选择模型或打开模型配置" if configured
                else "尚未配置模型；点击打开模型配置"
            )
            selector.setObjectName("ModelSelector" if configured else "ModelSelectorUnconfigured")
            selector.style().unpolish(selector)
            selector.style().polish(selector)
        finally:
            selector.blockSignals(False)


    def _on_model_selector_activated(self, index: int) -> None:
        selector = getattr(self, "model_selector", None)
        if selector is None:
            return
        model = str(selector.itemData(index) or "")
        if not model or model == "__configure__" or model == self._provider_model():
            if model == "__configure__":
                self._open_agent_settings_from_preferences()
            return
        self.provider_model_edit.setText(model)
        self._save_provider_settings()


    def _sync_provider_identity(self) -> None:
        configured = self._provider_configured()
        model = self._provider_model()
        title = getattr(self, "agent_title", None)
        if title is not None:
            title.setText(model if configured else "Coding Agent")
            title.setToolTip(model if configured else "配置模型后开始编码")
        status = getattr(self, "chat_status", None)
        if status is not None:
            status.setText("就绪 · 等待任务" if configured else "未配置模型")
            status.setObjectName("StateReady" if configured else "StateUnconfigured")
            status.setToolTip("点击配置模型接入" if not configured else f"当前模型：{model}")
            status.style().unpolish(status)
            status.style().polish(status)
        label = getattr(self, "connection_label", None)
        if label is not None:
            label.setText(self._provider_state())
            label.setObjectName("StateReady" if configured else "StateWorking")
            label.setToolTip("已配置模型（尚未验证连接）" if configured else "尚未配置 LLM；打开 Agent 设置查看配置方式")
            label.style().unpolish(label)
            label.style().polish(label)


    def _save_provider_settings(self) -> None:
        api_key = self.provider_key_edit.text().strip() or None
        try:
            config = save_provider_config(
                self.project_root,
                base_url=self.provider_base_edit.text(),
                model=self.provider_model_edit.text(),
                api_key=api_key,
                reasoning_effort=str(self.provider_reasoning_combo.currentData() or ""),
                fallback_model=self.provider_fallback_edit.text(),
            )
        except Exception as exc:  # noqa: BLE001 - show validation/credential failures in the page
            self.provider_status_label.setText(f"保存失败：{exc}")
            return
        self.provider_config = config
        self._pending_model_choice = ""
        self._available_models = [config.model, *[
            model for model in self._available_models if model != config.model
        ]]
        self.provider_key_edit.clear()
        self.provider_key_edit.setPlaceholderText("已保存；留空表示不修改")
        key_note = (
            "API Key 已写入 Windows 凭据管理器；下一次任务生效。"
            if config.api_key
            else "本地服务不需要 API Key；下一次任务生效。"
        )
        self.provider_status_label.setText(
            f"已保存：{config.base_url} · {config.model}。{key_note}"
        )
        self._sync_provider_identity()
        self._sync_provider_preset_selection()
        self._refresh_model_selector()


    def _test_provider_connection(self) -> None:
        try:
            config = load_provider_config(self.project_root, require_key=True)
        except Exception as exc:  # noqa: BLE001 - report missing/invalid provider settings
            self.provider_status_label.setText(f"无法测试：{exc}")
            return
        self.provider_test_button.setEnabled(False)
        self.provider_status_label.setText("正在测试连接…")
        thread = ProviderCheckThread(config, self)
        thread.checked.connect(self._on_provider_check_finished)
        thread.finished.connect(thread.deleteLater)
        self._provider_check_thread = thread
        thread.start()


    def _probe_provider_capabilities(self) -> None:
        try:
            config = load_provider_config(self.project_root, require_key=True)
        except Exception as exc:  # noqa: BLE001 - report missing/invalid provider settings
            self.provider_status_label.setText(f"无法探测：{exc}")
            return
        self.provider_test_button.setEnabled(False)
        self.provider_probe_button.setEnabled(False)
        self.provider_status_label.setText("正在探测模型能力（会发送一次极小的工具调用请求）…")
        thread = ProviderCheckThread(config, self, probe=True)
        thread.checked.connect(self._on_provider_check_finished)
        thread.finished.connect(thread.deleteLater)
        self._provider_check_thread = thread
        thread.start()


    def _on_provider_check_finished(self, ok: bool, message: str) -> None:
        self.provider_test_button.setEnabled(True)
        self.provider_probe_button.setEnabled(True)
        prefix = "连接成功：" if ok else "连接失败："
        self.provider_status_label.setText(prefix + message)


    def _refresh_session_list(self) -> None:
        combo = getattr(self, "session_resume_combo", None)
        button = getattr(self, "session_resume_button", None)
        if combo is None:
            return
        selected = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        try:
            sessions = self.ledger.list_sessions(limit=30)
        except Exception:  # noqa: BLE001 - a session list must not break settings
            sessions = []
        for item in sessions:
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                continue
            prompt = " ".join(str(item.get("prompt") or "").split())[:60]
            status = str(item.get("status") or "")
            combo.addItem(f"{prompt or session_id} · {status}", session_id)
        if selected:
            index = combo.findData(selected)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.setEnabled(combo.count() > 0)
        if button is not None:
            button.setEnabled(combo.count() > 0)
        combo.blockSignals(False)


    def _restore_selected_session(self) -> None:
        combo = getattr(self, "session_resume_combo", None)
        if combo is None or not combo.currentData():
            self._append_log("没有可恢复的历史会话")
            return
        session_id = str(combo.currentData())
        try:
            sessions = self.ledger.list_sessions(limit=200)
        except Exception as exc:  # noqa: BLE001 - report a failed session lookup
            self._append_log(f"读取历史会话失败：{exc}")
            return
        record = next(
            (item for item in sessions if str(item.get("session_id") or "") == session_id),
            None,
        )
        if record is None:
            self._append_log("历史会话不存在或已被清理")
            self._refresh_session_list()
            return
        self.current_session_id = session_id
        self._execution_group_active = False
        self._execution_group_session_id = ""
        self._active_execution_group = None
        prompt = " ".join(str(record.get("prompt") or "").split())[:160]
        self._append_chat(
            "系统",
            f"已恢复会话 {session_id}（状态 {record.get('status') or 'unknown'}）。"
            f"下一条消息会继续该会话上下文。\n上次任务：{prompt}",
            "meta",
        )
        self._set_state(self.chat_status, "就绪 · 已恢复会话", "StateReady")
        self._set_state(self.workspace_state, "就绪", "StateReady")
        self.chat_input.setFocus()


    def _restore_last_checkpoint(self) -> None:
        agent = getattr(self, "agent", None)
        if agent is None or getattr(agent, "last_checkpoint", None) is None:
            self._append_log("没有可回滚的任务检查点")
            return
        answer = QMessageBox.question(
            self,
            "回滚上次任务",
            "只恢复 Agent 上次任务写过的文件；任务开始前不存在的文件会被删除。\n"
            "你在任务开始后对这些文件做的修改也会被覆盖。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = agent.restore_last_checkpoint()
        except Exception as exc:  # noqa: BLE001 - report a failed rollback in the UI
            self._append_log(f"回滚失败：{type(exc).__name__}: {exc}")
            return
        restored = len(result.get("restored") or [])
        deleted = len(result.get("deleted") or [])
        failed = result.get("failed") or {}
        message = f"已回滚上次任务：恢复 {restored} 个文件，删除 {deleted} 个新文件"
        if failed:
            message += f"，{len(failed)} 个失败"
        self._append_log(message)
        self.refresh_file_tree()
        self.refresh_git_status()
        self._refresh_open_file_after_task()


    def _persist_summary_settings(self) -> None:
        if not hasattr(self, "summary_enabled_checkbox"):
            return
        timer = getattr(self, "summary_instruction_save_timer", None)
        if timer is not None:
            timer.stop()
        self.summary_settings.enabled = self.summary_enabled_checkbox.isChecked()
        self.summary_settings.model = self.summary_model_edit.text().strip()
        self.summary_settings.max_tokens = self.summary_tokens_spin.value()
        self.summary_settings.context_chars = self.summary_context_spin.value()
        self.summary_settings.retries = self.summary_retry_spin.value()
        self.summary_settings.interval_turns = self.summary_interval_spin.value()
        self.summary_settings.instruction = self.summary_instruction_edit.toPlainText().strip() or SummarySettings.instruction
        self.summary_settings.memory_enabled = self.project_memory_checkbox.isChecked()
        self.summary_settings.memory_results = self.project_memory_results_spin.value()
        self.summary_settings.memory_context_chars = self.project_memory_context_spin.value()
        self.summary_settings.context_window_tokens = self.context_window_spin.value()
        self.summary_settings.execution_mode = ExecutionMode.parse(
            self.execution_mode_combo.currentData()
        ).value
        self.summary_settings.network_policy = NetworkPolicy.parse(
            self.network_policy_combo.currentData()
        ).value
        self.summary_settings.allowed_commands = tuple(
            normalize_command_prefixes(self.allowed_commands_edit.text())
        )
        self.summary_settings.max_agent_turns = self.max_agent_turns_spin.value()
        self.summary_settings.max_task_tokens = self.max_task_tokens_spin.value()
        sandbox_backend = str(self.sandbox_backend_combo.currentData() or "local").strip().lower()
        sandbox_image = self.sandbox_image_edit.text().strip()[:200]
        if sandbox_backend == "docker" and not sandbox_image:
            self._append_log("Docker 沙箱需要填写镜像；设置未保存")
            return
        self.summary_settings.sandbox_backend = sandbox_backend
        self.summary_settings.sandbox_image = sandbox_image
        self.summary_settings.acceptance_mode = SemanticAcceptance.mode(self.acceptance_mode_combo.currentData())
        self.summary_settings.acceptance_model = self.acceptance_model_edit.text().strip()[:160]
        self.summary_settings.multi_agent_enabled = self.multi_agent_enabled_checkbox.isChecked()
        self.summary_settings.multi_agent_model = self.multi_agent_model_edit.text().strip()[:160]
        try:
            self.summary_settings.save(self.project_root)
            self.settings_page.refresh_filter()
            self.agent.summary_settings = self.summary_settings
            self.agent.toolbox.set_execution_mode(self.summary_settings.execution_mode)
            self.agent.toolbox.set_network_policy(self.summary_settings.network_policy)
            self.agent.toolbox.set_allowed_commands(self.summary_settings.allowed_commands)
            try:
                self.agent.toolbox.set_sandbox(
                    self.summary_settings.sandbox_backend,
                    self.summary_settings.sandbox_image,
                )
            except SandboxError as exc:
                self._append_log(f"命令沙箱设置未生效: {exc}")
            self._render_context_meter()
            state = self._summary_status_text()
            compact_state = (
                self._summary_compact_status_text()
                if self._summary_settings_compact
                else state
            )
            self.summary_state_label.setText(compact_state)
            self.summary_state_label.setToolTip(state)
            self.summary_chip.setText(self._summary_chip_text())
            self.summary_chip.setToolTip(state)
            self.summary_chip.setAccessibleName(state)
        except OSError as exc:
            self._append_log(f"总结设置保存失败: {exc}")


    def _on_multi_agent_enabled_changed(self, enabled: bool) -> None:
        self.multi_agent_model_edit.setEnabled(bool(enabled))
        self._persist_summary_settings()


    def _provider_state(self) -> str:
        return f"● {self._provider_model()}" if self._provider_configured() else "○ 配置 LLM"


    def _provider_configured(self) -> bool:
        config = getattr(self, "provider_config", None)
        if config is not None and config.source == "saved":
            if is_local_provider(config.base_url):
                return True
            if config.api_key:
                return True
        base_url = str(getattr(config, "base_url", "") or "")
        if is_local_provider(base_url):
            return True
        return bool(os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY"))


    def _provider_model(self) -> str:
        config = getattr(self, "provider_config", None)
        if config is not None and config.source == "saved" and config.model:
            return config.model
        return os.getenv("SCIDEV_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5"
