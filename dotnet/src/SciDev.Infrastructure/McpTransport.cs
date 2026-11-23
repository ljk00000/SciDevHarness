using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal interface IMcpTransport : IAsyncDisposable
{
    string InitialVersion { get; }
    bool AllowsUndeclaredCapabilities { get; }
    string? ResolveVersion(JsonElement result);
    Task<JsonElement> ExchangeAsync(McpRequest request, CancellationToken token);
    Task InitializedAsync(string version, CancellationToken token);
    Task CloseAsync(CancellationToken token);
}

internal sealed record McpRequest(int Id, string Method, object Parameters)
{
    public object Packet => new { jsonrpc = "2.0", id = Id, method = Method, @params = Parameters };
}

internal static class McpRpc
{
    internal const int MaxPacketBytes = 512 * 1024;
    internal static readonly object Initialized = new { jsonrpc = "2.0", method = "notifications/initialized" };
    private static readonly JsonDocumentOptions InputOptions = new() { MaxDepth = 32, AllowDuplicateProperties = false };

    public static byte[] Encode(object packet)
    {
        var data = JsonSerializer.SerializeToUtf8Bytes(packet);
        if (data.Length > MaxPacketBytes)
            throw new ToolDeniedException("mcp_request_too_large", "MCP 请求超过 512 KiB。");
        return data;
    }

    public static async ValueTask<JsonElement?> MatchAsync(ReadOnlyMemory<byte> bytes, McpRequest request,
        Func<string, object, CancellationToken, ValueTask> respond, CancellationToken token)
    {
        using var document = JsonDocument.Parse(bytes, InputOptions);
        var packet = document.RootElement;
        if (packet.ValueKind != JsonValueKind.Object || StringField(packet, "jsonrpc") != "2.0")
            throw InvalidResponse();
        var hasId = packet.TryGetProperty("id", out var received);
        if (StringField(packet, "method") is { } serverMethod)
        {
            if (hasId)
            {
                if (received.ValueKind != JsonValueKind.String &&
                    (received.ValueKind != JsonValueKind.Number || !received.TryGetInt64(out _)))
                    throw InvalidResponse();
                object response = serverMethod == "ping"
                    ? new { jsonrpc = "2.0", id = received, result = new { } }
                    : new { jsonrpc = "2.0", id = received, error = new { code = -32601, message = "Method not available" } };
                await respond(serverMethod == "ping" ? "ping response" : "unsupported server request response", response, token);
            }
            return null;
        }
        if (!hasId || received.ValueKind != JsonValueKind.Number || !received.TryGetInt32(out var id) || id != request.Id)
            throw InvalidResponse();
        var hasError = packet.TryGetProperty("error", out var error);
        var hasResult = packet.TryGetProperty("result", out var result);
        if (hasError == hasResult)
            throw InvalidResponse();
        if (hasError)
        {
            if (error.ValueKind == JsonValueKind.Object && error.TryGetProperty("code", out var code) &&
                code.ValueKind == JsonValueKind.Number && code.TryGetInt32(out var errorCode) && errorCode == -32601)
                throw new ToolDeniedException("mcp_method_unsupported", $"MCP 服务未实现 {request.Method}。");
            throw new ToolDeniedException("mcp_request_rejected", $"MCP 服务拒绝了 {request.Method}。");
        }
        if (result.ValueKind != JsonValueKind.Object)
            throw InvalidResponse();
        return result.Clone();
    }

    public static string? StringField(JsonElement item, string key) =>
        item.TryGetProperty(key, out var field) && field.ValueKind == JsonValueKind.String ? field.GetString() : null;

    public static ToolDeniedException InvalidResponse() => new("mcp_invalid_response", "MCP 服务的响应无效或超过上限。");
}
