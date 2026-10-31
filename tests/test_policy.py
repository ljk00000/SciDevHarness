from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_core import CodingToolbox, EventLedger, SummarySettings
from scidev_policy import (
    ExecutionMode,
    ExecutionPolicy,
    assess_command,
    format_verification_diagnostics,
    parse_verification_diagnostics,
)


class ExecutionPolicyTests(unittest.TestCase):
    def test_mode_aliases_and_invalid_values_fall_back_safely(self) -> None:
        self.assertIs(ExecutionMode.parse("read-only"), ExecutionMode.READ_ONLY)
        self.assertIs(ExecutionMode.parse("READONLY"), ExecutionMode.READ_ONLY)
        self.assertIs(ExecutionMode.parse("workspace_write"), ExecutionMode.WORKSPACE_WRITE)
        self.assertIs(ExecutionMode.parse("danger-full-access"), ExecutionMode.FULL_ACCESS)
        self.assertIs(ExecutionMode.parse("nonsense"), ExecutionMode.WORKSPACE_WRITE)
        self.assertIs(
            ExecutionMode.parse(None, ExecutionMode.READ_ONLY), ExecutionMode.READ_ONLY,
        )

    def test_common_verification_commands_are_auto_approvable(self) -> None:
        for command in (
            "pytest -q",
            "python -m pytest tests -q",
            "python -m unittest discover",
            "npm test",
            "npm run test:unit",
            "pnpm test",
            "cargo test --quiet",
            "go test ./...",
            "dotnet test",
            "ruff check src",
            "python -m ruff check .",
            "npx eslint src",
            "mypy src",
            "npx tsc --noEmit",
            "cargo clippy",
        ):
            with self.subTest(command=command):
                assessment = assess_command(command)
                self.assertTrue(assessment.auto_approvable, assessment.reason)
                self.assertIn(assessment.category, {"test", "lint", "typecheck"})

    def test_shell_composition_network_and_path_escape_are_not_auto_approved(self) -> None:
        for command in (
            "pytest -q && rm -rf build",
            "pytest -q | tee out.txt",
            "pytest -q > out.txt",
            "pytest $(whoami)",
            "pytest ../other-tests",
            "pytest C:\\outside",
            "curl https://example.com",
            "python -m pytest -q; echo done",
            "FOO=bar pytest -q",
        ):
            with self.subTest(command=command):
                self.assertFalse(assess_command(command).auto_approvable)

    def test_modes_gate_writes_and_commands(self) -> None:
        read_only = ExecutionPolicy(ExecutionMode.READ_ONLY)
        self.assertFalse(read_only.can_write_files)
        self.assertFalse(read_only.can_run_commands)
        self.assertFalse(read_only.auto_approves(assess_command("pytest -q")))

        workspace = ExecutionPolicy(ExecutionMode.WORKSPACE_WRITE)
        self.assertTrue(workspace.can_write_files)
        self.assertTrue(workspace.can_run_commands)
        self.assertTrue(workspace.auto_approves(assess_command("pytest -q")))
        self.assertFalse(workspace.auto_approves(assess_command("echo hi")))

        manual = ExecutionPolicy(ExecutionMode.MANUAL)
        self.assertTrue(manual.can_write_files)
        self.assertTrue(manual.can_run_commands)
        self.assertFalse(manual.auto_approves(assess_command("pytest -q")))
        self.assertFalse(manual.auto_approves(assess_command("echo hi")))

        full = ExecutionPolicy(ExecutionMode.FULL_ACCESS)
        self.assertTrue(full.auto_approves(assess_command("echo hi")))
        self.assertTrue(full.auto_approves(assess_command("pytest -q && echo done")))


class VerificationDiagnosticsTests(unittest.TestCase):
    def test_pytest_ruff_mypy_and_tsc_failures_are_structured(self) -> None:
        pytest_output = "\n".join([
            "FAILED tests/test_a.py::test_add - AssertionError: expected 3",
            "tests/test_b.py:12: KeyError: 'missing'",
            "2 failed, 4 passed",
        ])
        pytest_diagnostics = parse_verification_diagnostics("test", pytest_output)
        self.assertEqual(len(pytest_diagnostics), 2)
        self.assertEqual(pytest_diagnostics[0].file, "tests/test_a.py")
        self.assertIn("test_add", pytest_diagnostics[0].message)
        self.assertEqual(pytest_diagnostics[1].line, 12)

        lint_output = "src/app.py:3:5: E501 Line too long (100 > 88)\n"
        lint_diagnostics = parse_verification_diagnostics("lint", lint_output)
        self.assertEqual(len(lint_diagnostics), 1)
        self.assertEqual((lint_diagnostics[0].line, lint_diagnostics[0].column), (3, 5))
        self.assertEqual(lint_diagnostics[0].code, "E501")

        mypy_output = "src/app.py:7: error: Incompatible return value type\n"
        mypy_diagnostics = parse_verification_diagnostics("typecheck", mypy_output)
        self.assertEqual(len(mypy_diagnostics), 1)
        self.assertEqual(mypy_diagnostics[0].line, 7)
        self.assertIn("Incompatible", mypy_diagnostics[0].message)

        tsc_output = "src/app.ts(9,4): error TS2322: Type 'string' is not assignable\n"
        tsc_diagnostics = parse_verification_diagnostics("typecheck", tsc_output)
        self.assertEqual(len(tsc_diagnostics), 1)
        self.assertEqual((tsc_diagnostics[0].line, tsc_diagnostics[0].column), (9, 4))
        self.assertEqual(tsc_diagnostics[0].code, "TS2322")

    def test_duplicates_are_removed_and_limit_is_enforced(self) -> None:
        output = "\n".join([f"src/app.py:{line}:1: E501 too long" for line in range(1, 8)])
        diagnostics = parse_verification_diagnostics("lint", output, limit=3)
        self.assertEqual(len(diagnostics), 3)
        self.assertEqual([item.line for item in diagnostics], [1, 2, 3])

        duplicate = "src/app.py:3:5: E501 too long\nsrc/app.py:3:5: E501 too long\n"
        self.assertEqual(len(parse_verification_diagnostics("lint", duplicate)), 1)

    def test_non_verification_output_and_empty_results_are_ignored(self) -> None:
        self.assertEqual(format_verification_diagnostics("other", "src/app.py:3:1: E501 x"), "")
        self.assertEqual(format_verification_diagnostics("lint", "all checks passed"), "")
        self.assertEqual(parse_verification_diagnostics("test", ""), [])


