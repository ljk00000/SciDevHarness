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
from urllib.parse import urlsplit

import scidev_credentials as credentials


CONFIG_NAME = "provider.json"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5"

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    model: str
    note: str = ""
    requires_key: bool = True
    local: bool = False
    models: tuple[str, ...] = ()


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "openai", "OpenAI", DEFAULT_BASE_URL, DEFAULT_MODEL,
        models=("gpt-5", "gpt-5-mini", "gpt-4.1"),
    ),
    ProviderPreset(
        "deepseek",
        "DeepSeek",
        DEEPSEEK_BASE_URL,
        "deepseek-chat",
        "官方 OpenAI 兼容接口；也支持 deepseek-reasoner。",
        models=("deepseek-chat", "deepseek-reasoner"),
    ),
    ProviderPreset(
        "anthropic",
        "Anthropic Claude",
        ANTHROPIC_BASE_URL,
        "claude-sonnet-4-5",
        "使用 Anthropic 的 OpenAI 兼容层；若账号未开放该层，可改用 OpenRouter。",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter（多模型）",
        OPENROUTER_BASE_URL,
        "anthropic/claude-sonnet-4.5",
        "一个 Key 路由 OpenAI/Claude/DeepSeek 等模型；模型名可在官网查询。",
        models=("anthropic/claude-sonnet-4.5", "openai/gpt-5", "deepseek/deepseek-chat"),
    ),
    ProviderPreset(
        "ollama",
        "Ollama 本地",
        "http://127.0.0.1:11434/v1",
        "qwen2.5-coder:7b",
        "本机 Ollama；选择后会自动探测已安装模型。",
        requires_key=False,
        local=True,
        models=("qwen2.5-coder:7b",),
    ),
    ProviderPreset(
        "lmstudio",
        "LM Studio 本地",
        "http://127.0.0.1:1234/v1",
        "",
        "LM Studio 的 OpenAI 兼容服务；选择后会自动探测模型。",
        requires_key=False,
        local=True,
    ),
    ProviderPreset(
        "custom",
        "自定义 OpenAI 兼容",
        "",
        "",
        "填写任意兼容 /v1/chat/completions 的服务地址。",
    ),
)

LOCAL_PROVIDER_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("ollama", "Ollama 本地", "http://127.0.0.1:11434/v1"),
    ("lmstudio", "LM Studio 本地", "http://127.0.0.1:1234/v1"),
    ("llamacpp", "llama.cpp 本地", "http://127.0.0.1:8080/v1"),
)


def preset_for(key: str) -> ProviderPreset:
    for preset in PROVIDER_PRESETS:
        if preset.key == key:
            return preset
    return PROVIDER_PRESETS[-1]


def is_local_provider(base_url: str) -> bool:
    try:
        host = (urlsplit(str(base_url or "")).hostname or "").casefold()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _local_models(base_url: str, timeout: float) -> list[str]:
    opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    path = "/api/tags" if root.endswith(":11434") else "/v1/models"
    request = urllib_request.Request(
        root + path,
        headers={"Accept": "application/json", "User-Agent": "SciDevHarness/local-detect"},
    )
    with opener.open(request, timeout=min(2.0, max(0.1, timeout))) as response:
        payload = json.loads(response.read(500_000).decode("utf-8"))
    if not isinstance(payload, dict):
        return []
    entries = payload.get("models") or payload.get("data") or []
    if not isinstance(entries, list):
        return []
    models: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("id") or "")
        else:
            continue
        name = name.strip()
        if name and name not in models:
            models.append(name[:160])
    return models[:100]


