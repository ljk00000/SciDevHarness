using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public static class RunInspection
{
    public sealed record SessionRun(string SessionId, string? PreviousRunId, string? ReviewedRunId = null, string? LegacyRevision = null);
    public sealed record PendingTool(string Id, string Name);
    public sealed record Result(string Status, string? FinalStatus, PendingTool[] UnconfirmedTools,
        string? Task = null, string? Summary = null, TaskPlan? Plan = null, VerificationReport? Verification = null,
        TaskCommitResult? GitCommit = null, SessionRun? Session = null, bool ContinuationTextAvailable = false,
        DateTimeOffset? UpdatedAt = null);

    private static readonly JsonSerializerOptions JournalJson = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

    public static Result Check(string workspace, string id)
    {
        ValidateId(id);
        _ = new WindowsWorkspace(workspace);
        var folder = Path.Combine(workspace, ".research");
        if (!Directory.Exists(folder))
            return new("run_not_found", null, []);
        var state = new WindowsWorkspace(folder);
        using var path = state.OpenPath($"csharp-{id}.jsonl");
        if (!WindowsWorkspace.FileExists(path.AbsolutePath))
            return new("run_not_found", null, []);
        using var stream = File.Open(path.AbsolutePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        WindowsWorkspace.GetFileIdentity(stream.SafeFileHandle, path.AbsolutePath);
        if (stream.Length > 16 * 1024 * 1024)
            throw new InvalidDataException("运行日志超出检查上限。");

        using var reader = new StreamReader(stream);
        var pending = new Dictionary<string, PendingTool>(StringComparer.Ordinal);
        var startingHostCommands = new HashSet<string>(StringComparer.Ordinal);
        string? finalStatus = null;
        string? task = null;
        string? summary = null;
        TaskPlan? plan = null;
        VerificationReport? verification = null;
        TaskCommitResult? gitCommit = null;
        SessionRun? session = null;
        var continuationText = false;
        var incompleteTail = false;
        while (reader.ReadLine() is { } line)
        {
            try
            {
                using var doc = JsonDocument.Parse(line);
                var entry = doc.RootElement;
                if (entry.GetProperty("run_id").GetString() != id)
                    throw new InvalidDataException("运行日志包含不同的运行 ID。");
                var data = entry.GetProperty("data");
                switch (entry.GetProperty("type").GetString())
                {
                    case "session_attempt":
                        session = data.Deserialize<SessionRun>(JournalJson) ?? throw new InvalidDataException("会话运行关联无效。");
                        break;
                    case "task_submitted":
                        task = data.GetProperty("task").GetString();
                        break;
                    case "task_summary":
                        summary = data.GetProperty("text").GetString();
                        continuationText = data.TryGetProperty("continuation_text", out var flag) && flag.ValueKind == JsonValueKind.True;
                        break;
                    case "task_plan_updated":
                        plan = TaskPlan.ReadRecord(data) ?? plan;
                        break;
                    case "verification_reported":
                        verification = data.Deserialize<VerificationReport>(JournalJson);
                        break;
                    case "git_task_commit_publishing":
                        gitCommit = new("git_task_commit_uncertain", data.GetProperty("commit").GetString(),
                            data.GetProperty("reference").GetString(), data.GetProperty("paths").GetArrayLength());
                        break;
                    case "git_task_commit_finished":
                        var result = data.Deserialize<TaskCommitResult>(JournalJson);
                        if (gitCommit?.Status != "git_task_commit_uncertain" || result?.Status is "committed" or
                            "git_task_index_write_failed" or "git_task_commit_uncertain" or "git_task_index_repair_needed")
                            gitCommit = result ?? gitCommit;
                        break;
                    case "tool_started":
                        var tool = new PendingTool(data.GetProperty("id").GetString()!, data.GetProperty("name").GetString()!);
                        if (!pending.TryAdd(tool.Id, tool))
                            throw new InvalidDataException("工具记录存在重复 ID。");
                        break;
                    case "tool_completed":
                        pending.Remove(data.GetProperty("id").GetString()!);
                        break;
                    case "mcp_tool_executed":
                        // The remote execution returned. Only delivery to the model may still be pending.
                        var mcpId = data.GetProperty("id").GetString()!;
                        if (pending.GetValueOrDefault(mcpId)?.Name == "mcp_tool")
                            pending.Remove(mcpId);
                        break;
                    case "host_command_starting":
                        startingHostCommands.Add(data.GetProperty("id").GetString()!);
                        break;
                    case "host_command_exited":
                        startingHostCommands.Remove(data.GetProperty("id").GetString()!);
                        break;
                    case "tool_denied":
                        var deniedId = data.GetProperty("id").GetString()!;
                        var deniedTool = pending.GetValueOrDefault(deniedId)?.Name;
                        var denial = data.GetProperty("status").GetString();
                        if (deniedTool == "git_diff" || (deniedTool == "run_host_command" && !startingHostCommands.Contains(deniedId)) ||
                            ((denial is "host_output_not_shared" or "host_command_error") && !startingHostCommands.Contains(deniedId)) ||
                            denial is "approval_denied" or "approval_required" or
                            "edit_conflict" or "rename_conflict" or "unsupported_tool" or "invalid_tool" or "path_denied" or "checkpoint_limit" or
                            "checkpoint_credential" or "sandbox_limit" or "sandbox_content_denied" or "sandbox_unavailable" or "sandbox_timeout" or
                            "verification_directory" or "network_policy_denied" or "host_approval_required" or "host_command_credential" or "host_command_blocked" or "sandbox_command_blocked" or
                            "read_only" or "external_credential" or "invalid_plan" or "plan_required" or "editor_draft" or
                            "mcp_invalid_arguments" or "mcp_credential")
                            pending.Remove(deniedId);
                        break;
                    case "run_finished":
                        finalStatus = data.GetProperty("status").GetString();
                        break;
                }
            }
            catch (JsonException) when (reader.EndOfStream)
            {
                // The last line may be torn if the host stopped while appending it.
                incompleteTail = true;
            }
        }
        var status = pending.Count > 0 || gitCommit?.Status is "git_task_commit_uncertain" or "git_task_index_repair_needed" ? "needs_inspection" :
            finalStatus is null || incompleteTail || (task is not null && summary is null) ? "interrupted" : "finished";
        return new(status, finalStatus, pending.Values.ToArray(), task, summary, plan, verification, gitCommit, session,
            continuationText, File.GetLastWriteTimeUtc(stream.SafeFileHandle));
    }

    internal static void ValidateId(string id)
    {
        if (!IsRunId(id))
            throw new ArgumentException("运行 ID 必须是 32 位小写十六进制。");
    }

    internal static bool IsRunId(string id) => id.Length == 32 && id.All(char.IsAsciiHexDigitLower);
}
