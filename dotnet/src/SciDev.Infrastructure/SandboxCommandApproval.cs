using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class SandboxCommandApproval(string workspace, ExecutionMode mode, IOperationReview? reviewer, Action<RunEvent> record,
    ContainerCommandPrefixes? prefixes = null)
{
    private static readonly JsonSerializerOptions PreviewFormat = new() { WriteIndented = true };

    public string? AutomaticCategory(string[] argv, string[]? policyArguments = null)
    {
        if (mode == ExecutionMode.FullAccess && CommandGuard.BlockedContainer(argv))
            throw new ToolDeniedException("sandbox_command_blocked", "此容器命令属于已限制的破坏性操作或由 Harness 管理的 Git 修改。");
        var recognized = policyArguments ?? argv;
        return mode switch
        {
            ExecutionMode.WorkspaceWrite => SandboxVerification.Classify(recognized) ?? (prefixes?.Allows(recognized) == true ? "allowlist" : null),
            ExecutionMode.FullAccess => SandboxVerification.Classify(recognized) ?? "full_access",
            _ => null,
        };
    }

    public void RequireReviewer()
    {
        if (reviewer is null)
            throw new ToolDeniedException("approval_denied", "本条容器命令需要在交互式终端或工作台批准。");
    }

    public void RecordAutomatic(string callId, string imageId, IReadOnlyList<string> argv, string directory, string category, int timeoutSeconds = 90)
    {
        var preview = JsonSerializer.Serialize(argv, PreviewFormat);
        record(new("command_auto_approved", 0, new { id = callId, imageId, argvSha256 = Digest(preview), directory, category, timeoutSeconds }));
    }

    public async ValueTask RequireAsync(string callId, string imageId, IReadOnlyList<string> argv, string directory, bool omitDrafts,
        CancellationToken token, int timeoutSeconds = 90)
    {
        token.ThrowIfCancellationRequested();
        var preview = JsonSerializer.Serialize(argv, PreviewFormat);
        var audit = new { id = callId, imageId, argvSha256 = Digest(preview), directory, timeoutSeconds };
        var detail = $"镜像 {imageId}\n工作目录 {directory}\n时间上限 {timeoutSeconds} 秒\n命令在断网容器的临时项目副本中运行，源工作区保持只读。"
            + (omitDrafts ? "\n临时副本已排除未保存编辑对应的文件。" : "");
        var request = new OperationReview(callId, OperationKind.RunSandboxCommand, workspace, detail, Before: preview);
        record(new("command_review_requested", 0, audit));
        var decision = await OperationApproval.DecideAsync(reviewer!, request, token);
        record(new("command_review_decided", 0, new { review = audit, outcome = decision }));
        token.ThrowIfCancellationRequested();
        if (decision != "approved")
            throw new ToolDeniedException("approval_denied", "本条容器命令未获批准。");
    }

    private static string Digest(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value)));
}
