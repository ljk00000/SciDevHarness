using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed record LegacyContainerCommand(string[] Arguments, string Directory, int TimeoutSeconds, string[]? PolicyArguments)
{
    internal static LegacyContainerCommand Read(JsonElement root)
    {
        var (command, directory, seconds) = LegacyShellRequest.Read(root, outsideWorkspace: false);
        // Recognize plain words for policy checks while preserving the shell's builtin behavior at launch.
        var simple = command.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is ' ' or '\t' or '.' or '_' or '-' or '/' or ':' or '@' or '+');
        var words = simple ? command.Split([' ', '\t'], StringSplitOptions.RemoveEmptyEntries) : null;
        if (words is { } plain && (plain.Length is 0 or > 64 || plain[0].StartsWith('-')))
            throw Invalid();
        return new(["/bin/sh", "-c", command], directory, seconds, words);
    }

    private static ToolDeniedException Invalid() => new("invalid_tool", "旧版容器命令需要 command 文本，可附容器目录和秒数。宿主命令使用单独的审批入口。");
}

internal sealed record LegacyShellRequest(string Command, string Directory, int TimeoutSeconds)
{
    internal static bool RequestsHost(JsonElement root) => root.ValueKind == JsonValueKind.Object &&
        root.EnumerateObject().Any(property => property.NameEquals("outside_workspace") && property.Value.ValueKind == JsonValueKind.True);

    internal static LegacyShellRequest Read(JsonElement root, bool outsideWorkspace)
    {
        var names = new HashSet<string>(StringComparer.Ordinal);
        if (root.EnumerateObject().Any(property => !names.Add(property.Name) ||
            property.Name is not ("command" or "outside_workspace" or "timeout_seconds" or "directory")))
            throw Invalid();
        if (root.TryGetProperty("outside_workspace", out var outside) && outside.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            throw Invalid();
        if (!outsideWorkspace && outside.ValueKind == JsonValueKind.True)
            throw new ToolDeniedException("host_approval_required", "旧版宿主命令需要工作区审批模式和显式启用的宿主审批。");
        if ((outsideWorkspace && outside.ValueKind != JsonValueKind.True) || !root.TryGetProperty("command", out var raw) ||
            raw.ValueKind != JsonValueKind.String || raw.GetString() is not { } command || string.IsNullOrWhiteSpace(command) ||
            command.Length > 4096 || command.Any(ch => char.IsControl(ch) && ch is not ('\r' or '\n' or '\t')))
            throw Invalid();
        var directory = root.TryGetProperty("directory", out var cwd) ? cwd.ValueKind == JsonValueKind.String ? cwd.GetString()! : throw Invalid() : ".";
        var seconds = 120;
        if (root.TryGetProperty("timeout_seconds", out var timeout) && timeout.ValueKind != JsonValueKind.Null &&
            (timeout.ValueKind != JsonValueKind.Number || !timeout.TryGetInt32(out seconds)))
            throw Invalid();
        return new(command, directory, Math.Clamp(seconds, 1, 300));
    }

    private static ToolDeniedException Invalid() => new("invalid_tool", "旧版命令需要 command 文本，可附工作区相对目录和秒数。");
}
