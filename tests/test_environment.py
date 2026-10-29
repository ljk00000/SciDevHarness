from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scidev_environment import (
    DIAGNOSTIC_TIMEOUT_SECONDS,
    ProjectEnvironmentInspector,
    _DIAGNOSTIC_SCRIPT,
)


def _python_candidate(root: Path, environment: str = ".venv") -> Path:
    executable = (
        root / environment / "Scripts" / "python.exe"
        if os.name == "nt"
        else root / environment / "bin" / "python"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder; process runner is mocked")
    return executable


def _diagnostic_payload() -> dict[str, object]:
    return {
        "python": "3.13.2",
        "implementation": "CPython",
        "pip_check": {"available": True, "returncode": 0, "output": ""},
    }


class ProjectEnvironmentInspectorTests(unittest.TestCase):
    def test_embedded_diagnostic_compiles_without_running(self) -> None:
        compile(_DIAGNOSTIC_SCRIPT, "<scidev-environment-diagnostic>", "exec")
        self.assertIn("runpy.run_module", _DIAGNOSTIC_SCRIPT)
        self.assertIn("site-packages 超出项目虚拟环境范围", _DIAGNOSTIC_SCRIPT)

    def test_no_project_venv_never_falls_back_to_harness_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approval = Mock(return_value=True)
            runner = Mock()
            inspector = ProjectEnvironmentInspector(
                root, approval, process_runner=runner, tool_finder=lambda _name: None,
            )

            result = inspector.inspect()

            self.assertEqual(result["status"], "unavailable")
            self.assertIn("Harness Python", result["report"])
            approval.assert_not_called()
            runner.assert_not_called()

    def test_rejection_makes_no_process_and_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _python_candidate(root)
            approval = Mock(return_value=False)
            runner = Mock()
            events: list[tuple[str, dict[str, object]]] = []
            inspector = ProjectEnvironmentInspector(
                root,
                approval,
                process_runner=runner,
                event_recorder=lambda name, payload: events.append((name, payload)),
                tool_finder=lambda _name: None,
            )

            result = inspector.inspect()

            self.assertEqual(result["status"], "denied")
            self.assertIn("未启动任何进程", result["report"])
            approval.assert_called_once()
            runner.assert_not_called()
            self.assertEqual(events[-1][0], "project_environment_check_not_run")

    def test_approved_diagnostic_uses_only_fixed_argv_without_a_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _python_candidate(root)
            approval = Mock(return_value=True)
            runner = Mock(return_value=SimpleNamespace(
                stdout=json.dumps(_diagnostic_payload()), returncode=0,
            ))
            events: list[tuple[str, dict[str, object]]] = []
            inspector = ProjectEnvironmentInspector(
                root,
                approval,
                process_runner=runner,
                event_recorder=lambda name, payload: events.append((name, payload)),
                tool_finder=lambda _name: None,
            )

            result = inspector.inspect()

            approval.assert_called_once()
            command, timeout = approval.call_args.args
            self.assertIn("pip", command)
            self.assertIn("check", command)
            self.assertEqual(timeout, DIAGNOSTIC_TIMEOUT_SECONDS)
            argv = runner.call_args_list[0].args[0]
            kwargs = runner.call_args_list[0].kwargs
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(argv[1:5], ["-I", "-B", "-S", "-c"])
            self.assertIn("runpy.run_module", argv[5])
            self.assertEqual(Path(argv[6]).resolve(), (root / ".venv").resolve())
            self.assertEqual(Path(kwargs["cwd"]).resolve(), root.resolve())
            self.assertFalse(kwargs["shell"])
            self.assertLessEqual(kwargs["timeout"], DIAGNOSTIC_TIMEOUT_SECONDS)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            if os.name == "nt":
                self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
                self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)
            self.assertNotIn("pip install", argv[5])
            self.assertEqual(result["status"], "passed")
            self.assertIn("pip check 未发现", result["report"])
            self.assertIn("不证明项目声明的依赖齐全或可导入", result["report"])
            self.assertEqual(events[-1][0], "project_environment_check_finished")

    def test_conflicting_dependency_metadata_is_reported_as_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _python_candidate(root)
            payload = _diagnostic_payload()
            payload["pip_check"] = {
                "available": True,
                "returncode": 1,
                "output": "demo 1.0 has requirement numpy<2, but you have numpy 2.1.",
            }
            inspector = ProjectEnvironmentInspector(
                root,
                lambda _command, _timeout: True,
                process_runner=lambda *_args, **_kwargs: SimpleNamespace(
                    stdout=json.dumps(payload), returncode=0,
                ),
                tool_finder=lambda _name: None,
            )

            result = inspector.inspect()

            self.assertEqual(result["status"], "issues")
            self.assertIn("返回 1", result["report"])
            self.assertIn("numpy 2.1", result["report"])

    def test_linked_environment_is_skipped_in_favor_of_next_project_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _python_candidate(root, ".venv")
            _python_candidate(root, "venv")
            inspector = ProjectEnvironmentInspector(root, lambda _command, _timeout: False)
            with patch.object(
                inspector,
                "_is_link_or_junction",
                side_effect=lambda path: Path(path).name == ".venv",
            ):
                selected = inspector._project_interpreter()

            self.assertIsNotNone(selected)
            self.assertEqual(selected[1], "venv/" + (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            ))

    @unittest.skipIf(os.name == "nt", "Windows venv interpreters are regular executables")
    def test_posix_venv_interpreter_symlink_keeps_its_environment_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_root = root / ".venv"
            executable = env_root / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(Path(sys.executable))
            (env_root / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
            inspector = ProjectEnvironmentInspector(root, lambda _command, _timeout: False)

            selected = inspector._project_interpreter()

            self.assertIsNotNone(selected)
            self.assertEqual(selected[0], executable.absolute())
            self.assertNotEqual(selected[0], executable.resolve())

    def test_timeout_and_malformed_output_return_bounded_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _python_candidate(root)
            timeout_inspector = ProjectEnvironmentInspector(
                root,
                lambda _command, _timeout: True,
                process_runner=Mock(side_effect=subprocess.TimeoutExpired("diagnostic", 45)),
                tool_finder=lambda _name: None,
            )
            malformed_inspector = ProjectEnvironmentInspector(
                root,
                lambda _command, _timeout: True,
                process_runner=lambda *_args, **_kwargs: SimpleNamespace(
                    stdout="x" * 40_000, returncode=0,
                ),
                tool_finder=lambda _name: None,
            )

            timed_out = timeout_inspector.inspect()
            malformed = malformed_inspector.inspect()

            self.assertEqual(timed_out["status"], "issues")
            self.assertIn("超时", timed_out["report"])
            self.assertEqual(malformed["status"], "issues")
            self.assertIn("JSONDecodeError", malformed["report"])
            self.assertLessEqual(len(malformed["report"]), 16_000)

    def test_history_recorder_failure_is_kept_for_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def broken_recorder(_event: str, _payload: dict) -> None:
                raise RuntimeError("history unavailable")

            inspector = ProjectEnvironmentInspector(
                root, lambda _command, _timeout: False, event_recorder=broken_recorder,
            )
            inspector._record("environment_probe", {"status": "ok"})
            self.assertIn("RuntimeError", inspector.last_record_error)
            self.assertIn("history unavailable", inspector.last_record_error)

    def test_node_rust_go_and_gpu_probes_are_batched_under_one_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
            (root / "Cargo.toml").write_text('[package]\nname="demo"\n', encoding="utf-8")
            (root / "go.mod").write_text("module example.test/demo\n", encoding="utf-8")
            tools = root / "system tools"
            tools.mkdir()
            names = ("node", "rustc", "cargo", "go", "nvidia-smi", "nvcc")
            tool_paths = {}
            for name in names:
                path = tools / f"{name}.exe"
                path.write_bytes(b"mocked; never launched")
                tool_paths[name] = str(path)
            responses = iter((
                "v22.4.0\n",
                "rustc 1.82.0 (f6e511eec 2024-10-15)\n",
                "cargo 1.82.0 (8f40fc59f 2024-08-21)\n",
                "go version go1.23.1 windows/amd64\n",
                "RTX 4090, 560.1, 24564 MiB\n",
                "nvcc: NVIDIA (R) Cuda compiler driver\nCuda compilation tools, release 12.6, V12.6.85\n",
            ))
            runner = Mock(side_effect=lambda *_args, **_kwargs: SimpleNamespace(
                stdout=next(responses), returncode=0,
            ))
            approval = Mock(return_value=True)
            inspector = ProjectEnvironmentInspector(
                root,
                approval,
                process_runner=runner,
                tool_finder=tool_paths.get,
            )

            result = inspector.inspect()

            approval.assert_called_once()
            plan = approval.call_args.args[0]
            for expected in ("node.exe", "rustc.exe", "cargo.exe", "go.exe", "nvidia-smi.exe", "nvcc.exe"):
                self.assertIn(expected, plan)
            self.assertEqual(runner.call_count, 6)
            for call in runner.call_args_list:
                self.assertFalse(call.kwargs["shell"])
                if os.name == "nt":
                    self.assertEqual(call.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(call.kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)
                self.assertNotIn("-c", call.args[0])
                self.assertLessEqual(call.kwargs["timeout"], 3)
            for call in runner.call_args_list[:4]:
                self.assertIn(call.args[0][-1], {"--version", "version"})
            self.assertIn("--query-gpu=name,driver_version,memory.total", runner.call_args_list[4].args[0])
            self.assertEqual(runner.call_args_list[5].args[0][-1], "--version")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["checks"], [
                "Node.js（node）", "Rust（rustc）", "Rust（cargo）", "Go（go）",
                "NVIDIA GPU / 驱动 / 显存", "CUDA Toolkit",
            ])
            for version in ("v22.4.0", "rustc 1.82.0", "cargo 1.82.0", "go1.23.1", "RTX 4090", "release 12.6"):
                self.assertIn(version, result["report"])

    def test_missing_declared_toolchain_is_reported_without_running_or_approving(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
            approval = Mock(return_value=True)
            runner = Mock()
            inspector = ProjectEnvironmentInspector(
                root, approval, process_runner=runner, tool_finder=lambda _name: None,
            )

            result = inspector.inspect()

            self.assertEqual(result["status"], "issues")
            self.assertIn("package.json", result["report"])
            self.assertIn("未安装或不可执行", result["report"])
            approval.assert_not_called()
            runner.assert_not_called()

    def test_python_manifest_without_project_venv_never_uses_host_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
            approval = Mock(return_value=True)
            runner = Mock()
            inspector = ProjectEnvironmentInspector(
                root, approval, process_runner=runner, tool_finder=lambda _name: None,
            )

            result = inspector.inspect()

            self.assertEqual(result["status"], "issues")
            self.assertIn("Harness Python", result["report"])
            approval.assert_not_called()
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
