using System.Text.Json;
using SciDev.Core;
using static SciDev.Infrastructure.McpRpc;

namespace SciDev.Infrastructure;

/// <summary>An approved MCP session. The caller issues requests sequentially.</summary>
public sealed class McpClient : IAsyncDisposable
{
    private const int MaxItems = 200;
    private const int MaxTextChars = 80_000;
    private readonly IMcpTransport _transport;
    private readonly string? _key;
    private JsonElement _capabilities;
    private int _nextId;

    private McpClient(IMcpTransport transport, string? key)
    {
        _transport = transport;
        _key = key;
    }

    public static async Task<McpClient> ConnectAsync(string workspace, string name, JsonElement entry,
        IOperationReview? reviewer, string? key, CancellationToken token)
    {
        IMcpTransport transport = McpLocalConfiguration.ReadHttp(entry) is { } http
            ? McpHttpTransport.Open(workspace, name, http, reviewer, key)
            : await McpStdioTransport.OpenAsync(workspace, name, entry, reviewer, key, token);
        var client = new McpClient(transport, key);
        try
        {
            await client.InitializeAsync(token);
            return client;
        }
        catch
        {
            await client.DisposeAsync();
            throw;
        }
    }

    private async Task InitializeAsync(CancellationToken token)
    {
        var result = await _transport.ExchangeAsync(new(++_nextId, "initialize", new
        {
            protocolVersion = _transport.InitialVersion,
            capabilities = new { },
            clientInfo = new { name = "scidev-harness", version = "0.1.0" },
        }), token);
        var version = _transport.ResolveVersion(result);
        if (version is not ("2024-11-05" or "2025-03-26" or "2025-06-18" or "2025-11-25"))
            throw new ToolDeniedException("mcp_protocol_version", "MCP 服务返回了本客户端未支持的版本。");
        _capabilities = default;
        if (result.TryGetProperty("capabilities", out var capabilities) && capabilities.ValueKind == JsonValueKind.Object)
            _capabilities = capabilities;
        else if (!_transport.AllowsUndeclaredCapabilities)
            throw InvalidResponse();
        await _transport.InitializedAsync(version, token);
    }

    public Task<IReadOnlyList<JsonElement>> ListToolsAsync(CancellationToken token) => ListAsync("tools", token);
    public Task<IReadOnlyList<JsonElement>> ListResourcesAsync(CancellationToken token) => ListAsync("resources", token);
    public Task<IReadOnlyList<JsonElement>> ListPromptsAsync(CancellationToken token) => ListAsync("prompts", token);
    internal bool IsHttp => _transport is McpHttpTransport;

    internal async Task<string> CallToolAsync(string name, JsonElement arguments, CancellationToken token)
    {
        RequireCapability("tools");
        // A tool may have side effects. Only the read-only API reinitializes and repeats work on expiry.
        var result = await _transport.ExchangeAsync(new(++_nextId, "tools/call", new { name, arguments }), token);
        if (result.TryGetProperty("isError", out var error) && error.ValueKind == JsonValueKind.True)
            throw new ToolDeniedException("mcp_tool_error", "MCP 工具报告调用失败。");
        if (result.TryGetProperty("content", out var content) && content.ValueKind == JsonValueKind.Array)
        {
            var parts = content.EnumerateArray().Take(200)
                .Where(item => item.ValueKind == JsonValueKind.Object && StringField(item, "type") == "text")
                .Select(item => StringField(item, "text"));
            var text = JoinText(parts, "");
            if (text.Length > 0)
                return text;
        }
        return result.TryGetProperty("structuredContent", out var structured) &&
            structured.ValueKind == JsonValueKind.Object ? RunJson.RedactElement(structured, _key) : "MCP 工具没有返回文本内容";
    }

    private async Task<IReadOnlyList<JsonElement>> ListAsync(string type, CancellationToken token)
    {
        try { return await ListPagesAsync(type, token); }
        catch (ToolDeniedException ex) when (ex.Status == "mcp_session_expired")
        {
            await InitializeAsync(token);
            return await ListPagesAsync(type, token);
        }
    }

