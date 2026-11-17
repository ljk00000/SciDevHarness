"""Compatibility and dependency boundaries for the extracted project services."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

import scidev.agent.core
import scidev.workspace.patching
import scidev.agent.project_settings
import scidev.integrations.svg
import scidev.agent.toolbox


class ExtractedModuleBoundaryTests(unittest.TestCase):
    def _run_isolated(self, script: str) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_core_legacy_exports_keep_object_identity(self) -> None:
        for name, module in (
            ("SummarySettings", scidev.agent.project_settings),
            ("SvgArtifactAdapter", scidev.integrations.svg),
            ("CodingToolbox", scidev.agent.toolbox),
            ("atomic_write_bytes", scidev.agent.toolbox),
            ("decode_process_output", scidev.agent.toolbox),
            ("destructive_command_reason", scidev.agent.toolbox),
            ("manual_git_mutation_reason", scidev.agent.toolbox),
            ("PatchError", scidev.workspace.patching),
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(scidev.agent.core, name), getattr(module, name))

    def test_project_settings_import_is_independent_of_agent_model_and_qt(self) -> None:
        self._run_isolated("""
import sys
import scidev.agent.project_settings
unexpected = [name for name in sys.modules
              if name in {'scidev.agent.core', 'scidev.desktop.client', 'scidev.integrations.model'}
              or name.startswith('PySide6')]
assert not unexpected, unexpected
assert scidev.agent.project_settings.SummarySettings().max_tokens == 12000
""")

    def test_toolbox_and_svg_import_without_agent_or_external_actions(self) -> None:
        self._run_isolated("""
import socket
import subprocess
import sys
from unittest.mock import patch
with patch.object(socket, 'create_connection', side_effect=AssertionError('network on import')), \
     patch.object(subprocess, 'Popen', side_effect=AssertionError('process on import')):
    import scidev.agent.toolbox
    import scidev.integrations.svg
assert 'scidev.agent.core' not in sys.modules
assert 'scidev.desktop.client' not in sys.modules
assert scidev.integrations.svg.CodingToolbox is scidev.agent.toolbox.CodingToolbox
assert scidev.agent.toolbox.destructive_command_reason('git reset --hard')
assert scidev.agent.toolbox.manual_git_mutation_reason('git add -A')
""")

    def test_client_legacy_exports_keep_shared_helpers(self) -> None:
        self._run_isolated("""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_QUICK_BACKEND'] = 'software'
import scidev.desktop.client
import scidev.runtime.paths
import scidev.runtime.process
import scidev.desktop.theme
assert scidev.desktop.client.THEME is scidev.desktop.theme.THEME
assert scidev.desktop.client.TerminalOutputDecoder is scidev.runtime.process.TerminalOutputDecoder
for name in ('resolve_initial_workspace', 'default_sample_workspace', 'resolve_startup_workspace'):
    assert getattr(scidev.desktop.client, name) is getattr(scidev.runtime.paths, name), name
""")

    def test_editor_workspace_import_does_not_load_agent_or_client(self) -> None:
        self._run_isolated("""
import sys
import scidev.desktop.editor_workspace
from PySide6.QtCore import QObject
assert not {'scidev.agent.core', 'scidev.desktop.client', 'scidev.integrations.model'} & sys.modules.keys()
assert not issubclass(scidev.desktop.editor_workspace.EditorWorkspaceMixin, QObject)
assert '__init__' not in scidev.desktop.editor_workspace.EditorWorkspaceMixin.__dict__
""")

    def test_client_keeps_the_editor_method_descriptors(self) -> None:
        self._run_isolated("""
import inspect
import scidev.desktop.client
from scidev.desktop.editor_workspace import EditorWorkspaceMixin
assert issubclass(scidev.desktop.client.ClientWindow, EditorWorkspaceMixin)
count = 0
for name, descriptor in EditorWorkspaceMixin.__dict__.items():
    if inspect.isfunction(descriptor) or isinstance(descriptor, (staticmethod, classmethod)):
        assert inspect.getattr_static(scidev.desktop.client.ClientWindow, name) is descriptor, name
        count += 1
assert count > 0
""")



if __name__ == "__main__":
    unittest.main()
