from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from scidev_core import CodingToolbox, EventLedger, PermanentError, SummarySettings
from scidev_sandbox import DockerSandbox, SandboxError


class SandboxSettingsTests(unittest.TestCase):
    def test_settings_round_trip_and_unknown_backend_falls_back_to_local(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-sandbox-settings-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            settings = SummarySettings(enabled=False, sandbox_backend="docker", sandbox_image="tools:1")
            settings.save(root)
            with patch.dict(
                "os.environ", {"SCIDEV_SANDBOX_BACKEND": "", "SCIDEV_SANDBOX_IMAGE": ""}, clear=False,
            ):
                loaded = SummarySettings.load(root)
                self.assertEqual(loaded.sandbox_backend, "docker")
                self.assertEqual(loaded.sandbox_image, "tools:1")
                (root / ".research" / "settings.json").write_text(
                    '{"sandbox_backend": "firecracker", "sandbox_image": "x"}', encoding="utf-8",
                )
                self.assertEqual(SummarySettings.load(root).sandbox_backend, "local")


class DockerSandboxTests(unittest.TestCase):
    def test_local_backend_is_unchanged(self) -> None:
        with patch.dict("os.environ", {"SCIDEV_SANDBOX_BACKEND": "local"}, clear=False):
            self.assertIsNone(DockerSandbox.from_environment())

    def test_docker_requires_an_image_and_known_backend(self) -> None:
        with self.assertRaises(SandboxError):
            DockerSandbox.from_environment(backend="docker", image="")
        with self.assertRaises(SandboxError):
            DockerSandbox.from_environment(backend="firecracker", image="img")
        with self.assertRaises(SandboxError):
            DockerSandbox(image="")

    def test_wrap_adds_filesystem_and_network_boundaries(self) -> None:
        sandbox = DockerSandbox(image="scidev-tools:1", docker_path="docker", checker=lambda: "24.0")
        workspace = Path("C:/work")
        argv = sandbox.wrap("python -m pytest -q", workspace)
        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("--cap-drop", argv)
        self.assertIn("ALL", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertIn("--pids-limit", argv)
        self.assertIn("--memory", argv)
        self.assertIn("--name", argv)
        self.assertIn("scidev-", argv[argv.index("--name") + 1])
        self.assertIn(f"{workspace}:/workspace:rw", argv)
        self.assertEqual(argv[-4:], ["scidev-tools:1", "sh", "-lc", "python -m pytest -q"])

    def test_cleanup_force_removes_only_the_started_container(self) -> None:
        calls: list[list[str]] = []
        sandbox = DockerSandbox(
            image="scidev-tools:1",
            docker_path="docker",
            checker=lambda: "24.0",
            cleanup_runner=calls.append,
        )
        argv = sandbox.wrap("pytest", Path("C:/work"))
        name = argv[argv.index("--name") + 1]
        sandbox.cleanup()
        self.assertEqual(calls, [["docker", "rm", "-f", name]])
        sandbox.cleanup()
        self.assertEqual(len(calls), 1)

    def test_missing_daemon_raises_instead_of_falling_back(self) -> None:
        sandbox = DockerSandbox(image="img", docker_path="docker", checker=lambda: (_ for _ in ()).throw(SandboxError("no daemon")))
        with self.assertRaises(SandboxError):
            sandbox.wrap("pytest", Path("."))

    def test_toolbox_runs_the_command_inside_docker(self) -> None:
        class Process:
            returncode = 0
            pid = 123

            def communicate(self, timeout=None):
                return b"inside-container\n", None

        with tempfile.TemporaryDirectory(prefix="scidev-sandbox-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            toolbox = CodingToolbox(
                root,
                EventLedger(root),
                command_approval=lambda _command, _timeout: True,
                sandbox_backend="docker",
                sandbox_image="scidev-tools:1",
            )
            assert toolbox.sandbox is not None
            toolbox.sandbox.docker_path = "docker"
            toolbox.sandbox.checker = lambda: "24.0"
            with patch("scidev_core.subprocess.Popen", return_value=Process()) as popen:
                result = toolbox.run_command("python -V")
            argv = popen.call_args.args[0]
            self.assertEqual(argv[0], "docker")
            self.assertEqual(argv[1], "run")
            self.assertIn("scidev-tools:1", argv)
            self.assertEqual(argv[-3:], ["sh", "-lc", "python -V"])
            self.assertEqual(argv[-4], "scidev-tools:1")
            self.assertIn("inside-container", result)
            self.assertNotIn("shell", popen.call_args.kwargs)

    def test_bridge_network_requires_allow_policy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-sandbox-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            with patch.dict("os.environ", {"SCIDEV_SANDBOX_NETWORK": "bridge"}, clear=False):
                toolbox = CodingToolbox(
                    root,
                    EventLedger(root),
                    command_approval=lambda _command, _timeout: True,
                    sandbox_backend="docker",
                    sandbox_image="scidev-tools:1",
                )
            assert toolbox.sandbox is not None
            self.assertEqual(toolbox.sandbox.network, "bridge")
            with self.assertRaisesRegex(PermanentError, "网络策略不是 allow"):
                toolbox.run_command("python -V")

    def test_unavailable_docker_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-sandbox-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            toolbox = CodingToolbox(
                root,
                EventLedger(root),
                command_approval=lambda _command, _timeout: True,
                sandbox_backend="docker",
                sandbox_image="scidev-tools:1",
            )
            assert toolbox.sandbox is not None
            toolbox.sandbox.docker_path = "docker"
            toolbox.sandbox.checker = lambda: (_ for _ in ()).throw(SandboxError("daemon down"))
            with self.assertRaisesRegex(PermanentError, "沙箱不可用"):
                toolbox.run_command("python -V")


if __name__ == "__main__":
    unittest.main()
