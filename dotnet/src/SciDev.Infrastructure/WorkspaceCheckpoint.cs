using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceFiles;

namespace SciDev.Infrastructure;

/// <summary>Persists an undo record before publishing each file mutation.</summary>
public sealed partial class WorkspaceCheckpoint
{
    private const int FormatVersion = 1;
    private const int RenameVersion = 2;
    private const int MaxEntries = 256;
    private readonly string _root;
    private readonly string _runId;
    private readonly HashSet<string> _tracked = new(StringComparer.OrdinalIgnoreCase);
    private readonly byte[] _credential;
    private int _next;

    public WorkspaceCheckpoint(string workspace, string runId, string? credential = null)
    {
        RunInspection.ValidateId(runId);
        _root = Path.GetFullPath(workspace);
        _runId = runId;
        _credential = string.IsNullOrEmpty(credential) ? [] : Encoding.UTF8.GetBytes(credential);
    }

    public sealed record RestoreResult(string Status, string[] Restored, string[] Deleted,
        Dictionary<string, string> Failed);

    internal string? CheckpointId => _next > 0 ? _runId : null;

    private sealed record Mutation(int Version, string Path, byte[]? Original, FileStamp? Before, FileStamp? After,
        [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Destination = null);
    internal sealed record TaskChange(byte[]? Original, FileStamp? Before, FileStamp? After, string? MovedFrom = null);

    internal IReadOnlyDictionary<string, TaskChange> ReadTaskChanges(CancellationToken token, out string[][] renames)
    {
        using var folder = OpenRunDirectory(create: false);
        renames = [];
        if (folder is null)
            return new Dictionary<string, TaskChange>(StringComparer.OrdinalIgnoreCase);
        var records = ReadRecords(folder.AbsolutePath, token);
        if (records.Count != _next)
            throw new InvalidDataException("任务检查点缺失，无法核对提交范围。");
        renames = RenameGroups(records).Select(group => group.ToArray()).ToArray();
        var changes = records.SelectMany(Expand).GroupBy(entry => entry.Path, StringComparer.OrdinalIgnoreCase).ToDictionary(group => group.Key, group =>
        {
            var edits = group.ToArray();
            for (var i = 1; i < edits.Length; i++)
                if (edits[i].Before != edits[i - 1].After)
                    throw new ToolDeniedException("git_task_file_changed", "任务期间存在其他编辑，无法隔离提交范围。");
            return new TaskChange(edits[0].Original, edits[0].Before, edits[^1].After);
        }, StringComparer.OrdinalIgnoreCase);
        var origins = renames.SelectMany(group => group).ToDictionary(path => path,
            path => changes[path].Before is null ? null : path, StringComparer.OrdinalIgnoreCase);
        foreach (var record in records)
        {
            if (record.Destination is { } destination)
            {
                origins[destination] = origins[record.Path];
                origins[record.Path] = null;
            }
            else if (record.Before is null && origins.ContainsKey(record.Path))
                origins[record.Path] = null;
        }
        foreach (var (path, origin) in origins)
            if (origin is not null && !string.Equals(path, origin, StringComparison.OrdinalIgnoreCase))
                changes[path] = changes[path] with { MovedFrom = origin };
        return changes;
    }

    internal void Prepare(string path, FileStamp? before, byte[]? original, FileStamp after, CancellationToken token)
        => Append(new(FormatVersion, path, original, before, after), token);

    internal void PrepareRename(string path, string destination, FileStamp before, byte[] original, CancellationToken token)
        => Append(new(RenameVersion, path, original, before, null, destination), token);

    private void Append(Mutation mutation, CancellationToken token)
    {
        if (_next >= MaxEntries)
            throw new ToolDeniedException("checkpoint_limit", "本轮写入次数超过可恢复范围。");
        if (_tracked.Contains(mutation.Path))
            mutation = mutation with { Original = null };
        if (mutation.Original is not null && _credential.Length > 0 && mutation.Original.AsSpan().IndexOf(_credential) >= 0)
            throw new ToolDeniedException("checkpoint_credential", "文件包含当前模型凭据，拒绝写入检查点。");
        using var folder = OpenRunDirectory(create: true)!;
        var checkpoints = new WindowsWorkspace(folder.AbsolutePath);
        using var record = checkpoints.OpenPath(RecordName(_next));
        Save(record.AbsolutePath, JsonSerializer.SerializeToUtf8Bytes(mutation), token);
        _next++;
        _tracked.Add(mutation.Path);
        if (mutation.Destination is not null)
            _tracked.Add(mutation.Destination);
    }

    public static RestoreResult RestoreInactive(string workspace, string id, CancellationToken token)
    {
        var checkpoint = new WorkspaceCheckpoint(workspace, id);
        _ = new WindowsWorkspace(checkpoint._root);
        var stateDir = Path.Combine(checkpoint._root, ".research");
        if (!Directory.Exists(stateDir))
            return new("run_not_found", [], [], []);
        var state = new WindowsWorkspace(stateDir);
        using var logPath = state.OpenPath($"csharp-{id}.jsonl");
        if (!WindowsWorkspace.FileExists(logPath.AbsolutePath))
            return new("run_not_found", [], [], []);
        // The writer has write access for the run's entire lifetime. This excludes a live writer.
        using var log = OpenInactiveLog(logPath.AbsolutePath);
        WindowsWorkspace.GetFileIdentity(log.SafeFileHandle, logPath.AbsolutePath);
        return checkpoint.Restore(token);
    }

    private static FileStream OpenInactiveLog(string path)
    {
        try { return File.Open(path, FileMode.Open, FileAccess.Read, FileShare.Read); }
        catch (IOException ex) when ((ex.HResult & 0xFFFF) is 32 or 33)
        {
            throw new ToolDeniedException("run_active", "运行仍在写入，结束后才能恢复文件。");
        }
    }

    public RestoreResult Restore(CancellationToken token)
    {
        var workspace = new WindowsWorkspace(_root);
        var restored = new List<string>();
        var deleted = new List<string>();
        var failed = new Dictionary<string, string>();
        using var folder = OpenRunDirectory(create: false);
        if (folder is null)
            return new("no_checkpoint", [], [], failed);

        var records = ReadRecords(folder.AbsolutePath, token);
        var grouped = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var group in RenameGroups(records))
        {
            token.ThrowIfCancellationRequested();
            grouped.UnionWith(group);
            RestoreRenameGroup(workspace, records.Where(entry => group.Contains(entry.Path)), restored, deleted, failed, token);
        }
        foreach (var file in records.Where(entry => !grouped.Contains(entry.Path))
                     .GroupBy(entry => entry.Path, StringComparer.OrdinalIgnoreCase))
        {
            token.ThrowIfCancellationRequested();
            try
            {
                var history = file.ToArray();
                using var target = workspace.OpenPath(file.Key);
                var original = history[0].Original;
                if (!WindowsWorkspace.FileExists(target.AbsolutePath))
                {
                    if (original is not null)
                        failed.Add(file.Key, "target_missing");
                    continue;
                }

                using var handle = WindowsWorkspace.OpenForRecovery(target.AbsolutePath, delete: original is null);
                var identity = WindowsWorkspace.GetFileIdentity(handle, target.AbsolutePath);
                using var stream = new FileStream(handle, original is null ? FileAccess.Read : FileAccess.ReadWrite);
                if (stream.Length > MaxBytes)
                {
                    failed.Add(file.Key, "edit_conflict");
                    continue;
                }
                var bytes = new byte[(int)stream.Length];
                stream.ReadExactly(bytes);
                if (original is not null && bytes.AsSpan().SequenceEqual(original))
                    continue;
                var stamp = Stamp(identity, bytes);
                if (history.Skip(1).Zip(history).Any(pair => pair.First.Before != pair.Second.After) ||
                    !history.Any(entry => entry.After == stamp))
                {
                    failed.Add(file.Key, "edit_conflict");
                    continue;
                }

                if (original is null)
                {
                    WindowsWorkspace.DeleteOnClose(handle);
                    deleted.Add(file.Key);
                }
                else
                {
                    stream.Position = 0;
                    stream.Write(original);
                    stream.SetLength(original.Length);
                    stream.Flush(flushToDisk: true);
                    restored.Add(file.Key);
                }
            }
            catch (ToolDeniedException ex)
            {
                failed.Add(file.Key, ex.Status);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                failed.Add(file.Key, "restore_io_error");
            }
        }
        return new(failed.Count == 0 ? "restored" : "restore_conflict", restored.ToArray(), deleted.ToArray(), failed);
    }