class ToolboxExecutionPolicyTests(unittest.TestCase):
    def _toolbox(self, root: Path, mode: ExecutionMode, approval=None) -> CodingToolbox:
        return CodingToolbox(
            root,
            EventLedger(root),
            command_approval=approval,
            execution_mode=mode,
        )

    def test_read_only_rejects_writes_and_commands_before_execution(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            toolbox = self._toolbox(root, ExecutionMode.READ_ONLY)
            with self.assertRaisesRegex(Exception, "只读"):
                toolbox.write_file("a.txt", "hello")
            with self.assertRaisesRegex(Exception, "只读"):
                toolbox.replace_in_file("a.txt", "hello", "bye")
            with self.assertRaisesRegex(Exception, "只读"):
                toolbox.apply_patch("*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch\n")
            with patch("scidev_core.subprocess.Popen") as process:
                with self.assertRaisesRegex(Exception, "只读"):
                    toolbox.run_command("pytest -q")
                process.assert_not_called()

    def test_workspace_write_auto_approves_verification_and_adds_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            calls = []
            toolbox = self._toolbox(
                root,
                ExecutionMode.WORKSPACE_WRITE,
                approval=lambda command, timeout: calls.append((command, timeout)) or True,
            )

            class FakeProcess:
                returncode = 1

                def communicate(self, timeout=None):
                    return b"FAILED tests/test_a.py::test_x - AssertionError: boom\n", None

            with patch("scidev_core.subprocess.Popen", return_value=FakeProcess()):
                result = toolbox.run_command("pytest -q")
            self.assertEqual(calls, [])
            self.assertIn("结构化测试失败", result)
            self.assertIn("tests/test_a.py", result)
            self.assertEqual(toolbox.last_command_execution["approval_source"], "policy")
            self.assertEqual(toolbox.last_command_execution["command_category"], "test")
            events = [
                json.loads(line)["event_type"]
                for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("command_auto_approved", events)
            self.assertNotIn("command_approval_requested", events)

    def test_workspace_write_still_requires_approval_for_other_commands(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            calls = []
            toolbox = self._toolbox(
                root,
                ExecutionMode.WORKSPACE_WRITE,
                approval=lambda command, timeout: calls.append(command) or False,
            )
            with patch("scidev_core.subprocess.Popen") as process:
                with self.assertRaisesRegex(Exception, "命令未执行"):
                    toolbox.run_command("echo hi")
                process.assert_not_called()
            self.assertEqual(calls, ["echo hi"])
            self.assertEqual(toolbox.last_command_execution["approval_source"], "user")

    def test_full_access_auto_approves_arbitrary_commands(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            toolbox = self._toolbox(root, ExecutionMode.FULL_ACCESS)

            class FakeProcess:
                returncode = 0

                def communicate(self, timeout=None):
                    return b"ok\n", None

            with patch("scidev_core.subprocess.Popen", return_value=FakeProcess()) as process:
                result = toolbox.run_command("echo hi")
            process.assert_called_once()
            self.assertIn("执行状态：passed", result)
            self.assertEqual(toolbox.last_command_execution["approval_source"], "policy")

    def test_execution_mode_persists_in_project_settings(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            settings = SummarySettings(enabled=False, execution_mode=ExecutionMode.READ_ONLY.value)
            settings.save(root)
            loaded = SummarySettings.load(root)
            self.assertEqual(loaded.execution_mode, ExecutionMode.READ_ONLY.value)

            with patch.dict("os.environ", {"SCIDEV_EXECUTION_MODE": "full-access"}):
                self.assertEqual(SummarySettings.load(root).execution_mode, ExecutionMode.FULL_ACCESS.value)


if __name__ == "__main__":
    unittest.main()
