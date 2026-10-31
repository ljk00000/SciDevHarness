"""Small real-repository eval harness.

A suite is a JSON file with a workspace template and fixed tasks. Each task
runs in an isolated copy, then a deterministic check command decides success.
The default runner is ``scidev_cli.py exec``; tests can inject a fake runner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

MAX_TASKS = 50
MAX_SETUP_BYTES = 2_000_000
MAX_CHECK_OUTPUT = 8_000


class EvalError(ValueError):
    pass


@dataclass(frozen=True)
class EvalTask:
    task_id: str
    prompt: str
    setup: dict[str, str] = field(default_factory=dict)
    check: list[str] = field(default_factory=list)
    timeout: float = 180.0
    verify: bool = False


@dataclass
class EvalResult:
    task_id: str
    success: bool
    elapsed_seconds: float
    turn_count: int = 0
    tokens: int = 0
    exit_code: int | None = None
    check_exit: int | None = None
    error: str = ""
    output: str = ""
    check_output: str = ""


TaskRunner = Callable[[Path, EvalTask], dict[str, Any]]


def _safe_relative(raw: str, *, base: Path | None = None) -> Path:
    path = Path(str(raw))
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise EvalError(f"不安全的相对路径：{raw}")
    candidate = path if base is None else base / path
    if base is not None:
        try:
            candidate.resolve().relative_to(base.resolve())
        except ValueError as exc:
            raise EvalError(f"路径逃逸：{raw}") from exc
    return path


def _parse_suite(path: Path) -> tuple[str, Path | None, list[EvalTask]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalError(f"无法读取 eval suite：{exc}") from exc
    if not isinstance(data, dict):
        raise EvalError("eval suite 必须是 JSON 对象")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise EvalError("eval suite 缺少 tasks")
    if len(raw_tasks) > MAX_TASKS:
        raise EvalError(f"最多 {MAX_TASKS} 个任务")
    workspace = data.get("workspace")
    workspace_path = None
    if workspace:
        workspace_path = (path.parent / str(workspace)).resolve()
        if not workspace_path.is_dir():
            raise EvalError(f"workspace 模板不存在：{workspace_path}")
    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise EvalError(f"tasks[{index}] 必须是对象")
        task_id = str(raw.get("id") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not task_id or task_id in seen:
            raise EvalError(f"tasks[{index}] id 为空或重复")
        if not prompt:
            raise EvalError(f"tasks[{index}] prompt 为空")
        seen.add(task_id)
        setup_raw = raw.get("setup") or {}
        if not isinstance(setup_raw, dict):
            raise EvalError(f"tasks[{index}].setup 必须是对象")
        setup: dict[str, str] = {}
        total = 0
        for raw_path, raw_content in setup_raw.items():
            relative = _safe_relative(str(raw_path))
            content = str(raw_content)
            total += len(content.encode("utf-8"))
            if total > MAX_SETUP_BYTES:
                raise EvalError(f"tasks[{index}] setup 超过大小上限")
            setup[relative.as_posix()] = content
        check_raw = raw.get("check") or []
        if not isinstance(check_raw, list) or not check_raw:
            raise EvalError(f"tasks[{index}].check 必须是非空命令数组")
        check = [str(value) for value in check_raw]
        try:
            timeout = max(5.0, min(float(raw.get("timeout", 180) or 180), 1800.0))
        except (TypeError, ValueError):
            timeout = 180.0
        tasks.append(EvalTask(
            task_id=task_id,
            prompt=prompt,
            setup=setup,
            check=check,
            timeout=timeout,
            verify=bool(raw.get("verify", False)),
        ))
    return str(data.get("name") or path.stem), workspace_path, tasks


def _init_git(work: Path) -> None:
    git = shutil.which("git")
    if not git:
        return
    commands = [
        [git, "init", "-q"],
        [git, "-c", "user.name=SciDev Eval", "-c", "user.email=eval@example.invalid", "add", "-A"],
        [git, "-c", "user.name=SciDev Eval", "-c", "user.email=eval@example.invalid",
         "commit", "-q", "-m", "eval baseline"],
    ]
    for command in commands:
        try:
            subprocess.run(
                command,
                cwd=work,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return


def _default_task_runner(work: Path, task: EvalTask) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent
    cli = project_root / "scidev_cli.py"
    command = [
        os.fspath(Path(sys.executable)),
        str(cli),
        "exec",
        "--workspace", str(work),
        "--task", task.prompt,
        "--json",
        "--execution-mode", "workspace-write",
    ]
    if not task.verify:
        command.append("--no-verify")
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=task.timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "error": str(exc), "output": ""}
    payload: dict[str, Any] = {}
    for line in reversed(completed.stdout.splitlines()):
        if line.strip().startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {
        "success": bool(payload.get("success")) and completed.returncode == 0,
        "exit_code": completed.returncode,
        "turn_count": int(payload.get("turn_count", 0) or 0),
        "tokens": int(usage.get("total_tokens", 0) or 0),
        "output": (completed.stdout or completed.stderr)[-MAX_CHECK_OUTPUT:],
        "error": str(payload.get("error") or ""),
    }


class EvalRunner:
    def __init__(
        self,
        project_root: Path,
        suite_path: Path,
        *,
        task_runner: TaskRunner | None = None,
        output_path: Path | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.suite_path = Path(suite_path).resolve()
        self.task_runner = task_runner or _default_task_runner
        self.output_path = (
            Path(output_path) if output_path is not None
            else self.project_root / ".research" / "evals" / f"eval-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        )
        self.suite_name, self.workspace_template, self.tasks = _parse_suite(self.suite_path)

    def _prepare(self, work: Path, task: EvalTask) -> None:
        if self.workspace_template is not None:
            shutil.copytree(self.workspace_template, work, dirs_exist_ok=True)
        for raw_path, content in task.setup.items():
            target = work / _safe_relative(raw_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _init_git(work)

    def _check(self, work: Path, task: EvalTask) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                task.check,
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(task.timeout, 120.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)[-MAX_CHECK_OUTPUT:]
        return int(completed.returncode), (completed.stdout + completed.stderr)[-MAX_CHECK_OUTPUT:]

    def run_task(self, task: EvalTask) -> EvalResult:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix=f"scidev-eval-{task.task_id}-") as temp:
            work = Path(temp) / "workspace"
            work.mkdir()
            self._prepare(work, task)
            try:
                outcome = self.task_runner(work, task)
            except Exception as exc:  # noqa: BLE001 - one task failure is data, not a crash
                outcome = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            success = bool(outcome.get("success"))
            check_exit, check_output = self._check(work, task)
            if check_exit != 0:
                success = False
            elapsed = round(time.monotonic() - started, 3)
            return EvalResult(
                task_id=task.task_id,
                success=success,
                elapsed_seconds=elapsed,
                turn_count=int(outcome.get("turn_count", 0) or 0),
                tokens=int(outcome.get("tokens", 0) or 0),
                exit_code=outcome.get("exit_code"),
                check_exit=check_exit,
                error=str(outcome.get("error") or ""),
                output=str(outcome.get("output") or "")[-MAX_CHECK_OUTPUT:],
                check_output=check_output,
            )

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        results = [self.run_task(task) for task in self.tasks]
        total = len(results)
        passed = sum(1 for result in results if result.success)
        total_tokens = sum(result.tokens for result in results)
        total_turns = sum(result.turn_count for result in results)
        summary = {
            "suite": self.suite_name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "average_turns": round(total_turns / total, 2) if total else 0.0,
            "total_tokens": total_tokens,
            "average_tokens": round(total_tokens / total, 1) if total else 0.0,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "previous_pass_rate": None,
            "regression": None,
            "results": [result.__dict__ for result in results],
        }
        previous = self._load_previous()
        if previous is not None:
            previous_rate = previous.get("pass_rate")
            summary["previous_pass_rate"] = previous_rate
            if isinstance(previous_rate, (int, float)):
                summary["regression"] = round(summary["pass_rate"] - float(previous_rate), 4)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary

    def _load_previous(self) -> dict[str, Any] | None:
        path = self.output_path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
