using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceFiles;

namespace SciDev.Infrastructure;

public sealed partial class WorkspaceCheckpoint
{
    // Connected names share a recovery boundary, including intermediate names in a chain.
    private static IEnumerable<HashSet<string>> RenameGroups(IEnumerable<Mutation> records)
    {
        var groups = new Dictionary<string, HashSet<string>>(StringComparer.OrdinalIgnoreCase);
        foreach (var record in records)
        {
            if (record.Destination is not { } destination)
                continue;
            if (!groups.TryGetValue(record.Path, out var sourceGroup))
                sourceGroup = new(StringComparer.OrdinalIgnoreCase) { record.Path };
            if (groups.TryGetValue(destination, out var targetGroup) && targetGroup != sourceGroup)
            {
                sourceGroup.UnionWith(targetGroup);
                foreach (var path in targetGroup)
                    groups[path] = sourceGroup;
            }
            sourceGroup.Add(destination);
            groups[record.Path] = groups[destination] = sourceGroup;
        }
        return groups.Values.Distinct();
    }

    private static void RestoreRenameGroup(WindowsWorkspace workspace, IEnumerable<Mutation> history,
        List<string> restored, List<string> deleted, Dictionary<string, string> failed, CancellationToken token)
    {
        var records = history.ToArray();
        var files = new Dictionary<string, RecoveryFile>(StringComparer.OrdinalIgnoreCase);
        var originals = new Dictionary<string, Mutation>(StringComparer.OrdinalIgnoreCase);
        foreach (var change in records.SelectMany(Expand))
            originals.TryAdd(change.Path, change);
        try
        {
            foreach (var change in originals.Values)
            {
                token.ThrowIfCancellationRequested();
                var file = new RecoveryFile(workspace.OpenPath(change.Path), change.Before, change.Original);
                files.Add(change.Path, file);
                file.Open();
            }

            if (!MatchesHistory(files, records))
                throw new ToolDeniedException("edit_conflict", "重命名涉及的文件与检查点不符。");

            if (records is [{ Destination: { } destination } rename] &&
                files[rename.Path].Current is null && files[destination].Current?.Stamp == rename.Before)
            {
                var source = files[rename.Path].Path;
                WindowsWorkspace.RenameWithoutReplace(files[destination].Current!.Stream.SafeFileHandle, source.AbsolutePath);
                restored.Add(rename.Path);
                deleted.Add(destination);
                return;
            }

            // Restore existing-at-start names before deleting any task-created names. A retry can
            // recognize the restored bytes after an error or an interruption between names.
            foreach (var (path, file) in files)
            {
                token.ThrowIfCancellationRequested();
                if (file.Original is null || file.HasOriginal)
                    continue;
                file.RestoreOriginal(token);
                restored.Add(path);
            }
            foreach (var (path, file) in files)
            {
                token.ThrowIfCancellationRequested();
                if (file.Original is not null || file.Current is null)
                    continue;
                WindowsWorkspace.DeleteOnClose(file.Current.Stream.SafeFileHandle);
                file.Current.Dispose();
                deleted.Add(path);
            }
        }
        catch (ToolDeniedException ex)
        {
            foreach (var path in originals.Keys)
                failed[path] = ex.Status;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            foreach (var path in originals.Keys)
                failed[path] = "restore_io_error";
        }
        finally
        {
            foreach (var file in files.Values)
                file.Dispose();
        }
    }

    private static bool MatchesHistory(Dictionary<string, RecoveryFile> files, Mutation[] records)
    {
        var expected = files.ToDictionary(pair => pair.Key, pair => pair.Value.Initial, StringComparer.OrdinalIgnoreCase);
        var originalsRestored = files.Values.Where(file => file.Original is not null).All(file => file.HasOriginal);
        bool Matches() => files.All(pair => pair.Value.Current?.Stamp == expected[pair.Key] ||
            (pair.Value.Original is not null ? pair.Value.HasOriginal : originalsRestored && pair.Value.Current is null));

        var matched = Matches();
        foreach (var record in records)
        {
            // A move's two effects become visible together. Check only whole operation boundaries.
            foreach (var change in Expand(record))
            {
                if (expected[change.Path] != change.Before)
                    return false;
                expected[change.Path] = change.After;
            }
            matched |= Matches();
        }
        return matched;
    }

    private sealed class RecoveryFile(WindowsWorkspace.Lease path, FileStamp? initial, byte[]? original) : IDisposable
    {
        public WindowsWorkspace.Lease Path { get; } = path;
        public FileStamp? Initial { get; } = initial;
        public byte[]? Original { get; } = original;
        public Snapshot? Current { get; private set; }
        public bool HasOriginal => Original is not null && Current is not null && Current.Bytes.AsSpan().SequenceEqual(Original);

        public void Open()
        {
            if (!WindowsWorkspace.FileExists(Path.AbsolutePath))
                return;
            Current = ReadSnapshot(WindowsWorkspace.OpenForRecovery(Path.AbsolutePath, delete: Original is null),
                Path.AbsolutePath, Original is null ? FileAccess.Read : FileAccess.ReadWrite, MaxBytes, allowBinary: true);
        }

        public void RestoreOriginal(CancellationToken token)
        {
            if (Current is { } opened)
            {
                opened.Stream.Position = 0;
                opened.Stream.Write(Original!);
                opened.Stream.SetLength(Original!.Length);
                opened.Stream.Flush(flushToDisk: true);
                return;
            }
            var saved = Save(Path.AbsolutePath, Original!, token);
            Open();
            if (Current?.Stamp != saved)
                throw new ToolDeniedException("edit_conflict", "还原后的源文件已发生变化。");
        }

        public void Dispose()
        {
            Current?.Dispose();
            Path.Dispose();
        }
    }
}
