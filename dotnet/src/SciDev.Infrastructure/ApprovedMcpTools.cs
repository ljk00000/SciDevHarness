using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class ApprovedMcpTools(string workspace, IFileTools inside, IOperationReview reviewer,
    Uri modelEndpoint, Action<RunEvent> record, string? credential) : IFileTools, IAsyncDisposable
{
    private const int MaxTools = 64;
    private const int MaxCatalogChars = 24_000;
    private const int MaxResultChars = 8_000;
    private static readonly JsonSerializerOptions ReviewFormat = new() { WriteIndented = true };
    private readonly List<McpClient> _sessions = [];
    private readonly Dictionary<string, (McpClient Client, string Server, McpToolDefinition Tool)> _tools = new(StringComparer.Ordinal);
    private readonly IOperationReview _reviewer = new AuditedReview(reviewer, record);
    public IReadOnlyList<JsonElement> ModelTools => _tools.Values.Select(entry => entry.Tool.ModelDefinition).ToArray();

    public async Task LoadAsync(CancellationToken token)
    {
        foreach (var (name, entry) in McpLocalConfiguration.Read(workspace))
        {
            token.ThrowIfCancellationRequested();
            if (_tools.Count >= MaxTools)
                break;
            var reviewId = $"mcp-catalog-{name}";
            McpClient? client = null;
            try
            {
                client = await McpClient.ConnectAsync(workspace, name, entry, _reviewer, credential, token);
                var available = await client.ListToolsAsync(token);
                var definitions = Catalog(name, available);
                if (definitions.Length == 0)
                    continue;
                var preview = JsonSerializer.Serialize(definitions.Select(tool => tool.ModelDefinition), ReviewFormat);
                await RequireAsync(new(reviewId, OperationKind.ShareMcpCatalog, name,
                    "将下列工具名称、说明和参数定义发送给模型。每次执行及结果发送仍各需批准。",
                    Destination: modelEndpoint.GetLeftPart(UriPartial.Path), Before: preview), "approval_denied", token);
                foreach (var tool in definitions)
                    _tools.Add(tool.ModelName, (client, name, tool));
                _sessions.Add(client);
                client = null;
                record(new("mcp_catalog_ready", 0, new { count = definitions.Length }));
            }
            catch (ToolDeniedException ex)
            { record(new("mcp_catalog_unavailable", 0, new { status = ex.Status })); }
            finally
            {
                if (client is not null)
                    await DisposeClientAsync(client);
            }
        }
    }

    private McpToolDefinition[] Catalog(string server, IReadOnlyList<JsonElement> items)
    {
        var batch = new Dictionary<string, McpToolDefinition>(StringComparer.Ordinal);
        foreach (var item in items)
        {
            if (batch.Count + _tools.Count >= MaxTools)
                break;
            if (McpToolDefinition.Read(server, item) is not { } tool || _tools.ContainsKey(tool.ModelName) ||
                ContainsCredential(tool.RemoteName) || ContainsCredential(tool.ModelDefinition))
                continue;
            if (!batch.TryAdd(tool.ModelName, tool))
                continue;
            var preview = JsonSerializer.Serialize(batch.Values.Select(value => value.ModelDefinition), ReviewFormat);
            if (preview.Length > MaxCatalogChars)
                batch.Remove(tool.ModelName);
        }
        return batch.Values.ToArray();
    }

    public async ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token)
    {
        if (!call.Name.StartsWith("mcp__", StringComparison.Ordinal))
            return await inside.ExecuteAsync(call, token);
        if (!_tools.TryGetValue(call.Name, out var entry))
            throw new ToolDeniedException("unsupported_tool", "本轮未批准此 MCP 工具定义。");
        WorkspaceNetworkPolicy.RequireAllowed(workspace, "断网策略禁止执行 MCP 工具。");
        var args = ParseArguments(call.Arguments);
        if (ContainsCredential(args))
            throw new ToolDeniedException("mcp_credential", "MCP 工具参数包含当前模型密钥。");

        var target = $"{entry.Server} / {entry.Tool.RemoteName}";
        if (!entry.Client.IsHttp)
        {
            var preview = JsonSerializer.Serialize(args, ReviewFormat);
            await RequireAsync(new(call.Id, OperationKind.CallMcpTool, target,
                "按下列参数请求已启动的 MCP 进程执行一次。进程具有当前 Windows 用户权限。",
                Before: preview), "approval_denied", token);
            WorkspaceNetworkPolicy.RequireAllowed(workspace, "断网策略禁止执行 MCP 工具。");
        }
        var text = await entry.Client.CallToolAsync(entry.Tool.RemoteName, args, token);
        record(new("mcp_tool_executed", 0, new { id = call.Id }));
        var cleaned = WorkspaceFiles.RedactCredential(text, credential).ReplaceLineEndings("\n");
        var output = McpToolDefinition.PreviewText(cleaned, MaxResultChars);
        var detail = cleaned.Length > output.Length ? "仅将下列截断后的工具结果发送给模型。" : "将下列工具结果发送给模型。";
        await RequireAsync(new(call.Id, OperationKind.ShareMcpResult, target, detail,
            Destination: modelEndpoint.GetLeftPart(UriPartial.Path), Before: output), "mcp_output_not_shared", token);
        return new(output, ExternalDataShared: true);
    }

    private async Task RequireAsync(OperationReview request, string rejected, CancellationToken token)
    {
        var outcome = await OperationApproval.DecideAsync(_reviewer, request, token);
        token.ThrowIfCancellationRequested();
        if (outcome != "approved")
            throw new ToolDeniedException(rejected, rejected == "mcp_output_not_shared"
                ? "MCP 工具已执行，结果发送未获批准。" : "本次 MCP 操作未获批准。");
    }

    private bool ContainsCredential(string value) => !string.IsNullOrEmpty(credential) &&
        value.Contains(credential, StringComparison.Ordinal);

    private bool ContainsCredential(JsonElement value)
    {
        if (string.IsNullOrEmpty(credential))
            return false;
        return value.ValueKind switch
        {
            JsonValueKind.Object => value.EnumerateObject().Any(property =>
                ContainsCredential(property.Name) || ContainsCredential(property.Value)),
            JsonValueKind.Array => value.EnumerateArray().Any(ContainsCredential),
            JsonValueKind.String => ContainsCredential(value.GetString()!),
            _ => ContainsCredential(value.GetRawText()),
        };
    }

    private static JsonElement ParseArguments(string raw)
    {
        if (Encoding.UTF8.GetByteCount(raw) <= 16_384)
        {
            try
            {
                using var parsed = JsonDocument.Parse(raw, new JsonDocumentOptions { MaxDepth = 24, AllowDuplicateProperties = false });
                if (parsed.RootElement.ValueKind == JsonValueKind.Object)
                    return JsonSerializer.SerializeToElement(parsed.RootElement);
            }
            catch (JsonException) { }
        }
        throw new ToolDeniedException("mcp_invalid_arguments", "MCP 工具参数须为不超过 16 KiB 的 JSON 对象。");
    }

    public async Task CloseSessionsAsync(CancellationToken token)
    {
        foreach (var client in _sessions)
            await client.CloseAsync(token);
    }

    public async ValueTask DisposeAsync()
    {
        foreach (var client in _sessions)
            await DisposeClientAsync(client);
    }

    private async ValueTask DisposeClientAsync(McpClient client)
    {
        try { await client.DisposeAsync(); }
        catch (Exception ex) when (ex is IOException or TimeoutException)
        { record(new("mcp_session_cleanup_failed", 0, new { })); }
    }

    private sealed class AuditedReview(IOperationReview inner, Action<RunEvent> emit) : IOperationReview, IOperationReviewAudit
    {
        public ValueTask<bool> ApproveAsync(OperationReview request, CancellationToken token) => inner.ApproveAsync(request, token);
        public void Requested(OperationReview request) => emit(new("mcp_review_requested", 0, new { operation = request.Kind.ToString() }));
        public void Decided(OperationReview request, string outcome) =>
            emit(new("mcp_review_decided", 0, new { operation = request.Kind.ToString(), outcome }));
    }
}
