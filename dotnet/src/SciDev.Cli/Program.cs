using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using SciDev.Cli;
using SciDev.Core;
using SciDev.Infrastructure;

return await Cli.RunAsync(args);

internal static class Cli
{
    private const string Usage = "exec --workspace <directory> --task <text> [--api-base <url>] "
        + "[--model <id>] [--max-tokens <n>] [--max-turns <n>] [--patch-approval reject|review] [--json]\n"
        + "    [--execution-mode workspace-approval|workspace-write|manual|read-only|full-access] [--sandbox-image <local-image>] [--restore-on-failure]\n"
        + "    [--sandbox docker] [--request-timeout <5..600 seconds>] [--allow-no-changes] [--result-file <workspace-file>]\n"
        + "    [--image <PNG/JPEG/GIF/WebP>] (repeat up to 4 times)\n"
        + "    [--outside-approval reject|review] [--command-approval reject|review] [--network-approval reject|review] [--git-approval reject|review]\n"
        + "    [--host-approval reject|review] [--mcp-approval reject|review] [--allowed-commands <container-prefixes>]\n"
        + "    [--use-legacy-policy] [--network-policy ask|deny]\n"
        + "    [--planning-mode auto|required|off] [--no-verify] [--auto-commit reject|review]\n"
        + "    [--worktree [--worktree-id <id>] [--metadata-workspace <approved-repository>]]\n"
        + "    [--session-id <id> [--retry-after-inspection <run-id>]]\n"
        + "    [--resume-python-session --session-id <id> --api-base <explicit-url> [--accept-python-recovery <revision>]]\n"
        + "    [--credential-profile <name> --api-base <explicit-url>]\n"
        + "    [--pelican-native | --pelican-validator <trusted-script> --validator-python <python.exe>]\n"
        + DoctorCommand.Usage + "\n"
        + "sandbox-check --workspace <directory> --sandbox-image <local-image> | terminal-check\n"
        + "check-pelican --svg <input.svg> [--preview <new.png>]\n"
        + "migrate --workspace <python-workspace-copy> (--dry-run | --snapshot <new-directory> [--qt-preferences <export.json>])\n"
        + "history --workspace <directory> [--limit 1..100]\n"
        + "sessions --workspace <directory> [--limit 1..100] [--json]\n"
        + "eval --suite <file> --sandbox-image <local-image> [--workspace <directory>] [--output <workspace-file>] [--jobs 1..8]\n"
        + "    [--api-base <url>] [--model <id>] [--max-tokens <n>] [--json]\n"
        + McpCommand.Usage + "\n"
        + "mcp-serve [--workspace <directory>]\n"
        + PullRequestCommand.Usage + "\n"
        + UpdateCommand.Usage + "\n"
        + "git-status --workspace <directory> [--metadata-workspace <approved-repository>]\n"
        + "git-init --workspace <directory> [--initial-branch <name>]\n"
        + "git-diff --workspace <directory> --path <relative-path> [--original <relative-path>] [--metadata-workspace <approved-repository>]\n"
        + "git-stage | git-unstage --workspace <directory> --path <relative-path> [--original <relative-path>] [--metadata-workspace <approved-repository>]\n"
        + "git-stage-all --workspace <directory> [--metadata-workspace <approved-repository>]\n"
        + "git-commit-preview --workspace <directory> [--metadata-workspace <approved-repository>]\n"
        + "git-commit --workspace <directory> --message <text> --name <author> --email <address> [--metadata-workspace <approved-repository>]\n"
        + "git-worktree-list --workspace <directory> [--metadata-workspace <approved-repository>]\n"
        + "git-worktree-create --workspace <directory> --worktree-id <id> [--metadata-workspace <approved-repository>]\n"
        + "git-worktree-remove --workspace <directory> --worktree-id <id> [--metadata-workspace <approved-repository>]\n"
        + "inspect-run --workspace <directory> --run-id <id> [--source csharp|python]\n"
        + "restore-run --workspace <directory> --run-id <id>";
    private static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

