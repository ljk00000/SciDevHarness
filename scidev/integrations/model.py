"""OpenAI-compatible model communication and direct-first HTTP transport."""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, getproxies, proxy_bypass

from scidev.runtime.errors import PermanentError, RetryableError, TaskCancelled
from scidev.runtime.helpers import MAX_SERVER_RETRY_AFTER_SECONDS, new_id


PROXY_FALLBACK_LOCAL_SUFFIXES = (
    ".localhost", ".local", ".lan", ".internal", ".intranet", ".home.arpa",
    ".test", ".invalid", ".example",
)


# Ollama's server-side default when a model's Modelfile does not declare
# ``num_ctx``. The architecture's maximum context length (``*.context_length``)
# is NOT what the server uses, so it must never be trusted as the effective
# window; doing so turns silent prompt truncation back on for those models.
OLLAMA_DEFAULT_NUM_CTX = 4096
# Keep at least a minimal generation budget after clamping output tokens to the
# remaining context window; below this a local task cannot finish anyway.
LOCAL_MIN_OUTPUT_TOKENS = 64


def urlopen(request: Request, timeout: float = 90.0):
    """Open a URL directly, ignoring ambient proxy settings on the first attempt."""
    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


def _public_proxy_fallback_target(request: Request) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(request.full_url)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").rstrip(".").casefold()
    except (AttributeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not host or host == "localhost":
        return None
    if host.endswith(PROXY_FALLBACK_LOCAL_SUFFIXES) or "." not in host:
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    try:
        if proxy_bypass(host):
            return None
    except (OSError, ValueError):
        return None
    return scheme, host


def _is_preconnect_failure(error: BaseException) -> bool:
    """Only retry via a proxy when the direct request could not reach the host."""
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, socket.gaierror):
        return True
    if not isinstance(reason, OSError):
        return False
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return False
    return (
        isinstance(reason, ConnectionRefusedError)
        or reason.errno == errno.ECONNREFUSED
        or getattr(reason, "winerror", None) == 10061  # WSAECONNREFUSED
    )


def _open_provider_request(
    request: Request,
    timeout: float,
    *,
    on_proxy_fallback: Callable[[], None] | None = None,
):
    """Try direct first; use one configured proxy only for safe, pre-connect failures."""
    try:
        return urlopen(request, timeout=timeout)
    except HTTPError:
        # A remote HTTP response proves that direct transport reached the service.
        raise
    except (URLError, TimeoutError, OSError) as direct_error:
        if not _is_preconnect_failure(direct_error):
            raise
        target = _public_proxy_fallback_target(request)
        if target is None:
            raise
        scheme, _host = target
        proxies = getproxies()
        if not (proxies.get(scheme) or proxies.get("all")):
            raise
        if on_proxy_fallback is not None:
            on_proxy_fallback()
        return build_opener(ProxyHandler(proxies)).open(request, timeout=timeout)


