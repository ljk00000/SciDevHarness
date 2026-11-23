using System.ComponentModel;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class ApprovedHostCommands(string workspace, IFileTools inside, IOperationReview? reviewer, Uri modelEndpoint,
    Action<RunEvent> record, string? credential = null, bool denyExtraNetwork = false, bool legacyWorkspaceApproval = false,
    bool legacyLocalCommands = false) : IFileTools
{
    private static readonly JsonSerializerOptions PreviewFormat = new() { WriteIndented = true };
    private readonly WindowsWorkspace _workspace = new(workspace);

    private sealed record Command(string Executable, string Directory, string[] Arguments, int TimeoutSeconds, string? ShellCommand = null);

    public async ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token)
    {
        var local = legacyLocalCommands && call.Name == "run_command";
        using var outside = legacyWorkspaceApproval && call.Name == "run_command" ? ReadLegacyHostRequest(call.Arguments) : null;
        if (call.Name != "run_host_command" && !local && outside is null)
            return await inside.ExecuteAsync(call, token);
        token.ThrowIfCancellationRequested();
        if (denyExtraNetwork)
            throw new ToolDeniedException("network_policy_denied", "断网策略禁止可能联网的宿主命令。");
        if (reviewer is null)
            throw new ToolDeniedException(call.Name == "run_host_command" ? "approval_denied" : "host_approval_required", "宿主命令需要明确启用并逐次审批。");

        var command = local ? ParseLegacyLocal(call.Arguments) : outside is null ? Parse(call.Arguments) : ParseLegacy(outside.RootElement, outsideWorkspace: true);
        var directory = command.Directory == "." ? "scidev-host-cwd" : command.Directory + "/scidev-host-cwd";
        using var cwd = HoldWorkingDirectory(directory);
        var workingDirectory = Path.GetDirectoryName(cwd.AbsolutePath)!;
        if (ContainsCredential(command.Executable) || ContainsCredential(workingDirectory) || command.Arguments.Any(ContainsCredential) ||
            command.ShellCommand is { } raw && ContainsCredential(raw))
            throw new ToolDeniedException("host_command_credential", "宿主命令的路径或参数含有当前模型凭据。");
        var preview = command.ShellCommand ?? JsonSerializer.Serialize(command.Arguments, PreviewFormat);
        var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(command))));

        await RequireAsync(new(call.Id, OperationKind.InspectHostCommand, command.Executable,
            "仅在本机打开此可执行文件并核对身份和 SHA-256。进程启动会另行审批。"), digest, token);
        try
        {
            using var executable = await InspectedHostExecutable.OpenAsync(command.Executable, token);
            var hash = executable.Sha256;
            var detail = $"工作目录 {workingDirectory}\n时间上限 {command.TimeoutSeconds} 秒\nSHA-256 {hash}\n" +
                (command.ShellCommand is not null ? "按下方原文在 Windows 命令解释器运行。允许 shell 管道、命令链和重定向。\n" : "") +
                "本次进程以当前 Windows 用户权限运行，可能读写区外文件或联网。工作区检查点只覆盖文件工具产生的改动。";
            await RequireAsync(new(call.Id, OperationKind.RunHostCommand, command.Executable, detail, Before: preview), digest, token);
            executable.VerifyUnchanged();

            token.ThrowIfCancellationRequested();
            record(new("host_command_starting", 0, new { id = call.Id, commandSha256 = digest, executableSha256 = hash }));
            var timeout = TimeSpan.FromSeconds(command.TimeoutSeconds);
            var result = command.ShellCommand is { } shell
                ? await HostCommandProcess.RunShellAsync(executable.Path, shell, workingDirectory, timeout, token)
                : await HostCommandProcess.RunAsync(executable.Path, command.Arguments, workingDirectory, timeout, token);
            record(new("host_command_exited", 0, new { id = call.Id, result.ExitCode, result.Truncated }));
            var clean = new string(result.Output.Select(character =>
                !char.IsControl(character) && CharUnicodeInfo.GetUnicodeCategory(character) != UnicodeCategory.Format || character is '\r' or '\n' or '\t'
                    ? character : '�').ToArray());
            clean = WorkspaceFiles.RedactCredential(clean, credential);
            var output = $"退出码 {result.ExitCode}" + (result.Truncated ? " · 输出已截断" : "") + "\n" + clean.TrimEnd();
            var destination = WorkspaceFiles.RedactCredential(modelEndpoint.GetLeftPart(UriPartial.Path), credential);
            await RequireAsync(new(call.Id, OperationKind.ShareHostCommandOutput, command.Executable,
                "进程已经结束。把下面的输出预览发送给当前模型。", destination, Before: output), digest, token);
            return new(output, CommandExitCode: result.ExitCode, ExternalDataShared: true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or Win32Exception or TimeoutException)
        {
            throw new ToolDeniedException("host_command_error", "无法核对或运行该宿主程序，请查看已记录的进程状态。", ex);
        }
    }

    private async ValueTask RequireAsync(OperationReview request, string digest, CancellationToken token)
    {
        var audit = new { id = request.CallId, operation = request.Kind.ToString(), commandSha256 = digest };
        record(new("host_review_requested", 0, audit));
        var outcome = await OperationApproval.DecideAsync(reviewer!, request, token);
        record(new("host_review_decided", 0, new { review = audit, outcome }));
        token.ThrowIfCancellationRequested();
        if (outcome == "approved")
            return;
        if (request.Kind == OperationKind.ShareHostCommandOutput)
            throw new ToolDeniedException("host_output_not_shared", "宿主命令已运行，输出未获准发送给模型。");
        throw new ToolDeniedException("approval_denied", "本次宿主命令操作未获批准。");
    }

    private bool ContainsCredential(string value) => !string.IsNullOrEmpty(credential) && value.Contains(credential, StringComparison.Ordinal);

    private WindowsWorkspace.Lease HoldWorkingDirectory(string path)
    {
        try { return _workspace.OpenPath(path); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            throw new ToolDeniedException("path_denied", "不能打开已指定的宿主命令工作目录。", ex);
        }
    }

    private static JsonDocument? ReadLegacyHostRequest(string arguments)
    {
        try
        {
            var document = JsonDocument.Parse(arguments);
            if (LegacyShellRequest.RequestsHost(document.RootElement))
                return document;
            document.Dispose();
        }
        catch (JsonException) { }
        return null;
    }

    private static Command ParseLegacyLocal(string arguments)
    {
        try
        {
            using var document = JsonDocument.Parse(arguments);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
                throw Invalid();
            return ParseLegacy(document.RootElement, outsideWorkspace: false);
        }
        catch (JsonException) { throw Invalid(); }
    }

    private static Command ParseLegacy(JsonElement root, bool outsideWorkspace)
    {
        var request = LegacyShellRequest.Read(root, outsideWorkspace);
        var directory = request.Directory == "." ? "." : WorkspaceDocuments.VisiblePath(request.Directory, allowHidden: false);
        if (!Displayable(request.Command) || !Displayable(directory))
            throw new ToolDeniedException("invalid_tool", "旧版宿主命令需要单行可见文本和工作区相对目录。");
        if (CommandGuard.BlockedShell(request.Command))
            throw new ToolDeniedException("host_command_blocked", "旧版宿主命令含破坏性操作或由 Harness 管理的 Git 修改。");
        var executable = Path.Combine(Environment.SystemDirectory, "cmd.exe");
        return new(executable, directory, [], request.TimeoutSeconds, request.Command);
    }

    private static Command Parse(string arguments)
    {
        try
        {
            using var document = JsonDocument.Parse(arguments);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                throw Invalid();
            var names = new HashSet<string>(StringComparer.Ordinal);
            if (root.EnumerateObject().Any(property => !names.Add(property.Name) ||
                property.Name is not ("executable" or "argv" or "directory" or "timeout_seconds")))
                throw Invalid();
            var executable = WindowsWorkspace.ValidateAbsoluteFilePath(WorkspaceFiles.RequiredString(root, "executable"));
            if (!Path.GetExtension(executable).Equals(".exe", StringComparison.OrdinalIgnoreCase) || !Displayable(executable))
                throw Invalid();
            var directory = root.TryGetProperty("directory", out var path) ? path.GetString() ?? throw Invalid() : ".";
            if (directory != ".")
                directory = WorkspaceDocuments.VisiblePath(directory, allowHidden: false);
            var timeout = root.TryGetProperty("timeout_seconds", out var limit) ? limit.GetInt32() : 30;
            if (timeout is < 1 or > 120 || !root.TryGetProperty("argv", out var values) || values.ValueKind != JsonValueKind.Array ||
                values.GetArrayLength() > 64 || !Displayable(directory))
                throw Invalid();
            var argv = values.EnumerateArray().Select(value => value.GetString() ?? throw Invalid()).ToArray();
            if (argv.Sum(value => (long)value.Length) > 4096 || !argv.All(Displayable))
                throw Invalid();
            return new(executable, directory, argv, timeout);
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException) { throw Invalid(); }
    }

    private static bool Displayable(string value) => !value.Any(ch => char.IsControl(ch) ||
        CharUnicodeInfo.GetUnicodeCategory(ch) is UnicodeCategory.Format or UnicodeCategory.LineSeparator or UnicodeCategory.ParagraphSeparator);

    private static ToolDeniedException Invalid() => new("invalid_tool", "宿主命令需要绝对 EXE 路径及分开的 argv 参数，工作目录在工作区内。");
}
