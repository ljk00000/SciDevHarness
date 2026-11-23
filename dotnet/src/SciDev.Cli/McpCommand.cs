using System.ComponentModel;
using System.Globalization;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Cli;

internal static class McpCommand
{
    internal const string Usage = "mcp [--workspace <directory>] [--read <server> <uri> | --prompt <server> <name> [--arg <key=value> ...]] [--json]";

    private sealed record Options(string Workspace, bool Json, string? Server, string? Resource, string? Prompt,
        Dictionary<string, string> Arguments);
    private sealed record Tool(string Name, string Description);
    private sealed record Catalog(List<Tool> Tools, List<Dictionary<string, JsonElement>> Resources,
        List<Dictionary<string, JsonElement>> Prompts, List<string> Errors);

    public static async Task<int> RunAsync(string[] args, JsonSerializerOptions json)
    {
        if (args is ["mcp", "--help"] or ["mcp", "-h"])
        {
            Console.WriteLine(Usage);
            return 0;
        }
        var key = Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim();
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            var options = Parse(args);
            if (!OperatingSystem.IsWindows() || !Directory.Exists(options.Workspace))
                throw new ArgumentException("MCP 需要本机 Windows 工作区目录。");
            var servers = McpLocalConfiguration.Read(options.Workspace);
            var reviewer = Console.IsInputRedirected || Console.IsErrorRedirected ? null : new ConsoleOperationReview();
            if (options.Server is { } server)
            {
                if (!servers.TryGetValue(server, out var entry))
                    throw new ToolDeniedException("mcp_server_missing", "指定的 MCP 服务未启用或不存在。");
                string text;
                await using (var client = await McpClient.ConnectAsync(options.Workspace, server, entry, reviewer, key, stop.Token))
                {
                    text = options.Resource is { } uri
                        ? await client.ReadResourceAsync(uri, stop.Token)
                        : await client.GetPromptAsync(options.Prompt!, options.Arguments, stop.Token);
                    await client.CloseAsync(stop.Token);
                }
                if (options.Json)
                {
                    object payload = options.Resource is { } resource
                        ? new { server, uri = resource, text } : new { server, prompt = options.Prompt, text };
                    Console.WriteLine(RunJson.SerializeRedacted(payload, json, key));
                }
                else
                    Console.WriteLine(DisplayText(string.IsNullOrEmpty(key) ? text : text.Replace(key, "[redacted]", StringComparison.Ordinal)));
                return 0;
            }

            var catalog = new Catalog([], [], [], []);
            foreach (var (name, entry) in servers)
                await CollectAsync(options.Workspace, name, entry, reviewer, catalog, key, stop.Token);
            if (options.Json)
                Console.WriteLine(RunJson.SerializeRedacted(catalog, json, key));
            else
            {
                Console.WriteLine($"tools: {catalog.Tools.Count}\nresources: {catalog.Resources.Count}\nprompts: {catalog.Prompts.Count}");
                foreach (var error in catalog.Errors)
                    Console.Error.WriteLine(string.IsNullOrEmpty(key) ? error : error.Replace(key, "[redacted]", StringComparison.Ordinal));
            }
            return catalog.Errors.Count == 0 ? 0 : 1;
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { return 130; }
        catch (ArgumentException) { return Error("invalid_input", Usage, 2); }
        catch (ToolDeniedException ex) { return Error(ex.Status, ex.Message, 1); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or Win32Exception or TimeoutException)
        { return Error("mcp_process_error", "无法读取 MCP 配置或关闭宿主进程。", 1); }
        finally { Console.CancelKeyPress -= cancel; }

        int Error(string status, string message, int exit)
        {
            if (args.Contains("--json", StringComparer.Ordinal))
                Console.WriteLine(RunJson.SerializeRedacted(new { status, message }, json, key));
            else
            {
                var text = string.IsNullOrEmpty(key) ? message : message.Replace(key, "[redacted]", StringComparison.Ordinal);
                Console.Error.WriteLine($"{status} · {text}");
            }
            return exit;
        }
    }