    public static async Task<int> RunAsync(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args.Length == 0 || args is ["--help"] or ["-h"] or ["exec", "--help"])
        {
            Console.WriteLine(Usage);
            return 0;
        }
        if (args[0] == "eval")
            return await EvalCommand.RunAsync(args, Json);
        if (args[0] == "mcp")
            return await McpCommand.RunAsync(args, Json);
        if (args[0] == "mcp-serve")
            return await McpServeCommand.RunAsync(args);
        if (args[0] is "pr" or "pr-view" or "pr-diff" or "pr-review")
            return await PullRequestCommand.RunAsync(args, Json);
        if (args[0] == "update")
            return await UpdateCommand.RunAsync(args, Json);
        if (args[0] == "doctor")
            return await DoctorCommand.RunAsync(args, Json);
        if (args[0] is "sandbox-check" or "terminal-check" or "check-pelican" or "migrate" or "history" or "sessions" or "inspect-run" or
            "restore-run" or "git-status" or "git-init" or "git-diff" or "git-stage" or "git-unstage" or "git-stage-all" or "git-commit-preview" or "git-commit" or
            "git-worktree-list" or "git-worktree-create" or "git-worktree-remove")
            return await Diagnostics.RunAsync(args, Json);

        var asJson = args.Contains("--json");
        var runId = Guid.NewGuid().ToString("N");
        var key = Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim();
        RunResult result;
        string? eventsPath = null;
        string? resultFile = null;
        string? resultWorkspace = null;
        string? sessionId = null;
        string? checkpointId = null;
        WorkspaceCheckpoint.RestoreResult? checkpointRestore = null;
        TaskCommitResult? gitCommit = null;
        GitWorktrees.Created? taskWorktree = null;
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            if (!OperatingSystem.IsWindows())
                throw new ArgumentException("当前原型只支持 Windows。");
            var imagePaths = new List<string>();
            var options = Parse(args, imagePaths);
            var workspace = Path.GetFullPath(Required(options, "--workspace"));
            resultWorkspace = workspace;
            resultFile = options.GetValueOrDefault("--result-file");
            var task = Required(options, "--task");
            var legacy = options.ContainsKey("--use-legacy-policy")
                ? LegacyExecutionPolicy.Read(workspace) ?? throw new ToolDeniedException("legacy_policy_missing", "当前工作区没有可应用的旧版权限配置。") : null;
            var executionMode = options.GetValueOrDefault("--execution-mode") switch
            {
                "workspace-approval" => ExecutionMode.WorkspaceApproval,
                "workspace-write" => ExecutionMode.WorkspaceWrite,
                "read-only" => ExecutionMode.ReadOnly,
                "manual" => ExecutionMode.Manual,
                "full-access" => ExecutionMode.FullAccess,
                null => legacy is null ? ExecutionMode.WorkspaceApproval : legacy.Mode ??
                    throw new ArgumentException("旧版完全访问需要显式指定 --execution-mode full-access 或选择其他模式。"),
                _ => throw new ArgumentException("--execution-mode 支持 workspace-approval、workspace-write、manual、read-only 和 full-access。"),
            };
            var sandboxImage = options.GetValueOrDefault("--sandbox-image") ?? legacy?.ImageFor(executionMode);
            if (options.GetValueOrDefault("--sandbox") is { } backend && (backend != "docker" || string.IsNullOrWhiteSpace(sandboxImage)))
                throw new ArgumentException("--sandbox 仅支持 docker，且需要已在本机的镜像。宿主命令使用 --host-approval review。");
            var networkPolicy = options.GetValueOrDefault("--network-policy") switch
            {
                null => legacy?.EffectiveNetwork ?? AgentNetworkPolicy.Ask,
                "ask" => AgentNetworkPolicy.Ask,
                "deny" => AgentNetworkPolicy.Deny,
                _ => throw new ArgumentException("--network-policy 只支持 ask 或 deny。"),
            };
            var planning = options.GetValueOrDefault("--planning-mode", "off") switch
            {
                "auto" => PlanningMode.Auto,
                "required" => PlanningMode.Required,
                "off" => PlanningMode.Off,
                _ => throw new ArgumentException("--planning-mode 支持 auto、required 和 off。"),
            };
            var validator = ValidatorOptions(options, workspace);
            var address = options.GetValueOrDefault("--api-base") ?? Environment.GetEnvironmentVariable("SCIDEV_API_BASE");
            var modelId = options.GetValueOrDefault("--model") ?? Environment.GetEnvironmentVariable("SCIDEV_MODEL") ?? "";
            var endpoint = WorkspaceRunner.EndpointFor(address);
            if (options.GetValueOrDefault("--credential-profile") is { } profile)
            {
                if (!options.ContainsKey("--api-base"))
                    throw new ArgumentException("读取 Windows 凭据时必须显式指定 --api-base。");
                key = ProviderCredential.Read(profile)?.Trim();
                if (string.IsNullOrWhiteSpace(key))
                    throw new ToolDeniedException("credential_not_found", "找不到此 Windows 模型凭据。");
            }
            var review = options.GetValueOrDefault("--patch-approval") == "review";
            var outsideReview = options.GetValueOrDefault("--outside-approval") == "review";
            var commandReview = options.GetValueOrDefault("--command-approval") == "review";
            var networkReview = options.GetValueOrDefault("--network-approval") == "review";
            var gitReview = options.GetValueOrDefault("--git-approval") == "review";
            var hostReview = options.GetValueOrDefault("--host-approval") == "review";
            var mcpReview = options.GetValueOrDefault("--mcp-approval") == "review";
            var autoCommit = options.GetValueOrDefault("--auto-commit") == "review";
            if ((review || outsideReview || commandReview || networkReview || gitReview || hostReview || mcpReview || autoCommit) &&
                (asJson || Console.IsInputRedirected || Console.IsErrorRedirected))
                throw new ArgumentException("审批需要交互式终端，不能与 --json 或输入重定向一起使用。");
            var requestedSession = options.GetValueOrDefault("--session-id");
            sessionId = requestedSession is null ? runId : CliSession.ValidateId(requestedSession.Trim(), key);
            var settings = new WorkspaceRunOptions(workspace, task, endpoint, modelId, ReadInt(options, "--max-tokens", 12000, 32768),
                ReadInt(options, "--max-turns", 8, 16), executionMode, sandboxImage, validator,
                options.ContainsKey("--restore-on-failure"), planning, !options.ContainsKey("--no-verify"),
                NativePelican: options.ContainsKey("--pelican-native"),
                GitMetadataWorkspace: options.GetValueOrDefault("--metadata-workspace"), AutoCommit: autoCommit,
                AllowedCommands: options.GetValueOrDefault("--allowed-commands") ?? legacy?.PrefixesFor(executionMode, sandboxImage),
                NetworkPolicy: networkPolicy, LegacyCommandArguments: legacy is not null,
                RequestTimeoutSeconds: ReadRequestTimeout(options), AllowNoChanges: options.ContainsKey("--allow-no-changes"),
                Images: ImageAttachmentFiles.Load(imagePaths));
            WorkspaceRunner.ValidateOptions(settings, key, outsideReview, commandReview, networkReview, gitReview, autoCommit, hostReview, mcpReview);
            using var session = CliSession.Open(workspace, sessionId, endpoint, options.ContainsKey("--resume-python-session"),
                options.GetValueOrDefault("--accept-python-recovery"), key, stop.Token);
            if (!options.ContainsKey("--planning-mode") && session.HasPythonPlan(task))
                settings = settings with { Planning = PlanningMode.Auto };
            var worktreeId = options.ContainsKey("--worktree")
                ? options.GetValueOrDefault("--worktree-id") ?? session.Previous?.WorktreeId ?? (requestedSession is null ? runId : session.RoutingId) : null;
            if (session.Previous is { } previous && previous.WorktreeId != worktreeId)
                throw new ToolDeniedException("session_workspace_changed", "续接会话需要保持原工作树选项和 ID。");
            if (worktreeId is not null)
            {
                taskWorktree = session.Previous?.BaseSha is { } baseSha
                    ? await GitWorktrees.OpenManagedAsync(workspace, worktreeId, baseSha, stop.Token, settings.GitMetadataWorkspace)
                    : await GitWorktrees.CreateAsync(workspace, worktreeId, stop.Token, settings.GitMetadataWorkspace);
                settings = settings with { Workspace = taskWorktree.Path, GitMetadataWorkspace = taskWorktree.Repository };
                resultWorkspace = settings.Workspace;
            }
            stop.Token.ThrowIfCancellationRequested();
            var sessionContext = session.StartAttempt(settings.Workspace, runId, worktreeId, taskWorktree?.BaseSha, key,
                options.GetValueOrDefault("--retry-after-inspection"), task, settings.Images);
            var outcome = await WorkspaceRunner.RunAsync(runId, settings, key, stop.Token,
                patchReview: review ? record => new ConsoleEditApproval(settings.Workspace, record, key) : null,
                outsideReview: outsideReview ? new ConsoleOperationReview() : null,
                onEvent: asJson ? null : entry =>
                {
                    if (entry.Data is TaskPlanUpdate update)
                        Console.Error.WriteLine($"任务计划 · {update.Plan.Completed}/{update.Plan.Steps.Count} 步已处理");
                    if (entry.Type == "mcp_catalog_unavailable")
                        Console.Error.WriteLine("有 MCP 服务的工具未能加入本次任务。");
                },
                commandReview: commandReview ? new ConsoleOperationReview() : null,
                networkReview: networkReview ? new ConsoleOperationReview() : null,
                gitReview: gitReview ? new ConsoleOperationReview() : null,
                taskCommitReview: autoCommit ? new ConsoleOperationReview() : null,
                hostCommandReview: hostReview ? new ConsoleOperationReview() : null,
                sessionContext: sessionContext, mcpReview: mcpReview ? new ConsoleOperationReview() : null);
            (result, eventsPath, checkpointRestore, gitCommit, checkpointId) =
                (outcome.Result, outcome.EventsPath, outcome.CheckpointRestore, outcome.GitCommit, outcome.CheckpointId);
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested)
        {
            result = new(runId, false, "cancelled", "运行已取消。", []);
        }
        catch (ToolDeniedException ex)
        {
            result = new(runId, false, ex.Status, ex.Message, []);
        }
        catch (ArgumentException ex)
        {
            result = new(runId, false, "invalid_input", ex.Message, []);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            result = new(runId, false, "io_error", "无法创建或写入运行日志。", []);
        }
        finally
        {
            Console.CancelKeyPress -= cancel;
        }

        var written = true;
        if (asJson || !string.IsNullOrEmpty(resultFile))
        {
            object payload = new { result.RunId, result.Success, result.Status, result.Text, result.FilesChanged,
                result.Acceptance, result.ExternalDataShared, result.Plan, result.Verification, checkpointRestore, eventsPath,
                sessionId = sessionId ?? result.RunId, summary = result.Success ? result.Text : "", verificationReport = result.Verification,
                gitResultSha = gitCommit is { Status: "committed" } ? gitCommit.CommitId : "",
                noChangesDetected = result.Status == "no_changes", error = result.Success ? null : result.Text,
                checkpointId = checkpointId ?? "", result.TurnCount, usage = UsagePayload(result.Usage) };
            if ((!result.Success || taskWorktree is not null) && JsonSerializer.SerializeToNode(payload, Json) is JsonObject fields)
            {
                if (!result.Success)
                {
                    var failure = FailureReport.For(result);
                    fields["error_kind"] = failure.ErrorKind;
                    fields["user_message"] = failure.UserMessage;
                    fields["retryable"] = failure.Retryable;
                    fields["detail"] = failure.Detail;
                }
                if (taskWorktree is not null)
                {
                    fields["workspace"] = taskWorktree.Path;
                    fields["worktree"] = taskWorktree.Path;
                    fields["worktree_branch"] = taskWorktree.Branch;
                    fields["worktree_base_sha"] = taskWorktree.BaseSha;
                }
                payload = fields;
            }
            var json = RunJson.SerializeRedacted(payload, Json, key);
            if (!string.IsNullOrEmpty(resultFile))
            {
                try { WorkspaceResultFile.Write(resultWorkspace!, resultFile, json); }
                catch (Exception ex) when (ex is ArgumentException or IOException or UnauthorizedAccessException or ToolDeniedException)
                {
                    written = false;
                    var detail = ex is ToolDeniedException ? ex.Message : "无法写入或替换工作区结果文件。";
                    Console.Error.WriteLine(Redact($"result file not written: {detail}", key));
                }
            }
            if (asJson)
                Console.WriteLine(json);
        }
        if (!asJson)
        {
            Console.WriteLine(Redact($"[{result.Status}] {result.Text}", key));
            if (result.Verification?.Reason == VerificationReport.ContainerNotConfigured)
                Console.WriteLine("尚未运行代码检查（容器镜像为空）。");
            if (taskWorktree is not null)
                Console.WriteLine(Redact($"工作树 {taskWorktree.Path} · {taskWorktree.Branch}", key));
            if (checkpointRestore is not null)
                Console.WriteLine(RunJson.SerializeRedacted(checkpointRestore, Json, key));
            if (gitCommit is not null)
                Console.WriteLine(RunJson.SerializeRedacted(gitCommit, Json, key));
        }
        return result.Success && written ? 0 : result.Status == "invalid_input" ? 2 : result.Status == "cancelled" ? 130 : 1;
    }

    private static object UsagePayload(ModelUsage? usage)
    {
        if (usage is null)
            return new { };
        if (usage.Transport is not { } transport)
            return usage;
        return new
        {
            usage.PromptTokens, usage.CompletionTokens, usage.TotalTokens, transport.RequestAttempts,
            transport.RetryAttempts, transport.FailedAttempts, transport.ProxyFallbackAttempts, transport.RequestBodyBytes,
            transport.ResponseBodyBytes, transport.ElapsedMs, transport.TokenReports,
        };
    }

    private static Dictionary<string, string> Parse(string[] args, List<string> imagePaths)
    {
        if (args[0] != "exec")
            throw new ArgumentException(Usage);
        var options = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var i = 1; i < args.Length; i++)
        {
            var flag = args[i];
            if (flag == "--json")
                continue;
            if (flag == "--image" && i + 1 < args.Length && !string.IsNullOrWhiteSpace(args[i + 1]) &&
                !args[i + 1].StartsWith("--", StringComparison.Ordinal))
            {
                imagePaths.Add(args[++i]);
                ImageAttachment.Validate(imagePaths.Count, 0);
                continue;
            }
            if ((flag is "--restore-on-failure" or "--worktree" or "--no-verify" or "--pelican-native" or "--use-legacy-policy" or "--allow-no-changes"
                or "--resume-python-session") && options.TryAdd(flag, "true"))
                continue;
            if (flag is not ("--workspace" or "--task" or "--api-base" or "--model" or "--max-tokens" or "--max-turns" or "--patch-approval"
                or "--pelican-validator" or "--validator-python" or "--sandbox-image" or "--execution-mode" or "--outside-approval"
                or "--credential-profile" or "--command-approval" or "--network-approval" or "--git-approval" or "--auto-commit" or "--host-approval" or "--mcp-approval"
                or "--planning-mode" or "--worktree-id" or "--metadata-workspace" or "--allowed-commands" or "--network-policy"
                or "--result-file" or "--request-timeout" or "--sandbox" or "--session-id" or "--retry-after-inspection" or "--accept-python-recovery") ||
                i + 1 == args.Length || args[i + 1].StartsWith("--", StringComparison.Ordinal) ||
                !options.TryAdd(flag, args[++i]))
                throw new ArgumentException("存在未知、重复或缺值的参数。");
        }
        if (options.GetValueOrDefault("--patch-approval", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--patch-approval 只支持 reject 或 review。");
        if (options.GetValueOrDefault("--outside-approval", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--outside-approval 只支持 reject 或 review。");
        if (options.GetValueOrDefault("--command-approval", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--command-approval 只支持 reject 或 review。");
        if (options.GetValueOrDefault("--network-approval", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--network-approval 只支持 reject 或 review。");
        if (options.GetValueOrDefault("--git-approval", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--git-approval 只支持 reject 或 review。");
        if (options.GetValueOrDefault("--host-approval", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--host-approval 只支持 reject 或 review。");
        if (options.GetValueOrDefault("--mcp-approval", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--mcp-approval 只支持 reject 或 review。");
        if (options.GetValueOrDefault("--auto-commit", "reject") is not ("reject" or "review"))
            throw new ArgumentException("--auto-commit 只支持 reject 或 review。");
        if (!options.ContainsKey("--worktree") && options.ContainsKey("--worktree-id"))
            throw new ArgumentException("--worktree-id 需要与 --worktree 一起使用。");
        if (options.ContainsKey("--retry-after-inspection") && !options.ContainsKey("--session-id"))
            throw new ArgumentException("--retry-after-inspection 需要与 --session-id 一起使用。");
        if (options.ContainsKey("--resume-python-session") &&
            (!options.ContainsKey("--session-id") || !options.ContainsKey("--api-base") || options.ContainsKey("--worktree")))
            throw new ArgumentException("Python 续跑须指定 --session-id、--api-base，并把 --workspace 指向旧会话所在的工作区。");
        if (options.ContainsKey("--accept-python-recovery") && !options.ContainsKey("--resume-python-session"))
            throw new ArgumentException("--accept-python-recovery 需要与 --resume-python-session 一起使用。");
        if (!options.ContainsKey("--worktree") && options.ContainsKey("--metadata-workspace") &&
            options.GetValueOrDefault("--git-approval") != "review" && options.GetValueOrDefault("--auto-commit") != "review")
            throw new ArgumentException("--metadata-workspace 需要与 --worktree、--git-approval review 或 --auto-commit review 一起使用。");
        return options;
    }

    private static (string Python, string Script)? ValidatorOptions(Dictionary<string, string> args, string workspace)
    {
        if (!args.ContainsKey("--pelican-validator") && !args.ContainsKey("--validator-python"))
            return null;
        var script = Path.GetFullPath(Required(args, "--pelican-validator"));
        var python = Path.GetFullPath(Required(args, "--validator-python"));
        var workspacePrefix = Path.TrimEndingDirectorySeparator(workspace) + Path.DirectorySeparatorChar;
        if (!python.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) || !script.EndsWith(".py", StringComparison.OrdinalIgnoreCase) ||
            new[] { script, python }.Any(path => path.StartsWith(workspacePrefix, StringComparison.OrdinalIgnoreCase) ||
                !File.Exists(path) || (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0))
            throw new ArgumentException("严格验收需要工作区外已安装的可信 Python 和验收脚本。");
        return (python, script);
    }

    private static string Required(Dictionary<string, string> args, string key) =>
        args.TryGetValue(key, out var value) && !string.IsNullOrWhiteSpace(value)
            ? value : throw new ArgumentException($"缺少 {key}。");

    private static int ReadInt(Dictionary<string, string> args, string key, int fallback, int max) =>
        !args.TryGetValue(key, out var raw) ? fallback :
        int.TryParse(raw, out var n) && n >= 1 && n <= max ? n :
        throw new ArgumentException($"{key} 必须在 1 到 {max} 之间。");

    private static double ReadRequestTimeout(Dictionary<string, string> args)
    {
        var raw = args.GetValueOrDefault("--request-timeout") ?? Environment.GetEnvironmentVariable("SCIDEV_REQUEST_TIMEOUT_SECONDS");
        if (raw is null)
            return WorkspaceRunner.DefaultRequestTimeoutSeconds;
        if (!double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var value) || value is not (>= 5 and <= 600))
            throw new ArgumentException("--request-timeout 或 SCIDEV_REQUEST_TIMEOUT_SECONDS 必须在 5 到 600 秒之间。");
        return value;
    }

    private static string Redact(string value, string? key) =>
        string.IsNullOrEmpty(key) ? value : value.Replace(key, "[redacted]", StringComparison.Ordinal);
}
