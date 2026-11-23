using System.ComponentModel;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Unicode;
using Microsoft.Win32.SafeHandles;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Serialize attempts for a named CLI session while retaining independent run journals.</summary>
public sealed class CliSession : IDisposable
{
    private const string DirectoryName = "csharp-sessions";
    private const string Prefix = "csharp-session-";
    private const int MaxBytes = 8 * 1024;
    private static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    private static readonly JsonSerializerOptions ContextJson = new() { Encoder = JavaScriptEncoder.Create(UnicodeRanges.All) };

    public sealed record LatestRun(string SessionId, string RunId, string EndpointHash, string? WorktreeId, string? BaseSha, int Version,
        string? LegacyRevision = null);
    public sealed record Listing(string SessionId, string Status, string Prompt, DateTimeOffset UpdatedAt);
    private sealed record PreviousAnswer(string Task, string Answer);

    private readonly WindowsWorkspace.Lease _pointer;
    private readonly SafeFileHandle _lock;
    private readonly WorkspaceFiles.FileStamp? _stamp;
    private readonly string _endpointHash;
    private readonly string _id;
    private readonly LegacyCodingLease? _legacyLease;
    private readonly LegacySessionTranscript.Snapshot? _legacy;

    private CliSession(WindowsWorkspace.Lease pointer, SafeFileHandle gate, WorkspaceFiles.FileStamp? stamp,
        string id, string endpointHash, string routingId, LatestRun? previous, LegacyCodingLease? legacyLease,
        LegacySessionTranscript.Snapshot? legacy)
    {
        (_pointer, _lock, _stamp, _id, _endpointHash) = (pointer, gate, stamp, id, endpointHash);
        Previous = previous;
        RoutingId = routingId;
        _legacyLease = legacyLease;
        _legacy = legacy;
    }

    public LatestRun? Previous { get; }
    public string RoutingId { get; }
    public bool HasPythonPlan(string task) => _legacy?.LastPrompt == task && _legacy.Resolved.Plan is not null;

    public static string ValidateId(string id, string? credential)
    {
        if (!IsId(id) || (!string.IsNullOrEmpty(credential) && id.Contains(credential, StringComparison.Ordinal)))
            throw new ArgumentException("会话 ID 使用 1 到 128 个英文字母、数字、短横线或下划线，且不能包含模型密钥。");
        _ = WindowsWorkspace.NormalizeOperatorPath(id + ".json");
        return id;
    }