def detect_local_providers(timeout: float = 0.6) -> list[dict[str, Any]]:
    """Probe loopback OpenAI-compatible servers without using a proxy."""
    found: list[dict[str, Any]] = []
    for key, label, base_url in LOCAL_PROVIDER_CANDIDATES:
        try:
            models = _local_models(base_url, timeout)
        except (urllib_error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
            continue
        if models:
            found.append({
                "key": f"local-{key}",
                "label": f"{label}（{models[0]}）",
                "base_url": base_url,
                "model": models[0],
                "models": models,
                "local": True,
            })
    return found


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
    reasoning_effort: str = ""
    fallback_model: str = ""
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


def _reasoning_effort(value: Any) -> str:
    effort = str(value or "").strip().lower()
    if effort not in {"", "none", "low", "medium", "high", "max"}:
        raise ProviderConfigError("推理强度必须是空、none、low、medium、high 或 max")
    return effort


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
        "reasoning_effort": os.getenv("SCIDEV_REASONING_EFFORT", ""),
        "fallback_model": os.getenv("SCIDEV_FALLBACK_MODEL", ""),
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
        "reasoning_effort": saved.get("reasoning_effort", ""),
        "fallback_model": saved.get("fallback_model", ""),
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
    if require_key and not api_key and not is_local_provider(base_url):
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
        reasoning_effort=_reasoning_effort(values.get("reasoning_effort")),
        fallback_model=str(values.get("fallback_model") or "").strip()[:160],
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
    reasoning_effort: str = "",
    fallback_model: str = "",
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
        "reasoning_effort": _reasoning_effort(reasoning_effort),
        "fallback_model": str(fallback_model or "").strip()[:160],
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


def _provider_request(
    config: ProviderConfig,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    url = f"{config.base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Accept": "application/json",
        "User-Agent": "SciDevHarness/provider-check",
    }
    if (urlsplit(config.base_url).hostname or "").casefold() == "api.anthropic.com":
        headers["x-api-key"] = config.api_key
        headers["anthropic-version"] = "2023-06-01"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib_request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib_request.urlopen(request, timeout=min(timeout, config.request_timeout)) as response:
        raw = response.read(1_000_000)
    return json.loads(raw.decode("utf-8")) if raw else {}


def _context_length(models: Any, model: str) -> int | None:
    if not isinstance(models, list):
        return None
    for item in models:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") != model:
            continue
        for key in ("context_length", "max_context_length", "context_window", "max_input_tokens"):
            try:
                value = int(item.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        meta = item.get("meta")
        if isinstance(meta, dict):
            try:
                value = int(meta.get("n_ctx_train"))
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    return None


def test_connection(config: ProviderConfig, timeout: float = 15.0) -> tuple[bool, str]:
    if not config.api_key and not is_local_provider(config.base_url):
        return False, "未配置 API Key"
    try:
        data = _provider_request(config, "models", timeout=timeout)
    except urllib_error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        return False, f"连接失败：{exc}"
    except (ValueError, UnicodeError) as exc:
        return False, f"响应不是有效 JSON：{exc}"
    models = data.get("data") if isinstance(data, dict) else None
    count = len(models) if isinstance(models, list) else 0
    return True, f"连接成功，返回 {count} 个模型"


def probe_capabilities(config: ProviderConfig, timeout: float = 20.0) -> dict[str, Any]:
    """Probe model metadata and one tiny tool-call request.

    The tool probe intentionally sends a minimal request with a harmless
    ``ping`` schema. It costs a few tokens and only runs when the user asks.
    """
    if not config.api_key and not is_local_provider(config.base_url):
        raise ProviderConfigError("未配置 API Key")
    models_payload = _provider_request(config, "models", timeout=timeout)
    models = models_payload.get("data") if isinstance(models_payload, dict) else None
    result: dict[str, Any] = {
        "tools_supported": False,
        "context_length": _context_length(models, config.model),
        "models_count": len(models) if isinstance(models, list) else 0,
        "vision": None,
        "detail": "",
    }
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and str(item.get("id") or "") == config.model:
                capabilities = item.get("capabilities")
                if isinstance(capabilities, list):
                    lowered = {str(value).casefold() for value in capabilities}
                    if lowered & {"vision", "image", "image_input"}:
                        result["vision"] = True
                break
    body = {
        "model": config.model,
        "messages": [{
            "role": "user",
            "content": "Call the ping tool once with an empty object. Do not answer with text.",
        }],
        "tools": [{
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Return a short acknowledgement.",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        "tool_choice": "auto",
        "max_tokens": 64,
        "temperature": 0,
        "stream": False,
    }
    try:
        chat_payload = _provider_request(config, "chat/completions", body=body, timeout=timeout)
        choices = chat_payload.get("choices") if isinstance(chat_payload, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        result["tools_supported"] = bool(calls)
    except urllib_error.HTTPError as exc:
        result["detail"] = f"工具探测返回 HTTP {exc.code}: {exc.reason}"
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        result["detail"] = f"工具探测连接失败：{exc}"
    except (ValueError, UnicodeError) as exc:
        result["detail"] = f"工具探测响应不是有效 JSON：{exc}"
    return result


def format_capabilities(result: dict[str, Any]) -> str:
    tools = "支持" if result.get("tools_supported") else "未检测到"
    context = result.get("context_length")
    context_text = f"{int(context):,}" if isinstance(context, int) and context > 0 else "未报告"
    vision = result.get("vision")
    vision_text = "支持" if vision is True else ("未报告" if vision is None else "不支持")
    detail = str(result.get("detail") or "").strip()
    message = (
        f"工具调用：{tools}；上下文：{context_text}；视觉：{vision_text}；"
        f"模型数：{int(result.get('models_count') or 0)}"
    )
    if detail:
        message += f"；{detail}"
    return message
