"""Optional container-backed command execution.

This is a real isolation boundary only when Docker is selected *and* the
daemon/image are available. It is intentionally opt-in: the default local
backend keeps the current behavior, while selecting Docker fails closed rather
than silently falling back when the daemon, image, or CLI is missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import shutil
import subprocess
from typing import Any, Callable
import uuid


class SandboxError(RuntimeError):
    """The requested sandbox cannot be used or could not be constructed."""


@dataclass
class DockerSandbox:
    image: str
    network: str = "none"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 256
    docker_path: str = ""
    checker: Callable[[], str] | None = None
    cleanup_runner: Callable[[list[str]], Any] | None = None
    _checked: str = field(default="", init=False, repr=False)
    _container_name: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self.image = str(self.image or "").strip()
        if not self.image:
            raise SandboxError("Docker 沙箱需要 SCIDEV_SANDBOX_IMAGE，且不能为空")
        if any(character.isspace() for character in self.image):
            raise SandboxError("SCIDEV_SANDBOX_IMAGE 不能包含空白")
        if self.network not in {"none", "bridge"}:
            raise SandboxError("SCIDEV_SANDBOX_NETWORK 只能是 none 或 bridge")

    @classmethod
    def from_environment(cls, *, backend: str = "", image: str = "") -> DockerSandbox | None:
        selected = (backend or os.getenv("SCIDEV_SANDBOX_BACKEND", "local")).strip().lower()
        if selected in {"", "local", "none", "off"}:
            return None
        if selected != "docker":
            raise SandboxError(f"不认识的沙箱后端：{selected}（支持 local/docker）")
        return cls(
            image=image or os.getenv("SCIDEV_SANDBOX_IMAGE", ""),
            network=os.getenv("SCIDEV_SANDBOX_NETWORK", "none").strip().lower(),
            memory=os.getenv("SCIDEV_SANDBOX_MEMORY", "2g"),
            cpus=os.getenv("SCIDEV_SANDBOX_CPUS", "2"),
        )

    def _docker(self) -> str:
        if self.docker_path:
            return self.docker_path
        found = shutil.which("docker")
        if not found:
            raise SandboxError("找不到 docker CLI；拒绝回退到本机执行")
        self.docker_path = found
        return found

    def ensure_available(self) -> str:
        if self._checked:
            return self._checked
        docker = self._docker()
        if self.checker is not None:
            version = self.checker()
        else:
            try:
                completed = subprocess.run(
                    [docker, "version", "--format", "{{.Server.Version}}"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise SandboxError(f"无法检查 Docker daemon：{exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[-300:]
                raise SandboxError(f"Docker daemon 不可用，拒绝回退到本机执行：{detail}")
            version = completed.stdout.strip()
        self._checked = version or "unknown"
        return self._checked

    def wrap(self, command: str, workspace) -> list[str]:
        self.ensure_available()
        docker = self._docker()
        self._container_name = f"scidev-{uuid.uuid4().hex[:12]}"
        argv = [
            docker,
            "run",
            "--rm",
            "--init",
            "--name",
            self._container_name,
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
        ]
        if os.name != "nt":
            getuid = getattr(os, "getuid", None)
            getgid = getattr(os, "getgid", None)
            if callable(getuid) and callable(getgid):
                argv.extend(["--user", f"{getuid()}:{getgid()}"])
        argv.extend([
            "-v",
            f"{workspace}:/workspace:rw",
            "-w",
            "/workspace",
            "--env",
            "PYTHONIOENCODING=utf-8",
            "--env",
            "PYTHONUTF8=1",
            self.image,
            "sh",
            "-lc",
            command,
        ])
        return argv

    @property
    def name(self) -> str:
        return f"docker:{self.image}"


    def cleanup(self) -> None:
        """Force-remove the last container after timeout/cancel; best effort."""
        container = self._container_name
        self._container_name = ""
        if not container:
            return
        try:
            docker = self._docker()
            runner = self.cleanup_runner
            if runner is None:
                subprocess.run(
                    [docker, "rm", "-f", container],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            else:
                runner([docker, "rm", "-f", container])
        except (OSError, subprocess.SubprocessError, SandboxError):
            return
