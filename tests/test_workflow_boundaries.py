"""Composition and import boundaries of the desktop and Agent workflows."""
from pathlib import Path
import subprocess
import sys
import unittest


class WorkflowBoundaryTests(unittest.TestCase):
    def test_package_entrypoints_and_launcher_preflight(self) -> None:
        root = Path(__file__).resolve().parents[1]
        commands = (
            (['-m', 'scidev', '--help'], 'usage:'),
            (['-m', 'scidev.cli', '--help'], 'usage:'),
            (['scripts/bootstrap.py', '--check-runtime'], 'runtime check: OK'),
        )
        for arguments, expected in commands:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, '-B', *arguments], cwd=root,
                    capture_output=True, text=True, encoding='utf-8', timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)

    def run_isolated(self, source: str) -> None:
        result = subprocess.run(
            [sys.executable, '-B', '-c', source],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workflows_import_without_entry_points_or_external_actions(self) -> None:
        self.run_isolated('''
import importlib
import socket
import subprocess
import sys
from unittest.mock import patch
modules = (
    'scidev.desktop.window_builders', 'scidev.desktop.window_layout', 'scidev.desktop.agent_panel',
    'scidev.desktop.agent_settings', 'scidev.desktop.git_workspace', 'scidev.desktop.explorer_workspace',
    'scidev.desktop.terminal_workspace', 'scidev.desktop.chat_workflow', 'scidev.agent.completion',
    'scidev.agent.summary', 'scidev.agent.tools', 'scidev.agent.run',
)
with patch.object(socket, 'create_connection', side_effect=AssertionError('network on import')), \
     patch.object(subprocess, 'Popen', side_effect=AssertionError('process on import')):
    for name in modules:
        importlib.import_module(name)
assert not {'scidev.agent.core', 'scidev.desktop.client'} & sys.modules.keys()
''')

    def test_hosts_inherit_original_descriptors_without_new_state_owners(self) -> None:
        self.run_isolated('''
import importlib
import inspect
from PySide6.QtCore import QObject
from scidev.desktop.client import ClientWindow
from scidev.agent.core import CodingAgent
groups = (
    (ClientWindow, 'scidev.desktop.window_builders', 'WindowBuilderMixin'),
    (ClientWindow, 'scidev.desktop.window_layout', 'WindowLayoutMixin'),
    (ClientWindow, 'scidev.desktop.agent_panel', 'AgentPanelMixin'),
    (ClientWindow, 'scidev.desktop.agent_settings', 'AgentSettingsMixin'),
    (ClientWindow, 'scidev.desktop.git_workspace', 'GitWorkspaceMixin'),
    (ClientWindow, 'scidev.desktop.explorer_workspace', 'ExplorerWorkspaceMixin'),
    (ClientWindow, 'scidev.desktop.terminal_workspace', 'TerminalWorkspaceMixin'),
    (ClientWindow, 'scidev.desktop.chat_workflow', 'ChatWorkflowMixin'),
    (CodingAgent, 'scidev.agent.completion', 'AgentCompletionMixin'),
    (CodingAgent, 'scidev.agent.summary', 'AgentSummaryMixin'),
    (CodingAgent, 'scidev.agent.tools', 'AgentToolsMixin'),
    (CodingAgent, 'scidev.agent.run', 'AgentRunMixin'),
)
for host, module, name in groups:
    mixin = getattr(importlib.import_module(module), name)
    assert issubclass(host, mixin), name
    assert not issubclass(mixin, QObject), name
    assert '__init__' not in mixin.__dict__, name
    for method, descriptor in mixin.__dict__.items():
        if inspect.isfunction(descriptor) or isinstance(descriptor, (staticmethod, classmethod)):
            assert inspect.getattr_static(host, method) is descriptor, (name, method)
''')

    def test_client_keeps_widget_and_probe_class_exports(self) -> None:
        self.run_isolated('''
import scidev.desktop.client as client
from scidev.desktop.agent_panel import PromptEdit
from scidev.desktop.agent_settings import LocalProviderCheckThread, ProviderCheckThread
assert client.PromptEdit is PromptEdit
assert client.LocalProviderCheckThread is LocalProviderCheckThread
assert client.ProviderCheckThread is ProviderCheckThread
''')


if __name__ == '__main__':
    unittest.main()
