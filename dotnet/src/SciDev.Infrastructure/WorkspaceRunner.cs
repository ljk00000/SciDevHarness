using System.Net.Http.Headers;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed record WorkspaceRunOptions(string Workspace, string Task, Uri Endpoint, string Model, int MaxTokens = 12_000,
    int MaxTurns = 8, ExecutionMode Mode = ExecutionMode.WorkspaceApproval, string? SandboxImage = null,
    (string Python, string Script)? PelicanValidator = null, bool RestoreOnFailure = false, PlanningMode Planning = PlanningMode.Off,
    bool RequireVerification = true, bool NativePelican = false, string? GitMetadataWorkspace = null, bool AutoCommit = false,
    string? AllowedCommands = null, AgentNetworkPolicy NetworkPolicy = AgentNetworkPolicy.Ask, bool LegacyCommandArguments = false,
    double RequestTimeoutSeconds = WorkspaceRunner.DefaultRequestTimeoutSeconds, bool AllowNoChanges = false,
    IReadOnlyList<ImageAttachment>? Images = null, bool TextToolCallFallback = false);

public sealed record WorkspaceRunOutcome(RunResult Result, string EventsPath, WorkspaceCheckpoint.RestoreResult? CheckpointRestore,
    TaskCommitResult? GitCommit = null, string? CheckpointId = null);
public sealed record SessionRunContext(RunInspection.SessionRun Link, string RoutingId, string? PreviousText,
    ResumedConversation? PythonConversation = null);

public static class WorkspaceRunner
{
    public const int DefaultRequestTimeoutSeconds = 240;

    public static Uri EndpointFor(string? baseAddress)
    {
        if (!Uri.TryCreate(baseAddress, UriKind.Absolute, out var uri) ||
            (uri.Scheme != "https" && !(uri.IsLoopback && uri.Scheme == "http")) ||
            uri.UserInfo.Length > 0 || uri.Query.Length > 0 || uri.Fragment.Length > 0)
            throw new ArgumentException("须配置 HTTPS API base；本机模拟端点可使用 HTTP。");
        return new Uri(uri.AbsoluteUri.TrimEnd('/') + "/chat/completions");
    }

    public static void ValidateOptions(WorkspaceRunOptions options, string? credential, bool outsideReview, bool commandReview,
        bool networkReview = false, bool gitReview = false, bool taskCommitReview = false, bool hostCommandReview = false,
        bool mcpReview = false)
    {
        if (options.NativePelican && options.PelicanValidator.HasValue)
            throw new ArgumentException("原生 SVG 验收与 Python 对照验收只能选择一种。");
        if (options.NativePelican && !OperatingSystem.IsWindows())
            throw new ArgumentException("原生 SVG 验收需要 Windows。");
        if (options.RequestTimeoutSeconds is not (>= 5 and <= 600))
            throw new ArgumentException("模型请求超时必须在 5 到 600 秒之间。");
        if (options.Images is { } attachments)
            ImageAttachment.Validate(attachments.Count, attachments.Sum(image => (long)image.ByteCount));
        if (!Enum.IsDefined(options.NetworkPolicy))
            throw new ArgumentException("联网策略无效。");
        if (options.NetworkPolicy == AgentNetworkPolicy.Deny && (networkReview || hostCommandReview || mcpReview))
            throw new ToolDeniedException("network_policy_denied", "断网策略禁止额外 HTTPS 请求、宿主命令及 MCP。");
        var endpoint = options.Endpoint;
        if (string.IsNullOrWhiteSpace(options.Model) || string.IsNullOrWhiteSpace(options.Task))
            throw new ArgumentException("请提供模型 ID 和任务。");
        if (!endpoint.IsAbsoluteUri || (endpoint.Scheme != "https" && !(endpoint.IsLoopback && endpoint.Scheme == "http")) ||
            endpoint.UserInfo.Length > 0 || endpoint.Query.Length > 0 || endpoint.Fragment.Length > 0)
            throw new ArgumentException("模型请求地址无效。");
        if (!endpoint.IsLoopback && string.IsNullOrEmpty(credential))
            throw new ArgumentException("远程模型需要提供 API 凭据。");
        if (credential?.Any(char.IsControl) == true)
            throw new ArgumentException("模型凭据含无效字符。");
        if (options.Mode == ExecutionMode.ReadOnly && (options.SandboxImage is not null || outsideReview || networkReview || gitReview ||
            options.AutoCommit || hostCommandReview || mcpReview))
            throw new ArgumentException("只读模式仅提供工作区内读取。");
        if (options.Mode == ExecutionMode.FullAccess && options.SandboxImage is null && !hostCommandReview)
            throw new ArgumentException("完全访问需要配置本机容器镜像或启用逐次宿主命令审批。");
        if (options.AutoCommit && !taskCommitReview)
            throw new ArgumentException("任务结束后提交需要交互式审批。");
        if (!Enum.IsDefined(options.Planning))
            throw new ArgumentException("计划模式无效。");
        if (!Enum.IsDefined(options.Mode) ||
            (commandReview && (options.Mode is not (ExecutionMode.Manual or ExecutionMode.WorkspaceWrite) || options.SandboxImage is null)))
            throw new ArgumentException("容器命令审批适用于配置本机镜像的手动审批或工作区可写模式。");
        if (!string.IsNullOrWhiteSpace(options.AllowedCommands))
        {
            if (options.Mode != ExecutionMode.WorkspaceWrite || options.SandboxImage is null)
                throw new ArgumentException("命令白名单需要工作区可写模式和已配置的本机镜像。");
            _ = ContainerCommandPrefixes.Parse(options.AllowedCommands);
        }
        if (options.SandboxImage is { } image)
            DockerWorkspaceTools.Validate(options.Workspace, image);
    }

