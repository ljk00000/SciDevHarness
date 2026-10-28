"""Explicit, read-only diagnostics for runtimes declared by a project."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from scidev_process import background_process_options


APPROVAL_PURPOSE = "允许 SciDevHarness 对项目运行环境执行一次本地只读核验？"
APPROVAL_EXPLANATION = (
    "只核验项目声明的 Python/Node/Rust/Go 工具链版本、Python pip check 元数据冲突，"
    "并在系统可用时读取 nvidia-smi / nvcc 信息；不构建、不测试、不解析/安装依赖、不联网、不运行项目脚本。"
)
MAX_REPORT_BYTES = 16_000
DIAGNOSTIC_TIMEOUT_SECONDS = 45
TOOL_TIMEOUT_SECONDS = 3
MAX_TOOL_OUTPUT_BYTES = 4000


_DIAGNOSTIC_SCRIPT = r'''import json
import contextlib
import io
import os
import platform
import runpy
import sys
from pathlib import Path

def check_installed_metadata():
    environment_root = Path(sys.argv[1]).resolve(strict=True)
    version = "python%d.%d%s" % (
        sys.version_info.major, sys.version_info.minor, getattr(sys, "abiflags", ""))
    candidates = ([environment_root / "Lib" / "site-packages"] if os.name == "nt" else [
        environment_root / "lib" / version / "site-packages",
        environment_root / "lib64" / version / "site-packages",
    ])
    site_packages = next((path for path in candidates if path.is_dir()), None)
    if site_packages is None:
        return {"available": False, "returncode": None,
                "output": "未找到该虚拟环境的 site-packages"}
    try:
        site_packages.resolve(strict=True).relative_to(environment_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return {"available": False, "returncode": None,
                "output": "site-packages 超出项目虚拟环境范围"}
    sys.path.insert(0, str(site_packages))
    stdout, stderr = io.StringIO(), io.StringIO()
    original_argv = sys.argv
    code = 1
    try:
        sys.argv = ["pip", "check"]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                runpy.run_module("pip", run_name="__main__", alter_sys=True)
                code = 0
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except (ImportError, OSError, ValueError) as exc:
        return {"available": False, "returncode": None,
                "output": "pip check 无法启动：" + type(exc).__name__}
    finally:
        sys.argv = original_argv
    output = (stdout.getvalue() + stderr.getvalue())[:4000]
    return {"available": True, "returncode": code, "output": output}

pip = check_installed_metadata()
print(json.dumps({
    "python": sys.version.split()[0],
    "implementation": platform.python_implementation(),
    "pip_check": pip,
}, ensure_ascii=False))
'''


class ProjectEnvironmentInspector:
    """Run one approved diagnostic against project-declared language toolchains."""

    ENVIRONMENTS = (".venv", "venv", "env")
    RUNTIME_TOOLS = {
        "Node.js": (("node", "--version"),),
        "Rust": (("rustc", "--version"), ("cargo", "--version")),
        "Go": (("go", "version"),),
    }

    def __init__(
        self,
        project_root: Path,
        command_approval: Callable[[str, int], bool],
        *,
        process_runner: Callable[..., Any] | None = None,
        event_recorder: Callable[[str, dict[str, Any]], Any] | None = None,
        tool_finder: Callable[[str], str | None] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.command_approval = command_approval
        self.process_runner = process_runner or subprocess.run
        self.event_recorder = event_recorder
        self.tool_finder = tool_finder or shutil.which
        # Last failure of the optional history recorder; empty when healthy.
        self.last_record_error = ""

    def _record(self, event: str, payload: dict[str, Any]) -> None:
        if self.event_recorder is not None:
            try:
                self.event_recorder(event, payload)
            except Exception as exc:  # noqa: BLE001 - history is optional, report stays usable
                # Diagnostics must remain usable when local history is
                # unavailable, but the failure must not be invisible.
                self.last_record_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            return bool(callable(is_junction) and is_junction())
        except OSError:
            return True

    def _project_interpreter(self) -> tuple[Path, str] | None:
        if not self.project_root.is_dir():
            return None
        windows = os.name == "nt"
        for environment in self.ENVIRONMENTS:
            env_root = self.project_root / environment
            if self._is_link_or_junction(env_root) or not env_root.is_dir():
                continue
            relative_candidates = (
                ("Scripts/python.exe",) if windows else ("bin/python", "bin/python3")
            )
            for executable in relative_candidates:
                candidate = env_root / executable
                linked_interpreter = self._is_link_or_junction(candidate)
                if linked_interpreter and (windows or not candidate.is_symlink()):
                    continue
                if not candidate.is_file():
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                    if not linked_interpreter:
                        resolved.relative_to(self.project_root)
                    if not resolved.is_file():
                        continue
                except (OSError, RuntimeError, ValueError):
                    continue
                # Preserve the venv path as argv[0]; resolving POSIX venv symlinks
                # would launch the base interpreter outside its virtual environment.
                return candidate.absolute(), f"{environment}/{executable}"
        return None

    def _declared_runtimes(self) -> dict[str, str]:
        manifests = {
            "Node.js": "package.json",
            "Rust": "Cargo.toml",
            "Go": "go.mod",
        }
        declared: dict[str, str] = {}
        python_files = ("pyproject.toml",)
        for name in python_files:
            path = self.project_root / name
            if not self._is_link_or_junction(path) and path.is_file():
                declared["Python"] = name
                break
        if "Python" not in declared:
            try:
                for path in self.project_root.glob("requirements*.txt"):
                    if not self._is_link_or_junction(path) and path.is_file():
                        declared["Python"] = path.name
                        break
            except OSError:
                pass
        for runtime, filename in manifests.items():
            path = self.project_root / filename
            if not self._is_link_or_junction(path) and path.is_file():
                declared[runtime] = filename
        return declared

    def _tool_path(self, name: str) -> str | None:
        try:
            found = self.tool_finder(name)
        except (OSError, ValueError):
            return None
        if not found:
            return None
        try:
            path = Path(found)
            if os.name == "nt" and path.suffix.casefold() not in {".exe", ".com"}:
                return None
            return str(path.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _python_argv(interpreter: Path) -> list[str]:
        return [
            str(interpreter), "-I", "-B", "-S", "-c", _DIAGNOSTIC_SCRIPT,
            str(interpreter.parent.parent),
        ]

    @staticmethod
    def _display_argv(argv: list[str]) -> str:
        if os.name == "nt":
            return subprocess.list2cmdline(argv)
        return shlex.join(argv)

    def _command_for_approval(
        self,
        interpreter: Path | None,
        probes: list[tuple[str, list[str]]],
    ) -> str:
        lines = ["固定本地诊断计划（逐项 shell=False；不运行构建/测试/项目脚本）："]
        if interpreter is not None:
            lines.append("Python + pip check: " + self._display_argv(self._python_argv(interpreter)))
        lines.extend(f"{label}: {self._display_argv(argv)}" for label, argv in probes)
        return "\n".join(lines)

    @staticmethod
    def _format_report(data: dict[str, Any], interpreter_label: str) -> tuple[str, str]:
        python = str(data.get("python") or "未知")[:80]
        implementation = str(data.get("implementation") or "未知")[:80]
        lines = [
            "项目环境核验（本地只读）",
            f"项目解释器：{interpreter_label}",
            f"Python：{python}（{implementation}）",
        ]
        pip = data.get("pip_check") if isinstance(data.get("pip_check"), dict) else {}
        pip_output = str(pip.get("output") or "").strip()[:4000]
        pip_code = pip.get("returncode")
        if not pip.get("available"):
            lines.append("依赖元数据：未安装 pip，无法运行 pip check")
            status = "issues"
        elif pip_code == 0:
            lines.append("依赖元数据：pip check 未发现已安装包之间的冲突")
            status = "passed"
        elif pip_code is None:
            lines.append("依赖元数据：pip check 未能完成")
            status = "issues"
        else:
            lines.append(f"依赖元数据：pip check 返回 {pip_code}，发现冲突或检查错误")
            status = "issues"
        if pip_output:
            lines.append(pip_output)
        lines.append(
            "边界：pip check 只检查已安装分布的元数据冲突，不证明项目声明的依赖齐全或可导入；"
            "Python 版本与声明约束一致也不证明项目可运行。"
        )
        return "\n".join(lines)[:MAX_REPORT_BYTES], status

    def inspect(self) -> dict[str, Any]:
        selected = self._project_interpreter()
        interpreter, interpreter_label = selected if selected else (None, "")
        declared = self._declared_runtimes()
        probes: list[tuple[str, list[str]]] = []
        missing_tools: list[tuple[str, str]] = []
        for runtime, tool_specs in self.RUNTIME_TOOLS.items():
            if runtime not in declared:
                continue
            for tool_name, *arguments in tool_specs:
                executable = self._tool_path(tool_name)
                label = f"{runtime}（{tool_name}）"
                if executable is None:
                    missing_tools.append((label, tool_name))
                else:
                    probes.append((label, [executable, *arguments]))

        optional_tools = (
            ("NVIDIA GPU / 驱动 / 显存", "nvidia-smi", [
                "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader",
            ]),
            ("CUDA Toolkit", "nvcc", ["--version"]),
        )
        optional_missing: list[tuple[str, str]] = []
        for label, tool_name, arguments in optional_tools:
            executable = self._tool_path(tool_name)
            if executable is None:
                optional_missing.append((label, tool_name))
            else:
                probes.append((label, [executable, *arguments]))

        has_project_runtime = bool(declared or selected)
        has_any_command = bool(interpreter or probes)
        if not has_project_runtime and not probes:
            report = (
                "未发现可核验的项目运行时清单、项目 Python 虚拟环境或本机 GPU/CUDA 工具；"
                "不会使用 Harness Python 作为回退。"
            )
            self._record("project_environment_check_unavailable", {"reason": "no_runtime_or_tools"})
            return {"status": "unavailable", "interpreter": "", "report": report, "checks": []}

        if not has_any_command:
            lines = ["项目环境核验（本地静态发现；未运行命令）"]
            if "Python" in declared:
                lines.append("Python：项目声明了运行时，但未找到项目内 .venv / venv / env；未回退到 Harness Python。")
            for runtime, manifest in declared.items():
                if runtime != "Python":
                    lines.append(f"{runtime}：发现根目录 {manifest}，但对应工具链未安装或不可执行。")
            self._record("project_environment_check_unavailable", {
                "reason": "declared_runtime_tools_missing",
                "runtimes": sorted(declared),
            })
            return {"status": "issues", "interpreter": "", "report": "\n".join(lines), "checks": []}

        planned_checks = (["Python"] if interpreter is not None else []) + [label for label, _argv in probes]
        command = self._command_for_approval(interpreter, probes)
        self._record(
            "project_environment_check_approval_requested",
            {
                "interpreter": interpreter_label,
                "runtimes": sorted(declared),
                "checks": planned_checks,
                "probe_count": int(interpreter is not None) + len(probes),
                "timeout_seconds": DIAGNOSTIC_TIMEOUT_SECONDS,
            },
        )
        try:
            approved = bool(self.command_approval(command, DIAGNOSTIC_TIMEOUT_SECONDS))
        except Exception as exc:
            approved = False
            approval_error = type(exc).__name__
        else:
            approval_error = ""
        if not approved:
            self._record(
                "project_environment_check_not_run",
                {
                    "interpreter": interpreter_label,
                    "runtimes": sorted(declared),
                    "checks": [],
                    "reason": approval_error or "user_denied",
                },
            )
            return {
                "status": "denied",
                "interpreter": interpreter_label,
                "report": "已取消环境核验；未启动任何进程。",
                "checks": [],
            }

        self._record("project_environment_check_started", {
            "interpreter": interpreter_label,
            "runtimes": sorted(declared),
        })
        lines = ["项目环境核验（本地只读）"]
        has_issues = False
        deadline = time.monotonic() + DIAGNOSTIC_TIMEOUT_SECONDS
        last_exit_code: int | None = None
        attempted_checks: list[str] = []

        if interpreter is not None:
            attempted_checks.append("Python")
            lines.append(f"Python（项目解释器 {interpreter_label}）：")
            remaining = max(1, int(deadline - time.monotonic()))
            try:
                result = self.process_runner(
                    self._python_argv(interpreter),
                    cwd=str(self.project_root), shell=False,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=min(24, remaining), check=False,
                    **background_process_options(),
                )
                last_exit_code = getattr(result, "returncode", None)
                output = str(getattr(result, "stdout", "") or "")[:MAX_REPORT_BYTES]
                payload = json.loads(output)
                if not isinstance(payload, dict):
                    raise ValueError("diagnostic result must be an object")
                python_report, python_status = self._format_report(payload, interpreter_label)
                lines.extend(python_report.splitlines()[2:])
                has_issues |= python_status != "passed" or last_exit_code != 0
                if last_exit_code != 0:
                    lines.append("Python 诊断进程返回非零退出码，结果可能不完整。")
            except subprocess.TimeoutExpired:
                has_issues = True
                lines.append("Python：项目解释器核验超时")
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                has_issues = True
                lines.append(f"Python：无法完成项目解释器核验（{type(exc).__name__}）")
        elif "Python" in declared:
            has_issues = True
            lines.append("Python：发现项目清单，但没有项目内 .venv / venv / env；未回退到 Harness Python。")

        for label, tool_name in missing_tools:
            has_issues = True
            lines.append(f"{label}：未在 PATH 找到可执行的 {tool_name}。")
        for label, tool_name in optional_missing:
            lines.append(f"{label}：未检测到 {tool_name}（这不证明设备/Toolkit 不存在）。")

        for label, argv in probes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                has_issues = True
                lines.append(f"{label}：总核验超时，未启动该探测。")
                continue
            attempted_checks.append(label)
            try:
                result = self.process_runner(
                    argv,
                    cwd=str(self.project_root), shell=False,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=min(TOOL_TIMEOUT_SECONDS, remaining), check=False,
                    **background_process_options(),
                )
                code = getattr(result, "returncode", None)
                last_exit_code = code
                output = str(getattr(result, "stdout", "") or "").strip()[:MAX_TOOL_OUTPUT_BYTES]
                if code == 0 and output:
                    shown = output.splitlines()[-1][:1200] if label == "CUDA Toolkit" else output[:1200]
                    lines.append(f"{label}：{shown}")
                else:
                    has_issues = True
                    detail = f"返回码 {code}" + (f"；{output}" if output else "")
                    lines.append(f"{label}：核验失败（{detail}）")
            except subprocess.TimeoutExpired:
                has_issues = True
                lines.append(f"{label}：版本探测超时")
            except OSError as exc:
                has_issues = True
                lines.append(f"{label}：无法启动（{type(exc).__name__}）")

        if not declared and interpreter is None:
            lines.insert(1, "未发现 Node/Rust/Go 根清单；仅探测本机 GPU/CUDA 工具。")
        lines.append(
            "边界：只核验工具链版本及 Python pip 元数据冲突；不解析其他语言依赖、不构建、不运行测试或项目脚本。"
        )
        status = "issues" if has_issues else "passed"
        report = "\n".join(lines)[:MAX_REPORT_BYTES]
        self._record(
            "project_environment_check_finished",
            {
                "interpreter": interpreter_label,
                "runtimes": sorted(declared),
                "checks": attempted_checks,
                "status": status,
                "last_exit_code": last_exit_code,
            },
        )
        return {
            "status": status,
            "interpreter": interpreter_label,
            "report": report,
            "exit_code": last_exit_code,
            "checks": attempted_checks,
        }
