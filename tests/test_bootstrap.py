from __future__ import annotations

import io
import re
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from scripts.bootstrap import (
    BootstrapError,
    _ensure_virtual_environment,
    _installed_qt_essentials_version,
    _select_python_command,
    main,
    supports_python_version,
    supports_pyside6_version,
)
from scripts.verify_minimal_qt import validate_minimal_qt_distributions


class BootstrapTests(unittest.TestCase):
    def test_project_license_metadata_and_runtime_license_notice_stay_consistent(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        license_text = (project_root / "LICENSE").read_text(encoding="utf-8")
        readme = (project_root / "README.md").read_text(encoding="utf-8")

        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["license-files"], ["LICENSE"])
        self.assertIn("Apache License", license_text[:200])
        self.assertIn("Version 2.0", license_text[:200])
        self.assertIn("Qt/PySide6", readme)
        self.assertIn("LGPLv3", readme)
        self.assertIn("GPLv3", readme)
        self.assertIn("不含 Qt/PySide6 运行时二进制", readme)

    def test_supported_python_range_matches_project_metadata(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        project_metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project_metadata["project"]["requires-python"], ">=3.12,<3.15")
        expected_ci_versions = [f"3.{minor}" for minor in range(12, 15)]
        workflow = (project_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        matrix = re.search(r"python-version:\s*\[([^\]]+)\]", workflow)
        self.assertIsNotNone(matrix, "CI Python matrix is missing")
        configured_ci_versions = re.findall(r"['\"](\d+\.\d+)['\"]", matrix.group(1))
        self.assertEqual(configured_ci_versions, expected_ci_versions)

        for version in ((3, 12), (3, 13), (3, 14)):
            with self.subTest(version=version):
                self.assertTrue(supports_python_version(version))
        for version in ((3, 11), (3, 15), (4, 0), (), (3,)):
            with self.subTest(version=version):
                self.assertFalse(supports_python_version(version))

    def test_ci_captures_a_second_ui_layout_at_high_dpi_scale(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        workflow = (project_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("Render IDE layouts at 125% DPI scale", workflow)
        self.assertIn('QT_SCALE_FACTOR: "1.25"', workflow)
        self.assertIn("scidev-ui-shots-125dpi", workflow)
        self.assertRegex(workflow, r"(?ms)name: ui-audit-python-.*?path: \|.*?scidev-ui-shots-125dpi")

    def test_github_actions_are_sha_pinned_and_dependabot_tracks_updates(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        workflows_dir = project_root / ".github" / "workflows"
        expected_action_counts = {"ci.yml": 3, "windows-package.yml": 3}
        self.assertEqual(
            {path.name for path in workflows_dir.glob("*.yml")},
            set(expected_action_counts),
            "review and add explicit action pin expectations for every workflow",
        )
        for workflow_name, expected_count in expected_action_counts.items():
            workflow = (workflows_dir / workflow_name).read_text(encoding="utf-8")
            action_refs = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", workflow, flags=re.MULTILINE)
            self.assertTrue(action_refs, f"{workflow_name} should use at least one external GitHub Action")
            self.assertEqual(len(action_refs), expected_count, "update action pin checks when changing a workflow")
            for reference in action_refs:
                with self.subTest(workflow=workflow_name, reference=reference):
                    self.assertRegex(reference, r"^[^@\s]+@[0-9a-f]{40}$")

        dependabot = (project_root / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: pip", dependabot)
        self.assertIn("package-ecosystem: github-actions", dependabot)
        self.assertIn("interval: weekly", dependabot)

    def test_windows_package_workflow_is_manual_and_does_not_publish_binary(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        workflow = (project_root / ".github" / "workflows" / "windows-package.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^on:\s*$")
        self.assertRegex(workflow, r"(?m)^  workflow_dispatch:\s*$")
        self.assertNotRegex(workflow, r"(?m)^  (?:push|pull_request|release):")
        self.assertIn("contents: read", workflow)
        self.assertIn("runs-on: windows-2022", workflow)
        self.assertIn("vcvars64.bat", workflow)
        self.assertIn("--config-file pysidedeploy.spec --nuitka-version=4.1.1", workflow)
        self.assertIn('Filter "scidev_client.exe"', workflow)
        self.assertIn('Filter "DevelopmentTree.qml"', workflow)
        self.assertIn("--smoke-test --workspace", workflow)
        self.assertIn("actions/upload-artifact@", workflow)
        self.assertIn("SciDevHarness-Windows-x64-build-manifest.json", workflow)
        self.assertIn("retention-days: 7", workflow)
        self.assertIn("scripts\\audit_standalone.py", workflow)
        audit_script = (project_root / "scripts" / "audit_standalone.py").read_text(encoding="utf-8")
        self.assertIn('"runtime_binaries"', audit_script)
        self.assertIn('"build_environment_license_metadata"', audit_script)
        self.assertIn('"manual-review-required"', audit_script)
        self.assertNotIn("Compress-Archive", workflow)
        self.assertNotIn("SciDevHarness-Windows-x64.zip", workflow)
        self.assertNotIn("softprops/action-gh-release", workflow)

    def test_standalone_build_pyside6_version_is_explicitly_pinned(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        build_constraints = (project_root / "requirements-build.txt").read_text(encoding="utf-8")
        pins = [line.strip() for line in build_constraints.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(pins, ["PySide6-Essentials==6.11.2"])
        workflow = (project_root / ".github" / "workflows" / "windows-package.yml").read_text(encoding="utf-8")
        self.assertIn("-r requirements.txt -r requirements-build.txt", workflow)
        self.assertIn('--pyside-version "6.11.2"', workflow)
        audit_script = (project_root / "scripts" / "audit_standalone.py").read_text(encoding="utf-8")
        self.assertIn('"pyside6_distribution": "PySide6-Essentials"', audit_script)

    def test_runtime_uses_only_qt_essentials_and_provides_required_tools(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        requirements = (project_root / "requirements.txt").read_text(encoding="utf-8")
        project_metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
        runtime_requirements = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            runtime_requirements,
            ["PySide6-Essentials>=6.11,<7"],
        )
        self.assertEqual(
            project_metadata["project"]["dependencies"],
            ["PySide6-Essentials>=6.11,<7"],
        )

        essentials = metadata.distribution("PySide6_Essentials")
        essentials_dependencies = [
            dependency.casefold().replace("_", "-")
            for dependency in essentials.requires or ()
        ]
        self.assertFalse(any(dependency.startswith("pyside6-addons") for dependency in essentials_dependencies))
        installed_files = [str(path).replace("\\", "/").casefold() for path in essentials.files or ()]
        for module in ("qtcore", "qtgui", "qtwidgets", "qtqml", "qtquick", "qtquickwidgets"):
            with self.subTest(module=module):
                self.assertTrue(
                    any(path.startswith(f"pyside6/{module}.") for path in installed_files),
                    f"PySide6-Essentials is missing required module {module}",
                )
        self.assertTrue(any(path.endswith("/pyside6-deploy.exe") for path in installed_files))
        self.assertTrue(any(path.endswith("/pyside6-qmllint.exe") for path in installed_files))

        for workflow_name in ("ci.yml", "windows-package.yml"):
            with self.subTest(workflow=workflow_name):
                workflow = (
                    project_root / ".github" / "workflows" / workflow_name
                ).read_text(encoding="utf-8")
                self.assertIn("Verify minimal Qt runtime", workflow)
                self.assertIn("scripts\\verify_minimal_qt.py", workflow)

    def test_minimal_qt_gate_rejects_missing_essentials_and_installed_addons(self) -> None:
        validate_minimal_qt_distributions(("PySide6_Essentials", "shiboken6"))
        with self.assertRaisesRegex(RuntimeError, "Essentials is not installed"):
            validate_minimal_qt_distributions(("shiboken6",))
        with self.assertRaisesRegex(RuntimeError, "PySide6-Addons"):
            validate_minimal_qt_distributions(("PySide6-Essentials", "PySide6-Addons"))

    def test_pyside6_requirement_boundaries(self) -> None:
        for version in ("6.11.0", "6.11.2", "6.99.0", "6.12.0.dev1"):
            with self.subTest(version=version):
                self.assertTrue(supports_pyside6_version(version))
        for version in ("6.10.9", "7.0.0", "bad", ""):
            with self.subTest(version=version):
                self.assertFalse(supports_pyside6_version(version))

    def test_launcher_checks_the_essentials_distribution_not_the_metapackage(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python", "-c", ""],
            returncode=0,
            stdout="6.11.2\n",
            stderr="",
        )
        with patch("scripts.bootstrap.subprocess.run", return_value=completed) as run:
            installed_version = _installed_qt_essentials_version(Path("python.exe"))

        self.assertEqual(installed_version, "6.11.2")
        command = run.call_args.args[0]
        self.assertIn("version('PySide6_Essentials')", command[2])

    def test_unsupported_system_python_selects_supported_py_launcher(self) -> None:
        with (
            patch("scripts.bootstrap.sys.version_info", (3, 11, 9)),
            patch("scripts.bootstrap.shutil.which", return_value="py.exe"),
            patch(
                "scripts.bootstrap._query_python_version",
                side_effect=[None, None, (3, 12)],
            ),
        ):
            command, version = _select_python_command()
        self.assertEqual(command, ["py.exe", "-3.12"])
        self.assertEqual(version, (3, 12))

    def test_incomplete_existing_environment_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            interpreter = root / ".venv" / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"keep this existing environment")

            with self.assertRaises(BootstrapError):
                _ensure_virtual_environment(root, create=True, output=lambda _message: None)

            self.assertEqual(interpreter.read_bytes(), b"keep this existing environment")

    def test_runtime_check_does_not_create_missing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            error_output = io.StringIO()
            with patch("scripts.bootstrap.PROJECT_ROOT", root), redirect_stderr(error_output):
                result = main(["--check-runtime"])

            self.assertEqual(result, 1)
            self.assertFalse((root / ".venv").exists())
            self.assertIn("virtual environment is missing", error_output.getvalue())
