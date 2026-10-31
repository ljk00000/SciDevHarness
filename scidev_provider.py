"""Non-secret provider configuration with API keys stored in Credential Manager."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

import scidev_credentials as credentials


CONFIG_NAME = "provider.json"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5"


class ProviderConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    request_timeout: float = 90.0
    streaming: bool = True
    text_tool_call_fallback: bool = False
    temperature: float = 0.2
    presence_penalty: float | None = None
    source: str = "environment"
    credential_profile: str = "default"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _float_value(value: Any, default: float, label: str, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigError(f"{label}必须是 {low:g} 到 {high:g} 之间的数字") from exc
    if not low <= parsed <= high:
        raise ProviderConfigError(f"{label}必须是 {low:g} 到 {high:g} 之间的数字")
    return parsed


def validate_config(
    base_url: str,
    model: str,
    *,
    request_timeout: Any = 90,
    temperature: Any = 0.2,
    presence_penalty: Any = None,
) -> tuple[str, str, float, float, float | None]:
    cleaned_url = str(base_url or "").strip().rstrip("/")
    if not cleaned_url or len(cleaned_url) > 500:
        raise ProviderConfigError("API Base URL 不能为空且不能超过 500 个字符")
    if not cleaned_url.startswith(("https://", "http://")):
        raise ProviderConfigError("API Base URL 必须以 http:// 或 https:// 开头")
    cleaned_model = str(model or "").strip()
    if not cleaned_model or len(cleaned_model) > 160:
        raise ProviderConfigError("模型名不能为空且不能超过 160 个字符")
    timeout = _float_value(request_timeout, 90, "请求超时", 5, 600)
    temp = _float_value(temperature, 0.2, "temperature", 0, 2)
    penalty: float | None = None
    if presence_penalty not in {None, ""}:
        penalty = _float_value(presence_penalty, 0, "presence_penalty", -2, 2)
    return cleaned_url, cleaned_model, timeout, temp, penalty


def config_path(project_root: Path) -> Path:
    return Path(project_root).resolve() / ".research" / CONFIG_NAME


def exists(project_root: Path) -> bool:
    return config_path(project_root).is_file()


def _saved_values(project_root: Path) -> dict[str, Any]:
    path = config_path(project_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProviderConfigError(f"模型配置文件无法读取：{exc}") from exc
    if not isinstance(data, dict):
        raise ProviderConfigError("模型配置文件必须是 JSON 对象")
    return data


def _env_values() -> dict[str, Any]:
    return {
        "base_url": os.getenv("SCIDEV_API_BASE") or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
        "model": os.getenv("SCIDEV_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL,
        "request_timeout": os.getenv("SCIDEV_REQUEST_TIMEOUT_SECONDS", "90"),
        "streaming": os.getenv("SCIDEV_STREAMING", "true"),
        "text_tool_call_fallback": os.getenv("SCIDEV_TEXT_TOOL_CALL_FALLBACK", ""),
        "temperature": os.getenv("SCIDEV_TEMPERATURE", "0.2"),
        "presence_penalty": os.getenv("SCIDEV_PRESENCE_PENALTY", ""),
        "api_key": os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
    }


def load(project_root: Path, *, require_key: bool = False) -> ProviderConfig:
    saved = _saved_values(project_root)
    source = "saved" if saved else "environment"
    values = _env_values() if not saved else {
        "base_url": saved.get("base_url", DEFAULT_BASE_URL),
        "model": saved.get("model", DEFAULT_MODEL),
        "request_timeout": saved.get("request_timeout", 90),
        "streaming": saved.get("streaming", True),
        "text_tool_call_fallback": saved.get("text_tool_call_fallback", False),
        "temperature": saved.get("temperature", 0.2),
        "presence_penalty": saved.get("presence_penalty"),
        "api_key": "",
    }
    base_url, model, timeout, temperature, penalty = validate_config(
        values["base_url"],
        values["model"],
        request_timeout=values["request_timeout"],
        temperature=values["temperature"],
        presence_penalty=values["presence_penalty"],
    )
    api_key = ""
    if credentials.secure_storage_available():
        try:
            api_key = credentials.get_api_key(str(saved.get("credential_profile") or "default"))
        except credentials.CredentialError:
            api_key = ""
    if not api_key:
        api_key = str(values.get("api_key") or "").strip()
    if not api_key:
        api_key = (os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if require_key and not api_key:
        raise ProviderConfigError(
            "未配置 API Key：请在 Agent 设置的“模型接入”中保存，或设置 SCIDEV_API_KEY"
        )
    return ProviderConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        request_timeout=timeout,
        streaming=_bool_value(values.get("streaming"), True),
        text_tool_call_fallback=_bool_value(values.get("text_tool_call_fallback"), False),
        temperature=temperature,
        presence_penalty=penalty,
        source=source,
        credential_profile=str(saved.get("credential_profile") or "default"),
    )


def save(
    project_root: Path,
    *,
    base_url: str,
    model: str,
    api_key: str | None = None,
    request_timeout: Any = 90,
    temperature: Any = 0.2,
    presence_penalty: Any = None,
    streaming: bool = True,
    text_tool_call_fallback: bool = False,
    credential_profile: str = "default",
) -> ProviderConfig:
    cleaned_url, cleaned_model, timeout, temp, penalty = validate_config(
        base_url,
        model,
        request_timeout=request_timeout,
        temperature=temperature,
        presence_penalty=presence_penalty,
    )
    if api_key is not None:
        cleaned_key = api_key.strip()
        if cleaned_key:
            if not credentials.secure_storage_available():
                raise ProviderConfigError("当前系统不支持 Windows 凭据管理器，无法安全保存 API Key")
            credentials.set_api_key(cleaned_key, credential_profile)
    payload = {
        "base_url": cleaned_url,
        "model": cleaned_model,
        "request_timeout": timeout,
        "temperature": temp,
        "presence_penalty": penalty,
        "streaming": bool(streaming),
        "text_tool_call_fallback": bool(text_tool_call_fallback),
        "credential_profile": credential_profile,
    }
    _atomic_write(
        config_path(project_root),
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return load(project_root)


def key_status(project_root: Path) -> str:
    if credentials.secure_storage_available():
        try:
            if credentials.get_api_key("default"):
                return "credential"
        except credentials.CredentialError:
            pass
    if os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY"):
        return "environment"
    return "missing"


def test_connection(config: ProviderConfig, timeout: float = 15.0) -> tuple[bool, str]:
    if not config.api_key:
        return False, "未配置 API Key"
    url = f"{config.base_url.rstrip('/')}/models"
    request = urllib_request.Request(
        url,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
            "User-Agent": "SciDevHarness/provider-check",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=min(timeout, config.request_timeout)) as response:
            raw = response.read(1_000_000)
            data = json.loads(raw.decode("utf-8")) if raw else {}
    except urllib_error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        return False, f"连接失败：{exc}"
    except (ValueError, UnicodeError) as exc:
        return False, f"响应不是有效 JSON：{exc}"
    models = data.get("data") if isinstance(data, dict) else None
    count = len(models) if isinstance(models, list) else 0
    return True, f"连接成功，返回 {count} 个模型"