    private WindowsWorkspace.Lease? OpenRunDirectory(bool create)
    {
        var statePath = Path.Combine(_root, ".research");
        if (!Directory.Exists(statePath) && !create)
            return null;
        // The CLI creates the validated state root before starting an Agent run.
        var state = new WindowsWorkspace(statePath);
        using var basePath = state.OpenPath("csharp-checkpoints");
        if (create)
            Directory.CreateDirectory(basePath.AbsolutePath);
        if (!Directory.Exists(basePath.AbsolutePath))
            return null;

        var folder = new WindowsWorkspace(basePath.AbsolutePath).OpenPath(_runId);
        try
        {
            if (create)
                Directory.CreateDirectory(folder.AbsolutePath);
            if (Directory.Exists(folder.AbsolutePath))
                return folder;
        }
        catch
        {
            folder.Dispose();
            throw;
        }
        folder.Dispose();
        return null;
    }

    private static List<Mutation> ReadRecords(string folder, CancellationToken token)
    {
        var source = new WindowsWorkspace(folder);
        var names = Directory.EnumerateFiles(folder, "*.json").Take(MaxEntries + 1)
            .Select(Path.GetFileName).Order(StringComparer.Ordinal).ToArray();
        if (names.Length > MaxEntries)
            throw new InvalidDataException("检查点超出记录数上限。");

        var records = new List<Mutation>(names.Length);
        var tracked = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        for (var i = 0; i < names.Length; i++)
        {
            token.ThrowIfCancellationRequested();
            if (names[i] != RecordName(i))
                throw new InvalidDataException("检查点序列缺失或名称无效。");
            using var path = source.OpenPath(names[i]!);
            using var stream = File.Open(path.AbsolutePath, FileMode.Open, FileAccess.Read, FileShare.Read);
            WindowsWorkspace.GetFileIdentity(stream.SafeFileHandle, path.AbsolutePath);
            if (stream.Length > 2 * MaxBytes)
                throw new InvalidDataException("单条检查点超过大小限制。");
            var entry = JsonSerializer.Deserialize<Mutation>(stream);
            if (entry is not { Path.Length: > 0 } || entry.Original is { Length: > MaxBytes } ||
                (entry.Before is { } before && before.ContentHash is not { Length: 64 }) ||
                !IsCheckpointPath(entry.Path) ||
                !(entry is { Version: FormatVersion, After.ContentHash.Length: 64, Destination: null } ||
                  entry is { Version: RenameVersion, After: null, Before.ContentHash.Length: 64, Destination.Length: > 0 } &&
                  IsCheckpointPath(entry.Destination) && !string.Equals(entry.Path, entry.Destination, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("检查点数据无效或版本不受支持。");
            foreach (var change in Expand(entry))
            {
                var first = tracked.Add(change.Path);
                if ((first && change.Original is null && change.Before is not null) ||
                    (change.Original is not null && (!first ||
                        Convert.ToHexString(SHA256.HashData(change.Original)) != change.Before?.ContentHash)))
                    throw new InvalidDataException("检查点原始内容校验失败。");
            }
            records.Add(entry);
        }
        return records;
    }

    private static bool IsCheckpointPath(string path) =>
        WorkspaceDocuments.IsVisible(path) && !path.Contains('\\');

    private static IEnumerable<Mutation> Expand(Mutation entry)
    {
        yield return entry;
        if (entry.Destination is { } destination)
            yield return new(FormatVersion, destination, null, null, entry.Before);
    }

    private static string RecordName(int index) => FormattableString.Invariant($"{index:D6}.json");
}