    public static async Task<WorkspaceRunOutcome> RunAsync(string runId, WorkspaceRunOptions options, string? credential,
        CancellationToken token, Func<Action<RunEvent>, IFileEditApproval>? patchReview = null,
        IOperationReview? outsideReview = null, Action<RunEvent>? onEvent = null, IOperationReview? commandReview = null,
        IOperationReview? networkReview = null, WorkspaceDrafts? drafts = null, IOperationReview? gitReview = null,
        Func<CancellationToken, ValueTask<string?>>? gitMetadataPicker = null, IOperationReview? taskCommitReview = null,
        IOperationReview? hostCommandReview = null, SessionRunContext? sessionContext = null, IOperationReview? mcpReview = null)
    {
        var images = options.Images?.ToArray() ?? [];
        options = options with { Images = images };
        ValidateOptions(options, credential, outsideReview is not null, commandReview is not null, networkReview is not null,
            gitReview is not null, taskCommitReview is not null, hostCommandReview is not null, mcpReview is not null);
        var endpoint = options.Endpoint;
        var pythonConversation = sessionContext?.PythonConversation;
        var sensitiveHistory = pythonConversation?.IncludesSensitiveHistory == true;
        if (pythonConversation is not null)
        {
            var historicalImages = pythonConversation.Messages.SelectMany(message => message.Images ?? []).ToArray();
            ImageAttachment.Validate(images.Length + historicalImages.Length,
                images.Concat(historicalImages).Sum(image => (long)image.ByteCount));
            options = options with { Task = LegacySessionTranscript.Sanitize(options.Task, credential) };
        }
        token.ThrowIfCancellationRequested();
        using var journal = new RunJournal(options.Workspace, runId, credential);
        void Record(RunEvent entry)
        {
            if (entry.Data is TaskPlanUpdate update)
            {
                var savedPlan = update.Plan;
                if (update.IncludesExternalContent || sensitiveHistory || drafts is { ContentCount: > 0 } || images.Length > 0)
                    savedPlan = savedPlan with
                    {
                        Steps = savedPlan.Steps.Select((step, index) => step with { Step = $"步骤 {index + 1}" }).ToArray(),
                        Explanation = "计划可能引用附件、未保存草稿或经授权发送的区外内容。历史仅保留步骤状态。",
                    };
                journal.Record(entry with { Data = savedPlan });
            }
            else if (entry.Data is VerificationReport { Diagnostics: not null } report)
                journal.Record(entry with { Data = report with { Diagnostics = report.Diagnostics.Select(item => item with { Message = null }).ToArray() } });
            else
                journal.Record(entry);
            onEvent?.Invoke(entry);
        }

        var checkpoint = new WorkspaceCheckpoint(options.Workspace, runId, credential);
        var files = new WorkspaceFiles(options.Workspace, patchReview?.Invoke(Record), checkpoint, options.Mode, credential, drafts);
        var container = options.SandboxImage is null ? null :
            new DockerWorkspaceTools(options.Workspace, files, options.SandboxImage, credential)
            {
                Drafts = drafts ?? WorkspaceDrafts.Empty,
                LegacyCommandArguments = options.LegacyCommandArguments,
                CommandApproval = options.Mode is ExecutionMode.Manual or ExecutionMode.WorkspaceWrite or ExecutionMode.FullAccess
                    ? new(options.Workspace, options.Mode, commandReview, Record, ContainerCommandPrefixes.Parse(options.AllowedCommands)) : null,
            };
        IFileTools tools = container is null ? files : container;
        var denyExtraNetwork = options.NetworkPolicy == AgentNetworkPolicy.Deny;
        var legacyLocalCommands = options.LegacyCommandArguments && options.Mode == ExecutionMode.FullAccess && container is null;
        tools = new ApprovedHostCommands(options.Workspace, tools, hostCommandReview, endpoint, Record, credential, denyExtraNetwork,
            legacyWorkspaceApproval: options.LegacyCommandArguments && options.Mode == ExecutionMode.WorkspaceApproval,
            legacyLocalCommands: legacyLocalCommands);
        tools = new OutsideWorkspaceTools(options.Workspace, tools, outsideReview, endpoint, Record, credential);
        tools = new ApprovedNetworkTools(tools, networkReview, endpoint, Record, credential, denyExtraNetwork: denyExtraNetwork);
        tools = new ApprovedGitTools(options.Workspace, options.GitMetadataWorkspace, tools, gitReview, endpoint, Record,
            drafts ?? WorkspaceDrafts.Empty, credential, gitMetadataPicker);
        var canRunCommands = container is not null && (options.Mode != ExecutionMode.Manual || commandReview is not null);
        Record(new("execution_policy", 0, new { mode = options.Mode.ToString(), network = options.NetworkPolicy.ToString(),
            container = canRunCommands, hostReview = hostCommandReview is not null, networkReview = networkReview is not null }));
        if (sessionContext is not null)
            Record(new("session_attempt", 0, sessionContext.Link));
        Record(new("task_submitted", 0, new { options.Task }));
        if (images.Length > 0)
            Record(new("image_attachments", 0, new { images = images.Select(image => new { image.Format, image.ByteCount }).ToArray() }));
        await using var mcp = mcpReview is null ? null : new ApprovedMcpTools(options.Workspace, tools, mcpReview, endpoint, Record, credential);
        if (mcp is not null)
        {
            await mcp.LoadAsync(token);
            tools = mcp;
        }
        var mcpTools = mcp?.ModelTools;
        using var client = new HttpClient(new SocketsHttpHandler
        {
            AllowAutoRedirect = false, UseProxy = false, ConnectTimeout = TimeSpan.FromSeconds(15), MaxResponseDrainSize = 0,
        }) { Timeout = Timeout.InfiniteTimeSpan };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("SciDevHarness-CSharp/0.1");
        // OpenCode Go uses a caller-generated ID to route a conversation.
        if (endpoint.IdnHost == "opencode.ai" &&
            (endpoint.AbsolutePath.StartsWith("/inference/go/", StringComparison.Ordinal) ||
             endpoint.AbsolutePath.StartsWith("/zen/go/", StringComparison.Ordinal)))
            client.DefaultRequestHeaders.Add("x-opencode-session", sessionContext?.RoutingId ?? runId);
        if (!string.IsNullOrEmpty(credential))
            client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", credential);
        var model = new OpenAiSseModel(client, endpoint, options.Model, options.MaxTokens,
            dockerCommands: canRunCommands,
            executionMode: options.Mode, outsideApproval: outsideReview is not null,
            record: Record, networkApproval: networkReview is not null, planning: options.Planning, gitApproval: gitReview is not null,
            hostCommandApproval: hostCommandReview is not null, legacyCommandArguments: options.LegacyCommandArguments,
            requestTimeout: TimeSpan.FromSeconds(options.RequestTimeoutSeconds), mcpTools: mcpTools, legacyLocalCommands: legacyLocalCommands,
            textToolCallFallback: options.TextToolCallFallback);
        var acceptance = options.NativePelican || options.PelicanValidator.HasValue
            ? new PelicanAcceptance(files, credential, options.PelicanValidator) : null;
        var agent = new Agent(model, tools, Record, options.MaxTurns, acceptance, options.Mode, outsideReview is not null,
            networkReview is not null, options.Planning, options.RequireVerification, hostCommandApproval: hostCommandReview is not null,
            allowNoChanges: options.AllowNoChanges);
        var automaticVerification = options.RequireVerification && container is not null
            ? new AutomaticProjectVerification(files, container, drafts is not { Count: > 0 } &&
                options.Mode is ExecutionMode.WorkspaceApproval or ExecutionMode.WorkspaceWrite or ExecutionMode.FullAccess) : null;
        var result = await agent.RunAsync(runId, options.Task, token, runToken =>
        {
            string? context;
            try { context = files.InitialContext(options.Task, runToken); }
            catch (Exception ex) when (ex is ToolDeniedException or IOException or UnauthorizedAccessException or System.Text.DecoderFallbackException)
            {
                Record(new("workspace_context_unavailable", 0, new { reason = ex is ToolDeniedException denied ? denied.Status : "file_error" }));
                return null;
            }
            if (context is not null)
                Record(new("workspace_context_prepared", 0, new { characters = context.Length, drafts = drafts?.ContentCount ?? 0 }));
            return context;
        }, beginToolBatch: files.BeginToolBatch, verificationFingerprint: canRunCommands ? container!.CurrentFingerprint : null,
            automaticVerification: automaticVerification, images: images, priorSessionContext: sessionContext?.PreviousText,
            externalToolCatalog: mcpTools is { Count: > 0 }, resumedConversation: pythonConversation,
            verificationAvailable: container is not null);
        if (model.Usage is { } measured)
            result = result with { Usage = measured };
        Record(new("task_summary", 0, new { text = sensitiveHistory
            ? "本次续跑引用旧会话的附件或外部工具结果，模型结果仅在运行时显示。"
            : result.ExternalDataShared
            ? "本次结果可能引用获准发送的区外数据，仅在运行时显示。"
            : images.Length > 0
                ? "本次任务使用了图片附件，模型结果仅在运行时显示。"
            : drafts is { ContentCount: > 0 }
                ? "本次结果可能引用未保存草稿，仅在运行时显示。"
                : pythonConversation is null ? result.Text[..Math.Min(result.Text.Length, 64_000)]
                : LegacySessionTranscript.Sanitize(result.Text[..Math.Min(result.Text.Length, 64_000)], credential),
            continuationText = sessionContext is not null && result.Success && !result.ExternalDataShared &&
                !sensitiveHistory && images.Length == 0 && drafts is not { ContentCount: > 0 } }));

        WorkspaceCheckpoint.RestoreResult? restored = null;
        if (!result.Success && options.RestoreOnFailure)
        {
            try { restored = checkpoint.Restore(CancellationToken.None); }
            catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException or ToolDeniedException or JsonException)
            {
                restored = new("restore_failed", [], [], new() { ["checkpoint"] = ex.GetType().Name });
            }
            Record(new("checkpoint_restore", 0, restored));
        }
        TaskCommitResult? gitCommit = null;
        if (options.AutoCommit)
        {
            gitCommit = await TaskCommitWorkflow.RunAsync(options, result, checkpoint, taskCommitReview!, gitMetadataPicker,
                credential, Record, token);
            Record(new("git_task_commit_finished", 0, gitCommit));
        }
        if (mcp is not null && result.Success && !token.IsCancellationRequested)
        {
            try { await mcp.CloseSessionsAsync(token); }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        }
        return new(result, journal.RelativePath, restored, gitCommit, checkpoint.CheckpointId);
    }
}