    private static async Task CollectAsync(string workspace, string name, JsonElement entry, IOperationReview? reviewer,
        Catalog catalog, string? key, CancellationToken token)
    {
        try
        {
            await using var client = await McpClient.ConnectAsync(workspace, name, entry, reviewer, key, token);
            await ReadListAsync("tools", client.ListToolsAsync, items => catalog.Tools.AddRange(items
                .Where(tool => Text(tool, "name").Length > 0)
                .Select(tool => new Tool(McpLocalConfiguration.ToolName(name, Text(tool, "name")),
                    Text(tool, "description")))));
            await ReadListAsync("resources", client.ListResourcesAsync, items => AddEntries(catalog.Resources, items));
            await ReadListAsync("prompts", client.ListPromptsAsync, items => AddEntries(catalog.Prompts, items));
            await client.CloseAsync(token);
        }
        catch (ToolDeniedException ex) { catalog.Errors.Add($"{name}: {ex.Status} · {ex.Message}"); }
        catch (Exception ex) when (ex is IOException or TimeoutException)
        { catalog.Errors.Add($"{name}: mcp_process_error · 无法关闭宿主进程。"); }

        async Task ReadListAsync(string type, Func<CancellationToken, Task<IReadOnlyList<JsonElement>>> list,
            Action<IReadOnlyList<JsonElement>> add)
        {
            try { add(await list(token)); }
            catch (ToolDeniedException ex) when (ex.Status is "mcp_request_rejected" or "mcp_method_unsupported")
            { catalog.Errors.Add($"{name}: {type}/list · {ex.Status} · {ex.Message}"); }
        }

        void AddEntries(List<Dictionary<string, JsonElement>> destination, IReadOnlyList<JsonElement> items)
        {
            foreach (var item in items.Take(500 - destination.Count))
            {
                var fields = item.EnumerateObject().ToDictionary(field => field.Name, field => field.Value, StringComparer.Ordinal);
                fields["server"] = JsonSerializer.SerializeToElement(name);
                destination.Add(fields);
            }
        }
    }

    private static string Text(JsonElement item, string name) =>
        item.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString()! : "";

    private static string DisplayText(string text) => new(text.Select(ch =>
        ch is '\n' or '\r' or '\t' || (!char.IsControl(ch) && CharUnicodeInfo.GetUnicodeCategory(ch) != UnicodeCategory.Format) ? ch : '�').ToArray());

    private static Options Parse(string[] args)
    {
        var workspace = ".";
        string? server = null, resource = null, prompt = null;
        var asJson = false;
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var arguments = new Dictionary<string, string>(StringComparer.Ordinal);
        var index = 1;
        while (index < args.Length)
        {
            var option = args[index++];
            if (!seen.Add(option) && option != "--arg")
                throw Invalid();
            switch (option)
            {
                case "--workspace":
                    workspace = Value();
                    break;
                case "--json":
                    asJson = true;
                    break;
                case "--read" or "--prompt" when server is null:
                    server = Value();
                    var target = Value();
                    if (target.Length > 4096)
                        throw Invalid();
                    if (option == "--read")
                        resource = target;
                    else
                        prompt = target;
                    break;
                case "--arg":
                    var pair = Value();
                    var split = pair.IndexOf('=');
                    var name = (split < 0 ? pair : pair[..split]).Trim();
                    if (name.Length is 0 or > 128 || pair.Length > 16_384 || arguments.Count >= 32)
                        throw Invalid();
                    arguments[name] = split < 0 ? "" : pair[(split + 1)..];
                    break;
                default: throw Invalid();
            }
        }
        if (arguments.Count > 0 && prompt is null)
            throw Invalid();
        return new(Path.GetFullPath(workspace), asJson, server, resource, prompt, arguments);

        string Value()
        {
            if (index >= args.Length || args[index].Length == 0 || args[index].StartsWith("--", StringComparison.Ordinal))
                throw Invalid();
            return args[index++];
        }

        static ArgumentException Invalid() => new(Usage);
    }
}
