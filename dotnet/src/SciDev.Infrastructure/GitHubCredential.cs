using System.ComponentModel;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class GitHubCredential
{
    private static readonly string[] TokenArguments = ["auth", "token", "--hostname", "github.com", "--no-refresh"];

    public static async Task<string> ReadAsync(string workspace, IOperationReview reviewer, CancellationToken token)
    {
        var configured = Environment.GetEnvironmentVariable("GH_TOKEN")?.Trim();
        if (string.IsNullOrEmpty(configured))
            configured = Environment.GetEnvironmentVariable("GITHUB_TOKEN")?.Trim();
        if (!string.IsNullOrEmpty(configured))
            return Validate(configured);

        var tool = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "GitHub CLI", "gh.exe");
        await RequireAsync(reviewer, new("github-credential-inspect", OperationKind.InspectHostCommand, tool,
            "核对已安装的 GitHub CLI。读取本机登录凭据将另行审批。"), token);
        try
        {
            using var executable = await InspectedHostExecutable.OpenAsync(tool, token);
            await RequireAsync(reviewer, new("github-credential-read", OperationKind.RunHostCommand, executable.Path,
                $"读取 GitHub CLI 已保存的 github.com 凭据。凭据仅供本次 GitHub API 调用使用，关闭自动刷新。\nSHA-256 {executable.Sha256}",
                Before: System.Text.Json.JsonSerializer.Serialize(TokenArguments)), token);
            executable.VerifyUnchanged();
            var environment = new Dictionary<string, string>
            {
                ["GH_PROMPT_DISABLED"] = "1", ["NO_COLOR"] = "1", ["GH_NO_UPDATE_NOTIFIER"] = "1",
                ["GH_NO_EXTENSION_UPDATE_NOTIFIER"] = "1", ["GH_TELEMETRY"] = "0",
            };
            foreach (var key in new[] { "APPDATA", "LOCALAPPDATA", "USERPROFILE" })
                if (Environment.GetEnvironmentVariable(key) is { } value)
                    environment[key] = value;
            var result = await HostCommandProcess.RunAsync(executable.Path, TokenArguments,
                workspace, TimeSpan.FromSeconds(10), token, environment);
            if (result.ExitCode != 0 || result.Truncated)
            {
                if (result.Output.Contains("unknown flag: --no-refresh", StringComparison.OrdinalIgnoreCase))
                    throw new ToolDeniedException("github_cli_no_refresh_unsupported", "已安装的 GitHub CLI 不支持禁止刷新。请通过 GH_TOKEN 或 GITHUB_TOKEN 提供 GitHub 凭据。");
                throw Missing();
            }
            return Validate(result.Output.Trim());
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or Win32Exception or TimeoutException)
        { throw Missing(); }
    }

    internal static async ValueTask RequireAsync(IOperationReview reviewer, OperationReview request, CancellationToken token)
    {
        var decision = await OperationApproval.DecideAsync(reviewer, request, token);
        token.ThrowIfCancellationRequested();
        if (decision != "approved")
            throw new ToolDeniedException("approval_denied", "本次 GitHub PR 操作未获批准。");
    }

    internal static string Validate(string token) =>
        token.Length is >= 16 and <= 512 && token.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is '_' or '-' or '.')
            ? token : throw Missing();

    private static ToolDeniedException Missing() =>
        new("github_credential_unavailable", "无法使用 GitHub 凭据。请配置 GH_TOKEN、GITHUB_TOKEN，或登录支持 --no-refresh 的 GitHub CLI。");
}