    private async Task<IReadOnlyList<JsonElement>> ListPagesAsync(string type, CancellationToken token)
    {
        List<JsonElement> found = [];
        if (!_transport.AllowsUndeclaredCapabilities && !HasCapability(type))
            return found;
        var cursors = new HashSet<string>(StringComparer.Ordinal);
        string? cursor = null;
        for (var page = 0; page < 8 && found.Count < MaxItems; page++)
        {
            object parameters = cursor is null ? new { } : new { cursor };
            JsonElement result;
            try { result = await _transport.ExchangeAsync(new(++_nextId, type + "/list", parameters), token); }
            catch (ToolDeniedException ex) when (ex.Status == "mcp_method_unsupported" && page == 0 && !HasCapability(type)) { break; }
            found.AddRange(Entries(result, type).Where(item => item.ValueKind == JsonValueKind.Object).Take(MaxItems - found.Count));
            cursor = StringField(result, "nextCursor");
            if (string.IsNullOrEmpty(cursor))
                break;
            if (cursor.Length > 4096 || !cursors.Add(cursor))
                throw InvalidResponse();
        }
        return found;
    }

    public async Task<string> ReadResourceAsync(string uri, CancellationToken token)
    {
        RequireCapability("resources");
        McpLocalLaunch.CheckCredential([uri], _key);
        var result = await QueryAsync("resources", "resources/read", new { uri }, token);
        var parts = Entries(result, "contents").Take(50).Where(item => item.ValueKind == JsonValueKind.Object)
            .Select(item => StringField(item, "text") ?? (item.TryGetProperty("blob", out _)
                ? $"[binary resource {StringField(item, "mimeType") ?? "unknown"}]" : null));
        return JoinText(parts, "MCP 资源没有返回文本内容");
    }

    public async Task<string> GetPromptAsync(string name, IReadOnlyDictionary<string, string> arguments, CancellationToken token)
    {
        RequireCapability("prompts");
        McpLocalLaunch.CheckCredential(arguments.Keys.Concat(arguments.Values).Append(name), _key);
        var result = await QueryAsync("prompts", "prompts/get", new { name, arguments }, token);
        var parts = Entries(result, "messages").Take(200)
            .Where(item => item.ValueKind == JsonValueKind.Object && item.TryGetProperty("content", out _))
            .SelectMany(item =>
            {
                var content = item.GetProperty("content");
                return content.ValueKind == JsonValueKind.Array ? content.EnumerateArray().ToArray() : [content];
            })
            .Where(item => item.ValueKind == JsonValueKind.Object && StringField(item, "type") == "text")
            .Select(item => StringField(item, "text"));
        return JoinText(parts, "MCP 提示词没有返回文本内容");
    }

    private async Task<JsonElement> QueryAsync(string capability, string method, object parameters, CancellationToken token)
    {
        try { return await _transport.ExchangeAsync(new(++_nextId, method, parameters), token); }
        catch (ToolDeniedException ex) when (ex.Status == "mcp_session_expired")
        {
            await InitializeAsync(token);
            RequireCapability(capability);
            return await _transport.ExchangeAsync(new(++_nextId, method, parameters), token);
        }
    }

    private bool HasCapability(string type) => _capabilities.ValueKind == JsonValueKind.Object &&
        _capabilities.TryGetProperty(type, out var capability) && capability.ValueKind == JsonValueKind.Object;

    private void RequireCapability(string type)
    {
        if (!_transport.AllowsUndeclaredCapabilities && !HasCapability(type))
            throw new ToolDeniedException("mcp_method_unsupported", $"MCP 服务未声明 {type} 能力。");
    }

    private static JsonElement.ArrayEnumerator Entries(JsonElement result, string key)
    {
        if (result.TryGetProperty(key, out var items) && items.ValueKind == JsonValueKind.Array)
            return items.EnumerateArray();
        throw InvalidResponse();
    }

    private static string JoinText(IEnumerable<string?> parts, string fallback)
    {
        var value = string.Join('\n', parts.OfType<string>());
        if (value.Length == 0)
            return fallback;
        if (value.Length <= MaxTextChars)
            return value;
        return value[..(char.IsHighSurrogate(value[MaxTextChars - 1]) ? MaxTextChars - 1 : MaxTextChars)];
    }

    public Task CloseAsync(CancellationToken token) => _transport.CloseAsync(token);
    public ValueTask DisposeAsync() => _transport.DisposeAsync();
}
