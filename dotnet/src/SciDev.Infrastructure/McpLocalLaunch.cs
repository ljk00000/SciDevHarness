using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class McpLocalLaunch
{
    private static readonly JsonSerializerOptions ArgumentsPreview = new() { WriteIndented = true };

    public static async Task<LocalStdioProcess> StartAsync(string workspace, string name, McpLocalConfiguration.Stdio config,
        IOperationReview? reviewer, string? key, CancellationToken token)
    {
        WorkspaceNetworkPolicy.RequireAllowed(workspace, "断网策略禁止启动可能联网的宿主 MCP 服务。");
        if (reviewer is null)
            throw new ToolDeniedException("approval_required", "启动宿主 MCP 服务需要交互终端逐次批准。");

        workspace = Path.GetFullPath(workspace);
        var cwd = Path.GetRelativePath(workspace, Path.GetFullPath(config.Directory, workspace));
        var marker = cwd == "." ? "scidev-mcp-cwd" : WorkspaceDocuments.VisiblePath(cwd, allowHidden: true) + "/scidev-mcp-cwd";
        using var directory = new WindowsWorkspace(workspace).OpenOperatorPath(marker)!;
        var resolvedDirectory = Path.GetDirectoryName(directory.AbsolutePath)!;
        CheckCredential(config.Arguments.Concat(config.Environment.Values).Concat(config.Environment.Keys)
            .Concat([resolvedDirectory, config.Command]), key);
        if (new[] { "SCIDEV_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY" }.Any(config.Environment.ContainsKey))
            throw new ToolDeniedException("mcp_credential", "宿主 MCP 服务配置不能传入模型密钥变量。");

        var id = "mcp-" + name;
        await RequireAsync(new(id, OperationKind.InspectHostCommand, config.Command,
            "核对配置中的 MCP 服务程序及 SHA-256。若是裸命令，将查找本机 PATH 中的 EXE。"), reviewer, token);
        var path = Resolve(workspace, config.Command);
        CheckCredential([path], key);
        using var executable = await InspectedHostExecutable.OpenAsync(path, token);
        var environmentNames = JsonSerializer.Serialize(config.Environment.Keys.Order(StringComparer.OrdinalIgnoreCase));
        var detail = $"MCP 服务 {name}\n工作目录 {JsonSerializer.Serialize(resolvedDirectory)}\nSHA-256 {executable.Sha256}\n" +
            $"配置传入的环境变量名称 {environmentNames}\n本次进程以当前 Windows 用户权限运行，可能读写区外文件或联网。";
        await RequireAsync(new(id, OperationKind.RunHostCommand, executable.Path, detail,
            Before: JsonSerializer.Serialize(config.Arguments, ArgumentsPreview)), reviewer, token);
        executable.VerifyUnchanged();
        token.ThrowIfCancellationRequested();
        return LocalStdioProcess.Start(executable.Path, config.Arguments, resolvedDirectory, config.Environment);
    }

    internal static void CheckCredential(IEnumerable<string> values, string? key)
    {
        if (!string.IsNullOrEmpty(key) && values.Any(value => value.Contains(key, StringComparison.Ordinal)))
            throw new ToolDeniedException("mcp_credential", "MCP 请求或服务配置包含当前模型密钥。");
    }

    private static async Task RequireAsync(OperationReview request, IOperationReview reviewer, CancellationToken token)
    {
        var decision = await OperationApproval.DecideAsync(reviewer, request, token);
        token.ThrowIfCancellationRequested();
        if (decision != "approved")
            throw new ToolDeniedException("approval_denied", "本次 MCP 宿主操作未获批准。");
    }

    private static string Resolve(string workspace, string command)
    {
        if (Path.GetExtension(command).Length == 0)
            command += ".exe";
        if (!Path.GetExtension(command).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            throw new ToolDeniedException("host_command_executable", "MCP stdio 服务需要 Windows EXE。");
        if (Path.IsPathFullyQualified(command))
            return WindowsWorkspace.ValidateAbsoluteFilePath(command);
        if (command.IndexOfAny(['/', '\\']) >= 0 || Path.IsPathRooted(command))
        {
            var relative = command.StartsWith("./", StringComparison.Ordinal) || command.StartsWith(".\\", StringComparison.Ordinal) ? command[2..] : command;
            return Path.Combine(workspace, WorkspaceDocuments.VisiblePath(relative, allowHidden: true));
        }

        var paths = Environment.GetEnvironmentVariable("PATH")?.Split(';') ?? [];
        foreach (var item in paths.Prepend(Environment.SystemDirectory))
        {
            var directory = item.Trim().Trim('"');
            if (!Path.IsPathFullyQualified(directory))
                continue;
            try
            {
                using var candidate = WindowsWorkspace.OpenAbsoluteFilePath(Path.Combine(directory, command));
                if (WindowsWorkspace.FileExists(candidate.AbsolutePath))
                    return candidate.AbsolutePath;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException) { }
        }
        throw new ToolDeniedException("host_command_executable", "在本机 PATH 中找不到普通 EXE，请在 MCP 配置里指定完整路径。");
    }
}
