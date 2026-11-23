using System.Diagnostics;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Expose a Linux project command against a disposable copy of the workspace.</summary>
public sealed class DockerWorkspaceTools(string workspace, WorkspaceFiles files, string image, string? credential = null)
    : IFileTools
{
    private const string PrepareScript = """
        set -eu
        mkdir /tmp/project /tmp/home
        cp -R /workspace/. /tmp/project
        cd "/tmp/project/$1"
        shift
        export HOME=/tmp/home TMPDIR=/tmp
        """;
    private const string ExecuteScript = PrepareScript + "\nexec \"$@\"";
    private const string RestoreDotnetTest = """
        export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
        export NUGET_ENHANCED_MAX_NETWORK_TRY_COUNT=1 NUGET_CERT_REVOCATION_MODE=offline
        dotnet restore "$3" --ignore-failed-sources --disable-parallel -p:NuGetAudit=false
        """;
    private readonly string _root = Path.GetFullPath(workspace);
    private readonly WindowsWorkspace _workspace = new(workspace);
    internal SandboxCommandApproval? CommandApproval { get; init; }
    internal WorkspaceDrafts Drafts { get; init; } = WorkspaceDrafts.Empty;
    internal bool LegacyCommandArguments { get; init; }

    public static void Validate(string workspace, string image)
    {
        DockerProbe.ValidateImage(image);
        if (workspace.Contains(',') || workspace.Contains('"'))
            throw new ArgumentException("沙箱工作区路径不能含逗号或引号。");
    }

    public ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token)
    {
        if (call.Name != "run_command")
            return files.ExecuteAsync(call, token);
        var (argv, directory, timeout, policyArguments) = ReadArguments(call.Arguments);
        return ExecuteCommandAsync(call.Id, argv, directory, token, timeoutSeconds: timeout, policyArguments: policyArguments);
    }

    internal ValueTask<ToolResult> RunVerificationAsync(string callId, VerificationCandidate candidate, CancellationToken token)
    {
        if (SandboxVerification.Classify(candidate.Arguments) != candidate.Category)
            throw new ToolDeniedException("verification_policy", "候选命令不符合自动验证策略。");
        var directory = CommandDirectory(candidate.Directory);
        var restoreDotnet = candidate is { Category: "test", Arguments: ["dotnet", "test", var project, "--no-restore"] } &&
            DotnetTestProjects.IsProject(project);
        if (candidate.ReferencedProject is not { } referenced)
            return ExecuteCommandAsync(callId, candidate.Arguments, directory, token,
                targetDirectories: candidate.TargetDirectories, restoreDotnet: restoreDotnet);
        var subject = WorkspaceDocuments.VisiblePath(referenced, allowHidden: false);
        var subjectDirectory = WorkspaceProjectContext.DirectoryPart(subject);
        if (!DotnetTestProjects.IsProject(subject) || directory != (subjectDirectory.Length == 0 ? "." : subjectDirectory) ||
            SandboxVerification.Classify(["dotnet", "test", subject, "--no-restore"]) != "test" ||
            candidate.Arguments is not ["dotnet", "test", var test, "--no-restore"] || !DotnetTestProjects.IsProject(test) ||
            WorkspaceDocuments.VisiblePath(test, allowHidden: false) != test)
            throw new ToolDeniedException("verification_policy", "无法核对 .NET 项目的测试引用。");
        return ExecuteCommandAsync(callId, candidate.Arguments, ".", token, subject);
    }

    internal ValueTask<ToolResult> RunEvaluationCheckAsync(string taskId, string[] arguments, int timeoutSeconds, CancellationToken token) =>
        ExecuteCommandAsync(taskId, arguments, ".", token, timeoutSeconds: timeoutSeconds);

    private async ValueTask<ToolResult> ExecuteCommandAsync(string callId, string[] argv, string directory, CancellationToken token,
        string? referencedProject = null, int timeoutSeconds = 90, string[]? policyArguments = null,
        IReadOnlyList<string>? targetDirectories = null, bool restoreDotnet = false)
    {
        token.ThrowIfCancellationRequested();
        if (!string.IsNullOrEmpty(credential) && (directory.Contains(credential, StringComparison.Ordinal) ||
            referencedProject?.Contains(credential, StringComparison.Ordinal) == true ||
            argv.Any(arg => arg.Contains(credential, StringComparison.Ordinal))))
            throw new ToolDeniedException("sandbox_content_denied", "命令参数或目录包含当前模型凭据。");
        var recognizedArguments = policyArguments ?? argv;
        var verificationCategory = SandboxVerification.EvidenceCategory(recognizedArguments);
        var automaticCategory = CommandApproval?.AutomaticCategory(argv, policyArguments);
        if (automaticCategory is null)
            CommandApproval?.RequireReviewer();
        var docker = DockerProbe.DockerExecutable();
        if (!File.Exists(docker))
            throw DockerProbe.Unavailable("docker_cli_missing");

        var state = _workspace.EnsureStateDirectory();
        var name = $"scidev-command-{Guid.NewGuid():N}";
        using var stage = state.OpenPath(name);
        Directory.CreateDirectory(stage.AbsolutePath);
        try
        {
            var staging = new WindowsWorkspace(stage.AbsolutePath);
            using var stagingLease = staging.OpenPath("local-snapshot");
            var config = Directory.CreateDirectory(Path.Combine(stage.AbsolutePath, "docker-config")).FullName;
            var id = await DockerProbe.RequireLocalImageAsync(docker, config, image, token);
            var fixture = Directory.CreateDirectory(Path.Combine(stage.AbsolutePath, "source")).FullName;
            using var mountLease = new WindowsWorkspace(fixture).OpenPath("local-snapshot");
            var snapshotHash = new WorkspaceSnapshot(_root, credential, Drafts).Capture(fixture, token);
            directory = ResolveSnapshotDirectory(fixture, directory);
            var evidenceDirectory = directory;
            if (referencedProject is not null)
            {
                if (!DotnetTestProjects.SnapshotHasProjects(fixture, argv[2], referencedProject))
                    throw new ToolDeniedException("verification_directory", "容器副本缺少应验证的项目或测试项目。");
                evidenceDirectory = WorkspaceProjectContext.DirectoryPart(referencedProject);
                if (evidenceDirectory.Length == 0)
                    evidenceDirectory = ".";
            }
            if (!string.IsNullOrEmpty(credential) && directory.Contains(credential, StringComparison.Ordinal))
                throw new ToolDeniedException("sandbox_content_denied", "命令目录包含当前模型凭据。");
            if (CommandApproval is { } approval)
            {
                if (automaticCategory is null)
                    await approval.RequireAsync(callId, id, argv, directory, Drafts.Count > 0, token, timeoutSeconds);
                else
                    approval.RecordAutomatic(callId, id, argv, directory, automaticCategory, timeoutSeconds);
            }
            string[]? coveredDirectories = null;
            var elapsed = Stopwatch.StartNew();
            if (SandboxVerification.IsCargoWorkspaceTest(argv))
            {
                var metadata = await DockerProbe.RunContainerAsync(docker, config, id, fixture, name + "-metadata", ExecuteScript, token,
                    argv: [directory, "cargo", "metadata", "--format-version", "1", "--no-deps", "--offline"],
                    commandTimeoutSeconds: Math.Min(20, timeoutSeconds), stdoutBeginning: 1_000_000);
                coveredDirectories = metadata.ExitCode == 0 ? CargoWorkspaceTests.ReadMetadata(metadata.Output) : [];
                if (!string.IsNullOrEmpty(credential))
                    coveredDirectories = coveredDirectories.Where(scope => !scope.Contains(credential, StringComparison.Ordinal)).ToArray();
                if (targetDirectories is not null && !coveredDirectories.Intersect(targetDirectories, StringComparer.OrdinalIgnoreCase).Any())
                    throw new ToolDeniedException("verification_directory", "容器副本的 Cargo 元数据未确认候选覆盖的项目。");
            }
            var remaining = timeoutSeconds - (int)elapsed.Elapsed.TotalSeconds;
            if (remaining <= 0)
                throw new TimeoutException("No time remains for the container command.");
            var traceGradle = verificationCategory == "test" && GradleTestTrace.IsTest(recognizedArguments);
            var marker = "scidev-trace-" + name["scidev-command-".Length..];
            var script = ExecuteScript;
            string[] command = [directory, .. argv];
            if (referencedProject is not null)
            {
                script = PrepareScript + "\n" + DotnetReferenceTrace.Script(RestoreDotnetTest);
                command = [directory, marker, referencedProject, .. argv];
            }
            else if (restoreDotnet)
                script = PrepareScript + "\n" + RestoreDotnetTest + "\nexec \"$@\"";
            else if (traceGradle)
            {
                script = PrepareScript + "\n" + GradleTestTrace.Script;
                command = [directory, marker, .. recognizedArguments];
            }
            var result = await DockerProbe.RunContainerAsync(docker, config, id, fixture, name, script, token,
                argv: command, commandTimeoutSeconds: remaining);
            if (result.ExitCode == 125)
                throw DockerProbe.Unavailable(DockerProbe.FailureStatus(result.Error, "sandbox_probe_failed"));
            var draftNote = Drafts.Count > 0 ? "\n容器副本已排除任务启动时的未保存文件；验证结果仅覆盖副本。" : "";
            var (stdout, verifiedScope) = referencedProject is null ? (result.Output, true) : DotnetReferenceTrace.Read(result.Output, marker);
            string[] gradleRecords = [];
            if (traceGradle)
                (stdout, gradleRecords) = ContainerVerificationTrace.Read(stdout, marker);
            var output = (Redact(stdout) + "\n" + Redact(result.Error)).TrimEnd();
            var testIssue = verificationCategory == "test" && result.ExitCode == 0
                ? coveredDirectories is { Length: 0 } ? "test_result_unconfirmed" :
                    (traceGradle ? GradleTestTrace.UnsuccessfulTest(output, gradleRecords, recognizedArguments) :
                        TestRunEvidence.UnsuccessfulTest(recognizedArguments, stdout + "\n" + result.Error)) ??
                    (!verifiedScope ? "test_result_unconfirmed" : null)
                : null;
            var testNote = testIssue switch
            {
                "no_tests" => "\n测试命令没有执行有效测试，当前结果无法作为验证依据。",
                "test_result_unconfirmed" when !verifiedScope => "\n测试构建未确认对目标项目的实际引用，当前结果无法验证该项目。",
                "test_result_unconfirmed" => "\n未找到可确认的测试结果，当前结果无法作为验证依据。",
                _ => "",
            };
            var diagnostics = verificationCategory is not null && result.ExitCode != 0
                ? VerificationDiagnostics.Parse(verificationCategory, output, directory) : [];
            var details = diagnostics.Length == 0 ? "" : "\n结构化诊断（容器输出，仅供核对）\n" + string.Join('\n', diagnostics);
            return new($"容器命令退出码 {result.ExitCode}{draftNote}{testNote}{details}\n{output}".TrimEnd(),
                CommandExitCode: result.ExitCode,
                Verification: verificationCategory is null || !verifiedScope && result.ExitCode != 0 ? null : new(verificationCategory, result.ExitCode, snapshotHash,
                    Drafts.Count > 0, diagnostics.Length == 0 ? null : diagnostics, evidenceDirectory, testIssue, coveredDirectories));
        }
        catch (TimeoutException ex)
        {
            throw new ToolDeniedException("sandbox_timeout", "容器命令或 Docker 客户端达到时间上限。", ex);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or
            ArgumentException or System.ComponentModel.Win32Exception)
        {
            throw new ToolDeniedException("sandbox_error", "无法准备工作区副本、运行或清理容器。", ex);
        }
        finally
        {
            var folder = new DirectoryInfo(stage.AbsolutePath);
            if (folder.Name == name &&
                string.Equals(folder.Parent?.FullName, Path.Combine(_root, ".research"), StringComparison.OrdinalIgnoreCase) &&
                (folder.Attributes & FileAttributes.ReparsePoint) == 0)
                folder.Delete(recursive: true);
        }
    }

    internal string? CurrentFingerprint(CancellationToken token)
    {
        try { return new WorkspaceSnapshot(_root, credential, Drafts).Capture(null, token); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException) { return null; }
    }

    private static string CommandDirectory(string directory) =>
        directory == "." ? "." : WorkspaceDocuments.VisiblePath(directory, allowHidden: false);

    private static string ResolveSnapshotDirectory(string fixture, string directory)
    {
        if (directory == ".")
            return directory;
        var current = new DirectoryInfo(fixture);
        var resolved = new List<string>();
        foreach (var name in directory.Split('/'))
        {
            // Use the copied name's spelling because Docker resolves the path on Linux.
            current = current.EnumerateDirectories().FirstOrDefault(child => child.Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                ?? throw new ToolDeniedException("verification_directory", "指定目录不在本次容器工作区副本中。");
            resolved.Add(current.Name);
        }
        return string.Join('/', resolved);
    }

    private (string[] Argv, string Directory, int Timeout, string[]? PolicyArguments) ReadArguments(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (LegacyCommandArguments && root.ValueKind == JsonValueKind.Object && root.TryGetProperty("command", out _))
            {
                var command = LegacyContainerCommand.Read(root);
                return (command.Arguments, CommandDirectory(command.Directory), command.TimeoutSeconds, command.PolicyArguments);
            }
            if (root.ValueKind == JsonValueKind.Object &&
                root.TryGetProperty("argv", out var value) && value.ValueKind == JsonValueKind.Array &&
                value.GetArrayLength() is >= 1 and <= 64 && value.EnumerateArray().All(item => item.ValueKind == JsonValueKind.String))
            {
                var scoped = root.TryGetProperty("directory", out var directory);
                var argv = value.EnumerateArray().Select(item => item.GetString()!).ToArray();
                if (root.EnumerateObject().Count() == (scoped ? 2 : 1) && (!scoped || directory.ValueKind == JsonValueKind.String) &&
                    !string.IsNullOrWhiteSpace(argv[0]) && !argv[0].StartsWith('-') &&
                    argv.Sum(arg => (long)arg.Length) <= 4096 && argv.All(arg => !arg.Contains('\0')))
                    return (argv, scoped ? CommandDirectory(directory.GetString()!) : ".", 90, null);
            }
        }
        catch (JsonException) { }
        throw new ToolDeniedException("invalid_tool", "run_command 需要 argv 字符串数组，可附工作区相对目录 directory。");
    }

    private string Redact(string value) => string.IsNullOrEmpty(credential) ? value :
        value.Replace(credential, "[redacted]", StringComparison.Ordinal);
}