class OpenAICompatibleProvider:
    """OpenAI-compatible Chat Completions adapter with function/tool calling."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 90.0,
        text_tool_call_fallback: bool = False,
        streaming: bool = True,
        presence_penalty: float | None = None,
        temperature: float = 0.2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.text_tool_call_fallback = text_tool_call_fallback
        self.streaming = streaming
        if presence_penalty is not None and not -2.0 <= float(presence_penalty) <= 2.0:
            raise ValueError("presence_penalty must be between -2 and 2")
        self.presence_penalty = float(presence_penalty) if presence_penalty is not None else None
        if not 0.0 <= float(temperature) <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        self.temperature = float(temperature)
        # Set when an on_metrics callback raises; exposed for diagnostics.
        self.last_metrics_error = ""
        # Filled for loopback Ollama endpoints; requests larger than this are
        # rejected instead of being silently truncated by the server.
        self.local_context_limit: int | None = None
        self.local_context_source = ""
        # (requested, effective) when the output budget had to shrink to fit.
        self.last_output_token_clamp: tuple[int, int] | None = None

    def with_model(
        self,
        model: str,
        *,
        streaming: bool | None = None,
        temperature: float | None = None,
    ) -> "OpenAICompatibleProvider":
        effective_model = model.strip() or self.model
        provider = type(self)(
            self.base_url,
            self.api_key,
            effective_model,
            self.timeout,
            self.text_tool_call_fallback,
            self.streaming if streaming is None else streaming,
            self.presence_penalty,
            self.temperature if temperature is None else temperature,
        )
        if effective_model == self.model:
            provider.local_context_limit = self.local_context_limit
            provider.local_context_source = self.local_context_source
        else:
            # Summary/acceptance/vision models can be a different local tag with
            # its own num_ctx; reusing the main model's window would either
            # block valid requests or let Ollama truncate the new model's prompt.
            provider._detect_local_context_limit()
        return provider

    @staticmethod
    def _loopback_endpoint(base_url: str) -> tuple[str, str, int] | None:
        parsed = urlsplit(base_url)
        host = (parsed.hostname or "").casefold()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return None
        scheme = parsed.scheme or "http"
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, host, port

    def _detect_local_context_limit(self, *, timeout: float = 3.0) -> None:
        """Resolve the effective context window Ollama will use for this model.

        Ollama applies, in order: an explicit Modelfile ``num_ctx`` parameter,
        the loaded runtime window reported by ``/api/ps``, and its built-in
        default of 4096 tokens. The model's architecture maximum
        (``*.context_length``) is deliberately ignored: without ``num_ctx`` the
        server does not use it, so trusting it disables the truncation guard.
        """
        self.local_context_limit = None
        self.local_context_source = ""
        endpoint = self._loopback_endpoint(self.base_url)
        if endpoint is None:
            return
        scheme, host, port = endpoint
        base = f"{scheme}://{host}:{port}"
        show = self._ollama_json(
            f"{base}/api/show", {"model": self.model}, timeout=timeout
        )
        if not isinstance(show, dict):
            # Not an Ollama endpoint (or it is down); do not impose a window on
            # unrelated local OpenAI-compatible servers.
            return
        limit = self._ollama_num_ctx(show)
        source = "ollama_api"
        if limit <= 0:
            status = self._ollama_json(f"{base}/api/ps", None, timeout=timeout)
            limit = self._ollama_runtime_context(status)
            source = "ollama_runtime"
        if limit <= 0:
            limit = OLLAMA_DEFAULT_NUM_CTX
            source = "ollama_default"
        self.local_context_limit = limit
        self.local_context_source = source

    @classmethod
    def _ollama_json(
        cls,
        url: str,
        body: dict[str, Any] | None,
        *,
        timeout: float,
    ) -> Any:
        """Best-effort Ollama metadata call; returns None instead of raising."""
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            request = Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST" if body is not None else "GET",
            )
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - metadata is optional; never block chat.
            return None

    @staticmethod
    def _ollama_num_ctx(show: Any) -> int:
        """Read an explicit ``num_ctx`` from ``/api/show`` parameters, if any."""
        if not isinstance(show, dict):
            return 0
        parameters = show.get("parameters")
        if isinstance(parameters, dict):
            try:
                return max(0, int(parameters.get("num_ctx") or 0))
            except (TypeError, ValueError):
                return 0
        if isinstance(parameters, str):
            for line in parameters.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].casefold() == "num_ctx":
                    try:
                        return max(0, int(parts[1]))
                    except ValueError:
                        return 0
        return 0

    def _ollama_runtime_context(self, status: Any) -> int:
        """Read the loaded model's effective window from ``/api/ps``."""
        models = status.get("models") if isinstance(status, dict) else None
        if not isinstance(models, list):
            return 0
        wanted = self.model.strip().casefold()
        wanted_base = wanted.split(":", 1)[0]
        for entry in models:
            if not isinstance(entry, dict):
                continue
            names = {
                str(entry.get("name") or "").strip().casefold(),
                str(entry.get("model") or "").strip().casefold(),
            }
            names.discard("")
            if wanted not in names and wanted_base not in {name.split(":", 1)[0] for name in names}:
                continue
            try:
                return max(0, int(entry.get("context_length") or 0))
            except (TypeError, ValueError):
                return 0
        return 0

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        """Rough token estimate that does not undercount CJK text.

        ASCII averages ~4 characters per token, while Chinese/Japanese/Korean
        text is close to one token per character; a flat ``len // 4`` estimate
        understates a Chinese prompt by ~4x and would let Ollama truncate it.
        """
        if not text:
            return 0
        ascii_chars = len(text.encode("ascii", "ignore"))
        return ascii_chars // 4 + (len(text) - ascii_chars) + 1

    def _guard_local_context(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        """Fail before Ollama can truncate, and fit output into the window.

        Ollama counts prompt + generated tokens against ``num_ctx`` and drops
        the oldest prompt tokens when they do not fit, so a request that looks
        fine can silently lose its instructions. Oversized prompts are refused;
        an output budget larger than the remaining window is clamped instead of
        rejecting every task on models with a small default window.
        """
        limit = self.local_context_limit
        if not limit:
            return max_tokens
        prompt_tokens = 64
        for message in messages:
            if not isinstance(message, dict):
                continue
            prompt_tokens += self._estimate_text_tokens(str(message.get("content") or ""))
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    prompt_tokens += self._estimate_text_tokens(
                        str(function.get("arguments") or "")
                    )
        if tools:
            try:
                prompt_tokens += self._estimate_text_tokens(
                    json.dumps(tools, ensure_ascii=False)
                )
            except (TypeError, ValueError):
                pass
        window = int(limit * 0.95)
        if prompt_tokens >= window:
            raise PermanentError(
                f"本地模型上下文窗口约 {limit} tokens，本次提示词（含工具定义）约 {prompt_tokens} tokens，"
                "超过可用窗口；为避免 Ollama 静默截断提示词，请提高模型的 num_ctx"
                "（例如 Modelfile: `PARAMETER num_ctx 32768`），或缩短任务描述/上下文后重试。"
            )
        available = window - prompt_tokens
        if available < LOCAL_MIN_OUTPUT_TOKENS:
            raise PermanentError(
                f"本地模型上下文窗口约 {limit} tokens，提示词已占用 {prompt_tokens} tokens，"
                f"剩余不足 {LOCAL_MIN_OUTPUT_TOKENS} tokens 用于生成；请提高 num_ctx 或缩短提示词。"
            )
        if max_tokens <= available:
            return max_tokens
        self.last_output_token_clamp = (int(max_tokens), int(available))
        return available

    @classmethod
    def from_env(cls) -> "OpenAICompatibleProvider":
        api_key = (os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        base_url = (
            os.getenv("SCIDEV_API_BASE")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip()
        model = (os.getenv("SCIDEV_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5").strip()
        if not api_key:
            raise PermanentError("未配置 SCIDEV_API_KEY 或 OPENAI_API_KEY")
        raw_timeout = os.getenv("SCIDEV_REQUEST_TIMEOUT_SECONDS", "90").strip()
        try:
            request_timeout = float(raw_timeout)
        except ValueError as exc:
            raise PermanentError("SCIDEV_REQUEST_TIMEOUT_SECONDS 必须是 5 到 600 秒之间的数字") from exc
        if not 5 <= request_timeout <= 600:
            raise PermanentError("SCIDEV_REQUEST_TIMEOUT_SECONDS 必须是 5 到 600 秒之间的数字")
        raw_streaming = os.getenv("SCIDEV_STREAMING", "true").strip().lower()
        if raw_streaming not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise PermanentError("SCIDEV_STREAMING 必须是 true/false")
        streaming = raw_streaming in {"1", "true", "yes", "on"}
        text_tool_call_fallback = os.getenv("SCIDEV_TEXT_TOOL_CALL_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        raw_presence_penalty = os.getenv("SCIDEV_PRESENCE_PENALTY", "").strip()
        presence_penalty = None
        if raw_presence_penalty:
            try:
                presence_penalty = float(raw_presence_penalty)
            except ValueError as exc:
                raise PermanentError("SCIDEV_PRESENCE_PENALTY 必须是 -2 到 2 之间的数字") from exc
            if not -2.0 <= presence_penalty <= 2.0:
                raise PermanentError("SCIDEV_PRESENCE_PENALTY 必须是 -2 到 2 之间的数字")
        raw_temperature = os.getenv("SCIDEV_TEMPERATURE", "0.2").strip()
        try:
            temperature = float(raw_temperature)
        except ValueError as exc:
            raise PermanentError("SCIDEV_TEMPERATURE 必须是 0 到 2 之间的数字") from exc
        if not 0.0 <= temperature <= 2.0:
            raise PermanentError("SCIDEV_TEMPERATURE 必须是 0 到 2 之间的数字")
        provider = cls(
            base_url,
            api_key,
            model,
            timeout=request_timeout,
            text_tool_call_fallback=text_tool_call_fallback,
            streaming=streaming,
            presence_penalty=presence_penalty,
            temperature=temperature,
        )
        provider._detect_local_context_limit()
        return provider

    @staticmethod
    def _tool_arguments_match_schema(
        value: Any,
        schema: dict[str, Any],
        *,
        path: str = "$.arguments",
        error_out: list[str] | None = None,
    ) -> bool:
        """Validate the JSON Schema subset used by declared coding tools, recursively."""
        def reject(reason: str) -> bool:
            if error_out is not None and not error_out:
                error_out.append(reason[:180])
            return False

        def child_path(parent: str, key: Any) -> str:
            name = str(key)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,47}", name):
                return f"{parent}.{name}"
            return f"{parent}.[key]"

        value_type = schema.get("type")
        if isinstance(value_type, list):
            for candidate_type in value_type:
                candidate_errors: list[str] = []
                if OpenAICompatibleProvider._tool_arguments_match_schema(
                    value,
                    {**schema, "type": candidate_type},
                    path=path,
                    error_out=candidate_errors,
                ):
                    return True
            expected = "|".join(str(item) for item in value_type[:6])
            return reject(f"type_mismatch at {path} (expected {expected})")
        elif value_type == "object":
            if not isinstance(value, dict):
                return reject(f"type_mismatch at {path} (expected object)")
            properties = schema.get("properties") or {}
            if not isinstance(properties, dict):
                return reject(f"invalid_schema at {path}")
            missing_keys = sorted(set(schema.get("required") or ()) - value.keys())
            if missing_keys:
                return reject(f"missing_required at {child_path(path, missing_keys[0])}")
            extra_keys = sorted(value.keys() - properties.keys(), key=str)
            additional_schema = schema.get("additionalProperties")
            if extra_keys and additional_schema is not True and not isinstance(additional_schema, dict):
                return reject(f"unexpected_property at {child_path(path, extra_keys[0])}")
            if isinstance(additional_schema, dict):
                for key in extra_keys:
                    if not OpenAICompatibleProvider._tool_arguments_match_schema(
                        value[key],
                        additional_schema,
                        path=child_path(path, key),
                        error_out=error_out,
                    ):
                        return False
            for key, property_schema in properties.items():
                if key in value and isinstance(property_schema, dict):
                    if not OpenAICompatibleProvider._tool_arguments_match_schema(
                        value[key],
                        property_schema,
                        path=child_path(path, key),
                        error_out=error_out,
                    ):
                        return False
        elif value_type == "array":
            if not isinstance(value, list):
                return reject(f"type_mismatch at {path} (expected array)")
            if len(value) < int(schema.get("minItems", 0)):
                return reject(f"min_items at {path}")
            max_items = schema.get("maxItems")
            if max_items is not None and len(value) > int(max_items):
                return reject(f"max_items at {path}")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    if not OpenAICompatibleProvider._tool_arguments_match_schema(
                        item,
                        item_schema,
                        path=f"{path}[{index}]",
                        error_out=error_out,
                    ):
                        return False
        elif value_type == "string":
            if not isinstance(value, str):
                return reject(f"type_mismatch at {path} (expected string)")
            if len(value) < int(schema.get("minLength", 0)):
                return reject(f"min_length at {path}")
            max_length = schema.get("maxLength")
            if max_length is not None and len(value) > int(max_length):
                return reject(f"max_length at {path}")
        elif value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return reject(f"type_mismatch at {path} (expected integer)")
        elif value_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return reject(f"type_mismatch at {path} (expected number)")
        elif value_type == "boolean":
            if not isinstance(value, bool):
                return reject(f"type_mismatch at {path} (expected boolean)")
        elif value_type == "null" and value is not None:
            return reject(f"type_mismatch at {path} (expected null)")

        if "enum" in schema and value not in schema["enum"]:
            return reject(f"enum_mismatch at {path}")
        return True

    @staticmethod
    def _coerce_text_tool_calls(
        message: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Normalize explicitly tagged or fenced Qwen-style tool JSON when native calls are absent."""
        content = message.get("content")
        if not isinstance(content, str) or not content:
            return message

        allowed: dict[str, dict[str, Any]] = {}
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict) or not function.get("name"):
                continue
            parameters = function.get("parameters") or {}
            allowed[str(function["name"])] = parameters if isinstance(parameters, dict) else {}
        if not allowed:
            return message

        # Models sometimes copy read_file's display line numbers onto both
        # tool wrapper tags and JSON lines. Strip only that exact visual prefix
        # while parsing; the original assistant text is retained on failure.
        parse_content = re.sub(r"(?m)^[ \t]*\d+[ \t]*:[ \t]?", "", content)

        patterns = (
            (
                re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.IGNORECASE | re.DOTALL),
                False,
                True,
            ),
            (
                re.compile(r"<tool_request>\s*(.*?)\s*</tool_request>", re.IGNORECASE | re.DOTALL),
                False,
                True,
            ),
            # Some Ollama templates surface a line-numbered tool envelope using
            # this wrapper. Accept it only when the entire body is still a
            # schema-valid declared tool call below.
            (
                re.compile(r"\A\s*<tool_response>\s*(.*?)\s*</tool_response>\s*\Z", re.IGNORECASE | re.DOTALL),
                True,
                True,
            ),
            (re.compile(r"```(?:json|xml)\s*(.*?)```", re.IGNORECASE | re.DOTALL), False, True),
            # Some local models omit the requested wrapper. Only accept a JSON
            # object that occupies the entire response; prose/code examples
            # must never be promoted to executable workspace actions.
            (re.compile(r"\A\s*(\{.*\})\s*\Z", re.DOTALL), False, False),
        )
        matches = sorted(
            (
                (match, strip_line_numbers, allow_multiple)
                for pattern, strip_line_numbers, allow_multiple in patterns
                for match in pattern.finditer(parse_content)
            ),
            key=lambda item: item[0].start(),
        )

        def parse_payload_sequence(text: str) -> list[Any] | None:
            """Read several JSON objects separated by whitespace/commas."""
            decoder = json.JSONDecoder()
            payloads: list[Any] = []
            index = 0
            length = len(text)
            while index < length:
                while index < length and text[index] in " \t\r\n,":
                    index += 1
                if index >= length:
                    break
                try:
                    payload, index = decoder.raw_decode(text, index)
                except json.JSONDecodeError:
                    return None
                payloads.append(payload)
            return payloads if len(payloads) >= 2 else None

        def build_call(
            payload: Any,
        ) -> tuple[dict[str, Any] | None, str, str, str]:
            """Validate one textual envelope; return (call, name, reason, detail)."""
            if (
                not isinstance(payload, dict)
                or set(payload) != {"name", "arguments"}
                or not isinstance(payload.get("name"), str)
            ):
                attempted_name = payload.get("name") if isinstance(payload, dict) else None
                if isinstance(attempted_name, str) and attempted_name in allowed:
                    return (
                        None,
                        attempted_name,
                        "invalid_envelope",
                        "envelope_must_contain_only_name_and_arguments",
                    )
                return None, "", "", ""
            name = payload["name"]
            if name not in allowed:
                return None, "", "", ""
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    return (
                        None,
                        name,
                        "invalid_arguments_json",
                        f"invalid_arguments_json at character {exc.pos}: {exc.msg}"[:180],
                    )
            if not isinstance(arguments, dict):
                return None, name, "arguments_not_object", "arguments_must_be_an_object"
            schema_errors: list[str] = []
            if not OpenAICompatibleProvider._tool_arguments_match_schema(
                arguments,
                allowed[name],
                error_out=schema_errors,
            ):
                return (
                    None,
                    name,
                    "arguments_schema_mismatch",
                    schema_errors[0] if schema_errors else "schema_mismatch",
                )
            return (
                {
                    "id": new_id("call"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                },
                "",
                "",
                "",
            )

        tool_calls: list[dict[str, Any]] = []
        consumed_spans: list[tuple[int, int]] = []
        rejected_tool_name = ""
        rejected_reason = ""
        rejected_detail = ""
        for match, strip_line_numbers, allow_multiple in matches:
            candidate = match.group(1).strip()
            if strip_line_numbers:
                candidate = re.sub(r"(?m)^[ \t]*\d+[ \t]*:[ \t]?", "", candidate)
            payloads: list[Any] | None = None
            try:
                payloads = [json.loads(candidate)]
            except json.JSONDecodeError as exc:
                attempted_name = re.search(r'"name"\s*:\s*"([^"\\]+)"', candidate)
                if attempted_name and attempted_name.group(1) in allowed:
                    rejected_tool_name = attempted_name.group(1)
                    rejected_reason = "invalid_json"
                    rejected_detail = f"invalid_json at character {exc.pos}: {exc.msg}"[:180]
                # Local models occasionally append one redundant closing brace
                # to an otherwise valid tool envelope. Recover only a valid
                # JSON prefix followed by exactly that one character; tool and
                # argument allowlists below still decide whether it can run.
                try:
                    payload, end = json.JSONDecoder().raw_decode(candidate)
                except json.JSONDecodeError as decode_error:
                    if attempted_name and attempted_name.group(1) in allowed:
                        rejected_detail = (
                            f"invalid_json at character {decode_error.pos}: {decode_error.msg}"
                        )[:180]
                    payloads = parse_payload_sequence(candidate) if allow_multiple else None
                except TypeError:
                    payloads = None
                else:
                    trailing = candidate[end:].strip()
                    if trailing == "}":
                        payloads = [payload]
                    elif allow_multiple:
                        # A weak model may place several objects inside one
                        # <tool_call> wrapper; accept the batch only if every
                        # envelope validates below.
                        payloads = parse_payload_sequence(candidate)
                        if payloads is None and attempted_name and attempted_name.group(1) in allowed:
                            rejected_reason = "invalid_json"
                            rejected_detail = "unexpected_trailing_content"
            except TypeError:
                payloads = None
            if not payloads:
                continue
            built = [build_call(item) for item in payloads]
            if any(call is None for call, *_rest in built):
                for call, name, reason, detail in built:
                    if call is None and name:
                        rejected_tool_name = name
                        rejected_reason = reason
                        rejected_detail = detail
                        break
                continue
            tool_calls.extend(
                call for call, *_rest in built if call is not None
            )
            consumed_spans.append((match.start(), match.end()))

        if not tool_calls and "apply_patch" in allowed:
            # Weak local models often print the exact apply_patch envelope instead
            # of making a native tool call. Recover only a complete, explicitly
            # delimited patch that names at least one file; the normal patch
            # parser, path checks and human review still decide whether it runs.
            patch_match = re.search(
                r"(?ms)^[ \t]*\*\*\* Begin Patch[ \t]*\r?\n"
                r".*?^[ \t]*\*\*\* (?:Update|Add|Delete) File:.*?"
                r"^[ \t]*\*\*\* End Patch[ \t]*$",
                parse_content,
            )
            if patch_match:
                patch_text = patch_match.group(0)
                call, name, reason, detail = build_call({
                    "name": "apply_patch",
                    "arguments": {"patch": patch_text},
                })
                if call is not None:
                    tool_calls.append(call)
                    consumed_spans.append((patch_match.start(), patch_match.end()))
                else:
                    rejected_tool_name = name or "apply_patch"
                    rejected_reason = reason or "invalid_patch_envelope"
                    rejected_detail = detail or "patch_envelope_failed_schema_validation"
            elif re.search(r"(?ms)^[ \t]*\*\*\* Begin Patch[ \t]*$", parse_content):
                rejected_tool_name = "apply_patch"
                rejected_reason = "incomplete_or_invalid_tool_envelope"
                rejected_detail = "patch_block_missing_end_or_file_header"

        if not tool_calls:
            tool_markup_rejected = False
            if not rejected_tool_name:
                # A truncated JSON envelope may not match the complete-object
                # regex above. Recover only the name of a declared function
                # from an unmistakable tool/JSON response shape; never execute
                # this content or treat arbitrary prose examples as calls.
                tool_shaped = re.match(
                    r"\s*(?:<tool_(?:call|request|response)>|```(?:json|xml)\b|\{)",
                    parse_content,
                    re.IGNORECASE,
                )
                attempted_name = re.search(r'"name"\s*:\s*"([^"\\]+)"', parse_content)
                if tool_shaped and attempted_name and attempted_name.group(1) in allowed:
                    rejected_tool_name = attempted_name.group(1)
                    rejected_reason = "incomplete_or_invalid_tool_envelope"
                    if not rejected_detail:
                        rejected_detail = "tool_call_not_a_complete_json_envelope"
                elif tool_shaped:
                    # Weak models sometimes echo a tool result (or a truncated
                    # envelope) instead of calling a tool. Accepting that as the
                    # final answer silently "completes" the task without any
                    # action, so reject it and ask for a proper call.
                    rejected_reason = "incomplete_or_invalid_tool_envelope"
                    rejected_detail = "tool_markup_without_a_declared_tool_call"
                    tool_markup_rejected = True
            if rejected_tool_name or tool_markup_rejected:
                rejected = dict(message)
                rejected["tool_parse_error"] = {
                    "name": rejected_tool_name,
                    "reason": rejected_reason or "invalid_tool_envelope",
                }
                if rejected_detail:
                    rejected["tool_parse_error"]["detail"] = rejected_detail
                return rejected
            return message
        remaining = parse_content
        for start, end in reversed(consumed_spans):
            remaining = remaining[:start] + remaining[end:]
        normalized = dict(message)
        normalized["content"] = remaining.strip()
        normalized["tool_calls"] = tool_calls
        return normalized

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    @staticmethod
    def _retry_after_seconds(value: Any) -> float | None:
        """Parse standard Retry-After delta-seconds/date, clamped to one hour."""
        text = str(value or "").strip()
        if not text:
            return None
        if re.fullmatch(r"\d+", text):
            return min(float(text), MAX_SERVER_RETRY_AFTER_SECONDS)
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return min(MAX_SERVER_RETRY_AFTER_SECONDS, max(0.0, delay))

    @staticmethod
    def _stream_message(
        response: Any,
        on_delta: Callable[[str], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        on_bytes: Callable[[int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Assemble an OpenAI-compatible SSE response, including fragmented tool calls.

        ``should_cancel`` is polled between SSE lines so a user pressing Stop
        does not have to wait for a long local generation to finish.
        """
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        saw_done = False
        saw_finish = False
        for raw_line in response:
            if should_cancel is not None and should_cancel():
                raise TaskCancelled("任务已取消，已停止等待模型继续生成。")
            if on_bytes:
                on_bytes(len(raw_line) if isinstance(raw_line, bytes) else len(str(raw_line).encode("utf-8")))
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
            line = line.strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as exc:
                raise PermanentError("Provider returned malformed SSE JSON") from exc
            if isinstance(chunk, dict) and chunk.get("error"):
                raise RetryableError(f"Provider stream error: {str(chunk['error'])[:500]}")
            usage = chunk.get("usage") if isinstance(chunk, dict) else None
            if on_usage and isinstance(usage, dict):
                on_usage(usage)
            choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    saw_finish = True
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content_parts.append(text)
                    if on_delta:
                        on_delta(text)
                elif isinstance(text, list):
                    for part in text:
                        if not isinstance(part, dict) or part.get("type") not in {"text", "output_text"}:
                            continue
                        piece = str(part.get("text", ""))
                        if piece:
                            content_parts.append(piece)
                            if on_delta:
                                on_delta(piece)

                raw_tool_calls = delta.get("tool_calls") or []
                if not isinstance(raw_tool_calls, list):
                    continue
                for position, fragment in enumerate(raw_tool_calls):
                    if not isinstance(fragment, dict):
                        continue
                    try:
                        index = int(fragment.get("index", position))
                    except (TypeError, ValueError):
                        index = position
                    target = tool_parts.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if fragment.get("id"):
                        target["id"] = str(fragment["id"])
                    if fragment.get("type"):
                        target["type"] = str(fragment["type"])
                    function = fragment.get("function")
                    if isinstance(function, dict):
                        if function.get("name"):
                            target["function"]["name"] += str(function["name"])
                        if function.get("arguments") is not None:
                            target["function"]["arguments"] += str(function["arguments"])

        if not saw_done and not saw_finish:
            raise RetryableError("Provider stream ended before a completion marker")
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if tool_parts:
            message["tool_calls"] = [
                {
                    **tool_parts[index],
                    "id": tool_parts[index]["id"] or new_id("call"),
                }
                for index in sorted(tool_parts)
            ]
        return message

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 12000,
        request_id: str = "",
        on_delta: Callable[[str], None] | None = None,
        tool_choice: str = "auto",
        reasoning_effort: str | None = None,
        on_metrics: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        max_tokens = self._guard_local_context(messages, max_tokens, tools)
        body_data: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "stream": self.streaming,
        }
        if self.presence_penalty is not None:
            body_data["presence_penalty"] = self.presence_penalty
        if reasoning_effort is not None:
            effort = str(reasoning_effort).strip().lower()
            if effort not in {"none", "low", "medium", "high", "max"}:
                raise PermanentError("reasoning_effort must be one of none, low, medium, high, max")
            body_data["reasoning_effort"] = effort
        if tools:
            body_data["tools"] = tools
            if tool_choice not in {"auto", "required"}:
                raise PermanentError("tool_choice must be 'auto' or 'required'")
            body_data["tool_choice"] = tool_choice
        if self.text_tool_call_fallback and tools:
            request_messages = [dict(message) for message in messages]
            protocol = (
                "\n\nLocal tool compatibility mode: you MUST use the declared tools for workspace actions. "
                "Never claim an action is complete until you receive that tool's result. If native tool calls "
                "are unavailable, emit exactly <tool_call>{\"name\":\"declared_function_name\","
                "\"arguments\":{...}}</tool_call> with valid JSON, outside Markdown fences."
            )
            system_message = next(
                (message for message in request_messages if message.get("role") == "system"),
                None,
            )
            if system_message is None:
                request_messages.insert(0, {"role": "system", "content": protocol.strip()})
            else:
                system_message["content"] = f"{system_message.get('content') or ''}{protocol}"
            body_data["messages"] = request_messages
        body = json.dumps(
            body_data,
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if (urlsplit(self._endpoint()).hostname or "").casefold() == "api.anthropic.com":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        if request_id:
            # The logical request ID stays stable across an identical retry, while
            # the body digest prevents one reused turn ID from deduplicating a
            # different prompt/transcript after task recovery or session reuse.
            request_fingerprint = hashlib.sha256(request_id.encode("utf-8") + b"\0" + body).hexdigest()
            headers["Idempotency-Key"] = f"scidev-{request_fingerprint}"
        request = Request(
            self._endpoint(),
            data=body,
            headers=headers,
            method="POST",
        )
        metrics: dict[str, Any] = {
            "request_body_bytes": len(body),
            "response_body_bytes": 0,
            "response_body_complete": False,
            "success": False,
            "status_code": None,
            "transport_route": "direct",
        }
        usage_data: dict[str, Any] = {}
        started = time.monotonic()

        def add_response_bytes(count: int) -> None:
            metrics["response_body_bytes"] += max(0, int(count))

        def save_usage(value: dict[str, Any]) -> None:
            usage_data.update(value)

        try:
            try:
                with _open_provider_request(
                    request,
                    timeout=self.timeout,
                    on_proxy_fallback=lambda: metrics.update({"transport_route": "proxy_fallback"}),
                ) as response:
                    status = getattr(response, "status", None)
                    if status is None and callable(getattr(response, "getcode", None)):
                        status = response.getcode()
                    metrics["status_code"] = status
                    if self.streaming:
                        buffer_text_tool_response = self.text_tool_call_fallback and bool(tools)
                        message = self._stream_message(
                            response,
                            on_delta=None if buffer_text_tool_response else on_delta,
                            on_usage=save_usage,
                            on_bytes=add_response_bytes,
                            should_cancel=should_cancel,
                        )
                        metrics["response_body_complete"] = True
                    else:
                        response_body = response.read()
                        add_response_bytes(len(response_body))
                        metrics["response_body_complete"] = True
                        data = json.loads(response_body.decode("utf-8"))
                        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                            save_usage(data["usage"])
            except HTTPError as exc:
                metrics["status_code"] = exc.code
                try:
                    error_body = exc.read(4096)
                finally:
                    exc.close()
                add_response_bytes(len(error_body))
                metrics["response_body_complete"] = len(error_body) < 4096
                detail = error_body.decode("utf-8", errors="replace")[:500]
                if exc.code == 429 or exc.code >= 500:
                    retry_after = self._retry_after_seconds(
                        exc.headers.get("Retry-After") if exc.headers is not None else None
                    )
                    raise RetryableError(
                        f"HTTP {exc.code}: {detail}", retry_after_seconds=retry_after,
                    ) from exc
                raise PermanentError(f"HTTP {exc.code}: {detail}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                raise RetryableError(f"网络连接失败：{exc}") from exc
            if self.streaming:
                if self.text_tool_call_fallback and tools and not message.get("tool_calls"):
                    message = self._coerce_text_tool_calls(message, tools)
                    # Buffer text-tool responses until parsing finishes so invalid
                    # JSON is not streamed into the chat UI as if it were an answer.
                    if (
                        on_delta
                        and not message.get("tool_parse_error")
                        and isinstance(message.get("content"), str)
                        and message["content"]
                    ):
                        on_delta(message["content"])
            else:
                try:
                    message = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise PermanentError("Provider 返回格式不包含 choices[0].message") from exc
                if not isinstance(message, dict):
                    raise PermanentError("Provider 返回的 message 格式无效")
                if self.text_tool_call_fallback and tools and not message.get("tool_calls"):
                    message = self._coerce_text_tool_calls(message, tools)
            metrics["success"] = True
            return message
        finally:
            metrics["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
            metrics["model"] = self.model
            for key, alternatives in (
                ("prompt_tokens", ("prompt_tokens", "input_tokens")),
                ("completion_tokens", ("completion_tokens", "output_tokens")),
                ("total_tokens", ("total_tokens",)),
            ):
                value = next((usage_data.get(name) for name in alternatives if usage_data.get(name) is not None), None)
                if value is not None:
                    metrics[key] = value
            if on_metrics is not None:
                try:
                    on_metrics(dict(metrics))
                except Exception as exc:  # noqa: BLE001 - caller telemetry is untrusted
                    # Telemetry must never change provider success/failure
                    # semantics, but keep the reason inspectable.
                    self.last_metrics_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    def call(self, prompt: str, max_tokens: int = 12000) -> str:
        """Compatibility helper for a plain, non-tool request."""
        message = self.chat([{"role": "user", "content": prompt}], max_tokens=max_tokens)
        content = message.get("content") or ""
        return str(content)
