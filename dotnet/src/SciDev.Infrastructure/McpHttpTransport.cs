using System.Net;
using System.Net.Http.Headers;
using System.Net.ServerSentEvents;
using System.Net.Sockets;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class McpHttpTransport : IMcpTransport
{
    private const string SessionHeader = "MCP-Session-Id";
    private const string VersionHeader = "MCP-Protocol-Version";
    private const string PolicyDenied = "断网策略禁止 MCP HTTP 请求。";
    private static readonly HttpRequestOptionsKey<ConnectionAttempt> AttemptOption = new("SciDev.McpConnection");
    private readonly string _workspace;
    private readonly string _name;
    private readonly McpLocalConfiguration.Http _config;
    private readonly IOperationReview _reviewer;
    private readonly string? _key;
    private readonly HttpClient _http;
    private IPAddress? _pinnedAddress;
    private string? _session;
    private string? _version;
    private bool _legacyHandshake;
    private int _requestCount;

    private sealed class ConnectionAttempt { public int Used; }
    private sealed record Reply(JsonElement? Result, string? Cursor = null, TimeSpan Delay = default);

    private McpHttpTransport(string workspace, string name, McpLocalConfiguration.Http config, IOperationReview reviewer, string? key)
    {
        _workspace = workspace;
        _name = name;
        _config = config;
        _reviewer = reviewer;
        _key = key;
        _http = new(new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            UseProxy = false,
            UseCookies = false,
            AutomaticDecompression = DecompressionMethods.None,
            MaxResponseHeadersLength = 16,
            MaxResponseDrainSize = 0,
            PooledConnectionLifetime = TimeSpan.Zero,
            SslOptions = new() { CertificateChainPolicy = new X509ChainPolicy { DisableCertificateDownloads = true } },
            ConnectCallback = ConnectAsync,
        }) { Timeout = Timeout.InfiniteTimeSpan };
    }

    public string InitialVersion => "2025-11-25";
    public bool AllowsUndeclaredCapabilities => _legacyHandshake;

    public string? ResolveVersion(JsonElement result)
    {
        // The original Python HTTP client accepted an empty InitializeResult.
        _legacyHandshake = !result.EnumerateObject().Any();
        return _legacyHandshake ? "2024-11-05" : McpRpc.StringField(result, "protocolVersion");
    }

    public static McpHttpTransport Open(string workspace, string name, McpLocalConfiguration.Http config,
        IOperationReview? reviewer, string? key)
    {
        WorkspaceNetworkPolicy.RequireAllowed(workspace, PolicyDenied);
        if (reviewer is null)
            throw new ToolDeniedException("approval_required", "MCP HTTP 请求需要交互终端逐次批准。");
        McpLocalLaunch.CheckCredential([config.Endpoint.AbsoluteUri, Uri.UnescapeDataString(config.Endpoint.AbsoluteUri)], key);
        return new(workspace, name, config, reviewer, key);
    }

    public async Task<JsonElement> ExchangeAsync(McpRequest request, CancellationToken token)
    {
        var reply = await SendAsync(HttpMethod.Post, request.Method, request.Packet, cursor: null,
            (response, deadline) => ReadExchangeAsync(response, request, deadline), token);
        if (reply.Result is { } match)
            return match;
        if (reply.Cursor is not { Length: > 0 })
            throw McpRpc.InvalidResponse();
        if (reply.Delay >= _config.Timeout)
            throw new ToolDeniedException("mcp_timeout", "MCP SSE 要求等待的时间超过请求上限。");
        await Task.Delay(reply.Delay, token);
        var resumed = await SendAsync(HttpMethod.Get, "resume " + request.Method, packet: null, cursor: reply.Cursor,
            (response, deadline) => ReadExchangeAsync(response, request, deadline), token);
        return resumed.Result ?? throw McpRpc.InvalidResponse();
    }

    public async Task InitializedAsync(string version, CancellationToken token)
    {
        _version = version;
        await NotifyAsync("notifications/initialized", McpRpc.Initialized, token);
    }

    private async ValueTask NotifyAsync(string operation, object packet, CancellationToken token) =>
        await SendAsync(HttpMethod.Post, operation, packet, cursor: null, (response, _) =>
            response.StatusCode == HttpStatusCode.Accepted ? Task.FromResult(true) : throw McpRpc.InvalidResponse(), token);

    public async Task CloseAsync(CancellationToken token)
    {
        if (_session is null)
            return;
        try
        {
            await SendAsync(HttpMethod.Delete, "close MCP session", packet: null, cursor: null,
                (_, _) => Task.FromResult(true), token);
        }
        catch (ToolDeniedException)
        {
            // Session termination is optional and remains subject to its own approval.
        }
        finally
        {
            _session = null;
            _version = null;
        }
    }

    private async Task<T> SendAsync<T>(HttpMethod method, string operation, object? packet, string? cursor,
        Func<HttpResponseMessage, CancellationToken, Task<T>> read, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        WorkspaceNetworkPolicy.RequireAllowed(_workspace, PolicyDenied);
        var body = packet is null ? null : McpRpc.Encode(packet);
        var preview = body is null ? null : Encoding.UTF8.GetString(body);
        if (preview is not null && !string.IsNullOrEmpty(_key))
            McpLocalLaunch.CheckCredential([preview], JsonSerializer.Serialize(_key)[1..^1]);
        if (cursor is not null && !VisibleAscii(cursor))
            throw McpRpc.InvalidResponse();
        McpLocalLaunch.CheckCredential([preview ?? "", cursor ?? ""], _key);

        var endpoint = _config.Endpoint;
        var detail = $"MCP 服务 {JsonSerializer.Serialize(_name)} · 直连 {method} · {operation}。" +
            (endpoint.Scheme == "http" ? " 此端点使用明文 HTTP。" : "");
        if (cursor is not null)
            detail += " 仅继续读取同一端点的 SSE 响应。";
        var kind = operation == "tools/call" ? OperationKind.CallMcpTool : OperationKind.RequestMcpHttp;
        var operationReview = new OperationReview($"mcp-http-{++_requestCount}", kind,
            endpoint.AbsoluteUri, detail, Before: preview);
        var approval = await OperationApproval.DecideAsync(_reviewer, operationReview, token);
        token.ThrowIfCancellationRequested();
        if (approval != "approved")
            throw new ToolDeniedException("approval_denied", "本次 MCP HTTP 请求未获批准。");
        WorkspaceNetworkPolicy.RequireAllowed(_workspace, PolicyDenied);

        using var budget = CancellationTokenSource.CreateLinkedTokenSource(token);
        budget.CancelAfter(_config.Timeout);
        using var request = new HttpRequestMessage(method, endpoint)
        {
            Version = HttpVersion.Version11,
            VersionPolicy = HttpVersionPolicy.RequestVersionExact,
            Content = body is null ? null : new ByteArrayContent(body),
        };
        request.Options.Set(AttemptOption, new());
        request.Headers.ConnectionClose = true;
        request.Headers.Accept.Add(new("text/event-stream"));
        if (method != HttpMethod.Get)
            request.Headers.Accept.Add(new("application/json"));
        if (request.Content is not null)
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        if (_session is not null)
            request.Headers.Add(SessionHeader, _session);
        if (_version is not null)
            request.Headers.Add(VersionHeader, _version);
        if (cursor is not null)
            request.Headers.Add("Last-Event-ID", cursor);
        try
        {
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, budget.Token);
            if (response.StatusCode == HttpStatusCode.NotFound && _legacyHandshake &&
                operation is "resources/list" or "prompts/list")
                throw new ToolDeniedException("mcp_method_unsupported", "旧 MCP 服务未实现此列表。");
            if (response.StatusCode == HttpStatusCode.NotFound && _session is not null)
            {
                _session = null;
                _version = null;
                throw new ToolDeniedException("mcp_session_expired", "MCP 服务的会话已过期。");
            }
            if (!response.IsSuccessStatusCode && !(method == HttpMethod.Delete && response.StatusCode == HttpStatusCode.MethodNotAllowed))
                throw new ToolDeniedException("mcp_http_status", $"MCP 端点返回 HTTP {(int)response.StatusCode}。");
            if (operation is "initialize" or "resume initialize")
                ReadSession(response);
            return await read(response, budget.Token);
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        { throw new ToolDeniedException("mcp_timeout", "MCP HTTP 请求达到时间上限。"); }
        catch (JsonException) { throw McpRpc.InvalidResponse(); }
        catch (Exception ex) when (ex is HttpRequestException or IOException or SocketException)
        { throw new ToolDeniedException("mcp_http_error", "无法完成已批准的 MCP HTTP 直连请求。", ex); }
    }

    private void ReadSession(HttpResponseMessage response)
    {
        if (!response.Headers.TryGetValues(SessionHeader, out var values))
            return;
        var items = values.Take(2).ToArray();
        if (items.Length != 1 || !VisibleAscii(items[0]) || _session is not null && _session != items[0])
            throw McpRpc.InvalidResponse();
        McpLocalLaunch.CheckCredential(items, _key);
        _session = items[0];
    }

    private async Task<Reply> ReadExchangeAsync(HttpResponseMessage response, McpRequest request, CancellationToken token)
    {
        if (response.Content.Headers.ContentLength > McpHttpResponseStream.MaxBytes)
            throw McpHttpResponseStream.Limit();
        await using var stream = await response.Content.ReadAsStreamAsync(token);
        using var bounded = new McpHttpResponseStream(stream);
        switch (response.Content.Headers.ContentType?.MediaType?.ToLowerInvariant())
        {
            case "application/json":
                using (var buffer = new MemoryStream())
                {
                    await bounded.CopyToAsync(buffer, token);
                    var data = buffer.GetBuffer().AsMemory(0, (int)buffer.Length);
                    return new(await McpRpc.MatchAsync(data, request, NotifyAsync, token) ?? throw McpRpc.InvalidResponse());
                }
            case "text/event-stream":
                var parser = SseParser.Create(bounded, static (_, data) => data.ToArray());
                var count = 0;
                try
                {
                    await foreach (var item in parser.EnumerateAsync(token))
                    {
                        if (item.Data.Length == 0)
                            continue;
                        if (++count > 64)
                            throw McpRpc.InvalidResponse();
                        if (await McpRpc.MatchAsync(item.Data, request, NotifyAsync, token) is { } result)
                            return new(result);
                    }
                }
                catch (IOException) when (!token.IsCancellationRequested && !string.IsNullOrEmpty(parser.LastEventId)) { }
                var delay = parser.ReconnectionInterval == Timeout.InfiniteTimeSpan
                    ? TimeSpan.FromSeconds(2) : parser.ReconnectionInterval;
                return new(null, parser.LastEventId, delay);
            default:
                throw McpRpc.InvalidResponse();
        }
    }

    private async ValueTask<Stream> ConnectAsync(SocketsHttpConnectionContext context, CancellationToken token)
    {
        var endpoint = _config.Endpoint;
        var host = endpoint.IdnHost.Trim('[', ']');
        if (!context.InitialRequestMessage.Options.TryGetValue(AttemptOption, out var attempt) ||
            Interlocked.Exchange(ref attempt.Used, 1) != 0 ||
            !context.DnsEndPoint.Host.Trim('[', ']').Equals(host, StringComparison.OrdinalIgnoreCase) ||
            context.DnsEndPoint.Port != endpoint.Port)
            throw new HttpRequestException("MCP 网络连接与已批准目标不符。");

        if (_pinnedAddress is null)
        {
            if (IPAddress.TryParse(host, out var address))
                _pinnedAddress = address;
            else if (host.Equals("localhost", StringComparison.OrdinalIgnoreCase))
                _pinnedAddress = IPAddress.Loopback;
            else
            {
                var addresses = await Dns.GetHostAddressesAsync(host, token);
                _pinnedAddress = addresses.FirstOrDefault(item => item.AddressFamily == AddressFamily.InterNetwork) ?? addresses.FirstOrDefault() ??
                    throw new HttpRequestException("MCP 网络地址未解析。");
            }
        }
        var socket = new Socket(_pinnedAddress.AddressFamily, SocketType.Stream, ProtocolType.Tcp);
        try
        {
            await socket.ConnectAsync(new IPEndPoint(_pinnedAddress, endpoint.Port), token);
            return new NetworkStream(socket, ownsSocket: true);
        }
        catch
        {
            socket.Dispose();
            throw;
        }
    }

    private static bool VisibleAscii(string value) => value.Length is > 0 and <= 512 && value.All(ch => ch is >= '!' and <= '~');

    public ValueTask DisposeAsync()
    {
        _http.Dispose();
        return ValueTask.CompletedTask;
    }
}
