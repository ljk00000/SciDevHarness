"""Stable classification for worker/CLI failures.

The classifier is intentionally message-based because the provider layer
raises a mix of built-in, urllib and project exceptions. It never includes
request bodies or credentials in the user-facing message.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    user_message: str
    retryable: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "error_kind": self.kind,
            "user_message": self.user_message,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class PermanentError(RuntimeError):
    """An operation should not be retried automatically."""


class TaskCancelled(PermanentError):
    """The user stopped the task while a provider stream was still running."""


class TaskAborted(PermanentError):
    """A safety guard must stop the run instead of feeding an error back."""


_AUTH = re.compile(r"(?i)\b(401|403|unauthori[sz]ed|invalid api key|authentication|forbidden|permission denied)\b|认证|未授权|权限不足|密钥无效")
_RATE = re.compile(r"(?i)\b(429|rate limit|too many requests|quota)\b|限流|额度|过于频繁")
_TIMEOUT = re.compile(r"(?i)\b(timeout|timed out|deadline exceeded)\b|超时")
_NETWORK = re.compile(r"(?i)\b(connection|connect|network|dns|name resolution|proxy|ssl|tls)\b|网络|连接失败|代理")
_APPROVAL = re.compile(r"(?i)\b(approval|approve|review screen|rejected)\b|审批|审核界面|未批准|拒绝批准")
_TOOL = re.compile(r"(?i)\b(tool|schema|json|argument|patch|apply_patch)\b|工具|补丁|参数|调用")
_VERIFY = re.compile(r"(?i)\b(test|pytest|unittest|lint|ruff|mypy|tsc|verification|check)\b|测试|检查|验证")
_WORKSPACE = re.compile(r"(?i)\b(workspace|project|path|file|directory|git|worktree)\b|工作区|文件|路径|仓库")


def classify_message(message: str, *, retryable: bool = True) -> ErrorInfo:
    text = str(message or "").strip()
    detail = text[:4000]
    combined = text[:1200]
    if _AUTH.search(combined):
        return ErrorInfo("auth", "模型服务认证或权限失败，请检查 API Key、模型权限和 endpoint。", False, detail)
    if _RATE.search(combined):
        return ErrorInfo("rate_limit", "模型服务限流或额度不足，稍后重试或切换模型。", True, detail)
    if _TIMEOUT.search(combined):
        return ErrorInfo("timeout", "请求超时；可重试，或拆小任务/降低上下文。", True, detail)
    if _NETWORK.search(combined):
        return ErrorInfo("network", "网络连接失败；检查直连、代理或服务地址。", True, detail)
    if _APPROVAL.search(combined):
        return ErrorInfo("approval", "操作需要人工审批，但当前界面/模式无法批准。", False, detail)
    if _TOOL.search(combined):
        return ErrorInfo("tool", "工具调用或补丁参数无效；模型应修正参数后重试。", True, detail)
    if _VERIFY.search(combined):
        return ErrorInfo("verification", "验证命令失败；检查诊断并修复后重试。", True, detail)
    if _WORKSPACE.search(combined):
        return ErrorInfo("workspace", "工作区或文件操作失败；检查路径、权限和 Git 状态。", False, detail)
    return ErrorInfo(
        "internal",
        "任务内部错误；详细信息已记录，可重试或查看崩溃报告。",
        bool(retryable),
        detail,
    )


def classify_exception(exc: BaseException, *, retryable: bool = True) -> ErrorInfo:
    if isinstance(exc, TimeoutError):
        return ErrorInfo("timeout", "请求超时；可重试，或拆小任务/降低上下文。", True, f"{type(exc).__name__}: {exc}"[:4000])
    return classify_message(f"{type(exc).__name__}: {exc}", retryable=retryable)
