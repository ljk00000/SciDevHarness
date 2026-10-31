from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_core import CodingAgent, EventLedger, GitManager, PermanentError, SummarySettings
from scidev_verification import TestCommandDiscovery, VerificationTracker


class VerificationTrackerTests(unittest.TestCase):
    def test_classifies_common_checks_including_windows_virtualenv_commands(self) -> None:
        cases = {
            "python -V": "command",
            r".\.venv\Scripts\python.exe -m unittest discover -s tests": "test",
            r'& "C:\Python 3.14\python.exe" -m pytest -q': "test",
            "npm run test": "test",
            "cargo test": "test",
            "ruff check .": "lint",
            "pyright src": "typecheck",
            r"C:\Python\python.exe -m py_compile module.py": "compile",
            "git diff --check": "check",
            "echo verification": "command",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(VerificationTracker.classify(command), expected)
        self.assertTrue(VerificationTracker.requires_check(["src/module.PY"]))
        self.assertFalse(VerificationTracker.requires_check(["README.md", "notes.txt"]))

    def test_fingerprint_tracks_content_and_deletion_but_bounds_unsafe_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("value = 1\n", encoding="utf-8")

            def fingerprint(paths, complete=True):
                return VerificationTracker.fingerprint(
                    root, paths, lambda value: value == ".env", scope_complete=complete,
                )

            first = fingerprint(["module.py"])
            same = fingerprint(["module.py"])
            self.assertEqual(first["fingerprint"], same["fingerprint"])
            source.write_text("value = 2\n", encoding="utf-8")
            changed = fingerprint(["module.py"])
            self.assertNotEqual(first["fingerprint"], changed["fingerprint"])
            source.unlink()
            deleted = fingerprint(["module.py"])
            self.assertNotEqual(changed["fingerprint"], deleted["fingerprint"])

            limited = fingerprint(["../outside.py", ".env"], complete=True)
            self.assertFalse(limited["complete"])
            incomplete_repo = fingerprint([], complete=False)
            self.assertFalse(incomplete_repo["complete"])

    def test_fingerprint_respects_file_and_byte_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.py").write_text("a", encoding="utf-8")
            (root / "b.py").write_text("b", encoding="utf-8")
            with patch.object(VerificationTracker, "MAX_FINGERPRINT_FILES", 1):
                limited_files = VerificationTracker.fingerprint(
                    root, ["a.py", "b.py"], lambda _path: False, scope_complete=True,
                )
            self.assertFalse(limited_files["complete"])
            self.assertEqual(limited_files["file_count"], 1)
            with patch.object(VerificationTracker, "MAX_FINGERPRINT_TOTAL_BYTES", 1):
                limited_bytes = VerificationTracker.fingerprint(
                    root, ["a.py", "b.py"], lambda _path: False, scope_complete=True,
                )
            self.assertFalse(limited_bytes["complete"])

    def test_symlink_is_not_followed_or_attested(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "outside.py"
            outside.write_text("secret = True", encoding="utf-8")
            try:
                os.symlink(outside, root / "linked.py")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            result = VerificationTracker.fingerprint(
                root, ["linked.py"], lambda _path: False, scope_complete=True,
            )
            self.assertFalse(result["complete"])
            self.assertEqual(result["file_count"], 0)

    def test_report_distinguishes_pass_failure_staleness_scope_and_not_run(self) -> None:
        before = {"fingerprint": "same", "complete": True}
        execution = {
            "command_id": "cmd_1", "command": "pytest", "status": "passed", "exit_code": 0,
        }
        passed_record = VerificationTracker.record(execution, before, before, edit_revision=2, turn=3)
        passed = VerificationTracker.summarize([passed_record], before, edit_revision=2)
        self.assertEqual(passed["status"], "passed")

        changed = {"fingerprint": "changed", "complete": True}
        stale = VerificationTracker.summarize([passed_record], changed, edit_revision=2)
        self.assertEqual(stale["status"], "stale")

        failed_record = VerificationTracker.record(
            {**execution, "status": "failed", "exit_code": 1}, before, before,
            edit_revision=2, turn=4,
        )
        self.assertEqual(VerificationTracker.summarize([failed_record], before, edit_revision=2)["status"], "failed")

        limited = {"fingerprint": "same", "complete": False}
        limited_record = VerificationTracker.record(execution, limited, limited, edit_revision=2, turn=5)
        self.assertEqual(
            VerificationTracker.summarize([limited_record], limited, edit_revision=2)["status"],
            "scope_limited",
        )
        self.assertEqual(VerificationTracker.summarize([], before, edit_revision=0)["status"], "not_run")


class TestCommandDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _write_files(root: Path, files: dict[str, str]) -> None:
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_python_pytest_and_unittest_candidates_are_evidence_based_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["pytest>=8"]\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
                encoding="utf-8",
            )
            source = root / "tests" / "test_service.py"
            source.write_text(
                "import pytest\nimport unittest\nclass ServiceTests(unittest.TestCase):\n    def test_ok(self): pass\n",
                encoding="utf-8",
            )
            with (
                patch("subprocess.run", side_effect=AssertionError("discovery must not run commands")),
                patch("subprocess.Popen", side_effect=AssertionError("discovery must not run commands")),
            ):
                result = TestCommandDiscovery.discover(
                    root, ["pyproject.toml", "tests/test_service.py"],
                )

            commands = {item["name"]: item["command"] for item in result["candidates"]}
            self.assertEqual(commands["Python · pytest"], "python -m pytest")
            self.assertEqual(commands["Python · unittest (tests)"], "python -m unittest discover -s tests -v")
            self.assertEqual(result["test_file_count"], 1)
            summary = TestCommandDiscovery.format(result)
            self.assertIn("逐条用户批准", summary)
            self.assertNotIn(str(root), summary)

    def test_plain_pytest_function_is_detected_without_importing_pytest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "tests" / "test_math.py").write_text("def test_addition():\n    assert 1 + 1 == 2\n", encoding="utf-8")
            result = TestCommandDiscovery.discover(root, ["tests/test_math.py"])
            self.assertEqual([item["command"] for item in result["candidates"]], ["python -m pytest"])
            self.assertIn("未实际验证", TestCommandDiscovery.format(result))

    def test_unittest_methods_are_not_misclassified_as_pytest_functions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            test_file = root / "tests" / "test_agent.py"
            test_file.write_text(
                "import unittest\nclass TestAgent(unittest.TestCase):\n"
                "    def test_run(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            result = TestCommandDiscovery.discover(root, ["tests/test_agent.py"])
            self.assertEqual([item["name"] for item in result["candidates"]], ["Python · unittest (tests)"])

    def test_unittest_roots_are_reported_separately_instead_of_omitting_nested_suites(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "tests/test_core.py": "import unittest\nclass Core(unittest.TestCase): pass\n",
                "examples/tetris/test_engine.py": "import unittest\nclass Engine(unittest.TestCase): pass\n",
            }
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            result = TestCommandDiscovery.discover(root, files)
            commands = {item["name"]: item["command"] for item in result["candidates"]}
            self.assertEqual(commands["Python · unittest (tests)"], "python -m unittest discover -s tests -v")
            self.assertEqual(
                commands["Python · unittest (examples/tetris)"],
                "python -m unittest discover -s examples/tetris -v",
            )

    def test_python_candidates_prefer_an_existing_project_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "tests" / "test_math.py").write_text("def test_add(): pass\n", encoding="utf-8")
            if os.name == "nt":
                interpreter = root / ".venv" / "Scripts" / "python.exe"
                expected_prefix = r".\.venv\Scripts\python.exe"
            else:
                interpreter = root / ".venv" / "bin" / "python"
                expected_prefix = "./.venv/bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"placeholder; discovery never executes it")
            result = TestCommandDiscovery.discover(root, ["tests/test_math.py"])
            self.assertEqual(result["candidates"][0]["command"], f"{expected_prefix} -m pytest")

    def test_python_candidates_do_not_follow_a_symlinked_project_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_math.py").write_text("def test_add(): pass\n", encoding="utf-8")
            outside = Path(temp) / "python.exe"
            outside.write_bytes(b"external executable marker")
            if os.name == "nt":
                interpreter = root / ".venv" / "Scripts" / "python.exe"
            else:
                interpreter = root / ".venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            try:
                os.symlink(outside, interpreter)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            result = TestCommandDiscovery.discover(root, ["tests/test_math.py"])
            self.assertEqual(result["candidates"][0]["command"], "python -m pytest")

    def test_node_script_selects_lockfile_manager_without_echoing_script_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                '{"scripts":{"test":"node -e \\\"do-not-echo-secret\\\""}}', encoding="utf-8",
            )
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            result = TestCommandDiscovery.discover(root, ["package.json", "pnpm-lock.yaml"])
            self.assertEqual(result["candidates"][0]["command"], "pnpm test")
            self.assertNotIn("do-not-echo-secret", TestCommandDiscovery.format(result))

    def test_go_rust_maven_gradle_and_dotnet_commands_require_test_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "go.mod": "module example.test/app\n",
                "pkg/app_test.go": "package pkg\nfunc TestApp(t *testing.T) {}\n",
                "Cargo.toml": "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n",
                "src/lib.rs": "#[cfg(test)]\nmod tests { #[test] fn works() {} }\n",
                "pom.xml": "<project/>",
                "src/test/java/demo/AppTest.java": "class AppTest {}\n",
                "build.gradle.kts": "plugins {}\n",
                "src/test/kotlin/demo/AppTests.kt": "class AppTests\n",
                "Demo.sln": "Microsoft Visual Studio Solution File\n",
                "tests/Demo.Tests/Demo.Tests.csproj": "<Project />\n",
            }
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            result = TestCommandDiscovery.discover(root, list(files))
            commands = {item["name"]: item["command"] for item in result["candidates"]}
            self.assertEqual(commands["Go"], "go test ./...")
            self.assertEqual(commands["Rust · Cargo"], "cargo test")
            self.assertEqual(commands["Java · Maven"], "mvn test")
            self.assertEqual(commands["JVM · Gradle"], "gradle test")
            self.assertEqual(commands[".NET"], "dotnet test")
            self.assertLessEqual(len(result["candidates"]), TestCommandDiscovery.MAX_CANDIDATES)

    def test_nested_monorepo_manifests_are_scoped_to_their_own_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "services/api/go.mod": "module example.test/api\n",
                "services/api/pkg/api_test.go": "package pkg\nfunc TestAPI(t *testing.T) {}\n",
                "crates/engine/Cargo.toml": '[package]\nname="engine"\nversion="0.1.0"\n',
                "crates/engine/src/lib.rs": "#[cfg(test)]\nmod tests { #[test] fn works() {} }\n",
                "services/java/pom.xml": "<project />\n",
                "services/java/src/test/java/AppTest.java": "class AppTest {}\n",
                "tools/android/build.gradle.kts": "plugins {}\n",
                "tools/android/src/test/kotlin/AppTests.kt": "class AppTests\n",
                "dotnet/Worker.Tests/Worker.Tests.csproj": "<Project />\n",
            }
            self._write_files(root, files)
            result = TestCommandDiscovery.discover(root, list(files))
            commands = {candidate["name"]: candidate["command"] for candidate in result["candidates"]}
            self.assertEqual(commands["Go (services/api)"],
                             TestCommandDiscovery._at_directory("services/api", "go test ./..."))
            self.assertEqual(commands["Rust · Cargo (crates/engine)"],
                             "cargo test --manifest-path crates/engine/Cargo.toml")
            self.assertEqual(commands["Java · Maven (services/java)"],
                             "mvn -f services/java/pom.xml test")
            self.assertEqual(commands["JVM · Gradle (tools/android)"],
                             "gradle -p tools/android test")
            self.assertEqual(commands[".NET (dotnet/Worker.Tests/Worker.Tests.csproj)"],
                             "dotnet test dotnet/Worker.Tests/Worker.Tests.csproj")

    def test_nested_manifests_without_test_evidence_do_not_produce_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "modules/go/go.mod": "module example.test/go\n",
                "modules/go/main.go": "package main\nfunc main() {}\n",
                "modules/rust/Cargo.toml": '[package]\nname="plain"\nversion="0.1.0"\n',
                "modules/rust/src/lib.rs": "pub fn value() -> i32 { 1 }\n",
                "modules/java/pom.xml": "<project />\n",
                "modules/java/src/main/java/App.java": "class App {}\n",
                "modules/dotnet/Worker/Worker.csproj": "<Project />\n",
                "modules/js/package.json": '{"scripts":{"build":"vite build","unit":"vitest"}}',
            }
            self._write_files(root, files)
            result = TestCommandDiscovery.discover(root, list(files))
            self.assertEqual(result["candidates"], [])

    def test_nested_node_test_scripts_use_nearest_manager_and_never_echo_script_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
                "packages/ui/package.json":
                    '{"scripts":{"test:unit":"vitest run confidential-script-body"}}',
                "services/api/package.json": '{"scripts":{"test":"node test.js"}}',
                "services/api/package-lock.json": "{}\n",
            }
            self._write_files(root, files)
            result = TestCommandDiscovery.discover(root, list(files))
            commands = {item["name"]: item["command"] for item in result["candidates"]}
            self.assertEqual(commands["JavaScript · test:unit (packages/ui)"],
                             "pnpm --dir packages/ui run test:unit")
            self.assertEqual(commands["JavaScript · package test (services/api)"],
                             "npm --prefix services/api test")
            self.assertNotIn("confidential-script-body", TestCommandDiscovery.format(result))

    def test_unknown_node_package_manager_does_not_guess_npm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                '{"packageManager":"bun@1.2.0","scripts":{"test":"bun test"}}', encoding="utf-8",
            )
            result = TestCommandDiscovery.discover(root, ["package.json"])
            self.assertEqual(result["candidates"], [])
            self.assertTrue(result["limited"])

    def test_default_npm_placeholder_is_not_mistaken_for_a_real_test_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                '{"scripts":{"test":"echo \\\"Error: no test specified\\\" && exit 1"}}',
                encoding="utf-8",
            )
            result = TestCommandDiscovery.discover(root, ["package.json"])
            self.assertEqual(result["candidates"], [])

    def test_python_nested_pytest_configuration_yields_scoped_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "packages/analytics/pyproject.toml":
                    '[project]\nname="analytics"\ndependencies=["pytest>=8"]\n',
                "packages/analytics/tests/test_report.py": "def test_report():\n    assert True\n",
            }
            self._write_files(root, files)
            result = TestCommandDiscovery.discover(root, list(files))
            self.assertEqual(result["candidates"][0]["command"], "python -m pytest packages/analytics")

    def test_tox_and_nox_test_entries_are_static_and_build_only_entries_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "python/tox.ini": "[testenv:unit]\ncommands = python -m pytest\n",
                "python/noxfile.py": (
                    "import nox\n@nox.session(python='3.12')\ndef tests(session):\n"
                    "    session.install('pytest')\n    session.run('pytest', 'private-arg')\n"
                ),
                "build/tox.ini": "[testenv:build]\ncommands = python -m build\n",
                "build/noxfile.py": "import nox\n@nox.session\ndef lint(session):\n    session.run('ruff')\n",
            }
            self._write_files(root, files)
            result = TestCommandDiscovery.discover(root, list(files))
            commands = {item["name"]: item["command"] for item in result["candidates"]}
            self.assertEqual(commands["Python · tox (unit)"], "tox -c python/tox.ini -e unit")
            self.assertEqual(commands["Python · nox (tests)"], "nox -f python/noxfile.py -s tests")
            self.assertEqual(len(commands), 2)
            self.assertNotIn("private-arg", TestCommandDiscovery.format(result))

    def test_make_and_just_test_targets_are_detected_but_build_targets_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "native/Makefile": "test: prepare\n\t./run-tests\nbuild:\n\t./build\n",
                "tools/justfile": "test-unit:\n    pytest\nbuild:\n    cargo build\n",
            }
            self._write_files(root, files)
            result = TestCommandDiscovery.discover(root, list(files))
            commands = {item["name"]: item["command"] for item in result["candidates"]}
            self.assertEqual(commands["Make · test (native)"], "make -C native test")
            self.assertEqual(commands["Just · test-unit (tools)"],
                             "just --justfile tools/justfile test-unit")
            self.assertEqual(len(commands), 2)

    def test_shell_metacharacter_in_module_path_is_not_emitted_as_a_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                "modules/api&admin/go.mod": "module example.test/api\n",
                "modules/api&admin/main_test.go": "package api\nfunc TestAPI(t *testing.T) {}\n",
            }
            self._write_files(root, files)
            result = TestCommandDiscovery.discover(root, list(files))
            self.assertEqual(result["candidates"], [])
            self.assertTrue(result["limited"])

    def test_dirty_manifest_overrides_disk_for_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "package.json"
            manifest.write_text('{"scripts":{"test":"npm run secret-test"}}', encoding="utf-8")
            listed = ["package.json"]
            hidden_on_disk = TestCommandDiscovery.discover(
                root, listed, content_overrides={"package.json": '{"scripts":{}}'},
            )
            added_in_editor = TestCommandDiscovery.discover(
                root, listed, content_overrides={"package.json": '{"scripts":{"test":"npm test"}}'},
            )
            self.assertEqual(hidden_on_disk["candidates"], [])
            self.assertEqual(added_in_editor["candidates"][0]["command"], "npm test")

    def test_symlink_and_out_of_workspace_paths_are_never_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "package.json"
            outside.write_text('{"scripts":{"test":"npm test"}}', encoding="utf-8")
            try:
                os.symlink(outside, root / "package.json")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            result = TestCommandDiscovery.discover(root, ["package.json", "../package.json"])
            self.assertEqual(result["candidates"], [])
            self.assertNotIn(str(outside), TestCommandDiscovery.format(result))

    def test_oversized_manifests_and_unclassified_tests_are_reported_without_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "package.json").write_text('{"scripts":{"test":"npm test"}}', encoding="utf-8")
            (root / "tests" / "helper.py").write_text("value = 1\n", encoding="utf-8")
            with patch.object(TestCommandDiscovery, "MAX_FILE_BYTES", 4):
                result = TestCommandDiscovery.discover(root, ["package.json", "tests/helper.py"])
            self.assertTrue(result["limited"])
            self.assertEqual(result["candidates"], [])
            self.assertIn("无法可靠推断运行器", TestCommandDiscovery.format(result))


