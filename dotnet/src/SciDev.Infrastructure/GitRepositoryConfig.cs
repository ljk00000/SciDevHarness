using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class GitRepositoryConfig
{
    public static async Task<Dictionary<string, string>> ReadAsync(GitReadContext query, string config, CancellationToken token)
    {
        var settings = await query.RunAsync(["config", "--file", config, "--no-includes", "--null", "--list"],
            token, maxChars: 1_000_000);
        if (settings.Code != 0 || settings.Truncated)
            throw new ToolDeniedException("git_config_error", "无法核对 Git 仓库配置。");
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in settings.Text.Split('\0', StringSplitOptions.RemoveEmptyEntries))
        {
            var separator = entry.IndexOf('\n');
            var key = separator < 0 ? entry : entry[..separator];
            var value = separator < 0 ? "true" : entry[(separator + 1)..];
            if (key.StartsWith("include.", StringComparison.OrdinalIgnoreCase) ||
                key.StartsWith("includeif.", StringComparison.OrdinalIgnoreCase))
                throw new ToolDeniedException("git_commit_config_include", "此仓库包含 Git 配置引用，请通过本地 Git 处理。");
            values[key] = value;
        }
        return values;
    }
}
