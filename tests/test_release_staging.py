from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ReleaseStagingTests(unittest.TestCase):
    def _stage_project(self, project_root: Path, stage: Path) -> None:
        project_resolved = project_root.resolve()
        stage_resolved = Path(stage).resolve()
        if stage_resolved == project_resolved or stage_resolved.is_relative_to(project_resolved):
            self.skipTest(
                "release staging requires a destination outside the source checkout; "
                "this sandbox forces the temporary directory inside it"
            )
        powershell = shutil.which("powershell")
        self.assertIsNotNone(powershell, "Windows PowerShell is required to validate release staging")
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(project_root / "scripts" / "stage_release.ps1"),
                "-Destination",
                str(stage),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_gitignore_excludes_local_model_and_research_artifacts(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        ignored_paths = (
            "Qwen2.5-Coder-7B.gguf",
            "models/model.safetensors",
            "checkpoints/run.pt",
            "datasets/train.parquet",
            "artifacts/embedding.npy",
            ".research/memory.sqlite3",
            ".research/memory.sqlite3-wal",
            "output.bin",
        )
        for relative_path in ignored_paths:
            with self.subTest(path=relative_path):
                result = subprocess.run(
                    ["git", "check-ignore", "--quiet", "--no-index", "--", relative_path],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, f"not ignored: {relative_path}")

        for source_path in ("scidev_core.py", ".env.example"):
            with self.subTest(path=source_path):
                result = subprocess.run(
                    ["git", "check-ignore", "--quiet", "--no-index", "--", source_path],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 1, f"unexpectedly ignored: {source_path}")

    def test_release_stage_contains_runtime_files_and_excludes_local_state(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        expected_files = {
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "pysidedeploy.spec",
            "requirements.txt",
            "requirements-build.txt",
            "scripts/smoke_local_ollama.py",
            "scidev_client.py",
            "scidev_core.py",
            "scidev_process.py",
            "scidev_acceptance.py",
            "scidev_usage.py",
            "scidev_context.py",
            "scidev_environment.py",
            "scidev_patching.py",
            "scidev_memory.py",
            "scidev_multi_agent.py",
            "scidev_planning.py",
            "scidev_policy.py",
            "scidev_verification.py",
            "scidev_browser.py",
            "scidev_icons.py",
            "scidev_ui.py",
            "scidev_preferences.py",
            "scidev_settings.py",
            "scidev_highlighting.py",
            "scidev_visual.py",
            "scripts/bootstrap.py",
            "start_client.bat",
            "start_qwen_local.bat",
            "ui/qml/DevelopmentTree.qml",
            "ui/icons/chevron-down.svg",
            "ui/icons/chevron-up.svg",
            "examples/tetris/README.txt",
            "examples/tetris/index.html",
        }

        with tempfile.TemporaryDirectory(prefix="scidev-release-test-") as temporary:
            stage = Path(temporary) / "stage"
            self._stage_project(project_root, stage)

            staged_files = {
                path.relative_to(stage).as_posix()
                for path in stage.rglob("*")
                if path.is_file()
            }
            self.assertEqual(staged_files, expected_files)
            self.assertFalse((stage / ".venv").exists())
            self.assertFalse((stage / ".research").exists())
            self.assertFalse((stage / ".git").exists())

    def test_tetris_sample_is_a_self_contained_offline_web_page(self) -> None:
        sample = Path(__file__).resolve().parents[1] / "examples" / "tetris"
        html = (sample / "index.html").read_text(encoding="utf-8")
        instructions = (sample / "README.txt").read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn('<canvas id="board"', html)
        self.assertIn("requestAnimationFrame(frame)", html)
        self.assertNotIn("<script src=", html.lower())
        self.assertNotIn("<link rel=\"stylesheet\"", html.lower())
        self.assertNotIn("https://", html.lower())
        self.assertIn("双击 index.html", instructions)

    def test_default_sample_packaging_copies_editable_project_next_to_app(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell, "PowerShell is required to validate default sample packaging")
        source = project_root / "examples" / "tetris"
        expected_files = {"README.txt", "index.html"}

        with tempfile.TemporaryDirectory(prefix="scidev-default-sample-package-") as temporary:
            application_directory = Path(temporary) / "application"
            application_directory.mkdir()
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(project_root / "scripts" / "copy_default_sample.ps1"),
                    "-ApplicationDirectory",
                    str(application_directory),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            copied = application_directory / "examples" / "tetris"
            self.assertEqual({path.name for path in copied.iterdir() if path.is_file()}, expected_files)
            for file_name in expected_files:
                self.assertEqual((copied / file_name).read_bytes(), (source / file_name).read_bytes())

    def test_staged_source_launches_client_and_loads_qml(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="scidev-staged-ui-test-") as temporary:
            temp_root = Path(temporary)
            stage = temp_root / "stage"
            workspace = temp_root / "workspace"
            workspace.mkdir()
            self._stage_project(project_root, stage)

            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment["QT_QUICK_BACKEND"] = "software"
            result = subprocess.run(
                [
                    sys.executable,
                    str(stage / "scidev_client.py"),
                    "--smoke-test",
                    "--workspace",
                    str(workspace),
                ],
                cwd=stage,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_staged_local_model_smoke_cli_starts_without_network_access(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="scidev-staged-ollama-smoke-") as temporary:
            stage = Path(temporary) / "stage"
            self._stage_project(project_root, stage)
            result = subprocess.run(
                [sys.executable, str(stage / "scripts" / "smoke_local_ollama.py"), "--help"],
                cwd=stage,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("never pulls a model", result.stdout)
            self.assertIn("remote hosts", result.stdout)

    def test_deployment_dry_run_uses_the_configured_entrypoint_and_mode(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        scripts_dir = "Scripts" if os.name == "nt" else "bin"
        executable_name = "pyside6-deploy.exe" if os.name == "nt" else "pyside6-deploy"
        deployer = Path(sys.prefix) / scripts_dir / executable_name
        self.assertTrue(deployer.is_file(), "PySide6 deployment entry point is missing from the test environment")
        readme = (project_root / "README.md").read_text(encoding="utf-8")
        self.assertIn("--config-file pysidedeploy.spec --nuitka-version=4.1.1", readme)

        with tempfile.TemporaryDirectory(prefix="scidev-deploy-test-") as temporary:
            stage = Path(temporary) / "stage"
            self._stage_project(project_root, stage)
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join(
                part for part in (str(deployer.parent), environment.get("PATH", "")) if part
            )
            result = subprocess.run(
                [
                    str(deployer),
                    "--dry-run",
                    "--config-file",
                    "pysidedeploy.spec",
                    "--nuitka-version=4.1.1",
                ],
                cwd=stage,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("scidev_client.py", output)
        self.assertIn("--standalone", output)
        self.assertIn("--windows-console-mode=disable", output)
        self.assertIn("Nuitka==4.1.1", output)
        self.assertIn("DevelopmentTree.qml", output)
        self.assertNotIn("main.py", output)
        self.assertNotIn("--onefile", output)
