"""Verify a clean Windows install and optionally an upgrade and rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import winreg

from smoke import FAKE_KEY, ROOT, model, stop, tool


UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
PREVIEW_ID = "{8163BF31-5F43-4B08-B545-E6DDB95DA965}_is1"
PYTHON_ID = "{8C6D7B1E-5A0F-4F9E-9F1A-6C2D8B7E4A31}_is1"
FLAGS = [
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
    "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS",
]
CAPTURES = {"1280x720.png", "1920x1080.png", "2560x1440.png", "3840x2160.png"}
LEGACY_SESSION = "session_fixture"
SHORTCUT_NAME = "SciDevHarness C# Preview"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def invoke(args: list, env: dict[str, str], stage: str, timeout: int = 30) -> str:
    result = subprocess.run(
        [str(arg) for arg in args], cwd=ROOT, env=env, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    require(result.returncode == 0, f"{stage} returned exit code {result.returncode}.")
    require(FAKE_KEY not in result.stdout + result.stderr, f"{stage} exposed the fixture credential.")
    return result.stdout


def package_version(installer: Path) -> tuple[int, ...]:
    match = re.fullmatch(r"SciDevHarness-CSharp-Preview-Setup-(\d+)\.(\d+)\.(\d+)\.exe", installer.name)
    if match is None or not installer.is_file():
        raise ValueError(f"Expected a local C# preview installer: {installer.name}")
    return tuple(map(int, match.groups()))


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def registrations(app_id: str) -> set[tuple[str, str]]:
    found = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = winreg.OpenKey(hive, f"{UNINSTALL}\\{app_id}", 0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with key:
                location = winreg.QueryValueEx(key, "InstallLocation")[0]
                version = winreg.QueryValueEx(key, "DisplayVersion")[0]
                found.add((location, version))
    return found


class PreviewInstall:
    def __init__(self, evidence: Path, env: dict[str, str]):
        self.evidence = evidence
        self.target = evidence / "installed-preview"
        self.cli = self.target / "Cli/SciDev.Cli.exe"
        self.desktop = self.target / "Workbench/SciDev.Desktop.exe"
        self.env = env
        self.versions: set[str] = set()
        require(not registrations(PREVIEW_ID) and not registrations(PYTHON_ID),
                "Use a clean Windows profile without a registered SciDevHarness install.")
        require(not self.target.exists(), "The temporary installation directory already exists.")
        self.powershell = Path(env["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        programs = self.shell("(New-Object -ComObject WScript.Shell).SpecialFolders.Item('Programs')")
        require(Path(programs).is_absolute(), "Could not locate the current user's Start menu.")
        self.shortcut = Path(programs) / SHORTCUT_NAME / (SHORTCUT_NAME + ".lnk")
        require(not self.shortcut.exists(), "An existing preview shortcut is present.")

    def install(self, installer: Path, stage: str) -> None:
        version = ".".join(map(str, package_version(installer)))
        self.versions.add(version)
        invoke([installer, *FLAGS, "/SP-", f"/DIR={self.target}", f"/LOG={self.evidence / (stage + '.log')}"],
               self.env, stage, timeout=180)
        require(self.registration() == version, f"{stage} did not register the requested preview version.")
        require(self.cli.is_file() and self.desktop.is_file(), f"{stage} did not install both executables.")
        require(self.shortcut.is_file(), f"{stage} did not install the Start menu shortcut.")
        link = json.loads(self.shell(
            "$link=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SCIDEV_PREVIEW_TEST_SHORTCUT); "
            "@{target=$link.TargetPath;arguments=$link.Arguments} | ConvertTo-Json -Compress",
            shortcut=self.shortcut,
        ))
        arguments = ("", "--choose-workspace") if package_version(installer) < (0, 1, 8) else ("",)
        require(Path(link["target"]).resolve() == self.desktop and link["arguments"] in arguments,
                f"{stage} created an incorrect Start menu shortcut.")
        status = self.inspect("doctor", "--workspace", self.evidence)
        require(status["version"] == version, f"{stage} installed the wrong CLI version.")
        for executable in (self.cli, self.desktop):
            config = executable.with_suffix(".runtimeconfig.json")
            with config.open(encoding="utf-8-sig") as file:
                framework = json.load(file)["runtimeOptions"].get("includedFrameworks")
            require(bool(framework), f"{executable.name} is not self-contained.")

    def registration(self) -> str | None:
        current = registrations(PREVIEW_ID)
        if not current:
            return None
        require(len(current) == 1 and not registrations(PYTHON_ID), "Unexpected install registrations.")
        location, version = next(iter(current))
        require(Path(location).resolve() == self.target and version in self.versions,
                "Refusing to use a registration outside the disposable preview.")
        return version

    def uninstall(self, stage: str) -> None:
        if self.registration() is None:
            require(not any(path.exists() for path in (self.cli, self.desktop, self.shortcut)),
                    f"{stage} left unregistered preview files.")
            return
        uninstaller = self.target / "unins000.exe"
        require(uninstaller.is_file(), f"{stage} cannot find the disposable preview uninstaller.")
        invoke([uninstaller, *FLAGS, f"/LOG={self.evidence / (stage + '.log')}"],
               self.env, stage, timeout=180)
        until = time.monotonic() + 10
        while self.registration() is not None or any(path.exists() for path in (self.cli, self.desktop, self.shortcut)):
            require(time.monotonic() < until, f"{stage} left files or registration behind.")
            time.sleep(0.2)

    def inspect(self, *args: object) -> dict:
        return json.loads(invoke([self.cli, *args, "--json"], self.env, str(args[0])))

    def shell(self, code: str, *, shortcut: Path | None = None) -> str:
        env = {**self.env, "SCIDEV_PREVIEW_TEST_SHORTCUT": str(shortcut)} if shortcut else self.env
        return invoke([self.powershell, "-NoProfile", "-NonInteractive", "-Command",
                       "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                       "$ErrorActionPreference='Stop'; " + code], env, "Start menu inspection").strip()


def make_project(evidence: Path) -> tuple[Path, Path]:
    project = evidence / "project-copy"
    legacy = project / ".research" / "sessions" / f"{LEGACY_SESSION}.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "session_id": LEGACY_SESSION, "prompt": "Existing Python session", "status": "completed",
        "messages": [{"role": "assistant", "content": "Keep the original session."}],
    }), encoding="utf-8")
    return project, legacy


def check_legacy(cli: Path, project: Path, env: dict[str, str]) -> None:
    result = json.loads(invoke([cli, "inspect-run", "--workspace", project,
                                "--run-id", LEGACY_SESSION, "--source", "python", "--json"], env, "legacy session"))
    require(result["summary"] == "Keep the original session.", "Legacy session could not be read.")


def run_code_task(cli: Path, project: Path, env: dict[str, str]) -> tuple[str, Path]:
    content = "ANSWER = 42\n"
    with model([tool("write_file", path="answer.py", content=content), stop("Wrote answer.py.")]) as (endpoint, requests):
        result = json.loads(invoke([
            cli, "exec", "--workspace", project, "--task", "Create answer.py containing the requested constant.",
            "--session-id", "upgrade-fixture", "--api-base", endpoint, "--model", "offline-fixture",
            "--max-tokens", "12000", "--json",
        ], {**env, "SCIDEV_API_KEY": FAKE_KEY}, "installed file task"))
        require(len(requests) == 2, "Unexpected model interactions.")
    require(result["success"] and result["verification"]["status"] == "not_run" and
            result["verification"]["reason"] == "container_not_configured", "Default no-container code task failed.")
    require((project / "answer.py").read_text(encoding="utf-8") == content, "The task did not write the expected file.")
    return result["run_id"], project / result["events_path"]


def verify(previous: Path | None, candidate: Path, evidence: Path) -> None:
    version = package_version(candidate)
    if previous is not None:
        require(package_version(previous) < version, "Candidate version must be newer.")
    evidence.mkdir(parents=True)
    temporary = evidence / "temp"
    temporary.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("SCIDEV_", "OPENAI_"))}
    env.update(TEMP=str(temporary), TMP=str(temporary), PYTHONUTF8="1")
    report = {
        "previous": {"name": previous.name, "sha256": digest(previous)} if previous else None,
        "candidate": {"name": candidate.name, "sha256": digest(candidate)},
        "completed_stages": [],
        "installed_preview_verified": False,
    }
    stages = report["completed_stages"]
    preview = None
    try:
        preview = PreviewInstall(evidence, env)
        project, legacy = make_project(evidence)
        legacy_hash = digest(legacy)
        if previous is not None:
            preview.install(previous, "install-previous")
            check_legacy(preview.cli, project, env)
            stages.append("install_previous")

        preview.install(candidate, "upgrade" if previous else "install-candidate")
        check_legacy(preview.cli, project, env)
        invoke([sys.executable, ROOT / "dotnet/tests/smoke.py", "--preview-exe", preview.cli],
               env, "installed default permissions", timeout=120)
        run_id, events = run_code_task(preview.cli, project, env)
        retained = {path: digest(path) for path in (events, project / "answer.py")}
        capture = evidence / "captures"
        invoke([preview.desktop, "--capture-workbench", capture], env, "installed workbench capture", timeout=120)
        require({path.name for path in capture.glob("*.png")} == CAPTURES, "Incomplete installed workbench captures.")
        stages.append("upgrade_and_run_without_docker" if previous else "install_and_run_without_docker")

        preview.uninstall("uninstall-candidate")
        stages.append("uninstall_candidate")
        if previous is not None:
            preview.install(previous, "rollback")
            check_legacy(preview.cli, project, env)
            inspection = preview.inspect("inspect-run", "--workspace", project, "--run-id", run_id)
            require(inspection["final_status"] == "completed" and inspection["session"]["session_id"] == "upgrade-fixture",
                    "The previous CLI could not reopen the new run after rollback.")
            stages.append("rollback_retains_old_and_new_data")
            preview.uninstall("uninstall-rollback")
            stages.append("uninstall_previous")
        require(all(digest(path) == expected for path, expected in retained.items()), "Installer lifecycle changed new project data.")
        require(digest(legacy) == legacy_hash, "Installer lifecycle changed original Python session bytes.")
        report["installed_preview_verified"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = str(error)
        if preview is not None:
            try:
                preview.uninstall("cleanup")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        raise
    finally:
        (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, help="Previous installer for upgrade and rollback checks.")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="New directory for disposable test files and logs.")
    args = parser.parse_args()
    verify(args.previous.resolve() if args.previous else None, args.candidate.resolve(), args.evidence.resolve())
