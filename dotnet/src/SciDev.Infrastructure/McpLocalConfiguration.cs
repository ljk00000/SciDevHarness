using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public static class McpLocalConfiguration
{
    public static IReadOnlyDictionary<string, JsonElement> Read(string workspace)
    {
        var guard = new WindowsWorkspace(workspace);
        using var path = guard.OpenOperatorPath(".research/mcp.json", allowMissingParents: true);
        var servers = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        if (path is null || !WindowsWorkspace.FileExists(path.AbsolutePath))
            return servers;
        using var file = WorkspaceFiles.OpenSnapshot(path.AbsolutePath, FileShare.Read, maxBytes: 256 * 1024);
        try
        {
            var offset = file.Bytes.AsSpan().StartsWith(System.Text.Encoding.UTF8.Preamble) ? 3 : 0;
            using var document = JsonDocument.Parse(file.Bytes.AsMemory(offset), new() { AllowDuplicateProperties = false, MaxDepth = 16 });
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                throw Invalid();
            if (!root.TryGetProperty("enabled", out var enabled) || enabled.ValueKind != JsonValueKind.True)
                return servers;
            if (!root.TryGetProperty("servers", out var entries) || entries.ValueKind != JsonValueKind.Object)
                throw Invalid();
            foreach (var entry in entries.EnumerateObject().Take(8))
                if (!servers.TryAdd(Component(entry.Name, "server"), entry.Value.Clone()))
                    throw new ToolDeniedException("mcp_config_invalid", "MCP 服务名称经过规范化后重复。");
            return servers;
        }
        catch (JsonException) { throw Invalid(); }
    }

    internal sealed record Stdio(string Command, string[] Arguments, string Directory,
        Dictionary<string, string> Environment, TimeSpan Timeout, bool UsesLengthHeaders);
    internal sealed record Http(Uri Endpoint, TimeSpan Timeout);

    internal static Http? ReadHttp(JsonElement server)
    {
        try
        {
            if (server.ValueKind != JsonValueKind.Object)
                throw Invalid();
            var transport = Field(server, "transport")?.Trim().ToLowerInvariant();
            var url = Field(server, "url")?.Trim();
            if (string.IsNullOrEmpty(url) && transport is null or "" or "stdio")
                return null;
            if (transport is not (null or "" or "http" or "sse" or "streamable-http" or "streamable_http"))
                throw new ToolDeniedException("mcp_transport_unsupported", "HTTP 服务需要 http、streamable-http 或旧版的 sse 配置。");
            if (url is not { Length: > 0 and <= 2048 } || !Displayable(url) ||
                !Uri.TryCreate(url, UriKind.Absolute, out var endpoint) ||
                !endpoint.IsWellFormedOriginalString() ||
                endpoint.Scheme is not ("http" or "https") || endpoint.HostNameType is not (UriHostNameType.Dns or UriHostNameType.IPv4 or UriHostNameType.IPv6) ||
                endpoint.Port <= 0 || endpoint.UserInfo.Length != 0 || endpoint.Fragment.Length != 0)
                throw new ToolDeniedException("mcp_url_invalid", "MCP URL 需要完整 HTTP 或 HTTPS 地址，且不能包含用户名、密码或片段。");
            return new(endpoint, ReadTimeout(server));
        }
        catch (Exception ex) when (ex is InvalidOperationException or FormatException) { throw Invalid(); }
    }

    internal static Stdio ReadStdio(JsonElement server)
    {
        try
        {
            if (server.ValueKind != JsonValueKind.Object)
                throw Invalid();
            var transport = Field(server, "transport")?.Trim().ToLowerInvariant();
            if (!string.IsNullOrWhiteSpace(Field(server, "url")) || transport is "http" or "sse" or "streamable-http" or "streamable_http")
                throw Invalid();
            if (transport is not (null or "" or "stdio"))
                throw Invalid();
            var command = Field(server, "command")?.Trim();
            var cwd = Field(server, "cwd");
            cwd = string.IsNullOrEmpty(cwd) ? "." : cwd;
            if (string.IsNullOrEmpty(command) || command.Length > 243 || !Displayable(command) || !Displayable(cwd))
                throw Invalid();

            var args = server.TryGetProperty("args", out var argv) && argv.ValueKind != JsonValueKind.Null
                ? argv.EnumerateArray().Select(value => value.GetString() ?? throw Invalid()).ToArray() : [];
            if (args.Length > 64 || args.Sum(value => (long)value.Length) > 8192 || !args.All(Displayable))
                throw Invalid();
            var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (server.TryGetProperty("env", out var env) && env.ValueKind != JsonValueKind.Null)
                foreach (var item in env.EnumerateObject())
                {
                    var value = item.Value.GetString() ?? throw Invalid();
                    if (item.Name.Length is 0 or > 80 || !item.Name.All(ch => char.IsAsciiLetterOrDigit(ch) || ch == '_') ||
                        value.Contains('\0') || value.Length > 4096 || !environment.TryAdd(item.Name, value))
                        throw Invalid();
                }
            if (environment.Count > 32 || environment.Sum(item => item.Key.Length + item.Value.Length) > 16_384)
                throw Invalid();
            var lengthHeaders = Field(server, "framing") switch
            {
                null or "content-length" => true,
                "newline" => false,
                _ => throw Invalid(),
            };
            return new(command, args, cwd, environment, ReadTimeout(server), lengthHeaders);
        }
        catch (Exception ex) when (ex is InvalidOperationException or FormatException) { throw Invalid(); }
    }

    public static string Component(string value, string fallback)
    {
        var normalized = new string(value.Select(ch => char.IsAsciiLetterOrDigit(ch) || ch is '_' or '-' ? ch : '_').ToArray()).Trim('_');
        return normalized.Length == 0 ? fallback : normalized[..Math.Min(normalized.Length, 48)];
    }

    public static string ToolName(string server, string name)
    {
        var component = Component(name, "tool");
        var model = $"mcp__{server}__{component}";
        if (model.Length <= 64 && component == name)
            return model;
        var suffix = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(server + "\0" + name)))[..8].ToLowerInvariant();
        return model[..Math.Min(55, model.Length)] + "_" + suffix;
    }

    internal static string? Field(JsonElement item, string name) =>
        item.TryGetProperty(name, out var field) ? field.GetString() : null;

    private static TimeSpan ReadTimeout(JsonElement server)
    {
        var seconds = server.TryGetProperty("timeout", out var timeout) && timeout.ValueKind != JsonValueKind.Null
            ? timeout.GetDouble() : 15;
        if (!double.IsFinite(seconds) || seconds is < 1 or > 120)
            throw Invalid();
        return TimeSpan.FromSeconds(seconds);
    }

    private static bool Displayable(string value) => !value.Any(ch => char.IsControl(ch) ||
        CharUnicodeInfo.GetUnicodeCategory(ch) is UnicodeCategory.Format or UnicodeCategory.LineSeparator or UnicodeCategory.ParagraphSeparator);

    private static ToolDeniedException Invalid() => new("mcp_config_invalid", "MCP 服务配置无效，请核对 transport、url、command、args、cwd、env、timeout 和 framing。");
}