    public static CliSession Open(string workspace, string id, Uri endpoint, bool resumePython = false,
        string? inspectedPythonRevision = null, string? credential = null, CancellationToken token = default)
    {
        _ = ValidateId(id, credential: null);
        var guard = new WindowsWorkspace(workspace);
        using var legacyPath = guard.OpenOperatorPath($".research/sessions/{id}.json", allowMissingParents: true);
        var hasPython = legacyPath is not null && WindowsWorkspace.FileExists(legacyPath.AbsolutePath);
        if (hasPython && !resumePython)
            throw new ToolDeniedException("legacy_session_requires_python",
                "旧会话须显式指定 --resume-python-session 和 --api-base，或通过 Python 版继续。");
        if (resumePython && !hasPython)
            throw new ToolDeniedException("legacy_session_not_found", "这个工作区没有指定的 Python 会话。");

        var rootState = guard.EnsureStateDirectory();
        using var directory = rootState.OpenPath(DirectoryName);
        Directory.CreateDirectory(directory.AbsolutePath);
        var state = new WindowsWorkspace(directory.AbsolutePath);
        var name = Prefix + Hash(id);
        var pointer = state.OpenPath(name + ".json");
        SafeFileHandle? gate = null;
        LegacyCodingLease? legacyLease = null;
        try
        {
            using var gatePath = state.OpenPath(name + ".lock");
            gate = Acquire(gatePath.AbsolutePath);
            WindowsWorkspace.GetFileIdentity(gate, gatePath.AbsolutePath);
            var (previous, stamp) = Read(pointer.AbsolutePath);
            var endpointHash = Hash(endpoint.AbsoluteUri);
            if (previous is not null && previous.SessionId != id)
                throw InvalidPointer();
            if (previous is not null && previous.EndpointHash != endpointHash)
                throw new ToolDeniedException("session_model_endpoint_changed", "模型地址与旧会话不同。请恢复原地址或使用新的会话 ID。");
            if (previous?.LegacyRevision is not null && !resumePython)
                throw new ToolDeniedException("legacy_session_not_found", "已续跑的 Python 会话源已移走。请恢复原会话或使用新的 ID。");
            LegacySessionTranscript.Snapshot? legacy = null;
            if (resumePython)
            {
                if (previous is { LegacyRevision: null })
                    throw new ToolDeniedException("legacy_session_conflict", "此 ID 已用于独立的 C# 会话。");
                legacyLease = LegacyCodingLease.Acquire(guard);
                legacy = LegacySessionTranscript.Read(guard, id, credential, token);
                if (previous?.LegacyRevision is { } previousRevision && previousRevision != legacy.Revision)
                    throw new ToolDeniedException("legacy_session_changed", "Python 会话内容在首次续跑后发生变化。请使用与首次续跑相同的源副本。");
                if (inspectedPythonRevision is not null && inspectedPythonRevision != legacy.Revision ||
                    previous is null && legacy.Resolved.Continuation!.RequiresInspection && inspectedPythonRevision != legacy.Revision)
                    throw new ToolDeniedException("legacy_session_needs_inspection",
                        "请先用 inspect-run --source python 核对未确认的操作，再用 --accept-python-recovery 提交该次检查的 revision。");
            }
            var canonicalWorkspace = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace)).ToUpperInvariant();
            return new(pointer, gate, stamp, id, endpointHash, Hash(canonicalWorkspace + "\n" + id)[..32], previous, legacyLease, legacy);
        }
        catch
        {
            gate?.Dispose();
            pointer.Dispose();
            legacyLease?.Dispose();
            throw;
        }
    }

    public SessionRunContext StartAttempt(string workspace, string runId, string? worktreeId, string? baseSha, string? credential,
        string? reviewedRunId = null, string? task = null, IReadOnlyList<ImageAttachment>? attachments = null)
    {
        if (_legacy is not null)
        {
            var images = _legacy.Messages.SelectMany(message => message.Images ?? []).Concat(attachments ?? []).ToArray();
            ImageAttachment.Validate(images.Length, images.Sum(image => (long)image.ByteCount));
        }
        var previousId = Previous?.RunId;
        if (reviewedRunId is not null)
        {
            if (reviewedRunId != previousId)
                throw new ToolDeniedException("session_retry_mismatch", "请提供此会话最近一轮待核对的运行 ID。");
            var abandoned = RunInspection.Check(workspace, reviewedRunId);
            if (abandoned.Status == "finished")
                throw new ToolDeniedException("session_retry_mismatch", "此运行已有终态，可以直接续接会话。");
            if (abandoned.Session is { } link && link.SessionId != _id)
                throw InvalidPointer();
            previousId = abandoned.Session?.PreviousRunId;
        }
        var context = ReadPriorAnswers(workspace, credential, previousId);
        var bytes = JsonSerializer.SerializeToUtf8Bytes(new LatestRun(_id, runId, _endpointHash, worktreeId, baseSha, 1,
            _legacy?.Revision), Json);
        WorkspaceFiles.Save(_pointer.AbsolutePath, bytes, CancellationToken.None, _stamp, maxCurrentBytes: MaxBytes);
        var originalTask = _legacy?.LastPrompt is { } prompt && string.Equals(prompt, task, StringComparison.Ordinal);
        var resumed = _legacy is null ? null : new ResumedConversation(_legacy.Messages, _legacy.PriorToolIds,
            originalTask ? _legacy.Resolved.Plan : null, originalTask ? _legacy.PriorChanges : [], _legacy.SensitiveHistory);
        return new(new(_id, previousId, reviewedRunId, _legacy?.Revision), RoutingId, context, resumed);
    }

    private string? ReadPriorAnswers(string workspace, string? credential, string? runId)
    {
        var answers = new List<PreviousAnswer>();
        var visited = new HashSet<string>(StringComparer.Ordinal);
        while (runId is not null && answers.Count < 4 && visited.Count < 16)
        {
            if (!RunInspection.IsRunId(runId) || !visited.Add(runId))
                throw InvalidPointer();
            var run = RunInspection.Check(workspace, runId);
            if (run.Status != "finished")
                throw new ToolDeniedException("session_needs_inspection", $"运行 {runId} 尚待核对。请先用 inspect-run 查看，再决定如何继续。");
            if (run.Session?.SessionId != _id)
                throw InvalidPointer();
            if (run.ContinuationTextAvailable && run.Task is { } task && run.Summary is { } answer)
                answers.Add(new(Excerpt(task, 1_500), Excerpt(answer, 3_000)));
            runId = run.Session.PreviousRunId;
        }
        answers.Reverse();
        return answers.Count > 0 ? JsonSerializer.Serialize(answers, ContextJson) : null;

        string Excerpt(string value, int limit)
        {
            value = WorkspaceFiles.RedactCredential(value, credential);
            return value.Length > limit ? value[..limit] + "…" : value;
        }
    }

    public static Listing[] Recent(string workspace, int limit, CancellationToken token)
    {
        var runs = RunHistory.Recent(workspace, 100, token, fullPrompt: true).Where(item => item.SessionId is null)
            .Select(item => new Listing(item.RunId, item.Status, item.Task ?? "", item.UpdatedAt)).ToList();
        var folder = Path.Combine(workspace, ".research", DirectoryName);
        if (Directory.Exists(folder))
        {
            var state = new WindowsWorkspace(folder);
            foreach (var file in new DirectoryInfo(folder).EnumerateFiles(Prefix + "*.json").Take(10_001)
                         .OrderByDescending(file => file.LastWriteTimeUtc).Take(limit))
            {
                token.ThrowIfCancellationRequested();
                using var target = state.OpenPath(file.Name);
                var (latest, _) = Read(target.AbsolutePath);
                if (latest is null || file.Name != Prefix + Hash(latest.SessionId) + ".json")
                    throw InvalidPointer();
                var runWorkspace = latest.WorktreeId is { } id ? GitWorktrees.ManagedPath(workspace, id) : workspace;
                var run = Directory.Exists(runWorkspace) ? RunInspection.Check(runWorkspace, latest.RunId) : null;
                if (run?.Session is { } link && link.SessionId != latest.SessionId)
                    throw InvalidPointer();
                var status = run?.Status == "finished" ? run.FinalStatus ?? "finished" : run?.Status ?? "run_not_found";
                if (latest.LegacyRevision is not null)
                    runs.RemoveAll(item => item.SessionId == latest.SessionId);
                runs.Add(new(latest.SessionId, status, run?.Task ?? "", run?.UpdatedAt ?? file.LastWriteTimeUtc));
            }
        }
        return runs.OrderByDescending(item => item.UpdatedAt).ThenBy(item => item.SessionId, StringComparer.Ordinal).Take(limit).ToArray();
    }

    private static (LatestRun? Attempt, WorkspaceFiles.FileStamp? Stamp) Read(string path)
    {
        if (!WindowsWorkspace.FileExists(path))
            return (null, null);
        using var file = WorkspaceFiles.OpenSnapshot(path, FileShare.Read | FileShare.Delete, maxBytes: MaxBytes);
        try
        {
            var value = JsonSerializer.Deserialize<LatestRun>(file.Bytes, Json);
            if (value is not { Version: 1, SessionId: { } id, RunId: { } runId, EndpointHash.Length: 64 } || !IsId(id) ||
                !RunInspection.IsRunId(runId) || (value.WorktreeId is null) != (value.BaseSha is null))
                throw InvalidPointer();
            if (value.WorktreeId is { } tree)
                GitWorktrees.ValidateId(tree);
            if (value.LegacyRevision is { } revision && (revision.Length != 64 || !revision.All(char.IsAsciiHexDigitLower)))
                throw InvalidPointer();
            return (value, file.Stamp);
        }
        catch (JsonException) { throw InvalidPointer(); }
    }

    private static SafeFileHandle Acquire(string path)
    {
        try { return WindowsWorkspace.OpenForRead(path, FileShare.None); }
        catch (IOException ex) when (ex.InnerException is Win32Exception { NativeErrorCode: 2 })
        {
            try { return File.OpenHandle(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None); }
            catch (IOException race) when ((race.HResult & 0xFFFF) is 32 or 80 or 183) { throw Busy(); }
        }
        catch (IOException ex) when (ex.InnerException is Win32Exception { NativeErrorCode: 32 }) { throw Busy(); }
    }

    private static bool IsId(string id) => id.Length is >= 1 and <= 128 && id.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is '_' or '-');
    private static string Hash(string value) => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(value)));
    private static ToolDeniedException InvalidPointer() => new("session_invalid", "会话记录无效。请保留原始日志并选择新的会话 ID。");
    private static ToolDeniedException Busy() => new("session_busy", "此会话已有一个运行，请等待完成后再续接。");

    public void Dispose()
    {
        _lock.Dispose();
        _pointer.Dispose();
        _legacyLease?.Dispose();
    }
}
