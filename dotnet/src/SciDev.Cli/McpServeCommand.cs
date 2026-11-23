using System.Diagnostics;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Cli;

internal sealed class McpServeCommand(McpWorkspaceTools tools)
{
    private const string LegacyVersion = "2024-11-05";
    private const string LatestVersion = "2025-11-25";
    private static readonly UTF8Encoding Utf8 = new(false, true);
    private static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
    private static readonly JsonElement Empty = JsonSerializer.SerializeToElement(new { });
    private string? _version;
    private bool _shutdown;
    private long _windowStart = Stopwatch.GetTimestamp();
    private int _calls;

    public static async Task<int> RunAsync(string[] args)
    {
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            if (!OperatingSystem.IsWindows())
                throw new ArgumentException("当前 MCP 服务只支持 Windows。");
            var workspace = args switch
            {
                ["mcp-serve"] => Directory.GetCurrentDirectory(),
                ["mcp-serve", "--workspace", var path] when !string.IsNullOrWhiteSpace(path) && !path.StartsWith("--", StringComparison.Ordinal) => path,
                _ => throw new ArgumentException("用法 mcp-serve [--workspace <目录>]"),
            };
            if (!Directory.Exists(Path.GetFullPath(workspace)))
                throw new ArgumentException("MCP 工作区目录不存在。");
            var server = new McpServeCommand(new McpWorkspaceTools(workspace, Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim()));
            await server.ServeAsync(Console.OpenStandardInput(), Console.OpenStandardOutput(), stop.Token);
            return 0;
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { return 0; }
        catch (ArgumentException ex) { Console.Error.WriteLine(ex.Message); return 2; }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException)
        { Console.Error.WriteLine("无法打开 MCP 工作区或处理 stdio 连接。"); return 1; }
        finally { Console.CancelKeyPress -= cancel; }
    }

    private async Task ServeAsync(Stream input, Stream output, CancellationToken token)
    {
        var frames = new McpStdioFrames(input);
        while (!_shutdown && await frames.ReadAsync(token) is { } frame)
        {
            var response = frame.Error is { } fault ? Error(null, -32600, fault) : await ReadFrameAsync(frame.Payload, token);
            if (response is null)
                continue;
            var bytes = JsonSerializer.SerializeToUtf8Bytes(response, Json);
            await McpStdioFrames.WriteAsync(output, bytes, frames.UsesLengthHeaders, token);
            if (frame.Fatal)
                break;
        }
    }

    private async ValueTask<object?> ReadFrameAsync(byte[] bytes, CancellationToken token)
    {
        try
        {
            var line = Utf8.GetString(bytes);
            if (string.IsNullOrWhiteSpace(line))
                return null;
            using var document = JsonDocument.Parse(line, new JsonDocumentOptions { MaxDepth = 32, AllowDuplicateProperties = false });
            return await HandleAsync(document.RootElement, token);
        }
        catch (Exception ex) when (ex is JsonException or DecoderFallbackException or InvalidOperationException)
        { return Error(null, -32700, "无效的 UTF-8 JSON 请求。"); }
    }

    private async ValueTask<object?> HandleAsync(JsonElement request, CancellationToken token)
    {
        if (request.ValueKind != JsonValueKind.Object || !TryString(request, "jsonrpc", out var rpc) || rpc != "2.0" ||
            !TryString(request, "method", out var method) || method.Length is < 1 or > 128)
            return Error(null, -32600, "无效的 JSON-RPC 请求。");
        if (!request.TryGetProperty("id", out var id))
            return null;
        if (!(id.ValueKind == JsonValueKind.String && id.GetString() is { Length: <= 256 } ||
            id.ValueKind == JsonValueKind.Number && id.TryGetInt64(out _)))
            return Error(null, -32600, "请求 ID 必须是字符串或整数。");
        var parameters = request.TryGetProperty("params", out var input) ? input : Empty;
        if (parameters.ValueKind != JsonValueKind.Object)
            return Error(id, -32602, "params 必须是对象。");

        switch (method)
        {
            case "initialize":
                if (_version is not null)
                    return Error(id, -32600, "连接已经初始化。");
                if (parameters.TryGetProperty("protocolVersion", out var version) && version.ValueKind != JsonValueKind.String)
                    return Error(id, -32602, "protocolVersion 必须是字符串。");
                var requested = version.ValueKind == JsonValueKind.Undefined ? LegacyVersion : version.GetString();
                _version = requested switch
                {
                    LegacyVersion or "2025-03-26" or "2025-06-18" or LatestVersion => requested,
                    _ => LatestVersion,
                };
                return Result(id, new { protocolVersion = _version, capabilities = new { tools = new { listChanged = false } },
                    serverInfo = new { name = "scidev-harness", version = "0.1.0" } });
            case "ping":
                return Result(id, Empty);
            case "shutdown":
                _shutdown = true;
                return Result(id, null);
            case "tools/list" when _version is not null:
                if (parameters.TryGetProperty("cursor", out var cursor) && cursor.ValueKind != JsonValueKind.Null &&
                    !(cursor.ValueKind == JsonValueKind.String && cursor.GetString() == ""))
                    return Error(id, -32602, "此工具列表没有后续分页。");
                return Result(id, new { tools = tools.Available });
            case "tools/call" when _version is not null:
                return await CallAsync(id, parameters, token);
            case "tools/list" or "tools/call":
                return Error(id, -32002, "先初始化 MCP 连接。");
            default:
                return Error(id, -32601, "未知的 JSON-RPC 方法。");
        }
    }

    private async ValueTask<object> CallAsync(JsonElement id, JsonElement parameters, CancellationToken token)
    {
        if (!TryString(parameters, "name", out var name) || name.Length is < 1 or > 128)
            return Error(id, -32602, "tools/call 需要有效的 name。");
        var args = parameters.TryGetProperty("arguments", out var requested) ? requested : Empty;
        if (args.ValueKind != JsonValueKind.Object)
            return Error(id, -32602, "arguments 必须是对象。");
        if (!tools.Contains(name))
            return _version == LegacyVersion ? TextAnswer(id, "此服务仅提供列出的工作区只读工具。", true) :
                Error(id, -32602, "此服务仅提供列出的工作区只读工具。");

        if (Stopwatch.GetElapsedTime(_windowStart) >= TimeSpan.FromSeconds(1))
        {
            _windowStart = Stopwatch.GetTimestamp();
            _calls = 0;
        }
        if (++_calls > 64)
            return Error(id, -32000, "只读工具调用过于频繁，请稍后再试。");

        using var budget = CancellationTokenSource.CreateLinkedTokenSource(token);
        budget.CancelAfter(TimeSpan.FromSeconds(20));
        try
        {
            var answer = await tools.CallAsync(name, args, budget.Token);
            return TextAnswer(id, answer.Text, answer.IsError);
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        { return TextAnswer(id, "工作区查询超时。请缩小范围后重试。", true); }
    }

    private static bool TryString(JsonElement element, string key, out string value)
    {
        value = element.TryGetProperty(key, out var field) && field.ValueKind == JsonValueKind.String ? field.GetString()! : "";
        return value.Length > 0;
    }

    private static object TextAnswer(JsonElement id, string text, bool isError) =>
        Result(id, new { content = new[] { new { type = "text", text } }, isError });
    private static object Result(JsonElement id, object? result) => new { jsonrpc = "2.0", id = id.Clone(), result };
    private static object Error(JsonElement? id, int code, string message) => new { jsonrpc = "2.0", id = id?.Clone(), error = new { code, message } };
}
