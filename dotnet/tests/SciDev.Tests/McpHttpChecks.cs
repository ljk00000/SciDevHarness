using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class McpHttpChecks
{
    private const string ModelKey = "local-mcp-http-test-model-key";
    private const string Negotiated = "2025-06-18";

    internal static async Task RunAsync(DirectoryInfo workspace)
    {
        var state = workspace.CreateSubdirectory(".research");
        var settings = Path.Combine(state.FullName, "settings.json");
        var original = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY");
        Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", "ask");
        await using var server = new Server();
        using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(45));
        try
        {
            Task<McpClient> Connect(string path, IOperationReview? review, int timeout = 6)
            {
                var entry = JsonSerializer.SerializeToElement(new { url = server.Address + path, transport = "sse", timeout });
                return McpClient.ConnectAsync(workspace.FullName, "http_fixture", entry, review, ModelKey, stop.Token);
            }

            await ExpectAsync(Connect("/mcp", null), "approval_required");
            var reject = new Reviewer(operation => !operation.Detail.Contains("initialize", StringComparison.Ordinal));
            await ExpectAsync(Connect("/mcp", reject), "approval_denied");
            Assert(reject.Requests.Count == 1 && server.Requests.IsEmpty, "first refusal sends no traffic");
            File.WriteAllText(settings, """{"network_policy":"deny"}""");
            var denied = new Reviewer(_ => true);
            await ExpectAsync(Connect("/mcp", denied), "network_policy_denied");
            Assert(denied.Requests.Count == 0 && server.Requests.IsEmpty, "offline policy prevents approval and traffic");
            File.Delete(settings);
            await ExpectAsync(Connect($"/mcp?key={ModelKey}", denied), "mcp_credential");
            Assert(denied.Requests.Count == 0 && server.Requests.IsEmpty, "model credentials are rejected before preview");

            var approval = new Reviewer(_ => true);
            await using (var client = await Connect("/mcp", approval))
            {
                Assert((await client.ListToolsAsync(stop.Token)).Single().GetProperty("name").GetString() == "resume_tool",
                    "SSE response resumes after an empty priming event");
                Assert((await client.ListResourcesAsync(stop.Token)).Single().GetProperty("uri").GetString() == "fixture://one" &&
                    (await client.ListPromptsAsync(stop.Token)).Single().GetProperty("name").GetString() == "hello",
                    "catalog categories share the negotiated HTTP session");
                Assert(await client.ReadResourceAsync("fixture://one", stop.Token) == "你好，资源", "resource read restarts a rejected session");
                Assert(await client.GetPromptAsync("hello", new Dictionary<string, string> { ["who"] = "鹈鹕" }, stop.Token) == "你好，鹈鹕",
                    "prompt arguments and UTF-8 survive");
                await client.CloseAsync(stop.Token);
            }
            var sent = server.Requests.ToArray();
            Assert(sent.Select(item => item.Operation).SequenceEqual([
                "initialize", "notifications/initialized", "tools/list", "GET", "resources/list", "prompts/list",
                "resources/read", "initialize", "notifications/initialized", "resources/read", "prompts/get", "DELETE",
            ]), "the original POST is never retried to resume an SSE response");
            Assert(sent[3].Headers["Last-Event-ID"] == "resume-1" && sent[0].Headers.GetValueOrDefault("MCP-Session-Id") is null &&
                sent[1].Headers["MCP-Session-Id"] == "fixture-1" && sent[6].Headers["MCP-Protocol-Version"] == Negotiated &&
                sent[7].Headers.GetValueOrDefault("MCP-Session-Id") is null && sent[11].Headers["MCP-Session-Id"] == "fixture-2",
                "SSE cursor, session reinitialization, and negotiated version reach the server");
            Assert(approval.Requests.Count == sent.Length && approval.Requests.All(request =>
                request.Kind == OperationKind.RequestMcpHttp && request.Target == server.Address + "/mcp" &&
                request.Before?.Contains(ModelKey, StringComparison.Ordinal) != true &&
                request.Before?.Contains("fixture-1", StringComparison.Ordinal) != true),
                "every HTTP request needs one approval without credentials or a session identifier in the preview");
            Assert(approval.Requests[^2].Before?.Contains("prompts/get", StringComparison.Ordinal) == true, "review includes exact JSON request");
            Assert(sent.All(item => !item.Headers.ContainsKey("Authorization") && !item.Headers.ContainsKey("Cookie")),
                "model credentials and received cookies are never forwarded");

            var midway = new Reviewer(request => request.Before?.Contains("notifications/initialized", StringComparison.Ordinal) != true);
            await ExpectAsync(Connect("/mcp", midway), "approval_denied");
            Assert(server.Requests.Count == sent.Length + 1, "denying initialization notification sends only the approved handshake");
            var redirect = new Reviewer(_ => true);
            await ExpectAsync(Connect("/redirect", redirect), "mcp_http_status");
            Assert(redirect.Requests.Count == 1 && !server.Requests.Any(item => item.Path == "/unapproved"),
                "redirect does not send an unapproved request");
            await using (var legacy = await Connect("/legacy", new Reviewer(_ => true)))
            {
                Assert((await legacy.ListToolsAsync(stop.Token)).Single().GetProperty("name").GetString() == "legacy_tool" &&
                    (await legacy.ListResourcesAsync(stop.Token)).Count == 0 && (await legacy.ListPromptsAsync(stop.Token)).Count == 0,
                    "legacy empty handshake preserves tool discovery when optional lists return HTTP 404");
                await legacy.CloseAsync(stop.Token);
            }
            var beforeCall = server.Requests.Count;
            await using (var tool = await Connect("/mcp", approval))
                await ExpectAsync(tool.CallToolAsync("resume_tool", JsonSerializer.SerializeToElement(new { }), stop.Token), "mcp_session_expired");
            Assert(server.Requests.Skip(beforeCall).Select(item => item.Operation).SequenceEqual([
                "initialize", "notifications/initialized", "tools/call",
            ]), "a tool call is not replayed or reinitialized after session expiration");
            await ExpectAsync(Connect("/oversized", approval), "mcp_response_limit");
            await ExpectAsync(Connect("/stall", approval, timeout: 1), "mcp_timeout");
        }
        finally { Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", original); }
    }

    private sealed class Server : IAsyncDisposable
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _stop = new();
        private readonly Task _loop;
        private int _sessionCount;
        private int _listId;

        public Server()
        {
            _listener.Start();
            Address = $"http://127.0.0.1:{((IPEndPoint)_listener.LocalEndpoint).Port}";
            _loop = RunAsync();
        }

        public ConcurrentQueue<Request> Requests { get; } = new();
        public string Address { get; }

        private async Task RunAsync()
        {
            try
            {
                while (true)
                {
                    using var connection = await _listener.AcceptTcpClientAsync(_stop.Token);
                    try { await RespondAsync(connection.GetStream(), _stop.Token); }
                    catch (IOException) { } // A rejected or timed-out response may close its connection before the last write.
                }
            }
            catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        }

        private async Task RespondAsync(Stream stream, CancellationToken token)
        {
            using var reader = new StreamReader(stream, Encoding.ASCII, leaveOpen: true);
            var firstLine = (await reader.ReadLineAsync(token))?.Split(' ') ?? [];
            Assert(firstLine.Length == 3, "valid HTTP request line");
            var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            while (await reader.ReadLineAsync(token) is { Length: > 0 } header)
            {
                var colon = header.IndexOf(':');
                headers.Add(header[..colon], header[(colon + 1)..].Trim());
            }
            var length = int.Parse(headers.GetValueOrDefault("Content-Length") ?? "0");
            var body = new char[length];
            if (length > 0)
                Assert(await reader.ReadBlockAsync(body.AsMemory(), token) == length, "complete HTTP request body");
            using var packet = JsonDocument.Parse(length == 0 ? "{}" : new string(body));
            var root = packet.RootElement;
            var method = root.TryGetProperty("method", out var rpc) ? rpc.GetString()! : firstLine[0];
            var id = root.TryGetProperty("id", out var requestId) ? requestId.GetInt32() : 0;
            var path = firstLine[1];
            Requests.Enqueue(new(path, method, headers));
            switch (path)
            {
                case "/redirect":
                    await WriteAsync("307 Temporary Redirect", extra: $"Location: {Address}/unapproved\r\n");
                    return;
                case "/oversized":
                    var prefix = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n";
                    await stream.WriteAsync(Encoding.ASCII.GetBytes(prefix), token);
                    try { await stream.WriteAsync(new byte[McpHttpResponseStream.MaxBytes + 1], token); }
                    catch (IOException) { }
                    return;
                case "/stall":
                    await Task.Delay(TimeSpan.FromSeconds(3), token);
                    return;
            }
            switch (method)
            {
                case "initialize" when path == "/legacy":
                    await ResultAsync(new { }, extra: "MCP-Session-Id: legacy-fixture\r\n");
                    break;
                case "initialize":
                    Assert(!headers.ContainsKey("MCP-Session-Id") && !headers.ContainsKey("MCP-Protocol-Version") &&
                        headers["Accept"].Contains("application/json") && headers["Accept"].Contains("text/event-stream"),
                        "fresh HTTP initialization offers both media types");
                    await ResultAsync(new { protocolVersion = Negotiated, capabilities = new { tools = new { }, prompts = new { }, resources = new { } } },
                        extra: $"MCP-Session-Id: fixture-{++_sessionCount}\r\nSet-Cookie: hidden=fixture-token\r\n");
                    break;
                case "notifications/initialized":
                    await WriteAsync("202 Accepted");
                    break;
                case "tools/list" when path == "/legacy":
                    await ResultAsync(new { tools = new[] { new { name = "legacy_tool" } } });
                    break;
                case "tools/list":
                    _listId = id;
                    await WriteAsync("200 OK", "text/event-stream", "id: resume-1\r\nretry: 1\r\ndata:\r\n\r\n");
                    break;
                case "GET":
                    Assert(headers["Accept"] == "text/event-stream", "SSE resumption negotiates only event streams");
                    var json = JsonSerializer.Serialize(new { tools = new[] { new { name = "resume_tool" } } });
                    await WriteAsync("200 OK", "text/event-stream", "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\n\n" +
                        $"data: {{\"jsonrpc\":\"2.0\",\"id\":{_listId},\ndata: \"result\":{json}}}\n\n");
                    break;
                case "resources/list" or "prompts/list" when path == "/legacy":
                    await WriteAsync("404 Not Found");
                    break;
                case "resources/list":
                    await ResultAsync(new { resources = new[] { new { uri = "fixture://one" } } });
                    break;
                case "prompts/list":
                    await ResultAsync(new { prompts = new[] { new { name = "hello" } } });
                    break;
                case "resources/read" when headers["MCP-Session-Id"] == "fixture-1":
                case "tools/call":
                    await WriteAsync("404 Not Found");
                    break;
                case "resources/read":
                    await ResultAsync(new { contents = new[] { new { text = "你好，资源" } } });
                    break;
                case "prompts/get":
                    var who = root.GetProperty("params").GetProperty("arguments").GetProperty("who").GetString();
                    await ResultAsync(new { messages = new[] { new { content = new { type = "text", text = $"你好，{who}" } } } });
                    break;
                case "DELETE":
                    await WriteAsync("204 No Content");
                    break;
                default: throw new InvalidOperationException("Unexpected MCP fixture request.");
            }

            Task ResultAsync(object result, string extra = "") =>
                WriteAsync("200 OK", "application/json", JsonSerializer.Serialize(new { jsonrpc = "2.0", id, result }), extra);

            async Task WriteAsync(string status, string contentType = "text/plain", string body = "", string extra = "")
            {
                var bytes = Encoding.UTF8.GetBytes(body);
                var head = $"HTTP/1.1 {status}\r\nContent-Type: {contentType}\r\nContent-Length: {bytes.Length}\r\nConnection: close\r\n{extra}\r\n";
                await stream.WriteAsync(Encoding.UTF8.GetBytes(head + body), token);
            }
        }

        public async ValueTask DisposeAsync()
        {
            _stop.Cancel();
            try { await _loop; }
            finally
            {
                _listener.Stop();
                _stop.Dispose();
            }
        }
    }

    private sealed record Request(string Path, string Operation, Dictionary<string, string> Headers);

    private sealed class Reviewer(Func<OperationReview, bool> approve) : IOperationReview
    {
        public List<OperationReview> Requests { get; } = [];
        public ValueTask<bool> ApproveAsync(OperationReview request, CancellationToken token)
        {
            Requests.Add(request);
            return new(approve(request));
        }
    }

    private static async Task ExpectAsync(Task operation, string status)
    {
        try { await operation; }
        catch (ToolDeniedException ex) when (ex.Status == status) { return; }
        throw new InvalidOperationException("Expected " + status);
    }

    private static void Assert(bool condition, string description)
    {
        if (!condition)
            throw new InvalidOperationException(description);
    }
}
