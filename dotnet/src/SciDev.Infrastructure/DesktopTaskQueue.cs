using System.ComponentModel;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed class DesktopTaskQueue(string workspace)
{
    private const string FileName = "desktop-queue.json";
    private const int MaxTasks = 16;
    private const int MaxPromptLength = 16_384;
    private const int MaxBytes = 2 * 1024 * 1024;
    private static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

    public sealed record Entry(string Id, string Prompt, bool Started = false, bool AutoCommit = false, int ImageCount = 0);
    private sealed record QueueFile(int Version, Entry[] Entries);

    public Entry[] Load()
    {
        _ = new WindowsWorkspace(workspace);
        var folder = Path.Combine(workspace, ".research");
        if (!Directory.Exists(folder))
            return [];
        using var file = new WindowsWorkspace(folder).OpenPath(FileName);
        return Read(file.AbsolutePath).Entries;
    }

    public Entry[] Add(Entry entry) => Edit(entries =>
    {
        if (entries.Length >= MaxTasks)
            throw new ToolDeniedException("queue_full", "队列已满，请先处理或移除任务。");
        if (entry.Started || !Valid(entry) || entries.Any(item => item.Id == entry.Id))
            throw new ToolDeniedException("queue_entry", "任务格式无效或提示词超过 16,384 字符。");
        return [..entries, entry];
    });

    public Entry[] Start(Entry entry) => Edit(entries =>
    {
        if (entry.Started || entries.FirstOrDefault(item => !item.Started) != entry)
            throw QueueChanged();
        return entries.Select(item => item == entry ? item with { Started = true } : item).ToArray();
    });

    public Entry[] Remove(Entry entry) => Edit(entries =>
    {
        if (!entries.Contains(entry))
            throw QueueChanged();
        return entries.Where(item => item != entry).ToArray();
    });

    private Entry[] Edit(Func<Entry[], Entry[]> change)
    {
        var state = new WindowsWorkspace(workspace).EnsureStateDirectory();
        using var file = state.OpenPath(FileName);
        using var lockFile = state.OpenPath("desktop-queue.lock");
        using var gate = Acquire(lockFile.AbsolutePath);
        WindowsWorkspace.GetFileIdentity(gate, lockFile.AbsolutePath);
        var (entries, expected) = Read(file.AbsolutePath);
        var updated = change(entries);
        // A pre-attachment build must refuse a queue whose tasks need reattachment.
        var version = updated.Any(item => item.ImageCount > 0) ? 2 : 1;
        var bytes = JsonSerializer.SerializeToUtf8Bytes(new QueueFile(version, updated), Json);
        WorkspaceFiles.Save(file.AbsolutePath, bytes, CancellationToken.None, expected, maxCurrentBytes: MaxBytes);
        return updated;
    }

    private static SafeFileHandle Acquire(string path)
    {
        try
        {
            return WindowsWorkspace.OpenForRead(path, FileShare.None);
        }
        catch (IOException ex) when (ex.InnerException is Win32Exception { NativeErrorCode: 2 })
        {
            try { return File.OpenHandle(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None); }
            catch (IOException conflict) when ((conflict.HResult & 0xFFFF) is 32 or 80 or 183)
            {
                throw QueueChanged();
            }
        }
        catch (IOException ex) when (ex.InnerException is Win32Exception { NativeErrorCode: 32 })
        {
            throw QueueChanged();
        }
    }

    private static (Entry[] Entries, WorkspaceFiles.FileStamp? Stamp) Read(string file)
    {
        if (!WindowsWorkspace.FileExists(file))
            return ([], null);
        using var source = WorkspaceFiles.OpenSnapshot(file, FileShare.Read | FileShare.Delete, maxBytes: MaxBytes);
        try
        {
            var stored = JsonSerializer.Deserialize<QueueFile>(source.Bytes, Json);
            if (stored is not { Version: 1 or 2, Entries.Length: <= MaxTasks } ||
                stored.Entries.Any(item => item is null || !Valid(item)) ||
                (stored.Version == 1 && stored.Entries.Any(item => item.ImageCount != 0)) ||
                stored.Entries.Select(item => item.Id).Distinct(StringComparer.Ordinal).Count() != stored.Entries.Length)
                throw Invalid();
            return (stored.Entries, source.Stamp);
        }
        catch (JsonException) { throw Invalid(); }
    }

    private static bool Valid(Entry item) => item.Id is not null && RunInspection.IsRunId(item.Id) &&
        item.Prompt is { Length: <= MaxPromptLength } && !string.IsNullOrWhiteSpace(item.Prompt) &&
        item.ImageCount is >= 0 and <= ImageAttachment.MaxImages;

    private static ToolDeniedException Invalid() => new("queue_invalid", "无法识别工作区队列，请保留原文件。");
    private static ToolDeniedException QueueChanged() => new("queue_changed", "任务队列正被使用或已有变化，请重试。");
}
