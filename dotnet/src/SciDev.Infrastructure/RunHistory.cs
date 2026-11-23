using System.Globalization;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public static class RunHistory
{
    private const int MaxRuns = 10_000;
    private const string CurrentPrefix = "csharp-";
    private const string CurrentSuffix = ".jsonl";

    public sealed record Entry(string Source, string RunId, DateTimeOffset UpdatedAt, string Status, string? Task, string? SessionId = null);
    public sealed record LegacyRun(string Status, string? FinalStatus, string? Task, string? Summary, DateTimeOffset? UpdatedAt,
        TaskPlan? Plan = null, VerificationReport? Verification = null, LegacyContinuation? Continuation = null, string? ResumeUnavailable = null);
    public sealed record LegacyToolRecovery(string Id, string Name, string Status);
    public sealed record LegacyContinuation(string Revision, bool RequiresInspection, int Messages, int Attachments, long AttachmentBytes,
        LegacyToolRecovery[] RecoveredTools, int UnmatchedJournalRows);
    private sealed record Candidate(string Source, string Id, DateTimeOffset LastWrite);

    public static Entry[] Recent(string workspace, int limit, CancellationToken token, bool fullPrompt = false)
    {
        if (limit is < 1 or > 100)
            throw new ArgumentException("运行列表上限须在 1 到 100 之间。");
        _ = new WindowsWorkspace(workspace);
        var folder = Path.Combine(workspace, ".research");
        if (!Directory.Exists(folder))
            return [];
        _ = new WindowsWorkspace(folder);

        var items = ListCandidates(folder, token);
        var result = new List<Entry>();
        foreach (var item in items.OrderByDescending(x => x.LastWrite).ThenBy(x => x.Id, StringComparer.Ordinal).Take(limit))
        {
            token.ThrowIfCancellationRequested();
            try
            {
                if (item.Source == "python")
                {
                    var legacy = ReadLegacy(workspace, item.Id);
                    result.Add(new(item.Source, item.Id, legacy.UpdatedAt ?? item.LastWrite, legacy.FinalStatus ?? legacy.Status,
                        fullPrompt ? legacy.Task : Preview(legacy.Task)));
                }
                else
                {
                    var run = RunInspection.Check(workspace, item.Id);
                    var visible = run.Status == "finished" ? run.FinalStatus ?? run.Status : run.Status;
                    result.Add(new(item.Source, item.Id, item.LastWrite, visible, fullPrompt ? run.Task : Preview(run.Task), run.Session?.SessionId));
                }
            }
            catch (Exception ex) when (ex is ToolDeniedException or IOException or InvalidDataException or UnauthorizedAccessException or JsonException or
                InvalidOperationException or KeyNotFoundException)
            {
                result.Add(new(item.Source, item.Id, item.LastWrite, "unreadable", null));
            }
        }
        return result.OrderByDescending(x => x.UpdatedAt).ThenBy(x => x.RunId, StringComparer.Ordinal).ToArray();
    }

    public static LegacyRun ReadLegacy(string workspace, string id)
    {
        if (!LegacyId(id))
            throw new ArgumentException("旧会话 ID 无效。");
        _ = new WindowsWorkspace(workspace);
        var sessions = Path.Combine(workspace, ".research", "sessions");
        if (!Directory.Exists(sessions))
            return new("run_not_found", null, null, null, null);
        using var source = new WindowsWorkspace(sessions).OpenPath(id + ".json");
        if (!WindowsWorkspace.FileExists(source.AbsolutePath))
            return new("run_not_found", null, null, null, null);
        using var handle = WindowsWorkspace.OpenForRead(source.AbsolutePath, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(handle, source.AbsolutePath);
        using var stream = new FileStream(handle, FileAccess.Read);
        if (stream.Length > 16 * 1024 * 1024)
            throw new InvalidDataException("旧会话超出读取上限。");
        using var document = JsonDocument.Parse(stream);
        return ReadLegacyRecord(document.RootElement, id);
    }

    public static LegacyRun InspectLegacy(string workspace, string id, string? credential, CancellationToken token)
    {
        if (!LegacyId(id))
            throw new ArgumentException("旧会话 ID 无效。");
        var guard = new WindowsWorkspace(workspace);
        try
        {
            return LegacySessionTranscript.Read(guard, id, credential, token).Resolved;
        }
        catch (ToolDeniedException error) when (error.Status is "legacy_session_invalid" or "legacy_session_not_found" or
            "legacy_session_limit" or "snapshot_limit" or "snapshot_mismatch")
        {
            return LegacySessionTranscript.SanitizeRun(ReadLegacy(workspace, id), credential) with { ResumeUnavailable = error.Status };
        }
    }

    internal static LegacyRun ReadLegacyRecord(JsonElement session, string id)
    {
        if (String(session, "session_id") != id)
            throw new InvalidDataException("会话文件与会话 ID 不符。");
        var timestamp = DateTimeOffset.TryParse(String(session, "updated_at"), CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeLocal, out var updated) ? updated : (DateTimeOffset?)null;

        string? summary = null;
        if (session.TryGetProperty("messages", out var messages) && messages.ValueKind == JsonValueKind.Array)
            foreach (var entry in messages.EnumerateArray())
                if (String(entry, "role") == "assistant" && String(entry, "content") is { Length: > 0 } content)
                    summary = content;
        var plan = session.TryGetProperty("task_plan", out var savedPlan) ? TaskPlan.ReadRecord(savedPlan) : null;
        return new("legacy_session", String(session, "status"), String(session, "prompt"), summary, timestamp, plan,
            ReadLegacyVerification(session));
    }

    private static VerificationReport? ReadLegacyVerification(JsonElement session)
    {
        if (!session.TryGetProperty("verification_report", out var report) || report.ValueKind != JsonValueKind.Object)
            return null;
        var original = String(report, "status");
        var status = original switch
        {
            "scope_limited" => "partial",
            "workspace_changed" => "stale",
            "passed" or "failed" or "stale" or "not_run" or "timed_out" or "start_failed" => original,
            _ => null,
        };
        if (status is null)
            return null;
        // Historical summaries may include commands, raw output, and hashes. Expose only the structured status fields.
        if (!report.TryGetProperty("latest", out var latest) || latest.ValueKind != JsonValueKind.Object ||
            String(latest, "check_status") != original)
            return new(status);
        var category = String(latest, "kind");
        if (category is not ("test" or "lint" or "typecheck" or "compile" or "build" or "check"))
            category = null;
        return new(status, category, latest.TryGetProperty("exit_code", out var exit) && exit.ValueKind == JsonValueKind.Number &&
            exit.TryGetInt32(out var code) ? code : null);
    }

    private static List<Candidate> ListCandidates(string state, CancellationToken token)
    {
        var found = new List<Candidate>();
        foreach (var file in new DirectoryInfo(state).EnumerateFiles(CurrentPrefix + "*" + CurrentSuffix))
        {
            token.ThrowIfCancellationRequested();
            if (!file.Name.StartsWith(CurrentPrefix, StringComparison.Ordinal) ||
                !file.Name.EndsWith(CurrentSuffix, StringComparison.Ordinal))
                continue;
            var id = file.Name[CurrentPrefix.Length..^CurrentSuffix.Length];
            if (RunInspection.IsRunId(id))
                Add(file, id, "csharp");
        }
        var sessions = Path.Combine(state, "sessions");
        if (Directory.Exists(sessions))
        {
            _ = new WindowsWorkspace(sessions);
            foreach (var file in new DirectoryInfo(sessions).EnumerateFiles("*.json"))
            {
                token.ThrowIfCancellationRequested();
                var id = Path.GetFileNameWithoutExtension(file.Name);
                if (LegacyId(id))
                    Add(file, id, "python");
            }
        }
        return found;

        void Add(FileInfo file, string id, string source)
        {
            if (found.Count >= MaxRuns)
                throw new ToolDeniedException("history_limit", "工作区中的会话数超过当前清单上限。");
            if ((file.Attributes & FileAttributes.ReparsePoint) == 0)
                found.Add(new(source, id, file.LastWriteTimeUtc));
        }
    }

    private static string? String(JsonElement entry, string name) => entry.ValueKind == JsonValueKind.Object &&
        entry.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    private static bool LegacyId(string id) => id.Length is >= 1 and <= 128 &&
        id.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is '_' or '-');

    private static string? Preview(string? task) => task is { Length: > 160 } ? task[..160] + "…" : task;
}
