namespace SciDev.Core;

public enum ExecutionMode { WorkspaceApproval, ReadOnly, Manual, WorkspaceWrite, FullAccess }
public enum AgentNetworkPolicy { Ask, Deny }

public record ToolCall(string Id, string Name, string Arguments);
public record Message(string Role, string? Content, IReadOnlyList<ToolCall>? Calls = null,
    string? ToolCallId = null, string? Reasoning = null, IReadOnlyList<ImageAttachment>? Images = null);
public record ResumedConversation(IReadOnlyList<Message> Messages, IReadOnlyList<string> PriorToolIds,
    TaskPlan? Plan, IReadOnlyList<FileChange> PriorChanges, bool IncludesSensitiveHistory);
public record ModelTurn(string? Text, IReadOnlyList<ToolCall> Calls, ModelUsage? Usage = null, string? Reasoning = null);
public record ModelUsage(long? PromptTokens, long? CompletionTokens, long? TotalTokens)
{
    [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    public ModelTransportUsage? Transport { get; init; }
}
public record ModelTransportUsage(long RequestAttempts, long RetryAttempts, long FailedAttempts, long ProxyFallbackAttempts,
    long RequestBodyBytes, long ResponseBodyBytes, long ElapsedMs, long TokenReports);
public record FileChange(string Path, string Operation)
{
    public bool IsWorkspaceChange() => Operation != "external_edit" && !System.IO.Path.IsPathRooted(Path);
}
public record ToolResult(string Text, FileChange? Change = null, int? CommandExitCode = null, bool ExternalDataShared = false,
    CommandVerification? Verification = null, IReadOnlyList<FileChange>? RelatedChanges = null);
public record RunResult(string RunId, bool Success, string Status, string Text, IReadOnlyList<FileChange> FilesChanged,
    ArtifactAcceptance? Acceptance = null, bool ExternalDataShared = false, TaskPlan? Plan = null,
    VerificationReport? Verification = null, int TurnCount = 0, ModelUsage? Usage = null,
    ModelFailureKind? ModelFailure = null);
public enum ModelFailureKind { Authentication, RateLimit, Timeout, Network, RejectedRequest, Protocol }
public record RunEvent(string Type, int Turn, object Data);
public record FileEditPreview(string Path, int Line, string Before, string After, string? Destination = null);
public record ArtifactAcceptance(string Status, string? Path, string Detail, string? RepairInstruction = null);

public interface IArtifactAcceptance
{
    Task<ArtifactAcceptance> CheckAsync(IReadOnlyList<FileChange> changes, CancellationToken token);
}

public interface IModel
{
    Task<ModelTurn> CompleteAsync(IReadOnlyList<Message> messages, CancellationToken token);
}

public interface IFileTools
{
    ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token);
}

public interface IFileEditApproval
{
    ValueTask<bool> ApproveAsync(FileEditPreview preview, CancellationToken token);
}

public enum OperationKind
{
    OpenExternalText, ShareExternalText, EditExternalText, RunSandboxCommand, FetchExternalText, ShareNetworkText,
    RunGitDiff, ShareGitDiff, PrepareGitCommit, PublishGitCommit, InspectHostCommand, RunHostCommand, ShareHostCommandOutput,
    RequestMcpHttp, ShareMcpCatalog, CallMcpTool, ShareMcpResult,
    ReadGitHubPullRequest, SubmitGitHubReview, PushGitHubBranch, CreateGitHubPullRequest,
    CheckForUpdate, DownloadUpdate, LaunchUpdate,
}

public record OperationReview(string CallId, OperationKind Kind, string Target, string Detail,
    string? Destination = null, string? Before = null, string? After = null);

public interface IOperationReview
{
    ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token);
}

public sealed class ToolDeniedException(string status, string message, Exception? inner = null) : Exception(message, inner)
{
    public string Status { get; } = status;
}

public sealed class IncompleteModelTurn(string finishReason, int callCount, ModelUsage? usage) : Exception
{
    public string FinishReason { get; } = finishReason;
    public int CallCount { get; } = callCount;
    public ModelUsage? Usage { get; } = usage;
}
