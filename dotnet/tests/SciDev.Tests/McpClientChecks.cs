using System.Diagnostics;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class McpClientChecks
{
    private const string ModelKey = "synthetic-mcp-model-api-key";
    private static string Executable => Environment.ProcessPath ?? throw new InvalidOperationException();
    private static string[] Args(string behavior) => Path.GetFileNameWithoutExtension(Executable).Equals("dotnet", StringComparison.OrdinalIgnoreCase)
        ? [typeof(McpClientChecks).Assembly.Location, "--mcp-fixture", behavior] : ["--mcp-fixture", behavior];

    internal static async Task RunAsync(DirectoryInfo workspace)
    {
        var state = workspace.CreateSubdirectory(".research");
        var cwd = workspace.CreateSubdirectory("MCP fixture");
        var marker = Path.Combine(cwd.FullName, "started.txt");
        var configuration = Path.Combine(state.FullName, "mcp.json");
        var oldKey = Environment.GetEnvironmentVariable("SCIDEV_API_KEY");
        var oldPolicy = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY");
        Environment.SetEnvironmentVariable("SCIDEV_API_KEY", ModelKey);
        Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", "ask");
        try
        {
            JsonElement Configure(string? framing, string behavior = "serve", Dictionary<string, string>? env = null, int timeout = 6)
            {
                var entry = new Dictionary<string, object>
                {
                    ["command"] = Executable, ["args"] = Args(behavior), ["cwd"] = cwd.Name, ["timeout"] = timeout,
                    ["env"] = env ?? new() { ["MCP_FIXTURE_VALUE"] = "configured service value" },
                };
                if (framing is not null)
                    entry["framing"] = framing;
                File.WriteAllText(configuration, JsonSerializer.Serialize(new { enabled = true, servers = new { test_fixture = entry } }));
                return McpLocalConfiguration.Read(workspace.FullName)["test_fixture"];
            }

            Task<McpClient> Connect(JsonElement entry, IOperationReview? review) =>
                McpClient.ConnectAsync(workspace.FullName, "test_fixture", entry, review, ModelKey, default);

            var legacy = Configure(null);
            await ExpectAsync(Connect(legacy, null), "approval_required");
            var refusal = new Reviewer(request => request.Kind != OperationKind.RunHostCommand);
            await ExpectAsync(Connect(legacy, refusal), "approval_denied");
            Assert(refusal.Requests.Count == 2 && !File.Exists(marker), "declining the process approval prevents start");

            File.WriteAllText(Path.Combine(state.FullName, "settings.json"), """{"network_policy":"deny"}""");
            var deny = new Reviewer(_ => true);
            await ExpectAsync(Connect(legacy, deny), "network_policy_denied");
            Assert(deny.Requests.Count == 0 && !File.Exists(marker), "workspace deny policy prevents launch even with an ask environment");
            File.Delete(Path.Combine(state.FullName, "settings.json"));

            var credential = new Reviewer(_ => true);
            var unsafeEnv = Configure(null, env: new() { ["OTHER_NAME"] = ModelKey });
            await ExpectAsync(Connect(unsafeEnv, credential), "mcp_credential");
            Assert(credential.Requests.Count == 0 && !File.Exists(marker), "blocked model keys do not reach approval or start");

            foreach (var framing in new[] { legacy, Configure("newline") })
            {
                var approve = new Reviewer(_ => true);
                await using var client = await Connect(framing, approve);
                Assert(approve.Requests.Select(item => item.Kind).SequenceEqual([OperationKind.InspectHostCommand, OperationKind.RunHostCommand]) &&
                    approve.Requests[1].Detail.Contains("SHA-256") && approve.Requests[1].Detail.Contains("MCP_FIXTURE_VALUE") &&
                    !approve.Requests[1].Detail.Contains("configured service value"), "approved executable and configuration preview");
                Assert((await client.ListToolsAsync(default)).Select(item => item.GetProperty("name").GetString()).SequenceEqual(["lookup", "finish"]),
                    "both tool pages and server ping are handled");
                Assert((await client.ListResourcesAsync(default)).Single().GetProperty("uri").GetString() == "fixture://one" &&
                    (await client.ListPromptsAsync(default)).Single().GetProperty("name").GetString() == "hello", "resources and prompts are listed");
                Assert(await client.ReadResourceAsync("fixture://one", default) == "你好 · configured service value · key absent\n[binary resource image/png]",
                    "resource supports Unicode and child diagnostics do not corrupt frames");
                Assert(await client.GetPromptAsync("hello", new Dictionary<string, string> { ["who"] = "骑自行车的鹈鹕" }, default) == "你好，骑自行车的鹈鹕",
                    "prompt arguments are passed and rendered");
                await ExpectAsync(client.ReadResourceAsync($"fixture://{ModelKey}", default), "mcp_credential");
            }

            await using (var client = await Connect(Configure("newline", behavior: "no-capabilities"), new Reviewer(_ => true)))
            {
                Assert((await client.ListToolsAsync(default)).Count == 0, "undeclared capabilities are not listed");
                await ExpectAsync(client.ReadResourceAsync("fixture://one", default), "mcp_method_unsupported");
            }
            Configure("newline");
            await ToolApprovalAsync(workspace, cwd);

            await ExpectAsync(Connect(Configure("newline", behavior: "hold", timeout: 2), new Reviewer(_ => true)), "mcp_timeout");
            var processes = File.ReadAllLines(Path.Combine(cwd.FullName, "processes.txt")).Select(int.Parse).ToArray();
            Assert(processes.Length == 2, "the MCP fixture spawned a descendant");
            foreach (var pid in processes)
            {
                try
                {
                    using var process = Process.GetProcessById(pid);
                    Assert(process.WaitForExit(2_000), "timeout must remove every process in the MCP job");
                }
                catch (ArgumentException) { }
            }
        }
        finally
        {
            Environment.SetEnvironmentVariable("SCIDEV_API_KEY", oldKey);
            Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", oldPolicy);
        }
    }

    private static async Task ToolApprovalAsync(DirectoryInfo workspace, DirectoryInfo cwd)
    {
        var journal = new List<RunEvent>();
        var marker = Path.Combine(cwd.FullName, "mcp-call.txt");
        ApprovedMcpTools Tools(Reviewer review) => new(workspace.FullName, new WorkspaceFiles(workspace.FullName), review,
            new Uri("http://127.0.0.1:1234/v1/chat/completions"), journal.Add, ModelKey);
        var call = new ToolCall("call-one", "mcp__test_fixture__lookup", """{"value":"private-mcp-argument"}""");

        await using (var hidden = Tools(new Reviewer(request => request.Kind != OperationKind.ShareMcpCatalog)))
        {
            await hidden.LoadAsync(default);
            Assert(hidden.ModelTools.Count == 0, "declining catalog disclosure removes every definition");
            await ExpectAsync(hidden.ExecuteAsync(call, default).AsTask(), "unsupported_tool");
        }
        await using (var blocked = Tools(new Reviewer(request => request.Kind != OperationKind.CallMcpTool)))
        {
            await blocked.LoadAsync(default);
            Assert(blocked.ModelTools.Count == 2, "only approved tools become available");
            await ExpectAsync(blocked.ExecuteAsync(call, default).AsTask(), "approval_denied");
            await ExpectAsync(blocked.ExecuteAsync(call with { Name = "mcp__test_fixture__not_listed" }, default).AsTask(), "unsupported_tool");
            Assert(!File.Exists(marker), "one denied or forged operation executes no tool");
        }
        var noResult = new Reviewer(request => request.Kind != OperationKind.ShareMcpResult);
        await using (var withheld = Tools(noResult))
        {
            await withheld.LoadAsync(default);
            await ExpectAsync(withheld.ExecuteAsync(call, default).AsTask(), "mcp_output_not_shared");
            Assert(File.ReadAllLines(marker).Length == 1 &&
                noResult.Requests[^1].Before?.Contains("private-mcp-result", StringComparison.Ordinal) == true &&
                !noResult.Requests[^1].Before!.Contains(ModelKey, StringComparison.Ordinal), "declined results were executed and previewed with redaction");
        }
        var yes = new Reviewer(_ => true);
        await using (var approved = Tools(yes))
        {
            await approved.LoadAsync(default);
            await ExpectAsync(approved.ExecuteAsync(call with { Arguments = """{"value":"one","value":"two"}""" }, default).AsTask(), "mcp_invalid_arguments");
            await ExpectAsync(approved.ExecuteAsync(call with { Arguments = JsonSerializer.Serialize(new { value = ModelKey }) }, default).AsTask(), "mcp_credential");
            var escaped = ModelKey.Replace("-", "\\u002d", StringComparison.Ordinal);
            await ExpectAsync(approved.ExecuteAsync(call with { Arguments = $"{{\"value\":\"{escaped}\"}}" }, default).AsTask(), "mcp_credential");
            var result = await approved.ExecuteAsync(call, default);
            Assert(result.ExternalDataShared && result.Text == yes.Requests[^1].Before &&
                result.Text.Contains("private-mcp-result", StringComparison.Ordinal) && File.ReadAllLines(marker).Length == 2,
                "approved result exactly matches the delivered text and each tool runs once");
            Assert(yes.Requests.Select(request => request.Kind).SequenceEqual([
                OperationKind.InspectHostCommand, OperationKind.RunHostCommand, OperationKind.ShareMcpCatalog,
                OperationKind.CallMcpTool, OperationKind.ShareMcpResult]), "each approval stage is independent");
            var structured = await approved.ExecuteAsync(call with { Id = "call-two", Arguments = """{"value":"structured"}""" }, default);
            using var decoded = JsonDocument.Parse(structured.Text);
            Assert(decoded.RootElement.GetProperty("key").GetString() == "[redacted]" && structured.Text == yes.Requests[^1].Before,
                "structured outputs redact model credentials inside JSON strings before review");
        }
        var audit = JsonSerializer.Serialize(journal);
        Assert(journal.Count(entry => entry.Type == "mcp_tool_executed") == 3 &&
            !audit.Contains("private-mcp-", StringComparison.Ordinal) && !audit.Contains(ModelKey, StringComparison.Ordinal), "audit contains metadata only");
    }

    internal static async Task FixtureAsync(string behavior)
    {
        if (behavior is "child" or "hold")
        {
            if (behavior == "hold")
            {
                var start = new ProcessStartInfo(Executable) { UseShellExecute = false, CreateNoWindow = true };
                foreach (var value in Args("child"))
                    start.ArgumentList.Add(value);
                using var child = Process.Start(start)!;
                File.WriteAllText("processes.txt", $"{Environment.ProcessId}\n{child.Id}\n");
            }
            await Task.Delay(TimeSpan.FromMinutes(1));
            return;
        }
        File.WriteAllText("started.txt", "started");
        Console.Error.Write(new string('x', 96 * 1024));
        var reader = new McpStdioFrames(Console.OpenStandardInput());
        var output = Console.OpenStandardOutput();
        var initialized = false;
        while (await reader.ReadAsync(default) is { } frame)
        {
            using var document = JsonDocument.Parse(frame.Payload);
            var request = document.RootElement;
            var method = request.GetProperty("method").GetString();
            if (method == "notifications/initialized")
            {
                initialized = true;
                continue;
            }
            var parameters = request.GetProperty("params");
            if (method == "tools/list" && !parameters.TryGetProperty("cursor", out _))
            {
                await SendAsync(new { jsonrpc = "2.0", id = "peer-test", method = "ping" });
                var answer = await reader.ReadAsync(default) ?? throw new InvalidOperationException("Missing MCP pong.");
                using var pong = JsonDocument.Parse(answer.Payload);
                Assert(pong.RootElement.GetProperty("id").GetString() == "peer-test" && pong.RootElement.TryGetProperty("result", out _),
                    "client replies to a ping even while waiting for a response");
            }
            object result = method switch
            {
                "initialize" when !initialized => new
                {
                    protocolVersion = parameters.GetProperty("protocolVersion").GetString(),
                    capabilities = behavior == "no-capabilities" ? (object)new { } : new { tools = new { }, resources = new { }, prompts = new { } },
                    serverInfo = new { name = "fixture", version = "1.0.0" },
                },
                "tools/list" when initialized && parameters.TryGetProperty("cursor", out var cursor) && cursor.GetString() == "page2"
                    => new { tools = new[] { new { name = "finish" } } },
                "tools/list" when initialized => new
                {
                    tools = new[] { new { name = "lookup", description = "Fixture lookup",
                        inputSchema = new { type = "object", properties = new { value = new { type = "string" } } } } },
                    nextCursor = "page2",
                },
                "tools/call" when initialized && parameters.GetProperty("name").GetString() == "lookup" =>
                    CallFixture(parameters, includeKey: behavior != "serve-no-key"),
                "resources/list" when initialized => new { resources = new[] { new { uri = "fixture://one" } } },
                "prompts/list" when initialized => new { prompts = new[] { new { name = "hello" } } },
                "resources/read" when initialized && parameters.GetProperty("uri").GetString() == "fixture://one" => new
                {
                    contents = new object[] {
                        new { text = $"你好 · {Environment.GetEnvironmentVariable("MCP_FIXTURE_VALUE")} · key " +
                            (Environment.GetEnvironmentVariable("SCIDEV_API_KEY") is null ? "absent" : "present") },
                        new { blob = "AA==", mimeType = "image/png" },
                    },
                },
                "prompts/get" when initialized && parameters.GetProperty("name").GetString() == "hello" => new
                {
                    messages = new[] { new { content = new { type = "text", text = $"你好，{parameters.GetProperty("arguments").GetProperty("who").GetString()}" } } },
                },
                _ => throw new InvalidOperationException("Unexpected MCP fixture request."),
            };
            await SendAsync(new { jsonrpc = "2.0", id = request.GetProperty("id"), result });
        }

        ValueTask SendAsync(object packet) => McpStdioFrames.WriteAsync(output, JsonSerializer.SerializeToUtf8Bytes(packet), reader.UsesLengthHeaders, default);
    }

    private static object CallFixture(JsonElement parameters, bool includeKey)
    {
        var value = parameters.GetProperty("arguments").GetProperty("value").GetString();
        File.AppendAllText("mcp-call.txt", "called\n");
        if (value == "structured")
            return new { structuredContent = new { key = ModelKey } };
        var suffix = includeKey ? " · " + ModelKey : "";
        return new { content = new[] { new { type = "text", text = $"private-mcp-result {value}{suffix}" } } };
    }

    private static async Task ExpectAsync(Task task, string status)
    {
        try { await task; }
        catch (ToolDeniedException ex) when (ex.Status == status) { return; }
        throw new InvalidOperationException("Expected " + status);
    }

    private static void Assert(bool condition, string description)
    {
        if (!condition)
            throw new InvalidOperationException(description);
    }

    private sealed class Reviewer(Func<OperationReview, bool> approve) : IOperationReview
    {
        public List<OperationReview> Requests { get; } = [];
        public ValueTask<bool> ApproveAsync(OperationReview request, CancellationToken token)
        {
            Requests.Add(request);
            return new(approve(request));
        }
    }
}