class _ScriptedProvider:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)

    def chat(self, _messages, tools=None, max_tokens=12000, request_id="", **_options):
        return self.responses.pop(0)


def _tool_call(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


class VerificationAgentTests(unittest.TestCase):
    class _Process:
        def __init__(self, returncode: int = 0, output: str = "tests passed\n"):
            self.returncode = returncode
            self.output = output

        def communicate(self, timeout=None):
            return self.output.encode("utf-8"), None

    def _run(
        self,
        root: Path,
        session_id: str,
        responses: list[dict],
        *,
        approved: bool = True,
        exit_code: int = 0,
    ):
        (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
        ledger = EventLedger(root)
        git = GitManager(root)
        git.init()
        git.commit_changes("baseline")
        events = []
        agent = CodingAgent(
            root, ledger, git,
            event_callback=lambda name, payload: events.append((name, payload)),
            command_approval=lambda _command, _timeout: approved,
            summary_settings=SummarySettings(enabled=False, execution_mode="manual"),
            require_verification=True,
        )
        provider = _ScriptedProvider(responses)
        real_popen = __import__("subprocess").Popen
        executed_shell_commands = []

        def fake_command_popen(command, *args, **kwargs):
            if isinstance(command, str):
                executed_shell_commands.append(command)
                return self._Process(returncode=exit_code)
            return real_popen(command, *args, **kwargs)

        with (
            patch("scidev_core.OpenAICompatibleProvider.from_env", return_value=provider),
            patch("scidev_core.subprocess.Popen", side_effect=fake_command_popen),
        ):
            try:
                result = agent.run({
                    "payload": {
                        "session_id": session_id,
                        "prompt": "Create and test the Python module.",
                    }
                })
            except PermanentError as exc:
                result = exc
        session = json.loads((root / ".research" / "sessions" / f"{session_id}.json").read_text(encoding="utf-8"))
        return result, session, events, ledger, executed_shell_commands

    def test_non_git_project_fingerprints_discovered_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            source = root / "src" / "module.py"
            source.write_text("value = 1\n", encoding="utf-8")
            agent = CodingAgent(
                root,
                EventLedger(root),
                GitManager(root),
                summary_settings=SummarySettings(enabled=False),
                require_verification=True,
            )
            snapshot = agent._verification_snapshot({"verification_paths": [], "agent_changed_paths": []})

            self.assertTrue(snapshot["complete"])
            self.assertIn("src/module.py", snapshot["path_fingerprints"])
            self.assertFalse(snapshot["git_repository"])

    def test_current_code_check_is_persisted_and_emitted_with_exit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = [
                _tool_call("write_file", {"path": "module.py", "content": "value = 1\n"}, "write"),
                _tool_call("run_command", {"command": "python -m unittest discover -s tests"}, "test"),
                {"role": "assistant", "content": "Checked.", "tool_calls": []},
                {"role": "assistant", "content": "Done.", "tool_calls": []},
            ]
            result, session, events, ledger, _commands = self._run(root, "verification_pass", responses)

            self.assertEqual(result["verification_report"]["status"], "passed")
            self.assertEqual(session["verification_runs"][0]["exit_code"], 0)
            self.assertEqual(session["verification_runs"][0]["check_status"], "passed")
            self.assertTrue(session["verification_runs"][0]["scope_complete"])
            self.assertEqual(session["verification_report"]["status"], session["verification_report"]["latest"]["check_status"])
            self.assertTrue(any(name == "verification_recorded" for name, _ in events))
            self.assertTrue(any(name == "verification_reported" for name, _ in events))
            ledger_types = [
                json.loads(line)["event_type"]
                for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("command_execution_recorded", ledger_types)
            self.assertIn("verification_report_recorded", ledger_types)

    def test_check_is_re_run_after_later_code_edit_before_auto_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = [
                _tool_call("write_file", {"path": "module.py", "content": "value = 1\n"}, "write1"),
                _tool_call("run_command", {"command": "pytest -q"}, "test"),
                _tool_call("replace_in_file", {
                    "path": "module.py", "old_text": "value = 1", "new_text": "value = 2",
                }, "write2"),
                _tool_call("run_command", {"command": "pytest -q"}, "test2"),
                {"role": "assistant", "content": "Done.", "tool_calls": []},
                {"role": "assistant", "content": "Done.", "tool_calls": []},
            ]
            result, session, _events, _ledger, _commands = self._run(root, "verification_stale", responses)

            self.assertEqual([run["check_status"] for run in session["verification_runs"]], ["passed", "passed"])
            self.assertEqual(result["verification_report"]["status"], "passed")
            self.assertEqual(session["verification_runs"][-1]["edit_revision"], session["verification_edit_revision"])

    def test_unchecked_code_is_left_uncommitted_after_bounded_followups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = [
                _tool_call("write_file", {"path": "module.py", "content": "value = 1\n"}, "write"),
                {"role": "assistant", "content": "Done.", "tool_calls": []},
                {"role": "assistant", "content": "Done.", "tool_calls": []},
                {"role": "assistant", "content": "Done.", "tool_calls": []},
                {"role": "assistant", "content": "Done.", "tool_calls": []},
            ]
            result, session, events, ledger, _commands = self._run(root, "verification_required", responses)

            self.assertIsInstance(result, PermanentError)
            self.assertEqual(session["status"], "verification_required")
            self.assertTrue(session["verification_required"])
            self.assertTrue((root / "module.py").exists())
            self.assertEqual(session["git_result_sha"], "")
            self.assertTrue(any(name == "verification_blocked" for name, _ in events))
            ledger_types = [
                json.loads(line)["event_type"]
                for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("verification_retry_scheduled", ledger_types)
            self.assertIn("coding_session_verification_required", ledger_types)

    def test_declined_check_is_not_retried_and_blocks_auto_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = [
                _tool_call("write_file", {"path": "module.py", "content": "value = 1\n"}, "write"),
                _tool_call("run_command", {"command": "pytest -q"}, "test"),
                {"role": "assistant", "content": "Done.", "tool_calls": []},
                {"role": "assistant", "content": "Done.", "tool_calls": []},
            ]
            result, session, _events, ledger, executed = self._run(
                root, "verification_denied", responses, approved=False,
            )

            self.assertIsInstance(result, PermanentError)
            self.assertEqual(executed, [])
            self.assertEqual(session["status"], "verification_required")
            self.assertEqual(session["verification_required_retries"], 0)
            self.assertEqual(session["verification_runs"][0]["execution_status"], "not_run")
            self.assertEqual(session["verification_runs"][0]["approval_status"], "denied")
            self.assertEqual(session["git_result_sha"], "")
            self.assertIn("command_approval_denied", [
                json.loads(line)["event_type"]
                for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
            ])


if __name__ == "__main__":
    unittest.main()
