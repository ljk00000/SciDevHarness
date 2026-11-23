using System.ComponentModel;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class McpStdioTransport(LocalStdioProcess process, McpLocalConfiguration.Stdio config) : IMcpTransport
{
    private readonly McpStdioFrames _frames = new(process.Output, McpRpc.MaxPacketBytes, config.UsesLengthHeaders);
    public string InitialVersion => config.UsesLengthHeaders ? "2024-11-05" : "2025-11-25";
    public bool AllowsUndeclaredCapabilities => config.UsesLengthHeaders;
    public string? ResolveVersion(JsonElement result) => McpRpc.StringField(result, "protocolVersion");

    public static async Task<McpStdioTransport> OpenAsync(string workspace, string name, JsonElement entry,
        IOperationReview? reviewer, string? key, CancellationToken token)
    {
        var config = McpLocalConfiguration.ReadStdio(entry);
        try
        {
            return new(await McpLocalLaunch.StartAsync(workspace, name, config, reviewer, key, token), config);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or Win32Exception or TimeoutException)
        {
            throw new ToolDeniedException("mcp_process_error", "无法核对或启动 MCP 服务进程，或 stdio 连接已关闭。", ex);
        }
    }

    public Task<JsonElement> ExchangeAsync(McpRequest request, CancellationToken token) =>
        WithDeadlineAsync(async deadline =>
        {
            await SendAsync(request.Packet, deadline);
            for (var count = 0; count < 64; count++)
            {
                var frame = await _frames.ReadAsync(deadline);
                if (frame is null || frame.Error is not null)
                    throw McpRpc.InvalidResponse();
                if (await McpRpc.MatchAsync(frame.Payload, request, (_, reply, ct) => SendAsync(reply, ct), deadline) is { } result)
                    return result;
            }
            throw McpRpc.InvalidResponse();
        }, token);

    public Task InitializedAsync(string version, CancellationToken token) =>
        WithDeadlineAsync(async deadline =>
        {
            await SendAsync(McpRpc.Initialized, deadline);
            return true;
        }, token);

    public Task CloseAsync(CancellationToken token) => Task.CompletedTask;

    private async Task<T> WithDeadlineAsync<T>(Func<CancellationToken, Task<T>> exchange, CancellationToken token)
    {
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(token);
        budget.CancelAfter(config.Timeout);
        using var kill = budget.Token.Register(process.Abort);
        try
        {
            var result = await exchange(budget.Token);
            budget.Token.ThrowIfCancellationRequested();
            return result;
        }
        catch (Exception) when (budget.IsCancellationRequested)
        {
            token.ThrowIfCancellationRequested();
            throw new ToolDeniedException("mcp_timeout", "MCP 请求超时，已结束本次进程树。");
        }
        catch (JsonException) { throw McpRpc.InvalidResponse(); }
        catch (IOException ex) { throw new ToolDeniedException("mcp_process_error", "MCP stdio 连接已关闭。", ex); }
    }

    private ValueTask SendAsync(object packet, CancellationToken token) =>
        McpStdioFrames.WriteAsync(process.Input, McpRpc.Encode(packet), config.UsesLengthHeaders, token);

    public ValueTask DisposeAsync() => process.DisposeAsync();
}
