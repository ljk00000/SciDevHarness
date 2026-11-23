using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed class OpenAiSseModel(HttpClient client, Uri endpoint, string model, int maxTokens, bool dockerCommands = false,
    ExecutionMode executionMode = ExecutionMode.WorkspaceApproval, bool outsideApproval = false,
    Action<RunEvent>? record = null, bool networkApproval = false, PlanningMode planning = PlanningMode.Off,
    bool gitApproval = false, bool hostCommandApproval = false, bool legacyCommandArguments = false, TimeSpan? requestTimeout = null,
    IReadOnlyList<JsonElement>? mcpTools = null, bool legacyLocalCommands = false, bool textToolCallFallback = false) : IModel
{
    private const int MaxStreamChars = 4 * 1024 * 1024;
    private const int MaxRetries = 2;
    private readonly object _usageLock = new();
    private ModelUsage? _usage;

    internal ModelUsage? Usage
    {
        get { lock (_usageLock) return _usage; }
    }

    private static readonly JsonNode ReadOnlyTools = WorkspaceReadToolCatalog.ModelTools();
    private static readonly JsonNode Tools = CreateWorkspaceTools();
    private static readonly JsonNode DockerTools = CreateDockerTools();
    private static readonly JsonNode OutsideTools = AddOutsideTools(Tools);
    private static readonly JsonNode DockerAndOutsideTools = AddOutsideTools(DockerTools);

    private static JsonNode CreateWorkspaceTools()
    {
        var tools = ReadOnlyTools.DeepClone().AsArray();
        var edits = JsonNode.Parse("""
        [
          {"type":"function","function":{"name":"write_file","description":"Create a new UTF-8 text file at a workspace-relative path. Never overwrite an existing file.","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
          {"type":"function","function":{"name":"replace_in_file","description":"Edit a file created by this task. Use either old_text/new_text for a single exact edit or edits for a batch of exact edits saved together.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"},"edits":{"type":"array","maxItems":32,"items":{"type":"object","properties":{"old_text":{"type":"string"},"new_text":{"type":"string"}},"required":["old_text","new_text"]}}},"required":["path"]}}},
          {"type":"function","function":{"name":"apply_patch","description":"Propose one exact, unique replacement in an existing UTF-8 file. The user reviews and approves the exact change before it is applied.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"}},"required":["path","old_text","new_text"]}}},
          {"type":"function","function":{"name":"rename_file","description":"Move one file to a vacant name within the workspace after approval. Both parent directories must already exist. Never overwrites a file.","parameters":{"type":"object","properties":{"path":{"type":"string"},"destination":{"type":"string"}},"required":["path","destination"]}}}
        ]
        """)!.AsArray();
        foreach (var edit in edits)
            tools.Add(edit!.DeepClone());
        return tools;
    }

    private JsonNode SelectTools()
    {
        var selected = executionMode == ExecutionMode.ReadOnly ? ReadOnlyTools : (dockerCommands, outsideApproval) switch
        {
            (true, true) => DockerAndOutsideTools,
            (true, false) => DockerTools,
            (false, true) => OutsideTools,
            _ => Tools,
        };
        var legacyDocker = legacyCommandArguments && dockerCommands && executionMode != ExecutionMode.ReadOnly;
        var legacyHost = legacyCommandArguments && hostCommandApproval && executionMode == ExecutionMode.WorkspaceApproval;
        var legacyLocal = legacyLocalCommands && legacyCommandArguments && hostCommandApproval && executionMode == ExecutionMode.FullAccess;
        if (!networkApproval && !gitApproval && !hostCommandApproval && !legacyDocker && planning == PlanningMode.Off &&
            mcpTools is not { Count: > 0 })
            return selected;

        var tools = selected.DeepClone().AsArray();
        if (legacyDocker)
        {
            var function = tools.Select(item => item!["function"]!).Single(item => item["name"]!.GetValue<string>() == "run_command");
            function["description"] = "Run a command in a disposable offline Docker copy. Supply exactly one of argv or the legacy command string. " +
                "Use argv for new tasks. " + (legacyHost ? "Legacy command with outside_workspace=true requires the host reviews." :
                    "Host execution uses the separate run_host_command approval tool.");
            function["parameters"] = JsonNode.Parse("""
                {"type":"object","properties":{"argv":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":64},
                "command":{"type":"string","maxLength":4096},"directory":{"type":"string","default":"."},
                "outside_workspace":{"type":"boolean"},
                "timeout_seconds":{"type":"integer","minimum":1,"maximum":300,"description":"For the legacy command string."}},"additionalProperties":false}
                """);
            function["parameters"]!["properties"]!["outside_workspace"]!["description"] = legacyHost
                ? "Set true only for a specifically requested and separately approved legacy host command."
                : "Legacy command field. Only false is accepted.";
        }
        else if (legacyHost || legacyLocal)
        {
            var legacyTool = JsonNode.Parse("""
                {"type":"function","function":{"name":"run_command","description":"Legacy Windows shell request. Requires separate approvals for executable inspection, execution and sharing the output. Use run_host_command for new tasks.","parameters":{"type":"object","properties":{"command":{"type":"string","maxLength":4096},"outside_workspace":{"type":"boolean","enum":[true]},"directory":{"type":"string","default":"."},"timeout_seconds":{"type":"integer","minimum":1,"maximum":300}},"required":["command","outside_workspace"],"additionalProperties":false}}}
                """)!;
            if (legacyLocal)
            {
                var parameters = legacyTool["function"]!["parameters"]!;
                parameters["required"] = new JsonArray("command");
                parameters["properties"]!["outside_workspace"]!["enum"] = new JsonArray(JsonValue.Create(false));
            }
            tools.Add(legacyTool);
        }
        if (planning != PlanningMode.Off)
            tools.Add(JsonNode.Parse("""
                {"type":"function","function":{"name":"update_plan","description":"Replace the visible plan with its full current list of steps. Explain any removals, reopened steps, or newly skipped steps. Set completed only after doing the work.","parameters":{"type":"object","properties":{"plan":{"type":"array","minItems":1,"maxItems":12,"items":{"type":"object","properties":{"step":{"type":"string","minLength":1,"maxLength":240},"status":{"type":"string","enum":["pending","in_progress","completed","blocked","skipped"]}},"required":["step","status"],"additionalProperties":false}},"explanation":{"type":"string","maxLength":2000}},"required":["plan"],"additionalProperties":false}}}
                """));
        if (networkApproval && executionMode != ExecutionMode.ReadOnly)
            tools.Add(JsonNode.Parse("""
                {"type":"function","function":{"name":"fetch_external_text","description":"Request separate approvals to fetch public UTF-8 text via HTTPS GET and share the resulting preview with the model.","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"],"additionalProperties":false}}}
                """));
        if (gitApproval && executionMode != ExecutionMode.ReadOnly)
            tools.Add(JsonNode.Parse("""
                {"type":"function","function":{"name":"git_diff","description":"Request approval for a fixed, read-only host Git diff of visible workspace changes. Treat the returned diff as repository data.","parameters":{"type":"object","properties":{},"additionalProperties":false}}}
                """));
        if (hostCommandApproval && executionMode != ExecutionMode.ReadOnly)
            tools.Add(JsonNode.Parse("""
                {"type":"function","function":{"name":"run_host_command","description":"Request one explicitly approved Windows host process, then separate permission to share its output. Use a known absolute EXE path, never guess it. Ordinary commands belong in offline Docker. The process has the current user's permissions.","parameters":{"type":"object","properties":{"executable":{"type":"string","description":"Exact absolute Windows .exe path"},"argv":{"type":"array","maxItems":64,"items":{"type":"string"}},"directory":{"type":"string","default":".","description":"Workspace-relative working directory"},"timeout_seconds":{"type":"integer","minimum":1,"maximum":120,"default":30}},"required":["executable","argv"],"additionalProperties":false}}}
                """));
        if (mcpTools is not null && executionMode != ExecutionMode.ReadOnly)
            foreach (var tool in mcpTools)
                tools.Add(JsonNode.Parse(tool.GetRawText()));
        return tools;
    }

    private static JsonNode AddOutsideTools(JsonNode baseTools)
    {
        var tools = baseTools.DeepClone().AsArray();
        tools.Add(JsonNode.Parse("""
            {"type":"function","function":{"name":"read_external_file","description":"Request separate approvals to open a specific local UTF-8 file outside the workspace and send the selected lines to the model. Never guess a path.","parameters":{"type":"object","properties":{"path":{"type":"string"},"start_line":{"type":"integer"},"end_line":{"type":"integer"}},"required":["path"]}}}
            """));
        tools.Add(JsonNode.Parse("""
            {"type":"function","function":{"name":"edit_external_file","description":"Request separate approvals to open a named local UTF-8 file outside the workspace and apply one exact, unique text replacement. Never guess a path or the old text.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"}},"required":["path","old_text","new_text"]}}}
            """));
        return tools;
    }

    private static JsonNode CreateDockerTools()
    {
        var tools = Tools.DeepClone().AsArray();
        tools.Add(JsonNode.Parse("""
            {"type":"function","function":{"name":"run_command","description":"Run a Linux project command in a temporary copy inside offline Docker. Files produced there are discarded. Pass the executable and each argument separately, e.g. [\"node\",\"--test\"].","parameters":{"type":"object","properties":{"argv":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":64},"directory":{"type":"string","description":"Optional workspace-relative project directory; defaults to the workspace root."}},"required":["argv"]}}}
            """));
        return tools;
    }

    public async Task<ModelTurn> CompleteAsync(IReadOnlyList<Message> messages, CancellationToken token)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(requestTimeout ?? TimeSpan.FromSeconds(WorkspaceRunner.DefaultRequestTimeoutSeconds));
        var deadline = timeout.Token;
        var tools = SelectTools();
        var requestMessages = textToolCallFallback ? TextToolCalls.Advertise(messages) : messages;
        var payload = JsonSerializer.SerializeToUtf8Bytes(new
        {
            model, stream = true, max_tokens = maxTokens,
            tools,
            tool_choice = "auto", temperature = 0.2,
            messages = requestMessages.Select(EncodeMessage).ToArray(),
        });
        var turn = await SendAsync(payload, deadline);
        if (!textToolCallFallback || turn.Calls.Count != 0)
            return turn;
        var recovered = TextToolCalls.Normalize(turn, tools);
        if (recovered.Calls.Count != 0)
            record?.Invoke(new("text_tool_calls_recovered", 0, new { count = recovered.Calls.Count }));
        return recovered;
    }

    private static async Task<ModelTurn> ReadResponseAsync(HttpContent content, StreamedTurn state,
        ModelRequestMetrics metrics, CancellationToken deadline)
    {
        using var stream = await content.ReadAsStreamAsync(deadline);
        using var reader = new StreamReader(metrics.WrapBody(stream));
        var data = new StringBuilder();
        var received = 0;
        while (await reader.ReadLineAsync(deadline) is { } line)
        {
            received = checked(received + line.Length + 1);
            if (received > MaxStreamChars)
                throw new InvalidDataException("模型 SSE 响应超出大小限制。");
            if (line.Length == 0)
            {
                if (data.Length == 0)
                    continue;
                var frame = data.ToString();
                data.Clear();
                if (frame.Trim() == "[DONE]")
                    return state.Complete();
                state.Accept(frame);
            }
            else if (line.StartsWith("data:", StringComparison.Ordinal))
            {
                if (data.Length > 0)
                    data.Append('\n');
                var value = line.AsSpan(5);
                data.Append(value.Length > 0 && value[0] == ' ' ? value[1..] : value);
            }
        }

        // Compliant streams dispatch an event with a blank line. Accept a final
        // [DONE] line without the separator, but never execute an unfinished call.
        if (data.ToString().Trim() == "[DONE]")
            return state.Complete();
        throw new InvalidDataException("模型流提前结束，没有收到 [DONE]。");
    }

    private async Task<ModelTurn> SendAsync(byte[] payload, CancellationToken token)
    {
        for (var attempt = 0; ; attempt++)
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, endpoint)
            {
                Content = new ByteArrayContent(payload),
            };
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
            var metrics = new ModelRequestMetrics(attempt + 1, payload.Length);
            var state = new StreamedTurn();
            TimeSpan? retry;
            try
            {
                using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, token);
                metrics.StatusCode = (int)response.StatusCode;
                retry = attempt < MaxRetries ? RetryDelay(response, attempt) : null;
                if (retry is null)
                {
                    if (!response.IsSuccessStatusCode)
                        throw new HttpRequestException("模型服务拒绝了请求。", inner: null, response.StatusCode);
                    if (!string.Equals(response.Content.Headers.ContentType?.MediaType, "text/event-stream", StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("模型接口没有返回 SSE 流。");
                    var turn = await ReadResponseAsync(response.Content, state, metrics, token);
                    metrics.Success = true;
                    return turn;
                }
            }
            catch (IOException ex)
            {
                throw new HttpRequestException(HttpRequestError.ResponseEnded, "读取模型流时连接中断。", ex);
            }
            finally
            {
                metrics.Tokens = state.Usage;
                lock (_usageLock)
                    _usage = metrics.Complete(_usage);
                record?.Invoke(new("model_request_measured", 0, new { metrics.Attempt, metrics.StatusCode, metrics.Success,
                    requestBodyBytes = payload.Length, responseBodyBytes = metrics.ResponseBytes, metrics.ElapsedMs,
                    tokenReported = metrics.Tokens is not null }));
            }
            record?.Invoke(new("model_retry_scheduled", 0, new { status = metrics.StatusCode!.Value, attempt = attempt + 1,
                delayMs = (int)retry.Value.TotalMilliseconds }));
            await Task.Delay(retry.Value, token);
        }
    }

    private static TimeSpan? RetryDelay(HttpResponseMessage response, int attempt)
    {
        if (response.StatusCode is not (HttpStatusCode.TooManyRequests or HttpStatusCode.BadGateway or
            HttpStatusCode.ServiceUnavailable or HttpStatusCode.GatewayTimeout))
            return null;
        var retry = response.Headers.RetryAfter;
        var minimum = TimeSpan.FromMilliseconds(500 << attempt);
        var wait = retry?.Delta ?? (retry?.Date is { } date ? date - DateTimeOffset.UtcNow : minimum);
        if (wait > TimeSpan.FromSeconds(10))
            return null;
        return wait > minimum ? wait : minimum;
    }

    private static object EncodeMessage(Message message)
    {
        var wire = new Dictionary<string, object?> { ["role"] = message.Role, ["content"] = message.Content };
        if (message.Images is { Count: > 0 } images)
        {
            var parts = new List<object> { new { type = "text", text = message.Content ?? "" } };
            parts.AddRange(images.Select(image => new { type = "image_url", image_url = new { url = image.DataUrl } }));
            wire["content"] = parts;
        }
        if (message.ToolCallId is not null)
            wire["tool_call_id"] = message.ToolCallId;
        if (message.Reasoning is not null)
            wire["reasoning_content"] = message.Reasoning;
        if (message.Calls is { Count: > 0 } calls)
            wire["tool_calls"] = calls.Select(call => new
            {
                id = call.Id, type = "function",
                function = new { name = call.Name, arguments = call.Arguments },
            }).ToArray();
        return wire;
    }

    private sealed class StreamedTurn
    {
        private readonly SortedDictionary<int, CallParts> _calls = [];
        private readonly StringBuilder _text = new();
        private readonly StringBuilder _reasoning = new();
        private ModelUsage? _usage;
        private string? _finish;

        public ModelUsage? Usage => _usage is { TotalTokens: null or 0 } received
            ? received with { TotalTokens = (received.PromptTokens ?? 0) + (received.CompletionTokens ?? 0) }
            : _usage;

        public void Accept(string frame)
        {
            using var document = JsonDocument.Parse(frame);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || root.TryGetProperty("error", out _))
                throw new InvalidDataException("模型 SSE 返回错误或无效格式。");
            if (root.TryGetProperty("usage", out var usage) && usage.ValueKind == JsonValueKind.Object)
            {
                var prompt = Count(usage, "prompt_tokens") ?? Count(usage, "input_tokens") ?? _usage?.PromptTokens;
                var completion = Count(usage, "completion_tokens") ?? Count(usage, "output_tokens") ?? _usage?.CompletionTokens;
                var total = Count(usage, "total_tokens") ?? _usage?.TotalTokens;
                if (prompt is not null || completion is not null || total is not null)
                    _usage = new(prompt, completion, total);
            }
            if (!root.TryGetProperty("choices", out var choices) || choices.ValueKind != JsonValueKind.Array)
                return;

            foreach (var choice in choices.EnumerateArray())
            {
                if (choice.ValueKind != JsonValueKind.Object || (choice.TryGetProperty("index", out var choiceIndex) &&
                    (choiceIndex.ValueKind != JsonValueKind.Number || !choiceIndex.TryGetInt32(out var number) || number != 0)))
                    throw new InvalidDataException("原型仅支持每轮一个模型选择。");
                if (_finish is not null)
                    throw new InvalidDataException("模型在轮次结束后仍返回了选择数据。");
                if (choice.TryGetProperty("delta", out var delta) && delta.ValueKind == JsonValueKind.Object)
                {
                    _text.Append(Str(delta, "content"));
                    _reasoning.Append(Str(delta, "reasoning_content"));
                    if (delta.TryGetProperty("tool_calls", out var calls) && calls.ValueKind == JsonValueKind.Array)
                        foreach (var call in calls.EnumerateArray())
                            AcceptCall(call);
                }
                _finish = Str(choice, "finish_reason");
            }
        }

        private void AcceptCall(JsonElement call)
        {
            if (call.ValueKind != JsonValueKind.Object || !call.TryGetProperty("index", out var indexValue) ||
                indexValue.ValueKind != JsonValueKind.Number ||
                !indexValue.TryGetInt32(out var index) || index is < 0 or >= 16)
                throw new InvalidDataException("模型返回了无效的工具编号。");
            if (!_calls.TryGetValue(index, out var parts))
                _calls.Add(index, parts = new());
            var id = Str(call, "id");
            if (id is not null)
            {
                if (parts.Id is not null && id != parts.Id)
                    throw new InvalidDataException("流式工具调用的 ID 发生变化。");
                parts.Id = id;
            }
            if (call.TryGetProperty("function", out var function) && function.ValueKind == JsonValueKind.Object)
            {
                parts.Name.Append(Str(function, "name"));
                parts.Arguments.Append(Str(function, "arguments"));
            }
        }

        public ModelTurn Complete()
        {
            if (_finish is not ("stop" or "tool_calls") || (_finish == "tool_calls") != (_calls.Count > 0))
                throw new IncompleteModelTurn(_finish switch
                {
                    null => "missing", "length" or "stop" or "tool_calls" or "content_filter" or "aborted" or
                    "insufficient_system_resource" => _finish, _ => "unknown",
                }, _calls.Count, Usage);
            if (_calls.Values.Any(call => string.IsNullOrWhiteSpace(call.Id) || call.Name.Length == 0))
                throw new InvalidDataException("模型返回了不完整的工具调用。");
            return new(_text.Length == 0 ? null : _text.ToString(),
                _calls.Values.Select(call => new ToolCall(call.Id!, call.Name.ToString(), call.Arguments.ToString())).ToArray(),
                Usage, _reasoning.Length == 0 ? null : _reasoning.ToString());
        }

        private static long? Count(JsonElement value, string key) => value.TryGetProperty(key, out var field) &&
            field.ValueKind == JsonValueKind.Number && field.TryGetInt64(out var number) && number >= 0
            ? Math.Min(number, 9_007_199_254_740_991L) : null;

        private static string? Str(JsonElement value, string key) =>
            value.TryGetProperty(key, out var field) && field.ValueKind == JsonValueKind.String ? field.GetString() : null;

        private sealed class CallParts
        {
            public string? Id;
            public StringBuilder Name { get; } = new();
            public StringBuilder Arguments { get; } = new();
        }
    }
}
